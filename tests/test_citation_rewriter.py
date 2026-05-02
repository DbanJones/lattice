"""Tests for the citation rewriter — Phase E (the killer feature).

End-to-end: scan → match → restyle. Verifies that an APA-style
document round-trips cleanly into Vancouver, IEEE, Chicago, etc.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lattice.graph.models import (
    Citation, Source, SourceMetadata, SourceType,
)
from lattice.references.matcher import match_citations
from lattice.references.rewriter import restyle_document
from lattice.references.scanner import scan_document


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _src(source_id: str, **kw) -> Source:
    base = {"title": "X", "year": 2020, "authors": ["Smith, J."]}
    base.update(kw)
    return Source(
        source_id=source_id,
        type=SourceType.primary_paper,
        citation=Citation(**base),
        metadata=SourceMetadata(
            date_added=_now(), file_path=f"refs/{source_id}.pdf",
            hash="sha256:abc",
        ),
    )


def _scan_match(text: str, sources: list[Source]):
    doc = scan_document(text, project_name="t", document_path="paper.md")
    match_citations(doc, sources)
    return doc


# ─── author-date → numeric ──────────────────────


def test_author_date_to_vancouver_swaps_inline_form() -> None:
    sources = [
        _src("smith_2020", authors=["Smith, J."], year=2020,
             title="On the mechanism"),
        _src("lee_2019", authors=["Lee, K."], year=2019,
             title="Forecasts revisited"),
    ]
    text = (
        "Climate forecasts diverge (Smith, 2020). Other work (Lee, 2019).\n\n"
        "# References\n\n"
        "Smith, J. (2020). On the mechanism. Nature.\n"
        "Lee, K. (2019). Forecasts revisited. JX.\n"
    )
    doc = _scan_match(text, sources)
    result = restyle_document(text, doc, sources, style="vancouver")
    # Inline citations replaced with numeric.
    assert "(Smith, 2020)" not in result.document
    assert "[1]" in result.document
    assert "[2]" in result.document
    assert result.inline_replaced >= 2


def test_multi_source_paren_becomes_numeric_list() -> None:
    sources = [
        _src("smith_2020", authors=["Smith, J."], year=2020,
             title="Mechanism"),
        _src("lee_2019", authors=["Lee, K."], year=2019,
             title="Forecasts"),
    ]
    text = (
        "See (Smith, 2020; Lee, 2019).\n\n"
        "# References\n\n"
        "Smith, J. (2020). Mechanism.\nLee, K. (2019). Forecasts.\n"
    )
    doc = _scan_match(text, sources)
    result = restyle_document(text, doc, sources, style="ieee")
    # Both numbers in one bracket.
    assert "[1, 2]" in result.document or "[1,2]" in result.document


def test_pinpoint_preserved_in_numeric_form() -> None:
    sources = [_src("smith_2020", authors=["Smith, J."], year=2020,
                    title="Mechanism")]
    text = (
        "Quote (Smith, 2020, p. 47).\n\n"
        "# References\n\nSmith, J. (2020). Mechanism.\n"
    )
    doc = _scan_match(text, sources)
    result = restyle_document(text, doc, sources, style="vancouver")
    assert "p. 47" in result.document


# ─── numeric → author-date ──────────────────────


def test_numeric_to_apa_swaps_inline_form() -> None:
    sources = [
        _src("smith_2020", authors=["Smith, J."], year=2020,
             title="Mechanism"),
        _src("lee_2019", authors=["Lee, K."], year=2019,
             title="Forecasts"),
    ]
    text = (
        "Studies confirm [1] and [2] this.\n\n"
        "# References\n\n"
        "[1] Smith, J. Mechanism. Nature, 2020.\n"
        "[2] Lee, K. Forecasts. JX, 2019.\n"
    )
    doc = _scan_match(text, sources)
    result = restyle_document(text, doc, sources, style="apa")
    # Numeric brackets gone, author-date in.
    assert "[1]" not in result.document
    assert "[2]" not in result.document
    # Some form of the author surname appears in inline.
    assert "Smith" in result.document
    assert "Lee" in result.document


# ─── narrative form ─────────────────────────────


def test_narrative_form_preserved_across_styles() -> None:
    """`Smith (2020) argues` should survive a restyle as a narrative,
    not flip to parenthetical."""
    sources = [_src("smith_2020", authors=["Smith, J."], year=2020,
                    title="Mechanism")]
    text = (
        "Smith (2020) argues for the mechanism.\n\n"
        "# References\n\nSmith, J. (2020). Mechanism. Nature.\n"
    )
    doc = _scan_match(text, sources)
    result = restyle_document(text, doc, sources, style="chicago_author_date")
    # Narrative form: "Smith (2020) argues" — should still have year in
    # parens immediately after the surname, in document order.
    smith_pos = result.document.find("Smith")
    paren_pos = result.document.find("(", smith_pos)
    assert smith_pos < paren_pos < smith_pos + 30


# ─── bibliography ───────────────────────────────


def test_bibliography_replaced_in_target_style() -> None:
    sources = [
        _src("smith_2020", authors=["Smith, John"], year=2020,
             title="Mechanism", container="Nature"),
    ]
    text = (
        "(Smith, 2020) is cited.\n\n"
        "# References\n\nSmith, J. (2020). Mechanism. Nature.\n"
    )
    doc = _scan_match(text, sources)
    result = restyle_document(text, doc, sources, style="vancouver")
    assert result.bibliography_emitted == 1
    # Vancouver bibliography starts with [1]
    assert "[1]" in result.document.split("# References")[1]


def test_alphabetical_bibliography_for_author_date() -> None:
    sources = [
        _src("z_paper", authors=["Zeta, Z."], year=2020, title="Z paper"),
        _src("a_paper", authors=["Alpha, A."], year=2020, title="A paper"),
    ]
    text = (
        "See (Zeta, 2020) and (Alpha, 2020).\n\n"
        "# References\n\n"
        "Zeta, Z. (2020). Z paper.\n"
        "Alpha, A. (2020). A paper.\n"
    )
    doc = _scan_match(text, sources)
    result = restyle_document(text, doc, sources, style="apa")
    refs_section = result.document.split("# References")[1]
    alpha_pos = refs_section.find("Alpha")
    zeta_pos = refs_section.find("Zeta")
    assert alpha_pos != -1 and zeta_pos != -1
    assert alpha_pos < zeta_pos  # alphabetical


def test_citation_order_bibliography_for_numeric() -> None:
    """Vancouver / IEEE bibliographies are in CITATION ORDER (the
    order sources are first cited), not alphabetical."""
    sources = [
        _src("z_paper", authors=["Zeta, Z."], year=2020, title="Z paper"),
        _src("a_paper", authors=["Alpha, A."], year=2020, title="A paper"),
    ]
    text = (
        "First (Zeta, 2020), then (Alpha, 2020).\n\n"
        "# References\n\n"
        "Zeta, Z. (2020). Z paper.\n"
        "Alpha, A. (2020). A paper.\n"
    )
    doc = _scan_match(text, sources)
    result = restyle_document(text, doc, sources, style="vancouver")
    refs_section = result.document.split("# References")[1]
    z_pos = refs_section.find("Zeta")
    a_pos = refs_section.find("Alpha")
    # Zeta cited first, so it's [1]; Alpha is [2].
    assert z_pos < a_pos


# ─── unresolved citations ───────────────────────


def test_unresolved_citations_preserved_unchanged() -> None:
    """A citation whose source_id never resolved should be left as-is
    rather than corrupted."""
    sources = [_src("smith_2020", authors=["Smith, J."], year=2020,
                    title="Mechanism")]
    text = (
        "Cited (Smith, 2020) and (Unknown, 1999).\n\n"
        "# References\n\nSmith, J. (2020). Mechanism.\n"
    )
    doc = _scan_match(text, sources)
    result = restyle_document(text, doc, sources, style="vancouver")
    # Unknown citation: source_id is None, so the span stays untouched.
    assert "(Unknown, 1999)" in result.document
    assert result.inline_unresolved >= 1


# ─── unsupported style ──────────────────────────


def test_unknown_style_raises() -> None:
    sources = [_src("smith_2020", authors=["Smith, J."], year=2020,
                    title="X")]
    doc = _scan_match("(Smith, 2020).", sources)
    with pytest.raises(ValueError, match="Unknown style"):
        restyle_document("(Smith, 2020).", doc, sources, style="made_up")


# ─── end-to-end round trip ──────────────────────


def test_apa_to_vancouver_to_apa_preserves_meaning() -> None:
    """Round trip: APA → Vancouver → APA. The final result should
    have the same set of cited sources (cited counts may differ in
    exact form because numeric→author-date can't perfectly undo)."""
    sources = [
        _src("smith_2020", authors=["Smith, J."], year=2020, title="X"),
        _src("lee_2019", authors=["Lee, K."], year=2019, title="Y"),
    ]
    apa_text = (
        "Cite (Smith, 2020). Also (Lee, 2019).\n\n"
        "# References\n\n"
        "Smith, J. (2020). X.\n"
        "Lee, K. (2019). Y.\n"
    )
    apa_doc = _scan_match(apa_text, sources)
    vancouver = restyle_document(apa_text, apa_doc, sources, style="vancouver")
    # Re-scan the restyled doc and round-trip back.
    van_doc = _scan_match(vancouver.document, sources)
    apa_again = restyle_document(
        vancouver.document, van_doc, sources, style="apa",
    )
    # Both source ids appear in the rewritten bibliography.
    assert "Smith" in apa_again.document
    assert "Lee" in apa_again.document
