"""Supervisor review pipeline.

Three passes:

1. **Per-cluster revision** — for each cluster's prose, Claude returns
   a revised version + a one-line supervisor comment. Word-level diffs
   are computed via ``difflib.SequenceMatcher`` and rendered as
   ``<del>``/``<ins>`` markup in the track-changes paper.
2. **Per-section critique** — Claude critiques each section as a
   whole (does it argue what it claims to argue? are the moves clean?).
3. **Document-level critique** — overall thesis quality, argument
   structure, evidence use, what a supervisor would want fixed before
   submission.

The output is two markdown files: a critique (read in the UI) and a
track-changes paper (also rendered in the UI).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..graph.models import AuthorGraph, Cluster
from ..graph.store import GraphStore
from ..utils.llm import ClaudeClient


class ClusterRevision(BaseModel):
    cluster_id: str
    section_id: str
    section_title: str
    original_prose: str
    revised_prose: str
    comment: str = ""
    severity: Literal["nit", "suggestion", "concern"] = "suggestion"


class SectionCritique(BaseModel):
    section_id: str
    section_title: str
    critique: str = ""


class ReviewReport(BaseModel):
    project_name: str
    voice_name: str
    generated_at: str
    overall_critique: str = ""
    section_critiques: list[SectionCritique] = Field(default_factory=list)
    cluster_revisions: list[ClusterRevision] = Field(default_factory=list)
    mode: Literal["fast", "thorough"] = "thorough"


# ─── prompts ─────────────────────────────────────


_SYSTEM_CLUSTER_REVISION = """You are an academic supervisor reviewing one paragraph of a student's paper. Your job is to suggest revisions a supervisor would write in track changes — specific, concrete, ready to accept.

Focus on:
- Clarity: tighten sentences that meander
- Precision: vague claims that need specifics
- Logic: missing connectors, claims that don't follow
- Voice: shifts in formality, weak hedges, unsupported confidence
- Citation engagement: passages that name a source without engaging its argument

