"""Activity-oriented pipeline entry points.

Replaces the monolithic ``run_pipeline`` in ``runner.py`` with four
focused activity functions:

- ``scaffold``   — auto-heal outline, ingest, plan, infer relationships
- ``draft``      — render prose, finalise, auto-recover failed clusters
- ``find_gaps``  — source-gap review against a reference document
- ``refine``     — audit + (thorough) autofix loop + voice review

Each activity takes a focused argument list and runs only the stages
that activity implies. State is derived from filesystem markers via
``project_state``; the dispatcher refuses to run activities whose
preconditions aren't met.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..auditor.autofix import run_autofix_async
from ..auditor.runner import AuditRunner
from ..auditor.source_gap_review import (
    SourceGapReview,
    write_report as write_gap_report,
)
from ..auditor.voice_review import review_document as voice_review_document
from ..graph.models import ProseState
from ..graph.store import GraphStore
from ..renderer.assembler import Assembler
from ..renderer.assembler_finalise import DocumentFinaliser
from ..renderer.chunked_renderer import ChunkedRenderer
from ..utils.config import Config
from ..utils.llm import ClaudeClient, claude_available
from ..voice.parser import Voice
from .runner import (
    EventQueueProgress,
    RunResult,
    capture_project_state,
)


ActivityVerb = Literal[
    "ingest", "scaffold", "draft", "find_gaps", "refine", "restructure", "review",
]
Mode = Literal["fast", "thorough"]


@dataclass
class ActivityRequest:
    """Inputs for a single activity run.

    Most fields are activity-specific; the dispatcher tolerates
    irrelevant fields being set (they're simply ignored)."""

    project_path: Path
    voice_name: str
    verb: ActivityVerb
    mode: Mode = "thorough"
    reference_path: Path | None = None  # find_gaps only
    max_passes: int = 3                  # refine only
    chunk_min: int = 3                   # draft only
    chunk_max: int = 4                   # draft only
    force: bool = False                  # draft only
    nesting_depth: int = 2               # scaffold only — 1=flat, 2=##, 3=###


# ─── state derivation ────────────────────────────


def project_state(project_path: Path) -> dict[str, Any]:
    """Compute current project state and per-activity blockers.

    Returns ``{"state": "S0".."S4", "blockers": {verb: msg|None}, "markers": {...}}``.
    The frontend uses this single payload to decide which activity
    cards to render as locked vs. ready.
    """
    structure_dir = project_path / "structure"
    outline_md = structure_dir / "outline.md"
    outline_docx = structure_dir / "outline.docx"
    graph_path = project_path / ".lattice" / "author_graph.json"
    cluster_path = project_path / ".lattice" / "cluster_plan.json"
    audit_dir = project_path / ".lattice" / "audit"

    outputs_dir = project_path / "outputs"
    paper_files = (
        list(outputs_dir.glob("paper.*.md")) if outputs_dir.exists() else []
    )

    has_outline = outline_md.exists() or outline_docx.exists()
    has_graph = graph_path.exists()
    has_clusters = cluster_path.exists()
    has_paper = bool(paper_files)
    has_audit_flags = audit_dir.exists() and any(audit_dir.glob("*.json"))

    if has_audit_flags:
        state = "S4"
    elif has_paper:
        state = "S3"
    elif has_graph and has_clusters:
        state = "S2"
    elif has_outline:
        state = "S1"
    else:
        state = "S0"

    blockers = {
        "ingest": None if has_outline else "Add an outline first.",
        "scaffold": None if has_outline else "Add an outline first.",
        "draft": (
            None if has_clusters
            else "Run Scaffold first to build a cluster plan."
        ),
        "find_gaps": (
            None if has_clusters
            else "Run Scaffold first."
        ),
        "refine": (
            None if has_paper
            else "Run Draft first to produce a paper."
        ),
        "restructure": (
            None if has_clusters
            else "Run Scaffold first to build a cluster plan."
        ),
        "review": (
            None if has_paper
            else "Run Draft first to produce a paper."
        ),
    }

    return {
        "state": state,
        "blockers": blockers,
        "markers": {
            "has_outline": has_outline,
            "has_graph": has_graph,
            "has_clusters": has_clusters,
            "has_paper": has_paper,
            "has_audit_flags": has_audit_flags,
            "paper_files": [p.name for p in paper_files],
        },
    }


# ─── dispatcher ──────────────────────────────────


async def run_activity(
    request: ActivityRequest,
    progress: EventQueueProgress,
) -> RunResult:
    """Dispatch to the right activity function based on verb.

    Wraps every activity in:
      - precondition check (refuses if blocked)
      - LLM availability check
      - try/except so exceptions surface as ``run_failed`` events
      - history + changelog write at the end
    """
    started = time.monotonic()
    result = RunResult()
    pre = capture_project_state(request.project_path)

    blockers = project_state(request.project_path)["blockers"]
    blocker = blockers.get(request.verb)
    if blocker is not None:
        result.notes.append(f"Cannot run {request.verb}: {blocker}")
        progress._emit({
            "type": "run_failed",
            "reason": "preconditions_not_met",
            "verb": request.verb,
            "detail": blocker,
        })
        return result

    # Ingest is purely deterministic — no LLM involved — so it should
    # work even without Claude on PATH (and without a configured voice).
    needs_llm = request.verb != "ingest"
    if needs_llm and not claude_available():
        result.notes.append("Claude CLI not found on PATH.")
        progress._emit({"type": "run_failed", "reason": "claude_not_available"})
        return result

    config = Config.load(request.project_path)
    voice_path = (
        request.project_path / "voices" / f"{request.voice_name}.voice.md"
    )
    if not voice_path.exists():
        result.notes.append(f"Voice file not found: {voice_path}")
        progress._emit({
            "type": "run_failed",
            "reason": "voice_not_found",
            "detail": str(voice_path),
        })
        return result
    voice = Voice.from_file(voice_path)
    llm: ClaudeClient | None = None
    if needs_llm:
        llm = ClaudeClient(
            default_model=config.default_model,
            parallel=config.parallel_renders,
        )

    handlers = {
        "ingest": _activity_ingest,
        "scaffold": _activity_scaffold,
        "draft": _activity_draft,
        "find_gaps": _activity_find_gaps,
        "refine": _activity_refine,
        "restructure": _activity_restructure,
        "review": _activity_review,
    }
    handler = handlers.get(request.verb)
    if handler is None:
        result.notes.append(f"Unknown verb: {request.verb}")
        progress._emit({"type": "run_failed", "reason": "unknown_verb"})
        return result

    try:
        await handler(request, config, voice, llm, progress, result)
    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = "".join(traceback.format_exception(exc))
        result.notes.append(
            f"{request.verb} raised {type(exc).__name__}: {exc}"
        )
        progress._emit({
            "type": "run_failed",
            "reason": "activity_exception",
            "verb": request.verb,
            "detail": f"{type(exc).__name__}: {exc}",
            "traceback": tb,
        })
        return result

    result.elapsed_seconds = round(time.monotonic() - started, 2)

    try:
        post = capture_project_state(request.project_path)
        record_activity_history(request, result, voice.name)
        write_activity_changelog(
            request, result, voice.name, pre, post
        )
    except Exception as exc:  # noqa: BLE001
        result.notes.append(
            f"Bookkeeping failed: {type(exc).__name__}: {exc}"
        )

    progress._emit({
        "type": "run_finished",
        "verb": request.verb,
        "mode": request.mode,
        "elapsed_seconds": result.elapsed_seconds,
        "final_path": (
            str(result.final_path) if result.final_path else None
        ),
        "audit_flags": result.audit_flags,
        "rendered_clusters": result.rendered_clusters,
        "total_clusters": result.total_clusters,
        "voice_review_path": (
            str(result.voice_review_path)
            if result.voice_review_path else None
        ),
        "source_gap_path": (
            str(result.source_gap_path)
            if result.source_gap_path else None
        ),
        "finalise_succeeded": result.finalise_succeeded,
        "notes": result.notes,
    })
    return result


# ─── activity: ingest ────────────────────────────


async def _activity_ingest(
    request: ActivityRequest,
    config: Config,
    voice: Voice,
    llm: ClaudeClient,
    progress: EventQueueProgress,
    result: RunResult,
) -> None:
    """Re-parse the outline into the graph + cluster plan, deterministically.

    Same first two steps as Scaffold, minus all LLM passes
    (no auto-heal, no relationship inference, no reference
    extraction). Use this after editing the outline by hand to refresh
    the diagram quickly without paying for Claude calls.

    Both Fast and Thorough behave identically — there's no LLM stage
    in this activity, so the toggle is a no-op (kept for UI uniformity).
    """
    project = request.project_path
    structure_dir = project / "structure"
    cluster_path = project / ".lattice" / "cluster_plan.json"

    outline_path = (
        structure_dir / "outline.md"
        if (structure_dir / "outline.md").exists()
        else structure_dir / "outline.docx"
        if (structure_dir / "outline.docx").exists()
        else None
    )
    if outline_path is None:
        result.notes.append("No outline file found.")
        progress._emit({"type": "run_failed", "reason": "no_outline"})
        return

    progress.begin("ingest", status=f"parsing {outline_path.name}")
    if outline_path.suffix.lower() == ".docx":
        from ..ingester.docx import DOCXOutlineIngester
        ingester: Any = DOCXOutlineIngester(config)
    else:
        from ..ingester.markdown import MarkdownOutlineIngester
        ingester = MarkdownOutlineIngester(config)
    try:
        graph = await ingester.ingest(outline_path, project_name=project.name)
    except Exception as exc:  # noqa: BLE001
        progress.end("ingest", status=f"failed: {type(exc).__name__}")
        result.notes.append(f"Ingest failed: {exc}")
        progress._emit({
            "type": "run_failed",
            "reason": "ingest_failed",
            "detail": f"{type(exc).__name__}: {exc}",
        })
        return

    if not graph.sections or not graph.claims:
        progress.end("ingest", status="parsed but found no sections / claims")
        result.notes.append(
            "Outline parsed but contained no sections or claims. "
            "Lattice expects `# THESIS` and `# A. Title` (or `## A.1 Title`) "
            "headers with `  - claim` bullets. If the outline is raw prose, "
            "run Scaffold instead — it calls Claude to extract structure."
        )
        progress._emit({
            "type": "run_failed",
            "reason": "outline_has_no_structure",
        })
        return

    store = GraphStore.load(project)
    store.save_graph(graph)
    if hasattr(ingester, "save_scaffold_report"):
        known = {s.source_id for s in store.list_sources()}
        ingester.save_scaffold_report(project, known_source_ids=known)
    progress.end(
        "ingest",
        status=f"{len(graph.sections)} sections, {len(graph.claims)} claims",
    )

    # Drop the old cluster plan so the planner re-runs from scratch.
    if cluster_path.exists():
        cluster_path.unlink()

    progress.begin("plan", status="building cluster plan")
    clusters = await Assembler(
        config, store, llm=None, voice=voice
    ).build_plan()
    progress.end(
        "plan",
        status=(
            f"{len(clusters)} clusters across "
            f"{len({c.section_id for c in clusters})} sections"
        ),
    )
    result.total_clusters = len(clusters)
    result.finalise_succeeded = True


# ─── activity: scaffold ──────────────────────────


async def _activity_scaffold(
    request: ActivityRequest,
    config: Config,
    voice: Voice,
    llm: ClaudeClient,
    progress: EventQueueProgress,
    result: RunResult,
) -> None:
    """Outline → graph → cluster plan. Optionally infers relationships
    and extracts references in thorough mode.

    Idempotent: re-running rebuilds the graph + plan from the current
    outline. Safe to call after every outline edit.
    """
    project = request.project_path
    structure_dir = project / "structure"
    graph_path = project / ".lattice" / "author_graph.json"
    cluster_path = project / ".lattice" / "cluster_plan.json"

    # ── 1. Auto-heal outline (raw prose → lattice format) ──
    outline_path = (
        structure_dir / "outline.md"
        if (structure_dir / "outline.md").exists()
        else structure_dir / "outline.docx"
        if (structure_dir / "outline.docx").exists()
        else None
    )
    if outline_path is None:
        result.notes.append("No outline file found.")
        progress._emit({"type": "run_failed", "reason": "no_outline"})
        return

    auto_outliner_summary: Any = None
    if outline_path.suffix.lower() in (".md", ".markdown", ".txt"):
        from ..ingester.auto_outliner import (
            append_conclusion_section,
            has_conclusion_section,
            looks_like_lattice_outline,
            normalise_to_user_synthesis,
            structure_outline_with_report,
            write_structured_outline,
        )
        try:
            raw_text = outline_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw_text = outline_path.read_text(
                encoding="utf-8", errors="replace"
            )

        if raw_text.strip() and not looks_like_lattice_outline(raw_text):
            progress.begin(
                "structure_outline",
                status="raw prose detected — extracting structure",
            )
            structured, auto_outliner_summary = await structure_outline_with_report(
                raw_text, llm, max_depth=request.nesting_depth,
            )
            outline_path = write_structured_outline(
                project, structured, raw_text
            )
            raw_text = outline_path.read_text(encoding="utf-8")
            progress.end(
                "structure_outline",
                status=(
                    f"{structured.count(chr(10) + '#')} sections "
                    f"extracted; original archived"
                ),
            )

        # No source papers? Tag claims as user_synthesis so they render.
        refs_papers = project / "refs" / "papers"
        has_source_papers = refs_papers.exists() and any(
            p.is_file() and p.suffix.lower() in (
                ".pdf", ".docx", ".md", ".markdown", ".txt"
            )
            for p in refs_papers.iterdir()
        )
        if not has_source_papers:
            normalised, changed = normalise_to_user_synthesis(raw_text)
            if changed > 0:
                progress.begin(
                    "normalise_outline",
                    status=(
                        f"no sources library — tagging "
                        f"{changed} claim(s) as [user_synthesis]"
                    ),
                )
                outline_path.write_text(normalised, encoding="utf-8")
                raw_text = normalised
                progress.end(
                    "normalise_outline",
                    status=f"{changed} claim(s) marked author-grounded",
                )

        if not has_conclusion_section(raw_text):
            progress.begin(
                "add_conclusion",
                status="appending default conclusion",
            )
            outline_path.write_text(
                append_conclusion_section(raw_text), encoding="utf-8"
            )
            progress.end(
                "add_conclusion",
                status="conclusion section added",
            )

    # ── 2. Ingest (always rebuilds graph) ──
    progress.begin("ingest", status=f"parsing {outline_path.name}")
    if outline_path.suffix.lower() == ".docx":
        from ..ingester.docx import DOCXOutlineIngester
        ingester: Any = DOCXOutlineIngester(config)
    else:
        from ..ingester.markdown import MarkdownOutlineIngester
        ingester = MarkdownOutlineIngester(config)
    graph = await ingester.ingest(
        outline_path, project_name=project.name
    )
    store = GraphStore.load(project)
    store.save_graph(graph)
    if hasattr(ingester, "save_scaffold_report"):
        known = {s.source_id for s in store.list_sources()}
        ingester.save_scaffold_report(
            project,
            known_source_ids=known,
            auto_outliner_summary=auto_outliner_summary,
        )
    progress.end(
        "ingest",
        status=f"{len(graph.sections)} sections, {len(graph.claims)} claims",
    )

    if not graph.sections or not graph.claims:
        result.notes.append(
            "Outline parsed but contained no sections or claims. "
            "Lattice expects `# THESIS` / `# A. Section` headers and "
            "`  - claim` bullets."
        )
        progress._emit({
            "type": "run_failed",
            "reason": "outline_has_no_structure",
        })
        return

    # Drop the stale plan so the planner runs.
    if cluster_path.exists():
        cluster_path.unlink()

    # ── 3. Plan ──
    progress.begin("plan", status="building cluster plan")
    clusters = await Assembler(
        config, store, llm=None, voice=voice
    ).build_plan()
    progress.end(
        "plan",
        status=(
            f"{len(clusters)} clusters across "
            f"{len({c.section_id for c in clusters})} sections"
        ),
    )
    result.total_clusters = len(clusters)
    # By the time we get here ingest + plan have both succeeded — set
    # finalise_succeeded so the activity history shows the scaffold as
    # OK rather than blocked. Subsequent thorough-mode passes (rel
    # inference, reference extraction) are best-effort enrichment; their
    # failure shouldn't downgrade the scaffold's overall success.
    result.finalise_succeeded = True

    # ── 4. (thorough) infer claim relationships ──
    if request.mode == "thorough":
        from ..enricher.relationship_inference import (
            infer_relationships,
            merge_inferred_relationships,
        )
        store = GraphStore.load(project)
        graph = store.get_graph()
        progress.begin(
            "relationship_inference",
            status=f"analysing {len(graph.claims)} claim(s)",
        )
        try:
            inferred = await infer_relationships(graph, llm)
            added = 0
            if inferred:
                added, _ = merge_inferred_relationships(graph, inferred)
                store.save_graph(graph)
            progress.end(
                "relationship_inference",
                status=f"+{added} relationship(s)",
            )
            # Re-run the planner so the new edges flow into each cluster's
            # ``relationship_context`` and clusters whose intra-cluster
            # rendering signature changed get marked dirty (forcing
            # re-render on the next draft).
            if added:
                dirty_before = sum(
                    1 for c in store.list_clusters()
                    if c.prose_state.value == "dirty"
                )
                await Assembler(config, store, llm=None, voice=voice).build_plan()
                dirty_after = sum(
                    1 for c in store.list_clusters()
                    if c.prose_state.value == "dirty"
                )
                marked = max(0, dirty_after - dirty_before)
                if marked:
                    result.notes.append(
                        f"Inferred relationships marked {marked} cluster(s) "
                        "dirty — re-render on next draft."
                    )
        except Exception as exc:  # noqa: BLE001
            progress.end(
                "relationship_inference",
                status=f"failed: {type(exc).__name__}",
            )
            result.notes.append(
                f"Relationship inference failed: {exc}"
            )

    # ── 5. (thorough) extract references from outline.raw.md ──
    if request.mode == "thorough":
        outline_raw = project / "structure" / "outline.raw.md"
        meta_path = project / ".lattice" / "project_meta.json"
        meta_data: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta_data = json.loads(
                    meta_path.read_text(encoding="utf-8")
                )
            except json.JSONDecodeError:
                pass
        already = bool(meta_data.get("references_extracted"))
        if outline_raw.exists() and not already:
            progress.begin(
                "extract_references",
                status="scanning outline.raw.md for citations",
            )
            try:
                from ..enricher.reference_extraction import (
                    citation_to_synthetic_source,
                    extract_citations_from_text,
                )
                raw = outline_raw.read_text(
                    encoding="utf-8", errors="replace"
                )
                citations = await extract_citations_from_text(raw, llm)
                store_for_refs = GraphStore.load(project)
                existing_ids = {
                    s.source_id for s in store_for_refs.list_sources()
                }
                added = 0
                for citation in citations:
                    source = citation_to_synthetic_source(citation)
                    base = source.source_id
                    counter = 2
                    while source.source_id in existing_ids:
                        source.source_id = f"{base}_{counter}"
                        counter += 1
                    store_for_refs.save_source(source)
                    existing_ids.add(source.source_id)
                    added += 1
                meta_data["references_extracted"] = True
                meta_data["references_extracted_count"] = added
                meta_path.parent.mkdir(parents=True, exist_ok=True)
                meta_path.write_text(
                    json.dumps(meta_data, indent=2), encoding="utf-8"
                )
                progress.end(
                    "extract_references", status=f"+{added} reference(s)"
                )
            except Exception as exc:  # noqa: BLE001
                progress.end(
                    "extract_references",
                    status=f"failed: {type(exc).__name__}",
                )
                result.notes.append(f"Reference extraction failed: {exc}")


# ─── activity: draft ─────────────────────────────


async def _activity_draft(
    request: ActivityRequest,
    config: Config,
    voice: Voice,
    llm: ClaudeClient,
    progress: EventQueueProgress,
    result: RunResult,
) -> None:
    """Render prose + finalise. Auto-recovers failed clusters once.

    In ``fast`` mode skips voice-rule autocorrect. In ``thorough`` mode
    runs autocorrect=safe so mechanical nits get fixed inline.
    """
    project = request.project_path
    store = GraphStore.load(project)
    clusters = store.list_clusters()
    result.total_clusters = len(clusters)
    if not clusters:
        result.notes.append("Cluster plan is empty.")
        progress._emit({"type": "run_failed", "reason": "empty_cluster_plan"})
        return

    config.autocorrect = "safe" if request.mode == "thorough" else "none"

    # ── 1. Render ──
    progress.begin_pass(1, 1)
    renderer = ChunkedRenderer(
        config, store, llm, voice,
        min_chunk=request.chunk_min, max_chunk=request.chunk_max,
    )
    rendered = await renderer.render_all(
        force=request.force, progress=progress
    )
    result.rendered_clusters = sum(
        1 for r in rendered.values()
        if r and "CLUSTER_UNRENDERABLE" not in r
    )

    # ── 2. Finalise ──
    progress.begin("finalise", status="checking readiness")
    final_path = DocumentFinaliser(project, store, voice).finalise()
    if final_path is not None:
        progress.end("finalise", status="document delivered")
        result.final_path = final_path
        result.finalise_succeeded = True
        return
    progress.end("finalise", status="not ready — attempting recovery")

    # ── 3. Auto-recovery: shrink chunks, retry failed clusters ──
    store = GraphStore.load(project)
    failed = [c for c in store.list_clusters() if c.prose_state == ProseState.failed]
    if failed:
        progress.begin(
            "auto_recovery",
            total=len(failed),
            status=(
                f"re-rendering {len(failed)} failed cluster(s) "
                f"with smaller chunks"
            ),
        )
        for cluster in failed:
            cluster.prose_state = ProseState.dirty
            cluster.last_rendered_hash = None
            store.save_cluster(cluster)
        store = GraphStore.load(project)
        recovery = ChunkedRenderer(
            config, store, llm, voice, min_chunk=1, max_chunk=2,
        )
        await recovery.render_all(force=False, progress=progress)
        store = GraphStore.load(project)
        still_failed = sum(
            1 for c in store.list_clusters()
            if c.prose_state == ProseState.failed
        )
        recovered = max(0, len(failed) - still_failed)
        progress.end(
            "auto_recovery",
            status=(
                f"{recovered} recovered · {still_failed} still failed"
                if still_failed else f"all {recovered} cluster(s) recovered"
            ),
        )

        progress.begin("finalise_retry", status="retrying after recovery")
        final_path = DocumentFinaliser(project, store, voice).finalise()
        if final_path is not None:
            progress.end("finalise_retry", status="delivered after recovery")
            result.final_path = final_path
            result.finalise_succeeded = True
        else:
            progress.end(
                "finalise_retry",
                status=f"still refused — {still_failed} cluster(s) unrecoverable",
            )


# ─── activity: find_gaps ─────────────────────────


async def _activity_find_gaps(
    request: ActivityRequest,
    config: Config,
    voice: Voice,
    llm: ClaudeClient,
    progress: EventQueueProgress,
    result: RunResult,
) -> None:
    """Per-section literature-gap analysis.

    Asks Claude what canonical works, standard counter-arguments, and
    recent papers each section should engage with. ``thorough`` mode
    additionally verifies every suggestion against OpenAlex so the
    report excludes hallucinated citations.

    Reads the scaffold (S2 onwards) — does NOT need a rendered paper
    or a reference document.
    """
    from ..lit_gaps import find_lit_gaps, write_lit_gaps_report

    project = request.project_path
    store = GraphStore.load(project)
    graph = store.get_graph()
    if not graph.sections:
        result.notes.append(
            "No sections in the author graph. Run Scaffold first."
        )
        progress._emit({"type": "run_failed", "reason": "no_sections"})
        return

    report = await find_lit_gaps(
        project_path=project,
        voice_name=voice.name,
        graph=graph,
        llm=llm,
        mode=request.mode,
        progress=progress,
    )
    out_path = write_lit_gaps_report(project, report)
    result.source_gap_path = out_path  # reuse field — UI surfaces it on Output
    result.finalise_succeeded = True
    paper_path = project / "outputs" / f"paper.{voice.name}.md"
    if paper_path.exists():
        result.final_path = paper_path
    result.notes.append(
        f"{report.total_suggestions} gap(s) suggested · "
        f"{report.verified_count} verified on OpenAlex"
    )


# ─── activity: refine ────────────────────────────


async def _activity_refine(
    request: ActivityRequest,
    config: Config,
    voice: Voice,
    llm: ClaudeClient,
    progress: EventQueueProgress,
    result: RunResult,
) -> None:
    """Audit + autofix loop + voice review.

    ``fast`` mode runs audit only (no LLM-driven edits).
    ``thorough`` mode runs autocorrect=aggressive convergence loop and
    a whole-document voice review at the end.
    """
    project = request.project_path
    store = GraphStore.load(project)
    clusters = store.list_clusters()
    result.total_clusters = len(clusters)
    result.rendered_clusters = sum(
        1 for c in clusters if c.prose_state == ProseState.generated
    )

    paper_path = project / "outputs" / f"paper.{voice.name}.md"
    if paper_path.exists():
        result.final_path = paper_path
        result.finalise_succeeded = True

    # ── 1. Audit ──
    progress.begin(
        "audit", total=len(clusters), status="running per-cluster checks",
    )
    flags = await AuditRunner(
        config, store, llm=llm, voice=voice
    ).run()
    result.audit_flags = len(flags)
    progress.end("audit", status=f"{len(flags)} flag(s)")

    if request.mode == "fast":
        return

    # ── 2. Convergence loop (thorough only) ──
    config.autocorrect = "aggressive"
    for pass_index in range(2, request.max_passes + 1):
        progress.begin_pass(pass_index, request.max_passes)
        store = GraphStore.load(project)
        autofix = await run_autofix_async(
            config, store, voice, llm, progress=progress
        )
        if autofix.accepted_rewrite > 0:
            progress.begin("rerender", status="re-rendering dirty clusters")
            store = GraphStore.load(project)
            renderer = ChunkedRenderer(
                config, store, llm, voice,
                min_chunk=request.chunk_min, max_chunk=request.chunk_max,
            )
            await renderer.render_all(force=False, progress=progress)
            progress.end("rerender", status="dirty clusters refreshed")

        progress.begin(
            "finalise", status=f"retry after pass {pass_index}",
        )
        final_path = DocumentFinaliser(project, store, voice).finalise()
        if final_path is not None:
            progress.end(
                "finalise", status=f"delivered after pass {pass_index}",
            )
            result.final_path = final_path
            result.finalise_succeeded = True
        else:
            progress.end("finalise", status="still refused")

        if autofix.total_changes == 0:
            result.notes.append(
                f"Pass {pass_index} produced no changes; loop stopped."
            )
            break

    # ── 3. Voice review (thorough only) ──
    if result.finalise_succeeded:
        progress.begin("voice_review", status="whole-document checks")
        store = GraphStore.load(project)
        try:
            report, vr_path = voice_review_document(project, store, voice)
            result.voice_review_path = vr_path
            progress.end(
                "voice_review",
                status=(
                    f"{report.pass_count} pass / "
                    f"{report.warning_count} warn / "
                    f"{report.fail_count} fail"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            progress.end(
                "voice_review", status=f"failed: {type(exc).__name__}"
            )
            result.notes.append(f"Voice review failed: {exc}")


# ─── activity: restructure ───────────────────────


async def _activity_restructure(
    request: ActivityRequest,
    config: Config,
    voice: Voice,
    llm: ClaudeClient,
    progress: EventQueueProgress,
    result: RunResult,
) -> None:
    """Suggest a more logically coherent ordering for the document.

    Reads the scaffold (S2 onwards). Advisory only — does not mutate
    the graph. ``fast`` mode skips the per-section cluster pass and
    only audits the top-level section order.
    """
    from ..restructure import analyse_structure, write_restructure_report

    project = request.project_path
    store = GraphStore.load(project)
    graph = store.get_graph()
    if not graph.sections:
        result.notes.append("No sections to analyse. Run Scaffold first.")
        progress._emit({"type": "run_failed", "reason": "no_sections"})
        return

    report = await analyse_structure(
        project_path=project,
        voice_name=voice.name,
        graph=graph,
        store=store,
        llm=llm,
        mode=request.mode,
        progress=progress,
    )
    out_path = write_restructure_report(project, report)
    result.source_gap_path = out_path  # reuse field for UI link
    result.finalise_succeeded = True
    paper_path = project / "outputs" / f"paper.{voice.name}.md"
    if paper_path.exists():
        result.final_path = paper_path
    result.notes.append(
        f"{len(report.suggestions)} restructure suggestion(s)"
    )


# ─── activity: review ────────────────────────────


async def _activity_review(
    request: ActivityRequest,
    config: Config,
    voice: Voice,
    llm: ClaudeClient,
    progress: EventQueueProgress,
    result: RunResult,
) -> None:
    """Supervisor-style review of the rendered paper.

    Produces per-cluster track-changes revisions, per-section
    critiques, and an overall assessment. ``fast`` mode skips the
    per-section + overall passes.
    """
    from ..review import produce_review, write_review_artefacts

    project = request.project_path
    paper_path = project / "outputs" / f"paper.{voice.name}.md"
    if not paper_path.exists():
        result.notes.append(f"No rendered paper at {paper_path}.")
        progress._emit({"type": "run_failed", "reason": "no_paper"})
        return

    store = GraphStore.load(project)
    graph = store.get_graph()
    report = await produce_review(
        project_path=project,
        voice_name=voice.name,
        graph=graph,
        store=store,
        llm=llm,
        mode=request.mode,
        progress=progress,
    )
    paths = write_review_artefacts(project, report)
    result.source_gap_path = paths.get("track_changes")
    result.voice_review_path = paths.get("critique")
    result.final_path = paper_path
    result.finalise_succeeded = True
    result.notes.append(
        f"{len(report.cluster_revisions)} cluster(s) reviewed; "
        f"track-changes paper at {paths.get('track_changes').name if paths.get('track_changes') else '(none)'}"
    )


# ─── bookkeeping ─────────────────────────────────


def record_activity_history(
    request: ActivityRequest,
    result: RunResult,
    voice_name: str,
) -> None:
    """Append a single activity record to ``activity_history.json``.

    Uses a separate file from the legacy ``run_history.json`` so the
    activity timeline is clean — old level-based records stay where
    they were."""
    history_path = (
        request.project_path / ".lattice" / "activity_history.json"
    )
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = []
    if history_path.exists():
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                history = data
        except json.JSONDecodeError:
            pass

    record = {
        "verb": request.verb,
        "mode": request.mode,
        "voice": voice_name,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": result.elapsed_seconds,
        "finalise_succeeded": result.finalise_succeeded,
        "rendered_clusters": result.rendered_clusters,
        "total_clusters": result.total_clusters,
        "audit_flags": result.audit_flags,
        "final_path": (
            str(result.final_path) if result.final_path else None
        ),
        "voice_review_path": (
            str(result.voice_review_path)
            if result.voice_review_path else None
        ),
        "source_gap_path": (
            str(result.source_gap_path)
            if result.source_gap_path else None
        ),
        "notes": list(result.notes),
    }
    history.append(record)
    if len(history) > 50:
        history = history[-50:]
    history_path.write_text(
        json.dumps(history, indent=2), encoding="utf-8"
    )


def read_activity_history(project_path: Path) -> list[dict[str, Any]]:
    """Return persisted activity history (oldest first) or empty list."""
    history_path = project_path / ".lattice" / "activity_history.json"
    if not history_path.exists():
        return []
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return []


def write_activity_changelog(
    request: ActivityRequest,
    result: RunResult,
    voice_name: str,
    pre: dict[str, Any],
    post: dict[str, Any],
) -> Path:
    """Write a markdown summary of what this activity changed."""
    changelogs_dir = (
        request.project_path / ".lattice" / "changelogs"
    )
    changelogs_dir.mkdir(parents=True, exist_ok=True)
    finished = datetime.now(timezone.utc)
    timestamp = finished.strftime("%Y%m%d_%H%M%S")
    target = changelogs_dir / f"{timestamp}_{request.verb}.md"

    def _delta(key: str) -> str:
        before = pre.get(key, 0) or 0
        after = post.get(key, 0) or 0
        diff = after - before
        if diff == 0:
            return f"{after} (no change)"
        sign = "+" if diff > 0 else ""
        return f"{after} ({sign}{diff} from {before})"

    notes_md = (
        "\n".join(f"- {n}" for n in result.notes)
        if result.notes else "_(no notes)_"
    )

    body = (
        f"# Changelog · {finished.isoformat(timespec='seconds')}\n\n"
        f"**Activity:** `{request.verb}` ({request.mode})\n"
        f"**Voice:** `{voice_name}`\n"
        f"**Duration:** {result.elapsed_seconds}s\n"
        f"**Outcome:** "
        f"{'✅ delivered' if result.finalise_succeeded else '❌ blocked'}\n\n"
        f"## What changed\n\n"
        f"| Metric | After (delta) |\n"
        f"|---|---|\n"
        f"| Sections | {_delta('section_count')} |\n"
        f"| Claims | {_delta('claim_count')} |\n"
        f"| Clusters | {_delta('cluster_count')} |\n"
        f"| Audit flags | {_delta('audit_flag_count')} |\n"
        f"| Paper word count | {_delta('paper_word_count')} |\n\n"
        f"## Notes\n\n{notes_md}\n"
    )
    target.write_text(body, encoding="utf-8")
    (changelogs_dir / "latest.md").write_text(body, encoding="utf-8")
    return target
