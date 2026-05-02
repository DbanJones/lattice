"""Tests for the citation matcher — Phase B."""
from __future__ import annotations

from datetime import datetime, timezone

from lattice.graph.models import (
    Citation, CitationLocationKind, DocumentCitations,
    FootnoteCitation, InlineCitation, Source, SourceMetadata, SourceType,
)
from lattice.references.matcher import match_citations


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _src(source_id: str, *, authors: list[str], year: int, title: str = "X") -> Source:
    return Source(
        source_id=source_id,
        type=SourceType.primary_paper,
        citation=Citation(authors=authors, year=year, title=title),
        passages=[],
        metadata=SourceMetadata(
            date_added=_now(), file_path=f"refs/{source_id}.pdf",
            hash="sha256:abc",
        ),
    )


def _doc(*, inline=None, footnotes=None) -> DocumentCitations:
    return DocumentCitations(
        project_name="t",
        document_path="paper.md",
        scanned_at=_now(),
        inline_citations=list(inline or []),
        footnotes=list(footnotes or []),
    )


def _ic(**kw) -> InlineCitation:
    base = dict(
        citation_id="ic.1",
        raw_text="(Smith, 2020)",
        kind=CitationLocationKind.parenthetical,
    )
    base.update(kw)
    return InlineCitation(**base)


# ─── exact author + year ─────────────────────────


def test_exact_match_by_surname_and_year() -> None:
    src = _src("smith_2020", authors=["Smith, J."], year=2020)
    doc = _doc(inline=[_ic(cited_authors=["Smith"], cited_year=2020)])
    match_citations(doc, [src])
    assert doc.inline_citations[0].source_id == "smith_2020"
    assert doc.inline_citations[0].match_confidence >= 0.9


def test_unknown_surname_left_unresolved() -> None:
    src = _src("smith_2020", authors=["Smith, J."], year=2020)
    doc = _doc(inline=[_ic(cited_authors=["Bogus"], cited_year=2020)])
    match_citations(doc, [src])
    assert doc.inline_citations[0].source_id is None
    assert "no_match" in (doc.inline_citations[0].unresolved_reason or "")


def test_year_off_by_one_yields_partial_match() -> None:
    """`forthcoming` papers often shift year by 1 between submission
    and publication. The matcher should pick up the near-match with
    reduced confidence."""
    src = _src("smith_2020", authors=["Smith, J."], year=2020)
    doc = _doc(inline=[_ic(cited_authors=["Smith"], cited_year=2021)])
    match_citations(doc, [src])
    c = doc.inline_citations[0]
    assert c.source_id == "smith_2020"
    assert c.match_confidence < 0.9


def test_two_sources_same_year_same_surname_disambiguates_by_secondary() -> None:
    a = _src("smith_jones_2020", authors=["Smith, J.", "Jones, K."], year=2020)
    b = _src("smith_lee_2020", authors=["Smith, J.", "Lee, M."], year=2020)
    doc = _doc(inline=[_ic(cited_authors=["Smith", "Lee"], cited_year=2020)])
    match_citations(doc, [a, b])
    assert doc.inline_citations[0].source_id == "smith_lee_2020"


def test_ambiguous_when_only_primary_known() -> None:
    a = _src("smith_jones_2020", authors=["Smith, J.", "Jones, K."], year=2020)
    b = _src("smith_lee_2020", authors=["Smith, J.", "Lee, M."], year=2020)
    doc = _doc(inline=[_ic(cited_authors=["Smith"], cited_year=2020)])
    match_citations(doc, [a, b])
    assert doc.inline_citations[0].source_id is None
    assert "ambiguous" in (doc.inline_citations[0].unresolved_reason or "")


def test_diacritics_normalised() -> None:
    """`Coroamă` and `coroama` should match the same source."""
    src = _src("coroama_2021", authors=["Coroamă, V."], year=2021)
    doc = _doc(inline=[_ic(cited_authors=["Coroama"], cited_year=2021)])
    match_citations(doc, [src])
    assert doc.inline_citations[0].source_id == "coroama_2021"


def test_no_year_with_unique_author_matches_with_low_confidence() -> None:
    src = _src("smith_2020", authors=["Smith, J."], year=2020)
    doc = _doc(inline=[_ic(cited_authors=["Smith"], cited_year=None)])
    match_citations(doc, [src])
    c = doc.inline_citations[0]
    assert c.source_id == "smith_2020"
    assert c.match_confidence == 0.5


def test_no_year_with_ambiguous_author_unresolved() -> None:
    a = _src("smith_2020", authors=["Smith, J."], year=2020)
    b = _src("smith_2019", authors=["Smith, K."], year=2019)
    doc = _doc(inline=[_ic(cited_authors=["Smith"], cited_year=None)])
    match_citations(doc, [a, b])
    assert doc.inline_citations[0].source_id is None


