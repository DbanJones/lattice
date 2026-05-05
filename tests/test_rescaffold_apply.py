"""Tests for the rescaffold apply step.

The planner produces a ``RescaffoldPlan``; the apply step takes a
plan plus per-operation accept/reject decisions and rewrites
``structure/outline.md`` accordingly. These tests cover the pure
functions (apply_operations, decide_batch, decide_interactive,
filter_accepted) and the I/O orchestrator (apply_to_project) on a
tmp-path project.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimType, Confidence,
    Evidence, Relationship, RelationshipStrength, RelationshipType,
    Section, SectionRole,
)
from lattice.restructure.rescaffold_apply import (
    APPLY_ORDER,
    Decision,
    OffcutRecord,
    apply_operations,
    apply_to_project,
    decide_batch,
    decide_interactive,
    filter_accepted,
    load_plan,
    render_offcuts_block,
)
from lattice.restructure.rescaffold_models import (
    RescaffoldOperation, RescaffoldPlan,
)


# ─── helpers (mirror test_rescaffold_planner) ───────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _claim(claim_id: str, *,
           section_id: str = "s.a",
           type_: ClaimType = ClaimType.empirical,
           importance: float = 0.5,
           statement: str | None = None) -> Claim:
    now = _now()
    return Claim(
        claim_id=claim_id,
        statement=statement or f"Statement of {claim_id}.",
        type=type_,
        confidence=Confidence.medium,
        importance=importance,
        evidence=[],
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


def _build_graph(*, claims: list[Claim], rels: list[Relationship] | None = None,
                 sections: list[Section] | None = None) -> AuthorGraph:
    now = _now()
    rels = rels or []
    if sections is None:
        section_ids = sorted({c.section_id for c in claims if c.section_id})
        sections = [
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
        ]
        for i, sid in enumerate(section_ids, start=1):
            sections.append(Section(
                section_id=sid, title=f"Section {sid}", position=i,
                role=SectionRole.argumentative,
                claim_ids=[c.claim_id for c in claims if c.section_id == sid],
            ))
    thesis = _claim("cl.thesis", type_=ClaimType.user_synthesis,
                    section_id="s.thesis", importance=1.0,
                    statement="The thesis statement.")
    return AuthorGraph(
        project_name="testproj", thesis_statement="The thesis statement.",
        sections=sections, claims=[thesis] + claims, relationships=rels,
        created_at=now, modified_at=now,
    )


def _op(op_id: str, kind: str, **kwargs) -> RescaffoldOperation:
    kwargs.setdefault("rationale", f"reason for {op_id}")
    kwargs.setdefault("confidence", 0.7)
    return RescaffoldOperation(op_id=op_id, kind=kind, **kwargs)


def _plan(operations: list[RescaffoldOperation]) -> RescaffoldPlan:
    return RescaffoldPlan(
        project_name="testproj",
        voice_name="academic",
        generated_at=_now(),
        operations=operations,
    )


# ─── apply_operations: per-kind ─────────────────────────


def test_split_section_creates_subsections() -> None:
    """split_section with two groups: parent keeps group 0, group 1
    becomes a new subsection with id ``<parent>.split1``."""
    claims = [
        _claim("cl.a.1"), _claim("cl.a.2"),
        _claim("cl.a.3"), _claim("cl.a.4"),
    ]
    graph = _build_graph(claims=claims)
    op = _op("op.1", "split_section",
             source_section_id="s.a",
             split_groups=[["cl.a.1", "cl.a.2"], ["cl.a.3", "cl.a.4"]])
    new_graph, _ = apply_operations(graph, [op])
    parent = next(s for s in new_graph.sections if s.section_id == "s.a")
    child = next(s for s in new_graph.sections if s.section_id == "s.a.split1")
    assert parent.claim_ids == ["cl.a.1", "cl.a.2"]
    assert child.claim_ids == ["cl.a.3", "cl.a.4"]
    # Claims' section_id is updated to follow them.
    moved = next(c for c in new_graph.claims if c.claim_id == "cl.a.3")
    assert moved.section_id == "s.a.split1"


def test_add_section_stub_appends_section_with_synthetic_id() -> None:
    graph = _build_graph(claims=[_claim("cl.a.1")])
    op = _op("op.1", "add_section_stub",
             new_section_role="counterargument",
             new_section_title="Counter-arguments",
             target_section_id="new:counterargument")
    new_graph, _ = apply_operations(graph, [op])
    new = next(s for s in new_graph.sections if s.section_id == "new:counterargument")
    assert new.title == "Counter-arguments"
    assert new.role == SectionRole.counterargument
    assert new.claim_ids == []


def test_move_claim_relocates_between_sections() -> None:
    graph = _build_graph(claims=[
        _claim("cl.a.1", section_id="s.a"),
        _claim("cl.a.2", section_id="s.a"),
        _claim("cl.b.1", section_id="s.b"),
    ])
    op = _op("op.1", "move_claim",
             target_claim_id="cl.a.2",
             source_section_id="s.a",
             target_section_id="s.b")
    new_graph, _ = apply_operations(graph, [op])
    sa = next(s for s in new_graph.sections if s.section_id == "s.a")
    sb = next(s for s in new_graph.sections if s.section_id == "s.b")
    assert "cl.a.2" not in sa.claim_ids
    assert "cl.a.2" in sb.claim_ids
    moved = next(c for c in new_graph.claims if c.claim_id == "cl.a.2")
    assert moved.section_id == "s.b"


def test_reorder_within_section_reorders_claims() -> None:
    graph = _build_graph(claims=[
        _claim("cl.a.1"), _claim("cl.a.2"), _claim("cl.a.3"),
    ])
    op = _op("op.1", "reorder_within_section",
             source_section_id="s.a",
             claim_order=["cl.a.3", "cl.a.1", "cl.a.2"])
    new_graph, _ = apply_operations(graph, [op])
    sa = next(s for s in new_graph.sections if s.section_id == "s.a")
    assert sa.claim_ids == ["cl.a.3", "cl.a.1", "cl.a.2"]


def test_promote_to_offcuts_removes_claim_and_returns_record() -> None:
    """The claim is dropped from sections + claims + relationships,
    and an OffcutRecord with the rendered bullet is returned."""
    claims = [
        _claim("cl.a.1", statement="Important claim"),
        _claim("cl.a.2", statement="Aside to drop"),
    ]
    rels = [_rel("r.1", RelationshipType.supports, "cl.a.2", "cl.a.1")]
    graph = _build_graph(claims=claims, rels=rels)
    op = _op("op.1", "promote_to_offcuts", target_claim_id="cl.a.2")
    new_graph, offcuts = apply_operations(graph, [op])
    assert "cl.a.2" not in {c.claim_id for c in new_graph.claims}
    assert all(r.from_claim != "cl.a.2" and r.to_claim != "cl.a.2"
               for r in new_graph.relationships)
    assert len(offcuts) == 1
    assert offcuts[0].claim_id == "cl.a.2"
    assert offcuts[0].section_id == "s.a"
    assert "Aside to drop" in offcuts[0].bullet_text


# ─── apply_operations: dependency ordering ──────────────


def test_split_runs_before_move_even_if_listed_after() -> None:
    """If a move targets a section that the split is supposed to
    create, the apply step must run the split first regardless of
    list order."""
    claims = [
        _claim("cl.a.1"), _claim("cl.a.2"),
        _claim("cl.a.3"), _claim("cl.a.4"),
        _claim("cl.b.1", section_id="s.b"),
    ]
    graph = _build_graph(claims=claims)
    move_op = _op("op.move", "move_claim",
                  target_claim_id="cl.b.1",
                  source_section_id="s.b",
                  target_section_id="s.a.split1")
    split_op = _op("op.split", "split_section",
                   source_section_id="s.a",
                   split_groups=[["cl.a.1", "cl.a.2"], ["cl.a.3", "cl.a.4"]])
    # Listed move-first; split_runs_first ordering should still apply.
    new_graph, _ = apply_operations(graph, [move_op, split_op])
    target = next(s for s in new_graph.sections if s.section_id == "s.a.split1")
    assert "cl.b.1" in target.claim_ids


def test_offcut_runs_last() -> None:
    """A reorder listed AFTER a promote_to_offcuts must still see the
    claim in the section. (If offcut ran first, the reorder would
    silently drop the missing id.)"""
    claims = [_claim("cl.a.1"), _claim("cl.a.2"), _claim("cl.a.3")]
    graph = _build_graph(claims=claims)
    offcut_op = _op("op.cut", "promote_to_offcuts", target_claim_id="cl.a.3")
    reorder_op = _op("op.ord", "reorder_within_section",
                     source_section_id="s.a",
                     claim_order=["cl.a.3", "cl.a.2", "cl.a.1"])
    new_graph, offcuts = apply_operations(graph, [offcut_op, reorder_op])
    sa = next(s for s in new_graph.sections if s.section_id == "s.a")
    # cl.a.3 is gone (offcut), but the reorder of the remaining claims
    # still applied — cl.a.2 leads cl.a.1.
    assert "cl.a.3" not in sa.claim_ids
    assert sa.claim_ids == ["cl.a.2", "cl.a.1"]
    assert len(offcuts) == 1


def test_unknown_op_kind_is_skipped() -> None:
    """``merge_sections`` is recognised by the model but the apply step
    skips it. An unknown kind should not raise."""
    graph = _build_graph(claims=[_claim("cl.a.1")])
    op = _op("op.1", "merge_sections", section_ids_to_merge=["s.a", "s.b"])
    new_graph, offcuts = apply_operations(graph, [op])
    assert offcuts == []
    # Sections unchanged
    assert {s.section_id for s in new_graph.sections} == {"s.thesis", "s.a"}


def test_apply_does_not_mutate_input_graph() -> None:
    """apply_operations must work on a copy."""
    claims = [_claim("cl.a.1"), _claim("cl.a.2")]
    graph = _build_graph(claims=claims)
    original_section = next(s for s in graph.sections if s.section_id == "s.a")
    original_ids = list(original_section.claim_ids)
    op = _op("op.1", "reorder_within_section",
             source_section_id="s.a",
             claim_order=["cl.a.2", "cl.a.1"])
    apply_operations(graph, [op])
    after = next(s for s in graph.sections if s.section_id == "s.a")
    assert after.claim_ids == original_ids


# ─── apply order is the canonical sequence ──────────────


def test_apply_order_constant_lists_all_kinds_planner_emits() -> None:
    """The dependency-order tuple must include every operation kind
    the planner can produce. If we add a new kind to the model we
    should be forced to update APPLY_ORDER too."""
    expected = {
        "split_section", "add_section_stub", "move_claim",
        "reorder_within_section", "promote_to_offcuts",
    }
    assert set(APPLY_ORDER) == expected


# ─── decisions ──────────────────────────────────────────


def test_decide_batch_above_threshold_accepts() -> None:
    plan = _plan([
        _op("op.high", "split_section", confidence=0.8),
        _op("op.mid", "split_section", confidence=0.6),
        _op("op.low", "split_section", confidence=0.4),
    ])
    decisions = decide_batch(plan, confidence_threshold=0.6)
    by_id = {d.op_id: d.decision for d in decisions}
    # Threshold is exclusive — 0.6 is NOT above 0.6.
    assert by_id == {"op.high": "accept", "op.mid": "reject", "op.low": "reject"}


def test_decide_interactive_calls_prompt_per_op_in_plan_order() -> None:
    plan = _plan([
        _op("op.1", "split_section"),
        _op("op.2", "move_claim",
            target_claim_id="cl.a.1",
            source_section_id="s.a", target_section_id="s.b"),
        _op("op.3", "reorder_within_section"),
    ])
    seen_order: list[str] = []
    def prompt(op):
        seen_order.append(op.op_id)
        return op.op_id == "op.2"  # accept only op.2
    decisions = decide_interactive(plan, prompt)
    assert seen_order == ["op.1", "op.2", "op.3"]
    assert {d.op_id: d.decision for d in decisions} == {
        "op.1": "reject", "op.2": "accept", "op.3": "reject",
    }


def test_filter_accepted_returns_ops_in_plan_order() -> None:
    """filter_accepted preserves plan order, not decision order."""
    op1 = _op("op.1", "split_section")
    op2 = _op("op.2", "move_claim", target_claim_id="x",
              source_section_id="a", target_section_id="b")
    op3 = _op("op.3", "reorder_within_section")
    plan = _plan([op1, op2, op3])
    decisions = [
        Decision(op_id="op.3", op_kind="reorder_within_section",
                 confidence=0.7, decision="accept", decided_at=_now()),
        Decision(op_id="op.1", op_kind="split_section",
                 confidence=0.7, decision="accept", decided_at=_now()),
        Decision(op_id="op.2", op_kind="move_claim",
                 confidence=0.7, decision="reject", decided_at=_now()),
    ]
    accepted = filter_accepted(plan, decisions)
    assert [op.op_id for op in accepted] == ["op.1", "op.3"]


# ─── load_plan: defensive parsing ───────────────────────


def test_load_plan_drops_malformed_operation_row(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A row missing ``op_id`` should be dropped with a warning, not
    raise. The valid rows around it should still parse."""
    plan_dict = {
        "project_name": "testproj",
        "voice_name": "academic",
        "generated_at": _now().isoformat(),
        "operations": [
            {  # valid
                "op_id": "op.good",
                "kind": "split_section",
                "rationale": "ok",
                "confidence": 0.7,
            },
            {  # missing op_id and kind — invalid
                "rationale": "bad",
                "confidence": 0.5,
            },
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_dict), encoding="utf-8")
    with caplog.at_level("WARNING"):
        plan = load_plan(plan_path)
    assert len(plan.operations) == 1
    assert plan.operations[0].op_id == "op.good"
    assert any("Dropping malformed operation row" in r.message for r in caplog.records)


