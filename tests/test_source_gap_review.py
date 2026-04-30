"""Tests for the source-gap review module."""
from __future__ import annotations

from pathlib import Path

import pytest

from lattice.auditor.source_gap_review import (
    Gap,
    SourceGapReport,
    SourceGapReview,
    _chunk_by_words,
    write_report,
)
from lattice.utils.config import Config


def _config(tmp_path: Path) -> Config:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    return Config.load(tmp_path)


# ─── Chunker ────────────────────────────────────────


def test_chunk_by_words_keeps_whole_paragraphs() -> None:
    paragraphs = [
        "First paragraph " + ("word " * 100).strip(),
        "Second paragraph " + ("word " * 100).strip(),
        "Third paragraph " + ("word " * 100).strip(),
    ]
    text = "\n\n".join(paragraphs)
    # Force chunking
    chunks = _chunk_by_words(text, target_words=150)
    # Each chunk should contain whole paragraphs (joined with \n\n)
    for chunk in chunks:
        for piece in chunk.split("\n\n"):
            assert piece in paragraphs


def test_chunk_by_words_short_text_one_chunk() -> None:
    text = "Hello world."
    chunks = _chunk_by_words(text, target_words=3500)
    assert len(chunks) == 1
    assert chunks[0] == text


# ─── Report rendering ───────────────────────────────


def test_report_to_markdown_groups_by_category(tmp_path: Path) -> None:
    report = SourceGapReport(
        paper_path=tmp_path / "paper.md",
        reference_path=tmp_path / "reference.md",
        gaps=[
            Gap(gap_id="g1", category="quantitative", summary="Missing 21% figure",
                reference_snippet="only around 21% of transistors",
                suggested_action="Add this figure to claim cl.gap1.dennard"),
            Gap(gap_id="g2", category="analytical_move", summary="Missing diagnostic sentence",
                reference_snippet="reading X as Y mistakes A for B"),
            Gap(gap_id="g3", category="quantitative", summary="Missing 70% collapse",
                reference_snippet="wholesale telecom pricing collapsed by roughly 70%"),
        ],
    )
    md = report.to_markdown()
    assert "3 gap(s)" in md
    assert "Specific numbers the render omits" in md
    assert "Analytical moves the render flattens" in md
    # Both quantitative gaps under the same heading
    assert md.count("Missing 21% figure") == 1
    assert md.count("Missing 70% collapse") == 1


def test_write_report_creates_outputs_dir(tmp_path: Path) -> None:
    report = SourceGapReport(
        paper_path=tmp_path / "paper.md",
        reference_path=tmp_path / "ref.md",
        gaps=[Gap(gap_id="g1", category="mechanism", summary="x", reference_snippet="y")],
    )
    out_path = write_report(report, project_path=tmp_path, voice_name="academic")
    assert out_path.exists()
    assert out_path.name == "source_gap_review.academic.md"
    assert "x" in out_path.read_text(encoding="utf-8")
    # JSON sidecar also written.
    json_path = tmp_path / ".lattice" / "source_gap_review.academic.json"
    assert json_path.exists()


def test_report_round_trips_json(tmp_path: Path) -> None:
    """Report JSON serialisation/deserialisation preserves fields."""
    from lattice.auditor.source_gap_review import load_report

    report = SourceGapReport(
        paper_path=tmp_path / "paper.md",
        reference_path=tmp_path / "ref.md",
        gaps=[
            Gap(
                gap_id="g1", category="mechanism",
                summary="dark silicon",
                reference_snippet="21% of transistors at 8nm",
                suggested_action="add to cl.efficiency",
                target_claim_id="cl.efficiency.dennard",
                decision="accepted",
            ),
        ],
    )
    write_report(report, project_path=tmp_path, voice_name="academic")
    loaded = load_report(tmp_path, "academic")
    assert loaded is not None
    assert len(loaded.gaps) == 1
    g = loaded.gaps[0]
    assert g.gap_id == "g1"
    assert g.target_claim_id == "cl.efficiency.dennard"
    assert g.decision == "accepted"


# ─── End-to-end with stub LLM ──────────────────────


class _StubLLM:
    """Returns canned gap payloads for each chunk."""

    def __init__(self, payloads: list) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, model=None, temperature=0.2):
        self.calls.append((system, user))
        if not self.payloads:
            return {"gaps": []}, None
        return self.payloads.pop(0), None


async def test_review_aggregates_and_filters_invalid_categories(tmp_path: Path) -> None:
    paper = tmp_path / "paper.md"
    paper.write_text("# Paper\n\nShort body.\n", encoding="utf-8")
    reference = tmp_path / "ref.md"
    reference.write_text("Short reference text.\n", encoding="utf-8")

    payloads = [
        {"gaps": [
            {"category": "quantitative",
             "summary": "Missing 21%",
             "reference_snippet": "21% of transistors",
             "suggested_action": "add to claim X",
             "target_claim_id": "cl.efficiency.dennard"},
            {"category": "named_scholar",
             "summary": "No engagement with Sorrell",
             "reference_snippet": "Sorrell distinguishes partial rebound",
             "target_claim_id": ""},
            {"category": "invalid_category",
             "summary": "Should be dropped",
             "reference_snippet": "..."},
            {"category": "mechanism",
             "summary": "",  # empty summary should be dropped
             "reference_snippet": "x"},
        ]},
    ]

    config = _config(tmp_path)
    review = SourceGapReview(config, _StubLLM(payloads))
    report = await review.review(paper_path=paper, reference_path=reference)

    cats = [g.category for g in report.gaps]
    assert "quantitative" in cats
    assert "named_scholar" in cats
    assert "invalid_category" not in cats
    # Empty summary dropped.
    assert len(report.gaps) == 2
    # target_claim_id propagated through.
    quant = next(g for g in report.gaps if g.category == "quantitative")
    assert quant.target_claim_id == "cl.efficiency.dennard"
    # gap_id auto-generated.
    assert all(g.gap_id for g in report.gaps)


async def test_review_handles_llm_failure_per_chunk(tmp_path: Path) -> None:
    paper = (tmp_path / "p.md")
    paper.write_text("paper", encoding="utf-8")
    reference = (tmp_path / "r.md")
    reference.write_text("reference", encoding="utf-8")

    class _FailingLLM:
        async def complete_json(self, system, user, model=None, temperature=0.2):
            raise RuntimeError("boom")

    config = _config(tmp_path)
    review = SourceGapReview(config, _FailingLLM())
    report = await review.review(paper_path=paper, reference_path=reference)
    # Failure surfaces as a structural gap entry rather than crashing.
    assert any(g.category == "structural" for g in report.gaps)
