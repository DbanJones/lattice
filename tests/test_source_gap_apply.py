"""Tests for the source-gap apply module."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.auditor.source_gap_apply import (
    apply_gap,
    apply_report,
    log_decisions,
)
from lattice.auditor.source_gap_review import Gap, SourceGapReport
from lattice.graph.models import (
    AuthorGraph,
    BindingStrength,
    Claim,
    ClaimType,
    Confidence,
    Section,
    SectionRole,
)
from lattice.graph.store import GraphStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_graph_with_claim(claim_id: str = "cl.x.1") -> AuthorGraph:
    now = _now()
    claim = Claim(
        claim_id=claim_id,
        statement="Dennard scaling broke down around 2006.",
        type=ClaimType.empirical,
        confidence=Confidence.high,
        section_id="s.x",
        created_by="test",
        created_at=now,
        modified_at=now,
    )
    section = Section(
        section_id="s.x", title="X", position=1,
        role=SectionRole.argumentative,
        claim_ids=[claim_id],
    )
    return AuthorGraph(
        project_name="t",
        sections=[section], claims=[claim], relationships=[],
        created_at=now, modified_at=now,
    )


# ─── apply_gap: per-category injection ────────────────


def test_skips_gap_not_yet_decided() -> None:
    graph = _seed_graph_with_claim()
    gap = Gap(gap_id="g1", category="mechanism", summary="x",
              reference_snippet="y", target_claim_id="cl.x.1")
    # decision is None
    result = apply_gap(gap, graph)
    assert result.action == "skipped_already_decided"
    assert graph.claims[0].mechanism is None


def test_skips_rejected_gap() -> None:
    graph = _seed_graph_with_claim()
    gap = Gap(gap_id="g1", category="mechanism", summary="x",
              reference_snippet="y", target_claim_id="cl.x.1",
              decision="rejected")
    result = apply_gap(gap, graph)
    assert result.action == "skipped_already_decided"
    assert graph.claims[0].mechanism is None


def test_applies_mechanism_to_claim() -> None:
    graph = _seed_graph_with_claim()
    gap = Gap(
        gap_id="g1", category="mechanism",
        summary="dark silicon explanation",
        reference_snippet=(
            "Dark silicon is the regime where transistors must remain "
            "powered down to respect thermal limits, forcing parallelism."
        ),
        target_claim_id="cl.x.1",
        decision="accepted",
    )
    result = apply_gap(gap, graph)
    assert result.action == "applied_mechanism"
    assert result.target_claim_id == "cl.x.1"
    assert graph.claims[0].mechanism is not None
    assert "Dark silicon" in graph.claims[0].mechanism


def test_appends_to_existing_mechanism() -> None:
    """Existing mechanism is preserved; new content concatenated with separator."""
    graph = _seed_graph_with_claim()
    graph.claims[0].mechanism = "Existing mechanism text."

    gap = Gap(
        gap_id="g1", category="mechanism",
        summary="additional",
        reference_snippet="New mechanism content from reference.",
        target_claim_id="cl.x.1",
        decision="accepted",
    )
    result = apply_gap(gap, graph)
    assert result.action == "applied_mechanism"
    assert "Existing mechanism text" in graph.claims[0].mechanism
    assert "New mechanism content" in graph.claims[0].mechanism
    assert " | " in graph.claims[0].mechanism


def test_applies_quantitative_as_evidence_quote() -> None:
    graph = _seed_graph_with_claim()
    gap = Gap(
        gap_id="g1", category="quantitative",
        summary="Esmaeilzadeh 21% figure",
        reference_snippet=(
            "Esmaeilzadeh et al. (2011) estimated that on a chip "
            "fabricated at 8nm, only around 21% of transistors could run "
            "at full speed without breaching thermal design constraints."
        ),
        target_claim_id="cl.x.1",
        decision="accepted",
    )
    result = apply_gap(gap, graph)
    assert result.action == "applied_evidence"

    claim = graph.claims[0]
    # New evidence appended with the snippet as quote_text.
    assert len(claim.evidence) == 1
    ev = claim.evidence[0]
    assert ev.source == "expanded_lit_review"
    assert ev.binding_strength == BindingStrength.weak
    assert ev.quote_verbatim is True
    assert "21% of transistors" in ev.quote_text


@pytest.mark.parametrize("category", [
    "arithmetic", "named_scholar", "named_example",
])
def test_other_quote_categories_apply_as_evidence(category: str) -> None:
    graph = _seed_graph_with_claim()
    gap = Gap(
        gap_id="g1", category=category,
        summary="x",
        reference_snippet="A specific quote from the reference.",
        target_claim_id="cl.x.1",
        decision="accepted",
    )
    result = apply_gap(gap, graph)
    assert result.action == "applied_evidence"
    assert len(graph.claims[0].evidence) == 1


def test_dedupes_same_quote() -> None:
    """Applying the same snippet twice doesn't duplicate evidence."""
    graph = _seed_graph_with_claim()
    snippet = "A specific quote."
    g1 = Gap(gap_id="g1", category="quantitative", summary="x",
             reference_snippet=snippet, target_claim_id="cl.x.1",
             decision="accepted")
    g2 = Gap(gap_id="g2", category="quantitative", summary="x",
             reference_snippet=snippet, target_claim_id="cl.x.1",
             decision="accepted")
    apply_gap(g1, graph)
    result2 = apply_gap(g2, graph)
    assert result2.action == "skipped_already_decided"
    assert len(graph.claims[0].evidence) == 1