def test_load_plan_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_plan(Path("/nonexistent/plan.json"))


def test_load_plan_invalid_json_raises_value_error(tmp_path: Path) -> None:
    p = tmp_path / "plan.json"
    p.write_text("not json {{{", encoding="utf-8")
    with pytest.raises(ValueError):
        load_plan(p)


# ─── orchestrator: snapshot, outline, offcuts, decisions ─


@pytest.fixture
def project_with_outline(tmp_path: Path) -> tuple[Path, AuthorGraph]:
    """A tmp project with structure/outline.md present + a graph
    matching it. The outline content is arbitrary — apply doesn't
    re-parse it; the graph drives the new outline."""
    project = tmp_path / "proj"
    (project / "structure").mkdir(parents=True)
    (project / ".lattice").mkdir(parents=True)
    outline = project / "structure" / "outline.md"
    outline.write_text(
        "# THESIS\n\nThe thesis statement.\n\n"
        "# A. Section s.a\n\n"
        "  - Statement of cl.a.1.\n"
        "  - Statement of cl.a.2.\n",
        encoding="utf-8",
    )
    claims = [_claim("cl.a.1"), _claim("cl.a.2")]
    graph = _build_graph(claims=claims)
    return project, graph


def test_apply_to_project_full_accept_batch(
    project_with_outline: tuple[Path, AuthorGraph],
) -> None:
    project, graph = project_with_outline
    op = _op("op.1", "reorder_within_section",
             source_section_id="s.a",
             claim_order=["cl.a.2", "cl.a.1"], confidence=0.8)
    plan = _plan([op])
    decisions = decide_batch(plan, confidence_threshold=0.6)
    result = apply_to_project(
        project, plan, decisions,
        voice_name="academic", mode="batch",
        confidence_threshold=0.6,
        graph=graph, save_graph=None,
    )
    assert result.operations_applied == 1
    # Snapshot exists and equals the pre-apply outline.
    assert result.snapshot_path.exists()
    snapshot = result.snapshot_path.read_text(encoding="utf-8")
    assert "cl.a.1" in snapshot or "Statement of cl.a.1" in snapshot
    # Outline rewritten with the reorder applied — cl.a.2 leads.
    new = result.outline_path.read_text(encoding="utf-8")
    a1_pos = new.index("Statement of cl.a.1")
    a2_pos = new.index("Statement of cl.a.2")
    assert a2_pos < a1_pos
    # Decisions log has the accept on disk.
    log = json.loads(result.decisions_path.read_text(encoding="utf-8"))
    assert log["mode"] == "batch"
    assert log["confidence_threshold"] == 0.6
    assert len(log["decisions"]) == 1
    assert log["decisions"][0]["decision"] == "accept"


