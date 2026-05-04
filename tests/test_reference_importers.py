"""Tests for reference import (Zotero CSL-JSON / BibTeX / RIS)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from lattice.graph.models import (
    Citation, Source, SourceMetadata, SourceType,
)
from lattice.references.importers import (
    import_references,
    import_references_from_file,
    merge_into_store,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── format detection ───────────────────────────


def test_detect_csl_json_from_array() -> None:
    text = '[{"id": "x", "title": "T", "type": "article-journal"}]'
    report = import_references(text)
    assert report.detected_format == "csl-json"
    assert len(report.sources) == 1


def test_detect_bibtex_from_at_sign() -> None:
    text = "@article{x, title = {T}, year = {2020}}"
    report = import_references(text)
    assert report.detected_format == "bib"
    assert len(report.sources) == 1


def test_detect_ris_from_tag_line() -> None:
    text = "TY  - JOUR\nTI  - The Title\nPY  - 2020\nER  - \n"
    report = import_references(text)
    assert report.detected_format == "ris"
    assert len(report.sources) == 1


def test_unknown_format_returns_warning() -> None:
    report = import_references("just some prose")
    assert report.detected_format == "unknown"
    assert report.warnings


# ─── CSL-JSON ───────────────────────────────────


def test_csl_json_basic_entry_imports() -> None:
    text = json.dumps([{
        "id": "smith_2020",
        "type": "article-journal",
        "title": "On the Mechanism",
        "author": [
            {"family": "Smith", "given": "John A."},
            {"family": "Jones", "given": "Kira"},
        ],
        "issued": {"date-parts": [[2020]]},
        "container-title": "Nature",
        "volume": "580",
        "issue": "1",
        "page": "12-19",
        "DOI": "10.1234/x",
    }])
    report = import_references(text, format="csl-json")
    assert len(report.sources) == 1
    s = report.sources[0]
    assert s.source_id == "smith_2020"
    assert s.type == SourceType.primary_paper
    assert s.citation.authors == ["Smith, John A.", "Jones, Kira"]
    assert s.citation.year == 2020
    assert s.citation.container == "Nature"
    assert s.citation.doi == "10.1234/x"


def test_csl_json_literal_author_handled() -> None:
    text = json.dumps([{
        "id": "anon",
        "type": "report",
        "title": "WHO Report 2024",
        "author": [{"literal": "World Health Organization"}],
        "issued": {"date-parts": [[2024]]},
    }])
    report = import_references(text)
    assert report.sources[0].citation.authors == ["World Health Organization"]


def test_csl_json_no_title_skipped() -> None:
    text = json.dumps([
        {"id": "x", "type": "article-journal"},  # missing title
        {"id": "y", "type": "article-journal", "title": "Real"},
    ])
    report = import_references(text)
    assert len(report.sources) == 1
    assert len(report.skipped) == 1


def test_csl_json_zotero_wrapped_items_handled() -> None:
    """Zotero's "Better CSL JSON" sometimes wraps entries in
    ``{"items": [...]}``."""
    text = json.dumps({
        "items": [
            {"id": "x", "type": "article-journal", "title": "T"},
        ]
    })
    report = import_references(text, format="csl-json")
    assert len(report.sources) == 1


def test_csl_json_round_trip_with_exporter() -> None:
    """Export → import should preserve the entry."""
    from lattice.references.exporters import export_csl_json_text
    src = Source(
        source_id="rt",
        type=SourceType.primary_paper,
        citation=Citation(
            authors=["Smith, John"], year=2020,
            title="Round Trip", container="Nature", doi="10.1234/x",
        ),
        metadata=SourceMetadata(
            date_added=_now(), file_path="x", hash="x",
        ),
    )
    exported = export_csl_json_text([src])
    report = import_references(exported, format="csl-json")
    assert len(report.sources) == 1
    re_imported = report.sources[0]
    assert re_imported.source_id == "rt"
    assert re_imported.citation.title == "Round Trip"
    assert re_imported.citation.doi == "10.1234/x"
    assert "Smith" in re_imported.citation.authors[0]


def test_csl_json_invalid_json_warned() -> None:
    report = import_references("[not valid", format="csl-json")
    assert not report.sources
    assert any("JSON parse error" in w for w in report.warnings)


# ─── BibTeX ─────────────────────────────────────


def test_bibtex_basic_article_imports() -> None:
    text = """\
