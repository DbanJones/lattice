"""Phase 6 follow-ups: voice-aware cockpit endpoints, cluster
boundary markers in the joined paper, and the
``coverage.unrenderable_mechanism_marker`` audit rule.

These cover the limitations called out at the end of Phases 3, 5,
and 6 (#1 voice picker, #2 paragraph→cluster bindings, #3 mechanism
marker audit).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lattice.auditor.coverage import CoverageCheck
from lattice.graph.models import (
    AuthorGraph, Citation, Claim, ClaimRoleInCluster, ClaimType, Cluster,
    ClusterRole, Confidence, ProseState, Section, SectionRole,
)
from lattice.graph.store import GraphStore
from lattice.renderer.assembler_finalise import DocumentFinaliser
from lattice.utils.config import Config
from lattice.voice.parser import Voice
from lattice.web.app import create_app


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _voice() -> Voice:
    voice_path = (
        Path(__file__).parent.parent
        / "examples" / "voices" / "academic.voice.md"
    )
    return Voice.from_file(voice_path)


# ─── #1: voice-aware /api/projects/{name}/paper ──────


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from tests.test_web import _seed_project
    _seed_project(tmp_path, "demo")
    return TestClient(create_app(projects_root=tmp_path))


def test_get_paper_endpoint_accepts_voice_query_param(
    client: TestClient, tmp_path: Path,
) -> None:
    """The cockpit's voice picker depends on /paper accepting ?voice."""
    project = tmp_path / "demo"
    outputs = project / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "paper.policy.md").write_text(
        "# Demo paper\n\nPolicy-voice content.", encoding="utf-8")
    resp = client.get("/api/projects/demo/paper?voice=policy")
    assert resp.status_code == 200
    assert "Policy-voice content." in resp.text


def test_get_paper_endpoint_404_when_voice_not_rendered(
    client: TestClient,
) -> None:
    resp = client.get("/api/projects/demo/paper?voice=journalistic")
    assert resp.status_code == 404


def test_get_paper_endpoint_default_voice_unchanged(
    client: TestClient, tmp_path: Path,
) -> None:
    """Without a voice param, the endpoint serves paper.academic.md
    so existing callers keep working."""
    project = tmp_path / "demo"
    outputs = project / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "paper.academic.md").write_text(
        "# Demo\n\nAcademic.", encoding="utf-8")
    resp = client.get("/api/projects/demo/paper")
    assert resp.status_code == 200
    assert "Academic." in resp.text


# ─── #2: cluster boundary markers in joined paper ────


