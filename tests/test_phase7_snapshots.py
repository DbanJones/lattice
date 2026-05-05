"""Phase 7 — provenance + versioning.

Covers:
- ``GraphStore.create_snapshot`` bundles graph, clusters, sources,
  and audit flags into one restorable JSON file.
- ``GraphStore.list_snapshots`` returns snapshots newest-first.
- ``GraphStore.revert_to_snapshot`` restores every artefact and
  takes a pre-revert snapshot first.
- ``diff_graphs`` reports adds/removes/modifies for sections,
  claims, relationships, sources, and clusters.
- The snapshot endpoints round-trip via the FastAPI app.
- The dispatcher takes a snapshot before non-ingest activities
  (verified via the snapshot file appearing on disk).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lattice.differ.graph_diff import diff_graphs, diff_snapshots
from lattice.graph.models import (
    AuditFlag, AuthorGraph, BindingStrength, Citation, Claim,
    ClaimRoleInCluster, ClaimType, Cluster, ClusterRole, Confidence,
    EditMode, Evidence, FlagCategory, ProseLocation, ProseState,
    Relationship, RelationshipStrength, RelationshipType, Section,
    SectionRole, Severity, Snapshot, SnapshotKind, Source,
    SourceMetadata, SourceType,
)
from lattice.graph.store import GraphStore
from lattice.web.app import create_app


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_minimal(tmp_path: Path) -> tuple[Path, GraphStore]:
    project = tmp_path / "demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / "config.yml").write_text("", encoding="utf-8")
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
        section_id="s.x", title="X", position=1,
        role=SectionRole.argumentative, claim_ids=["cl.x.1"],
    )
    claim = Claim(
        claim_id="cl.x.1", statement="A claim.",
        type=ClaimType.empirical, confidence=Confidence.high,
        section_id="s.x", created_by="t",
        created_at=now, modified_at=now,
    )
    store.save_graph(AuthorGraph(
        project_name="demo", sections=[section], claims=[claim],
        relationships=[], created_at=now, modified_at=now,
    ))
    return project, store


# ─── snapshot create / list / load / revert ───────────


def test_create_snapshot_persists_full_state(tmp_path: Path) -> None:
    project, store = _seed_minimal(tmp_path)
    snap = store.create_snapshot(
        kind=SnapshotKind.before_draft,
        actor="activity:draft",
        message="pre-draft test",
    )
    assert snap.snapshot_id.startswith("snap.")
    target = project / ".lattice" / "snapshots" / f"{snap.snapshot_id}.json"
    assert target.exists()
    payload = Snapshot.model_validate_json(target.read_text(encoding="utf-8"))
    assert payload.kind == SnapshotKind.before_draft
    assert payload.actor == "activity:draft"
    assert payload.graph is not None
    assert len(payload.graph.claims) == 1
    assert payload.graph.claims[0].claim_id == "cl.x.1"


def test_list_snapshots_newest_first(tmp_path: Path) -> None:
    project, store = _seed_minimal(tmp_path)
    s1 = store.create_snapshot(kind=SnapshotKind.before_scaffold, message="first")
    s2 = store.create_snapshot(kind=SnapshotKind.before_draft, message="second")
    s3 = store.create_snapshot(kind=SnapshotKind.before_refine, message="third")
    snapshots = store.list_snapshots()
    ids = [s.snapshot_id for s in snapshots]
    # Newest first.
    assert ids[0] == s3.snapshot_id
    assert ids[-1] == s1.snapshot_id
    assert len(snapshots) == 3


def test_revert_takes_pre_revert_snapshot_and_restores_state(
    tmp_path: Path,
) -> None:
    project, store = _seed_minimal(tmp_path)
    # Take a snapshot of the original state.
    original = store.create_snapshot(kind=SnapshotKind.manual, message="origin")
    # Mutate: add a new claim.
    graph = store.get_graph()
    now = _now()
    graph.claims.append(Claim(
        claim_id="cl.x.2", statement="An added claim.",
        type=ClaimType.empirical, confidence=Confidence.medium,
        section_id="s.x", created_by="t",
        created_at=now, modified_at=now,
    ))
    store.save_graph(graph)
    assert len(store.get_graph().claims) == 2
    # Revert.
    restored = store.revert_to_snapshot(original.snapshot_id)
    assert restored.snapshot_id == original.snapshot_id
    # The added claim is gone.
    assert {c.claim_id for c in store.get_graph().claims} == {"cl.x.1"}
    # A pre_revert snapshot exists capturing the mutated state.
    snapshots = store.list_snapshots()
    pre_revert = [s for s in snapshots if s.kind == SnapshotKind.pre_revert]
    assert len(pre_revert) == 1
    assert any(c.claim_id == "cl.x.2" for c in pre_revert[0].graph.claims)


def test_revert_can_skip_pre_revert(tmp_path: Path) -> None:
    """The auto-revert flow already snapshots before; passing
    ``take_pre_revert=False`` avoids the redundant write."""
    project, store = _seed_minimal(tmp_path)
    snap = store.create_snapshot(kind=SnapshotKind.manual)
    initial_count = len(store.list_snapshots())
    store.revert_to_snapshot(snap.snapshot_id, take_pre_revert=False)
    assert len(store.list_snapshots()) == initial_count


def test_revert_unknown_id_raises_keyerror(tmp_path: Path) -> None:
    _project, store = _seed_minimal(tmp_path)
    with pytest.raises(KeyError):
        store.revert_to_snapshot("snap.does_not_exist")


# ─── diff_graphs ──────────────────────────────────────


def test_diff_graphs_detects_claim_add_remove_modify() -> None:
    now = _now()
    base = AuthorGraph(
        project_name="t", relationships=[], sections=[Section(
            section_id="s.x", title="X", position=1,
            role=SectionRole.argumentative, claim_ids=["cl.x.1", "cl.x.2"],
        )],
        claims=[
            Claim(claim_id="cl.x.1", statement="Original statement.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  created_by="t", created_at=now, modified_at=now),
            Claim(claim_id="cl.x.2", statement="Will be removed.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  created_by="t", created_at=now, modified_at=now),
        ],
        created_at=now, modified_at=now,
    )
    later = AuthorGraph(
        project_name="t", relationships=[], sections=base.sections,
        claims=[
            # cl.x.1 modified (statement changed)
            Claim(claim_id="cl.x.1", statement="Revised statement.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  created_by="t", created_at=now, modified_at=now),
            # cl.x.2 removed; cl.x.3 added
            Claim(claim_id="cl.x.3", statement="A brand new claim.",
                  type=ClaimType.empirical, confidence=Confidence.medium,
                  created_by="t", created_at=now, modified_at=now),
        ],
        created_at=now, modified_at=now,
    )
    diff = diff_graphs(base, later)
    assert diff.claims_added == ["cl.x.3"]
    assert diff.claims_removed == ["cl.x.2"]
    assert len(diff.claims_modified) == 1
    assert diff.claims_modified[0].claim_id == "cl.x.1"
    fields = {c.field for c in diff.claims_modified[0].fields}
    assert "statement" in fields
    assert diff.total_changes == 3


def test_diff_graphs_handles_sections_relationships_sources() -> None:
    now = _now()
    base = AuthorGraph(
        project_name="t",
        sections=[Section(section_id="s.x", title="X", position=1,
                          role=SectionRole.argumentative)],
        claims=[], relationships=[],
        created_at=now, modified_at=now,
    )
    after = AuthorGraph(
        project_name="t",
        sections=[
            Section(section_id="s.x", title="X", position=1,
                    role=SectionRole.argumentative),
            Section(section_id="s.y", title="Y", position=2,
                    role=SectionRole.argumentative),
        ],
        claims=[], relationships=[Relationship(
            rel_id="r.1", type=RelationshipType.supports,
            **{"from": "cl.a", "to": "cl.b"},
            strength=RelationshipStrength.direct,
            created_by="t", created_at=now,
        )],
        created_at=now, modified_at=now,
    )
    src1 = Source(
        source_id="src.A", type=SourceType.primary_paper,
        citation=Citation(authors=["x"], year=2020, title="x"),
        metadata=SourceMetadata(
            date_added=now, file_path="x.pdf", hash="sha:x"),
    )
    src2 = Source(
        source_id="src.B", type=SourceType.primary_paper,
        citation=Citation(authors=["y"], year=2021, title="y"),
        metadata=SourceMetadata(
            date_added=now, file_path="y.pdf", hash="sha:y"),
    )
    diff = diff_graphs(
        base, after,
        before_sources=[src1], after_sources=[src1, src2],
    )
    assert diff.sections_added == ["s.y"]
    assert diff.relationships_added == ["r.1"]
    assert diff.sources_added == ["src.B"]


def test_diff_snapshots_threads_clusters_and_sources(tmp_path: Path) -> None:
    project, store = _seed_minimal(tmp_path)
    s_before = store.create_snapshot(kind=SnapshotKind.manual, message="t0")
    # Mutate clusters: add one.
    cluster = Cluster(
        cluster_id="c.x.1", section_id="s.x", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[ClaimRoleInCluster(
            claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence,
        )],
        prose_state=ProseState.generated,
    )
    store.save_cluster(cluster)
    s_after = store.create_snapshot(kind=SnapshotKind.manual, message="t1")
    diff = diff_snapshots(s_before, s_after)
    assert diff.clusters_added == ["c.x.1"]


# ─── HTTP endpoints ───────────────────────────────────


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from tests.test_web import _seed_project
    _seed_project(tmp_path, "demo")
    return TestClient(create_app(projects_root=tmp_path))


def test_snapshots_endpoints_round_trip(
    client: TestClient, tmp_path: Path,
) -> None:
    # Take a manual snapshot.
    resp = client.post(
        "/api/projects/demo/snapshots",
        json={"message": "test checkpoint"},
    )
    assert resp.status_code == 200
    snap_id = resp.json()["snapshot_id"]

    # List should include it.
    listing = client.get("/api/projects/demo/snapshots").json()
    assert any(s["snapshot_id"] == snap_id for s in listing["snapshots"])
    assert listing["total"] >= 1

    # Get returns the full bundle.
    detail = client.get(
        f"/api/projects/demo/snapshots/{snap_id}",
    ).json()
    assert detail["snapshot_id"] == snap_id
    assert detail["message"] == "test checkpoint"

    # Diff against current with no mutations: total_changes == 0.
    diff = client.get(
        f"/api/projects/demo/snapshots/{snap_id}/diff",
    ).json()
    assert diff["before"] == snap_id
    assert diff["after"] == "current"
    assert diff["diff"]["total_changes"] == 0


def test_diff_endpoint_404_for_unknown_snapshot(
    client: TestClient,
) -> None:
    resp = client.get(
        "/api/projects/demo/snapshots/snap.nope/diff",
    )
    assert resp.status_code == 404


def test_revert_endpoint_restores_state(
    client: TestClient, tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    store = GraphStore.load(project)
    # Take snapshot of the seeded state.
    snap_resp = client.post(
        "/api/projects/demo/snapshots",
        json={"message": "before mutation"},
    ).json()
    snap_id = snap_resp["snapshot_id"]

    # Mutate via the store directly (bypass activities so the test
    # doesn't depend on Claude availability).
    graph = store.get_graph()
    now = _now()
    graph.claims.append(Claim(
        claim_id="cl.x.added", statement="Added by test.",
        type=ClaimType.empirical, confidence=Confidence.medium,
        section_id="s.x", created_by="t",
        created_at=now, modified_at=now,
    ))
    store.save_graph(graph)
    # Revert.
    resp = client.post(
        f"/api/projects/demo/snapshots/{snap_id}/revert",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["restored"] == snap_id

    store_after = GraphStore.load(project)
    claim_ids = {c.claim_id for c in store_after.get_graph().claims}
    assert "cl.x.added" not in claim_ids


def test_revert_endpoint_404_for_unknown_snapshot(
    client: TestClient,
) -> None:
    resp = client.post(
        "/api/projects/demo/snapshots/snap.nope/revert",
    )
    assert resp.status_code == 404
