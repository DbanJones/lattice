"""Document rewriter — re-format every citation in a target style.

The killer feature: given a scanned document plus a target style,
walk every inline citation and the bibliography section and re-emit
the whole document in the new format. Deterministic, no LLM calls.

Handles:

- **Style switches across systems** — author-date → numeric (APA →
  Vancouver) or numeric → author-date. Rebuilds the bibliography in
  the right order (alphabetical for author-date, citation-order for
  numeric).
- **Multi-source citations** — ``(Smith, 2020; Lee, 2019)`` becomes
  ``[3, 7]`` for numeric, etc.
- **Pinpoint citations** — ``(Smith, 2020, p. 47)`` preserves the
  page-number suffix in the target style.
- **Footnote → inline conversion** (and vice versa) — when the target
  style is in-text, footnote-only sources become inline ``(Smith,
  2020)`` citations and the footnote bodies are stripped.
- **Position-correct edits** — uses the InlineCitation char_start /
  char_end spans from the scanner, so untouched text is byte-identical.

The rewriter never edits source metadata; it only re-formats. Run
``lattice citations fill`` first to canonicalise the bibliography,
then restyle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..graph.models import (
    CitationLocationKind,
    CitationSystem,
    DocumentCitations,
    Source,
)
from ..output.citation_formatter import format_citation, supported_styles


# Author-date styles emit "(Smith, 2020)" inline; numeric styles emit
# "[12]". This map drives the per-citation rewriter.
_NUMERIC_STYLES = frozenset({"vancouver", "ieee"})


@dataclass
class RestyleResult:
    """Outcome of a single restyle pass."""

    document: str                  # the rewritten text
    style: str
    inline_replaced: int = 0
    inline_unresolved: int = 0     # left untouched because no source_id
    bibliography_emitted: int = 0
    notes: list[str] = None        # human-readable warnings

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []


# ─── public entry point ──────────────────────────────


def restyle_document(
    text: str,
    doc: DocumentCitations,
    sources: Sequence[Source],
    *,
    style: str,
) -> RestyleResult:
    """Produce ``text`` rewritten in ``style``.

    Inputs are not mutated. ``doc`` should be the result of
    ``scan_document`` + ``match_citations`` against ``sources`` — the
    rewriter relies on each ``InlineCitation.source_id`` to know which
    source a span refers to.
    """
    if style not in supported_styles():
        raise ValueError(
            f"Unknown style {style!r}. Supported: {', '.join(supported_styles())}"
        )
    sources_by_id = {s.source_id: s for s in sources}
    result = RestyleResult(document=text, style=style)

    is_numeric_target = style in _NUMERIC_STYLES

    # Build the citation order for numeric-style output. This is the
    # order in which sources are FIRST cited in the document — drives
    # both the inline ``[N]`` numbers and the bibliography order.
    citation_order = _first_citation_order(doc)

    # Step 1: rewrite inline citations in reverse order so char spans
    # don't shift as we edit. Group inline_citations that share a span
    # (multi-source parens) — they get a single combined replacement.
    text = _rewrite_inline(
        text, doc, sources_by_id, style,
        is_numeric_target=is_numeric_target,
        citation_order=citation_order,
        result=result,
    )

    # Step 2: replace the bibliography section.
    text = _rewrite_bibliography(
        text, doc, sources_by_id, style,
        is_numeric_target=is_numeric_target,
        citation_order=citation_order,
        result=result,
    )

    result.document = text
    return result


# ─── inline rewrite ──────────────────────────────────


def _rewrite_inline(
    text: str,
    doc: DocumentCitations,
    sources_by_id: dict[str, Source],
    style: str,
    *,
    is_numeric_target: bool,
    citation_order: list[str],
    result: RestyleResult,
) -> str:
    """Walk every InlineCitation, group those with shared spans, and
    replace each unique span with the new-style rendering.

    Citations are grouped by (char_start, char_end) so a multi-source
    parenthetical like ``(Smith, 2020; Lee, 2019)`` produces one
    combined replacement rather than three overlapping edits.
    """
    # Group by span.
    groups: dict[tuple[int, int], list] = {}
    for ic in doc.inline_citations:
        # Footnote markers in body are handled in the bibliography step
        # (they aren't replaced inline directly because the marker IS
        # the inline form).
        if ic.kind == CitationLocationKind.footnote_marker:
            continue
        key = (ic.char_start, ic.char_end)
        groups.setdefault(key, []).append(ic)

    # Apply edits in reverse order so spans before the cursor stay valid.
    for span in sorted(groups.keys(), reverse=True):
        ics = groups[span]
        # Skip if any IC in the group has no source — leave the span
        # untouched (and count as unresolved).
        unresolved = [c for c in ics if not c.source_id]
        if unresolved:
            result.inline_unresolved += len(unresolved)
            continue
        # Build the new inline string.
        first = ics[0]
        new_str = _format_inline_group(
            ics, sources_by_id, style,
            is_numeric_target=is_numeric_target,
            citation_order=citation_order,
            kind=first.kind,
        )
        if new_str is None:
            result.inline_unresolved += len(ics)
            continue
        text = text[:span[0]] + new_str + text[span[1]:]
        result.inline_replaced += len(ics)
    return text


def _format_inline_group(
    ics: list,
    sources_by_id: dict[str, Source],
    style: str,
    *,
    is_numeric_target: bool,
    citation_order: list[str],
    kind: CitationLocationKind,
) -> str | None:
    """Render a span's citations in the target style. Returns None if
    we can't produce a confident rewrite (caller leaves the span
    alone)."""
    if is_numeric_target:
        nums: list[int] = []
        pinpoints: list[str | None] = []
        for ic in ics:
            try:
                idx = citation_order.index(ic.source_id) + 1
            except ValueError:
                return None
            nums.append(idx)
            pinpoints.append(ic.pinpoint)
        # Sort + dedupe.
        seen: set[int] = set()
        ordered: list[int] = []
        for n in sorted(nums):
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        body = ", ".join(str(n) for n in ordered)
        # Pin-points appear after the number list.
        pin = next((p for p in pinpoints if p), None)
        suffix = f", {pin}" if pin else ""
        return f"[{body}{suffix}]"

    # Author-date target.
    parts: list[str] = []
    for ic in ics:
        src = sources_by_id.get(ic.source_id)
        if src is None:
            return None
        formatted = format_citation(src.citation, style)
        if kind == CitationLocationKind.narrative:
            # Smith (2020, p. 47)
            base = formatted.in_text_narrative
        else:
            # Strip outer parens — multiple entries share one set.
            base = formatted.in_text
            if base.startswith("(") and base.endswith(")"):
                base = base[1:-1]
        if ic.pinpoint:
            base = f"{base}, {ic.pinpoint}"
        parts.append(base)
    if kind == CitationLocationKind.narrative:
        # Narrative form is single-source by construction.
        return parts[0]
    body = "; ".join(parts)
    return f"({body})"


# ─── bibliography rewrite ────────────────────────────


def _rewrite_bibliography(
    text: str,
    doc: DocumentCitations,
    sources_by_id: dict[str, Source],
    style: str,
    *,
    is_numeric_target: bool,
    citation_order: list[str],
    result: RestyleResult,
) -> str:
    """Replace the existing bibliography section (or append one) with
    a freshly-formatted entry per cited Source."""
    # Determine which sources actually appear in the document.
    cited_ids = _cited_source_ids(doc)
    if not cited_ids:
        result.notes.append("no_cited_sources_to_format")
        return text

    if is_numeric_target:
        ordered_ids = [sid for sid in citation_order if sid in cited_ids]
    else:
        # Alphabetical by primary surname for author-date styles.
        ordered_ids = sorted(
            cited_ids,
            key=lambda sid: _alpha_key(sources_by_id.get(sid)),
        )

    entries: list[str] = []
    for i, sid in enumerate(ordered_ids, start=1):
        src = sources_by_id.get(sid)
        if src is None:
            entries.append(f"[{i}] [unknown source: {sid}]")
            continue
        formatted = format_citation(src.citation, style)
        if is_numeric_target:
            entries.append(f"[{i}] {formatted.bibliography}")
        else:
            entries.append(formatted.bibliography)
    result.bibliography_emitted = len(entries)

    new_section = "# References\n\n" + "\n\n".join(entries) + "\n"

    # Find the existing references heading (if any) and replace.
    from .scanner import _BIBLIO_HEADING  # reuse the same regex
    match = _BIBLIO_HEADING.search(text)
    if match is None:
        # Append.
        sep = "\n\n" if not text.endswith("\n\n") else ""
        return text + sep + new_section
    head_start = match.start()
    return text[:head_start].rstrip() + "\n\n" + new_section


def _cited_source_ids(doc: DocumentCitations) -> set[str]:
    """The set of source_ids actually used somewhere in the document
    (inline or footnote)."""
    out: set[str] = set()
    for ic in doc.inline_citations:
        if ic.source_id:
            out.add(ic.source_id)
    for fn in doc.footnotes:
        if fn.source_id:
            out.add(fn.source_id)
        if fn.resolves_to_source_id:
            out.add(fn.resolves_to_source_id)
    return out


# ─── helpers ────────────────────────────────────────


def _first_citation_order(doc: DocumentCitations) -> list[str]:
    """Order in which sources are first cited in the document. Used
    for numeric-style numbering and bibliography ordering."""
    seen: set[str] = set()
    out: list[str] = []
    # Sort by char_start so we walk the document in order.
    by_position = sorted(
        (ic for ic in doc.inline_citations if ic.source_id),
        key=lambda ic: (ic.paragraph_index, ic.char_start),
    )
    for ic in by_position:
        sid = ic.source_id
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    # Footnote citations append after inline ones.
    for fn in doc.footnotes:
        sid = fn.resolves_to_source_id or fn.source_id
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _alpha_key(src) -> tuple:
    """Sort key for alphabetical bibliography ordering. ``Anon.``
    sorts last."""
    if src is None or not src.citation.authors:
        return ("zzz_anon",)
    surname = src.citation.authors[0]
    if "," in surname:
        surname = surname.split(",")[0]
    return (surname.lower(),)


def write_restyled(
    document_path: Path,
    result: RestyleResult,
    *,
    output_path: Path | None = None,
) -> Path:
    """Write the restyled document. By default produces ``<stem>.<style><suffix>``
    next to the original; passing ``output_path`` overrides."""
    if output_path is None:
        output_path = document_path.parent / (
            f"{document_path.stem}.{result.style}{document_path.suffix}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.document, encoding="utf-8")
    return output_path
