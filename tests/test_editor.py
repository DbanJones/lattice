"""Tests for the edit proposer and applier."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.editor.applier import EditApplier
from lattice.editor.proposer import EditProposer
from lattice.graph.models import (
    AuditFlag, Cluster, ClaimRoleInCluster, ClusterRole, Confidence, EditMode,
    EditProposal, EditStatus, EditType, FlagCategory, FlagDecision,
    ProseLocation, ProseState, Severity,
)
from lattice.graph.store import GraphStore
from lattice.utils.config import Config
from lattice.voice.parser import Voice


def _academic_voice() -> Voice:
    path = Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    return Voice.from_file(path)


def _mk_flag(cluster_id: str = "c.x.1") -> AuditFlag:
    now = datetime.now(timezone.utc)
    return AuditFlag(
        flag_id="f.20260424.abc",
        category=FlagCategory.voice,
        rule_id="voice.banned_word.issues",
        severity=Severity.standard,
        default_mode=EditMode.suggest_changes,
        cluster_id=cluster_id,
        section_id="s.x",
        prose_location=ProseLocation(paragraph_index=0, char_start=20, char_end=26),
        offending_text="issues",
        rule_description="Prohibited word: issues",
        suggestion="Options: problems, constraints",
        voice_name="academic",
        created_at=now,
        decision=FlagDecision.accept_suggest_changes,
    )


class _StubLLM:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, model=None, temperature=0.2):
        self.calls.append((system, user))
        return self.payload, None


# ─── EditProposer ───────────────────────────────────

async def test_proposer_produces_proposals_for_accepted_flag(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    voice = _academic_voice()
    config = Config.load(tmp_path)

    # Persist the flag and accept it.
    store.save_audit_flags("academic", [_mk_flag()])
    store.update_flag_decision(_mk_flag().flag_id, "accept_suggest_changes")

    # Write prose for the target cluster.
    drafts = tmp_path / ".lattice" / "drafts" / "academic"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "cluster_c.x.1.md").write_text(
        "The study addresses issues with the framework.",
        encoding="utf-8",
    )

    llm = _StubLLM([
        {
            "type": "replace",
            "original": "issues with the framework",
            "proposed": "limitations of the framework",
            "rationale": "Replace banned word with a specific alternative.",
            "confidence": "high",
        }
    ])

    proposer = EditProposer(config, store, llm, voice)
    grouped = await proposer.propose_for_accepted_flags()
    assert "c.x.1" in grouped
    proposals = store.list_edit_proposals("c.x.1")
    assert len(proposals) == 1
    assert proposals[0].original_text == "issues with the framework"
    assert proposals[0].proposed_text == "limitations of the framework"


async def test_proposer_gracefully_handles_missing_cluster_prose(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    voice = _academic_voice()
    config = Config.load(tmp_path)
    store.save_audit_flags("academic", [_mk_flag()])
    store.update_flag_decision(_mk_flag().flag_id, "accept_suggest_changes")

    # No prose file present.
    llm = _StubLLM([])
    proposer = EditProposer(config, store, llm, voice)
    grouped = await proposer.propose_for_accepted_flags()
    # The cluster is in the grouping dict but the list of proposals is empty.
    assert grouped == {"c.x.1": []}


# ─── EditApplier ────────────────────────────────────

def _make_cluster(store: GraphStore) -> Cluster:
    c = Cluster(
        cluster_id="c.x.1",
        section_id="s.x",
        position=1,
        role=ClusterRole.evidence,
        claim_sequence=[ClaimRoleInCluster(claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence)],
        prose_state=ProseState.generated,
    )
    store.save_cluster(c)
    return c


def _accept_proposal(store: GraphStore, cluster_id: str, original: str, proposed: str) -> EditProposal:
    now = datetime.now(timezone.utc)
    proposal = EditProposal(
        proposal_id="e.20260424.001",
        cluster_id=cluster_id,
        flag_id="f.x",
        type=EditType.replace,
        original_text=original,
        proposed_text=proposed,
        rationale="test",
        rule_id="voice.banned_word.issues",
        confidence=Confidence.high,
        status=EditStatus.pending,
        created_at=now,
    )
    store.save_edit_proposals(cluster_id, [proposal])
    # Accept it via the store's update method.
    store.update_proposal_decision(proposal.proposal_id, "accepted")
    return proposal


def test_applier_replaces_text_and_marks_edited(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    _make_cluster(store)
    drafts = tmp_path / ".lattice" / "drafts" / "academic"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "cluster_c.x.1.md").write_text(
        "The study addresses issues with the framework.",
        encoding="utf-8",
    )
    _accept_proposal(store, "c.x.1", "issues with the framework", "limitations of the framework")

    applier = EditApplier(tmp_path, store, voice_name="academic")
    applied, skipped = applier.apply_all_accepted()
    assert applied == 1
    assert skipped == 0
    prose = (drafts / "cluster_c.x.1.md").read_text(encoding="utf-8")
    assert "limitations of the framework" in prose
    assert "issues with the framework" not in prose
    assert store.get_cluster("c.x.1").prose_state == ProseState.edited


def test_applier_skips_when_original_no_longer_matches(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    _make_cluster(store)
    drafts = tmp_path / ".lattice" / "drafts" / "academic"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "cluster_c.x.1.md").write_text("completely different prose", encoding="utf-8")
    _accept_proposal(store, "c.x.1", "text that does not exist", "replacement")

    applier = EditApplier(tmp_path, store, voice_name="academic")
    applied, skipped = applier.apply_all_accepted()
    assert applied == 0
    assert skipped == 1
    # Prose unchanged
    assert (drafts / "cluster_c.x.1.md").read_text(encoding="utf-8") == "completely different prose"
