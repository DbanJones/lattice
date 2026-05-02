"""Tests for graph.claim_size — the per-claim weight used by the
rescaffold planner."""
from __future__ import annotations

from datetime import datetime, timezone

from lattice.graph.claim_size import claim_size, claim_sizes
from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimType, Confidence,
    Evidence, Relationship, RelationshipStrength, RelationshipType,
    Section, SectionRole,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _claim(claim_id: str, *, importance: float = 0.5,
           evidence: list[Evidence] | None = None,
           mechanism: str | None = None,
           scope_conditions: list[str] | None = None) -> Claim:
    now = _now()
    return Claim(
        claim_id=claim_id,
        statement=f"Statement of {claim_id}",
        type=ClaimType.empirical,
        confidence=Confidence.medium,
        importance=importance,
        evidence=evidence or [],
        mechanism=mechanism,
        scope_conditions=scope_conditions or [],
        section_id="s.a",
        created_by="t",
        created_at=now, modified_at=now,
    )


def _graph(claims: list[Claim], rels: list[Relationship] | None = None) -> AuthorGraph:
    now = _now()
    return AuthorGraph(
        project_name="t", thesis_statement="T.",
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative,
                          claim_ids=[c.claim_id for c in claims])],
        claims=claims,
        relationships=rels or [],
        created_at=now, modified_at=now,
    )


# ─── importance dominates ────────────────────────────


def test_importance_zero_yields_low_size() -> None:
    """With importance=0 and nothing else set, the size floor is the
    sum of the other component contributions = 0 → 0.0 exactly."""
    g = _graph([_claim("cl.x", importance=0.0)])
    assert claim_size(g.claims[0], g) == 0.0


def test_importance_one_alone_gives_at_least_0_4() -> None:
    """40% weight on importance: importance=1, everything else 0 →
    0.4."""
    g = _graph([_claim("cl.x", importance=1.0)])
    assert claim_size(g.claims[0], g) == 0.4


def test_full_signal_saturates_at_one() -> None:
    """importance=1, 3+ evidence rows, mechanism, 2+ scope, 4+
    relationships — all components saturate → score = 1.0."""
    ev = Evidence(source="x", passage="p", binding_strength=BindingStrength.strong)
    c = _claim(
        "cl.x", importance=1.0,
        evidence=[ev, ev, ev],
        mechanism="X causes Y",
        scope_conditions=["cond1", "cond2"],
    )
    rels = [
        Relationship(rel_id=f"r.{i}", type=RelationshipType.supports,
                     **{"from": f"cl.other{i}", "to": "cl.x"},
                     strength=RelationshipStrength.direct, note="",
                     created_by="t", created_at=_now())
        for i in range(4)
    ]
    g = _graph([c], rels=rels)
    assert claim_size(c, g) == 1.0


def test_evidence_count_increases_size() -> None:
    base = _claim("cl.x", importance=0.5)
    g = _graph([base])
    base_size = claim_size(base, g)

    ev = Evidence(source="x", passage="p", binding_strength=BindingStrength.strong)
    bound = _claim("cl.x", importance=0.5, evidence=[ev, ev, ev])
    g2 = _graph([bound])
    assert claim_size(bound, g2) > base_size


def test_mechanism_increases_size() -> None:
    base = _claim("cl.x", importance=0.5)
    with_mech = _claim("cl.x", importance=0.5, mechanism="X causes Y")
    g_base = _graph([base])
    g_mech = _graph([with_mech])
    assert claim_size(with_mech, g_mech) > claim_size(base, g_base)


def test_relationships_increase_size() -> None:
    """A claim that everything else points at carries more structural
    weight than an isolated claim of the same importance."""
    isolated = _claim("cl.x", importance=0.5)
    g_iso = _graph([isolated])
    iso_size = claim_size(isolated, g_iso)

    central = _claim("cl.x", importance=0.5)
    rels = [
        Relationship(rel_id=f"r.{i}", type=RelationshipType.supports,
                     **{"from": f"cl.other{i}", "to": "cl.x"},
                     strength=RelationshipStrength.direct, note="",
                     created_by="t", created_at=_now())
        for i in range(3)
    ]
    g_cent = _graph([central], rels=rels)
    assert claim_size(central, g_cent) > iso_size


def test_claim_sizes_batch_matches_per_claim() -> None:
    """The batch helper should produce the same numbers as calling
    claim_size individually — used as a fast path by the planner."""
    ev = Evidence(source="x", passage="p", binding_strength=BindingStrength.strong)
    claims = [
        _claim("cl.a", importance=0.9, evidence=[ev]),
        _claim("cl.b", importance=0.3, mechanism="Z"),
        _claim("cl.c", importance=0.6, scope_conditions=["x", "y"]),
    ]
    g = _graph(claims)
    individual = {c.claim_id: claim_size(c, g) for c in claims}
    batch = claim_sizes(g)
    assert individual == batch


def test_importance_clamped_to_unit_range() -> None:
    """An out-of-range importance (which the ingester clamps but
    doesn't always — pydantic validates 0..1 only via the model)
    shouldn't propagate to a >1 size."""
    c = _claim("cl.x", importance=0.5)
    c.importance = 5.0  # pretend something set it without validation
    g = _graph([c])
    assert claim_size(c, g) <= 1.0
