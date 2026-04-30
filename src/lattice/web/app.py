"""FastAPI app: thin layer over the existing pipeline modules.

Endpoints:

- ``GET  /api/projects``                  list lattice projects under root
- ``GET  /api/projects/{name}``           project detail (cluster count, last render)
- ``POST /api/projects/{name}/runs``      kick off a new pipeline run; returns run_id
- ``WS   /api/projects/{name}/runs/{id}`` WebSocket for live progress events
- ``GET  /api/projects/{name}/paper``     last delivered paper (markdown)
- ``GET  /api/projects/{name}/audit``     audit flags JSON
- ``GET  /api/projects/{name}/source-gap`` source-gap review JSON

The static frontend is served at ``/`` from ``web/static/``.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__ as LATTICE_VERSION
from ..graph.store import GraphStore
from .runner import (
    EventQueueProgress,
    RunRequest,
    list_changelogs,
    list_projects,
    read_run_history,
    run_pipeline,
)


_STATIC_DIR = Path(__file__).parent / "static"


# ─── default outline template (used when creating a new project) ──

_DEFAULT_OUTLINE = """# THESIS

[State your thesis in one sentence. The rest of the paper develops this claim.]

# A. [First section heading]

  - [First claim] [ref: source_id]
  - MY VIEW: [Your synthesis claim] [user_synthesis]

# B. [Second section heading]

  - [Continue adding bullets per section. Tags: ref, supports, contradicts, role, weak/strong, mechanism]
"""


# ─── in-memory run registry ────────────────────────


class _RunState:
    """Tracks a running or finished pipeline so the WebSocket can attach
    after the run was kicked off via POST."""

    def __init__(self, run_id: str, request: RunRequest) -> None:
        self.run_id = run_id
        self.request = request
        self.events: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.finished = False

    async def feed_subscribers(self) -> None:
        """Drain the runner queue into the persisted events list. Forever
        until the task ends."""
        try:
            while not self.finished:
                event = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                self.events.append(event)
                if event.get("type") in ("run_finished", "run_failed"):
                    self.finished = True
                    break
        except TimeoutError:
            return  # caller will retry


_RUNS: dict[str, _RunState] = {}


# ─── request / response models ─────────────────────


class RunStartRequest(BaseModel):
    voice: str = "academic"
    level: str = "standard"  # quick | standard | deep
    reference_path: str | None = None
    max_passes: int = 3
    chunk_min: int = 3
    chunk_max: int = 4
    force: bool = False


class RunStartResponse(BaseModel):
    run_id: str
    project: str
    level: str


class CreateProjectRequest(BaseModel):
    name: str
    outline: str | None = None
    voice: str = "academic"
    ingest_now: bool = True


class CreateProjectResponse(BaseModel):
    name: str
    path: str
    section_count: int = 0
    claim_count: int = 0
    cluster_count: int = 0
    ingested: bool = False
    notes: list[str] = []


class ProjectPatch(BaseModel):
    display_name: str | None = None
    folder_name: str | None = None
    category: str | None = None
    position: float | None = None


class ReorderEntry(BaseModel):
    name: str
    category: str
    position: float


class ReorderRequest(BaseModel):
    order: list[ReorderEntry]


class ExtractRefsRequest(BaseModel):
    text: str | None = None
    source: str | None = None  # "outline" / "outline.raw" / "papers/foo.pdf"


class AcceptRefsRequest(BaseModel):
    citations: list[dict[str, Any]]


# ─── app factory ───────────────────────────────────


def get_projects_root() -> Path:
    return Path(os.environ.get("LATTICE_PROJECTS_ROOT", str(Path.home() / "lattice")))


def _slugify_project_name(name: str) -> str:
    """Turn a human-readable project title into a filesystem-safe folder
    name. Preserves underscores and dashes; collapses runs of whitespace
    and punctuation into single underscores; strips edges; lowercases.

    Examples:
        'Extraneous factors in judicial decisions' → 'extraneous_factors_in_judicial_decisions'
        'My_Paper-v2'                             → 'my_paper-v2'
        'Smith & Jones (2024)'                    → 'smith_jones_2024'
    """
    import re as _re
    # Lowercase, replace anything that isn't alphanumeric or - or _ with a space.
    cleaned = _re.sub(r"[^A-Za-z0-9_\-]+", " ", name).strip().lower()
    # Collapse internal whitespace runs into single underscores.
    slug = _re.sub(r"\s+", "_", cleaned)
    # Trim leading/trailing underscores or dashes.
    slug = slug.strip("_-")
    # Cap length so the folder name stays usable on Windows.
    return slug[:80]


def _find_examples_dir() -> Path | None:
    """Locate the lattice repo's examples/ directory (for voice templates).
    Walks up from this file until it finds a sibling examples/ folder."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "examples"
        if (candidate / "voices" / "academic.voice.md").exists():
            return candidate
    return None


