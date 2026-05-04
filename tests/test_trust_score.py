"""Phase 3C: per-section trust score."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from lattice.auditor.trust_score import (
    SectionTrustScore,
    TrustReport,
    cluster_to_section_map,
    compute_trust,
    load_audit_flags,
    load_readiness_blocks,
    load_voice_review_section_failures,
)
from lattice.graph.metrics import (
    ArgumentMetrics, ArgumentBreadth, ArgumentStrength, SectionMetrics,
)
from lattice.graph.models import (
    AuditFlag, AuthorGraph, BindingStrength, Citation, Claim,
    ClaimType, Confidence, Cluster, ClusterRole, ClaimRoleInCluster,
    EditMode, Evidence, FlagCategory, ProseLocation, Relationship,
    RelationshipStrength, RelationshipType, Section, SectionRole, Severity,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _section_metric(
    section_id: str, *,
    score: float = 0.5,
    title: str = "Section",
    claim_count: int = 4,
) -> SectionMetrics:
    return SectionMetrics(
        section_id=section_id, section_title=title,
        claim_count=claim_count, score=score,
    )


def _flag(
    cluster_id: str, *,
    section_id: str = "",
    rule_id: str = "x",
    category: FlagCategory = FlagCategory.voice,
) -> AuditFlag:
    return AuditFlag(
        flag_id=f"f.{cluster_id}.{rule_id}",
        category=category,
        rule_id=rule_id,
        severity=Severity.standard,
        default_mode=EditMode.suggest_changes,
        cluster_id=cluster_id,
        section_id=section_id,
        prose_location=ProseLocation(paragraph_index=0, char_start=0, char_end=0),
        offending_text="",
        rule_description="",
        suggestion="",
        voice_name="academic",
        created_at=_now(),
    )


def _build_metrics(*sections: SectionMetrics) -> ArgumentMetrics:
    return ArgumentMetrics(
        strength=ArgumentStrength(),
        breadth=ArgumentBreadth(),
        per_section={s.section_id: s for s in sections},
    )


# ─── empty / edge cases ──────────────────────────


def test_no_sections_returns_zero_document_score() -> None:
    metrics = _build_metrics()
    report = compute_trust(_empty_graph(), metrics)
    assert report.document_score == 0.0
    assert report.sections == []


def test_no_inputs_uses_pure_metric_component() -> None:
    """With no audit / readiness / voice-review signals, the trust
    score equals weighted metric (0.50) + 1.0 audit + 1.0 readiness +
    1.0 voice, ignoring zero claim_count edge cases."""
    sm = _section_metric("s.a", score=0.6, claim_count=4)
    metrics = _build_metrics(sm)
    report = compute_trust(_empty_graph(), metrics)
    s = report.sections[0]
    # 0.50 * 0.6 + 0.20 * 1.0 + 0.20 * 1.0 + 0.10 * 1.0 = 0.80
    assert s.score == 0.80
    assert s.audit_flag_count == 0
    assert s.readiness_blocks == 0


# ─── audit component ────────────────────────────


def test_high_audit_flag_density_lowers_trust() -> None:
    """A section with 1 flag per claim drops audit_component to 0."""
    sm = _section_metric("s.a", score=0.8, claim_count=2)
    metrics = _build_metrics(sm)
    flags = [
        _flag("c.a.1", section_id="s.a"),
        _flag("c.a.1", section_id="s.a", rule_id="y"),
    ]
    report = compute_trust(_empty_graph(), metrics, audit_flags=flags)
    s = report.sections[0]
    assert s.audit_flag_count == 2
    # density = 2/2 = 1.0; audit_component = 1 - 1 = 0
    assert s.audit_component == 0.0


def test_audit_flags_routed_via_cluster_to_section_map() -> None:
    """A flag whose section_id is empty but whose cluster_id is set
    should still get attributed to the right section."""
    sm = _section_metric("s.a", score=0.5, claim_count=2)
    metrics = _build_metrics(sm)
    flags = [_flag("c.a.1", section_id="")]
    cluster_to_section = {"c.a.1": "s.a"}
    report = compute_trust(
        _empty_graph(), metrics,
        audit_flags=flags,
        cluster_to_section=cluster_to_section,
    )
    assert report.sections[0].audit_flag_count == 1


def test_zero_claims_gives_audit_component_one() -> None:
    sm = _section_metric("s.empty", score=0.5, claim_count=0)
    metrics = _build_metrics(sm)
    report = compute_trust(_empty_graph(), metrics)
    s = report.sections[0]
    assert s.audit_component == 1.0


# ─── readiness component ────────────────────────


def test_readiness_block_drops_component_to_zero() -> None:
    sm = _section_metric("s.a", score=0.8, claim_count=4)
    metrics = _build_metrics(sm)
    report = compute_trust(
        _empty_graph(), metrics,
        readiness_blocked_clusters={"c.a.1"},
        cluster_to_section={"c.a.1": "s.a"},
    )
    s = report.sections[0]
    assert s.readiness_blocks == 1
    assert s.readiness_component == 0.0
    # And the score reflects that component drop.
    assert s.score < 0.80


def test_no_readiness_blocks_keeps_component_one() -> None:
    sm = _section_metric("s.a", score=0.8, claim_count=4)
    metrics = _build_metrics(sm)
    report = compute_trust(_empty_graph(), metrics)
    assert report.sections[0].readiness_component == 1.0


# ─── voice review component ─────────────────────


def test_voice_review_failure_dings_trust() -> None:
    sm = _section_metric("s.a", score=0.8, claim_count=4)
    metrics = _build_metrics(sm)
    report = compute_trust(
        _empty_graph(), metrics,
        voice_review_section_failures={"s.a"},
    )
    s = report.sections[0]
    assert s.voice_review_component == 0.5
    # Score should be lower than the no-failure case.
    no_fail = compute_trust(_empty_graph(), metrics)
    assert s.score < no_fail.sections[0].score


# ─── document score weighting ───────────────────


def test_document_score_weighted_by_claim_count() -> None:
    """Big sections dominate the document score.

    With no audit / readiness / voice signals, a section's trust is
    0.5*metric + 0.5 (the other three components default to 1.0).
    big section (metric=0.4, 20 claims) → trust 0.70.
    small section (metric=0.9, 2 claims) → trust 0.95.
    Doc score = (0.70*20 + 0.95*2) / 22 = 0.723.
    Confirms the big section's trust dominates.
    """
    big = _section_metric("s.a", score=0.4, claim_count=20)
    small = _section_metric("s.b", score=0.9, claim_count=2)
    metrics = _build_metrics(big, small)
    report = compute_trust(_empty_graph(), metrics)
    # Closer to big section's trust (0.70) than to small's (0.95).
    assert abs(report.document_score - 0.70) < abs(report.document_score - 0.95)
    # And weighted average is in the right ballpark.
    assert 0.71 < report.document_score < 0.74


def test_untrustworthy_sections_listed() -> None:
    a = _section_metric("s.a", score=0.2, claim_count=4)
    b = _section_metric("s.b", score=0.8, claim_count=4)
    metrics = _build_metrics(a, b)
    report = compute_trust(_empty_graph(), metrics)
    # s.a has metric_component 0.2 + audit 1.0 + readiness 1.0 + voice 1.0
    # = 0.50*0.2 + 0.20 + 0.20 + 0.10 = 0.60. Above 0.5 — not untrustworthy.
    # Make s.a worse explicitly by adding flags.
    flags = [_flag("c.a.1", section_id="s.a") for _ in range(8)]
    report = compute_trust(_empty_graph(), metrics, audit_flags=flags)
    # 8 flags / 4 claims = 2; audit_component = max(0, 1-2) = 0.
    # score = 0.50*0.2 + 0 + 0.20 + 0.10 = 0.40 → in untrustworthy.
    assert "s.a" in report.untrustworthy_sections
    assert "s.b" not in report.untrustworthy_sections


# ─── notes / observations ───────────────────────


def test_notes_call_out_readiness_blocks() -> None:
    sm = _section_metric("s.a", score=0.5, claim_count=4)
    metrics = _build_metrics(sm)
    report = compute_trust(
        _empty_graph(), metrics,
        readiness_blocked_clusters={"c.a.1"},
        cluster_to_section={"c.a.1": "s.a"},
    )
    s = report.sections[0]
    assert any("blocked" in n.lower() for n in s.notes)


def test_notes_call_out_high_flag_density() -> None:
    sm = _section_metric("s.a", score=0.5, claim_count=4)
    metrics = _build_metrics(sm)
    flags = [_flag("c.a.1", section_id="s.a") for _ in range(5)]
    report = compute_trust(_empty_graph(), metrics, audit_flags=flags)
    s = report.sections[0]
    assert any("audit flag" in n.lower() for n in s.notes)


def test_notes_call_out_low_metric() -> None:
    sm = _section_metric("s.a", score=0.2, claim_count=4)
    metrics = _build_metrics(sm)
    report = compute_trust(_empty_graph(), metrics)
    s = report.sections[0]
    assert any("rescaffold" in n.lower() for n in s.notes)


# ─── disk-reading helpers ───────────────────────


def test_load_audit_flags_returns_empty_when_missing(tmp_path: Path) -> None:
    flags = load_audit_flags(tmp_path, "academic")
    assert flags == []


def test_load_audit_flags_reads_voiced_file(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".lattice" / "audit"
    audit_dir.mkdir(parents=True)
    flag = _flag("c.a.1", section_id="s.a")
    (audit_dir / "audit_flags.academic.json").write_text(
        json.dumps([json.loads(flag.model_dump_json())]),
        encoding="utf-8",
    )
    loaded = load_audit_flags(tmp_path, "academic")
    assert len(loaded) == 1
    assert loaded[0].section_id == "s.a"


def test_load_audit_flags_handles_corrupt_json(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".lattice" / "audit"
    audit_dir.mkdir(parents=True)
    (audit_dir / "audit_flags.academic.json").write_text(
        "not valid json", encoding="utf-8",
    )
    assert load_audit_flags(tmp_path, "academic") == []


def test_load_readiness_blocks(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".lattice" / "audit"
    audit_dir.mkdir(parents=True)
    (audit_dir / "readiness.academic.json").write_text(
        json.dumps({"blocking_clusters": ["c.a.1", "c.b.2"]}),
        encoding="utf-8",
    )
    blocks = load_readiness_blocks(tmp_path, "academic")
    assert blocks == {"c.a.1", "c.b.2"}


def test_load_voice_review_failures(tmp_path: Path) -> None:
    lattice_dir = tmp_path / ".lattice"
    lattice_dir.mkdir(parents=True)
    (lattice_dir / "voice_review.academic.json").write_text(
        json.dumps({
            "findings": [
                {"section_id": "s.a", "compliance": "fail"},
                {"section_id": "s.b", "compliance": "pass"},
                {"section_id": "s.c", "compliance": "fail"},
            ],
        }),
        encoding="utf-8",
    )
    failed = load_voice_review_section_failures(tmp_path, "academic")
    assert failed == {"s.a", "s.c"}


def test_cluster_to_section_map_built_from_clusters() -> None:
    clusters = [
        Cluster(
            cluster_id="c.a.1", section_id="s.a", position=1,
            role=ClusterRole.evidence,
            claim_sequence=[ClaimRoleInCluster(claim_id="cl.a.1",
                                               role_in_cluster=ClusterRole.evidence)],
        ),
        Cluster(
            cluster_id="c.b.1", section_id="s.b", position=1,
            role=ClusterRole.evidence,
            claim_sequence=[ClaimRoleInCluster(claim_id="cl.b.1",
                                               role_in_cluster=ClusterRole.evidence)],
        ),
    ]
    m = cluster_to_section_map(_empty_graph(), clusters)
    assert m == {"c.a.1": "s.a", "c.b.1": "s.b"}


# ─── helpers ────────────────────────────────────


def _empty_graph() -> AuthorGraph:
    now = _now()
    return AuthorGraph(
        project_name="t", thesis_statement="T.",
        sections=[], claims=[], relationships=[],
        created_at=now, modified_at=now,
    )
