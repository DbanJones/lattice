"""Tests for the autofix pipeline + autocorrect config."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.auditor.autofix import AutofixResult, run_autofix
from lattice.graph.models import (
    AuditFlag,
    AuthorGraph,
    Claim,
    ClaimRoleInCluster,
    ClaimType,
    Cluster,
    ClusterRole,
    Confidence,
    EditMode,
    FlagCategory,
    ProseLocation,
    ProseState,
    Section,
    SectionRole,
    Severity,
)
from lattice.graph.store import GraphStore
from lattice.utils.config import Config
from lattice.voice.parser import Voice


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _voice() -> Voice:
    return Voice.from_file(
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )


def _config(tmp_path: Path, autocorrect: str = "safe") -> Config:
    (tmp_path / "config.yml").write_text(
        f"autocorrect: {autocorrect}\n", encoding="utf-8"
    )
    return Config.load(tmp_path)


def _seed_project(tmp_path: Path) -> tuple[GraphStore, str]:
    """Returns (store, cluster_id). Creates a small graph with one cluster
    + one rendered prose file in drafts/."""
    store = GraphStore.load(tmp_path)
    now = _now()
    claim = Claim(
        claim_id="cl.x.1", statement="Some claim.",
        type=ClaimType.empirical, confidence=Confidence.medium,
        section_id="s.x",
        created_by="t", created_at=now, modified_at=now,
    )
    section = Section(
        section_id="s.x", title="X", position=1,
        role=SectionRole.argumentative,
        claim_ids=["cl.x.1"],
    )
    cluster = Cluster(
        cluster_id="c.x.1",
        section_id="s.x",
        position=1,
        role=ClusterRole.evidence,
        claim_sequence=[ClaimRoleInCluster(
            claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence,
        )],
        prose_state=ProseState.generated,
    )
    graph = AuthorGraph(
        project_name="t",
        sections=[section], claims=[claim], relationships=[],
        created_at=now, modified_at=now,
    )
    store.save_graph(graph)
    store.save_cluster(cluster)

    # Write a prose file so the EditApplier has something to apply against.
    drafts_dir = tmp_path / ".lattice" / "drafts" / "academic"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    (drafts_dir / "cluster_c.x.1.md").write_text(
        "Several studies have examined this. The mechanism is straightforward.\n",
        encoding="utf-8",
    )

    return store, cluster.cluster_id


def _make_flag(
    cluster_id: str,
    rule_id: str,
    default_mode: EditMode,
    category: FlagCategory = FlagCategory.quantification,
    offending: str = "Several",
) -> AuditFlag:
    return AuditFlag(
        flag_id=f"f.{rule_id}.{offending[:6]}",
        category=category,
        rule_id=rule_id,
        severity=Severity.critical,
        default_mode=default_mode,
        cluster_id=cluster_id,
        section_id="s.x",
        prose_location=ProseLocation(paragraph_index=0, char_start=0, char_end=8),
        offending_text=offending,
        rule_description="x",
        suggestion="x",
        voice_name="academic",
        created_at=_now(),
    )


# ─── Config validation ──────────────────────────────


def test_config_accepts_valid_autocorrect_levels(tmp_path: Path) -> None:
    for level in ("none", "safe", "aggressive"):
        (tmp_path / "config.yml").write_text(
            f"autocorrect: {level}\n", encoding="utf-8"
        )
        cfg = Config.load(tmp_path)
        assert cfg.autocorrect == level


def test_config_default_is_safe(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    cfg = Config.load(tmp_path)
    assert cfg.autocorrect == "safe"


def test_config_rejects_invalid_autocorrect(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text(
        "autocorrect: extreme\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="autocorrect"):
        Config.load(tmp_path)


def test_config_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.yml").write_text(
        "autocorrect: none\n", encoding="utf-8"
    )
    monkeypatch.setenv("LATTICE_AUTOCORRECT", "aggressive")
    cfg = Config.load(tmp_path)
    assert cfg.autocorrect == "aggressive"


# ─── Autofix at each level ──────────────────────────


def test_autofix_none_refuses(tmp_path: Path) -> None:
    config = _config(tmp_path, autocorrect="none")
    store, cluster_id = _seed_project(tmp_path)
    store.save_audit_flags("academic", [
        _make_flag(cluster_id, "quantification.unquantified_magnitude",
                   EditMode.suggest_changes),
    ])
    result = run_autofix(config, store, _voice(), llm=None)
    assert result.accepted_suggest_changes == 0
    assert result.accepted_rewrite == 0
    assert any("autocorrect=none" in note for note in result.notes)


def test_autofix_safe_accepts_suggest_changes_only(tmp_path: Path) -> None:
    config = _config(tmp_path, autocorrect="safe")
    store, cluster_id = _seed_project(tmp_path)
    store.save_audit_flags("academic", [
        _make_flag(cluster_id, "quantification.unquantified_magnitude",
                   EditMode.suggest_changes, offending="Several"),
        _make_flag(cluster_id, "voice.boilerplate.x",
                   EditMode.rewrite, offending="The mechanism is"),
    ])
    result = run_autofix(config, store, _voice(), llm=None)
    # Suggest_changes accepted; rewrite NOT accepted at safe level.
    assert result.accepted_suggest_changes == 1
    assert result.accepted_rewrite == 0
    # Without an LLM the proposer can't run, but the flags should still
    # have been accepted.
    flags = store.list_audit_flags("academic")
    assert any(f.decision == "accept_suggest_changes" for f in flags)
    assert all(f.decision != "accept_rewrite" for f in flags)


def test_autofix_aggressive_accepts_both_modes(tmp_path: Path) -> None:
    config = _config(tmp_path, autocorrect="aggressive")
    store, cluster_id = _seed_project(tmp_path)
    store.save_audit_flags("academic", [
        _make_flag(cluster_id, "quantification.unquantified_magnitude",
                   EditMode.suggest_changes, offending="Several"),
        _make_flag(cluster_id, "voice.boilerplate.x",
                   EditMode.rewrite, offending="The mechanism is"),
    ])
    result = run_autofix(config, store, _voice(), llm=None)
    assert result.accepted_suggest_changes == 1
    assert result.accepted_rewrite == 1
    # Cluster should be marked dirty so the next render re-generates it.
    cluster = store.get_cluster(cluster_id)
    assert cluster.prose_state == ProseState.dirty


def test_autofix_aggressive_deletes_orphan_sentences(tmp_path: Path) -> None:
    config = _config(tmp_path, autocorrect="aggressive")
    store, cluster_id = _seed_project(tmp_path)
    # Replace prose with one orphan + one anchored sentence.
    drafts = tmp_path / ".lattice" / "drafts" / "academic"
    (drafts / f"cluster_{cluster_id}.md").write_text(
        "This orphan sentence has no claim. The valid sentence stays.\n",
        encoding="utf-8",
    )
    store.save_audit_flags("academic", [
        _make_flag(
            cluster_id,
            "coverage.orphan_sentence",
            EditMode.rewrite,
            category=FlagCategory.coverage,
            offending="This orphan sentence has no claim",
        ),
    ])
    result = run_autofix(config, store, _voice(), llm=None)
    assert result.orphan_sentences_deleted == 1
    surviving = (drafts / f"cluster_{cluster_id}.md").read_text(encoding="utf-8")
    assert "orphan" not in surviving
    assert "valid sentence stays" in surviving


def test_autofix_safe_does_not_delete_orphan_sentences(tmp_path: Path) -> None:
    """Safe level preserves content — never deletes."""
    config = _config(tmp_path, autocorrect="safe")
    store, cluster_id = _seed_project(tmp_path)
    drafts = tmp_path / ".lattice" / "drafts" / "academic"
    (drafts / f"cluster_{cluster_id}.md").write_text(
        "This orphan sentence has no claim. The valid sentence stays.\n",
        encoding="utf-8",
    )
    store.save_audit_flags("academic", [
        _make_flag(
            cluster_id,
            "coverage.orphan_sentence",
            EditMode.rewrite,
            category=FlagCategory.coverage,
            offending="This orphan sentence has no claim",
        ),
    ])
    result = run_autofix(config, store, _voice(), llm=None)
    assert result.orphan_sentences_deleted == 0
    # Safe level doesn't accept rewrite flags either.
    assert result.accepted_rewrite == 0


def test_autofix_no_pending_flags(tmp_path: Path) -> None:
    config = _config(tmp_path, autocorrect="aggressive")
    store, _ = _seed_project(tmp_path)
    store.save_audit_flags("academic", [])
    result = run_autofix(config, store, _voice(), llm=None)
    assert result.total_changes == 0
    assert any("No pending flags" in n for n in result.notes)


def test_summary_line_is_human_readable() -> None:
    r = AutofixResult(
        accepted_suggest_changes=14,
        proposals_generated=14,
        proposals_accepted=14,
        edits_applied=14,
    )
    line = r.summary_line()
    assert "14 suggest_changes" in line
    assert "14 edit(s) applied" in line


def test_total_changes_excludes_proposal_counts() -> None:
    """total_changes counts actual mutations, not intermediate steps."""
    r = AutofixResult(
        accepted_suggest_changes=10,
        proposals_generated=10,
        proposals_accepted=10,
        edits_applied=8,
        edits_skipped=2,
    )
    # Only 8 edits actually applied; total_changes should reflect that.
    assert r.total_changes == 8


# ─── progress callback wiring ───────────────────────


class _RecordingProgress:
    """Captures every progress callback invocation for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def begin(self, phase, total=None, status=""):
        self.calls.append(("begin", phase, total, status))

    def advance(self, phase, n=1, status=""):
        self.calls.append(("advance", phase, n, status))

    def update_status(self, phase, status):
        self.calls.append(("status", phase, status))

    def end(self, phase, status="complete"):
        self.calls.append(("end", phase, status))

    def begin_pass(self, pass_index, total_passes):
        self.calls.append(("begin_pass", pass_index, total_passes))