Do NOT:
- Rewrite the whole paragraph from scratch unless it's truly broken
- Change the substantive claims (only how they're expressed)
- Add new sources

If the paragraph is already good, return it unchanged and say so in the comment.

Output strict JSON, no fenced code block, no prose outside JSON:
{
  "revised_prose": "The revised paragraph (full text, ready to drop in).",
  "comment": "One short sentence in a supervisor's voice — 'tighten this opening' or 'unsupported claim — cite or remove'.",
  "severity": "nit | suggestion | concern"
}"""


_SYSTEM_SECTION_CRITIQUE = """You are an academic supervisor giving feedback on one section of a student's paper.

In two to four sentences, identify the section's main strengths and the most important weakness — the one a supervisor would flag first. Be specific, no vague platitudes.

Reply with strict JSON:
{
  "critique": "Two to four sentences, supervisor voice."
}"""


_SYSTEM_OVERALL = """You are an academic supervisor giving an end-of-draft review of a student's paper. Read the thesis and the section critiques you've already written, then produce an overall assessment.

In one paragraph (5-8 sentences), cover:
- Thesis clarity and ambition
- Argument structure: does the paper actually argue what its thesis claims?
- Evidence use: where the paper is strongest and where it's thin
- The single most important revision priority

Honest, direct, specific. The student should walk away knowing exactly what to fix first.

Reply with strict JSON:
{
  "overall_critique": "One paragraph in supervisor voice."
}"""


# ─── per-cluster pass ────────────────────────────


def _read_cluster_prose(project_path: Path, voice_name: str, cluster: Cluster) -> str:
    """Locate and read a cluster's prose file. Returns empty string
    if the cluster hasn't been rendered yet."""
    if cluster.prose_file:
        prose_path = project_path / cluster.prose_file
        if prose_path.exists():
            try:
                return prose_path.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
    # Fallback to the conventional path.
    fallback = project_path / ".lattice" / "drafts" / voice_name / f"{cluster.cluster_id}.md"
    if fallback.exists():
        try:
            return fallback.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


async def _revise_cluster(
    cluster: Cluster,
    section_title: str,
    original: str,
    llm: ClaudeClient,
) -> ClusterRevision | None:
    if not original or "CLUSTER_UNRENDERABLE" in original:
        return None
    user = (
        f"Section: \"{section_title}\"\n"
        f"Cluster role: {cluster.role.value if hasattr(cluster.role, 'value') else cluster.role}\n\n"
        f"Original paragraph:\n{original}"
    )
    try:
        data, _ = await llm.complete_json(_SYSTEM_CLUSTER_REVISION, user)
    except Exception:
        return None
    revised = str(data.get("revised_prose", "")).strip()
    if not revised:
        revised = original
    severity = data.get("severity", "suggestion")
    if severity not in ("nit", "suggestion", "concern"):
        severity = "suggestion"
    return ClusterRevision(
        cluster_id=cluster.cluster_id,
        section_id=cluster.section_id,
        section_title=section_title,
        original_prose=original,
        revised_prose=revised,
        comment=str(data.get("comment", "")).strip(),
        severity=severity,
    )


# ─── per-section + overall passes ────────────────


async def _critique_section(
    section_id: str,
    section_title: str,
    revisions: list[ClusterRevision],
    llm: ClaudeClient,
) -> SectionCritique:
    if not revisions:
        return SectionCritique(
            section_id=section_id,
            section_title=section_title,
            critique="(section has no rendered clusters to critique)",
        )
    body = "\n\n".join(
        f"[{r.cluster_id}] {r.original_prose}" for r in revisions
    )
    user = f"Section: \"{section_title}\"\n\nRendered prose:\n{body}"
    try:
        data, _ = await llm.complete_json(_SYSTEM_SECTION_CRITIQUE, user)
    except Exception as exc:
        return SectionCritique(
            section_id=section_id,
            section_title=section_title,
            critique=f"(section critique failed: {exc})",
        )
    return SectionCritique(
        section_id=section_id,
        section_title=section_title,
        critique=str(data.get("critique", "")).strip(),
    )


async def _critique_overall(
    graph: AuthorGraph,
    section_critiques: list[SectionCritique],
    llm: ClaudeClient,
) -> str:
    body_lines = [f"Thesis: {graph.thesis_statement or '(not stated)'}", ""]
    for sc in section_critiques:
        body_lines.append(f"## {sc.section_title}\n{sc.critique}\n")
    user = "\n".join(body_lines)
    try:
        data, _ = await llm.complete_json(_SYSTEM_OVERALL, user)
    except Exception as exc:
        return f"(overall critique failed: {exc})"
    return str(data.get("overall_critique", "")).strip()


# ─── word-level diff rendering ───────────────────


def _word_diff_html(original: str, revised: str) -> str:
    """Return ``original`` with word-level ``<del>``/``<ins>`` markup
    showing how it changes into ``revised``. Markdown renders these
    HTML tags natively, so the output drops cleanly into a markdown
    document.
    """
    a = original.split()
    b = revised.split()
    matcher = SequenceMatcher(a=a, b=b, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.append(" ".join(a[i1:i2]))
        elif tag == "delete":
            out.append(f"<del>{_escape(' '.join(a[i1:i2]))}</del>")
        elif tag == "insert":
            out.append(f"<ins>{_escape(' '.join(b[j1:j2]))}</ins>")
        elif tag == "replace":
            out.append(f"<del>{_escape(' '.join(a[i1:i2]))}</del>")
            out.append(f"<ins>{_escape(' '.join(b[j1:j2]))}</ins>")
    return " ".join(out)


def _escape(s: str) -> str:
    """Escape HTML special chars so ``<`` in the prose doesn't break
    markup. (`<del>` and `<ins>` are inserted by us *after* this is
    applied to the text they wrap, so they're never escaped.)"""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


# ─── public entry point ──────────────────────────


async def produce_review(
    project_path: Path,
    voice_name: str,
    graph: AuthorGraph,
    store: GraphStore,
    llm: ClaudeClient,
    *,
    mode: Literal["fast", "thorough"] = "thorough",
    progress: Any = None,
) -> ReviewReport:
    """Build a supervisor review.

    ``fast`` mode: cluster revisions only, no per-section or overall
    critique. ``thorough`` mode: full three-pass review.
    """
    sections = sorted(graph.sections, key=lambda s: s.position)
    section_title_by_id = {s.section_id: s.title for s in sections}
    clusters = sorted(store.list_clusters(), key=lambda c: (c.section_id, c.position))

    # Phase 1: per-cluster revisions (parallel).
    if progress:
        progress.begin(
            "review_clusters",
            total=len(clusters),
            status=f"reviewing {len(clusters)} cluster(s)",
        )

    async def revise_one(cl: Cluster) -> ClusterRevision | None:
        original = _read_cluster_prose(project_path, voice_name, cl)
        rev = await _revise_cluster(
            cl,
            section_title_by_id.get(cl.section_id, cl.section_id),
            original,
            llm,
        )
        if progress:
            progress.advance("review_clusters")
        return rev

    raw = await asyncio.gather(*[revise_one(c) for c in clusters])
    revisions = [r for r in raw if r is not None]

    if progress:
        progress.end(
            "review_clusters",
            status=f"{len(revisions)} cluster(s) reviewed, {len(raw) - len(revisions)} skipped (not rendered)",
        )

    section_critiques: list[SectionCritique] = []
    overall_critique = ""

    if mode == "thorough":
        # Phase 2: per-section critique.
        by_section: dict[str, list[ClusterRevision]] = {}
        for r in revisions:
            by_section.setdefault(r.section_id, []).append(r)
        if progress:
            progress.begin(
                "review_sections",
                total=len(sections),
                status="critiquing each section",
            )

        async def crit_one(s) -> SectionCritique:
            sc = await _critique_section(
                s.section_id, s.title, by_section.get(s.section_id, []), llm,
            )
            if progress:
                progress.advance("review_sections")
            return sc

        section_critiques = list(await asyncio.gather(*[crit_one(s) for s in sections]))
        if progress:
            progress.end("review_sections", status="section critiques done")

        # Phase 3: overall critique.
        if progress:
            progress.begin("review_overall", status="composing overall critique")
        overall_critique = await _critique_overall(graph, section_critiques, llm)
        if progress:
            progress.end("review_overall", status="overall critique done")

    return ReviewReport(
        project_name=graph.project_name or project_path.name,
        voice_name=voice_name,
        generated_at=datetime.now(timezone.utc).isoformat(),
        overall_critique=overall_critique,
        section_critiques=section_critiques,
        cluster_revisions=revisions,
        mode=mode,
    )


def write_review_artefacts(
    project_path: Path, report: ReviewReport,
) -> dict[str, Path]:
    """Write the critique markdown + the track-changes paper.

    Returns a dict ``{"critique": Path, "track_changes": Path,
    "json": Path}``. The JSON copy is the source of truth read by the
    UI; the markdown copies are for human reading and DOCX export.
    """
    outputs = project_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)

    json_path = outputs / f"review.{report.voice_name}.json"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    # Critique markdown.
    crit_lines: list[str] = [
        f"# Supervisor review · {report.project_name}",
        "",
        f"_Generated {report.generated_at}_  ·  voice: `{report.voice_name}`",
        "",
        "## Overall",
        "",
        report.overall_critique or "_(not generated — fast mode)_",
        "",
        "## By section",
        "",
    ]
    for sc in report.section_critiques:
        crit_lines.append(f"### {sc.section_title}")
        crit_lines.append("")
        crit_lines.append(sc.critique or "_(no critique)_")
        crit_lines.append("")

    crit_lines.append("## Per-cluster revisions")
    crit_lines.append("")
    for r in report.cluster_revisions:
        if r.comment:
            crit_lines.append(
                f"- **{r.cluster_id}** · {r.section_title} · _{r.severity}_ — {r.comment}"
            )
    if not report.cluster_revisions:
        crit_lines.append("_(no clusters were rendered for review)_")
    crit_md = "\n".join(crit_lines) + "\n"
    crit_path = outputs / f"review.{report.voice_name}.md"
    crit_path.write_text(crit_md, encoding="utf-8")

    # Track-changes paper.
    tc_lines: list[str] = [
        f"# {report.project_name} — track changes",
        "",
        f"_Supervisor: {report.voice_name} voice · "
        f"{len(report.cluster_revisions)} cluster(s) revised._",
        "",
    ]
    by_section: dict[str, list[ClusterRevision]] = {}
    for r in report.cluster_revisions:
        by_section.setdefault(r.section_id, []).append(r)

    for sc in report.section_critiques:
        tc_lines.append(f"## {sc.section_title}")
        tc_lines.append("")
        if sc.critique:
            tc_lines.append(f"> _Supervisor:_ {sc.critique}")
            tc_lines.append("")
        for r in by_section.get(sc.section_id, []):
            diff = _word_diff_html(r.original_prose, r.revised_prose)
            tc_lines.append(diff)
            tc_lines.append("")
            if r.comment:
                tc_lines.append(f"> _{r.severity}:_ {r.comment}")
                tc_lines.append("")
    tc_path = outputs / f"review_track_changes.{report.voice_name}.md"
    tc_path.write_text("\n".join(tc_lines) + "\n", encoding="utf-8")

    return {"critique": crit_path, "track_changes": tc_path, "json": json_path}


def read_review_report(
    project_path: Path, voice_name: str,
) -> ReviewReport | None:
    target = project_path / "outputs" / f"review.{voice_name}.json"
    if not target.exists():
        return None
    try:
        return ReviewReport.model_validate_json(
            target.read_text(encoding="utf-8")
        )
    except Exception:
        return None
