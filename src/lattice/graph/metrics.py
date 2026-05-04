"""Argument-level metrics computed against the author graph.

Two diagnostic dimensions, surfaced in ``ScaffoldReport`` so the author
can see them at ingest time (not after rendering):

- **Strength** — how well does the graph prove the thesis? Composed of
  direct support, transitive reach, evidence backing on the supporting
  subgraph, counter-handling, and supporting-chain depth.
- **Breadth** — how wide is the argument's coverage? Composed of
  section diversity, source diversity, claim-type diversity,
  relationship-type diversity, mechanism coverage, and section spread.

Both dimensions emit per-component sub-scores plus structured
observations, so the diagram and audit can show *why* a number is what
it is rather than treating it as a black-box rating.

Pure function over ``AuthorGraph``; no LLM, no I/O.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Iterable

from pydantic import BaseModel, Field

from .models import (
    AuthorGraph,
    BindingStrength,
    ClaimType,
    EvidenceStatus,
    RelationshipType,
    SectionRole,
)


# Edges that count as "supporting" when walking backwards from the thesis
# to the leaves. ``supports`` and ``extends`` are the canonical case;
# ``depends_on`` and ``is_evidence_for`` count too because if A depends
# on B and B is part of the supporting structure, A inherits relevance.
_SUPPORTING_EDGE_TYPES: frozenset[RelationshipType] = frozenset({
    RelationshipType.supports,
    RelationshipType.extends,
    RelationshipType.depends_on,
    RelationshipType.is_evidence_for,
})

# Edges that count as "addressing" a counter-argument: if claim C
# contradicts the thesis, and another claim D contradicts / qualifies /
# pivots / is_counterexample_to C, then C has been addressed.
_COUNTER_HANDLING_EDGE_TYPES: frozenset[RelationshipType] = frozenset({
    RelationshipType.contradicts,
    RelationshipType.qualifies,
    RelationshipType.interpretive_pivot,
    RelationshipType.is_counterexample_to,
})

# Relationship types that "count" toward type-diversity. Excludes
# ``unlabelled`` because that's a placeholder, not a deliberate choice.
_REL_TYPES_FOR_DIVERSITY: frozenset[RelationshipType] = frozenset({
    RelationshipType.supports,
    RelationshipType.contradicts,
    RelationshipType.qualifies,
    RelationshipType.extends,
    RelationshipType.depends_on,
    RelationshipType.is_counterexample_to,
    RelationshipType.is_evidence_for,
    RelationshipType.interpretive_pivot,
})


# ─── pydantic models ─────────────────────────────────


class ArgumentStrength(BaseModel):
    """How well-proven is the thesis?

    All sub-scores are normalised to [0, 1]. ``score`` is the weighted
    aggregate. ``observations`` is a human-readable breakdown so the
    UI doesn't have to invent its own narrative for the number.
    """

    score: float = 0.0
    direct_support: float = 0.0          # 0..1, saturates at 5 direct supporters
    reachable_support: float = 0.0       # fraction of non-thesis claims reachable
    evidence_backing: float = 0.0        # avg evidence quality on supporting subgraph
    counter_handling: float = 1.0        # 1.0 when no counter-arguments to handle
    depth: float = 0.0                   # avg path-length from leaf supporter to thesis
    direct_supporter_count: int = 0
    transitively_supporting_claim_count: int = 0
    contradicting_thesis_count: int = 0
    counters_addressed_count: int = 0
    weakest_supporters: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)


class ArgumentBreadth(BaseModel):
    """How wide is the argument?"""

    score: float = 0.0
    section_diversity: float = 0.0
    source_diversity: float = 0.0
    claim_type_diversity: float = 0.0
    relationship_type_diversity: float = 0.0
    mechanism_coverage: float = 0.0
    section_spread: float = 0.0
    section_count: int = 0
    distinct_source_count: int = 0
    claim_types_present: list[str] = Field(default_factory=list)
    relationship_types_used: list[str] = Field(default_factory=list)
    section_concentration: dict[str, float] = Field(default_factory=dict)
    observations: list[str] = Field(default_factory=list)


class SectionMetrics(BaseModel):
    """Per-section diagnostic. A focused subset of ``ArgumentMetrics``
    computed over the claims that live in one section.

    Some sub-scores from the document-level metrics don't make sense
    at section scale (counter_handling assumes contradicts→thesis
    edges; depth measures backward chains to the thesis). This model
    drops those and adds two section-specific signals:

    - **thesis_connection** — what fraction of section claims have at
      least one transitive supporting path back to ``cl.thesis``. A
      section that doesn't connect to the thesis is either off-topic
      or needs explicit linking work.
    - **relationship_density** — relationships among claims-in-section
      divided by the section's claim count. Catches sections that read
      as a list of independent assertions rather than an argument.
    """

    section_id: str
    section_title: str = ""
    parent: str | None = None
    role: str = ""
    # Headline counts (raw, not normalised).
    claim_count: int = 0
    relationship_count: int = 0   # edges with both endpoints in this section
    source_count: int = 0
    # Per-component scores in [0, 1].
    avg_importance: float = 0.0
    evidence_backing: float = 0.0
    mechanism_coverage: float = 0.0
    source_diversity: float = 0.0
    relationship_density: float = 0.0
    thesis_connection: float = 0.0
    claim_type_diversity: float = 0.0
    # Aggregate: weighted average of the sub-scores above.
    score: float = 0.0
    observations: list[str] = Field(default_factory=list)


class ArgumentMetrics(BaseModel):
    """Combined view emitted with every scaffold report."""

    strength: ArgumentStrength = Field(default_factory=ArgumentStrength)
    breadth: ArgumentBreadth = Field(default_factory=ArgumentBreadth)
    # Per-section roll-up. Lazily populated by ``compute_argument_metrics``;
    # ``{}`` when sections weren't computed (older serialised reports).
    per_section: dict[str, SectionMetrics] = Field(default_factory=dict)


# ─── public entry point ──────────────────────────────


def compute_argument_metrics(graph: AuthorGraph) -> ArgumentMetrics:
    """Run both metric passes against ``graph`` and return the combined view.

    Also computes per-section metrics so the diagram + UI can show a
    section-level heat-map. The per-section pass is cheap (each is a
    subset of the document-level computation) and makes the metrics
    useful for revision: an academic looking at a 50-page paper can
    see at a glance which two sections need an afternoon of work.
    """
    return ArgumentMetrics(
        strength=compute_strength(graph),
        breadth=compute_breadth(graph),
        per_section=compute_all_section_metrics(graph),
    )


def compute_all_section_metrics(
    graph: AuthorGraph,
) -> dict[str, SectionMetrics]:
    """Compute ``SectionMetrics`` for every body section in ``graph``.

    Skips ``s.thesis`` (the thesis claim sits in its own slot; its
    metrics are degenerate at section scale) and any section flagged
    as references.
    """
    from .models import SectionRole

    out: dict[str, SectionMetrics] = {}
    for section in graph.sections:
        if section.section_id == "s.thesis":
            continue
        if section.role == SectionRole.references:
            continue
        out[section.section_id] = compute_section_metrics(graph, section.section_id)
    return out


def compute_section_metrics(
    graph: AuthorGraph, section_id: str,
) -> SectionMetrics:
    """Compute the section-level metric for one section.

    Pure function. Filters claims to those whose ``section_id`` matches
    (or whose claim_id is in the section's ``claim_ids`` list, for
    robustness against orphan claims). Operates over the resulting
    sub-graph as if it were a complete document — except for the
    thesis-connection signal, which still walks the full graph.
    """
    section = next(
        (s for s in graph.sections if s.section_id == section_id), None,
    )
    if section is None:
        return SectionMetrics(section_id=section_id)

    section_claim_ids: set[str] = set(section.claim_ids)
    # Robustness: also include claims whose section_id matches, in case
    # the claim_ids list is stale.
    for claim in graph.claims:
        if claim.section_id == section_id:
            section_claim_ids.add(claim.claim_id)
    section_claim_ids.discard("cl.thesis")

    claims = [c for c in graph.claims if c.claim_id in section_claim_ids]
    if not claims:
        return SectionMetrics(
            section_id=section_id,
            section_title=section.title,
            parent=section.parent,
            role=section.role.value if section.role else "",
        )

    # Relationships fully inside the section.
    in_section_relationships = [
        r for r in graph.relationships
        if r.from_claim in section_claim_ids and r.to_claim in section_claim_ids
    ]

    # Sources cited by section claims.
    source_ids: set[str] = set()
    for claim in claims:
        for ev in claim.evidence:
            if ev.source:
                source_ids.add(ev.source)

    # avg_importance — claims in the section, weighted by mass.
    avg_importance = sum(c.importance for c in claims) / len(claims)

    # evidence_backing — same scoring as the document-level metric,
    # averaged across the section's claims.
    backing_scores = [_evidence_quality_score(c) for c in claims]
    evidence_backing = sum(backing_scores) / len(backing_scores) if backing_scores else 0.0

    # mechanism_coverage — fraction of empirical / methodological claims
    # in the section that have a non-empty mechanism field.
    mechanism_eligible = [
        c for c in claims
        if c.type in (ClaimType.empirical, ClaimType.methodological)
    ]
    if mechanism_eligible:
        with_mech = sum(
            1 for c in mechanism_eligible if (c.mechanism or "").strip()
        )
        mechanism_coverage = with_mech / len(mechanism_eligible)
    else:
        mechanism_coverage = 0.0

    # source_diversity — same shape as breadth's source_diversity, but
    # over just this section. Distinct count saturates at 6 (a section
    # rarely cites more than 6 distinct sources well).
    distinct = len(source_ids)
    distinct_score = min(1.0, distinct / 6)
    source_counts: dict[str, int] = defaultdict(int)
    for claim in claims:
        for ev in claim.evidence:
            if ev.source:
                source_counts[ev.source] += 1
    if distinct > 1:
        total = sum(source_counts.values())
        entropy = -sum(
            (n / total) * math.log2(n / total) for n in source_counts.values()
        ) if total else 0.0
        normalised_entropy = entropy / math.log2(distinct) if distinct > 1 else 0.0
        source_diversity = math.sqrt(distinct_score * normalised_entropy)
    else:
        source_diversity = 0.0

    # relationship_density — edges-per-claim within the section.
    # Saturates at 1.5 edges per claim (a typical well-developed
    # section averages 1-2 connections per claim).
    rel_density_raw = len(in_section_relationships) / len(claims) if claims else 0.0
    relationship_density = min(1.0, rel_density_raw / 1.5)

    # thesis_connection — fraction of section claims that have at
    # least one transitive supporting path back to cl.thesis (using
    # the document graph, not just the section sub-graph).
    thesis_connection = _thesis_connection_fraction(graph, section_claim_ids)

    # claim_type_diversity — distinct types present in section / 5.
    types_present = {c.type for c in claims}
    claim_type_diversity = len(types_present) / 5

    score = round(
        0.20 * avg_importance
        + 0.20 * evidence_backing
        + 0.15 * mechanism_coverage
        + 0.15 * source_diversity
        + 0.15 * relationship_density
        + 0.10 * thesis_connection
        + 0.05 * claim_type_diversity,
        4,
    )

    metrics = SectionMetrics(
        section_id=section_id,
        section_title=section.title,
        parent=section.parent,
        role=section.role.value if section.role else "",
        claim_count=len(claims),
        relationship_count=len(in_section_relationships),
        source_count=distinct,
        avg_importance=round(avg_importance, 4),
        evidence_backing=round(evidence_backing, 4),
        mechanism_coverage=round(mechanism_coverage, 4),
        source_diversity=round(source_diversity, 4),
        relationship_density=round(relationship_density, 4),
        thesis_connection=round(thesis_connection, 4),
        claim_type_diversity=round(claim_type_diversity, 4),
        score=score,
    )

    # Observations — actionable, short, prioritised.
    if metrics.thesis_connection < 0.5:
        metrics.observations.append(
            f"Only {metrics.thesis_connection:.0%} of this section's "
            "claims connect to the thesis. Either link the off-topic "
            "claims explicitly with [supports:] tags, or move them to "
            "another section."
        )
    if metrics.relationship_density < 0.3 and metrics.claim_count >= 4:
        metrics.observations.append(
            f"{metrics.claim_count} claims but only "
            f"{metrics.relationship_count} intra-section edge(s). The "
            "section reads as a list rather than an argument; add "
            "qualifies / extends / depends_on edges."
        )
    if metrics.evidence_backing < 0.4:
        metrics.observations.append(
            f"Evidence backing {metrics.evidence_backing:.2f} — most "
            "claims here are weakly grounded. Run lattice fill-evidence."
        )
    if metrics.mechanism_coverage < 0.3 and mechanism_eligible:
        metrics.observations.append(
            f"Only {metrics.mechanism_coverage:.0%} of empirical / "
            "methodological claims have a [mechanism:] tag. Run "
            "lattice fill-mechanisms."
        )
    if metrics.source_count == 0 and metrics.claim_count > 0:
        metrics.observations.append(
            "No sources cited in this section. Either add [ref:] tags "
            "to the empirical claims or convert them to user_synthesis."
        )
    return metrics


def _thesis_connection_fraction(
    graph: AuthorGraph, claim_ids: set[str],
) -> float:
    """Fraction of ``claim_ids`` with a transitive supporting path
    back to ``cl.thesis``. Uses the same edge set as the strength
    metric so the two stay aligned."""
    if not claim_ids or "cl.thesis" not in {c.claim_id for c in graph.claims}:
        return 0.0
    inbound: dict[str, list] = defaultdict(list)
    for rel in graph.relationships:
        inbound[rel.to_claim].append(rel)
    seen: set[str] = {"cl.thesis"}
    queue: deque[str] = deque(["cl.thesis"])
    while queue:
        node = queue.popleft()
        for rel in inbound[node]:
            if rel.type not in _SUPPORTING_EDGE_TYPES:
                continue
            if rel.from_claim in seen:
                continue
            seen.add(rel.from_claim)
            queue.append(rel.from_claim)
    connected = sum(1 for cid in claim_ids if cid in seen)
    return connected / len(claim_ids)


# ─── strength ────────────────────────────────────────


def compute_strength(graph: AuthorGraph) -> ArgumentStrength:
    """Walk the graph from the thesis backwards through supporting
    edges; score how thoroughly the body argues for the thesis."""
    result = ArgumentStrength()
    claims_by_id = {c.claim_id: c for c in graph.claims}
    if "cl.thesis" not in claims_by_id:
        result.observations.append(
            "No `cl.thesis` claim — argument strength can't be computed "
            "until the outline declares a thesis."
        )
        return result

    # Build inbound/outbound edge indices once.
    inbound: dict[str, list] = defaultdict(list)   # to → list of (rel_type, from)
    outbound: dict[str, list] = defaultdict(list)  # from → list of (rel_type, to)
    for rel in graph.relationships:
        if rel.from_claim not in claims_by_id or rel.to_claim not in claims_by_id:
            continue
        inbound[rel.to_claim].append((rel.type, rel.from_claim))
        outbound[rel.from_claim].append((rel.type, rel.to_claim))

    # ── 1. Direct support: how many claims point a supporting edge
    # straight at the thesis. Saturates at 5 — that's the rough cutoff
    # past which extra direct supporters are coordinate rather than
    # additive proof. ──
    direct_supporters = [
        from_id for (rtype, from_id) in inbound["cl.thesis"]
        if rtype in _SUPPORTING_EDGE_TYPES
    ]
    result.direct_supporter_count = len(direct_supporters)
    result.direct_support = min(1.0, len(direct_supporters) / 5)

    # ── 2. Reachable support: BFS backwards from thesis through
    # supporting edges. Anything reachable is part of the supporting
    # subgraph for strength scoring. ──
    supporting: set[str] = set()
    queue: deque[tuple[str, int]] = deque([("cl.thesis", 0)])
    depths: dict[str, int] = {"cl.thesis": 0}
    while queue:
        node, depth = queue.popleft()
        for rtype, predecessor in inbound[node]:
            if rtype not in _SUPPORTING_EDGE_TYPES:
                continue
            if predecessor in supporting:
                continue
            supporting.add(predecessor)
            depths[predecessor] = depth + 1
            queue.append((predecessor, depth + 1))
    result.transitively_supporting_claim_count = len(supporting)
    body_claim_count = sum(1 for c in graph.claims if c.claim_id != "cl.thesis")
    result.reachable_support = (
        len(supporting) / body_claim_count if body_claim_count else 0.0
    )

    # ── 3. Evidence backing: average evidence quality across the
    # supporting subgraph. Use the per-claim evidence-quality bucket so
    # this matches the diagram's colour-coding. ──
    if supporting:
        backing_scores: list[float] = []
        weak_supporters: list[tuple[str, float]] = []
        # Iterate in sorted order so ``weakest_supporters`` is stable
        # across runs (set iteration order is not).
        for claim_id in sorted(supporting):
            claim = claims_by_id[claim_id]
            quality = _evidence_quality_score(claim)
            backing_scores.append(quality)
            if quality < 0.5:
                weak_supporters.append((claim_id, quality))
        result.evidence_backing = sum(backing_scores) / len(backing_scores)
        # Surface the worst-grounded supporting claims so the author
        # knows where to dig if strength is low. Tie-break by claim_id
        # so the output is fully deterministic.
        weak_supporters.sort(key=lambda pair: (pair[1], pair[0]))
        result.weakest_supporters = [cid for cid, _ in weak_supporters[:5]]
    else:
        result.evidence_backing = 0.0

    # ── 4. Counter-handling: of claims that contradict the thesis,
    # how many are themselves addressed (contradicted / qualified /
    # pivoted / shown as counterexample) by another claim? ──
    contradictors = [
        from_id for (rtype, from_id) in inbound["cl.thesis"]
        if rtype == RelationshipType.contradicts
    ]
    result.contradicting_thesis_count = len(contradictors)
    if not contradictors:
        result.counter_handling = 1.0  # nothing to handle
    else:
        addressed = 0
        for c in contradictors:
            if any(
                rtype in _COUNTER_HANDLING_EDGE_TYPES
                for (rtype, _from) in inbound[c]
            ):
                addressed += 1
        result.counters_addressed_count = addressed
        result.counter_handling = addressed / len(contradictors)

    # ── 5. Depth: average path length from leaf supporters (those with
    # no further supporting predecessors) up to the thesis. Saturates
    # at 4 — beyond that the chain is mostly redundant. ──
    if supporting:
        leaf_depths: list[int] = []
        for claim_id in supporting:
            has_supporting_predecessor = any(
                rtype in _SUPPORTING_EDGE_TYPES
                for (rtype, _from) in inbound[claim_id]
            )
            if not has_supporting_predecessor:
                leaf_depths.append(depths.get(claim_id, 1))
        if leaf_depths:
            avg = sum(leaf_depths) / len(leaf_depths)
            result.depth = min(1.0, avg / 4)

    # ── Aggregate. Weights chosen so evidence backing carries the
    # most signal (it's the actual proof), with direct + transitive
    # support as the structural skeleton. Counter-handling is
    # consequential when there are counter-arguments to address. ──
    result.score = round(
        0.20 * result.direct_support
        + 0.15 * result.reachable_support
        + 0.30 * result.evidence_backing
        + 0.20 * result.counter_handling
        + 0.15 * result.depth,
        4,
    )

    # Observations.
    obs = result.observations
    if result.direct_supporter_count == 0:
        obs.append(
            "No claim directly supports the thesis. Add a `MY VIEW:` "
            "claim or a `[supports: thesis]` tag on the headline finding."
        )
    elif result.direct_supporter_count == 1:
        obs.append(
            "Only one claim directly supports the thesis — single-leg "
            "arguments are fragile. Consider adding 1-2 more independent "
            "supporters."
        )
    if result.evidence_backing < 0.4 and supporting:
        obs.append(
            f"Evidence backing on the supporting subgraph is weak "
            f"({result.evidence_backing:.2f}). Bind the worst-grounded "
            "supporters: " + ", ".join(result.weakest_supporters)
        )
    if (
        result.contradicting_thesis_count > 0
        and result.counter_handling < 0.5
    ):
        obs.append(
            f"{result.contradicting_thesis_count - result.counters_addressed_count} "
            "counter-claim(s) against the thesis are unaddressed — the "
            "argument doesn't engage with them."
        )
    if result.depth < 0.25 and len(supporting) > 3:
        obs.append(
            "Supporting chains are shallow — most supporters point "
            "directly at the thesis with no intermediate development."
        )
    return result


# ─── breadth ────────────────────────────────────────


def compute_breadth(graph: AuthorGraph) -> ArgumentBreadth:
    """Score how wide the argument's coverage is across sections,
    sources, claim types, relationship types, and mechanism coverage."""
    result = ArgumentBreadth()

    # ── 1. Section diversity: count of non-thesis, non-references
    # sections. Saturates at 6 — six well-developed sections is plenty
    # for a long-form paper. ──
    body_sections = [
        s for s in graph.sections
        if s.section_id != "s.thesis" and s.role != SectionRole.references
    ]
    result.section_count = len(body_sections)
    result.section_diversity = min(1.0, len(body_sections) / 6)

    # ── 2. Source diversity: distinct cited sources × Shannon entropy
    # of the citation-count distribution. Both penalise narrowness:
    # a single dominant source pulls the score down even if there are
    # nominally many. ──
    source_counts: dict[str, int] = defaultdict(int)
    for claim in graph.claims:
        for ev in claim.evidence:
            if ev.source:
                source_counts[ev.source] += 1
    distinct = len(source_counts)
    result.distinct_source_count = distinct
    distinct_score = min(1.0, distinct / 12)
    if distinct > 0:
        total = sum(source_counts.values())
        # Shannon entropy normalised against log2(distinct).
        entropy = -sum(
            (n / total) * math.log2(n / total) for n in source_counts.values()
        ) if total else 0.0
        normalised_entropy = entropy / math.log2(distinct) if distinct > 1 else 0.0
        # Geometric mean — both have to be high for the score to be high.
        result.source_diversity = math.sqrt(distinct_score * normalised_entropy)
    else:
        result.source_diversity = 0.0

    # ── 3. Claim type diversity: how many of the five claim types
    # appear at least once. ──
    types_present = {c.type for c in graph.claims}
    result.claim_types_present = sorted(t.value for t in types_present)
    result.claim_type_diversity = len(types_present) / len(ClaimType)

    # ── 4. Relationship type diversity: how many of the eight core
    # relationship types appear at least once. ──
    rel_types_present = {r.type for r in graph.relationships}
    rel_types_present_for_diversity = (
        rel_types_present & _REL_TYPES_FOR_DIVERSITY
    )
    result.relationship_types_used = sorted(
        t.value for t in rel_types_present
    )
    result.relationship_type_diversity = (
        len(rel_types_present_for_diversity) / len(_REL_TYPES_FOR_DIVERSITY)
    )

    # ── 5. Mechanism coverage: fraction of empirical+methodological
    # claims that have a non-empty `mechanism` field. ──
    mechanism_eligible = [
        c for c in graph.claims
        if c.type in {ClaimType.empirical, ClaimType.methodological}
    ]
    if mechanism_eligible:
        with_mech = sum(
            1 for c in mechanism_eligible if (c.mechanism or "").strip()
        )
        result.mechanism_coverage = with_mech / len(mechanism_eligible)
    else:
        result.mechanism_coverage = 0.0

    # ── 6. Section spread: 1 − max_section_share. If 80% of claims
    # sit in one section, spread is 0.2. If perfectly even across N
    # sections, spread = 1 − 1/N. ──
    section_claim_counts: dict[str, int] = defaultdict(int)
    body_claims = [c for c in graph.claims if c.claim_id != "cl.thesis"]
    for c in body_claims:
        if c.section_id and any(s.section_id == c.section_id for s in body_sections):
            section_claim_counts[c.section_id] += 1
    total_body = sum(section_claim_counts.values())
    if total_body == 0:
        result.section_spread = 0.0
    else:
        section_concentration = {
            sid: round(n / total_body, 3)
            for sid, n in section_claim_counts.items()
        }
        result.section_concentration = section_concentration
        max_share = max(section_concentration.values())
        result.section_spread = round(1 - max_share, 4)

    # ── Aggregate. Source diversity carries the most weight because
    # citation breadth is the primary dimension along which an academic
    # argument is "wide". ──
    result.score = round(
        0.15 * result.section_diversity
        + 0.25 * result.source_diversity
        + 0.15 * result.claim_type_diversity
        + 0.15 * result.relationship_type_diversity
        + 0.15 * result.mechanism_coverage
        + 0.15 * result.section_spread,
        4,
    )

    # Observations.
    obs = result.observations
    if result.section_count <= 2:
        obs.append(
            f"Only {result.section_count} body section(s). The argument "
            "covers narrow ground; consider whether it has more than one "
            "argumentative move."
        )
    if result.distinct_source_count < 3:
        obs.append(
            f"Only {result.distinct_source_count} distinct source(s) "
            "cited — the citation breadth is thin for an academic argument."
        )
    if result.claim_type_diversity < 0.4:
        obs.append(
            f"Claim types used: {result.claim_types_present}. The "
            "argument only operates in one or two registers — empirical "
            "without methodological/normative moves often reads as flat."
        )
    if result.mechanism_coverage < 0.3 and mechanism_eligible:
        obs.append(
            f"Only {result.mechanism_coverage:.0%} of empirical / "
            "methodological claims have an explicit mechanism. The "
            "argument is descriptive rather than explanatory."
        )
    if (
        result.section_spread < 0.5
        and result.section_count > 1
        and result.section_concentration
    ):
        # Skip the observation if section_concentration is empty —
        # happens when no body claim has a known section, which the
        # rescaffold planner can produce by speculatively promoting
        # claims to offcuts in the in-memory proposed graph.
        dominant = max(
            result.section_concentration.items(),
            key=lambda pair: pair[1],
        )
        obs.append(
            f"Argument is concentrated in section {dominant[0]} "
            f"({dominant[1]:.0%} of body claims). Consider whether other "
            "sections need development."
        )
    return result


# ─── helpers ─────────────────────────────────────────


def _evidence_quality_score(claim) -> float:
    """Score a single claim's evidence quality on [0, 1]. Mirrors the
    diagram's evidence_quality bucketing but emits a number, not a
    label."""
    # Author-declared status takes precedence — it's an explicit signal.
    if claim.evidence_status == EvidenceStatus.bound:
        return 1.0
    if claim.evidence_status == EvidenceStatus.source_hint:
        return 0.5
    if claim.evidence_status == EvidenceStatus.unbound:
        return 0.2
    # No declared status — derive from the evidence list / claim type.
    if claim.evidence:
        strengths = [ev.binding_strength for ev in claim.evidence]
        if any(s == BindingStrength.contradictory for s in strengths):
            return 0.0
        if any(s == BindingStrength.strong for s in strengths):
            return 1.0
        if any(s == BindingStrength.weak for s in strengths):
            return 0.5
        return 0.2
    # No evidence and no status: an author-original synthesis is
    # acceptable cover; a bare empirical claim is a gap.
    if claim.author_origin and claim.type == ClaimType.user_synthesis:
        return 0.7
    return 0.2