def test_autofix_emits_progress_callbacks(tmp_path: Path) -> None:
    """Autofix should call begin/advance/end on the autofix phase so the
    operator sees the live progression even when no LLM is configured."""
    config = _config(tmp_path, autocorrect="safe")
    store, cluster_id = _seed_project(tmp_path)
    store.save_audit_flags("academic", [
        _make_flag(cluster_id, "quantification.unquantified_magnitude",
                   EditMode.suggest_changes),
    ])
    progress = _RecordingProgress()
    run_autofix(config, store, _voice(), llm=None, progress=progress)
    phases = [c for c in progress.calls if c[0] in ("begin", "end")]
    assert any(c == ("begin", "autofix", 4, "1 pending flags") or
               (c[0] == "begin" and c[1] == "autofix") for c in phases), \
        f"expected an autofix begin call in {phases}"
    assert any(c[0] == "end" and c[1] == "autofix" for c in phases), \
        f"expected an autofix end call in {phases}"


def test_autofix_no_progress_callback_works(tmp_path: Path) -> None:
    """Passing progress=None should be silent and equivalent to safe mode."""
    config = _config(tmp_path, autocorrect="safe")
    store, cluster_id = _seed_project(tmp_path)
    store.save_audit_flags("academic", [
        _make_flag(cluster_id, "quantification.unquantified_magnitude",
                   EditMode.suggest_changes),
    ])
    # No progress argument supplied at all.
    result = run_autofix(config, store, _voice(), llm=None)
    assert result.accepted_suggest_changes == 1