def test_apply_to_project_full_reject_keeps_outline_byte_stable(
    project_with_outline: tuple[Path, AuthorGraph],
) -> None:
    """Zero accepts: outline.md is byte-equal to its pre-apply state.
    Snapshot still exists. Decisions log records all rejects."""
    project, graph = project_with_outline
    pre_outline = (project / "structure" / "outline.md").read_text(encoding="utf-8")
    op = _op("op.1", "reorder_within_section",
             source_section_id="s.a",
             claim_order=["cl.a.2", "cl.a.1"], confidence=0.3)
    plan = _plan([op])
    # Threshold above the op's confidence — full reject.
    decisions = decide_batch(plan, confidence_threshold=0.9)
    result = apply_to_project(
        project, plan, decisions,
        voice_name="academic", mode="batch",
        confidence_threshold=0.9,
        graph=graph, save_graph=None,
    )
    assert result.operations_applied == 0
    post_outline = result.outline_path.read_text(encoding="utf-8")
    assert post_outline == pre_outline
    # Snapshot still written.
    assert result.snapshot_path.exists()
    assert result.snapshot_path.read_text(encoding="utf-8") == pre_outline
    log = json.loads(result.decisions_path.read_text(encoding="utf-8"))
    assert all(d["decision"] == "reject" for d in log["decisions"])


