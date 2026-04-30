"""LLM-driven extraction of citation metadata from raw text.

A user uploads a paper (e.g. the judicial-decisions paper as an
outline). The paper contains a References / Bibliography section
listing every work it cites, but Lattice's auto-outliner doesn't
parse those into ``Source`` records — so the project's References
tab stays empty.

This module reads raw text, asks Claude to identify and parse
citation entries into structured ``Citation`` payloads, and the
caller turns each into a ``Source`` and persists it. The output is
defensively parsed: malformed rows are dropped instead of failing
the whole call.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ..graph.models import (
    Citation, Source, SourceMetadata, SourceType,
)


_REFERENCES_HEADING_RE = re.compile(
    r"(?:^|\n)\s*(?:references|bibliography|works\s+cited|literature\s+cited|"
    r"reference\s+list|cited\s+works|sources)\s*\n",
    re.IGNORECASE,
)

# Heuristic: a long run of "1. Author (year)" or "[1] Author" lines
# usually means the references list. We pick the earliest such block
# of 3+ consecutive numbered citation lines as the start of the
# references when no heading is found.
_NUMBERED_CITATION_RE = re.compile(
    r"(?:^|\n)\s*(?:\d{1,3}\.|\[\d{1,3}\])\s+[A-Z][A-Za-z'\-]+",
)


class _LLMProtocol(Protocol):
    async def complete_json(
        self,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> tuple[Any, Any]: ...


_SYSTEM_PROMPT = """You extract structured citation metadata from any text that contains references — a Bibliography, a References list, a numbered citation block (1. Author... 2. Author...), inline footnote-style citations, or a mix of these. The text may not have a "References" heading at all; look for the citations themselves.

For each reference entry, output one JSON object with these fields:
- authors: list of author names as strings (e.g. ["Shai Danziger", "Jonathan Levav"]).
  - Preserve original spelling.
  - Each name should be a full name where given (first + last). If only initials are present, keep them.
  - Do not invent given names if only the surname is shown.
- year: the publication year as an integer, or null if absent.
- title: the work's title as a single string, with no surrounding quotes.
- container: the journal, book, or conference name. null if it's a book or website with no container.
- volume: as a string (preserves leading zeros). null if absent.
- issue: as a string. null if absent.
- pages: as a string (e.g. "1234-1240"). null if absent.
- doi: just the DOI identifier, no URL prefix (e.g. "10.1073/pnas.1018033108"). null if absent.
- url: the URL if present and not a DOI. null otherwise.

Citation formats you should recognise (a non-exhaustive list):
- Numbered Vancouver/IEEE-style: `1. Smith J, Jones B (2011) Title. Journal 12(3):45-60.`
- Numbered with brackets: `[1] J. Smith and B. Jones, "Title," Journal, vol. 12, pp. 45-60, 2011.`
- APA-style: `Smith, J., & Jones, B. (2011). Title. Journal, 12(3), 45-60.`
- Chicago/Harvard-style: `Smith, J. and Jones, B. 2011. "Title." Journal 12(3): 45-60.`
- MLA-style: `Smith, John, and Bob Jones. "Title." Journal 12.3 (2011): 45-60.`

Each reference may span multiple lines; do not split a single reference across multiple JSON objects.

Rules:
1. Output ONLY a JSON array. No prose, no code fences. Empty array if you find nothing.
2. Skip non-references: figure captions, acknowledgements, bare URLs without context, table rows, statistical formulas.
3. Be conservative — if a value is genuinely unclear, use null rather than guess.
4. Order entries as they appear in the source.
5. If the text contains a long numbered list of citations (1., 2., 3., …) that are clearly references, extract every entry — do not skip middle entries to save space.

