"""Apply a rescaffold plan to the author graph + outline.

The planner (``rescaffold_planner.py``) produces a ``RescaffoldPlan``
listing structural operations, sorted by predicted impact. This
module is the apply step: take a plan + per-operation accept/reject
decisions, apply the accepted operations to the ``AuthorGraph`` in
dependency order, and serialize the result back to
``structure/outline.md``.

Apply contract — operations are applied in this fixed dependency
order regardless of how the plan listed them:

  1. ``split_section``          — splits one section into N subsections.
                                  Run first so later moves can target
                                  the new subsection IDs.
  2. ``add_section_stub``       — appends an empty heading. Same
                                  reason as above (synthetic IDs like
                                  ``new:counterargument``).
  3. ``move_claim``             — relocates a claim. Runs after
                                  splits/stubs because the target
                                  section may not have existed.
  4. ``reorder_within_section`` — orders claims within a section.
                                  Runs after moves because the final
                                  set of claims in the section is now
                                  known.
  5. ``promote_to_offcuts``     — drops a claim from the graph and
                                  schedules it for outline.offcuts.md.
                                  Last because earlier ops could
                                  otherwise silently no-op on
                                  already-removed claim IDs.

The merge_sections operation kind is recognised by the model but the
planner doesn't generate it (``_apply_in_memory`` skips it too). We
also skip it here; if a future plan includes one, it falls through
without raising.

Pure functions throughout. The CLI (``lattice rescaffold-apply``)
wraps these to read the plan from disk, prompt the user, write the
new outline, and persist the decisions log.

Two acceptance modes:

- **interactive**: walk operations in display order, calling a
  caller-supplied ``prompt`` callable per op. Returns True for
  accept, False for reject.
- **batch**: auto-accept every operation whose confidence is above
  a threshold; reject the rest. Used for ``--accept-all-with-
  confidence-above 0.6`` mode.

Defensive on input. A malformed plan file (or a single operation row
that doesn't validate) drops the bad rows with a logger warning
rather than raising — applying a partial plan is safer than refusing
to apply anything.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from ..graph.models import AuthorGraph, Section, SectionRole
from ..graph.serialize_outline import _format_claim_bullet
from .rescaffold_models import (
    RescaffoldAdvisory,
    RescaffoldOperation,
    RescaffoldPlan,
)


logger = logging.getLogger(__name__)


# ─── apply order ─────────────────────────────────────


# Authoritative dependency ordering. ``apply_operations`` sorts the
# accepted ops by their position in this tuple before applying.
APPLY_ORDER: tuple[str, ...] = (
    "split_section",
    "add_section_stub",
    "move_claim",
    "reorder_within_section",
    "promote_to_offcuts",
)


# ─── decision model ──────────────────────────────────


DecisionMode = Literal["interactive", "batch"]
DecisionValue = Literal["accept", "reject"]


class Decision(BaseModel):
    """One author decision on one operation."""

    op_id: str
    op_kind: str
    confidence: float
    decision: DecisionValue
    decided_at: datetime


class DecisionsLog(BaseModel):
    """Persisted to ``.lattice/rescaffold_decisions.json`` after each
    apply. Tracks every operation in the plan and the call the author
    (or the batch threshold) made on it, so re-running the apply step
    is auditable. Append-only across applies — each apply writes a
    new top-level entry."""

    project_name: str
    voice_name: str
    plan_generated_at: datetime
    applied_at: datetime
    mode: DecisionMode
    confidence_threshold: float | None = None    # only set in batch mode
    decisions: list[Decision] = Field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return sum(1 for d in self.decisions if d.decision == "accept")

    @property
    def rejected_count(self) -> int:
        return sum(1 for d in self.decisions if d.decision == "reject")


class OffcutRecord(BaseModel):
    """A claim that was promoted out of the main outline. The bullet
    text is captured while the claim is still in the graph so it can
    be appended to ``structure/outline.offcuts.md`` after the apply."""

    claim_id: str
    section_id: str
    statement: str
    bullet_text: str   # ``  - <statement> [tags...]`` — ready to write


# ─── plan loading ────────────────────────────────────


def load_plan(path: Path) -> RescaffoldPlan:
    """Load a ``RescaffoldPlan`` from JSON. Defensive — a single
    malformed operation row is dropped with a warning rather than
    invalidating the whole plan.

    Raises ``FileNotFoundError`` if the path doesn't exist and
    ``ValueError`` if the file is unreadable as JSON or fails the
    plan-level schema (project name / voice / generated_at). Anything
    less than that — bad operation rows, bad advisory rows — is
    salvaged with a warning."""
    if not path.exists():
        raise FileNotFoundError(f"No rescaffold plan at {path}")
    raw = path.read_text(encoding="utf-8")
    try:
        return RescaffoldPlan.model_validate_json(raw)
    except ValidationError:
        # Fall through to per-row defensive parsing.
        pass

    import json

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Plan file at {path} is not valid JSON: {exc}") from exc

    operations: list[dict] = list(data.get("operations") or [])
    advisories: list[dict] = list(data.get("advisories") or [])

    valid_ops: list[RescaffoldOperation] = []
    for i, row in enumerate(operations):
        try:
            valid_ops.append(RescaffoldOperation.model_validate(row))
        except ValidationError as exc:
            logger.warning(
                "Dropping malformed operation row %d (op_id=%r): %s",
                i, (row or {}).get("op_id", "<missing>"), exc.errors()[0]["msg"]
                if exc.errors() else str(exc),
            )

    data["operations"] = [op.model_dump(mode="json") for op in valid_ops]
    # Drop advisories defensively too — they're not load-bearing for the
    # apply step but a bad row shouldn't crash the load.
    valid_advs: list[dict] = []
    for i, row in enumerate(advisories):
        try:
            valid_advs.append(
                RescaffoldAdvisory.model_validate(row).model_dump(mode="json")
            )
        except ValidationError as exc:
            logger.warning(
                "Dropping malformed advisory row %d: %s",
                i, exc.errors()[0]["msg"] if exc.errors() else str(exc),
            )
    data["advisories"] = valid_advs

    return RescaffoldPlan.model_validate(data)


# ─── decisions ───────────────────────────────────────


def decide_batch(
    plan: RescaffoldPlan, *, confidence_threshold: float,
) -> list[Decision]:
    """Auto-accept every operation with confidence strictly above
    ``confidence_threshold``; reject the rest. The threshold is
    exclusive on purpose — passing 0.6 should mean "anything I'd
    eyeball as 'high confidence' but not 'middling'", and the
    planner's confidences cluster around 0.5/0.55/0.6/0.7."""
    now = datetime.now(timezone.utc)
    return [
        Decision(
            op_id=op.op_id,
            op_kind=op.kind,
            confidence=op.confidence,
            decision="accept" if op.confidence > confidence_threshold else "reject",
            decided_at=now,
        )
        for op in plan.operations
    ]


