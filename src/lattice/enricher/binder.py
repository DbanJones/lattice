"""Enricher: bind author claims to source passages.

For each (claim, cited-source) pair, ask the LLM to pick the best-matching
passage and the binding strength. Never adds or removes claims; only
updates Evidence entries on existing claims.

See docs/PROMPTS.md "Stage 2: Enricher".
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Protocol

from ..graph.models import BindingStrength, Claim, Evidence, Source
from ..graph.store import GraphStore
from ..utils.config import Config


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

        passages_xml = "\n".join(
            f'<passage id="{p.id}" page="{p.location.page or ""}">{p.text[:1500]}</passage>'
            for p in source.passages[:40]  # cap to keep the call cheap
        )
        citation = _format_citation(source)
        user_msg = (
            f"Author's claim: <claim>{claim.statement}</claim>\n\n"
            f"Source: {citation}\n"
            f"Available passages from this source:\n\n"
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

        return _evidence_from_payload(payload, source)


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


def _evidence_from_payload(payload: object, source: Source) -> Evidence:
    if not isinstance(payload, dict):
        return Evidence(
            source=source.source_id,
            passage="",
            binding_strength=BindingStrength.none_,
        )
    raw_strength = str(payload.get("binding_strength") or "none").lower()
    strength = _BINDING_MAP.get(raw_strength, BindingStrength.none_)
    passage_id = str(payload.get("best_passage_id") or "")
    # Defensive: confirm the passage_id actually exists on the source.
    if passage_id and not any(p.id == passage_id for p in source.passages):
        passage_id = ""
        if strength != BindingStrength.contradictory:
            strength = BindingStrength.none_

    quote = payload.get("extracted_quote")
    page_val = payload.get("page")
    page: int | None = None
    if isinstance(page_val, int):
        page = page_val
    elif isinstance(page_val, str) and page_val.isdigit():
        page = int(page_val)

    return Evidence(
        source=source.source_id,
        passage=passage_id,
        binding_strength=strength,
        quote_verbatim=bool(quote),
        quote_text=str(quote) if quote else None,
        page=page,
    )
