"""Tests for reference export to BibTeX / RIS / CSL-JSON."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    Citation, Source, SourceMetadata, SourceType,
)
from lattice.references.exporters import (
    export_bibtex_text,
    export_csl_json_text,
    export_references,
    export_ris_text,
    supported_export_formats,
    write_bibtex,
    write_csl_json,
    write_ris,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _src(source_id: str, **kw) -> Source:
    base = {
        "title": "On the Mechanism", "year": 2020,
        "authors": ["Smith, John A.", "Jones, Kira"],
        "container": "Nature", "volume": "580", "issue": "1",
        "pages": "12-19", "doi": "10.1234/x",
    }
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


# ─── BibTeX ─────────────────────────────────────


def test_bibtex_basic_shape() -> None:
    s = _src("smith_2020")
    out = export_bibtex_text([s])
    assert "@article{smith_2020," in out
    assert "author = {Smith, John A. and Jones, Kira}" in out
    assert "year = {2020}" in out
    assert "title = {On the Mechanism}" in out
    assert "journal = {Nature}" in out
    assert "volume = {580}" in out
    assert "number = {1}" in out
    assert "pages = {12--19}" in out  # BibTeX en-dash
    assert "doi = {10.1234/x}" in out
    assert out.endswith("}\n")


def test_bibtex_skips_empty_fields() -> None:
    s = _src("min", year=None, container=None, volume=None,
             issue=None, pages=None, doi=None, url=None)
    out = export_bibtex_text([s])
    assert "year" not in out
    assert "journal" not in out
    assert "doi" not in out


def test_bibtex_escapes_special_characters() -> None:
    s = _src("special", title="Cost & Benefit: 100% accuracy_check")
    out = export_bibtex_text([s])
    # & % _ all need escaping inside braces.
    assert "\\&" in out
    assert "\\%" in out
    assert "\\_" in out


def test_bibtex_pages_converts_hyphen_to_endash() -> None:
    s = _src("p", pages="100-200")
    out = export_bibtex_text([s])
    assert "pages = {100--200}" in out


def test_bibtex_two_entries_separated() -> None:
    a = _src("a", title="A")
    b = _src("b", title="B")
    out = export_bibtex_text([a, b])
    assert "@article{a," in out
    assert "@article{b," in out
    # Two entries, with a blank line between.
    assert "}\n\n@" in out


def test_bibtex_safe_key_strips_unsafe_chars() -> None:
    s = _src("smith.2020 (rev)", title="X")
    out = export_bibtex_text([s])
    # The dot, space, and parens get collapsed to underscores.
    assert "@article{smith_2020_rev," in out


def test_bibtex_report_type_uses_techreport() -> None:
    s = _src("rep", title="Report")
    s.type = SourceType.report
    out = export_bibtex_text([s])
    assert "@techreport{rep," in out


def test_write_bibtex_roundtrip(tmp_path: Path) -> None:
    s = _src("x")
    path = write_bibtex([s], tmp_path / "refs.bib")
    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert "@article{x," in body


# ─── RIS ────────────────────────────────────────


def test_ris_basic_shape() -> None:
    s = _src("smith_2020")
    out = export_ris_text([s])
    assert "TY  - JOUR" in out
    assert "AU  - Smith, John A." in out
    assert "AU  - Jones, Kira" in out
    assert "PY  - 2020" in out
    assert "TI  - On the Mechanism" in out
    assert "T2  - Nature" in out  # secondary title (journal)
    assert "VL  - 580" in out
    assert "IS  - 1" in out
    assert "DO  - 10.1234/x" in out
    assert "ID  - smith_2020" in out
    assert "ER  -" in out


def test_ris_pages_split_into_sp_ep() -> None:
    s = _src("s", pages="100-200")
    out = export_ris_text([s])
    assert "SP  - 100" in out
    assert "EP  - 200" in out


def test_ris_pages_endash_split() -> None:
    s = _src("s", pages="100–200")  # en-dash
    out = export_ris_text([s])
    assert "SP  - 100" in out
    assert "EP  - 200" in out


def test_ris_minimal_fields_when_metadata_sparse() -> None:
    s = _src("min", year=None, container=None, doi=None, pages=None)
    out = export_ris_text([s])
    assert "TY  - JOUR" in out
    assert "ER  -" in out
    assert "PY  -" not in out
    assert "DO  -" not in out


def test_ris_each_entry_terminated_with_er() -> None:
    a = _src("a", title="A")
    b = _src("b", title="B")
    out = export_ris_text([a, b])
    assert out.count("ER  -") == 2


def test_write_ris(tmp_path: Path) -> None:
    s = _src("x")
    path = write_ris([s], tmp_path / "refs.ris")
    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert "TY  - JOUR" in body


# ─── CSL-JSON ───────────────────────────────────


def test_csl_json_basic_shape() -> None:
    s = _src("smith_2020")
    out = export_csl_json_text([s])
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 1
    e = data[0]
    assert e["id"] == "smith_2020"
    assert e["type"] == "article-journal"
    assert e["title"] == "On the Mechanism"
    assert e["issued"] == {"date-parts": [[2020]]}
    assert e["DOI"] == "10.1234/x"
    assert e["author"] == [
        {"family": "Smith", "given": "John A."},
        {"family": "Jones", "given": "Kira"},
    ]


def test_csl_json_author_without_comma() -> None:
    s = _src("x", authors=["John Smith"])
    data = json.loads(export_csl_json_text([s]))
    assert data[0]["author"] == [{"given": "John", "family": "Smith"}]


def test_csl_json_single_word_author_uses_literal() -> None:
    s = _src("x", authors=["Anonymous"])
    data = json.loads(export_csl_json_text([s]))
    assert data[0]["author"] == [{"literal": "Anonymous"}]


def test_csl_json_round_trips_through_json() -> None:
    s = _src("smith_2020")
    out = export_csl_json_text([s])
    # Must be valid JSON and re-emit the same shape.
    re_emitted = json.dumps(json.loads(out), indent=2, ensure_ascii=False)
    assert re_emitted.strip() == out.strip()


def test_write_csl_json(tmp_path: Path) -> None:
    s = _src("x")
    path = write_csl_json([s], tmp_path / "refs.json")
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[0]["id"] == "x"


# ─── format dispatch ────────────────────────────


def test_export_references_dispatches_to_bib() -> None:
    s = _src("x")
    text, suffix = export_references([s], "bib")
    assert "@article{x," in text
    assert suffix == "bib"


def test_export_references_dispatches_to_ris() -> None:
    s = _src("x")
    text, suffix = export_references([s], "ris")
    assert "TY  - JOUR" in text
    assert suffix == "ris"


def test_export_references_dispatches_to_csl_json() -> None:
    s = _src("x")
    text, suffix = export_references([s], "csl-json")
    assert json.loads(text)[0]["id"] == "x"
    assert suffix == "json"


def test_export_references_zotero_alias_uses_csl_json() -> None:
    s = _src("x")
    text, _ = export_references([s], "zotero")
    assert json.loads(text)[0]["type"] == "article-journal"


def test_export_references_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match="Unknown export format"):
        export_references([_src("x")], "endnote_xml")


def test_supported_export_formats_lists_all() -> None:
    assert "bib" in supported_export_formats()
    assert "ris" in supported_export_formats()
    assert "csl-json" in supported_export_formats()


# ─── empty case ─────────────────────────────────


def test_empty_source_list_produces_valid_output() -> None:
    """An empty bibliography should still produce a valid file (just
    empty), not crash."""
    assert export_bibtex_text([]).strip() == ""
    assert export_ris_text([]).strip() == ""
    assert json.loads(export_csl_json_text([])) == []
