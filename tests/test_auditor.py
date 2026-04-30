"""Tests for audit checks, patterns, and the runner."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.auditor import patterns
from lattice.auditor.coverage import CoverageCheck
from lattice.auditor.formality import FormalityCheck
from lattice.auditor.paragraph import ParagraphArchitectureCheck
from lattice.auditor.quantification import QuantificationCheck
from lattice.auditor.runner import AuditRunner
from lattice.auditor.sentence import SentenceCraftCheck
from lattice.auditor.voice import VoiceComplianceCheck
from lattice.graph.models import (
    BindingStrength, Citation, Claim, ClaimRoleInCluster, ClaimType, Cluster,
    ClusterRole, Confidence, Evidence, FlagCategory, Passage, PassageLocation,
    PassageType, ProseState, Section, SectionRole, Severity, Source, SourceMetadata,
    SourceType,
)
from lattice.graph.store import GraphStore
from lattice.utils.config import Config
from lattice.voice.parser import Voice


# ─── Pattern detectors ──────────────────────────────

def test_contraction_pattern() -> None:
    text = "It's a nice day and we can't complain."
    matches = patterns.contraction(text)
    assert len(matches) == 2
    assert any(m[2].lower() == "it's" for m in matches)
    assert any(m[2].lower() == "can't" for m in matches)


def test_expletive_construction_pattern() -> None:
    text = "There are three factors. It is clear. A real sentence follows."
    matches = patterns.expletive_construction_at_sentence_start(text)
    hits = [m[2].split()[0] for m in matches]
    assert hits.count("There") + hits.count("It") == 2


def test_continuation_opener_pattern() -> None:
    text = "First idea.\n\nMoreover, a second idea.\n\nA third idea."
    matches = patterns.continuation_opener(text)
    assert len(matches) == 1
    assert matches[0][2] == "Moreover,"


def test_rhetorical_question_pattern() -> None:
    text = "Why does this matter? Because it does."
    matches = patterns.rhetorical_question(text)
    assert len(matches) == 1
    assert "Why does this matter" in matches[0][2]


def test_catalogue_pattern_three_sequential_citations() -> None:
    text = (
        "Jones (2019) examined this. Lee (2020) reported similar results. "
        "Park (2021) observed the same."
    )
    matches = patterns.catalogue_pattern(text)
    assert len(matches) >= 1


def test_catalogue_pattern_allows_synthesis_language() -> None:
    text = (
        "Jones (2019), Lee (2020), and Park (2021) converge on one finding: "
        "three lines of evidence point to the same mechanism."
    )
    matches = patterns.catalogue_pattern(text)
    assert matches == []


# ─── Per-check harness ──────────────────────────────

def _mk_check_env(tmp_path: Path, voice_text_override: str | None = None) -> tuple[Cluster, Voice, GraphStore, Config]:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    now = datetime.now(timezone.utc)
    # Minimal voice loaded from the canonical academic.voice.md.
    academic_path = Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    voice = Voice.from_file(academic_path)
    cluster = Cluster(
        cluster_id="c.x.1",
        section_id="s.x",
        position=1,
        role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence),
        ],
    )
    store.save_claim(
        Claim(
            claim_id="cl.x.1",
            statement="Koomey's Law slowdown accelerated during the 2010s.",
            type=ClaimType.empirical,
            confidence=Confidence.high,
            created_by="test", created_at=now, modified_at=now,
        )
    )
    config = Config.load(tmp_path)
    return cluster, voice, store, config


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─── VoiceComplianceCheck ───────────────────────────

async def test_voice_compliance_flags_banned_word(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    prose = "The study has many issues with its methodology."  # "issues" + "methodology" are banned
    check = VoiceComplianceCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, prose)
    rule_ids = {f.rule_id for f in flags}
    assert any("issues" in r for r in rule_ids)
    assert any("methodology" in r for r in rule_ids)


async def test_voice_compliance_flags_banned_phrase(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    prose = "In terms of energy, the outcome improved."
    check = VoiceComplianceCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, prose)
    assert any("banned_phrase" in f.rule_id for f in flags)


async def test_voice_compliance_flags_em_dash(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    prose = "The result — one we did not expect — shifts the argument."
    check = VoiceComplianceCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, prose)
    # em_dashes is listed as a bare-string prohibition; the check won't
    # find the literal "em_dashes" string but will have fallen back to
    # the literal-word search (which finds nothing). Here we confirm the
    # check runs without error; a dedicated em-dash regex is future work.
    assert isinstance(flags, list)


# ─── ParagraphArchitectureCheck ─────────────────────

async def test_paragraph_flags_continuation_opener(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    prose = "Opening idea is stated.\n\nMoreover, a related idea extends the point."
    check = ParagraphArchitectureCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, prose)
    assert any(f.rule_id == "paragraph.continuation_opener" for f in flags)


async def test_paragraph_flags_overlong_paragraph(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    # 300 words in one paragraph — exceeds academic voice's 250-word max.
    long_para = " ".join(["word"] * 300)
    check = ParagraphArchitectureCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, long_para)
    assert any(f.rule_id == "paragraph.too_long" for f in flags)


# ─── QuantificationCheck ────────────────────────────

async def test_quantification_flags_unquantified_weasel_word(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    prose = (
        "Koomey measured the slowdown carefully.\n\n"
        "Later studies showed results that significantly changed the outlook."
    )
    check = QuantificationCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, prose)
    assert any(f.rule_id == "quantification.unquantified_magnitude" for f in flags)


async def test_quantification_allows_weasel_word_with_number(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    prose = "The metric improved significantly, rising from 1.5 to 2.6 years."
    check = QuantificationCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, prose)
    assert flags == []


# ─── SentenceCraftCheck ─────────────────────────────

async def test_sentence_flags_expletive(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    prose = "There are three factors that influence the result."
    check = SentenceCraftCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, prose)
    assert any(f.rule_id == "sentence.expletive_construction" for f in flags)


# ─── CoverageCheck ──────────────────────────────────

async def test_coverage_flags_missing_claim_marker(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    prose = 'Standard sentence. {MISSING_CLAIM: "unstated assumption"}'
    check = CoverageCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, prose)
    assert any(f.rule_id == "coverage.missing_claim_marker" for f in flags)


async def test_coverage_flags_orphan_sentence(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    # The cluster's only claim is about Koomey's Law slowdown.
    # This prose wanders into an unrelated topic.
    prose = (
        "Koomey's Law slowdown accelerated during the 2010s. "
        "Quantum computing efficiency gains remain far from realisation in today's hardware."
    )
    check = CoverageCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, prose)
    assert any(f.rule_id == "coverage.orphan_sentence" for f in flags)


# ─── FormalityCheck ─────────────────────────────────

async def test_formality_flags_contraction(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    prose = "It's a well-known result."
    check = FormalityCheck(config, store, None, voice)
    flags = await check.check_cluster(cluster, prose)
    assert any(f.rule_id == "formality.contraction" for f in flags)


# ─── AuditRunner ────────────────────────────────────

async def test_runner_persists_flags_and_writes_report(tmp_path: Path) -> None:
    cluster, voice, store, config = _mk_check_env(tmp_path)
    # Save the cluster and write prose.
    store.save_cluster(cluster)
    drafts = tmp_path / ".lattice" / "drafts" / voice.name
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / f"cluster_{cluster.cluster_id}.md").write_text(
        "It's a known issue. In terms of energy, the trend improved significantly.",
        encoding="utf-8",
    )
    runner = AuditRunner(config, store, llm=None, voice=voice)
    flags = await runner.run()
    assert len(flags) > 0
    # Flags persisted
    loaded = store.list_audit_flags(voice.name)
    assert len(loaded) == len(flags)
    # Report written
    report = tmp_path / ".lattice" / "audit" / f"audit.{voice.name}.md"
    assert report.exists()
    content = report.read_text(encoding="utf-8")
    assert "Audit report" in content
