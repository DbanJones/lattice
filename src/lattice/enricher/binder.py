"""Enricher: bind author claims to source passages.

For each (claim, cited-source) pair, ask the LLM to pick the best-matching
passage and the binding strength. Never adds or removes claims; only
updates Evidence entries on existing claims.

Phase 4 changes:
- Passage candidate selection is BM25-ranked, not document-position-ranked.
  The previous head-of-document scan (``passages[:40]``) missed the relevant
  passage whenever it lived past index 40 — common for long sources.
- The bound Evidence carries a passage character span (when the LLM's
  extracted_quote can be located in the passage text) and a numeric
  confidence in [0, 1] derived from the binding strength + retrieval rank.
  Both are optional; old graphs round-trip cleanly.

See docs/PROMPTS.md "Stage 2: Enricher".
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timezone
from typing import Protocol

from ..graph.models import BindingStrength, Claim, Evidence, Passage, Source
from ..graph.store import GraphStore
from ..utils.config import Config


# Cap how many ranked passages we send to the LLM. Higher = more recall,
# higher token cost. 25 is enough for the LLM to disambiguate while
# keeping a single call cheap; the original 40 was a hardcoded prefix
# slice, which is a different failure mode (recall capped by document
# *position*, not by relevance).
_RANKED_PASSAGE_CAP = 25
_BM25_K1 = 1.5
_BM25_B = 0.75


class _LLMProtocol(Protocol):
    async def complete_json(
        self,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> tuple[object, object]:
        ...


_SYSTEM_PROMPT = """\
You determine how strongly a passage supports an author's claim.

Possible bindings:
- strong: the passage directly states what the claim asserts
- weak: the passage partially supports or supports indirectly
- none: no semantic connection
- contradictory: the passage contradicts the claim

