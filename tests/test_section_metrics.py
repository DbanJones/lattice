"""Phase 3A: per-section argument metrics."""
from __future__ import annotations

from datetime import datetime, timezone

from lattice.graph.metrics import (
    SectionMetrics,
    compute_all_section_metrics,
    compute_argument_metrics,
    compute_section_metrics,
)
from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimType, Confidence,
    Evidence, Relationship, RelationshipStrength, RelationshipType,
    Section, SectionRole,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _claim(claim_id: str, *, type_=ClaimType.empirical, section_id="s.a",
           importance: float = 0.5,
           evidence: list[Evidence] | None = None,
           mechanism: str | None = None) -> Claim:
    now = _now()
    return Claim(
        claim_id=claim_id,
        statement=f"Statement {claim_id}.",
        type=type_,
        confidence=Confidence.medium,
        importance=importance,
        evidence=evidence or [],
        mechanism=mechanism,
        section_id=section_id,
        created_by="t",
        created_at=now,
        modified_at=now,
    )


def _rel(rid: str, type_, frm, to) -> Relationship:
    return Relationship(
        rel_id=rid, type=type_, **{"from": frm, "to": to},
        strength=RelationshipStrength.direct, note="",
        created_by="t", created_at=_now(),
    )


def _build(*, sections: list[Section], claims: list[Claim],
           rels: list[Relationship] | None = None) -> AuthorGraph:
    now = _now()
    return AuthorGraph(
        project_name="t", thesis_statement="T.",
        sections=sections, claims=claims,
        relationships=rels or [],
        created_at=now, modified_at=now,
    )


# ─── empty / edge cases ──────────────────────────


def test_empty_section_returns_zero_metrics() -> None:
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative, claim_ids=[])],
        claims=[],
    )
    m = compute_section_metrics(graph, "s.a")
    assert m.section_id == "s.a"
    assert m.section_title == "A"
    assert m.claim_count == 0
    assert m.score == 0.0


def test_unknown_section_id_returns_default() -> None:
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative, claim_ids=[])],
        claims=[],
    )
    m = compute_section_metrics(graph, "s.zzz")
    assert m.section_id == "s.zzz"
    assert m.claim_count == 0


# ─── claim_count / relationship_count ────────────


def test_claim_count_matches_section_membership() -> None:
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative,
                          claim_ids=["cl.a.1", "cl.a.2"])],
        claims=[
            _claim("cl.a.1", section_id="s.a"),
            _claim("cl.a.2", section_id="s.a"),
            _claim("cl.b.1", section_id="s.b"),
        ],
    )
    m = compute_section_metrics(graph, "s.a")
    assert m.claim_count == 2


def test_relationship_count_only_intra_section() -> None:
    """Relationships from a section claim to a non-section claim
    must not be counted in the section's relationship_count."""
    graph = _build(
        sections=[
            Section(section_id="s.a", title="A", position=0,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1", "cl.a.2"]),
            Section(section_id="s.b", title="B", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.b.1"]),
        ],
        claims=[
            _claim("cl.a.1", section_id="s.a"),
            _claim("cl.a.2", section_id="s.a"),
            _claim("cl.b.1", section_id="s.b"),
        ],
        rels=[
            _rel("r.1", RelationshipType.qualifies, "cl.a.2", "cl.a.1"),  # intra
            _rel("r.2", RelationshipType.supports, "cl.b.1", "cl.a.1"),   # cross-section
        ],
    )
    m = compute_section_metrics(graph, "s.a")
    assert m.relationship_count == 1


# ─── evidence_backing ────────────────────────────


def test_evidence_backing_uses_binding_strength() -> None:
    strong = Evidence(source="x", passage="p",
                      binding_strength=BindingStrength.strong)
    weak = Evidence(source="y", passage="",
                    binding_strength=BindingStrength.weak)
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative,
                          claim_ids=["cl.a.1", "cl.a.2"])],
        claims=[
            _claim("cl.a.1", section_id="s.a", evidence=[strong]),
            _claim("cl.a.2", section_id="s.a", evidence=[weak]),
        ],
    )
    m = compute_section_metrics(graph, "s.a")
    # avg of strong (1.0) + weak (0.5) = 0.75
    assert m.evidence_backing == 0.75


# ─── mechanism_coverage ──────────────────────────


def test_mechanism_coverage_counts_only_empirical_methodological() -> None:
    """A user_synthesis claim shouldn't count against mechanism
    coverage — it's not expected to have one."""
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative,
                          claim_ids=["cl.a.1", "cl.a.2", "cl.a.3"])],
        claims=[
            _claim("cl.a.1", section_id="s.a", mechanism="X causes Y"),
            _claim("cl.a.2", section_id="s.a", mechanism=None),
            _claim("cl.a.3", section_id="s.a", type_=ClaimType.user_synthesis),
        ],
    )
    m = compute_section_metrics(graph, "s.a")
    # 1 of 2 eligible → 0.5
    assert m.mechanism_coverage == 0.5


