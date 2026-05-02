"""Per-journal style overrides on top of the six base citation styles.

A journal style is a YAML file at ``voices/journals/<name>.yml``
listing tweaks against one of the base styles (harvard, apa,
chicago_author_date, mla, vancouver, ieee). Lets an academic codify
"this journal's house style" once and reuse across submissions.

Schema (everything optional):

    name: nature
    base: vancouver
    description: |
      Nature uses superscript numeric inline citations and a
      reference list ordered by citation appearance. Author lists
      are abbreviated to first author + "et al." after 3 authors.

    inline:
      bracket_style: superscript           # superscript | square | round
      max_authors_inline: 3
      etal_after: 3                        # use "et al." beyond N authors

    bibliography:
      max_authors_listed: 5                # ", and N other authors" beyond
      author_format: "Surname, F.M."       # APA-style initials
      title_case: sentence                 # sentence | title | unchanged
      italicise_journal: true
      doi_format: "https://doi.org/{doi}"  # template

The runtime applies the override AFTER the base formatter produces
its output. Pure post-processing — no LLM, no extra dependencies
beyond pyyaml (already a project dep).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..graph.models import Citation
from ..output.citation_formatter import (
    FormattedCitation,
    format_citation,
    supported_styles,
)


# ─── data models ─────────────────────────────────────


@dataclass
class JournalStyle:
    """Parsed journal style override."""

    name: str
    base: str
    description: str = ""
    # Inline tweaks
    bracket_style: str = "square"            # square | round | superscript
    max_authors_inline: int | None = None
    etal_after: int | None = None
    # Bibliography tweaks
    max_authors_listed: int | None = None
    author_format: str = ""                  # purely informational; emit as-is
    title_case: str = "unchanged"            # sentence | title | unchanged
    italicise_journal: bool = False
    doi_format: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> JournalStyle:
        inline = data.get("inline") or {}
        biblio = data.get("bibliography") or {}
        return cls(
            name=name,
            base=data.get("base", "harvard").lower(),
            description=data.get("description", "").strip(),
            bracket_style=inline.get("bracket_style", "square"),
            max_authors_inline=inline.get("max_authors_inline"),
            etal_after=inline.get("etal_after"),
            max_authors_listed=biblio.get("max_authors_listed"),
            author_format=biblio.get("author_format", ""),
            title_case=biblio.get("title_case", "unchanged"),
            italicise_journal=biblio.get("italicise_journal", False),
            doi_format=biblio.get("doi_format", ""),
        )


# ─── public entry points ─────────────────────────────


def list_journal_styles(project_path: Path) -> list[str]:
    """Return the names of every journal style available in the project."""
    journals_dir = project_path / "voices" / "journals"
    if not journals_dir.exists():
        return []
    return sorted(p.stem for p in journals_dir.glob("*.yml"))


def load_journal_style(project_path: Path, name: str) -> JournalStyle:
    """Load a journal style by name from ``voices/journals/<name>.yml``."""
    path = project_path / "voices" / "journals" / f"{name}.yml"
    if not path.exists():
        raise FileNotFoundError(
            f"Journal style not found: {path}. "
            f"Available: {list_journal_styles(project_path)}"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if data.get("base") and data["base"].lower() not in supported_styles():
        raise ValueError(
            f"Journal {name!r} declares base={data.get('base')!r} which "
            f"isn't a supported style. Use one of: "
            f"{', '.join(supported_styles())}."
        )
    return JournalStyle.from_dict(name, data)


def format_for_journal(
    citation: Citation,
    journal: JournalStyle,
) -> FormattedCitation:
    """Format ``citation`` in ``journal``'s style.

    Runs the base style first, then applies journal-specific
    post-processing to inline + narrative + bibliography forms.
    """
    base = format_citation(citation, journal.base)
    return FormattedCitation(
        style=f"{journal.base}+{journal.name}",
        in_text=_apply_inline_tweaks(base.in_text, citation, journal),
        in_text_narrative=_apply_inline_tweaks(
            base.in_text_narrative, citation, journal,
        ),
        bibliography=_apply_bibliography_tweaks(
            base.bibliography, citation, journal,
        ),
    )


# ─── post-processing ────────────────────────────────


_SQUARE_BRACKET_RE = re.compile(r"\[([^\]]+)\]")
_ROUND_BRACKET_NUM_RE = re.compile(r"(?<![A-Za-z])\((\d+(?:[, ]+\d+)*)\)")


def _apply_inline_tweaks(s: str, citation: Citation, j: JournalStyle) -> str:
    """Apply bracket-style + author-truncation tweaks to an inline
    citation string."""
    if not s:
        return s
    # Bracket style swap (numeric forms only).
    if j.bracket_style == "superscript":
        # [12] → ¹²
        s = _SQUARE_BRACKET_RE.sub(
            lambda m: _to_superscript(m.group(1)), s,
        )
    elif j.bracket_style == "round":
        s = _SQUARE_BRACKET_RE.sub(lambda m: f"({m.group(1)})", s)
    # Author truncation in inline form (Smith, Jones, and Lee → Smith et al.)
    if j.etal_after is not None and citation.authors:
        if len(citation.authors) > j.etal_after:
            first_surname = _surname(citation.authors[0])
            # Replace any "Smith and X" / "Smith, X, and Y" with "Smith et al."
            s = re.sub(
                rf"({re.escape(first_surname)})[^,(]*?(?:and\s+\w+|, \w+)+",
                rf"\1 et al.",
                s,
                count=1,
            )
    return s


def _apply_bibliography_tweaks(
    s: str, citation: Citation, j: JournalStyle,
) -> str:
    """Apply title-case, journal italicisation, DOI-format,
    author-truncation tweaks to a bibliography entry."""
    if not s:
        return s
    out = s

    # Author truncation in bibliography — append "and N other authors"
    # when the listed count exceeds max_authors_listed.
    if (
        j.max_authors_listed is not None
        and citation.authors
        and len(citation.authors) > j.max_authors_listed
    ):
        excess = len(citation.authors) - j.max_authors_listed
        # Use the joined surnames to find the truncation cut point.
        for surname in (
            _surname(a) for a in citation.authors[j.max_authors_listed:]
        ):
            if surname and surname in out:
                idx = out.find(surname)
                # Trim from before the surname back to the previous separator.
                cut = out.rfind(",", 0, idx)
                if cut > 0:
                    out = out[:cut] + f", and {excess} others" + out[idx + len(surname):]
                    break

    # Title case.
    if j.title_case == "sentence" and citation.title:
        out = out.replace(
            citation.title, _to_sentence_case(citation.title), 1,
        )
    elif j.title_case == "title" and citation.title:
        out = out.replace(
            citation.title, _to_title_case(citation.title), 1,
        )

    # Italicise journal name. We use markdown emphasis since Lattice
    # outputs markdown.
    if j.italicise_journal and citation.container:
        if f"*{citation.container}*" not in out and citation.container in out:
            out = out.replace(
                citation.container, f"*{citation.container}*", 1,
            )

    # DOI format template.
    if j.doi_format and citation.doi:
        formatted_doi = j.doi_format.format(doi=citation.doi)
        # Replace the bare DOI string if present.
        if citation.doi in out and formatted_doi not in out:
            out = out.replace(citation.doi, formatted_doi, 1)

    return out


# ─── helpers ────────────────────────────────────────


_SUPERSCRIPT = str.maketrans("0123456789,", "⁰¹²³⁴⁵⁶⁷⁸⁹·")


def _to_superscript(text: str) -> str:
    return re.sub(r"\s+", "", text).translate(_SUPERSCRIPT)


def _surname(author: str) -> str:
    a = author.strip()
    if "," in a:
        return a.split(",", 1)[0].strip()
    parts = a.rsplit(" ", 1)
    return parts[-1].strip() if parts else a


def _to_sentence_case(s: str) -> str:
    """Sentence case: first word capitalised, the rest lowercase
    except proper nouns. We don't try to detect proper nouns; the
    base formatter already preserves case where the user wrote it."""
    if not s:
        return s
    return s[0].upper() + s[1:].lower()


def _to_title_case(s: str) -> str:
    """Title case: every significant word capitalised."""
    if not s:
        return s
    minor = {"a", "an", "the", "and", "or", "but", "in", "on", "at", "to",
             "for", "of", "by", "with", "as", "is"}
    parts = s.split()
    out: list[str] = []
    for i, w in enumerate(parts):
        if i > 0 and w.lower() in minor:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


# ─── starter library ─────────────────────────────────


def write_starter_journal_styles(project_path: Path) -> list[Path]:
    """Drop a small library of common journal styles into
    ``voices/journals/``. Idempotent — won't overwrite an existing
    file if the user has customised it."""
    target = project_path / "voices" / "journals"
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, content in _STARTER_LIBRARY.items():
        path = target / f"{name}.yml"
        if path.exists():
            continue
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


_STARTER_LIBRARY = {
    "nature": """\
