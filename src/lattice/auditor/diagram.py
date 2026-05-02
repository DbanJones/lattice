"""Diagram-readiness audit (Phase 4c).

Sanity checks against the visualisation payload, before the cytoscape
HTML is shipped to the browser. Catches the failure modes that turn a
diagram into a misleading artefact rather than a missing-data signal:

1. **Hidden sections via bad parent IDs** — a section node whose
   ``parent`` points to a section that doesn't exist will silently
   disappear under cytoscape's compound-node layout.
2. **Invisible edges via missing nodes** — relationship edges whose
   ``source`` or ``target`` points to a node that wasn't included in
   the payload (e.g. a claim in a references section).
3. **Stale graph HTML** — the cached ``argument_graph.html`` exists
   but is older than one of its inputs.
4. **Unsupported claims shown as cleanly grounded** — a claim with
   ``evidence_quality`` in {bound, author} but whose underlying claim
   has no evidence and no evidence_status. This catches payload-builder
   bugs (the worst kind, because they look right).

Like the scaffold audit, findings are advisory — they don't refuse to
ship the diagram. The CLI / web UI surface them; the operator decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Severity = Literal["info", "warning", "error"]


@dataclass
class DiagramFinding:
    code: str
    severity: Severity
    message: str
    node_id: str | None = None
    edge_id: str | None = None


@dataclass
class DiagramReadinessReport:
    findings: list[DiagramFinding] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")


def audit_diagram(
    payload: dict,
    *,
    cached_html_path: Path | None = None,
    input_paths: list[Path] | None = None,
) -> DiagramReadinessReport:
    """Run every diagram-readiness check against ``payload`` (the dict
    returned by ``output.visualise.build_visualisation_payload``).

    ``cached_html_path`` and ``input_paths`` enable the staleness check.
    Pass them when you want to verify the cached HTML is fresh; omit
    them when auditing an in-memory payload only.
    """
    report = DiagramReadinessReport()

    sections = payload.get("sections", [])
    claims = payload.get("claims", [])
    relationships = payload.get("relationships", [])
    section_ids = {s["id"] for s in sections}
    claim_ids = {c["id"] for c in claims}

    _check_section_parents_resolve(sections, section_ids, report)
    _check_edge_endpoints_present(relationships, claim_ids, report)
    _check_evidence_quality_consistency(claims, report)
    if cached_html_path is not None:
        _check_cached_html_fresh(cached_html_path, input_paths or [], report)

    return report


# ─── individual checks ────────────────────────────────


def _check_section_parents_resolve(
    sections: list[dict],
    section_ids: set[str],
    report: DiagramReadinessReport,
) -> None:
    for s in sections:
        parent = s.get("parent")
        if parent and parent not in section_ids:
            report.findings.append(
                DiagramFinding(
                    code="section_dangling_parent",
                    severity="error",
                    message=(
                        f"Section {s['id']!r} references parent "
                        f"{parent!r} which is not in the payload — "
                        "cytoscape will drop it from the layout."
                    ),
                    node_id=s["id"],
                )
            )


def _check_edge_endpoints_present(
    relationships: list[dict],
    claim_ids: set[str],
    report: DiagramReadinessReport,
) -> None:
    for rel in relationships:
        source = rel.get("source")
        target = rel.get("target")
        if source not in claim_ids:
            report.findings.append(
                DiagramFinding(
                    code="edge_dangling_source",
                    severity="error",
                    message=(
                        f"Edge {rel['id']!r} ({rel.get('type')}) points "
                        f"from {source!r} which isn't a node in the "
                        "diagram — the edge will be invisible."
                    ),
                    edge_id=rel.get("id"),
                )
            )
        if target not in claim_ids:
            report.findings.append(
                DiagramFinding(
                    code="edge_dangling_target",
                    severity="error",
                    message=(
                        f"Edge {rel['id']!r} ({rel.get('type')}) points "
                        f"to {target!r} which isn't a node in the "
                        "diagram — the edge will be invisible."
                    ),
                    edge_id=rel.get("id"),
                )
            )


def _check_evidence_quality_consistency(
    claims: list[dict],
    report: DiagramReadinessReport,
) -> None:
    """A claim shown as ``bound`` (a green-tick visual signal in the
    diagram) MUST actually have evidence rows AND a strong/weak binding.
    A claim shown as ``author`` (the user-synthesis style) MUST be
    user_synthesis with author_origin=true. Anything else is the
    payload builder lying to the viewer."""
    for c in claims:
        eq = c.get("evidence_quality")
        ev = c.get("evidence", []) or []
        if eq == "bound":
            if not ev:
                report.findings.append(
                    DiagramFinding(
                        code="bound_claim_without_evidence",
                        severity="error",
                        message=(
                            f"Claim {c['id']!r} is rendered as 'bound' "
                            "(grounded) but has no Evidence rows — the "
                            "diagram is misleading the reader."
                        ),
                        node_id=c["id"],
                    )
                )
        elif eq == "author":
            if c.get("type") != "user_synthesis" or not c.get("author_origin"):
                report.findings.append(
                    DiagramFinding(
                        code="author_quality_on_non_synthesis",
                        severity="warning",
                        message=(
                            f"Claim {c['id']!r} is shown as 'author' "
                            "but isn't a user_synthesis claim with "
                            "author_origin=true."
                        ),
                        node_id=c["id"],
                    )
                )


def _check_cached_html_fresh(
    cached_html_path: Path,
    input_paths: list[Path],
    report: DiagramReadinessReport,
) -> None:
    if not cached_html_path.exists():
        return  # not stale if it doesn't exist — the regen path will handle it
    try:
        cached_mtime = cached_html_path.stat().st_mtime
    except OSError:
        return
    for input_path in input_paths:
        if not input_path.exists():
            continue
        try:
            if input_path.stat().st_mtime > cached_mtime:
                report.findings.append(
                    DiagramFinding(
                        code="cached_html_stale",
                        severity="warning",
                        message=(
                            f"Cached diagram HTML at {cached_html_path} "
                            f"is older than {input_path} — re-render "
                            "before sharing the link."
                        ),
                    )
                )
                return
        except OSError:
            continue