@article{smith_2020,
  author = {Smith, John A. and Jones, Kira},
  year = {2020},
  title = {On the Mechanism},
  journal = {Nature},
  volume = {580},
  number = {1},
  pages = {12--19},
  doi = {10.1234/x}
}"""
    report = import_references(text)
    assert len(report.sources) == 1
    s = report.sources[0]
    assert s.source_id == "smith_2020"
    assert s.citation.authors == ["Smith, John A.", "Jones, Kira"]
    assert s.citation.year == 2020
    assert s.citation.title == "On the Mechanism"
    assert s.citation.container == "Nature"
    assert s.citation.volume == "580"
    assert s.citation.issue == "1"
    assert s.citation.pages == "12-19"  # normalised from "12--19"


def test_bibtex_quoted_values() -> None:
    text = '@article{x, title = "Quoted Title", year = "2020"}'
    report = import_references(text)
    assert report.sources[0].citation.title == "Quoted Title"
    assert report.sources[0].citation.year == 2020


def test_bibtex_inproceedings_uses_booktitle_as_container() -> None:
    text = """\
@inproceedings{x,
  title = {Conf Paper},
  booktitle = {Proceedings of XYZ},
  year = {2020}
}"""
    report = import_references(text)
    assert report.sources[0].citation.container == "Proceedings of XYZ"


def test_bibtex_techreport_maps_to_report_type() -> None:
    text = '@techreport{x, title = {Report}, year = {2020}}'
    report = import_references(text)
    assert report.sources[0].type == SourceType.report


def test_bibtex_unbalanced_braces_warned() -> None:
    text = "@article{x, title = {Unclosed"
    report = import_references(text, format="bib")
    assert not report.sources
    assert any("brace" in w.lower() for w in report.warnings)


def test_bibtex_handles_escaped_specials() -> None:
    text = r"@article{x, title = {Cost \& Benefit}, year = {2020}}"
    report = import_references(text)
    assert report.sources[0].citation.title == "Cost & Benefit"


def test_bibtex_multiple_entries() -> None:
    text = """\
@article{a, title = {A}, year = {2020}}

@article{b, title = {B}, year = {2021}}
"""
    report = import_references(text)
    assert len(report.sources) == 2
    assert {s.source_id for s in report.sources} == {"a", "b"}


def test_bibtex_round_trip_with_exporter() -> None:
    from lattice.references.exporters import export_bibtex_text
    src = Source(
        source_id="rt_2020",
        type=SourceType.primary_paper,
        citation=Citation(
            authors=["Smith, John A."], year=2020,
            title="Round Trip", container="Nature",
            volume="1", pages="10-20", doi="10.1234/x",
        ),
        metadata=SourceMetadata(
            date_added=_now(), file_path="x", hash="x",
        ),
    )
    exported = export_bibtex_text([src])
    report = import_references(exported, format="bib")
    assert len(report.sources) == 1
    re_imported = report.sources[0]
    assert re_imported.source_id == "rt_2020"
    assert re_imported.citation.title == "Round Trip"
    assert re_imported.citation.year == 2020


# ─── RIS ────────────────────────────────────────


def test_ris_basic_record_imports() -> None:
    text = """\
TY  - JOUR
AU  - Smith, John A.
AU  - Jones, Kira
PY  - 2020
TI  - On the Mechanism
JO  - Nature
VL  - 580
IS  - 1
SP  - 12
EP  - 19
DO  - 10.1234/x
ID  - smith_2020
ER  -
"""
    report = import_references(text)
    assert len(report.sources) == 1
    s = report.sources[0]
    assert s.source_id == "smith_2020"
    assert s.citation.authors == ["Smith, John A.", "Jones, Kira"]
    assert s.citation.year == 2020
    assert s.citation.container == "Nature"
    assert s.citation.pages == "12-19"
    assert s.citation.doi == "10.1234/x"


def test_ris_multiple_records() -> None:
    text = """\
TY  - JOUR
TI  - First
PY  - 2020
ID  - a
ER  -

TY  - JOUR
TI  - Second
PY  - 2021
ID  - b
ER  -
"""
    report = import_references(text)
    assert len(report.sources) == 2


def test_ris_no_title_skipped() -> None:
    text = """\
