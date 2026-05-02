"""Document readiness check (Fix 1 of the pipeline-integrity brief).

Distinct from per-cluster audit checks. Verifies a document is fit to
deliver to the user. Runs after rendering completes and before the
audit stage. Failures block delivery until the author has resolved them.

Conditions checked (each produces blocking AuditFlags):

1. No clusters in state ``failed`` or ``not_yet_rendered``
2. No rendered prose contains ``{MISSING_CLAIM:...}`` or
   ``{CLUSTER_UNRENDERABLE:...}`` markers
3. All sections required by the voice's architecture template are present
4. Every section in the cluster plan has at least one rendered cluster
5. The document ends with one of the template's required closing section
   roles
6. No prose contains register bleed (belt and braces — should already be
   caught at render time but checked again on stored prose)
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import (
    AuditFlag,
    EditMode,
    FlagCategory,
    ProseLocation,
    ProseState,
    Severity,
)
from ..graph.store import GraphStore
from ..voice.parser import Voice


@dataclass
class ReadinessReport:
    is_ready: bool
    blocking_flags: list[AuditFlag] = field(default_factory=list)
    summary: str = ""


_DEFAULT_LOCATION = ProseLocation(paragraph_index=0, char_start=0, char_end=0)


def _short_uid() -> str:
    return uuid.uuid4().hex[:6]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentReadinessCheck:
    """Document-level readiness check.

    Runs after rendering completes. Produces flags for any blocking
    condition. The pipeline must not advance to finalise until the
    readiness report returns ``is_ready=True``.
    """

    REQUIRED_SECTIONS_BY_TEMPLATE: dict[str, list[str]] = {
        # Each entry is a list of section role-tokens that must appear at
        # least once. Matching is forgiving: either a section.role.value
        # exact match OR the token appearing as a substring in the title.
        "six_element_paper": [
            "introduction",
            "argumentative",
            "conclusion",
        ],
        "review_paper": [
            "history",
            "techniques",
            "results",
            "synthesis",
            "gaps",
        ],
        "policy_brief": [
            "bottom_line",
            "findings",
            "evidence",
            "implications",
            "recommendations",
        ],
        "journalistic_feature": ["hook", "nut_graf", "reporting", "close"],
        "nature_compressed": ["introduction", "argumentative", "conclusion"],
        "freeform": [],
    }

    REQUIRED_CLOSING_BY_TEMPLATE: dict[str, list[str]] = {
        "six_element_paper": ["conclusion"],
        "review_paper": ["synthesis", "conclusion", "evidence_synthesis"],
        "policy_brief": ["conclusion"],
        "journalistic_feature": ["conclusion"],
        "nature_compressed": ["conclusion"],
        "freeform": [],
    }

    def __init__(
        self,
        store: GraphStore,
        voice: Voice,
        project_path: Path,
    ) -> None:
        self.store = store
        self.voice = voice
        self.project_path = Path(project_path)

    # ─── public entry point ───────────────────────

    def check(self) -> ReadinessReport:
        flags: list[AuditFlag] = []
        flags.extend(self._check_no_failed_clusters())
        flags.extend(self._check_no_unresolved_markers())
        flags.extend(self._check_required_sections_present())
        flags.extend(self._check_sections_have_prose())
        flags.extend(self._check_document_has_closing_section())
        flags.extend(self._check_no_register_bleed())
        flags.extend(self._check_source_order())
        # Phase 4b additions — pre-render quality gates (run alongside
        # the post-render gates above so a single readiness pass surfaces
        # both kinds of issue).
        flags.extend(self._check_weak_grounding_marked())
        flags.extend(self._check_relationship_aware_clusters())
        flags.extend(self._check_sane_word_ranges())
        is_ready = not flags
        return ReadinessReport(
            is_ready=is_ready,
            blocking_flags=flags,
            summary=self._build_summary(flags, is_ready),
        )

    # ─── individual checks ─────────────────────

    def _check_no_failed_clusters(self) -> list[AuditFlag]:
        from ..graph.models import SectionRole
        flags: list[AuditFlag] = []
        # Skip clusters whose section is references — those aren't expected
        # to render at all.
        sections_by_id = {s.section_id: s for s in self.store.list_sections()}
        for cluster in self.store.list_clusters():
            section = sections_by_id.get(cluster.section_id)
            if section and section.role == SectionRole.references:
                continue
            if cluster.prose_state in (ProseState.failed, ProseState.not_yet_rendered):
                flags.append(self._mk_flag(
                    rule_id="readiness.cluster_not_rendered",
                    cluster_id=cluster.cluster_id,
                    section_id=cluster.section_id,
                    description=(
                        f"Cluster {cluster.cluster_id} is in state "
                        f"{cluster.prose_state.value} and cannot be delivered."
                    ),
                    suggestion=(
                        "Re-run rendering for this cluster, or add evidence "
                        "bindings (or mark claims user_synthesis) so it becomes renderable."
                    ),
                    category=FlagCategory.coverage,
                ))
        return flags

    def _check_no_unresolved_markers(self) -> list[AuditFlag]:
        marker_specs: list[tuple[re.Pattern, str]] = [
            (re.compile(r'\{MISSING_CLAIM:[^}]*\}'), "missing_claim_marker"),
            (re.compile(r'\{CLUSTER_UNRENDERABLE:[^}]*\}'), "unrenderable_marker"),
        ]
        flags: list[AuditFlag] = []
        for cluster in self.store.list_clusters():
            prose = self._read_cluster_prose(cluster)
            if not prose:
                continue
            for pattern, kind in marker_specs:
                for match in pattern.finditer(prose):
                    flags.append(self._mk_flag(
                        rule_id=f"readiness.{kind}_present",
                        cluster_id=cluster.cluster_id,
                        section_id=cluster.section_id,
                        offending_text=match.group(0),
                        location=ProseLocation(
                            paragraph_index=0,
                            char_start=match.start(),
                            char_end=match.end(),
                        ),
                        description=(
                            "Rendered prose contains an unresolved marker. "
                            "The cluster cannot be delivered until the gap is filled."
                        ),
                        suggestion=(
                            "Add the missing claim or evidence binding, "
                            "then re-render the cluster."
                        ),
                        category=FlagCategory.coverage,
                    ))
        return flags

    def _check_required_sections_present(self) -> list[AuditFlag]:
        template = self.voice.architecture.template
        required = self.REQUIRED_SECTIONS_BY_TEMPLATE.get(template, [])
        if not required:
            return []
        sections = self.store.list_sections()
        present_roles = {s.role.value for s in sections}
        present_titles_lower = {s.title.lower() for s in sections}

        flags: list[AuditFlag] = []
        for req in required:
            req_lower = req.lower()
            role_match = req_lower in present_roles
            title_match = any(req_lower in t for t in present_titles_lower)
            if not (role_match or title_match):
                flags.append(self._mk_flag(
                    rule_id="readiness.required_section_missing",
                    cluster_id="",
                    section_id="",
                    description=(
                        f"The {template} template requires a section with role "
                        f"or title containing {req!r}. None was found."
                    ),
                    suggestion=(
                        f"Add a section to your outline with role {req!r}, or "
                        f"switch architecture template in the voice file."
                    ),
                    category=FlagCategory.architecture,
                ))
        return flags

    def _check_sections_have_prose(self) -> list[AuditFlag]:
        from ..graph.models import SectionRole
        flags: list[AuditFlag] = []
        for section in self.store.list_sections():
            if section.section_id == "s.thesis":
                continue
            if section.role == SectionRole.references:
                continue
            clusters = self.store.list_clusters(section_id=section.section_id)
            if not clusters:
                flags.append(self._mk_flag(
                    rule_id="readiness.section_has_no_clusters",
                    cluster_id="",
                    section_id=section.section_id,
                    description=(
                        f"Section {section.title!r} has no clusters. The "
                        "assembler did not produce any rendering plan for it."
                    ),
                    suggestion=(
                        "Check the section's claim_ids in the author graph. "
                        "If empty, add claims to the outline. If populated, "
                        "re-run `lattice plan`."
                    ),
                    category=FlagCategory.architecture,
                ))
                continue
            any_rendered = any(
                c.prose_state in (
                    ProseState.generated, ProseState.edited, ProseState.needs_review
                )
                for c in clusters
            )
            if not any_rendered:
                flags.append(self._mk_flag(
                    rule_id="readiness.section_has_no_prose",
                    cluster_id="",
                    section_id=section.section_id,
                    description=(
                        f"Section {section.title!r} has clusters but none "
                        "rendered successfully."
                    ),
                    suggestion="Re-run `lattice render` for this section.",
                    category=FlagCategory.architecture,
                ))
        return flags

    def _check_document_has_closing_section(self) -> list[AuditFlag]:
        from ..graph.models import SectionRole
        template = self.voice.architecture.template
        required_closings = self.REQUIRED_CLOSING_BY_TEMPLATE.get(template, [])
        if not required_closings:
            return []
        sections = sorted(
            (
                s for s in self.store.list_sections()
                if s.section_id != "s.thesis"
                and s.role != SectionRole.references
            ),
            key=lambda s: s.position,
        )
        if not sections:
            return []
        last_section = sections[-1]
        if last_section.role.value not in required_closings:
            return [self._mk_flag(
                rule_id="readiness.document_lacks_closing",
                cluster_id="",
                section_id=last_section.section_id,
                description=(
                    f"The {template} template requires the document to end "
                    f"with one of: {required_closings}. Document ends with "
                    f"section role {last_section.role.value!r} ({last_section.title!r})."
                ),
                suggestion=(
                    f"Add a closing section with one of these roles: "
                    f"{required_closings}, or change the last section's role."
                ),
                category=FlagCategory.architecture,
            )]
        return []

    def _check_no_register_bleed(self) -> list[AuditFlag]:
        # Reuse the renderer's validator for consistency.
        from ..renderer.cluster_renderer import validate_response

        flags: list[AuditFlag] = []
        for cluster in self.store.list_clusters():
            prose = self._read_cluster_prose(cluster)
            if not prose:
                continue
            validation = validate_response(prose)
            if validation.is_valid:
                continue
            for violation in validation.violations[:8]:
                flags.append(self._mk_flag(
                    rule_id="readiness.register_bleed",
                    cluster_id=cluster.cluster_id,
                    section_id=cluster.section_id,
                    offending_text=violation,
                    description=(
                        "Prose contains conversational meta-commentary "
                        "(renderer addressing the user, asking questions, "
                        "or refusing the task in prose)."
                    ),
                    suggestion="Re-render this cluster with `--force`.",
                    category=FlagCategory.voice,
                ))
        return flags

    def _check_source_order(self) -> list[AuditFlag]:
        from .ordering import OrderingCheck
        return OrderingCheck(self.store, self.voice).check().flags

    # ─── Phase 4b: pre-render quality gates ──────

    def _check_weak_grounding_marked(self) -> list[AuditFlag]:
        """Empirical / methodological / normative / definition claims
        that have neither evidence rows nor an ``evidence_status`` are
        weak grounding — the renderer would emit MISSING_CLAIM markers
        for them. Flag them BEFORE drafting so the author sees the gap
        in advance."""
        from ..graph.models import ClaimType, SectionRole
        flags: list[AuditFlag] = []
        graph = self.store.get_graph()
        sections_by_id = {s.section_id: s for s in graph.sections}
        clusters_by_claim: dict[str, str] = {}
        for cluster in self.store.list_clusters():
            for entry in cluster.claim_sequence:
                clusters_by_claim[entry.claim_id] = cluster.cluster_id

        needs_evidence = {
            ClaimType.empirical,
            ClaimType.methodological,
            ClaimType.normative,
            ClaimType.definition,
        }
        for claim in graph.claims:
            if claim.type not in needs_evidence:
                continue
            if claim.evidence or claim.evidence_status is not None:
                continue
            section = sections_by_id.get(claim.section_id) if claim.section_id else None
            if section and section.role == SectionRole.references:
                continue
            cluster_id = clusters_by_claim.get(claim.claim_id, "")
            flags.append(self._mk_flag(
                rule_id="readiness.claim_weak_grounding",
                cluster_id=cluster_id,
                section_id=claim.section_id or "",
                description=(
                    f"{claim.type.value} claim {claim.claim_id!r} has no "
                    "Evidence rows and no evidence_status — render will "
                    "emit a MISSING_CLAIM marker."
                ),
                suggestion=(
                    "Add a [ref:] tag, an [evidence_status: source_hint] / "
                    "[evidence_status: unbound] tag, or convert the claim "
                    "to [type: user_synthesis]."
                ),
                category=FlagCategory.coverage,
            ))
        return flags

    def _check_relationship_aware_clusters(self) -> list[AuditFlag]:
        """Clusters whose claims are densely connected via sticky
        relationships (qualifies / extends / depends_on / pivot /
        contradicts) need every endpoint inside the cluster *or* an
        ``incoming``/``outgoing`` ClusterRelationshipContext entry that
        names the other cluster. A cluster with two intra-cluster claims
        joined by a sticky edge plus zero relationship_context entries
        means the assembler ran on an older schema and the cluster
        should be re-planned."""
        flags: list[AuditFlag] = []
        graph = self.store.get_graph()
        sticky_pairs: set[frozenset[str]] = set()
        from ..graph.models import RelationshipType
        sticky_types = {
            RelationshipType.interpretive_pivot,
            RelationshipType.qualifies,
            RelationshipType.extends,
            RelationshipType.depends_on,
            RelationshipType.contradicts,
        }
        for rel in graph.relationships:
            if rel.type in sticky_types:
                sticky_pairs.add(frozenset({rel.from_claim, rel.to_claim}))

        for cluster in self.store.list_clusters():
            ids = {entry.claim_id for entry in cluster.claim_sequence}
            sticky_inside = sum(
                1 for pair in sticky_pairs
                if pair.issubset(ids)
            )
            if sticky_inside == 0:
                continue
            if cluster.relationship_context:
                continue
            flags.append(self._mk_flag(
                rule_id="readiness.cluster_missing_relationship_context",
                cluster_id=cluster.cluster_id,
                section_id=cluster.section_id,
                description=(
                    f"Cluster {cluster.cluster_id} contains "
                    f"{sticky_inside} sticky-edge pair(s) but has no "
                    "relationship_context payload — re-run the planner "
                    "so the renderer sees the relationships."
                ),
                suggestion="Run `lattice plan` (or the Scaffold activity) again.",
                category=FlagCategory.coverage,
            ))
        return flags

    def _check_sane_word_ranges(self) -> list[AuditFlag]:
        """Cluster target_words must define a usable band: min ≤ max,
        min ≥ 80 (smaller than that and the renderer can't develop the
        claims at all), max ≤ 800 (larger and a single cluster blows
        the chunked-renderer's per-call budget). Out-of-band ranges are
        usually a sign the assembler ran on a malformed claim_count."""
        flags: list[AuditFlag] = []
        for cluster in self.store.list_clusters():
            issues: list[str] = []
            if cluster.target_words_min > cluster.target_words_max:
                issues.append(
                    f"min ({cluster.target_words_min}) > max "
                    f"({cluster.target_words_max})"
                )
            if cluster.target_words_min < 80:
                issues.append(
                    f"min ({cluster.target_words_min}) below the 80-word "
                    "renderer floor"
                )
            if cluster.target_words_max > 800:
                issues.append(
                    f"max ({cluster.target_words_max}) above the 800-word "
                    "chunked-renderer cap"
                )
            if not issues:
                continue
            flags.append(self._mk_flag(
                rule_id="readiness.cluster_word_range_unsane",
                cluster_id=cluster.cluster_id,
                section_id=cluster.section_id,
                description=(
                    f"Cluster {cluster.cluster_id} has an unworkable "
                    f"target_words band: {'; '.join(issues)}."
                ),
                suggestion=(
                    "Re-run `lattice plan` after pruning or splitting the "
                    "cluster's claims; or set explicit `[words: ...]` tags "
                    "on the claims to control density."
                ),
                category=FlagCategory.architecture,
            ))
        return flags

    # ─── helpers ────────────────────────────────

    def _read_cluster_prose(self, cluster) -> str | None:
        if not cluster.prose_file:
            return None
        path = self.project_path / cluster.prose_file
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def _mk_flag(
        self,
        *,
        rule_id: str,
        cluster_id: str,
        section_id: str,
        description: str,
        suggestion: str,
        category: FlagCategory,
        severity: Severity = Severity.critical,
        default_mode: EditMode = EditMode.rewrite,
        location: ProseLocation = _DEFAULT_LOCATION,
        offending_text: str = "",
    ) -> AuditFlag:
        return AuditFlag(
            flag_id=f"f.readiness.{_short_uid()}",
            category=category,
            rule_id=rule_id,
            severity=severity,
            default_mode=default_mode,
            cluster_id=cluster_id,
            section_id=section_id,
            prose_location=location,
            offending_text=offending_text,
            rule_description=description,
            suggestion=suggestion,
            voice_name=self.voice.name,
            created_at=_now(),
        )

    def _build_summary(self, flags: list[AuditFlag], is_ready: bool) -> str:
        if is_ready:
            return "Document is ready for delivery."
        lines = [
            f"Document NOT ready for delivery. {len(flags)} blocking issue(s):",
            "",
        ]
        by_rule: dict[str, list[AuditFlag]] = {}
        for f in flags:
            by_rule.setdefault(f.rule_id, []).append(f)
        for rule_id, rule_flags in sorted(by_rule.items()):
            lines.append(f"- {rule_id}: {len(rule_flags)} flag(s)")
            # Show one example per rule.
            example = rule_flags[0]
            lines.append(f"    {example.rule_description}")
            if example.suggestion:
                lines.append(f"    -> {example.suggestion}")
        lines.append("")
        lines.append(
            "Run `lattice flags <project> --voice <voice>` to review and resolve."
        )
        return "\n".join(lines)
