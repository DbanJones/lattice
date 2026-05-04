"""Metrics-driven rescaffold planner.

Given an ``AuthorGraph`` plus its current ``ArgumentMetrics``, propose
a set of structural operations that the metrics predict would lift
the strength + breadth scores. The output is a ``RescaffoldPlan`` —
purely advisory; nothing in this module mutates the on-disk graph.

Design (see DESIGN.md / chat for the long form):

1. **Diagnose** — every sub-score below threshold becomes a
   ``RescaffoldDiagnosis`` entry.
2. **Categorise claims** — backbone (transitively supports thesis,
   high importance), mechanism, counter, setup, synthesis, aside,
   orphan. Drives where each claim wants to live in the rewrite.
3. **Per-rule operation generators** — each weak sub-score emits zero
   or more ``RescaffoldOperation`` instances with a confidence and a
   predicted ``expected_delta``.
4. **Predict deltas** — apply the operations to an in-memory copy of
   the graph and re-run ``compute_argument_metrics``. The difference
   is ``predicted_metrics − current_metrics``.
5. **Compose the plan** — sort operations by predicted-delta
   magnitude × confidence; surface advisories for the rule families
   that no single structural move can fix.
"""

from __future__ import annotations

import copy
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from ..graph.claim_size import claim_sizes
from ..graph.metrics import (
    ArgumentMetrics,
    compute_argument_metrics,
    compute_breadth,
    compute_strength,
)
from ..graph.models import (
    AuthorGraph,
    Claim,
    ClaimType,
    Cluster,
    Relationship,
    RelationshipType,
    Section,
    SectionRole,
)
from ..voice.parser import Voice
from .rescaffold_models import (
    RescaffoldAdvisory,
    RescaffoldDiagnosis,
    RescaffoldOperation,
    RescaffoldPlan,
)


# ─── thresholds ──────────────────────────────────────


# A sub-score below this fires its rule. 0.5 is the rough "half-way"
# heuristic — any lower and the metric is reporting a gap worth a
# structural move; any higher and we'd churn for marginal gains.
_DEFAULT_THRESHOLD = 0.5

# Minimum claim_size for a claim to deserve its own paragraph.
# Below this, claims merge with neighbours.
_OWN_PARAGRAPH_FLOOR = 0.4

# Claims at-or-below this size + zero inbound = offcut candidates.
_OFFCUT_CEILING = 0.2

# Section is "dominant" when it owns this much of the body claims.
_DOMINANT_SECTION_SHARE = 0.45


_STICKY_TYPES: frozenset[RelationshipType] = frozenset({
    RelationshipType.interpretive_pivot,
    RelationshipType.qualifies,
    RelationshipType.extends,
    RelationshipType.depends_on,
    RelationshipType.contradicts,
    RelationshipType.is_counterexample_to,
})

_SUPPORTING_TYPES: frozenset[RelationshipType] = frozenset({
    RelationshipType.supports,
    RelationshipType.extends,
    RelationshipType.depends_on,
    RelationshipType.is_evidence_for,
})


# ─── public entry point ──────────────────────────────


