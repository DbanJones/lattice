"""Reference export to BibTeX / RIS / Zotero CSL-JSON.

Deterministic, no LLM. Drives the LaTeX-using audience and gives any
academic a way to take their canonical (verified) bibliography out of
Lattice into another tool.

Three formats:

- **BibTeX** (`.bib`) — the LaTeX standard; ``@article{key, author = ...}``
- **RIS** (`.ris`) — the lingua franca for reference managers (Zotero,
  Mendeley, EndNote all import it)
- **CSL-JSON** (`.json`) — Zotero's native export format; round-trips
  cleanly with the ``import`` path.

Each emits one entry per ``Source`` in the project. Emits to bytes
(via the ``export_*_text`` functions) or writes to disk (via
``write_*``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Sequence

from ..graph.models import Citation, Source


# ─── BibTeX ──────────────────────────────────────────


# Map our Source.type to BibTeX entry types. Most academic refs are
# @article; reports / books / web pages get distinct entry types so
# downstream LaTeX produces correct formatting.
_BIBTEX_ENTRY_TYPES = {
    "primary_paper": "article",
    "review_paper": "article",
    "report": "techreport",
    "dataset": "misc",
    "web_page": "online",
    "note": "misc",
    "prior_writing": "misc",
    "interview": "misc",
}

# Characters BibTeX wants escaped inside braced field values.
_BIBTEX_ESCAPE_RE = re.compile(r"([\\&%$#_{}~^])")


def export_bibtex_text(sources: Sequence[Source]) -> str:
    """Render every source as a BibTeX entry. Returns the full file body."""
    entries: list[str] = []
    for src in sources:
        entries.append(_one_bibtex_entry(src))
    return "\n\n".join(entries) + "\n"


def write_bibtex(sources: Sequence[Source], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(export_bibtex_text(sources), encoding="utf-8")
    return path


def _one_bibtex_entry(src: Source) -> str:
    entry_type = _BIBTEX_ENTRY_TYPES.get(
        src.type.value if hasattr(src.type, "value") else str(src.type),
        "misc",
    )
    key = _bibtex_key(src)
    fields = _bibtex_fields(src.citation)
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{body}\n}}"


def _bibtex_key(src: Source) -> str:
    """The citekey users put in their LaTeX. We use ``source_id`` because
    that's already a human-readable slug."""
    return _safe_bibtex_key(src.source_id)


def _safe_bibtex_key(s: str) -> str:
    # BibTeX keys allow alphanumerics + a small punctuation set.
    return re.sub(r"[^A-Za-z0-9_\-:]+", "_", s).strip("_") or "anon"


def _bibtex_fields(c: Citation) -> list[tuple[str, str]]:
    """Build a deterministic ordered list of (field_name, value) pairs.
    Empty fields are skipped."""
    out: list[tuple[str, str]] = []
    if c.authors:
        out.append(("author", _bibtex_escape(" and ".join(c.authors))))
    if c.year is not None:
        out.append(("year", str(c.year)))
    if c.title:
        out.append(("title", _bibtex_escape(c.title)))
    if c.container:
        # @article uses 'journal'; @inproceedings/@inbook uses
        # 'booktitle'. Without entry-level introspection we use 'journal'
        # which BibTeX accepts on most types.
        out.append(("journal", _bibtex_escape(c.container)))
    if c.volume:
        out.append(("volume", c.volume))
    if c.issue:
        out.append(("number", c.issue))
    if c.pages:
        out.append(("pages", c.pages.replace("-", "--")))
    if c.doi:
        out.append(("doi", c.doi))
    if c.url:
        out.append(("url", c.url))
    return out


def _bibtex_escape(s: str) -> str:
    """Escape BibTeX-special characters inside a braced value."""
    if s is None:
        return ""
    return _BIBTEX_ESCAPE_RE.sub(r"\\\1", s)


# ─── RIS ─────────────────────────────────────────────


# RIS tags — two-letter codes, value follows ``  - ``.
# Spec at https://en.wikipedia.org/wiki/RIS_(file_format)
_RIS_TYPES = {
    "primary_paper": "JOUR",
    "review_paper": "JOUR",
    "report": "RPRT",
    "dataset": "DATA",
    "web_page": "ELEC",
    "note": "GEN",
    "prior_writing": "GEN",
    "interview": "GEN",
}


def export_ris_text(sources: Sequence[Source]) -> str:
    """Render every source as an RIS entry."""
    entries: list[str] = []
    for src in sources:
        entries.append(_one_ris_entry(src))
    return "\n".join(entries)


