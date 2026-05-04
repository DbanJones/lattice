"""Reference import from Zotero CSL-JSON / BibTeX / RIS.

The single biggest adoption unlock: most academics already have a
curated reference library in Zotero, Mendeley, EndNote, or as a
``.bib`` file. Until Lattice can read them, the friction of dropping
PDFs into ``refs/papers/`` and re-tagging everything blocks adoption.

Three input formats:

- **CSL-JSON** (Zotero's native export) — best round-trip; preserves
  field semantics. Detected by ``.json`` extension or sniffing JSON.
- **BibTeX** (`.bib`) — the LaTeX-world standard. Parsed with our own
  defensive parser (no extra dependency); handles ``@article{key, ...}``
  with brace-balanced field values.
- **RIS** (`.ris`) — the lingua franca for reference managers. Tag-line
  format; one record per ``ER  -`` block.

Each format produces a list of ``Source`` records ready to merge into
the project's source store. Defensive: bad rows are skipped with a
warning rather than failing the whole call.

Deduplication on merge: by DOI when present, else by a hash of (year,
first-author-surname, normalised-title).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..graph.models import (
    Citation,
    Source,
    SourceMetadata,
    SourceType,
)


# ─── public entry point ──────────────────────────────


@dataclass
class ImportReport:
    """What an import produced — the parsed sources plus diagnostics."""

    sources: list[Source] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # raw entries that didn't parse
    warnings: list[str] = field(default_factory=list)
    detected_format: str = "unknown"


def import_references(text: str, *, format: str | None = None) -> ImportReport:
    """Parse ``text`` and return an ImportReport.

    ``format`` may be ``"csl-json"`` / ``"bib"`` / ``"ris"`` to skip
    detection. Pass ``None`` (default) to auto-detect by sniffing.
    """
    fmt = format or _detect_format(text)
    if fmt == "csl-json":
        report = _import_csl_json(text)
    elif fmt == "bib":
        report = _import_bibtex(text)
    elif fmt == "ris":
        report = _import_ris(text)
    else:
        report = ImportReport(detected_format=fmt)
        report.warnings.append(
            f"Could not detect import format. "
            f"Pass format='csl-json'|'bib'|'ris' explicitly."
        )
        return report
    report.detected_format = fmt
    return report


def import_references_from_file(
    path: Path, *, format: str | None = None,
) -> ImportReport:
    """Convenience wrapper: read a file, infer format from suffix when
    not specified, return the parsed ImportReport."""
    fmt = format
    if fmt is None:
        suffix = path.suffix.lower()
        fmt = {
            ".json": "csl-json",
            ".bib": "bib",
            ".bibtex": "bib",
            ".ris": "ris",
        }.get(suffix)
    return import_references(path.read_text(encoding="utf-8"), format=fmt)


def merge_into_store(
    sources: list[Source], existing: list[Source],
) -> tuple[list[Source], dict[str, str]]:
    """Add ``sources`` to ``existing``, deduplicating.

    Returns ``(merged_list, decision_map)`` where ``decision_map`` is
    ``{source_id: "added" | "duplicate" | "renamed"}``. A duplicate is
    detected by DOI match (case-insensitive) or by content hash of
    ``(year, surname_of_first_author, normalised_title)``.

    A "renamed" outcome means the imported source matched an existing
    one but had a different ``source_id``; we keep the existing id and
    don't add a duplicate. The original source_id is added to the
    map so the caller can update inline citations if desired.
    """
    by_doi: dict[str, Source] = {
        s.citation.doi.lower(): s for s in existing
        if s.citation.doi
    }
    by_hash: dict[str, Source] = {
        _content_hash(s): s for s in existing
    }
    merged = list(existing)
    decisions: dict[str, str] = {}
    for src in sources:
        doi = (src.citation.doi or "").strip().lower()
        if doi and doi in by_doi:
            decisions[src.source_id] = "duplicate"
            continue
        h = _content_hash(src)
        if h in by_hash:
            decisions[src.source_id] = "duplicate"
            continue
        # Distinct — add it.
        merged.append(src)
        if doi:
            by_doi[doi] = src
        by_hash[h] = src
        decisions[src.source_id] = "added"
    return merged, decisions


# ─── detection ───────────────────────────────────────


def _detect_format(text: str) -> str:
    """Sniff the format from the leading non-whitespace content."""
    stripped = text.lstrip()
    if not stripped:
        return "unknown"
    # CSL-JSON starts with '['; some Zotero exports start with '{'.
    if stripped[0] in ("[", "{"):
        try:
            json.loads(text)
            return "csl-json"
        except json.JSONDecodeError:
            pass
    # BibTeX entries always start with '@'.
    if stripped[0] == "@":
        return "bib"
    # RIS records start with a 2-letter tag + '  - '.
    if re.match(r"[A-Z][A-Z0-9]\s\s-\s", stripped):
        return "ris"
    return "unknown"


# ─── CSL-JSON ────────────────────────────────────────


_CSL_TYPE_MAP = {
    "article-journal": SourceType.primary_paper,
    "article-magazine": SourceType.primary_paper,
    "article-newspaper": SourceType.primary_paper,
    "article": SourceType.primary_paper,
    "paper-conference": SourceType.primary_paper,
    "report": SourceType.report,
    "dataset": SourceType.dataset,
    "webpage": SourceType.web_page,
    "post-weblog": SourceType.web_page,
    "manuscript": SourceType.note,
    "book": SourceType.primary_paper,
    "chapter": SourceType.primary_paper,
    "thesis": SourceType.primary_paper,
    "interview": SourceType.interview,
}


def _import_csl_json(text: str) -> ImportReport:
    report = ImportReport(detected_format="csl-json")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        report.warnings.append(f"JSON parse error: {e}")
        return report
    if isinstance(data, dict):
        # Zotero's "Better CSL JSON" sometimes wraps the array.
        data = data.get("items") or [data]
    if not isinstance(data, list):
        report.warnings.append("CSL-JSON payload was not a list of items.")
        return report
    for entry in data:
        try:
            src = _csl_entry_to_source(entry)
            if src is not None:
                report.sources.append(src)
            else:
                report.skipped.append(json.dumps(entry, default=str)[:120])
        except Exception as e:  # noqa: BLE001 — defensive
            report.skipped.append(f"{type(e).__name__}: {str(e)[:120]}")
    return report


def _csl_entry_to_source(entry: dict) -> Source | None:
    if not isinstance(entry, dict):
        return None
    title = (entry.get("title") or "").strip()
    if not title:
        return None
    raw_type = entry.get("type") or "article-journal"
    src_type = _CSL_TYPE_MAP.get(raw_type, SourceType.primary_paper)

    authors: list[str] = []
    for a in entry.get("author") or []:
        if not isinstance(a, dict):
            continue
        if "literal" in a:
            authors.append(a["literal"])
            continue
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        if family and given:
            authors.append(f"{family}, {given}")
        elif family:
            authors.append(family)

    year: int | None = None
    issued = entry.get("issued") or {}
    parts = (issued.get("date-parts") or [[None]])[0]
    if parts and parts[0] is not None:
        try:
            year = int(parts[0])
        except (TypeError, ValueError):
            year = None

    citation = Citation(
        authors=authors,
        year=year,
        title=title,
        container=entry.get("container-title") or None,
        volume=str(entry["volume"]) if entry.get("volume") else None,
        issue=str(entry["issue"]) if entry.get("issue") else None,
        pages=entry.get("page") or None,
        doi=entry.get("DOI") or None,
        url=entry.get("URL") or None,
    )
    return _build_source(
        source_id=_choose_id(entry.get("id"), citation),
        type_=src_type,
        citation=citation,
    )


# ─── BibTeX ──────────────────────────────────────────


# Map BibTeX entry types (lowercased) to our Source type.
_BIB_TYPE_MAP = {
    "article": SourceType.primary_paper,
    "inproceedings": SourceType.primary_paper,
    "conference": SourceType.primary_paper,
    "incollection": SourceType.primary_paper,
    "book": SourceType.primary_paper,
    "inbook": SourceType.primary_paper,
    "phdthesis": SourceType.primary_paper,
    "mastersthesis": SourceType.primary_paper,
    "techreport": SourceType.report,
    "manual": SourceType.report,
    "online": SourceType.web_page,
    "misc": SourceType.note,
    "unpublished": SourceType.note,
}


# An @entry header: @type{key,
# Captures the position of the opening brace itself so we can find
# the matching closer correctly.
_BIB_HEADER_RE = re.compile(
    r"@(?P<type>[A-Za-z]+)\s*(?P<brace>\{)\s*(?P<key>[^,\s]+)\s*,",
)


def _import_bibtex(text: str) -> ImportReport:
    """Defensive BibTeX parser. Handles the canonical ``@article{key,
    field = {value}, field = "value", ...}`` form. Doesn't try to be
    bibtex-perfect — drops entries it can't parse."""
    report = ImportReport(detected_format="bib")
    cursor = 0
    while True:
        m = _BIB_HEADER_RE.search(text, cursor)
        if not m:
            break
        entry_type = m.group("type").lower()
        entry_key = m.group("key").strip()
        brace_pos = m.start("brace")
        # Find the matching closing brace from the entry's opening brace.
        body_start = m.end()  # position right after the comma
        body_end = _find_matching_brace(text, brace_pos)
        if body_end is None:
            report.warnings.append(
                f"Unbalanced braces around @{entry_type}{{{entry_key}}}; skipped."
            )
            cursor = m.end()
            continue
        body = text[body_start:body_end]
        try:
            fields = _parse_bib_fields(body)
            src = _bib_entry_to_source(entry_type, entry_key, fields)
            if src is not None:
                report.sources.append(src)
            else:
                report.skipped.append(f"{entry_type}{{{entry_key}}}")
        except Exception as e:  # noqa: BLE001
            report.skipped.append(
                f"{entry_type}{{{entry_key}}}: {type(e).__name__}: "
                f"{str(e)[:120]}"
            )
        cursor = body_end + 1
    return report


