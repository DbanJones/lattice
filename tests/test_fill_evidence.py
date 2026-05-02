"""Tests for the focused evidence walkthrough.

Layers (mirroring test_fill_mechanisms): candidate selection (pure),
weak-grounding detection, supporter ordering, and apply behaviour for
each of the four actions plus idempotence.
"""
from __future__ import annotations

from pathlib import Path

from lattice.graph.models import (
    BindingStrength, Evidence, EvidenceStatus, RelationshipType,
)
from lattice.ingester.markdown import MarkdownOutlineIngester
from lattice.restructure.fill_evidence import (
    apply_evidence_edits,
    collect_candidates,
    EvidenceCandidate,
    EvidenceEdit,
    _existing_ref_keys,
    _is_weakly_grounded,
    _sanitise_citekey,
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


async def test_user_synthesis_excluded(tmp_path: Path) -> None:
    """user_synthesis claims are author-grounded by definition; they
    should never appear in the candidate list."""
    graph, report, _ = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical]\n"
        "  - Two. [type: user_synthesis]\n"
        "  - MY VIEW: three.\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    cids = {c.claim_id for c in cands}
    assert "cl.a.1" in cids
    assert "cl.a.2" not in cids  # user_synthesis
    assert "cl.a.3" not in cids  # MY VIEW → user_synthesis


async def test_strong_evidence_excluded(tmp_path: Path) -> None:
    """A claim with [evidence_status: bound] or strong evidence rows
    is grounded; not a candidate."""
    graph, report, _ = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Bound. [type: empirical] [evidence_status: bound]\n"
        "  - Source-hint. [type: empirical] [evidence_status: source_hint]\n"
        "  - Unbound. [type: empirical] [evidence_status: unbound]\n"
        "  - No status. [type: empirical]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    cids = {c.claim_id for c in cands}
    assert "cl.a.1" not in cids  # bound
    # source_hint, unbound, no_status are all weakly grounded.
    assert "cl.a.2" in cids
    assert "cl.a.3" in cids
    assert "cl.a.4" in cids


async def test_supporters_sort_first(tmp_path: Path) -> None:
    """At equal importance, claims that transitively support the thesis
    sort before non-supporters."""
    graph, report, _ = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Non-supporter. [type: empirical] [importance: 0.7]\n"
        "  - Supporter. [type: empirical] [importance: 0.7] [supports: thesis]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    # Supporter first.
    assert cands[0].claim_id == "cl.a.2"
    assert cands[0].is_supporter is True
    assert cands[1].is_supporter is False


async def test_min_importance_filters(tmp_path: Path) -> None:
    graph, report, _ = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - High. [type: empirical] [importance: 0.8]\n"
        "  - Low.  [type: empirical] [importance: 0.3]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report, min_importance=0.5)
    cids = {c.claim_id for c in cands}
    assert cids == {"cl.a.1"}


async def test_weak_grounding_predicate_direct() -> None:
    """Spot-check the predicate against synthetic claim values."""
    from datetime import datetime, timezone
    from lattice.graph.models import Claim, ClaimType, Confidence

    now = datetime.now(timezone.utc)

    def _c(**kw):
        return Claim(
            claim_id="cl.x", statement="x", type=ClaimType.empirical,
            confidence=Confidence.high,
            created_by="t", created_at=now, modified_at=now, **kw,
        )

    # No evidence, no status → weak.
    assert _is_weakly_grounded(_c())
    # evidence_status=bound → strong (regardless of evidence list).
    assert not _is_weakly_grounded(_c(evidence_status=EvidenceStatus.bound))
    # Strong evidence row, no status → strong.
    strong_ev = Evidence(source="x", passage="p",
                         binding_strength=BindingStrength.strong)
    assert not _is_weakly_grounded(_c(evidence=[strong_ev]))
    # Strong evidence + explicit source_hint → still weak (the author's
    # signal overrides the row).
    assert _is_weakly_grounded(_c(
        evidence=[strong_ev], evidence_status=EvidenceStatus.source_hint,
    ))
    # Weak-binding evidence, no status → weak.
    weak_ev = Evidence(source="x", passage="",
                       binding_strength=BindingStrength.weak)
    assert _is_weakly_grounded(_c(evidence=[weak_ev]))


