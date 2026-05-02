"""Assembler: build cluster plan from author graph and voice.

Responsibilities (SPEC §5.7):
1. Architecture validation against voice template
2. Cluster construction — group section claims into 2-4 claim clusters
3. Citation strategy per cluster — synthesis flag, reporting verbs,
   first-mention-full tracking across the whole document
4. Cross-cluster transitions

Cluster construction is deterministic (role-aware chunking); an
LLM-based grouping fallback is a future enhancement. The MVP produces
reasonable clusters on any tag-annotated outline.
"""

from __future__ import annotations

import itertools
from typing import Any

from ..graph.models import (
    AuthorGraph,
    CitationStrategy,
    Claim,
    ClaimRoleInCluster,
    ClaimType,
    Cluster,
    ClusterRelationshipContext,
    ClusterRole,
    Confidence,
    Relationship,
    RelationshipStrength,
    RelationshipType,
    Section,
)
from ..graph.store import GraphStore
from ..utils.config import Config
from ..utils.llm import ClaudeClient
from ..voice.parser import Voice


_ROLE_FROM_STR = {r.value: r for r in ClusterRole}

_CLUSTER_BOUNDARY_ROLES = {ClusterRole.synthesis, ClusterRole.conclusion}

_MIN_CLUSTER_SIZE = 1
_MAX_CLUSTER_SIZE = 4

# Relationship types that mean "these two claims belong in one paragraph
# if at all possible". The assembler will avoid splitting an adjacent
# pair joined by one of these (subject to the max-cluster-size cap), and
# will run a post-pass that merges adjacent clusters when one of these
# bridges them. ``interpretive_pivot`` is the sharpest case: it's the
# whole point of the move that the two claims sit together.
_STICKY_RELATIONSHIP_TYPES: frozenset[RelationshipType] = frozenset({
    RelationshipType.interpretive_pivot,
    RelationshipType.qualifies,
    RelationshipType.extends,
    RelationshipType.depends_on,
    RelationshipType.contradicts,
    RelationshipType.is_counterexample_to,
})

# Cluster word target = source content words * this factor. Set high
# enough that Claude has to genuinely expand the source content rather
# than tracking source length. Empirically Claude treats target_words
# as advisory and stays close to source length unless the gap is wide.
_SOURCE_DENSITY_BOOST = 1.8