def write_ris(sources: Sequence[Source], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(export_ris_text(sources), encoding="utf-8")
    return path


def _one_ris_entry(src: Source) -> str:
    src_type_value = src.type.value if hasattr(src.type, "value") else str(src.type)
    ty = _RIS_TYPES.get(src_type_value, "GEN")
    lines = [f"TY  - {ty}"]
    c = src.citation
    for author in c.authors or []:
        lines.append(f"AU  - {author}")
    if c.year is not None:
        lines.append(f"PY  - {c.year}")
        lines.append(f"Y1  - {c.year}")
    if c.title:
        lines.append(f"TI  - {c.title}")
    if c.container:
        # T2 is RIS's secondary title (the journal / book).
        lines.append(f"T2  - {c.container}")
        lines.append(f"JO  - {c.container}")
    if c.volume:
        lines.append(f"VL  - {c.volume}")
    if c.issue:
        lines.append(f"IS  - {c.issue}")
    if c.pages:
        # RIS splits start / end pages where possible.
        if "-" in c.pages or "–" in c.pages:
            sep = "–" if "–" in c.pages else "-"
            sp, _, ep = c.pages.partition(sep)
            if sp.strip().isdigit():
                lines.append(f"SP  - {sp.strip()}")
            if ep.strip().isdigit():
                lines.append(f"EP  - {ep.strip()}")
        else:
            lines.append(f"SP  - {c.pages}")
    if c.doi:
        lines.append(f"DO  - {c.doi}")
    if c.url:
        lines.append(f"UR  - {c.url}")
    lines.append(f"ID  - {src.source_id}")
    lines.append("ER  - ")  # end-of-record marker
    lines.append("")  # blank line between entries
    return "\n".join(lines)


# ─── CSL-JSON ────────────────────────────────────────


# Maps to Zotero's canonical type names. CSL-JSON is the most
# round-trip-friendly format because it preserves field semantics
# better than BibTeX or RIS.
_CSL_TYPES = {
    "primary_paper": "article-journal",
    "review_paper": "article-journal",
    "report": "report",
    "dataset": "dataset",
    "web_page": "webpage",
    "note": "manuscript",
    "prior_writing": "manuscript",
    "interview": "interview",
}


def export_csl_json_text(sources: Sequence[Source]) -> str:
    """Render every source as a CSL-JSON array. Round-trips with the
    Zotero export path."""
    return json.dumps(
        [_one_csl_entry(s) for s in sources], indent=2, ensure_ascii=False,
    )


def write_csl_json(sources: Sequence[Source], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(export_csl_json_text(sources), encoding="utf-8")
    return path


def _one_csl_entry(src: Source) -> dict:
    src_type_value = src.type.value if hasattr(src.type, "value") else str(src.type)
    out: dict = {
        "id": src.source_id,
        "type": _CSL_TYPES.get(src_type_value, "manuscript"),
    }
    c = src.citation
    if c.authors:
        out["author"] = [_csl_author(a) for a in c.authors]
    if c.year is not None:
        out["issued"] = {"date-parts": [[c.year]]}
    if c.title:
        out["title"] = c.title
    if c.container:
        out["container-title"] = c.container
    if c.volume:
        out["volume"] = c.volume
    if c.issue:
        out["issue"] = c.issue
    if c.pages:
        out["page"] = c.pages
    if c.doi:
        out["DOI"] = c.doi
    if c.url:
        out["URL"] = c.url
    return out


def _csl_author(raw: str) -> dict:
    """Parse "Surname, Given" or "Given Surname" into CSL's
    {family, given} shape."""
    raw = (raw or "").strip()
    if not raw:
        return {"literal": ""}
    if "," in raw:
        family, given = raw.split(",", 1)
        return {"family": family.strip(), "given": given.strip()}
    parts = raw.rsplit(" ", 1)
    if len(parts) == 2:
        return {"given": parts[0].strip(), "family": parts[1].strip()}
    return {"literal": raw}


# ─── format dispatch ─────────────────────────────────


_EXPORTERS = {
    "bib": (export_bibtex_text, "bib"),
    "bibtex": (export_bibtex_text, "bib"),
    "ris": (export_ris_text, "ris"),
    "csl": (export_csl_json_text, "json"),
    "csl-json": (export_csl_json_text, "json"),
    "json": (export_csl_json_text, "json"),
    "zotero": (export_csl_json_text, "json"),
}


def supported_export_formats() -> list[str]:
    return ["bib", "ris", "csl-json"]


def export_references(
    sources: Sequence[Source], format: str,
) -> tuple[str, str]:
    """Render ``sources`` in the named format. Returns ``(text, suffix)``
    where ``suffix`` is the recommended file extension. Raises
    ``ValueError`` for unknown formats."""
    fmt = format.lower().strip()
    if fmt not in _EXPORTERS:
        raise ValueError(
            f"Unknown export format {format!r}. "
            f"Supported: {supported_export_formats()}"
        )
    fn, suffix = _EXPORTERS[fmt]
    return fn(sources), suffix