def plan_rescaffold(
    graph: AuthorGraph,
    voice: Voice,
    current_clusters: list[Cluster] | None = None,
    *,
    threshold: float = _DEFAULT_THRESHOLD,
    section_id: str | None = None,
) -> RescaffoldPlan:
    """Produce a metrics-driven rescaffold plan against ``graph``.

    Pure function — no LLM, no I/O. ``current_clusters`` is optional;
    when provided, the planner uses cluster boundaries to avoid moving
    claims across already-rendered cluster groups unnecessarily. Pass
    ``None`` for graph-only planning (e.g. before the assembler has
    run).

    ``section_id`` scopes the planner: when set, only operations and
    advisories that touch claims in that section are emitted. The
    diagnosis is built from the section's metrics rather than the
    document's; the predicted-deltas reflect the section's score
    rather than the whole document.
    """
    sizes = claim_sizes(graph)
    current_metrics = compute_argument_metrics(graph)

    diagnosis = _diagnose(current_metrics, threshold=threshold)
    if not diagnosis:
        # The structure is already healthy — return an empty plan
        # rather than churning for marginal gains.
        return RescaffoldPlan(
            project_name=graph.project_name,
            voice_name=voice.name,
            generated_at=datetime.now(timezone.utc),
            current_metrics=current_metrics.model_dump(),
            predicted_metrics=current_metrics.model_dump(),
            claim_sizes=sizes,
        )

    categories = _categorise_claims(graph, sizes)

    operations: list[RescaffoldOperation] = []
    advisories: list[RescaffoldAdvisory] = []

    # Per-rule generators. Each returns (ops, advisories). Order
    # matters: structural moves first (split/merge/add) so claim
    # placement (move/reorder) can act on the proposed structure.
    weak = {d.sub_score for d in diagnosis}

    if "section_spread" in weak or "section_diversity" in weak:
        ops, adv = _generate_section_split_ops(graph, sizes, categories)
        operations.extend(ops)
        advisories.extend(adv)

    if "counter_handling" in weak:
        ops, adv = _generate_counter_engagement_ops(graph, categories)
        operations.extend(ops)
        advisories.extend(adv)

    if "evidence_backing" in weak:
        advisories.extend(
            _generate_evidence_advisories(graph, current_metrics, categories)
        )

    if "depth" in weak:
        advisories.extend(
            _generate_depth_advisories(graph, categories)
        )

    if "mechanism_coverage" in weak:
        advisories.extend(_generate_mechanism_advisories(graph, sizes))

    if "claim_type_diversity" in weak:
        advisories.extend(_generate_type_diversity_advisories(graph))

    if "source_diversity" in weak:
        advisories.extend(_generate_source_diversity_advisories(graph))

    # Edge-poor graph (≪ 1 relationship per body claim) — fire the
    # infer advisory unconditionally even when type diversity isn't
    # flagged, because density is itself the blocking issue.
    body_count = max(1, sum(1 for c in graph.claims if c.claim_id != "cl.thesis"))
    rel_density = len(graph.relationships) / body_count
    if "relationship_type_diversity" in weak or rel_density < 0.3:
        advisories.append(_advisory_run_inference(current_metrics, graph))

    if "direct_support" in weak:
        ops, adv = _generate_direct_support_ops(graph, sizes, categories)
        operations.extend(ops)
        advisories.extend(adv)

    # Hourglass + skim-target reorder runs unconditionally — even a
    # high-scoring structure can have a section opening on its
    # weakest claim.
    operations.extend(_generate_skim_target_reorders(graph, sizes))

    # Offcut step: claims with very low size and no inbound relationships.
    proposed_offcuts = _identify_offcuts(graph, sizes, categories)
    if proposed_offcuts:
        for cid in proposed_offcuts:
            operations.append(RescaffoldOperation(
                op_id=_op_id(),
                kind="promote_to_offcuts",
                rationale=(
                    f"Claim {cid} has size {sizes.get(cid, 0):.2f} and "
                    "no inbound relationships — likely an aside that "
                    "could move to outline.offcuts.md."
                ),
                confidence=0.55,
                target_claim_id=cid,
            ))

    # Predict deltas by applying every operation to an in-memory copy
    # of the graph and re-running the metrics.
    proposed_graph = _apply_in_memory(graph, operations)
    predicted_metrics = compute_argument_metrics(proposed_graph)

    # Per-op expected_delta — for now we attribute the whole
    # current→predicted delta proportionally to ops by their
    # confidence. A finer breakdown (apply each op alone, measure)
    # is O(|ops| × |graph|); too expensive for large papers. The
    # global delta is what authors actually look at.
    operations = _attribute_deltas(
        operations, current_metrics, predicted_metrics
    )

    # Sort by (confidence × |delta_strength_score|) so the operations
    # most likely to move the needle bubble to the top.
    operations.sort(
        key=lambda op: -(
            op.confidence * abs(op.expected_delta.get("strength.score", 0))
            + op.confidence * abs(op.expected_delta.get("breadth.score", 0))
        )
    )

    # Scope to a single section if requested. We filter AFTER full
    # planning because the planner's per-rule generators look at the
    # whole graph; restricting the inputs would change which rules
    # fire. Filtering the outputs lets the section view inherit
    # global context (e.g. "your thesis isn't connected") while only
    # surfacing actionable items in this section.
    if section_id is not None:
        operations, advisories, proposed_offcuts = _filter_to_section(
            graph, section_id, operations, advisories, proposed_offcuts,
        )

    return RescaffoldPlan(
        project_name=graph.project_name,
        voice_name=voice.name,
        generated_at=datetime.now(timezone.utc),
        diagnosis=diagnosis,
        operations=operations,
        advisories=advisories,
        proposed_offcuts=proposed_offcuts,
        current_metrics=current_metrics.model_dump(),
        predicted_metrics=predicted_metrics.model_dump(),
        claim_sizes=sizes,
        expected_strength_delta=round(
            predicted_metrics.strength.score - current_metrics.strength.score, 4
        ),
        expected_breadth_delta=round(
            predicted_metrics.breadth.score - current_metrics.breadth.score, 4
        ),
    )


# ─── section scoping ─────────────────────────────────


def _filter_to_section(
    graph: AuthorGraph,
    section_id: str,
    operations: list[RescaffoldOperation],
    advisories: list[RescaffoldAdvisory],
    proposed_offcuts: list[str],
) -> tuple[list[RescaffoldOperation], list[RescaffoldAdvisory], list[str]]:
    """Drop operations / advisories / offcuts that don't touch the
    target section. A claim is "in scope" if it lives in the named
    section; an operation is in scope if it touches at least one
    in-scope claim or one in-scope section."""
    section_claim_ids: set[str] = set()
    section_ids = {section_id}
    for claim in graph.claims:
        if claim.section_id == section_id:
            section_claim_ids.add(claim.claim_id)
    # Also include nested subsections (s.b.1 is in scope if section_id == s.b).
    for s in graph.sections:
        if s.parent == section_id:
            section_ids.add(s.section_id)
            for claim in graph.claims:
                if claim.section_id == s.section_id:
                    section_claim_ids.add(claim.claim_id)

    def _op_touches_section(op: RescaffoldOperation) -> bool:
        if op.target_claim_id and op.target_claim_id in section_claim_ids:
            return True
        if op.source_section_id in section_ids:
            return True
        if op.target_section_id in section_ids:
            return True
        if op.section_ids_to_merge and any(
            sid in section_ids for sid in op.section_ids_to_merge
        ):
            return True
        if op.split_groups:
            for group in op.split_groups:
                if any(cid in section_claim_ids for cid in group):
                    return True
        if op.claim_order and any(
            cid in section_claim_ids for cid in op.claim_order
        ):
            return True
        return False

    def _advisory_touches_section(a: RescaffoldAdvisory) -> bool:
        # advisories without a target_claim_id (document-wide
        # recommendations like add_methodological_framing) propagate —
        # they're context the section author needs to know about.
        if a.target_claim_id is None and a.target_section_id is None:
            return True
        if a.target_claim_id in section_claim_ids:
            return True
        if a.target_section_id in section_ids:
            return True
        return False

    return (
        [op for op in operations if _op_touches_section(op)],
        [a for a in advisories if _advisory_touches_section(a)],
        [cid for cid in proposed_offcuts if cid in section_claim_ids],
    )


