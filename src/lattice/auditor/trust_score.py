"""Per-section trust score.

A single 0–1 number per section combining the signals an academic
actually uses to decide where to read carefully:

- **section metrics** — the per-section ``ArgumentMetrics`` score
  (evidence_backing, mechanism_coverage, source_diversity,
  relationship_density, thesis_connection, claim_type_diversity).
- **audit-flag density** — flags-per-claim within the section. A
  section bristling with flags reads less trustworthy than one with
  none.
- **readiness blocks** — clusters in this section that the readiness
  check refused to deliver. A blocked cluster invalidates trust at
  the section level.
- **voice-review compliance** — when a voice review has run, sections
  with whole-document layer failures (citation engagement, register
  bleed, etc.) lose trust.

The trust score is **diagnostic, not normative**: a low score means
"check this carefully," not "this section is wrong." Each component
is exposed alongside the aggregate so the user can see why the
number is what it is.

Pure function over the project's persisted state. No LLM. No I/O
beyond reading the project's existing audit / readiness / metrics
files. The CLI command surfaces it as a per-section table; the web
UI uses the same payload to colour the section heat-map.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..graph.metrics import ArgumentMetrics, SectionMetrics
from ..graph.models import AuthorGraph, AuditFlag


# Component weights — they sum to 1.0 and are tuned so:
# - the metric is the dominant signal (you can't be untrustworthy if
#   the metrics say you're well-developed)
# - audit flags + readiness BLOCKS pull strongly when present
# - voice review nudges
_WEIGHTS = {
    "metric": 0.50,
    "audit": 0.20,
    "readiness": 0.20,
    "voice_review": 0.10,
}


@dataclass
class SectionTrustScore:
    """One section's trust view."""

    section_id: str
    section_title: str = ""
    score: float = 0.0
    # Per-component sub-scores (each in 0..1; 1 = trustworthy)
    metric_component: float = 0.0
    audit_component: float = 1.0
    readiness_component: float = 1.0
    voice_review_component: float = 1.0
    # Raw counts the consumer can render
    audit_flag_count: int = 0
    readiness_blocks: int = 0
    # Human-readable reasons the score is what it is.
    notes: list[str] = field(default_factory=list)


@dataclass
class TrustReport:
    """Document-level roll-up."""

    document_score: float = 0.0
    sections: list[SectionTrustScore] = field(default_factory=list)
    # Quick summaries for the CLI / UI:
    untrustworthy_sections: list[str] = field(default_factory=list)


# ─── public entry points ─────────────────────────────


def compute_trust(
    graph: AuthorGraph,
    metrics: ArgumentMetrics,
    *,
    audit_flags: Iterable[AuditFlag] | None = None,
    readiness_blocked_clusters: set[str] | None = None,
    cluster_to_section: dict[str, str] | None = None,
    voice_review_section_failures: set[str] | None = None,
) -> TrustReport:
    """Build a ``TrustReport`` from the inputs the project already
    has on disk.

    Inputs are passed in (rather than read from disk inside the
    function) so the function stays pure and easy to test. The CLI
    command does the disk-reading and then calls this.
    """
    audit_flags = list(audit_flags or [])
    readiness_blocks = readiness_blocked_clusters or set()
    cluster_to_section = cluster_to_section or {}
    voice_failures = voice_review_section_failures or set()

    # Audit flags by section (using cluster_to_section to map flags
    # whose section_id is empty but whose cluster_id is set).
    flags_by_section: dict[str, list[AuditFlag]] = {}
    for flag in audit_flags:
        sid = flag.section_id or cluster_to_section.get(flag.cluster_id, "")
        if sid:
            flags_by_section.setdefault(sid, []).append(flag)

    # Readiness blocks by section.
    blocks_by_section: dict[str, int] = {}
    for cluster_id in readiness_blocks:
        sid = cluster_to_section.get(cluster_id, "")
        if sid:
            blocks_by_section[sid] = blocks_by_section.get(sid, 0) + 1

    sections: list[SectionTrustScore] = []
    for section_id, section_metric in metrics.per_section.items():
        ts = _compute_one_section(
            section_metric,
            flags=flags_by_section.get(section_id, []),
            readiness_block_count=blocks_by_section.get(section_id, 0),
            voice_review_failed=section_id in voice_failures,
        )
        sections.append(ts)

    # Document score: simple average of section scores, weighted by
    # claim_count so heavier sections dominate.
    if sections:
        total_claims = sum(
            metrics.per_section[s.section_id].claim_count for s in sections
        ) or 1
        weighted = sum(
            s.score * metrics.per_section[s.section_id].claim_count
            for s in sections
        )
        document_score = round(weighted / total_claims, 4)
    else:
        document_score = 0.0

    untrustworthy = [
        s.section_id for s in sections if s.score < 0.5
    ]
    return TrustReport(
        document_score=document_score,
        sections=sections,
        untrustworthy_sections=untrustworthy,
    )