# ─── numeric matching ───────────────────────────


def test_numeric_matches_position() -> None:
    sources = [
        _src("a", authors=["A, X."], year=2020),
        _src("b", authors=["B, Y."], year=2019),
        _src("c", authors=["C, Z."], year=2018),
    ]
    doc = _doc(inline=[
        _ic(citation_id="ic.1", raw_text="[2]",
            kind=CitationLocationKind.numeric, cited_number=2),
    ])
    match_citations(doc, sources)
    assert doc.inline_citations[0].source_id == "b"


def test_numeric_out_of_range_unresolved() -> None:
    sources = [_src("a", authors=["A"], year=2020)]
    doc = _doc(inline=[
        _ic(citation_id="ic.1", raw_text="[5]",
            kind=CitationLocationKind.numeric, cited_number=5),
    ])
    match_citations(doc, sources)
    assert doc.inline_citations[0].source_id is None
    assert "out_of_range" in (
        doc.inline_citations[0].unresolved_reason or ""
    )


# ─── footnote matching ──────────────────────────


def test_full_citation_footnote_matches() -> None:
    src = _src("smith_2020", authors=["Smith, J."], year=2020)
    fn = FootnoteCitation(
        footnote_id="1",
        raw_text='Smith, J. "On the Mechanism." Nature 2020.',
        is_full_citation=True,
    )
    doc = _doc(footnotes=[fn])
    match_citations(doc, [src])
    f = doc.footnotes[0]
    assert f.source_id == "smith_2020"
    assert f.resolves_to_source_id == "smith_2020"


def test_ibid_inherits_previous_full_citation() -> None:
    src = _src("smith_2020", authors=["Smith, J."], year=2020)
    full = FootnoteCitation(
        footnote_id="1",
        raw_text='Smith, J. "On the Mechanism." Nature 2020.',
        is_full_citation=True,
    )
    ibid = FootnoteCitation(
        footnote_id="2",
        raw_text="Ibid., p. 47.",
        is_ibid=True,
    )
    doc = _doc(footnotes=[full, ibid])
    match_citations(doc, [src])
    assert doc.footnotes[0].resolves_to_source_id == "smith_2020"
    assert doc.footnotes[1].resolves_to_source_id == "smith_2020"
    # ibid shouldn't OWN the citation.
    assert doc.footnotes[1].source_id is None


def test_ibid_with_no_preceding_citation_flagged() -> None:
    """An Ibid. footnote that's the FIRST footnote in the document
    has nothing to inherit from — should surface unresolved."""
    src = _src("smith_2020", authors=["Smith, J."], year=2020)
    ibid = FootnoteCitation(
        footnote_id="1", raw_text="Ibid., p. 47.", is_ibid=True,
    )
    doc = _doc(footnotes=[ibid])
    match_citations(doc, [src])  # sources exist; just no prior full citation
    assert doc.footnotes[0].resolves_to_source_id is None
    assert "no_preceding" in (doc.footnotes[0].unresolved_reason or "")


def test_op_cit_resolves_to_previous_surname_match() -> None:
    src = _src("smith_2020", authors=["Smith, J."], year=2020)
    full = FootnoteCitation(
        footnote_id="1", raw_text='Smith, J. "X." Nature 2020.',
        is_full_citation=True,
    )
    opcit = FootnoteCitation(
        footnote_id="2",
        raw_text="Smith, op. cit., p. 12.",
        is_op_cit=True,
    )
    doc = _doc(footnotes=[full, opcit])
    match_citations(doc, [src])
    assert doc.footnotes[1].resolves_to_source_id == "smith_2020"


# ─── empty / edge cases ─────────────────────────


def test_no_sources_marks_everything_unresolved() -> None:
    doc = _doc(inline=[_ic(cited_authors=["Smith"], cited_year=2020)])
    match_citations(doc, [])
    assert doc.inline_citations[0].source_id is None
    assert doc.inline_citations[0].unresolved_reason == "no_sources_in_store"


def test_counts_summary_updated() -> None:
    src = _src("smith_2020", authors=["Smith, J."], year=2020)
    doc = _doc(inline=[
        _ic(cited_authors=["Smith"], cited_year=2020),
        _ic(cited_authors=["Bogus"], cited_year=1999),
    ])
    match_citations(doc, [src])
    assert doc.counts["inline_matched"] == 1
    assert doc.counts["inline_unmatched"] == 1


def test_idempotent_re_run() -> None:
    """Calling match_citations twice on the same doc shouldn't change
    anything beyond the first call."""
    src = _src("smith_2020", authors=["Smith, J."], year=2020)
    doc = _doc(inline=[_ic(cited_authors=["Smith"], cited_year=2020)])
    match_citations(doc, [src])
    first_sid = doc.inline_citations[0].source_id
    match_citations(doc, [src])
    assert doc.inline_citations[0].source_id == first_sid