def test_mechanism_coverage_zero_when_no_eligible() -> None:
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative,
                          claim_ids=["cl.a.1"])],
        claims=[
            _claim("cl.a.1", section_id="s.a",
                   type_=ClaimType.user_synthesis),
        ],
    )
    m = compute_section_metrics(graph, "s.a")
    assert m.mechanism_coverage == 0.0


# ─── source_diversity ────────────────────────────


def test_source_diversity_zero_with_one_source() -> None:
    ev = Evidence(source="single", passage="p",
                  binding_strength=BindingStrength.strong)
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative,
                          claim_ids=["cl.a.1", "cl.a.2"])],
        claims=[
            _claim("cl.a.1", section_id="s.a", evidence=[ev]),
            _claim("cl.a.2", section_id="s.a", evidence=[ev]),
        ],
    )
    m = compute_section_metrics(graph, "s.a")
    assert m.source_count == 1
    assert m.source_diversity == 0.0  # entropy of single source = 0


def test_source_diversity_high_with_balanced_set() -> None:
    """Six distinct sources, each cited once by one claim each."""
    claims = []
    for i, src in enumerate(["a", "b", "c", "d", "e", "f"]):
        ev = Evidence(source=src, passage="p",
                      binding_strength=BindingStrength.strong)
        claims.append(_claim(f"cl.a.{i+1}", section_id="s.a", evidence=[ev]))
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative,
                          claim_ids=[c.claim_id for c in claims])],
        claims=claims,
    )
    m = compute_section_metrics(graph, "s.a")
    assert m.source_count == 6
    # Saturates source-count component AND has max entropy.
    assert m.source_diversity > 0.95


# ─── relationship_density ────────────────────────


def test_relationship_density_low_with_no_edges() -> None:
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative,
                          claim_ids=["cl.a.1", "cl.a.2", "cl.a.3", "cl.a.4"])],
        claims=[_claim(f"cl.a.{i}", section_id="s.a") for i in range(1, 5)],
    )
    m = compute_section_metrics(graph, "s.a")
    assert m.relationship_density == 0.0


def test_relationship_density_saturates_at_threshold() -> None:
    """1.5 edges per claim saturates the density score to 1.0."""
    claims = [_claim(f"cl.a.{i}", section_id="s.a") for i in range(1, 3)]
    rels = [
        _rel("r.1", RelationshipType.qualifies, "cl.a.1", "cl.a.2"),
        _rel("r.2", RelationshipType.extends, "cl.a.2", "cl.a.1"),
        _rel("r.3", RelationshipType.depends_on, "cl.a.1", "cl.a.2"),
    ]
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative,
                          claim_ids=[c.claim_id for c in claims])],
        claims=claims,
        rels=rels,
    )
    m = compute_section_metrics(graph, "s.a")
    # 3 edges / 2 claims = 1.5 → saturates to 1.0
    assert m.relationship_density == 1.0


# ─── thesis_connection ───────────────────────────