# ─── diagnose ────────────────────────────────────────


def _diagnose(metrics: ArgumentMetrics, *, threshold: float) -> list[RescaffoldDiagnosis]:
    rows: list[RescaffoldDiagnosis] = []
    sub_score_weights = (
        ("strength", "direct_support", metrics.strength.direct_support, 0.4),
        ("strength", "reachable_support", metrics.strength.reachable_support, 0.4),
        ("strength", "evidence_backing", metrics.strength.evidence_backing, 0.5),
        ("strength", "counter_handling", metrics.strength.counter_handling, 0.5),
        ("strength", "depth", metrics.strength.depth, 0.4),
        ("breadth", "section_diversity", metrics.breadth.section_diversity, 0.5),
        ("breadth", "source_diversity", metrics.breadth.source_diversity, 0.5),
        # 0.5 = "fewer than 3 of the 5 claim types present" — any
        # less and the document is operating in only one or two
        # registers, which the rule wants to surface.
        ("breadth", "claim_type_diversity", metrics.breadth.claim_type_diversity, 0.5),
        ("breadth", "relationship_type_diversity",
            metrics.breadth.relationship_type_diversity, 0.4),
        ("breadth", "mechanism_coverage", metrics.breadth.mechanism_coverage, 0.4),
        ("breadth", "section_spread", metrics.breadth.section_spread, 0.5),
    )
    for dimension, name, value, override_threshold in sub_score_weights:
        if value >= override_threshold:
            continue
        if value < override_threshold * 0.5:
            severity = "critical"
        elif value < override_threshold * 0.75:
            severity = "warning"
        else:
            severity = "info"
        rows.append(RescaffoldDiagnosis(
            dimension=dimension,
            sub_score=name,
            value=round(value, 4),
            threshold=round(override_threshold, 4),
            severity=severity,
            message=_diagnosis_message(name, value, override_threshold),
        ))
    return rows


def _diagnosis_message(name: str, value: float, threshold: float) -> str:
    return (
        f"{name} is {value:.2f} (target ≥ {threshold:.2f}); "
        + {
            "direct_support":
                "the thesis lacks a clear set of headline supporters.",
            "reachable_support":
                "much of the body is disconnected from the thesis.",
            "evidence_backing":
                "supporting claims are weakly grounded.",
            "counter_handling":
                "counter-arguments aren't engaged.",
            "depth":
                "supporting chains are flat (one-step proofs).",
            "section_diversity":
                "the document covers narrow ground.",
            "source_diversity":
                "citation breadth is thin.",
            "claim_type_diversity":
                "the argument operates in only one or two registers.",
            "relationship_type_diversity":
                "claims connect through too few relationship types.",
            "mechanism_coverage":
                "empirical claims lack causal mechanisms.",
            "section_spread":
                "claims pile into one section.",
        }.get(name, "below threshold.")
    )


# ─── categorisation ─────────────────────────────────


