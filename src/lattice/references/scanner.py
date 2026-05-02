"""Citation scanner — extract every citation location from a document.

Three jobs, in order:

1. **Detect the citation system** — author-date (Smith, 2020), numeric
   ([12] / (12)), or footnote (¹ marker with content elsewhere). Mixed
   systems are flagged but tolerated.
2. **Extract inline citations** — every parenthetical, narrative, and
   numeric mark in the body, with paragraph/char positions so the
   rewriter can replace the span without re-parsing.
3. **Extract footnotes** — both the markers in the body and the
   footnote bodies themselves; classify each as Ibid. / op. cit. /
   full citation / non-citation prose.
4. **Isolate the bibliography section** — return the raw entry strings
   so the existing ``enricher.reference_extraction`` can parse them.

Pure regex + heuristics — no LLM calls. Defensive parsing: bad spans
are dropped, not raised. Re-runs are idempotent against the same input.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import (
    CitationLocationKind,
    CitationSystem,
    DocumentCitations,
    FootnoteCitation,
    InlineCitation,
)


# ─── regex patterns ──────────────────────────────────


# Author-date inline citations.
#
# Parenthetical: (Smith, 2020) · (Smith and Jones, 2020) ·
#                (Smith et al., 2020) · (Smith 2020) ·
#                (Smith, 2020, p. 47) · (Smith, 2020a)
#
# We accept comma-separated and ``and`` / ``&`` separators between
# names, optional comma before the year, and optional pinpoint after.
_AUTHOR = r"(?:[A-Z][A-Za-zÀ-ſ’'\-]+(?:\s+[A-Z][A-Za-zÀ-ſ’'\-]+)*)"
# A "name group" is one author, or "Smith and Jones", or "Smith et al."
_NAME_GROUP = (
    rf"{_AUTHOR}(?:\s+et\s+al\.?|\s*&\s*{_AUTHOR}|\s+and\s+{_AUTHOR})?"
)
# Year with optional letter suffix (Smith 2020a, 2020b)
_YEAR = r"(?P<year>(?:19|20|21)\d{2}[a-z]?|n\.?\s*d\.?|forthcoming|in\s+press)"
_PINPOINT = r"(?:\s*,?\s*(?P<pinpoint>(?:p+\.?|pp\.?|ch(?:ap)?\.?|sec(?:t)?\.?|§)\s*[\dxiv–—\-,–\s]+))?"

# A SINGLE author-date entry inside parens — used to split
# multi-citation blocks like (Smith, 2020; Lee, 2019).
_AUTHOR_DATE_ENTRY = re.compile(
    rf"(?P<authors>{_NAME_GROUP})\s*,?\s*{_YEAR}{_PINPOINT}",
    re.UNICODE,
)

# Outer parenthetical block. Captures the full inside of the parens
# so we can split on ``;`` for multi-source citations.
_PARENTHETICAL = re.compile(
    r"\((?P<body>"
    rf"{_NAME_GROUP}\s*,?\s*{_YEAR}"
    r"(?:[^()]*?)"
    r")\)",
    re.UNICODE,
)

# Narrative form: "Smith (2020) argues" / "Smith and Jones (2020a)"
_NARRATIVE = re.compile(
    rf"(?P<authors>{_NAME_GROUP})\s+\({_YEAR}{_PINPOINT}\)",
    re.UNICODE,
)

# Numeric inline: "[12]" or "(12)" or "[12, 13]" or "[12-15]"
_NUMERIC_BRACKET = re.compile(r"\[(?P<body>\d+(?:\s*[,–—\-]\s*\d+)*)\]")
_NUMERIC_PAREN = re.compile(r"(?<![A-Za-z])\((?P<body>\d+(?:\s*[,–—\-]\s*\d+)*)\)(?![A-Za-z])")

# Footnote markers in body: superscript digit / dagger / asterisk.
_FOOTNOTE_MARKER = re.compile(
    r"(?P<marker>"
    r"[¹²³⁰-⁹]+"   # ¹²³⁴⁵⁶⁷⁸⁹⁰
    r"|\^\d+"                                # ^1, ^2 (markdown footnote refs)
    r"|\[\^?\d+\]"                           # [1] or [^1] for footnotes
    r")"
)

# Footnote body line in markdown: "[^1]: footnote text"
_FOOTNOTE_BODY = re.compile(
    r"^\[\^(?P<id>\d+)\]:\s*(?P<text>.+?)$",
    re.MULTILINE,
)

# Bibliography section heading.
_BIBLIO_HEADING = re.compile(
    r"(?:^|\n)\s*(?:#{1,4}\s*)?(?:references|bibliography|works\s+cited"
    r"|literature\s+cited|reference\s+list|cited\s+works)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Ibid / Idem / op. cit. detection (case-insensitive, word-boundaried).
_IBID_RE = re.compile(r"\b(ibid|idem)\b\.?", re.IGNORECASE)
_OP_CIT_RE = re.compile(r"\bop\.?\s*cit\.?", re.IGNORECASE)


# ─── public entry point ──────────────────────────────


def scan_document(text: str, *, project_name: str, document_path: str) -> DocumentCitations:
    """Walk ``text`` and produce a populated ``DocumentCitations``.

    Pure function. ``text`` is the markdown document; if a bibliography
    section is present it gets isolated and its entries are returned in
    ``bibliography_entries`` for downstream parsing by
    ``enricher.reference_extraction``.
    """
    body, biblio_entries = _split_bibliography(text)
    paragraphs = _paragraphs_with_offsets(body)

    inline: list[InlineCitation] = []
    for para_idx, (para_offset, para_text) in enumerate(paragraphs):
        inline.extend(_extract_inline_in_paragraph(
            para_idx, para_offset, para_text,
        ))

    footnotes = _extract_footnotes(body)
    inline.extend(_extract_footnote_markers(body, paragraphs))

    detected = _detect_system(inline, footnotes)

    counts = _summarise_counts(inline, footnotes, biblio_entries)

    return DocumentCitations(
        project_name=project_name,
        document_path=document_path,
        detected_system=detected,
        scanned_at=datetime.now(timezone.utc),
        inline_citations=inline,
        footnotes=footnotes,
        bibliography_entries=biblio_entries,
        counts=counts,
    )


# ─── bibliography isolation ──────────────────────────


def _split_bibliography(text: str) -> tuple[str, list[str]]:
    """Split ``text`` into ``(body, bibliography_entries)``.

    The bibliography section starts at the first heading whose title
    is one of the recognised reference-list names. Entries are split
    on blank lines OR on lines that start with a numeric / bullet
    marker (``[1]``, ``1.``, ``-``).
    """
    match = _BIBLIO_HEADING.search(text)
    if not match:
        return text, []
    body = text[: match.start()].rstrip()
    raw = text[match.end():].lstrip("\n")
    return body, _split_entries(raw)


def _split_entries(raw: str) -> list[str]:
    """Split the raw bibliography text into one entry per element.

    Handles four common formats (in order of preference):
    - numbered list ``1.`` / ``[1]``
    - bullet list ``- entry``
    - blank-line-separated paragraphs
    - single-newline-separated lines where each line starts with what
      looks like a citation (capital-letter surname). Catches the very
      common "no blank lines" bibliography format.
    """
    if not raw.strip():
        return []
    # Numbered list (bracketed or dotted) — each new number starts an entry.
    numbered = re.split(
        r"\n(?=\s*(?:\[\d+\]|\d+\.)\s+)", raw,
    )
    if len(numbered) > 1:
        return [_clean_entry(e) for e in numbered if _clean_entry(e)]
    # Bullet-list ``- entry``
    bulleted = re.split(r"\n(?=\s*-\s+)", raw)
    if len(bulleted) > 1:
        return [_clean_entry(e) for e in bulleted if _clean_entry(e)]
    # Blank-line separation.
    paras = [_clean_entry(e) for e in raw.split("\n\n") if _clean_entry(e)]
    if len(paras) > 1:
        return paras
    # Last resort: split single-newline runs where each new line looks
    # like the start of a citation (capitalised surname at line head,
    # followed by either comma, period, or another capitalised word).
    line_starts = re.split(
        r"\n(?=[A-Z][A-Za-zÀ-ſ’'\-]+(?:[,.]|\s+[A-Z]))", raw,
    )
    if len(line_starts) > 1:
        return [_clean_entry(e) for e in line_starts if _clean_entry(e)]
    return paras  # one entry, or empty


def _clean_entry(s: str) -> str:
    s = s.strip()
    # Strip leading numbering markers ``[1]`` / ``1.`` / ``- ``.
    s = re.sub(r"^\s*(?:\[\d+\]|\d+\.|-)\s+", "", s)
    return s.strip()


# ─── paragraph splitting ─────────────────────────────


def _paragraphs_with_offsets(text: str) -> list[tuple[int, str]]:
    """Split body text into paragraphs, returning (char_offset, text)
    tuples so inline-citation positions can be expressed as absolute
    document offsets."""
    out: list[tuple[int, str]] = []
    cursor = 0
    for raw in text.split("\n\n"):
        out.append((cursor, raw))
        cursor += len(raw) + 2  # +2 for the consumed "\n\n"
    return out


# ─── inline citation extraction ──────────────────────


def _extract_inline_in_paragraph(
    para_idx: int, para_offset: int, para_text: str,
) -> list[InlineCitation]:
    """Pull every author-date / narrative / numeric citation out of one
    paragraph. Spans are recorded as absolute document offsets."""
    out: list[InlineCitation] = []
    seen_spans: set[tuple[int, int]] = set()

    # Parenthetical author-date — possibly multi-source.
    for m in _PARENTHETICAL.finditer(para_text):
        span = (para_offset + m.start(), para_offset + m.end())
        if _overlaps(span, seen_spans):
            continue
        seen_spans.add(span)
        body = m.group("body")
        for entry in _split_multi_source(body):
            authors = _parse_authors(entry.get("authors", ""))
            year = _parse_year(entry.get("year"))
            out.append(InlineCitation(
                citation_id=_cid(),
                raw_text=m.group(0),
                kind=CitationLocationKind.parenthetical,
                paragraph_index=para_idx,
                char_start=span[0],
                char_end=span[1],
                cited_authors=authors,
                cited_year=year,
                pinpoint=entry.get("pinpoint"),
            ))

    # Narrative form — must NOT overlap with parenthetical spans.
    for m in _NARRATIVE.finditer(para_text):
        span = (para_offset + m.start(), para_offset + m.end())
        if _overlaps(span, seen_spans):
            continue
        seen_spans.add(span)
        out.append(InlineCitation(
            citation_id=_cid(),
            raw_text=m.group(0),
            kind=CitationLocationKind.narrative,
            paragraph_index=para_idx,
            char_start=span[0],
            char_end=span[1],
            cited_authors=_parse_authors(m.group("authors")),
            cited_year=_parse_year(m.group("year")),
            pinpoint=m.group("pinpoint"),
        ))

    # Numeric — bracketed first (less ambiguous), then bare-paren as a
    # fallback (only when the document looks numeric overall).
    for m in _NUMERIC_BRACKET.finditer(para_text):
        span = (para_offset + m.start(), para_offset + m.end())
        if _overlaps(span, seen_spans):
            continue
        seen_spans.add(span)
        for n in _parse_numeric_body(m.group("body")):
            out.append(InlineCitation(
                citation_id=_cid(),
                raw_text=m.group(0),
                kind=CitationLocationKind.numeric,
                paragraph_index=para_idx,
                char_start=span[0],
                char_end=span[1],
                cited_number=n,
            ))
    return out


def _split_multi_source(body: str) -> list[dict]:
    """Split ``Smith, 2020; Lee, 2019`` into per-entry dicts. Returns
    one dict per matched author-date combination; ``;`` is the
    canonical separator across styles."""
    parts = re.split(r"\s*;\s*", body)
    out: list[dict] = []
    for part in parts:
        m = _AUTHOR_DATE_ENTRY.search(part)
        if m:
            out.append({
                "authors": m.group("authors"),
                "year": m.group("year"),
                "pinpoint": m.group("pinpoint"),
            })
    return out


def _parse_authors(raw: str) -> list[str]:
    """Best-effort split of an author-name group into surnames.

    ``Smith`` → [``Smith``]
    ``Smith and Jones`` → [``Smith``, ``Jones``]
    ``Smith et al.`` → [``Smith``]   (et al. doesn't expand)
    ``Smith & Jones`` → [``Smith``, ``Jones``]
    """
    if not raw:
        return []
    cleaned = re.sub(r"\s+et\s+al\.?\s*", "", raw, flags=re.IGNORECASE).strip()
    # Split on " and " / " & " / "," (the comma case is rare in author-date).
    parts = re.split(r"\s*(?:&|\band\b|,)\s*", cleaned)
    return [p.strip() for p in parts if p.strip()]


def _parse_year(raw: str | None) -> int | None:
    if raw is None:
        return None
    m = re.match(r"((?:19|20|21)\d{2})", raw)
    if m:
        return int(m.group(1))
    return None


def _parse_numeric_body(body: str) -> list[int]:
    """Parse ``[12]`` / ``[12, 14]`` / ``[12-15]`` into a list of int.
    Ranges are expanded so each cited number gets its own entry."""
    out: list[int] = []
    for chunk in re.split(r"\s*,\s*", body):
        # Range
        rng = re.match(r"(\d+)\s*[–—\-]\s*(\d+)", chunk)
        if rng:
            a, b = int(rng.group(1)), int(rng.group(2))
            if 0 < b - a < 50:  # sanity cap
                out.extend(range(a, b + 1))
            continue
        single = re.match(r"\d+", chunk)
        if single:
            out.append(int(single.group(0)))
    return out


# ─── footnotes ───────────────────────────────────────


def _extract_footnotes(text: str) -> list[FootnoteCitation]:
    """Extract markdown-style footnote bodies from the text and
    classify each as Ibid. / op. cit. / full citation / prose."""
    out: list[FootnoteCitation] = []
    for m in _FOOTNOTE_BODY.finditer(text):
        body = m.group("text").strip()
        is_ibid = bool(_IBID_RE.search(body))
        is_opcit = bool(_OP_CIT_RE.search(body))
        is_full = _looks_like_full_citation(body) and not (is_ibid or is_opcit)
        out.append(FootnoteCitation(
            footnote_id=m.group("id"),
            raw_text=body,
            char_start=m.start(),
            char_end=m.end(),
            is_ibid=is_ibid,
            is_op_cit=is_opcit,
            is_full_citation=is_full,
            pinpoint=_extract_pinpoint(body),
        ))
    return out


def _extract_footnote_markers(
    body: str, paragraphs: list[tuple[int, str]],
) -> list[InlineCitation]:
    """The body-side markers (``[^1]``, superscripts) need their own
    InlineCitation entries so the rewriter knows where to slot the
    citation when converting between systems."""
    out: list[InlineCitation] = []
    for para_idx, (para_offset, para_text) in enumerate(paragraphs):
        for m in _FOOTNOTE_MARKER.finditer(para_text):
            marker = m.group("marker")
            # Skip markers that are footnote BODIES (lines that start
            # with ``[^N]:``) — the regex matches both, body extraction
            # handles those separately.
            line_start = para_text.rfind("\n", 0, m.start()) + 1
            if para_text[line_start:line_start + 4].startswith("[^") and ":" in para_text[line_start:line_start + 12]:
                continue
            n = _parse_marker_to_int(marker)
            out.append(InlineCitation(
                citation_id=_cid(),
                raw_text=marker,
                kind=CitationLocationKind.footnote_marker,
                paragraph_index=para_idx,
                char_start=para_offset + m.start(),
                char_end=para_offset + m.end(),
                cited_number=n,
            ))
    return out


def _parse_marker_to_int(marker: str) -> int | None:
    """Convert ``[^12]`` / ``^12`` / ``¹²`` to integer 12."""
    digits = "".join(
        ch for ch in marker
        if ch.isdigit()
        or "⁰" <= ch <= "⁹"
        or ch in "¹²³"
    )
    # Map superscripts to ASCII digits.
    super_map = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
    digits = digits.translate(super_map)
    digits = digits.replace("¹", "1").replace("²", "2").replace("³", "3")
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _looks_like_full_citation(text: str) -> bool:
    """Heuristic: a footnote that contains a year + a comma is probably
    a full citation. Cheap pre-filter for downstream parsing."""
    if not re.search(r"(?:19|20|21)\d{2}", text):
        return False
    if "," not in text and "(" not in text:
        return False
    return len(text) >= 40


def _extract_pinpoint(text: str) -> str | None:
    """Pull a ``p. 47`` / ``ch. 3`` style pinpoint out of free text."""
    m = re.search(
        r"\b(?:p+\.?|pp\.?|ch(?:ap)?\.?|sec(?:t)?\.?|§)\s*[\dxiv–—\-,–\s]+",
        text,
        re.IGNORECASE,
    )
    return m.group(0).strip() if m else None


# ─── system detection ────────────────────────────────


def _detect_system(
    inline: list[InlineCitation], footnotes: list[FootnoteCitation],
) -> CitationSystem:
    """Classify the overall citation system used.

    A document with > 60% of one kind is that kind; otherwise it's
    ``mixed``. An empty document is ``unknown``."""
    if not inline and not footnotes:
        return CitationSystem.unknown
    counts = {
        CitationSystem.author_date: 0,
        CitationSystem.numeric: 0,
        CitationSystem.footnote: 0,
    }
    for c in inline:
        if c.kind == CitationLocationKind.parenthetical:
            counts[CitationSystem.author_date] += 1
        elif c.kind == CitationLocationKind.narrative:
            counts[CitationSystem.author_date] += 1
        elif c.kind == CitationLocationKind.numeric:
            counts[CitationSystem.numeric] += 1
        elif c.kind == CitationLocationKind.footnote_marker:
            counts[CitationSystem.footnote] += 1
    # If footnote bodies exist, they reinforce the footnote signal.
    if footnotes:
        counts[CitationSystem.footnote] += len(footnotes)

    total = sum(counts.values())
    if total == 0:
        return CitationSystem.unknown
    top, top_count = max(counts.items(), key=lambda kv: kv[1])
    if top_count / total > 0.6:
        return top
    return CitationSystem.mixed


# ─── helpers ─────────────────────────────────────────


def _overlaps(span: tuple[int, int], seen: set[tuple[int, int]]) -> bool:
    a, b = span
    for c, d in seen:
        if a < d and c < b:
            return True
    return False


def _summarise_counts(
    inline: list[InlineCitation],
    footnotes: list[FootnoteCitation],
    biblio: list[str],
) -> dict[str, int]:
    by_kind: dict[str, int] = {}
    for c in inline:
        by_kind[c.kind.value] = by_kind.get(c.kind.value, 0) + 1
    return {
        "inline_total": len(inline),
        "inline_parenthetical": by_kind.get("parenthetical", 0),
        "inline_narrative": by_kind.get("narrative", 0),
        "inline_numeric": by_kind.get("numeric", 0),
        "inline_footnote_marker": by_kind.get("footnote_marker", 0),
        "footnotes_total": len(footnotes),
        "footnotes_ibid": sum(1 for f in footnotes if f.is_ibid),
        "footnotes_op_cit": sum(1 for f in footnotes if f.is_op_cit),
        "footnotes_full_citation": sum(1 for f in footnotes if f.is_full_citation),
        "bibliography_entries": len(biblio),
    }


def _cid() -> str:
    return f"ic.{uuid.uuid4().hex[:8]}"


# ─── persistence helper ──────────────────────────────


def save_document_citations(project_path: Path, doc: DocumentCitations) -> Path:
    """Write ``doc`` to ``.lattice/document_citations.json`` and return
    the path."""
    target = project_path / ".lattice" / "document_citations.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    return target


def load_document_citations(project_path: Path) -> DocumentCitations | None:
    """Load ``.lattice/document_citations.json`` if present."""
    p = project_path / ".lattice" / "document_citations.json"
    if not p.exists():
        return None
    return DocumentCitations.model_validate_json(p.read_text(encoding="utf-8"))
