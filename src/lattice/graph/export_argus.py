"""Argus exporter: writes the working graph as Argus-compatible JSON.

Reverse of a potential ingester/argus.py. The exact Argus JSON schema
is provisional until a real Argus export is available to compare
against; the shape below reflects the mapping described in SPEC §4.1.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import AuthorGraph, ClaimType, RelationshipType


def export_to_argus(graph: AuthorGraph, output_path: Path) -> None:
    thesis_claim = next((c for c in graph.claims if c.claim_id == "cl.thesis"), None)
    thesis_node: dict = {}
    if thesis_claim:
        thesis_node = {
            "id": thesis_claim.claim_id,
            "type": "thesis",
            "statement": thesis_claim.statement,
        }
    elif graph.thesis_statement:
        thesis_node = {
            "id": "cl.thesis",
            "type": "thesis",
            "statement": graph.thesis_statement,
        }

    # Claims by id for relationship rendering.
    claims_by_id = {c.claim_id: c for c in graph.claims}

    # Mark counter-claims via the relationship table.
    counter_claims = {
        rel.from_claim
        for rel in graph.relationships
        if rel.type == RelationshipType.contradicts
    }

    arguments: list[dict] = []
    for section in graph.sections:
        if section.section_id == "s.thesis":
            continue
        arguments.append(
            {
                "id": section.section_id,
                "title": section.title,
                "role": section.role.value,
                "claims": section.claim_ids,
            }
        )

    claims_out: list[dict] = []
    evidences: list[dict] = []
    references: list[dict] = []
    seen_sources: set[str] = set()

    for claim in graph.claims:
        if claim.claim_id == "cl.thesis":
            continue
        node_type = "claim"
        if claim.claim_id in counter_claims:
            node_type = "counter_claim"
        elif claim.type == ClaimType.user_synthesis:
            node_type = "user_synthesis"
        claims_out.append(
            {
                "id": claim.claim_id,
                "type": node_type,
                "statement": claim.statement,
                "confidence": claim.confidence.value,
                "author_origin": claim.author_origin,
                "section_id": claim.section_id,
                "tags": claim.tags,
            }
        )
        for ev in claim.evidence:
            evidences.append(
                {
                    "claim_id": claim.claim_id,
                    "source_id": ev.source,
                    "passage_id": ev.passage or None,
                    "binding_strength": ev.binding_strength.value,
                    "page": ev.page,
                }
            )
            if ev.source and ev.source not in seen_sources:
                references.append({"id": ev.source})
                seen_sources.add(ev.source)

    edges: list[dict] = []
    for rel in graph.relationships:
        edges.append(
            {
                "id": rel.rel_id,
                "from": rel.from_claim,
                "to": rel.to_claim,
                "type": rel.type.value,
                "strength": rel.strength.value,
                "note": rel.note,
            }
        )

    payload = {
        "thesis": thesis_node,
        "arguments": arguments,
        "claims": claims_out,
        "evidences": evidences,
        "references": references,
        "edges": edges,
        "project_name": graph.project_name,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