def decide_interactive(
    plan: RescaffoldPlan,
    prompt: Callable[[RescaffoldOperation], bool],
) -> list[Decision]:
    """Walk operations in plan order. ``prompt(op)`` returns True for
    accept, False for reject. The CLI supplies a typer-based prompt;
    tests supply a deterministic callable."""
    decisions: list[Decision] = []
    for op in plan.operations:
        accepted = bool(prompt(op))
        decisions.append(Decision(
            op_id=op.op_id,
            op_kind=op.kind,
            confidence=op.confidence,
            decision="accept" if accepted else "reject",
            decided_at=datetime.now(timezone.utc),
        ))
    return decisions


def filter_accepted(
    plan: RescaffoldPlan, decisions: list[Decision],
) -> list[RescaffoldOperation]:
    """Return the plan's operations that the decisions accepted, in
    plan order. Operations the decisions don't mention are treated as
    rejected."""
    accepted_ids = {d.op_id for d in decisions if d.decision == "accept"}
    return [op for op in plan.operations if op.op_id in accepted_ids]


# ─── core apply ──────────────────────────────────────


def apply_operations(
    graph: AuthorGraph, operations: list[RescaffoldOperation],
) -> tuple[AuthorGraph, list[OffcutRecord]]:
    """Apply ``operations`` to a deep copy of ``graph``. Returns the
    modified graph plus a list of ``OffcutRecord``s — one per
    ``promote_to_offcuts`` operation that actually fired — so the
    caller can write them to ``structure/outline.offcuts.md``.

    Operations are sorted by ``APPLY_ORDER`` first; ties within a
    kind retain their input order. Operations whose ``kind`` isn't in
    ``APPLY_ORDER`` (e.g. ``merge_sections``) are skipped.
    """
    g = copy.deepcopy(graph)
    sections_by_id: dict[str, Section] = {s.section_id: s for s in g.sections}

    sorted_ops = _sort_by_apply_order(operations)
    offcut_records: list[OffcutRecord] = []

    for op in sorted_ops:
        if op.kind == "split_section":
            _apply_split_section(g, op, sections_by_id)
        elif op.kind == "add_section_stub":
            _apply_add_section_stub(g, op, sections_by_id)
        elif op.kind == "move_claim":
            _apply_move_claim(g, op, sections_by_id)
        elif op.kind == "reorder_within_section":
            _apply_reorder(g, op, sections_by_id)
        elif op.kind == "promote_to_offcuts":
            record = _apply_promote_to_offcuts(g, op)
            if record is not None:
                offcut_records.append(record)
        # Other kinds (e.g. merge_sections) are skipped — the planner
        # doesn't generate them today, and an unknown kind shouldn't
        # crash the apply.

    return g, offcut_records


