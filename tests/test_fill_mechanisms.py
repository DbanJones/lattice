"""Tests for the focused mechanism walkthrough.

Two layers: candidate selection (pure function) and outline editing
(in-place line edits with snapshots, idempotence, skip handling).
"""
from __future__ import annotations

from pathlib import Path

from lattice.ingester.markdown import MarkdownOutlineIngester
from lattice.restructure.fill_mechanisms import (
    apply_mechanism_edits,
    collect_candidates,
    MechanismCandidate,
    MechanismEdit,
)
from lattice.utils.config import Config


async def _ingest(outline: str, tmp_path: Path) -> tuple:
    (tmp_path / "config.yml").write_text(
        "default_voice: academic\n", encoding="utf-8"
    )
    config = Config.load(tmp_path)
    outline_path = tmp_path / "outline.md"
    outline_path.write_text(outline, encoding="utf-8")
    ing = MarkdownOutlineIngester(config)
    graph = await ing.ingest(outline_path, project_name="t")
    return graph, ing.last_report, outline_path


# ─── candidate selection ─────────────────────────


async def test_only_empirical_methodological_above_floor_qualify(
    tmp_path: Path,
) -> None:
    graph, report, _ = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - High empirical, no mechanism. [type: empirical] [importance: 0.8]\n"
        "  - Methodological, no mech. [type: methodological] [importance: 0.7]\n"
        "  - Empirical with mechanism already. [type: empirical] [importance: 0.8] [mechanism: A causes B]\n"
        "  - Empirical too low importance. [type: empirical] [importance: 0.4]\n"
        "  - Synthesis (excluded by type). [type: user_synthesis] [importance: 0.9]\n"
        "  - Definition (excluded by type). [type: definition] [importance: 0.7]\n"
        "  - Normative (excluded by type). [type: normative] [importance: 0.7]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    cids = [c.claim_id for c in cands]
    assert cids == ["cl.a.1", "cl.a.2"]


async def test_candidates_sorted_by_importance_desc(tmp_path: Path) -> None:
    graph, report, _ = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Medium. [type: empirical] [importance: 0.7]\n"
        "  - Top. [type: empirical] [importance: 0.95]\n"
        "  - Floor. [type: empirical] [importance: 0.6]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    assert [c.importance for c in cands] == [0.95, 0.7, 0.6]


async def test_candidate_has_line_number(tmp_path: Path) -> None:
    """The candidate must know which outline line to edit."""
    graph, report, _ = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    assert len(cands) == 1
    assert cands[0].line is not None and cands[0].line > 0


# ─── outline editing ─────────────────────────────


async def test_edit_appends_mechanism_tag_to_correct_line(
    tmp_path: Path,
) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n"
        "  - Two. [type: empirical] [importance: 0.7]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    edits = [
        MechanismEdit(candidate=cands[0], mechanism="X drives Y"),
        MechanismEdit(candidate=cands[1], mechanism="A causes B"),
    ]
    result = apply_mechanism_edits(outline, edits, snapshot=False)
    text = outline.read_text(encoding="utf-8")
    assert result.edits_applied == 2
    assert "[mechanism: X drives Y]" in text
    assert "[mechanism: A causes B]" in text


async def test_skipped_edit_is_a_no_op(tmp_path: Path) -> None:
    """An empty mechanism string means skip — outline mustn't change."""
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    before = outline.read_text(encoding="utf-8")
    edits = [MechanismEdit(candidate=cands[0], mechanism="")]
    result = apply_mechanism_edits(outline, edits, snapshot=False)
    after = outline.read_text(encoding="utf-8")
    assert result.edits_applied == 0
    assert result.edits_skipped == 1
    assert after == before


async def test_idempotent_against_stale_candidate_with_existing_tag(
    tmp_path: Path,
) -> None:
    """If the bullet already has [mechanism: ...] (stale candidate),
    skip — no double-tagging."""
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    # First pass tags it.
    apply_mechanism_edits(
        outline,
        [MechanismEdit(candidate=cands[0], mechanism="first mech")],
        snapshot=False,
    )
    # Second pass with the OLD candidate (line still right; tag now there)
    # should be a no-op.
    result = apply_mechanism_edits(
        outline,
        [MechanismEdit(candidate=cands[0], mechanism="second attempt")],
        snapshot=False,
    )
    text = outline.read_text(encoding="utf-8")
    assert result.edits_applied == 0
    assert result.edits_skipped == 1
    # Original tag preserved; no second tag appended.
    assert text.count("[mechanism:") == 1
    assert "first mech" in text
    assert "second attempt" not in text


async def test_snapshot_written_before_edit(tmp_path: Path) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    before = outline.read_text(encoding="utf-8")
    cands = collect_candidates(graph, report)
    apply_mechanism_edits(
        outline,
        [MechanismEdit(candidate=cands[0], mechanism="mech")],
        snapshot=True,
    )
    snapshot = tmp_path / "outline.pre-fill-mechanisms.md"
    assert snapshot.exists()
    assert snapshot.read_text(encoding="utf-8") == before


async def test_mechanism_with_closing_bracket_is_sanitised(
    tmp_path: Path,
) -> None:
    """The tag parser stops at the first `]`; allowing one in the
    mechanism body would corrupt the bullet."""
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    apply_mechanism_edits(
        outline,
        [MechanismEdit(
            candidate=cands[0],
            mechanism="X causes Y [under Z conditions]",
        )],
        snapshot=False,
    )
    text = outline.read_text(encoding="utf-8")
    # The closing bracket inside the value gets replaced with `)`.
    assert "[mechanism: X causes Y (under Z conditions)]" in text


async def test_edit_round_trips_through_ingester(tmp_path: Path) -> None:
    """After the edit, re-ingesting the outline should produce a graph
    with the mechanism populated on the original claim."""
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    apply_mechanism_edits(
        outline,
        [MechanismEdit(candidate=cands[0], mechanism="A drives B")],
        snapshot=False,
    )
    config = Config.load(tmp_path)
    ing2 = MarkdownOutlineIngester(config)
    g2 = await ing2.ingest(outline, project_name="t")
    by_id = {c.claim_id: c for c in g2.claims}
    assert by_id["cl.a.1"].mechanism == "A drives B"


async def test_no_line_number_skips_gracefully(tmp_path: Path) -> None:
    """A candidate built without a line (synthetic test scenario) should
    skip rather than crash."""
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    bogus = MechanismCandidate(
        claim_id="cl.a.1", section_id="s.a",
        statement="One.", importance=0.8,
        line=None, original_excerpt="One. [type: empirical]",
        claim_type="empirical",
    )
    result = apply_mechanism_edits(
        outline, [MechanismEdit(candidate=bogus, mechanism="x")],
        snapshot=False,
    )
    assert result.edits_applied == 0
    assert result.edits_skipped == 1
