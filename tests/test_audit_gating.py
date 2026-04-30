"""Acceptance tests for Fix 1: audit must gate delivery."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.auditor.readiness import DocumentReadinessCheck
from lattice.graph.models import (
    AuditFlag, BindingStrength, Citation, Claim, ClaimRoleInCluster, ClaimType,
    Cluster, ClusterRole, Confidence, EditMode, Evidence, FlagCategory,
    Passage, PassageLocation, PassageType, ProseLocation, ProseState, Section,
    SectionRole, Severity, Source, SourceMetadata, SourceType,
)
from lattice.graph.store import GraphStore
from lattice.renderer.assembler_finalise import DocumentFinaliser
from lattice.voice.parser import Voice


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _academic_voice() -> Voice:
    return Voice.from_file(
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )


def _mk_full_project(tmp_path: Path) -> tuple[GraphStore, Voice, Path]:
    """A graph that satisfies all readiness checks. Tests can then mutate
    one piece to verify a single gate fires."""
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    voice = _academic_voice()

    # Three sections that satisfy six_element_paper readiness:
    # introduction (s.thesis), argumentative (s.body), conclusion (s.end).
    sections = [
        Section(section_id="s.thesis", title="Thesis", position=0,
                role=SectionRole.introduction, claim_ids=["cl.thesis"]),
        Section(section_id="s.body", title="Body", position=1,
                role=SectionRole.argumentative, claim_ids=["cl.body.1"]),
        Section(section_id="s.end", title="Conclusion", position=2,
                role=SectionRole.conclusion, claim_ids=["cl.end.1"]),
    ]
    for s in sections:
        store.save_section(s)

    claims = [
        Claim(claim_id="cl.thesis", statement="Thesis statement.",
              type=ClaimType.user_synthesis, confidence=Confidence.high,
              author_origin=True, section_id="s.thesis",
              created_by="test", created_at=_now(), modified_at=_now()),
        Claim(claim_id="cl.body.1", statement="Body claim.",
              type=ClaimType.user_synthesis, confidence=Confidence.high,
              author_origin=True, section_id="s.body",
              created_by="test", created_at=_now(), modified_at=_now()),
        Claim(claim_id="cl.end.1", statement="Closing claim.",
              type=ClaimType.user_synthesis, confidence=Confidence.high,
              author_origin=True, section_id="s.end",
              created_by="test", created_at=_now(), modified_at=_now()),
    ]
    for c in claims:
        store.save_claim(c)

    # One cluster per section, each in ProseState.generated.
    clusters = [
        Cluster(cluster_id="c.thesis.1", section_id="s.thesis", position=1,
                role=ClusterRole.synthesis,
                claim_sequence=[ClaimRoleInCluster(
                    claim_id="cl.thesis", role_in_cluster=ClusterRole.synthesis,
                )],
                prose_state=ProseState.generated,
                prose_file=".lattice/drafts/academic/cluster_c.thesis.1.md"),
        Cluster(cluster_id="c.body.1", section_id="s.body", position=1,
                role=ClusterRole.evidence,
                claim_sequence=[ClaimRoleInCluster(
                    claim_id="cl.body.1", role_in_cluster=ClusterRole.evidence,
                )],
                prose_state=ProseState.generated,
                prose_file=".lattice/drafts/academic/cluster_c.body.1.md"),
        Cluster(cluster_id="c.end.1", section_id="s.end", position=1,
                role=ClusterRole.conclusion,
                claim_sequence=[ClaimRoleInCluster(
                    claim_id="cl.end.1", role_in_cluster=ClusterRole.conclusion,
                )],
                prose_state=ProseState.generated,
                prose_file=".lattice/drafts/academic/cluster_c.end.1.md"),
    ]
    for c in clusters:
        store.save_cluster(c)

    # Write clean prose for each cluster.
    drafts = tmp_path / ".lattice" / "drafts" / "academic"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "cluster_c.thesis.1.md").write_text(
        "Thesis paragraph rendered cleanly.", encoding="utf-8"
    )
    (drafts / "cluster_c.body.1.md").write_text(
        "Body paragraph rendered cleanly.", encoding="utf-8"
    )
    (drafts / "cluster_c.end.1.md").write_text(
        "Closing paragraph rendered cleanly.", encoding="utf-8"
    )

    return store, voice, tmp_path


def _mk_critical_flag(rule_id: str = "voice.banned_word.test") -> AuditFlag:
    return AuditFlag(
        flag_id=f"f.crit.{rule_id}",
        category=FlagCategory.voice,
        rule_id=rule_id,
        severity=Severity.critical,
        default_mode=EditMode.suggest_changes,
        cluster_id="c.body.1",
        section_id="s.body",
        prose_location=ProseLocation(paragraph_index=0, char_start=0, char_end=10),
        offending_text="bad",
        rule_description="critical voice violation",
        suggestion="fix it",
        voice_name="academic",
        created_at=_now(),
    )


# ─── DocumentFinaliser refusal paths ────────────────

def test_finalise_succeeds_when_all_checks_pass(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    finaliser = DocumentFinaliser(project, store, voice)
    result = finaliser.finalise()
    assert result is not None
    assert result.exists()
    output = result.read_text(encoding="utf-8")
    assert "Body paragraph" in output
    assert "Closing paragraph" in output
    # No delivery_blocked.md should exist when finalise succeeds.
    assert not (project / ".lattice" / "delivery_blocked.md").exists()


def test_finalise_refuses_when_cluster_failed(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    # Mark one cluster failed.
    body_cluster = store.get_cluster("c.body.1")
    body_cluster.prose_state = ProseState.failed
    store.save_cluster(body_cluster)

    result = DocumentFinaliser(project, store, voice).finalise()
    assert result is None
    blocked = project / ".lattice" / "delivery_blocked.md"
    assert blocked.exists()
    assert "cluster_not_rendered" in blocked.read_text(encoding="utf-8")
    # No outputs/ written.
    assert not (project / "outputs" / "paper.academic.md").exists()


def test_finalise_refuses_when_marker_in_prose(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    drafts = project / ".lattice" / "drafts" / "academic"
    (drafts / "cluster_c.body.1.md").write_text(
        'Some prose. {MISSING_CLAIM: cluster_id="c.body.1", claim_id="cl.x", '
        'description="needed"} more.',
        encoding="utf-8",
    )

    result = DocumentFinaliser(project, store, voice).finalise()
    assert result is None
    blocked = (project / ".lattice" / "delivery_blocked.md").read_text(encoding="utf-8")
    assert "missing_claim_marker_present" in blocked


def test_finalise_refuses_when_unrenderable_marker_in_prose(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    drafts = project / ".lattice" / "drafts" / "academic"
    (drafts / "cluster_c.body.1.md").write_text(
        '{CLUSTER_UNRENDERABLE: cluster_id="c.body.1", reason="no bindings"}',
        encoding="utf-8",
    )
    body_cluster = store.get_cluster("c.body.1")
    body_cluster.prose_state = ProseState.failed
    store.save_cluster(body_cluster)

    result = DocumentFinaliser(project, store, voice).finalise()
    assert result is None
    blocked = (project / ".lattice" / "delivery_blocked.md").read_text(encoding="utf-8")
    # The cluster_not_rendered rule fires (failed state) AND the marker rule.
    assert "cluster_not_rendered" in blocked or "unrenderable_marker" in blocked


def test_finalise_refuses_when_section_empty(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    # Add a fourth section with no clusters.
    store.save_section(Section(
        section_id="s.empty", title="Empty section", position=3,
        role=SectionRole.argumentative, claim_ids=[],
    ))

    result = DocumentFinaliser(project, store, voice).finalise()
    assert result is None
    blocked = (project / ".lattice" / "delivery_blocked.md").read_text(encoding="utf-8")
    assert "section_has_no_clusters" in blocked


def test_finalise_refuses_when_no_closing_section(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    # Remove the conclusion section by replacing the graph's section list.
    graph = store.get_graph()
    graph.sections = [s for s in graph.sections if s.section_id != "s.end"]
    store.save_graph(graph)

    result = DocumentFinaliser(project, store, voice).finalise()
    assert result is None
    blocked = (project / ".lattice" / "delivery_blocked.md").read_text(encoding="utf-8")
    # six_element_paper requires a 'conclusion' role section.
    assert (
        "document_lacks_closing" in blocked
        or "required_section_missing" in blocked
    )


def test_finalise_refuses_when_critical_flags_unresolved(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    # Inject one unresolved critical flag.
    store.save_audit_flags(voice.name, [_mk_critical_flag()])

    result = DocumentFinaliser(project, store, voice).finalise()
    assert result is None
    blocked = (project / ".lattice" / "delivery_blocked.md").read_text(encoding="utf-8")
    assert "Critical audit flags unresolved" in blocked


def test_finalise_succeeds_with_resolved_critical_flags(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    flag = _mk_critical_flag()
    store.save_audit_flags(voice.name, [flag])
    store.update_flag_decision(flag.flag_id, "accept_suggest_changes")

    result = DocumentFinaliser(project, store, voice).finalise()
    assert result is not None
    assert result.exists()


# ─── Readiness check unit-level ─────────────────────

def test_readiness_check_clean_project_is_ready(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    report = DocumentReadinessCheck(store, voice, project).check()
    assert report.is_ready
    assert report.blocking_flags == []


def test_readiness_check_register_bleed_is_blocking(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    drafts = project / ".lattice" / "drafts" / "academic"
    (drafts / "cluster_c.body.1.md").write_text(
        "I need to clarify something before proceeding. Could you let me know?",
        encoding="utf-8",
    )
    report = DocumentReadinessCheck(store, voice, project).check()
    assert not report.is_ready
    assert any(
        f.rule_id == "readiness.register_bleed" for f in report.blocking_flags
    )


def test_readiness_summary_lists_failing_rules(tmp_path):
    store, voice, project = _mk_full_project(tmp_path)
    body_cluster = store.get_cluster("c.body.1")
    body_cluster.prose_state = ProseState.failed
    store.save_cluster(body_cluster)
    report = DocumentReadinessCheck(store, voice, project).check()
    assert "NOT ready" in report.summary
    assert "cluster_not_rendered" in report.summary