# ─── apply: each action ──────────────────────────


async def test_add_ref_appends_citekey(tmp_path: Path) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Claim. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    edits = [EvidenceEdit(
        candidate=cands[0], action="add_ref", citekey="smith_2020",
    )]
    result = apply_evidence_edits(outline, edits, snapshot=False)
    text = outline.read_text(encoding="utf-8")
    assert result.edits_applied == 1
    assert "[ref: smith_2020]" in text


async def test_add_ref_idempotent(tmp_path: Path) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Claim. [type: empirical] [ref: smith_2020] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    # The claim has a ref already — but it's still weakly grounded
    # (binding_strength defaults to weak). So it's a candidate; trying
    # to add the SAME citekey should skip.
    edit = EvidenceEdit(
        candidate=cands[0], action="add_ref", citekey="smith_2020",
    )
    result = apply_evidence_edits(outline, [edit], snapshot=False)
    text = outline.read_text(encoding="utf-8")
    assert result.edits_applied == 0
    assert result.edits_skipped == 1
    assert text.count("smith_2020") == 1


async def test_add_ref_with_different_citekey_appends(tmp_path: Path) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Claim. [type: empirical] [ref: smith_2020] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    edit = EvidenceEdit(
        candidate=cands[0], action="add_ref", citekey="lee_2019",
    )
    result = apply_evidence_edits(outline, [edit], snapshot=False)
    text = outline.read_text(encoding="utf-8")
    assert result.edits_applied == 1
    assert "[ref: smith_2020]" in text
    assert "[ref: lee_2019]" in text


async def test_set_source_hint_appends_status(tmp_path: Path) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Claim. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    edit = EvidenceEdit(candidate=cands[0], action="set_source_hint")
    apply_evidence_edits(outline, [edit], snapshot=False)
    assert "[evidence_status: source_hint]" in outline.read_text(encoding="utf-8")


async def test_set_unbound_appends_status(tmp_path: Path) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Claim. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    edit = EvidenceEdit(candidate=cands[0], action="set_unbound")
    apply_evidence_edits(outline, [edit], snapshot=False)
    assert "[evidence_status: unbound]" in outline.read_text(encoding="utf-8")


async def test_evidence_status_idempotent(tmp_path: Path) -> None:
    """Re-applying the same status to a bullet that already has one is
    a no-op (the existing status takes precedence)."""
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Claim. [type: empirical] [evidence_status: source_hint] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    # Try to set unbound on a bullet that's already source_hint.
    edit = EvidenceEdit(candidate=cands[0], action="set_unbound")
    result = apply_evidence_edits(outline, [edit], snapshot=False)
    assert result.edits_applied == 0
    assert result.edits_skipped == 1
    text = outline.read_text(encoding="utf-8")
    assert text.count("evidence_status:") == 1
    assert "source_hint" in text
    assert "unbound" not in text


async def test_convert_to_synthesis_replaces_existing_type(
    tmp_path: Path,
) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - Claim. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    edit = EvidenceEdit(candidate=cands[0], action="convert_to_synthesis")
    apply_evidence_edits(outline, [edit], snapshot=False)
    text = outline.read_text(encoding="utf-8")
    assert "[type: user_synthesis]" in text
    assert "[type: empirical]" not in text


async def test_convert_to_synthesis_idempotent(tmp_path: Path) -> None:
    """Already-synthesis claims wouldn't even be candidates, but a
    stale candidate that gets through should be a no-op."""
    bogus = EvidenceCandidate(
        claim_id="cl.a.1", section_id="s.a", statement="x.",
        importance=0.8, line=1, original_excerpt="x.",
        claim_type="user_synthesis", current_status=None,
        has_evidence_rows=False, is_supporter=False,
    )
    outline = tmp_path / "outline.md"
    outline.write_text(
        "  - Claim. [type: user_synthesis]\n", encoding="utf-8"
    )
    edit = EvidenceEdit(candidate=bogus, action="convert_to_synthesis")
    result = apply_evidence_edits(outline, [edit], snapshot=False)
    assert result.edits_applied == 0
    assert result.edits_skipped == 1