def _sort_by_apply_order(
    operations: list[RescaffoldOperation],
) -> list[RescaffoldOperation]:
    order_index = {kind: i for i, kind in enumerate(APPLY_ORDER)}
    fallback = len(APPLY_ORDER)  # unknown kinds drift to the end
    return sorted(
        operations,
        key=lambda op: (order_index.get(op.kind, fallback), 0),
    )


# ─── per-kind appliers ───────────────────────────────


def _apply_split_section(
    g: AuthorGraph, op: RescaffoldOperation, sections_by_id: dict[str, Section],
) -> None:
    if not op.source_section_id or not op.split_groups:
        return
    parent = sections_by_id.get(op.source_section_id)
    if parent is None:
        return
    first, *rest = op.split_groups
    parent.claim_ids = list(first)
    next_position = max(s.position for s in g.sections) + 1
    for i, group in enumerate(rest, start=1):
        new_id = f"{parent.section_id}.split{i}"
        new_section = Section(
            section_id=new_id,
            title=f"{parent.title} (split {i + 1})",
            parent=parent.section_id,
            position=next_position,
            role=parent.role,
            thesis_claim=parent.thesis_claim,
            claim_ids=list(group),
            target_length=parent.target_length // (len(op.split_groups) or 1),
            depth=parent.depth,
        )
        next_position += 1
        g.sections.append(new_section)
        sections_by_id[new_id] = new_section
        for cid in group:
            for c in g.claims:
                if c.claim_id == cid:
                    c.section_id = new_id
                    break


def _apply_add_section_stub(
    g: AuthorGraph, op: RescaffoldOperation, sections_by_id: dict[str, Section],
) -> None:
    if not op.new_section_role or not op.new_section_title:
        return
    new_id = op.target_section_id or f"new:{op.new_section_role}"
    if new_id in sections_by_id:
        return
    role_enum = _coerce_role(op.new_section_role)
    next_position = max((s.position for s in g.sections), default=-1) + 1
    new_section = Section(
        section_id=new_id,
        title=op.new_section_title,
        position=next_position,
        role=role_enum,
        claim_ids=[],
    )
    g.sections.append(new_section)
    sections_by_id[new_id] = new_section


