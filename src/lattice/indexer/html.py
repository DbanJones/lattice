"""HTML / archived web page indexer using BeautifulSoup.

Passage IDs: p.<seq> — sequential, since HTML line numbers are unreliable.
Preserves URL in citation.url if a <link rel="canonical"> or <meta property="og:url"> is present.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

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


_BLOCK_TAGS = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote")


class HTMLIndexer(Indexer):
    def index(self, file_path: Path) -> Source:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(raw, "html.parser")

        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()

        passages: list[Passage] = []
        seq = 0
        for node in soup.find_all(_BLOCK_TAGS):
            text = " ".join((node.get_text(" ") or "").split())
            if not text:
                continue
            seq += 1
            passages.append(
                Passage(
                    id=f"p.{seq}.1",
                    text=text,
                    location=PassageLocation(paragraph=seq, section=node.name),
                    type=PassageType.claim,
                    char_count=len(text),
                )
            )

        title = _infer_title(soup, file_path)
        url = _infer_url(soup)

        return Source(
            source_id=Indexer.slugify(file_path.stem),
            type=SourceType.web_page,
            citation=Citation(authors=[], year=None, title=title, url=url),
            passages=passages,
            metadata=SourceMetadata(
                date_added=datetime.now(timezone.utc),
                file_path=str(file_path),
                hash="",
            ),
        )


def _infer_title(soup: BeautifulSoup, file_path: Path) -> str:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return file_path.stem


def _infer_url(soup: BeautifulSoup) -> str | None:
    link = soup.find("link", rel="canonical")
    if link and link.get("href"):
        return str(link["href"])
    og = soup.find("meta", property="og:url")
    if og and og.get("content"):
        return str(og["content"])
    return None
