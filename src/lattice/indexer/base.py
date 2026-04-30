"""Base indexer interface and top-level dispatcher.

See docs/HANDOFF.md step 5 and docs/SPEC.md 5.2.

Sub-folder dispatch:
- papers/*.pdf       -> PDFIndexer            primary_paper
- papers/*.docx      -> DOCXIndexer           primary_paper
- notes/*.md         -> MarkdownIndexer       note
- data/*.xlsx        -> SpreadsheetIndexer    dataset
- web/*.html         -> HTMLIndexer           web_page
- prior_writing/*    -> by extension, tagged author_origin=True, type=prior_writing
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import Source, SourceType


class Indexer(ABC):
    """Base for all source indexers."""

    @abstractmethod
    def index(self, file_path: Path) -> Source:
        """Read a file, produce a Source with stable passage IDs."""
        raise NotImplementedError

    @staticmethod
    def hash_file(file_path: Path) -> str:
        h = hashlib.sha256()
        with file_path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()

    @staticmethod
    def slugify(name: str) -> str:
        """Turn a filename stem into a stable source_id slug."""
        slug = re.sub(r"[^\w]+", "_", name.lower()).strip("_")
        return slug or "unnamed"


class SourceIndexer:
    """Top-level indexer that dispatches to format-specific indexers.

    Skips files whose hash is unchanged (unless force=True). The hash cache
    lives at .lattice/cache/source_hashes.json.
    """

    # file extensions this dispatcher knows about
    _EXT_MAP: dict[str, str] = {
        ".pdf": "pdf",
        ".docx": "docx",
        ".md": "markdown",
        ".markdown": "markdown",
        ".xlsx": "spreadsheet",
        ".xls": "spreadsheet",
        ".html": "html",
        ".htm": "html",
    }

    # refs/ sub-folder -> default source type
    _FOLDER_TYPE: dict[str, SourceType] = {
        "papers": SourceType.primary_paper,
        "notes": SourceType.note,
        "data": SourceType.dataset,
        "web": SourceType.web_page,
        "prior_writing": SourceType.prior_writing,
    }

    def __init__(self, project_path: Path) -> None:
        self.project_path = Path(project_path)
        self.refs_dir = self.project_path / "refs"
        self.cache_path = self.project_path / ".lattice" / "cache" / "source_hashes.json"

    # ─── hash cache ──────────────────────────────────────

    def _load_cache(self) -> dict[str, str]:
        if not self.cache_path.exists():
            return {}
        return json.loads(self.cache_path.read_text(encoding="utf-8"))

    def _save_cache(self, cache: dict[str, str]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    # ─── dispatch ────────────────────────────────────────

    def _indexer_for(self, file_path: Path) -> Indexer | None:
        ext = file_path.suffix.lower()
        kind = self._EXT_MAP.get(ext)
        if kind is None:
            return None
        # Local imports to avoid cycles and keep cold-start cheap.
        if kind == "pdf":
            from .pdf import PDFIndexer
            return PDFIndexer()
        if kind == "docx":
            from .docx import DOCXIndexer
            return DOCXIndexer()
        if kind == "markdown":
            from .markdown import MarkdownIndexer
            return MarkdownIndexer()
        if kind == "spreadsheet":
            from .spreadsheet import SpreadsheetIndexer
            return SpreadsheetIndexer()
        if kind == "html":
            from .html import HTMLIndexer
            return HTMLIndexer()
        return None

    def _source_type_for(self, file_path: Path) -> SourceType:
        # Sub-folder under refs/ dictates source type; fallback to file extension.
        try:
            rel = file_path.relative_to(self.refs_dir)
        except ValueError:
            return SourceType.note
        if not rel.parts:
            return SourceType.note
        folder = rel.parts[0]
        return self._FOLDER_TYPE.get(folder, SourceType.note)

    def _is_author_origin(self, file_path: Path) -> bool:
        try:
            rel = file_path.relative_to(self.refs_dir)
        except ValueError:
            return False
        return bool(rel.parts) and rel.parts[0] == "prior_writing"

    # ─── main entry point ───────────────────────────────

    def index_all(self, force: bool = False) -> tuple[list[Source], list[Path]]:
        """Index every file in refs/. Returns (sources, skipped_unchanged).

        Sources list contains only sources that were indexed this call.
        Skipped files (hash unchanged) are not re-indexed; caller should
        merge with GraphStore's existing sources.
        """
        if not self.refs_dir.exists():
            return [], []

        cache = self._load_cache()
        new_cache: dict[str, str] = {}
        sources: list[Source] = []
        skipped: list[Path] = []

        for file_path in sorted(self.refs_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.name.startswith("."):
                continue
            indexer = self._indexer_for(file_path)
            if indexer is None:
                continue

            rel = str(file_path.relative_to(self.project_path)).replace("\\", "/")
            file_hash = Indexer.hash_file(file_path)
            new_cache[rel] = file_hash

            if not force and cache.get(rel) == file_hash:
                skipped.append(file_path)
                continue

            source = indexer.index(file_path)
            # Apply sub-folder conventions to the Source the indexer returned.
            source.type = self._source_type_for(file_path)
            source.metadata.file_path = rel
            source.metadata.hash = file_hash
            source.metadata.date_added = datetime.now(timezone.utc)
            if self._is_author_origin(file_path):
                source.type = SourceType.prior_writing
            sources.append(source)

        self._save_cache(new_cache)
        return sources, skipped
