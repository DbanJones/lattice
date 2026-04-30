"""DOCX outline ingester.

Author scaffolds often arrive as Word documents. This ingester reads the
DOCX's paragraph/heading/list structure, reshapes it into the same
outline syntax the markdown ingester expects, and delegates to
MarkdownOutlineIngester for tag parsing. Keeping a single text-level
parser means the same tag vocabulary (`[ref:]`, `MY VIEW:`, `COUNTER:`,
`[role:]`, `[depth:]`, `[supports:]`/`[contradicts:]`, etc.) works for
both file types.

Mapping:
- Paragraph with text "THESIS" (case-insensitive)  → `# THESIS` marker
- Style "Title" / "Heading 1"                     → `# <Letter>. <text>`
                                                     (preserves existing A./B.
                                                      prefixes in the heading text)
- Everything else                                  → `  - <text>` bullet
  - Bulleted styles are used when available, but any body paragraph
    sitting under a heading is treated as a claim.
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document

from ..graph.models import AuthorGraph
from ..utils.config import Config
from .markdown import MarkdownOutlineIngester


_LETTER_PREFIX_RE = re.compile(r"^\s*([A-Z])\.\s+(.+)$")


class DOCXOutlineIngester:
    def __init__(self, config: Config) -> None:
        self.config = config

    async def ingest(self, file_path: Path, project_name: str) -> AuthorGraph:
        doc = Document(str(file_path))
        outline_md = _docx_to_outline_markdown(doc)
        # Delegate all tag/relationship/claim parsing to the markdown ingester
        # so there is one source of truth for the outline syntax.
        md_ingester = MarkdownOutlineIngester(self.config)
        return md_ingester._parse(outline_md, project_name)


def _docx_to_outline_markdown(doc) -> str:
    """Convert a Word document into the outline-markdown string the
    MarkdownOutlineIngester understands. Pure text-level transformation;
    no LLM involved.
    """
    lines: list[str] = []
    section_counter = 0
    mode = "preamble"  # "preamble" | "thesis" | "section"

    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if not text:
            continue
        style = ((para.style.name if para.style else "") or "").lower()

        # THESIS marker — any heading-ish paragraph whose text is just "THESIS".
        if _is_thesis_marker(text):
            lines.append("# THESIS")
            lines.append("")
            mode = "thesis"
            continue

        if _is_heading(style):
            letter, title = _heading_letter_and_title(text, section_counter)
            if letter:
                section_counter = max(section_counter, _letter_index(letter))
            else:
                section_counter += 1
                letter = _index_letter(section_counter)
            lines.append(f"# {letter}. {title}")
            lines.append("")
            mode = "section"
            continue

        # Non-heading body paragraph.
        if mode == "thesis":
            # Collect thesis-statement prose verbatim until the next heading.
            lines.append(text)
            continue

        if mode == "section":
            # Strip a leading "- " if the user already typed one.
            stripped = text.lstrip("-• \t")
            lines.append(f"  - {stripped}")
            continue

        # Preamble body paragraph with no heading above it — treat it as
        # the thesis if we haven't seen THESIS yet. Cheap ergonomics win.
        if mode == "preamble":
            lines.append("# THESIS")
            lines.append("")
            lines.append(text)
            mode = "thesis"

    return "\n".join(lines) + "\n"


def _is_heading(style: str) -> bool:
    if not style:
        return False
    return style == "title" or style.startswith("heading 1")


def _is_thesis_marker(text: str) -> bool:
    # Allow "THESIS", "# THESIS", "Thesis:", etc.
    cleaned = text.strip().lstrip("#").strip().rstrip(":").strip()
    return cleaned.upper() == "THESIS"


def _heading_letter_and_title(text: str, prev_counter: int) -> tuple[str, str]:
    m = _LETTER_PREFIX_RE.match(text)
    if m:
        return m.group(1), m.group(2).strip()
    return "", text


def _index_letter(n: int) -> str:
    # 1 → 'A', 2 → 'B', ..., 26 → 'Z', 27 → 'AA', etc.
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters or "A"


def _letter_index(letter: str) -> int:
    # Inverse of _index_letter for A–Z; treats multi-letter prefixes as their final char.
    if not letter:
        return 0
    return ord(letter[-1].upper()) - ord("A") + 1
