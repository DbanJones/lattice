"""Tests for the ingest-time argument-metrics pass.

Two scores, six and five sub-scores respectively. Each test exercises
one knob of one sub-score so a regression points at the right component
quickly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from lattice.graph.metrics import (
    ArgumentBreadth,
    ArgumentMetrics,
    ArgumentStrength,
    compute_argument_metrics,
    compute_breadth,
    compute_strength,
)
from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimType, Confidence,
    Evidence, EvidenceStatus, Relationship, RelationshipStrength,
    RelationshipType, Section, SectionRole,
)
from lattice.ingester.markdown import MarkdownOutlineIngester
from lattice.utils.config import Config


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── helpers ─────────────────────────────────────────


def _claim(claim_id: str, *, type_=ClaimType.empirical, section_id="s.a",
           evidence: list[Evidence] | None = None,
           evidence_status: EvidenceStatus | None = None,
           mechanism: str | None = None,
           author_origin: bool = False,
           confidence=Confidence.medium) -> Claim:
    now = _now()
    return Claim(
        claim_id=claim_id,
        statement=f"Statement of {claim_id}.",
        type=type_,
        confidence=confidence,
        evidence=evidence or [],
        evidence_status=evidence_status,
        mechanism=mechanism,
        author_origin=author_origin,
        section_id=section_id,
        created_by="t",
        created_at=now, modified_at=now,
    )


def _rel(rel_id: str, type_: RelationshipType, frm: str, to: str,
         strength=RelationshipStrength.direct, note: str = "") -> Relationship:
    now = _now()
    return Relationship(
        rel_id=rel_id, type=type_,
        **{"from": frm, "to": to},
        strength=strength, note=note,
        created_by="t", created_at=now,
    )


def _build(claims: list[Claim], rels: list[Relationship],
           sections: list[Section] | None = None) -> AuthorGraph:
    now = _now()
    if sections is None:
        sections = [
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction,
                    claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="Body A", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=[c.claim_id for c in claims if c.section_id == "s.a"]),
            Section(section_id="s.z", title="Conclusion", position=2,
                    role=SectionRole.conclusion,
                    claim_ids=[c.claim_id for c in claims if c.section_id == "s.z"]),
        ]
    thesis = _claim("cl.thesis", type_=ClaimType.user_synthesis,
                    section_id="s.thesis", author_origin=True,
                    confidence=Confidence.high)
    return AuthorGraph(
        project_name="t", thesis_statement="The thesis.",
        sections=sections,
        claims=[thesis] + claims,
        relationships=rels,
        created_at=now, modified_at=now,
    )


# ─── strength: direct support ───────────────────────


def test_no_thesis_returns_empty_strength_with_message() -> None:
    now = _now()
    graph = AuthorGraph(
        project_name="t", thesis_statement=None,
        sections=[], claims=[], relationships=[],
        created_at=now, modified_at=now,
    )
    s = compute_strength(graph)
    assert s.score == 0.0
    assert any("No `cl.thesis`" in obs for obs in s.observations)


def test_zero_direct_supporters_scores_low() -> None:
    graph = _build(
        claims=[_claim("cl.a.1")],
        rels=[],  # nothing supports the thesis
    )
    s = compute_strength(graph)
    assert s.direct_supporter_count == 0
    assert s.direct_support == 0.0
    assert any("No claim directly supports" in obs for obs in s.observations)


def test_three_direct_supporters_scores_partial() -> None:
    """3 supporters / 5 = 0.6 — partial credit, doesn't saturate."""
    graph = _build(
        claims=[
            _claim("cl.a.1"), _claim("cl.a.2"), _claim("cl.a.3"),
        ],
        rels=[
            _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
            _rel("r.2", RelationshipType.supports, "cl.a.2", "cl.thesis"),
            _rel("r.3", RelationshipType.supports, "cl.a.3", "cl.thesis"),
        ],
    )
    s = compute_strength(graph)
    assert s.direct_supporter_count == 3
    assert s.direct_support == 0.6


def test_five_or_more_direct_supporters_saturates_to_one() -> None:
    claims = [_claim(f"cl.a.{i}") for i in range(1, 7)]
    rels = [
        _rel(f"r.{i}", RelationshipType.supports, f"cl.a.{i}", "cl.thesis")
        for i in range(1, 7)
    ]
    graph = _build(claims=claims, rels=rels)
    s = compute_strength(graph)
    assert s.direct_supporter_count == 6
    assert s.direct_support == 1.0


# ─── strength: reachable + depth ────────────────────