Return JSON: {
  "binding_strength": "strong|weak|none|contradictory",
  "best_passage_id": "...",
  "rationale": "one sentence",
  "extracted_quote": "verbatim quote from passage if binding_strength is strong, else null",
  "page": integer or null
}
"""


_BINDING_MAP = {
    "strong": BindingStrength.strong,
    "weak": BindingStrength.weak,
    "none": BindingStrength.none_,
    "contradictory": BindingStrength.contradictory,
}


class Enricher:
    def __init__(
        self, config: Config, store: GraphStore, llm: _LLMProtocol
    ) -> None:
        self.config = config
        self.store = store
        self.llm = llm

    async def enrich_all(self) -> int:
        """Enrich every claim with cited sources. Returns number of claims updated."""
        graph = self.store.get_graph()
        tasks = [self._enrich_one(claim) for claim in graph.claims if claim.evidence]
        results = await asyncio.gather(*tasks)
        updated = [c for c in results if c is not None]
        for claim in updated:
            self.store.save_claim(claim)
        return len(updated)

    async def enrich_claim(self, claim: Claim) -> Claim:
        result = await self._enrich_one(claim)
        if result is not None:
            self.store.save_claim(result)
            return result
        return claim

    # ─── per-claim ─────────────────────────────────────

    async def _enrich_one(self, claim: Claim) -> Claim | None:
        if not claim.evidence:
            return None
        sources_by_id = {s.source_id: s for s in self.store.list_sources()}
        updated_any = False

        # Group evidence by source_id to avoid duplicate calls for the same source.
        seen_sources: set[str] = set()
        new_evidence: list[Evidence] = []

        for ev in claim.evidence:
            if ev.source in seen_sources:
                continue
            seen_sources.add(ev.source)
            source = sources_by_id.get(ev.source)
            if source is None:
                # Unknown source: mark binding none so the author sees the gap.
                new_evidence.append(
                    Evidence(
                        source=ev.source,
                        passage="",
                        binding_strength=BindingStrength.none_,
                        quote_verbatim=False,
                        quote_text="unknown_source: not indexed",
                        page=None,
                    )
                )
                updated_any = True
                continue
            bound = await self._bind_claim_to_source(claim, source)
            new_evidence.append(bound)
            updated_any = True

        if not updated_any:
            return None

        claim_model = claim.model_copy(deep=True)
        claim_model.evidence = new_evidence
        claim_model.modified_at = datetime.now(timezone.utc)
        return claim_model

    async def _bind_claim_to_source(self, claim: Claim, source: Source) -> Evidence:
        if not source.passages:
            return Evidence(
                source=source.source_id,
                passage="",
                binding_strength=BindingStrength.none_,
                quote_verbatim=False,
                quote_text=None,
                page=None,
            )

        # Phase 4: BM25-rank the source's passages by relevance to the
        # claim, then send the top-N to the LLM. The previous code did
        # ``source.passages[:40]`` which silently dropped relevant
        # passages whenever they sat past index 40 in the document.
        ranked = rank_passages_bm25(claim.statement, source.passages, top_n=_RANKED_PASSAGE_CAP)
        passages_xml = "\n".join(
            f'<passage id="{p.id}" page="{p.location.page or ""}">{p.text[:1500]}</passage>'
            for p, _score in ranked
        )
        rank_index = {p.id: i for i, (p, _s) in enumerate(ranked)}
        citation = _format_citation(source)
        user_msg = (
            f"Author's claim: <claim>{claim.statement}</claim>\n\n"
            f"Source: {citation}\n"
            f"Available passages from this source (ranked by lexical "
            f"relevance to the claim; pick whichever best supports it):\n\n"
            f"<passages>\n{passages_xml}\n</passages>\n\n"
            "Determine the best-binding passage and the binding strength."
        )

        model = self.config.model_for_stage("enricher")
        try:
            payload, _resp = await self.llm.complete_json(
                system=_SYSTEM_PROMPT,
                user=user_msg,
                model=model,
                temperature=0.2,
            )
        except Exception as exc:
            return Evidence(
                source=source.source_id,
                passage="",
                binding_strength=BindingStrength.none_,
                quote_verbatim=False,
                quote_text=f"enricher_error: {type(exc).__name__}",
                page=None,
            )

        return _evidence_from_payload(payload, source, rank_index=rank_index)


def _format_citation(source: Source) -> str:
    cit = source.citation
    parts = []
    if cit.authors:
        parts.append(", ".join(cit.authors[:3]) + (" et al." if len(cit.authors) > 3 else ""))
    if cit.year is not None:
        parts.append(f"({cit.year})")
    if cit.title:
        parts.append(cit.title)
    return " ".join(parts) or source.source_id


# ─── BM25 retrieval ────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]+")
_BM25_STOP = frozenset(
    "the a an of in on at to for and or but with by from as is are was "
    "were be been being have has had do does did this that these those "
    "it its their there which who whose what whom how when where why "
    "we our you your they them he she his her i me my".split()
)


def _bm25_tokenise(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")
            if len(t) > 2 and t.lower() not in _BM25_STOP]


def rank_passages_bm25(
    query: str, passages: list[Passage], top_n: int = _RANKED_PASSAGE_CAP,
) -> list[tuple[Passage, float]]:
    """Rank ``passages`` by Okapi BM25 against ``query`` and return the
    top-N ``(passage, score)`` pairs in descending-score order.

    Pure function. Deterministic: identical inputs give identical output.
    Use this instead of any positional slicing — passage ordering in a
    Source reflects document order, which says nothing about how
    relevant the passage is to a given claim.
    """
    if not passages:
        return []
    query_tokens = _bm25_tokenise(query)
    if not query_tokens:
        # Fall back to document order — there's nothing to score on.
        return [(p, 0.0) for p in passages[:top_n]]
    docs = [_bm25_tokenise(p.text) for p in passages]
    doc_lengths = [len(d) for d in docs]
    avgdl = sum(doc_lengths) / max(1, len(doc_lengths))
    n_docs = len(docs)

    # IDF per query term using the BM25+ smoothing variant that never
    # goes negative (Lucene-style).
    df: dict[str, int] = {}
    for d in docs:
        for tok in set(d):
            df[tok] = df.get(tok, 0) + 1
    idf = {
        tok: math.log(1 + (n_docs - df.get(tok, 0) + 0.5) / (df.get(tok, 0) + 0.5))
        for tok in set(query_tokens)
    }

    scored: list[tuple[Passage, float]] = []
    for passage, doc, dl in zip(passages, docs, doc_lengths):
        if not doc:
            scored.append((passage, 0.0))
            continue
        term_counts: dict[str, int] = {}
        for tok in doc:
            term_counts[tok] = term_counts.get(tok, 0) + 1
        score = 0.0
        norm = 1 - _BM25_B + _BM25_B * (dl / avgdl) if avgdl else 1.0
        for tok in set(query_tokens):
            tf = term_counts.get(tok, 0)
            if tf == 0:
                continue
            num = tf * (_BM25_K1 + 1)
            denom = tf + _BM25_K1 * norm
            score += idf.get(tok, 0.0) * (num / denom if denom else 0.0)
        scored.append((passage, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_n]


# ─── confidence + span derivation ──────────────────────

# Confidence floors per binding strength. The retrieval rank lifts
# confidence further (top-1 hit nudges higher), but binding strength
# is the dominant signal because it comes from the LLM's read of the
# passage rather than lexical overlap.
_BASE_CONFIDENCE = {
    BindingStrength.strong: 0.85,
    BindingStrength.weak: 0.55,
    BindingStrength.none_: 0.10,
    BindingStrength.contradictory: 0.0,
}


def _derive_confidence(strength: BindingStrength, rank: int | None) -> float:
    base = _BASE_CONFIDENCE.get(strength, 0.0)
    if strength in (BindingStrength.none_, BindingStrength.contradictory):
        return base
    if rank is None:
        return base
    # Lift the top-3 ranked passages slightly; the further down the
    # ranked list the picked passage was, the less confident we are
    # the LLM had the right context to choose well.
    if rank == 0:
        return min(1.0, base + 0.10)
    if rank == 1:
        return min(1.0, base + 0.05)
    if rank == 2:
        return min(1.0, base + 0.02)
    return base


def _locate_quote_span(passage_text: str, quote: str | None) -> tuple[int | None, int | None]:
    """Find ``quote`` (or a normalised whitespace variant of it) inside
    ``passage_text``. Returns the (char_start, char_end) span or
    (None, None) when the quote cannot be located.

    PDF/OCR text often has line-break weirdness, so we match on both
    raw-quote and a whitespace-normalised version before giving up.
    """
    if not quote or not passage_text:
        return None, None
    if quote in passage_text:
        idx = passage_text.index(quote)
        return idx, idx + len(quote)
    # Whitespace-normalised match.
    norm_quote = re.sub(r"\s+", " ", quote).strip()
    norm_text = re.sub(r"\s+", " ", passage_text)
    if norm_quote and norm_quote in norm_text:
        # Locate inside the normalised text, then translate back to
        # the original by walking the original text.
        norm_idx = norm_text.index(norm_quote)
        return _translate_norm_offset(passage_text, norm_idx, len(norm_quote))
    return None, None


def _translate_norm_offset(
    original: str, norm_start: int, norm_len: int,
) -> tuple[int | None, int | None]:
    """Map an offset from a whitespace-normalised string back to the
    original. Walks the original char-by-char, skipping past
    consecutive whitespace as the normaliser would have."""
    out_start: int | None = None
    out_end: int | None = None
    norm_pos = 0
    i = 0
    while i < len(original):
        ch = original[i]
        if ch.isspace():
            # Collapse any run of whitespace into a single space.
            if norm_pos == norm_start and out_start is None:
                out_start = i
            norm_pos += 1
            j = i + 1
            while j < len(original) and original[j].isspace():
                j += 1
            i = j
        else:
            if norm_pos == norm_start and out_start is None:
                out_start = i
            norm_pos += 1
            i += 1
        if out_start is not None and norm_pos >= norm_start + norm_len:
            out_end = i
            break
    if out_start is not None and out_end is None:
        out_end = len(original)
    return out_start, out_end


def _evidence_from_payload(
    payload: object,
    source: Source,
    *,
    rank_index: dict[str, int] | None = None,
) -> Evidence:
    if not isinstance(payload, dict):
        return Evidence(
            source=source.source_id,
            passage="",
            binding_strength=BindingStrength.none_,
            confidence=0.0,
        )
    raw_strength = str(payload.get("binding_strength") or "none").lower()
    strength = _BINDING_MAP.get(raw_strength, BindingStrength.none_)
    passage_id = str(payload.get("best_passage_id") or "")
    # Defensive: confirm the passage_id actually exists on the source.
    matched_passage: Passage | None = None
    if passage_id:
        for p in source.passages:
            if p.id == passage_id:
                matched_passage = p
                break
        if matched_passage is None:
            passage_id = ""
            if strength != BindingStrength.contradictory:
                strength = BindingStrength.none_

    quote = payload.get("extracted_quote")
    quote_str = str(quote) if quote else None
    page_val = payload.get("page")
    page: int | None = None
    if isinstance(page_val, int):
        page = page_val
    elif isinstance(page_val, str) and page_val.isdigit():
        page = int(page_val)

    char_start: int | None = None
    char_end: int | None = None
    if matched_passage is not None and quote_str:
        char_start, char_end = _locate_quote_span(matched_passage.text, quote_str)

    rank = (rank_index or {}).get(passage_id) if passage_id else None
    confidence = _derive_confidence(strength, rank)

    return Evidence(
        source=source.source_id,
        passage=passage_id,
        binding_strength=strength,
        quote_verbatim=bool(quote),
        quote_text=quote_str,
        page=page,
        passage_char_start=char_start,
        passage_char_end=char_end,
        confidence=confidence,
    )