class Assembler:
    def __init__(
        self,
        config: Config,
        store: GraphStore,
        llm: ClaudeClient | None,
        voice: Voice,
    ) -> None:
        self.config = config
        self.store = store
        self.llm = llm
        self.voice = voice

    async def build_plan(self) -> list[Cluster]:
        graph = self.store.get_graph()
        claims_by_id = {c.claim_id: c for c in graph.claims}

        # Index relationships once so the section builder can look up
        # sticky pairs in O(1). Edges with from/to claims that aren't
        # in claims_by_id are dropped (defensive — the differ should
        # have already pruned dangling edges, but a stale cluster_plan
        # against a freshly-edited graph can still hit this).
        rels_from: dict[str, list[Relationship]] = {}
        rels_to: dict[str, list[Relationship]] = {}
        for rel in graph.relationships:
            if rel.from_claim not in claims_by_id or rel.to_claim not in claims_by_id:
                continue
            rels_from.setdefault(rel.from_claim, []).append(rel)
            rels_to.setdefault(rel.to_claim, []).append(rel)

        # Preserve render state from any previous plan so re-planning
        # doesn't trigger unnecessary re-renders for unchanged clusters.
        existing_by_id = {c.cluster_id: c for c in self.store.list_clusters()}

        # 1. Architecture validation (advisory — surfaced, not blocking).
        violations = self.validate_architecture(graph)
        if violations:
            self._violations = violations  # caller can inspect

        # 2. Cluster construction per section. Skip sections the annotator
        # flagged as references / bibliography — they aren't argument prose.
        from ..graph.models import SectionRole
        clusters: list[Cluster] = []
        for section in graph.sections:
            if section.role == SectionRole.references:
                continue
            section_clusters = self._build_section_clusters(
                section, claims_by_id, rels_from, rels_to,
            )
            clusters.extend(section_clusters)

        # 3. Transitions (requires full cluster list for neighbour lookup).
        self._assign_transitions(clusters, claims_by_id)

        # 4. Citation strategy (document-wide first-mention tracking).
        self._plan_citation_strategy(clusters, claims_by_id)

        # 5. Relationship context — compute intra/incoming/outgoing edges
        # per cluster so the renderer can shape paragraphs around them.
        self._populate_relationship_context(clusters, graph)

        # Carry over prose state from the previous plan where IDs still
        # match. If the rendering-affecting relationship context has
        # changed since the cluster was last rendered, escalate state to
        # ``dirty`` so the renderer re-runs — newly-inferred or removed
        # edges meaningfully change paragraph shape.
        from ..graph.models import ProseState
        for cluster in clusters:
            old = existing_by_id.get(cluster.cluster_id)
            if old is None:
                continue
            cluster.prose_state = old.prose_state
            cluster.last_rendered_at = old.last_rendered_at
            cluster.last_rendered_hash = old.last_rendered_hash
            cluster.last_render_token_count = old.last_render_token_count
            cluster.prose_file = old.prose_file

            old_sig = _relationship_signature(getattr(old, "relationship_context", []))
            new_sig = _relationship_signature(cluster.relationship_context)
            if (
                old_sig != new_sig
                and cluster.prose_state in (
                    ProseState.generated, ProseState.edited, ProseState.needs_review
                )
            ):
                cluster.prose_state = ProseState.dirty

        # Persist. Rewrite cluster_plan.json from scratch so orphaned clusters
        # (e.g. from sections now flagged references) don't linger on disk.
        if self.store.cluster_plan_path.exists():
            self.store.cluster_plan_path.unlink()
        for c in clusters:
            self.store.save_cluster(c)
        self.store.save_graph(graph)
        return clusters

    # ─── 1. Architecture validation ────────────────────

    def validate_architecture(self, graph: AuthorGraph) -> list[str]:
        template = self.voice.architecture.template
        issues: list[str] = []
        if template == "six_element_paper":
            required_roles = {
                "introduction",
                "argumentative",
                "conclusion",
            }
            present = {s.role.value for s in graph.sections}
            missing = required_roles - present
            if missing:
                issues.append(
                    f"six_element_paper: missing sections with roles {sorted(missing)}"
                )
        return issues

    # ─── 2. Cluster construction ───────────────────────

    def _build_section_clusters(
        self,
        section: Section,
        claims_by_id: dict[str, Claim],
        rels_from: dict[str, list[Relationship]],
        rels_to: dict[str, list[Relationship]],
    ) -> list[Cluster]:
        ordered = [
            claims_by_id[cid]
            for cid in section.claim_ids
            if cid in claims_by_id and "skip" not in claims_by_id[cid].tags
        ]
        if not ordered:
            return []
        # Defence-in-depth: even if claim_ids was mutated out of source
        # order downstream of ingest, render in the order the author
        # wrote. Stable sort — legacy claims (source_order=0) keep their
        # relative position.
        ordered.sort(key=lambda c: c.source_order)

        # Build a "sticky pair" set: pairs of claims joined by a sticky
        # relationship type, restricted to claims in this section's
        # ordered set. We keep it undirected — the rendering doesn't
        # care which direction the edge runs in.
        ordered_ids = {c.claim_id for c in ordered}
        sticky_pairs: set[frozenset[str]] = set()
        for claim in ordered:
            for rel in rels_from.get(claim.claim_id, []):
                if rel.type in _STICKY_RELATIONSHIP_TYPES and rel.to_claim in ordered_ids:
                    sticky_pairs.add(frozenset({rel.from_claim, rel.to_claim}))

        def _sticky_to_any_in(claim: Claim, others: list[Claim]) -> bool:
            for other in others:
                if frozenset({claim.claim_id, other.claim_id}) in sticky_pairs:
                    return True
            return False

        groups: list[list[Claim]] = []
        current: list[Claim] = []

        # Walk by index so we can peek the next claim in O(1); the old
        # ``ordered.index(claim)`` lookup was O(n²) for large sections.
        for i, claim in enumerate(ordered):
            role = _role_for_claim(claim)
            current.append(claim)
            at_max = len(current) >= _MAX_CLUSTER_SIZE
            at_boundary = role in _CLUSTER_BOUNDARY_ROLES and len(current) >= 2
            if not (at_max or at_boundary):
                continue

            # We'd otherwise close the current cluster here. Before doing
            # that, check whether the *next* claim is sticky-bound to
            # anything in the current cluster — if so, defer the break
            # so the related pair stays together (subject to max size).
            if not at_max and i + 1 < len(ordered):
                next_claim = ordered[i + 1]
                if _sticky_to_any_in(next_claim, current):
                    continue

            groups.append(current)
            current = []
        if current:
            groups.append(current)

        # Merge tiny trailing cluster into the previous one if possible.
        if len(groups) >= 2 and len(groups[-1]) == 1 and len(groups[-2]) < _MAX_CLUSTER_SIZE:
            groups[-2].extend(groups[-1])
            groups.pop()

        # Final pass: if two adjacent clusters are bridged by a sticky
        # relationship and the merged size would not exceed the cap,
        # merge them. Catches cases the streaming pass couldn't (e.g. a
        # qualifies-edge that points backward across a boundary role).
        merged: list[list[Claim]] = []
        for group in groups:
            if merged:
                prev = merged[-1]
                bridged = any(
                    frozenset({a.claim_id, b.claim_id}) in sticky_pairs
                    for a in prev for b in group
                )
                if bridged and len(prev) + len(group) <= _MAX_CLUSTER_SIZE:
                    prev.extend(group)
                    continue
            merged.append(group)
        groups = merged

        clusters: list[Cluster] = []
        # ``section_letter`` is used as the readable cluster-id prefix.
        # For top-level sections (``s.c``) this stays just the letter
        # (``c``), preserving the existing ``c.c.<seq>`` ids. Nested
        # sections (``s.c.1``) collapse their inner dots to underscores
        # so cluster ids stay unambiguous (``c.c_1.<seq>``) and don't
        # collide with the top-level ``c.c.1`` cluster.
        section_letter = (
            section.section_id.removeprefix("s.").replace(".", "_")
            or "x"
        )

        for seq, group in enumerate(groups, start=1):
            roles = [_role_for_claim(c) for c in group]
            cluster_role = _dominant_cluster_role(roles)
            sequence_entries = [
                ClaimRoleInCluster(
                    claim_id=c.claim_id,
                    role_in_cluster=r,
                    reporting_verb=_reporting_verb_for(
                        c, self.voice, is_user_synthesis=c.type == ClaimType.user_synthesis
                    ),
                )
                for c, r in zip(group, roles, strict=True)
            ]
            # Target-words by source density: total words across the cluster's
            # claim statements, multiplied by _SOURCE_DENSITY_BOOST to force
            # genuine development above source length, then ±15% to define
            # the band. A 150-word floor protects very short clusters from
            # being squeezed below a renderable size.
            source_words = sum(len(c.statement.split()) for c in group)
            target = max(150, int(source_words * _SOURCE_DENSITY_BOOST))
            min_words = int(target * 0.85)
            max_words = int(target * 1.15)
            clusters.append(
                Cluster(
                    cluster_id=f"c.{section_letter}.{seq}",
                    section_id=section.section_id,
                    position=seq,
                    role=cluster_role,
                    claim_sequence=sequence_entries,
                    target_words_min=min_words,
                    target_words_max=max_words,
                )
            )

        # Populate section.cluster_ids for easy lookup.
        section.cluster_ids = [c.cluster_id for c in clusters]
        return clusters

    # ─── 3. Transitions ────────────────────────────────

    def _assign_transitions(
        self, clusters: list[Cluster], claims_by_id: dict[str, Claim]
    ) -> None:
        for i, cluster in enumerate(clusters):
            if i > 0:
                cluster.previous_cluster = clusters[i - 1].cluster_id
                cluster.transition_in_hint = (
                    f"Pick up the topic closing the previous cluster "
                    f"(role={clusters[i - 1].role.value})."
                )
            if i < len(clusters) - 1:
                next_c = clusters[i + 1]
                cluster.next_cluster = next_c.cluster_id
                cluster.transition_out_hint = (
                    f"End on a sentence that supports the next cluster's "
                    f"role ({next_c.role.value})."
                )
            else:
                cluster.transition_out_hint = (
                    "This is the final cluster in its section. End emphatically."
                )

    # ─── 5. Relationship context ───────────────────────

    def _populate_relationship_context(
        self, clusters: list[Cluster], graph: AuthorGraph,
    ) -> None:
        """For each cluster, attach the edges that touch its claims.

        Each edge produces at most one ``ClusterRelationshipContext``
        per cluster:

        - Both endpoints in the same cluster → ``intra``.
        - One endpoint in this cluster, the other elsewhere → ``incoming``
          (this cluster owns the ``to`` end) or ``outgoing`` (this
          cluster owns the ``from`` end).

        ``affects_rendering`` is True for edges whose type matters for
        paragraph shape (the sticky set plus the canonical
        supports/contradicts/is_evidence_for triplet); False for
        unlabelled / inferred edges.
        """
        # Build a claim → cluster index for O(1) lookup of "where does
        # the other endpoint live?".
        cluster_for_claim: dict[str, str] = {}
        section_for_cluster: dict[str, str] = {}
        for cluster in clusters:
            section_for_cluster[cluster.cluster_id] = cluster.section_id
            for entry in cluster.claim_sequence:
                cluster_for_claim[entry.claim_id] = cluster.cluster_id

        renderable_types = _STICKY_RELATIONSHIP_TYPES | {
            RelationshipType.supports,
            RelationshipType.is_evidence_for,
        }

        for cluster in clusters:
            cluster.relationship_context = []
            in_cluster = {entry.claim_id for entry in cluster.claim_sequence}

            for rel in graph.relationships:
                from_in = rel.from_claim in in_cluster
                to_in = rel.to_claim in in_cluster
                if not (from_in or to_in):
                    continue

                if from_in and to_in:
                    direction = "intra"
                    other_cluster_id = None
                elif from_in:
                    direction = "outgoing"
                    other_cluster_id = cluster_for_claim.get(rel.to_claim)
                else:  # to_in
                    direction = "incoming"
                    other_cluster_id = cluster_for_claim.get(rel.from_claim)

                other_section_id = (
                    section_for_cluster.get(other_cluster_id)
                    if other_cluster_id
                    else None
                )
                affects = rel.type in renderable_types

                cluster.relationship_context.append(
                    ClusterRelationshipContext(
                        rel_id=rel.rel_id,
                        type=rel.type,
                        strength=rel.strength,
                        note=rel.note or "",
                        direction=direction,
                        from_claim=rel.from_claim,
                        to_claim=rel.to_claim,
                        other_cluster_id=other_cluster_id,
                        other_section_id=other_section_id,
                        affects_rendering=affects,
                    )
                )

    # ─── 4. Citation strategy ──────────────────────────

    def _plan_citation_strategy(
        self, clusters: list[Cluster], claims_by_id: dict[str, Claim]
    ) -> None:
        threshold = self.voice.citation.synthesis_threshold
        positioning_categories = set(self.voice.citation.positioning_required_for)

        seen_sources: set[str] = set()

        for cluster in clusters:
            claim_ids = [c.claim_id for c in cluster.claim_sequence]
            claims = [claims_by_id[cid] for cid in claim_ids if cid in claims_by_id]
            sources_here = _collect_sources(claims)

            synthesis_required = len(sources_here) >= threshold
            positioning_required_for = _identify_positioning(
                claims, cluster.role, positioning_categories
            )

            first_mention_full: list[str] = []
            for src in sources_here:
                if src not in seen_sources:
                    first_mention_full.append(src)
                    seen_sources.add(src)

            cluster.citation_strategy = CitationStrategy(
                synthesis_required=synthesis_required,
                synthesis_target_claims=(
                    [c.claim_id for c in claims] if synthesis_required else []
                ),
                positioning_required_for=positioning_required_for,
                catalogue_forbidden=self.voice.citation.forbid_catalogue_pattern,
                first_mention_full=first_mention_full,
            )


