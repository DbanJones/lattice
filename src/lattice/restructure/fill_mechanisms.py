"""Mechanism-walkthrough helper.

The most common rescaffold-planner advisory on real, well-scaffolded
papers is ``add_mechanism`` — empirical / methodological claims with
``importance >= 0.6`` that lack a ``[mechanism: ...]`` tag. ``towers``
produced 22 of these in a single planner run.

Walking 22 advisories one-by-one through the rescaffold-apply UX is
heavy. This module is the focused alternative: list mechanism
candidates, prompt the author for one mechanism each, and append
``[mechanism: ...]`` to the matching bullet in ``structure/outline.md``
in place — no graph mutation, no rescaffold detour.

The CLI command (``lattice fill-mechanisms``) is a thin wrapper over
``MechanismCandidate`` + ``apply_mechanism_edits``; both are exposed
here so tests can exercise them without going through typer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import (
    AuthorGraph,
    ClaimType,
    ScaffoldReport,
)


# Default importance floor. Set to 0.5 (the model default) so that
# outlines that haven't been through the annotator still surface every
# empirical / methodological claim — better to walk them all than miss
# real candidates because importance hasn't been computed. The CLI
# exposes ``--min-importance`` to raise it for annotated projects.
_DEFAULT_IMPORTANCE_FLOOR = 0.5

# Bullet line shape: leading whitespace, dash, space, body. Reused
# from the markdown ingester for consistency.
_BULLET_RE = re.compile(r"^(\s*-\s+)(.*)$")
# Detect an existing [mechanism: ...] tag so re-runs don't double-tag.
_MECHANISM_TAG_RE = re.compile(r"\[mechanism\s*[:=]", re.IGNORECASE)


@dataclass
class MechanismCandidate:
    """One claim worth a mechanism prompt."""

    claim_id: str
    section_id: str | None
    statement: str
    importance: float
    line: int | None
    original_excerpt: str
    claim_type: str  # "empirical" | "methodological"


@dataclass
class MechanismEdit:
    """The author's decision for one candidate."""

    candidate: MechanismCandidate
    mechanism: str  # the text to insert; "" means skipped


@dataclass
class FillMechanismsReport:
    """Summary of what was changed; persisted alongside decisions."""

    project_name: str
    voice_name: str | None
    generated_at: datetime
    candidate_count: int
    edits_applied: int
    edits_skipped: int
    outline_path: str
    snapshot_path: str | None
    edits: list[dict]


def collect_candidates(
    graph: AuthorGraph,
    report: ScaffoldReport,
    *,
    min_importance: float = _DEFAULT_IMPORTANCE_FLOOR,
) -> list[MechanismCandidate]:
    """Walk the graph and pull out every claim that the rescaffold
    planner would emit an ``add_mechanism`` advisory for.

    Sorted by ``importance`` descending — the heaviest claims come
    first so the author burns budget on the most consequential bullets.

    ``min_importance`` defaults to 0.5 (the no-info importance) so that
    un-annotated outlines still surface candidates. Pass a higher value
    (e.g. 0.7) on annotated projects to skip the long tail.
    """
    line_by_claim: dict[str, int | None] = {
        cr.claim_id: cr.line for cr in report.claim_reports
    }
    excerpt_by_claim: dict[str, str] = {
        cr.claim_id: cr.original_excerpt for cr in report.claim_reports
    }

    out: list[MechanismCandidate] = []
    for claim in graph.claims:
        if claim.type not in (ClaimType.empirical, ClaimType.methodological):
            continue
        if (claim.mechanism or "").strip():
            continue
        if claim.importance < min_importance:
            continue
        out.append(MechanismCandidate(
            claim_id=claim.claim_id,
            section_id=claim.section_id,
            statement=claim.statement,
            importance=float(claim.importance),
            line=line_by_claim.get(claim.claim_id),
            original_excerpt=excerpt_by_claim.get(claim.claim_id, ""),
            claim_type=claim.type.value,
        ))
    out.sort(key=lambda c: (-c.importance, c.claim_id))
    return out


