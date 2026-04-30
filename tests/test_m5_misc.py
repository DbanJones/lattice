"""Tests for Argus exporter, VoiceConsistencyCheck, ResumeManager."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.export_argus import export_to_argus
from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimType, Confidence, Evidence,
    Relationship, RelationshipStrength, RelationshipType, Section, SectionRole,
)
from lattice.utils.resume import ResumeManager, Stage, StageStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Argus exporter ─────────────────────────────────

def test_argus_export_includes_thesis_and_claims(tmp_path: Path) -> None:
    now = _now()
    graph = AuthorGraph(
        project_name="test",
        thesis_statement="A thesis in one sentence.",
        sections=[
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="A", position=1,
                    role=SectionRole.argumentative, claim_ids=["cl.a.1", "cl.a.2"]),
        ],
        claims=[
            Claim(claim_id="cl.thesis", statement="A thesis in one sentence.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.thesis",
                  created_by="t", created_at=now, modified_at=now),
            Claim(claim_id="cl.a.1", statement="An empirical claim.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  evidence=[Evidence(source="koomey_2015", passage="p.1.1",
                                     binding_strength=BindingStrength.strong)],
                  section_id="s.a",
                  created_by="t", created_at=now, modified_at=now),
            Claim(claim_id="cl.a.2", statement="A counter claim.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.a",
                  created_by="t", created_at=now, modified_at=now),
        ],
        relationships=[
            Relationship(rel_id="r.1", type=RelationshipType.contradicts,
                         **{"from": "cl.a.2", "to": "cl.a.1"},
                         strength=RelationshipStrength.direct, note="",
                         created_by="t", created_at=now),
        ],
        created_at=now, modified_at=now,
    )
    out = tmp_path / "export.argus.json"
    export_to_argus(graph, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["thesis"]["statement"] == "A thesis in one sentence."
    assert data["project_name"] == "test"
    # Argument sections exclude s.thesis
    assert [a["id"] for a in data["arguments"]] == ["s.a"]
    # cl.a.1 gets evidence + a reference entry
    assert any(e["claim_id"] == "cl.a.1" and e["source_id"] == "koomey_2015" for e in data["evidences"])
    assert any(r["id"] == "koomey_2015" for r in data["references"])
    # cl.a.2 is tagged as a counter_claim because of the contradicts edge
    a2 = next(c for c in data["claims"] if c["id"] == "cl.a.2")
    assert a2["type"] == "counter_claim"


# ─── ResumeManager ──────────────────────────────────

def test_resume_manager_records_completion(tmp_path: Path) -> None:
    manager = ResumeManager(tmp_path)
    run = manager.start_run(voice="academic")
    assert run.run_id
    manager.update_stage(run.run_id, Stage.ingest, StageStatus.completed)
    manager.update_stage(run.run_id, Stage.index, StageStatus.completed)
    latest = manager.latest_run()
    assert latest is not None
    assert latest.last_completed_stage == Stage.index
    assert latest.stage_status[Stage.ingest] == StageStatus.completed


def test_resume_next_stage_after_ingest(tmp_path: Path) -> None:
    manager = ResumeManager(tmp_path)
    run = manager.start_run()
    manager.update_stage(run.run_id, Stage.ingest, StageStatus.completed)
    latest = manager.latest_run()
    assert latest is not None
    assert manager.next_stage_after(latest) == Stage.index


def test_resume_next_stage_after_last_stage_is_none(tmp_path: Path) -> None:
    manager = ResumeManager(tmp_path)
    run = manager.start_run()
    manager.update_stage(run.run_id, Stage.apply, StageStatus.completed)
    latest = manager.latest_run()
    assert latest is not None
    assert manager.next_stage_after(latest) is None


def test_resume_no_prior_runs(tmp_path: Path) -> None:
    manager = ResumeManager(tmp_path)
    assert manager.latest_run() is None
