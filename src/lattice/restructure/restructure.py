"""Per-document and per-section structural analysis.

The output is a ``RestructureReport`` — a list of advisory suggestions
the author can choose to apply (or ignore). The graph is never
modified directly. Three passes:

1. **Document-level**: section ordering. Asks Claude whether the
   current section order is logical (motivation → mechanism → evidence
   → counterargument → conclusion is the typical academic shape; the
   actual sequence depends on the discipline).
2. **Per-section**: cluster ordering within each section. Setup
   clusters before payoff, definitions before claims that depend on
   them.
3. **Per-cluster**: claim ordering. Same dependency check at finer
   granularity.

Each pass is a separate LLM call so failures degrade gracefully — a
flaky call on one section doesn't kill the whole report.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..graph.models import AuthorGraph, Section
from ..graph.store import GraphStore
from ..utils.llm import ClaudeClient


SuggestionKind = Literal[
    "section_move",     # move a section to a different position
    "section_swap",     # swap two adjacent sections
    "cluster_move",     # move a cluster within a section
    "claim_move",       # move a claim within a cluster
    "merge",            # two units make the same point and should merge
    "split",            # one unit covers two distinct points
    "missing_setup",    # a definition / motivation is missing before its dependents
]


class RestructureSuggestion(BaseModel):
    """A single advisory suggestion from the structural analysis."""
    kind: SuggestionKind
    target_id: str          # the unit being moved / merged / split
    target_label: str       # human-readable description (section title, cluster role, etc.)
    before_id: str | None = None   # for moves: place the target before this id
    after_id: str | None = None    # for moves: place the target after this id
    paired_id: str | None = None   # for merges/swaps: the other unit involved
    rationale: str = ""
    rule: str = ""          # the academic-writing principle (e.g. "setup before payoff")
    confidence: Literal["high", "medium", "low"] = "medium"


class SectionReorder(BaseModel):
    """Document-level reordering proposal: the LLM's preferred section
    sequence with rationale.

    ``proposed_order`` is a list of section_ids in the order the LLM
    thinks they should appear. ``current_order`` is the existing order
    for diff display. ``commentary`` explains the high-level rationale.
    """
    current_order: list[str] = Field(default_factory=list)
    proposed_order: list[str] = Field(default_factory=list)
    commentary: str = ""


class ClusterReorder(BaseModel):
    section_id: str
    section_title: str
    current_order: list[str] = Field(default_factory=list)
    proposed_order: list[str] = Field(default_factory=list)
    commentary: str = ""


class ClaimReorder(BaseModel):
    cluster_id: str
    section_id: str
    current_order: list[str] = Field(default_factory=list)
    proposed_order: list[str] = Field(default_factory=list)
    commentary: str = ""


class RestructureReport(BaseModel):
    project_name: str
    voice_name: str
    generated_at: str
    section_reorder: SectionReorder | None = None
    cluster_reorders: list[ClusterReorder] = Field(default_factory=list)
    claim_reorders: list[ClaimReorder] = Field(default_factory=list)
    suggestions: list[RestructureSuggestion] = Field(default_factory=list)
    mode: Literal["fast", "thorough"] = "thorough"


# ─── prompts ─────────────────────────────────────


_SYSTEM_SECTION_ORDER = """You audit the section order of an academic paper using these academic-writing rules:

