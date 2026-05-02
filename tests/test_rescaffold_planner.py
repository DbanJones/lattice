"""Tests for the metrics-driven rescaffold planner.

One test per rule-driven generator; one end-to-end test on a known-bad
scaffold; one test that a healthy graph produces an empty plan.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimRoleInCluster, ClaimType,
    Cluster, ClusterRole, Confidence, Evidence, EvidenceStatus,
    Relationship, RelationshipStrength, RelationshipType, Section,
    SectionRole,
)
from lattice.restructure.rescaffold_planner import plan_rescaffold
from lattice.restructure.rescaffold_formatter import format_plan_markdown
from lattice.voice.parser import Voice


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _claim(claim_id: str, *,
           type_: ClaimType = ClaimType.empirical,
           section_id: str = "s.a",
           importance: float = 0.5,
           evidence: list[Evidence] | None = None,
           mechanism: str | None = None,
           tags: list[str] | None = None,
           confidence=Confidence.medium,
           author_origin: bool = False,
           evidence_status: EvidenceStatus | None = None) -> Claim:
    now = _now()
    return Claim(
        claim_id=claim_id,
        statement=f"Statement of {claim_id}.",
        type=type_,
        confidence=confidence,
        importance=importance,
        evidence=evidence or [],
        mechanism=mechanism,
        author_origin=author_origin,
        evidence_status=evidence_status,
        tags=tags or [],
        section_id=section_id,
        created_by="t",
        created_at=now, modified_at=now,
    )


def _rel(rel_id: str, type_: RelationshipType, frm: str, to: str) -> Relationship:
    return Relationship(
        rel_id=rel_id, type=type_,
        **{"from": frm, "to": to},
        strength=RelationshipStrength.direct, note="",
        created_by="t", created_at=_now(),
    )


def _voice(academic_voice_path: Path) -> Voice:
    return Voice.from_file(academic_voice_path)


def _build(*, claims: list[Claim], rels: list[Relationship],
           sections: list[Section] | None = None,
           thesis_text: str = "The thesis.") -> AuthorGraph:
    now = _now()
    if sections is None:
        section_ids = sorted({c.section_id for c in claims if c.section_id})
        sections = [
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction,
                    claim_ids=["cl.thesis"]),
        ]
        for i, sid in enumerate(section_ids, start=1):
            role = (
                SectionRole.conclusion if sid == "s.z"
                else SectionRole.argumentative
            )
            sections.append(Section(
                section_id=sid, title=f"Section {sid}", position=i, role=role,
                claim_ids=[c.claim_id for c in claims if c.section_id == sid],
            ))
    thesis = _claim("cl.thesis", type_=ClaimType.user_synthesis,
                    section_id="s.thesis", author_origin=True,
                    confidence=Confidence.high, importance=1.0)
    return AuthorGraph(
        project_name="t", thesis_statement=thesis_text,
        sections=sections, claims=[thesis] + claims, relationships=rels,
        created_at=now, modified_at=now,
    )


# ─── healthy graph → empty plan ─────────────────────


def test_healthy_graph_produces_empty_plan(academic_voice_path: Path) -> None:
    """Every sub-score above threshold → no diagnosis, no operations,
    no advisories.

    The fixture is deliberately rich: 5 direct supporters of the thesis
    (saturate direct_support), deep two-hop chains via ``extends`` /
    ``depends_on`` (depth ≥ 0.4), six distinct sources, mechanisms on
    every empirical claim, all five claim types present.
    """
    voice = _voice(academic_voice_path)
    sources = [f"src_{x}" for x in "abcdefgh"]
    evs = {s: Evidence(source=s, passage="p",
                       binding_strength=BindingStrength.strong)
           for s in sources}
    claims = [
        # Five direct thesis supporters across three sections
        _claim("cl.a.1", type_=ClaimType.empirical, section_id="s.a",
               importance=0.8, evidence=[evs["src_a"]],
               mechanism="X causes Y"),
        _claim("cl.a.2", type_=ClaimType.methodological, section_id="s.a",
               importance=0.7, evidence=[evs["src_b"]],
               mechanism="A drives B"),
        _claim("cl.b.1", type_=ClaimType.empirical, section_id="s.b",
               importance=0.7, evidence=[evs["src_c"]],
               mechanism="C → D"),
        _claim("cl.b.2", type_=ClaimType.normative, section_id="s.b",
               importance=0.6, evidence=[evs["src_d"]]),
        _claim("cl.c.1", type_=ClaimType.definition, section_id="s.c",
               importance=0.6, evidence=[evs["src_e"]]),
        _claim("cl.c.2", type_=ClaimType.empirical, section_id="s.c",
               importance=0.7, evidence=[evs["src_f"]],
               mechanism="E → F"),
        # Two-hop deepeners — these support the direct supporters,
        # giving depth = 2 (0.5 normalised) instead of 1 (0.25).
        _claim("cl.a.3", type_=ClaimType.empirical, section_id="s.a",
               importance=0.5, evidence=[evs["src_g"]],
               mechanism="X' drives X"),
        _claim("cl.b.3", type_=ClaimType.empirical, section_id="s.b",
               importance=0.5, evidence=[evs["src_h"]],
               mechanism="C' drives C"),
        # Conclusion synthesis
        _claim("cl.z.1", type_=ClaimType.user_synthesis, section_id="s.z",
               importance=0.95, author_origin=True),
    ]
    rels = [
        # Direct supporters
        _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        _rel("r.2", RelationshipType.supports, "cl.b.1", "cl.thesis"),
        _rel("r.3", RelationshipType.supports, "cl.c.2", "cl.thesis"),
        _rel("r.4", RelationshipType.supports, "cl.z.1", "cl.thesis"),
        _rel("r.5", RelationshipType.supports, "cl.a.2", "cl.thesis"),
        # Deeper chain (lifts depth + relationship_type_diversity)
        _rel("r.6", RelationshipType.extends, "cl.a.3", "cl.a.1"),
        _rel("r.7", RelationshipType.extends, "cl.b.3", "cl.b.1"),
        _rel("r.8", RelationshipType.qualifies, "cl.b.2", "cl.b.1"),
        _rel("r.9", RelationshipType.depends_on, "cl.c.1", "cl.c.2"),
    ]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    assert plan.diagnosis == [], (
        f"unexpected diagnosis: {[(d.dimension, d.sub_score, d.value) for d in plan.diagnosis]}"
    )
    assert plan.operations == []
    assert plan.advisories == []


# ─── per-rule generators ────────────────────────────


def test_dominant_section_triggers_split(academic_voice_path: Path) -> None:
    """One section holding ≥45% of body claims → split_section op."""
    voice = _voice(academic_voice_path)
    # 7 claims in s.a, 1 in s.z, 2 sections total — s.a dominates.
    claims = [
        _claim(f"cl.a.{i}", section_id="s.a", importance=0.5)
        for i in range(1, 8)
    ] + [
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True, importance=0.9),
    ]
    rels = [_rel("r.1", RelationshipType.supports, "cl.z.1", "cl.thesis")]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    assert any(op.kind == "split_section" for op in plan.operations)
    split = next(op for op in plan.operations if op.kind == "split_section")
    assert split.source_section_id == "s.a"
    assert len(split.split_groups) >= 2


def test_unaddressed_counter_triggers_advisory(
    academic_voice_path: Path,
) -> None:
    """A counter→thesis claim with no inbound counter-handling edge
    fires an add_counter_engagement advisory."""
    voice = _voice(academic_voice_path)
    claims = [
        _claim("cl.a.1", section_id="s.a", importance=0.7),
        _claim("cl.a.2", section_id="s.a", importance=0.7),  # the counter
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True, importance=0.9),
    ]
    rels = [
        _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        _rel("r.2", RelationshipType.contradicts, "cl.a.2", "cl.thesis"),
        _rel("r.3", RelationshipType.supports, "cl.z.1", "cl.thesis"),
    ]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    counter_advs = [a for a in plan.advisories if a.kind == "add_counter_engagement"]
    assert counter_advs
    assert any(a.target_claim_id == "cl.a.2" for a in counter_advs)


def test_two_or_more_unaddressed_counters_add_section_stub(
    academic_voice_path: Path,
) -> None:
    """≥2 unaddressed counters AND no counterargument section → propose
    add_section_stub."""
    voice = _voice(academic_voice_path)
    claims = [
        _claim("cl.a.1", section_id="s.a", importance=0.7),
        _claim("cl.a.2", section_id="s.a", importance=0.7),
        _claim("cl.a.3", section_id="s.a", importance=0.7),
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True, importance=0.9),
    ]
    rels = [
        _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        _rel("r.2", RelationshipType.contradicts, "cl.a.2", "cl.thesis"),
        _rel("r.3", RelationshipType.contradicts, "cl.a.3", "cl.thesis"),
        _rel("r.4", RelationshipType.supports, "cl.z.1", "cl.thesis"),
    ]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    stubs = [op for op in plan.operations if op.kind == "add_section_stub"]
    assert any(op.new_section_role == "counterargument" for op in stubs)


def test_weak_evidence_supporters_get_advisories(
    academic_voice_path: Path,
) -> None:
    """Supporting claims with low evidence → bind_evidence advisories."""
    voice = _voice(academic_voice_path)
    claims = [
        _claim("cl.a.1", section_id="s.a", importance=0.7,
               evidence_status=EvidenceStatus.unbound),
        _claim("cl.a.2", section_id="s.a", importance=0.7,
               evidence_status=EvidenceStatus.unbound),
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True, importance=0.9),
    ]
    rels = [
        _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        _rel("r.2", RelationshipType.supports, "cl.a.2", "cl.thesis"),
        _rel("r.3", RelationshipType.supports, "cl.z.1", "cl.thesis"),
    ]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    bind_advs = [a for a in plan.advisories if a.kind == "bind_evidence"]
    assert bind_advs
    assert {a.target_claim_id for a in bind_advs} >= {"cl.a.1", "cl.a.2"}


def test_high_importance_empirical_without_mechanism_advised(
    academic_voice_path: Path,
) -> None:
    """Empirical claim, importance ≥ 0.6, no mechanism → add_mechanism
    advisory."""
    voice = _voice(academic_voice_path)
    ev = Evidence(source="x", passage="p", binding_strength=BindingStrength.strong)
    claims = [
        _claim("cl.a.1", section_id="s.a", importance=0.8, evidence=[ev]),
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True, importance=0.9),
    ]
    rels = [
        _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        _rel("r.2", RelationshipType.supports, "cl.z.1", "cl.thesis"),
    ]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    mech_advs = [
        a for a in plan.advisories
        if a.kind == "add_mechanism" and a.target_claim_id == "cl.a.1"
    ]
    assert mech_advs


def test_one_dominant_source_triggers_diversify_advisory(
    academic_voice_path: Path,
) -> None:
    voice = _voice(academic_voice_path)
    ev = Evidence(source="dominant_src", passage="p",
                  binding_strength=BindingStrength.strong)
    claims = [
        _claim(f"cl.a.{i}", section_id="s.a", importance=0.6, evidence=[ev])
        for i in range(1, 6)
    ] + [
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True, importance=0.9),
    ]
    rels = [_rel("r.1", RelationshipType.supports, "cl.z.1", "cl.thesis")]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    diversify = [a for a in plan.advisories if a.kind == "diversify_sources"]
    assert diversify


def test_low_type_diversity_advises_methodological(
    academic_voice_path: Path,
) -> None:
    voice = _voice(academic_voice_path)
    # Only empirical + user_synthesis → 2/5 = 0.4, just below 0.4 threshold.
    # Need to push below threshold — only empirical + user_synthesis is exactly 0.4.
    # Make it a single empirical claim plus thesis — types_present = {empirical, user_synthesis} = 2/5 = 0.4.
    # 0.4 is not < 0.4. So we need only empirical → 1/5 = 0.2.
    # But the helper always adds a user_synthesis thesis. So it's empirical + user_synthesis = 2/5 = 0.4.
    # 0.4 fails the < 0.4 check, so the rule won't fire.
    # Use _build with explicit sections instead.
    now = _now()
    thesis = _claim("cl.thesis", type_=ClaimType.user_synthesis,
                    section_id="s.thesis", author_origin=True,
                    confidence=Confidence.high, importance=1.0)
    claims = [
        _claim("cl.a.1", section_id="s.a", importance=0.6),
        _claim("cl.a.2", section_id="s.a", importance=0.6),
    ]
    sections = [
        Section(section_id="s.thesis", title="T", position=0,
                role=SectionRole.introduction, claim_ids=["cl.thesis"]),
        Section(section_id="s.a", title="A", position=1,
                role=SectionRole.argumentative,
                claim_ids=["cl.a.1", "cl.a.2"]),
        Section(section_id="s.z", title="Z", position=2,
                role=SectionRole.conclusion, claim_ids=[]),
    ]
    graph = AuthorGraph(
        project_name="t", thesis_statement="T.",
        sections=sections, claims=[thesis] + claims, relationships=[],
        created_at=now, modified_at=now,
    )
    plan = plan_rescaffold(graph, voice)
    framing = [
        a for a in plan.advisories if a.kind == "add_methodological_framing"
    ]
    assert framing


def test_skim_target_reorder_when_lead_isnt_heaviest(
    academic_voice_path: Path,
) -> None:
    """A section whose first claim isn't its highest claim_size → propose
    reorder_within_section."""
    voice = _voice(academic_voice_path)
    ev = Evidence(source="x", passage="p", binding_strength=BindingStrength.strong)
    light = _claim("cl.a.1", section_id="s.a", importance=0.2)
    heavy = _claim("cl.a.2", section_id="s.a", importance=0.95,
                   evidence=[ev], mechanism="big mechanism")
    conclusion = _claim("cl.z.1", section_id="s.z",
                        type_=ClaimType.user_synthesis,
                        author_origin=True, importance=0.9)
    rels = [_rel("r.1", RelationshipType.supports, "cl.z.1", "cl.thesis")]
    graph = _build(claims=[light, heavy, conclusion], rels=rels)
    plan = plan_rescaffold(graph, voice)
    reorders = [op for op in plan.operations if op.kind == "reorder_within_section"]
    s_a_reorder = next(
        (op for op in reorders if op.source_section_id == "s.a"),
        None,
    )
    assert s_a_reorder is not None
    assert s_a_reorder.claim_order[0] == "cl.a.2"


def test_low_importance_orphans_become_offcuts(
    academic_voice_path: Path,
) -> None:
    """Claims with size ≤ 0.2 and no inbound relationships → proposed
    offcuts. Fixture has 6 body claims so the half-the-body cap leaves
    room for 3 cuts; the planner should pick the two zero-importance
    orphans plus one of the other low-size ones."""
    voice = _voice(academic_voice_path)
    ev = Evidence(source="x", passage="p",
                  binding_strength=BindingStrength.strong)
    claims = [
        _claim("cl.a.1", section_id="s.a", importance=0.0),  # offcut: orphan + size 0
        _claim("cl.a.2", section_id="s.a", importance=0.0),  # offcut: orphan + size 0
        # Three "kept" claims so body has structural content to score against.
        _claim("cl.a.3", section_id="s.a", importance=0.7,
               evidence=[ev], mechanism="m"),
        _claim("cl.a.4", section_id="s.a", importance=0.7,
               evidence=[ev], mechanism="m"),
        _claim("cl.a.5", section_id="s.a", importance=0.7,
               evidence=[ev], mechanism="m"),
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True, importance=0.9),
    ]
    rels = [
        _rel("r.1", RelationshipType.supports, "cl.a.3", "cl.thesis"),
        _rel("r.2", RelationshipType.supports, "cl.a.4", "cl.thesis"),
        _rel("r.3", RelationshipType.supports, "cl.a.5", "cl.thesis"),
        _rel("r.4", RelationshipType.supports, "cl.z.1", "cl.thesis"),
    ]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    assert "cl.a.1" in plan.proposed_offcuts
    assert "cl.a.2" in plan.proposed_offcuts


# ─── predicted-delta sanity ─────────────────────────


def test_predicted_metrics_at_least_match_current_when_no_ops(
    academic_voice_path: Path,
) -> None:
    """No ops → predicted ≥ current (proposing nothing can't make
    things worse)."""
    voice = _voice(academic_voice_path)
    # Healthy graph from earlier test.
    ev = Evidence(source="x", passage="p", binding_strength=BindingStrength.strong)
    claims = [
        _claim("cl.a.1", importance=0.8, evidence=[ev], mechanism="m",
               section_id="s.a", type_=ClaimType.empirical),
        _claim("cl.b.1", importance=0.7, evidence=[ev], mechanism="m",
               section_id="s.b", type_=ClaimType.methodological),
        _claim("cl.c.1", importance=0.6, evidence=[ev], mechanism="m",
               section_id="s.c", type_=ClaimType.normative),
        _claim("cl.d.1", importance=0.6, evidence=[ev],
               section_id="s.d", type_=ClaimType.definition),
        _claim("cl.z.1", section_id="s.z",
               type_=ClaimType.user_synthesis, author_origin=True,
               importance=0.95),
    ]
    rels = [
        _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        _rel("r.2", RelationshipType.qualifies, "cl.b.1", "cl.a.1"),
        _rel("r.3", RelationshipType.extends, "cl.c.1", "cl.a.1"),
        _rel("r.4", RelationshipType.depends_on, "cl.d.1", "cl.c.1"),
        _rel("r.5", RelationshipType.supports, "cl.z.1", "cl.thesis"),
    ]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    assert plan.expected_strength_delta == pytest.approx(0.0, abs=1e-3)
    assert plan.expected_breadth_delta == pytest.approx(0.0, abs=1e-3)


def test_split_section_op_lifts_predicted_section_spread(
    academic_voice_path: Path,
) -> None:
    """A dominant-section split should lift predicted breadth.section_spread."""
    voice = _voice(academic_voice_path)
    claims = [
        _claim(f"cl.a.{i}", section_id="s.a", importance=0.5)
        for i in range(1, 8)
    ] + [
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True, importance=0.9),
    ]
    rels = [_rel("r.1", RelationshipType.supports, "cl.z.1", "cl.thesis")]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    current_spread = plan.current_metrics["breadth"]["section_spread"]
    predicted_spread = plan.predicted_metrics["breadth"]["section_spread"]
    assert predicted_spread > current_spread


# ─── markdown formatter ─────────────────────────────


def test_formatter_handles_empty_plan(academic_voice_path: Path) -> None:
    """A plan with no diagnosis / ops / advisories renders the
    health-OK message. Uses the same rich fixture as the healthy-graph
    test so every sub-score clears its threshold."""
    voice = _voice(academic_voice_path)
    sources = [f"src_{x}" for x in "abcdefgh"]
    evs = {s: Evidence(source=s, passage="p",
                       binding_strength=BindingStrength.strong)
           for s in sources}
    claims = [
        _claim("cl.a.1", type_=ClaimType.empirical, section_id="s.a",
               importance=0.8, evidence=[evs["src_a"]], mechanism="X"),
        _claim("cl.a.2", type_=ClaimType.methodological, section_id="s.a",
               importance=0.7, evidence=[evs["src_b"]], mechanism="A"),
        _claim("cl.b.1", type_=ClaimType.empirical, section_id="s.b",
               importance=0.7, evidence=[evs["src_c"]], mechanism="C"),
        _claim("cl.b.2", type_=ClaimType.normative, section_id="s.b",
               importance=0.6, evidence=[evs["src_d"]]),
        _claim("cl.c.1", type_=ClaimType.definition, section_id="s.c",
               importance=0.6, evidence=[evs["src_e"]]),
        _claim("cl.c.2", type_=ClaimType.empirical, section_id="s.c",
               importance=0.7, evidence=[evs["src_f"]], mechanism="E"),
        _claim("cl.a.3", type_=ClaimType.empirical, section_id="s.a",
               importance=0.5, evidence=[evs["src_g"]], mechanism="X'"),
        _claim("cl.b.3", type_=ClaimType.empirical, section_id="s.b",
               importance=0.5, evidence=[evs["src_h"]], mechanism="C'"),
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True, importance=0.95),
    ]
    rels = [
        _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        _rel("r.2", RelationshipType.supports, "cl.b.1", "cl.thesis"),
        _rel("r.3", RelationshipType.supports, "cl.c.2", "cl.thesis"),
        _rel("r.4", RelationshipType.supports, "cl.z.1", "cl.thesis"),
        _rel("r.5", RelationshipType.supports, "cl.a.2", "cl.thesis"),
        _rel("r.6", RelationshipType.extends, "cl.a.3", "cl.a.1"),
        _rel("r.7", RelationshipType.extends, "cl.b.3", "cl.b.1"),
        _rel("r.8", RelationshipType.qualifies, "cl.b.2", "cl.b.1"),
        _rel("r.9", RelationshipType.depends_on, "cl.c.1", "cl.c.2"),
    ]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    md = format_plan_markdown(plan)
    assert "Structure is healthy" in md or "no rescaffold proposed" in md


def test_formatter_renders_unhealthy_plan(academic_voice_path: Path) -> None:
    voice = _voice(academic_voice_path)
    claims = [
        _claim("cl.a.1", section_id="s.a", importance=0.7),
        _claim("cl.a.2", section_id="s.a", importance=0.7),
        _claim("cl.a.3", section_id="s.a", importance=0.7),
        _claim("cl.z.1", section_id="s.z", type_=ClaimType.user_synthesis,
               author_origin=True, importance=0.9),
    ]
    rels = [
        _rel("r.1", RelationshipType.supports, "cl.a.1", "cl.thesis"),
        _rel("r.2", RelationshipType.contradicts, "cl.a.2", "cl.thesis"),
        _rel("r.3", RelationshipType.contradicts, "cl.a.3", "cl.thesis"),
        _rel("r.4", RelationshipType.supports, "cl.z.1", "cl.thesis"),
    ]
    graph = _build(claims=claims, rels=rels)
    plan = plan_rescaffold(graph, voice)
    md = format_plan_markdown(plan)
    assert "## Diagnosis" in md
    assert "## Proposed operations" in md
    assert "Scoreboard" in md
    # Counter-engagement should appear in advisories.
    assert "add_counter_engagement" in md
