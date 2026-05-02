"""Citation matcher — link inline citations + footnotes to Sources.

Three resolution paths:

1. **Author + year matching** for parenthetical / narrative citations.
   Walks every Source in the project's source store and scores each
   candidate by surname + year overlap.
2. **Numeric matching** for [12]-style citations: index N in the
   bibliography list maps to the Nth source in store order, IF the
   bibliography entries can be associated 1:1 with Sources.
3. **Ibid / op. cit. resolution** for footnotes: walk footnotes in
   document order, propagating the most-recent distinct source to
   each ``Ibid.`` entry; ``op. cit.`` requires the surname mentioned
   in the footnote text to match a previously-cited source.

Pure function over the inputs. Annotates each ``InlineCitation`` /
``FootnoteCitation`` in place with a ``source_id`` (or
``unresolved_reason``) and a ``match_confidence``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Sequence

from ..graph.models import (
    DocumentCitations,
    FootnoteCitation,
    InlineCitation,
    Source,
)


def match_citations(
    doc: DocumentCitations,
    sources: Sequence[Source],
) -> DocumentCitations:
    """Annotate ``doc.inline_citations`` and ``doc.footnotes`` with
    their resolved ``source_id`` (or ``unresolved_reason``).

    Mutates the citations list in place; returns ``doc`` for chaining.
    Re-running on an already-matched document is idempotent — re-runs
    the matcher fresh each time.
    """
    if not sources:
        for c in doc.inline_citations:
            c.source_id = None
            c.match_confidence = 0.0
            c.unresolved_reason = "no_sources_in_store"
        for f in doc.footnotes:
            f.source_id = None
            f.resolves_to_source_id = None
            f.unresolved_reason = "no_sources_in_store"
        return doc

    # Index sources by surname + year for fast author-date matching.
    surname_year_index = _build_surname_year_index(sources)
    # Numbered-list ordering for numeric matching.
    sources_in_order = list(sources)

    for c in doc.inline_citations:
        _match_inline(c, surname_year_index, sources_in_order)

    _match_footnotes(doc.footnotes, surname_year_index)

    # Counts summary refresh.
    matched = sum(1 for c in doc.inline_citations if c.source_id)
    doc.counts["inline_matched"] = matched
    doc.counts["inline_unmatched"] = (
        len(doc.inline_citations) - matched
    )
    fn_matched = sum(
        1 for f in doc.footnotes if f.resolves_to_source_id or f.source_id
    )
    doc.counts["footnotes_matched"] = fn_matched
    doc.counts["footnotes_unmatched"] = len(doc.footnotes) - fn_matched

    return doc


# ─── inline matching ────────────────────────────────


def _match_inline(
    c: InlineCitation,
    surname_year_index: dict[tuple[str, int], list[Source]],
    sources_in_order: list[Source],
) -> None:
    """Resolve a single inline citation. Sets ``source_id``,
    ``match_confidence``, and ``unresolved_reason`` on the citation."""
    # Numeric: position N in source list.
    if c.cited_number is not None:
        idx = c.cited_number - 1  # 1-indexed in citation, 0-indexed in list
        if 0 <= idx < len(sources_in_order):
            c.source_id = sources_in_order[idx].source_id
            c.match_confidence = 0.7  # positional match — moderate confidence
            c.unresolved_reason = None
            return
        c.source_id = None
        c.match_confidence = 0.0
        c.unresolved_reason = (
            f"numeric_out_of_range:{c.cited_number}_of_{len(sources_in_order)}"
        )
        return

    # Author-date: need at least one cited author + a year.
    if not c.cited_authors:
        c.source_id = None
        c.match_confidence = 0.0
        c.unresolved_reason = "no_cited_authors"
        return

    primary = _normalise_surname(c.cited_authors[0])
    year = c.cited_year

    if year is None:
        # Try matching by primary author alone — a popular practice in
        # some humanities styles, accepted with reduced confidence.
        candidates: list[Source] = []
        for (surname, _y), src_list in surname_year_index.items():
            if surname == primary:
                candidates.extend(src_list)
        if len(candidates) == 1:
            c.source_id = candidates[0].source_id
            c.match_confidence = 0.5
            c.unresolved_reason = None
            return
        c.source_id = None
        c.match_confidence = 0.0
        c.unresolved_reason = (
            "no_year_and_ambiguous_author" if candidates
            else "no_year_and_unknown_author"
        )
        return

    # Exact (surname, year) match.
    candidates = surname_year_index.get((primary, year), [])
    if len(candidates) == 1:
        c.source_id = candidates[0].source_id
        c.match_confidence = 0.95
        c.unresolved_reason = None
        return
    if len(candidates) > 1:
        # Disambiguate by looking at additional cited authors.
        if len(c.cited_authors) > 1:
            secondary = _normalise_surname(c.cited_authors[1])
            for src in candidates:
                src_surnames = _source_surnames(src)
                if len(src_surnames) >= 2 and src_surnames[1] == secondary:
                    c.source_id = src.source_id
                    c.match_confidence = 0.9
                    c.unresolved_reason = None
                    return
        c.source_id = None
        c.match_confidence = 0.0
        c.unresolved_reason = (
            f"ambiguous_author_year:{len(candidates)}_candidates"
        )
        return

    # No exact match — try a forgiving year match (±1) for late-stage
    # 'in press' / preprint shifts.
    near = []
    for offset in (-1, 1):
        near.extend(surname_year_index.get((primary, year + offset), []))
    if len(near) == 1:
        c.source_id = near[0].source_id
        c.match_confidence = 0.6
        c.unresolved_reason = None
        return

    c.source_id = None
    c.match_confidence = 0.0
    c.unresolved_reason = f"no_match:{primary}_{year}"


# ─── footnote matching ──────────────────────────────


def _match_footnotes(
    footnotes: list[FootnoteCitation],
    surname_year_index: dict[tuple[str, int], list[Source]],
) -> None:
    """Walk footnotes in document order. A "full citation" footnote
    that mentions a known surname + year sets the running source; an
    Ibid. inherits it; an op-cit looks back for the most recent
    citation matching the named surname."""
    last_full_source_id: str | None = None
    # surname → most recent source_id seen for that surname (for op cit)
    surname_to_last_source: dict[str, str] = {}

    for fn in footnotes:
        if fn.is_ibid:
            if last_full_source_id:
                fn.resolves_to_source_id = last_full_source_id
                fn.source_id = None  # ibid doesn't OWN the citation
                fn.unresolved_reason = None
            else:
                fn.unresolved_reason = "ibid_with_no_preceding_citation"
            continue

        if fn.is_op_cit:
            # Find a surname mentioned in the footnote text and look up.
            surname = _surname_from_text(fn.raw_text)
            if surname and surname in surname_to_last_source:
                fn.resolves_to_source_id = surname_to_last_source[surname]
                fn.source_id = None
                fn.unresolved_reason = None
            else:
                fn.unresolved_reason = (
                    f"op_cit_unresolved:{surname or 'no_surname_found'}"
                )
            continue

        if fn.is_full_citation:
            # Try to match by surname + year extracted from the body.
            surname, year = _surname_year_from_text(fn.raw_text)
            if surname and year:
                src_list = surname_year_index.get((surname, year), [])
                if len(src_list) == 1:
                    fn.source_id = src_list[0].source_id
                    fn.resolves_to_source_id = src_list[0].source_id
                    fn.unresolved_reason = None
                    last_full_source_id = src_list[0].source_id
                    surname_to_last_source[surname] = src_list[0].source_id
                    continue
                if len(src_list) > 1:
                    fn.unresolved_reason = "ambiguous_full_citation"
                    continue
            fn.unresolved_reason = "full_citation_no_match"
            continue

        # Non-citation prose footnote — nothing to resolve.
        fn.unresolved_reason = "non_citation_footnote"


# ─── helpers ────────────────────────────────────────


def _build_surname_year_index(
    sources: Sequence[Source],
) -> dict[tuple[str, int], list[Source]]:
    out: dict[tuple[str, int], list[Source]] = {}
    for src in sources:
        year = src.citation.year
        if year is None:
            continue
        for surname in _source_surnames(src):
            key = (surname, year)
            out.setdefault(key, []).append(src)
    return out


def _source_surnames(src: Source) -> list[str]:
    """Extract normalised surnames from a Source's authors list.

    ``Smith, J.`` → ``smith``
    ``John A. Smith`` → ``smith``
    ``van der Berg`` → ``van_der_berg``
    """
    out: list[str] = []
    for raw in src.citation.authors or []:
        out.append(_normalise_surname(_extract_surname(raw)))
    return out


def _extract_surname(raw: str) -> str:
    """Pull the surname out of a "Surname, Given" or "Given Surname"
    formatted name."""
    raw = raw.strip()
    if "," in raw:
        return raw.split(",", 1)[0].strip()
    parts = raw.rsplit(" ", 1)
    return parts[-1].strip() if parts else raw


def _normalise_surname(raw: str) -> str:
    """Lowercase + strip diacritics + collapse whitespace + replace
    spaces with underscores so ``Van der Berg`` and ``van der berg``
    and ``Van Der Berg`` all collapse to the same key."""
    if not raw:
        return ""
    normalised = unicodedata.normalize("NFKD", raw)
    ascii_only = "".join(c for c in normalised if not unicodedata.combining(c))
    return re.sub(r"\s+", "_", ascii_only.lower().strip())


_SURNAME_YEAR_RE = re.compile(
    r"\b([A-Z][A-Za-zÀ-ſ’'\-]+)\b[^()]*?\b((?:19|20|21)\d{2})\b"
)


def _surname_year_from_text(text: str) -> tuple[str | None, int | None]:
    """Pull the first surname + year combo out of free-text. Used by
    the footnote matcher to identify which source a full-citation
    footnote refers to."""
    m = _SURNAME_YEAR_RE.search(text)
    if not m:
        return None, None
    return _normalise_surname(m.group(1)), int(m.group(2))


_SURNAME_RE = re.compile(r"\b([A-Z][A-Za-zÀ-ſ’'\-]+)\b")


def _surname_from_text(text: str) -> str | None:
    """Find the first surname-shaped word in free text. Used for
    op-cit resolution where there's no year to pin against."""
    m = _SURNAME_RE.search(text)
    return _normalise_surname(m.group(1)) if m else None
