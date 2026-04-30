"""LLM-driven enrichment of reference metadata.

Per source, asks Claude for:
  - a 2-3 sentence summary of what the paper is about
  - 3-5 bullet key findings
  - the source's standing / influence in its field (with explicit
    confidence flag, since the model can hallucinate citation counts)
  - per-claim explanation of what role each citation plays in *this*
    paper (the user's draft) — distinct from the source's own
    contribution

The enrichment is persisted to ``.lattice/reference_enrichment.json``
keyed by source_id so we don't repay the LLM cost on every page load.
The user can click "Refresh with AI" to re-enrich.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ..graph.models import AuthorGraph, Source


class _LLMProtocol(Protocol):
    async def complete_json(
        self,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> tuple[Any, Any]: ...


_SYSTEM_PROMPT = """You are enriching a reference card with structured analysis. You receive:
  1. A reference's citation metadata (authors, year, title, journal).
  2. A short excerpt from the source paper (if available).
  3. The user's project context (their thesis + the claims that cite this source).

Output ONE JSON object with these fields:
- `summary`: 2-3 sentence prose summary of what the source paper is about. Stay close to its actual contribution; do not project the user's framing onto it.
- `key_findings`: array of 3-5 short strings (max ~15 words each) — the source's headline findings or contributions.
- `field_position`: 1-2 sentence prose statement on the source's standing in its field (seminal / well-cited / niche / disputed / superseded / unknown). Be honest: if you don't recognise the work, say `"Unknown — verify via external citation databases."`
- `citation_count_estimate`: integer estimate of how many times this paper has been cited (rough order of magnitude is fine), or null if you don't know. Anchor to your training data — do not guess if uncertain.
- `confidence`: one of `"high"` (work is widely-cited and you recognise it), `"medium"` (you have some training-data signal), `"low"` (model recall is shaky), `"unknown"` (no signal).
- `usage_purposes`: array of objects, one per cited claim, each `{"claim_id": "cl.x.y", "role": "primary_evidence|supporting_context|counterpoint|definition|method|background", "explanation": "1 sentence on what the citation does in *the user's* argument"}`. Only include claims listed in the input.

