"""Heuristic conversion of pypdf-extracted text → tidy markdown.

pypdf gives us a flat string of text per page. The plain extraction
loses headings, lists, footers, page numbers, and column structure.
This module applies cheap, deterministic rules to reconstruct
something close to the original layout:

  - Numbered / lettered section headings (`1. Introduction`,
    `1.1 Background`, `Abstract`, `Methods`, …) become markdown
    headings (`#`, `##`).
  - Lines that look like running headers / footers / page numbers
    (a single integer, a stray `1 of 10`, a repeated all-caps title
    on every page) are stripped.
  - Bullet glyphs (`•`, `▪`, `–`) at the start of a line become
    markdown `-` bullets.
  - Multiple blank lines collapse to a single paragraph break.
  - Hyphenated words split across line breaks (`extra-\nordinary`
    → `extraordinary`) are rejoined.

No LLM calls. Output is intentionally close to the input — better to
preserve raw text than to invent structure.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable


# Section-heading patterns that mainstream academic PDFs use.
# These match a line *on its own*, not text in the middle of a paragraph.
_LEVEL_1_HEADINGS = re.compile(
    r"^(?:Abstract|Introduction|Background|Related\s+Work|Methods|"
    r"Methodology|Materials\s+and\s+Methods|Results|Discussion|"
    r"Conclusion|Conclusions|References|Bibliography|"
    r"Acknowledg(?:e?ments?|ments?)|Appendix|Supplementary\s+(?:Material|Information))"
    r"\s*$",
    re.IGNORECASE,
)

# "1. Introduction" / "1.1 Background" / "I. Overview"
_NUMBERED_H1 = re.compile(r"^(\d+)\.\s+([A-Z][A-Za-z][^.!?]*?)\s*$")
_NUMBERED_H2 = re.compile(r"^(\d+\.\d+)\s+([A-Z][A-Za-z][^.!?]*?)\s*$")
_NUMBERED_H3 = re.compile(r"^(\d+\.\d+\.\d+)\s+([A-Z][A-Za-z][^.!?]*?)\s*$")
_ROMAN_H1 = re.compile(r"^([IVX]+)\.\s+([A-Z][A-Za-z][^.!?]*?)\s*$")

# Bare integer (likely a page number) or "Page X of Y" stamps.
_PAGE_NUM = re.compile(r"^\s*\d+\s*$")
_PAGE_OF = re.compile(r"^\s*\d+\s*(?:of|/)\s*\d+\s*$", re.IGNORECASE)
_PAGE_LABEL = re.compile(r"^\s*Page\s+\d+\s*$", re.IGNORECASE)

# Figure / table captions we want to mark distinctly.
_CAPTION = re.compile(r"^(Figure|Fig\.?|Table|Tab\.?)\s*\d+\b", re.IGNORECASE)

# Bullet glyphs the extractor often emits at the start of list items.
# Includes en-dash (–) and em-dash (—) which PDFs occasionally use.
_BULLET_GLYPH = re.compile(r"^[•▪●◦‣⁃∙\-–—]\s+")

# A run of capital letters interrupted only by spaces — almost
# certainly a section heading the typesetter set in caps.
_CAPS_HEADING = re.compile(r"^[A-Z][A-Z0-9 \-:&,/'()]{3,79}$")

# Hyphen-at-end-of-line followed by a continuation on the next line
# (`extra-\nordinary`). Keep the rejoin conservative: only when the
# next line starts with a lowercase letter, otherwise it might be
# a real hyphenated phrase.
_HYPHEN_BREAK = re.compile(r"-\n([a-z])")


def _detect_repeated_headers_footers(
    pages: list[str], min_repetitions: int = 3
) -> set[str]:
    """Find lines that appear identically on the first / last line of
    many pages — those are running headers and footers (the journal
    name, "Danziger et al.", etc.) and should be stripped."""
    candidates: list[str] = []
    for page in pages:
        lines = [l.strip() for l in page.splitlines() if l.strip()]
        if len(lines) >= 2:
            candidates.append(lines[0])
            candidates.append(lines[-1])
    counts = Counter(candidates)
    threshold = max(min_repetitions, len(pages) // 3)
    return {line for line, n in counts.items() if n >= threshold and len(line) < 120}


def _format_one_line(line: str) -> str | None:
    """Return the markdown-formatted version of a line, or ``None``
    to drop it entirely. Operates line-by-line because most academic
    formatting decisions can be made locally."""
    raw = line.rstrip()
    stripped = raw.strip()
    if not stripped:
        return ""

    # Drop page-number debris.
    if _PAGE_NUM.match(stripped) and len(stripped) <= 4:
        return None
    if _PAGE_OF.match(stripped) or _PAGE_LABEL.match(stripped):
        return None

    # Headings — most specific patterns first so 1.1.1 beats 1.1 beats 1.
    m = _NUMBERED_H3.match(stripped)
    if m:
        return f"### {m.group(1)} {m.group(2).strip()}"
    m = _NUMBERED_H2.match(stripped)
    if m:
        return f"## {m.group(1)} {m.group(2).strip()}"
    m = _NUMBERED_H1.match(stripped)
    if m:
        return f"# {m.group(1)}. {m.group(2).strip()}"
    m = _ROMAN_H1.match(stripped)
    if m:
        return f"# {m.group(1)}. {m.group(2).strip()}"
    if _LEVEL_1_HEADINGS.match(stripped):
        return f"# {stripped.title() if stripped.isupper() else stripped}"
    if _CAPS_HEADING.match(stripped) and len(stripped.split()) <= 12:
        # All-caps short line — likely a heading. Title-case it so
        # the markdown is readable.
        return f"## {stripped.title()}"

    # Captions stay inline as bold lines so they're visually grouped
    # without disrupting the heading hierarchy.
    if _CAPTION.match(stripped):
        return f"**{stripped}**"

    # Bullet lists.
    bullet = _BULLET_GLYPH.match(stripped)
    if bullet:
        return f"- {stripped[bullet.end():]}"

    return raw


def pdf_text_to_markdown(pages: list[str]) -> str:
    """Convert a list of per-page extracted text strings into a
    single markdown document. Strips repeated running headers/
    footers, applies heading detection, and rejoins hyphenation."""
    junk_lines = _detect_repeated_headers_footers(pages)

    out_lines: list[str] = []
    blank_streak = 0
    for page in pages:
        for raw in page.splitlines():
            stripped = raw.strip()
            if stripped in junk_lines:
                continue
            formatted = _format_one_line(raw)
            if formatted is None:
                continue
            if formatted == "":
                # Collapse consecutive blanks down to one paragraph break.
                if blank_streak == 0 and out_lines:
                    out_lines.append("")
                blank_streak += 1
                continue
            blank_streak = 0
            out_lines.append(formatted)
        # Page boundaries also count as a blank line.
        if blank_streak == 0 and out_lines:
            out_lines.append("")
            blank_streak = 1

    body = "\n".join(out_lines)

    # Rejoin hyphenated line-breaks (`extra-\nordinary` → `extraordinary`).
    body = _HYPHEN_BREAK.sub(r"\1", body)

    # Tidy: collapse 3+ blank lines to 2.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip() + "\n"


def extract_pdf_pages(pdf_path) -> list[str]:
    """Helper: pypdf-extract every page's text into a list. Returns
    [] if pypdf can't open the file (the caller usually already has
    the pages but this keeps the module self-contained for tests)."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return []
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:  # noqa: BLE001
        return []
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    return pages
