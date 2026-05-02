"""Tests for the citation scanner — Phase A of the references package."""
from __future__ import annotations

from pathlib import Path

from lattice.graph.models import (
    CitationLocationKind, CitationSystem, DocumentCitations,
)
from lattice.references.scanner import (
    load_document_citations,
    save_document_citations,
    scan_document,
)


def _doc(text: str) -> DocumentCitations:
    return scan_document(text, project_name="t", document_path="paper.md")


# ─── system detection ──────────────────────────


def test_detects_author_date_system() -> None:
    text = (
        "Climate forecasts diverge (Smith, 2020). Many studies "
        "(Lee, 2019; Park, 2021) confirm. Andrae (2015) projects more.\n\n"
        "# References\n\nSmith, J. (2020). X. Nature, 1.\n"
    )
    doc = _doc(text)
    assert doc.detected_system == CitationSystem.author_date


def test_detects_numeric_system() -> None:
    text = (
        "Studies [1] and [2-4] confirm the model, with later work [5, 6] "
        "extending to mobile networks [7].\n\n"
        "# References\n\n[1] Smith. X. Nature.\n[2] Lee. Y. JX.\n"
    )
    doc = _doc(text)
    assert doc.detected_system == CitationSystem.numeric


def test_detects_mixed_system() -> None:
    text = (
        "Some inline (Smith, 2020) and some numeric [1] and another "
        "(Lee, 2019).\n\n"
        "# References\n\nSmith, J. (2020).\n"
    )
    doc = _doc(text)
    assert doc.detected_system == CitationSystem.mixed


def test_empty_document_unknown_system() -> None:
    doc = _doc("Just some prose with nothing cited.")
    assert doc.detected_system == CitationSystem.unknown


# ─── parenthetical extraction ──────────────────


def test_single_parenthetical_extracted() -> None:
    doc = _doc("Climate forecasts diverge (Smith, 2020).")
    assert len(doc.inline_citations) == 1
    c = doc.inline_citations[0]
    assert c.kind == CitationLocationKind.parenthetical
    assert c.cited_authors == ["Smith"]
    assert c.cited_year == 2020


def test_multi_source_parenthetical_splits_into_entries() -> None:
    """`(Smith, 2020; Lee, 2019; Chen, 2021)` should produce 3 entries
    sharing a span but with different authors/years."""
    doc = _doc("See (Smith, 2020; Lee, 2019; Chen, 2021).")
    assert len(doc.inline_citations) == 3
    years = sorted(c.cited_year for c in doc.inline_citations)
    assert years == [2019, 2020, 2021]


def test_two_authors_with_and() -> None:
    doc = _doc("(Lee and Park, 2019)")
    c = doc.inline_citations[0]
    assert c.cited_authors == ["Lee", "Park"]


def test_et_al_keeps_first_author_only() -> None:
    doc = _doc("(Chen et al., 2021)")
    c = doc.inline_citations[0]
    assert c.cited_authors == ["Chen"]
    assert c.cited_year == 2021


def test_year_letter_suffix_handled() -> None:
    doc = _doc("(Smith, 2020a; Smith, 2020b)")
    assert len(doc.inline_citations) == 2
    assert all(c.cited_year == 2020 for c in doc.inline_citations)


# ─── narrative extraction ──────────────────────


def test_narrative_form_extracted() -> None:
    doc = _doc("Smith (2020) argues that efficiency offsets growth.")
    assert any(
        c.kind == CitationLocationKind.narrative
        and c.cited_authors == ["Smith"]
        and c.cited_year == 2020
        for c in doc.inline_citations
    )


def test_narrative_two_authors() -> None:
    doc = _doc("Andrae and Edler (2015) project explosive growth.")
    cands = [
        c for c in doc.inline_citations
        if c.kind == CitationLocationKind.narrative
    ]
    assert cands and cands[0].cited_authors == ["Andrae", "Edler"]


# ─── numeric extraction ────────────────────────


def test_numeric_bracket_single() -> None:
    doc = _doc("Studies show [12] this is true.")
    assert any(
        c.kind == CitationLocationKind.numeric and c.cited_number == 12
        for c in doc.inline_citations
    )


def test_numeric_bracket_range_expands() -> None:
    doc = _doc("Earlier work [3-5] established this.")
    nums = sorted(
        c.cited_number for c in doc.inline_citations
        if c.kind == CitationLocationKind.numeric
    )
    assert nums == [3, 4, 5]


def test_numeric_bracket_list_expands() -> None:
    doc = _doc("See [12, 14, 16].")
    nums = sorted(
        c.cited_number for c in doc.inline_citations
        if c.kind == CitationLocationKind.numeric
    )
    assert nums == [12, 14, 16]


def test_no_false_numeric_in_text() -> None:
    """An ordinary parenthesised number like '(2020)' should NOT be
    picked up as a numeric citation — that ambiguity is what kind=
    parenthetical is for."""
    doc = _doc("This was published in 2020. The page number is (47).")
    nums = [
        c for c in doc.inline_citations
        if c.kind == CitationLocationKind.numeric
    ]
    # We allow the bracketed (47) to NOT match because there's no author
    # context. The current implementation does match (47) — verify the
    # behaviour is intentional via narrative-form absence.
    # The looser numeric_paren is intentionally not enabled by default
    # to avoid false positives like this.
    assert all(c.cited_number != 47 for c in nums) or len(nums) == 0


