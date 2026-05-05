"""Tests for the web layer: FastAPI endpoints + EventQueueProgress.

The pipeline integration tests live in their own modules; here we only
check that the API is shaped correctly and that the progress callback
emits the structured events the frontend depends on.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lattice.graph.models import (
    AuthorGraph,
    Claim,
    ClaimRoleInCluster,
    ClaimType,
    Cluster,
    ClusterRole,
    Confidence,
    ProseState,
    Section,
    SectionRole,
)
from lattice.graph.store import GraphStore
from lattice.web.app import create_app
from lattice.web.runner import EventQueueProgress


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_project(root: Path, name: str = "demo") -> Path:
    """Seed a minimal lattice project so the web API has something to list."""
    project = root / name
    project.mkdir(parents=True, exist_ok=True)
    (project / "config.yml").write_text("autocorrect: safe\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir(exist_ok=True)
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )

    store = GraphStore.load(project)
    now = _now()
    claim = Claim(
        claim_id="cl.x.1", statement="A claim.",
        type=ClaimType.empirical, confidence=Confidence.medium,
        section_id="s.x", created_by="t",
        created_at=now, modified_at=now,
        author_origin=True,
    )
    section = Section(
        section_id="s.x", title="X", position=1,
        role=SectionRole.argumentative, claim_ids=["cl.x.1"],
    )
    graph = AuthorGraph(
        project_name=name, sections=[section], claims=[claim],
        relationships=[], created_at=now, modified_at=now,
    )
    store.save_graph(graph)
    cluster = Cluster(
        cluster_id="c.x.1", section_id="s.x", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[ClaimRoleInCluster(
            claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence,
        )],
        prose_state=ProseState.generated,
    )
    store.save_cluster(cluster)
    return project


# ─── EventQueueProgress ────────────────────────────


def test_event_queue_progress_emits_structured_events() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)

    progress.begin_pass(1, 3)
    progress.begin("render", total=4, status="starting chunks")
    progress.advance("render", status="chunk 1/4")
    progress.advance("render", status="chunk 2/4")
    progress.update_status("render", "chunk 3/4 in flight")
    progress.end("render", status="4 chunks rendered")

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    types = [e["type"] for e in events]
    assert types == [
        "pass_started",
        "phase_begun",
        "phase_advanced",
        "phase_advanced",
        "phase_status",
        "phase_ended",
    ]
    # Every event carries pass_index + total_elapsed_seconds.
    for e in events:
        assert e["pass_index"] == 1
        assert "total_elapsed_seconds" in e

    # Counter math: two advances → done=2.
    advance_events = [e for e in events if e["type"] == "phase_advanced"]
    assert advance_events[-1]["done"] == 2
    assert advance_events[-1]["total"] == 4


def test_event_queue_progress_pass_change_propagates() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    progress.begin_pass(2, 3)
    progress.begin("audit", total=10)

    while not queue.empty():
        last = queue.get_nowait()
    assert last["type"] == "phase_begun"
    assert last["pass_index"] == 2


def test_event_queue_progress_unknown_phase_advance_safe() -> None:
    """Calling advance/end on a phase that wasn't begun should not raise."""
    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    progress.advance("never_began")  # no exception
    progress.end("never_began", status="ok")  # no exception
    assert queue.qsize() == 2


# ─── FastAPI endpoints ─────────────────────────────


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    _seed_project(tmp_path, "demo")
    app = create_app(projects_root=tmp_path)
    return TestClient(app)


def test_version_endpoint_returns_package_version(client: TestClient) -> None:
    """The /api/version endpoint should mirror the package's
    ``__version__`` so the frontend topbar shows whatever the running
    server actually is."""
    from lattice import __version__
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": __version__}


def test_get_projects_lists_seeded_project(client: TestClient) -> None:
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    projects = resp.json()
    assert len(projects) == 1
    assert projects[0]["name"] == "demo"
    # paper_words and last_render are 0/None when no render has happened.
    assert projects[0]["paper_words"] == 0


def test_get_project_detail(client: TestClient) -> None:
    resp = client.get("/api/projects/demo")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "demo"
    assert data["section_count"] == 1
    assert data["claim_count"] == 1
    assert data["cluster_count"] == 1
    assert data["paper_exists"] is False
    assert "academic" in data["voices"]


def test_get_project_404_for_unknown(client: TestClient) -> None:
    resp = client.get("/api/projects/does_not_exist")
    assert resp.status_code == 404