def _find_matching_brace(text: str, open_pos: int) -> int | None:
    """Find the position of the closing '}' that matches the '{' at
    ``open_pos``. Returns None if unbalanced. Doesn't track quotes —
    BibTeX field values can use either {braces} or "quotes" but the
    outer entry braces are always real."""
    depth = 0
    i = open_pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _parse_bib_fields(body: str) -> dict[str, str]:
    """Walk a BibTeX entry body, extracting ``field = value`` pairs.
    Handles brace-balanced and quoted values."""
    out: dict[str, str] = {}
    i = 0
    n = len(body)
    while i < n:
        # Skip whitespace + commas.
        while i < n and body[i] in " \t\n\r,":
            i += 1
        if i >= n:
            break
        # Read field name.
        name_start = i
        while i < n and (body[i].isalnum() or body[i] in "-_"):
            i += 1
        name = body[name_start:i].strip().lower()
        if not name:
            break
        # Skip to '='.
        while i < n and body[i] != "=":
            i += 1
        if i >= n:
            break
        i += 1  # skip '='
        # Skip whitespace.
        while i < n and body[i] in " \t\n\r":
            i += 1
        if i >= n:
            break
        # Read value (braced, quoted, or bare).
        value, consumed = _read_bib_value(body[i:])
        i += consumed
        out[name] = _clean_bib_value(value)
    return out


