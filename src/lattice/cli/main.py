"""Lattice CLI entry point.

See docs/CLI.md for the full command reference.
See docs/HANDOFF.md step 4 for the build order.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows: force UTF-8 stdout/stderr so Rich progress glyphs (→, ┌, etc.)
# don't crash the legacy Windows console renderer with cp1252
# UnicodeEncodeError. No-op on POSIX. Must run before any Rich Console
# is constructed.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

import typer
from rich.console import Console
from rich.table import Table

import asyncio

from ..auditor.consistency import VoiceConsistencyCheck
from ..auditor.runner import AuditRunner
from ..auditor.voice_review import review_document as voice_review_document
from ..differ.diff import Differ
from ..editor.applier import EditApplier
from ..editor.proposer import EditProposer
from ..enricher.binder import Enricher
from ..enricher.report import EnrichmentReporter
from ..tui.coverage_review import CoverageReviewTUI
from ..graph.export_argus import export_to_argus
from ..graph.models import AuthorGraph
from ..graph.store import GraphStore
from ..indexer.base import SourceIndexer
from ..ingester.annotator import ContextualAnnotator
from ..ingester.docx import DOCXOutlineIngester
from ..ingester.markdown import MarkdownOutlineIngester
from ..renderer.assembler import Assembler
from ..renderer.assembler_finalise import DocumentFinaliser
from ..renderer.chunked_renderer import ChunkedRenderer
from ..renderer.cluster_renderer import ClusterRenderer
from ..output.visualise import render_tree, write_html, write_mermaid
from ..renderer.parallel import ParallelRenderer
from ..shadow import ShadowMapper
from ..utils.config import Config
from ..utils.errors import (
    LatticeError,
    err_claude_unavailable,
    err_project_not_found,
    err_unknown_voice,
)
from ..utils.llm import ClaudeClient, claude_available
from ..utils.resume import ResumeManager, StageStatus
from ..voice.parser import Voice
from .run import PipelineRunner


def _surface_lattice_error(err: LatticeError) -> typer.Exit:
    """Render a LatticeError to the console and return a typer.Exit.

    Two-line shape:
        [red]error: message[/red]
        [yellow]→ next_step[/yellow]
        [dim]docs: docs_link[/dim]   (only when present)

    Call sites use ``raise _surface_lattice_error(err)`` so the exit
    code propagates through typer.
    """
    console.print(f"[red]error[/red]: {err.message}")
    console.print(f"[yellow]→[/yellow] {err.next_step}")
    if err.docs_link:
        console.print(f"[dim]docs: {err.docs_link}[/dim]")
    return typer.Exit(code=err.exit_code)


def _load_voice(project: Path, voice_name: str) -> Voice:
    voice_path = project / "voices" / f"{voice_name}.voice.md"
    if not voice_path.exists():
        raise _surface_lattice_error(err_unknown_voice(voice_name, str(voice_path)))
    return Voice.from_file(voice_path)


def _require_project(project: Path) -> Path:
    project = project.resolve()
    if not project.exists():
        raise _surface_lattice_error(err_project_not_found(str(project)))
    return project


def _require_claude() -> None:
    if not claude_available():
        raise _surface_lattice_error(err_claude_unavailable())

app = typer.Typer(
    name="lattice",
    help="Argument-first long-form writing tool. See docs/SPEC.md.",
    no_args_is_help=True,
)

console = Console()


# ─────────────────────────────────────────────────────────
# Helpers for asset discovery
# ─────────────────────────────────────────────────────────


def _package_examples_dir() -> Path | None:
    """Locate the lattice repo's examples/ directory for template discovery.

    Works when installed editable from the repo. Returns None otherwise.
    """
    # src/lattice/cli/main.py -> parents[3] is the repo root.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "examples"
        if (candidate / "voices" / "academic.voice.md").exists():
            return candidate
    return None


_DEFAULT_OUTLINE = """# THESIS
[TODO: state your thesis in one sentence.]

# A. [TODO: first section heading]
  - [TODO: first bullet] [ref: source_id]
  - MY VIEW: [TODO: your synthesis claim] [user_synthesis]
"""

_DEFAULT_CONFIG = """# Lattice project configuration.
# See docs/CLI.md for full reference.
#
# Models are passed through to `claude --model <value>`. Use Claude Code
# aliases ('sonnet', 'opus', 'haiku') or full model IDs.

default_voice: academic

default_model: sonnet

model_per_stage:
  ingester: sonnet
  enricher: sonnet
  shadow_extractor: sonnet
  shadow_architect: sonnet
  renderer: sonnet
  auditor: sonnet
  examiner: opus
  edit_proposer: sonnet

# Lower than the old HTTP default — each call is a subprocess to `claude`.
parallel_renders: 4
cache_dir: .lattice/cache
output_dir: outputs
"""

_DEFAULT_GITIGNORE = """# Lattice state
.lattice/
outputs/

# Env
.env
"""


# ─────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────


@app.command()
def init(
    project: Path = typer.Argument(..., help="Path to the new project folder."),
) -> None:
    """Scaffold a new Lattice project with default folders and config."""
    project = project.resolve()
    if project.exists() and any(project.iterdir()):
        console.print(f"[yellow]{project} already exists and is not empty. Aborting.[/yellow]")
        raise typer.Exit(code=3)

    for sub in [
        "structure",
        "refs/papers",
        "refs/notes",
        "refs/data",
        "refs/prior_writing",
        "refs/web",
        "voices",
        "figures",
    ]:
        (project / sub).mkdir(parents=True, exist_ok=True)

    (project / "config.yml").write_text(_DEFAULT_CONFIG, encoding="utf-8")
    (project / ".gitignore").write_text(_DEFAULT_GITIGNORE, encoding="utf-8")
    (project / "structure" / "outline.md").write_text(_DEFAULT_OUTLINE, encoding="utf-8")

    examples = _package_examples_dir()
    if examples is not None:
        shutil.copy2(examples / "voices" / "academic.voice.md", project / "voices" / "academic.voice.md")
    else:
        # Minimal fallback — signal to the user they need to author a voice.
        (project / "voices" / "academic.voice.md").write_text(
            "# TODO: populate academic.voice.md from examples/voices/academic.voice.md\n",
            encoding="utf-8",
        )

    GraphStore.load(project)  # creates .lattice/

    console.print(f"[green]Initialised project at {project}[/green]")
    console.print("Next: drop references into refs/ and run [bold]lattice index[/bold].")


@app.command()
def status(
    project: Path = typer.Argument(..., help="Path to project."),
) -> None:
    """Show current project state: indexed sources, claims, last runs, pending flags."""
    project = project.resolve()
    if not project.exists():
        console.print(f"[red]Project not found: {project}[/red]")
        raise typer.Exit(code=3)

    store = GraphStore.load(project)
    graph = store.get_graph()
    sources = store.list_sources()

    def _mtime(path: Path) -> str:
        if not path.exists():
            return "—"
        dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")

    table = Table(title=f"Lattice project: {project.name}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value")

    table.add_row("Indexed sources", str(len(sources)))
    table.add_row("Claims", str(len(graph.claims)))
    table.add_row("Sections", str(len(graph.sections)))
    table.add_row("Relationships", str(len(graph.relationships)))
    table.add_row("Clusters", str(len(store.list_clusters())))
    table.add_row("Last author_graph update", _mtime(store.author_graph_path))
    table.add_row("Last source_store update", _mtime(store.source_store_path))
    table.add_row("Last cluster_plan update", _mtime(store.cluster_plan_path))
    table.add_row("Last audit_flags update", _mtime(store.audit_flags_path))

    # Pending flag summary by voice
    pending_by_voice: dict[str, int] = {}
    if store.audit_flags_path.exists():
        data = json.loads(store.audit_flags_path.read_text(encoding="utf-8"))
        for voice_name, flags in data.items():
            pending = sum(1 for f in flags if not f.get("decision"))
            if pending:
                pending_by_voice[voice_name] = pending
    if pending_by_voice:
        for voice_name, count in pending_by_voice.items():
            table.add_row(f"Pending flags ({voice_name})", str(count))
    else:
        table.add_row("Pending flags", "0")

    console.print(table)


@app.command()
def ingest(project: Path = typer.Argument(...)) -> None:
    """Rebuild author_graph from structure/. Auto-detects format by extension."""
    project = project.resolve()
    if not project.exists():
        console.print(f"[red]Project not found: {project}[/red]")
        raise typer.Exit(code=3)

    structure_dir = project / "structure"
    annotated = structure_dir / "outline.annotated.md"
    if annotated.exists():
        structure_file = annotated
    else:
        candidates = (
            list(structure_dir.glob("*.md"))
            + list(structure_dir.glob("*.docx"))
            + list(structure_dir.glob("*.argus.json"))
        )
        candidates = [
            p for p in candidates
            if p.is_file()
            and not p.name.startswith("~$")
            and not p.name.endswith(".original.docx")
        ]
        if not candidates:
            console.print(f"[red]No structure file found in {structure_dir}/[/red]")
            raise typer.Exit(code=3)
        structure_file = candidates[0]

    config = Config.load(project)
    store = GraphStore.load(project)

    suffix = structure_file.suffix.lower()
    if suffix == ".json":
        console.print("[yellow]Argus ingester not yet implemented.[/yellow]")
        raise typer.Exit(code=3)
    if suffix == ".docx":
        ingester: object = DOCXOutlineIngester(config)
    else:
        ingester = MarkdownOutlineIngester(config)
    graph = asyncio.run(ingester.ingest(structure_file, project_name=project.name))

    # Contextual annotation: thesis extraction + section-role classification
    # + claim-role inference + inline-citation detection. Uses Claude CLI if
    # available; falls back to deterministic citation regex only if not.
    llm = None
    if claude_available():
        try:
            llm = ClaudeClient(default_model=config.default_model, parallel=config.parallel_renders)
        except Exception:
            llm = None
    if llm is not None:
        console.print("[dim]Annotating (thesis, section roles, claim roles, citations)...[/dim]")
    else:
        console.print("[dim]Annotating (inline citations only; install Claude for full annotation)...[/dim]")

    known_sources = {s.source_id for s in store.list_sources()}

    # Persist the scaffold report from the markdown ingester before the
    # annotator runs, so the author can see exactly what the deterministic
    # parser saw vs what was inferred. The DOCX ingester delegates to the
    # markdown ingester internally, so it will also have ``last_report``
    # populated when available.
    if hasattr(ingester, "save_scaffold_report"):
        ingester.save_scaffold_report(project, known_source_ids=known_sources)

    # Phase 4: scaffold audit — surface structural issues (empty
    # sections, orphan claims, missing evidence signals, dangling
    # relationships, missing conclusion, disconnected thesis) before
    # the renderer wastes work on a broken graph.
    from ..auditor.scaffold import audit_scaffold
    scaffold_report = audit_scaffold(graph)
    if scaffold_report.findings:
        report_path = project / ".lattice" / "scaffold_audit.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        # Serialise as a plain JSON list — these are dataclasses, not
        # pydantic models, so go via dict.
        import json as _json
        from dataclasses import asdict as _asdict
        report_path.write_text(
            _json.dumps(
                [_asdict(f) for f in scaffold_report.findings], indent=2
            ),
            encoding="utf-8",
        )
        if scaffold_report.error_count:
            console.print(
                f"[yellow]scaffold audit: {scaffold_report.error_count} "
                f"error(s), {scaffold_report.warning_count} warning(s) "
                f"— see .lattice/scaffold_audit.json[/yellow]"
            )
        elif scaffold_report.warning_count:
            console.print(
                f"[dim]scaffold audit: {scaffold_report.warning_count} "
                f"warning(s) — see .lattice/scaffold_audit.json[/dim]"
            )

    annotator = ContextualAnnotator(config, llm)
    graph = asyncio.run(annotator.annotate(graph, known_source_ids=known_sources))

    store.save_graph(graph)

    # Post-annotation summary so the user sees what was inferred.
    references_sections = [s for s in graph.sections if s.role.value == "references"]
    with_evidence = sum(1 for c in graph.claims if c.evidence)
    with_roles = sum(1 for c in graph.claims if any(t.startswith("role:") for t in c.tags))
    user_synth = sum(1 for c in graph.claims if c.type.value == "user_synthesis")

    console.print(
        f"[green]Ingested {len(graph.sections)} sections, "
        f"{len(graph.claims)} claims, "
        f"{len(graph.relationships)} relationships.[/green]"
    )
    console.print(
        f"  thesis: {(graph.thesis_statement or '(none)')[:120]}"
    )
    console.print(
        f"  annotations — references sections skipped: {len(references_sections)}, "
        f"claims with evidence: {with_evidence}, "
        f"claims with roles: {with_roles}, "
        f"user_synthesis: {user_synth}"
    )


@app.command()
def index(
    project: Path = typer.Argument(...),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Rebuild source_store from refs/. Skips files whose SHA256 is unchanged."""
    project = project.resolve()
    if not project.exists():
        console.print(f"[red]Project not found: {project}[/red]")
        raise typer.Exit(code=3)

    Config.load(project)  # validates config; unused locally yet
    store = GraphStore.load(project)
    indexer = SourceIndexer(project)
    sources, skipped = indexer.index_all(force=force)

    for src in sources:
        store.save_source(src)

    console.print(
        f"[green]Indexed {len(sources)} source(s), skipped {len(skipped)} unchanged.[/green]"
    )
    if sources:
        table = Table(title="Indexed")
        table.add_column("source_id", style="cyan")
        table.add_column("type")
        table.add_column("passages")
        for src in sources:
            table.add_row(src.source_id, src.type.value, str(len(src.passages)))
        console.print(table)


