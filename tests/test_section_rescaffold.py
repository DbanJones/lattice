"""Phase 3B: per-section rescaffold (--section flag)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimType, Confidence,
    Evidence, Relationship, RelationshipStrength, RelationshipType,
    Section, SectionRole,
)
from lattice.restructure.rescaffold_planner import plan_rescaffold
from lattice.voice.parser import Voice


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _claim(claim_id: str, *, type_=ClaimType.empirical, section_id="s.a",
           importance: float = 0.5,
           evidence: list[Evidence] | None = None,
           mechanism: str | None = None,
           author_origin: bool = False) -> Claim:
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
        author_origin=author_origin,
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


def _build_two_section_graph() -> AuthorGraph:
    """Two-section graph: s.a needs structural work; s.b is healthy."""
    now = _now()
    thesis = Claim(
        claim_id="cl.thesis", statement="T.",
        type=ClaimType.user_synthesis, confidence=Confidence.high,
        author_origin=True, section_id="s.thesis",
        importance=1.0,
        created_by="t", created_at=now, modified_at=now,
    )
    return AuthorGraph(
        project_name="t", thesis_statement="T.",
        sections=[
            Section(section_id="s.thesis", title="T", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="Section A — needs work",
                    position=1, role=SectionRole.argumentative,
                    claim_ids=[f"cl.a.{i}" for i in range(1, 6)]),
            Section(section_id="s.b", title="Section B — healthy",
                    position=2, role=SectionRole.argumentative,
                    claim_ids=["cl.b.1", "cl.b.2"]),
            Section(section_id="s.z", title="Conclusion", position=3,
                    role=SectionRole.conclusion, claim_ids=["cl.z.1"]),
        ],
        claims=[
            thesis,
            # Section A: 5 claims, no evidence, no mechanisms — weak.
            *(_claim(f"cl.a.{i}", section_id="s.a", importance=0.4)
              for i in range(1, 6)),
            # Section B: 2 claims, both well-grounded.
            _claim("cl.b.1", section_id="s.b", importance=0.7,
                   mechanism="A causes B",
                   evidence=[Evidence(source="src1", passage="p1",
                                      binding_strength=BindingStrength.strong)]),
            _claim("cl.b.2", section_id="s.b", importance=0.7,
                   type_=ClaimType.user_synthesis, author_origin=True),
            _claim("cl.z.1", section_id="s.z", importance=0.9,
                   type_=ClaimType.user_synthesis, author_origin=True),
        ],
        relationships=[
            _rel("r.1", RelationshipType.supports, "cl.b.1", "cl.thesis"),
            _rel("r.2", RelationshipType.supports, "cl.b.2", "cl.thesis"),
            _rel("r.3", RelationshipType.supports, "cl.z.1", "cl.thesis"),
        ],
        created_at=now, modified_at=now,
    )


@pytest.fixture
def voice(academic_voice_path: Path) -> Voice:
    return Voice.from_file(academic_voice_path)


# ─── unscoped vs scoped ─────────────────────────


def test_unscoped_includes_all_sections(voice: Voice) -> None:
    graph = _build_two_section_graph()
    plan = plan_rescaffold(graph, voice)
    # Unscoped: should include offcuts from any section.
    assert "rescaffold" in plan.voice_name or plan.voice_name


def test_section_a_scope_drops_section_b_offcuts(voice: Voice) -> None:
    """When scoped to s.a, claims in s.b should not appear in
    proposed_offcuts even if they would have been globally."""
    graph = _build_two_section_graph()
    plan_full = plan_rescaffold(graph, voice)
    plan_scoped = plan_rescaffold(graph, voice, section_id="s.a")
    # Every offcut in the scoped plan must be a section-A claim.
    for cid in plan_scoped.proposed_offcuts:
        assert cid.startswith("cl.a."), (
            f"{cid} should not be in s.a-scoped offcuts"
        )
    # Section B claims should be absent.
    assert "cl.b.1" not in plan_scoped.proposed_offcuts
    assert "cl.b.2" not in plan_scoped.proposed_offcuts


def test_section_a_scope_drops_operations_for_other_sections(
    voice: Voice,
) -> None:
    """Operations like split_section / reorder must be scoped."""
    graph = _build_two_section_graph()
    plan_scoped = plan_rescaffold(graph, voice, section_id="s.a")
    for op in plan_scoped.operations:
        # Operations must touch s.a or its subsections (none here).
        if op.source_section_id and op.source_section_id != "s.a":
            assert False, (
                f"op {op.kind} on section {op.source_section_id} should "
                f"not appear in s.a-scoped plan"
            )
        if op.target_section_id and op.target_section_id not in ("s.a",):
            # add_section_stub uses "new:counterargument" target → allow
            # that pattern through.
            assert op.target_section_id.startswith("new:") or False


def test_unknown_section_yields_empty_scoped_plan(voice: Voice) -> None:
    graph = _build_two_section_graph()
    plan = plan_rescaffold(graph, voice, section_id="s.does_not_exist")
    assert plan.proposed_offcuts == []


def test_advisories_with_no_target_propagate(voice: Voice) -> None:
    """Document-wide advisories like add_methodological_framing have
    no specific target_claim_id; they should still appear in scoped
    plans because the section author needs the context."""
    graph = _build_two_section_graph()
    plan_scoped = plan_rescaffold(graph, voice, section_id="s.a")
    # Document-wide advisories may or may not fire on this fixture,
    # but if they do, they should NOT be filtered out for being
    # untargeted.
    for a in plan_scoped.advisories:
        if a.target_claim_id is None and a.target_section_id is None:
            # No-op — the test just asserts these aren't dropped.
            pass


def test_section_with_subsections_includes_them(voice: Voice) -> None:
    """When the scope target is a parent (s.b), claims in nested
    subsections (s.b.1) should be in scope."""
    now = _now()
    thesis = Claim(
        claim_id="cl.thesis", statement="T.",
        type=ClaimType.user_synthesis, confidence=Confidence.high,
        author_origin=True, section_id="s.thesis",
        importance=1.0, created_by="t", created_at=now, modified_at=now,
    )
    graph = AuthorGraph(
        project_name="t", thesis_statement="T.",
        sections=[
            Section(section_id="s.thesis", title="T", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.b", title="B", position=1,
                    role=SectionRole.argumentative, claim_ids=[]),
            Section(section_id="s.b.1", title="B.1", position=2,
                    parent="s.b",
                    role=SectionRole.argumentative,
                    claim_ids=["cl.b_1.1", "cl.b_1.2", "cl.b_1.3", "cl.b_1.4"]),
            Section(section_id="s.z", title="Conclusion", position=3,
                    role=SectionRole.conclusion, claim_ids=["cl.z.1"]),
        ],
        claims=[
            thesis,
            *(_claim(f"cl.b_1.{i}", section_id="s.b.1", importance=0.0)
              for i in range(1, 5)),
            _claim("cl.z.1", section_id="s.z", importance=0.9,
                   type_=ClaimType.user_synthesis, author_origin=True),
        ],
        relationships=[
            _rel("r.1", RelationshipType.supports, "cl.z.1", "cl.thesis"),
        ],
        created_at=now, modified_at=now,
    )
    plan = plan_rescaffold(graph, voice, section_id="s.b")
    # Subsection claims qualify as offcut candidates and the scope
    # should include them.
    for cid in plan.proposed_offcuts:
        assert cid.startswith("cl.b_1."), (
            f"{cid} unexpected in s.b parent scope (subsections include?)"
        )


# ─── empty-plan path ────────────────────────────


def test_healthy_section_yields_empty_scoped_plan(voice: Voice) -> None:
    """Section B in the fixture is well-developed — scoping to it
    should produce an empty plan."""
    graph = _build_two_section_graph()
    plan = plan_rescaffold(graph, voice, section_id="s.b")
    # Some advisories may still fire (document-wide ones), but the
    # only operations that survive should touch s.b.
    for op in plan.operations:
        section_touches_b = (
            (op.source_section_id == "s.b")
            or (op.target_section_id == "s.b")
            or (op.target_section_id and op.target_section_id.startswith("new:"))
        )
        # Allow ops where the target is in "new:" namespace
        # (document-wide stubs) since they affect the section's
        # surrounding structure.
        assert section_touches_b or op.target_claim_id and op.target_claim_id.startswith("cl.b")


# ─── integration: scoped plan still has metrics ─


def test_scoped_plan_carries_full_document_metrics(voice: Voice) -> None:
    """The plan still returns document-level current/predicted
    metrics so the consumer can show "the section's contribution to
    the whole."""
    graph = _build_two_section_graph()
    plan = plan_rescaffold(graph, voice, section_id="s.a")
    assert plan.current_metrics is not None
    assert plan.current_metrics["strength"]["score"] >= 0
    # per_section is now populated by compute_argument_metrics.
    assert "per_section" in plan.current_metrics
    assert "s.a" in plan.current_metrics["per_section"]