# ─── footnotes ─────────────────────────────────


def test_markdown_footnote_body_extracted() -> None:
    text = (
        "See note[^1] for details.\n\n"
        '[^1]: Smith, John. "On the mechanism." Nature, 2020, pp. 12-19.\n'
    )
    doc = _doc(text)
    assert len(doc.footnotes) == 1
    f = doc.footnotes[0]
    assert f.footnote_id == "1"
    assert f.is_full_citation
    assert not f.is_ibid


def test_ibid_footnote_classified() -> None:
    text = (
        "Earlier point[^1]; same source again[^2].\n\n"
        "[^1]: Smith 2020.\n"
        "[^2]: Ibid., p. 47.\n"
    )
    doc = _doc(text)
    by_id = {f.footnote_id: f for f in doc.footnotes}
    assert by_id["2"].is_ibid is True
    assert by_id["1"].is_ibid is False


def test_op_cit_classified() -> None:
    text = (
        "[^1]: Smith, J. 2020. Mechanism. Nature.\n"
        "[^2]: Smith, op. cit., p. 12.\n"
    )
    doc = _doc(text)
    by_id = {f.footnote_id: f for f in doc.footnotes}
    assert by_id["2"].is_op_cit is True


def test_footnote_pinpoint_extracted() -> None:
    text = "[^1]: Smith 2020, pp. 47-49.\n"
    doc = _doc(text)
    assert doc.footnotes[0].pinpoint is not None
    assert "47" in doc.footnotes[0].pinpoint


# ─── bibliography isolation ────────────────────


def test_bibliography_split_blank_line() -> None:
    text = (
        "Body.\n\n"
        "# References\n\n"
        "Smith, J. (2020). On the mechanism. Nature, 580(1), 12-19.\n\n"
        "Lee, K. and Park, S. (2019). Forecasts revisited. JX, 12(3), 45-60.\n"
    )
    doc = _doc(text)
    assert len(doc.bibliography_entries) == 2


def test_bibliography_split_single_newline_lines() -> None:
    """A bibliography where entries are separated by single newlines
    only (no blank line between entries) — common in academic markdown.
    Each line starts with a capitalised surname."""
    text = (
        "Body.\n\n"
        "# References\n\n"
        "Smith, J. (2020). On the mechanism. Nature, 580(1), 12-19.\n"
        "Lee, K. and Park, S. (2019). Forecasts revisited. JX, 12(3), 45-60.\n"
    )
    doc = _doc(text)
    assert len(doc.bibliography_entries) == 2


def test_bibliography_numbered_split() -> None:
    text = (
        "Body.\n\n"
        "# References\n\n"
        "[1] Smith, J. X. Nature, 2020.\n"
        "[2] Lee, K. Y. JX, 2019.\n"
    )
    doc = _doc(text)
    assert len(doc.bibliography_entries) == 2
    # Numbering markers stripped from cleaned entries.
    assert all(not e.startswith("[") for e in doc.bibliography_entries)


def test_no_bibliography_returns_empty() -> None:
    doc = _doc("Some prose without a references section (Smith, 2020).")
    assert doc.bibliography_entries == []


def test_bibliography_heading_not_in_body() -> None:
    """The body passed to inline-citation extraction should NOT include
    the bibliography section — citations there shouldn't be double-
    counted as inline."""
    text = (
        "Body cite (Smith, 2020).\n\n"
        "# References\n\n"
        "Smith, J. (2020). X. Nature.\n"
    )
    doc = _doc(text)
    # Only one inline citation — the body one.
    inline_paren = [
        c for c in doc.inline_citations
        if c.kind == CitationLocationKind.parenthetical
    ]
    assert len(inline_paren) == 1


# ─── persistence ───────────────────────────────


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    text = "Cited (Smith, 2020).\n\n# References\n\nSmith. X. 2020.\n"
    doc = _doc(text)
    (tmp_path / ".lattice").mkdir()
    path = save_document_citations(tmp_path, doc)
    assert path.exists()
    loaded = load_document_citations(tmp_path)
    assert loaded is not None
    assert loaded.detected_system == doc.detected_system
    assert len(loaded.inline_citations) == len(doc.inline_citations)


def test_load_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_document_citations(tmp_path) is None


# ─── counts summary ────────────────────────────


def test_counts_summary_populated() -> None:
    text = (
        "Cite (Smith, 2020) and Lee (2019) and [3].\n\n"
        "[^1]: Smith 2020.\n\n"
        "# References\n\n"
        "Smith. X. 2020.\nLee. Y. 2019.\n"
    )
    doc = _doc(text)
    assert doc.counts["inline_total"] >= 3
    assert doc.counts["bibliography_entries"] == 2
    assert doc.counts["footnotes_total"] == 1
