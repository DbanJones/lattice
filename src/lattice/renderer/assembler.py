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
    ClusterRole,
    Confidence,
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
            section_clusters = self._build_section_clusters(section, claims_by_id)
            clusters.extend(section_clusters)

        # 3. Transitions (requires full cluster list for neighbour lookup).
        self._assign_transitions(clusters, claims_by_id)

        # 4. Citation strategy (document-wide first-mention tracking).
        self._plan_citation_strategy(clusters, claims_by_id)

        # Carry over prose state from the previous plan where IDs still match.
        for cluster in clusters:
            old = existing_by_id.get(cluster.cluster_id)
            if old is None:
                continue
            cluster.prose_state = old.prose_state
            cluster.last_rendered_at = old.last_rendered_at
            cluster.last_rendered_hash = old.last_rendered_hash
            cluster.last_render_token_count = old.last_render_token_count
            cluster.prose_file = old.prose_file

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
        self, section: Section, claims_by_id: dict[str, Claim]
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

        groups: list[list[Claim]] = []
        current: list[Claim] = []

        for claim in ordered:
            role = _role_for_claim(claim)
            current.append(claim)
            # Break if we've hit the max OR we just consumed a boundary role
            # and already have 2+ claims in the cluster.
            if len(current) >= _MAX_CLUSTER_SIZE:
                groups.append(current)
                current = []
            elif role in _CLUSTER_BOUNDARY_ROLES and len(current) >= 2:
                groups.append(current)
                current = []
        if current:
            groups.append(current)

        # Merge tiny trailing cluster into the previous one if possible.
        if len(groups) >= 2 and len(groups[-1]) == 1 and len(groups[-2]) < _MAX_CLUSTER_SIZE:
            groups[-2].extend(groups[-1])
            groups.pop()

        clusters: list[Cluster] = []
        section_letter = section.section_id.removeprefix("s.") or "x"

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