def _read_bib_value(s: str) -> tuple[str, int]:
    """Read one BibTeX field value from the start of ``s``. Returns
    ``(value, consumed_count)`` where consumed_count includes the
    enclosing braces / quotes."""
    if not s:
        return "", 0
    if s[0] == "{":
        depth = 0
        for i, ch in enumerate(s):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[1:i], i + 1
        return s[1:], len(s)
    if s[0] == '"':
        for i in range(1, len(s)):
            if s[i] == '"' and s[i - 1] != "\\":
                return s[1:i], i + 1
        return s[1:], len(s)
    # Bare token (number, single word).
    m = re.match(r"[^\s,}]+", s)
    if m:
        return m.group(0), len(m.group(0))
    return "", 0


def _clean_bib_value(s: str) -> str:
    """Strip braces left over from nested groups, collapse whitespace,
    unescape common BibTeX escapes."""
    s = re.sub(r"\s+", " ", s).strip()
    # Unescape \&, \%, \_, \$ etc.
    s = re.sub(r"\\([&%$#_{}~^])", r"\1", s)
    # Strip leftover braces around a single word/phrase.
    while s.startswith("{") and s.endswith("}"):
        # Only strip if the braces are a balanced pair with no nesting.
        if s.count("{") == 1 and s.count("}") == 1:
            s = s[1:-1].strip()
        else:
            break
    return s


def _bib_entry_to_source(
    entry_type: str, key: str, fields: dict[str, str],
) -> Source | None:
    title = fields.get("title", "").strip()
    if not title:
        return None
    src_type = _BIB_TYPE_MAP.get(entry_type, SourceType.primary_paper)
    authors_raw = fields.get("author", "")
    authors = [a.strip() for a in re.split(r"\s+and\s+", authors_raw) if a.strip()]
    year = None
    if "year" in fields:
        try:
            year = int(re.search(r"\d{4}", fields["year"]).group(0))
        except (AttributeError, ValueError):
            year = None
    pages = fields.get("pages")
    if pages:
        # BibTeX uses ``--`` for page ranges; normalise to single hyphen.
        pages = pages.replace("--", "-")

    citation = Citation(
        authors=authors,
        year=year,
        title=title,
        container=(
            fields.get("journal") or fields.get("booktitle")
            or fields.get("publisher") or None
        ),
        volume=fields.get("volume") or None,
        issue=fields.get("number") or fields.get("issue") or None,
        pages=pages or None,
        doi=fields.get("doi") or None,
        url=fields.get("url") or None,
    )
    return _build_source(source_id=key, type_=src_type, citation=citation)