def _compute_one_section(
    metric: SectionMetrics,
    *,
    flags: list[AuditFlag],
    readiness_block_count: int,
    voice_review_failed: bool,
) -> SectionTrustScore:
    """Combine the four components for one section."""
    metric_component = float(metric.score)

    # Audit component: flags-per-claim, normalised + inverted (more
    # flags → lower trust). 0 flags → 1.0; 1 flag/claim → ~0.0.
    if metric.claim_count > 0:
        density = len(flags) / metric.claim_count
        audit_component = max(0.0, 1.0 - density)
    else:
        audit_component = 1.0

    # Readiness component: any blocked cluster in this section drops
    # the component to 0; otherwise 1.0.
    readiness_component = 0.0 if readiness_block_count > 0 else 1.0

    # Voice review: pass/fail signal.
    voice_review_component = 0.5 if voice_review_failed else 1.0

    score = round(
        _WEIGHTS["metric"] * metric_component
        + _WEIGHTS["audit"] * audit_component
        + _WEIGHTS["readiness"] * readiness_component
        + _WEIGHTS["voice_review"] * voice_review_component,
        4,
    )

    # Notes — short, prioritised by signal strength.
    notes: list[str] = []
    if readiness_block_count > 0:
        notes.append(
            f"{readiness_block_count} cluster(s) blocked by readiness "
            "— review cannot proceed until resolved."
        )
    if metric.claim_count > 0 and len(flags) >= 3:
        notes.append(
            f"{len(flags)} audit flag(s) across "
            f"{metric.claim_count} claim(s)."
        )
    if metric_component < 0.4:
        notes.append(
            f"Section metric is {metric_component:.2f} — see "
            "rescaffold for what's missing."
        )
    if voice_review_failed:
        notes.append("Voice review flagged this section.")

    return SectionTrustScore(
        section_id=metric.section_id,
        section_title=metric.section_title,
        score=score,
        metric_component=round(metric_component, 4),
        audit_component=round(audit_component, 4),
        readiness_component=round(readiness_component, 4),
        voice_review_component=round(voice_review_component, 4),
        audit_flag_count=len(flags),
        readiness_blocks=readiness_block_count,
        notes=notes,
    )


# ─── disk-reading helpers (used by the CLI command) ──


def load_audit_flags(project_path: Path, voice: str) -> list[AuditFlag]:
    """Read ``.lattice/audit/audit_flags.<voice>.json`` if present.
    Returns an empty list when no audit has run."""
    path = project_path / ".lattice" / "audit" / f"audit_flags.{voice}.json"
    if not path.exists():
        # Older layouts: try the un-voiced file too.
        path = project_path / ".lattice" / "audit_flags.json"
        if not path.exists():
            return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out: list[AuditFlag] = []
    for raw in data if isinstance(data, list) else []:
        try:
            out.append(AuditFlag.model_validate(raw))
        except Exception:  # noqa: BLE001
            continue
    return out


def load_readiness_blocks(project_path: Path, voice: str) -> set[str]:
    """Read the readiness report's blocking_clusters list."""
    candidates = [
        project_path / ".lattice" / "audit" / f"readiness.{voice}.json",
        project_path / ".lattice" / f"readiness_report.{voice}.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return set(data.get("blocking_clusters") or [])
            except (json.JSONDecodeError, OSError):
                continue
    return set()


def load_voice_review_section_failures(
    project_path: Path, voice: str,
) -> set[str]:
    """Read the voice review JSON (if present); collect section_ids
    that have at least one fail-level finding."""
    path = project_path / ".lattice" / f"voice_review.{voice}.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    failed: set[str] = set()
    for finding in data.get("findings") or []:
        if (finding.get("compliance") or "").lower() == "fail":
            sid = finding.get("section_id") or ""
            if sid:
                failed.add(sid)
    return failed


def cluster_to_section_map(graph: AuthorGraph, clusters) -> dict[str, str]:
    """Build a cluster_id → section_id map from the in-memory cluster
    plan."""
    return {c.cluster_id: c.section_id for c in clusters}
