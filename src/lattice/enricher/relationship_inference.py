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

You receive a JSON document listing every section and every claim in a paper. Each claim has a stable id (e.g. `cl.a.1`, `cl.b.3`).

Your job: identify pairs of claims where one is in a recognisable analytical relationship to another, and emit a JSON array of these relationships.

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
2. Each entry: `{"from": "cl.x.y", "to": "cl.x.z", "type": "<one of above>", "strength": "<direct|partial|inferred>", "note": "<one short sentence explaining the link>"}`
3. Do not invent claim ids — only use ids that appear in the input.
4. Do not emit self-relationships (`from` == `to`).
5. Aim for the strongest 10-30 relationships across the paper, prioritising:
   - claims that support the thesis claim
   - the thesis claim's relationships to section conclusions
   - sequential claims within a section that build on each other
   - cross-section claims that bear on the same argumentative thread
6. If two claims merely sit next to each other without a clear analytical link, do not emit a relationship.

Example output:
[
  {"from": "cl.a.1", "to": "cl.thesis", "type": "supports", "strength": "direct", "note": "Establishes the formalist position the thesis rejects."},
  {"from": "cl.c.2", "to": "cl.c.1", "type": "extends", "strength": "direct", "note": "Quantifies the recovery effect introduced in the previous claim."}
]
"""


def _build_user_prompt(graph: AuthorGraph) -> str:
    sections_dump: list[dict[str, Any]] = []
    claims_by_id = {c.claim_id: c for c in graph.claims}
    for section in graph.sections:
        sections_dump.append({
            "section_id": section.section_id,
            "title": section.title,
            "role": section.role.value if hasattr(section.role, "value") else str(section.role),
            "claims": [
                {
                    "claim_id": cid,
                    "statement": claims_by_id[cid].statement if cid in claims_by_id else "",
                }
                for cid in section.claim_ids
            ],
        })
    payload = {
        "thesis_statement": graph.thesis_statement,
        "sections": sections_dump,
    }
    return (
        "Analyse the following outline and propose relationships between "
        "the claims. Output a single JSON array.\n\n"
        f"{json.dumps(payload, indent=2)}"
    )


async def infer_relationships(
    graph: AuthorGraph, llm: _LLMProtocol
) -> list[Relationship]:
    """Ask Claude to propose relationships, parse the JSON response,
    and return a list of valid ``Relationship`` objects.

    Invalid entries (unknown ids, unknown types) are silently dropped
    rather than failing the whole call — the LLM occasionally
    hallucinates a single bad row, and we'd rather keep the good ones.
    """
    if not graph.claims:
        return []

    data, _response = await llm.complete_json(
        system=_SYSTEM_PROMPT,
        user=_build_user_prompt(graph),
    )
    if not isinstance(data, list):
        return []

    valid_ids = {c.claim_id for c in graph.claims}
    now = datetime.now(timezone.utc)
    relationships: list[Relationship] = []
    seen: set[tuple[str, str, str]] = set()
    for i, row in enumerate(data):
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
