"""Scaffold audit (Phase 4a).

Runs after ingest. Checks structural quality of the author graph before
the renderer touches it, so problems with the *scaffold itself* (rather
than with the prose) get caught early. Distinct from:

- ``ingester.markdown.MarkdownOutlineIngester.last_report`` (parser-level
  diagnostics — malformed tags, raw excerpts, line numbers)
- ``auditor.readiness.DocumentReadinessCheck`` (post-render gate before
  delivery)

The checks here are advisory: each issue is captured as a structured
``ScaffoldAuditFinding``. The CLI / web UI surface them but do not block
ingest itself — the author may legitimately have an in-progress outline.

Checks:

1. **No empty sections** — every section that isn't a references stub
   should have at least one claim. Flag empty argumentative sections
   loudly; empty top-level sections that have populated children are
   acceptable (they're scaffolding for nested sections).
2. **No orphan claims** — every Claim.section_id should resolve to a
   real Section, and every Section.claim_ids entry to a real Claim.
3. **Evidence presence** — every non-`user_synthesis` claim either has
   an Evidence row or an explicit ``evidence_status`` (so the author has
   acknowledged the gap). Bare empirical/methodological/normative claims
   with no evidence and no status are flagged.
4. **Relationship targets resolve** — every Relationship's from/to
   claim_id must exist in the graph.
5. **Conclusion exists** — the document must have at least one section
   with role=conclusion (matches the auto-outliner's invariant).
6. **Thesis is connected** — the thesis claim must have at least one
   incoming edge (typically ``supports`` from MY VIEW claims). A thesis
   with no inbound argument is a strong signal that the body of the
   paper isn't actually arguing for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..graph.models import (
    AuthorGraph,
    ClaimType,
    SectionRole,
)


Severity = Literal["info", "warning", "error"]


@dataclass
class ScaffoldAuditFinding:
    """One structured finding from the scaffold audit."""

    code: str
    severity: Severity
    message: str
    section_id: str | None = None
    claim_id: str | None = None
    rel_id: str | None = None


@dataclass
class ScaffoldAuditReport:
    """Aggregate of every check's findings, plus a quick is_clean flag."""

    findings: list[ScaffoldAuditFinding] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")

    def by_code(self, code: str) -> list[ScaffoldAuditFinding]:
        return [f for f in self.findings if f.code == code]


def audit_scaffold(graph: AuthorGraph) -> ScaffoldAuditReport:
    """Run every scaffold check against ``graph`` and return the report.

    Pure function — no I/O, no LLM calls. Safe to call after every
    ingest pass; cheap enough to run synchronously.
    """
    report = ScaffoldAuditReport()
    sections_by_id = {s.section_id: s for s in graph.sections}
    claims_by_id = {c.claim_id: c for c in graph.claims}

    _check_no_empty_sections(graph, sections_by_id, report)
    _check_no_orphan_claims(graph, sections_by_id, claims_by_id, report)
    _check_evidence_presence(graph, claims_by_id, report)
    _check_relationship_targets_resolve(graph, claims_by_id, report)
    _check_conclusion_exists(graph, report)
    _check_thesis_connected(graph, claims_by_id, report)

    return report


# ─── individual checks ────────────────────────────────


def _check_no_empty_sections(
    graph: AuthorGraph,
    sections_by_id: dict,
    report: ScaffoldAuditReport,
) -> None:
    """An empty section can be acceptable as a scaffolding parent for
    nested subsections, but a leaf section with no claims and no children
    is dead weight the renderer would skip silently."""
    children_by_parent: dict[str, list] = {}
    for section in graph.sections:
        if section.parent:
            children_by_parent.setdefault(section.parent, []).append(section)

    for section in graph.sections:
        if section.role == SectionRole.references:
            continue
        if section.section_id == "s.thesis":
            continue
        if section.claim_ids:
            continue
        children = children_by_parent.get(section.section_id, [])
        if children:
            report.findings.append(
                ScaffoldAuditFinding(
                    code="empty_section_with_children",
                    severity="info",
                    message=(
                        f"Section {section.section_id!r} has no direct claims "
                        f"but {len(children)} subsection(s); intentional "
                        "scaffolding parent."
                    ),
                    section_id=section.section_id,
                )
            )
        else:
            report.findings.append(
                ScaffoldAuditFinding(
                    code="empty_section",
                    severity="error",
                    message=(
                        f"Section {section.section_id!r} ({section.title!r}) "
                        "has no claims and no subsections — the renderer "
                        "will produce no prose for it."
                    ),
                    section_id=section.section_id,
                )
            )