def test_reachable_includes_transitive_supporters() -> None:
    """A → B → thesis: B is direct, A is transitive. Both should be in
    the reachable set."""
    graph = _build(
        claims=[_claim("cl.a.1"), _claim("cl.a.2")],
        rels=[
            _rel("r.1", RelationshipType.extends, "cl.a.1", "cl.a.2"),
            _rel("r.2", RelationshipType.supports, "cl.a.2", "cl.thesis"),
        ],
    )
    s = compute_strength(graph)
    assert s.transitively_supporting_claim_count == 2
    assert s.reachable_support == 1.0  # 2 / (3 - 1) body claims


def test_deeper_chains_score_higher_depth() -> None:
    # Build a depth-3 supporting chain: cl.a.1 → cl.a.2 → cl.a.3 → thesis
    claims = [_claim(f"cl.a.{i}") for i in range(1, 4)]
    rels = [
        _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.a.2"),
        _rel("r.2", RelationshipType.supports, "cl.a.2", "cl.a.3"),
        _rel("r.3", RelationshipType.supports, "cl.a.3", "cl.thesis"),
    ]
    graph = _build(claims=claims, rels=rels)
    s = compute_strength(graph)
    # leaf is cl.a.1 at depth 3; avg = 3, normalised = 0.75.
    assert s.depth == 0.75


# ─── strength: evidence backing ──────────────────────


def test_evidence_backing_uses_binding_strength() -> None:
    """A bound supporter should pull evidence_backing toward 1.0; a
    source_hint supporter toward 0.5."""
    bound_ev = Evidence(source="x", passage="p.1.1",
                        binding_strength=BindingStrength.strong)
    weak_ev = Evidence(source="y", passage="",
                       binding_strength=BindingStrength.weak)
    graph = _build(
        claims=[
            _claim("cl.a.1", evidence=[bound_ev]),
            _claim("cl.a.2", evidence=[weak_ev]),
        ],
        rels=[
            _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
            _rel("r.2", RelationshipType.supports, "cl.a.2", "cl.thesis"),
        ],
    )
    s = compute_strength(graph)
    # avg of 1.0 + 0.5 = 0.75
    assert s.evidence_backing == 0.75


def test_unbound_evidence_status_lowers_backing() -> None:
    graph = _build(
        claims=[
            _claim("cl.a.1", evidence_status=EvidenceStatus.unbound),
            _claim("cl.a.2", evidence_status=EvidenceStatus.unbound),
        ],
        rels=[
            _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
            _rel("r.2", RelationshipType.supports, "cl.a.2", "cl.thesis"),
        ],
    )
    s = compute_strength(graph)
    assert s.evidence_backing == 0.2
    assert any("Evidence backing" in obs for obs in s.observations)
    assert s.weakest_supporters == ["cl.a.1", "cl.a.2"]


def test_user_synthesis_supporter_gets_author_score() -> None:
    """A user_synthesis claim with no evidence should score 0.7 — it's
    author-grounded, which is decent cover."""
    graph = _build(
        claims=[
            _claim("cl.a.1", type_=ClaimType.user_synthesis,
                   author_origin=True),
        ],
        rels=[
            _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        ],
    )
    s = compute_strength(graph)
    assert s.evidence_backing == 0.7


# ─── strength: counter handling ─────────────────────


def test_no_counter_arguments_yields_perfect_handling() -> None:
    graph = _build(
        claims=[_claim("cl.a.1")],
        rels=[
            _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        ],
    )
    s = compute_strength(graph)
    assert s.counter_handling == 1.0
    assert s.contradicting_thesis_count == 0


def test_unaddressed_counter_drops_handling_to_zero() -> None:
    graph = _build(
        claims=[_claim("cl.a.1")],
        rels=[
            _rel("r.1", RelationshipType.contradicts, "cl.a.1", "cl.thesis"),
        ],
    )
    s = compute_strength(graph)
    assert s.contradicting_thesis_count == 1
    assert s.counter_handling == 0.0
    assert any("unaddressed" in obs for obs in s.observations)


def test_counter_addressed_via_pivot_scores_full_handling() -> None:
    graph = _build(
        claims=[_claim("cl.a.1"), _claim("cl.a.2")],
        rels=[
            _rel("r.1", RelationshipType.contradicts, "cl.a.1", "cl.thesis"),
            _rel("r.2", RelationshipType.interpretive_pivot, "cl.a.2", "cl.a.1"),
        ],
    )
    s = compute_strength(graph)
    assert s.counter_handling == 1.0
    assert s.counters_addressed_count == 1


# ─── breadth: section diversity ─────────────────────


