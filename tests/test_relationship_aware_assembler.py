"""Phase 2 tests: relationship-aware cluster planning + prompt threading.

Covers:
- ``ClusterRelationshipContext`` is populated with intra/incoming/outgoing
  edges for each cluster.
- The cluster builder avoids splitting an interpretive_pivot pair across
  clusters when sequential chunking would otherwise have done so.
- A new edge added to the graph (simulating relationship inference) marks
  the affected cluster dirty on re-plan.
- The renderer prompts include the relationship payload as text.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    AuthorGraph,
    Claim,
    ClaimRoleInCluster,
    ClaimType,
    Cluster,
    ClusterRole,
    Confidence,
    ProseState,
    Relationship,
    RelationshipStrength,
    RelationshipType,
    Section,
    SectionRole,
)
from lattice.graph.store import GraphStore
from lattice.ingester.markdown import MarkdownOutlineIngester
from lattice.renderer.assembler import Assembler
from lattice.renderer.cluster_renderer import _format_relationship_context
from lattice.renderer.chunked_renderer import _format_chunk_relationship_context
from lattice.utils.config import Config
from lattice.voice.parser import Voice


@pytest.fixture
def voice(academic_voice_path: Path) -> Voice:
    return Voice.from_file(academic_voice_path)


async def _setup_project(outline: str, tmp_path: Path) -> tuple[Path, Config]:
    (tmp_path / "config.yml").write_text(
        "default_voice: academic\n", encoding="utf-8"
    )
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    config = Config.load(tmp_path)
    outline_path = tmp_path / "outline.md"
    outline_path.write_text(outline, encoding="utf-8")
    ingester = MarkdownOutlineIngester(config)
    graph = await ingester.ingest(outline_path, project_name="test")
    store = GraphStore.load(tmp_path)
    store.save_graph(graph)
    return tmp_path, config


# ─── relationship_context population ───────────────────


async def test_intra_cluster_edges_appear_in_context(
    tmp_path: Path, voice: Voice,
) -> None:
    """A pair of claims joined by ``[qualifies:]`` and placed in the same
    cluster should produce an intra-cluster ClusterRelationshipContext."""
    project, config = await _setup_project(
        "# THESIS\n\nT.\n\n"
        "# A. Section\n\n"
        "  - First. [type: empirical]\n"
        "  - Second. [type: empirical] [qualifies: cl.a.1]\n",
        tmp_path,
    )
    store = GraphStore.load(project)
    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    a_clusters = [c for c in clusters if c.section_id == "s.a"]
    assert a_clusters, "section A should produce at least one cluster"
    intra = [
        rc for c in a_clusters for rc in c.relationship_context
        if rc.direction == "intra"
    ]
    assert any(
        rc.type == RelationshipType.qualifies
        and rc.from_claim == "cl.a.2" and rc.to_claim == "cl.a.1"
        for rc in intra
    )


async def test_outgoing_edge_to_thesis_appears(
    tmp_path: Path, voice: Voice,
) -> None:
    """A MY VIEW claim implicitly supports the thesis. The thesis lives in
    its own cluster, so the edge should appear as outgoing from the MY
    VIEW cluster and incoming on the thesis cluster."""
    project, config = await _setup_project(
        "# THESIS\n\nThe thesis.\n\n"
        "# A. Section\n\n"
        "  - MY VIEW: a synthesis. [type: user_synthesis]\n",
        tmp_path,
    )
    store = GraphStore.load(project)
    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    by_section = {c.section_id: c for c in clusters}
    # Both "thesis" and "a" sections should have a cluster.
    assert "s.thesis" in by_section
    assert "s.a" in by_section
    a_outgoing = [
        rc for rc in by_section["s.a"].relationship_context
        if rc.direction == "outgoing"
    ]
    assert any(
        rc.type == RelationshipType.supports and rc.to_claim == "cl.thesis"
        for rc in a_outgoing
    )
    thesis_incoming = [
        rc for rc in by_section["s.thesis"].relationship_context
        if rc.direction == "incoming"
    ]
    assert any(
        rc.type == RelationshipType.supports and rc.from_claim != "cl.thesis"
        for rc in thesis_incoming
    )


# ─── interpretive_pivot keeps claims together ──────────


async def test_interpretive_pivot_pair_stays_in_one_cluster(
    tmp_path: Path, voice: Voice,
) -> None:
    """Without relationship-awareness, a long string of role-marked claims
    plus a final boundary role would split the pivot pair across clusters.
    With sticky-edge logic, the pivot pair should stay together."""
    project, config = await _setup_project(
        "# THESIS\n\nT.\n\n"
        "# A. Section\n\n"
        "  - First. [type: empirical] [role: setup]\n"
        "  - Second. [type: empirical] [role: evidence]\n"
        "  - Third. [type: empirical] [role: synthesis]\n"
        "  - Pivot move A. [type: empirical] [role: evidence]\n"
        "  - Pivot move B. [type: empirical] [role: evidence] [pivot: cl.a.4]\n",
        tmp_path,
    )
    store = GraphStore.load(project)
    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    a_clusters = [c for c in clusters if c.section_id == "s.a"]
    # The pivot pair (cl.a.4, cl.a.5) must end up in the same cluster.
    pair_cluster = None
    for cl in a_clusters:
        ids = {entry.claim_id for entry in cl.claim_sequence}
        if "cl.a.4" in ids and "cl.a.5" in ids:
            pair_cluster = cl
            break
    assert pair_cluster is not None, (
        f"interpretive_pivot pair was split across clusters: "
        f"{[(c.cluster_id, [e.claim_id for e in c.claim_sequence]) for c in a_clusters]}"
    )


# ─── dirty-marking on inference ────────────────────────


async def test_new_inferred_edge_marks_cluster_dirty(
    tmp_path: Path, voice: Voice,
) -> None:
    """Adding a new relationship that touches a cluster's claims should
    flip its prose_state from generated to dirty on re-plan."""
    project, config = await _setup_project(
        "# THESIS\n\nT.\n\n"
        "# A. Section\n\n"
        "  - First. [type: empirical]\n"
        "  - Second. [type: empirical]\n",
        tmp_path,
    )
    store = GraphStore.load(project)
    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()

    # Simulate a clean draft: mark every cluster as generated.
    for cluster in clusters:
        cluster.prose_state = ProseState.generated
        store.save_cluster(cluster)

    # Inject a new sticky edge into the graph.
    graph = store.get_graph()
    graph.relationships.append(
        Relationship(
            rel_id="r.new",
            type=RelationshipType.qualifies,
            **{"from": "cl.a.2", "to": "cl.a.1"},
            strength=RelationshipStrength.direct,
            note="",
            created_by="relationship_inference",
            created_at=datetime.now(timezone.utc),
        )
    )
    store.save_graph(graph)

    # Re-plan. The cluster containing cl.a.1 / cl.a.2 should be dirty now.
    new_clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    affected = [
        c for c in new_clusters
        if any(e.claim_id in {"cl.a.1", "cl.a.2"} for e in c.claim_sequence)
    ]
    assert affected, "the test setup should have produced at least one cluster"
    assert any(c.prose_state == ProseState.dirty for c in affected)


async def test_no_signature_change_keeps_state(
    tmp_path: Path, voice: Voice,
) -> None:
    """Re-running build_plan when nothing changed should NOT churn
    prose_state from generated to dirty."""
    project, config = await _setup_project(
        "# THESIS\n\nT.\n\n"
        "# A. Section\n\n"
        "  - First. [type: empirical]\n"
        "  - Second. [type: empirical] [qualifies: cl.a.1]\n",
        tmp_path,
    )
    store = GraphStore.load(project)
    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    for cluster in clusters:
        cluster.prose_state = ProseState.generated
        store.save_cluster(cluster)

    # Re-plan with no graph changes.
    new_clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    assert all(c.prose_state == ProseState.generated for c in new_clusters)


# ─── prompt formatting ─────────────────────────────────


def _make_cluster_with_context() -> Cluster:
    from lattice.graph.models import ClusterRelationshipContext
    cluster = Cluster(
        cluster_id="c.x.1",
        section_id="s.x",
        position=1,
        role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(
                claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence
            ),
            ClaimRoleInCluster(
                claim_id="cl.x.2", role_in_cluster=ClusterRole.evidence
            ),
        ],
    )
    cluster.relationship_context = [
        ClusterRelationshipContext(
            rel_id="r.001",
            type=RelationshipType.interpretive_pivot,
            strength=RelationshipStrength.direct,
            note="diagnostic move",
            direction="intra",
            from_claim="cl.x.2",
            to_claim="cl.x.1",
            affects_rendering=True,
        ),
        ClusterRelationshipContext(
            rel_id="r.002",
            type=RelationshipType.supports,
            strength=RelationshipStrength.direct,
            note="",
            direction="outgoing",
            from_claim="cl.x.1",
            to_claim="cl.thesis",
            other_cluster_id="c.thesis.1",
            other_section_id="s.thesis",
            affects_rendering=True,
        ),
        ClusterRelationshipContext(
            rel_id="r.003",
            type=RelationshipType.unlabelled,
            strength=RelationshipStrength.inferred,
            note="",
            direction="intra",
            from_claim="cl.x.1",
            to_claim="cl.x.2",
            affects_rendering=False,
        ),
    ]
    return cluster


def test_cluster_renderer_prompt_includes_interpretive_pivot() -> None:
    cluster = _make_cluster_with_context()
    rendered = _format_relationship_context(cluster)
    assert "interpretive_pivot" in rendered
    assert "cl.x.2" in rendered
    assert "cl.x.1" in rendered
    # Non-rendering edge should not appear.
    assert "unlabelled" not in rendered


def test_cluster_renderer_prompt_separates_intra_and_outgoing() -> None:
    cluster = _make_cluster_with_context()
    rendered = _format_relationship_context(cluster)
    assert "intra-cluster" in rendered
    assert "outgoing" in rendered


def test_chunked_renderer_prompt_includes_relationship_payload() -> None:
    cluster = _make_cluster_with_context()
    rendered = _format_chunk_relationship_context(cluster)
    assert "interpretive_pivot" in rendered
    assert "cl.x.2" in rendered
    # Outgoing edge to thesis should be included with its target cluster id.
    assert "c.thesis.1" in rendered
