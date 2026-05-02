"""Phase 3 tests: enriched visualisation payload + cytoscape compound rendering.

Covers:
- ``build_visualisation_payload`` exposes sections, clusters, claims with
  state/evidence/relationship data (not just flat claim nodes).
- Cluster compound nodes appear in the rendered HTML when clusters are
  passed; failed/dirty/blocked clusters are tagged appropriately.
- Evidence quality bucketing handles bound, source_hint, unbound,
  contradictory, and author-original cases.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimType, ClaimRoleInCluster,
    Cluster, ClusterRole, Confidence, Evidence, EvidenceStatus, ProseState,
    Relationship, RelationshipStrength, RelationshipType, Section, SectionRole,
)
from lattice.output.visualise import (
    build_visualisation_payload, render_html,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_graph_with_clusters() -> tuple[AuthorGraph, list[Cluster]]:
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        thesis_statement="The thesis.",
        sections=[
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="Body", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1", "cl.a.2", "cl.a.3"]),
        ],
        claims=[
            Claim(claim_id="cl.thesis", statement="The thesis.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.thesis",
                  created_by="t", created_at=now, modified_at=now),
            Claim(claim_id="cl.a.1",
                  statement="Bound empirical claim.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  importance=0.8, mechanism="A causes B because C",
                  scope_conditions=["after 2010"],
                  evidence=[
                      Evidence(source="koomey_2015", passage="p.3.2",
                               binding_strength=BindingStrength.strong),
                  ],
                  section_id="s.a", created_by="t",
                  created_at=now, modified_at=now,
                  tags=["role:evidence"]),
            Claim(claim_id="cl.a.2",
                  statement="Source-hint claim.",
                  type=ClaimType.empirical, confidence=Confidence.medium,
                  evidence_status=EvidenceStatus.source_hint,
                  evidence=[
                      Evidence(source="lee_2019", passage="",
                               binding_strength=BindingStrength.weak),
                  ],
                  section_id="s.a", created_by="t",
                  created_at=now, modified_at=now),
            Claim(claim_id="cl.a.3",
                  statement="Author synthesis tying together.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.a",
                  created_by="t", created_at=now, modified_at=now),
        ],
        relationships=[
            Relationship(rel_id="r.1", type=RelationshipType.supports,
                         **{"from": "cl.a.3", "to": "cl.thesis"},
                         strength=RelationshipStrength.direct, note="",
                         created_by="t", created_at=now),
            Relationship(rel_id="r.2", type=RelationshipType.qualifies,
                         **{"from": "cl.a.2", "to": "cl.a.1"},
                         strength=RelationshipStrength.direct, note="",
                         created_by="t", created_at=now),
        ],
        created_at=now, modified_at=now,
    )
    clusters = [
        Cluster(
            cluster_id="c.a.1", section_id="s.a", position=1,
            role=ClusterRole.evidence,
            claim_sequence=[
                ClaimRoleInCluster(claim_id="cl.a.1", role_in_cluster=ClusterRole.evidence),
                ClaimRoleInCluster(claim_id="cl.a.2", role_in_cluster=ClusterRole.evidence),
            ],
            prose_state=ProseState.dirty,
        ),
        Cluster(
            cluster_id="c.a.2", section_id="s.a", position=2,
            role=ClusterRole.synthesis,
            claim_sequence=[
                ClaimRoleInCluster(claim_id="cl.a.3", role_in_cluster=ClusterRole.synthesis),
            ],
            prose_state=ProseState.failed,
        ),
    ]
    return graph, clusters


# ─── payload shape ─────────────────────────────────────


def test_payload_exposes_sections_clusters_claims_relationships() -> None:
    graph, clusters = _make_graph_with_clusters()
    payload = build_visualisation_payload(graph, clusters=clusters)
    assert {"meta", "sections", "clusters", "claims", "relationships"} <= payload.keys()
    assert payload["meta"]["section_count"] >= 2
    assert payload["meta"]["cluster_count"] == 2
    assert payload["meta"]["claim_count"] >= 4
    assert payload["meta"]["relationship_count"] == 2


def test_payload_clusters_carry_state_and_blocking() -> None:
    graph, clusters = _make_graph_with_clusters()
    payload = build_visualisation_payload(
        graph,
        clusters=clusters,
        audit_flags_by_cluster={"c.a.1": 3},
        readiness_blocking_clusters={"c.a.2"},
    )
    by_id = {c["id"]: c for c in payload["clusters"]}
    assert by_id["c.a.1"]["prose_state"] == "dirty"
    assert by_id["c.a.1"]["is_dirty"] is True
    assert by_id["c.a.1"]["audit_flag_count"] == 3
    assert by_id["c.a.2"]["prose_state"] == "failed"
    assert by_id["c.a.2"]["is_failed"] is True
    assert by_id["c.a.2"]["blocks_readiness"] is True


def test_payload_claim_evidence_quality_buckets() -> None:
    graph, clusters = _make_graph_with_clusters()
    payload = build_visualisation_payload(graph, clusters=clusters)
    by_id = {c["id"]: c for c in payload["claims"]}
    assert by_id["cl.a.1"]["evidence_quality"] == "bound"
    # cl.a.2 has explicit evidence_status=source_hint AND a weak Evidence.
    assert by_id["cl.a.2"]["evidence_quality"] == "source_hint"
    # User-synthesis with author_origin and no evidence → "author".
    assert by_id["cl.a.3"]["evidence_quality"] == "author"


def test_payload_includes_mechanism_and_scope() -> None:
    graph, clusters = _make_graph_with_clusters()
    payload = build_visualisation_payload(graph, clusters=clusters)
    by_id = {c["id"]: c for c in payload["claims"]}
    assert "A causes B because C" in (by_id["cl.a.1"]["mechanism"] or "")
    assert "after 2010" in by_id["cl.a.1"]["scope_conditions"]


def test_payload_assigns_cluster_id_to_each_claim() -> None:
    graph, clusters = _make_graph_with_clusters()
    payload = build_visualisation_payload(graph, clusters=clusters)
    by_id = {c["id"]: c for c in payload["claims"]}
    assert by_id["cl.a.1"]["cluster_id"] == "c.a.1"
    assert by_id["cl.a.2"]["cluster_id"] == "c.a.1"
    assert by_id["cl.a.3"]["cluster_id"] == "c.a.2"


# ─── HTML rendering ────────────────────────────────────


def test_html_with_clusters_emits_cluster_compound_nodes() -> None:
    graph, clusters = _make_graph_with_clusters()
    out = render_html(graph, clusters=clusters)
    # Cluster compound nodes embedded in elements JSON.
    assert '"id": "c.a.1"' in out or '"id":"c.a.1"' in out
    assert '"id": "c.a.2"' in out or '"id":"c.a.2"' in out
    # Section compound nodes.
    assert '"id": "s.a"' in out or '"id":"s.a"' in out
    # Claim nodes set ``parent`` to their cluster, so cytoscape groups them.
    assert '"parent": "c.a.1"' in out or '"parent":"c.a.1"' in out
    # State markers come through.
    assert '"isDirty": true' in out or '"isDirty":true' in out
    assert '"isFailed": true' in out or '"isFailed":true' in out


def test_html_without_clusters_keeps_flat_layout() -> None:
    """Backwards-compat: passing only ``graph`` should produce the
    pre-Phase-3 flat-node diagram (no compound section/cluster nodes)."""
    graph, _ = _make_graph_with_clusters()
    out = render_html(graph)
    # No compound nodes when clusters not provided.
    assert '"type": "cluster"' not in out and '"type":"cluster"' not in out
    assert '"type": "section"' not in out and '"type":"section"' not in out


def test_html_includes_filter_sidebar() -> None:
    graph, clusters = _make_graph_with_clusters()
    out = render_html(graph, clusters=clusters)
    # Filter checkboxes for the new claim-state filters.
    for label in (
        "unsupported", "synthesis", "weak_evidence",
        "dirty_clusters", "touched_since_render",
    ):
        assert f'data-filter="{label}"' in out
    # Edge-type filter container.
    assert 'id="edge-filter"' in out


def test_audit_and_readiness_propagate_to_payload() -> None:
    graph, clusters = _make_graph_with_clusters()
    out = render_html(
        graph,
        clusters=clusters,
        audit_flags_by_cluster={"c.a.1": 5},
        readiness_blocking_clusters={"c.a.1"},
    )
    assert '"auditFlagCount": 5' in out or '"auditFlagCount":5' in out
    assert '"blocksReadiness": true' in out or '"blocksReadiness":true' in out


# ─── unrenderable marker detection ─────────────────────


def test_unrenderable_marker_detected_from_drafts(tmp_path: Path) -> None:
    """When a draft file under ``drafts_dir`` contains a MISSING_CLAIM
    marker, the cluster's ``has_unrenderable_marker`` flag should fire."""
    graph, clusters = _make_graph_with_clusters()
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "cluster_c.a.1.md").write_text(
        "Some prose. {MISSING_CLAIM: cluster_id=\"c.a.1\", "
        "claim_id=\"cl.a.1\", description=\"unbound\"} more prose.",
        encoding="utf-8",
    )
    payload = build_visualisation_payload(
        graph, clusters=clusters, drafts_dir=drafts,
    )
    by_id = {c["id"]: c for c in payload["clusters"]}
    assert by_id["c.a.1"]["has_unrenderable_marker"] is True
    assert by_id["c.a.2"]["has_unrenderable_marker"] is False


# ─── relationships are preserved ───────────────────────


def test_relationships_in_payload_keep_strength_and_note() -> None:
    graph, clusters = _make_graph_with_clusters()
    payload = build_visualisation_payload(graph, clusters=clusters)
    by_id = {r["id"]: r for r in payload["relationships"]}
    assert by_id["r.1"]["type"] == "supports"
    assert by_id["r.1"]["strength"] == "direct"
    assert by_id["r.2"]["type"] == "qualifies"
