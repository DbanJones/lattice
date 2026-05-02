"""Citation filler — walk verifier discrepancies, accept canonical
fields, fill gaps.

For every source whose verification surfaced one or more
``CitationDiscrepancy`` entries, present the per-field comparison
to the author:

- **paper says**: what the bibliography entry currently has
- **canonical says**: what Crossref / OpenAlex returned
- **author chooses**: accept canonical, keep paper, or override with
  hand-typed value

Each decision is logged to ``.lattice/citation_decisions.json``
(append-only) so re-runs only walk undecided fields. Source records
are updated in-place via ``GraphStore.save_source``.

The interactive prompt loop lives in the CLI command; this module
exposes pure functions the CLI assembles into a session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Sequence

from ..graph.models import (
    Citation,
    CitationDecision,
    CitationDiscrepancy,
    CitationDiscrepancySeverity,
    CitationVerification,
    CitationVerifier,
    Source,
)


FillAction = Literal["accept_canonical", "reject", "manual_override", "skip"]


@dataclass
class FillCandidate:
    """One field-level decision waiting on the author."""

    source_id: str
    field: str
    paper_value: str
    canonical_value: str
    severity: CitationDiscrepancySeverity
    verifier: CitationVerifier
    note: str = ""

    @property
    def is_gap_fill(self) -> bool:
        """True when the paper has nothing and canonical has something
        — pure addition rather than correction."""
        return not self.paper_value and bool(self.canonical_value)


@dataclass
class FillDecision:
    """The author's choice for one ``FillCandidate``."""

    candidate: FillCandidate
    action: FillAction
    chosen_value: str = ""  # only meaningful for accept_canonical / manual_override


# ─── candidate selection ─────────────────────────


def collect_fill_candidates(
    verifications: dict[str, CitationVerification],
    *,
    decided: Iterable[CitationDecision] | None = None,
    severity_floor: CitationDiscrepancySeverity = CitationDiscrepancySeverity.info,
) -> list[FillCandidate]:
    """Build the list of fields the author still needs to decide.

    Skips already-decided (source_id, field) pairs in ``decided``.
    Sorts by severity (errors first), then by source_id for stability.
    """
    decided_set = {(d.source_id, d.field) for d in (decided or [])}
    severity_rank = {
        CitationDiscrepancySeverity.error: 0,
        CitationDiscrepancySeverity.warning: 1,
        CitationDiscrepancySeverity.info: 2,
    }
    floor_rank = severity_rank[severity_floor]

    out: list[FillCandidate] = []
    for source_id, ver in verifications.items():
        if not ver.matched:
            continue
        for d in ver.discrepancies:
            if (source_id, d.field) in decided_set:
                continue
            if severity_rank[d.severity] > floor_rank:
                continue
            out.append(FillCandidate(
                source_id=source_id,
                field=d.field,
                paper_value=d.paper_value,
                canonical_value=d.canonical_value,
                severity=d.severity,
                verifier=ver.verifier,
                note=d.note,
            ))
    out.sort(key=lambda c: (severity_rank[c.severity], c.source_id, c.field))
    return out


# ─── apply decisions ─────────────────────────────


def apply_decisions(
    sources: Sequence[Source],
    decisions: list[FillDecision],
) -> tuple[dict[str, Source], list[CitationDecision]]:
    """Apply ``decisions`` to a copy of each affected ``Source``.

    Returns ``(updated_sources_by_id, decision_log)``. The caller
    persists the sources to disk via ``GraphStore.save_source`` and
    appends the decision log to ``.lattice/citation_decisions.json``.

    Pure function — never mutates the input list directly. Skipped
    decisions don't appear in either output.
    """
    by_id = {s.source_id: s.model_copy(deep=True) for s in sources}
    log: list[CitationDecision] = []
    now = datetime.now(timezone.utc)

    for d in decisions:
        if d.action == "skip":
            continue
        src = by_id.get(d.candidate.source_id)
        if src is None:
            continue
        chosen = d.chosen_value
        if d.action == "accept_canonical":
            chosen = d.candidate.canonical_value
        elif d.action == "reject":
            chosen = d.candidate.paper_value
        _set_citation_field(src.citation, d.candidate.field, chosen)

        log.append(CitationDecision(
            source_id=d.candidate.source_id,
            field=d.candidate.field,
            action=d.action,
            paper_value=d.candidate.paper_value,
            canonical_value=d.candidate.canonical_value,
            chosen_value=chosen,
            decided_at=now,
            verifier=d.candidate.verifier,
        ))

    return by_id, log


def _set_citation_field(citation: Citation, field: str, value: str) -> None:
    """Write ``value`` into ``citation.{field}`` with the right type."""
    if field == "year":
        try:
            citation.year = int(value) if value else None
        except ValueError:
            return
    elif field == "authors":
        citation.authors = [v.strip() for v in value.split(",") if v.strip()]
    elif field == "title":
        citation.title = value
    elif field == "container":
        citation.container = value or None
    elif field == "volume":
        citation.volume = value or None
    elif field == "issue":
        citation.issue = value or None
    elif field == "pages":
        citation.pages = value or None
    elif field == "doi":
        citation.doi = value or None
    elif field == "url":
        citation.url = value or None


# ─── decision log persistence ────────────────────


def load_decisions(project_path: Path) -> list[CitationDecision]:
    """Read ``.lattice/citation_decisions.json``. Returns an empty
    list if the file doesn't exist or is corrupt."""
    path = project_path / ".lattice" / "citation_decisions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[CitationDecision] = []
    for raw in data:
        try:
            out.append(CitationDecision.model_validate(raw))
        except Exception:  # noqa: BLE001
            continue
    return out


def append_decisions(
    project_path: Path, decisions: list[CitationDecision],
) -> Path:
    """Append ``decisions`` to ``.lattice/citation_decisions.json``."""
    path = project_path / ".lattice" / "citation_decisions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_decisions(project_path)
    existing.extend(decisions)
    serialised = [json.loads(d.model_dump_json()) for d in existing]
    path.write_text(json.dumps(serialised, indent=2), encoding="utf-8")
    return path
