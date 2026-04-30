"""Tests for graph models and store."""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import pytest
from lattice.graph.models import (
    AuthorGraph, Citation, Claim, ClaimType, Confidence, Cluster, ClusterRole,
    ClaimRoleInCluster, Section, SectionRole, Source, SourceMetadata, SourceType,
    Passage, PassageLocation, PassageType, Relationship, RelationshipType,
)
from lattice.graph.store import GraphStore


def test_claim_round_trip() -> None:
    now = datetime.now()
    c = Claim(
        claim_id="cl.test.001",
        statement="A test claim.",
        type=ClaimType.empirical,
        confidence=Confidence.high,
        created_by="test",
        created_at=now,
        modified_at=now,
    )
    payload = c.model_dump_json()
    restored = Claim.model_validate_json(payload)
    assert restored.claim_id == "cl.test.001"
    assert restored.type == ClaimType.empirical


def test_source_round_trip() -> None:
    now = datetime.now()
    s = Source(
        source_id="test_2024",
        type=SourceType.primary_paper,
        citation=Citation(authors=["Test, A."], year=2024, title="A Test Paper"),
        passages=[
            Passage(
                id="p.1.1",
                text="A passage.",
                location=PassageLocation(page=1, paragraph=1),
                type=PassageType.claim,
                char_count=10,
            )
        ],
        metadata=SourceMetadata(
            date_added=now, file_path="refs/papers/test.pdf", hash="sha256:abc"
        ),
    )
    payload = s.model_dump_json()
    restored = Source.model_validate_json(payload)
    assert restored.source_id == "test_2024"
    assert len(restored.passages) == 1


def test_cluster_with_claim_sequence() -> None:
    c = Cluster(
        cluster_id="c.test.evidence",
        section_id="s.test",
        position=1,
        role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(
                claim_id="cl.001",
                role_in_cluster=ClusterRole.evidence,
                reporting_verb="documents",
            )
        ],
    )
    payload = c.model_dump_json()
    restored = Cluster.model_validate_json(payload)
    assert restored.claim_sequence[0].reporting_verb == "documents"


def test_author_graph_round_trip() -> None:
    now = datetime.now()
    g = AuthorGraph(
        project_name="test",
        thesis_statement="A test thesis.",
        sections=[Section(section_id="s.root", title="Root", position=0, role=SectionRole.introduction)],
        created_at=now,
        modified_at=now,
    )
    payload = g.model_dump_json()
    restored = AuthorGraph.model_validate_json(payload)
    assert restored.project_name == "test"
    assert restored.sections[0].role == SectionRole.introduction


def _make_claim(claim_id: str = "cl.test.001", statement: str = "A test claim.") -> Claim:
    now = datetime.now()
    return Claim(
        claim_id=claim_id,
        statement=statement,
        type=ClaimType.empirical,
        confidence=Confidence.high,
        created_by="test",
        created_at=now,
        modified_at=now,
    )


def test_store_load_creates_lattice_dir(tmp_path: Path) -> None:
    store = GraphStore.load(tmp_path)
    assert store.lattice_dir.is_dir()
    assert store.history_dir.is_dir()
    assert store.edit_proposals_dir.is_dir()


def test_store_save_and_load_claim(tmp_path: Path) -> None:
    store = GraphStore.load(tmp_path)
    store.save_claim(_make_claim())
    loaded = store.get_claim("cl.test.001")
    assert loaded.claim_id == "cl.test.001"
    assert loaded.type == ClaimType.empirical


def test_store_save_claim_upserts(tmp_path: Path) -> None:
    store = GraphStore.load(tmp_path)
    store.save_claim(_make_claim(statement="original"))
    store.save_claim(_make_claim(statement="revised"))
    claims = store.list_claims()
    assert len(claims) == 1
    assert claims[0].statement == "revised"


def test_store_list_claims_filters_by_type(tmp_path: Path) -> None:
    store = GraphStore.load(tmp_path)
    store.save_claim(_make_claim(claim_id="cl.a"))
    user_synth = _make_claim(claim_id="cl.b")
    user_synth.type = ClaimType.user_synthesis
    store.save_claim(user_synth)
    empirical = store.list_claims(type=ClaimType.empirical)
    assert len(empirical) == 1
    assert empirical[0].claim_id == "cl.a"


def test_store_get_missing_claim_raises(tmp_path: Path) -> None:
    store = GraphStore.load(tmp_path)
    with pytest.raises(KeyError):
        store.get_claim("cl.nope")


def test_store_snapshot_copies_current_graph(tmp_path: Path) -> None:
    store = GraphStore.load(tmp_path)
    store.save_claim(_make_claim())
    snapshot_path = store.snapshot(label="pre-edit")
    assert snapshot_path.exists()
    assert "pre-edit" in snapshot_path.name
    assert snapshot_path.read_text(encoding="utf-8").strip().startswith("{")


def test_store_save_and_list_sources(tmp_path: Path) -> None:
    now = datetime.now()
    store = GraphStore.load(tmp_path)
    source = Source(
        source_id="test_2024",
        type=SourceType.primary_paper,
        citation=Citation(authors=["Test, A."], year=2024, title="A Test Paper"),
        passages=[],
        metadata=SourceMetadata(
            date_added=now, file_path="refs/papers/test.pdf", hash="sha256:abc"
        ),
    )
    store.save_source(source)
    loaded = store.list_sources()
    assert len(loaded) == 1
    assert loaded[0].source_id == "test_2024"


def test_store_save_relationship_and_filter(tmp_path: Path) -> None:
    now = datetime.now()
    store = GraphStore.load(tmp_path)
    store.save_relationship(
        Relationship(
            rel_id="r.001",
            type=RelationshipType.supports,
            **{"from": "cl.a", "to": "cl.b"},
            created_by="test",
            created_at=now,
        )
    )
    store.save_relationship(
        Relationship(
            rel_id="r.002",
            type=RelationshipType.contradicts,
            **{"from": "cl.c", "to": "cl.d"},
            created_by="test",
            created_at=now,
        )
    )
    supports = store.list_relationships(type_="supports")
    assert len(supports) == 1
    assert supports[0].from_claim == "cl.a"


def test_store_token_tracking(tmp_path: Path) -> None:
    store = GraphStore.load(tmp_path)
    store.log_tokens("renderer", "run_1", input_tokens=100, output_tokens=50)
    store.log_tokens("renderer", "run_1", input_tokens=200, output_tokens=80)
    total = store.total_cost("run_1")
    assert total == {"input": 300, "output": 130, "calls": 2}
