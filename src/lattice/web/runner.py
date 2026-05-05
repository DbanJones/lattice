"""Pipeline runner — legacy quick/standard/deep entry point.

.. deprecated::
   The "review levels" (quick / standard / deep) implemented here have
   been superseded by the verb-oriented activity model in
   :mod:`lattice.web.activities` (ingest, scaffold, draft, find_gaps,
   refine, restructure, review). The frontend no longer offers
   quick/standard/deep as user-facing options.

   This module is retained because:

   1. ``EventQueueProgress``, ``RunResult``, and
      :func:`capture_project_state` are imported by ``activities.py``.
   2. ``record_run_history`` and ``read_run_history`` continue to read
      and write ``.lattice/run_history.json`` for projects that have
      pre-activity records.
   3. The legacy ``POST /api/projects/{name}/runs`` endpoint still
      accepts level-based requests for any external callers.

The legacy review levels were:

- **quick**     — render only (no autocorrect, no audit, no convergence)
- **standard**  — render + audit + voice review (single pass, no autofix)
- **deep**      — render with autocorrect aggressive + convergence loop +
                  audit + voice review + source-gap review (if reference
                  document is supplied)

The runner exposes ``EventQueueProgress`` — a callback object compatible
with the ``ProgressTracker`` protocol from ``cli/progress.py`` — that
pushes structured JSON events to an asyncio.Queue. The web app's
WebSocket reads from the queue and forwards events to the browser, so
the existing renderer/autofix progress callbacks stream straight to the
front-end timeline.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..auditor.autofix import run_autofix_async
from ..auditor.runner import AuditRunner
from ..auditor.source_gap_review import SourceGapReview, write_report as write_gap_report
from ..auditor.voice_review import review_document as voice_review_document
from ..graph.models import AuthorGraph
from ..graph.store import GraphStore
from ..renderer.assembler_finalise import DocumentFinaliser
from ..renderer.chunked_renderer import ChunkedRenderer
from ..utils.config import Config
from ..utils.llm import ClaudeClient, claude_available
from ..voice.parser import Voice


ReviewLevel = Literal["quick", "standard", "deep"]


@dataclass
class RunRequest:
    """Inputs for a single web-triggered run."""

    project_path: Path
    voice_name: str
    level: ReviewLevel = "standard"
    reference_path: Path | None = None
    max_passes: int = 3
    chunk_min: int = 3
    chunk_max: int = 4
    force: bool = False


@dataclass
class RunResult:
    final_path: Path | None = None
    audit_flags: int = 0
    voice_review_path: Path | None = None
    source_gap_path: Path | None = None
    rendered_clusters: int = 0
    total_clusters: int = 0
    elapsed_seconds: float = 0.0
    finalise_succeeded: bool = False
    notes: list[str] = field(default_factory=list)


class EventQueueProgress:
    """Implements the cli/progress.py callback protocol; pushes JSON
    events to an ``asyncio.Queue`` so a WebSocket can stream them.

    The web frontend interprets these event types:

    - ``pass_started``        — a new convergence pass begins
    - ``phase_begun``         — a phase (render, audit, autofix, ...) starts
    - ``phase_advanced``      — increment a phase's counter (used per chunk)
    - ``phase_status``        — update a phase's status text
    - ``phase_ended``         — a phase finishes
    - ``run_finished``        — the whole pipeline is done
    - ``run_failed``          — pipeline raised an exception
    """

    def __init__(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.queue = queue
        self._started_at = time.monotonic()
        # Track per-phase state so we can include elapsed and counter
        # totals in every event without the frontend re-counting.
        self._phase_started_at: dict[str, float] = {}
        self._phase_total: dict[str, int | None] = {}
        self._phase_done: dict[str, int] = {}
        self._current_pass = 1
        self._total_passes = 1

    # ─── callback protocol ─────────────────────────

    def begin(self, phase: str, total: int | None = None, status: str = "") -> None:
        self._phase_started_at[phase] = time.monotonic()
        self._phase_total[phase] = total
        self._phase_done[phase] = 0
        self._emit({
            "type": "phase_begun",
            "phase": phase,
            "total": total,
            "status": status,
        })

    def advance(self, phase: str, n: int = 1, status: str = "") -> None:
        self._phase_done[phase] = self._phase_done.get(phase, 0) + n
        self._emit({
            "type": "phase_advanced",
            "phase": phase,
            "done": self._phase_done[phase],
            "total": self._phase_total.get(phase),
            "status": status,
            "elapsed_seconds": self._phase_elapsed(phase),
        })

    def update_status(self, phase: str, status: str) -> None:
        self._emit({
            "type": "phase_status",
            "phase": phase,
            "status": status,
            "done": self._phase_done.get(phase, 0),
            "total": self._phase_total.get(phase),
            "elapsed_seconds": self._phase_elapsed(phase),
        })

    def end(self, phase: str, status: str = "complete") -> None:
        self._emit({
            "type": "phase_ended",
            "phase": phase,
            "status": status,
            "elapsed_seconds": self._phase_elapsed(phase),
        })

    def begin_pass(self, pass_index: int, total_passes: int) -> None:
        self._current_pass = pass_index
        self._total_passes = total_passes
        self._emit({
            "type": "pass_started",
            "pass_index": pass_index,
            "total_passes": total_passes,
        })

    # ─── helpers ───────────────────────────────────

    def _phase_elapsed(self, phase: str) -> float:
        started = self._phase_started_at.get(phase)
        if started is None:
            return 0.0
        return round(time.monotonic() - started, 2)

    def total_elapsed(self) -> float:
        return round(time.monotonic() - self._started_at, 2)

    def _emit(self, event: dict[str, Any]) -> None:
        # Stamp every event with the wall-clock-relative total elapsed so
        # the frontend can render a single timeline without re-deriving.
        event.setdefault("total_elapsed_seconds", self.total_elapsed())
        event.setdefault("pass_index", self._current_pass)
        # ``put_nowait`` instead of ``put`` so the renderer never blocks
        # on a slow consumer. Queue is unbounded so this is always safe.
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover — unbounded queue
            pass


# ─── pipeline driver ───────────────────────────────


async def run_pipeline(
    request: RunRequest,
    progress: EventQueueProgress,
) -> RunResult:
    """Execute the requested review level. All progress flows through
    ``progress``; the return value is a final summary the API can persist
    or echo back to the frontend.

    The pipeline assumes the project has already been ingested + planned
    (i.e. ``cluster_plan.json`` exists). The web UI's project setup flow
    handles those upstream stages separately.
    """
    started = time.monotonic()
    result = RunResult(total_clusters=0)

    config = Config.load(request.project_path)
    if not claude_available():
        result.notes.append("Claude CLI not found on PATH; cannot run pipeline.")
        progress._emit({"type": "run_failed", "reason": "claude_not_available"})
        return result

    # Snapshot the pre-run state so we can emit a changelog after.
    pre_state = capture_project_state(request.project_path)

    store = GraphStore.load(request.project_path)
    voice = Voice.from_file(
        request.project_path / "voices" / f"{request.voice_name}.voice.md"
    )

    # Bootstrap ingest + plan if the project hasn't been parsed yet.
    # Freshly-created projects (especially scaffolded ones with no
    # outline body) won't have an author_graph or cluster_plan, so
    # the user shouldn't be expected to know to call those CLI
    # commands first.
    graph_path = request.project_path / ".lattice" / "author_graph.json"
    cluster_plan_path = request.project_path / ".lattice" / "cluster_plan.json"

    outline_md = request.project_path / "structure" / "outline.md"
    outline_docx = request.project_path / "structure" / "outline.docx"
    outline_path = outline_md if outline_md.exists() else outline_docx if outline_docx.exists() else None

    # ─── Auto-heal the outline before ingest ──────────────
    # This stage combines two recoveries into one phase the user sees
    # as "Structuring outline":
    #
    #   1. Raw prose (no `# THESIS` / `# A.` headers) → call Claude
    #      to extract the thesis, sections, and per-section claims.
    #   2. Structured but unrenderable (no `[user_synthesis]` tags
    #      AND no source papers indexed) → deterministically rewrite
    #      every claim bullet to add `[user_synthesis]` so the
    #      renderer's grounding check passes without a sources
    #      library.
    #   3. Missing conclusion section → append a default one (the
    #      voice template requires a closing section).
    #
    # Doing this inline with ingest means the user never has to
    # manually click a "Re-structure" button — the pipeline self-heals.
    if outline_path is not None and outline_path.suffix.lower() in (".md", ".markdown", ".txt"):
        from ..ingester.auto_outliner import (
            append_conclusion_section,
            has_conclusion_section,
            looks_like_lattice_outline,
            normalise_to_user_synthesis,
            structure_outline,
            write_structured_outline,
        )
        try:
            raw_text = outline_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw_text = outline_path.read_text(encoding="utf-8", errors="replace")

        outline_was_modified = False

        # ── (1) Raw prose path ─────────────────────────
        if raw_text.strip() and not looks_like_lattice_outline(raw_text):
            progress.begin(
                "structure_outline",
                status="raw prose detected — asking Claude to extract structure",
            )
            try:
                auto_llm = ClaudeClient(
                    default_model=config.default_model,
                    parallel=config.parallel_renders,
                )
                structured = await structure_outline(raw_text, auto_llm)
                outline_path = write_structured_outline(
                    request.project_path, structured, raw_text
                )
                outline_was_modified = True
                # Re-read so the (2)/(3) heuristics see the structured
                # version and don't double-process.
                raw_text = outline_path.read_text(encoding="utf-8")
                progress.end(
                    "structure_outline",
                    status=(
                        f"{structured.count(chr(10) + '#')} sections extracted; "
                        f"original archived to outline.raw.md"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                progress.end("structure_outline", status="failed")
                result.notes.append(
                    f"Auto-structuring failed: {type(exc).__name__}: {exc}"
                )
                progress._emit({
                    "type": "run_failed",
                    "reason": "auto_structure_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "outline_path": str(outline_path),
                })
                return result

        # ── (2) Renderability normalisation ─────────────
        # If the project has no indexed source papers and the outline
        # has claims tagged `[empirical]` / `[strong]` / etc. without
        # `[user_synthesis]`, those claims will fail the renderer's
        # grounding check. Rewrite every bullet to add
        # `[user_synthesis]` so they all render.
        refs_papers = request.project_path / "refs" / "papers"
        has_source_papers = (
            refs_papers.exists()
            and any(
                p.is_file() and p.suffix.lower() in (".pdf", ".docx", ".md", ".markdown", ".txt")
                for p in refs_papers.iterdir()
            )
        )
        if not has_source_papers:
            normalised, changed = normalise_to_user_synthesis(raw_text)
            if changed > 0:
                progress.begin(
                    "normalise_outline",
                    status=f"no sources library — tagging {changed} claim(s) as [user_synthesis]",
                )
                outline_path.write_text(normalised, encoding="utf-8")
                raw_text = normalised
                outline_was_modified = True
                progress.end(
                    "normalise_outline",
                    status=f"{changed} claim(s) marked as author-grounded so they render without sources",
                )

        # ── (3) Conclusion section ──────────────────────
        if not has_conclusion_section(raw_text):
            progress.begin(
                "add_conclusion",
                status="appending a default conclusion section",
            )
            with_conclusion = append_conclusion_section(raw_text)
            outline_path.write_text(with_conclusion, encoding="utf-8")
            raw_text = with_conclusion
            outline_was_modified = True
            progress.end(
                "add_conclusion",
                status="conclusion section added (edit it before re-running for a polished result)",
            )

        # Drop stale graph + plan if anything changed so ingest re-runs.
        if outline_was_modified:
            if graph_path.exists():
                graph_path.unlink()
            if cluster_plan_path.exists():
                cluster_plan_path.unlink()

    # Re-ingest if outline is newer than the saved graph. Otherwise a
    # user who fixes a malformed outline would stay stuck forever
    # because the bootstrap would short-circuit on the stale graph.
    needs_ingest = not graph_path.exists()
    if (
        not needs_ingest
        and outline_path is not None
        and outline_path.stat().st_mtime > graph_path.stat().st_mtime
    ):
        needs_ingest = True
        if cluster_plan_path.exists():
            cluster_plan_path.unlink()

    # Consistency check: if the outline declares a `[role: conclusion]`
    # tag but no saved graph section has that role, the parser must
    # have been older when the graph was last written. Force re-ingest
    # so the user doesn't get stuck on stale parsing.
    if (
        not needs_ingest
        and outline_path is not None
        and graph_path.exists()
    ):
        try:
            outline_text = outline_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            outline_text = ""
        from ..ingester.auto_outliner import has_conclusion_role_tag
        if has_conclusion_role_tag(outline_text):
            try:
                graph_data = __import__("json").loads(
                    graph_path.read_text(encoding="utf-8")
                )
                roles = {s.get("role") for s in graph_data.get("sections", [])}
                if "conclusion" not in roles:
                    needs_ingest = True
                    if cluster_plan_path.exists():
                        cluster_plan_path.unlink()
            except Exception:  # noqa: BLE001
                pass

    if needs_ingest:
        if outline_path is None:
            result.notes.append(
                "Project has no outline. Add one in the project setup before "
                "starting a review."
            )
            progress._emit({"type": "run_failed", "reason": "no_outline"})
            return result

        progress.begin("ingest", status=f"parsing {outline_path.name}")
        try:
            if outline_path.suffix.lower() == ".docx":
                from ..ingester.docx import DOCXOutlineIngester
                ingester: Any = DOCXOutlineIngester(config)
            else:
                from ..ingester.markdown import MarkdownOutlineIngester
                ingester = MarkdownOutlineIngester(config)
            graph = await ingester.ingest(
                outline_path, project_name=request.project_path.name
            )
            store.save_graph(graph)
            progress.end(
                "ingest",
                status=f"{len(graph.sections)} sections, {len(graph.claims)} claims",
            )
        except Exception as exc:  # noqa: BLE001
            result.notes.append(
                f"Ingest failed: {type(exc).__name__}: {exc}"
            )
            progress._emit({
                "type": "run_failed",
                "reason": "ingest_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            return result


    # Whether or not we just re-ingested, if the graph on disk has no
    # structure there's no point running the planner. This catches the
    # common UX trap of pasting raw paper prose into outline.md instead
    # of writing a lattice-format outline (no `# THESIS` / `# A.`
    # headers means the parser produces 0 sections).
    store = GraphStore.load(request.project_path)
    current_graph = store.get_graph()
    if not current_graph.sections or not current_graph.claims:
        outline_hint = (
            str(outline_path) if outline_path is not None
            else str(request.project_path / "structure" / "outline.md")
        )
        result.notes.append(
            "Outline parsed but contained no sections or claims. "
            "Lattice expects `# THESIS` and `# A. Section` headers "
            "with `  - claim text` bullets — not raw paper prose. "
            f"Edit {outline_hint} and try again."
        )
        progress._emit({
            "type": "run_failed",
            "reason": "outline_has_no_structure",
            "detail": (
                "Outline produced no headed sections or claim bullets. "
                "The file looks like raw prose rather than a lattice "
                "outline. Lattice expects:\n\n"
                "    # THESIS\n\n"
                "    Your thesis sentence.\n\n"
                "    # A. Section heading\n\n"
                "      - First claim\n"
                "      - MY VIEW: synthesis [user_synthesis]\n"
            ),
            "outline_path": outline_hint,
        })
        return result

    if not cluster_plan_path.exists():
        progress.begin("plan", status="building cluster plan")
        try:
            from ..renderer.assembler import Assembler
            clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
            progress.end(
                "plan",
                status=f"{len(clusters)} clusters across "
                       f"{len({c.section_id for c in clusters})} sections",
            )
        except Exception as exc:  # noqa: BLE001
            result.notes.append(
                f"Plan failed: {type(exc).__name__}: {exc}"
            )
            progress._emit({
                "type": "run_failed",
                "reason": "plan_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            return result

    store = GraphStore.load(request.project_path)  # reload after possible plan
    clusters = store.list_clusters()
    if not clusters:
        result.notes.append(
            "Cluster plan is empty after ingest. Check the outline structure."
        )
        progress._emit({"type": "run_failed", "reason": "empty_cluster_plan"})
        return result
    result.total_clusters = len(clusters)

    llm = ClaudeClient(
        default_model=config.default_model,
        parallel=config.parallel_renders,
    )

    # Override autocorrect per review level.
    if request.level == "quick":
        config.autocorrect = "none"
    elif request.level == "standard":
        config.autocorrect = "safe"
    elif request.level == "deep":
        config.autocorrect = "aggressive"

    # ─── Stage 1: render ─────────────────────────
    progress.begin_pass(1, max(1, request.max_passes if request.level == "deep" else 1))
    renderer = ChunkedRenderer(
        config, store, llm, voice,
        min_chunk=request.chunk_min, max_chunk=request.chunk_max,
    )
    rendered = await renderer.render_all(force=request.force, progress=progress)
    result.rendered_clusters = sum(
        1 for r in rendered.values()
        if r and "CLUSTER_UNRENDERABLE" not in r
    )

    # ─── Stage 2: finalise (always tried) ───────
    progress.begin("finalise", status="checking readiness")
    final_path = DocumentFinaliser(request.project_path, store, voice).finalise()
    if final_path is not None:
        progress.end("finalise", status="document delivered")
        result.final_path = final_path
        result.finalise_succeeded = True
    else:
        # The finalise refused, but downstream stages might fix it
        # (auto-recovery + convergence loop). Differentiate the status
        # so the UI can render this as an intermediate "needs more
        # work" step instead of a terminal failure.
        next_steps: list[str] = []
        if request.level in ("standard", "deep"):
            next_steps.append("audit")
        # Auto-recovery only fires if there are failed clusters; we can't
        # know that yet, but mention it as a possibility.
        next_steps.append("recovery retry")
        if request.level == "deep" and config.autocorrect != "none":
            next_steps.append("convergence loop")
        progress.end(
            "finalise",
            status=f"not ready yet — pipeline continues with {' → '.join(next_steps)}",
        )

    # ─── Stage 2b: auto-recovery (all review levels) ───
    # If delivery was refused on the first finalise, look at the
    # blocking reasons. For things we can fix without LLM input
    # (failed clusters, register-bleed clusters, prose with
    # CLUSTER_UNRENDERABLE markers) reset them to ``dirty`` and
    # re-render — the LLM gets a fresh shot and false-positive
    # validation rejections often resolve themselves on a retry.
    # This runs once; the deeper convergence loop below handles
    # iterative fixes when level=deep.
    if not result.finalise_succeeded:
        from ..graph.models import ProseState
        store = GraphStore.load(request.project_path)
        all_clusters = store.list_clusters()
        failed_clusters = [
            c for c in all_clusters if c.prose_state == ProseState.failed
        ]
        failed_before = len(failed_clusters)

        if failed_clusters:
            # On the recovery pass, shrink the chunk size. The most
            # common cause of cluster failure is JSON truncation when
            # the model fills its token budget and the chunked
            # renderer's response gets cut off mid-string. Smaller
            # chunks → smaller responses → less truncation risk. One
            # cluster at a time is the safest fallback.
            recovery_chunk_min = 1
            recovery_chunk_max = max(1, min(2, request.chunk_max - 1))

            progress.begin(
                "auto_recovery",
                total=failed_before,
                status=(
                    f"first finalise refused — re-rendering "
                    f"{failed_before} failed cluster(s) with smaller "
                    f"chunks (max {recovery_chunk_max} per call)"
                ),
            )
            recovered = 0
            still_failed = failed_before
            try:
                # Reset state on disk so the renderer picks them up.
                for cluster in failed_clusters:
                    cluster.prose_state = ProseState.dirty
                    cluster.last_rendered_hash = None
                    store.save_cluster(cluster)
                # Reload and re-render (force=False — only dirty ones run).
                store = GraphStore.load(request.project_path)
                recovery_renderer = ChunkedRenderer(
                    config, store, llm, voice,
                    min_chunk=recovery_chunk_min,
                    max_chunk=recovery_chunk_max,
                )
                await recovery_renderer.render_all(
                    force=False, progress=progress
                )
                # Compute delta from this recovery pass.
                store = GraphStore.load(request.project_path)
                still_failed = sum(
                    1 for c in store.list_clusters()
                    if c.prose_state == ProseState.failed
                )
                recovered = max(0, failed_before - still_failed)
                # Update the totals exposed in the run summary.
                result.rendered_clusters = sum(
                    1 for c in store.list_clusters()
                    if c.prose_state == ProseState.generated
                )

                if recovered > 0 and still_failed == 0:
                    end_status = (
                        f"all {recovered} cluster(s) recovered"
                    )
                elif recovered > 0:
                    end_status = (
                        f"{recovered} recovered · {still_failed} "
                        f"still failed"
                    )
                else:
                    end_status = (
                        f"0 recovered · {still_failed} still failed "
                        f"(retrying didn't help)"
                    )
                progress.end("auto_recovery", status=end_status)
            except Exception as exc:  # noqa: BLE001
                progress.end(
                    "auto_recovery",
                    status=f"recovery raised {type(exc).__name__}: {exc}",
                )
                result.notes.append(
                    f"Auto-recovery raised {type(exc).__name__}: {exc}"
                )

            # Retry finalise after the recovery render. Use a distinct
            # phase key so the timeline shows it as its own row in
            # chronological order rather than collapsing into the first
            # finalise row.
            progress.begin(
                "finalise_retry",
                status="checking readiness after auto-recovery",
            )
            final_path = DocumentFinaliser(
                request.project_path, store, voice
            ).finalise()
            if final_path is not None:
                progress.end(
                    "finalise_retry",
                    status="delivered after recovery",
                )
                result.final_path = final_path
                result.finalise_succeeded = True
                result.notes.append(
                    f"Auto-recovery succeeded: {recovered} of "
                    f"{failed_before} cluster(s) recovered, "
                    f"finalise passed on retry."
                )
            else:
                progress.end(
                    "finalise_retry",
                    status=(
                        f"still refused — {still_failed} cluster(s) "
                        f"could not be recovered"
                    ),
                )
                result.notes.append(
                    f"Auto-recovery could not save delivery. "
                    f"{recovered} of {failed_before} cluster(s) "
                    f"recovered; {still_failed} still failed. "
                    f"See blocking detail below for diagnostics."
                )

    # ─── Stage 2b2: reference extraction from outline.raw.md ──
    # When the auto-outliner fired earlier, the original raw paper
    # text is preserved at structure/outline.raw.md. That text usually
    # contains the paper's References / Bibliography section, which
    # otherwise gets lost. Pull citation metadata out of it on the
    # first standard/deep review and persist as Sources so the
    # References tab fills automatically.
    if request.level in ("standard", "deep"):
        outline_raw = request.project_path / "structure" / "outline.raw.md"
        meta_path = request.project_path / ".lattice" / "project_meta.json"
        meta_data: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta_data = __import__("json").loads(
                    meta_path.read_text(encoding="utf-8")
                )
            except Exception:  # noqa: BLE001
                pass
        already_extracted = bool(meta_data.get("references_extracted"))
        if outline_raw.exists() and not already_extracted:
            progress.begin(
                "extract_references",
                status=(
                    "scanning outline.raw.md for a bibliography "
                    "section"
                ),
            )
            try:
                from ..enricher.reference_extraction import (
                    extract_citations_from_text,
                    citation_to_synthetic_source,
                )
                raw_text = outline_raw.read_text(
                    encoding="utf-8", errors="replace"
                )
                citations = await extract_citations_from_text(raw_text, llm)
                store_for_refs = GraphStore.load(request.project_path)
                existing_ids = {
                    s.source_id for s in store_for_refs.list_sources()
                }
                added = 0
                for citation in citations:
                    source = citation_to_synthetic_source(citation)
                    base_id = source.source_id
                    counter = 2
                    while source.source_id in existing_ids:
                        source.source_id = f"{base_id}_{counter}"
                        counter += 1
                    store_for_refs.save_source(source)
                    existing_ids.add(source.source_id)
                    added += 1

                # Persist the "we've done this" flag so we don't
                # re-extract on every subsequent review.
                meta_data["references_extracted"] = True
                meta_data["references_extracted_count"] = added
                meta_path.parent.mkdir(parents=True, exist_ok=True)
                meta_path.write_text(
                    __import__("json").dumps(meta_data, indent=2),
                    encoding="utf-8",
                )
                progress.end(
                    "extract_references",
                    status=(
                        f"+{added} reference(s) extracted from "
                        f"outline.raw.md"
                    ),
                )
                if added > 0:
                    result.notes.append(
                        f"Extracted {added} reference(s) from the original "
                        f"paper text in outline.raw.md."
                    )
            except Exception as exc:  # noqa: BLE001
                progress.end(
                    "extract_references",
                    status=f"extraction raised {type(exc).__name__}",
                )
                result.notes.append(
                    f"Reference extraction raised "
                    f"{type(exc).__name__}: {exc}"
                )
        else:
            progress._emit({
                "type": "phase_skipped",
                "phase": "extract_references",
                "reason": (
                    "already extracted on a previous run"
                    if already_extracted else
                    "no outline.raw.md to scan (auto-structure didn't fire)"
                ),
            })
    else:
        progress._emit({
            "type": "phase_skipped",
            "phase": "extract_references",
            "reason": "only runs in Standard or Deep reviews",
        })

    # ─── Stage 2c: relationship inference (standard + deep) ──
    # Auto-outlined projects start with zero inter-claim
    # relationships. Ask the LLM to propose `supports` /
    # `contradicts` / `extends` / etc. edges so the Outline tab's
    # graph view actually shows argumentative structure.
    inferred_added = 0
    affected_clusters: set[str] = set()
    if request.level in ("standard", "deep"):
        from ..graph.models import ProseState
        store = GraphStore.load(request.project_path)
        graph = store.get_graph()
        existing_rel_count = len(graph.relationships)
        progress.begin(
            "relationship_inference",
            status=f"analysing {len(graph.claims)} claim(s) for relationships",
        )
        try:
            from ..enricher.relationship_inference import (
                infer_relationships, merge_inferred_relationships,
            )
            inferred = await infer_relationships(graph, llm)
            if inferred:
                inferred_added, _skipped_dupes = merge_inferred_relationships(
                    graph, inferred
                )
                store.save_graph(graph)

                # Mark every cluster that contains a claim newly
                # touched by an inferred relationship as `dirty` so
                # the renderer redrafts those clusters with the new
                # argumentative context baked in. Without this, the
                # 29 new edges would sit on the graph but never
                # surface in the prose.
                if inferred_added > 0:
                    touched_claims = {r.from_claim for r in inferred}
                    touched_claims.update(r.to_claim for r in inferred)
                    for cluster in store.list_clusters():
                        if cluster.prose_state != ProseState.generated:
                            continue
                        cluster_claims = {
                            entry.claim_id
                            for entry in cluster.claim_sequence
                        }
                        if cluster_claims & touched_claims:
                            cluster.prose_state = ProseState.dirty
                            cluster.last_rendered_hash = None
                            store.save_cluster(cluster)
                            affected_clusters.add(cluster.cluster_id)

            end_status = (
                f"+{inferred_added} relationship(s) "
                f"({existing_rel_count + inferred_added} total)"
            )
            if affected_clusters:
                end_status += (
                    f" · {len(affected_clusters)} cluster(s) marked "
                    f"for redraft"
                )
            progress.end("relationship_inference", status=end_status)
            if inferred_added > 0:
                result.notes.append(
                    f"Inferred {inferred_added} new claim relationships; "
                    f"queued {len(affected_clusters)} cluster(s) for "
                    f"redraft."
                )
        except Exception as exc:  # noqa: BLE001
            progress.end(
                "relationship_inference",
                status=f"inference raised {type(exc).__name__}",
            )
            result.notes.append(
                f"Relationship inference raised {type(exc).__name__}: {exc}"
            )
    else:
        progress._emit({
            "type": "phase_skipped",
            "phase": "relationship_inference",
            "reason": "only runs in Standard or Deep reviews",
        })

    # ─── Stage 2d: redraft affected clusters ──
    # Any cluster the inference stage marked dirty needs its prose
    # regenerated so the new relationships get woven into the text.
    # Runs only when there are dirty clusters — otherwise this stage
    # is a no-op and emits as skipped.
    if affected_clusters:
        progress.begin(
            "redraft",
            total=len(affected_clusters),
            status=(
                f"redrafting {len(affected_clusters)} cluster(s) with "
                f"the new relationships baked in"
            ),
        )
        try:
            store = GraphStore.load(request.project_path)
            redraft_renderer = ChunkedRenderer(
                config, store, llm, voice,
                min_chunk=request.chunk_min, max_chunk=request.chunk_max,
            )
            await redraft_renderer.render_all(
                force=False, progress=progress,
            )
            store = GraphStore.load(request.project_path)
            result.rendered_clusters = sum(
                1 for c in store.list_clusters()
                if c.prose_state == ProseState.generated
            )
            progress.end(
                "redraft",
                status=(
                    f"redrafted {len(affected_clusters)} cluster(s)"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            progress.end(
                "redraft",
                status=f"redraft raised {type(exc).__name__}",
            )
            result.notes.append(
                f"Redraft raised {type(exc).__name__}: {exc}"
            )
    else:
        progress._emit({
            "type": "phase_skipped",
            "phase": "redraft",
            "reason": (
                "no clusters needed redrafting — either no new "
                "relationships, or none touched cached prose"
            ),
        })

    # ─── Stage 3: audit (standard + deep) ──────
    if request.level in ("standard", "deep"):
        progress.begin(
            "audit", total=len(clusters),
            status="running per-cluster checks",
        )
        flags = await AuditRunner(config, store, llm=llm, voice=voice).run()
        result.audit_flags = len(flags)
        progress.end("audit", status=f"{len(flags)} flag(s)")
    else:
        progress._emit({
            "type": "phase_skipped",
            "phase": "audit",
            "reason": "only runs in Standard or Deep reviews",
        })

    # ─── Stage 4: convergence loop (deep only) ──
    if request.level != "deep":
        progress._emit({
            "type": "phase_skipped",
            "phase": "convergence_loop",
            "reason": "only runs in Deep reviews",
        })
    elif result.finalise_succeeded:
        progress._emit({
            "type": "phase_skipped",
            "phase": "convergence_loop",
            "reason": "already delivered — no need to autofix",
        })
    elif config.autocorrect == "none":
        progress._emit({
            "type": "phase_skipped",
            "phase": "convergence_loop",
            "reason": "autocorrect=none in config.yml",
        })
    if (
        request.level == "deep"
        and not result.finalise_succeeded
        and config.autocorrect != "none"
    ):
        for pass_index in range(2, request.max_passes + 1):
            progress.begin_pass(pass_index, request.max_passes)
            store = GraphStore.load(request.project_path)  # reload after persist
            autofix_result = await run_autofix_async(
                config, store, voice, llm, progress=progress
            )

            if autofix_result.accepted_rewrite > 0:
                progress.begin("rerender", status="re-rendering dirty clusters")
                store = GraphStore.load(request.project_path)
                renderer2 = ChunkedRenderer(
                    config, store, llm, voice,
                    min_chunk=request.chunk_min, max_chunk=request.chunk_max,
                )
                await renderer2.render_all(force=False, progress=progress)
                progress.end("rerender", status="dirty clusters refreshed")

            progress.begin("finalise", status=f"retry after pass {pass_index}")
            final_path = DocumentFinaliser(request.project_path, store, voice).finalise()
            if final_path is not None:
                progress.end("finalise", status=f"delivered after pass {pass_index}")
                result.final_path = final_path
                result.finalise_succeeded = True
                break
            progress.end("finalise", status="still refused")

            if autofix_result.total_changes == 0:
                result.notes.append(
                    f"Pass {pass_index} produced no changes; convergence loop stopped."
                )
                break

    # ─── Stage 5: voice review (standard + deep) ──
    if request.level in ("standard", "deep") and result.finalise_succeeded:
        progress.begin("voice_review", status="running whole-document checks")
        report, vr_path = voice_review_document(request.project_path, store, voice)
        result.voice_review_path = vr_path
        progress.end(
            "voice_review",
            status=f"{report.pass_count} pass / {report.warning_count} warn / {report.fail_count} fail",
        )
    elif request.level in ("standard", "deep"):
        progress._emit({
            "type": "phase_skipped",
            "phase": "voice_review",
            "reason": "skipped because finalise was refused — fix the blocking flags first",
        })
    else:
        progress._emit({
            "type": "phase_skipped",
            "phase": "voice_review",
            "reason": "only runs in Standard or Deep reviews",
        })

    # ─── Stage 6: source-gap review (deep only) ──
    if (
        request.level == "deep"
        and request.reference_path is not None
        and result.finalise_succeeded
    ):
        progress.begin("source_gap_review", status="comparing to reference document")
        graph = store.get_graph()
        review = SourceGapReview(config, llm)
        sg_report = await review.review(
            paper_path=result.final_path,
            reference_path=request.reference_path,
            graph=graph,
        )
        result.source_gap_path = write_gap_report(
            sg_report, request.project_path, voice.name
        )
        progress.end(
            "source_gap_review",
            status=f"{len(sg_report.gaps)} gap(s) identified",
        )
    else:
        if request.level != "deep":
            reason = "only runs in Deep reviews"
        elif request.reference_path is None:
            reason = "no reference document path supplied (Advanced options)"
        elif not result.finalise_succeeded:
            reason = "skipped because finalise was refused"
        else:
            reason = "preconditions not met"
        progress._emit({
            "type": "phase_skipped",
            "phase": "source_gap_review",
            "reason": reason,
        })

    result.elapsed_seconds = round(time.monotonic() - started, 2)

    # Gather blocking diagnostics so the UI can show actionable detail
    # without the user having to crack open delivery_blocked.md by hand.
    blocking_summary = (
        gather_delivery_diagnostics(request.project_path, voice.name)
        if not result.finalise_succeeded
        else None
    )

    # Append to run history so the UI can show level progression
    # (which levels have run, when, and what they produced). This lets
    # the user start at quick and upgrade to standard / deep without
    # losing context.
    try:
        record_run_history(request, result, voice.name)
    except Exception as exc:  # noqa: BLE001
        # Non-essential; don't kill the run because history failed.
        result.notes.append(
            f"Could not record run history: {type(exc).__name__}: {exc}"
        )

    # Snapshot the post-run state and write a markdown changelog. The
    # user can inspect what each review actually changed (clusters
    # re-rendered, words added/removed, audit flags resolved or raised,
    # outline mutations) without comparing two graphs by hand.
    changelog_path: Path | None = None
    try:
        post_state = capture_project_state(request.project_path)
        changelog_path = write_changelog(
            request, result, voice.name, pre_state, post_state
        )
    except Exception as exc:  # noqa: BLE001
        result.notes.append(
            f"Could not write changelog: {type(exc).__name__}: {exc}"
        )

    # Persist a per-project references file so the citations + usage
    # data travel with the project. ``references.json`` is the
    # structured manifest (every supported style pre-formatted) and
    # ``references.md`` is a human-readable bibliography.
    references_paths: dict[str, Path] = {}
    try:
        from ..output.references_manifest import write_project_references
        # Pull any user-saved 'about' overrides so the markdown
        # surfaces hand-written summaries.
        notes_path = request.project_path / ".lattice" / "reference_notes.json"
        overrides: dict[str, str] = {}
        if notes_path.exists():
            try:
                import json as _json
                overrides = {
                    k: str(v) for k, v in _json.loads(
                        notes_path.read_text(encoding="utf-8")
                    ).items() if isinstance(v, str)
                }
            except Exception:  # noqa: BLE001
                pass
        references_paths = write_project_references(
            request.project_path,
            summary_overrides=overrides,
            cited_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        result.notes.append(
            f"Could not write references file: {type(exc).__name__}: {exc}"
        )

    progress._emit({
        "type": "run_finished",
        "elapsed_seconds": result.elapsed_seconds,
        "final_path": str(result.final_path) if result.final_path else None,
        "rendered_clusters": result.rendered_clusters,
        "total_clusters": result.total_clusters,
        "audit_flags": result.audit_flags,
        "finalise_succeeded": result.finalise_succeeded,
        "voice_review_path": str(result.voice_review_path) if result.voice_review_path else None,
        "source_gap_path": str(result.source_gap_path) if result.source_gap_path else None,
        "notes": result.notes,
        "blocking": blocking_summary,
        "changelog_path": str(changelog_path) if changelog_path else None,
        "references_json_path": (
            str(references_paths.get("json"))
            if references_paths.get("json") else None
        ),
        "references_md_path": (
            str(references_paths.get("md"))
            if references_paths.get("md") else None
        ),
    })
    return result


def capture_project_state(project_path: Path) -> dict[str, Any]:
    """Snapshot the parts of a project that change run-to-run, so a
    pre / post diff can describe what a review did. Cheap to run; only
    reads small JSON files."""
    import json as _json
    state: dict[str, Any] = {
        "section_count": 0,
        "claim_count": 0,
        "cluster_count": 0,
        "cluster_states": {},
        "audit_flag_count": 0,
        "audit_flags_by_severity": {"critical": 0, "standard": 0, "minor": 0},
        "paper_word_count": 0,
        "outline_chars": 0,
        "outline_first_lines": [],
    }
    graph_path = project_path / ".lattice" / "author_graph.json"
    if graph_path.exists():
        try:
            data = _json.loads(graph_path.read_text(encoding="utf-8"))
            state["section_count"] = len(data.get("sections") or [])
            state["claim_count"] = len(data.get("claims") or [])
        except _json.JSONDecodeError:
            pass

    cluster_path = project_path / ".lattice" / "cluster_plan.json"
    if cluster_path.exists():
        try:
            data = _json.loads(cluster_path.read_text(encoding="utf-8"))
            clusters = data if isinstance(data, list) else data.get("clusters", [])
            state["cluster_count"] = len(clusters)
            state["cluster_states"] = {
                c.get("cluster_id"): c.get("prose_state", "unknown")
                for c in clusters
            }
        except _json.JSONDecodeError:
            pass

    flags_dir = project_path / ".lattice" / "audit"
    if flags_dir.exists():
        for flags_file in flags_dir.glob("*.json"):
            try:
                data = _json.loads(flags_file.read_text(encoding="utf-8"))
                flags = data if isinstance(data, list) else data.get("flags", [])
                state["audit_flag_count"] += len(flags)
                for f in flags:
                    sev = f.get("severity", "minor")
                    if sev in state["audit_flags_by_severity"]:
                        state["audit_flags_by_severity"][sev] += 1
            except _json.JSONDecodeError:
                pass

    paper_path = project_path / "outputs" / "paper.academic.md"
    if paper_path.exists():
        try:
            text = paper_path.read_text(encoding="utf-8")
            state["paper_word_count"] = len(text.split())
        except OSError:
            pass

    outline_path = project_path / "structure" / "outline.md"
    if outline_path.exists():
        try:
            text = outline_path.read_text(encoding="utf-8", errors="replace")
            state["outline_chars"] = len(text)
            state["outline_first_lines"] = text.splitlines()[:5]
        except OSError:
            pass
    return state


def write_changelog(
    request: RunRequest,
    result: RunResult,
    voice_name: str,
    pre: dict[str, Any],
    post: dict[str, Any],
) -> Path:
    """Compare pre/post snapshots and produce a human-readable
    markdown changelog. Saved under ``.lattice/changelogs/`` with a
    timestamped filename; the most recent run is also linked to as
    ``.lattice/changelogs/latest.md`` for quick UI access."""
    from datetime import datetime, timezone

    changelogs_dir = request.project_path / ".lattice" / "changelogs"
    changelogs_dir.mkdir(parents=True, exist_ok=True)
    finished = datetime.now(timezone.utc)
    timestamp = finished.strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{request.level}.md"
    target = changelogs_dir / filename

    def _delta(key: str) -> str:
        before = pre.get(key, 0)
        after = post.get(key, 0)
        diff = after - before
        if diff == 0:
            return f"{after} (no change)"
        sign = "+" if diff > 0 else ""
        return f"{after} ({sign}{diff} from {before})"

    def _diff_cluster_states() -> list[str]:
        before = pre.get("cluster_states", {}) or {}
        after = post.get("cluster_states", {}) or {}
        all_ids = sorted(set(before) | set(after))
        rows: list[str] = []
        for cid in all_ids:
            b = before.get(cid, "—")
            a = after.get(cid, "—")
            if b != a:
                rows.append(f"- `{cid}`: `{b}` → `{a}`")
        return rows

    cluster_rows = _diff_cluster_states()
    severity_lines: list[str] = []
    for sev in ("critical", "standard", "minor"):
        b = pre.get("audit_flags_by_severity", {}).get(sev, 0)
        a = post.get("audit_flags_by_severity", {}).get(sev, 0)
        if a == b:
            severity_lines.append(f"- {sev}: {a}")
        else:
            sign = "+" if a > b else ""
            severity_lines.append(f"- {sev}: {a} ({sign}{a - b} from {b})")

    notes_md = (
        "\n".join(f"- {n}" for n in result.notes)
        if result.notes else "_(no notes)_"
    )

    body = f"""# Changelog · {finished.isoformat(timespec='seconds')}

