"""Tests for source indexers (M1, no LLM)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lattice.graph.models import PassageType, SourceType
from lattice.indexer.base import Indexer, SourceIndexer
from lattice.indexer.html import HTMLIndexer
from lattice.indexer.markdown import MarkdownIndexer
from lattice.indexer.pdf import PDFIndexer


# ─── Indexer.hash_file ────────────────────────────────

def test_hash_file_is_deterministic(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello world")
    h1 = Indexer.hash_file(p)
    h2 = Indexer.hash_file(p)
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_hash_file_changes_on_content_change(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello")
    h1 = Indexer.hash_file(p)
    p.write_bytes(b"goodbye")
    h2 = Indexer.hash_file(p)
    assert h1 != h2


def test_slugify_lowercases_and_separates(tmp_path: Path) -> None:
    assert Indexer.slugify("Koomey 2015") == "koomey_2015"
    assert Indexer.slugify("Andrae & Edler (2015)") == "andrae_edler_2015"


# ─── MarkdownIndexer ──────────────────────────────────

def test_markdown_indexer_extracts_paragraphs(tmp_path: Path) -> None:
    md = tmp_path / "notes.md"
    md.write_text(
        "# Thesis\n"
        "\n"
        "First paragraph.\n"
        "\n"
        "Second paragraph with more words.\n"
        "\n"
        "Figure 1: A diagram of the system.\n",
        encoding="utf-8",
    )
    source = MarkdownIndexer().index(md)
    assert source.source_id == "notes"
    assert source.citation.title == "Thesis"
    # 1 heading + 3 paragraphs
    assert len(source.passages) == 4
    # Figure caption classification
    fig = next(p for p in source.passages if p.text.startswith("Figure 1"))
    assert fig.type == PassageType.figure_caption


def test_markdown_passage_ids_are_stable(tmp_path: Path) -> None:
    md = tmp_path / "notes.md"
    md.write_text("# Title\n\npara one.\n\npara two.\n", encoding="utf-8")
    ids_a = [p.id for p in MarkdownIndexer().index(md).passages]
    ids_b = [p.id for p in MarkdownIndexer().index(md).passages]
    assert ids_a == ids_b
    # Line-based IDs: "# Title" starts at line 1
    assert ids_a[0] == "p.1.1"


# ─── PDFIndexer (mocked pypdf) ────────────────────────

class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    def __init__(self, pages: list[str]) -> None:
        self.pages = [_FakePage(t) for t in pages]
        self.metadata = {"/Title": "Test Paper", "/Author": "Jones, D.", "/CreationDate": "D:20260101000000"}


def test_pdf_indexer_extracts_paragraphs_per_page(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("lattice.indexer.pdf.PdfReader", lambda _p: _FakeReader([
        "Paragraph one on page one.\n\nParagraph two on page one.",
        "Figure 1: caption.\n\nParagraph on page two.",
    ]))
    fake_pdf = tmp_path / "test.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n")
    source = PDFIndexer().index(fake_pdf)
    assert len(source.passages) == 4
    assert source.passages[0].id == "p.1.1"
    assert source.passages[1].id == "p.1.2"
    assert source.passages[2].id == "p.2.1"
    assert source.passages[2].type == PassageType.figure_caption
    # Source ID from PDF author + year
    assert source.source_id == "jones_2026"
    assert source.citation.title == "Test Paper"


def test_pdf_indexer_uses_ocr_when_page_text_sparse(monkeypatch, tmp_path: Path) -> None:
    """Pages with sparse text should trigger OCR; OCR output should populate passages."""
    monkeypatch.setattr("lattice.indexer.pdf.PdfReader", lambda _p: _FakeReader(["", ""]))
    ocr_calls: list[int] = []

    def fake_ocr(pdf_path: Path, page_index: int) -> str:
        ocr_calls.append(page_index)
        return f"OCR paragraph one on page {page_index + 1}.\n\nAnother paragraph."

    monkeypatch.setattr("lattice.indexer.pdf._ocr_page", fake_ocr)
    fake_pdf = tmp_path / "scanned.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n")
    source = PDFIndexer().index(fake_pdf)
    assert ocr_calls == [0, 1]
    assert source.metadata.ocr_used is True
    assert len(source.passages) == 4  # 2 pages × 2 paragraphs
    assert "OCR paragraph" in source.passages[0].text


def test_pdf_indexer_skips_ocr_when_text_present(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "lattice.indexer.pdf.PdfReader",
        lambda _p: _FakeReader(["Plenty of extracted text here. " * 10]),
    )
    ocr_calls: list[int] = []
    monkeypatch.setattr(
        "lattice.indexer.pdf._ocr_page",
        lambda _path, idx: ocr_calls.append(idx) or "",
    )
    fake_pdf = tmp_path / "digital.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n")
    source = PDFIndexer().index(fake_pdf)
    assert ocr_calls == []
    assert source.metadata.ocr_used is False


def test_pdf_indexer_mixed_pages_marks_ocr_used(monkeypatch, tmp_path: Path) -> None:
    """Some pages have text, others need OCR. ocr_used should still be True."""
    monkeypatch.setattr(
        "lattice.indexer.pdf.PdfReader",
        lambda _p: _FakeReader(["Page one has enough text. " * 10, ""]),
    )
    monkeypatch.setattr(
        "lattice.indexer.pdf._ocr_page",
        lambda _path, idx: "Recovered from image." if idx == 1 else "",
    )
    fake_pdf = tmp_path / "mixed.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n")
    source = PDFIndexer().index(fake_pdf)
    assert source.metadata.ocr_used is True
    page1 = [p for p in source.passages if p.location.page == 1]
    page2 = [p for p in source.passages if p.location.page == 2]
    assert page1 and page2


def test_pdf_indexer_flags_unocrable_pages(monkeypatch, tmp_path: Path) -> None:
    """If OCR also returns nothing, page is counted as unocrable."""
    monkeypatch.setattr("lattice.indexer.pdf.PdfReader", lambda _p: _FakeReader(["", ""]))
    monkeypatch.setattr("lattice.indexer.pdf._ocr_page", lambda _p, _i: "")
    fake_pdf = tmp_path / "blank.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4\n")
    source = PDFIndexer().index(fake_pdf)
    assert source.metadata.ocr_used is False
    assert "unocrable_pages=2" in source.metadata.indexer_version


def test_find_tesseract_cmd_respects_env_override(monkeypatch, tmp_path: Path) -> None:
    fake_binary = tmp_path / "tesseract.exe"
    fake_binary.write_bytes(b"")
    monkeypatch.setenv("LATTICE_TESSERACT_CMD", str(fake_binary))
    from lattice.indexer.pdf import _find_tesseract_cmd
    assert _find_tesseract_cmd() == str(fake_binary)


# ─── HTMLIndexer ──────────────────────────────────────

def test_html_indexer_extracts_block_text(tmp_path: Path) -> None:
    html = tmp_path / "page.html"
    html.write_text(
        "<html><head><title>A Page</title>"
        "<link rel='canonical' href='https://example.test/a'></head>"
        "<body><h1>Heading</h1><p>Para one.</p>"
        "<script>ignore()</script><p>Para two.</p></body></html>",
        encoding="utf-8",
    )
    source = HTMLIndexer().index(html)
    texts = [p.text for p in source.passages]
    assert "Heading" in texts
    assert "Para one." in texts
    assert "Para two." in texts
    assert all("ignore()" not in t for t in texts)
    assert source.citation.url == "https://example.test/a"


# ─── SourceIndexer dispatcher ─────────────────────────

def test_source_indexer_dispatches_by_extension(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "refs" / "notes").mkdir(parents=True)
    (project / "refs" / "notes" / "a.md").write_text(
        "# A\n\nFirst.\n", encoding="utf-8"
    )
    (project / "refs" / "notes" / "b.md").write_text(
        "# B\n\nAlso.\n", encoding="utf-8"
    )
    si = SourceIndexer(project)
    sources, skipped = si.index_all()
    assert len(sources) == 2
    assert len(skipped) == 0
    assert {s.source_id for s in sources} == {"a", "b"}
    assert all(s.type == SourceType.note for s in sources)


def test_source_indexer_skips_unchanged_files(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "refs" / "notes").mkdir(parents=True)
    md = project / "refs" / "notes" / "a.md"
    md.write_text("# A\n\nContent.\n", encoding="utf-8")
    si = SourceIndexer(project)
    first, _ = si.index_all()
    assert len(first) == 1
    second, skipped = si.index_all()
    assert len(second) == 0
    assert len(skipped) == 1


def test_source_indexer_reindexes_when_content_changes(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "refs" / "notes").mkdir(parents=True)
    md = project / "refs" / "notes" / "a.md"
    md.write_text("# A\n\nOriginal.\n", encoding="utf-8")
    si = SourceIndexer(project)
    si.index_all()
    md.write_text("# A\n\nRevised content.\n", encoding="utf-8")
    second, skipped = si.index_all()
    assert len(second) == 1
    assert len(skipped) == 0


def test_source_indexer_applies_folder_conventions(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "refs" / "prior_writing").mkdir(parents=True)
    (project / "refs" / "prior_writing" / "me.md").write_text(
        "# Mine\n\nPrior.\n", encoding="utf-8"
    )
    sources, _ = SourceIndexer(project).index_all()
    assert len(sources) == 1
    assert sources[0].type == SourceType.prior_writing


def test_source_indexer_no_refs_dir_returns_empty(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    sources, skipped = SourceIndexer(project).index_all()
    assert sources == []
    assert skipped == []


def test_source_indexer_force_reindexes_everything(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "refs" / "notes").mkdir(parents=True)
    (project / "refs" / "notes" / "a.md").write_text(
        "# A\n\nStable.\n", encoding="utf-8"
    )
    si = SourceIndexer(project)
    si.index_all()
    sources, skipped = si.index_all(force=True)
    assert len(sources) == 1
    assert len(skipped) == 0
