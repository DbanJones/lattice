"""Pipeline orchestration for `lattice run`.

End-to-end default flow:
  annotate (if needed) -> ingest -> index -> enrich -> plan -> render
  -> audit -> auto-fix loop -> final DOCX with unresolved flags as comments

The auto-fix loop runs up to `max_passes` iterations. Each iteration:
  1. Accept every mechanical flag (voice / sentence / quantification / paragraph).
  2. Propose surgical edits via the LLM.
  3. Auto-accept any proposal with confidence == high.
  4. Apply accepted proposals to the cluster prose files.
  5. Re-audit.

The loop exits early when the flag count drops by fewer than `min_delta`
flags between passes OR when only coverage / examiner flags remain.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from rich.console import Console

from ..auditor.readiness import DocumentReadinessCheck
from ..auditor.runner import AuditRunner
from ..auditor.voice_review import review_document as voice_review_document
from ..differ.diff import Differ
from ..editor.applier import EditApplier
from ..editor.proposer import EditProposer
from ..enricher.binder import Enricher
from ..enricher.report import EnrichmentReporter
from ..graph.models import AuthorGraph, FlagCategory
from ..graph.serialize_outline import write_annotated_outline
from ..graph.store import GraphStore
from ..indexer.base import SourceIndexer
from ..ingester.annotator import ContextualAnnotator
from ..ingester.docx import DOCXOutlineIngester
from ..ingester.markdown import MarkdownOutlineIngester
from ..output.docx_with_comments import write_paper_with_flags
from ..renderer.assembler import Assembler
from ..renderer.assembler_finalise import DocumentFinaliser
from ..renderer.chunked_renderer import ChunkedRenderer
from ..renderer.cluster_renderer import ClusterRenderer
from ..renderer.parallel import ParallelRenderer
from ..shadow import ShadowMapper
from ..utils.config import Config
from ..utils.llm import ClaudeClient, claude_available
from ..utils.resume import ResumeManager, RunState, Stage, StageStatus
from ..voice.parser import Voice


_MECHANICAL_CATEGORIES = {
    FlagCategory.voice,
    FlagCategory.sentence,
    FlagCategory.quantification,
    FlagCategory.paragraph,
    FlagCategory.formality,
    # Citation-engagement flags (Graff & Birkenstein) default to
    # suggest_changes — the proposer can rewrite the surrounding
    # sentence to add the missing element. Coverage / architecture
    # flags require author judgment and stay manual.
    FlagCategory.citation,
}

_AUTHOR_FACING_CATEGORIES = {
    FlagCategory.coverage,
    FlagCategory.architecture,
    FlagCategory.skim_target,
    FlagCategory.examiner,
}


class PipelineRunner:
    def __init__(
        self,
        project_path: Path,
        voice: str,
        *,
        with_shadow: bool = False,
        review: bool = False,
        max_passes: int = 3,
        min_delta: int = 5,
        console: Console | None = None,
    ) -> None:
        self.project_path = Path(project_path)
        self.voice_name = voice
        self.with_shadow = with_shadow
        self.review = review
        self.max_passes = max_passes
        self.min_delta = min_delta
        self.console = console or Console()
        self.resume_manager = ResumeManager(self.project_path)

    async def run_full(self, resume: bool = False) -> RunState:
        state = (
            self.resume_manager.latest_run() if resume else None
        ) or self.resume_manager.start_run(voice=self.voice_name)
        # If resuming, clear in-flight states so they retry.
        if resume:
            for s, status in list(state.stage_status.items()):
                if status in (StageStatus.running, StageStatus.interrupted):
                    state.stage_status[s] = StageStatus.pending

        config = Config.load(self.project_path)
        store = GraphStore.load(self.project_path)
        voice_obj = _load_voice(self.project_path, self.voice_name)

        llm = None
        if claude_available():
            try:
                llm = ClaudeClient(
                    default_model=config.default_model,
                    parallel=config.parallel_renders,
                )
            except Exception:
                llm = None

        # Stage 0: annotate (writes structure/outline.annotated.md) — does not
        # appear in ResumeManager stages, but runs before ingest if needed.
        await self._stage_annotate(config, store, llm)
        if self.review:
            self.console.print(
                "[yellow]Review pause: inspect structure/outline.annotated.md, "
                "then press Enter to continue (Ctrl-C to stop).[/yellow]"
            )
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                raise

        stages: list[tuple[Stage, Callable[[], Awaitable[None]]]] = [
            (Stage.ingest, lambda: self._stage_ingest(config, store)),
            (Stage.index, lambda: self._stage_index(config, store)),
        ]
        if llm is not None:
            stages.append((Stage.enrich, lambda: self._stage_enrich(config, store, llm)))
        if self.with_shadow and llm is not None:
            stages.append((Stage.shadow, lambda: self._stage_shadow(config, store, llm)))
            stages.append((Stage.differ, lambda: self._stage_differ(store)))
        stages.append((Stage.plan, lambda: self._stage_plan(config, store, voice_obj, llm)))
        if llm is not None:
            stages.append((Stage.render, lambda: self._stage_render(config, store, voice_obj, llm)))

        # Stop the stage loop before audit so we can run the readiness gate.
        # Audit only runs once readiness passes; otherwise the pipeline halts.
        for stage, run_fn in stages:
            if resume and state.stage_status.get(stage) == StageStatus.completed:
                self.console.print(f"[dim]skip {stage.value} (already completed)[/dim]")
                continue
            self.console.print(f"[cyan]-> {stage.value}[/cyan]")
            self.resume_manager.update_stage(state.run_id, stage, StageStatus.running)
            try:
                await run_fn()
            except Exception as exc:
                self.resume_manager.update_stage(state.run_id, stage, StageStatus.failed)
                state.error = f"{stage.value}: {type(exc).__name__}: {exc}"
                self.resume_manager._write(state)
                self.console.print(f"[red]stage {stage.value} failed: {exc}[/red]")
                raise
            self.resume_manager.update_stage(state.run_id, stage, StageStatus.completed)
            state.stage_status[stage] = StageStatus.completed

            # ── Hard gate: enrichment coverage check between enrich and plan ──
            # Block render if any claim is unbound without a resolution decision.
            if stage == Stage.enrich:
                reporter = EnrichmentReporter(store, self.project_path)
                report = reporter.generate_report()
                reporter.save_report(report)
                if not report.can_proceed_to_render:
                    pending = (
                        len([r for r in report.unbound if r.resolution.value == "pending"])
                        + len([r for r in report.contradictory if r.resolution.value == "pending"])
                    )
                    self.console.print(
                        f"[red]Pipeline halted at coverage gate: "
                        f"{pending} unbound claim(s) need author decisions.[/red]"
                    )
                    self.console.print(
                        f"Run [bold]lattice coverage {self.project_path}[/bold] "
                        "to resolve, then re-run."
                    )
                    return state

        # ── Hard gate 1: readiness check after render, before audit ──
        readiness = DocumentReadinessCheck(store, voice_obj, self.project_path).check()
        if not readiness.is_ready:
            self._write_blocked(readiness)
            self.console.print(
                f"[red]Pipeline halted at readiness check: "
                f"{len(readiness.blocking_flags)} blocking issue(s).[/red]"
            )
            self.console.print(
                f"See {self.project_path / '.lattice' / 'delivery_blocked.md'}"
            )
            return state

        # Audit + auto-fix loop only run once readiness passes.
        self.console.print("[cyan]-> audit[/cyan]")
        self.resume_manager.update_stage(state.run_id, Stage.audit, StageStatus.running)
        await self._stage_audit(config, store, voice_obj, llm=llm)
        self.resume_manager.update_stage(state.run_id, Stage.audit, StageStatus.completed)
        state.stage_status[Stage.audit] = StageStatus.completed

        if llm is not None:
            await self._auto_fix_loop(config, store, voice_obj, llm)

        # ── Hard gate 2: refuse delivery if critical flags remain ──
        # (DocumentFinaliser also checks this, but surface it explicitly.)
        unresolved_critical = [
            f for f in store.list_audit_flags(voice_obj.name)
            if f.severity.value == "critical" and f.decision is None
        ]
        if unresolved_critical:
            self.console.print(
                f"[yellow]{len(unresolved_critical)} unresolved critical flag(s); "
                f"delivery blocked. Run `lattice flags` to review.[/yellow]"
            )

        # Finalise: DocumentFinaliser checks readiness + critical flags itself.
        # If both pass, it writes outputs/paper.<voice>.md. Otherwise it
        # writes .lattice/delivery_blocked.md and returns None.
        finaliser = DocumentFinaliser(self.project_path, store, voice_obj)
        paper_path = finaliser.finalise()

        if paper_path is None:
            self.console.print(
                f"[yellow]Finalise refused to write outputs/. See "
                f"{self.project_path / '.lattice' / 'delivery_blocked.md'}[/yellow]"
            )
            return state

        # On success, also produce the DOCX with Word comments and run the
        # document-level voice review.
        self._write_final_docx(store, voice_obj)

        if paper_path.exists() and paper_path.stat().st_size > 100:
            try:
                report, review_path = voice_review_document(
                    self.project_path, store, voice_obj
                )
                if review_path is not None:
                    self.console.print(
                        f"[cyan]voice review:[/cyan] {report.overall} "
                        f"({report.pass_count} pass, {report.warning_count} warning, "
                        f"{report.fail_count} fail) -> {review_path.name}"
                    )
            except Exception as exc:
                self.console.print(
                    f"[yellow]voice review failed: {exc}[/yellow]"
                )

        return state

    def _write_blocked(self, readiness) -> None:
        """Persist the readiness summary so the author can read it."""
        path = self.project_path / ".lattice" / "delivery_blocked.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Delivery blocked at readiness check\n\n" + readiness.summary,
            encoding="utf-8",
        )

    # ─── stages ──────────────────────────────────────

    async def _stage_annotate(self, config: Config, store: GraphStore, llm) -> None:
        """Write structure/outline.annotated.md if missing / stale."""
        structure_dir = self.project_path / "structure"
        annotated = structure_dir / "outline.annotated.md"
        raw_candidates = sorted(
            [
                p for p in structure_dir.glob("*")
                if p.is_file()
                and p.suffix.lower() in (".docx", ".md")
                and p.name != "outline.annotated.md"
                and not p.name.startswith("~$")
                and not p.name.endswith(".original.docx")
            ],
            key=lambda p: p.name,
        )
        if not raw_candidates:
            return
        raw = raw_candidates[0]

        annotated_is_fresh = (
            annotated.exists()
            and annotated.stat().st_mtime >= raw.stat().st_mtime
        )
        if annotated_is_fresh:
            self.console.print(
                f"[dim]annotate: {annotated.name} already fresh, skipping[/dim]"
            )
            return

        self.console.print(f"[cyan]-> annotate[/cyan]  ({raw.name} -> {annotated.name})")

        ingester: object = (
            DOCXOutlineIngester(config) if raw.suffix.lower() == ".docx"
            else MarkdownOutlineIngester(config)
        )
        graph = await ingester.ingest(raw, project_name=self.project_path.name)

        known_sources = {s.source_id for s in store.list_sources()}
        annotator = ContextualAnnotator(config, llm)
        graph = await annotator.annotate(graph, known_source_ids=known_sources)

        write_annotated_outline(graph, self.project_path)
        # Persist the annotated graph so downstream stages and `lattice graph`
        # see the inferred relationships even before the ingest stage runs.
        store.save_graph(graph)

    async def _stage_ingest(self, config: Config, store: GraphStore) -> None:
        structure_dir = self.project_path / "structure"
        annotated = structure_dir / "outline.annotated.md"
        if annotated.exists():
            structure_file = annotated
        else:
            candidates = sorted(
                [
                    p for p in structure_dir.glob("*")
                    if p.is_file()
                    and p.suffix.lower() in (".md", ".docx")
                    and not p.name.startswith("~$")
                    and not p.name.endswith(".original.docx")
                ],
                key=lambda p: p.name,
            )
            if not candidates:
                raise FileNotFoundError(f"No outline in {structure_dir}")
            structure_file = candidates[0]

        if structure_file.suffix.lower() == ".docx":
            ingester: object = DOCXOutlineIngester(config)
        else:
            ingester = MarkdownOutlineIngester(config)
        graph = await ingester.ingest(
            structure_file, project_name=self.project_path.name
        )
        store.save_graph(graph)

    async def _stage_index(self, config: Config, store: GraphStore) -> None:
        indexer = SourceIndexer(self.project_path)
        sources, _skipped = indexer.index_all()
        for src in sources:
            store.save_source(src)

    async def _stage_enrich(self, config: Config, store: GraphStore, llm) -> None:
        await Enricher(config, store, llm).enrich_all()

    async def _stage_shadow(self, config: Config, store: GraphStore, llm) -> None:
        graph = store.get_graph()
        sources = store.list_sources()
        shadow_graph = await ShadowMapper(config, llm).build(
            sources, thesis=graph.thesis_statement or ""
        )
        (self.project_path / ".lattice" / "shadow_graph.json").write_text(
            shadow_graph.model_dump_json(indent=2), encoding="utf-8"
        )

    async def _stage_differ(self, store: GraphStore) -> None:
        graph = store.get_graph()
        shadow_path = self.project_path / ".lattice" / "shadow_graph.json"
        if not shadow_path.exists():
            return
        shadow = AuthorGraph.model_validate_json(shadow_path.read_text(encoding="utf-8"))
        differ = Differ(self.project_path)
        diffs = differ.diff(graph, shadow, sources=store.list_sources())
        differ.write_report(diffs)

    async def _stage_plan(
        self, config: Config, store: GraphStore, voice_obj: Voice, llm
    ) -> None:
        await Assembler(config, store, llm, voice_obj).build_plan()

    async def _stage_render(
        self, config: Config, store: GraphStore, voice_obj: Voice, llm
    ) -> None:
        # Chunked rendering is the default: one LLM call per chunk of 8-20
        # clusters, so Claude sees full argument context and can do
        # cross-cluster callbacks. Each cluster's prose is parsed back out
        # for downstream audit / edit workflows.
        renderer = ChunkedRenderer(config, store, llm, voice_obj)
        await renderer.render_all()
        # finalise() runs only at the end of run_full, gated by readiness +
        # critical-flag checks. Don't call it here.

    async def _stage_audit(
        self, config: Config, store: GraphStore, voice_obj: Voice, llm=None
    ) -> None:
        # The auditor's per-cluster CitationCheck is LLM-bound; pass the
        # client through so the Graff & Birkenstein engagement check runs
        # alongside the deterministic checks.
        await AuditRunner(config, store, llm=llm, voice=voice_obj).run()

    # ─── auto-fix loop ───────────────────────────────

    async def _auto_fix_loop(
        self, config: Config, store: GraphStore, voice_obj: Voice, llm
    ) -> None:
        previous_count = len(store.list_audit_flags(voice_obj.name))
        self.console.print(
            f"[cyan]-> auto-fix loop[/cyan] ({previous_count} flags; "
            f"max_passes={self.max_passes}, min_delta={self.min_delta})"
        )
        for pass_num in range(1, self.max_passes + 1):
            flags = store.list_audit_flags(voice_obj.name)
            mechanical = [
                f for f in flags
                if f.category in _MECHANICAL_CATEGORIES and not f.decision
            ]
            if not mechanical:
                self.console.print(
                    f"[dim]  pass {pass_num}: no mechanical flags remain; stop[/dim]"
                )
                break

            # 1. Accept every mechanical flag.
            for flag in mechanical:
                decision = (
                    "accept_rewrite"
                    if flag.default_mode.value == "rewrite"
                    else "accept_suggest_changes"
                )
                store.update_flag_decision(flag.flag_id, decision)

            # 2. Propose.
            proposer = EditProposer(config, store, llm, voice_obj)
            grouped = await proposer.propose_for_accepted_flags()
            proposal_count = sum(len(v) for v in grouped.values())

            # 3. Auto-accept only confidence=high proposals.
            pending = [p for p in store.list_edit_proposals() if p.status.value == "pending"]
            accepted = 0
            for proposal in pending:
                if proposal.confidence.value == "high":
                    store.update_proposal_decision(proposal.proposal_id, "accepted")
                    accepted += 1

            # 4. Apply.
            applier = EditApplier(self.project_path, store, voice_name=voice_obj.name)
            applied, skipped = applier.apply_all_accepted()

            # 5. Re-render (cached prose is skipped; applier changed only existing files).
            # Then re-audit.
            await self._stage_render(config, store, voice_obj, llm)
            await self._stage_audit(config, store, voice_obj)

            new_count = len(store.list_audit_flags(voice_obj.name))
            delta = previous_count - new_count
            self.console.print(
                f"[dim]  pass {pass_num}: mechanical={len(mechanical)}, "
                f"proposals={proposal_count}, high_conf={accepted}, applied={applied}, "
                f"skipped={skipped}, flags: {previous_count}->{new_count} (-{delta})[/dim]"
            )
            previous_count = new_count

            if delta < self.min_delta:
                self.console.print(
                    f"[dim]  stopping: delta ({delta}) below min_delta ({self.min_delta})[/dim]"
                )
                break

    # ─── final DOCX export ──────────────────────────

    def _write_final_docx(self, store: GraphStore, voice_obj: Voice) -> None:
        md_path = self.project_path / "outputs" / f"paper.{voice_obj.name}.md"
        if not md_path.exists():
            return
        flags = store.list_audit_flags(voice_obj.name)
        unresolved = [f for f in flags if not f.decision]
        docx_path = self.project_path / "outputs" / f"paper.{voice_obj.name}.docx"
        _, attached = write_paper_with_flags(
            md_path.read_text(encoding="utf-8"),
            unresolved,
            docx_path,
        )

        # Plain-text unresolved-flags report for quick scanning.
        report = self.project_path / "outputs" / f"unresolved_flags.{voice_obj.name}.md"
        _write_flag_report(unresolved, report)

        self.console.print(
            f"[green]wrote {docx_path.name} "
            f"({attached}/{len(unresolved)} unresolved flags as comments) + "
            f"{report.name}[/green]"
        )


# ─── helpers ───────────────────────────────────────

def _load_voice(project: Path, voice_name: str) -> Voice:
    voice_path = project / "voices" / f"{voice_name}.voice.md"
    if not voice_path.exists():
        raise FileNotFoundError(f"Voice not found: {voice_path}")
    return Voice.from_file(voice_path)


def _write_flag_report(unresolved, path: Path) -> None:
    from collections import defaultdict
    by_cat = defaultdict(list)
    for f in unresolved:
        by_cat[f.category.value].append(f)

    lines = [
        "# Unresolved flags",
        "",
        f"Total: **{len(unresolved)}** flags remaining for author review.",
        "",
        "These are the structural issues the auto-fix loop could not resolve "
        "without author input. Coverage flags mean prose that doesn't trace to "
        "a claim in your scaffold — either add the missing claim or accept the "
        "orphan sentence as-is.",
        "",
    ]
    for cat in ("coverage", "architecture", "skim_target", "examiner",
                "sentence", "quantification", "paragraph", "voice", "formality"):
        flags = by_cat.get(cat, [])
        if not flags:
            continue
        lines.append(f"## {cat} ({len(flags)})")
        lines.append("")
        for f in flags:
            lines.append(f"- **{f.rule_id}** · cluster `{f.cluster_id}` · {f.severity.value}")
            snippet = (f.offending_text or "").replace("\n", " ")[:140]
            lines.append(f"  - offending: `{snippet}`")
            if f.suggestion:
                lines.append(f"  - suggestion: {f.suggestion}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