TY  - JOUR
PY  - 2020
ER  -
"""
    report = import_references(text)
    assert len(report.sources) == 0
    assert len(report.skipped) >= 1


def test_ris_round_trip_with_exporter() -> None:
    from lattice.references.exporters import export_ris_text
    src = Source(
        source_id="rt_2020",
        type=SourceType.primary_paper,
        citation=Citation(
            authors=["Smith, John A."], year=2020,
            title="Round Trip", container="Nature", pages="10-20",
            doi="10.1234/x",
        ),
        metadata=SourceMetadata(
            date_added=_now(), file_path="x", hash="x",
        ),
    )
    exported = export_ris_text([src])
    report = import_references(exported, format="ris")
    assert len(report.sources) == 1
    re_imported = report.sources[0]
    assert re_imported.source_id == "rt_2020"
    assert re_imported.citation.year == 2020


# ─── from-file convenience ──────────────────────


def test_import_from_file_infers_bib_from_suffix(tmp_path: Path) -> None:
    path = tmp_path / "refs.bib"
    path.write_text("@article{x, title = {T}, year = {2020}}", encoding="utf-8")
    report = import_references_from_file(path)
    assert report.detected_format == "bib"
    assert len(report.sources) == 1


def test_import_from_file_infers_csl_json_from_suffix(tmp_path: Path) -> None:
    path = tmp_path / "refs.json"
    path.write_text(
        '[{"id": "x", "type": "article-journal", "title": "T"}]',
        encoding="utf-8",
    )
    report = import_references_from_file(path)
    assert report.detected_format == "csl-json"


def test_import_from_file_infers_ris_from_suffix(tmp_path: Path) -> None:
    path = tmp_path / "refs.ris"
    path.write_text("TY  - JOUR\nTI  - T\nER  - \n", encoding="utf-8")
    report = import_references_from_file(path)
    assert report.detected_format == "ris"


# ─── merge into store ───────────────────────────


def _src(source_id: str, *, doi: str | None = None,
         title: str = "T", year: int = 2020,
         authors: list[str] | None = None) -> Source:
    return Source(
        source_id=source_id,
        type=SourceType.primary_paper,
        citation=Citation(
            authors=authors or ["Smith, J."], year=year, title=title, doi=doi,
        ),
        metadata=SourceMetadata(
            date_added=_now(), file_path="x", hash="x",
        ),
    )


def test_merge_adds_new_sources_to_empty_store() -> None:
    incoming = [_src("a", title="A paper"), _src("b", title="B paper")]
    merged, decisions = merge_into_store(incoming, [])
    assert len(merged) == 2
    assert decisions == {"a": "added", "b": "added"}


def test_merge_dedups_by_doi() -> None:
    existing = [_src("smith_2020", doi="10.1234/x")]
    incoming = [_src("different_id", doi="10.1234/X")]  # case-insensitive
    merged, decisions = merge_into_store(incoming, existing)
    assert len(merged) == 1
    assert decisions["different_id"] == "duplicate"


def test_merge_dedups_by_content_hash_when_no_doi() -> None:
    """Same year, surname, title → same source, even with different ids."""
    existing = [_src(
        "smith_2020", title="On the Mechanism", year=2020,
        authors=["Smith, John A."],
    )]
    incoming = [_src(
        "completely_different_id", title="On the Mechanism", year=2020,
        authors=["Smith, John A."],
    )]
    merged, decisions = merge_into_store(incoming, existing)
    assert len(merged) == 1
    assert decisions["completely_different_id"] == "duplicate"


def test_merge_keeps_distinct_sources() -> None:
    existing = [_src("a", doi="10.1/x", title="A paper")]
    incoming = [_src("b", doi="10.2/y", title="B paper")]
    merged, decisions = merge_into_store(incoming, existing)
    assert len(merged) == 2
    assert decisions["b"] == "added"


def test_merge_handles_one_with_doi_one_without() -> None:
    """A source with a DOI and one without — different sources, both kept."""
    existing = [_src("a", doi="10.1/x", title="A")]
    incoming = [_src("b", doi=None, title="B")]
    merged, decisions = merge_into_store(incoming, existing)
    assert len(merged) == 2
