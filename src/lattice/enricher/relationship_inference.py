"""LLM-driven inference of relationships between claims.

The markdown ingester only creates relationships when the user
explicitly tags them (``- claim text [supports: cl.x.1]``). Most
auto-outlined projects therefore have a flat graph with zero
relationships, which makes the argument-graph view useless.

This module asks Claude to read the structured outline (sections +
claims) and propose ``RelationshipType`` edges. Output is persisted
back onto the ``AuthorGraph`` and surfaced in the Outline tab.

Runs as a new pipeline stage in Standard + Deep reviews. The stage is
deterministic in its prompt + parsing — invalid output is dropped, not
guessed at.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol

from ..graph.models import (
    AuthorGraph,
    Relationship,
    RelationshipStrength,
    RelationshipType,
)


class _LLMProtocol(Protocol):
    async def complete_json(
        self,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> tuple[Any, Any]: ...


_VALID_TYPES = {t.value for t in RelationshipType}
_VALID_STRENGTHS = {s.value for s in RelationshipStrength}


_SYSTEM_PROMPT = """You analyse the argument structure of an academic outline and propose relationships between claims.

You receive one focused section of a paper plus its sibling-section context, and emit a JSON array of relationships involving the claims in that focused section.

Available relationship types (be conservative — only emit a type when the relationship is clear):
- `supports`         — claim A provides evidence or reasoning that strengthens claim B
- `contradicts`      — claim A denies or rebuts claim B (use sparingly)
- `qualifies`        — claim A adds a boundary condition or scope to claim B
- `extends`          — claim A elaborates / builds further on claim B
- `depends_on`       — claim A's truth requires claim B's truth
- `is_counterexample_to` — claim A is a specific case that breaks claim B
- `is_evidence_for`  — claim A is a piece of evidence for claim B (lower-level than `supports`)
- `interpretive_pivot` — claim A reframes the question claim B was answering
- `unlabelled`       — there is a clear connection but no other type fits

Available strengths:
- `direct`   — explicit / unambiguous
- `partial`  — clear but with caveats
- `inferred` — implied by context, not stated

Output rules:
1. Output ONLY a JSON array. No prose, no fences.
2. Each entry: `{"from": "cl.x.y", "to": "cl.x.z", "type": "<one of above>", "strength": "<direct|partial|inferred>", "note": "<one short sentence>"}`
3. Do not invent claim ids — only use ids that appear in the input.
4. Do not emit self-relationships (`from` == `to`).
5. **Density target: most claims should appear at least once as `from` or `to`.** A typical academic paragraph claim has 1-3 analytical neighbours (the previous claim it builds on, the next claim it sets up, an external claim it supports/contradicts). Don't pad with weak links, but don't artificially under-report either.
6. Prioritise:
   - sequential claims inside this section that build on each other
   - claims in this section that support / extend / qualify the thesis
   - cross-section links: claims here that connect to the listed sibling sections
   - the section's concluding / synthesis claims linking back to its setup claims
7. If two claims merely sit next to each other without a clear analytical link, do not emit a relationship.