def test_single_body_section_scores_low_diversity() -> None:
    graph = _build(
        claims=[_claim("cl.a.1")],
        rels=[],
    )
    b = compute_breadth(graph)
    assert b.section_count == 2  # body A + conclusion (built into helper)
    assert b.section_diversity < 0.5


def test_six_sections_saturate_diversity() -> None:
    now = _now()
    sections = [
        Section(section_id="s.thesis", title="Thesis", position=0,
                role=SectionRole.introduction,
                claim_ids=["cl.thesis"]),
    ]
    claims: list[Claim] = []
    for i, letter in enumerate("abcdef", start=1):
        sections.append(Section(
            section_id=f"s.{letter}", title=f"S{letter}", position=i,
            role=SectionRole.argumentative,
            claim_ids=[f"cl.{letter}.1"],
        ))
        claims.append(_claim(f"cl.{letter}.1", section_id=f"s.{letter}"))
    sections.append(Section(
        section_id="s.z", title="Conclusion", position=99,
        role=SectionRole.conclusion, claim_ids=[],
    ))
    thesis = _claim("cl.thesis", type_=ClaimType.user_synthesis,
                    section_id="s.thesis", author_origin=True,
                    confidence=Confidence.high)
    graph = AuthorGraph(
        project_name="t", thesis_statement="T.",
        sections=sections, claims=[thesis] + claims, relationships=[],
        created_at=now, modified_at=now,
    )
    b = compute_breadth(graph)
    # Saturates at 6: 6 + the conclusion = 7 body sections, score capped at 1.
    assert b.section_diversity == 1.0


# ─── breadth: source diversity ──────────────────────


def test_one_source_yields_low_source_diversity() -> None:
    ev = Evidence(source="x", passage="p.1.1",
                  binding_strength=BindingStrength.strong)
    graph = _build(
        claims=[
            _claim("cl.a.1", evidence=[ev]),
            _claim("cl.a.2", evidence=[ev]),
            _claim("cl.a.3", evidence=[ev]),
        ],
        rels=[],
    )
    b = compute_breadth(graph)
    # One source = perfect concentration = entropy 0 = source_diversity 0.
    assert b.distinct_source_count == 1
    assert b.source_diversity == 0.0
    assert any("distinct source" in obs for obs in b.observations)


def test_many_balanced_sources_score_high_source_diversity() -> None:
    claims = []
    for i, src in enumerate(
        ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l"], start=1
    ):
        claims.append(_claim(
            f"cl.a.{i}",
            evidence=[Evidence(source=src, passage="p.1.1",
                               binding_strength=BindingStrength.strong)],
        ))
    graph = _build(claims=claims, rels=[])
    b = compute_breadth(graph)
    assert b.distinct_source_count == 12
    # 12 distinct + balanced should saturate distinct_score and entropy.
    assert b.source_diversity > 0.95


# ─── breadth: claim type + relationship diversity ───


def test_only_one_claim_type_scores_low_type_diversity() -> None:
    graph = _build(
        claims=[_claim(f"cl.a.{i}") for i in range(1, 4)],
        rels=[],
    )
    b = compute_breadth(graph)
    # Two types present in helper: empirical + the thesis (user_synthesis)
    # = 2/5 = 0.4
    assert b.claim_type_diversity == 0.4


def test_relationship_diversity_counts_distinct_types() -> None:
    graph = _build(
        claims=[_claim(f"cl.a.{i}") for i in range(1, 5)],
        rels=[
            _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
            _rel("r.2", RelationshipType.qualifies, "cl.a.2", "cl.a.1"),
            _rel("r.3", RelationshipType.extends, "cl.a.3", "cl.a.1"),
            _rel("r.4", RelationshipType.contradicts, "cl.a.4", "cl.thesis"),
        ],
    )
    b = compute_breadth(graph)
    # 4 distinct types out of 8 in the diversity universe = 0.5.
    assert b.relationship_type_diversity == 0.5


# ─── breadth: mechanism + spread ────────────────────


def test_mechanism_coverage_only_counts_eligible_claims() -> None:
    """user_synthesis claims aren't expected to have a mechanism;
    coverage should be 1.0 if every eligible (empirical/methodological)
    claim has one."""
    graph = _build(
        claims=[
            _claim("cl.a.1", mechanism="X causes Y"),
            _claim("cl.a.2", mechanism="A drives B"),
            _claim("cl.a.3", type_=ClaimType.user_synthesis,
                   author_origin=True),  # not counted in denominator
        ],
        rels=[],
    )
    b = compute_breadth(graph)
    assert b.mechanism_coverage == 1.0


