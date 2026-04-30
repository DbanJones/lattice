"""Markdown indexer.

Passage IDs: p.<line>.<seq>, where line is 1-based start line of the
paragraph, seq is sequence within that line (almost always 1).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from markdown_it import MarkdownIt

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


class MarkdownIndexer(Indexer):
    def index(self, file_path: Path) -> Source:
        text = file_path.read_text(encoding="utf-8")
        md = MarkdownIt()
        tokens = md.parse(text)

        passages: list[Passage] = []
        seq_by_line: dict[int, int] = {}

        # Walk tokens; collect paragraphs and headings as passages.
        for i, token in enumerate(tokens):
            if token.type not in {"paragraph_open", "heading_open"}:
                continue
            # inline text comes in the following token
            inline = tokens[i + 1] if i + 1 < len(tokens) else None
            if inline is None or inline.type != "inline":
                continue
            content = (inline.content or "").strip()
            if not content:
                continue
            start_line = (token.map[0] + 1) if token.map else 0
            seq_by_line[start_line] = seq_by_line.get(start_line, 0) + 1
            passage_id = f"p.{start_line}.{seq_by_line[start_line]}"
            ptype = _classify(content, is_heading=token.type == "heading_open")
            passages.append(
                Passage(
                    id=passage_id,
                    text=content,
                    location=PassageLocation(line=start_line),
                    type=ptype,
                    char_count=len(content),
                )
            )

        title = _infer_title(text, file_path)

        return Source(
            source_id=Indexer.slugify(file_path.stem),
            type=SourceType.note,
            citation=Citation(authors=[], year=None, title=title),
            passages=passages,
            metadata=SourceMetadata(
                date_added=datetime.now(timezone.utc),
                file_path=str(file_path),
                hash="",  # filled in by SourceIndexer
            ),
        )


_FIGURE_RE = re.compile(r"^(figure|fig\.?)\s*\d+", re.IGNORECASE)
_TABLE_RE = re.compile(r"^table\s*\d+", re.IGNORECASE)


def _classify(content: str, is_heading: bool) -> PassageType:
    if is_heading:
        return PassageType.claim
    if _FIGURE_RE.match(content):
        return PassageType.figure_caption
    if _TABLE_RE.match(content):
        return PassageType.table_cell
    return PassageType.claim


def _infer_title(text: str, file_path: Path) -> str:
    # First ATX heading if present; otherwise filename.
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return file_path.stem