Example output:
[
  {"from": "cl.a.1", "to": "cl.thesis", "type": "supports", "strength": "direct", "note": "Establishes the formalist position the thesis rejects."},
  {"from": "cl.c.2", "to": "cl.c.1", "type": "extends", "strength": "direct", "note": "Quantifies the recovery effect introduced in the previous claim."}
]
"""


def _build_section_prompt(
    graph: AuthorGraph,
    focus_section_id: str,
) -> str:
    """Compact prompt: full focus-section claims + sibling-section
    summaries (titles + claim ids only) so cross-section links remain
    addressable without ballooning the prompt."""
    claims_by_id = {c.claim_id: c for c in graph.claims}
    sections_by_id = {s.section_id: s for s in graph.sections}

    focus = sections_by_id.get(focus_section_id)
    if focus is None:
        return ""

    focus_claims = [
        {
            "claim_id": cid,
            "statement": claims_by_id[cid].statement if cid in claims_by_id else "",
        }
        for cid in focus.claim_ids
    ]

    # Sibling sections: titles + claim ids + first 80 chars of each
    # claim. Enough for the LLM to reach cross-section, without sending
    # the full text of every other section's claims.
    siblings: list[dict[str, Any]] = []
    for s in graph.sections:
        if s.section_id == focus_section_id:
            continue
        siblings.append({
            "section_id": s.section_id,
            "title": s.title,
            "claims": [
                {
                    "claim_id": cid,
                    "statement": (
                        claims_by_id[cid].statement[:120]
                        if cid in claims_by_id else ""
                    ),
                }
                for cid in s.claim_ids
            ],
        })

    payload = {
        "thesis_statement": graph.thesis_statement,
        "focus_section": {
            "section_id": focus.section_id,
            "title": focus.title,
            "role": focus.role.value if hasattr(focus.role, "value") else str(focus.role),
            "claims": focus_claims,
        },
        "sibling_sections": siblings,
    }
    return (
        "Propose relationships involving claims in the FOCUS section. "
        "You may also link them to claims in sibling sections when there "
        "is a clear analytical connection. Output a single JSON array.\n\n"
        f"{json.dumps(payload, indent=2)}"
    )


async def infer_relationships(
    graph: AuthorGraph, llm: _LLMProtocol
) -> list[Relationship]:
    """Ask Claude to propose relationships per-section in parallel,
    then merge into a deduplicated list.

    Per-section chunking gives each section its own output budget so
    a 300-claim paper isn't squeezed into one ~30-relationship response.
    Invalid entries (unknown ids, unknown types) are silently dropped.
    """
    if not graph.claims:
        return []

    import asyncio as _asyncio

    valid_ids = {c.claim_id for c in graph.claims}
    now = datetime.now(timezone.utc)

    # Skip sections with no claims and reference-only sections.
    eligible_sections = [
        s for s in graph.sections
        if s.claim_ids
        and s.section_id != "s.thesis"
        and (
            s.role.value if hasattr(s.role, "value") else str(s.role)
        ) != "references"
    ]
    if not eligible_sections:
        return []

    async def per_section(section_id: str) -> list[dict[str, Any]]:
        prompt = _build_section_prompt(graph, section_id)
        if not prompt:
            return []
        try:
            data, _ = await llm.complete_json(
                system=_SYSTEM_PROMPT,
                user=prompt,
            )
        except Exception:
            return []
        return data if isinstance(data, list) else []

    batches = await _asyncio.gather(
        *[per_section(s.section_id) for s in eligible_sections]
    )

    relationships: list[Relationship] = []
    seen: set[tuple[str, str, str]] = set()
    for batch in batches:
        for row in batch:
            if not isinstance(row, dict):
                continue
            from_id = row.get("from")
            to_id = row.get("to")
            rtype = row.get("type", "unlabelled")
            rstrength = row.get("strength", "inferred")
            note = (row.get("note") or "").strip()
            if not isinstance(from_id, str) or not isinstance(to_id, str):
                continue
            if from_id not in valid_ids or to_id not in valid_ids:
                continue
            if from_id == to_id:
                continue
            if rtype not in _VALID_TYPES:
                rtype = "unlabelled"
            if rstrength not in _VALID_STRENGTHS:
                rstrength = "inferred"
            key = (from_id, to_id, rtype)
            if key in seen:
                continue
            seen.add(key)
            relationships.append(Relationship(
                rel_id=f"rel.inferred.{len(relationships) + 1}",
                type=RelationshipType(rtype),
                **{"from": from_id},
                to=to_id,
                strength=RelationshipStrength(rstrength),
                note=note[:280],
                created_by="relationship_inference",
                created_at=now,
            ))
    return relationships


def merge_inferred_relationships(
    graph: AuthorGraph, inferred: list[Relationship]
) -> tuple[int, int]:
    """Merge inferred relationships into the graph in-place. Returns
    ``(added, skipped_duplicates)``. Skips inferred relationships if a
    user-authored relationship of the same (from, to, type) already
    exists — author-tagged relationships always win."""
    existing_keys = {
        (r.from_claim, r.to_claim, r.type.value)
        for r in graph.relationships
    }
    added = 0
    skipped = 0
    for rel in inferred:
        key = (rel.from_claim, rel.to_claim, rel.type.value)
        if key in existing_keys:
            skipped += 1
            continue
        graph.relationships.append(rel)
        existing_keys.add(key)
        added += 1
    return added, skipped
