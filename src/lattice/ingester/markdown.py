"""Markdown outline ingester — parses the SPEC §4.2 outline format.

Produces an AuthorGraph with sections, claims, and relationships from a
tag-annotated markdown outline. Deterministic for explicitly-tagged
bullets; untagged bullets default to empirical/low-confidence (a future
enhancement can invoke the LLM inference from PROMPTS.md Stage 1.6).

Section IDs:   s.<letter>                        e.g. s.c
Thesis:        s.thesis  /  cl.thesis
Claim IDs:     cl.<section_letter>.<seq>          e.g. cl.c.3
Rel IDs:       r.<seq>
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import (
    AuthorGraph,
    BindingStrength,
    Claim,
    ClaimType,
    Confidence,
    Depth,
    Evidence,
    EvidenceStatus,
    Relationship,
    RelationshipStrength,
    RelationshipType,
    ScaffoldClaimReport,
    ScaffoldReport,
    ScaffoldWarning,
    ScaffoldWarningLevel,
    Section,
    SectionRole,
)
from ..utils.config import Config


# Section heading: ``# A.``, ``## A.1``, ``### A.1.2`` etc. Up to three
# levels of nesting. The hash count must match the number of components
# in the path (``A`` = 1, ``A.1`` = 2, ``A.1.2`` = 3) — mismatches are
# rejected so the author gets a parse error instead of silently mis-nested
# sections.
# Path component after the hashes can be ``A``, ``A.1``, ``A.1.2``.
# The path is optionally followed by a trailing dot (``# A. Title``,
# legacy single-letter form) but subsection paths typically don't use
# one (``## A.1 Title``). Either way, whitespace separates the path
# from the title.
_SECTION_RE = re.compile(
    r"^(#+)\s+([A-Z](?:\.\d+)*)(?:\.\s+|\s+)(.+?)\s*$"
)
_THESIS_RE = re.compile(r"^#\s*THESIS\s*$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^(\s*)-\s+(.*)$")
_TAG_RE = re.compile(r"\[([^\]]+)\]")
_FIGURE_RE = re.compile(r"^figure\s*\d+", re.IGNORECASE)

_CONFIDENCE_TAGS = {
    "weak": Confidence.low,
    "contested": Confidence.medium,
    "strong": Confidence.high,
}

_TYPE_TAG_MAP = {
    "empirical": ClaimType.empirical,
    "methodological": ClaimType.methodological,
    "normative": ClaimType.normative,
    "user_synthesis": ClaimType.user_synthesis,
    "definition": ClaimType.definition,
}

_EVIDENCE_STATUS_MAP = {
    "unbound": EvidenceStatus.unbound,
    "source_hint": EvidenceStatus.source_hint,
    "bound": EvidenceStatus.bound,
}

# Maps an inline relationship tag to the RelationshipType it creates from
# the current claim to the named target. ``pivot`` is the short alias for
# ``interpretive_pivot`` because the latter is a mouthful in outline tags.
_RELATIONSHIP_TAGS: dict[str, RelationshipType] = {
    "supports": RelationshipType.supports,
    "contradicts": RelationshipType.contradicts,
    "qualifies": RelationshipType.qualifies,
    "extends": RelationshipType.extends,
    "depends_on": RelationshipType.depends_on,
    "is_counterexample_to": RelationshipType.is_counterexample_to,
    "pivot": RelationshipType.interpretive_pivot,
    "interpretive_pivot": RelationshipType.interpretive_pivot,
}

_SECTION_ROLE_MAP = {
    "introduction": SectionRole.introduction,
    "argumentative": SectionRole.argumentative,
    "evidence_synthesis": SectionRole.evidence_synthesis,
    "methodological": SectionRole.methodological,
    "counterargument": SectionRole.counterargument,
    "conclusion": SectionRole.conclusion,
    "discussion": SectionRole.conclusion,  # treat discussion as conclusion role
    "appendix": SectionRole.appendix,
    "references": SectionRole.references,
}

_DEPTH_MAP = {d.value: d for d in Depth}


class MarkdownOutlineIngester:
    """Parses a tagged markdown outline into an AuthorGraph.

    The `llm` argument is accepted for future LLM-assisted claim-type
    inference but is unused in this deterministic implementation.
    """

    def __init__(self, config: Config, llm: object | None = None) -> None:
        self.config = config
        self.llm = llm
        # Populated by ``ingest``/``_parse``. Callers (CLI, web) read this
        # after a successful parse and persist it to ``.lattice/scaffold_report.json``.
        self.last_report: ScaffoldReport | None = None

    async def ingest(self, file_path: Path, project_name: str) -> AuthorGraph:
        text = file_path.read_text(encoding="utf-8")
        graph = self._parse(text, project_name)
        if self.last_report is not None:
            self.last_report.source_file = str(file_path)
        return graph

    def save_scaffold_report(
        self,
        project_path: Path,
        *,
        known_source_ids: set[str] | None = None,
        auto_outliner_summary: object | None = None,
    ) -> Path | None:
        """Persist ``self.last_report`` to ``.lattice/scaffold_report.json``.

        - ``known_source_ids``: refs that resolve to a real Source in the
          source store get filtered out of each claim_report's
          ``unresolved_refs`` list. Pass ``None`` to skip resolution and
          report every ref as unresolved (e.g. before the indexer ran).
        - ``auto_outliner_summary``: when the outline was first generated
          by the LLM auto-outliner, pass its summary to embed it under
          ``ScaffoldReport.auto_outliner``. Typed as ``object`` to avoid
          a circular import; expected to be an ``AutoOutlinerSummary``.

        Returns the written path, or ``None`` if there is no report to
        write yet (e.g. ``ingest`` hasn't run).
        """
        report = self.last_report
        if report is None:
            return None
        if known_source_ids is not None:
            # Recompute unresolved_refs from the immutable cited_refs
            # snapshot so this method is idempotent — previous saves
            # don't poison later ones if the indexed source set shrinks.
            for claim_report in report.claim_reports:
                claim_report.unresolved_refs = [
                    ref for ref in claim_report.cited_refs
                    if ref not in known_source_ids
                ]
        if auto_outliner_summary is not None:
            # Lazy import to avoid the circular import auto_outliner →
            # markdown → auto_outliner.
            from ..graph.models import AutoOutlinerSummary
            if isinstance(auto_outliner_summary, AutoOutlinerSummary):
                report.auto_outliner = auto_outliner_summary
        target = project_path / ".lattice" / "scaffold_report.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return target

    # ─── core parse ────────────────────────────────────

    def _parse(self, text: str, project_name: str) -> AuthorGraph:
        now = datetime.now(timezone.utc)
        graph = AuthorGraph(
            project_name=project_name,
            thesis_statement=None,
            sections=[],
            claims=[],
            relationships=[],
            created_at=now,
            modified_at=now,
        )
        report = ScaffoldReport(
            project_name=project_name,
            generated_at=now,
            parser="markdown_ingester",
        )
        self.last_report = report

        state: dict[str, object] = {
            "mode": None,                # "thesis" | "section" | None
            "section": None,             # current (innermost) Section
            "section_letter": None,      # legacy: top-level letter for claim-id derivation
            "open_sections": [],         # stack of Sections, deepest last; used to set parent
            "thesis_buffer": [],         # collected thesis paragraphs
            "thesis_claim_id": None,     # set once thesis is committed
            "pending_claim": None,       # dict being built
            "claim_seq_per_section": {}, # section_id -> int (was: letter -> int)
            "rel_seq": 0,
            "deferred_rels": [],         # (from_id, target, type) to resolve after pass
            "source_order_seq": 0,       # monotonic claim counter across the whole document
        }

        def _commit_pending_claim() -> None:
            pending = state["pending_claim"]
            if not pending:
                return
            section: Section | None = state["section"]
            if section is None:
                state["pending_claim"] = None
                return
            claim, rels = self._finalise_claim(pending, section, state, now, report)
            if claim is not None:
                graph.claims.append(claim)
                section.claim_ids.append(claim.claim_id)
                for rel in rels:
                    graph.relationships.append(rel)
            state["pending_claim"] = None

        def _commit_thesis() -> None:
            thesis_text = " ".join(s.strip() for s in state["thesis_buffer"]).strip()
            if not thesis_text:
                return
            graph.thesis_statement = thesis_text
            state["source_order_seq"] += 1
            thesis_claim = Claim(
                claim_id="cl.thesis",
                statement=thesis_text,
                source_order=state["source_order_seq"],
                type=ClaimType.user_synthesis,
                confidence=Confidence.high,
                author_origin=True,
                section_id="s.thesis",
                created_by="markdown_ingester",
                created_at=now,
                modified_at=now,
                tags=["thesis"],
            )
            graph.claims.append(thesis_claim)
            state["thesis_claim_id"] = thesis_claim.claim_id
            report.claim_reports.append(
                ScaffoldClaimReport(
                    claim_id=thesis_claim.claim_id,
                    section_id="s.thesis",
                    original_excerpt=thesis_text,
                    extracted_statement=thesis_text,
                    confidence=1.0,
                )
            )
            graph.sections.insert(
                0,
                Section(
                    section_id="s.thesis",
                    title="Thesis",
                    parent=None,
                    position=0,
                    role=SectionRole.introduction,
                    thesis_claim="cl.thesis",
                    claim_ids=["cl.thesis"],
                    target_length=0,
                    depth=Depth.standard,
                ),
            )

        lines = text.splitlines()
        for line_no, raw_line in enumerate(lines, start=1):
            line = raw_line.rstrip()

            if _THESIS_RE.match(line):
                _commit_pending_claim()
                if state["mode"] == "thesis":
                    _commit_thesis()
                state["mode"] = "thesis"
                state["thesis_buffer"] = []
                state["section"] = None
                state["section_letter"] = None
                continue

            section_match = _SECTION_RE.match(line)
            if section_match:
                _commit_pending_claim()
                if state["mode"] == "thesis":
                    _commit_thesis()
                hashes = section_match.group(1)
                path = section_match.group(2)            # e.g. "A", "A.1", "A.1.2"
                raw_title = section_match.group(3)
                section_depth = len(hashes)              # 1 = top, 2 = sub, 3 = sub-sub
                path_components = path.split(".")        # ["A"], ["A", "1"], ["A", "1", "2"]
                # Skip mismatched: e.g. "## A." (depth 2, one path component) is treated
                # as a nesting bug; fall through to the legacy single-letter path so the
                # ingest doesn't crash. Older outlines using "# A." are unaffected.
                if section_depth != len(path_components):
                    # Best-effort: fall back to top-level interpretation if the path
                    # is just a single letter and the heading was deeper than expected.
                    if len(path_components) == 1 and section_depth >= 1:
                        section_depth = 1
                    else:
                        # Genuinely malformed — skip the line so we don't
                        # poison downstream sections with a phantom one.
                        continue

                title, tags = _parse_tags(raw_title)
                role = _SECTION_ROLE_MAP.get(
                    (tags.get("role") or [""])[0], SectionRole.argumentative
                )
                depth_tag = _DEPTH_MAP.get((tags.get("depth") or [""])[0], Depth.standard)
                target_length = int((tags.get("words") or ["800"])[0])

                # Derive section_id and parent. Top-level keeps the legacy
                # "s.<letter>" shape so existing projects don't change ids.
                top_letter = path_components[0]
                if section_depth == 1:
                    section_id = f"s.{top_letter.lower()}"
                    parent_id = None
                else:
                    section_id = (
                        f"s.{top_letter.lower()}." + ".".join(path_components[1:])
                    )
                    # Parent is the path with one fewer component.
                    if section_depth == 2:
                        parent_id = f"s.{top_letter.lower()}"
                    else:
                        parent_id = (
                            f"s.{top_letter.lower()}." +
                            ".".join(path_components[1:-1])
                        )

                # Pop any deeper sections off the open stack — a new
                # heading at this depth ends them.
                stack: list[Section] = state["open_sections"]  # type: ignore[assignment]
                while stack and (stack[-1].section_id.count(".") >= section_id.count(".")):
                    stack.pop()
                # If parent_id is set, the parent must be on the stack (at the
                # right depth). If it isn't, we have an out-of-order heading
                # (e.g. ``## A.1`` with no preceding ``# A.``); pretend it's
                # top-level instead.
                if parent_id is not None and (
                    not stack or stack[-1].section_id != parent_id
                ):
                    # Promote to top-level rather than dropping the section.
                    section_id = f"s.{top_letter.lower()}"
                    parent_id = None
                    section_depth = 1

                section = Section(
                    section_id=section_id,
                    title=title.strip(),
                    parent=parent_id,
                    position=len(graph.sections),
                    role=role,
                    thesis_claim=state["thesis_claim_id"],
                    claim_ids=[],
                    target_length=target_length,
                    depth=depth_tag,
                )
                graph.sections.append(section)
                stack.append(section)
                state["mode"] = "section"
                state["section"] = section
                # ``section_letter`` is kept for legacy claim-id derivation —
                # claims are always tagged with their innermost section's path,
                # but the readable letter prefix stays the top-level one.
                state["section_letter"] = top_letter
                state["claim_seq_per_section"][section_id] = 0
                continue

            bullet_match = _BULLET_RE.match(line)
            if bullet_match and state["mode"] == "section":
                _commit_pending_claim()
                body = bullet_match.group(2)
                state["pending_claim"] = {"raw": body, "line": line_no}
                continue

            if state["mode"] == "thesis" and line.strip():
                state["thesis_buffer"].append(line.strip())
                continue

            if state["mode"] == "section" and line.strip() and state["pending_claim"]:
                # continuation of previous bullet
                pending = state["pending_claim"]
                pending["raw"] = pending["raw"] + " " + line.strip()
                continue

            # blank line terminates pending thesis buffer only if we're in thesis mode
            # (otherwise just keep state unchanged)

        # Flush at EOF
        _commit_pending_claim()
        if state["mode"] == "thesis":
            _commit_thesis()

        # Resolve deferred relationship targets (e.g. ``[supports: cl.x]``
        # where ``cl.x`` was declared later in the file). Anything still
        # unresolved becomes a scaffold warning so the author sees it.
        known_claim_ids = {c.claim_id for c in graph.claims}
        for claim_report in report.claim_reports:
            still_unresolved = [
                t for t in claim_report.unresolved_targets
                if t not in known_claim_ids
            ]
            claim_report.unresolved_targets = still_unresolved
            for target in still_unresolved:
                report.warnings.append(
                    ScaffoldWarning(
                        level=ScaffoldWarningLevel.warning,
                        code="unresolved_relationship_target",
                        message=(
                            f"Relationship target {target!r} on claim "
                            f"{claim_report.claim_id} did not resolve to "
                            "any known claim_id."
                        ),
                        claim_id=claim_report.claim_id,
                        section_id=claim_report.section_id,
                    )
                )

        # Counts summary (lets consumers skip the per-claim payload when
        # they only need a top-line picture).
        report.counts = {
            "sections": len(graph.sections),
            "claims": len(graph.claims),
            "relationships": len(graph.relationships),
            "warnings": len(report.warnings),
            "claims_user_synthesis": sum(
                1 for c in graph.claims if c.type == ClaimType.user_synthesis
            ),
            "claims_with_evidence": sum(
                1 for c in graph.claims if c.evidence
            ),
            "claims_with_mechanism": sum(
                1 for c in graph.claims if c.mechanism
            ),
        }

        # Argument metrics — strength + breadth scores computed against
        # the parsed graph. Stored as a plain dict on the report so
        # ScaffoldReport doesn't have to import metrics types (avoids
        # tight coupling between the diagnostic schema and the metric
        # algorithm). Skipped silently if computation fails — the
        # report is still useful even without the scores.
        try:
            from ..graph.metrics import compute_argument_metrics
            report.argument_metrics = compute_argument_metrics(graph).model_dump()
        except Exception:  # pragma: no cover - defensive
            report.argument_metrics = None

        return graph

    # ─── finalise one claim ───────────────────────────

    def _finalise_claim(
        self,
        pending: dict,
        section: Section,
        state: dict[str, object],
        now: datetime,
        report: ScaffoldReport,
    ) -> tuple[Claim | None, list[Relationship]]:
        raw = pending["raw"]
        line_no = pending.get("line")
        statement_with_prefix, tags = _parse_tags(raw)

        # Per-claim diagnostics — populated as we go.
        claim_warnings: list[ScaffoldWarning] = []

        is_counter = False
        is_my_view = False
        statement = statement_with_prefix.strip()
        stripped = statement
        if stripped.upper().startswith("COUNTER:"):
            is_counter = True
            statement = stripped.split(":", 1)[1].strip()
        elif stripped.upper().startswith("MY VIEW:"):
            is_my_view = True
            statement = stripped.split(":", 1)[1].strip()

        # Figure bullets are special — they attach to section.figure_ids, no Claim.
        if _FIGURE_RE.match(statement):
            fig_slug = _slug(statement)
            section.figure_ids.append(f"fig.{fig_slug}")
            return None, []

        # Sequence per innermost section so subsections get their own
        # claim numbering; the readable claim_id keeps the top-level
        # letter prefix so existing single-level outlines are unchanged
        # (``cl.c.1``), while nested sections get an underscore-joined
        # path (``cl.c_1.1``) — unambiguous and sorts naturally.
        innermost_id = section.section_id  # e.g. "s.c" or "s.c.1"
        state["claim_seq_per_section"].setdefault(innermost_id, 0)
        state["claim_seq_per_section"][innermost_id] += 1
        seq = state["claim_seq_per_section"][innermost_id]
        section_key = innermost_id.removeprefix("s.").replace(".", "_")
        claim_id = f"cl.{section_key}.{seq}"
        state["source_order_seq"] += 1
        claim_source_order = state["source_order_seq"]

        # type — explicit ``[type: ...]`` overrides the prefix-based defaults.
        claim_type = ClaimType.empirical
        if is_my_view or is_counter or "user_synthesis" in tags:
            claim_type = ClaimType.user_synthesis
        explicit_type_values = tags.get("type", [])
        if explicit_type_values:
            requested = explicit_type_values[0].lower()
            if requested in _TYPE_TAG_MAP:
                claim_type = _TYPE_TAG_MAP[requested]
            else:
                claim_warnings.append(
                    ScaffoldWarning(
                        level=ScaffoldWarningLevel.warning,
                        code="unknown_claim_type",
                        message=(
                            f"Unknown [type: {requested}] tag on claim {claim_id}. "
                            f"Valid: {sorted(_TYPE_TAG_MAP.keys())}. Falling back "
                            "to prefix-derived type."
                        ),
                        claim_id=claim_id,
                        section_id=section.section_id,
                        line=line_no,
                        raw=raw,
                    )
                )

        # confidence
        confidence = Confidence.medium
        if claim_type == ClaimType.user_synthesis:
            confidence = Confidence.high
        for tag_name in _CONFIDENCE_TAGS:
            if tag_name in tags:
                confidence = _CONFIDENCE_TAGS[tag_name]
                break

        # importance — explicit float in [0, 1].
        importance = 0.5
        if tags.get("importance"):
            raw_importance = tags["importance"][0]
            try:
                importance = float(raw_importance)
            except ValueError:
                claim_warnings.append(
                    ScaffoldWarning(
                        level=ScaffoldWarningLevel.warning,
                        code="malformed_importance",
                        message=(
                            f"[importance: {raw_importance!r}] on claim {claim_id} "
                            "is not a float; using default 0.5."
                        ),
                        claim_id=claim_id,
                        section_id=section.section_id,
                        line=line_no,
                        raw=raw,
                    )
                )
            else:
                if not 0.0 <= importance <= 1.0:
                    claim_warnings.append(
                        ScaffoldWarning(
                            level=ScaffoldWarningLevel.warning,
                            code="importance_out_of_range",
                            message=(
                                f"[importance: {importance}] on claim {claim_id} "
                                "outside [0, 1]; clamping."
                            ),
                            claim_id=claim_id,
                            section_id=section.section_id,
                            line=line_no,
                            raw=raw,
                        )
                    )
                    importance = max(0.0, min(1.0, importance))

        # evidence_status — author's declared evidence state. None means
        # "let downstream derive from the evidence list".
        evidence_status: EvidenceStatus | None = None
        if tags.get("evidence_status"):
            requested = tags["evidence_status"][0].lower()
            if requested in _EVIDENCE_STATUS_MAP:
                evidence_status = _EVIDENCE_STATUS_MAP[requested]
            else:
                claim_warnings.append(
                    ScaffoldWarning(
                        level=ScaffoldWarningLevel.warning,
                        code="unknown_evidence_status",
                        message=(
                            f"Unknown [evidence_status: {requested}] on claim "
                            f"{claim_id}. Valid: {sorted(_EVIDENCE_STATUS_MAP.keys())}."
                        ),
                        claim_id=claim_id,
                        section_id=section.section_id,
                        line=line_no,
                        raw=raw,
                    )
                )

        # scope conditions — [scope: cond1, cond2] becomes ["cond1", "cond2"].
        scope_conditions: list[str] = list(tags.get("scope", []))

        # evidence from [ref: ...]
        evidence: list[Evidence] = []
        for source_id in _split_tag_list(tags.get("ref")):
            evidence.append(
                Evidence(
                    source=source_id,
                    passage="",  # enricher fills
                    binding_strength=BindingStrength.weak,
                )
            )

        # source excerpt hint — preserved as a tag the auto_outliner can emit
        # when it has located a quotable span. Tag values get comma-split, so
        # rejoin to recover the original text.
        source_excerpt: str | None = None
        if tags.get("source_excerpt"):
            source_excerpt = ", ".join(tags["source_excerpt"]).strip() or None

        # role tag → stored as a claim tag (consumed by assembler later)
        claim_tags: list[str] = []
        for role in tags.get("role", []):
            claim_tags.append(f"role:{role}")
        if "skip" in tags:
            claim_tags.append("skip")
        if "central_contribution" in tags:
            claim_tags.append("central_contribution")
        if "arithmetic" in tags:
            # Tells the renderer to preserve any step-by-step arithmetic
            # in the claim/evidence verbatim rather than abstracting it.
            # Reader auditability beats prose flow for these claims.
            claim_tags.append("arithmetic")
        if source_excerpt:
            # Stored as a tag rather than a model field so the auto_outliner
            # can preserve the original text without a schema change. The
            # scaffold report carries it visibly, and the renderer can read
            # it via tag-prefix lookup.
            claim_tags.append(f"source_excerpt:{source_excerpt}")

        # Mechanism is free-text — _parse_tags splits values on commas, so
        # rejoin the parts to recover the author's original text.
        mechanism: str | None = None
        if tags.get("mechanism"):
            mechanism = ", ".join(tags["mechanism"]).strip() or None

        claim = Claim(
            claim_id=claim_id,
            statement=statement,
            mechanism=mechanism,
            source_order=claim_source_order,
            type=claim_type,
            confidence=confidence,
            importance=importance,
            evidence_status=evidence_status,
            evidence=evidence,
            scope_conditions=scope_conditions,
            counterclaims=[],
            supporting_claims=[],
            author_origin=(claim_type == ClaimType.user_synthesis),
            section_id=section.section_id,
            created_by="markdown_ingester",
            created_at=now,
            modified_at=now,
            tags=claim_tags,
        )

        # Build relationships — dedup by (type, from, to) to avoid duplicates
        # when MY VIEW's implicit supports-thesis coincides with explicit [supports: thesis].
        rels: list[Relationship] = []
        seen_edges: set[tuple[RelationshipType, str, str]] = set()
        unresolved_targets: list[str] = []

        def _add_rel(
            rtype: RelationshipType,
            to_id: str,
            *,
            track_resolution: bool = False,
        ) -> None:
            key = (rtype, claim.claim_id, to_id)
            if key in seen_edges:
                return
            seen_edges.add(key)
            state["rel_seq"] += 1
            rels.append(
                Relationship(
                    rel_id=f"r.{state['rel_seq']:03d}",
                    type=rtype,
                    **{"from": claim.claim_id, "to": to_id},
                    strength=RelationshipStrength.direct,
                    note="",
                    created_by="markdown_ingester",
                    created_at=now,
                )
            )
            if track_resolution:
                unresolved_targets.append(to_id)

        thesis_id = state.get("thesis_claim_id")
        if is_my_view and thesis_id:
            _add_rel(RelationshipType.supports, thesis_id)
        if is_counter and thesis_id:
            _add_rel(RelationshipType.contradicts, thesis_id)

        # Process every supported relationship tag uniformly. ``supports``
        # and ``contradicts`` retain their thesis alias (``[supports: thesis]``);
        # the new tags (``qualifies``, ``extends``, ``depends_on``, ``pivot``)
        # take an explicit claim id.
        for tag_name, rtype in _RELATIONSHIP_TAGS.items():
            for target in _split_tag_list(tags.get(tag_name)):
                resolved = (
                    thesis_id
                    if target.lower() == "thesis" and thesis_id
                    else target
                )
                _add_rel(rtype, resolved, track_resolution=True)

        # Capture per-claim report for the scaffold diagnostics file.
        # ``cited_refs`` is the immutable record of every ``[ref:]`` the
        # author wrote; ``unresolved_refs`` starts as the same list and
        # gets pruned against the indexed source set on save (idempotent
        # — see ``MarkdownOutlineIngester.save_scaffold_report``).
        cited = [e.source for e in evidence if e.source]
        report.claim_reports.append(
            ScaffoldClaimReport(
                claim_id=claim.claim_id,
                section_id=section.section_id,
                original_excerpt=raw.strip(),
                extracted_statement=statement,
                confidence=1.0,  # deterministic parser; LLM scaffolds may set lower
                cited_refs=list(cited),
                unresolved_refs=list(cited),
                unresolved_targets=sorted(set(unresolved_targets)),
                warnings=claim_warnings,
                line=line_no,
            )
        )
        # Also surface the claim-level warnings at the top level so the
        # consumer doesn't have to scan claim_reports for them.
        report.warnings.extend(claim_warnings)

        return claim, rels


# ─── tag parsing helpers ─────────────────────────────

def _parse_tags(line: str) -> tuple[str, dict[str, list[str]]]:
    """Extract [key: val, val] and [bare_flag] tags. Returns (remainder, tags)."""
    tags: dict[str, list[str]] = {}
    remainder = line

    for match in _TAG_RE.finditer(line):
        body = match.group(1).strip()
        # Accept either `[key: val]` (canonical) or `[key=val]` (the form
        # an LLM tends to emit). Whichever separator appears first wins.
        sep_index = -1
        for sep in (":", "="):
            idx = body.find(sep)
            if idx >= 0 and (sep_index == -1 or idx < sep_index):
                sep_index = idx
        if sep_index >= 0:
            key = body[:sep_index]
            val = body[sep_index + 1:]
            tags.setdefault(key.strip(), []).extend(
                [v.strip() for v in val.split(",") if v.strip()]
            )
        else:
            tags.setdefault(body, [])

    # Strip tags from the remainder
    remainder = _TAG_RE.sub("", remainder).strip()
    return remainder, tags


def _split_tag_list(values: list[str] | None) -> list[str]:
    return values or []


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w]+", "_", text.lower()).strip("_")
    return slug or "unnamed"