def _apply_move_claim(
    g: AuthorGraph, op: RescaffoldOperation, sections_by_id: dict[str, Section],
) -> None:
    if not (op.target_claim_id and op.source_section_id and op.target_section_id):
        return
    src = sections_by_id.get(op.source_section_id)
    tgt = sections_by_id.get(op.target_section_id)
    if src and op.target_claim_id in src.claim_ids:
        src.claim_ids = [c for c in src.claim_ids if c != op.target_claim_id]
    if tgt and op.target_claim_id not in tgt.claim_ids:
        if op.target_position is not None:
            tgt.claim_ids.insert(op.target_position, op.target_claim_id)
        else:
            tgt.claim_ids.append(op.target_claim_id)
    for c in g.claims:
        if c.claim_id == op.target_claim_id:
            c.section_id = op.target_section_id
            break


def _apply_reorder(
    g: AuthorGraph, op: RescaffoldOperation, sections_by_id: dict[str, Section],
) -> None:
    if not op.source_section_id or not op.claim_order:
        return
    section = sections_by_id.get(op.source_section_id)
    if section is None:
        return
    valid = [c for c in op.claim_order if c in set(section.claim_ids)]
    missing = [c for c in section.claim_ids if c not in set(valid)]
    section.claim_ids = valid + missing


def _apply_promote_to_offcuts(
    g: AuthorGraph, op: RescaffoldOperation,
) -> OffcutRecord | None:
    if not op.target_claim_id:
        return None
    claim = next((c for c in g.claims if c.claim_id == op.target_claim_id), None)
    if claim is None:
        return None
    # Capture bullet text BEFORE we drop the claim, so the offcuts
    # file gets the same tag-rendered form as outline.md.
    rels_from_claim = [
        r for r in g.relationships if r.from_claim == op.target_claim_id
    ]
    bullet = _format_claim_bullet(claim, rels_from_claim)
    record = OffcutRecord(
        claim_id=claim.claim_id,
        section_id=claim.section_id or "",
        statement=claim.statement,
        bullet_text=bullet,
    )
    for s in g.sections:
        if op.target_claim_id in s.claim_ids:
            s.claim_ids = [c for c in s.claim_ids if c != op.target_claim_id]
    g.claims = [c for c in g.claims if c.claim_id != op.target_claim_id]
    g.relationships = [
        r for r in g.relationships
        if r.from_claim != op.target_claim_id
        and r.to_claim != op.target_claim_id
    ]
    return record


def _coerce_role(role_str: str) -> SectionRole:
    try:
        return SectionRole(role_str)
    except ValueError:
        return SectionRole.argumentative


# ─── offcuts file ────────────────────────────────────


def render_offcuts_block(
    records: list[OffcutRecord], *, applied_at: datetime,
) -> str:
    """Render an outline.offcuts.md block for one apply session.

    Includes a header naming the apply timestamp so multiple applies
    against the same project produce a clean append-only history."""
    if not records:
        return ""
    timestamp = applied_at.strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Promoted offcuts ({timestamp})",
        "",
        "_Claims removed from the main outline by `lattice rescaffold-apply`. "
        "Their original section is recorded next to each bullet so they can "
        "be reinstated by hand if the apply was a mistake._",
        "",
    ]
    for rec in records:
        origin = f" _(was {rec.section_id})_" if rec.section_id else ""
        # bullet_text already starts with two-space indent + "- "
        lines.append(f"{rec.bullet_text}{origin}")
    lines.append("")
    return "\n".join(lines) + "\n"


def append_offcuts_file(
    project_path: Path,
    records: list[OffcutRecord],
    *,
    applied_at: datetime,
) -> Path | None:
    """Append a block of offcut bullets to ``structure/outline.offcuts.md``.

    Creates the file if it doesn't exist. Returns the path written
    to, or ``None`` if there were no records to append."""
    if not records:
        return None
    target = project_path / "structure" / "outline.offcuts.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    block = render_offcuts_block(records, applied_at=applied_at)
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        target.write_text(existing + "\n" + block, encoding="utf-8")
    else:
        target.write_text(block, encoding="utf-8")
    return target


# ─── orchestrator ────────────────────────────────────