name: nature
base: vancouver
description: |
  Nature: superscript numeric inline citations, citation-order
  reference list, abbreviated authors after 5.

inline:
  bracket_style: superscript

bibliography:
  max_authors_listed: 5
  italicise_journal: true
  doi_format: "https://doi.org/{doi}"
""",
    "science": """\
name: science
base: vancouver
description: |
  Science: bracketed numeric inline citations (the default),
  citation-order references, italic journal, abbreviated authors.

inline:
  bracket_style: round

bibliography:
  max_authors_listed: 6
  italicise_journal: true
""",
    "ieee_transactions": """\
name: ieee_transactions
base: ieee
description: |
  IEEE Transactions journals — square brackets, full author lists,
  italic journal name, DOI as a hyperlink.

inline:
  bracket_style: square

bibliography:
  italicise_journal: true
  doi_format: "doi: {doi}"
""",
    "british_journal_political_science": """\
name: british_journal_political_science
base: chicago_author_date
description: |
  BJPS uses Chicago author-date with sentence-case article titles
  and italicised journal names. No DOI rewriting.

bibliography:
  title_case: sentence
  italicise_journal: true
""",
    "energy_policy": """\
name: energy_policy
base: harvard
description: |
  Elsevier Energy Policy: Harvard inline, italic journal, full
  authors up to 6, then "et al."

bibliography:
  italicise_journal: true
  max_authors_listed: 6
""",
}
