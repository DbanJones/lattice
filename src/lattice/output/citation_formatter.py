"""Deterministic citation formatter.

Given a ``Citation`` payload (authors, year, title, container, volume,
issue, pages, doi, url) produce a formatted reference-list entry and
in-text citation in any of the major academic styles. No LLM calls —
this is rule-driven so the user can switch styles instantly without
spending tokens.

Supported styles (each has both a `bibliography_entry` and an
`in_text` form):
- harvard
- apa
- chicago_author_date
- mla
- vancouver
- ieee
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..graph.models import Citation


_STYLES = ("harvard", "apa", "chicago_author_date", "mla", "vancouver", "ieee")


@dataclass(frozen=True)
class FormattedCitation:
    style: str
    in_text: str          # e.g. "(Danziger et al., 2011)"
    in_text_narrative: str  # e.g. "Danziger et al. (2011)"
    bibliography: str     # full reference-list entry


# ─── shared helpers ──────────────────────────────────────────


def _last_name(author: str) -> str:
    """Pull the last name out of a free-form author string. Handles
    'Shai Danziger' / 'Danziger, S.' / 'S. Danziger'."""
    a = author.strip()
    if "," in a:
        return a.split(",", 1)[0].strip()
    parts = a.split()
    return parts[-1] if parts else a


def _first_initial(author: str) -> str:
    a = author.strip()
    if "," in a:
        rest = a.split(",", 1)[1].strip()
        return rest[0].upper() + "." if rest else ""
    parts = a.split()
    if len(parts) < 2:
        return ""
    return parts[0][0].upper() + "."


def _author_initials(author: str) -> str:
    """Return all given-name initials for an author, e.g. 'S. R.'."""
    a = author.strip()
    if "," in a:
        rest = a.split(",", 1)[1].strip()
        parts = rest.split()
    else:
        parts = a.split()
        parts = parts[:-1]  # drop family name
    return " ".join(p[0].upper() + "." for p in parts if p)


def _short_authors_in_text(authors: list[str], max_listed: int = 1) -> str:
    """Format author list for in-text style (Harvard / APA / Chicago).
    1 author: 'Smith'; 2: 'Smith and Jones'; 3+: 'Smith et al.'"""
    if not authors:
        return "Anon."
    last = [_last_name(a) for a in authors]
    if len(last) == 1:
        return last[0]
    if len(last) == 2:
        return f"{last[0]} and {last[1]}"
    return f"{last[0]} et al."


def _safe(value: str | None) -> str:
    return value.strip() if value else ""


# ─── Harvard ─────────────────────────────────────────────────


def _harvard(c: Citation) -> FormattedCitation:
    short = _short_authors_in_text(c.authors)
    year = str(c.year) if c.year else "n.d."
    in_text_paren = f"({short}, {year})"
    in_text_narrative = f"{short} ({year})"

    if c.authors:
        bib_authors = []
        for i, a in enumerate(c.authors):
            last = _last_name(a)
            initials = _author_initials(a)
            if i == 0:
                bib_authors.append(f"{last}, {initials}".strip(", "))
            else:
                bib_authors.append(f"{initials} {last}".strip())
        if len(bib_authors) > 1:
            bib_author_str = ", ".join(bib_authors[:-1]) + " and " + bib_authors[-1]
        else:
            bib_author_str = bib_authors[0]
    else:
        bib_author_str = "Anon."

    parts: list[str] = [bib_author_str + ".", f"{year}."]
    parts.append(f"'{_safe(c.title)}'.")
    if c.container:
        container_part = c.container
        if c.volume:
            container_part += f", {c.volume}"
            if c.issue:
                container_part += f"({c.issue})"
        if c.pages:
            container_part += f", pp. {c.pages}"
        parts.append(container_part + ".")
    if c.doi:
        parts.append(f"https://doi.org/{c.doi}")
    elif c.url:
        parts.append(c.url)
    bibliography = " ".join(p for p in parts if p)
    return FormattedCitation(
        style="harvard",
        in_text=in_text_paren,
        in_text_narrative=in_text_narrative,
        bibliography=bibliography,
    )


# ─── APA (7th edition) ───────────────────────────────────────


def _apa(c: Citation) -> FormattedCitation:
    # APA convention: '&' inside parentheses, 'and' in narrative.
    short_paren = _short_authors_in_text(c.authors).replace(" and ", " & ")
    short_narr = _short_authors_in_text(c.authors)
    year = str(c.year) if c.year else "n.d."
    in_text_paren = f"({short_paren}, {year})"
    in_text_narrative = f"{short_narr} ({year})"

    if c.authors:
        bib_parts = []
        for a in c.authors:
            last = _last_name(a)
            initials = _author_initials(a)
            bib_parts.append(f"{last}, {initials}".strip(", "))
        if len(bib_parts) > 1:
            bib_author_str = ", ".join(bib_parts[:-1]) + ", & " + bib_parts[-1]
        else:
            bib_author_str = bib_parts[0]
    else:
        bib_author_str = "Anon."

    pieces: list[str] = [f"{bib_author_str} ({year}).", f"{_safe(c.title)}."]
    if c.container:
        container_part = f"*{c.container}*"
        if c.volume:
            container_part += f", {c.volume}"
            if c.issue:
                container_part += f"({c.issue})"
        if c.pages:
            container_part += f", {c.pages}"
        pieces.append(container_part + ".")
    if c.doi:
        pieces.append(f"https://doi.org/{c.doi}")
    elif c.url:
        pieces.append(c.url)
    bibliography = " ".join(p for p in pieces if p)
    return FormattedCitation(
        style="apa",
        in_text=in_text_paren,
        in_text_narrative=in_text_narrative,
        bibliography=bibliography,
    )


# ─── Chicago author-date ─────────────────────────────────────


def _chicago_author_date(c: Citation) -> FormattedCitation:
    short = _short_authors_in_text(c.authors)
    year = str(c.year) if c.year else "n.d."
    in_text_paren = f"({short} {year})"
    in_text_narrative = f"{short} ({year})"

    if c.authors:
        bib_parts: list[str] = []
        for i, a in enumerate(c.authors):
            last = _last_name(a)
            given = a.replace(",", " ").replace(last, "").strip()
            if i == 0:
                bib_parts.append(f"{last}, {given}" if given else last)
            else:
                bib_parts.append(f"{given} {last}".strip())
        if len(bib_parts) > 1:
            bib_author_str = ", ".join(bib_parts[:-1]) + ", and " + bib_parts[-1]
        else:
            bib_author_str = bib_parts[0]
    else:
        bib_author_str = "Anon."

    pieces: list[str] = [
        f"{bib_author_str}.", f"{year}.", f'"{_safe(c.title)}."',
    ]
    if c.container:
        container_part = f"*{c.container}*"
        if c.volume:
            container_part += f" {c.volume}"
            if c.issue:
                container_part += f", no. {c.issue}"
        if c.pages:
            container_part += f": {c.pages}"
        pieces.append(container_part + ".")
    if c.doi:
        pieces.append(f"https://doi.org/{c.doi}")
    bibliography = " ".join(p for p in pieces if p)
    return FormattedCitation(
        style="chicago_author_date",
        in_text=in_text_paren,
        in_text_narrative=in_text_narrative,
        bibliography=bibliography,
    )


# ─── MLA (9th edition) ───────────────────────────────────────


def _mla(c: Citation) -> FormattedCitation:
    short = _short_authors_in_text(c.authors)
    pages = c.pages or ""
    in_text_paren = f"({short}{' ' + pages if pages else ''})"
    in_text_narrative = short

    if c.authors:
        bib_parts: list[str] = []
        for i, a in enumerate(c.authors):
            last = _last_name(a)
            given = a.replace(",", " ").replace(last, "").strip()
            if i == 0:
                bib_parts.append(f"{last}, {given}." if given else f"{last}.")
            else:
                bib_parts.append(f"{given} {last}".strip())
        if len(bib_parts) > 1:
            bib_author_str = ", and ".join([", ".join(bib_parts[:-1]), bib_parts[-1]])
        else:
            bib_author_str = bib_parts[0]
    else:
        bib_author_str = ""

    pieces: list[str] = []
    if bib_author_str:
        pieces.append(bib_author_str)
    pieces.append(f'"{_safe(c.title)}."')
    if c.container:
        container_part = f"*{c.container}*"
        if c.volume:
            container_part += f", vol. {c.volume}"
            if c.issue:
                container_part += f", no. {c.issue}"
        if c.year:
            container_part += f", {c.year}"
        if c.pages:
            container_part += f", pp. {c.pages}"
        pieces.append(container_part + ".")
    elif c.year:
        pieces.append(f"{c.year}.")
    bibliography = " ".join(pieces)
    return FormattedCitation(
        style="mla",
        in_text=in_text_paren,
        in_text_narrative=in_text_narrative,
        bibliography=bibliography,
    )


# ─── Vancouver (numeric) ─────────────────────────────────────


def _vancouver(c: Citation) -> FormattedCitation:
    # Vancouver is numeric; the in-text form is just the entry number,
    # which the caller assigns based on first-appearance order. We
    # return placeholders the caller can substitute.
    in_text_paren = "[#]"
    in_text_narrative = "[#]"

    if c.authors:
        bib_parts: list[str] = []
        for a in c.authors[:6]:  # Vancouver: list up to 6, then "et al."
            last = _last_name(a)
            initials = _author_initials(a).replace(".", "").replace(" ", "")
            bib_parts.append(f"{last} {initials}".strip())
        bib_author_str = ", ".join(bib_parts)
        if len(c.authors) > 6:
            bib_author_str += ", et al"
    else:
        bib_author_str = "Anon"

    pieces: list[str] = [f"{bib_author_str}.", f"{_safe(c.title)}."]
    if c.container:
        container_part = c.container
        if c.year:
            container_part += f". {c.year}"
        if c.volume:
            container_part += f";{c.volume}"
            if c.issue:
                container_part += f"({c.issue})"
        if c.pages:
            container_part += f":{c.pages}"
        pieces.append(container_part + ".")
    elif c.year:
        pieces.append(f"{c.year}.")
    if c.doi:
        pieces.append(f"doi:{c.doi}")
    bibliography = " ".join(p for p in pieces if p)
    return FormattedCitation(
        style="vancouver",
        in_text=in_text_paren,
        in_text_narrative=in_text_narrative,
        bibliography=bibliography,
    )


# ─── IEEE (numeric) ──────────────────────────────────────────


def _ieee(c: Citation) -> FormattedCitation:
    in_text_paren = "[#]"
    in_text_narrative = "[#]"

    if c.authors:
        bib_parts: list[str] = []
        for a in c.authors:
            last = _last_name(a)
            initials = _author_initials(a)
            bib_parts.append(f"{initials} {last}".strip())
        if len(bib_parts) > 1:
            bib_author_str = ", ".join(bib_parts[:-1]) + " and " + bib_parts[-1]
        else:
            bib_author_str = bib_parts[0]
    else:
        bib_author_str = "Anon."

    pieces: list[str] = [f"{bib_author_str},", f'"{_safe(c.title)},"']
    if c.container:
        container_part = f"*{c.container}*"
        if c.volume:
            container_part += f", vol. {c.volume}"
            if c.issue:
                container_part += f", no. {c.issue}"
        if c.pages:
            container_part += f", pp. {c.pages}"
        if c.year:
            container_part += f", {c.year}"
        pieces.append(container_part + ".")
    elif c.year:
        pieces.append(f"{c.year}.")
    if c.doi:
        pieces.append(f"doi: {c.doi}.")
    bibliography = " ".join(pieces)
    return FormattedCitation(
        style="ieee",
        in_text=in_text_paren,
        in_text_narrative=in_text_narrative,
        bibliography=bibliography,
    )


_FORMATTERS: dict[str, Callable[[Citation], FormattedCitation]] = {
    "harvard": _harvard,
    "apa": _apa,
    "chicago_author_date": _chicago_author_date,
    "mla": _mla,
    "vancouver": _vancouver,
    "ieee": _ieee,
}


def supported_styles() -> tuple[str, ...]:
    """Return the list of style identifiers ``format_citation`` accepts."""
    return _STYLES


def format_citation(citation: Citation, style: str) -> FormattedCitation:
    """Render a citation in the requested style.

    Raises ``ValueError`` for an unknown style — callers should fall
    back to harvard or surface the error to the user.
    """
    fmt = _FORMATTERS.get(style.lower())
    if fmt is None:
        raise ValueError(
            f"Unknown citation style {style!r}. "
            f"Supported: {', '.join(_STYLES)}"
        )
    return fmt(citation)
