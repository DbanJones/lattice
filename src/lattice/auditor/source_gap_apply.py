"""Apply accepted source-gap-review entries to the author graph.

Reads the structured gap report at
``.lattice/source_gap_review.<voice>.json``, walks the gaps interactively
or in batch, and injects accepted ones into the graph as appropriate
for each category.

Injection rules per category:

- ``mechanism``        → set ``Claim.mechanism`` on target_claim_id (or
                         append a `[needs_mechanism: ...]` tag if no
                         target). Replaces any existing mechanism only
                         with author confirmation.
- ``quantitative``     → append the reference snippet to target_claim_id
- ``arithmetic``         as ``Evidence.quote_text`` against a synthetic
- ``named_scholar``      ``expanded_lit_review`` source binding (weak),
- ``named_example``      preserving the reference's specific phrasing
                         the renderer can later draw on.
- ``analytical_move``  → log only, surface in the markdown report so
                         the author can manually add an
                         ``interpretive_pivot`` relationship.
- ``structural``       → log only.

Decisions are logged to ``.lattice/source_gap_decisions.json``
(append-only) and the gap's ``decision`` field updated in place. Gaps
already decided are skipped on subsequent runs unless ``--reapply`` is
passed.

Nothing in this module is interactive on its own — the CLI command
handles input. This module exposes ``apply_gap`` so unit tests can
exercise the injection logic without a console.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import (
    AuthorGraph,
    BindingStrength,
    Claim,
    Evidence,
)
from ..graph.store import GraphStore
from .source_gap_review import Gap, SourceGapReport, save_report


# Categories the auto-applier handles directly.
_AUTO_APPLY_CATEGORIES = {
    "mechanism",
    "quantitative",
    "arithmetic",
    "named_scholar",
    "named_example",
}

# Categories that require manual author judgement — log only.
_MANUAL_CATEGORIES = {
    "analytical_move",
    "structural",
}


@dataclass
class ApplyResult:
    gap_id: str
    action: str  # "applied_mechanism" | "applied_evidence" | "logged_manual" | "skipped_no_target" | "skipped_already_decided"
    target_claim_id: str = ""
    note: str = ""


def apply_gap(
    gap: Gap,
    graph: AuthorGraph,
) -> ApplyResult:
    """Apply one accepted gap to the graph in place.

    Returns an ApplyResult describing what was done. The caller is
    responsible for persisting the graph after a batch.
    """
    if gap.decision != "accepted":
        return ApplyResult(
            gap_id=gap.gap_id,
            action="skipped_already_decided",
            note=f"decision={gap.decision!r}",
        )

    if gap.category in _MANUAL_CATEGORIES:
        return ApplyResult(
            gap_id=gap.gap_id,
            action="logged_manual",
            note=f"category={gap.category} requires manual graph edit",
        )

    if gap.category not in _AUTO_APPLY_CATEGORIES:
        return ApplyResult(
            gap_id=gap.gap_id,
            action="logged_manual",
            note=f"unknown category {gap.category!r}",
        )

    if not gap.target_claim_id:
        return ApplyResult(
            gap_id=gap.gap_id,
            action="skipped_no_target",
            note="gap has no target_claim_id; manual attachment needed",
        )

    target = next(
        (c for c in graph.claims if c.claim_id == gap.target_claim_id),
        None,
    )
    if target is None:
        return ApplyResult(
            gap_id=gap.gap_id,
            action="skipped_no_target",
            note=f"target {gap.target_claim_id!r} not found in graph",
        )

    if gap.category == "mechanism":
        return _apply_mechanism(gap, target)

    return _apply_evidence_quote(gap, target)


def _apply_mechanism(gap: Gap, target: Claim) -> ApplyResult:
    """Set Claim.mechanism. Preserves any existing mechanism by appending."""
    new_text = gap.reference_snippet.strip()
    if not new_text:
        return ApplyResult(
            gap_id=gap.gap_id,
            action="skipped_no_target",
            target_claim_id=target.claim_id,
            note="empty reference_snippet",
        )
    existing = (target.mechanism or "").strip()
    if existing:
        # Preserve. Concatenate with a clear separator. The renderer
        # treats the whole field as a single mechanism block.
        target.mechanism = f"{existing} | {new_text}"
        action_note = "appended to existing mechanism"
    else:
        target.mechanism = new_text
        action_note = "set mechanism"
    target.modified_at = datetime.now(timezone.utc)
    return ApplyResult(
        gap_id=gap.gap_id,
        action="applied_mechanism",
        target_claim_id=target.claim_id,
        note=action_note,
    )


def _apply_evidence_quote(gap: Gap, target: Claim) -> ApplyResult:
    """Append an Evidence entry carrying the reference snippet as
    ``quote_text``, bound weakly to ``expanded_lit_review`` so the
    renderer surfaces the phrasing without it counting as a strong
    citation."""
    snippet = gap.reference_snippet.strip()
    if not snippet:
        return ApplyResult(
            gap_id=gap.gap_id,
            action="skipped_no_target",
            target_claim_id=target.claim_id,
            note="empty reference_snippet",
        )
    # Avoid duplicates if the same snippet has already been added.
    for ev in target.evidence:
        if ev.quote_text and ev.quote_text.strip() == snippet:
            return ApplyResult(
                gap_id=gap.gap_id,
                action="skipped_already_decided",
                target_claim_id=target.claim_id,
                note="snippet already present on target",
            )
    target.evidence.append(Evidence(
        source="expanded_lit_review",
        passage="",
        binding_strength=BindingStrength.weak,
        quote_verbatim=True,
        quote_text=snippet,
        page=None,
    ))
    target.modified_at = datetime.now(timezone.utc)
    return ApplyResult(
        gap_id=gap.gap_id,
        action="applied_evidence",
        target_claim_id=target.claim_id,
        note=f"appended quote ({len(snippet.split())} words)",
    )


def apply_report(
    report: SourceGapReport,
    store: GraphStore,
) -> list[ApplyResult]:
    """Apply every accepted gap in ``report`` to the store's graph.

    Saves the graph and returns one ApplyResult per gap. Gaps without
    decision == 'accepted' are skipped (status reported in the result).
    """
    graph = store.get_graph()
    results = [apply_gap(gap, graph) for gap in report.gaps]

    # Persist any actually applied changes.
    if any(r.action.startswith("applied_") for r in results):
        store.save_graph(graph)

    return results


def log_decisions(
    report: SourceGapReport,
    project_path: Path,
    voice_name: str,
    results: list[ApplyResult],
) -> Path:
    """Append the apply pass to .lattice/source_gap_decisions.json."""
    log_path = project_path / ".lattice" / "source_gap_decisions.json"
    existing: list = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []
    if not isinstance(existing, list):
        existing = []
    existing.append({
        "voice": voice_name,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "decisions": [
            {
                "gap_id": r.gap_id,
                "action": r.action,
                "target_claim_id": r.target_claim_id,
                "note": r.note,
            }
            for r in results
        ],
    })
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return log_path


def save_decisions(
    report: SourceGapReport,
    project_path: Path,
    voice_name: str,
) -> Path:
    """Persist the report (with decisions) back to its JSON file."""
    return save_report(report, project_path, voice_name)