def test_apply_to_project_partial_accept(
    project_with_outline: tuple[Path, AuthorGraph],
) -> None:
    project, graph = project_with_outline
    accepted_op = _op("op.accept", "reorder_within_section",
                      source_section_id="s.a",
                      claim_order=["cl.a.2", "cl.a.1"], confidence=0.8)
    rejected_op = _op("op.reject", "promote_to_offcuts",
                      target_claim_id="cl.a.1", confidence=0.4)
    plan = _plan([accepted_op, rejected_op])
    decisions = decide_batch(plan, confidence_threshold=0.6)
    result = apply_to_project(
        project, plan, decisions,
        voice_name="academic", mode="batch",
        confidence_threshold=0.6,
        graph=graph, save_graph=None,
    )
    assert result.operations_applied == 1
    # Reorder applied.
    new = result.outline_path.read_text(encoding="utf-8")
    assert new.index("Statement of cl.a.2") < new.index("Statement of cl.a.1")
    # cl.a.1 NOT moved to offcuts (reject).
    assert result.offcuts_path is None or not result.offcuts_path.exists()
    log = json.loads(result.decisions_path.read_text(encoding="utf-8"))
    by_id = {d["op_id"]: d["decision"] for d in log["decisions"]}
    assert by_id == {"op.accept": "accept", "op.reject": "reject"}