@app.command()
def enrich(project: Path = typer.Argument(...)) -> None:
    """Bind author claims to source passages. One LLM call per (claim, source)."""
    project = project.resolve()
    if not project.exists():
        console.print(f"[red]Project not found: {project}[/red]")
        raise typer.Exit(code=3)

    config = Config.load(project)
    store = GraphStore.load(project)
    _require_claude()

    llm = ClaudeClient(
        api_key=config.api_key,
        default_model=config.default_model,
        parallel=config.parallel_renders,
    )
    enricher = Enricher(config, store, llm)
    count = asyncio.run(enricher.enrich_all())
    console.print(f"[green]Enriched {count} claim(s).[/green]")


@app.command()
def shadow(
    project: Path = typer.Argument(...),
    blind: bool = typer.Option(False, "--blind", help="Shadow mapper ignores thesis."),
) -> None:
    """Build shadow graph and report. Requires ANTHROPIC_API_KEY."""
    project = _require_project(project)
    config = Config.load(project)
    _require_claude()
    store = GraphStore.load(project)
    graph = store.get_graph()
    sources = store.list_sources()
    if not sources:
        console.print("[red]No sources indexed. Run `lattice index` first.[/red]")
        raise typer.Exit(code=3)
    llm = ClaudeClient(
        api_key=config.api_key,
        default_model=config.default_model,
        parallel=config.parallel_renders,
    )
    thesis = "" if blind else (graph.thesis_statement or "")
    shadow_graph = asyncio.run(ShadowMapper(config, llm).build(sources, thesis))
    shadow_path = project / ".lattice" / "shadow_graph.json"
    shadow_path.write_text(shadow_graph.model_dump_json(indent=2), encoding="utf-8")

    differ = Differ(project)
    diffs = differ.diff(graph, shadow_graph, sources=sources)
    report_path = differ.write_report(diffs)
    console.print(
        f"[green]Shadow graph: {len(shadow_graph.claims)} claims across "
        f"{len(shadow_graph.sections)} clusters. {len(diffs)} flag(s) in report.[/green]"
    )
    console.print(f"Report: {report_path}")