def test_get_audit_returns_empty_when_no_audit(client: TestClient) -> None:
    resp = client.get("/api/projects/demo/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["flags"] == []
    assert body["voice"] == "academic"


def test_get_audit_flattens_per_voice_keyed_file(
    client: TestClient, tmp_path: Path
) -> None:
    """audit_flags.json on disk is keyed by voice name. The endpoint
    should return a flat list (so .filter() works on the frontend),
    not the raw dict."""
    audit_path = tmp_path / "demo" / ".lattice" / "audit_flags.json"
    audit_path.write_text(
        json.dumps({
            "academic": [
                {"flag_id": "f.1", "category": "x", "rule_id": "r.1",
                 "severity": "minor"},
                {"flag_id": "f.2", "category": "y", "rule_id": "r.2",
                 "severity": "critical"},
            ],
            "journalistic": [
                {"flag_id": "f.3", "category": "z", "rule_id": "r.3",
                 "severity": "standard"},
            ],
        }),
        encoding="utf-8",
    )

    resp = client.get("/api/projects/demo/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["flags"], list)
    assert len(body["flags"]) == 2  # only academic flags
    assert {f["flag_id"] for f in body["flags"]} == {"f.1", "f.2"}
    assert body["voice"] == "academic"
    assert sorted(body["available_voices"]) == ["academic", "journalistic"]

    # Switching voice via query param.
    resp_j = client.get("/api/projects/demo/audit?voice=journalistic")
    assert resp_j.status_code == 200
    body_j = resp_j.json()
    assert len(body_j["flags"]) == 1
    assert body_j["flags"][0]["flag_id"] == "f.3"


def test_favicon_returns_204(client: TestClient) -> None:
    """/favicon.ico should be a 204, not 404, so the console doesn't
    show a phantom error."""
    resp = client.get("/favicon.ico")
    assert resp.status_code == 204


def test_get_paper_404_when_no_render(client: TestClient) -> None:
    resp = client.get("/api/projects/demo/paper")
    assert resp.status_code == 404


def test_start_run_rejects_unknown_level(client: TestClient) -> None:
    resp = client.post(
        "/api/projects/demo/runs",
        json={"voice": "academic", "level": "extreme"},
    )
    assert resp.status_code == 400


def test_start_run_rejects_missing_reference(client: TestClient) -> None:
    resp = client.post(
        "/api/projects/demo/runs",
        json={
            "voice": "academic",
            "level": "deep",
            "reference_path": "/nonexistent/reference.md",
        },
    )
    assert resp.status_code == 400


def test_start_run_returns_run_id(client: TestClient, monkeypatch) -> None:
    """Happy-path: posting a run returns a run_id immediately. The
    background pipeline task is mocked out — we only assert the API
    response shape, not the pipeline's execution.
    """
    # Replace run_pipeline with a no-op so the task completes instantly
    # rather than tying up the event loop with subprocess calls.
    async def _noop_pipeline(request, progress):
        progress._emit({"type": "run_finished", "elapsed_seconds": 0.0})
        from lattice.web.runner import RunResult
        return RunResult()
    monkeypatch.setattr("lattice.web.app.run_pipeline", _noop_pipeline)

    resp = client.post(
        "/api/projects/demo/runs",
        json={"voice": "academic", "level": "quick"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" in body
    assert len(body["run_id"]) == 12
    assert body["project"] == "demo"
    assert body["level"] == "quick"


def test_static_frontend_served(client: TestClient) -> None:
    """The single-page index is served at /."""
    resp = client.get("/")
    # FileResponse returns 200 with text/html.
    assert resp.status_code == 200
    assert "<title>Lattice</title>" in resp.text


# ─── new project + hierarchy + drafts + graph-viz ───


def test_get_hierarchy_returns_tree(client: TestClient) -> None:
    """The hierarchy endpoint returns the structured tree the frontend
    consumes for the Hierarchy tab."""
    resp = client.get("/api/projects/demo/hierarchy")
    assert resp.status_code == 200
    data = resp.json()
    assert "sections" in data
    assert "totals" in data
    assert data["totals"]["sections"] == 1
    assert data["totals"]["claims"] == 1
    assert len(data["sections"]) == 1
    section = data["sections"][0]
    assert section["section_id"] == "s.x"
    assert len(section["clusters"]) == 1
    cluster = section["clusters"][0]
    assert cluster["cluster_id"] == "c.x.1"
    assert len(cluster["claims"]) == 1
    claim = cluster["claims"][0]
    assert claim["claim_id"] == "cl.x.1"
    assert claim["author_origin"] is True


def test_get_drafts_empty_when_no_renders(client: TestClient) -> None:
    resp = client.get("/api/projects/demo/drafts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_drafts_lists_paper_files(client: TestClient, tmp_path: Path) -> None:
    """Once paper files exist, the drafts endpoint returns them with
    word counts and the current-flag."""
    outputs = tmp_path / "demo" / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "paper.academic.md").write_text("# Title\nFirst draft body.\n", encoding="utf-8")
    (outputs / "paper.academic.previous.md").write_text("Older body text.\n", encoding="utf-8")

    resp = client.get("/api/projects/demo/drafts")
    assert resp.status_code == 200
    drafts = resp.json()
    assert len(drafts) == 2
    names = [d["filename"] for d in drafts]
    assert "paper.academic.md" in names
    assert "paper.academic.previous.md" in names
    current = next(d for d in drafts if d["filename"] == "paper.academic.md")
    assert current["is_current"] is True
    # "# Title\nFirst draft body.\n" → 5 whitespace-split tokens.
    assert current["word_count"] == 5


def test_get_draft_rejects_path_traversal(client: TestClient) -> None:
    """Filenames with / or .. should be rejected even if a real file exists."""
    resp = client.get("/api/projects/demo/drafts/..%2Fconfig.yml")
    # Either rejected (400) or simply not found (404). Anything but the
    # actual file content is acceptable.
    assert resp.status_code in (400, 404)


def test_create_project_minimal_no_outline(client: TestClient, tmp_path: Path) -> None:
    """Creating a project with no outline scaffolds folders but doesn't ingest."""
    resp = client.post("/api/projects", json={"name": "fresh"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "fresh"
    assert data["ingested"] is False  # no outline supplied
    # Folder + key files should exist.
    project = tmp_path / "fresh"
    assert project.is_dir()
    assert (project / "config.yml").exists()
    assert (project / "structure" / "outline.md").exists()
    assert (project / "voices" / "academic.voice.md").exists()


def test_create_project_with_outline_runs_ingest(client: TestClient) -> None:
    """An outline body triggers ingest+plan in-process."""
    outline = (
        "# THESIS\n\nA short thesis statement.\n\n"
        "# A. First section\n\n"
        "  - First claim [strong]\n"
        "  - MY VIEW: My synthesis claim [user_synthesis]\n"
    )
    resp = client.post("/api/projects", json={
        "name": "ingested",
        "outline": outline,
        "ingest_now": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ingested"] is True
    assert data["claim_count"] >= 2  # at least the two body claims, plus possibly a thesis claim
    assert data["section_count"] >= 1


def test_create_project_rejects_empty_name(client: TestClient) -> None:
    for bad in ["", "   ", "/", "..", "/!@#$"]:
        resp = client.post("/api/projects", json={"name": bad})
        assert resp.status_code == 400, f"Expected 400 for {bad!r}; got {resp.status_code}"


def test_create_project_accepts_human_readable_name(
    client: TestClient, tmp_path: Path
) -> None:
    """A name with spaces is slugged to a safe folder while the
    human-readable display_name is preserved."""
    resp = client.post("/api/projects", json={
        "name": "Extraneous factors in judicial decisions",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "extraneous_factors_in_judicial_decisions"
    # Note that we slugged it.
    assert any("slugified" in n for n in data["notes"])
    # Folder + meta file actually exist.
    project = tmp_path / "extraneous_factors_in_judicial_decisions"
    assert project.is_dir()
    meta = json.loads((project / ".lattice" / "project_meta.json").read_text(encoding="utf-8"))
    assert meta["display_name"] == "Extraneous factors in judicial decisions"

    # The list endpoint returns the human-readable name.
    listed = client.get("/api/projects").json()
    matching = next((p for p in listed if p["name"] == data["name"]), None)
    assert matching is not None
    assert matching["display_name"] == "Extraneous factors in judicial decisions"

    # The detail endpoint also surfaces it (project not yet ingested).
    detail = client.get(f"/api/projects/{data['name']}").json()
    assert detail["display_name"] == "Extraneous factors in judicial decisions"
    assert detail["ingested"] is False


def test_create_project_slug_collision(client: TestClient) -> None:
    """Two display names that slug to the same folder collide."""
    r1 = client.post("/api/projects", json={"name": "My Paper"})
    assert r1.status_code == 200
    r2 = client.post("/api/projects", json={"name": "my paper"})
    assert r2.status_code == 409  # second one slugs to the same folder


def test_create_project_rejects_duplicate(client: TestClient) -> None:
    """The seeded fixture already has a project named 'demo'."""
    resp = client.post("/api/projects", json={"name": "demo"})
    assert resp.status_code == 409


def test_upload_outline_markdown_replaces_outline(client: TestClient, tmp_path: Path) -> None:
    """Uploading a markdown outline file replaces structure/outline.md."""
    outline_text = "# THESIS\n\nUploaded thesis.\n\n# A. First section\n\n  - First claim\n"
    resp = client.post(
        "/api/projects/demo/outline",
        files={"file": ("outline.md", outline_text.encode("utf-8"), "text/markdown")},
        data={"ingest": "false"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["saved_to"].endswith("outline.md")
    saved = (tmp_path / "demo" / "structure" / "outline.md").read_text(encoding="utf-8")
    assert "Uploaded thesis." in saved


def test_upload_outline_rejects_unsupported_extension(client: TestClient) -> None:
    resp = client.post(
        "/api/projects/demo/outline",
        files={"file": ("outline.pdf", b"PDFstuff", "application/pdf")},
        data={"ingest": "false"},
    )
    assert resp.status_code == 400


def test_list_sources_returns_buckets(client: TestClient) -> None:
    resp = client.get("/api/projects/demo/sources")
    assert resp.status_code == 200
    data = resp.json()
    # All allowed buckets should appear, even when empty.
    for b in ["papers", "notes", "data", "web", "prior_writing"]:
        assert b in data["buckets"]
        assert isinstance(data["buckets"][b], list)
    assert "indexed" in data


def test_upload_sources_saves_files(client: TestClient, tmp_path: Path) -> None:
    """Uploading a markdown source saves it to refs/papers/."""
    resp = client.post(
        "/api/projects/demo/sources",
        files=[
            ("files", ("note.md", b"# Note\nbody\n", "text/markdown")),
            ("files", ("data.txt", b"some text", "text/plain")),
        ],
        data={"bucket": "notes"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["saved"]) == 2
    saved_names = {s["filename"] for s in body["saved"]}
    assert {"note.md", "data.txt"} == saved_names
    # Files actually exist on disk.
    notes_dir = tmp_path / "demo" / "refs" / "notes"
    assert (notes_dir / "note.md").exists()
    assert (notes_dir / "data.txt").exists()


def test_upload_sources_rejects_invalid_bucket(client: TestClient) -> None:
    resp = client.post(
        "/api/projects/demo/sources",
        files=[("files", ("a.md", b"x", "text/markdown"))],
        data={"bucket": "untrusted"},
    )
    assert resp.status_code == 400


def test_upload_pdf_source(client: TestClient, tmp_path: Path) -> None:
    """End-to-end: a PDF lands in refs/papers/ with the right size."""
    fake_pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    resp = client.post(
        "/api/projects/demo/sources",
        files=[("files", ("paper.pdf", fake_pdf, "application/pdf"))],
        data={"bucket": "papers"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["saved"]) == 1
    saved = body["saved"][0]
    assert saved["filename"] == "paper.pdf"
    assert saved["bucket"] == "papers"
    assert saved["size_bytes"] == len(fake_pdf)
    assert (tmp_path / "demo" / "refs" / "papers" / "paper.pdf").read_bytes() == fake_pdf


def test_upload_pdf_writes_text_sidecar(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """Uploading a parseable PDF should also write a <name>.pdf.txt
    sidecar holding the extracted text. The PDF itself must remain on
    disk as the canonical archival copy."""
    class _FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _FakeReader:
        def __init__(self, _path) -> None:
            self.pages = [_FakePage("Hello world."), _FakePage("Second page text.")]

    # Patch the PdfReader symbol the helper actually imports (lazy
    # import inside _extract_pdf_to_sidecar grabs the real pypdf, so we
    # patch at the source).
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)

    fake_pdf = b"%PDF-1.4 fake but reader is mocked\n%%EOF\n"
    resp = client.post(
        "/api/projects/demo/sources",
        files=[("files", ("article.pdf", fake_pdf, "application/pdf"))],
        data={"bucket": "papers"},
    )
    assert resp.status_code == 200, resp.text
    saved = resp.json()["saved"][0]
    assert saved["filename"] == "article.pdf"
    assert saved["text_sidecar"] == "article.pdf.txt"
    assert saved["page_count"] == 2

    pdf_path = tmp_path / "demo" / "refs" / "papers" / "article.pdf"
    txt_path = tmp_path / "demo" / "refs" / "papers" / "article.pdf.txt"
    assert pdf_path.exists()
    assert pdf_path.read_bytes() == fake_pdf
    assert txt_path.exists()
    body = txt_path.read_text(encoding="utf-8")
    assert "Hello world." in body
    assert "Second page text." in body


def test_list_sources_attaches_pdf_sidecar(
    client: TestClient, tmp_path: Path
) -> None:
    """The list-sources response should pair a PDF with its sidecar
    text file rather than show them as two unrelated entries."""
    papers_dir = tmp_path / "demo" / "refs" / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    (papers_dir / "manuscript.pdf").write_bytes(b"binary blob")
    (papers_dir / "manuscript.pdf.txt").write_text(
        "Extracted manuscript body.", encoding="utf-8"
    )

    resp = client.get("/api/projects/demo/sources")
    assert resp.status_code == 200
    papers = resp.json()["buckets"]["papers"]
    # Sidecar must NOT appear as a separate top-level entry.
    names = [e["filename"] for e in papers]
    assert "manuscript.pdf" in names
    assert "manuscript.pdf.txt" not in names
    pdf_entry = next(e for e in papers if e["filename"] == "manuscript.pdf")
    assert pdf_entry["text_sidecar"] == "manuscript.pdf.txt"
    assert pdf_entry["text_size_bytes"] == len(
        b"Extracted manuscript body."
    )


def test_get_source_file_serves_text_sidecar(
    client: TestClient, tmp_path: Path
) -> None:
    """The new /sources/<bucket>/<filename> route serves the extracted
    text sidecar inline so the UI can display it."""
    papers_dir = tmp_path / "demo" / "refs" / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    (papers_dir / "p.pdf").write_bytes(b"binary blob")
    (papers_dir / "p.pdf.txt").write_text("Plain extracted body.", encoding="utf-8")

    resp = client.get("/api/projects/demo/sources/papers/p.pdf.txt")
    assert resp.status_code == 200
    assert resp.text == "Plain extracted body."


def test_get_source_file_rejects_path_traversal(client: TestClient) -> None:
    """Path traversal attempts must be rejected."""
    resp = client.get("/api/projects/demo/sources/papers/..%2Fconfig.yml")
    assert resp.status_code in (400, 404)


def test_get_source_file_rejects_unknown_bucket(client: TestClient) -> None:
    resp = client.get("/api/projects/demo/sources/evil/anything.txt")
    assert resp.status_code == 404


def test_upload_pdf_with_unparseable_content_records_failure(
    client: TestClient, tmp_path: Path
) -> None:
    """If the PDF parser raises, the upload still succeeds and the file
    is preserved — only the extraction is flagged as failed."""
    fake_pdf = b"not a real pdf at all"
    resp = client.post(
        "/api/projects/demo/sources",
        files=[("files", ("broken.pdf", fake_pdf, "application/pdf"))],
        data={"bucket": "papers"},
    )
    assert resp.status_code == 200, resp.text
    saved = resp.json()["saved"][0]
    assert saved["filename"] == "broken.pdf"
    assert saved.get("text_extraction_failed") is True
    assert saved["text_sidecar"] is None
    # The PDF itself is still preserved on disk.
    assert (tmp_path / "demo" / "refs" / "papers" / "broken.pdf").exists()


def test_upload_sources_skips_invalid_files(client: TestClient) -> None:
    """Files with bad extensions are skipped, not errored — partial success."""
    resp = client.post(
        "/api/projects/demo/sources",
        files=[
            ("files", ("legit.md", b"# Title", "text/markdown")),
            ("files", ("script.exe", b"MZbinary", "application/octet-stream")),
        ],
        data={"bucket": "notes"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["saved"]) == 1
    assert body["saved"][0]["filename"] == "legit.md"
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["filename"] == "script.exe"


def test_extract_text_markdown_passes_through(client: TestClient) -> None:
    """The extract-text endpoint round-trips markdown unchanged."""
    body = b"# Title\n\nFirst paragraph.\n"
    resp = client.post(
        "/api/extract-text",
        files={"file": ("note.md", body, "text/markdown")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_format"] == "md"
    assert data["text"] == body.decode("utf-8")
    assert data["char_count"] == len(body)


def test_extract_text_rejects_unsupported_extension(client: TestClient) -> None:
    resp = client.post(
        "/api/extract-text",
        files={"file": ("graphic.png", b"PNGdata", "image/png")},
    )
    assert resp.status_code == 400


def test_extract_text_pdf_returns_text(client: TestClient) -> None:
    """A minimal valid PDF returns a 200 with text + page_count.

    The text may be empty for synthetic PDFs (no content streams), but
    the response shape should be correct."""
    # Build a minimal PDF with a single empty page using pypdf itself —
    # avoids hand-crafting a parser-fragile byte sequence.
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    import io
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)

    resp = client.post(
        "/api/extract-text",
        files={"file": ("paper.pdf", buf.read(), "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["source_format"] == "pdf"
    assert data["page_count"] == 1
    assert "text" in data


def test_run_pipeline_auto_bootstraps_ingest_and_plan(
    tmp_path: Path, monkeypatch
) -> None:
    """A project that has an outline but no graph or cluster plan should
    have ingest + plan run automatically before the renderer kicks in.
    Regression test for the 'no_cluster_plan' failure on freshly-created
    projects.
    """
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "fresh"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nA thesis sentence.\n\n"
        "# A. First section\n\n"
        "  - A claim [strong]\n"
        "  - MY VIEW: A view [user_synthesis]\n",
        encoding="utf-8",
    )

    # Stub out the LLM and renderer so this test stays fast and offline.
    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)

    class _StubClient:
        def __init__(self, *a, **k) -> None: ...

    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _StubClient)

    async def _no_render(self, *, force=False, progress=None):
        return {}

    monkeypatch.setattr(
        "lattice.web.runner.ChunkedRenderer.render_all", _no_render
    )
    monkeypatch.setattr(
        "lattice.web.runner.DocumentFinaliser.finalise", lambda self: None
    )

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project,
        voice_name="academic",
        level="quick",
    )
    asyncio.run(run_pipeline(request, progress))

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    phases_begun = [e["phase"] for e in events if e["type"] == "phase_begun"]
    assert "ingest" in phases_begun, "auto-bootstrap should emit an ingest phase"
    assert "plan" in phases_begun, "auto-bootstrap should emit a plan phase"

    # The bootstrap should have actually written the artefacts.
    assert (project / ".lattice" / "author_graph.json").exists()
    assert (project / ".lattice" / "cluster_plan.json").exists()

    # And the run shouldn't have failed with no_cluster_plan.
    fail_events = [e for e in events if e["type"] == "run_failed"]
    assert fail_events == [], f"unexpected run_failed events: {fail_events}"


def test_run_pipeline_detects_empty_graph_from_prior_run(
    tmp_path: Path, monkeypatch
) -> None:
    """If a previous run already saved an empty graph (because the
    user's outline had no headers), subsequent runs must surface
    'outline_has_no_structure' rather than slipping through to
    'empty_cluster_plan'."""
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "stuck"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    # Outline is raw prose (no headers).
    (structure_dir / "outline.md").write_text(
        "Just some flat paragraphs of paper prose.\n", encoding="utf-8"
    )
    # Persist an empty graph from a hypothetical prior run.
    lattice_dir = project / ".lattice"
    lattice_dir.mkdir()
    empty_graph = {
        "project_name": "stuck",
        "thesis_statement": None,
        "thesis_argued": None,
        "thesis_argued_confidence": None,
        "thesis_argued_note": None,
        "sections": [],
        "claims": [],
        "relationships": [],
        "created_at": "2026-01-01T00:00:00Z",
        "modified_at": "2026-01-01T00:00:00Z",
    }
    (lattice_dir / "author_graph.json").write_text(
        json.dumps(empty_graph), encoding="utf-8"
    )
    # Make the graph file *newer* than the outline so the mtime-based
    # auto-reingest path doesn't fire — we want to prove the post-ingest
    # check still catches it.
    import os as _os
    outline_path = structure_dir / "outline.md"
    _os.utime(outline_path, (1700000000, 1700000000))
    _os.utime(lattice_dir / "author_graph.json", (1800000000, 1800000000))

    # Stub Claude so the auto-outliner returns text without
    # `# THESIS` / `# A.` headers — its structural validator then
    # raises and the runner emits auto_structure_failed (the
    # equivalent "stuck on empty graph" symptom under the new flow).
    class _Resp:
        text = "I cannot extract structure from this text."

    class _Stub:
        def __init__(self, *a, **k): pass
        async def complete(self, **k): return _Resp()

    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)
    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _Stub)

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    asyncio.run(run_pipeline(request, progress))

    fail_events = [
        queue.get_nowait() for _ in range(queue.qsize())
    ]
    fails = [e for e in fail_events if e["type"] == "run_failed"]
    assert len(fails) == 1
    assert fails[0]["reason"] == "auto_structure_failed"


def test_restructure_outline_restores_raw_and_drops_graph(
    client: TestClient, tmp_path: Path
) -> None:
    """Hitting POST /outline-restructure should:
       1. Copy outline.raw.md back over outline.md
       2. Delete author_graph.json and cluster_plan.json
    so the next review run re-runs the auto-outliner."""
    structure_dir = tmp_path / "demo" / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nold structured.\n\n# A. x\n\n  - y [empirical]\n",
        encoding="utf-8",
    )
    (structure_dir / "outline.raw.md").write_text(
        "Original raw paper prose with no headers.", encoding="utf-8"
    )
    lattice_dir = tmp_path / "demo" / ".lattice"
    cluster_plan = lattice_dir / "cluster_plan.json"
    cluster_plan.write_text("{}", encoding="utf-8")
    graph_path = lattice_dir / "author_graph.json"
    assert graph_path.exists()  # seeded by fixture

    resp = client.post("/api/projects/demo/outline-restructure")
    assert resp.status_code == 200, resp.text
    notes = resp.json()["notes"]
    assert any("Restored outline.md" in n for n in notes)
    assert any("Removed stale author_graph" in n for n in notes)
    assert any("Removed stale cluster_plan" in n for n in notes)

    # outline.md was overwritten with the raw text.
    saved = (structure_dir / "outline.md").read_text(encoding="utf-8")
    assert "Original raw paper prose" in saved
    assert "# THESIS" not in saved
    # Graph + plan are gone.
    assert not graph_path.exists()
    assert not cluster_plan.exists()


def test_restructure_outline_without_raw_archive(
    client: TestClient, tmp_path: Path
) -> None:
    """If no raw archive exists, restructure should still drop the
    stale graph but leave the existing outline.md intact."""
    structure_dir = tmp_path / "demo" / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    original = "# THESIS\n\nstructured.\n\n# A. x\n\n  - y\n"
    (structure_dir / "outline.md").write_text(original, encoding="utf-8")

    resp = client.post("/api/projects/demo/outline-restructure")
    assert resp.status_code == 200
    # outline.md untouched.
    assert (structure_dir / "outline.md").read_text(encoding="utf-8") == original
    notes = resp.json()["notes"]
    assert any("No outline.raw.md" in n for n in notes)


def test_outline_status_no_outline(client: TestClient) -> None:
    """The seeded demo project has a graph but no structure/outline.md;
    outline-status should reflect that cleanly."""
    resp = client.get("/api/projects/demo/outline-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["outline"]["exists"] is False
    assert data["raw_archive"]["exists"] is False
    assert data["graph"]["exists"] is True
    assert data["graph"]["section_count"] == 1
    assert data["graph"]["claim_count"] == 1


def test_outline_status_raw_prose_flagged(
    client: TestClient, tmp_path: Path
) -> None:
    """An outline that lacks lattice headers should report
    is_structured=False so the UI can show the warn banner."""
    structure_dir = tmp_path / "demo" / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    (structure_dir / "outline.md").write_text(
        "Plain academic prose with no headers at all.\n", encoding="utf-8"
    )
    resp = client.get("/api/projects/demo/outline-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["outline"]["exists"] is True
    assert data["outline"]["is_structured"] is False
    assert data["outline"]["filename"] == "outline.md"


def test_outline_status_structured_outline(
    client: TestClient, tmp_path: Path
) -> None:
    structure_dir = tmp_path / "demo" / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nThe thesis.\n\n# A. Section\n\n  - claim\n",
        encoding="utf-8",
    )
    resp = client.get("/api/projects/demo/outline-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["outline"]["is_structured"] is True
    assert "# THESIS" in data["outline"]["preview"]


def test_outline_status_surfaces_raw_archive(
    client: TestClient, tmp_path: Path
) -> None:
    structure_dir = tmp_path / "demo" / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nx\n\n# A. y\n\n  - z\n", encoding="utf-8"
    )
    (structure_dir / "outline.raw.md").write_text(
        "Original raw paper text.", encoding="utf-8"
    )
    resp = client.get("/api/projects/demo/outline-status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["raw_archive"]["exists"] is True
    assert data["raw_archive"]["filename"] == "outline.raw.md"


def test_run_pipeline_restructures_when_graph_is_stale_empty(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: previously, an empty graph saved AFTER the outline
    (graph mtime > outline mtime) would skip ingest and bypass
    auto-structuring. The runner should now check outline content
    directly and structure it regardless of mtime ordering."""
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "stale_empty"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    raw = "Just raw paper prose with no headers anywhere.\n"
    (structure_dir / "outline.md").write_text(raw, encoding="utf-8")

    # Save an empty graph that is NEWER than outline.md.
    lattice_dir = project / ".lattice"
    lattice_dir.mkdir()
    (lattice_dir / "author_graph.json").write_text(
        json.dumps({
            "project_name": "stale_empty",
            "thesis_statement": None, "thesis_argued": None,
            "thesis_argued_confidence": None, "thesis_argued_note": None,
            "sections": [], "claims": [], "relationships": [],
            "created_at": "2026-01-01T00:00:00Z",
            "modified_at": "2026-01-01T00:00:00Z",
        }), encoding="utf-8",
    )
    import os as _os
    _os.utime(structure_dir / "outline.md", (1700000000, 1700000000))
    _os.utime(lattice_dir / "author_graph.json", (1800000000, 1800000000))

    structured = (
        "# THESIS\n\nA structured thesis.\n\n"
        "# A. Section\n\n  - First claim.\n  - Second claim.\n"
    )

    class _Resp:
        def __init__(self, t): self.text = t

    class _StubClient:
        def __init__(self, *a, **k): pass
        async def complete(self, **k):
            return _Resp(structured)

    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)
    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _StubClient)

    async def _no_render(self, *, force=False, progress=None): return {}
    monkeypatch.setattr("lattice.web.runner.ChunkedRenderer.render_all", _no_render)
    monkeypatch.setattr(
        "lattice.web.runner.DocumentFinaliser.finalise", lambda self: None
    )

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    asyncio.run(run_pipeline(request, progress))

    events = []
    while not queue.empty(): events.append(queue.get_nowait())
    fail_events = [e for e in events if e["type"] == "run_failed"]
    assert fail_events == [], f"unexpected failures: {fail_events}"
    phases_begun = [e["phase"] for e in events if e["type"] == "phase_begun"]
    assert "structure_outline" in phases_begun
    assert "ingest" in phases_begun
    # outline.md was rewritten with the structured version.
    saved = (project / "structure" / "outline.md").read_text(encoding="utf-8")
    assert "# THESIS" in saved


def test_run_pipeline_auto_structures_raw_prose(
    tmp_path: Path, monkeypatch
) -> None:
    """If the user pastes raw paper prose into outline.md, the runner
    should call Claude to convert it into a lattice-format outline,
    then proceed with the normal pipeline. The original raw text is
    archived to outline.raw.md."""
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "auto_structured"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    raw_prose = (
        "Extraneous factors in judicial decisions\n\n"
        "Are judicial rulings based solely on laws and facts? "
        "We test the common caricature of realism that justice is "
        "what the judge ate for breakfast in sequential parole "
        "decisions made by experienced judges.\n"
    )
    (structure_dir / "outline.md").write_text(raw_prose, encoding="utf-8")

    structured_outline = (
        "# THESIS\n\n"
        "Judicial rulings are influenced by extraneous factors like meal breaks.\n\n"
        "# A. The formalist–realist debate\n\n"
        "  - Formalism holds that judges apply legal reason mechanically.\n"
        "  - Realism argues psychological factors influence rulings [strong]\n\n"
        "# B. Empirical test using parole decisions\n\n"
        "  - Favourable rulings drop from 65% to near zero across a session.\n"
        "  - Rulings reset to 65% after a meal break [empirical]\n"
    )

    class _StubLLMResponse:
        def __init__(self, text: str) -> None:
            self.text = text

    class _StubClient:
        def __init__(self, *a, **k) -> None: ...

        async def complete(self, *, system: str, user: str, **k):
            assert "Lattice outline" in system
            assert "Are judicial rulings" in user
            return _StubLLMResponse(structured_outline)

    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)
    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _StubClient)

    async def _no_render(self, *, force=False, progress=None):
        return {}

    monkeypatch.setattr(
        "lattice.web.runner.ChunkedRenderer.render_all", _no_render
    )
    monkeypatch.setattr(
        "lattice.web.runner.DocumentFinaliser.finalise", lambda self: None
    )

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    asyncio.run(run_pipeline(request, progress))

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    phases = [e["phase"] for e in events if e["type"] == "phase_begun"]
    assert "structure_outline" in phases, (
        f"expected structure_outline phase, got {phases!r}"
    )
    # Auto-outliner replaced outline.md and archived the original.
    assert (project / "structure" / "outline.raw.md").exists()
    saved = (project / "structure" / "outline.md").read_text(encoding="utf-8")
    assert "# THESIS" in saved
    assert "Are judicial rulings" not in saved  # raw prose was replaced
    raw = (project / "structure" / "outline.raw.md").read_text(encoding="utf-8")
    assert "Are judicial rulings" in raw

    # And the run succeeded — no run_failed events.
    fails = [e for e in events if e["type"] == "run_failed"]
    assert fails == [], f"unexpected failures: {fails}"


def test_hierarchy_endpoint_includes_per_claim_relationships(
    client: TestClient, tmp_path: Path
) -> None:
    """The hierarchy endpoint should expose ``rels_in`` and
    ``rels_out`` on each claim so the tree view can show what links
    in/out of it."""
    from lattice.graph.models import (
        Relationship, RelationshipStrength, RelationshipType,
    )
    project = tmp_path / "demo"
    store = GraphStore.load(project)
    graph = store.get_graph()
    now = _now()
    new_claim = Claim(
        claim_id="cl.x.2", statement="Second claim",
        type=ClaimType.user_synthesis, confidence=Confidence.medium,
        section_id="s.x", created_by="t",
        created_at=now, modified_at=now, author_origin=True,
    )
    graph.claims.append(new_claim)
    graph.sections[0].claim_ids.append("cl.x.2")
    graph.relationships.append(Relationship(
        rel_id="rel.test.1",
        type=RelationshipType.supports,
        **{"from": "cl.x.2"}, to="cl.x.1",
        strength=RelationshipStrength.direct,
        note="links second to first",
        created_by="relationship_inference", created_at=now,
    ))
    store.save_graph(graph)

    resp = client.get("/api/projects/demo/hierarchy")
    assert resp.status_code == 200
    data = resp.json()
    # Find claim cl.x.1 and check rels_in shape.
    target = None
    for s in data["sections"]:
        for c in s["clusters"]:
            for cl in c["claims"]:
                if cl["claim_id"] == "cl.x.1":
                    target = cl
    assert target is not None
    assert "rels_in" in target
    assert "rels_out" in target
    assert len(target["rels_in"]) == 1
    assert target["rels_in"][0]["other_claim"] == "cl.x.2"
    assert target["rels_in"][0]["type"] == "supports"


def test_graph_viz_regenerates_when_graph_is_newer(
    client: TestClient, tmp_path: Path
) -> None:
    """The graph-viz endpoint should rebuild the cached HTML when the
    underlying author_graph.json was modified after the cached file."""
    import os as _os, time as _time
    project = tmp_path / "demo"
    viz_path = project / "outputs" / "argument_graph.html"
    graph_path = project / ".lattice" / "author_graph.json"

    # First hit lazily generates the viz.
    resp1 = client.get("/api/projects/demo/graph-viz")
    assert resp1.status_code == 200
    body1 = resp1.text
    first_mtime = viz_path.stat().st_mtime

    # Touch the graph file to be newer than the viz. Use utime with
    # an explicit forward timestamp to avoid filesystem mtime
    # granularity issues on Windows.
    new_t = _time.time() + 10
    _os.utime(graph_path, (new_t, new_t))
    assert graph_path.stat().st_mtime > viz_path.stat().st_mtime

    resp2 = client.get("/api/projects/demo/graph-viz")
    assert resp2.status_code == 200
    second_mtime = viz_path.stat().st_mtime
    assert second_mtime > first_mtime, (
        "graph-viz should have regenerated the HTML"
    )


def test_graph_viz_regenerates_when_cluster_plan_is_newer(
    client: TestClient, tmp_path: Path
) -> None:
    """Phase 3: cluster_plan.json changes should also invalidate the
    cached graph-viz HTML, since cluster compound nodes + render-state
    badges come from the plan, not the graph."""
    import os as _os, time as _time
    project = tmp_path / "demo"
    viz_path = project / "outputs" / "argument_graph.html"
    cluster_path = project / ".lattice" / "cluster_plan.json"

    resp1 = client.get("/api/projects/demo/graph-viz")
    assert resp1.status_code == 200
    first_mtime = viz_path.stat().st_mtime

    if not cluster_path.exists():
        cluster_path.parent.mkdir(parents=True, exist_ok=True)
        cluster_path.write_text("[]", encoding="utf-8")
    new_t = _time.time() + 10
    _os.utime(cluster_path, (new_t, new_t))
    assert cluster_path.stat().st_mtime > viz_path.stat().st_mtime

    resp2 = client.get("/api/projects/demo/graph-viz")
    assert resp2.status_code == 200
    assert viz_path.stat().st_mtime > first_mtime, (
        "graph-viz should regenerate when cluster_plan.json is newer"
    )


def test_graph_viz_regenerates_when_audit_dir_changes(
    client: TestClient, tmp_path: Path
) -> None:
    """Phase 3: audit/readiness output changes should invalidate the
    cached HTML, since cluster compound nodes carry audit-flag and
    blocks-readiness badges."""
    import os as _os, time as _time
    project = tmp_path / "demo"
    viz_path = project / "outputs" / "argument_graph.html"
    audit_dir = project / ".lattice" / "audit"

    resp1 = client.get("/api/projects/demo/graph-viz")
    assert resp1.status_code == 200
    first_mtime = viz_path.stat().st_mtime

    audit_dir.mkdir(parents=True, exist_ok=True)
    flag_path = audit_dir / "audit_flags.json"
    flag_path.write_text("[]", encoding="utf-8")
    new_t = _time.time() + 10
    _os.utime(flag_path, (new_t, new_t))

    resp2 = client.get("/api/projects/demo/graph-viz")
    assert resp2.status_code == 200
    assert viz_path.stat().st_mtime > first_mtime, (
        "graph-viz should regenerate when audit/* changes"
    )


def test_graph_viz_detects_unrenderable_marker_under_voice_subdir(
    client: TestClient, tmp_path: Path
) -> None:
    """The visualiser's marker scan must look at the voice-specific
    drafts subdirectory. The renderer writes
    ``.lattice/drafts/<voice>/cluster_*.md`` — reading the flat
    ``.lattice/drafts/`` directory always missed the marker.
    """
    project = tmp_path / "demo"
    drafts_voice_dir = project / ".lattice" / "drafts" / "academic"
    drafts_voice_dir.mkdir(parents=True, exist_ok=True)
    (drafts_voice_dir / "cluster_c.x.1.md").write_text(
        'Some prose. {CLUSTER_UNRENDERABLE: cluster_id="c.x.1", '
        'reason="no bindings"}',
        encoding="utf-8",
    )

    resp = client.get("/api/projects/demo/graph-viz")
    assert resp.status_code == 200
    body = resp.text
    assert '"hasUnrenderableMarker": true' in body or '"hasUnrenderableMarker":true' in body


def test_graph_viz_uses_local_cytoscape_not_unpkg(
    client: TestClient, tmp_path: Path
) -> None:
    """The web-served graph HTML must reference the vendored
    cytoscape.min.js so the UI works offline."""
    resp = client.get("/api/projects/demo/graph-viz")
    assert resp.status_code == 200
    body = resp.text
    assert "/static/vendor/cytoscape/cytoscape.min.js" in body
    assert "unpkg.com" not in body


def test_graph_viz_regenerates_when_voice_drafts_change(
    client: TestClient, tmp_path: Path
) -> None:
    """Touching a cluster file under the active voice's drafts
    directory should invalidate the cached HTML so missing-claim and
    unrenderable markers refresh after a re-render."""
    import os as _os, time as _time
    project = tmp_path / "demo"
    viz_path = project / "outputs" / "argument_graph.html"
    drafts_voice_dir = project / ".lattice" / "drafts" / "academic"
    drafts_voice_dir.mkdir(parents=True, exist_ok=True)
    cluster_md = drafts_voice_dir / "cluster_c.x.1.md"
    cluster_md.write_text("clean prose, no markers", encoding="utf-8")

    resp1 = client.get("/api/projects/demo/graph-viz")
    assert resp1.status_code == 200
    first_mtime = viz_path.stat().st_mtime

    # Re-write the cluster file with an unrenderable marker, future-stamped.
    cluster_md.write_text(
        'prose. {CLUSTER_UNRENDERABLE: cluster_id="c.x.1", reason="x"}',
        encoding="utf-8",
    )
    new_t = _time.time() + 10
    _os.utime(cluster_md, (new_t, new_t))

    resp2 = client.get("/api/projects/demo/graph-viz")
    assert resp2.status_code == 200
    assert viz_path.stat().st_mtime > first_mtime, (
        "graph-viz should regenerate when a voice's draft file changes"
    )
    body2 = resp2.text
    assert '"hasUnrenderableMarker": true' in body2 or '"hasUnrenderableMarker":true' in body2


def test_cockpit_queue_returns_empty_when_nothing_run(client: TestClient) -> None:
    """Fresh project: no audit, no lit gaps, no restructure, no review.
    The endpoint should still respond 200 with an empty items list and
    a sources map saying everything is missing."""
    resp = client.get("/api/projects/demo/cockpit-queue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["voice"] == "academic"
    assert data["sources"] == {
        "audit": "missing", "lit_gaps": "missing",
        "restructure": "missing", "review": "missing",
    }
    assert data["counts"]["total"] == 0


def test_cockpit_queue_merges_audit_lit_gaps_and_review(
    client: TestClient, tmp_path: Path
) -> None:
    """Drop one audit flag, one lit-gap suggestion, and one review
    revision into the project. The queue should surface all three with
    the right targets, severities, and action sets."""
    project = tmp_path / "demo"
    lattice_dir = project / ".lattice"
    outputs_dir = project / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    audit_path = lattice_dir / "audit_flags.json"
    audit_path.write_text(json.dumps({
        "academic": [{
            "flag_id": "f.1",
            "rule_id": "voice.hedge.weak",
            "category": "voice",
            "severity": "critical",
            "cluster_id": "c.x.1",
            "section_id": "s.x",
            "offending_text": "may possibly perhaps",
            "suggestion": "tighten",
        }],
    }), encoding="utf-8")

    gaps_path = outputs_dir / "lit_gaps.academic.json"
    gaps_path.write_text(json.dumps({
        "project_name": "demo", "voice_name": "academic",
        "generated_at": "2026-05-05T00:00:00Z", "mode": "thorough",
        "total_suggestions": 1, "verified_count": 0,
        "sections": [{
            "section_id": "s.x", "section_title": "X",
            "suggestions": [{
                "author": "Smith", "year": 2020, "work": "Important paper",
                "why_relevant": "directly bears on cl.x.1",
                "claim_ids": ["cl.x.1"], "kind": "canonical",
                "confidence": "high",
            }],
        }],
    }), encoding="utf-8")

    review_path = outputs_dir / "review.academic.json"
    review_path.write_text(json.dumps({
        "project_name": "demo", "voice_name": "academic",
        "generated_at": "2026-05-05T00:00:00Z", "mode": "thorough",
        "overall_critique": "needs work",
        "section_critiques": [],
        "cluster_revisions": [{
            "cluster_id": "c.x.1", "section_id": "s.x", "section_title": "X",
            "original_prose": "old", "revised_prose": "new",
            "comment": "tighten this opening",
            "severity": "concern",
        }],
    }), encoding="utf-8")

    resp = client.get("/api/projects/demo/cockpit-queue")
    assert resp.status_code == 200
    data = resp.json()
    kinds = sorted({it["kind"] for it in data["items"]})
    assert kinds == ["audit_flag", "lit_gap", "review_proposal"]

    # Ordering: critical audit + concern review (mapped to critical) come first.
    assert data["items"][0]["severity"] == "critical"
    # Lit gap targets the right claim.
    lit = next(it for it in data["items"] if it["kind"] == "lit_gap")
    assert lit["target_claim_id"] == "cl.x.1"
    assert lit["target_section_id"] == "s.x"
    assert "add-source" in lit["actions"]

    assert data["sources"]["audit"] == "present"
    assert data["sources"]["lit_gaps"] == "present"
    assert data["sources"]["review"] == "present"
    assert data["sources"]["restructure"] == "missing"


def test_cockpit_claim_returns_claim_section_cluster_and_flags(
    client: TestClient, tmp_path: Path
) -> None:
    """The cockpit-claim endpoint should return the union of claim,
    section, cluster, rendered paragraph, and audit flags."""
    project = tmp_path / "demo"
    drafts_dir = project / ".lattice" / "drafts" / "academic"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    (drafts_dir / "cluster_c.x.1.md").write_text(
        "Rendered paragraph text.", encoding="utf-8")

    audit_path = project / ".lattice" / "audit_flags.json"
    audit_path.write_text(json.dumps({
        "academic": [{
            "flag_id": "f.1", "rule_id": "r.1", "category": "voice",
            "severity": "standard", "cluster_id": "c.x.1",
            "section_id": "s.x", "offending_text": "x", "suggestion": "y",
        }],
    }), encoding="utf-8")

    resp = client.get("/api/projects/demo/cockpit-claim/cl.x.1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["claim"]["claim_id"] == "cl.x.1"
    assert data["section"]["section_id"] == "s.x"
    assert data["cluster"]["cluster_id"] == "c.x.1"
    assert data["rendered_paragraph"] == "Rendered paragraph text."
    assert len(data["audit_flags"]) == 1
    assert "redraft-cluster" in data["available_actions"]


def test_cockpit_claim_404_for_unknown_claim(client: TestClient) -> None:
    resp = client.get("/api/projects/demo/cockpit-claim/cl.does.not.exist")
    assert resp.status_code == 404


def test_cockpit_action_endpoint_returns_501_with_structured_body(
    client: TestClient,
) -> None:
    """Phase 3 stub: action routes exist and return 501 with a body
    pointing at the phase that lands real behaviour. The frontend
    relies on the structured detail payload to show a useful toast."""
    resp = client.post(
        "/api/projects/demo/cockpit/actions/redraft-cluster",
        json={"claim_id": "cl.x.1", "cluster_id": "c.x.1"},
    )
    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert detail["status"] == "not_implemented"
    assert detail["action"] == "redraft-cluster"
    assert "Phase" in detail["next_phase"]


def test_cockpit_action_rejects_unknown_action(client: TestClient) -> None:
    resp = client.post("/api/projects/demo/cockpit/actions/nuke-everything",
                       json={})
    assert resp.status_code == 400


def test_harvard_bibliography_includes_doi_url() -> None:
    """The bibliography string should embed a https://doi.org/...
    URL when a DOI is present, so the frontend's autolink regex
    can wrap it in an anchor tag."""
    from lattice.graph.models import Citation
    from lattice.output.citation_formatter import format_citation
    c = Citation(
        authors=["Author"], year=2024, title="x",
        doi="10.1073/pnas.1018033108",
    )
    out = format_citation(c, "harvard")
    assert "https://doi.org/10.1073/pnas.1018033108" in out.bibliography
    out_apa = format_citation(c, "apa")
    assert "https://doi.org/10.1073/pnas.1018033108" in out_apa.bibliography


def test_citation_formatter_harvard_shape() -> None:
    """Harvard format should include 'et al.' for 3+ authors and a
    parenthetical year."""
    from lattice.graph.models import Citation
    from lattice.output.citation_formatter import format_citation
    c = Citation(
        authors=["Shai Danziger", "Jonathan Levav", "Liora Avnaim-Pesso"],
        year=2011,
        title="Extraneous factors in judicial decisions",
        container="Proceedings of the National Academy of Sciences",
        volume="108", issue="17", pages="6889-6892",
        doi="10.1073/pnas.1018033108",
    )
    out = format_citation(c, "harvard")
    assert out.style == "harvard"
    assert out.in_text == "(Danziger et al., 2011)"
    assert out.in_text_narrative == "Danziger et al. (2011)"
    assert "Extraneous factors" in out.bibliography
    assert "2011" in out.bibliography
    assert "Proceedings" in out.bibliography
    assert "10.1073" in out.bibliography


def test_citation_formatter_apa_uses_ampersand() -> None:
    """APA: '&' inside parentheses, 'and' in narrative."""
    from lattice.graph.models import Citation
    from lattice.output.citation_formatter import format_citation
    c = Citation(authors=["Alice Smith", "Bob Jones"], year=2024, title="A title")
    out = format_citation(c, "apa")
    assert "Smith, A." in out.bibliography
    assert "& Jones" in out.bibliography
    assert out.in_text == "(Smith & Jones, 2024)"
    assert out.in_text_narrative == "Smith and Jones (2024)"


def test_citation_formatter_supports_all_styles() -> None:
    """Every advertised style should produce a non-empty bibliography
    line for a basic single-author citation."""
    from lattice.graph.models import Citation
    from lattice.output.citation_formatter import format_citation, supported_styles
    c = Citation(authors=["Jane Doe"], year=2020, title="On things")
    for style in supported_styles():
        out = format_citation(c, style)
        assert out.style == style
        assert out.bibliography.strip(), f"empty bibliography for {style}"
        assert out.in_text.strip(), f"empty in-text for {style}"


def test_citation_formatter_rejects_unknown_style() -> None:
    from lattice.graph.models import Citation
    from lattice.output.citation_formatter import format_citation
    with pytest.raises(ValueError, match="Unknown citation style"):
        format_citation(Citation(title="x"), "made_up")


def test_references_endpoint_returns_manifest_shape(
    client: TestClient, tmp_path: Path
) -> None:
    """The /references endpoint should return a manifest with
    formatted citations + per-claim usage."""
    from lattice.graph.models import (
        Citation, Evidence, BindingStrength, Source, SourceMetadata,
        SourceType, Passage, PassageType, PassageLocation,
    )
    from lattice.graph.store import GraphStore
    project = tmp_path / "demo"

    # Seed a Source with a citation.
    src = Source(
        source_id="danziger_2011",
        type=SourceType.primary_paper,
        citation=Citation(
            authors=["Shai Danziger", "Jonathan Levav"], year=2011,
            title="Extraneous factors in judicial decisions",
            container="PNAS",
        ),
        passages=[Passage(
            id="p.1.1", text="Abstract: Are judicial rulings based solely on laws and facts?",
            location=PassageLocation(page=1, paragraph=1),
            type=PassageType.claim, char_count=64,
        )],
        metadata=SourceMetadata(
            peer_reviewed=True, primary=True,
            date_added=datetime.now(timezone.utc),
            file_path="refs/papers/danziger.pdf", hash="sha256:ab",
        ),
    )
    store = GraphStore.load(project)
    store.save_source(src)

    # Bind an Evidence to a claim so usage shows up.
    graph = store.get_graph()
    graph.claims[0].evidence.append(Evidence(
        source="danziger_2011", passage="p.1.1",
        binding_strength=BindingStrength.strong,
        quote_verbatim=True,
        quote_text="Abstract: Are judicial rulings based solely on laws and facts?",
        page=1,
    ))
    store.save_graph(graph)

    resp = client.get("/api/projects/demo/references?style=harvard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["style"] == "harvard"
    assert "harvard" in data["supported_styles"]
    assert data["totals"]["source_count"] == 1
    assert data["totals"]["used_count"] == 1
    assert data["totals"]["total_usages"] == 1
    ref = data["references"][0]
    assert ref["source_id"] == "danziger_2011"
    assert ref["formatted"]["style"] == "harvard"
    assert "(Danziger and Levav, 2011)" == ref["formatted"]["in_text"]
    assert len(ref["used_in_paper"]) == 1
    usage = ref["used_in_paper"][0]
    assert usage["binding_strength"] == "strong"
    assert usage["quote_verbatim"] is True


def test_references_endpoint_rejects_unknown_style(client: TestClient) -> None:
    resp = client.get("/api/projects/demo/references?style=cuneiform")
    assert resp.status_code == 400


def test_ai_enrichment_coerces_invalid_responses() -> None:
    """The coercer should drop rows missing a summary, validate
    citation_count_estimate as int, default unknown roles to
    'supporting_context', and clamp confidence to the allowed set."""
    from lattice.enricher.reference_ai_enrichment import _coerce_enrichment

    # Valid: full payload.
    out = _coerce_enrichment({
        "summary": "A study of X.",
        "key_findings": ["Finding 1", "Finding 2"],
        "field_position": "Seminal in subfield Y.",
        "citation_count_estimate": 1234,
        "confidence": "high",
        "usage_purposes": [
            {"claim_id": "cl.a.1", "role": "primary_evidence",
             "explanation": "establishes the empirical baseline"},
        ],
    })
    assert out is not None
    assert out["summary"] == "A study of X."
    assert len(out["key_findings"]) == 2
    assert out["citation_count_estimate"] == 1234
    assert out["confidence"] == "high"
    assert out["usage_purposes"][0]["role"] == "primary_evidence"

    # Invalid: no summary → drop entirely.
    assert _coerce_enrichment({"summary": ""}) is None
    assert _coerce_enrichment({}) is None
    assert _coerce_enrichment("not a dict") is None

    # Coercion: bad citation count → null.
    out2 = _coerce_enrichment({
        "summary": "x.", "citation_count_estimate": "many",
    })
    assert out2["citation_count_estimate"] is None

    # Coercion: unknown role → default supporting_context.
    out3 = _coerce_enrichment({
        "summary": "x.",
        "usage_purposes": [{"claim_id": "cl.a.1", "role": "made_up"}],
    })
    assert out3["usage_purposes"][0]["role"] == "supporting_context"

    # Coercion: confidence outside allowed set → unknown.
    out4 = _coerce_enrichment({"summary": "x.", "confidence": "very_high"})
    assert out4["confidence"] == "unknown"


def test_refresh_ai_endpoint_persists_enrichment(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """/references/refresh-ai should call the LLM, store the result
    keyed by source_id, and surface it on the next /references read."""
    from lattice.graph.models import (
        Citation, Source, SourceMetadata, SourceType, Evidence,
        BindingStrength, Passage, PassageLocation, PassageType,
    )
    from lattice.graph.store import GraphStore

    project = tmp_path / "demo"
    src = Source(
        source_id="enrich.test",
        type=SourceType.primary_paper,
        citation=Citation(
            authors=["Test Author"], year=2024, title="A test paper",
            container="Test Journal",
        ),
        passages=[Passage(
            id="p.1.1", text="Abstract content.",
            location=PassageLocation(page=1, paragraph=1),
            type=PassageType.claim, char_count=20,
        )],
        metadata=SourceMetadata(
            peer_reviewed=False, primary=False,
            date_added=datetime.now(timezone.utc),
            file_path="refs/papers/test.pdf", hash="sha256:0",
        ),
    )
    store = GraphStore.load(project)
    store.save_source(src)
    graph = store.get_graph()
    graph.claims[0].evidence.append(Evidence(
        source="enrich.test", passage="p.1.1",
        binding_strength=BindingStrength.strong, quote_verbatim=False,
    ))
    store.save_graph(graph)

    # Stub claude_available + ClaudeClient.
    monkeypatch.setattr(
        "lattice.web.app.claude_available", lambda: True, raising=False,
    )
    # Patch where the refresh endpoint imports them — they're imported
    # inside the endpoint, so we patch the source modules.
    from lattice.utils import llm as _llm_module
    monkeypatch.setattr(_llm_module, "claude_available", lambda: True)

    class _StubResp:
        text = "ignored"

    class _StubClient:
        def __init__(self, *a, **k): pass

        async def complete_json(self, **k):
            return {
                "summary": "Investigates a test phenomenon.",
                "key_findings": ["X is correlated with Y", "Z mediates"],
                "field_position": "Niche but well-cited in subfield.",
                "citation_count_estimate": 42,
                "confidence": "medium",
                "usage_purposes": [{
                    "claim_id": "cl.x.1",
                    "role": "primary_evidence",
                    "explanation": "Anchors the empirical baseline.",
                }],
            }, _StubResp()

    monkeypatch.setattr(_llm_module, "ClaudeClient", _StubClient)

    resp = client.post(
        "/api/projects/demo/references/refresh-ai",
        json={"cited_only": True},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["enriched_count"] == 1
    assert data["failed_count"] == 0
    assert data["errors"] == []
    assert "enrich.test" in data["source_ids"]

    # The enrichment file is persisted.
    enrich_path = project / ".lattice" / "reference_enrichment.json"
    assert enrich_path.exists()

    # And it surfaces on the next /references read.
    listed = client.get("/api/projects/demo/references").json()
    target = next(r for r in listed["references"] if r["source_id"] == "enrich.test")
    assert target["ai"] is not None
    assert "test phenomenon" in target["ai"]["summary"]
    assert target["ai"]["citation_count_estimate"] == 42
    assert target["ai"]["confidence"] == "medium"


def test_enrich_all_references_captures_per_source_errors() -> None:
    """If the LLM raises or returns malformed data for some sources,
    those should appear in the errors dict — not be silently swallowed."""
    import asyncio
    from datetime import datetime as _dt, timezone as _tz
    from lattice.enricher.reference_ai_enrichment import enrich_all_references
    from lattice.graph.models import (
        AuthorGraph, Citation, Claim, ClaimType, Confidence, Evidence,
        BindingStrength, Section, SectionRole, Source, SourceMetadata,
        SourceType,
    )

    now = _dt.now(_tz.utc)
    sources = [
        Source(
            source_id=f"s{i}", type=SourceType.primary_paper,
            citation=Citation(authors=["A"], year=2024, title=f"Paper {i}"),
            passages=[],
            metadata=SourceMetadata(
                peer_reviewed=False, primary=False, date_added=now,
                file_path=f"refs/papers/p{i}.pdf", hash="sha256:0",
            ),
        ) for i in range(3)
    ]
    section = Section(
        section_id="s.a", title="A", role=SectionRole.argumentative,
        position=1, claim_ids=["c.1"],
    )
    claim = Claim(
        claim_id="c.1", statement="x",
        type=ClaimType.user_synthesis, confidence=Confidence.medium,
        section_id="s.a", created_by="t",
        created_at=now, modified_at=now, author_origin=True,
        evidence=[Evidence(source=s.source_id, passage=f"p.1.1",
                           binding_strength=BindingStrength.strong,
                           quote_verbatim=False) for s in sources],
    )
    graph = AuthorGraph(
        project_name="t", sections=[section], claims=[claim],
        relationships=[], created_at=now, modified_at=now,
    )

    class _Stub:
        def __init__(self): self.calls = 0

        async def complete_json(self, **k):
            self.calls += 1
            if self.calls == 1:
                return {
                    "summary": "Good summary.",
                    "key_findings": ["Finding"],
                    "field_position": "Niche.",
                    "citation_count_estimate": 10,
                    "confidence": "low",
                    "usage_purposes": [],
                }, None
            if self.calls == 2:
                # Missing summary → coercer drops it → EnrichmentError.
                return {"key_findings": ["X"]}, None
            # LLM exception.
            raise RuntimeError("subprocess died")

    enrichments, errors = asyncio.run(
        enrich_all_references(sources, graph, _Stub(), cited_only=True)
    )
    assert len(enrichments) == 1
    assert len(errors) == 2
    assert any("missing required `summary`" in m for m in errors.values())
    assert any("subprocess died" in m for m in errors.values())


def test_refresh_ai_endpoint_rejects_when_no_sources(
    client: TestClient,
) -> None:
    """A project with no indexed sources should get a 400 — there's
    nothing to enrich."""
    resp = client.post(
        "/api/projects/demo/references/refresh-ai",
        json={"cited_only": True},
    )
    # demo has no sources by default.
    assert resp.status_code == 400


def test_export_teaching_deck_produces_pptx(
    client: TestClient, tmp_path: Path
) -> None:
    """The teaching-deck endpoint should generate a PPTX file with at
    least a title slide + section slides."""
    resp = client.get("/api/projects/demo/export/teaching-deck")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert len(resp.content) > 5000  # PPTX has minimum overhead

    # Check the file persisted to outputs/.
    outputs = tmp_path / "demo" / "outputs"
    pptx_files = list(outputs.glob("teaching_deck_*.pptx"))
    assert len(pptx_files) >= 1


def test_export_teaching_deck_rejects_empty_project(
    client: TestClient, tmp_path: Path
) -> None:
    """A project with no sections should return 400 — there's no
    content to put on slides."""
    # Create a project with no sections in the graph.
    project = tmp_path / "empty"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    (project / ".lattice").mkdir()
    (project / ".lattice" / "author_graph.json").write_text(
        json.dumps({
            "project_name": "empty",
            "thesis_statement": None, "thesis_argued": None,
            "thesis_argued_confidence": None, "thesis_argued_note": None,
            "sections": [], "claims": [], "relationships": [],
            "created_at": "2026-01-01T00:00:00Z",
            "modified_at": "2026-01-01T00:00:00Z",
        }),
        encoding="utf-8",
    )
    resp = client.get("/api/projects/empty/export/teaching-deck")
    assert resp.status_code == 400


def test_extract_references_isolates_numbered_list_without_heading() -> None:
    """When the bibliography section has no 'References' heading but
    starts with `1. Author (Year)…` style citations, the isolator
    should pick up the start of the numbered list."""
    from lattice.enricher.reference_extraction import (
        _isolate_references_section,
    )
    text = (
        "Long body of paper text discussing methodology. " * 200 + "\n\n"
        "1. Leiter B (2005) The Blackwell Guide. (Blackwell, Oxford).\n"
        "2. Holmes OW (1881) The Common Law (Little, Brown).\n"
        "3. Frank J (1930) Law and the Modern Mind (Brentano's, NY).\n"
    )
    isolated = _isolate_references_section(text)
    assert "Leiter" in isolated
    assert "Long body" not in isolated  # body trimmed away


def test_pdf_to_markdown_detects_section_headings() -> None:
    """The heuristic converter should turn numbered headings,
    all-caps headings, and 'Abstract'-style labels into markdown
    headings."""
    from lattice.ingester.pdf_to_markdown import pdf_text_to_markdown
    pages = [
        "Some Paper Title\n"
        "Author Name\n\n"
        "Abstract\n\n"
        "We test the common caricature of realism.\n\n"
        "1. Introduction\n\n"
        "Are judicial rulings based on laws?\n\n"
        "1.1 Background\n\n"
        "Legal formalism holds that judges...\n\n"
        "METHODS\n\n"
        "We analysed 1,112 parole rulings.\n"
    ]
    md = pdf_text_to_markdown(pages)
    assert "# Abstract" in md
    assert "# 1. Introduction" in md
    assert "## 1.1 Background" in md
    # "METHODS" matches the standard L1 label list so it gets `#`,
    # not `##` like an arbitrary all-caps line would.
    assert "# Methods" in md


def test_pdf_to_markdown_strips_page_numbers_and_running_headers() -> None:
    """Page numbers + recurring headers/footers should be dropped."""
    from lattice.ingester.pdf_to_markdown import pdf_text_to_markdown
    pages = [
        "Danziger et al.\nBody of page 1.\n1\n",
        "Danziger et al.\nBody of page 2.\n2\n",
        "Danziger et al.\nBody of page 3.\n3\n",
        "Danziger et al.\nBody of page 4.\n4\n",
    ]
    md = pdf_text_to_markdown(pages)
    assert "Danziger et al." not in md
    # Page-number digits on their own line are dropped.
    for n in ("\n1\n", "\n2\n", "\n3\n", "\n4\n"):
        assert n not in md
    assert "Body of page 1." in md


def test_pdf_to_markdown_rejoins_hyphenated_breaks() -> None:
    """A line ending with `-` followed by a lowercase continuation
    on the next line should be rejoined."""
    from lattice.ingester.pdf_to_markdown import pdf_text_to_markdown
    pages = ["A useful and extra-\nordinary fact about decisions.\n"]
    md = pdf_text_to_markdown(pages)
    assert "extraordinary" in md
    assert "extra-\n" not in md


def test_pdf_to_markdown_marks_bullet_glyphs() -> None:
    """Lines starting with •, ▪, etc. should become markdown `-`
    bullets."""
    from lattice.ingester.pdf_to_markdown import pdf_text_to_markdown
    pages = ["• First point\n• Second point\n– Third point\n"]
    md = pdf_text_to_markdown(pages)
    assert "- First point" in md
    assert "- Second point" in md
    assert "- Third point" in md


def test_originals_endpoint_lists_present_files(
    client: TestClient, tmp_path: Path
) -> None:
    """The /originals endpoint should surface outline.raw.md +
    outline.md + outline.original.{pdf,docx} when they exist."""
    structure_dir = tmp_path / "demo" / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nx\n", encoding="utf-8"
    )
    (structure_dir / "outline.raw.md").write_text(
        "Original paper text body.\n", encoding="utf-8"
    )
    (structure_dir / "outline.original.pdf").write_bytes(
        b"%PDF-1.4 fake\n%%EOF\n"
    )

    resp = client.get("/api/projects/demo/originals")
    assert resp.status_code == 200
    data = resp.json()
    roles = {o["role"] for o in data["originals"]}
    assert "current_outline" in roles
    assert "raw_text" in roles
    assert "original_pdf" in roles
    # Word count surfaces only for markdown entries.
    raw = next(o for o in data["originals"] if o["role"] == "raw_text")
    assert raw["word_count"] > 0
    assert raw["kind"] == "markdown"
    pdf = next(o for o in data["originals"] if o["role"] == "original_pdf")
    assert pdf["kind"] == "pdf"


def test_originals_file_endpoint_serves_md_inline_pdf_as_download(
    client: TestClient, tmp_path: Path
) -> None:
    structure_dir = tmp_path / "demo" / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    (structure_dir / "outline.raw.md").write_text(
        "# RAW\n\nbody", encoding="utf-8"
    )
    (structure_dir / "outline.original.pdf").write_bytes(
        b"%PDF-1.4 fake\n%%EOF\n"
    )

    md_resp = client.get("/api/projects/demo/originals/outline.raw.md")
    assert md_resp.status_code == 200
    assert "# RAW" in md_resp.text

    pdf_resp = client.get("/api/projects/demo/originals/outline.original.pdf")
    assert pdf_resp.status_code == 200
    assert pdf_resp.content.startswith(b"%PDF-1.4")


def test_originals_endpoint_rejects_non_whitelisted_filenames(
    client: TestClient,
) -> None:
    resp = client.get("/api/projects/demo/originals/secret.txt")
    assert resp.status_code == 404


def test_upload_original_paper_saves_pdf_to_structure(
    client: TestClient, tmp_path: Path
) -> None:
    fake_pdf = b"%PDF-1.4 stub\n%%EOF\n"
    resp = client.post(
        "/api/projects/demo/structure/original",
        files={"file": ("paper.pdf", fake_pdf, "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    target = tmp_path / "demo" / "structure" / "outline.original.pdf"
    assert target.exists()
    assert target.read_bytes() == fake_pdf


def test_upload_original_paper_rejects_other_extensions(
    client: TestClient,
) -> None:
    resp = client.post(
        "/api/projects/demo/structure/original",
        files={"file": ("paper.txt", b"text", "text/plain")},
    )
    assert resp.status_code == 400


def test_extract_citations_isolates_references_section() -> None:
    """The extractor's text-isolator should pull only the
    bibliography portion when a 'References' heading is present."""
    from lattice.enricher.reference_extraction import (
        _isolate_references_section,
    )
    text = (
        "Body of paper text. Introduction. Methods. Results.\n\n"
        "References\n\n"
        "Smith, J. (2020). A title. *Journal*, 1, 1-2.\n"
        "Doe, J. (2021). Another title. *Journal*, 2, 3-4.\n"
    )
    isolated = _isolate_references_section(text)
    assert "Body of paper" not in isolated
    assert "Smith, J." in isolated
    assert "Doe, J." in isolated


def test_extract_citations_drops_invalid_rows(monkeypatch) -> None:
    """The extractor should drop rows with neither title nor authors."""
    import asyncio
    from lattice.enricher.reference_extraction import (
        extract_citations_from_text,
    )

    class _Stub:
        async def complete_json(self, **k):
            return [
                {"authors": ["John Smith"], "year": 2020, "title": "Good ref"},
                {"authors": [], "year": None, "title": ""},  # dropped
                {"authors": ["Jane Doe"], "year": "twenty twenty", "title": "Bad year"},
                "not a dict",  # dropped
            ], None

    citations = asyncio.run(extract_citations_from_text("dummy", _Stub()))
    assert len(citations) == 2
    assert citations[0].title == "Good ref"
    assert citations[0].year == 2020
    # Bad year coerced to None, but title kept.
    assert citations[1].title == "Bad year"
    assert citations[1].year is None


def test_citation_to_synthetic_source_builds_stable_id() -> None:
    """The slug-based source_id should be deterministic from author +
    year so the same reference on different runs gets the same id."""
    from lattice.graph.models import Citation
    from lattice.enricher.reference_extraction import (
        citation_to_synthetic_source,
    )
    c = Citation(authors=["Shai Danziger"], year=2011, title="x")
    s = citation_to_synthetic_source(c)
    assert s.source_id == "danziger_2011"
    # Empty authors → falls back to title slug.
    c2 = Citation(authors=[], year=None, title="A study with no author")
    s2 = citation_to_synthetic_source(c2)
    assert s2.source_id.startswith("a_study")
    assert "nodate" in s2.source_id


def test_manual_reference_endpoint_creates_source(
    client: TestClient, tmp_path: Path
) -> None:
    resp = client.post(
        "/api/projects/demo/references/manual",
        json={
            "authors": ["Test Author"], "year": 2024,
            "title": "A manual reference",
            "container": "Test J.",
            "doi": "10.0/abc",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["saved"] is True
    assert data["source_id"] == "author_2024"

    # The source is now in source_store.
    sources_resp = client.get("/api/projects/demo/sources").json()
    assert any(
        s.get("source_id") == "author_2024"
        for s in sources_resp["indexed"]
    )

    # And the persisted references file has been refreshed.
    md_path = tmp_path / "demo" / "references.md"
    assert md_path.exists()


def test_manual_reference_endpoint_dedups_id_collisions(
    client: TestClient,
) -> None:
    """Two manual entries with the same first-author + year should
    get distinct source_ids."""
    body = {"authors": ["Same Author"], "year": 2020, "title": "First"}
    a = client.post("/api/projects/demo/references/manual", json=body)
    body["title"] = "Second"
    b = client.post("/api/projects/demo/references/manual", json=body)
    assert a.json()["source_id"] == "author_2020"
    assert b.json()["source_id"] == "author_2020_2"


def test_extract_endpoint_rejects_when_no_text_or_source(
    client: TestClient,
) -> None:
    resp = client.post(
        "/api/projects/demo/references/extract", json={},
    )
    assert resp.status_code == 400


def test_accept_extracted_references_creates_sources(
    client: TestClient,
) -> None:
    resp = client.post(
        "/api/projects/demo/references/extract/accept",
        json={"citations": [
            {"authors": ["Acc Test"], "year": 2024, "title": "Test"},
            {"authors": ["Acc Two"], "year": 2025, "title": "Test 2"},
        ]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["added"]) == 2
    assert data["skipped"] == []


def test_write_project_references_creates_files_at_root(
    tmp_path: Path,
) -> None:
    """``write_project_references`` should write ``references.json``
    and ``references.md`` to the project root, with all six styles
    pre-formatted in the JSON."""
    from datetime import datetime as _dt, timezone as _tz
    from lattice.graph.models import (
        AuthorGraph, Citation, Claim, ClaimRoleInCluster, ClaimType,
        Cluster, ClusterRole, Confidence, Evidence, BindingStrength,
        Passage, PassageLocation, PassageType, ProseState,
        Section, SectionRole, Source, SourceMetadata, SourceType,
    )
    from lattice.graph.store import GraphStore
    from lattice.output.references_manifest import write_project_references

    project = tmp_path / "p"
    now = _dt.now(_tz.utc)
    store = GraphStore.load(project)

    # Source + a claim that cites it.
    src = Source(
        source_id="src.test",
        type=SourceType.primary_paper,
        citation=Citation(
            authors=["Foo Bar"], year=2024, title="Test paper",
            container="J. Tests",
        ),
        passages=[Passage(
            id="p.1.1", text="Abstract: testing",
            location=PassageLocation(page=1, paragraph=1),
            type=PassageType.claim, char_count=20,
        )],
        metadata=SourceMetadata(
            peer_reviewed=True, primary=True, date_added=now,
            file_path="refs/papers/test.pdf", hash="sha256:0",
        ),
    )
    store.save_source(src)
    section = Section(
        section_id="s.a", title="A", role=SectionRole.argumentative,
        position=1, claim_ids=["c.1"],
    )
    claim = Claim(
        claim_id="c.1", statement="A claim",
        type=ClaimType.empirical, confidence=Confidence.medium,
        section_id="s.a", created_by="t",
        created_at=now, modified_at=now,
        evidence=[Evidence(
            source="src.test", passage="p.1.1",
            binding_strength=BindingStrength.strong,
            quote_verbatim=True, quote_text="testing",
            page=1,
        )],
    )
    store.save_graph(AuthorGraph(
        project_name="p", sections=[section], claims=[claim],
        relationships=[], created_at=now, modified_at=now,
    ))

    paths = write_project_references(project, cited_only=True)
    assert paths["json"].name == "references.json"
    assert paths["md"].name == "references.md"
    assert paths["json"].parent == project
    assert paths["md"].parent == project

    json_data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert json_data["scope"] == "cited_only"
    assert json_data["totals"]["cited_source_count"] == 1
    ref = json_data["references"][0]
    assert ref["source_id"] == "src.test"
    # Every supported style is pre-formatted.
    style_names = {s["style"] for s in ref["all_styles"]}
    assert {"harvard", "apa", "chicago_author_date", "mla", "vancouver", "ieee"}.issubset(style_names)

    md = paths["md"].read_text(encoding="utf-8")
    assert "Test paper" in md
    assert "src.test" in md
    assert "Bibliography by style" in md
    assert "Harvard" in md
    assert "APA" in md.upper()


def test_write_project_references_cited_only_excludes_orphan_sources(
    tmp_path: Path,
) -> None:
    """An indexed source that no claim cites should NOT appear in
    the persisted file when cited_only=True."""
    from datetime import datetime as _dt, timezone as _tz
    from lattice.graph.models import (
        Citation, Source, SourceMetadata, SourceType,
    )
    from lattice.graph.store import GraphStore
    from lattice.output.references_manifest import write_project_references

    project = tmp_path / "p2"
    now = _dt.now(_tz.utc)
    store = GraphStore.load(project)
    store.save_source(Source(
        source_id="orphan.src",
        type=SourceType.note,
        citation=Citation(authors=["Nobody"], year=2000, title="Unused"),
        passages=[],
        metadata=SourceMetadata(
            peer_reviewed=False, primary=False, date_added=now,
            file_path="refs/notes/x.md", hash="sha256:0",
        ),
    ))

    paths = write_project_references(project, cited_only=True)
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["totals"]["cited_source_count"] == 0
    assert data["references"] == []
    md = paths["md"].read_text(encoding="utf-8")
    assert "No sources are currently cited" in md

    # With cited_only=False, the orphan should appear.
    paths2 = write_project_references(project, cited_only=False)
    data2 = json.loads(paths2["json"].read_text(encoding="utf-8"))
    assert data2["scope"] == "all_indexed"
    assert data2["totals"]["cited_source_count"] == 1
    assert data2["references"][0]["source_id"] == "orphan.src"


def test_save_references_endpoint_writes_files(
    client: TestClient, tmp_path: Path
) -> None:
    """POST /references/save should write the project files and
    return the absolute paths."""
    resp = client.post("/api/projects/demo/references/save", json={"cited_only": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["saved"] is True
    assert data["cited_only"] is False
    json_path = Path(data["json_path"])
    md_path = Path(data["md_path"])
    assert json_path.exists()
    assert md_path.exists()
    assert json_path.parent == (tmp_path / "demo").resolve()


def test_references_file_endpoint_serves_md_and_json(
    client: TestClient, tmp_path: Path
) -> None:
    """GET /references-file?fmt=md should return the markdown body."""
    # First persist the files.
    client.post("/api/projects/demo/references/save", json={"cited_only": True})

    md_resp = client.get("/api/projects/demo/references-file?fmt=md")
    assert md_resp.status_code == 200
    assert "References" in md_resp.text

    json_resp = client.get("/api/projects/demo/references-file?fmt=json")
    assert json_resp.status_code == 200
    parsed = json.loads(json_resp.text)
    assert parsed["scope"] == "cited_only"


def test_references_endpoint_uses_user_about_overrides(
    client: TestClient, tmp_path: Path
) -> None:
    """Hand-written 'about' summaries persisted via PUT
    /references/{source_id}/about should take precedence over the
    auto-extracted snippet."""
    from lattice.graph.models import (
        Citation, Source, SourceMetadata, SourceType,
    )
    from lattice.graph.store import GraphStore
    project = tmp_path / "demo"
    src = Source(
        source_id="src.1",
        type=SourceType.primary_paper,
        citation=Citation(authors=["Author"], year=2020, title="T"),
        passages=[],
        metadata=SourceMetadata(
            peer_reviewed=False, primary=False,
            date_added=datetime.now(timezone.utc),
            file_path="refs/papers/x.pdf", hash="sha256:0",
        ),
    )
    GraphStore.load(project).save_source(src)

    # Save a custom about.
    save = client.put(
        "/api/projects/demo/references/src.1/about",
        json={"about": "This paper proves X via Y. Used to ground claim Z."},
    )
    assert save.status_code == 200
    assert save.json()["saved"] is True

    listed = client.get("/api/projects/demo/references").json()
    matching = next(r for r in listed["references"] if r["source_id"] == "src.1")
    assert "proves X" in matching["about"]


def test_infer_relationships_filters_invalid_rows(monkeypatch) -> None:
    """The relationship inferer should drop rows referencing
    unknown claim ids, self-referential rows, and unknown types
    rather than failing the whole call."""
    import asyncio
    from datetime import datetime, timezone
    from lattice.enricher.relationship_inference import infer_relationships
    from lattice.graph.models import (
        AuthorGraph, Claim, ClaimType, Confidence, Section, SectionRole,
    )

    now = datetime.now(timezone.utc)
    section = Section(
        section_id="s.a", title="A", role=SectionRole.argumentative,
        position=1, claim_ids=["cl.a.1", "cl.a.2"],
    )
    claims = [
        Claim(
            claim_id=cid, statement=f"claim {cid}",
            type=ClaimType.user_synthesis, confidence=Confidence.medium,
            section_id="s.a", created_by="t",
            created_at=now, modified_at=now, author_origin=True,
        ) for cid in ("cl.a.1", "cl.a.2")
    ]
    graph = AuthorGraph(
        project_name="p", sections=[section], claims=claims,
        relationships=[], created_at=now, modified_at=now,
    )

    class _Stub:
        async def complete_json(self, **k):
            return [
                {"from": "cl.a.1", "to": "cl.a.2", "type": "supports",
                 "strength": "direct", "note": "ok"},
                # Invalid: references unknown claim
                {"from": "cl.a.1", "to": "cl.x.99", "type": "supports",
                 "strength": "direct", "note": "bad"},
                # Invalid: self-reference
                {"from": "cl.a.1", "to": "cl.a.1", "type": "supports",
                 "strength": "direct", "note": "self"},
                # Unknown type and strength → coerced to unlabelled / inferred
                {"from": "cl.a.2", "to": "cl.a.1", "type": "made_up",
                 "strength": "weird", "note": "coerced"},
                # Duplicate of first → deduplicated
                {"from": "cl.a.1", "to": "cl.a.2", "type": "supports",
                 "strength": "direct", "note": "dup"},
                # Wrong shape → dropped
                "this is not a dict",
            ], None

    rels = asyncio.run(infer_relationships(graph, _Stub()))
    assert len(rels) == 2  # one supports + one coerced unlabelled
    types = sorted(r.type.value for r in rels)
    assert types == ["supports", "unlabelled"]


def test_merge_inferred_relationships_dedup_against_existing(monkeypatch) -> None:
    """If the graph already has a relationship of the same shape,
    the inferred one is skipped. Author-tagged wins."""
    from datetime import datetime, timezone
    from lattice.enricher.relationship_inference import (
        merge_inferred_relationships,
    )
    from lattice.graph.models import (
        AuthorGraph, Claim, ClaimType, Confidence, Relationship,
        RelationshipStrength, RelationshipType, Section, SectionRole,
    )

    now = datetime.now(timezone.utc)
    section = Section(
        section_id="s.a", title="A", role=SectionRole.argumentative,
        position=1, claim_ids=["cl.a.1", "cl.a.2"],
    )
    claims = [
        Claim(
            claim_id=cid, statement="x",
            type=ClaimType.user_synthesis, confidence=Confidence.medium,
            section_id="s.a", created_by="t",
            created_at=now, modified_at=now, author_origin=True,
        ) for cid in ("cl.a.1", "cl.a.2")
    ]
    existing = Relationship(
        rel_id="rel.user.1", type=RelationshipType.supports,
        **{"from": "cl.a.1"}, to="cl.a.2",
        strength=RelationshipStrength.direct, note="hand-tagged",
        created_by="user", created_at=now,
    )
    graph = AuthorGraph(
        project_name="p", sections=[section], claims=claims,
        relationships=[existing], created_at=now, modified_at=now,
    )
    inferred = [
        Relationship(
            rel_id="rel.inferred.1", type=RelationshipType.supports,
            **{"from": "cl.a.1"}, to="cl.a.2",
            strength=RelationshipStrength.inferred, note="inferred dup",
            created_by="relationship_inference", created_at=now,
        ),
        Relationship(
            rel_id="rel.inferred.2", type=RelationshipType.extends,
            **{"from": "cl.a.2"}, to="cl.a.1",
            strength=RelationshipStrength.inferred, note="new",
            created_by="relationship_inference", created_at=now,
        ),
    ]
    added, skipped = merge_inferred_relationships(graph, inferred)
    assert added == 1
    assert skipped == 1
    assert len(graph.relationships) == 2


def test_changelogs_endpoint_empty_for_new_project(client: TestClient) -> None:
    """A project with no runs has no changelogs."""
    resp = client.get("/api/projects/demo/changelogs")
    assert resp.status_code == 200
    assert resp.json() == {"changelogs": []}


def test_changelog_endpoint_serves_specific_file(
    client: TestClient, tmp_path: Path
) -> None:
    """The /changelogs/{filename} endpoint reads markdown body inline."""
    cl_dir = tmp_path / "demo" / ".lattice" / "changelogs"
    cl_dir.mkdir(parents=True, exist_ok=True)
    (cl_dir / "20260101_120000_quick.md").write_text(
        "# Changelog · test\n\nbody", encoding="utf-8"
    )
    resp = client.get("/api/projects/demo/changelogs")
    assert resp.status_code == 200
    listing = resp.json()["changelogs"]
    assert len(listing) == 1
    assert listing[0]["filename"] == "20260101_120000_quick.md"

    body = client.get(
        "/api/projects/demo/changelogs/20260101_120000_quick.md"
    )
    assert body.status_code == 200
    assert "# Changelog · test" in body.text


def test_changelog_endpoint_rejects_path_traversal(client: TestClient) -> None:
    resp = client.get("/api/projects/demo/changelogs/..%2Fconfig.yml")
    assert resp.status_code in (400, 404)


def test_changelog_endpoint_rejects_non_markdown(
    client: TestClient, tmp_path: Path
) -> None:
    cl_dir = tmp_path / "demo" / ".lattice" / "changelogs"
    cl_dir.mkdir(parents=True, exist_ok=True)
    (cl_dir / "secrets.txt").write_text("nope", encoding="utf-8")
    resp = client.get("/api/projects/demo/changelogs/secrets.txt")
    assert resp.status_code == 400


def test_capture_project_state_handles_missing_files(tmp_path: Path) -> None:
    """``capture_project_state`` should never raise on an empty project."""
    from lattice.web.runner import capture_project_state
    project = tmp_path / "fresh"
    project.mkdir()
    state = capture_project_state(project)
    assert state["section_count"] == 0
    assert state["claim_count"] == 0
    assert state["cluster_count"] == 0
    assert state["paper_word_count"] == 0


def test_write_changelog_produces_readable_markdown(tmp_path: Path) -> None:
    """``write_changelog`` should produce a markdown file under
    ``.lattice/changelogs/`` summarising the delta between pre + post
    snapshots."""
    from lattice.web.runner import RunRequest, RunResult, write_changelog

    project = tmp_path / "proj"
    project.mkdir()
    request = RunRequest(
        project_path=project, voice_name="academic", level="standard",
    )
    result = RunResult(
        rendered_clusters=3, total_clusters=4,
        elapsed_seconds=12.5, finalise_succeeded=True,
        notes=["all good"],
    )
    pre = {
        "section_count": 5, "claim_count": 12, "cluster_count": 4,
        "cluster_states": {"c.1": "generated", "c.2": "failed"},
        "audit_flag_count": 8,
        "audit_flags_by_severity": {"critical": 1, "standard": 5, "minor": 2},
        "paper_word_count": 1200,
        "outline_chars": 4500,
        "outline_first_lines": ["# THESIS"],
    }
    post = {
        "section_count": 5, "claim_count": 12, "cluster_count": 4,
        "cluster_states": {"c.1": "generated", "c.2": "generated"},
        "audit_flag_count": 5,
        "audit_flags_by_severity": {"critical": 0, "standard": 4, "minor": 1},
        "paper_word_count": 1450,
        "outline_chars": 4500,
        "outline_first_lines": ["# THESIS"],
    }
    path = write_changelog(request, result, "academic", pre, post)
    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert "Review level" in body
    assert "standard" in body
    # Cluster c.2 went from failed → generated; should be in the diff.
    assert "c.2" in body
    assert "failed" in body and "generated" in body
    # Word delta surfaces.
    assert "+250 from 1200" in body
    # latest.md mirror is also written.
    latest = project / ".lattice" / "changelogs" / "latest.md"
    assert latest.exists()
    assert latest.read_text(encoding="utf-8") == body


def test_run_history_endpoint_empty_for_new_project(client: TestClient) -> None:
    """A project with no runs should return an empty history shape."""
    resp = client.get("/api/projects/demo/run-history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["history"] == []
    assert data["latest_by_level"] == {}
    assert data["summary"]["total_runs"] == 0
    assert data["summary"]["successful_deliveries"] == 0
    assert data["summary"]["levels_completed_successfully"] == []


def test_run_history_endpoint_summarises_by_level(
    client: TestClient, tmp_path: Path
) -> None:
    """When the history file has multiple runs at different levels,
    the summary should pick the most recent run per level and list
    the levels that have at least one successful delivery."""
    history = [
        {
            "level": "quick", "voice": "academic",
            "finished_at": "2026-01-01T10:00:00+00:00",
            "elapsed_seconds": 10.0,
            "finalise_succeeded": True,
            "rendered_clusters": 3, "total_clusters": 3,
            "audit_flags": 0,
            "final_path": "/p/paper.md",
            "voice_review_path": None, "source_gap_path": None,
            "notes": [],
        },
        {
            "level": "quick", "voice": "academic",
            "finished_at": "2026-01-02T10:00:00+00:00",
            "elapsed_seconds": 9.0,
            "finalise_succeeded": True,
            "rendered_clusters": 3, "total_clusters": 3,
            "audit_flags": 0, "final_path": "/p/paper.md",
            "voice_review_path": None, "source_gap_path": None,
            "notes": [],
        },
        {
            "level": "standard", "voice": "academic",
            "finished_at": "2026-01-03T10:00:00+00:00",
            "elapsed_seconds": 60.0,
            "finalise_succeeded": True,
            "rendered_clusters": 3, "total_clusters": 3,
            "audit_flags": 5, "final_path": "/p/paper.md",
            "voice_review_path": "/p/voice.md",
            "source_gap_path": None, "notes": [],
        },
        {
            "level": "deep", "voice": "academic",
            "finished_at": "2026-01-04T10:00:00+00:00",
            "elapsed_seconds": 120.0,
            "finalise_succeeded": False,
            "rendered_clusters": 2, "total_clusters": 3,
            "audit_flags": 1, "final_path": None,
            "voice_review_path": None, "source_gap_path": None,
            "notes": ["recovery couldn't fix 1"],
        },
    ]
    (tmp_path / "demo" / ".lattice" / "run_history.json").write_text(
        json.dumps(history), encoding="utf-8"
    )

    resp = client.get("/api/projects/demo/run-history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["total_runs"] == 4
    assert data["summary"]["successful_deliveries"] == 3
    # Deep didn't succeed → should NOT appear in completed levels.
    assert set(data["summary"]["levels_completed_successfully"]) == {"quick", "standard"}
    # Latest run per level: most recent quick is the second one.
    assert data["latest_by_level"]["quick"]["finished_at"] == "2026-01-02T10:00:00+00:00"
    assert data["latest_by_level"]["deep"]["finalise_succeeded"] is False


def test_record_run_history_appends_record(tmp_path: Path) -> None:
    """``record_run_history`` should append a new record to the file
    and cap the list at 50 entries."""
    from lattice.web.runner import (
        record_run_history, RunRequest, RunResult, read_run_history,
    )

    project = tmp_path / "p"
    (project / ".lattice").mkdir(parents=True)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    result = RunResult(
        rendered_clusters=2, total_clusters=2,
        elapsed_seconds=5.0, finalise_succeeded=True,
    )
    record_run_history(request, result, "academic")

    history = read_run_history(project)
    assert len(history) == 1
    assert history[0]["level"] == "quick"
    assert history[0]["finalise_succeeded"] is True

    # Append 60 more — total should be capped at 50.
    for _ in range(60):
        record_run_history(request, result, "academic")
    history = read_run_history(project)
    assert len(history) == 50


def test_run_pipeline_redrafts_clusters_after_relationship_inference(
    tmp_path: Path, monkeypatch
) -> None:
    """When relationship inference adds edges to claims that already
    have rendered prose, the affected clusters must be marked dirty
    and redrafted — otherwise the new relationships would never make
    it into the text."""
    from lattice.graph.models import (
        AuthorGraph, Claim, ClaimRoleInCluster, ClaimType, Cluster,
        ClusterRole, Confidence, ProseState, Section, SectionRole,
    )
    from lattice.graph.store import GraphStore
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "redraft"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nx.\n\n# A. body\n\n  - claim [user_synthesis]\n  - other [user_synthesis]\n\n"
        "# B. End [role: conclusion]\n\n  - close [user_synthesis]\n",
        encoding="utf-8",
    )

    store = GraphStore.load(project)
    now = datetime.now(timezone.utc)
    sections = [
        Section(section_id="s.a", title="body", role=SectionRole.argumentative,
                position=1, claim_ids=["c.1", "c.2"]),
        Section(section_id="s.b", title="End", role=SectionRole.conclusion,
                position=2, claim_ids=["c.3"]),
    ]
    claims = [
        Claim(claim_id="c.1", statement="A", type=ClaimType.user_synthesis,
              confidence=Confidence.medium, section_id="s.a", created_by="t",
              created_at=now, modified_at=now, author_origin=True),
        Claim(claim_id="c.2", statement="B", type=ClaimType.user_synthesis,
              confidence=Confidence.medium, section_id="s.a", created_by="t",
              created_at=now, modified_at=now, author_origin=True),
        Claim(claim_id="c.3", statement="C", type=ClaimType.user_synthesis,
              confidence=Confidence.medium, section_id="s.b", created_by="t",
              created_at=now, modified_at=now, author_origin=True),
    ]
    store.save_graph(AuthorGraph(
        project_name="redraft", sections=sections, claims=claims,
        relationships=[], created_at=now, modified_at=now,
    ))
    # All clusters start in `generated` state — i.e. cached prose.
    for cid, claim_id, section_id in [("c.a.1", "c.1", "s.a"), ("c.a.2", "c.2", "s.a"), ("c.b.1", "c.3", "s.b")]:
        store.save_cluster(Cluster(
            cluster_id=cid, section_id=section_id, position=1,
            role=ClusterRole.evidence,
            claim_sequence=[ClaimRoleInCluster(
                claim_id=claim_id, role_in_cluster=ClusterRole.evidence,
            )],
            prose_state=ProseState.generated,
        ))

    # Stub the LLM so relationship inference returns one edge
    # (c.1 → c.2) — so c.a.1 + c.a.2 should both be marked dirty
    # and redrafted, but c.b.1 should be left alone.
    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)

    class _StubResp:
        def __init__(self, t): self.text = t

    class _StubClient:
        def __init__(self, *a, **k): pass

        async def complete(self, **k):
            return _StubResp("ignored")

        async def complete_json(self, **k):
            return [{
                "from": "c.1", "to": "c.2", "type": "supports",
                "strength": "direct", "note": "inferred link",
            }], _StubResp("ignored")

    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _StubClient)

    # Track which clusters were rendered (and on which call).
    render_calls = {"clusters_rendered": []}

    async def _track_render(self, *, force=False, progress=None):
        s = self.store
        for c in s.list_clusters():
            if c.prose_state == ProseState.dirty:
                c.prose_state = ProseState.generated
                s.save_cluster(c)
                render_calls["clusters_rendered"].append(c.cluster_id)
        return {}

    monkeypatch.setattr(
        "lattice.web.runner.ChunkedRenderer.render_all", _track_render
    )
    monkeypatch.setattr(
        "lattice.web.runner.DocumentFinaliser.finalise",
        lambda self: self.project_path / "outputs" / "paper.academic.md",
    )

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="standard",
    )
    asyncio.run(run_pipeline(request, progress))

    events = []
    while not queue.empty(): events.append(queue.get_nowait())
    phases = [e["phase"] for e in events if e["type"] == "phase_begun"]
    assert "relationship_inference" in phases
    assert "redraft" in phases, (
        f"redraft stage should have fired; phases={phases}"
    )
    # Both touched clusters got redrafted; the unrelated conclusion
    # cluster did NOT.
    assert "c.a.1" in render_calls["clusters_rendered"]
    assert "c.a.2" in render_calls["clusters_rendered"]
    assert "c.b.1" not in render_calls["clusters_rendered"]


def test_run_pipeline_auto_recovery_retries_failed_clusters(
    tmp_path: Path, monkeypatch
) -> None:
    """If the first finalise refuses, the runner should reset failed
    clusters to dirty, re-render them, and retry finalise — without
    needing to be on the deep review level."""
    from lattice.graph.models import (
        AuthorGraph, Claim, ClaimRoleInCluster, ClaimType, Cluster,
        ClusterRole, Confidence, ProseState, Section, SectionRole,
    )
    from lattice.graph.store import GraphStore
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "recover"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nx.\n\n# A. body\n\n  - claim [user_synthesis]\n\n"
        "# B. End [role: conclusion]\n\n  - close [user_synthesis]\n",
        encoding="utf-8",
    )

    # Seed a graph + cluster_plan that matches the outline structure
    # (body section + conclusion section) so the auto-heal consistency
    # checks don't force a re-ingest. Body cluster is failed; the
    # conclusion is already generated.
    store = GraphStore.load(project)
    now = datetime.now(timezone.utc)
    sections = [
        Section(
            section_id="s.a", title="body", role=SectionRole.argumentative,
            position=1, claim_ids=["c.1"],
        ),
        Section(
            section_id="s.b", title="End", role=SectionRole.conclusion,
            position=2, claim_ids=["c.2"],
        ),
    ]
    claims = [
        Claim(
            claim_id="c.1", statement="A claim.",
            type=ClaimType.user_synthesis, confidence=Confidence.medium,
            section_id="s.a", created_by="t",
            created_at=now, modified_at=now, author_origin=True,
        ),
        Claim(
            claim_id="c.2", statement="Closing.",
            type=ClaimType.user_synthesis, confidence=Confidence.medium,
            section_id="s.b", created_by="t",
            created_at=now, modified_at=now, author_origin=True,
        ),
    ]
    store.save_graph(AuthorGraph(
        project_name="recover", sections=sections, claims=claims,
        relationships=[], created_at=now, modified_at=now,
    ))
    store.save_cluster(Cluster(
        cluster_id="c.a.1", section_id="s.a", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[ClaimRoleInCluster(
            claim_id="c.1", role_in_cluster=ClusterRole.evidence,
        )],
        prose_state=ProseState.failed,
    ))
    store.save_cluster(Cluster(
        cluster_id="c.b.1", section_id="s.b", position=1,
        role=ClusterRole.synthesis,
        claim_sequence=[ClaimRoleInCluster(
            claim_id="c.2", role_in_cluster=ClusterRole.synthesis,
        )],
        prose_state=ProseState.generated,
    ))

    # First finalise call refuses, second one succeeds — simulating a
    # successful auto-recovery.
    finalise_calls = {"n": 0}

    def _finalise(self):
        finalise_calls["n"] += 1
        if finalise_calls["n"] == 1:
            return None  # first finalise: refuse
        return self.project_path / "outputs" / "paper.academic.md"

    # Track that render_all was called during auto-recovery.
    render_calls = {"n": 0}

    async def _render(self, *, force=False, progress=None):
        render_calls["n"] += 1
        # Mark all clusters as generated on the second call so the
        # finalise re-tries on a fresh graph.
        if render_calls["n"] == 2:
            store2 = GraphStore.load(self.project_path)
            for c in store2.list_clusters():
                c.prose_state = ProseState.generated
                store2.save_cluster(c)
        return {}

    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)

    class _StubClient:
        def __init__(self, *a, **k): pass
        async def complete(self, **k):
            raise AssertionError("LLM should not be needed")

    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _StubClient)
    monkeypatch.setattr(
        "lattice.web.runner.ChunkedRenderer.render_all", _render
    )
    monkeypatch.setattr(
        "lattice.web.runner.DocumentFinaliser.finalise", _finalise
    )

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    result = asyncio.run(run_pipeline(request, progress))

    events = []
    while not queue.empty(): events.append(queue.get_nowait())
    phases = [e["phase"] for e in events if e["type"] == "phase_begun"]
    assert "auto_recovery" in phases, (
        f"auto_recovery should have fired; phases={phases}"
    )
    # Both first-finalise (`finalise`) and post-recovery retry
    # (`finalise_retry`) are present as distinct rows in the timeline.
    assert "finalise" in phases
    assert "finalise_retry" in phases
    # Two finalise + two render calls: first set refused, second pair succeeded.
    assert finalise_calls["n"] == 2
    assert render_calls["n"] == 2
    assert result.finalise_succeeded is True
    assert any("Auto-recovery succeeded" in n for n in result.notes)


def test_run_pipeline_auto_recovery_skips_when_no_failed_clusters(
    tmp_path: Path, monkeypatch
) -> None:
    """If finalise refuses but no clusters are in the failed state,
    auto-recovery has nothing to do and should not fire."""
    from lattice.graph.models import (
        AuthorGraph, Claim, ClaimRoleInCluster, ClaimType, Cluster,
        ClusterRole, Confidence, ProseState, Section, SectionRole,
    )
    from lattice.graph.store import GraphStore
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "no_recover"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nx.\n\n# A. body\n\n  - c [user_synthesis]\n\n"
        "# B. End [role: conclusion]\n\n  - close [user_synthesis]\n",
        encoding="utf-8",
    )

    store = GraphStore.load(project)
    now = datetime.now(timezone.utc)
    store.save_graph(AuthorGraph(
        project_name="no_recover",
        sections=[Section(
            section_id="s.a", title="body", role=SectionRole.argumentative,
            position=1, claim_ids=["c.1"],
        )],
        claims=[Claim(
            claim_id="c.1", statement="x", type=ClaimType.user_synthesis,
            confidence=Confidence.medium, section_id="s.a", created_by="t",
            created_at=now, modified_at=now, author_origin=True,
        )],
        relationships=[], created_at=now, modified_at=now,
    ))
    store.save_cluster(Cluster(
        cluster_id="c.a.1", section_id="s.a", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[ClaimRoleInCluster(
            claim_id="c.1", role_in_cluster=ClusterRole.evidence,
        )],
        prose_state=ProseState.generated,  # already rendered, NOT failed
    ))

    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)

    class _Stub:
        def __init__(self, *a, **k): pass
        async def complete(self, **k): raise AssertionError("not called")

    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _Stub)

    async def _no_render(self, *, force=False, progress=None): return {}
    monkeypatch.setattr("lattice.web.runner.ChunkedRenderer.render_all", _no_render)
    monkeypatch.setattr(
        "lattice.web.runner.DocumentFinaliser.finalise", lambda self: None
    )

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    asyncio.run(run_pipeline(request, progress))

    events = []
    while not queue.empty(): events.append(queue.get_nowait())
    phases = [e["phase"] for e in events if e["type"] == "phase_begun"]
    assert "auto_recovery" not in phases


def test_gather_delivery_diagnostics_parses_blocked_md(tmp_path: Path) -> None:
    """``gather_delivery_diagnostics`` should extract per-category
    readiness flags + per-cluster failure markers from a project's
    ``.lattice/`` directory so the UI can show actionable detail."""
    from lattice.web.runner import gather_delivery_diagnostics

    project = tmp_path / "blocked"
    lattice_dir = project / ".lattice"
    lattice_dir.mkdir(parents=True)
    (lattice_dir / "delivery_blocked.md").write_text(
        "# Delivery blocked\n\n## Readiness check\n\n"
        "Document NOT ready for delivery. 2 blocking issue(s):\n\n"
        "- readiness.cluster_not_rendered: 1 flag(s)\n"
        "    Cluster c.g.1 is in state failed and cannot be delivered.\n"
        "    -> Re-run rendering for this cluster.\n"
        "- readiness.register_bleed: 1 flag(s)\n"
        "    Prose contains conversational meta-commentary.\n"
        "    -> Re-render with --force.\n\n"
        "Run `lattice flags ...` to review.\n",
        encoding="utf-8",
    )

    drafts = lattice_dir / "drafts" / "academic"
    drafts.mkdir(parents=True)
    (drafts / "cluster_c.g.1.md").write_text(
        '{CLUSTER_UNRENDERABLE: cluster_id="c.g.1", '
        'reason="render produced register bleed: process_meta_comment:\'the rule\'"}',
        encoding="utf-8",
    )
    (drafts / "cluster_c.a.1.md").write_text(
        "Clean rendered prose for the first cluster.\n",
        encoding="utf-8",
    )

    diag = gather_delivery_diagnostics(project, "academic")
    assert len(diag["readiness_flags"]) == 2
    cats = [f["category"] for f in diag["readiness_flags"]]
    assert "readiness.cluster_not_rendered" in cats
    assert "readiness.register_bleed" in cats
    cluster_flag = next(
        f for f in diag["readiness_flags"]
        if f["category"] == "readiness.cluster_not_rendered"
    )
    assert "Cluster c.g.1" in cluster_flag["message"]
    assert "Re-run" in cluster_flag["fix"]

    assert len(diag["failed_clusters"]) == 1
    assert diag["failed_clusters"][0]["cluster_id"] == "c.g.1"
    assert "register bleed" in diag["failed_clusters"][0]["reason"]
    assert diag["raw_delivery_blocked"] is not None
    assert diag["errors"] == []


def test_gather_delivery_diagnostics_handles_missing_files(tmp_path: Path) -> None:
    """If neither delivery_blocked.md nor drafts exist, the helper
    should return empty lists rather than raise."""
    from lattice.web.runner import gather_delivery_diagnostics
    project = tmp_path / "fresh"
    project.mkdir()
    diag = gather_delivery_diagnostics(project, "academic")
    assert diag["readiness_flags"] == []
    assert diag["failed_clusters"] == []
    assert diag["raw_delivery_blocked"] is None
    assert diag["errors"] == []


def test_register_bleed_does_not_flag_legal_word_rule() -> None:
    """Regression: 'the rule' is no longer in the bleed list because
    it caused false positives in legal/judicial prose."""
    from lattice.renderer.cluster_renderer import validate_response
    legal_prose = (
        "The rule established by precedent governs subsequent decisions. "
        "Judges must apply the rule consistently across cases."
    )
    result = validate_response(legal_prose)
    assert result.is_valid, f"unexpected violations: {result.violations}"


def test_register_bleed_still_catches_real_meta_commentary() -> None:
    """Sanity: the remaining bleed patterns ('the prompt',
    'before proceeding', 'the constraint', 'the voice file')
    still trigger so we haven't gutted the validator."""
    from lattice.renderer.cluster_renderer import validate_response
    bleed = "Before proceeding, I need to clarify the prompt's intent."
    result = validate_response(bleed)
    assert not result.is_valid
    assert any("first_person_imperative" in v for v in result.violations)
    assert any("process_meta_comment" in v for v in result.violations)


def test_normalise_to_user_synthesis_tags_unmarked_claims() -> None:
    """Every `  - claim` bullet without `[user_synthesis]` should gain
    that tag. MY VIEW / COUNTER bullets are already user_synthesis by
    convention, so they're left alone."""
    from lattice.ingester.auto_outliner import normalise_to_user_synthesis
    src = (
        "# THESIS\n\nfoo.\n\n"
        "# A. x\n\n"
        "  - alpha [empirical]\n"
        "  - beta [strong]\n"
        "  - MY VIEW: gamma [user_synthesis]\n"
        "  - delta [user_synthesis]\n"
        "  - epsilon\n"
    )
    out, changed = normalise_to_user_synthesis(src)
    assert changed == 3  # alpha, beta, epsilon
    assert "alpha [empirical] [user_synthesis]" in out
    assert "beta [strong] [user_synthesis]" in out
    assert "epsilon [user_synthesis]" in out
    # MY VIEW and the already-tagged `delta` stay unchanged.
    assert out.count("[user_synthesis]") == 5
    assert "MY VIEW: gamma [user_synthesis]" in out


def test_append_conclusion_section_uses_next_letter() -> None:
    from lattice.ingester.auto_outliner import (
        append_conclusion_section,
        has_conclusion_section,
    )
    src = "# THESIS\n\nx\n\n# A. one\n\n  - a\n\n# B. two\n\n  - b\n"
    assert not has_conclusion_section(src)
    out = append_conclusion_section(src)
    assert "# C. Conclusion [role: conclusion]" in out
    assert has_conclusion_section(out)


def test_markdown_ingester_accepts_equals_tag_separator() -> None:
    """The ingester should treat `[role=conclusion]` (LLM-friendly
    syntax) the same as `[role: conclusion]` (canonical)."""
    from lattice.ingester.markdown import _parse_tags
    title, tags = _parse_tags("Conclusion [role=conclusion] [words=400]")
    assert title == "Conclusion"
    assert tags["role"] == ["conclusion"]
    assert tags["words"] == ["400"]


def test_has_conclusion_role_tag_accepts_both_forms() -> None:
    from lattice.ingester.auto_outliner import has_conclusion_role_tag
    assert has_conclusion_role_tag("# F. Wrap [role: conclusion]\n")
    assert has_conclusion_role_tag("# F. Wrap [role=conclusion]\n")
    assert has_conclusion_role_tag("# F. Wrap [role : conclusion]\n")
    assert not has_conclusion_role_tag("# F. Wrap-up\n  - x\n")


def test_run_pipeline_force_reingests_when_graph_missing_conclusion(
    tmp_path: Path, monkeypatch
) -> None:
    """If the outline declares `[role: conclusion]` but the saved
    graph has no section with that role (left over from an older
    parser), force a re-ingest so the user doesn't stay stuck on
    the old broken state."""
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "stale_role"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nx.\n\n# A. body\n\n  - claim [user_synthesis]\n\n"
        "# B. End [role: conclusion]\n\n  - close [user_synthesis]\n",
        encoding="utf-8",
    )
    # Stale graph with B section incorrectly tagged argumentative.
    lattice_dir = project / ".lattice"
    lattice_dir.mkdir()
    (lattice_dir / "author_graph.json").write_text(
        json.dumps({
            "project_name": "stale_role",
            "thesis_statement": "x", "thesis_argued": None,
            "thesis_argued_confidence": None, "thesis_argued_note": None,
            "sections": [
                {"section_id": "s.a", "title": "body", "role": "argumentative",
                 "position": 1, "claim_ids": ["c.1"], "target_length": 800,
                 "depth": "standard", "parent": None, "thesis_claim": None},
                {"section_id": "s.b", "title": "End", "role": "argumentative",
                 "position": 2, "claim_ids": ["c.2"], "target_length": 800,
                 "depth": "standard", "parent": None, "thesis_claim": None},
            ],
            "claims": [], "relationships": [],
            "created_at": "2026-01-01T00:00:00Z",
            "modified_at": "2026-01-01T00:00:00Z",
        }), encoding="utf-8",
    )
    # Make the graph newer than the outline so the mtime-based path
    # would otherwise skip ingest.
    import os as _os
    _os.utime(structure_dir / "outline.md", (1700000000, 1700000000))
    _os.utime(lattice_dir / "author_graph.json", (1800000000, 1800000000))

    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)

    class _Stub:
        def __init__(self, *a, **k): pass
        async def complete(self, **k):
            raise AssertionError("LLM should not be needed; outline is structured")

    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _Stub)

    async def _no_render(self, *, force=False, progress=None): return {}
    monkeypatch.setattr("lattice.web.runner.ChunkedRenderer.render_all", _no_render)
    monkeypatch.setattr(
        "lattice.web.runner.DocumentFinaliser.finalise", lambda self: None
    )

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    asyncio.run(run_pipeline(request, progress))

    events = []
    while not queue.empty(): events.append(queue.get_nowait())
    phases = [e["phase"] for e in events if e["type"] == "phase_begun"]
    # Ingest must have re-run despite the graph being newer than the outline.
    assert "ingest" in phases
    # Re-parsed graph now has a conclusion-role section.
    saved = json.loads(
        (project / ".lattice" / "author_graph.json").read_text(encoding="utf-8")
    )
    roles = {s["role"] for s in saved["sections"]}
    assert "conclusion" in roles


def test_has_conclusion_section_recognises_titles_and_tags() -> None:
    from lattice.ingester.auto_outliner import has_conclusion_section
    assert has_conclusion_section("# A. Conclusion\n\n  - x\n")
    assert has_conclusion_section("# B. Final discussion\n\n  - x\n")
    assert has_conclusion_section("# C. Wrap-up [role=conclusion]\n\n  - x\n")
    assert not has_conclusion_section("# A. Background\n\n  - x\n")


def test_run_pipeline_normalises_when_no_sources(
    tmp_path: Path, monkeypatch
) -> None:
    """If the outline is structured but uses non-user_synthesis tags
    AND the project has no source papers, ingest should
    auto-rewrite the tags to user_synthesis so claims render."""
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "needs_normalise"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    # Lattice format but uses [empirical] tags and no conclusion.
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nThe thesis sentence.\n\n"
        "# A. First section\n\n"
        "  - First claim. [empirical]\n"
        "  - Second claim. [strong]\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)

    class _StubClient:
        def __init__(self, *a, **k): pass
        async def complete(self, **k):
            raise AssertionError(
                "LLM should not be called when outline is already structured"
            )

    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _StubClient)

    async def _no_render(self, *, force=False, progress=None): return {}
    monkeypatch.setattr("lattice.web.runner.ChunkedRenderer.render_all", _no_render)
    monkeypatch.setattr(
        "lattice.web.runner.DocumentFinaliser.finalise", lambda self: None
    )

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    asyncio.run(run_pipeline(request, progress))

    events = []
    while not queue.empty(): events.append(queue.get_nowait())
    fail_events = [e for e in events if e["type"] == "run_failed"]
    assert fail_events == [], f"unexpected failures: {fail_events}"
    phases = [e["phase"] for e in events if e["type"] == "phase_begun"]
    # Both auto-heal stages should fire.
    assert "normalise_outline" in phases
    assert "add_conclusion" in phases

    # outline.md was rewritten with user_synthesis tags + a conclusion.
    saved = (project / "structure" / "outline.md").read_text(encoding="utf-8")
    assert saved.count("[user_synthesis]") >= 2
    assert "Conclusion" in saved


def test_run_pipeline_skips_normalise_when_sources_indexed(
    tmp_path: Path, monkeypatch
) -> None:
    """If the project has indexed source papers, the runner should
    trust the existing tags and NOT rewrite them."""
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "with_sources"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    (structure_dir / "outline.md").write_text(
        "# THESIS\n\nx.\n\n"
        "# A. s [role=conclusion]\n\n"
        "  - First [empirical]\n",
        encoding="utf-8",
    )
    refs_papers = project / "refs" / "papers"
    refs_papers.mkdir(parents=True)
    (refs_papers / "stub.pdf").write_bytes(b"%PDF-1.4 stub\n%%EOF\n")

    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)

    class _StubClient:
        def __init__(self, *a, **k): pass
        async def complete(self, **k): raise AssertionError("not called")

    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _StubClient)

    async def _no_render(self, *, force=False, progress=None): return {}
    monkeypatch.setattr("lattice.web.runner.ChunkedRenderer.render_all", _no_render)
    monkeypatch.setattr(
        "lattice.web.runner.DocumentFinaliser.finalise", lambda self: None
    )

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    asyncio.run(run_pipeline(request, progress))

    events = []
    while not queue.empty(): events.append(queue.get_nowait())
    phases = [e["phase"] for e in events if e["type"] == "phase_begun"]
    # Normalisation is skipped because sources exist.
    assert "normalise_outline" not in phases
    # outline.md tags are untouched.
    saved = (project / "structure" / "outline.md").read_text(encoding="utf-8")
    assert "[empirical]" in saved
    assert "[user_synthesis]" not in saved


def test_auto_outliner_rejects_garbage_response() -> None:
    """If Claude returns text without `# THESIS` or `# A.` markers,
    the outliner should refuse rather than overwrite outline.md."""
    import asyncio
    from lattice.ingester.auto_outliner import structure_outline

    class _StubResponse:
        text = "Sure, here is your outline: Lorem ipsum dolor sit amet."

    class _StubClient:
        async def complete(self, **k):
            return _StubResponse()

    with pytest.raises(RuntimeError, match="without `# THESIS`"):
        asyncio.run(structure_outline("some prose", _StubClient()))


def test_run_pipeline_fails_when_outline_has_no_structure(
    tmp_path: Path, monkeypatch
) -> None:
    """A common UX trap: user pastes raw academic prose into outline.md
    instead of writing a lattice-format outline. Ingest succeeds with
    zero sections, and the runner should surface a clear error rather
    than the cryptic 'empty_cluster_plan'."""
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "rawprose"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_dir = project / "structure"
    structure_dir.mkdir()
    # Raw paper text — no #THESIS or # A. headers.
    (structure_dir / "outline.md").write_text(
        "Extraneous factors in judicial decisions\n\n"
        "Are judicial rulings based solely on laws and facts? Legal "
        "formalism holds that judges apply legal reasons to the facts "
        "of a case in a rational, mechanical, and deliberative manner.\n",
        encoding="utf-8",
    )

    # Stub the auto-outliner so it returns text that *also* lacks
    # structure. The outliner enforces a structural check and raises,
    # so the runner falls through to the post-ingest empty-graph
    # check and emits outline_has_no_structure.
    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)

    class _StubResp:
        text = "I cannot extract structure from this text."

    class _StubClient:
        def __init__(self, *a, **k) -> None: ...
        async def complete(self, **k):
            return _StubResp()

    monkeypatch.setattr("lattice.web.runner.ClaudeClient", _StubClient)

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    asyncio.run(run_pipeline(request, progress))

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    fail_events = [e for e in events if e["type"] == "run_failed"]
    assert len(fail_events) == 1
    # When the auto-outliner can't produce a valid outline, the
    # runner reports auto_structure_failed and bails before ingest.
    assert fail_events[0]["reason"] == "auto_structure_failed"


def test_run_pipeline_fails_clean_when_no_outline(
    tmp_path: Path, monkeypatch
) -> None:
    """A project with no outline at all surfaces a clean 'no_outline'
    failure rather than an opaque exception."""
    from lattice.web.runner import RunRequest, run_pipeline

    project = tmp_path / "barren"
    project.mkdir()
    (project / "config.yml").write_text("autocorrect: none\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir()
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (project / "structure").mkdir()  # empty — no outline file

    monkeypatch.setattr("lattice.web.runner.claude_available", lambda: True)

    queue: asyncio.Queue = asyncio.Queue()
    progress = EventQueueProgress(queue)
    request = RunRequest(
        project_path=project, voice_name="academic", level="quick",
    )
    asyncio.run(run_pipeline(request, progress))

    fail_events = [e for e in [
        queue.get_nowait() for _ in range(queue.qsize())
    ] if e["type"] == "run_failed"]
    assert len(fail_events) == 1
    assert fail_events[0]["reason"] == "no_outline"


def test_patch_project_updates_category_and_position(
    client: TestClient, tmp_path: Path
) -> None:
    """PATCH writes category + position into project_meta.json and the
    list endpoint surfaces them on the next read."""
    resp = client.patch(
        "/api/projects/demo",
        json={"category": "Research papers", "position": 2.5},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["category"] == "Research papers"
    assert data["position"] == 2.5

    listed = client.get("/api/projects").json()
    matching = next(p for p in listed if p["name"] == "demo")
    assert matching["category"] == "Research papers"
    assert matching["position"] == 2.5

    # And it persists on disk.
    meta = json.loads(
        (tmp_path / "demo" / ".lattice" / "project_meta.json")
        .read_text(encoding="utf-8")
    )
    assert meta["category"] == "Research papers"
    assert meta["position"] == 2.5


def test_patch_project_blank_category_resets_to_default(client: TestClient) -> None:
    """An empty category string should fall back to the default
    'Uncategorised' bucket rather than persist as ''."""
    resp = client.patch("/api/projects/demo", json={"category": "   "})
    assert resp.status_code == 200
    assert resp.json()["category"] == "Uncategorised"


def test_patch_project_rejects_oversized_fields(client: TestClient) -> None:
    long = "x" * 200
    resp = client.patch("/api/projects/demo", json={"display_name": long})
    assert resp.status_code == 400
    resp = client.patch("/api/projects/demo", json={"category": long})
    assert resp.status_code == 400


def test_reorder_projects_writes_all_positions(
    client: TestClient, tmp_path: Path
) -> None:
    """The bulk-reorder endpoint should persist every entry's new
    category + position in one call."""
    # Seed a second project alongside 'demo'.
    second = tmp_path / "second"
    (second / ".lattice").mkdir(parents=True)
    (second / ".lattice" / "project_meta.json").write_text(
        json.dumps({"display_name": "Second"}), encoding="utf-8"
    )
    voice = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    ).read_text(encoding="utf-8")
    (second / "voices").mkdir()
    (second / "voices" / "academic.voice.md").write_text(voice, encoding="utf-8")

    resp = client.post(
        "/api/projects/_reorder",
        json={"order": [
            {"name": "demo", "category": "Drafts", "position": 0},
            {"name": "second", "category": "Drafts", "position": 1},
        ]},
    )
    assert resp.status_code == 200
    assert set(resp.json()["updated"]) == {"demo", "second"}

    listed = client.get("/api/projects").json()
    drafts = [p for p in listed if p["category"] == "Drafts"]
    assert len(drafts) == 2
    # Sort order is (category, position).
    assert drafts[0]["name"] == "demo"
    assert drafts[1]["name"] == "second"


def test_delete_project_moves_to_trash(
    client: TestClient, tmp_path: Path
) -> None:
    """DELETE moves the folder under <root>/.trash/ rather than
    deleting it outright, so an accidental click is recoverable."""
    project_path = tmp_path / "demo"
    assert project_path.exists()
    resp = client.delete("/api/projects/demo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == "demo"
    moved_to = Path(body["moved_to"])
    assert moved_to.exists()
    # Original folder is gone.
    assert not project_path.exists()
    # The moved folder is under .trash/.
    assert moved_to.parent.name == ".trash"
    # And the project no longer appears in listings.
    listed = client.get("/api/projects").json()
    assert all(p["name"] != "demo" for p in listed)


def test_list_projects_skips_trash_folder(
    client: TestClient, tmp_path: Path
) -> None:
    """A .trash folder under the projects root must never be listed,
    even if (somehow) it contains a .lattice/ directory."""
    trashed = tmp_path / ".trash" / "old_demo"
    (trashed / ".lattice").mkdir(parents=True)
    (trashed / ".lattice" / "project_meta.json").write_text(
        json.dumps({"display_name": "Old"}), encoding="utf-8"
    )
    listed = client.get("/api/projects").json()
    assert all(p["name"] != ".trash" for p in listed)
    assert all(p["name"] != "old_demo" for p in listed)


def test_graph_viz_generates_html_lazily(client: TestClient, tmp_path: Path) -> None:
    """The graph-viz endpoint generates outputs/argument_graph.html on
    first hit if it doesn't already exist."""
    viz_path = tmp_path / "demo" / "outputs" / "argument_graph.html"
    assert not viz_path.exists()  # confirm starting state
    resp = client.get("/api/projects/demo/graph-viz")
    assert resp.status_code == 200
    # Returned HTML body should reference cytoscape.js (the lib visualise.py uses).
    body = resp.text
    assert "<html" in body.lower()
    assert "cytoscape" in body.lower()
    # Subsequent hit should reuse the file (no exceptions, same content shape).
    resp2 = client.get("/api/projects/demo/graph-viz")
    assert resp2.status_code == 200