def test_thesis_connection_full_when_every_claim_supports() -> None:
    now = _now()
    thesis = Claim(
        claim_id="cl.thesis", statement="T.",
        type=ClaimType.user_synthesis, confidence=Confidence.high,
        author_origin=True, section_id="s.thesis",
        created_by="t", created_at=now, modified_at=now,
    )
    graph = _build(
        sections=[
            Section(section_id="s.thesis", title="T", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="A", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1", "cl.a.2"]),
        ],
        claims=[
            thesis,
            _claim("cl.a.1", section_id="s.a"),
            _claim("cl.a.2", section_id="s.a"),
        ],
        rels=[
            _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
            _rel("r.2", RelationshipType.supports, "cl.a.2", "cl.thesis"),
        ],
    )
    m = compute_section_metrics(graph, "s.a")
    assert m.thesis_connection == 1.0


def test_thesis_connection_partial_when_some_claims_disconnect() -> None:
    now = _now()
    thesis = Claim(
        claim_id="cl.thesis", statement="T.",
        type=ClaimType.user_synthesis, confidence=Confidence.high,
        author_origin=True, section_id="s.thesis",
        created_by="t", created_at=now, modified_at=now,
    )
    graph = _build(
        sections=[
            Section(section_id="s.thesis", title="T", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="A", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1", "cl.a.2", "cl.a.3", "cl.a.4"]),
        ],
        claims=[
            thesis,
            _claim("cl.a.1", section_id="s.a"),
            _claim("cl.a.2", section_id="s.a"),
            _claim("cl.a.3", section_id="s.a"),
            _claim("cl.a.4", section_id="s.a"),
        ],
        rels=[
            _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
            _rel("r.2", RelationshipType.supports, "cl.a.2", "cl.thesis"),
        ],
    )
    m = compute_section_metrics(graph, "s.a")
    assert m.thesis_connection == 0.5


def test_thesis_connection_includes_transitive_paths() -> None:
    """Section claim → another claim → cl.thesis should still count."""
    now = _now()
    thesis = Claim(
        claim_id="cl.thesis", statement="T.",
        type=ClaimType.user_synthesis, confidence=Confidence.high,
        author_origin=True, section_id="s.thesis",
        created_by="t", created_at=now, modified_at=now,
    )
    graph = _build(
        sections=[
            Section(section_id="s.thesis", title="T", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="A", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1"]),
            Section(section_id="s.b", title="B", position=2,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.b.1"]),
        ],
        claims=[
            thesis,
            _claim("cl.a.1", section_id="s.a"),
            _claim("cl.b.1", section_id="s.b"),
        ],
        rels=[
            _rel("r.1", RelationshipType.extends, "cl.a.1", "cl.b.1"),
            _rel("r.2", RelationshipType.supports, "cl.b.1", "cl.thesis"),
        ],
    )
    m = compute_section_metrics(graph, "s.a")
    # cl.a.1 → cl.b.1 → cl.thesis (both edges in supporting set).
    assert m.thesis_connection == 1.0


# ─── observations ────────────────────────────────


def test_observations_call_out_disconnected_section() -> None:
    """A section with no thesis links should generate the right
    observation."""
    now = _now()
    thesis = Claim(
        claim_id="cl.thesis", statement="T.",
        type=ClaimType.user_synthesis, confidence=Confidence.high,
        author_origin=True, section_id="s.thesis",
        created_by="t", created_at=now, modified_at=now,
    )
    graph = _build(
        sections=[
            Section(section_id="s.thesis", title="T", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="A", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1", "cl.a.2"]),
        ],
        claims=[
            thesis,
            _claim("cl.a.1", section_id="s.a"),
            _claim("cl.a.2", section_id="s.a"),
        ],
    )
    m = compute_section_metrics(graph, "s.a")
    assert any("connect to the thesis" in o.lower() for o in m.observations)


def test_observations_call_out_no_sources() -> None:
    graph = _build(
        sections=[Section(section_id="s.a", title="A", position=0,
                          role=SectionRole.argumentative,
                          claim_ids=["cl.a.1"])],
        claims=[_claim("cl.a.1", section_id="s.a")],
    )
    m = compute_section_metrics(graph, "s.a")
    assert any("no sources cited" in o.lower() for o in m.observations)


# ─── compute_all_section_metrics + integration ───


def test_compute_all_skips_thesis_and_references() -> None:
    graph = _build(
        sections=[
            Section(section_id="s.thesis", title="T", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="A", position=1,
                    role=SectionRole.argumentative, claim_ids=["cl.a.1"]),
            Section(section_id="s.refs", title="Refs", position=2,
                    role=SectionRole.references, claim_ids=[]),
        ],
        claims=[
            Claim(
                claim_id="cl.thesis", statement="T.",
                type=ClaimType.user_synthesis, confidence=Confidence.high,
                author_origin=True, section_id="s.thesis",
                created_by="t", created_at=_now(), modified_at=_now(),
            ),
            _claim("cl.a.1", section_id="s.a"),
        ],
    )
    metrics = compute_all_section_metrics(graph)
    assert "s.a" in metrics
    assert "s.thesis" not in metrics
    assert "s.refs" not in metrics


def test_compute_argument_metrics_includes_per_section() -> None:
    graph = _build(
        sections=[
            Section(section_id="s.thesis", title="T", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="A", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1"]),
            Section(section_id="s.b", title="B", position=2,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.b.1"]),
        ],
        claims=[
            Claim(
                claim_id="cl.thesis", statement="T.",
                type=ClaimType.user_synthesis, confidence=Confidence.high,
                author_origin=True, section_id="s.thesis",
                created_by="t", created_at=_now(), modified_at=_now(),
            ),
            _claim("cl.a.1", section_id="s.a"),
            _claim("cl.b.1", section_id="s.b"),
        ],
    )
    full = compute_argument_metrics(graph)
    assert set(full.per_section.keys()) == {"s.a", "s.b"}
    assert all(isinstance(v, SectionMetrics) for v in full.per_section.values())
