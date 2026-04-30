"""Tests for the markdown outline ingester (SPEC §4.2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from lattice.graph.models import ClaimType, Confidence, RelationshipType, SectionRole, Depth
from lattice.ingester.markdown import MarkdownOutlineIngester
from lattice.utils.config import Config


async def _ingest(outline: str, tmp_path: Path) -> "AuthorGraph":
    (tmp_path / "config.yml").write_text("default_voice: academic\n", encoding="utf-8")
    config = Config.load(tmp_path)
    ingester = MarkdownOutlineIngester(config)
    outline_path = tmp_path / "outline.md"
    outline_path.write_text(outline, encoding="utf-8")
    return await ingester.ingest(outline_path, project_name="test")


# ─── Thesis & sections ─────────────────────────────────

async def test_ingester_extracts_thesis(tmp_path: Path) -> None:
    graph = await _ingest(
        "# THESIS\n\nThe central claim in one sentence.\n\n"
        "# A. First section\n\n"
        "  - One bullet [ref: src_1]\n",
        tmp_path,
    )
    assert graph.thesis_statement == "The central claim in one sentence."
    thesis_claim = next(c for c in graph.claims if c.claim_id == "cl.thesis")
    assert thesis_claim.type == ClaimType.user_synthesis
    assert thesis_claim.author_origin
    # Thesis section is inserted before section A
    assert graph.sections[0].section_id == "s.thesis"
    assert graph.sections[1].section_id == "s.a"


async def test_ingester_parses_section_tags(tmp_path: Path) -> None:
    graph = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# B. Deep section [role: argumentative] [depth: deep] [words: 1200]\n\n"
        "  - A claim [ref: k_2015]\n",
        tmp_path,
    )
    section = next(s for s in graph.sections if s.section_id == "s.b")
    assert section.role == SectionRole.argumentative
    assert section.depth == Depth.deep
    assert section.target_length == 1200


# ─── Claim prefixes & tags ─────────────────────────────

async def test_my_view_creates_user_synthesis_supporting_thesis(tmp_path: Path) -> None:
    graph = await _ingest(
        "# THESIS\n\nThesis text.\n\n"
        "# A. Foo\n\n"
        "  - MY VIEW: this is my synthesis\n",
        tmp_path,
    )
    my_view = next(c for c in graph.claims if "synthesis" in c.statement)
    assert my_view.type == ClaimType.user_synthesis
    assert my_view.author_origin
    rel = next(r for r in graph.relationships if r.from_claim == my_view.claim_id)
    assert rel.type == RelationshipType.supports
    assert rel.to_claim == "cl.thesis"


async def test_counter_creates_contradicts_to_thesis(tmp_path: Path) -> None:
    graph = await _ingest(
        "# THESIS\n\nThesis text.\n\n"
        "# A. Foo\n\n"
        "  - COUNTER: not actually true\n",
        tmp_path,
    )
    counter = next(c for c in graph.claims if "not actually" in c.statement)
    assert counter.type == ClaimType.user_synthesis
    rel = next(r for r in graph.relationships if r.from_claim == counter.claim_id)
    assert rel.type == RelationshipType.contradicts
    assert rel.to_claim == "cl.thesis"


async def test_ref_tag_creates_evidence(tmp_path: Path) -> None:
    graph = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - A finding [ref: koomey_2015, andrae_2015]\n",
        tmp_path,
    )
    claim = next(c for c in graph.claims if c.claim_id == "cl.a.1")
    assert len(claim.evidence) == 2
    assert {e.source for e in claim.evidence} == {"koomey_2015", "andrae_2015"}
    # Passage not bound yet — that's the enricher's job.
    assert all(e.passage == "" for e in claim.evidence)


async def test_confidence_tags_apply(tmp_path: Path) -> None:
    graph = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - Strong claim [ref: a] [strong]\n"
        "  - Weak claim [ref: b] [weak]\n"
        "  - Contested claim [ref: c] [contested]\n",
        tmp_path,
    )
    by_id = {c.claim_id: c for c in graph.claims}
    assert by_id["cl.a.1"].confidence == Confidence.high
    assert by_id["cl.a.2"].confidence == Confidence.low
    assert by_id["cl.a.3"].confidence == Confidence.medium


async def test_role_and_skip_tags_stored_on_claim_tags(tmp_path: Path) -> None:
    graph = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - A claim [ref: x] [role: evidence]\n"
        "  - Another [ref: y] [skip]\n",
        tmp_path,
    )
    c1 = next(c for c in graph.claims if c.claim_id == "cl.a.1")
    c2 = next(c for c in graph.claims if c.claim_id == "cl.a.2")
    assert "role:evidence" in c1.tags
    assert "skip" in c2.tags


async def test_supports_tag_creates_relationship(tmp_path: Path) -> None:
    graph = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - Claim one [ref: a]\n"
        "  - Claim two [ref: b] [supports: thesis]\n",
        tmp_path,
    )
    c2 = next(c for c in graph.claims if c.claim_id == "cl.a.2")
    supports = [r for r in graph.relationships if r.from_claim == c2.claim_id]
    assert any(r.to_claim == "cl.thesis" and r.type == RelationshipType.supports for r in supports)


async def test_figure_line_attaches_to_section(tmp_path: Path) -> None:
    graph = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - Figure 1: forecast spread [central_contribution]\n"
        "  - A real claim [ref: a]\n",
        tmp_path,
    )
    section = next(s for s in graph.sections if s.section_id == "s.a")
    assert len(section.figure_ids) == 1
    # Figure doesn't create a claim
    assert len([c for c in graph.claims if "Figure" in c.statement]) == 0
    # But the real claim still gets cl.a.1 (figures don't consume sequence)
    assert any(c.claim_id == "cl.a.1" for c in graph.claims)


# ─── Multi-line bullets ─────────────────────────────────

async def test_continuation_line_appends_to_claim(tmp_path: Path) -> None:
    graph = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - First line of the claim\n"
        "    continues on the next line [ref: src]\n",
        tmp_path,
    )
    claim = graph.claims[-1]
    assert "continues on the next line" in claim.statement
    assert len(claim.evidence) == 1


# ─── Worked example parses end-to-end ────────────────

async def test_ingester_parses_worked_example(example_project: Path) -> None:
    outline_path = example_project / "structure" / "outline.md"
    assert outline_path.exists()
    (example_project / "config.yml").write_text("", encoding="utf-8")
    config = Config.load(example_project)
    graph = await MarkdownOutlineIngester(config).ingest(
        outline_path, project_name="ict_forecasting"
    )
    # Seven sections A–G plus the thesis section
    assert len(graph.sections) == 8
    assert graph.thesis_statement and "twenty-fold" in graph.thesis_statement
    # Every claim has a stable ID and author_graph-compatible fields
    for claim in graph.claims:
        assert claim.claim_id.startswith("cl.")
    # User-synthesis claims linked to thesis
    synth = [c for c in graph.claims if c.type == ClaimType.user_synthesis and c.claim_id != "cl.thesis"]
    assert synth, "expected some user_synthesis claims"