# ─── round-trip + snapshot ───────────────────────


async def test_round_trip_through_ingester(tmp_path: Path) -> None:
    """After applying add_ref + set_unbound, re-ingesting reflects the
    new state on the matching claims."""
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - First. [type: empirical] [importance: 0.8]\n"
        "  - Second. [type: empirical] [importance: 0.7]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    edits = [
        EvidenceEdit(
            candidate=next(c for c in cands if c.claim_id == "cl.a.1"),
            action="add_ref", citekey="koomey_2015",
        ),
        EvidenceEdit(
            candidate=next(c for c in cands if c.claim_id == "cl.a.2"),
            action="set_unbound",
        ),
    ]
    apply_evidence_edits(outline, edits, snapshot=False)

    config = Config.load(tmp_path)
    ing2 = MarkdownOutlineIngester(config)
    g2 = await ing2.ingest(outline, project_name="t")
    by_id = {c.claim_id: c for c in g2.claims}
    assert any(ev.source == "koomey_2015" for ev in by_id["cl.a.1"].evidence)
    assert by_id["cl.a.2"].evidence_status == EvidenceStatus.unbound


async def test_snapshot_written(tmp_path: Path) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    before = outline.read_text(encoding="utf-8")
    cands = collect_candidates(graph, report)
    edit = EvidenceEdit(
        candidate=cands[0], action="add_ref", citekey="x",
    )
    apply_evidence_edits(outline, [edit], snapshot=True)
    snap = tmp_path / "outline.pre-fill-evidence.md"
    assert snap.exists()
    assert snap.read_text(encoding="utf-8") == before


async def test_skip_action_is_no_op(tmp_path: Path) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    before = outline.read_text(encoding="utf-8")
    edit = EvidenceEdit(candidate=cands[0], action="skip")
    result = apply_evidence_edits(outline, [edit], snapshot=False)
    assert result.edits_applied == 0
    assert outline.read_text(encoding="utf-8") == before


async def test_no_line_number_skips_gracefully(tmp_path: Path) -> None:
    bogus = EvidenceCandidate(
        claim_id="cl.a.1", section_id="s.a", statement="x.",
        importance=0.8, line=None, original_excerpt="x.",
        claim_type="empirical", current_status=None,
        has_evidence_rows=False, is_supporter=False,
    )
    outline = tmp_path / "outline.md"
    outline.write_text("  - Claim. [type: empirical]\n", encoding="utf-8")
    edit = EvidenceEdit(candidate=bogus, action="add_ref", citekey="x")
    result = apply_evidence_edits(outline, [edit], snapshot=False)
    assert result.edits_applied == 0
    assert result.edits_skipped == 1


# ─── helpers ─────────────────────────────────────


def test_existing_ref_keys_handles_comma_lists() -> None:
    line = "  - X. [ref: smith_2020, lee_2019] [ref: chen_2021]"
    assert _existing_ref_keys(line) == {"smith_2020", "lee_2019", "chen_2021"}


def test_sanitise_citekey() -> None:
    assert _sanitise_citekey("Smith 2020") == "smith_2020"
    assert _sanitise_citekey(" smith,2020 ") == "smith_2020"
    assert _sanitise_citekey("[smith]") == "smith"
    assert _sanitise_citekey("___") == ""


async def test_missing_citekey_skipped(tmp_path: Path) -> None:
    graph, report, outline = await _ingest(
        "# THESIS\n\nT.\n\n"
        "# A. Body\n\n"
        "  - One. [type: empirical] [importance: 0.8]\n",
        tmp_path,
    )
    cands = collect_candidates(graph, report)
    edit = EvidenceEdit(candidate=cands[0], action="add_ref", citekey="")
    result = apply_evidence_edits(outline, [edit], snapshot=False)
    assert result.edits_applied == 0
    assert result.edits_skipped == 1
