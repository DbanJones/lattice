"""Pretty-print a ``RescaffoldPlan`` as human-readable markdown.

Used by the ``lattice rescaffold`` CLI to write
``outputs/rescaffold_plan.<voice>.md`` alongside the JSON. The
markdown is the primary surface authors actually read; the JSON is
for tooling.
"""

from __future__ import annotations

from .rescaffold_models import RescaffoldPlan


def format_plan_markdown(plan: RescaffoldPlan) -> str:
    """Render ``plan`` as readable markdown.

    Sections, in order: scoreboard, diagnosis, operations, advisories,
    proposed offcuts, claim-size table. Each section is skipped cleanly
    when empty so a healthy project produces a one-line "structure is
    fine" report rather than a wall of headers.
    """
    out: list[str] = []
    out.append(f"# Rescaffold plan — {plan.project_name}")
    out.append("")
    out.append(f"_voice: `{plan.voice_name}`_  ·  _generated {plan.generated_at.strftime('%Y-%m-%d %H:%M UTC')}_")
    out.append("")

    if not plan.diagnosis and not plan.operations and not plan.advisories:
        out.append("**Structure is healthy** — every metric sub-score "
                   "is above threshold; no rescaffold proposed.")
        return "\n".join(out) + "\n"

    out.extend(_format_scoreboard(plan))
    out.extend(_format_diagnosis(plan))
    out.extend(_format_operations(plan))
    out.extend(_format_advisories(plan))
    out.extend(_format_offcuts(plan))
    out.extend(_format_claim_sizes(plan))

    return "\n".join(out) + "\n"


# ─── sections ────────────────────────────────────────


def _format_scoreboard(plan: RescaffoldPlan) -> list[str]:
    if not plan.current_metrics or not plan.predicted_metrics:
        return []
    cs = plan.current_metrics["strength"]["score"]
    ps = plan.predicted_metrics["strength"]["score"]
    cb = plan.current_metrics["breadth"]["score"]
    pb = plan.predicted_metrics["breadth"]["score"]
    out = [
        "## Scoreboard",
        "",
        "| Metric | Current | Predicted | Δ |",
        "|---|---|---|---|",
        f"| Strength | {cs:.2f} | {ps:.2f} | {_arrow(ps - cs)} |",
        f"| Breadth  | {cb:.2f} | {pb:.2f} | {_arrow(pb - cb)} |",
        "",
    ]
    return out


def _format_diagnosis(plan: RescaffoldPlan) -> list[str]:
    if not plan.diagnosis:
        return []
    out = ["## Diagnosis", ""]
    for d in plan.diagnosis:
        marker = {"info": "·", "warning": "⚠", "critical": "✗"}.get(d.severity, "·")
        out.append(
            f"- {marker} **{d.dimension}.{d.sub_score}** = {d.value:.2f} "
            f"(target ≥ {d.threshold:.2f}) — {d.message}"
        )
    out.append("")
    return out


def _format_operations(plan: RescaffoldPlan) -> list[str]:
    if not plan.operations:
        return []
    out = [
        f"## Proposed operations ({len(plan.operations)})",
        "",
        "Sorted by predicted impact × confidence. Each is advisory; "
        "nothing is applied until you accept it.",
        "",
    ]
    for op in plan.operations:
        out.append(f"### `{op.kind}` — {op.op_id}")
        out.append("")
        out.append(f"**Rationale:** {op.rationale}")
        out.append("")
        out.append(f"_confidence: {op.confidence:.2f}_")
        out.append("")
        body = _format_op_body(op)
        if body:
            out.extend(body)
            out.append("")
        if op.expected_delta:
            out.append("Expected delta:")
            out.append("")
            for k, v in op.expected_delta.items():
                if abs(v) < 0.001:
                    continue
                out.append(f"- `{k}`: {_arrow(v)}")
            out.append("")
    return out


def _format_op_body(op) -> list[str]:
    """Op-specific detail block."""
    if op.kind == "split_section":
        rows = [f"**Split** `{op.source_section_id}` into:"]
        for i, group in enumerate(op.split_groups, 1):
            rows.append(f"  {i}. {', '.join(f'`{c}`' for c in group)}")
        return rows
    if op.kind == "merge_sections":
        return [f"**Merge** {', '.join(f'`{s}`' for s in op.section_ids_to_merge)}"]
    if op.kind == "add_section_stub":
        return [
            f"**Add** new section `{op.target_section_id}` "
            f"(role: `{op.new_section_role}`, title: \"{op.new_section_title}\")"
        ]
    if op.kind == "move_claim":
        return [
            f"**Move** `{op.target_claim_id}` from "
            f"`{op.source_section_id}` → `{op.target_section_id}`"
            + (f" at position {op.target_position}" if op.target_position is not None else "")
        ]
    if op.kind == "reorder_within_section":
        return [
            f"**Reorder** `{op.source_section_id}` to: "
            + " → ".join(f"`{c}`" for c in op.claim_order)
        ]
    if op.kind == "promote_to_offcuts":
        return [f"**Move** `{op.target_claim_id}` to offcuts."]
    return []


def _format_advisories(plan: RescaffoldPlan) -> list[str]:
    if not plan.advisories:
        return []
    out = [
        f"## Advisories ({len(plan.advisories)})",
        "",
        "Claim-level recommendations that no single structural move "
        "can address. Apply by editing the outline directly.",
        "",
    ]
    by_kind: dict[str, list] = {}
    for a in plan.advisories:
        by_kind.setdefault(a.kind, []).append(a)
    for kind, items in by_kind.items():
        out.append(f"### `{kind}` ({len(items)})")
        out.append("")
        for a in items:
            target = (
                f" — `{a.target_claim_id}`" if a.target_claim_id
                else f" — `{a.target_section_id}`" if a.target_section_id
                else ""
            )
            out.append(f"- **{a.advisory_id}**{target} _(conf {a.confidence:.2f})_")
            out.append(f"  - {a.rationale}")
            if a.suggestion:
                out.append(f"  - **how:** {a.suggestion}")
        out.append("")
    return out


def _format_offcuts(plan: RescaffoldPlan) -> list[str]:
    if not plan.proposed_offcuts:
        return []
    out = [
        f"## Proposed offcuts ({len(plan.proposed_offcuts)})",
        "",
        "Low-importance claims with no inbound relationships. Moving "
        "these to `structure/outline.offcuts.md` keeps them recoverable "
        "without weighing down the main scaffold.",
        "",
    ]
    for cid in plan.proposed_offcuts:
        out.append(f"- `{cid}` (size {plan.claim_sizes.get(cid, 0):.2f})")
    out.append("")
    return out


def _format_claim_sizes(plan: RescaffoldPlan) -> list[str]:
    if not plan.claim_sizes:
        return []
    items = sorted(plan.claim_sizes.items(), key=lambda kv: -kv[1])
    if len(items) > 25:
        items = items[:25]
    out = [
        "## Claim sizes (top 25)",
        "",
        "| Claim | Size |",
        "|---|---|",
    ]
    for cid, size in items:
        out.append(f"| `{cid}` | {size:.2f} |")
    out.append("")
    return out


# ─── helpers ─────────────────────────────────────────


def _arrow(delta: float) -> str:
    if abs(delta) < 0.001:
        return f"±0.00"
    sign = "▲" if delta > 0 else "▼"
    return f"{sign} {delta:+.2f}"