Rules:
1. Output ONLY a JSON object, no prose, no fences, no preamble.
2. If the citation metadata is sparse, do your best with what you have but mark `confidence: "low"`.
3. Do not invent findings the source doesn't actually make.
4. Citation counts must be integers (no "around 1000+", no "many").
"""


def _build_user_prompt(
    source: Source,
    claims_using: list[dict[str, Any]],
    user_thesis: str | None,
) -> str:
    citation = source.citation
    payload: dict[str, Any] = {
        "source_id": source.source_id,
        "citation": {
            "authors": citation.authors,
            "year": citation.year,
            "title": citation.title,
            "container": citation.container,
            "doi": citation.doi,
        },
        "user_project": {
            "thesis": user_thesis,
            "claims_citing_this_source": claims_using,
        },
    }
    if source.passages:
        excerpt = " ".join(p.text for p in source.passages[:2])[:1200].strip()
        if excerpt:
            payload["source_excerpt"] = excerpt
    return (
        "Enrich this reference. Output a single JSON object as specified.\n\n"
        f"{json.dumps(payload, indent=2)}"
    )


def _coerce_enrichment(row: Any) -> dict[str, Any] | None:
    """Validate the LLM's response into a clean record. Returns None
    if the row is unusable."""
    if not isinstance(row, dict):
        return None
    summary = str(row.get("summary") or "").strip()
    if not summary:
        return None
    findings = row.get("key_findings") or []
    if not isinstance(findings, list):
        findings = []
    findings = [str(f).strip()[:200] for f in findings if isinstance(f, str) and f.strip()]
    field_position = str(row.get("field_position") or "").strip()
    citation_count = row.get("citation_count_estimate")
    if citation_count is not None:
        try:
            citation_count = int(citation_count)
            if citation_count < 0 or citation_count > 10_000_000:
                citation_count = None
        except (TypeError, ValueError):
            citation_count = None
    confidence = str(row.get("confidence") or "unknown").lower()
    if confidence not in ("high", "medium", "low", "unknown"):
        confidence = "unknown"

    purposes_raw = row.get("usage_purposes") or []
    if not isinstance(purposes_raw, list):
        purposes_raw = []
    purposes: list[dict[str, str]] = []
    valid_roles = {
        "primary_evidence", "supporting_context", "counterpoint",
        "definition", "method", "background",
    }
    for p in purposes_raw:
        if not isinstance(p, dict):
            continue
        cid = str(p.get("claim_id") or "").strip()
        if not cid:
            continue
        role = str(p.get("role") or "supporting_context").strip()
        if role not in valid_roles:
            role = "supporting_context"
        explanation = str(p.get("explanation") or "").strip()[:300]
        purposes.append({
            "claim_id": cid,
            "role": role,
            "explanation": explanation,
        })

    return {
        "summary": summary[:1000],
        "key_findings": findings[:8],
        "field_position": field_position[:400],
        "citation_count_estimate": citation_count,
        "confidence": confidence,
        "usage_purposes": purposes,
    }


def _build_claims_using(
    source_id: str, graph: AuthorGraph
) -> list[dict[str, Any]]:
    """For each claim that has an Evidence binding to this source,
    return a small dict the LLM can read to figure out the citation's
    role."""
    rows: list[dict[str, Any]] = []
    for claim in graph.claims:
        for ev in claim.evidence:
            if ev.source != source_id:
                continue
            rows.append({
                "claim_id": claim.claim_id,
                "claim_statement": (claim.statement or "")[:280],
                "binding_strength": (
                    ev.binding_strength.value
                    if hasattr(ev.binding_strength, "value")
                    else str(ev.binding_strength)
                ),
                "quote": (ev.quote_text or "")[:200] or None,
            })
    return rows


class EnrichmentError(Exception):
    """Wraps the underlying failure with a human-friendly explanation."""
    pass


async def enrich_one_source(
    source: Source,
    graph: AuthorGraph,
    llm: _LLMProtocol,
) -> dict[str, Any]:
    """Enrich a single source. Returns the parsed enrichment dict on
    success. Raises ``EnrichmentError`` with a useful message on any
    failure (malformed JSON, missing summary, LLM exception)."""
    claims_using = _build_claims_using(source.source_id, graph)
    user = _build_user_prompt(source, claims_using, graph.thesis_statement)
    try:
        data, _resp = await llm.complete_json(
            system=_SYSTEM_PROMPT,
            user=user,
        )
    except Exception as exc:  # noqa: BLE001
        raise EnrichmentError(
            f"LLM call failed: {type(exc).__name__}: {exc}"
        ) from exc

    if data is None:
        raise EnrichmentError("LLM returned no JSON output")

    enrichment = _coerce_enrichment(data)
    if enrichment is None:
        # Useful diagnostic — show the user what shape we got back
        # so they can tell whether Claude hallucinated, refused, or
        # returned a structurally wrong payload.
        try:
            preview = json.dumps(data)[:160]
        except (TypeError, ValueError):
            preview = repr(data)[:160]
        raise EnrichmentError(
            "LLM response missing required `summary` field. "
            f"Got: {preview}"
        )
    enrichment["enriched_at"] = datetime.now(timezone.utc).isoformat()
    return enrichment


async def enrich_all_references(
    sources: list[Source],
    graph: AuthorGraph,
    llm: _LLMProtocol,
    cited_only: bool = True,
    progress=None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Enrich every source (or just those with at least one Evidence
    binding) in parallel. Returns ``(enrichments, errors)``:

    - ``enrichments``: ``{source_id: enrichment_dict}`` for successful sources
    - ``errors``: ``{source_id: error_message}`` for everything that failed,
      with the actual exception message — surfaced to the UI so the user
      can see WHY enrichment failed instead of a silent zero.
    """
    cited_ids: set[str] = set()
    if cited_only:
        for claim in graph.claims:
            for ev in claim.evidence:
                if ev.source:
                    cited_ids.add(ev.source)

    targets = [
        s for s in sources
        if not cited_only or s.source_id in cited_ids
    ]
    if progress is not None:
        progress.begin(
            "ai_enrich_refs", total=len(targets),
            status=f"asking Claude about {len(targets)} reference(s)",
        )

    async def _safely_enrich(s: Source) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            result = await enrich_one_source(s, graph, llm)
            err = None
        except EnrichmentError as exc:
            result = None
            err = str(exc)
        except Exception as exc:  # noqa: BLE001
            result = None
            err = f"unexpected {type(exc).__name__}: {exc}"
        if progress is not None:
            progress.advance("ai_enrich_refs", status=s.source_id)
        return s.source_id, result, err

    triples = await asyncio.gather(*(_safely_enrich(s) for s in targets))
    out: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for sid, enrichment, err in triples:
        if enrichment is not None:
            out[sid] = enrichment
        elif err:
            errors[sid] = err

    if progress is not None:
        progress.end(
            "ai_enrich_refs",
            status=f"{len(out)} of {len(targets)} enriched · {len(errors)} failed",
        )
    return out, errors


def load_enrichment(project_path: Path) -> dict[str, dict[str, Any]]:
    """Load persisted enrichment data. Returns ``{}`` if missing or
    corrupt."""
    target = project_path / ".lattice" / "reference_enrichment.json"
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {}


def save_enrichment(
    project_path: Path, enrichment: dict[str, dict[str, Any]]
) -> Path:
    """Persist enrichment data, merging with any existing entries so
    a partial refresh doesn't blow away earlier results."""
    target = project_path / ".lattice" / "reference_enrichment.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = load_enrichment(project_path)
    existing.update(enrichment)
    target.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return target
