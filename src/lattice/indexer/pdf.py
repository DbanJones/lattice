"""PDF indexer with per-page OCR fallback.

Strategy:
1. Extract text with pypdf (fast, works on digital PDFs).
2. For any page whose extracted text is under `_OCR_MIN_CHARS`, render
   that page to a PNG via pypdfium2 and OCR it with pytesseract.
3. Flag `metadata.ocr_used = True` if OCR was used on at least one page.

Passage IDs: p.<page>.<seq> where page is 1-based physical page, seq is
the paragraph sequence within that page.

Tesseract discovery: if `tesseract` isn't on PATH, we probe common Windows
install locations and the `LATTICE_TESSERACT_CMD` env var.
"""

from __future__ import annotations

import io
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pypdfium2 as pdfium
import pytesseract
from PIL import Image
from pypdf import PdfReader

from ..graph.models import (
    Citation,
    Passage,
    PassageLocation,
    PassageType,
    Source,
    SourceMetadata,
    SourceType,
)
from .base import Indexer


_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")
_FIGURE_RE = re.compile(r"^(figure|fig\.?)\s*\d+", re.IGNORECASE)
_TABLE_RE = re.compile(r"^table\s*\d+", re.IGNORECASE)

_OCR_MIN_CHARS = 100  # per-page threshold below which OCR is attempted
_OCR_DPI = 300


# ─── Tesseract binary discovery ────────────────────────────

def _find_tesseract_cmd() -> str | None:
    """Locate the tesseract binary, searching (in order):
    1. LATTICE_TESSERACT_CMD env var
    2. TESSERACT_CMD env var (pytesseract's convention)
    3. shutil.which on PATH
    4. Common Windows install locations
    """
    for var in ("LATTICE_TESSERACT_CMD", "TESSERACT_CMD"):
        candidate = os.environ.get(var)
        if candidate and Path(candidate).exists():
            return candidate
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path
    for candidate in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    ):
        if Path(candidate).exists():
            return candidate
    return None


_TESSERACT_CMD = _find_tesseract_cmd()
if _TESSERACT_CMD is not None:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD


def tesseract_available() -> bool:
    """Expose for tests and status reporting."""
    if _TESSERACT_CMD is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


# ─── OCR helpers ──────────────────────────────────────────

def _ocr_page(pdf_path: Path, page_index: int) -> str:
    """Render one PDF page to an image and run tesseract. Returns "" on failure."""
    try:
        doc = pdfium.PdfDocument(str(pdf_path))
    except Exception:
        return ""
    try:
        page = doc[page_index]
        pil_image: Image.Image = page.render(scale=_OCR_DPI / 72).to_pil()
        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        buffer.seek(0)
        with Image.open(buffer) as img:
            return pytesseract.image_to_string(img) or ""
    except pytesseract.TesseractNotFoundError:
        return ""
    except Exception:
        return ""
    finally:
        try:
            doc.close()
        except Exception:
            pass


# ─── Indexer ──────────────────────────────────────────────

class PDFIndexer(Indexer):
    def index(self, file_path: Path) -> Source:
        reader = PdfReader(str(file_path))
        passages: list[Passage] = []
        pages_ocr_used = 0
        pages_empty_after_ocr = 0

        for page_index, page in enumerate(reader.pages):
            page_num = page_index + 1
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""

            # Per-page OCR fallback for sparse pages
            used_ocr_here = False
            if len(page_text.strip()) < _OCR_MIN_CHARS:
                ocr_text = _ocr_page(file_path, page_index)
                if ocr_text.strip():
                    page_text = ocr_text
                    used_ocr_here = True
                    pages_ocr_used += 1
                elif not page_text.strip():
                    pages_empty_after_ocr += 1

            paragraphs = [p.strip() for p in _PARA_SPLIT_RE.split(page_text) if p.strip()]
            for seq, para in enumerate(paragraphs, start=1):
                passages.append(
                    Passage(
                        id=f"p.{page_num}.{seq}",
                        text=para,
                        location=PassageLocation(page=page_num, paragraph=seq),
                        type=_classify(para),
                        char_count=len(para),
                    )
                )

        meta_title = _safe_title(reader, file_path)
        meta_authors = _safe_authors(reader)
        meta_year = _safe_year(reader)

        version_note = "0.1.0"
        if pages_empty_after_ocr:
            version_note += f" (unocrable_pages={pages_empty_after_ocr})"

        return Source(
            source_id=_source_id(meta_authors, meta_year, file_path),
            type=SourceType.primary_paper,
            citation=Citation(authors=meta_authors, year=meta_year, title=meta_title),
            passages=passages,
            metadata=SourceMetadata(
                peer_reviewed=False,
                primary=True,
                date_added=datetime.now(timezone.utc),
                file_path=str(file_path),
                hash="",  # filled in by SourceIndexer
                ocr_used=pages_ocr_used > 0,
                indexer_version=version_note,
            ),
        )


# ─── Helpers ──────────────────────────────────────────────

def _classify(paragraph: str) -> PassageType:
    if _FIGURE_RE.match(paragraph):
        return PassageType.figure_caption
    if _TABLE_RE.match(paragraph):
        return PassageType.table_cell
    return PassageType.claim


def _safe_title(reader: PdfReader, file_path: Path) -> str:
    try:
        title = (reader.metadata or {}).get("/Title") if reader.metadata else None
        if title:
            return str(title).strip() or file_path.stem
    except Exception:
        pass
    return file_path.stem


def _safe_authors(reader: PdfReader) -> list[str]:
    try:
        author_raw = (reader.metadata or {}).get("/Author") if reader.metadata else None
        if not author_raw:
            return []
        return [a.strip() for a in re.split(r";|,| and ", str(author_raw)) if a.strip()]
    except Exception:
        return []


def _safe_year(reader: PdfReader) -> int | None:
    try:
        for key in ("/CreationDate", "/ModDate"):
            raw = (reader.metadata or {}).get(key) if reader.metadata else None
            if raw:
                m = re.search(r"D:(\d{4})", str(raw))
                if m:
                    return int(m.group(1))
    except Exception:
        return None
    return None


def _source_id(authors: list[str], year: int | None, file_path: Path) -> str:
    if authors and year:
        first_last = authors[0].split()[-1].split(",")[0]
        return Indexer.slugify(f"{first_last}_{year}")
    return Indexer.slugify(file_path.stem)