# ─── helpers ────────────────────────────────────────

def _role_for_claim(claim: Claim) -> ClusterRole:
    # Look for a `role:X` entry in tags.
    for tag in claim.tags:
        if tag.startswith("role:"):
            role_str = tag.split(":", 1)[1].strip()
            if role_str in _ROLE_FROM_STR:
                return _ROLE_FROM_STR[role_str]
    # Default inference:
    if claim.type == ClaimType.user_synthesis:
        return ClusterRole.synthesis
    return ClusterRole.evidence


def _dominant_cluster_role(roles: list[ClusterRole]) -> ClusterRole:
    # The last role dictates how the cluster resolves (Schimel-style).
    # Fall back to evidence if empty.
    return roles[-1] if roles else ClusterRole.evidence


def _reporting_verb_for(
    claim: Claim, voice: Voice, is_user_synthesis: bool
) -> str | None:
    if is_user_synthesis:
        return None  # author's own voice — no reporting verb needed
    verbs = voice.citation.reporting_verbs
    if claim.confidence == Confidence.high:
        bucket = verbs.direct_evidence
    elif claim.confidence == Confidence.medium:
        bucket = verbs.correlational
    else:
        bucket = verbs.speculative
    if not bucket:
        return None
    return _pick_round_robin(bucket, claim.claim_id)