def merge_saved_importance_and_mechanism(
    fresh: AuthorGraph, saved: AuthorGraph,
) -> AuthorGraph:
    """Copy ``importance`` and ``mechanism`` from ``saved`` onto
    ``fresh`` for matching claim_ids.

    The fresh re-ingest carries fresh line numbers (in the scaffold
    report) but defaults importance to 0.5 since the annotator hasn't
    run on the just-parsed outline. The saved graph carries the
    annotator's enriched importance from the last full pipeline run.
    Combining them gives the best of both: editable line numbers AND
    real importance signal.

    Mutates ``fresh`` in place; returns it for chaining.
    """
    saved_by_id = {c.claim_id: c for c in saved.claims}
    for claim in fresh.claims:
        s = saved_by_id.get(claim.claim_id)
        if s is None:
            continue
        # Only copy importance when the saved value is non-default —
        # we don't want to blow away an outline-explicit importance
        # with a stale 0.5.
        if s.importance != 0.5:
            claim.importance = s.importance
        if (s.mechanism or "").strip() and not (claim.mechanism or "").strip():
            claim.mechanism = s.mechanism
    return fresh


def apply_mechanism_edits(
    outline_path: Path,
    edits: list[MechanismEdit],
    *,
    snapshot: bool = True,
) -> FillMechanismsReport:
    """Append ``[mechanism: <text>]`` to each candidate's bullet line in
    ``outline_path``.

    - Skips candidates whose mechanism is empty (the author chose to
      skip).
    - Idempotent: a bullet that already has a ``[mechanism: ...]`` tag
      is skipped even if the candidate list says otherwise (defensive
      against a stale candidate list).
    - When ``snapshot`` is True (default), writes ``outline.pre-fill-
      mechanisms.md`` next to the outline before any edits land.

    Returns a ``FillMechanismsReport`` describing what changed.
    """
    text = outline_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    snapshot_path: Path | None = None
    if snapshot:
        snapshot_path = outline_path.parent / (
            outline_path.stem + ".pre-fill-mechanisms" + outline_path.suffix
        )
        snapshot_path.write_text(text, encoding="utf-8")

    edits_applied: list[dict] = []
    edits_skipped: list[dict] = []

    for edit in edits:
        if not edit.mechanism.strip():
            edits_skipped.append({
                "claim_id": edit.candidate.claim_id,
                "reason": "skipped_by_user",
            })
            continue
        line_no = edit.candidate.line
        if line_no is None or line_no < 1 or line_no > len(lines):
            edits_skipped.append({
                "claim_id": edit.candidate.claim_id,
                "reason": "no_line_number",
            })
            continue
        original_line = lines[line_no - 1]
        # Strip the trailing newline for matching, restore on write.
        stripped = original_line.rstrip("\r\n")
        if not _BULLET_RE.match(stripped):
            edits_skipped.append({
                "claim_id": edit.candidate.claim_id,
                "reason": "line_is_not_a_bullet",
            })
            continue
        if _MECHANISM_TAG_RE.search(stripped):
            edits_skipped.append({
                "claim_id": edit.candidate.claim_id,
                "reason": "mechanism_tag_already_present",
            })
            continue
        # Append the new tag. Preserve the original line ending.
        ending = original_line[len(stripped):]
        new_line = (
            f"{stripped} [mechanism: {_sanitise(edit.mechanism)}]{ending}"
        )
        lines[line_no - 1] = new_line
        edits_applied.append({
            "claim_id": edit.candidate.claim_id,
            "section_id": edit.candidate.section_id,
            "line": line_no,
            "mechanism": edit.mechanism,
        })

    if edits_applied:
        outline_path.write_text("".join(lines), encoding="utf-8")

    return FillMechanismsReport(
        project_name="",  # caller fills
        voice_name=None,
        generated_at=datetime.now(timezone.utc),
        candidate_count=len(edits),
        edits_applied=len(edits_applied),
        edits_skipped=len(edits_skipped),
        outline_path=str(outline_path),
        snapshot_path=str(snapshot_path) if snapshot_path else None,
        edits=edits_applied + edits_skipped,
    )


# ─── helpers ──────────────────────────────────────


def _sanitise(text: str) -> str:
    """Bullet-tag values can't contain ``[`` or ``]`` (the parser is
    greedy on the closing bracket) or unescaped newlines. Map both
    brackets to parentheses and collapse whitespace so the resulting
    tag round-trips cleanly through the markdown ingester."""
    text = (
        text
        .replace("[", "(")
        .replace("]", ")")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return re.sub(r"\s+", " ", text).strip()