def test_section_spread_low_when_one_section_dominates() -> None:
    """4 of 5 body claims in one section, 1 in the other → max share 80%."""
    claims = [
        _claim("cl.a.1", section_id="s.a"),
        _claim("cl.a.2", section_id="s.a"),
        _claim("cl.a.3", section_id="s.a"),
        _claim("cl.a.4", section_id="s.a"),
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True),
    ]
    graph = _build(claims=claims, rels=[])
    b = compute_breadth(graph)
    assert b.section_concentration["s.a"] == 0.8
    # 1 - 0.8 = 0.2
    assert b.section_spread == 0.2
    assert any("concentrated in section" in obs for obs in b.observations)


# ─── overall + integration ──────────────────────────


def test_compute_argument_metrics_returns_combined() -> None:
    graph = _build(
        claims=[_claim("cl.a.1")],
        rels=[
            _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        ],
    )
    metrics = compute_argument_metrics(graph)
    assert isinstance(metrics, ArgumentMetrics)
    assert isinstance(metrics.strength, ArgumentStrength)
    assert isinstance(metrics.breadth, ArgumentBreadth)
    assert 0 <= metrics.strength.score <= 1
    assert 0 <= metrics.breadth.score <= 1


async def test_metrics_appear_in_scaffold_report(tmp_path: Path) -> None:
    """End-to-end: ingesting an outline writes argument_metrics into
    last_report and the persisted scaffold_report.json."""
    (tmp_path / "config.yml").write_text(
        "default_voice: academic\n", encoding="utf-8"
    )
    (tmp_path / ".lattice").mkdir()
    (tmp_path / "outline.md").write_text(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Empirical claim. [type: empirical] [ref: src_a] [evidence_status: source_hint]\n"
        "  - MY VIEW: synthesis. [type: user_synthesis]\n\n"
        "# Z. Conclusion [role: conclusion]\n\n"
        "  - Closing. [type: user_synthesis] [supports: thesis]\n",
        encoding="utf-8",
    )
    config = Config.load(tmp_path)
    ingester = MarkdownOutlineIngester(config)
    await ingester.ingest(tmp_path / "outline.md", project_name="test")
    # In-memory report has metrics.
    assert ingester.last_report.argument_metrics is not None
    metrics = ingester.last_report.argument_metrics
    assert "strength" in metrics
    assert "breadth" in metrics
    # Persisted file has the same shape.
    written = ingester.save_scaffold_report(tmp_path)
    import json
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["argument_metrics"]["strength"]["score"] >= 0
    assert payload["argument_metrics"]["breadth"]["score"] >= 0


# ─── bug-fix regression tests ───────────────────────


async def test_scaffold_report_idempotent_under_changing_known_sources(
    tmp_path: Path,
) -> None:
    """Bug fix regression: calling ``save_scaffold_report`` twice with
    different ``known_source_ids`` should give the right answer each
    time (a previously-stripped ref must come back if the source is
    later removed from the index)."""
    (tmp_path / "config.yml").write_text(
        "default_voice: academic\n", encoding="utf-8"
    )
    (tmp_path / ".lattice").mkdir()
    (tmp_path / "outline.md").write_text(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Claim. [type: empirical] [ref: src_a, src_b, src_c]\n\n"
        "# Z. Conclusion [role: conclusion]\n\n"
        "  - Closing. [type: user_synthesis]\n",
        encoding="utf-8",
    )
    config = Config.load(tmp_path)
    ingester = MarkdownOutlineIngester(config)
    await ingester.ingest(tmp_path / "outline.md", project_name="test")

    # First save: src_a known. unresolved_refs should be [src_b, src_c].
    written = ingester.save_scaffold_report(
        tmp_path, known_source_ids={"src_a"}
    )
    import json
    payload = json.loads(written.read_text(encoding="utf-8"))
    a = next(cr for cr in payload["claim_reports"] if cr["claim_id"] == "cl.a.1")
    assert sorted(a["unresolved_refs"]) == ["src_b", "src_c"]

    # Second save: NO sources known (the index was wiped). All three
    # citekeys should reappear in unresolved_refs — the bug was that
    # they stayed pruned.
    written = ingester.save_scaffold_report(
        tmp_path, known_source_ids=set()
    )
    payload = json.loads(written.read_text(encoding="utf-8"))
    a = next(cr for cr in payload["claim_reports"] if cr["claim_id"] == "cl.a.1")
    assert sorted(a["unresolved_refs"]) == ["src_a", "src_b", "src_c"]