class ApplyResult(BaseModel):
    """What ``apply_to_project`` did. Returned to the CLI for display
    so the user sees a summary without having to read all four output
    paths."""

    snapshot_path: Path
    outline_path: Path
    offcuts_path: Path | None
    decisions_path: Path
    decisions: DecisionsLog
    offcut_count: int
    operations_applied: int

    model_config = {"arbitrary_types_allowed": True}


def apply_to_project(
    project_path: Path,
    plan: RescaffoldPlan,
    decisions: list[Decision],
    *,
    voice_name: str,
    mode: DecisionMode,
    confidence_threshold: float | None,
    graph: AuthorGraph,
    save_graph: Callable[[AuthorGraph], None] | None = None,
) -> ApplyResult:
    """End-to-end apply orchestrator. Steps:

    1. Snapshot ``structure/outline.md`` to
       ``structure/outline.pre-rescaffold.md``. Always runs, even if
       zero ops are accepted, so the author has a stable comparison
       point.
    2. Apply accepted operations to a deep copy of ``graph``.
    3. Serialize the modified graph to ``structure/outline.md``.
    4. Append offcut bullets to ``structure/outline.offcuts.md`` if
       any ``promote_to_offcuts`` ops were accepted.
    5. Persist the modified graph via ``save_graph`` if supplied.
    6. Write the decisions log to ``.lattice/rescaffold_decisions.json``.

    ``save_graph`` is injected so the function stays testable without
    a ``GraphStore`` dependency. The CLI passes
    ``store.save_graph``; tests can pass ``None`` (graph stays
    in-memory).
    """
    # Lazy import to avoid a top-of-module cycle: serialize_outline
    # already imports models, and pulling it eagerly here costs
    # nothing — it just keeps the import graph predictable.
    from ..graph.serialize_outline import serialize_graph_to_outline

    applied_at = datetime.now(timezone.utc)

    structure_dir = project_path / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    outline_path = structure_dir / "outline.md"
    snapshot_path = structure_dir / "outline.pre-rescaffold.md"

    # Step 1 — snapshot. Always runs, even with zero accepts.
    if outline_path.exists():
        snapshot_path.write_text(
            outline_path.read_text(encoding="utf-8"), encoding="utf-8",
        )
    else:
        # Outline file might not exist on a freshly-ingested project;
        # write an empty snapshot so the existence invariant holds.
        snapshot_path.write_text("", encoding="utf-8")

    # Step 2 — apply ops to a graph copy.
    accepted_ops = filter_accepted(plan, decisions)
    new_graph, offcut_records = apply_operations(graph, accepted_ops)

    # Step 3 — write new outline only when something was accepted. A
    # zero-accept apply leaves outline.md byte-stable (the snapshot is
    # the audit trail). Re-serialising on a no-op would otherwise show
    # cosmetic diffs from the canonicaliser, which is a confusing UX.
    if accepted_ops:
        new_outline = serialize_graph_to_outline(new_graph)
        outline_path.write_text(new_outline, encoding="utf-8")

    # Step 4 — offcuts.
    offcuts_path = append_offcuts_file(
        project_path, offcut_records, applied_at=applied_at,
    )

    # Step 5 — persist graph.
    if save_graph is not None and accepted_ops:
        save_graph(new_graph)

    # Step 6 — decisions log.
    decisions_log = DecisionsLog(
        project_name=plan.project_name,
        voice_name=voice_name,
        plan_generated_at=plan.generated_at,
        applied_at=applied_at,
        mode=mode,
        confidence_threshold=confidence_threshold if mode == "batch" else None,
        decisions=decisions,
    )
    decisions_path = project_path / ".lattice" / "rescaffold_decisions.json"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    decisions_path.write_text(
        decisions_log.model_dump_json(indent=2), encoding="utf-8",
    )

    return ApplyResult(
        snapshot_path=snapshot_path,
        outline_path=outline_path,
        offcuts_path=offcuts_path,
        decisions_path=decisions_path,
        decisions=decisions_log,
        offcut_count=len(offcut_records),
        operations_applied=len(accepted_ops),
    )