def create_app(projects_root: Path | None = None) -> FastAPI:
    """Build the FastAPI app. The projects root defaults to the
    ``LATTICE_PROJECTS_ROOT`` env var or ``~/lattice/``."""
    root = projects_root or get_projects_root()
    app = FastAPI(title="Lattice", version="0.1.0")

    def _project_path(name: str) -> Path:
        path = root / name
        if not path.exists() or not (path / ".lattice").exists():
            raise HTTPException(404, f"Project not found: {name}")
        return path

    # ─── projects ──────────────────────────────

    @app.get("/api/version")
    async def get_version() -> dict[str, str]:
        return {"version": LATTICE_VERSION}

    @app.get("/api/projects")
    async def get_projects() -> list[dict[str, Any]]:
        return await list_projects(root)

    @app.post("/api/projects")
    async def create_project(body: CreateProjectRequest) -> CreateProjectResponse:
        # Accept any human-readable name; slug to a safe folder name.
        # The original is preserved as the project_name on the graph
        # so it shows up on the dashboard and in render output.
        display_name = body.name.strip()
        if not display_name:
            raise HTTPException(400, "Project name cannot be empty.")
        if len(display_name) > 120:
            raise HTTPException(400, "Project name too long (max 120 chars).")
        folder_name = _slugify_project_name(display_name)
        if not folder_name:
            raise HTTPException(
                400,
                "Project name must contain at least one alphanumeric character.",
            )
        path = root / folder_name
        if path.exists():
            raise HTTPException(
                409,
                f"A project folder named {folder_name!r} already exists "
                f"(slugified from {display_name!r}).",
            )

        # Scaffold folder structure (matches `lattice init`).
        for sub in ["structure", "refs/papers", "refs/notes", "refs/data",
                    "refs/web", "refs/prior_writing", "voices", "figures"]:
            (path / sub).mkdir(parents=True, exist_ok=True)

        # Write config.yml.
        (path / "config.yml").write_text(
            "default_voice: academic\n"
            "default_model: sonnet\n"
            "parallel_renders: 4\n"
            "autocorrect: safe\n",
            encoding="utf-8",
        )

        # Copy academic voice from the package examples.
        examples_dir = _find_examples_dir()
        voice_target = path / "voices" / f"{body.voice}.voice.md"
        if examples_dir is not None:
            voice_src = examples_dir / "voices" / f"{body.voice}.voice.md"
            if voice_src.exists():
                voice_target.write_text(
                    voice_src.read_text(encoding="utf-8"), encoding="utf-8"
                )
            else:
                voice_target.write_text(
                    f"---\nname: {body.voice}\ndescription: TODO\n---\n",
                    encoding="utf-8",
                )
        else:
            voice_target.write_text(
                f"---\nname: {body.voice}\ndescription: TODO\n---\n",
                encoding="utf-8",
            )

        # Outline.
        outline_path = path / "structure" / "outline.md"
        outline_path.write_text(body.outline or _DEFAULT_OUTLINE, encoding="utf-8")

        # Persist the human-readable name alongside the slug so the
        # frontend can show it on cards and breadcrumbs without
        # forcing the slug into the UI.
        (path / ".lattice").mkdir(parents=True, exist_ok=True)
        (path / ".lattice" / "project_meta.json").write_text(
            json.dumps({
                "display_name": display_name,
                "folder_name": folder_name,
                "category": "Uncategorised",
                "position": 0.0,
            }, indent=2),
            encoding="utf-8",
        )

        result = CreateProjectResponse(name=folder_name, path=str(path.resolve()))
        if display_name != folder_name:
            result.notes.append(
                f"Saved to folder {folder_name!r} (slugified from {display_name!r})."
            )

        if body.ingest_now and (body.outline and body.outline.strip()):
            # Run ingest + plan in-process so the project is immediately
            # browsable. Catch errors so a malformed outline doesn't 500
            # the whole request — the project still exists.
            try:
                from ..ingester.markdown import MarkdownOutlineIngester
                from ..renderer.assembler import Assembler
                from ..utils.config import Config
                from ..voice.parser import Voice
                config = Config.load(path)
                ingester = MarkdownOutlineIngester(config)
                graph = await ingester.ingest(outline_path, project_name=display_name)
                store = GraphStore.load(path)
                store.save_graph(graph)
                voice = Voice.from_file(voice_target)
                clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
                result.section_count = len(graph.sections)
                result.claim_count = len(graph.claims)
                result.cluster_count = len(clusters)
                result.ingested = True
            except Exception as exc:  # noqa: BLE001
                result.notes.append(
                    f"Project created but ingest failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        elif body.ingest_now:
            result.notes.append(
                "ingest_now requested but no outline body supplied; project created empty."
            )

        return result

    @app.get("/api/projects/{name}")
    async def get_project(name: str) -> dict[str, Any]:
        path = _project_path(name)
        graph_path = path / ".lattice" / "author_graph.json"
        cluster_plan_path = path / ".lattice" / "cluster_plan.json"
        if not graph_path.exists():
            # Project scaffolded but not yet ingested. Return a minimal
            # detail payload so the UI doesn't 404 right after creation.
            meta_path = path / ".lattice" / "project_meta.json"
            display_name = name
            if meta_path.exists():
                try:
                    display_name = json.loads(meta_path.read_text(encoding="utf-8")).get("display_name") or name
                except json.JSONDecodeError:
                    pass
            return {
                "name": name,
                "display_name": display_name,
                "path": str(path),
                "thesis_statement": None,
                "thesis_argued": None,
                "thesis_argued_confidence": None,
                "section_count": 0,
                "claim_count": 0,
                "relationship_count": 0,
                "cluster_count": 0,
                "voices": sorted(
                    v.stem.replace(".voice", "")
                    for v in (path / "voices").glob("*.voice.md")
                ),
                "paper_exists": False,
                "paper_words": 0,
                "ingested": False,
            }
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        clusters = []
        if cluster_plan_path.exists():
            try:
                clusters = json.loads(cluster_plan_path.read_text(encoding="utf-8"))
                if isinstance(clusters, dict):
                    clusters = clusters.get("clusters", [])
            except json.JSONDecodeError:
                clusters = []
        voices = sorted(
            v.stem.replace(".voice", "")
            for v in (path / "voices").glob("*.voice.md")
        )
        paper_path = path / "outputs" / "paper.academic.md"
        # Resolve display name (project_meta.json wins, then graph.project_name).
        meta_path = path / ".lattice" / "project_meta.json"
        display_name = name
        if meta_path.exists():
            try:
                display_name = json.loads(meta_path.read_text(encoding="utf-8")).get("display_name") or name
            except json.JSONDecodeError:
                pass
        elif graph.get("project_name"):
            display_name = graph["project_name"]
        return {
            "name": name,
            "display_name": display_name,
            "path": str(path),
            "thesis_statement": graph.get("thesis_statement"),
            "thesis_argued": graph.get("thesis_argued"),
            "thesis_argued_confidence": graph.get("thesis_argued_confidence"),
            "section_count": len(graph.get("sections", [])),
            "claim_count": len(graph.get("claims", [])),
            "relationship_count": len(graph.get("relationships", [])),
            "cluster_count": len(clusters),
            "voices": voices,
            "paper_exists": paper_path.exists(),
            "paper_words": (
                len(paper_path.read_text(encoding="utf-8").split())
                if paper_path.exists() else 0
            ),
        }

    # ─── project mutation: rename, recategorise, reorder, delete ──

    def _read_meta(path: Path) -> dict[str, Any]:
        meta_path = path / ".lattice" / "project_meta.json"
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {}

    def _write_meta(path: Path, meta: dict[str, Any]) -> None:
        (path / ".lattice").mkdir(parents=True, exist_ok=True)
        (path / ".lattice" / "project_meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

    @app.patch("/api/projects/{name}")
    async def patch_project(name: str, body: ProjectPatch) -> dict[str, Any]:
        """Update display_name, folder_name, category, or position on a
        single project. Any field left None is left untouched. Renaming
        the folder physically moves the directory on disk and the
        project's URL changes to the new folder name (returned as
        ``name`` in the response)."""
        path = _project_path(name)
        meta = _read_meta(path)
        # Backfill folder_name so older projects (created before this
        # field existed) gain a meta record on first edit.
        meta.setdefault("folder_name", path.name)
        if body.display_name is not None:
            cleaned = body.display_name.strip()
            if not cleaned:
                raise HTTPException(400, "display_name cannot be empty")
            if len(cleaned) > 120:
                raise HTTPException(400, "display_name too long (max 120 chars)")
            meta["display_name"] = cleaned
        if body.category is not None:
            cat = body.category.strip()
            if len(cat) > 60:
                raise HTTPException(400, "category too long (max 60 chars)")
            meta["category"] = cat or "Uncategorised"
        if body.position is not None:
            meta["position"] = float(body.position)

        new_path = path
        if body.folder_name is not None:
            requested = body.folder_name.strip()
            if not requested:
                raise HTTPException(400, "folder_name cannot be empty")
            slug = _slugify_project_name(requested)
            if not slug:
                raise HTTPException(
                    400,
                    "folder_name must contain at least one alphanumeric character",
                )
            if slug != requested:
                raise HTTPException(
                    400,
                    f"folder_name must be a slug (lowercase, [a-z0-9_-]); "
                    f"got {requested!r}, expected {slug!r}",
                )
            if slug != path.name:
                target = root / slug
                if target.exists():
                    raise HTTPException(
                        409, f"A folder named {slug!r} already exists."
                    )
                # Block the rename if a run is currently in flight for
                # this project — the runner holds the old path open and
                # would write outputs into the orphaned directory.
                resolved = path.resolve()
                for run_state in _RUNS.values():
                    if run_state.finished:
                        continue
                    try:
                        if run_state.request.project_path.resolve() == resolved:
                            raise HTTPException(
                                409,
                                "Cannot rename folder while a run is active "
                                "for this project. Wait for it to finish.",
                            )
                    except OSError:
                        # resolve() can fail on a deleted path; ignore.
                        pass
                # Write the updated meta into the OLD folder first so
                # the rename carries the latest values atomically.
                meta["folder_name"] = slug
                _write_meta(path, meta)
                path.rename(target)
                new_path = target

        _write_meta(new_path, meta)
        return {
            "name": new_path.name,
            "display_name": meta.get("display_name", new_path.name),
            "folder_name": meta.get("folder_name", new_path.name),
            "category": meta.get("category", "Uncategorised"),
            "position": meta.get("position", 0.0),
        }

    @app.post("/api/projects/_reorder")
    async def reorder_projects(body: ReorderRequest) -> dict[str, Any]:
        """Bulk-update category + position across many projects in one
        request. Used after a drag-and-drop reorder so the whole list
        becomes consistent atomically."""
        updated: list[str] = []
        for entry in body.order:
            try:
                path = _project_path(entry.name)
            except HTTPException:
                continue  # silently drop unknown names so stale clients don't 500
            meta = _read_meta(path)
            meta.setdefault("folder_name", path.name)
            meta.setdefault("display_name", path.name)
            cat = entry.category.strip() or "Uncategorised"
            if len(cat) > 60:
                raise HTTPException(400, f"category too long for {entry.name}")
            meta["category"] = cat
            meta["position"] = float(entry.position)
            _write_meta(path, meta)
            updated.append(path.name)
        return {"updated": updated}

    @app.delete("/api/projects/{name}")
    async def delete_project(name: str) -> dict[str, Any]:
        """Soft-delete: move the project folder under
        ``<root>/.trash/<name>_<unix_ts>/`` so it can be recovered by
        hand. ``list_projects`` skips ``.trash``, so the project
        disappears from the UI."""
        import shutil
        from datetime import datetime as _dt
        path = _project_path(name)
        trash = root / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        target = trash / f"{path.name}_{ts}"
        # Avoid clobbering if a same-second deletion already happened.
        i = 1
        while target.exists():
            target = trash / f"{path.name}_{ts}_{i}"
            i += 1
        shutil.move(str(path), str(target))
        return {
            "deleted": path.name,
            "moved_to": str(target.resolve()),
        }

    # ─── runs ──────────────────────────────────

    @app.post("/api/projects/{name}/runs")
    async def start_run(name: str, body: RunStartRequest) -> RunStartResponse:
        path = _project_path(name)
        if body.level not in ("quick", "standard", "deep"):
            raise HTTPException(400, f"Unknown level: {body.level}")
        ref_path = Path(body.reference_path) if body.reference_path else None
        if ref_path and not ref_path.exists():
            raise HTTPException(400, f"Reference file not found: {ref_path}")

        run_id = uuid.uuid4().hex[:12]
        request = RunRequest(
            project_path=path,
            voice_name=body.voice,
            level=body.level,  # type: ignore[arg-type]
            reference_path=ref_path,
            max_passes=body.max_passes,
            chunk_min=body.chunk_min,
            chunk_max=body.chunk_max,
            force=body.force,
        )
        state = _RunState(run_id, request)
        _RUNS[run_id] = state

        progress = EventQueueProgress(state.queue)

        async def _drive() -> None:
            try:
                await run_pipeline(request, progress)
            except Exception as exc:  # noqa: BLE001
                import traceback
                tb = "".join(traceback.format_exception(exc))
                state.queue.put_nowait({
                    "type": "run_failed",
                    "reason": "exception",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "traceback": tb,
                })
            finally:
                state.finished = True

        state.task = asyncio.create_task(_drive())
        return RunStartResponse(run_id=run_id, project=name, level=body.level)

    @app.websocket("/api/projects/{name}/runs/{run_id}")
    async def run_events(websocket: WebSocket, name: str, run_id: str) -> None:
        await websocket.accept()
        state = _RUNS.get(run_id)
        if state is None:
            await websocket.send_json({"type": "run_failed", "reason": "run_not_found"})
            await websocket.close()
            return

        # Replay any events that arrived before the WebSocket connected.
        for event in list(state.events):
            await websocket.send_json(event)

        try:
            while True:
                # Drain the queue and persist.
                try:
                    event = await asyncio.wait_for(state.queue.get(), timeout=2.0)
                except TimeoutError:
                    if state.finished:
                        break
                    continue
                state.events.append(event)
                await websocket.send_json(event)
                if event.get("type") in ("run_finished", "run_failed"):
                    break
        except WebSocketDisconnect:
            return
        finally:
            try:
                await websocket.close()
            except RuntimeError:
                pass

    # ─── outputs ──────────────────────────────

    @app.get("/api/projects/{name}/paper")
    async def get_paper(name: str) -> PlainTextResponse:
        path = _project_path(name) / "outputs" / "paper.academic.md"
        if not path.exists():
            raise HTTPException(404, "No rendered paper.")
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/projects/{name}/audit")
    async def get_audit(name: str, voice: str = "academic") -> dict[str, Any]:
        """Return audit flags for the given voice. The on-disk file is
        keyed by voice name (`{academic: [flags], ...}`); we flatten to
        a list so the frontend can `.filter()` directly."""
        path = _project_path(name) / ".lattice" / "audit_flags.json"
        if not path.exists():
            return {"flags": [], "voice": voice, "available_voices": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"flags": [], "voice": voice, "available_voices": []}
        if isinstance(data, list):
            # Old-format file (flat list) — pass through unchanged.
            return {"flags": data, "voice": voice, "available_voices": []}
        if not isinstance(data, dict):
            return {"flags": [], "voice": voice, "available_voices": []}
        flags = data.get(voice, [])
        if not isinstance(flags, list):
            flags = []
        return {
            "flags": flags,
            "voice": voice,
            "available_voices": sorted(data.keys()),
        }

    @app.get("/api/projects/{name}/source-gap")
    async def get_source_gap(name: str, voice: str = "academic") -> dict[str, Any]:
        path = _project_path(name) / ".lattice" / f"source_gap_review.{voice}.json"
        if not path.exists():
            raise HTTPException(404, "No source-gap review.")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/projects/{name}/voice-review")
    async def get_voice_review(name: str, voice: str = "academic") -> PlainTextResponse:
        path = _project_path(name) / "outputs" / f"voice_review.{voice}.md"
        if not path.exists():
            raise HTTPException(404, "No voice review.")
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/projects/{name}/drafts")
    async def get_drafts(name: str) -> list[dict[str, Any]]:
        """List every paper variant under outputs/. Returns sorted by
        modification time, newest first. Each entry has filename,
        absolute path, mtime (epoch seconds), and word count."""
        path = _project_path(name)
        outputs = path / "outputs"
        if not outputs.exists():
            return []
        drafts: list[dict[str, Any]] = []
        for f in outputs.glob("paper.*.md"):
            stat = f.stat()
            text = f.read_text(encoding="utf-8")
            drafts.append({
                "filename": f.name,
                "path": str(f.resolve()),
                "mtime": stat.st_mtime,
                "size_bytes": stat.st_size,
                "word_count": len(text.split()),
                "is_current": f.name == "paper.academic.md",
            })
        drafts.sort(key=lambda d: -d["mtime"])
        return drafts

    @app.get("/api/projects/{name}/originals")
    async def get_original_paper(name: str) -> dict[str, Any]:
        """List 'original paper' artefacts living under structure/.
        These are the inputs the rendered drafts came from:

          - ``outline.original.pdf`` / ``.docx`` — the user-uploaded
            original (preserved when they upload via the new
            ``/structure/original`` endpoint).
          - ``outline.raw.md`` — extracted plain text archived during
            auto-structuring.
          - ``outline.md`` — the current lattice-format outline.

        Each entry includes a stable ``kind`` field the UI uses to
        decide whether to render inline (markdown) or offer a
        download (PDF/DOCX).
        """
        path = _project_path(name)
        structure_dir = path / "structure"
        if not structure_dir.exists():
            return {"originals": []}

        out: list[dict[str, Any]] = []
        # Map filename → (label, kind, role).
        candidates: list[tuple[str, str, str, str]] = [
            ("outline.original.pdf", "Original PDF",            "pdf",      "original_pdf"),
            ("outline.original.docx","Original DOCX",           "docx",     "original_docx"),
            ("outline.raw.md",       "Raw extracted text",      "markdown", "raw_text"),
            ("outline.md",           "Current outline",         "markdown", "current_outline"),
        ]
        for fname, label, kind, role in candidates:
            target = structure_dir / fname
            if not target.exists():
                continue
            stat = target.stat()
            entry = {
                "filename": fname,
                "label": label,
                "kind": kind,
                "role": role,
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
            }
            if kind == "markdown":
                try:
                    text = target.read_text(encoding="utf-8", errors="replace")
                    entry["word_count"] = len(text.split())
                except OSError:
                    entry["word_count"] = 0
            out.append(entry)
        return {"originals": out}

    @app.get("/api/projects/{name}/originals/{filename}")
    async def get_original_file(name: str, filename: str):
        """Serve a single original-paper file. Markdown / text →
        inline; PDF / DOCX → download. Path-traversal-safe."""
        path = _project_path(name)
        try:
            safe_name = _safe_filename(filename)
        except HTTPException as exc:
            raise exc
        # Whitelist: only specific outline.* names.
        allowed = {
            "outline.original.pdf", "outline.original.docx",
            "outline.raw.md", "outline.md",
        }
        if safe_name not in allowed:
            raise HTTPException(404, "not an original-paper artefact")
        target = (path / "structure" / safe_name).resolve()
        if not target.is_file():
            raise HTTPException(404, "file not found")
        ext = target.suffix.lower()
        if ext in (".md", ".markdown", ".txt"):
            return PlainTextResponse(
                target.read_text(encoding="utf-8", errors="replace"),
                media_type="text/markdown",
            )
        if ext == ".pdf":
            return FileResponse(str(target), media_type="application/pdf")
        if ext == ".docx":
            return FileResponse(
                str(target),
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        return FileResponse(str(target))

    @app.post("/api/projects/{name}/structure/original")
    async def upload_original_paper(
        name: str, file: UploadFile = File(...),
    ) -> dict[str, Any]:
        """Save the user's original paper file to
        ``structure/outline.original.<ext>``. Lets users retroactively
        attach the source PDF / DOCX to an existing project so it
        appears under Drafts → Original paper. Doesn't run ingest —
        the lattice outline still drives parsing."""
        path = _project_path(name)
        ext = Path(file.filename or "").suffix.lower()
        if ext not in (".pdf", ".docx"):
            raise HTTPException(
                400,
                f"Original file must be .pdf or .docx (got {ext!r}).",
            )
        contents = await file.read()
        if len(contents) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                400,
                f"File too large (limit "
                f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
            )
        structure_dir = path / "structure"
        structure_dir.mkdir(parents=True, exist_ok=True)
        target = structure_dir / f"outline.original{ext}"
        target.write_bytes(contents)
        return {
            "filename": target.name,
            "saved_to": str(target.resolve()),
            "size_bytes": len(contents),
        }

    @app.get("/api/projects/{name}/drafts/{filename}")
    async def get_draft(name: str, filename: str) -> PlainTextResponse:
        if "/" in filename or "\\" in filename or ".." in filename:
            raise HTTPException(400, "Invalid filename.")
        path = _project_path(name) / "outputs" / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(404, f"Draft not found: {filename}")
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    # ─── text extraction (used to seed outline editor from PDF/DOCX) ──

    @app.post("/api/extract-text")
    async def extract_text(file: UploadFile = File(...)) -> dict[str, Any]:
        """Extract plain text from an uploaded PDF or DOCX so the
        frontend can drop it into the outline editor for the user to
        massage into bullet form. Project-agnostic; the file is not
        persisted on the server."""
        ext = Path(file.filename or "").suffix.lower()
        if ext not in {".pdf", ".docx", ".md", ".markdown", ".txt"}:
            raise HTTPException(
                400,
                f"Unsupported extension {ext}. "
                f"Allowed: .pdf, .docx, .md, .txt.",
            )
        contents = await file.read()
        if len(contents) > 200 * 1024 * 1024:
            raise HTTPException(400, "File too large (200 MB limit).")

        if ext in {".md", ".markdown", ".txt"}:
            return {
                "text": contents.decode("utf-8", errors="replace"),
                "source_format": ext.lstrip("."),
                "char_count": len(contents),
            }

        if ext == ".pdf":
            try:
                import io
                from pypdf import PdfReader
                from ..ingester.pdf_to_markdown import pdf_text_to_markdown
                reader = PdfReader(io.BytesIO(contents))
                pages = []
                for page in reader.pages:
                    try:
                        pages.append(page.extract_text() or "")
                    except Exception:  # noqa: BLE001
                        pages.append("")
                # Heuristic markdown reconstruction: detects section
                # headings (numbered, all-caps, common labels), strips
                # repeated headers/footers + page numbers, rejoins
                # hyphenated line-breaks, marks figure / table captions.
                text = pdf_text_to_markdown(pages)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    400,
                    f"Could not parse PDF: {type(exc).__name__}: {exc}",
                ) from exc
            return {
                "text": text,
                "source_format": "pdf",
                "char_count": len(text),
                "word_count": len(text.split()),
                "page_count": len(reader.pages),
                "note": (
                    "Markdown reconstructed via pypdf + heuristic "
                    "formatting. Section headings, lists, and figure "
                    "captions are preserved where the layout was "
                    "regular; check tables / multi-column blocks by hand."
                ),
            }

        # ext == ".docx"
        try:
            import io
            from docx import Document  # type: ignore
            doc = Document(io.BytesIO(contents))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            text = "\n".join(paragraphs)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                400,
                f"Could not parse DOCX: {type(exc).__name__}: {exc}",
            ) from exc
        return {
            "text": text,
            "source_format": "docx",
            "char_count": len(text),
            "paragraph_count": len(paragraphs),
        }

    # ─── outline + source uploads ──────────────

    _ALLOWED_OUTLINE_EXT = {".md", ".markdown", ".txt", ".docx"}
    _ALLOWED_SOURCE_BUCKETS = {"papers", "notes", "data", "web", "prior_writing"}
    _ALLOWED_SOURCE_EXT = {
        ".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm", ".xlsx",
    }

    def _extract_pdf_to_sidecar(pdf_path: Path) -> tuple[Path | None, int, int]:
        """Extract text from ``pdf_path`` to ``<pdf_path>.txt``. Returns
        ``(sidecar_path, page_count, char_count)`` on success or
        ``(None, 0, 0)`` if extraction fails. Uses the markdown-aware
        PDF-to-markdown converter so the sidecar preserves section
        headings, lists, and figure captions where the original
        layout makes them detectable. Errors are swallowed so a
        broken PDF can't take down the upload."""
        try:
            from pypdf import PdfReader
            from ..ingester.pdf_to_markdown import pdf_text_to_markdown
            reader = PdfReader(str(pdf_path))
            pages: list[str] = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001
                    pages.append("")
            text = pdf_text_to_markdown(pages)
            if not text.strip():
                return None, len(reader.pages), 0
            sidecar = pdf_path.with_name(pdf_path.name + ".txt")
            sidecar.write_text(text, encoding="utf-8")
            return sidecar, len(reader.pages), len(text)
        except Exception:  # noqa: BLE001
            return None, 0, 0
    # 200 MB per file — academic PDFs with embedded images run 20-100 MB,
    # supplementary appendices can exceed that. Bumped from 50 MB.
    _MAX_UPLOAD_BYTES = 200 * 1024 * 1024

    def _safe_filename(filename: str) -> str:
        base = Path(filename).name
        # Strip path components and reject anything with path separators
        # left over after that.
        if "/" in base or "\\" in base or base.startswith(".."):
            raise HTTPException(400, f"Invalid filename: {filename}")
        return base

    @app.post("/api/projects/{name}/outline")
    async def upload_outline(
        name: str,
        file: UploadFile = File(...),
        ingest: bool = Form(True),
    ) -> dict[str, Any]:
        """Replace structure/outline.md (or .docx) with the uploaded
        file. Optionally re-runs ingest + plan in-process."""
        path = _project_path(name)
        ext = Path(file.filename or "").suffix.lower()
        if ext not in _ALLOWED_OUTLINE_EXT:
            raise HTTPException(
                400,
                f"Outline must be one of: {', '.join(sorted(_ALLOWED_OUTLINE_EXT))}.",
            )

        contents = await file.read()
        if len(contents) > _MAX_UPLOAD_BYTES:
            raise HTTPException(400, "File too large (limit 50 MB).")

        # Markdown/text → outline.md; docx → outline.docx. The ingester
        # auto-detects based on extension when ingest is run.
        target_name = "outline.docx" if ext == ".docx" else "outline.md"
        # If a previous outline of a different format existed, keep it
        # under outline.previous.<ext> so the user can revert.
        target = path / "structure" / target_name
        for existing in (path / "structure").glob("outline.*"):
            if existing.name in {"outline.md", "outline.docx"} and existing != target:
                existing.rename(
                    existing.with_name(f"outline.previous{existing.suffix}")
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)

        result: dict[str, Any] = {
            "saved_to": str(target),
            "size_bytes": len(contents),
            "ingested": False,
        }
        if ingest:
            try:
                from ..ingester.markdown import MarkdownOutlineIngester
                from ..ingester.docx import DOCXOutlineIngester
                from ..renderer.assembler import Assembler
                from ..utils.config import Config
                from ..voice.parser import Voice
                config = Config.load(path)
                if ext == ".docx":
                    ingester: Any = DOCXOutlineIngester(config)
                else:
                    ingester = MarkdownOutlineIngester(config)
                graph = await ingester.ingest(target, project_name=name)
                store = GraphStore.load(path)
                store.save_graph(graph)
                voice_path = next(
                    (path / "voices").glob("*.voice.md"), None
                )
                if voice_path is not None:
                    voice = Voice.from_file(voice_path)
                    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
                    result["cluster_count"] = len(clusters)
                result["ingested"] = True
                result["section_count"] = len(graph.sections)
                result["claim_count"] = len(graph.claims)
            except Exception as exc:  # noqa: BLE001
                result["ingest_error"] = f"{type(exc).__name__}: {exc}"
        return result

    @app.get("/api/projects/{name}/sources")
    async def list_sources(name: str) -> dict[str, Any]:
        """Return the contents of each refs/ subfolder + indexed
        source-store metadata (if available)."""
        path = _project_path(name)
        refs_dir = path / "refs"
        buckets: dict[str, list[dict[str, Any]]] = {}
        for bucket in _ALLOWED_SOURCE_BUCKETS:
            bucket_dir = refs_dir / bucket
            entries: list[dict[str, Any]] = []
            if bucket_dir.exists():
                # First pass: collect every file by name so we can attach
                # PDF→sidecar relationships in the second pass.
                files_in_bucket = {f.name: f for f in bucket_dir.iterdir() if f.is_file()}
                # Names of sidecar text files we're about to attach to a
                # PDF entry — drop them from the top-level listing so the
                # UI doesn't show them as separate items.
                attached_sidecars: set[str] = set()
                for f in sorted(files_in_bucket.values()):
                    if f.suffix.lower() == ".pdf":
                        sidecar_name = f.name + ".txt"
                        if sidecar_name in files_in_bucket:
                            attached_sidecars.add(sidecar_name)
                for f in sorted(files_in_bucket.values()):
                    if f.name in attached_sidecars:
                        continue
                    stat = f.stat()
                    entry: dict[str, Any] = {
                        "filename": f.name,
                        "size_bytes": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                    if f.suffix.lower() == ".pdf":
                        sidecar_name = f.name + ".txt"
                        sidecar = files_in_bucket.get(sidecar_name)
                        if sidecar is not None:
                            entry["text_sidecar"] = sidecar.name
                            entry["text_size_bytes"] = sidecar.stat().st_size
                    entries.append(entry)
            buckets[bucket] = entries

        # Indexed source metadata.
        source_store_path = path / ".lattice" / "source_store.json"
        indexed: list[dict[str, Any]] = []
        if source_store_path.exists():
            try:
                data = json.loads(source_store_path.read_text(encoding="utf-8"))
                for src in (data if isinstance(data, list) else data.get("sources", [])):
                    indexed.append({
                        "source_id": src.get("source_id"),
                        "type": src.get("type"),
                        "passage_count": len(src.get("passages", [])),
                    })
            except json.JSONDecodeError:
                pass

        return {"buckets": buckets, "indexed": indexed}

    @app.post("/api/projects/{name}/sources")
    async def upload_sources(
        name: str,
        bucket: str = Form("papers"),
        files: list[UploadFile] = File(...),
    ) -> dict[str, Any]:
        """Save uploaded files into refs/<bucket>/. Does NOT auto-index;
        the caller can hit POST /sources/index next."""
        path = _project_path(name)
        if bucket not in _ALLOWED_SOURCE_BUCKETS:
            raise HTTPException(
                400,
                f"bucket must be one of: {', '.join(sorted(_ALLOWED_SOURCE_BUCKETS))}",
            )
        target_dir = path / "refs" / bucket
        target_dir.mkdir(parents=True, exist_ok=True)

        saved: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        chunk_size = 1024 * 1024  # 1 MB chunks
        for upload in files:
            try:
                fname = _safe_filename(upload.filename or "")
            except HTTPException as exc:
                skipped.append({"filename": upload.filename or "", "reason": str(exc.detail)})
                continue
            ext = Path(fname).suffix.lower()
            if ext not in _ALLOWED_SOURCE_EXT:
                skipped.append({
                    "filename": fname,
                    "reason": f"unsupported extension {ext}",
                })
                continue

            # Stream to disk in chunks — large PDFs shouldn't sit in
            # memory waiting for the size check.
            target = target_dir / fname
            written = 0
            too_big = False
            with open(target, "wb") as fh:
                while True:
                    chunk = await upload.read(chunk_size)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _MAX_UPLOAD_BYTES:
                        too_big = True
                        break
                    fh.write(chunk)

            if too_big:
                # Clean up the partial write so we don't leave a half-file behind.
                try:
                    target.unlink()
                except OSError:
                    pass
                skipped.append({
                    "filename": fname,
                    "reason": f"file too large (limit {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
                })
                continue

            entry: dict[str, Any] = {
                "filename": fname,
                "bucket": bucket,
                "size_bytes": written,
                "path": str(target.resolve()),
            }

            # For PDFs, also extract a plaintext sidecar (<name>.pdf.txt) so
            # the rest of the tooling can read text without re-parsing the
            # binary. The PDF itself stays as the canonical archival copy.
            if ext == ".pdf":
                sidecar, page_count, char_count = _extract_pdf_to_sidecar(target)
                if sidecar is not None:
                    entry["text_sidecar"] = sidecar.name
                    entry["page_count"] = page_count
                    entry["text_char_count"] = char_count
                else:
                    entry["text_sidecar"] = None
                    entry["text_extraction_failed"] = True

            saved.append(entry)

        return {"saved": saved, "skipped": skipped}

    @app.get("/api/projects/{name}/sources/{bucket}/{filename}")
    async def get_source_file(name: str, bucket: str, filename: str):
        """Serve a single source file (PDF for download, .txt for inline
        viewing). Path-traversal-safe: filename is normalised and must
        resolve to a file directly inside ``refs/<bucket>/``."""
        path = _project_path(name)
        if bucket not in _ALLOWED_SOURCE_BUCKETS:
            raise HTTPException(404, "unknown bucket")
        try:
            safe_name = _safe_filename(filename)
        except HTTPException as exc:
            raise exc
        bucket_dir = (path / "refs" / bucket).resolve()
        target = (bucket_dir / safe_name).resolve()
        # Defence in depth: ensure resolved target is still under the bucket.
        try:
            target.relative_to(bucket_dir)
        except ValueError:
            raise HTTPException(400, "path escapes bucket")
        if not target.is_file():
            raise HTTPException(404, "file not found")

        ext = target.suffix.lower()
        if ext == ".txt" or target.name.endswith(".pdf.txt"):
            return PlainTextResponse(target.read_text(encoding="utf-8"))
        if ext == ".md" or ext == ".markdown":
            return PlainTextResponse(
                target.read_text(encoding="utf-8"),
                media_type="text/markdown",
            )
        if ext == ".pdf":
            return FileResponse(str(target), media_type="application/pdf")
        # Fallback: treat anything else as binary download.
        return FileResponse(str(target))

    @app.post("/api/projects/{name}/sources/index")
    async def index_sources(name: str) -> dict[str, Any]:
        """Run the source indexer over refs/. Updates source_store.json.
        Also backfills any missing PDF text sidecars so old uploads get
        the same plaintext companion that new ones do."""
        from ..indexer.base import SourceIndexer
        path = _project_path(name)
        store = GraphStore.load(path)

        # Backfill: every refs/**.pdf that doesn't already have a .pdf.txt
        # sibling gets one written now.
        sidecars_written = 0
        refs_dir = path / "refs"
        if refs_dir.exists():
            for pdf_path in refs_dir.rglob("*.pdf"):
                sidecar = pdf_path.with_name(pdf_path.name + ".txt")
                if sidecar.exists():
                    continue
                if _extract_pdf_to_sidecar(pdf_path)[0] is not None:
                    sidecars_written += 1

        indexer = SourceIndexer(path)
        indexed, skipped = indexer.index_all()
        for src in indexed:
            store.save_source(src)
        return {
            "indexed_count": len(indexed),
            "skipped_count": len(skipped),
            "sidecars_written": sidecars_written,
            "indexed": [
                {"source_id": s.source_id, "type": s.type.value,
                 "passage_count": len(s.passages)}
                for s in indexed
            ],
            "skipped": [str(p) for p in skipped],
        }

    @app.get("/api/projects/{name}/export/teaching-deck")
    async def export_teaching_deck(name: str):
        """Generate a teaching PowerPoint deck from the project's
        outline + claims. The deck has a title slide (project + thesis),
        a table-of-contents slide, and one slide per section with
        claim bullets and source citations in the speaker notes."""
        from datetime import datetime as _dt, timezone as _tz
        from ..output.powerpoint import write_teaching_deck
        path = _project_path(name)
        outputs = path / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)

        store = GraphStore.load(path)
        try:
            graph = store.get_graph()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                400,
                f"Could not load graph for {name}: {type(exc).__name__}: {exc}",
            )
        if not graph.sections:
            raise HTTPException(
                400,
                "Project has no sections. Add an outline and run a "
                "review before exporting a deck.",
            )

        meta = _read_meta(path)
        display_name = meta.get("display_name") or name
        timestamp = _dt.now(_tz.utc).strftime("%Y%m%d_%H%M%S")
        target = outputs / f"teaching_deck_{timestamp}.pptx"
        write_teaching_deck(
            graph, target,
            clusters=store.list_clusters(),
            project_name=display_name,
        )
        return FileResponse(
            str(target),
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            filename=target.name,
        )

    @app.get("/api/projects/{name}/graph-viz")
    async def get_graph_viz(name: str) -> FileResponse:
        """Return the interactive argument-graph HTML. Regenerates
        the cached file when:
          - it doesn't exist (first hit), OR
          - the underlying graph is newer (relationships changed), OR
          - the cached HTML was generated by a different lattice
            version (so cytoscape-config fixes propagate without
            requiring users to delete the file by hand).
        """
        path = _project_path(name)
        viz_path = path / "outputs" / "argument_graph.html"
        graph_path = path / ".lattice" / "author_graph.json"

        needs_regen = not viz_path.exists()
        if not needs_regen and graph_path.exists():
            if viz_path.stat().st_mtime < graph_path.stat().st_mtime:
                needs_regen = True
        # Version stamp embedded in the HTML — when the visualiser is
        # updated (e.g. a cytoscape-style fix), bumping LATTICE_VERSION
        # invalidates every project's cache automatically.
        version_stamp = f"<!-- lattice-viz-version: {LATTICE_VERSION} -->"
        if not needs_regen:
            try:
                first = viz_path.read_text(encoding="utf-8", errors="ignore")[:200]
                if version_stamp not in first:
                    needs_regen = True
            except OSError:
                needs_regen = True

        if needs_regen:
            from ..output.visualise import write_html as _write_html, write_mermaid
            store = GraphStore.load(path)
            graph = store.get_graph()
            _write_html(graph, path)
            # Prepend the version stamp so future regen checks can
            # detect a mismatched version cheaply.
            try:
                body = viz_path.read_text(encoding="utf-8")
                if version_stamp not in body[:200]:
                    viz_path.write_text(version_stamp + "\n" + body, encoding="utf-8")
            except OSError:
                pass
            try:
                write_mermaid(graph, path)
            except Exception:  # pragma: no cover — non-essential
                pass
        return FileResponse(str(viz_path), media_type="text/html")

    @app.get("/api/projects/{name}/outline-status")
    async def get_outline_status(name: str) -> dict[str, Any]:
        """Return a snapshot of the outline pipeline state for the
        Overview tab. Tells the user at a glance:

          - Is there an outline file?
          - Is it in lattice format (has `# THESIS` / `# A.` markers)?
          - Did a previous run save a raw archive (outline.raw.md)?
          - Does the saved graph have any sections + claims?
        """
        from ..ingester.auto_outliner import looks_like_lattice_outline
        path = _project_path(name)
        structure_dir = path / "structure"
        outline_md = structure_dir / "outline.md"
        outline_docx = structure_dir / "outline.docx"
        outline_raw = structure_dir / "outline.raw.md"
        graph_path = path / ".lattice" / "author_graph.json"

        outline: dict[str, Any] = {"exists": False}
        active = (
            outline_md if outline_md.exists()
            else outline_docx if outline_docx.exists()
            else None
        )
        if active is not None:
            stat = active.stat()
            outline = {
                "exists": True,
                "filename": active.name,
                "path": str(active.resolve()),
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "format": active.suffix.lower().lstrip("."),
            }
            if active.suffix.lower() in (".md", ".markdown", ".txt"):
                try:
                    text = active.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    text = active.read_text(encoding="utf-8", errors="replace")
                outline["is_structured"] = looks_like_lattice_outline(text)
                outline["preview"] = text[:600]
            else:
                # docx — assume structured (the docx ingester reads
                # styles), nothing to preview without parsing.
                outline["is_structured"] = True
                outline["preview"] = None

        graph_status: dict[str, Any] = {"exists": False}
        if graph_path.exists():
            try:
                data = json.loads(graph_path.read_text(encoding="utf-8"))
                graph_status = {
                    "exists": True,
                    "section_count": len(data.get("sections") or []),
                    "claim_count": len(data.get("claims") or []),
                    "mtime": graph_path.stat().st_mtime,
                }
            except json.JSONDecodeError:
                graph_status = {"exists": True, "corrupt": True}

        raw_archive: dict[str, Any] = {"exists": False}
        if outline_raw.exists():
            stat = outline_raw.stat()
            raw_archive = {
                "exists": True,
                "filename": outline_raw.name,
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
            }

        return {
            "outline": outline,
            "raw_archive": raw_archive,
            "graph": graph_status,
        }

    @app.get("/api/projects/{name}/changelogs")
    async def get_changelogs(name: str) -> dict[str, Any]:
        """List per-run changelog files. Each review writes one of
        these so the user can audit what a given review actually
        modified."""
        path = _project_path(name)
        return {"changelogs": list_changelogs(path)}

    @app.get("/api/projects/{name}/changelogs/{filename}")
    async def get_changelog(name: str, filename: str):
        """Return the markdown body of a specific changelog. Path-
        traversal-safe: filename must be a plain ``*.md`` name living
        directly under ``.lattice/changelogs/``."""
        path = _project_path(name)
        try:
            safe_name = _safe_filename(filename)
        except HTTPException as exc:
            raise exc
        if not safe_name.endswith(".md"):
            raise HTTPException(400, "filename must be a .md changelog")
        changelogs_dir = (path / ".lattice" / "changelogs").resolve()
        target = (changelogs_dir / safe_name).resolve()
        try:
            target.relative_to(changelogs_dir)
        except ValueError:
            raise HTTPException(400, "path escapes changelogs dir")
        if not target.is_file():
            raise HTTPException(404, "changelog not found")
        return PlainTextResponse(
            target.read_text(encoding="utf-8"), media_type="text/markdown"
        )

    @app.get("/api/projects/{name}/references")
    async def get_references(
        name: str,
        style: str = "harvard",
    ) -> dict[str, Any]:
        """Return the complete references manifest for the project.

        Each entry includes raw citation data (authors, year, title,
        etc.), a short 'about' summary, every claim that cites this
        source, and the citation pre-formatted in the requested
        style. The UI can switch styles by re-requesting with a
        different ``style`` query param — no LLM round-trip needed.
        """
        from ..output.references_manifest import build_references_manifest
        from ..output.citation_formatter import supported_styles
        path = _project_path(name)

        if style not in supported_styles():
            raise HTTPException(
                400,
                f"Unsupported citation style {style!r}. "
                f"Supported: {', '.join(supported_styles())}",
            )

        # Load any per-source 'about' overrides the user may have
        # written by hand. Stored under .lattice/reference_notes.json.
        overrides: dict[str, str] = {}
        notes_path = path / ".lattice" / "reference_notes.json"
        if notes_path.exists():
            try:
                data = json.loads(notes_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    overrides = {
                        k: str(v) for k, v in data.items() if isinstance(v, str)
                    }
            except json.JSONDecodeError:
                pass

        return build_references_manifest(
            path, style=style, summary_overrides=overrides,
        )

    @app.post("/api/projects/{name}/references/manual")
    async def add_reference_manually(
        name: str, body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Create a Source from hand-entered citation metadata. The
        resulting source has no indexed passages — it's purely a
        bibliographic record that can be cited by claims via Evidence
        bindings later."""
        from ..enricher.reference_extraction import (
            citation_to_synthetic_source,
        )
        from ..graph.models import Citation, SourceType
        path = _project_path(name)
        try:
            citation = Citation(
                authors=[
                    str(a).strip()
                    for a in (body.get("authors") or [])
                    if isinstance(a, str) and a.strip()
                ],
                year=(int(body["year"]) if body.get("year") not in (None, "") else None),
                title=str(body.get("title") or "").strip() or "(untitled)",
                container=(body.get("container") or None) or None,
                volume=(body.get("volume") or None) or None,
                issue=(body.get("issue") or None) or None,
                pages=(body.get("pages") or None) or None,
                doi=(body.get("doi") or None) or None,
                url=(body.get("url") or None) or None,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"invalid citation payload: {exc}")
        type_str = (body.get("type") or "primary_paper").strip()
        try:
            source_type = SourceType(type_str)
        except ValueError:
            raise HTTPException(
                400,
                f"unknown source type {type_str!r}. "
                f"Allowed: {', '.join(t.value for t in SourceType)}",
            )

        store = GraphStore.load(path)
        existing_ids = {s.source_id for s in store.list_sources()}
        source = citation_to_synthetic_source(citation, source_type=source_type)
        # De-duplicate: if the auto slug already exists, append a counter.
        base_id = source.source_id
        counter = 2
        while source.source_id in existing_ids:
            source.source_id = f"{base_id}_{counter}"
            counter += 1
        store.save_source(source)

        # Re-write the persisted references file so it includes the
        # new entry without waiting for the next review.
        try:
            from ..output.references_manifest import write_project_references
            write_project_references(path, cited_only=False)
        except Exception:  # noqa: BLE001
            pass

        return {
            "source_id": source.source_id,
            "saved": True,
            "citation": citation.model_dump(),
        }

    @app.post("/api/projects/{name}/references/extract")
    async def preview_reference_extraction(
        name: str, body: ExtractRefsRequest,
    ) -> dict[str, Any]:
        """LLM-extract structured citations from raw text or a
        project-local source. Returns the parsed citations as a
        preview — the user reviews, then accepts via the
        ``/extract/accept`` endpoint."""
        from ..enricher.reference_extraction import (
            extract_citations_from_text, read_text_for_extraction,
        )
        from ..utils.config import Config
        from ..utils.llm import ClaudeClient, claude_available
        path = _project_path(name)

        text = body.text or ""
        source_label = "raw text"
        if body.source:
            text = read_text_for_extraction(path, body.source)
            source_label = body.source
            if not text:
                raise HTTPException(
                    400,
                    f"no text found at {body.source!r} (is the file in refs/ or structure/?)",
                )
        if not text.strip():
            raise HTTPException(400, "no text supplied")

        if not claude_available():
            raise HTTPException(
                503,
                "Claude CLI not available — needed to extract structured "
                "citations from the bibliography text.",
            )

        config = Config.load(path)
        llm = ClaudeClient(
            default_model=config.default_model,
            parallel=config.parallel_renders,
        )
        try:
            citations = await extract_citations_from_text(text, llm)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                500,
                f"extraction raised {type(exc).__name__}: {exc}",
            )
        return {
            "source_label": source_label,
            "extracted_count": len(citations),
            "citations": [c.model_dump() for c in citations],
        }

    @app.post("/api/projects/{name}/references/extract/accept")
    async def accept_extracted_references(
        name: str, body: AcceptRefsRequest,
    ) -> dict[str, Any]:
        """Persist a previewed list of extracted citations as Sources.
        De-duplicates by source_id."""
        from ..enricher.reference_extraction import (
            citation_to_synthetic_source,
        )
        from ..graph.models import Citation
        path = _project_path(name)
        store = GraphStore.load(path)
        existing_ids = {s.source_id for s in store.list_sources()}
        added: list[str] = []
        skipped: list[str] = []
        for raw in body.citations:
            try:
                citation = Citation(**{
                    k: raw.get(k) for k in (
                        "authors", "year", "title", "container",
                        "volume", "issue", "pages", "doi", "url",
                    )
                })
            except (TypeError, ValueError) as exc:
                skipped.append(f"{raw.get('title', '?')}: {exc}")
                continue
            source = citation_to_synthetic_source(citation)
            base_id = source.source_id
            counter = 2
            while source.source_id in existing_ids:
                source.source_id = f"{base_id}_{counter}"
                counter += 1
            store.save_source(source)
            existing_ids.add(source.source_id)
            added.append(source.source_id)

        # Re-write the persisted references file so the new entries
        # show up immediately without waiting for the next review.
        try:
            from ..output.references_manifest import write_project_references
            write_project_references(path, cited_only=False)
        except Exception:  # noqa: BLE001
            pass

        return {"added": added, "skipped": skipped}

    @app.post("/api/projects/{name}/references/refresh-ai")
    async def refresh_references_ai(
        name: str, body: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        """Run AI enrichment over the project's references. For each
        source the LLM returns a structured record with: a 2-3 sentence
        summary, key findings, the work's standing in its field, an
        estimated citation count + confidence, and per-claim usage
        purposes. Persisted to ``.lattice/reference_enrichment.json``.

        Body: ``{"cited_only": bool}`` — defaults to True (only enrich
        sources actually cited by claims). Pass False to enrich
        every indexed source."""
        from ..enricher.reference_ai_enrichment import (
            enrich_all_references, save_enrichment,
        )
        from ..output.references_manifest import write_project_references
        from ..utils.config import Config
        from ..utils.llm import ClaudeClient, claude_available

        path = _project_path(name)
        cited_only = bool(body.get("cited_only", True))

        if not claude_available():
            raise HTTPException(
                503,
                "Claude CLI not available — needed to enrich references.",
            )

        store = GraphStore.load(path)
        sources = store.list_sources()
        if not sources:
            raise HTTPException(
                400,
                "Project has no indexed sources. Add references via "
                "'+ Add reference manually' or 'Extract from raw paper' "
                "first.",
            )

        config = Config.load(path)
        llm = ClaudeClient(
            default_model=config.default_model,
            parallel=config.parallel_renders,
        )
        graph = store.get_graph()
        try:
            enrichment, errors = await enrich_all_references(
                sources, graph, llm, cited_only=cited_only,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                500,
                f"AI enrichment raised {type(exc).__name__}: {exc}",
            )

        if enrichment:
            save_enrichment(path, enrichment)

        # Re-write the persisted references files so the new
        # enrichment shows up in references.md / references.json.
        try:
            write_project_references(path, cited_only=cited_only)
        except Exception:  # noqa: BLE001
            pass

        attempted = (
            len([s for s in sources if not cited_only or any(
                ev.source == s.source_id
                for c in graph.claims for ev in c.evidence
            )])
        )
        return {
            "enriched_count": len(enrichment),
            "attempted_count": attempted,
            "failed_count": len(errors),
            "source_ids": list(enrichment.keys()),
            "errors": [
                {"source_id": sid, "error": msg}
                for sid, msg in errors.items()
            ],
        }

    @app.post("/api/projects/{name}/references/save")
    async def save_references_files(
        name: str, body: dict[str, Any] = Body(default_factory=dict),
    ) -> dict[str, Any]:
        """Force-write the references file (``references.json`` and
        ``references.md``) to the project root. Normally happens
        automatically at the end of every review, but this lets the
        user trigger a refresh after editing a source's 'about'
        summary or after manual graph edits.

        Body: ``{"cited_only": bool}`` — defaults to True (only
        sources actually cited in this paper). Pass False to dump
        every indexed source.
        """
        from ..output.references_manifest import write_project_references
        path = _project_path(name)
        cited_only = bool(body.get("cited_only", True))

        # Reuse the user-saved 'about' overrides so manual edits flow
        # into the persisted file.
        overrides: dict[str, str] = {}
        notes_path = path / ".lattice" / "reference_notes.json"
        if notes_path.exists():
            try:
                data = json.loads(notes_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    overrides = {
                        k: str(v) for k, v in data.items()
                        if isinstance(v, str)
                    }
            except json.JSONDecodeError:
                pass

        paths = write_project_references(
            path, summary_overrides=overrides, cited_only=cited_only,
        )
        return {
            "saved": True,
            "json_path": str(paths["json"].resolve()),
            "md_path": str(paths["md"].resolve()),
            "cited_only": cited_only,
        }

    @app.get("/api/projects/{name}/references-file")
    async def get_references_file(name: str, fmt: str = "md"):
        """Serve the persisted references file. ``fmt=md`` (default)
        returns the human-readable markdown; ``fmt=json`` returns the
        structured manifest."""
        path = _project_path(name)
        if fmt == "md":
            target = path / "references.md"
            if not target.exists():
                raise HTTPException(404, "references.md not generated yet")
            return PlainTextResponse(
                target.read_text(encoding="utf-8"),
                media_type="text/markdown",
            )
        if fmt == "json":
            target = path / "references.json"
            if not target.exists():
                raise HTTPException(404, "references.json not generated yet")
            return PlainTextResponse(
                target.read_text(encoding="utf-8"),
                media_type="application/json",
            )
        raise HTTPException(400, "fmt must be 'md' or 'json'")

    @app.put("/api/projects/{name}/references/{source_id}/about")
    async def set_reference_about(
        name: str, source_id: str, body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Persist a hand-written 'what this paper is about / used
        for' summary against a specific source, overriding the
        deterministic passage-derived snippet."""
        path = _project_path(name)
        text = (body.get("about") or "").strip()
        notes_path = path / ".lattice" / "reference_notes.json"
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, str] = {}
        if notes_path.exists():
            try:
                data = json.loads(notes_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    existing = {
                        k: str(v) for k, v in data.items() if isinstance(v, str)
                    }
            except json.JSONDecodeError:
                pass
        if text:
            existing[source_id] = text[:2000]
        else:
            existing.pop(source_id, None)
        notes_path.write_text(
            json.dumps(existing, indent=2), encoding="utf-8"
        )
        return {"source_id": source_id, "saved": True, "about": text[:2000]}

    @app.get("/api/projects/{name}/run-history")
    async def get_run_history(name: str) -> dict[str, Any]:
        """Return the persisted run history for this project. Used by
        the UI to drive level-progression awareness — which levels
        have completed, what they produced, what the next level adds.

        The summary aggregates per-level state so the UI doesn't have
        to traverse the full history itself."""
        path = _project_path(name)
        history = read_run_history(path)

        # Per-level last successful run (most useful for level stacking).
        latest_by_level: dict[str, dict[str, Any]] = {}
        for record in history:
            level = record.get("level")
            if level in ("quick", "standard", "deep"):
                latest_by_level[level] = record

        return {
            "history": history,
            "latest_by_level": latest_by_level,
            "summary": {
                "total_runs": len(history),
                "successful_deliveries": sum(
                    1 for r in history if r.get("finalise_succeeded")
                ),
                "levels_completed_successfully": sorted({
                    r["level"] for r in history
                    if r.get("level") in ("quick", "standard", "deep")
                    and r.get("finalise_succeeded")
                }),
            },
        }

    @app.post("/api/projects/{name}/outline-restructure")
    async def restructure_outline(name: str) -> dict[str, Any]:
        """Force a re-structuring on the next review run.

        Behaviour:
          - If ``outline.raw.md`` exists, copy it back over
            ``outline.md`` so the auto-outliner detects raw prose.
          - Delete the saved ``author_graph.json`` + ``cluster_plan.json``
            so the next run does a clean re-ingest.

        This is the recovery path for outlines that got auto-structured
        with old prompts that produced unrenderable claim tags."""
        path = _project_path(name)
        outline_md = path / "structure" / "outline.md"
        outline_raw = path / "structure" / "outline.raw.md"
        graph_path = path / ".lattice" / "author_graph.json"
        plan_path = path / ".lattice" / "cluster_plan.json"

        notes: list[str] = []
        if outline_raw.exists():
            outline_md.write_text(
                outline_raw.read_text(encoding="utf-8"), encoding="utf-8"
            )
            notes.append("Restored outline.md from outline.raw.md")
        else:
            notes.append(
                "No outline.raw.md archive found. The next run will "
                "re-structure outline.md only if it currently lacks "
                "lattice headers."
            )
        if graph_path.exists():
            graph_path.unlink()
            notes.append("Removed stale author_graph.json")
        if plan_path.exists():
            plan_path.unlink()
            notes.append("Removed stale cluster_plan.json")
        return {"ok": True, "notes": notes}

    @app.get("/api/projects/{name}/hierarchy")
    async def get_hierarchy(name: str) -> dict[str, Any]:
        """Structured tree of the project: thesis → sections → clusters → claims.

        Frontend consumes this for the Hierarchy tab.
        """
        path = _project_path(name)
        graph = json.loads((path / ".lattice" / "author_graph.json").read_text(encoding="utf-8"))

        cluster_plan_path = path / ".lattice" / "cluster_plan.json"
        clusters: list[dict[str, Any]] = []
        if cluster_plan_path.exists():
            try:
                raw = json.loads(cluster_plan_path.read_text(encoding="utf-8"))
                clusters = raw if isinstance(raw, list) else raw.get("clusters", [])
            except json.JSONDecodeError:
                clusters = []

        clusters_by_section: dict[str, list[dict[str, Any]]] = {}
        for c in clusters:
            clusters_by_section.setdefault(c["section_id"], []).append(c)
        for sid in clusters_by_section:
            clusters_by_section[sid].sort(key=lambda c: c.get("position", 0))

        claims_by_id = {c["claim_id"]: c for c in graph.get("claims", [])}

        # Index relationships by claim so the tree view can show
        # incoming / outgoing edges directly on each claim node. The
        # graph view renders the full edge set; this just lets the
        # tree view surface "supports cl.x.y", "supported by cl.a.b"
        # without a separate API call.
        rels_out: dict[str, list[dict[str, Any]]] = {}
        rels_in: dict[str, list[dict[str, Any]]] = {}
        for r in graph.get("relationships", []):
            from_id = r.get("from_claim") or r.get("from")
            to_id = r.get("to_claim") or r.get("to")
            if not from_id or not to_id:
                continue
            entry = {
                "rel_id": r.get("rel_id"),
                "type": r.get("type"),
                "strength": r.get("strength"),
                "note": (r.get("note") or "")[:160],
                "created_by": r.get("created_by"),
                "other_claim": None,  # filled in below for direction
            }
            out_entry = dict(entry, other_claim=to_id)
            in_entry = dict(entry, other_claim=from_id)
            rels_out.setdefault(from_id, []).append(out_entry)
            rels_in.setdefault(to_id, []).append(in_entry)

        def _claim_summary(claim_id: str, role_in_cluster: str | None = None) -> dict[str, Any]:
            claim = claims_by_id.get(claim_id, {})
            return {
                "claim_id": claim_id,
                "statement": (claim.get("statement") or "")[:240],
                "type": claim.get("type"),
                "role_in_cluster": role_in_cluster,
                "role_tag": next(
                    (t.split(":", 1)[1] for t in claim.get("tags", []) if t.startswith("role:")),
                    None,
                ),
                "author_origin": claim.get("author_origin", False),
                "importance": claim.get("importance", 0.5),
                "mechanism": (claim.get("mechanism") or "")[:300] or None,
                "evidence_count": len([
                    ev for ev in claim.get("evidence", [])
                    if ev.get("binding_strength") in ("strong", "weak")
                ]),
                "tags": [t for t in claim.get("tags", []) if not t.startswith("role:")],
                "rels_out": rels_out.get(claim_id, []),
                "rels_in": rels_in.get(claim_id, []),
            }

        sections_data: list[dict[str, Any]] = []
        for s in sorted(graph.get("sections", []), key=lambda s: s.get("position", 0)):
            section_clusters: list[dict[str, Any]] = []
            for cluster in clusters_by_section.get(s["section_id"], []):
                section_clusters.append({
                    "cluster_id": cluster["cluster_id"],
                    "role": cluster.get("role"),
                    "prose_state": cluster.get("prose_state"),
                    "target_words_min": cluster.get("target_words_min"),
                    "target_words_max": cluster.get("target_words_max"),
                    "claims": [
                        _claim_summary(entry["claim_id"], entry.get("role_in_cluster"))
                        for entry in cluster.get("claim_sequence", [])
                    ],
                })
            sections_data.append({
                "section_id": s["section_id"],
                "title": s.get("title"),
                "role": s.get("role"),
                "position": s.get("position"),
                "clusters": section_clusters,
                # Surface claims with no cluster too (orphans).
                "orphan_claim_count": len([
                    cid for cid in s.get("claim_ids", [])
                    if cid not in {
                        entry["claim_id"]
                        for c in clusters_by_section.get(s["section_id"], [])
                        for entry in c.get("claim_sequence", [])
                    }
                ]),
            })

        return {
            "thesis_statement": graph.get("thesis_statement"),
            "thesis_argued": graph.get("thesis_argued"),
            "thesis_argued_confidence": graph.get("thesis_argued_confidence"),
            "thesis_argued_note": graph.get("thesis_argued_note"),
            "sections": sections_data,
            "totals": {
                "sections": len(graph.get("sections", [])),
                "claims": len(graph.get("claims", [])),
                "clusters": len(clusters),
                "relationships": len(graph.get("relationships", [])),
            },
        }

    # ─── static frontend ─────────────────────

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(str(_STATIC_DIR / "index.html"))

        @app.get("/favicon.ico")
        async def favicon():
            """Return an empty 204 instead of 404 — silences the
            console noise without forcing us to ship a real icon
            asset right now."""
            from fastapi import Response
            return Response(status_code=204)

    return app
