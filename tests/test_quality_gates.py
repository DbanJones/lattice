"""Phase 4 tests: scaffold audit, render-readiness extensions, diagram readiness.

The three quality gates run at three different points in the pipeline:
- ``audit_scaffold`` after ingest, before plan
- ``DocumentReadinessCheck._check_*`` (Phase 4b) before draft
- ``audit_diagram`` against the visualisation payload before serving HTML
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.auditor.diagram import audit_diagram
from lattice.auditor.scaffold import audit_scaffold
from lattice.auditor.readiness import DocumentReadinessCheck
from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimRoleInCluster, ClaimType,
    Cluster, ClusterRole, Confidence, Evidence, EvidenceStatus, ProseState,
    Relationship, RelationshipStrength, RelationshipType, Section, SectionRole,
)
from lattice.graph.store import GraphStore
from lattice.output.visualise import build_visualisation_payload
from lattice.utils.config import Config
from lattice.voice.parser import Voice


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Phase 4a: scaffold audit ───────────────────────────


def _minimal_clean_graph() -> AuthorGraph:
    now = _now()
    return AuthorGraph(
        project_name="t",
        thesis_statement="The thesis.",
        sections=[
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="Body", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1"]),
            Section(section_id="s.z", title="Conclusion", position=2,
                    role=SectionRole.conclusion,
                    claim_ids=["cl.z.1"]),
        ],
        claims=[
            Claim(claim_id="cl.thesis", statement="Thesis.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.thesis",
                  created_by="t", created_at=now, modified_at=now),
            Claim(claim_id="cl.a.1",
                  statement="Body claim.", type=ClaimType.empirical,
                  confidence=Confidence.high,
                  evidence=[Evidence(source="koomey_2015", passage="p.1.1",
                                     binding_strength=BindingStrength.strong)],
                  section_id="s.a", created_by="t",
                  created_at=now, modified_at=now),
            Claim(claim_id="cl.z.1", statement="Closing.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.z",
                  created_by="t", created_at=now, modified_at=now),
        ],
        relationships=[
            Relationship(rel_id="r.1", type=RelationshipType.supports,
                         **{"from": "cl.z.1", "to": "cl.thesis"},
                         strength=RelationshipStrength.direct, note="",
                         created_by="t", created_at=now),
        ],
        created_at=now, modified_at=now,
    )


def test_clean_graph_passes_scaffold_audit() -> None:
    report = audit_scaffold(_minimal_clean_graph())
    assert report.is_clean
    assert report.error_count == 0


def test_empty_argumentative_section_flagged() -> None:
    graph = _minimal_clean_graph()
    graph.sections.append(
        Section(section_id="s.b", title="Empty", position=3,
                role=SectionRole.argumentative, claim_ids=[])
    )
    report = audit_scaffold(graph)
    assert any(f.code == "empty_section" for f in report.findings)
    assert not report.is_clean


def test_empty_section_with_children_is_info_not_error() -> None:
    graph = _minimal_clean_graph()
    graph.sections.append(
        Section(section_id="s.b", title="Parent", position=3,
                role=SectionRole.argumentative, claim_ids=[])
    )
    graph.sections.append(
        Section(section_id="s.b.1", title="Child", position=4,
                role=SectionRole.argumentative, parent="s.b",
                claim_ids=["cl.b_1.1"])
    )
    now = _now()
    graph.claims.append(
        Claim(claim_id="cl.b_1.1", statement="X.", type=ClaimType.user_synthesis,
              confidence=Confidence.high, author_origin=True,
              section_id="s.b.1", created_by="t",
              created_at=now, modified_at=now)
    )
    report = audit_scaffold(graph)
    findings = report.by_code("empty_section_with_children")
    assert findings and findings[0].severity == "info"


def test_orphan_claim_flagged() -> None:
    graph = _minimal_clean_graph()
    now = _now()
    graph.claims.append(
        Claim(claim_id="cl.orphan",
              statement="Not in any section.",
              type=ClaimType.empirical, confidence=Confidence.high,
              section_id=None, created_by="t",
              created_at=now, modified_at=now)
    )
    report = audit_scaffold(graph)
    assert any(f.code == "orphan_claim" for f in report.findings)


def test_dangling_section_claim_ref_flagged() -> None:
    graph = _minimal_clean_graph()
    graph.sections[1].claim_ids.append("cl.does_not_exist")
    report = audit_scaffold(graph)
    assert any(
        f.code == "dangling_section_claim_ref"
        for f in report.findings
    )


def test_empirical_claim_without_evidence_or_status_flagged() -> None:
    graph = _minimal_clean_graph()
    now = _now()
    graph.claims.append(
        Claim(claim_id="cl.a.2", statement="Empirical with no backing.",
              type=ClaimType.empirical, confidence=Confidence.high,
              section_id="s.a", created_by="t",
              created_at=now, modified_at=now)
    )
    graph.sections[1].claim_ids.append("cl.a.2")
    report = audit_scaffold(graph)
    assert any(
        f.code == "claim_missing_evidence_signal"
        and f.claim_id == "cl.a.2"
        for f in report.findings
    )


def test_evidence_status_satisfies_evidence_check() -> None:
    """An empirical claim with [evidence_status: source_hint] should NOT
    fire claim_missing_evidence_signal — the author has acknowledged the
    state explicitly."""
    graph = _minimal_clean_graph()
    now = _now()
    graph.claims.append(
        Claim(claim_id="cl.a.2", statement="Acknowledged gap.",
              type=ClaimType.empirical, confidence=Confidence.high,
              evidence_status=EvidenceStatus.source_hint,
              section_id="s.a", created_by="t",
              created_at=now, modified_at=now)
    )
    graph.sections[1].claim_ids.append("cl.a.2")
    report = audit_scaffold(graph)
    assert not any(
        f.code == "claim_missing_evidence_signal"
        and f.claim_id == "cl.a.2"
        for f in report.findings
    )


def test_dangling_relationship_target_flagged() -> None:
    graph = _minimal_clean_graph()
    now = _now()
    graph.relationships.append(
        Relationship(rel_id="r.bad", type=RelationshipType.qualifies,
                     **{"from": "cl.a.1", "to": "cl.does_not_exist"},
                     strength=RelationshipStrength.direct, note="",
                     created_by="t", created_at=now)
    )
    report = audit_scaffold(graph)
    assert any(f.code == "relationship_dangling_to" for f in report.findings)


def test_no_conclusion_section_flagged() -> None:
    graph = _minimal_clean_graph()
    # Drop the conclusion section.
    graph.sections = [s for s in graph.sections if s.role != SectionRole.conclusion]
    report = audit_scaffold(graph)
    assert any(f.code == "no_conclusion_section" for f in report.findings)


def test_thesis_disconnected_warns() -> None:
    graph = _minimal_clean_graph()
    # Drop the supports edge into the thesis.
    graph.relationships = []
    report = audit_scaffold(graph)
    assert any(f.code == "thesis_disconnected" for f in report.findings)


# ─── Phase 4b: render-readiness additions ──────────────


def _seed_project_for_readiness(tmp_path: Path) -> tuple[Path, Voice]:
    project = tmp_path / "p"
    project.mkdir(exist_ok=True)
    (project / ".lattice").mkdir(exist_ok=True)
    (project / "config.yml").write_text(
        "default_voice: academic\n", encoding="utf-8"
    )
    voices_dir = project / "voices"
    voices_dir.mkdir(exist_ok=True)
    voice_src = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8"
    )
    voice = Voice.from_file(voices_dir / "academic.voice.md")
    return project, voice


def test_weak_grounding_flagged_before_render(tmp_path: Path) -> None:
    project, voice = _seed_project_for_readiness(tmp_path)
    store = GraphStore.load(project)
    now = _now()
    graph = AuthorGraph(
        project_name="p", thesis_statement="T.",
        sections=[
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="Body", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1"]),
            Section(section_id="s.z", title="Conclusion", position=2,
                    role=SectionRole.conclusion, claim_ids=["cl.z.1"]),
        ],
        claims=[
            Claim(claim_id="cl.thesis", statement="T.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.thesis",
                  created_by="t", created_at=now, modified_at=now),
            Claim(claim_id="cl.a.1", statement="Empirical no backing.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  section_id="s.a", created_by="t",
                  created_at=now, modified_at=now),
            Claim(claim_id="cl.z.1", statement="Closing.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.z",
                  created_by="t", created_at=now, modified_at=now),
        ],
        relationships=[],
        created_at=now, modified_at=now,
    )
    store.save_graph(graph)
    check = DocumentReadinessCheck(store, voice, project)
    flags = check._check_weak_grounding_marked()
    assert any(f.rule_id == "readiness.claim_weak_grounding" for f in flags)


def test_unsane_word_range_flagged(tmp_path: Path) -> None:
    project, voice = _seed_project_for_readiness(tmp_path)
    store = GraphStore.load(project)
    cluster = Cluster(
        cluster_id="c.x.1", section_id="s.x", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence)
        ],
        target_words_min=20, target_words_max=10,  # min > max
    )
    store.save_cluster(cluster)
    check = DocumentReadinessCheck(store, voice, project)
    flags = check._check_sane_word_ranges()
    assert any(f.rule_id == "readiness.cluster_word_range_unsane" for f in flags)


def test_cluster_missing_relationship_context_flagged(tmp_path: Path) -> None:
    """If the graph has a sticky-edge pair that lives entirely inside a
    cluster but the cluster's relationship_context is empty, the check
    fires (the assembler likely ran on an older schema)."""
    project, voice = _seed_project_for_readiness(tmp_path)
    store = GraphStore.load(project)
    now = _now()
    graph = AuthorGraph(
        project_name="p", thesis_statement="T.",
        sections=[
            Section(section_id="s.a", title="Body", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1", "cl.a.2"]),
        ],
        claims=[
            Claim(claim_id="cl.a.1", statement="A.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  section_id="s.a", created_by="t",
                  created_at=now, modified_at=now),
            Claim(claim_id="cl.a.2", statement="B.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  section_id="s.a", created_by="t",
                  created_at=now, modified_at=now),
        ],
        relationships=[
            Relationship(rel_id="r.1", type=RelationshipType.qualifies,
                         **{"from": "cl.a.2", "to": "cl.a.1"},
                         strength=RelationshipStrength.direct, note="",
                         created_by="t", created_at=now),
        ],
        created_at=now, modified_at=now,
    )
    store.save_graph(graph)
    cluster = Cluster(
        cluster_id="c.a.1", section_id="s.a", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(claim_id="cl.a.1", role_in_cluster=ClusterRole.evidence),
            ClaimRoleInCluster(claim_id="cl.a.2", role_in_cluster=ClusterRole.evidence),
        ],
        relationship_context=[],  # the bug condition
    )
    store.save_cluster(cluster)
    check = DocumentReadinessCheck(store, voice, project)
    flags = check._check_relationship_aware_clusters()
    assert any(
        f.rule_id == "readiness.cluster_missing_relationship_context"
        for f in flags
    )


# ─── Phase 4c: diagram readiness ───────────────────────


def test_diagram_clean_payload_passes() -> None:
    payload = {
        "sections": [
            {"id": "s.a", "parent": None},
        ],
        "claims": [
            {"id": "cl.a.1", "type": "empirical",
             "evidence_quality": "bound", "author_origin": False,
             "evidence": [{"source": "x"}]},
        ],
        "relationships": [],
    }
    report = audit_diagram(payload)
    assert report.is_clean


def test_diagram_dangling_section_parent_flagged() -> None:
    payload = {
        "sections": [
            {"id": "s.a.1", "parent": "s.does_not_exist"},
        ],
        "claims": [],
        "relationships": [],
    }
    report = audit_diagram(payload)
    assert any(f.code == "section_dangling_parent" for f in report.findings)
    assert not report.is_clean


def test_diagram_dangling_edge_endpoints_flagged() -> None:
    payload = {
        "sections": [{"id": "s.a", "parent": None}],
        "claims": [
            {"id": "cl.a.1", "type": "empirical",
             "evidence_quality": "bound",
             "evidence": [{"source": "x"}]},
        ],
        "relationships": [
            {"id": "r.1", "source": "cl.a.1",
             "target": "cl.does_not_exist", "type": "supports"},
        ],
    }
    report = audit_diagram(payload)
    assert any(f.code == "edge_dangling_target" for f in report.findings)


def test_diagram_bound_claim_without_evidence_flagged() -> None:
    """The payload-builder lying: showing a 'bound' badge on a claim
    that actually has no evidence rows."""
    payload = {
        "sections": [{"id": "s.a", "parent": None}],
        "claims": [
            {"id": "cl.a.1", "type": "empirical",
             "evidence_quality": "bound", "evidence": []},
        ],
        "relationships": [],
    }
    report = audit_diagram(payload)
    assert any(
        f.code == "bound_claim_without_evidence" for f in report.findings
    )
    assert not report.is_clean


def test_diagram_stale_html_flagged(tmp_path: Path) -> None:
    """When the cached HTML is older than its inputs, the audit warns."""
    import os, time
    payload = {"sections": [], "claims": [], "relationships": []}
    cached = tmp_path / "graph.html"
    cached.write_text("<html></html>", encoding="utf-8")
    input_path = tmp_path / "author_graph.json"
    input_path.write_text("{}", encoding="utf-8")
    # Push the input mtime past the cached html.
    new_t = time.time() + 10
    os.utime(input_path, (new_t, new_t))
    report = audit_diagram(
        payload,
        cached_html_path=cached,
        input_paths=[input_path],
    )
    assert any(f.code == "cached_html_stale" for f in report.findings)