def _check_no_orphan_claims(
    graph: AuthorGraph,
    sections_by_id: dict,
    claims_by_id: dict,
    report: ScaffoldAuditReport,
) -> None:
    """A claim is an orphan if its ``section_id`` doesn't resolve, or if
    it isn't listed in any section's ``claim_ids``. Likewise, a section's
    ``claim_ids`` must point to real claims."""
    claims_in_some_section: set[str] = set()
    for section in graph.sections:
        for cid in section.claim_ids:
            claims_in_some_section.add(cid)
            if cid not in claims_by_id:
                report.findings.append(
                    ScaffoldAuditFinding(
                        code="dangling_section_claim_ref",
                        severity="error",
                        message=(
                            f"Section {section.section_id!r} lists claim "
                            f"{cid!r} but no such claim exists in the graph."
                        ),
                        section_id=section.section_id,
                        claim_id=cid,
                    )
                )

    for claim in graph.claims:
        if claim.section_id and claim.section_id not in sections_by_id:
            report.findings.append(
                ScaffoldAuditFinding(
                    code="claim_unknown_section",
                    severity="error",
                    message=(
                        f"Claim {claim.claim_id!r} points to section "
                        f"{claim.section_id!r} which doesn't exist."
                    ),
                    claim_id=claim.claim_id,
                    section_id=claim.section_id,
                )
            )
        if claim.claim_id not in claims_in_some_section and claim.claim_id != "cl.thesis":
            # cl.thesis sits in s.thesis which is added by the ingester;
            # check separately so we don't double-flag.
            report.findings.append(
                ScaffoldAuditFinding(
                    code="orphan_claim",
                    severity="warning",
                    message=(
                        f"Claim {claim.claim_id!r} is not listed in any "
                        "section's claim_ids — it won't be planned or "
                        "rendered."
                    ),
                    claim_id=claim.claim_id,
                )
            )


def _check_evidence_presence(
    graph: AuthorGraph,
    claims_by_id: dict,
    report: ScaffoldAuditReport,
) -> None:
    """Empirical / methodological / normative / definition claims need
    either at least one Evidence row OR an explicit ``evidence_status``
    so the author has signed off on the gap. ``user_synthesis`` claims
    are exempt — they're the author's own analytical moves."""
    NEEDS_EVIDENCE = {
        ClaimType.empirical,
        ClaimType.methodological,
        ClaimType.normative,
        ClaimType.definition,
    }
    for claim in graph.claims:
        if claim.type not in NEEDS_EVIDENCE:
            continue
        if claim.evidence:
            continue
        if claim.evidence_status is not None:
            continue
        report.findings.append(
            ScaffoldAuditFinding(
                code="claim_missing_evidence_signal",
                severity="warning",
                message=(
                    f"{claim.type.value} claim {claim.claim_id!r} has no "
                    "Evidence rows and no [evidence_status: ...] tag. "
                    "Add a [ref:] tag, an [evidence_status: source_hint] "
                    "or [evidence_status: unbound] to acknowledge the gap, "
                    "or convert to [type: user_synthesis]."
                ),
                claim_id=claim.claim_id,
            )
        )


def _check_relationship_targets_resolve(
    graph: AuthorGraph,
    claims_by_id: dict,
    report: ScaffoldAuditReport,
) -> None:
    for rel in graph.relationships:
        if rel.from_claim not in claims_by_id:
            report.findings.append(
                ScaffoldAuditFinding(
                    code="relationship_dangling_from",
                    severity="error",
                    message=(
                        f"Relationship {rel.rel_id!r} ({rel.type.value}) "
                        f"has from-claim {rel.from_claim!r} which isn't in "
                        "the graph."
                    ),
                    rel_id=rel.rel_id,
                    claim_id=rel.from_claim,
                )
            )
        if rel.to_claim not in claims_by_id:
            report.findings.append(
                ScaffoldAuditFinding(
                    code="relationship_dangling_to",
                    severity="error",
                    message=(
                        f"Relationship {rel.rel_id!r} ({rel.type.value}) "
                        f"has to-claim {rel.to_claim!r} which isn't in "
                        "the graph."
                    ),
                    rel_id=rel.rel_id,
                    claim_id=rel.to_claim,
                )
            )


def _check_conclusion_exists(
    graph: AuthorGraph,
    report: ScaffoldAuditReport,
) -> None:
    if any(s.role == SectionRole.conclusion for s in graph.sections):
        return
    report.findings.append(
        ScaffoldAuditFinding(
            code="no_conclusion_section",
            severity="error",
            message=(
                "Document has no section with role=conclusion. The voice "
                "templates require a closing section; finalise will refuse "
                "to deliver. Add a `# Z. Conclusion [role: conclusion]` "
                "section."
            ),
        )
    )


def _check_thesis_connected(
    graph: AuthorGraph,
    claims_by_id: dict,
    report: ScaffoldAuditReport,
) -> None:
    if "cl.thesis" not in claims_by_id:
        report.findings.append(
            ScaffoldAuditFinding(
                code="no_thesis_claim",
                severity="error",
                message=(
                    "Document has no `cl.thesis` claim. Add a `# THESIS` "
                    "block at the top of the outline."
                ),
                claim_id="cl.thesis",
            )
        )
        return
    inbound = [
        r for r in graph.relationships if r.to_claim == "cl.thesis"
    ]
    if not inbound:
        report.findings.append(
            ScaffoldAuditFinding(
                code="thesis_disconnected",
                severity="warning",
                message=(
                    "The thesis claim has no inbound relationships — no "
                    "claim in the body explicitly supports or contradicts "
                    "it. Add `MY VIEW:` claims tied to the thesis, or use "
                    "`[supports: thesis]` / `[contradicts: thesis]` tags."
                ),
                claim_id="cl.thesis",
            )
        )