# ─── RIS ─────────────────────────────────────────────


_RIS_TYPE_MAP = {
    "JOUR": SourceType.primary_paper,
    "CONF": SourceType.primary_paper,
    "CHAP": SourceType.primary_paper,
    "BOOK": SourceType.primary_paper,
    "THES": SourceType.primary_paper,
    "MGZN": SourceType.primary_paper,
    "NEWS": SourceType.primary_paper,
    "RPRT": SourceType.report,
    "DATA": SourceType.dataset,
    "ELEC": SourceType.web_page,
    "WEB": SourceType.web_page,
    "GEN": SourceType.note,
}


def _import_ris(text: str) -> ImportReport:
    report = ImportReport(detected_format="ris")
    # Records separated by ``ER  -``.
    records = re.split(r"\n\s*ER\s+-.*\n?", text)
    for raw in records:
        if not raw.strip():
            continue
        try:
            src = _ris_record_to_source(raw)
            if src is not None:
                report.sources.append(src)
            else:
                report.skipped.append(raw[:120])
        except Exception as e:  # noqa: BLE001
            report.skipped.append(f"{type(e).__name__}: {str(e)[:120]}")
    return report


def _ris_record_to_source(raw: str) -> Source | None:
    fields: dict[str, list[str]] = {}
    for line in raw.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])\s\s-\s?(.*)$", line)
        if not m:
            continue
        tag, value = m.group(1), m.group(2).strip()
        fields.setdefault(tag, []).append(value)

    title = ""
    for tag in ("TI", "T1", "BT", "CT"):
        if fields.get(tag):
            title = fields[tag][0]
            break
    if not title:
        return None

    raw_type = (fields.get("TY") or ["GEN"])[0]
    src_type = _RIS_TYPE_MAP.get(raw_type.upper(), SourceType.primary_paper)
    authors = fields.get("AU", []) + fields.get("A1", [])

    year = None
    for tag in ("PY", "Y1", "DA"):
        if fields.get(tag):
            try:
                year = int(re.search(r"\d{4}", fields[tag][0]).group(0))
                break
            except (AttributeError, ValueError):
                pass

    container = ""
    for tag in ("JO", "JF", "JA", "T2"):
        if fields.get(tag):
            container = fields[tag][0]
            break

    pages = None
    sp = (fields.get("SP") or [None])[0]
    ep = (fields.get("EP") or [None])[0]
    if sp and ep:
        pages = f"{sp}-{ep}"
    elif sp:
        pages = sp
    elif fields.get("M2"):
        pages = fields["M2"][0]

    citation = Citation(
        authors=authors,
        year=year,
        title=title,
        container=container or None,
        volume=(fields.get("VL") or [None])[0],
        issue=(fields.get("IS") or [None])[0],
        pages=pages,
        doi=(fields.get("DO") or fields.get("DOI") or [None])[0],
        url=(fields.get("UR") or fields.get("L1") or [None])[0],
    )
    source_id = (
        (fields.get("ID") or [""])[0]
        or _choose_id(None, citation)
    )
    return _build_source(source_id=source_id, type_=src_type, citation=citation)


# ─── helpers ────────────────────────────────────────


def _build_source(
    *, source_id: str, type_: SourceType, citation: Citation,
) -> Source:
    return Source(
        source_id=_normalise_id(source_id),
        type=type_,
        citation=citation,
        passages=[],
        metadata=SourceMetadata(
            date_added=datetime.now(timezone.utc),
            file_path="(imported)",
            hash="imported",  # no underlying file to hash
        ),
    )


def _normalise_id(raw: str) -> str:
    """Convert a raw id into a Lattice-friendly slug."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", (raw or "").strip()).strip("_")
    return cleaned.lower() or "imported"


def _choose_id(raw_id: Any, citation: Citation) -> str:
    """Pick a citekey: prefer the import's id; fall back to surname_year."""
    if raw_id:
        return str(raw_id)
    if citation.authors and citation.year:
        first = citation.authors[0]
        surname = first.split(",", 1)[0].strip() if "," in first else first.split()[-1]
        return f"{surname}_{citation.year}".lower()
    if citation.year:
        return f"anon_{citation.year}"
    return "imported"


def _content_hash(src: Source) -> str:
    """Hash for dedup: (year, surname-of-first-author, normalised-title)."""
    c = src.citation
    surname = ""
    if c.authors:
        first = c.authors[0]
        surname = first.split(",", 1)[0].strip() if "," in first else first.split()[-1]
    payload = f"{c.year or 0}::{surname.lower()}::{(c.title or '').strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