def test_apply_to_project_offcuts_appended(
    project_with_outline: tuple[Path, AuthorGraph],
) -> None:
    project, graph = project_with_outline
    op = _op("op.1", "promote_to_offcuts",
             target_claim_id="cl.a.2", confidence=0.8)
    plan = _plan([op])
    decisions = decide_batch(plan, confidence_threshold=0.6)
    result = apply_to_project(
        project, plan, decisions,
        voice_name="academic", mode="batch",
        confidence_threshold=0.6,
        graph=graph, save_graph=None,
    )
    assert result.offcut_count == 1
    assert result.offcuts_path is not None
    assert result.offcuts_path.exists()
    contents = result.offcuts_path.read_text(encoding="utf-8")
    assert "Statement of cl.a.2" in contents
    assert "Promoted offcuts" in contents
    # Origin section is annotated.
    assert "s.a" in contents


def test_apply_to_project_offcuts_appended_to_existing_file(
    project_with_outline: tuple[Path, AuthorGraph],
) -> None:
    """A second apply writes a NEW header block, preserving the
    earlier one — append-only history."""
    project, graph = project_with_outline
    offcuts_file = project / "structure" / "outline.offcuts.md"
    offcuts_file.write_text("# Earlier offcut\n\n  - some old bullet\n", encoding="utf-8")
    op = _op("op.1", "promote_to_offcuts",
             target_claim_id="cl.a.2", confidence=0.8)
    plan = _plan([op])
    decisions = decide_batch(plan, confidence_threshold=0.6)
    apply_to_project(
        project, plan, decisions,
        voice_name="academic", mode="batch",
        confidence_threshold=0.6,
        graph=graph, save_graph=None,
    )
    contents = offcuts_file.read_text(encoding="utf-8")
    assert "Earlier offcut" in contents
    assert "some old bullet" in contents
    assert "Statement of cl.a.2" in contents