- Motivation / problem statement first
- Foundations / definitions before claims that depend on them
- Setup before payoff (don't reveal the conclusion before its support)
- Evidence near its claim, not deferred to a later section
- Counter-arguments AFTER the position they critique, before the conclusion
- Conclusion / synthesis last

For the section sequence given, decide if a different ordering would be clearer. Most papers are roughly fine — only suggest changes if you see a real dependency violation or a clearer arc.

Output strict JSON, no fenced code block, no prose outside JSON:
{
  "proposed_order": ["section_id_1", "section_id_2", ...],
  "commentary": "Explain why this order is better, citing specific dependencies. If no change is needed, return the current order and say so.",
  "suggestions": [
    {
      "kind": "section_move | section_swap | merge | missing_setup",
      "target_id": "section_id",
      "before_id": "section_id or null",
      "after_id": "section_id or null",
      "paired_id": "section_id or null",
      "rationale": "One short sentence.",
      "rule": "Which academic-writing rule justifies this.",
      "confidence": "high | medium | low"
    }
  ]
}"""


_SYSTEM_CLUSTER_ORDER = """You audit cluster ordering inside a single section using these rules:

- Setup clusters (motivation, definitions) before evidence
- Mechanism / how-it-works before complications and limits
- Counter-arguments AFTER the claim they oppose
- Synthesis / payoff at the end

Most sections are roughly right. Only suggest a reorder when there's a clear dependency violation.

Output strict JSON:
{
  "proposed_order": ["cluster_id_1", ...],
  "commentary": "Brief; if no change is needed, return current order and say so.",
  "suggestions": [
    {
      "kind": "cluster_move | merge | split | missing_setup",
      "target_id": "cluster_id",
      "before_id": "cluster_id or null",
      "after_id": "cluster_id or null",
      "paired_id": "cluster_id or null",
      "rationale": "One short sentence.",
      "rule": "Which rule.",
      "confidence": "high | medium | low"
    }
  ]
}"""


# ─── document-level pass ─────────────────────────


async def _analyse_section_order(
    graph: AuthorGraph, llm: ClaudeClient,
) -> tuple[SectionReorder, list[RestructureSuggestion]]:
    sections = sorted(graph.sections, key=lambda s: s.position)
    if len(sections) < 2:
        return SectionReorder(
            current_order=[s.section_id for s in sections],
            proposed_order=[s.section_id for s in sections],
            commentary="Only one section — nothing to reorder.",
        ), []

    section_lines: list[str] = []
    for s in sections:
        claim_count = sum(1 for c in graph.claims if c.section_id == s.section_id)
        role = s.role.value if hasattr(s.role, "value") else str(s.role)
        section_lines.append(
            f"  {s.section_id}: \"{s.title}\" (role: {role}, {claim_count} claims)"
        )
    user = (
        f"Paper thesis:\n{graph.thesis_statement or '(not stated)'}\n\n"
        f"Current section order:\n" + "\n".join(section_lines)
    )

    try:
        data, _ = await llm.complete_json(_SYSTEM_SECTION_ORDER, user)
    except Exception as exc:
        return SectionReorder(
            current_order=[s.section_id for s in sections],
            proposed_order=[s.section_id for s in sections],
            commentary=f"Document-level analysis failed: {exc}",
        ), []

    proposed = [str(x) for x in (data.get("proposed_order") or []) if x]
    valid_ids = {s.section_id for s in sections}
    proposed = [p for p in proposed if p in valid_ids]
    # Pad with any missing ids in their original position so the
    # author doesn't lose sections to a buggy LLM response.
    for s in sections:
        if s.section_id not in proposed:
            proposed.append(s.section_id)

    suggestions: list[RestructureSuggestion] = []
    for entry in (data.get("suggestions") or []):
        try:
            suggestions.append(_parse_suggestion(entry))
        except Exception:
            continue

    return SectionReorder(
        current_order=[s.section_id for s in sections],
        proposed_order=proposed,
        commentary=str(data.get("commentary", "")).strip(),
    ), suggestions


# ─── per-section pass ────────────────────────────


async def _analyse_cluster_order(
    graph: AuthorGraph,
    section: Section,
    store: GraphStore,
    llm: ClaudeClient,
) -> tuple[ClusterReorder, list[RestructureSuggestion]]:
    clusters = [c for c in store.list_clusters() if c.section_id == section.section_id]
    clusters.sort(key=lambda c: c.position)
    if len(clusters) < 2:
        return ClusterReorder(
            section_id=section.section_id,
            section_title=section.title,
            current_order=[c.cluster_id for c in clusters],
            proposed_order=[c.cluster_id for c in clusters],
            commentary="Section has fewer than two clusters — nothing to reorder.",
        ), []

    # Build a compact context for the LLM: cluster_id + role + the
    # statements of the claims in that cluster (truncated).
    cluster_lines: list[str] = []
    claim_by_id = {c.claim_id: c for c in graph.claims}
    for c in clusters:
        role = c.role.value if hasattr(c.role, "value") else str(c.role)
        claim_texts: list[str] = []
        for entry in c.claim_sequence:
            cl = claim_by_id.get(entry.claim_id)
            if cl is None:
                continue
            claim_texts.append(_short(cl.statement, 100))
        joined = " | ".join(claim_texts) if claim_texts else "(empty)"
        cluster_lines.append(
            f"  {c.cluster_id} (role: {role}): {joined}"
        )

    user = (
        f"Section: \"{section.title}\"\n"
        f"Section role: {section.role.value if hasattr(section.role, 'value') else section.role}\n\n"
        f"Current cluster sequence in this section:\n" + "\n".join(cluster_lines)
    )

    try:
        data, _ = await llm.complete_json(_SYSTEM_CLUSTER_ORDER, user)
    except Exception as exc:
        return ClusterReorder(
            section_id=section.section_id,
            section_title=section.title,
            current_order=[c.cluster_id for c in clusters],
            proposed_order=[c.cluster_id for c in clusters],
            commentary=f"Cluster analysis failed: {exc}",
        ), []

    proposed = [str(x) for x in (data.get("proposed_order") or []) if x]
    valid_ids = {c.cluster_id for c in clusters}
    proposed = [p for p in proposed if p in valid_ids]
    for c in clusters:
        if c.cluster_id not in proposed:
            proposed.append(c.cluster_id)

    suggestions: list[RestructureSuggestion] = []
    for entry in (data.get("suggestions") or []):
        try:
            suggestions.append(_parse_suggestion(entry))
        except Exception:
            continue

    return ClusterReorder(
        section_id=section.section_id,
        section_title=section.title,
        current_order=[c.cluster_id for c in clusters],
        proposed_order=proposed,
        commentary=str(data.get("commentary", "")).strip(),
    ), suggestions


# ─── public entry point ──────────────────────────


async def analyse_structure(
    project_path: Path,
    voice_name: str,
    graph: AuthorGraph,
    store: GraphStore,
    llm: ClaudeClient,
    *,
    mode: Literal["fast", "thorough"] = "thorough",
    progress: Any = None,
) -> RestructureReport:
    """Build a restructuring report.

    ``fast`` mode runs only the document-level section pass.
    ``thorough`` mode also runs a per-section cluster pass.
    """
    if progress:
        progress.begin("restructure_doc", status="analysing section order")
    section_reorder, doc_suggestions = await _analyse_section_order(graph, llm)
    if progress:
        progress.end(
            "restructure_doc",
            status=(
                f"{len(doc_suggestions)} section-level suggestion(s)"
                if doc_suggestions else "section order looks fine"
            ),
        )

    cluster_reorders: list[ClusterReorder] = []
    cluster_suggestions: list[RestructureSuggestion] = []

    if mode == "thorough":
        sections = sorted(graph.sections, key=lambda s: s.position)
        if progress:
            progress.begin(
                "restructure_clusters",
                total=len(sections),
                status="auditing cluster order per section",
            )

        async def run_one(s: Section):
            r, suggs = await _analyse_cluster_order(graph, s, store, llm)
            if progress:
                progress.advance("restructure_clusters")
            return r, suggs

        results = await asyncio.gather(*[run_one(s) for s in sections])
        for r, suggs in results:
            cluster_reorders.append(r)
            cluster_suggestions.extend(suggs)

        if progress:
            progress.end(
                "restructure_clusters",
                status=f"{len(cluster_suggestions)} cluster-level suggestion(s)",
            )

    report = RestructureReport(
        project_name=graph.project_name or project_path.name,
        voice_name=voice_name,
        generated_at=datetime.now(timezone.utc).isoformat(),
        section_reorder=section_reorder,
        cluster_reorders=cluster_reorders,
        suggestions=doc_suggestions + cluster_suggestions,
        mode=mode,
    )
    return report


def write_restructure_report(
    project_path: Path, report: RestructureReport,
) -> Path:
    target = project_path / "outputs" / f"restructure.{report.voice_name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return target


def read_restructure_report(
    project_path: Path, voice_name: str,
) -> RestructureReport | None:
    target = project_path / "outputs" / f"restructure.{voice_name}.json"
    if not target.exists():
        return None
    try:
        return RestructureReport.model_validate_json(
            target.read_text(encoding="utf-8")
        )
    except Exception:
        return None


# ─── helpers ─────────────────────────────────────


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


_VALID_KINDS = {
    "section_move", "section_swap", "cluster_move", "claim_move",
    "merge", "split", "missing_setup",
}


def _parse_suggestion(entry: dict[str, Any]) -> RestructureSuggestion:
    kind = entry.get("kind", "")
    if kind not in _VALID_KINDS:
        kind = "section_move"
    confidence = entry.get("confidence", "medium")
    if confidence not in ("high", "medium", "low"):
        confidence = "medium"
    return RestructureSuggestion(
        kind=kind,
        target_id=str(entry.get("target_id", "")).strip(),
        target_label=str(entry.get("target_label", entry.get("target_id", ""))).strip(),
        before_id=_str_or_none(entry.get("before_id")),
        after_id=_str_or_none(entry.get("after_id")),
        paired_id=_str_or_none(entry.get("paired_id")),
        rationale=str(entry.get("rationale", "")).strip(),
        rule=str(entry.get("rule", "")).strip(),
        confidence=confidence,
    )


def _str_or_none(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return str(v).strip() or None
