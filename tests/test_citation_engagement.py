"""Tests for the LLM-bound citation engagement check."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.auditor.citation import CitationCheck, _CITATION_RE
from lattice.graph.models import (
    ClaimRoleInCluster, Cluster, ClusterRole, FlagCategory, Severity,
)
from lattice.graph.store import GraphStore
from lattice.utils.config import Config
from lattice.voice.parser import Voice


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _academic_voice() -> Voice:
    return Voice.from_file(
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )


def _bare_store(tmp_path: Path) -> GraphStore:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    return GraphStore.load(tmp_path)


def _bare_cluster() -> Cluster:
    return Cluster(
        cluster_id="c.x.1", section_id="s.x", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence),
        ],
    )


class _StubLLM:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, model=None, temperature=0.2):
        self.calls.append((system, user))
        return self.payload, None


# ─── Citation regex coverage ────────────────────────

def test_citation_regex_matches_inline_form() -> None:
    matches = list(_CITATION_RE.finditer("Smith (2022) demonstrates the effect."))
    assert len(matches) == 1
    assert matches[0].group("authors") == "Smith"
    assert matches[0].group("year") == "2022"


def test_citation_regex_matches_parenthetical_form() -> None:
    matches = list(_CITATION_RE.finditer("The result holds (Smith, 2022)."))
    assert len(matches) == 1


def test_citation_regex_matches_et_al() -> None:
    matches = list(_CITATION_RE.finditer("Esmaeilzadeh et al. (2011) identify the breakdown."))
    assert len(matches) == 1
    assert "Esmaeilzadeh" in matches[0].group("authors")


# ─── Check returns no flags when prose is clean ─────

async def test_check_returns_no_flags_when_all_pass(tmp_path):
    store = _bare_store(tmp_path)
    config = Config.load(tmp_path)
    voice = _academic_voice()
    llm = _StubLLM([
        {
            "citation_text": "Smith (2022)",
            "passes": ["names_author", "states_claim", "explains_relevance"],
            "fails": [],
            "severity": "minor",
        }
    ])
    check = CitationCheck(config, store, llm, voice)
    prose = (
        "Smith (2022) demonstrates that the slowdown accelerated. "
        "This matters here because it sets the lower bound on plausible "
        "efficiency gains over the next decade."
    )
    flags = await check.check_cluster(_bare_cluster(), prose)
    assert flags == []
    assert len(llm.calls) == 1


# ─── Failure cases ──────────────────────────────────

async def test_parenthetical_only_citation_flagged(tmp_path):
    store = _bare_store(tmp_path)
    config = Config.load(tmp_path)
    voice = _academic_voice()
    llm = _StubLLM([
        {
            "citation_text": "(Smith, 2022)",
            "passes": [],
            "fails": ["names_author", "states_claim", "explains_relevance"],
            "severity": "critical",
        }
    ])
    check = CitationCheck(config, store, llm, voice)
    prose = "The slowdown is documented (Smith, 2022)."
    flags = await check.check_cluster(_bare_cluster(), prose)
    assert len(flags) == 1
    flag = flags[0]
    assert flag.category == FlagCategory.citation
    assert flag.severity == Severity.critical
    assert "names_author" in flag.rule_id
    assert "Lead with the author" in flag.suggestion


async def test_partial_failure_marks_as_standard(tmp_path):
    store = _bare_store(tmp_path)
    config = Config.load(tmp_path)
    voice = _academic_voice()
    llm = _StubLLM([
        {
            "citation_text": "Smith (2022)",
            "passes": ["names_author"],
            "fails": ["states_claim", "explains_relevance"],
            "severity": "standard",
        }
    ])
    check = CitationCheck(config, store, llm, voice)
    prose = "Smith (2022) examined this issue."
    flags = await check.check_cluster(_bare_cluster(), prose)
    assert len(flags) == 1
    assert flags[0].severity == Severity.standard
    assert "states_claim" in flags[0].rule_id
    assert "explains_relevance" in flags[0].rule_id


# ─── No-citation and no-LLM paths ───────────────────

async def test_no_llm_returns_empty(tmp_path):
    store = _bare_store(tmp_path)
    config = Config.load(tmp_path)
    voice = _academic_voice()
    check = CitationCheck(config, store, llm=None, voice=voice)
    flags = await check.check_cluster(_bare_cluster(), "Some prose with Smith (2022).")
    assert flags == []


async def test_no_citations_skips_llm(tmp_path):
    store = _bare_store(tmp_path)
    config = Config.load(tmp_path)
    voice = _academic_voice()
    llm = _StubLLM([])
    check = CitationCheck(config, store, llm, voice)
    flags = await check.check_cluster(_bare_cluster(), "Some prose with no citations.")
    assert flags == []
    assert len(llm.calls) == 0


async def test_llm_exception_is_swallowed(tmp_path):
    """An LLM failure shouldn't break the audit run — citation check returns []."""
    store = _bare_store(tmp_path)
    config = Config.load(tmp_path)
    voice = _academic_voice()

    class _BadLLM:
        async def complete_json(self, system, user, model=None, temperature=0.2):
            raise RuntimeError("model unavailable")

    check = CitationCheck(config, store, _BadLLM(), voice)
    flags = await check.check_cluster(_bare_cluster(), "Smith (2022) shows X.")
    assert flags == []


# ─── Multiple citations ─────────────────────────────

async def test_handles_multiple_citations(tmp_path):
    store = _bare_store(tmp_path)
    config = Config.load(tmp_path)
    voice = _academic_voice()
    llm = _StubLLM([
        {
            "citation_text": "Jones (2019)",
            "passes": ["names_author", "states_claim", "explains_relevance"],
            "fails": [],
            "severity": "minor",
        },
        {
            "citation_text": "Lee (2020)",
            "passes": ["names_author"],
            "fails": ["explains_relevance"],
            "severity": "minor",
        },
    ])
    check = CitationCheck(config, store, llm, voice)
    prose = (
        "Jones (2019) demonstrates that the trend slows; this matters "
        "because it bounds the forecast. Lee (2020) reports a similar "
        "magnitude."
    )
    flags = await check.check_cluster(_bare_cluster(), prose)
    assert len(flags) == 1  # Only Lee fails
    assert "explains_relevance" in flags[0].rule_id