Example output:
[
  {"authors": ["John Smith", "Jane Doe"], "year": 2011, "title": "Extraneous factors in judicial decisions", "container": "Proceedings of the National Academy of Sciences", "volume": "108", "issue": "17", "pages": "6889-6892", "doi": "10.1073/pnas.1018033108", "url": null},
  {"authors": ["Robert Roe"], "year": 2015, "title": "Decision fatigue in legal contexts", "container": null, "volume": null, "issue": null, "pages": null, "doi": null, "url": "https://example.org/decision-fatigue"}
]
"""


def _isolate_references_section(text: str) -> str:
    """Return only the text from the References / Bibliography heading
    onwards. If no explicit heading is found, look for the earliest
    point at which numbered citation lines (`1. Author...`,
    `[1] Author...`) start clustering. As a last resort, return the
    last ~30k chars of the document, where references usually live."""
    match = _REFERENCES_HEADING_RE.search(text)
    if match:
        return text[match.end():][:30000]

    # Fallback 1: locate the start of a numbered reference list. We
    # scan all matches and pick the earliest one that's followed by
    # at least one more numbered citation line within 1000 chars,
    # i.e. an actual list rather than a chance occurrence.
    matches = list(_NUMBERED_CITATION_RE.finditer(text))
    if len(matches) >= 3:
        for i, m in enumerate(matches[:-2]):
            next_m = matches[i + 2]
            if next_m.start() - m.start() < 1500:
                return text[m.start():][:30000]

    # Fallback 2: tail of the document.
    if len(text) <= 30000:
        return text
    return text[-30000:]


def _user_prompt(text: str) -> str:
    section = _isolate_references_section(text)
    return (
        "Extract every citation you can find in the text below. The text "
        "is the tail end of an academic paper and likely contains a "
        "numbered references list, a bibliography, or footnote-style "
        "citations. There may not be an explicit 'References' heading "
        "— look for citation patterns directly. Output a single JSON "
        "array.\n\n"
        "---BEGIN TEXT---\n"
        f"{section}\n"
        "---END TEXT---\n"
    )


def _coerce_citation(row: Any) -> Citation | None:
    """Validate and coerce a single LLM-extracted row into a
    ``Citation``. Returns ``None`` if the row is too malformed to be
    useful (no title and no authors)."""
    if not isinstance(row, dict):
        return None
    title = (row.get("title") or "").strip()
    authors_raw = row.get("authors") or []
    authors = [
        str(a).strip() for a in authors_raw
        if isinstance(a, str) and a.strip()
    ]
    if not title and not authors:
        return None
    year = row.get("year")
    if year is not None:
        try:
            year = int(year)
            if year < 0 or year > 9999:
                year = None
        except (TypeError, ValueError):
            year = None
    return Citation(
        authors=authors,
        year=year,
        title=title or "(untitled)",
        container=(row.get("container") or None) or None,
        volume=(row.get("volume") or None) or None,
        issue=(row.get("issue") or None) or None,
        pages=(row.get("pages") or None) or None,
        doi=(row.get("doi") or None) or None,
        url=(row.get("url") or None) or None,
    )


async def extract_citations_from_text(
    text: str, llm: _LLMProtocol
) -> list[Citation]:
    """Ask Claude to parse the bibliography section out of `text`,
    returning structured Citations. Invalid rows are dropped."""
    if not text or not text.strip():
        return []
    data, _resp = await llm.complete_json(
        system=_SYSTEM_PROMPT,
        user=_user_prompt(text),
    )
    if not isinstance(data, list):
        return []
    citations: list[Citation] = []
    for row in data:
        c = _coerce_citation(row)
        if c is not None:
            citations.append(c)
    return citations


def citation_to_synthetic_source(
    citation: Citation,
    source_id: str | None = None,
    source_type: SourceType = SourceType.primary_paper,
    file_path: str = "",
) -> Source:
    """Build a ``Source`` record from a citation with no indexed
    file (the citation came from a bibliography entry, not from a
    PDF dropped into refs/).

    The resulting Source has no ``passages`` so evidence binding
    requires the user to also drop the underlying file in. The
    citation metadata is enough for the References tab to render
    every style.
    """
    sid = source_id or _slugify_citation(citation)
    return Source(
        source_id=sid,
        type=source_type,
        citation=citation,
        passages=[],
        metadata=SourceMetadata(
            peer_reviewed=False,
            primary=False,
            date_added=datetime.now(timezone.utc),
            file_path=file_path,
            hash="extracted-from-text",
            ocr_used=False,
            indexer_version="extraction:0.1",
        ),
    )


def _slugify_citation(citation: Citation) -> str:
    """Build a stable source_id from author + year, falling back to
    title-based slug. Caller should de-duplicate against existing
    source_ids."""
    last_name = ""
    if citation.authors:
        first = citation.authors[0]
        last_name = first.split(",", 1)[0].strip() if "," in first else first.split()[-1]
    last_name = re.sub(r"[^\w]+", "_", (last_name or "").lower()).strip("_")
    year = str(citation.year) if citation.year else "nodate"
    if last_name:
        return f"{last_name}_{year}"
    title_slug = re.sub(r"[^\w]+", "_", (citation.title or "untitled").lower()).strip("_")
    return f"{title_slug[:40]}_{year}"


def read_text_for_extraction(project_path: Path, source: str) -> str:
    """Resolve the input text for extraction.

    ``source`` is one of:
      - "outline" → reads ``structure/outline.md`` or
        ``structure/outline.raw.md`` (preferred — usually the original
        paper before auto-structuring).
      - "outline.raw" → reads ``structure/outline.raw.md`` only
      - a relative path under ``refs/`` → reads that file's text-
        sidecar (``.txt``) if present, else the file itself.
    """
    if source in ("outline", "outline.raw"):
        raw = project_path / "structure" / "outline.raw.md"
        if raw.exists() and source != "outline":
            return raw.read_text(encoding="utf-8", errors="replace")
        if source == "outline" and raw.exists():
            return raw.read_text(encoding="utf-8", errors="replace")
        # Fall back to outline.md if no raw archive exists.
        outline = project_path / "structure" / "outline.md"
        if outline.exists():
            return outline.read_text(encoding="utf-8", errors="replace")
        return ""
    # Treat as a path under refs/.
    candidate = (project_path / "refs" / source).resolve()
    refs_dir = (project_path / "refs").resolve()
    try:
        candidate.relative_to(refs_dir)
    except ValueError:
        return ""
    if not candidate.is_file():
        return ""
    # Prefer text sidecar if it exists.
    sidecar = candidate.with_name(candidate.name + ".txt")
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8", errors="replace")
    if candidate.suffix.lower() in (".txt", ".md", ".markdown"):
        return candidate.read_text(encoding="utf-8", errors="replace")
    # PDF without sidecar — try a quick pypdf extraction.
    if candidate.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(candidate))
            return "\n\n".join(
                (p.extract_text() or "") for p in reader.pages
            )
        except Exception:  # noqa: BLE001
            return ""
    return ""
