"""Shadow mapper, sub-stage 3: build relationships between extracted claims.

Per topic cluster, ask the LLM to identify supports/contradicts/qualifies
relationships between claim pairs in that cluster.

Output: an AuthorGraph-shaped shadow graph. Symmetric schema makes the
differ's job trivial.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Protocol

from ..graph.models import (
    AuthorGraph,
    BindingStrength,
    Claim,
    ClaimType,
    Confidence,
    Evidence,
    Relationship,
    RelationshipStrength,
    RelationshipType,
    Section,
    SectionRole,
)
from ..utils.config import Config


class _LLMProtocol(Protocol):
    async def complete_json(
        self, system: str, user: str, model: str | None = None, temperature: float = 0.2
    ) -> tuple[object, object]: ...


_SYSTEM_PROMPT = """\
You identify relationships between claims in a literature cluster.

Relationship types:
- supports: A provides evidence for B
- contradicts: A and B cannot both be true
- qualifies: A is true only under conditions B describes
- extends: A builds on B
- depends_on: A only makes sense if B is true
- is_counterexample_to: A is a specific case undermining B

Return JSON array of relationships: [
  {"from": "claim_id_A", "to": "claim_id_B", "type": "...", "strength": "direct|partial|inferred", "note": "one sentence"}
]

Be conservative. Only assert relationships you can justify from the claim statements.
"""


_CLAIM_TYPE_MAP = {t.value: t for t in ClaimType}
_CONFIDENCE_MAP = {c.value: c for c in Confidence}
_REL_TYPE_MAP = {r.value: r for r in RelationshipType}
_REL_STRENGTH_MAP = {s.value: s for s in RelationshipStrength}


class ShadowArchitect:
    def __init__(self, config: Config, llm: _LLMProtocol) -> None:
        self.config = config
        self.llm = llm

    async def build(self, clusters: list[dict], thesis: str) -> AuthorGraph:
        now = datetime.now(timezone.utc)
        claims: list[Claim] = []
        relationships: list[Relationship] = []
        sections: list[Section] = []

        # One section per cluster; claims belong to that section.
        tasks: list[asyncio.Task] = []
        for cluster in clusters:
            tasks.append(asyncio.create_task(self._build_cluster_rels(cluster)))

        per_cluster_rels = await asyncio.gather(*tasks)

        rel_seq = 0
        source_order_seq = 0
        for i, (cluster, rels) in enumerate(zip(clusters, per_cluster_rels, strict=True), start=1):
            section_id = f"s.shadow_{i}"
            section_claim_ids: list[str] = []
            for c in cluster["claims"]:
                conf = _CONFIDENCE_MAP.get(c["confidence"], Confidence.medium)
                # Shadow extraction gets the claim directly from the source,
                # so the binding strength mirrors the source's stated confidence.
                binding = BindingStrength.strong if conf == Confidence.high else BindingStrength.weak
                source_order_seq += 1
                claim_obj = Claim(
                    claim_id=c["claim_id"],
                    statement=c["statement"],
                    source_order=source_order_seq,
                    type=_CLAIM_TYPE_MAP.get(c["type"], ClaimType.empirical),
                    confidence=conf,
                    evidence=[
                        Evidence(
                            source=c["source_id"],
                            passage=c.get("passage_id", "") or "",
                            binding_strength=binding,
                        )
                    ],
                    author_origin=False,
                    section_id=section_id,
                    created_by="shadow_architect",
                    created_at=now,
                    modified_at=now,
                    tags=list(c.get("tags") or []),
                )
                claims.append(claim_obj)
                section_claim_ids.append(claim_obj.claim_id)

            for rel in rels:
                rel_seq += 1
                try:
                    relationships.append(
                        Relationship(
                            rel_id=f"r.shadow.{rel_seq:03d}",
                            type=_REL_TYPE_MAP.get(rel.get("type", "supports"), RelationshipType.supports),
                            **{"from": str(rel.get("from") or ""), "to": str(rel.get("to") or "")},
                            strength=_REL_STRENGTH_MAP.get(rel.get("strength", "inferred"), RelationshipStrength.inferred),
                            note=str(rel.get("note") or ""),
                            created_by="shadow_architect",
                            created_at=now,
                        )
                    )
                except Exception:
                    continue

            sections.append(
                Section(
                    section_id=section_id,
                    title=cluster.get("topic", f"Shadow cluster {i}"),
                    parent=None,
                    position=i,
                    role=SectionRole.evidence_synthesis,
                    claim_ids=section_claim_ids,
                    target_length=500,
                )
            )

        return AuthorGraph(
            project_name="shadow",
            thesis_statement=thesis or None,
            sections=sections,
            claims=claims,
            relationships=relationships,
            created_at=now,
            modified_at=now,
        )

    async def _build_cluster_rels(self, cluster: dict) -> list[dict]:
        claims = cluster.get("claims") or []
        if len(claims) < 2:
            return []
        claims_xml = "\n".join(
            f'<claim id="{c["claim_id"]}" source="{c["source_id"]}" confidence="{c["confidence"]}">{c["statement"]}</claim>'
            for c in claims
        )
        user = (
            f"Topic cluster: {cluster.get('topic', 'unnamed')}\n\n"
            f"Claims in this cluster:\n\n<claims>\n{claims_xml}\n</claims>\n\n"
            "Identify all relationships between pairs of claims in this cluster."
        )
        try:
            payload, _ = await self.llm.complete_json(
                system=_SYSTEM_PROMPT,
                user=user,
                model=self.config.model_for_stage("shadow_architect"),
                temperature=0.3,
            )
        except Exception:
            return []
        if not isinstance(payload, list):
            return []
        return [p for p in payload if isinstance(p, dict)]