@app.command()
def review(
    project: Path = typer.Argument(...),
    accept: str | None = typer.Option(None, "--accept"),
    reject: str | None = typer.Option(None, "--reject"),
    rationale: str = typer.Option("", "--rationale"),
) -> None:
    """List shadow diffs and apply accept/reject (non-interactive)."""
    project = _require_project(project)
    shadow_path = project / ".lattice" / "shadow_graph.json"
    if not shadow_path.exists():
        console.print("[red]No shadow graph. Run `lattice shadow` first.[/red]")
        raise typer.Exit(code=3)
    reports_dir = project / ".lattice" / "shadow_reports"
    reports = sorted(reports_dir.glob("*.md")) if reports_dir.exists() else []
    if not reports:
        console.print("[yellow]No shadow reports yet.[/yellow]")
        return

    # Decision persistence for shadow diffs is a follow-on refinement; for
    # M5 we just surface the latest report and its full contents.
    if accept or reject:
        log_path = project / ".lattice" / "shadow_decisions.json"
        import json
        existing = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
        entry = {
            "diff_id": accept or reject,
            "decision": "accept" if accept else "reject",
            "rationale": rationale,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        existing.append(entry)
        log_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        console.print(f"[green]Logged decision for {entry['diff_id']}[/green]")
        return

    console.print(f"Latest report: {reports[-1]}")
    console.print(reports[-1].read_text(encoding="utf-8"))


@app.command()
def coverage(
    project: Path = typer.Argument(...),
) -> None:
    """Review enrichment coverage. Required before render when claims are unbound.

    Walks every unbound and contradictory claim and asks the author for a
    resolution decision (mark user_synthesis, add a new source, soften, remove,
    or accept the gap). Decisions persist in `.lattice/enrichment_coverage.json`.
    """
    project = _require_project(project)
    Config.load(project)
    store = GraphStore.load(project)
    reporter = EnrichmentReporter(store, project)
    report = CoverageReviewTUI(reporter).run()
    if not report.can_proceed_to_render:
        console.print(
            "[yellow]Some claims still pending. Re-run `lattice coverage` "
            "when ready.[/yellow]"
        )
        raise typer.Exit(code=1)
    console.print("[green]Coverage review complete. Ready to render.[/green]")


@app.command()
def plan(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
) -> None:
    """Build cluster plan from working graph and voice."""
    project = _require_project(project)
    config = Config.load(project)
    store = GraphStore.load(project)
    voice_obj = _load_voice(project, voice)

    # Assembler cluster construction is deterministic — no LLM required.
    assembler = Assembler(config, store, llm=None, voice=voice_obj)
    clusters = asyncio.run(assembler.build_plan())
    # Save graph so updated section.cluster_ids persists.
    store.save_graph(store.get_graph())

    console.print(
        f"[green]Planned {len(clusters)} cluster(s) across "
        f"{len({c.section_id for c in clusters})} section(s).[/green]"
    )
    violations = getattr(assembler, "_violations", [])
    if violations:
        console.print("[yellow]Architecture violations:[/yellow]")
        for v in violations:
            console.print(f"  - {v}")


@app.command()
def render(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
    cluster: str | None = typer.Option(None, "--cluster", help="Render single cluster only (cluster mode)."),
    section: str | None = typer.Option(None, "--section", help="Render one section's clusters (cluster mode)."),
    force: bool = typer.Option(False, "--force"),
    mode: str = typer.Option(
        "chunked", "--mode",
        help="chunked (default, 4-5 clusters per LLM call) or cluster (one call per cluster)",
    ),
    chunk_min: int = typer.Option(3, "--chunk-min", help="min clusters per chunk (chunked mode)"),
    chunk_max: int = typer.Option(4, "--chunk-max", help="max clusters per chunk (chunked mode)"),
    max_passes: int = typer.Option(
        3, "--max-passes",
        help="Max autofix→re-render→finalise passes when finalise refuses. Each "
             "pass that produces no changes (or releases the document) ends the "
             "loop early. Set to 1 to disable convergence retries.",
    ),
    no_progress: bool = typer.Option(
        False, "--no-progress",
        help="Disable the live progress display.",
    ),
) -> None:
    """Produce a rendered paper.

    Default mode: ``chunked``. Groups 4-5 clusters into a single LLM call so
    Claude sees the full argument arc and can do callbacks across clusters.
    Output is parsed back into per-cluster prose files for downstream audit
    and edit workflows. The 4-5 default avoids the JSON truncation observed
    at chunk sizes >=8 with the elaboration directives applied; raise it
    via --chunk-max if your model-stage budget is higher.

    Use ``--mode cluster`` for the older one-call-per-cluster behaviour
    (slower, more fragmented prose, tighter argument-graph traceability).
    """
    project = _require_project(project)
    config = Config.load(project)
    _require_claude()
    store = GraphStore.load(project)
    voice_obj = _load_voice(project, voice)

    llm = ClaudeClient(
        api_key=config.api_key,
        default_model=config.default_model,
        parallel=config.parallel_renders,
    )

    all_clusters = store.list_clusters()
    if not all_clusters:
        console.print("[red]No cluster plan found. Run `lattice plan` first.[/red]")
        raise typer.Exit(code=3)

    from .progress import progress_or_null

    # The convergence loop wraps render + audit + autofix + re-render +
    # finalise. Pass 1 is the initial render; passes 2..max_passes only
    # run if finalise refused and autofix has work to do.
    show_progress = not no_progress and mode == "chunked"
    final_path: Path | None = None

    with progress_or_null(
        console, enabled=show_progress, total_passes=max_passes,
    ) as prog:
        prog.begin_pass(1, max_passes)

        if mode == "chunked":
            if cluster or section:
                console.print(
                    "[yellow]--cluster and --section are ignored in chunked mode. "
                    "Use --mode cluster for cluster-level targeting.[/yellow]"
                )
            renderer = ChunkedRenderer(
                config, store, llm, voice_obj,
                min_chunk=chunk_min, max_chunk=chunk_max,
            )
            results = asyncio.run(renderer.render_all(force=force, progress=prog))
            rendered = sum(
                1 for r in results.values()
                if r and "CLUSTER_UNRENDERABLE" not in r
            )
            console.print(f"[green]Rendered {rendered}/{len(results)} cluster(s) (chunked).[/green]")
        elif mode == "cluster":
            cluster_renderer = ClusterRenderer(config, store, llm, voice_obj)
            if cluster:
                cluster_ids = [cluster]
            elif section:
                cluster_ids = [c.cluster_id for c in all_clusters if c.section_id == section]
            else:
                cluster_ids = [c.cluster_id for c in all_clusters]
            parallel = ParallelRenderer(cluster_renderer, max_concurrent=config.parallel_renders)
            results = asyncio.run(parallel.render_all(cluster_ids, force=force))
            errors = {cid: r for cid, r in results.items() if isinstance(r, Exception)}
            rendered = len(results) - len(errors)
            console.print(f"[green]Rendered {rendered}/{len(results)} cluster(s) (cluster mode).[/green]")
            if errors:
                console.print("[yellow]Failures:[/yellow]")
                for cid, exc in errors.items():
                    console.print(f"  - {cid}: {type(exc).__name__}: {exc}")
        else:
            console.print(f"[red]Unknown --mode {mode!r}. Use 'chunked' or 'cluster'.[/red]")
            raise typer.Exit(code=2)

        # First finalise attempt.
        prog.begin("finalise", status="checking readiness")
        final_path = DocumentFinaliser(project, store, voice_obj).finalise()
        if final_path is not None:
            prog.end("finalise", status="document delivered")
        else:
            prog.end("finalise", status="refused — running autofix")

        # Convergence loop. Skip entirely if autocorrect=none or already
        # finalised, or in cluster mode (autofix is chunked-mode only here).
        if final_path is None and config.autocorrect != "none" and mode == "chunked":
            from ..auditor.autofix import run_autofix
            from ..auditor.runner import AuditRunner

            for pass_index in range(2, max_passes + 1):
                prog.begin_pass(pass_index, max_passes)

                # Audit the current prose.
                prog.begin("audit", total=len(store.list_clusters()),
                           status="running per-cluster checks")
                audit_runner = AuditRunner(config, store, llm=llm, voice=voice_obj)
                flags = asyncio.run(audit_runner.run())
                store = GraphStore.load(project)  # reload after persist
                prog.end("audit", status=f"{len(flags)} flag(s)")

                # Autofix.
                result = run_autofix(config, store, voice_obj, llm, progress=prog)
                for note in result.notes:
                    console.print(f"  {note}")

                # If aggressive marked clusters dirty, re-render them.
                if result.accepted_rewrite > 0 and mode == "chunked":
                    prog.begin("rerender", status="re-rendering dirty clusters")
                    renderer2 = ChunkedRenderer(
                        config, store, llm, voice_obj,
                        min_chunk=chunk_min, max_chunk=chunk_max,
                    )
                    asyncio.run(renderer2.render_all(force=False, progress=prog))
                    prog.end("rerender", status="dirty clusters refreshed")

                # Retry finalise.
                prog.begin("finalise", status=f"retry after pass {pass_index}")
                final_path = DocumentFinaliser(project, store, voice_obj).finalise()
                if final_path is not None:
                    prog.end("finalise", status=f"delivered after pass {pass_index}")
                    break
                prog.end("finalise", status="still refused")

                # Stop early if this pass produced no changes — further
                # passes would loop without effect.
                if result.total_changes == 0:
                    console.print(
                        f"[yellow]Pass {pass_index} produced no changes; "
                        f"stopping convergence loop.[/yellow]"
                    )
                    break

    if final_path is None:
        console.print(
            f"[yellow]Finalise still refused after {max_passes} pass(es). See "
            f"{project / '.lattice' / 'delivery_blocked.md'}[/yellow]"
        )
    else:
        console.print(f"[green]Final document written to {final_path}[/green]")


@app.command()
def audit(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
) -> None:
    """Run the auditor on the last render.

    Deterministic checks always run. The LLM-bound citation engagement check
    runs when the Claude CLI is available; otherwise it's skipped silently.
    """
    project = _require_project(project)
    config = Config.load(project)
    store = GraphStore.load(project)
    voice_obj = _load_voice(project, voice)
    llm = None
    if claude_available():
        try:
            llm = ClaudeClient(
                default_model=config.default_model,
                parallel=config.parallel_renders,
            )
        except Exception:
            llm = None
    runner = AuditRunner(config, store, llm=llm, voice=voice_obj)
    flags = asyncio.run(runner.run())
    from collections import Counter
    cats = Counter(f.category.value for f in flags)
    sevs = Counter(f.severity.value for f in flags)
    console.print(f"[green]Audit produced {len(flags)} flag(s).[/green]")
    if flags:
        table = Table(title="Flags by category")
        table.add_column("category", style="cyan")
        table.add_column("count", justify="right")
        for cat, n in cats.most_common():
            table.add_row(cat, str(n))
        console.print(table)
        console.print(f"Severity: " + ", ".join(f"{k}={v}" for k, v in sevs.items()))
    console.print(
        f"Report: {(project / '.lattice' / 'audit' / f'audit.{voice}.md')}"
    )


@app.command()
def flags(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
    accept: str | None = typer.Option(None, "--accept", help="Accept one flag by id (uses default mode)."),
    reject: str | None = typer.Option(None, "--reject", help="Reject one flag by id."),
    accept_all_category: str | None = typer.Option(None, "--accept-all-category"),
    reject_all_minor: bool = typer.Option(False, "--reject-all-minor"),
) -> None:
    """List flags and apply decisions (non-interactive)."""
    project = _require_project(project)
    store = GraphStore.load(project)
    all_flags = store.list_audit_flags(voice)

    # Apply decisions first.
    if accept:
        flag = next((f for f in all_flags if f.flag_id == accept), None)
        if not flag:
            console.print(f"[red]Flag not found: {accept}[/red]")
            raise typer.Exit(code=3)
        decision = (
            "accept_rewrite"
            if flag.default_mode.value == "rewrite"
            else "accept_suggest_changes"
        )
        store.update_flag_decision(accept, decision)
        console.print(f"[green]Accepted {accept} as {decision}[/green]")
        return
    if reject:
        store.update_flag_decision(reject, "reject")
        console.print(f"[green]Rejected {reject}[/green]")
        return
    if accept_all_category:
        n = 0
        for f in all_flags:
            if f.category.value == accept_all_category and not f.decision:
                decision = (
                    "accept_rewrite"
                    if f.default_mode.value == "rewrite"
                    else "accept_suggest_changes"
                )
                store.update_flag_decision(f.flag_id, decision)
                n += 1
        console.print(f"[green]Accepted {n} flag(s) in category {accept_all_category}.[/green]")
        return
    if reject_all_minor:
        n = 0
        for f in all_flags:
            if f.severity.value == "minor" and not f.decision:
                store.update_flag_decision(f.flag_id, "reject")
                n += 1
        console.print(f"[green]Rejected {n} minor flag(s).[/green]")
        return

    # Otherwise: list pending flags.
    pending = [f for f in all_flags if not f.decision]
    if not pending:
        console.print("[green]No pending flags.[/green]")
        return
    table = Table(title=f"Pending flags: {len(pending)}")
    table.add_column("flag_id", style="cyan")
    table.add_column("category")
    table.add_column("severity")
    table.add_column("rule")
    table.add_column("cluster")
    table.add_column("offending")
    for f in pending[:60]:
        table.add_row(
            f.flag_id,
            f.category.value,
            f.severity.value,
            f.rule_id,
            f.cluster_id,
            f.offending_text[:40],
        )
    console.print(table)
    if len(pending) > 60:
        console.print(f"... and {len(pending) - 60} more")


@app.command()
def propose(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
) -> None:
    """Run the edit proposer for all accept_suggest_changes flags. Requires API key."""
    project = _require_project(project)
    config = Config.load(project)
    _require_claude()
    store = GraphStore.load(project)
    voice_obj = _load_voice(project, voice)
    llm = ClaudeClient(
        api_key=config.api_key,
        default_model=config.default_model,
        parallel=config.parallel_renders,
    )
    proposer = EditProposer(config, store, llm, voice_obj)
    grouped = asyncio.run(proposer.propose_for_accepted_flags())
    total = sum(len(v) for v in grouped.values())
    console.print(f"[green]Proposed {total} edit(s) across {len(grouped)} cluster(s).[/green]")


@app.command()
def edits(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
    accept: str | None = typer.Option(None, "--accept"),
    reject: str | None = typer.Option(None, "--reject"),
) -> None:
    """List edit proposals and apply accept/reject (non-interactive)."""
    project = _require_project(project)
    store = GraphStore.load(project)

    if accept:
        store.update_proposal_decision(accept, "accepted")
        console.print(f"[green]Accepted {accept}[/green]")
        return
    if reject:
        store.update_proposal_decision(reject, "rejected")
        console.print(f"[green]Rejected {reject}[/green]")
        return

    proposals = store.list_edit_proposals()
    pending = [p for p in proposals if p.status.value == "pending"]
    if not pending:
        console.print("[green]No pending edit proposals.[/green]")
        return
    table = Table(title=f"Pending edit proposals: {len(pending)}")
    table.add_column("proposal_id", style="cyan")
    table.add_column("cluster")
    table.add_column("type")
    table.add_column("rule")
    table.add_column("original")
    table.add_column("proposed")
    for p in pending[:40]:
        table.add_row(
            p.proposal_id,
            p.cluster_id,
            p.type.value,
            p.rule_id,
            p.original_text[:40],
            p.proposed_text[:40],
        )
    console.print(table)


@app.command()
def apply(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
) -> None:
    """Apply every accepted edit proposal to its prose file."""
    project = _require_project(project)
    store = GraphStore.load(project)
    applier = EditApplier(project, store, voice_name=voice)
    applied, skipped = applier.apply_all_accepted()
    console.print(
        f"[green]Applied {applied} edit(s), skipped {skipped} (original text no longer matches).[/green]"
    )


@app.command()
def serve(
    projects_root: Path = typer.Option(
        Path.home() / "lattice", "--projects-root",
        help="Root directory containing lattice projects (one folder per project).",
    ),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(5173, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Start the web UI: FastAPI + WebSocket progress streaming + static frontend.

    Visit http://localhost:5173/ in a browser. The UI lists every project
    under --projects-root, lets you pick a review level (quick / standard /
    deep), and streams live timeline events as the pipeline executes.
    """
    import os
    os.environ["LATTICE_PROJECTS_ROOT"] = str(projects_root.resolve())

    try:
        import uvicorn  # type: ignore
    except ImportError:
        console.print(
            "[red]uvicorn is not installed. Install with:[/red]\n"
            "  pip install fastapi 'uvicorn[standard]' websockets"
        )
        raise typer.Exit(code=2)

    console.print(
        f"[green]Lattice web UI:[/green] http://{host}:{port}/  "
        f"(projects root: {projects_root.resolve()})"
    )
    uvicorn.run(
        "lattice.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@app.command()
def autofix(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
    level: str | None = typer.Option(
        None, "--level",
        help="Override config.autocorrect for this run: none|safe|aggressive.",
    ),
) -> None:
    """Auto-resolve audit flags by chaining flag acceptance + edit proposing + applying.

    The behaviour depends on Config.autocorrect (or --level override):

    - none:       refuses to run; exits with a clear message.
    - safe:       accepts flags whose default_mode is suggest_changes
                  (mechanical prose nits — weasel words, citation
                  engagement, formality), proposes edits for them,
                  auto-accepts the proposals, applies them.
    - aggressive: runs the safe pass, additionally accepts rewrite-mode
                  flags (clusters marked dirty for re-render), and
                  deletes orphan sentences when no claim attachment
                  exists.

    Never mutates the author graph.
    """
    project = _require_project(project)
    config = Config.load(project)
    if level:
        valid = ("none", "safe", "aggressive")
        if level not in valid:
            console.print(f"[red]Invalid --level {level!r}. Must be one of: {', '.join(valid)}[/red]")
            raise typer.Exit(code=2)
        config.autocorrect = level

    store = GraphStore.load(project)
    voice_obj = _load_voice(project, voice)

    llm: ClaudeClient | None = None
    if claude_available():
        try:
            llm = ClaudeClient(
                default_model=config.default_model,
                parallel=config.parallel_renders,
            )
        except Exception:
            llm = None

    from ..auditor.autofix import run_autofix
    result = run_autofix(config, store, voice_obj, llm)

    if result.notes:
        for note in result.notes:
            console.print(f"[yellow]{note}[/yellow]")

    if result.total_changes == 0 and not result.notes:
        console.print("[green]Nothing to autofix.[/green]")
    else:
        console.print(
            f"[green]Autofix at level {config.autocorrect!r}:[/green] "
            f"{result.summary_line()}"
        )
    if result.accepted_rewrite:
        console.print(
            "[yellow]Rewrite-mode flags accepted; affected clusters are now "
            "dirty. Run `lattice render --force` to regenerate them.[/yellow]"
        )


@app.command(name="voice-review")
def voice_review(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
) -> None:
    """Review the rendered paper against the full academic voice document.

    Document-level layers: register (sentence length distribution, first-person
    frequency, hedge density, contractions), paragraph (opener variety, length
    distribution), citation (reporting verb variety, synthesis threshold,
    positioning frames), architecture (hourglass shape, skim-target presence),
    attribution (quote thresholds, page specificity), skim targets (gap
    statement, conclusion strength).

    Writes outputs/voice_review.<voice>.md.
    """
    project = _require_project(project)
    Config.load(project)
    store = GraphStore.load(project)
    voice_obj = _load_voice(project, voice)
    report, out_path = voice_review_document(project, store, voice_obj)
    if out_path is None:
        console.print(
            f"[yellow]No rendered paper found at outputs/paper.{voice}.md. "
            f"Run `lattice run` first.[/yellow]"
        )
        raise typer.Exit(code=3)

    emoji = {"pass": "[green][OK][/green]", "warning": "[yellow][!][/yellow]", "fail": "[red][FAIL][/red]"}
    console.print(
        f"{emoji[report.overall]} Voice review: {report.overall} "
        f"({report.pass_count} pass, {report.warning_count} warning, "
        f"{report.fail_count} fail)"
    )
    table = Table(title="Findings by layer")
    table.add_column("layer", style="cyan")
    table.add_column("rule")
    table.add_column("verdict")
    table.add_column("summary")
    for f in report.findings:
        table.add_row(f.layer, f.rule, f.compliance, f.summary[:80])
    console.print(table)
    console.print(f"[green]Report written to {out_path}[/green]")


@app.command(name="fill-mechanisms")
def fill_mechanisms(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(
        "academic", "--voice", "-v",
        help="Voice name (used for the report filename only).",
    ),
    use_editor: bool = typer.Option(
        False, "--editor",
        help="Launch $EDITOR to write each mechanism instead of "
             "prompting inline. Useful for longer mechanisms.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Walk candidates and prompt, but don't touch outline.md.",
    ),
    limit: int = typer.Option(
        0, "--limit",
        help="Stop after this many candidates (0 = no limit).",
    ),
    min_importance: float = typer.Option(
        0.5, "--min-importance",
        help="Skip claims with importance below this floor "
             "(default 0.5 = no-info default; raise to 0.7 on "
             "annotated projects to focus on heavy claims).",
    ),
) -> None:
    """Walk empirical / methodological claims that lack a mechanism.

    The most common rescaffold-planner advisory class on real,
    well-scaffolded papers is `add_mechanism`. Walking it as part of
    the full rescaffold-apply flow is heavy; this is the focused
    version: list every claim with importance >= 0.6 and no
    [mechanism: ...] tag, prompt for a mechanism, append it to the
    bullet in-place.

    The author graph is NOT mutated — the outline file is the single
    edit point. Re-ingest after running this to refresh the graph.

    Snapshots `structure/outline.md` to `structure/outline.pre-fill-
    mechanisms.md` before any edits.
    """
    import asyncio as _asyncio
    import json as _json
    import os as _os
    import subprocess as _subprocess
    import tempfile as _tempfile

    project = _require_project(project)
    config = Config.load(project)

    structure_dir = project / "structure"
    outline_path = structure_dir / "outline.md"
    if not outline_path.exists():
        console.print(f"[red]No outline at {outline_path}.[/red]")
        raise typer.Exit(code=3)

    # Re-parse outline to get fresh graph + line numbers.
    ingester = MarkdownOutlineIngester(config)
    graph = _asyncio.run(
        ingester.ingest(outline_path, project_name=project.name)
    )
    if ingester.last_report is None:
        console.print("[red]Ingester didn't produce a scaffold report.[/red]")
        raise typer.Exit(code=4)

    from ..restructure.fill_mechanisms import (
        apply_mechanism_edits,
        collect_candidates,
        merge_saved_importance_and_mechanism,
        MechanismEdit,
    )

    # Merge in the saved graph's annotator-enriched importance +
    # mechanism. The fresh re-ingest is the source of truth for
    # structure (line numbers); the saved graph is the source of
    # truth for derived signal (importance, mechanism).
    store = GraphStore.load(project)
    try:
        saved_graph = store.get_graph()
    except (FileNotFoundError, KeyError):
        saved_graph = None
    if saved_graph is not None:
        merge_saved_importance_and_mechanism(graph, saved_graph)

    candidates = collect_candidates(
        graph, ingester.last_report, min_importance=min_importance,
    )
    if not candidates:
        console.print(
            "[green]No mechanism candidates — every empirical / "
            "methodological claim above the importance floor already "
            "has a [mechanism: ...] tag.[/green]"
        )
        return

    if limit > 0:
        candidates = candidates[:limit]

    console.print(
        f"[cyan]{len(candidates)} mechanism candidate(s).[/cyan] "
        f"Press [bold]Enter[/bold] alone to skip a claim, "
        f"[bold]q[/bold] then Enter to quit early."
    )
    console.print()

    edits: list[MechanismEdit] = []
    for i, cand in enumerate(candidates, start=1):
        console.print(
            f"[bold]\\[{i}/{len(candidates)}][/bold] "
            f"[dim]section[/dim] {cand.section_id or '(none)'} "
            f"[dim]importance[/dim] {cand.importance:.2f} "
            f"[dim]type[/dim] {cand.claim_type}"
        )
        console.print(f"  [yellow]{cand.statement}[/yellow]")
        if cand.original_excerpt and cand.original_excerpt != cand.statement:
            console.print(f"  [dim]raw bullet:[/dim] {cand.original_excerpt}")
        if cand.line is None:
            console.print(
                "  [red]no line number — skipping (cannot edit)[/red]"
            )
            edits.append(MechanismEdit(candidate=cand, mechanism=""))
            continue

        if use_editor:
            mechanism = _prompt_via_editor(cand)
        else:
            try:
                mechanism = typer.prompt(
                    "  mechanism",
                    default="",
                    show_default=False,
                ).strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]aborted by user[/yellow]")
                break
        if mechanism.lower() == "q":
            console.print("[yellow]quit early[/yellow]")
            break
        edits.append(MechanismEdit(candidate=cand, mechanism=mechanism))
        console.print()

    if dry_run:
        applied = sum(1 for e in edits if e.mechanism.strip())
        console.print(
            f"[cyan][dry-run][/cyan] would apply {applied} mechanism edit(s) "
            f"to {outline_path}."
        )
        return

    report = apply_mechanism_edits(outline_path, edits, snapshot=True)
    report.project_name = project.name
    report.voice_name = voice

    decisions_path = project / ".lattice" / "fill_mechanisms_decisions.json"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list = []
    if decisions_path.exists():
        try:
            existing = _json.loads(decisions_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except _json.JSONDecodeError:
            existing = []
    existing.append({
        "generated_at": report.generated_at.isoformat(),
        "voice": voice,
        "candidate_count": report.candidate_count,
        "edits_applied": report.edits_applied,
        "edits_skipped": report.edits_skipped,
        "outline_path": report.outline_path,
        "snapshot_path": report.snapshot_path,
        "edits": report.edits,
    })
    decisions_path.write_text(
        _json.dumps(existing, indent=2), encoding="utf-8",
    )

    console.print(
        f"[green]Applied {report.edits_applied} mechanism edit(s); "
        f"skipped {report.edits_skipped}.[/green]"
    )
    if report.snapshot_path:
        console.print(f"  snapshot → {report.snapshot_path}")
    console.print(f"  decisions → {decisions_path}")
    console.print(
        "[dim]Re-run `lattice ingest` (or the Scaffold activity) so "
        "the graph picks up the new mechanism tags.[/dim]"
    )


def _prompt_via_editor(cand) -> str:
    """Open $EDITOR with a header + blank line, return the body the
    user wrote (everything after the first blank line, stripped)."""
    import os as _os
    import subprocess as _subprocess
    import tempfile as _tempfile

    editor = _os.environ.get("EDITOR") or "notepad"
    header = (
        f"# Claim {cand.claim_id} (importance {cand.importance:.2f})\n"
        f"# {cand.statement}\n"
        f"#\n"
        f"# Write the mechanism below this line. Save and quit when done.\n"
        f"# Empty file = skip this claim.\n"
        f"\n"
    )
    with _tempfile.NamedTemporaryFile(
        suffix=".md", mode="w", delete=False, encoding="utf-8",
    ) as tf:
        tf.write(header)
        tmp_path = tf.name
    try:
        _subprocess.call([editor, tmp_path])
        contents = Path(tmp_path).read_text(encoding="utf-8")
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass
    body_lines = [
        line for line in contents.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return " ".join(body_lines).strip()


references_app = typer.Typer(
    help="Reference-store management — import, export, list.",
    no_args_is_help=True,
)
app.add_typer(references_app, name="references")


@references_app.command("import")
def references_import(
    project: Path = typer.Argument(...),
    file: Path = typer.Argument(
        ..., exists=True, readable=True,
        help="Reference file: .json (Zotero CSL-JSON) / .bib / .ris",
    ),
    format: str = typer.Option(
        "", "--format", "-f",
        help="Force a format: csl-json / bib / ris. Default: detect "
             "from file suffix.",
    ),
    dedupe: bool = typer.Option(
        True, "--dedupe/--no-dedupe",
        help="Skip imports that match an existing source by DOI or "
             "by (year, surname, title) hash.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Parse + report; don't write to the source store.",
    ),
) -> None:
    """Import references from Zotero / BibTeX / RIS into the project's
    source store.

    Most academics already have a curated reference library; this is
    the on-ramp into Lattice without re-tagging by hand. Pairs with
    `lattice citations verify` (Crossref/OpenAlex check) and
    `lattice citations restyle` (output in any format) so a complete
    workflow is: import → verify → restyle for each submission.
    """
    from ..references.importers import (
        import_references_from_file,
        merge_into_store,
    )
    from ..utils.errors import LatticeError

    project = _require_project(project)
    Config.load(project)
    fmt = format.strip().lower() or None

    try:
        report = import_references_from_file(file, format=fmt)
    except Exception as e:  # noqa: BLE001
        raise _surface_lattice_error(LatticeError(
            code="reference_import_failed",
            message=f"Could not parse {file.name}: {type(e).__name__}: {e}",
            next_step="Check the file format. Try --format csl-json|bib|ris.",
            exit_code=3,
        ))

    if not report.sources:
        console.print(
            f"[yellow]Parsed {file.name} ({report.detected_format}) "
            f"but no usable entries.[/yellow]"
        )
        if report.warnings:
            for w in report.warnings:
                console.print(f"  warning: {w}")
        if report.skipped:
            console.print(f"  skipped: {len(report.skipped)} entries")
        raise typer.Exit(code=3)

    store = GraphStore.load(project)
    existing = store.list_sources()
    if dedupe:
        merged, decisions = merge_into_store(report.sources, existing)
        added = sum(1 for v in decisions.values() if v == "added")
        duplicates = sum(1 for v in decisions.values() if v == "duplicate")
    else:
        merged = list(existing) + list(report.sources)
        added = len(report.sources)
        duplicates = 0

    if dry_run:
        console.print(
            f"[cyan][dry-run][/cyan] would add {added} source(s); "
            f"would skip {duplicates} duplicate(s); "
            f"{len(report.skipped)} unparseable entries."
        )
        return

    # Persist new sources only.
    new_ids = {s.source_id for s in merged} - {s.source_id for s in existing}
    for src in merged:
        if src.source_id in new_ids:
            store.save_source(src)

    console.print(
        f"[green]Imported[/green] {added} source(s) from {file.name} "
        f"({report.detected_format}). "
        f"Skipped {duplicates} duplicate(s); "
        f"{len(report.skipped)} unparseable."
    )
    if report.warnings:
        for w in report.warnings[:5]:
            console.print(f"  [yellow]warning[/yellow]: {w}")


@references_app.command("export")
def references_export(
    project: Path = typer.Argument(...),
    format: str = typer.Option(
        "bib", "--format", "-f",
        help="Output format: bib (BibTeX), ris, or csl-json (Zotero).",
    ),
    output: Path = typer.Option(
        None, "--output", "-o",
        help="Output path (default: refs/export.<suffix> in the project).",
    ),
) -> None:
    """Export the project's source store to BibTeX / RIS / CSL-JSON.

    Pairs with `lattice citations verify` to produce a canonical
    bibliography that drops cleanly into a LaTeX project (`.bib`),
    another reference manager (`.ris`), or Zotero (`csl-json`).
    """
    from ..references.exporters import (
        export_references,
        supported_export_formats,
    )
    from ..utils.errors import LatticeError, err_no_sources, err_unknown_style

    project = _require_project(project)
    Config.load(project)
    store = GraphStore.load(project)
    sources = store.list_sources()
    if not sources:
        raise _surface_lattice_error(err_no_sources(str(project)))

    try:
        text, suffix = export_references(sources, format)
    except ValueError:
        raise _surface_lattice_error(err_unknown_style(
            format, supported_export_formats(),
        ))

    if output is None:
        output = project / "refs" / f"export.{suffix}"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    console.print(
        f"[green]Exported[/green] {len(sources)} source(s) "
        f"({format}) → {output}"
    )


@references_app.command("list")
def references_list(
    project: Path = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", "-n",
                              help="Show at most N entries (0 = all)."),
) -> None:
    """List the project's source store."""
    project = _require_project(project)
    Config.load(project)
    store = GraphStore.load(project)
    sources = store.list_sources()
    if not sources:
        console.print("[dim]No sources in the store.[/dim]")
        return
    table = Table(title=f"{len(sources)} source(s) in {project.name}")
    table.add_column("source_id", style="cyan")
    table.add_column("year", justify="right")
    table.add_column("authors")
    table.add_column("title")
    shown = sources if limit == 0 else sources[:limit]
    for src in shown:
        c = src.citation
        authors = ", ".join(c.authors[:2]) if c.authors else "?"
        if c.authors and len(c.authors) > 2:
            authors += " et al."
        table.add_row(
            src.source_id,
            str(c.year) if c.year else "?",
            authors[:40],
            (c.title or "")[:60],
        )
    console.print(table)
    if limit and len(sources) > limit:
        console.print(f"[dim]...and {len(sources) - limit} more (use --limit 0 to show all)[/dim]")


citations_app = typer.Typer(
    help="Citation management — scan, match, verify, fill, restyle.",
    no_args_is_help=True,
)
app.add_typer(citations_app, name="citations")


@citations_app.command("scan")
def citations_scan(
    project: Path = typer.Argument(...),
    document: Path = typer.Option(
        None, "--document", "-d",
        help="Document to scan (default: outputs/paper.<voice>.md or "
             "structure/outline.md, whichever exists).",
    ),
    voice: str = typer.Option(
        "academic", "--voice", "-v",
        help="Voice name (used to find the rendered paper).",
    ),
    no_match: bool = typer.Option(
        False, "--no-match",
        help="Skip the matcher pass that links inline citations to "
             "Sources. Default is to scan + match in one go.",
    ),
) -> None:
    """Scan a document for inline citations, footnotes, and the
    bibliography section. Detects the citation system in use and
    writes ``.lattice/document_citations.json``.

    By default, also runs the matcher (links each inline / footnote
    citation to a known Source by surname + year, resolves
    Ibid. / op. cit.). Pass ``--no-match`` to skip.

    Pure regex/heuristics — no LLM calls. Run on the full rendered
    paper (``outputs/paper.<voice>.md``) for best coverage; falls back
    to the outline if no paper exists.
    """
    project = _require_project(project)
    Config.load(project)

    if document is None:
        candidates = [
            project / "outputs" / f"paper.{voice}.md",
            project / "structure" / "outline.md",
        ]
        for c in candidates:
            if c.exists():
                document = c
                break
    if document is None or not document.exists():
        console.print(f"[red]No document found to scan.[/red]")
        raise typer.Exit(code=3)

    from ..references.matcher import match_citations
    from ..references.scanner import save_document_citations, scan_document

    text = document.read_text(encoding="utf-8")
    doc = scan_document(
        text, project_name=project.name, document_path=str(document),
    )

    if not no_match:
        store = GraphStore.load(project)
        sources = store.list_sources()
        match_citations(doc, sources)

    path = save_document_citations(project, doc)

    detail = (
        f"{doc.counts['inline_total']} inline, "
        f"{doc.counts['footnotes_total']} footnote(s), "
        f"{doc.counts['bibliography_entries']} bibliography entries"
    )
    if not no_match:
        m = doc.counts.get("inline_matched", 0)
        u = doc.counts.get("inline_unmatched", 0)
        detail += f" · {m} matched, {u} unresolved"
    console.print(
        f"[cyan]Scanned[/cyan] {document}: "
        f"{doc.detected_system.value} system · {detail}."
    )
    console.print(f"  json → {path}")


@citations_app.command("verify")
def citations_verify(
    project: Path = typer.Argument(...),
    no_cache: bool = typer.Option(
        False, "--no-cache",
        help="Ignore the verification cache and re-query everything.",
    ),
    crossref_only: bool = typer.Option(
        False, "--crossref-only",
        help="Skip the OpenAlex pass.",
    ),
    openalex_only: bool = typer.Option(
        False, "--openalex-only",
        help="Skip the Crossref pass.",
    ),
    user_email: str = typer.Option(
        "", "--email",
        help="Optional contact email; included in the polite-pool "
             "User-Agent so Crossref can reach you about traffic.",
    ),
) -> None:
    """Verify each source against Crossref + OpenAlex.

    For every source in the project's source store: lookup by DOI when
    present, else search by title + author + year. Score the canonical
    metadata against the paper's; surface field-level discrepancies.

    Cached by source-content hash; re-runs are cheap. Updates
    .lattice/document_citations.json (per-source verifications) AND
    .lattice/citation_verifications.json (the cache).
    """
    import asyncio as _asyncio

    project = _require_project(project)
    Config.load(project)
    store = GraphStore.load(project)
    sources = store.list_sources()
    if not sources:
        console.print(
            "[yellow]No sources to verify. Run `lattice index` (and "
            "optionally `lattice citations scan`) first.[/yellow]"
        )
        raise typer.Exit(code=3)

    from ..references.scanner import (
        load_document_citations,
        save_document_citations,
    )
    from ..references.verifier import (
        VerifierConfig,
        load_verification_cache,
        save_verification_cache,
        verify_sources,
    )

    cache = {} if no_cache else load_verification_cache(project)
    cfg = VerifierConfig(
        user_agent=(
            f"lattice-citation-verifier/0.1 (mailto:{user_email})"
            if user_email else f"lattice-citation-verifier/0.1"
        ),
        use_crossref=not openalex_only,
        use_openalex=not crossref_only,
    )

    console.print(
        f"[cyan]Verifying[/cyan] {len(sources)} source(s) "
        f"({len(cache)} cached)..."
    )
    results = _asyncio.run(verify_sources(sources, config=cfg, cache=cache))
    save_verification_cache(project, results)

    # Update document_citations.json so the filler + restyle steps see
    # the verifications.
    doc = load_document_citations(project)
    if doc is not None:
        doc.verifications = results
        save_document_citations(project, doc)

    matched = sum(1 for v in results.values() if v.matched)
    error_diffs = sum(
        1 for v in results.values()
        for d in v.discrepancies
        if d.severity.value == "error"
    )
    warn_diffs = sum(
        1 for v in results.values()
        for d in v.discrepancies
        if d.severity.value == "warning"
    )
    console.print(
        f"[green]Verified[/green] {matched}/{len(sources)} matched · "
        f"{error_diffs} error-level discrepancy(ies) · "
        f"{warn_diffs} warning(s)."
    )


@citations_app.command("fill")
def citations_fill(
    project: Path = typer.Argument(...),
    severity: str = typer.Option(
        "info", "--severity",
        help="Minimum severity to walk: error / warning / info "
             "(default: info — walks everything).",
    ),
    limit: int = typer.Option(
        0, "--limit",
        help="Stop after this many decisions (0 = no limit).",
    ),
    accept_all_gaps: bool = typer.Option(
        False, "--accept-all-gaps",
        help="Auto-accept canonical values for fields where the paper "
             "currently has nothing (info-level gap fills). Useful for "
             "DOI / pages / volume / issue when the verifier returned a "
             "high-confidence match.",
    ),
) -> None:
    """Walk the verifier's discrepancies and accept / reject each.

    For every field where the paper disagrees with Crossref / OpenAlex,
    show paper value vs canonical value and prompt:

      a) accept canonical · r) keep paper · m) manual override
      enter) skip · q) quit

    Decisions are logged to ``.lattice/citation_decisions.json``;
    re-runs only walk undecided fields. Source records in the source
    store are updated in place.
    """
    from ..references.filler import (
        apply_decisions,
        append_decisions,
        collect_fill_candidates,
        FillDecision,
        load_decisions,
    )
    from ..graph.models import CitationDiscrepancySeverity

    project = _require_project(project)
    Config.load(project)
    store = GraphStore.load(project)
    sources = store.list_sources()
    if not sources:
        console.print("[yellow]No sources to fill.[/yellow]")
        raise typer.Exit(code=3)

    from ..references.verifier import load_verification_cache
    verifications = load_verification_cache(project)
    if not verifications:
        console.print(
            "[yellow]No verification cache. Run "
            "`lattice citations verify` first.[/yellow]"
        )
        raise typer.Exit(code=3)

    severity_floor = {
        "error": CitationDiscrepancySeverity.error,
        "warning": CitationDiscrepancySeverity.warning,
        "info": CitationDiscrepancySeverity.info,
    }.get(severity.lower(), CitationDiscrepancySeverity.info)

    decided = load_decisions(project)
    candidates = collect_fill_candidates(
        verifications, decided=decided, severity_floor=severity_floor,
    )
    if not candidates:
        console.print(
            "[green]Nothing to fill — every discrepancy already "
            "decided or below severity floor.[/green]"
        )
        return
    if limit > 0:
        candidates = candidates[:limit]

    console.print(
        f"[cyan]{len(candidates)} field(s) to decide.[/cyan]  "
        f"[bold]a[/bold]ccept · [bold]r[/bold]eject · [bold]m[/bold]anual · "
        f"[bold]Enter[/bold] skip · [bold]q[/bold]uit"
    )
    console.print()

    decisions: list[FillDecision] = []
    for i, cand in enumerate(candidates, 1):
        sev_colour = {
            CitationDiscrepancySeverity.error: "red",
            CitationDiscrepancySeverity.warning: "yellow",
            CitationDiscrepancySeverity.info: "dim",
        }[cand.severity]
        gap = " [dim](gap fill)[/dim]" if cand.is_gap_fill else ""
        console.print(
            f"[bold]\\[{i}/{len(candidates)}][/bold] "
            f"source [cyan]{cand.source_id}[/cyan] · field "
            f"[bold]{cand.field}[/bold] · "
            f"[{sev_colour}]{cand.severity.value}[/{sev_colour}] · "
            f"verifier {cand.verifier.value}{gap}"
        )
        console.print(f"  paper:     [yellow]{cand.paper_value or '(empty)'}[/yellow]")
        console.print(f"  canonical: [green]{cand.canonical_value or '(empty)'}[/green]")

        if accept_all_gaps and cand.is_gap_fill and cand.severity == CitationDiscrepancySeverity.info:
            decisions.append(FillDecision(
                candidate=cand, action="accept_canonical",
            ))
            console.print("  [dim]auto-accepted (gap-fill mode)[/dim]")
            console.print()
            continue

        try:
            choice = typer.prompt(
                "  action [a/r/m/Enter/q]", default="", show_default=False,
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]aborted by user[/yellow]")
            break
        if choice == "q":
            break
        if choice == "" or choice == "skip":
            decisions.append(FillDecision(candidate=cand, action="skip"))
        elif choice == "a":
            decisions.append(FillDecision(
                candidate=cand, action="accept_canonical",
            ))
        elif choice == "r":
            decisions.append(FillDecision(candidate=cand, action="reject"))
        elif choice == "m":
            try:
                value = typer.prompt(
                    "  manual value", default="", show_default=False,
                ).strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]aborted[/yellow]")
                break
            decisions.append(FillDecision(
                candidate=cand, action="manual_override", chosen_value=value,
            ))
        else:
            console.print(f"  [yellow]unknown choice {choice!r} — skipped[/yellow]")
            decisions.append(FillDecision(candidate=cand, action="skip"))
        console.print()

    updated, log = apply_decisions(sources, decisions)
    for src in updated.values():
        # Only save sources that actually changed.
        original = next((s for s in sources if s.source_id == src.source_id), None)
        if original is None or original.model_dump_json() != src.model_dump_json():
            store.save_source(src)
    if log:
        append_decisions(project, log)

    applied = sum(1 for d in decisions if d.action != "skip")
    console.print(
        f"[green]Recorded {applied} decision(s); "
        f"updated {sum(1 for s in updated.values() if next((o for o in sources if o.source_id == s.source_id), None) and next(o for o in sources if o.source_id == s.source_id).model_dump_json() != s.model_dump_json())} source(s).[/green]"
    )


@citations_app.command("restyle")
def citations_restyle(
    project: Path = typer.Argument(...),
    document: Path = typer.Option(
        None, "--document", "-d",
        help="Source document (default: outputs/paper.<voice>.md).",
    ),
    voice: str = typer.Option("academic", "--voice", "-v"),
    style: str = typer.Option(
        "apa", "--style", "-s",
        help="Target citation style: harvard / apa / chicago_author_date / "
             "mla / vancouver / ieee. To use a per-journal override, "
             "pass `--journal <name>` instead (or as well — the journal's "
             "declared base wins).",
    ),
    journal: str = typer.Option(
        "", "--journal", "-j",
        help="Apply a per-journal style override from "
             "voices/journals/<journal>.yml. Supersedes --style when set.",
    ),
    output: Path = typer.Option(
        None, "--output", "-o",
        help="Output path (default: <stem>.<style><suffix> next to source).",
    ),
) -> None:
    """Rewrite a document in a target citation style.

    Walks every inline citation and the bibliography, re-emits in the
    new style. Deterministic, no LLM calls — instant style switching.

    Run `lattice citations scan` first so DocumentCitations is fresh;
    `lattice citations verify` + `lattice citations fill` are
    optional but improve the bibliography quality.

    Pass `--journal nature` (or another name from
    `lattice citations journals list`) to apply per-journal tweaks
    on top of the base style.
    """
    project = _require_project(project)
    Config.load(project)

    if document is None:
        document = project / "outputs" / f"paper.{voice}.md"
    if not document.exists():
        console.print(f"[red]Document not found: {document}[/red]")
        raise typer.Exit(code=3)

    from ..references.matcher import match_citations
    from ..references.rewriter import restyle_document, write_restyled
    from ..references.scanner import scan_document

    target_style = style
    journal_obj = None
    if journal:
        from ..references.journal_styles import load_journal_style
        try:
            journal_obj = load_journal_style(project, journal)
        except (FileNotFoundError, ValueError) as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=3)
        target_style = journal_obj.base

    text = document.read_text(encoding="utf-8")
    doc = scan_document(text, project_name=project.name, document_path=str(document))
    store = GraphStore.load(project)
    sources = store.list_sources()
    match_citations(doc, sources)

    try:
        result = restyle_document(text, doc, sources, style=target_style)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    if journal_obj is not None:
        # Apply journal-specific post-processing to the restyled doc.
        from ..output.citation_formatter import format_citation
        from ..references.journal_styles import format_for_journal
        cited_ids = {
            ic.source_id for ic in doc.inline_citations if ic.source_id
        } | {
            (f.source_id or f.resolves_to_source_id) for f in doc.footnotes
        }
        for src in sources:
            if src.source_id not in cited_ids:
                continue
            override = format_for_journal(src.citation, journal_obj)
            base_formatted = format_citation(src.citation, target_style)
            if (
                base_formatted.bibliography
                and base_formatted.bibliography in result.document
            ):
                result.document = result.document.replace(
                    base_formatted.bibliography, override.bibliography, 1,
                )
        result.style = f"{target_style}+{journal_obj.name}"

    out_path = write_restyled(document, result, output_path=output)
    console.print(
        f"[green]Restyled[/green] {document.name} → {out_path.name} "
        f"({result.inline_replaced} inline replaced, "
        f"{result.inline_unresolved} unresolved, "
        f"{result.bibliography_emitted} bibliography entries)."
    )
    if result.notes:
        for note in result.notes:
            console.print(f"  [yellow]note[/yellow] {note}")


journals_app = typer.Typer(
    help="Per-journal citation style overrides.",
    no_args_is_help=True,
)
citations_app.add_typer(journals_app, name="journals")


@journals_app.command("list")
def journals_list(project: Path = typer.Argument(...)) -> None:
    """List available per-journal style overrides."""
    project = _require_project(project)
    from ..references.journal_styles import list_journal_styles, load_journal_style
    names = list_journal_styles(project)
    if not names:
        console.print(
            "[yellow]No journal styles. Run "
            "`lattice citations journals install` to add the starter "
            "library (Nature, Science, IEEE Transactions, etc.).[/yellow]"
        )
        return
    table = Table(title=f"Journal styles in {project.name}")
    table.add_column("name", style="cyan")
    table.add_column("base")
    table.add_column("description")
    for name in names:
        try:
            j = load_journal_style(project, name)
            desc = (j.description or "").split("\n")[0][:60]
            table.add_row(name, j.base, desc)
        except Exception as e:  # noqa: BLE001
            table.add_row(name, "?", f"[red]{e}[/red]")
    console.print(table)


@journals_app.command("install")
def journals_install(project: Path = typer.Argument(...)) -> None:
    """Install the starter library of journal styles into
    ``voices/journals/``. Idempotent — won't overwrite your edits."""
    project = _require_project(project)
    from ..references.journal_styles import write_starter_journal_styles
    written = write_starter_journal_styles(project)
    if not written:
        console.print(
            "[dim]No new files written — every starter style was "
            "already present.[/dim]"
        )
    else:
        console.print(
            f"[green]Wrote {len(written)} journal style(s):[/green] "
            f"{', '.join(p.stem for p in written)}"
        )


@citations_app.command("report")
def citations_report(
    project: Path = typer.Argument(...),
) -> None:
    """Print a summary of the project's citation state.

    Shows: scan status, system detected, inline / footnote counts,
    matched vs unresolved, verification status, error-level
    discrepancies, undecided fill candidates.
    """
    project = _require_project(project)

    from ..references.scanner import load_document_citations
    from ..references.verifier import load_verification_cache
    from ..references.filler import collect_fill_candidates, load_decisions

    doc = load_document_citations(project)
    if doc is None:
        console.print(
            "[yellow]No DocumentCitations found. Run "
            "`lattice citations scan` first.[/yellow]"
        )
        raise typer.Exit(code=3)

    verifications = load_verification_cache(project)
    decided = load_decisions(project)
    pending = collect_fill_candidates(verifications, decided=decided)

    console.print(f"[bold]Citations report — {project.name}[/bold]")
    console.print()
    console.print(f"document: {doc.document_path}")
    console.print(f"system:   {doc.detected_system.value}")
    console.print(f"scanned:  {doc.scanned_at.isoformat()}")
    console.print()

    table = Table(title="Counts")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for k, v in doc.counts.items():
        table.add_row(k, str(v))
    console.print(table)
    console.print()

    if verifications:
        matched = sum(1 for v in verifications.values() if v.matched)
        errors = sum(
            1 for v in verifications.values() for d in v.discrepancies
            if d.severity.value == "error"
        )
        warnings = sum(
            1 for v in verifications.values() for d in v.discrepancies
            if d.severity.value == "warning"
        )
        console.print(
            f"verifier: {matched}/{len(verifications)} matched · "
            f"{errors} error(s) · {warnings} warning(s) · "
            f"{len(decided)} decided · {len(pending)} pending"
        )
    else:
        console.print(
            "[dim]No verification cache — run `lattice citations verify`.[/dim]"
        )


@app.command(name="fill-evidence")
def fill_evidence(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(
        "academic", "--voice", "-v",
        help="Voice name (used for the report filename only).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Walk candidates and prompt, but don't touch outline.md.",
    ),
    limit: int = typer.Option(
        0, "--limit",
        help="Stop after this many candidates (0 = no limit).",
    ),
    min_importance: float = typer.Option(
        0.5, "--min-importance",
        help="Skip claims with importance below this floor "
             "(default 0.5; raise to 0.7 on annotated projects).",
    ),
    supporters_only: bool = typer.Option(
        False, "--supporters-only",
        help="Only walk claims that transitively support the thesis. "
             "Use this when you want to focus on the strength-lifting "
             "subset rather than every weakly-grounded claim.",
    ),
) -> None:
    """Walk weakly-grounded empirical / methodological / normative /
    definition claims and bind evidence in-place.

    For each candidate, choose:
      r) [ref: <citekey>] — you know which source backs the claim
      h) [evidence_status: source_hint] — you've found the source but
         haven't bound a passage
      u) [evidence_status: unbound] — explicit acknowledgement of the gap
      s) convert to [type: user_synthesis] — claim is your own analysis
      enter) skip
      q) quit early

    The author graph is NOT mutated — the outline is the single edit
    point. Re-ingest after running this so the graph picks up the new
    tags. Snapshots `structure/outline.md` to
    `structure/outline.pre-fill-evidence.md` before editing.
    """
    import asyncio as _asyncio
    import json as _json

    project = _require_project(project)
    config = Config.load(project)

    structure_dir = project / "structure"
    outline_path = structure_dir / "outline.md"
    if not outline_path.exists():
        console.print(f"[red]No outline at {outline_path}.[/red]")
        raise typer.Exit(code=3)

    ingester = MarkdownOutlineIngester(config)
    graph = _asyncio.run(
        ingester.ingest(outline_path, project_name=project.name)
    )
    if ingester.last_report is None:
        console.print("[red]Ingester didn't produce a scaffold report.[/red]")
        raise typer.Exit(code=4)

    from ..restructure.fill_mechanisms import (
        merge_saved_importance_and_mechanism,
    )
    from ..restructure.fill_evidence import (
        apply_evidence_edits,
        collect_candidates,
        EvidenceEdit,
    )

    store = GraphStore.load(project)
    try:
        saved_graph = store.get_graph()
    except (FileNotFoundError, KeyError):
        saved_graph = None
    if saved_graph is not None:
        merge_saved_importance_and_mechanism(graph, saved_graph)

    candidates = collect_candidates(
        graph,
        ingester.last_report,
        min_importance=min_importance,
        supporters_first=True,
    )
    if supporters_only:
        candidates = [c for c in candidates if c.is_supporter]
    if not candidates:
        console.print(
            "[green]No evidence candidates — every empirical / "
            "methodological / normative / definition claim above the "
            "importance floor is bound or has an explicit "
            "evidence_status.[/green]"
        )
        return

    if limit > 0:
        candidates = candidates[:limit]

    console.print(
        f"[cyan]{len(candidates)} evidence candidate(s).[/cyan] "
        f"Per claim choose: [bold]r[/bold]ef · source-[bold]h[/bold]int · "
        f"[bold]u[/bold]nbound · [bold]s[/bold]ynthesis · enter to skip · "
        f"[bold]q[/bold] to quit."
    )
    console.print()

    edits: list[EvidenceEdit] = []
    for i, cand in enumerate(candidates, start=1):
        marker = " [supports thesis]" if cand.is_supporter else ""
        console.print(
            f"[bold]\\[{i}/{len(candidates)}][/bold] "
            f"[dim]section[/dim] {cand.section_id or '(none)'} "
            f"[dim]importance[/dim] {cand.importance:.2f} "
            f"[dim]type[/dim] {cand.claim_type}"
            f"[yellow]{marker}[/yellow]"
        )
        console.print(f"  [yellow]{cand.statement}[/yellow]")
        if cand.current_status:
            console.print(
                f"  [dim]current evidence_status:[/dim] {cand.current_status}"
            )
        if cand.line is None:
            console.print("  [red]no line number — skipping[/red]")
            edits.append(EvidenceEdit(candidate=cand, action="skip"))
            continue

        try:
            choice = typer.prompt(
                "  action [r/h/u/s/Enter/q]", default="", show_default=False
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]aborted by user[/yellow]")
            break
        if choice == "q":
            console.print("[yellow]quit early[/yellow]")
            break
        if choice == "" or choice == "skip":
            edits.append(EvidenceEdit(candidate=cand, action="skip"))
            console.print()
            continue
        if choice == "r":
            try:
                citekey = typer.prompt(
                    "  citekey", default="", show_default=False
                ).strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[yellow]aborted by user[/yellow]")
                break
            if not citekey:
                edits.append(EvidenceEdit(candidate=cand, action="skip"))
            else:
                edits.append(EvidenceEdit(
                    candidate=cand, action="add_ref", citekey=citekey,
                ))
        elif choice == "h":
            edits.append(EvidenceEdit(candidate=cand, action="set_source_hint"))
        elif choice == "u":
            edits.append(EvidenceEdit(candidate=cand, action="set_unbound"))
        elif choice == "s":
            edits.append(EvidenceEdit(
                candidate=cand, action="convert_to_synthesis",
            ))
        else:
            console.print(
                f"  [yellow]unknown choice {choice!r} — skipping[/yellow]"
            )
            edits.append(EvidenceEdit(candidate=cand, action="skip"))
        console.print()

    if dry_run:
        applied = sum(1 for e in edits if e.action != "skip")
        console.print(
            f"[cyan][dry-run][/cyan] would apply {applied} evidence "
            f"edit(s) to {outline_path}."
        )
        return

    report = apply_evidence_edits(outline_path, edits, snapshot=True)
    report.project_name = project.name
    report.voice_name = voice

    decisions_path = project / ".lattice" / "fill_evidence_decisions.json"
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    existing: list = []
    if decisions_path.exists():
        try:
            existing = _json.loads(decisions_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except _json.JSONDecodeError:
            existing = []
    existing.append({
        "generated_at": report.generated_at.isoformat(),
        "voice": voice,
        "candidate_count": report.candidate_count,
        "edits_applied": report.edits_applied,
        "edits_skipped": report.edits_skipped,
        "outline_path": report.outline_path,
        "snapshot_path": report.snapshot_path,
        "edits": report.edits,
    })
    decisions_path.write_text(
        _json.dumps(existing, indent=2), encoding="utf-8",
    )

    console.print(
        f"[green]Applied {report.edits_applied} evidence edit(s); "
        f"skipped {report.edits_skipped}.[/green]"
    )
    if report.snapshot_path:
        console.print(f"  snapshot → {report.snapshot_path}")
    console.print(f"  decisions → {decisions_path}")
    console.print(
        "[dim]Re-run `lattice ingest` (or the Scaffold activity) so "
        "the graph picks up the new evidence tags.[/dim]"
    )


@app.command(name="rescaffold")
def rescaffold(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
    threshold: float = typer.Option(
        0.5, "--threshold",
        help="Sub-scores below this trigger structural moves (default 0.5).",
    ),
) -> None:
    """Propose a metrics-driven rescaffold of the document.

    Reads the current author graph and computes argument strength + breadth
    metrics. For every sub-score below the threshold, generates structural
    operations (split section, add stub, reorder, move-to-offcuts) and
    claim-level advisories (bind evidence, add mechanism, diversify
    sources). Predicts the metric deltas if every operation were applied,
    so the author can see expected lift before accepting any move.

    Pure analysis — never mutates the graph or the outline. Writes:

    - .lattice/rescaffold_plan.json (machine-readable, with per-op
      predicted deltas + per-claim claim_size scores)
    - outputs/rescaffold_plan.<voice>.md (human-readable)

    The output is advisory; a separate apply step (lattice rescaffold-apply,
    not yet implemented) walks accepted operations and edits the outline
    after explicit confirmation.
    """
    import json as _json
    project = _require_project(project)
    Config.load(project)
    voice_obj = _load_voice(project, voice)
    store = GraphStore.load(project)
    graph = store.get_graph()
    if not graph.claims:
        console.print(
            "[red]Author graph is empty. Run `lattice ingest` first.[/red]"
        )
        raise typer.Exit(code=3)

    from ..restructure.rescaffold_planner import plan_rescaffold
    from ..restructure.rescaffold_formatter import format_plan_markdown

    clusters = store.list_clusters()
    plan = plan_rescaffold(
        graph, voice_obj, current_clusters=clusters, threshold=threshold,
    )

    json_path = project / ".lattice" / "rescaffold_plan.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    md_path = project / "outputs" / f"rescaffold_plan.{voice_obj.name}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(format_plan_markdown(plan), encoding="utf-8")

    op_count = len(plan.operations)
    adv_count = len(plan.advisories)
    if op_count == 0 and adv_count == 0:
        console.print(
            "[green]Structure is healthy — every metric is above threshold.[/green]"
        )
    else:
        console.print(
            f"[cyan]Rescaffold plan:[/cyan] {op_count} operation(s), "
            f"{adv_count} advisor(y/ies). "
            f"Predicted Δstrength {plan.expected_strength_delta:+.2f}, "
            f"Δbreadth {plan.expected_breadth_delta:+.2f}."
        )
    console.print(f"  json → {json_path}")
    console.print(f"  md   → {md_path}")


@app.command(name="source-review")
def source_review(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
    reference: Path = typer.Option(
        ..., "--reference", "-r",
        help="Path to a richer reference document (e.g. the human-written long form) "
             "to compare the rendered paper against.",
    ),
) -> None:
    """Compare the rendered paper to a richer reference document.

    Surfaces specific content gaps: quantitative facts, named scholars,
    mechanisms, analytical pivots, arithmetic walkthroughs, and concrete
    examples that the reference carries but the rendered paper omits.

    The output is advisory. The author reviews the gap report and decides
    what (if anything) to add to the graph; nothing is auto-injected.

    Writes outputs/source_gap_review.<voice>.md.
    """
    project = _require_project(project)
    config = Config.load(project)
    voice_obj = _load_voice(project, voice)
    paper_path = project / "outputs" / f"paper.{voice_obj.name}.md"
    if not paper_path.exists():
        console.print(
            f"[red]No rendered paper at {paper_path}. Run `lattice render` first.[/red]"
        )
        raise typer.Exit(code=3)
    if not reference.exists():
        console.print(f"[red]Reference document not found: {reference}[/red]")
        raise typer.Exit(code=3)

    _require_claude()
    llm = ClaudeClient(
        default_model=config.default_model,
        parallel=config.parallel_renders,
    )

    store = GraphStore.load(project)
    graph = store.get_graph()

    from ..auditor.source_gap_review import SourceGapReview, write_report
    review = SourceGapReview(config, llm)
    report = asyncio.run(review.review(
        paper_path=paper_path,
        reference_path=reference,
        graph=graph,
    ))
    out_path = write_report(report, project, voice_obj.name)

    by_cat = report.by_category
    console.print(f"[green]Source-gap review: {len(report.gaps)} gap(s) identified.[/green]")
    if by_cat:
        table = Table(title="Gaps by category")
        table.add_column("category", style="cyan")
        table.add_column("count", justify="right")
        for cat in sorted(by_cat, key=lambda k: -len(by_cat[k])):
            table.add_row(cat, str(len(by_cat[cat])))
        console.print(table)
    console.print(f"[green]Report written to {out_path}[/green]")


@app.command(name="source-review-apply")
def source_review_apply(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
    interactive: bool = typer.Option(
        True, "--interactive/--batch",
        help="Walk gaps interactively. Use --batch with --accept-all-with-targets "
             "to apply every gap that has a target_claim_id without prompting.",
    ),
    accept_all_with_targets: bool = typer.Option(
        False, "--accept-all-with-targets",
        help="In batch mode, mark every gap with a non-empty target_claim_id as "
             "accepted before applying. Useful for triaging large reports.",
    ),
    only_categories: str = typer.Option(
        "", "--only",
        help="Comma-separated list of categories to walk (e.g. 'mechanism,quantitative'). "
             "Gaps in other categories are left untouched.",
    ),
) -> None:
    """Walk the structured source-gap report and apply accepted gaps.

    Reads .lattice/source_gap_review.<voice>.json (produced by
    ``lattice source-review``). For each undecided gap, prompts the
    author for accept / reject / defer, then injects accepted gaps into
    the graph:

    - mechanism      → sets Claim.mechanism on target_claim_id
    - quantitative   → appends quote to Evidence on target_claim_id
    - arithmetic
    - named_scholar
    - named_example
    - analytical_move / structural → logged for manual handling

    Decisions persist to the JSON report so re-runs skip already-decided
    gaps. The graph is saved after the pass; nothing is silently
    revised.
    """
    project = _require_project(project)
    voice_obj = _load_voice(project, voice)

    from ..auditor.source_gap_apply import (
        apply_report,
        log_decisions,
        save_decisions,
    )
    from ..auditor.source_gap_review import load_report

    report = load_report(project, voice_obj.name)
    if report is None:
        console.print(
            f"[red]No source-gap report found for voice {voice_obj.name!r}. "
            f"Run `lattice source-review` first.[/red]"
        )
        raise typer.Exit(code=3)

    store = GraphStore.load(project)
    claim_lookup = {c.claim_id: c.statement for c in store.get_graph().claims}

    category_filter: set[str] | None = None
    if only_categories:
        category_filter = {c.strip() for c in only_categories.split(",") if c.strip()}

    pending = [
        g for g in report.gaps
        if g.decision is None
        and (category_filter is None or g.category in category_filter)
    ]
    if not pending:
        console.print(f"[yellow]No undecided gaps to apply.[/yellow]")
        raise typer.Exit(code=0)

    console.print(
        f"[cyan]Walking {len(pending)} undecided gap(s) "
        f"(of {len(report.gaps)} total).[/cyan]"
    )
    console.print()

    if not interactive and accept_all_with_targets:
        # Batch path: accept everything that has a target.
        for gap in pending:
            if gap.target_claim_id and gap.target_claim_id in claim_lookup:
                gap.decision = "accepted"
            else:
                gap.decision = "deferred"
        accepted = sum(1 for g in pending if g.decision == "accepted")
        console.print(
            f"[cyan]Batch: accepted {accepted}, deferred {len(pending) - accepted}.[/cyan]"
        )
    elif not interactive:
        console.print(
            "[red]--batch requires --accept-all-with-targets in this version.[/red]"
        )
        raise typer.Exit(code=2)
    else:
        for i, gap in enumerate(pending, start=1):
            target_summary = (
                claim_lookup.get(gap.target_claim_id, "(target not in graph)")[:120]
                if gap.target_claim_id
                else "(no target)"
            )
            console.print(f"[bold cyan]Gap {i}/{len(pending)}[/bold cyan] "
                          f"[{gap.category}]")
            console.print(f"  summary: {gap.summary}")
            console.print(f"  reference: [italic]{gap.reference_snippet[:300]}[/italic]")
            console.print(f"  suggested action: {gap.suggested_action or '(none)'}")
            console.print(f"  target: [yellow]{gap.target_claim_id or '(none)'}[/yellow] "
                          f"— {target_summary}")
            choice = typer.prompt(
                "  [a]ccept / [r]eject / [d]efer / [s]kip remaining",
                default="d",
            ).strip().lower()
            if choice in ("a", "accept"):
                gap.decision = "accepted"
            elif choice in ("r", "reject"):
                gap.decision = "rejected"
            elif choice in ("s", "skip"):
                console.print("[yellow]Skipping remainder.[/yellow]")
                break
            else:
                gap.decision = "deferred"
            console.print()

    # Apply accepted gaps to the graph.
    results = apply_report(report, store)
    log_decisions(report, project, voice_obj.name, results)
    save_decisions(report, project, voice_obj.name)

    # Summary.
    from collections import Counter
    counts = Counter(r.action for r in results)
    table = Table(title="Apply pass results")
    table.add_column("action", style="cyan")
    table.add_column("count", justify="right")
    for action, n in counts.most_common():
        table.add_row(action, str(n))
    console.print(table)
    console.print(
        f"[green]Decisions logged. Re-run `lattice render --force` to use the "
        f"updated graph.[/green]"
    )


@app.command()
def consistency(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
    threshold: float = typer.Option(0.35, "--threshold"),
) -> None:
    """Re-render edited clusters and flag any that have drifted from voice. Requires API key."""
    project = _require_project(project)
    config = Config.load(project)
    _require_claude()
    store = GraphStore.load(project)
    voice_obj = _load_voice(project, voice)
    llm = ClaudeClient(
        api_key=config.api_key,
        default_model=config.default_model,
        parallel=config.parallel_renders,
    )
    check = VoiceConsistencyCheck(config, store, llm, voice_obj, drift_threshold=threshold)
    drifted = asyncio.run(check.check_all_edited())
    if not drifted:
        console.print("[green]All edited clusters within voice similarity threshold.[/green]")
        return
    table = Table(title=f"Drifted clusters (threshold={threshold})")
    table.add_column("cluster_id", style="cyan")
    table.add_column("similarity", justify="right")
    for cluster, sim in drifted:
        table.add_row(cluster.cluster_id, f"{sim:.3f}")
    console.print(table)


@app.command()
def run(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
    with_shadow: bool = typer.Option(False, "--with-shadow", help="Include shadow mapping stage."),
    resume: bool = typer.Option(False, "--resume", help="Continue the latest run from its last completed stage."),
    max_passes: int = typer.Option(3, "--max-passes", help="Max auto-fix passes after audit."),
    min_delta: int = typer.Option(5, "--min-delta", help="Stop auto-fix loop if flag drop < this between passes."),
    review: bool = typer.Option(False, "--review", help="Pause after annotation so you can inspect structure/outline.annotated.md."),
) -> None:
    """Hands-free pipeline: annotate -> ingest -> index -> enrich -> plan ->
    render -> audit -> auto-fix loop -> DOCX with unresolved flags as comments.

    By default runs without pausing. Use `--review` to pause after annotation.
    `--with-shadow` adds shadow+differ between enrich and plan.
    """
    project = _require_project(project)
    runner = PipelineRunner(
        project, voice,
        with_shadow=with_shadow,
        review=review,
        max_passes=max_passes,
        min_delta=min_delta,
        console=console,
    )
    state = asyncio.run(runner.run_full(resume=resume))
    completed = sum(1 for v in state.stage_status.values() if v == StageStatus.completed)
    console.print(f"[green]Run {state.run_id}: {completed} stage(s) completed.[/green]")


@app.command()
def annotate(
    project: Path = typer.Argument(...),
) -> None:
    """Annotate the raw scaffold and write structure/outline.annotated.md.

    Runs the contextual annotator (thesis extraction, section roles, claim
    roles, inline citation mapping). The annotated file is plain markdown
    so you can open it, review what the LLM inferred, and edit anything
    you disagree with before running the rest of the pipeline.
    """
    project = _require_project(project)
    config = Config.load(project)
    store = GraphStore.load(project)

    structure_dir = project / "structure"
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
        console.print(f"[red]No raw outline in {structure_dir}[/red]")
        raise typer.Exit(code=3)
    raw = raw_candidates[0]

    llm = None
    if claude_available():
        try:
            llm = ClaudeClient(default_model=config.default_model, parallel=config.parallel_renders)
        except Exception:
            llm = None
    if llm is None:
        console.print("[yellow]Claude CLI not available. Running annotator in deterministic-only mode (citation regex only).[/yellow]")

    if raw.suffix.lower() == ".docx":
        ingester: object = DOCXOutlineIngester(config)
    else:
        ingester = MarkdownOutlineIngester(config)
    graph = asyncio.run(ingester.ingest(raw, project_name=project.name))

    from ..graph.serialize_outline import write_annotated_outline
    known_sources = {s.source_id for s in store.list_sources()}
    annotator = ContextualAnnotator(config, llm)
    graph = asyncio.run(annotator.annotate(graph, known_source_ids=known_sources))
    out_path = write_annotated_outline(graph, project)
    # Persist the annotated graph (with inferred relationships) so downstream
    # commands like `lattice graph` see the structure without a re-ingest.
    store.save_graph(graph)

    refs = sum(1 for s in graph.sections if s.role.value == "references")
    roles = sum(1 for c in graph.claims if any(t.startswith("role:") for t in c.tags))
    user_synth = sum(1 for c in graph.claims if c.type.value == "user_synthesis")
    rels = len(graph.relationships)
    console.print(
        f"[green]Annotated {raw.name} -> {out_path.name}: "
        f"{len(graph.sections)} sections ({refs} references), "
        f"{len(graph.claims)} claims ({roles} with roles, {user_synth} user_synthesis), "
        f"{rels} relationships inferred.[/green]"
    )
    console.print(f"Review and edit {out_path} before running `lattice run`.")


@app.command(name="run-clean")
def run_clean(
    project: Path = typer.Argument(...),
    voice: str = typer.Option(..., "--voice", "-v"),
) -> None:
    """Discard .lattice/cache/ and rerun the full pipeline from scratch."""
    project = _require_project(project)
    cache_dir = project / ".lattice" / "cache"
    if cache_dir.exists():
        import shutil
        shutil.rmtree(cache_dir)
        console.print(f"[yellow]Cleared {cache_dir}.[/yellow]")
    runner = PipelineRunner(project, voice, with_shadow=False, console=console)
    state = asyncio.run(runner.run_full(resume=False))
    console.print(f"[green]Clean run {state.run_id} finished.[/green]")


# ─── Voice sub-commands ──────────────────────────────────

voices_app = typer.Typer(name="voices", help="Voice file management.")
app.add_typer(voices_app, name="voices")


@voices_app.command("list")
def voices_list(project: Path = typer.Argument(...)) -> None:
    """Show available voices in voices/."""
    project = _require_project(project)
    voices_dir = project / "voices"
    if not voices_dir.exists():
        console.print(f"[red]No voices/ directory in {project}[/red]")
        raise typer.Exit(code=3)
    files = sorted(voices_dir.glob("*.voice.md"))
    if not files:
        console.print("[yellow]No voice files found.[/yellow]")
        return
    table = Table(title="Voices")
    table.add_column("name", style="cyan")
    table.add_column("architecture")
    table.add_column("description")
    for path in files:
        try:
            v = Voice.from_file(path)
            table.add_row(v.name, v.architecture.template, v.description[:80])
        except Exception as exc:
            table.add_row(path.stem, "(parse error)", str(exc)[:80])
    console.print(table)


@voices_app.command("new")
def voices_new(project: Path = typer.Argument(...), name: str = typer.Argument(...)) -> None:
    """Scaffold a new voice file from the academic template."""
    project = _require_project(project)
    dest = project / "voices" / f"{name}.voice.md"
    if dest.exists():
        console.print(f"[red]{dest} already exists.[/red]")
        raise typer.Exit(code=3)

    # Copy the academic voice as a starting point.
    src_path = project / "voices" / "academic.voice.md"
    if src_path.exists():
        dest.write_text(
            src_path.read_text(encoding="utf-8").replace(
                "name: academic", f"name: {name}", 1
            ),
            encoding="utf-8",
        )
    else:
        dest.write_text(
            f"---\nname: {name}\ndescription: TODO\n---\n", encoding="utf-8"
        )
    console.print(f"[green]Created {dest}[/green]")


@voices_app.command("validate")
def voices_validate(file: Path = typer.Argument(...)) -> None:
    """Check a voice file for errors."""
    if not file.exists():
        console.print(f"[red]Not found: {file}[/red]")
        raise typer.Exit(code=3)
    try:
        voice = Voice.from_file(file)
    except Exception as exc:
        console.print(f"[red]Parse error: {exc}[/red]")
        raise typer.Exit(code=3)
    issues = voice.validate_self()
    if not issues:
        console.print(f"[green]{file}: valid.[/green]")
        return
    console.print(f"[yellow]{file}: {len(issues)} issue(s):[/yellow]")
    for issue in issues:
        console.print(f"  - {issue}")
    raise typer.Exit(code=3)


# ─── Misc ────────────────────────────────────────────────


@app.command()
def diff(
    project: Path = typer.Argument(...),
    before: str = typer.Argument(...),
    after: str = typer.Argument(...),
) -> None:
    """Show graph changes between two versions."""
    raise NotImplementedError


@app.command()
def resume(project: Path = typer.Argument(...)) -> None:
    """Resume the latest run from its last completed stage (uses last run's voice)."""
    project = _require_project(project)
    manager = ResumeManager(project)
    latest = manager.latest_run()
    if latest is None:
        console.print("[red]No prior run to resume.[/red]")
        raise typer.Exit(code=3)
    if not latest.voice:
        console.print("[red]Latest run has no voice recorded; invoke `lattice run` manually.[/red]")
        raise typer.Exit(code=3)
    runner = PipelineRunner(project, latest.voice, console=console)
    state = asyncio.run(runner.run_full(resume=True))
    completed = sum(1 for v in state.stage_status.values() if v == StageStatus.completed)
    console.print(f"[green]Resumed run {state.run_id}: {completed} stage(s) completed.[/green]")


@app.command()
def graph(
    project: Path = typer.Argument(...),
    show: bool = typer.Option(True, "--show/--no-show", help="Print Rich tree to terminal."),
    mermaid: bool = typer.Option(True, "--mermaid/--no-mermaid", help="Write outputs/argument_graph.mmd."),
    html_view: bool = typer.Option(True, "--html/--no-html", help="Write outputs/argument_graph.html."),
) -> None:
    """Visualise the argument scaffold three ways: terminal tree, Mermaid, HTML."""
    project = _require_project(project)
    store = GraphStore.load(project)
    g = store.get_graph()
    clusters = store.list_clusters()
    if show:
        render_tree(g, clusters=clusters, console=console)
    paths_written: list[Path] = []
    if mermaid:
        paths_written.append(write_mermaid(g, project))
    if html_view:
        paths_written.append(write_html(g, project))
    for p in paths_written:
        console.print(f"[green]wrote {p}[/green]")


@app.command()
def export(
    project: Path = typer.Argument(...),
    to: str = typer.Option(..., "--to", help="Export format (currently: argus)."),
) -> None:
    """Export the working graph to an external format."""
    project = _require_project(project)
    if to != "argus":
        console.print(f"[red]Unknown export format: {to}[/red]")
        raise typer.Exit(code=3)
    store = GraphStore.load(project)
    graph = store.get_graph()
    out_path = project / "outputs" / f"{project.name}.argus.json"
    export_to_argus(graph, out_path)
    console.print(f"[green]Exported to {out_path}[/green]")


if __name__ == "__main__":
    app()
