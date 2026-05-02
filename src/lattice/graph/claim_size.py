"""Per-claim "size" — how much weight a claim carries in the document.

Distinct from ``Claim.importance`` (the author's stated 0–1 weight) and
from the cluster's ``target_words`` band (the renderer's word budget).
``claim_size`` blends importance with structural signals — evidence
count, mechanism presence, scope specificity, in/out-degree — into a
single number in [0, 1] used by the rescaffold planner to decide:

- whether a claim warrants its own paragraph (cluster) vs merging,
- which claim should anchor a section (highest size = skim target),
- which claims are cut candidates when the planner needs to lighten a
  section (lowest size, no inbound edges).

Pure function. Cheap to compute (O(|relationships|) once for the
in/out-degree pass; O(1) per claim after that)."""

from __future__ import annotations

from collections import defaultdict

from .models import AuthorGraph, Claim


def claim_size(claim: Claim, graph: AuthorGraph) -> float:
    """Compute claim_size in [0, 1] for a single claim.

    The weighting reflects what tends to need development in academic
    prose:

    - ``importance`` (40%) — author's explicit priority dominates.
    - ``evidence_weight`` (20%) — bound claims earn more space because
      the prose has to engage with each source.
    - ``has_mechanism`` (15%) — explaining a causal link eats words.
    - ``scope_specificity`` (10%) — scope conditions add qualifying
      sentences.
    - ``rel_weight`` (15%) — well-connected claims anchor structure.

    Cap each component at 1.0 so a wildly-cited claim doesn't dominate
    the average; the cluster-level word budget takes over from there.
    """
    in_degree, out_degree = _degree_counts(graph, claim.claim_id)
    return _claim_size_from_inputs(
        importance=claim.importance,
        evidence_count=len(claim.evidence),
        has_mechanism=bool((claim.mechanism or "").strip()),
        scope_count=len(claim.scope_conditions),
        in_degree=in_degree,
        out_degree=out_degree,
    )


def claim_sizes(graph: AuthorGraph) -> dict[str, float]:
    """Compute claim_size for every claim in ``graph`` in one pass.

    Faster than calling ``claim_size`` per claim because the in/out-
    degree pass over the relationship list runs once.
    """
    in_deg: dict[str, int] = defaultdict(int)
    out_deg: dict[str, int] = defaultdict(int)
    for rel in graph.relationships:
        out_deg[rel.from_claim] += 1
        in_deg[rel.to_claim] += 1
    return {
        c.claim_id: _claim_size_from_inputs(
            importance=c.importance,
            evidence_count=len(c.evidence),
            has_mechanism=bool((c.mechanism or "").strip()),
            scope_count=len(c.scope_conditions),
            in_degree=in_deg.get(c.claim_id, 0),
            out_degree=out_deg.get(c.claim_id, 0),
        )
        for c in graph.claims
    }


# ─── internals ───────────────────────────────────────


def _degree_counts(graph: AuthorGraph, claim_id: str) -> tuple[int, int]:
    in_d = sum(1 for r in graph.relationships if r.to_claim == claim_id)
    out_d = sum(1 for r in graph.relationships if r.from_claim == claim_id)
    return in_d, out_d


def _claim_size_from_inputs(
    *,
    importance: float,
    evidence_count: int,
    has_mechanism: bool,
    scope_count: int,
    in_degree: int,
    out_degree: int,
) -> float:
    importance = max(0.0, min(1.0, importance))
    evidence_weight = min(1.0, evidence_count / 3)
    scope_specificity = min(1.0, scope_count / 2)
    # Total degree saturating at 4 — beyond that, extra edges don't
    # add structural anchoring, they're just noise.
    rel_weight = min(1.0, (in_degree + out_degree) / 4)
    mechanism_weight = 1.0 if has_mechanism else 0.0

    score = (
        0.40 * importance
        + 0.20 * evidence_weight
        + 0.15 * mechanism_weight
        + 0.10 * scope_specificity
        + 0.15 * rel_weight
    )
    # Defensive clamp — the components are already in [0, 1] and the
    # weights sum to 1.0, but float arithmetic can produce 1.0000001.
    return round(max(0.0, min(1.0, score)), 4)