**Review level:** `{request.level}`
**Voice:** `{voice_name}`
**Duration:** {result.elapsed_seconds}s
**Outcome:** {"✅ delivered" if result.finalise_succeeded else "❌ blocked"}
**Final path:** {f"`{result.final_path}`" if result.final_path else "_(none — see blocking flags)_"}

## What changed

| Metric | After (delta) |
|---|---|
| Sections | {_delta('section_count')} |
| Claims | {_delta('claim_count')} |
| Clusters | {_delta('cluster_count')} |
| Audit flags (total) | {_delta('audit_flag_count')} |
| Paper word count | {_delta('paper_word_count')} |

### Audit flags by severity

{chr(10).join(severity_lines)}

### Cluster prose state changes

{chr(10).join(cluster_rows) if cluster_rows else "_(no clusters changed state)_"}

### Outline

- Pre size: {pre.get('outline_chars', 0):,} chars
- Post size: {post.get('outline_chars', 0):,} chars
- Outline {"was modified during this run (auto-heal or auto-structure fired)" if pre.get("outline_chars") != post.get("outline_chars") else "was not modified during this run"}

## Run notes

{notes_md}
"""
    target.write_text(body, encoding="utf-8")
    # Keep a `latest.md` symlink-like copy for quick access.
    (changelogs_dir / "latest.md").write_text(body, encoding="utf-8")
    return target


def list_changelogs(project_path: Path) -> list[dict[str, Any]]:
    """Return metadata for every changelog file in this project,
    newest first."""
    changelogs_dir = project_path / ".lattice" / "changelogs"
    if not changelogs_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for f in sorted(changelogs_dir.glob("*.md"), reverse=True):
        if f.name == "latest.md":
            continue
        stat = f.stat()
        out.append({
            "filename": f.name,
            "size_bytes": stat.st_size,
            "mtime": stat.st_mtime,
        })
    return out


def record_run_history(
    request: RunRequest, result: RunResult, voice_name: str
) -> None:
    """Append a single run record to ``.lattice/run_history.json``.

    The history is the source of truth the UI uses to decide which
    review levels have been completed, what each one produced, and
    what additional steps the next level would add. Records are
    immutable — we always append, never rewrite.
    """
    import json as _json
    from datetime import datetime, timezone
    history_path = request.project_path / ".lattice" / "run_history.json"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    history: list[dict[str, Any]] = []
    if history_path.exists():
        try:
            existing = _json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                history = existing
        except _json.JSONDecodeError:
            pass  # corrupt file → start fresh

    record = {
        "level": request.level,
        "voice": voice_name,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": result.elapsed_seconds,
        "finalise_succeeded": result.finalise_succeeded,
        "rendered_clusters": result.rendered_clusters,
        "total_clusters": result.total_clusters,
        "audit_flags": result.audit_flags,
        "final_path": str(result.final_path) if result.final_path else None,
        "voice_review_path": (
            str(result.voice_review_path) if result.voice_review_path else None
        ),
        "source_gap_path": (
            str(result.source_gap_path) if result.source_gap_path else None
        ),
        "notes": list(result.notes),
    }
    history.append(record)
    # Cap at 50 records so the file doesn't grow unbounded over time.
    if len(history) > 50:
        history = history[-50:]
    history_path.write_text(
        _json.dumps(history, indent=2), encoding="utf-8"
    )


def read_run_history(project_path: Path) -> list[dict[str, Any]]:
    """Return the persisted run history (newest last) or an empty list
    if no history exists. Never raises — UI can rely on a list shape."""
    import json as _json
    history_path = project_path / ".lattice" / "run_history.json"
    if not history_path.exists():
        return []
    try:
        data = _json.loads(history_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except _json.JSONDecodeError:
        pass
    return []


def gather_delivery_diagnostics(
    project_path: Path, voice_name: str
) -> dict[str, Any]:
    """Collect everything the UI needs to explain a refused delivery.

    Returns a dict with:
      - ``readiness_flags``: parsed list of {category, message, fix} from
        ``delivery_blocked.md`` (the readiness-check output).
      - ``failed_clusters``: list of {cluster_id, reason} pulled from any
        ``CLUSTER_UNRENDERABLE`` markers in the per-cluster prose files.
      - ``raw_delivery_blocked``: the full markdown blob if it exists,
        for power users who want to see everything.

    All errors are caught and surfaced in ``errors`` so the UI never
    breaks if a path is missing or a regex misfires.
    """
    diagnostics: dict[str, Any] = {
        "readiness_flags": [],
        "failed_clusters": [],
        "raw_delivery_blocked": None,
        "errors": [],
    }

    # 1. Readiness flags from delivery_blocked.md.
    blocked_path = project_path / ".lattice" / "delivery_blocked.md"
    if blocked_path.exists():
        try:
            blob = blocked_path.read_text(encoding="utf-8")
            diagnostics["raw_delivery_blocked"] = blob
            # Parse a typical entry:
            #   - readiness.cluster_not_rendered: 1 flag(s)
            #       Cluster c.g.1 is in state failed and cannot be delivered.
            #       -> Re-run rendering for this cluster, or add evidence...
            import re as _re
            entry_re = _re.compile(
                r"^- (readiness\.[\w_]+): (\d+) flag\(s\)\s*$",
                _re.MULTILINE,
            )
            lines = blob.splitlines()
            for i, line in enumerate(lines):
                m = entry_re.match(line)
                if not m:
                    continue
                category = m.group(1)
                count = int(m.group(2))
                # Continuation lines are indented; collect until next - or
                # blank section divider.
                msg_parts: list[str] = []
                fix_parts: list[str] = []
                for cont in lines[i + 1:]:
                    if cont.startswith("- ") or cont.startswith("## "):
                        break
                    stripped = cont.strip()
                    if stripped.startswith("->"):
                        fix_parts.append(stripped[2:].strip())
                    elif stripped:
                        msg_parts.append(stripped)
                diagnostics["readiness_flags"].append({
                    "category": category,
                    "count": count,
                    "message": " ".join(msg_parts).strip(),
                    "fix": " ".join(fix_parts).strip(),
                })
        except Exception as exc:  # noqa: BLE001
            diagnostics["errors"].append(
                f"could not parse delivery_blocked.md: {type(exc).__name__}: {exc}"
            )

    # 2. Per-cluster CLUSTER_UNRENDERABLE markers from prose files.
    drafts_dir = project_path / ".lattice" / "drafts" / voice_name
    if drafts_dir.exists():
        try:
            import re as _re
            marker_re = _re.compile(
                r'\{CLUSTER_UNRENDERABLE:\s*'
                r'cluster_id="([^"]+)"\s*,\s*'
                r'reason="([^"]+)"',
            )
            for prose_file in sorted(drafts_dir.glob("cluster_*.md")):
                try:
                    text = prose_file.read_text(encoding="utf-8")
                except OSError as exc:
                    diagnostics["errors"].append(
                        f"could not read {prose_file.name}: {exc}"
                    )
                    continue
                m = marker_re.search(text)
                if m:
                    diagnostics["failed_clusters"].append({
                        "cluster_id": m.group(1),
                        "reason": m.group(2),
                        "prose_file": prose_file.name,
                    })
        except Exception as exc:  # noqa: BLE001
            diagnostics["errors"].append(
                f"could not scan drafts directory: {type(exc).__name__}: {exc}"
            )

    return diagnostics


DEFAULT_CATEGORY = "Uncategorised"


async def list_projects(root: Path) -> list[dict[str, Any]]:
    """Return a list of project descriptors for every lattice project
    under ``root``. A project is identified by a populated ``.lattice/``
    directory.

    A project is listed if EITHER it has been ingested
    (``.lattice/author_graph.json`` exists) OR it has been scaffolded
    via the web UI (``.lattice/project_meta.json`` exists). The latter
    case lets freshly-created projects show up before an outline has
    been parsed.

    Each entry includes ``category`` and ``position`` so the frontend
    can group cards into rows and preserve a user-defined order. The
    list is returned sorted by (category, position, display_name) so
    fresh projects without explicit positions still get a stable order.
    """
    import json as _json
    projects: list[dict[str, Any]] = []
    if not root.exists():
        return projects
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        # Skip the .trash/ folder we use for soft-deletes.
        if child.name == ".trash":
            continue
        lattice_dir = child / ".lattice"
        graph_path = lattice_dir / "author_graph.json"
        meta_path = lattice_dir / "project_meta.json"
        if not graph_path.exists() and not meta_path.exists():
            continue

        display_name = child.name
        category = DEFAULT_CATEGORY
        position: float = 0.0
        if meta_path.exists():
            try:
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                display_name = meta.get("display_name") or display_name
                category = meta.get("category") or DEFAULT_CATEGORY
                if isinstance(meta.get("position"), (int, float)):
                    position = float(meta["position"])
            except _json.JSONDecodeError:
                pass
        elif graph_path.exists():
            try:
                graph = _json.loads(graph_path.read_text(encoding="utf-8"))
                display_name = graph.get("project_name") or display_name
            except _json.JSONDecodeError:
                pass

        paper_path = child / "outputs" / "paper.academic.md"
        last_render = None
        paper_words = 0
        if paper_path.exists():
            stat = paper_path.stat()
            last_render = stat.st_mtime
            paper_words = len(paper_path.read_text(encoding="utf-8").split())
        projects.append({
            "name": child.name,
            "display_name": display_name,
            "path": str(child.resolve()),
            "last_render": last_render,
            "paper_words": paper_words,
            "category": category,
            "position": position,
        })

    projects.sort(
        key=lambda p: (p["category"].lower(), p["position"], p["display_name"].lower())
    )
    return projects