def _categorise_claims(
    graph: AuthorGraph, sizes: dict[str, float],
) -> dict[str, str]:
    """Return ``{claim_id: category}`` mapping. Categories:

    backbone | mechanism | counter | setup | synthesis | aside | orphan

    Used by every operation generator to decide *what* a claim is,
    independent of where it currently sits.
    """
    claims_by_id = {c.claim_id: c for c in graph.claims}
    inbound: dict[str, list[Relationship]] = defaultdict(list)
    outbound: dict[str, list[Relationship]] = defaultdict(list)
    for rel in graph.relationships:
        inbound[rel.to_claim].append(rel)
        outbound[rel.from_claim].append(rel)

    # BFS backwards from cl.thesis through supporting edges.
    backbone: set[str] = set()
    if "cl.thesis" in claims_by_id:
        queue = deque(["cl.thesis"])
        seen = {"cl.thesis"}
        while queue:
            node = queue.popleft()
            for rel in inbound[node]:
                if rel.type in _SUPPORTING_TYPES and rel.from_claim not in seen:
                    seen.add(rel.from_claim)
                    backbone.add(rel.from_claim)
                    queue.append(rel.from_claim)

    # Claims contradicting the thesis (or backbone members).
    counters: set[str] = set()
    for rel in graph.relationships:
        if rel.type == RelationshipType.contradicts:
            if rel.to_claim == "cl.thesis" or rel.to_claim in backbone:
                counters.add(rel.from_claim)

    # Reachable-from-thesis (any relationship type) — everything else
    # is an orphan candidate.
    reachable: set[str] = set()
    if "cl.thesis" in claims_by_id:
        queue = deque(["cl.thesis"])
        reachable.add("cl.thesis")
        while queue:
            node = queue.popleft()
            for rel in inbound[node] + outbound[node]:
                other = rel.from_claim if rel.to_claim == node else rel.to_claim
                if other not in reachable:
                    reachable.add(other)
                    queue.append(other)

    out: dict[str, str] = {}
    for claim in graph.claims:
        cid = claim.claim_id
        if cid == "cl.thesis":
            out[cid] = "thesis"
            continue

        role_tags = [t.split(":", 1)[1] for t in claim.tags if t.startswith("role:")]
        size = sizes.get(cid, 0.5)

        if cid in counters:
            out[cid] = "counter"
        elif cid in backbone and size >= 0.55:
            out[cid] = "backbone"
        elif claim.mechanism or "mechanism" in role_tags:
            out[cid] = "mechanism"
        elif (
            claim.type == ClaimType.user_synthesis
            and (claim.author_origin or "synthesis" in role_tags or "conclusion" in role_tags)
            and size >= 0.5
        ):
            out[cid] = "synthesis"
        elif "setup" in role_tags or claim.type == ClaimType.definition:
            out[cid] = "setup"
        elif cid not in reachable:
            out[cid] = "orphan"
        elif size <= _OFFCUT_CEILING:
            out[cid] = "aside"
        elif cid in backbone:
            out[cid] = "backbone"
        else:
            out[cid] = "body"
    return out


# ─── per-rule operation generators ───────────────────