def _seed_renderable_project(tmp_path: Path) -> Path:
    """Build a minimal project where the finaliser will emit one
    cluster's prose and the cluster boundary marker before it."""
    project = tmp_path / "demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / "config.yml").write_text("autocorrect: safe\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir(exist_ok=True)
    voice_src = (
        Path(__file__).parent.parent
        / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8")

    store = GraphStore.load(project)
    now = _now()
    section = Section(
        section_id="s.x", title="Section X", position=1,
        role=SectionRole.argumentative, claim_ids=["cl.x.1"],
    )
    claim = Claim(
        claim_id="cl.x.1", statement="A bound claim.",
        type=ClaimType.empirical, confidence=Confidence.high,
        section_id="s.x", created_by="t",
        created_at=now, modified_at=now,
    )
    store.save_graph(AuthorGraph(
        project_name="demo", sections=[section], claims=[claim],
        relationships=[], created_at=now, modified_at=now,
    ))
    cluster = Cluster(
        cluster_id="c.x.1", section_id="s.x", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[ClaimRoleInCluster(
            claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence,
        )],
        prose_state=ProseState.generated,
    )
    store.save_cluster(cluster)
    drafts_dir = project / ".lattice" / "drafts" / "academic"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    (drafts_dir / "cluster_c.x.1.md").write_text(
        "Bound prose for the cluster.", encoding="utf-8")
    return project


def test_finaliser_emits_cluster_boundary_marker(tmp_path: Path) -> None:
    """The cockpit binds paragraphs to clusters by walking the joined
    paper for ``<!-- lattice:cluster <cid> <sid> -->`` markers — the
    finaliser must emit one before each cluster's prose.

    Tests the join logic directly via ``_concatenate_and_write`` so
    we don't need to satisfy the full readiness gate (architecture
    template requirements, evidence binding, etc.) for what is
    fundamentally a unit test of the boundary-marker emission.
    """
    project = _seed_renderable_project(tmp_path)
    store = GraphStore.load(project)
    finaliser = DocumentFinaliser(project, store, _voice())
    out_path = finaliser._concatenate_and_write()
    assert out_path is not None and out_path.exists()
    paper = out_path.read_text(encoding="utf-8")
    assert "<!-- lattice:cluster c.x.1 s.x -->" in paper
    # Marker must precede the cluster prose, not follow it.
    marker_idx = paper.index("<!-- lattice:cluster c.x.1")
    prose_idx = paper.index("Bound prose for the cluster.")
    assert marker_idx < prose_idx


# ─── #3: unrenderable_mechanism_marker audit rule ────


def _make_cluster(cluster_id: str = "c.x.1") -> Cluster:
    return Cluster(
        cluster_id=cluster_id, section_id="s.x", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[ClaimRoleInCluster(
            claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence,
        )],
        prose_state=ProseState.generated,
    )


@pytest.mark.asyncio
async def test_coverage_flags_unrenderable_mechanism_marker(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    (project / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(project)
    now = _now()
    store.save_graph(AuthorGraph(
        project_name="t", sections=[Section(
            section_id="s.x", title="X", position=1,
            role=SectionRole.argumentative, claim_ids=["cl.x.1"],
        )],
        claims=[Claim(
            claim_id="cl.x.1", statement="A claim.",
            type=ClaimType.empirical, confidence=Confidence.high,
            created_by="t", created_at=now, modified_at=now,
        )],
        relationships=[], created_at=now, modified_at=now,
    ))
    cluster = _make_cluster()
    store.save_cluster(cluster)

    prose = (
        "Some prose for the cluster. "
        '{UNRENDERABLE_MECHANISM: claim_id="cl.x.1", '
        'description="speculative chain"}'
        " More prose afterwards."
    )
    check = CoverageCheck(
        config=Config.load(project), store=store, llm=None, voice=_voice(),
    )
    flags = await check.check_cluster(cluster, prose)
    by_rule = {f.rule_id: f for f in flags}
    assert "coverage.unrenderable_mechanism_marker" in by_rule
    flag = by_rule["coverage.unrenderable_mechanism_marker"]
    assert "UNRENDERABLE_MECHANISM" in flag.offending_text
    assert flag.severity.value == "critical"


@pytest.mark.asyncio
async def test_coverage_no_marker_no_unrenderable_flag(
    tmp_path: Path,
) -> None:
    """Sanity: prose without the marker must not fire the new rule."""
    project = tmp_path / "demo"
    project.mkdir()
    (project / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(project)
    now = _now()
    store.save_graph(AuthorGraph(
        project_name="t", sections=[Section(
            section_id="s.x", title="X", position=1,
            role=SectionRole.argumentative, claim_ids=["cl.x.1"],
        )],
        claims=[Claim(
            claim_id="cl.x.1", statement="A claim.",
            type=ClaimType.empirical, confidence=Confidence.high,
            created_by="t", created_at=now, modified_at=now,
        )],
        relationships=[], created_at=now, modified_at=now,
    ))
    cluster = _make_cluster()
    store.save_cluster(cluster)

    prose = "Plain prose with no markers at all."
    check = CoverageCheck(
        config=Config.load(project), store=store, llm=None, voice=_voice(),
    )
    flags = await check.check_cluster(cluster, prose)
    assert not any(
        f.rule_id == "coverage.unrenderable_mechanism_marker" for f in flags
    )