def test_apply_to_project_save_graph_called_on_accept(
    project_with_outline: tuple[Path, AuthorGraph],
) -> None:
    project, graph = project_with_outline
    saved: list[AuthorGraph] = []
    op = _op("op.1", "reorder_within_section",
             source_section_id="s.a",
             claim_order=["cl.a.2", "cl.a.1"], confidence=0.8)
    plan = _plan([op])
    decisions = decide_batch(plan, confidence_threshold=0.6)
    apply_to_project(
        project, plan, decisions,
        voice_name="academic", mode="batch",
        confidence_threshold=0.6,
        graph=graph,
        save_graph=lambda g: saved.append(g),
    )
    assert len(saved) == 1
    sa = next(s for s in saved[0].sections if s.section_id == "s.a")
    assert sa.claim_ids == ["cl.a.2", "cl.a.1"]


def test_apply_to_project_save_graph_not_called_on_zero_accepts(
    project_with_outline: tuple[Path, AuthorGraph],
) -> None:
    """Zero accepts → graph hasn't changed → save_graph not called."""
    project, graph = project_with_outline
    saved: list[AuthorGraph] = []
    op = _op("op.1", "reorder_within_section", confidence=0.3)
    plan = _plan([op])
    decisions = decide_batch(plan, confidence_threshold=0.9)
    apply_to_project(
        project, plan, decisions,
        voice_name="academic", mode="batch",
        confidence_threshold=0.9,
        graph=graph,
        save_graph=lambda g: saved.append(g),
    )
    assert saved == []


def test_decisions_log_summary_counts() -> None:
    from lattice.restructure.rescaffold_apply import DecisionsLog
    log = DecisionsLog(
        project_name="t", voice_name="academic",
        plan_generated_at=_now(), applied_at=_now(), mode="batch",
        confidence_threshold=0.6,
        decisions=[
            Decision(op_id="a", op_kind="x", confidence=0.7,
                     decision="accept", decided_at=_now()),
            Decision(op_id="b", op_kind="x", confidence=0.4,
                     decision="reject", decided_at=_now()),
            Decision(op_id="c", op_kind="x", confidence=0.8,
                     decision="accept", decided_at=_now()),
        ],
    )
    assert log.accepted_count == 2
    assert log.rejected_count == 1


def test_render_offcuts_block_empty_returns_empty_string() -> None:
    assert render_offcuts_block([], applied_at=_now()) == ""


def test_render_offcuts_block_includes_origin_section() -> None:
    rec = OffcutRecord(
        claim_id="cl.x", section_id="s.a", statement="An aside.",
        bullet_text="  - An aside. [user_synthesis]",
    )
    block = render_offcuts_block([rec], applied_at=_now())
    assert "An aside." in block
    assert "s.a" in block
    assert block.startswith("# Promoted offcuts")