def test_logs_manual_for_analytical_move() -> None:
    """analytical_move category requires manual graph edit (interpretive_pivot relationship)."""
    graph = _seed_graph_with_claim()
    gap = Gap(
        gap_id="g1", category="analytical_move",
        summary="diagnostic sentence",
        reference_snippet="Reading X as Y mistakes A for B.",
        target_claim_id="cl.x.1",
        decision="accepted",
    )
    result = apply_gap(gap, graph)
    assert result.action == "logged_manual"
    # No graph mutation.
    assert graph.claims[0].mechanism is None
    assert graph.claims[0].evidence == []


def test_logs_manual_for_structural() -> None:
    graph = _seed_graph_with_claim()
    gap = Gap(
        gap_id="g1", category="structural",
        summary="missing References section",
        reference_snippet="...",
        decision="accepted",
    )
    result = apply_gap(gap, graph)
    assert result.action == "logged_manual"


def test_skipped_when_target_not_in_graph() -> None:
    graph = _seed_graph_with_claim()
    gap = Gap(
        gap_id="g1", category="mechanism", summary="x",
        reference_snippet="y", target_claim_id="cl.does.not.exist",
        decision="accepted",
    )
    result = apply_gap(gap, graph)
    assert result.action == "skipped_no_target"


def test_skipped_when_no_target() -> None:
    graph = _seed_graph_with_claim()
    gap = Gap(
        gap_id="g1", category="mechanism", summary="x",
        reference_snippet="y", target_claim_id="",
        decision="accepted",
    )
    result = apply_gap(gap, graph)
    assert result.action == "skipped_no_target"


# ─── apply_report end-to-end ────────────────────────


def test_apply_report_persists_graph(tmp_path: Path) -> None:
    """apply_report saves the graph after batch — full pipeline."""
    # Initialise a project
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    seed = _seed_graph_with_claim()
    store.save_graph(seed)

    report = SourceGapReport(
        paper_path=tmp_path / "p.md",
        reference_path=tmp_path / "r.md",
        gaps=[
            Gap(gap_id="g1", category="mechanism",
                summary="dark silicon",
                reference_snippet="Dark silicon mechanism explanation.",
                target_claim_id="cl.x.1",
                decision="accepted"),
            Gap(gap_id="g2", category="quantitative",
                summary="21% figure",
                reference_snippet="21% of transistors at 8nm",
                target_claim_id="cl.x.1",
                decision="accepted"),
            Gap(gap_id="g3", category="structural",
                summary="missing section",
                reference_snippet="...",
                decision="accepted"),
            Gap(gap_id="g4", category="mechanism",
                summary="ignored",
                reference_snippet="...",
                target_claim_id="cl.x.1",
                decision="rejected"),
        ],
    )
    results = apply_report(report, store)

    actions = [r.action for r in results]
    assert "applied_mechanism" in actions
    assert "applied_evidence" in actions
    assert "logged_manual" in actions
    assert "skipped_already_decided" in actions

    # Re-load the saved graph and confirm the changes stuck.
    store2 = GraphStore.load(tmp_path)
    claim = next(c for c in store2.get_graph().claims if c.claim_id == "cl.x.1")
    assert claim.mechanism is not None
    assert any(ev.source == "expanded_lit_review" for ev in claim.evidence)


def test_log_decisions_appends_to_existing_log(tmp_path: Path) -> None:
    """log_decisions creates the log on first call and appends on subsequent ones."""
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    store.save_graph(_seed_graph_with_claim())

    report = SourceGapReport(
        paper_path=tmp_path / "p.md",
        reference_path=tmp_path / "r.md",
        gaps=[Gap(gap_id="g1", category="mechanism", summary="x",
                  reference_snippet="y", target_claim_id="cl.x.1",
                  decision="accepted")],
    )
    results1 = apply_report(report, store)
    log_decisions(report, tmp_path, "academic", results1)

    # Second pass: nothing more to apply (already decided), but still logs.
    results2 = apply_report(report, store)
    log_decisions(report, tmp_path, "academic", results2)

    log_path = tmp_path / ".lattice" / "source_gap_decisions.json"
    import json
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(log) == 2
    assert log[0]["voice"] == "academic"