def _generate_section_split_ops(
    graph: AuthorGraph, sizes: dict[str, float], categories: dict[str, str],
) -> tuple[list[RescaffoldOperation], list[RescaffoldAdvisory]]:
    """If one section dominates the body, propose splitting it by
    sticky-edge connected components."""
    body_sections = [
        s for s in graph.sections
        if s.section_id != "s.thesis"
        and s.role != SectionRole.references
    ]
    if not body_sections:
        return [], []

    # Find the dominant section (by claim count).
    counts = {s.section_id: len(s.claim_ids) for s in body_sections}
    total = sum(counts.values())
    if total == 0:
        return [], []
    dominant_id = max(counts, key=counts.get)
    if counts[dominant_id] / total < _DOMINANT_SECTION_SHARE:
        return [], []
    dominant = next(s for s in body_sections if s.section_id == dominant_id)
    if len(dominant.claim_ids) < 4:
        return [], []  # not worth splitting tiny sections

    # Sticky-edge components within the dominant section.
    members = set(dominant.claim_ids)
    parent: dict[str, str] = {cid: cid for cid in members}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for rel in graph.relationships:
        if (
            rel.type in _STICKY_TYPES
            and rel.from_claim in members
            and rel.to_claim in members
        ):
            union(rel.from_claim, rel.to_claim)

    components: dict[str, list[str]] = defaultdict(list)
    for cid in members:
        components[find(cid)].append(cid)
    groups = sorted(components.values(), key=len, reverse=True)

    # Sticky-edge components only give a clean fault line when at
    # least one component has 2+ members (real connections, not just
    # isolated claims). If everything is a singleton, fall back to a
    # claim_size-balanced 2-way split — proposing 7 single-claim
    # subsections is worse than 2 balanced ones.
    has_real_component = any(len(g) >= 2 for g in groups)
    if not has_real_component or len(groups) > 5:
        ordered = sorted(
            members,
            key=lambda c: -sizes.get(c, 0.5),
        )
        half = max(1, len(ordered) // 2)
        groups = [ordered[:half], ordered[half:]]

    # Make sure the largest group ends up first so the original
    # section_id keeps its anchor cluster.
    groups.sort(key=len, reverse=True)

    op = RescaffoldOperation(
        op_id=_op_id(),
        kind="split_section",
        rationale=(
            f"Section {dominant.section_id!r} ({dominant.title!r}) holds "
            f"{counts[dominant_id]}/{total} body claims "
            f"({counts[dominant_id]/total:.0%}). Splitting into "
            f"{len(groups)} subsections by sticky-edge components would "
            "raise section_spread + section_diversity."
        ),
        confidence=0.7,
        source_section_id=dominant.section_id,
        split_groups=[sorted(g) for g in groups],
    )
    return [op], []


def _generate_counter_engagement_ops(
    graph: AuthorGraph, categories: dict[str, str],
) -> tuple[list[RescaffoldOperation], list[RescaffoldAdvisory]]:
    """For each unaddressed counter-claim, propose either a
    counter-engagement section stub or an advisory pointing at the
    pivot/qualifier the author should add."""
    inbound: dict[str, list[Relationship]] = defaultdict(list)
    for rel in graph.relationships:
        inbound[rel.to_claim].append(rel)

    counter_handling = {
        RelationshipType.contradicts,
        RelationshipType.qualifies,
        RelationshipType.interpretive_pivot,
        RelationshipType.is_counterexample_to,
    }
    unaddressed: list[str] = []
    for cid, cat in categories.items():
        if cat != "counter":
            continue
        if not any(rel.type in counter_handling for rel in inbound[cid]):
            unaddressed.append(cid)

    if not unaddressed:
        return [], []

    ops: list[RescaffoldOperation] = []
    advisories: list[RescaffoldAdvisory] = []

    # If the document has no section with role=counterargument, propose
    # adding one (single op covers all unaddressed counters).
    has_counter_section = any(
        s.role == SectionRole.counterargument for s in graph.sections
    )
    if not has_counter_section and len(unaddressed) >= 2:
        ops.append(RescaffoldOperation(
            op_id=_op_id(),
            kind="add_section_stub",
            rationale=(
                f"{len(unaddressed)} unaddressed counter-claim(s) — the "
                "argument never engages with the strongest objections. "
                "A dedicated counterargument section would lift "
                "counter_handling and force the rebuttal moves."
            ),
            confidence=0.65,
            new_section_role="counterargument",
            new_section_title="Counter-arguments",
            target_section_id="new:counterargument",
        ))

    # Per-counter advisory naming the pivot/qualifier the author needs
    # to add.
    for cid in unaddressed:
        advisories.append(RescaffoldAdvisory(
            advisory_id=_op_id("adv"),
            kind="add_counter_engagement",
            target_claim_id=cid,
            rationale=(
                f"Counter-claim {cid} contradicts the thesis (or a "
                "backbone claim) and nothing pivots, qualifies, or "
                "rebuts it. The argument leaves the strongest objection "
                "standing."
            ),
            confidence=0.7,
            suggestion=(
                f"Add a claim with [pivot: {cid}] (interpretive pivot — "
                "diagnose the analytical move the counter makes), or "
                f"[qualifies: {cid}] (constrain its scope), or another "
                f"claim with [contradicts: {cid}] (direct rebuttal)."
            ),
        ))
    return ops, advisories


def _generate_evidence_advisories(
    graph: AuthorGraph,
    metrics: ArgumentMetrics,
    categories: dict[str, str],
) -> list[RescaffoldAdvisory]:
    """For the weakest supporters surfaced by the strength metric,
    emit a bind-evidence advisory each."""
    advisories: list[RescaffoldAdvisory] = []
    for cid in metrics.strength.weakest_supporters:
        claim = next((c for c in graph.claims if c.claim_id == cid), None)
        if claim is None:
            continue
        advisories.append(RescaffoldAdvisory(
            advisory_id=_op_id("adv"),
            kind="bind_evidence",
            target_claim_id=cid,
            rationale=(
                f"{cid} sits on the supporting subgraph but its evidence "
                "quality is weak — binding it would directly raise "
                "evidence_backing."
            ),
            confidence=0.75,
            suggestion=(
                "Add a [ref:] tag pointing at a known indexed source, "
                "or set [evidence_status: source_hint] / [bound] if "
                "you've located the passage. If author-grounded, "
                "convert to [type: user_synthesis]."
            ),
        ))
    return advisories


def _generate_depth_advisories(
    graph: AuthorGraph, categories: dict[str, str],
) -> list[RescaffoldAdvisory]:
    """When ``depth`` is low, the typical pattern is "I assert X →
    therefore thesis" with no intermediate steps. Surface candidate
    claims for an inserted mechanism step."""
    inbound: dict[str, list[Relationship]] = defaultdict(list)
    for rel in graph.relationships:
        inbound[rel.to_claim].append(rel)

    advisories: list[RescaffoldAdvisory] = []
    for rel in graph.relationships:
        if rel.to_claim != "cl.thesis":
            continue
        if rel.type not in _SUPPORTING_TYPES:
            continue
        # Direct supporter — does it have an inbound supporting edge of
        # its own?
        direct = rel.from_claim
        has_predecessor = any(
            r.type in _SUPPORTING_TYPES for r in inbound[direct]
        )
        if has_predecessor:
            continue
        advisories.append(RescaffoldAdvisory(
            advisory_id=_op_id("adv"),
            kind="add_mechanism",
            target_claim_id=direct,
            rationale=(
                f"{direct} supports the thesis directly with no "
                "intermediate step — the proof is one-shot. A mechanism "
                "claim explaining *why* this supports the thesis would "
                "deepen the argument."
            ),
            confidence=0.55,
            suggestion=(
                f"Add an intermediate claim with [supports: {direct}] "
                "and [type: methodological], or set the [mechanism: ...] "
                f"tag on {direct} itself."
            ),
        ))
    return advisories


def _generate_mechanism_advisories(
    graph: AuthorGraph, sizes: dict[str, float],
) -> list[RescaffoldAdvisory]:
    advisories: list[RescaffoldAdvisory] = []
    for claim in graph.claims:
        if claim.type not in (ClaimType.empirical, ClaimType.methodological):
            continue
        if (claim.mechanism or "").strip():
            continue
        if claim.importance < 0.6:
            continue  # only flag the high-importance ones
        advisories.append(RescaffoldAdvisory(
            advisory_id=_op_id("adv"),
            kind="add_mechanism",
            target_claim_id=claim.claim_id,
            rationale=(
                f"{claim.claim_id} is a high-importance "
                f"{claim.type.value} claim with no [mechanism:] tag — "
                "the argument states *that* it holds without explaining "
                "*why*."
            ),
            confidence=0.6,
            suggestion=(
                "Add [mechanism: <causal middle link>] to the bullet. "
                "Short, declarative, in your own terms."
            ),
        ))
    return advisories


def _generate_type_diversity_advisories(graph: AuthorGraph) -> list[RescaffoldAdvisory]:
    types_present = {c.type for c in graph.claims}
    advisories: list[RescaffoldAdvisory] = []
    if ClaimType.methodological not in types_present:
        advisories.append(RescaffoldAdvisory(
            advisory_id=_op_id("adv"),
            kind="add_methodological_framing",
            rationale=(
                "No methodological claims — the argument operates "
                "purely in empirical/synthesis register. Adding a "
                "methodological framing claim near the start of the "
                "argumentative body sets up *how* you reach your "
                "conclusions."
            ),
            confidence=0.5,
            suggestion=(
                "Add a claim like 'I assess X using Y' tagged "
                "[type: methodological] in the section that introduces "
                "the analytical move."
            ),
        ))
    if ClaimType.normative not in types_present:
        advisories.append(RescaffoldAdvisory(
            advisory_id=_op_id("adv"),
            kind="add_methodological_framing",
            rationale=(
                "No normative claims — the document doesn't state "
                "*what should follow* from its findings. Conclusions "
                "without normative force read as descriptive."
            ),
            confidence=0.45,
            suggestion=(
                "Add a [type: normative] claim near the end of the "
                "conclusion making the should-follow explicit."
            ),
        ))
    return advisories


def _generate_source_diversity_advisories(graph: AuthorGraph) -> list[RescaffoldAdvisory]:
    counts: dict[str, int] = defaultdict(int)
    for claim in graph.claims:
        for ev in claim.evidence:
            if ev.source:
                counts[ev.source] += 1
    if not counts:
        return []
    total = sum(counts.values())
    dominant_source, dominant_count = max(counts.items(), key=lambda kv: kv[1])
    if dominant_count / total < 0.4:
        return []
    return [RescaffoldAdvisory(
        advisory_id=_op_id("adv"),
        kind="diversify_sources",
        rationale=(
            f"Source {dominant_source!r} accounts for "
            f"{dominant_count/total:.0%} of citations — the body leans "
            "on one paper. Either add corroborating sources to the "
            "claims that cite it, or convert author-original "
            "interpretations to user_synthesis."
        ),
        confidence=0.55,
        suggestion=(
            f"Run lit_gaps to surface candidate corroborating sources "
            f"for claims citing {dominant_source}."
        ),
    )]


def _advisory_run_inference(
    metrics: ArgumentMetrics, graph: AuthorGraph,
) -> RescaffoldAdvisory:
    body_count = max(1, sum(1 for c in graph.claims if c.claim_id != "cl.thesis"))
    rel_density = len(graph.relationships) / body_count
    # On an edge-poor graph (< 0.3 relationships per body claim),
    # this is the BLOCKING prerequisite: every other rule is
    # mis-firing because there are no edges to reason about.
    # Boost confidence to 1.0 so this advisory bubbles to the top.
    if rel_density < 0.3:
        return RescaffoldAdvisory(
            advisory_id=_op_id("adv"),
            kind="infer_relationships",
            rationale=(
                f"Graph has only {len(graph.relationships)} relationships "
                f"across {body_count} body claims ({rel_density:.2f} per "
                "claim). Every other diagnosis is unreliable until the "
                "graph has edges to reason about. Run inference FIRST, "
                "then re-plan."
            ),
            confidence=1.0,
            suggestion="Run `lattice annotate` (or the Scaffold activity in thorough mode).",
        )
    return RescaffoldAdvisory(
        advisory_id=_op_id("adv"),
        kind="infer_relationships",
        rationale=(
            "Only "
            f"{len(metrics.breadth.relationship_types_used)} relationship "
            "type(s) used. Running the relationship-inference pass would "
            "surface qualifies / extends / depends_on candidates the "
            "outline didn't tag explicitly."
        ),
        confidence=0.7,
        suggestion="Run `lattice annotate` (or the Scaffold activity in thorough mode).",
    )


def _generate_direct_support_ops(
    graph: AuthorGraph, sizes: dict[str, float], categories: dict[str, str],
) -> tuple[list[RescaffoldOperation], list[RescaffoldAdvisory]]:
    """When direct_support is weak, find the highest-importance claims
    that aren't connected to the thesis and propose tagging them
    [supports: thesis]."""
    advisories: list[RescaffoldAdvisory] = []
    candidates = sorted(
        (
            c for c in graph.claims
            if c.claim_id != "cl.thesis"
            and c.importance >= 0.6
            and categories.get(c.claim_id) not in {"counter"}
        ),
        key=lambda c: -c.importance,
    )
    direct_supports = {
        rel.from_claim for rel in graph.relationships
        if rel.to_claim == "cl.thesis"
        and rel.type in _SUPPORTING_TYPES
    }
    proposed = 0
    for claim in candidates:
        if claim.claim_id in direct_supports:
            continue
        advisories.append(RescaffoldAdvisory(
            advisory_id=_op_id("adv"),
            kind="tag_supports_thesis",
            target_claim_id=claim.claim_id,
            rationale=(
                f"{claim.claim_id} is high-importance "
                f"({claim.importance:.2f}) but no edge ties it to the "
                "thesis. Tagging it would lift direct_support."
            ),
            confidence=0.55,
            suggestion=(
                f"Add [supports: thesis] to bullet {claim.claim_id} in "
                "the outline (or [extends: thesis] if it broadens "
                "rather than supports)."
            ),
        ))
        proposed += 1
        if proposed >= 3:
            break
    return [], advisories


def _generate_skim_target_reorders(
    graph: AuthorGraph, sizes: dict[str, float],
) -> list[RescaffoldOperation]:
    """For each section, ensure the highest-claim_size claim leads
    (skim-target principle: section openers carry the most argument
    weight). Only proposes reorders that change the actual order — no
    no-op operations."""
    ops: list[RescaffoldOperation] = []
    for section in graph.sections:
        if section.section_id == "s.thesis":
            continue
        if section.role == SectionRole.references:
            continue
        if len(section.claim_ids) < 2:
            continue
        ranked = sorted(
            section.claim_ids,
            key=lambda c: -sizes.get(c, 0.5),
        )
        if ranked == list(section.claim_ids):
            continue
        # Only propose if the leading claim differs from the current.
        if ranked[0] == section.claim_ids[0]:
            continue
        ops.append(RescaffoldOperation(
            op_id=_op_id(),
            kind="reorder_within_section",
            rationale=(
                f"Section {section.section_id!r} opens on "
                f"{section.claim_ids[0]} (size "
                f"{sizes.get(section.claim_ids[0], 0):.2f}) but "
                f"{ranked[0]} (size {sizes.get(ranked[0], 0):.2f}) is "
                "heavier. Skim-target principle: the strongest claim "
                "leads."
            ),
            confidence=0.5,
            source_section_id=section.section_id,
            target_section_id=section.section_id,
            claim_order=ranked,
        ))
    return ops


def _identify_offcuts(
    graph: AuthorGraph,
    sizes: dict[str, float],
    categories: dict[str, str],
) -> list[str]:
    """Identify low-weight claims worth moving to outline.offcuts.md.

    Conservative on purpose:

    - Skip when the body has fewer than 4 claims (small papers don't
      have offcut budget; everything contributes).
    - Cap the proposed offcut count at half the body, so the planner
      can never propose dropping more than 50% of the document — a
      relationship-free scaffold (every claim is technically "orphan"
      because nothing connects to the thesis yet) shouldn't trigger a
      bulk delete.
    - "Orphan" alone isn't enough: require ALSO size ≤ ``_OFFCUT_CEILING``,
      so a high-importance claim that just hasn't been wired up yet stays
      in the document.
    """
    body_claims = [c for c in graph.claims if c.claim_id != "cl.thesis"]
    if len(body_claims) < 2:
        return []

    # Suppress orphan-based offcuts on edge-poor graphs. When the
    # relationship density is below 0.3 edges per body claim, every
    # claim looks orphaned because the graph hasn't been
    # edge-enriched yet — the right action is `lattice annotate`,
    # not bulk deletion. We learned this from running the planner
    # against first_year_report (0 rels, 97 claims), which proposed
    # promoting 48 claims to offcuts.
    rel_density = len(graph.relationships) / len(body_claims)
    if rel_density < 0.3:
        return []

    inbound_count: dict[str, int] = defaultdict(int)
    for rel in graph.relationships:
        inbound_count[rel.to_claim] += 1

    candidates: list[tuple[str, float]] = []
    for claim in body_claims:
        size = sizes.get(claim.claim_id, 0.5)
        if size > _OFFCUT_CEILING:
            continue  # too valuable to drop, even if orphaned
        cat = categories.get(claim.claim_id, "body")
        if cat in {"backbone", "synthesis", "counter", "mechanism"}:
            continue  # structurally important regardless of size
        if cat == "orphan" or inbound_count[claim.claim_id] == 0:
            candidates.append((claim.claim_id, size))

    # Cap so that the planner can never propose dropping more than
    # (body − 2) claims — at least two non-offcut body claims must
    # remain so the in-memory simulation has something to score
    # against. For a 3-claim body that's at most 1 offcut; for a
    # 10-claim body, at most 8 — the half-the-body cap kicks in
    # before the absolute one.
    candidates.sort(key=lambda pair: pair[1])
    max_offcuts = min(len(body_claims) // 2, max(0, len(body_claims) - 2))
    return sorted(cid for cid, _ in candidates[:max_offcuts])


# ─── apply in-memory ─────────────────────────────────


def _apply_in_memory(
    graph: AuthorGraph, operations: list[RescaffoldOperation],
) -> AuthorGraph:
    """Build a deep copy of ``graph`` with every operation applied.
    Used only for predicted-delta scoring; never written to disk.

    Some operations don't change the graph shape directly (e.g.
    ``add_section_stub`` adds a section but no claims, so metrics
    don't move from that alone). Each op is applied as conservatively
    as possible — the metrics will only reward operations that
    actually shift the structure.
    """
    g = copy.deepcopy(graph)
    sections_by_id = {s.section_id: s for s in g.sections}

    for op in operations:
        if op.kind == "move_claim":
            _apply_move_claim(g, op, sections_by_id)
        elif op.kind == "split_section":
            _apply_split_section(g, op, sections_by_id)
        elif op.kind == "add_section_stub":
            _apply_add_section_stub(g, op, sections_by_id)
        elif op.kind == "reorder_within_section":
            _apply_reorder(g, op, sections_by_id)
        elif op.kind == "promote_to_offcuts":
            _apply_promote_to_offcuts(g, op, sections_by_id)
        # merge_sections deferred — too easy to produce a worse layout
        # if the metric weights aren't carefully balanced.
    return g


def _apply_move_claim(g, op, sections_by_id) -> None:
    if not (op.target_claim_id and op.source_section_id and op.target_section_id):
        return
    src = sections_by_id.get(op.source_section_id)
    tgt = sections_by_id.get(op.target_section_id)
    if src and op.target_claim_id in src.claim_ids:
        src.claim_ids = [c for c in src.claim_ids if c != op.target_claim_id]
    if tgt and op.target_claim_id not in tgt.claim_ids:
        if op.target_position is not None:
            tgt.claim_ids.insert(op.target_position, op.target_claim_id)
        else:
            tgt.claim_ids.append(op.target_claim_id)
    for c in g.claims:
        if c.claim_id == op.target_claim_id:
            c.section_id = op.target_section_id
            break


def _apply_split_section(g, op, sections_by_id) -> None:
    if not op.source_section_id or not op.split_groups:
        return
    parent = sections_by_id.get(op.source_section_id)
    if parent is None:
        return
    # Group 0 keeps the parent's id; subsequent groups become subsections.
    first, *rest = op.split_groups
    parent.claim_ids = list(first)
    next_position = max(s.position for s in g.sections) + 1
    for i, group in enumerate(rest, start=1):
        new_id = f"{parent.section_id}.split{i}"
        new_section = Section(
            section_id=new_id,
            title=f"{parent.title} (split {i+1})",
            parent=parent.section_id,
            position=next_position,
            role=parent.role,
            thesis_claim=parent.thesis_claim,
            claim_ids=list(group),
            target_length=parent.target_length // (len(op.split_groups) or 1),
            depth=parent.depth,
        )
        next_position += 1
        g.sections.append(new_section)
        sections_by_id[new_id] = new_section
        for cid in group:
            for c in g.claims:
                if c.claim_id == cid:
                    c.section_id = new_id
                    break


def _apply_add_section_stub(g, op, sections_by_id) -> None:
    if not op.new_section_role or not op.new_section_title:
        return
    new_id = op.target_section_id or f"new:{op.new_section_role}"
    if new_id in sections_by_id:
        return
    role_enum = _coerce_role(op.new_section_role)
    next_position = max((s.position for s in g.sections), default=-1) + 1
    new_section = Section(
        section_id=new_id,
        title=op.new_section_title,
        position=next_position,
        role=role_enum,
        claim_ids=[],
    )
    g.sections.append(new_section)
    sections_by_id[new_id] = new_section


def _apply_reorder(g, op, sections_by_id) -> None:
    if not op.source_section_id or not op.claim_order:
        return
    section = sections_by_id.get(op.source_section_id)
    if section is None:
        return
    # Only keep IDs that are actually in the section to avoid dropping claims.
    valid = [c for c in op.claim_order if c in set(section.claim_ids)]
    missing = [c for c in section.claim_ids if c not in set(valid)]
    section.claim_ids = valid + missing


def _apply_promote_to_offcuts(g, op, sections_by_id) -> None:
    if not op.target_claim_id:
        return
    # Drop the claim from any section's claim_ids and from the claims list.
    for s in g.sections:
        if op.target_claim_id in s.claim_ids:
            s.claim_ids = [c for c in s.claim_ids if c != op.target_claim_id]
    g.claims = [c for c in g.claims if c.claim_id != op.target_claim_id]
    g.relationships = [
        r for r in g.relationships
        if r.from_claim != op.target_claim_id and r.to_claim != op.target_claim_id
    ]


def _coerce_role(role_str: str) -> SectionRole:
    try:
        return SectionRole(role_str)
    except ValueError:
        return SectionRole.argumentative


def _attribute_deltas(
    operations: list[RescaffoldOperation],
    current: ArgumentMetrics,
    predicted: ArgumentMetrics,
) -> list[RescaffoldOperation]:
    """Attribute the global metric delta to each operation in
    proportion to its confidence × normalised weight. Cheap and lossy
    — but better than each op showing zero. A future iteration could
    apply ops one-at-a-time and measure the per-op delta directly."""
    if not operations:
        return operations
    score_keys = (
        ("strength.score", current.strength.score, predicted.strength.score),
        ("breadth.score", current.breadth.score, predicted.breadth.score),
        ("strength.counter_handling",
         current.strength.counter_handling, predicted.strength.counter_handling),
        ("breadth.section_spread",
         current.breadth.section_spread, predicted.breadth.section_spread),
        ("breadth.section_diversity",
         current.breadth.section_diversity, predicted.breadth.section_diversity),
    )
    total_confidence = sum(op.confidence for op in operations) or 1.0
    for op in operations:
        share = op.confidence / total_confidence
        for key, c, p in score_keys:
            op.expected_delta[key] = round((p - c) * share, 4)
    return operations


# ─── ids ─────────────────────────────────────────────


def _op_id(prefix: str = "op") -> str:
    return f"{prefix}.{uuid.uuid4().hex[:8]}"