_ROUND_ROBIN_COUNTERS: dict[tuple[str, ...], itertools.cycle] = {}


def _pick_round_robin(options: list[str], salt: str) -> str:
    # Deterministic-per-key selection: hash the claim_id mod len(options).
    idx = sum(ord(c) for c in salt) % len(options)
    return options[idx]


def _collect_sources(claims: list[Claim]) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for c in claims:
        for ev in c.evidence:
            if ev.source and ev.source not in seen_set:
                seen.append(ev.source)
                seen_set.add(ev.source)
    return seen


def _relationship_signature(
    rel_context: list[ClusterRelationshipContext],
) -> tuple:
    """Deterministic, comparable summary of a cluster's
    rendering-affecting relationship context.

    Two contexts with the same set of intra/incoming/outgoing edge
    triples (type, from, to, direction) produce the same signature, so
    re-running the assembler doesn't churn cluster state when nothing
    actually changed. Edges with ``affects_rendering=False`` are
    excluded — they don't drive prose shape, so changes to them
    shouldn't trigger re-renders.
    """
    return tuple(sorted(
        (r.type.value, r.from_claim, r.to_claim, r.direction)
        for r in rel_context
        if r.affects_rendering
    ))


def _identify_positioning(
    claims: list[Claim], cluster_role: ClusterRole, categories: set[str]
) -> list[str]:
    positioning: list[str] = []
    for c in claims:
        if c.claim_id == "cl.thesis" and "thesis_claims" in categories:
            positioning.append(c.claim_id)
            continue
        if (
            "gap_statements" in categories
            and c.type == ClaimType.user_synthesis
            and any(t == "role:setup" for t in c.tags)
        ):
            positioning.append(c.claim_id)
            continue
        if (
            "novel_methodology_claims" in categories
            and c.type == ClaimType.methodological
            and c.author_origin
        ):
            positioning.append(c.claim_id)
    return positioning
