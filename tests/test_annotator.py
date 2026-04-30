"""Tests for the ContextualAnnotator."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    AuthorGraph, Claim, ClaimType, Confidence, Section, SectionRole,
)
from lattice.ingester.annotator import ContextualAnnotator, _CITATION_RE, _match_to_source
from lattice.utils.config import Config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_config(tmp_path: Path) -> Config:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    return Config.load(tmp_path)


def _make_claim(cid: str, statement: str, section_id: str = "s.a") -> Claim:
    now = _now()
    return Claim(
        claim_id=cid,
        statement=statement,
        type=ClaimType.empirical,
        confidence=Confidence.medium,
        section_id=section_id,
        created_by="test",
        created_at=now,
        modified_at=now,
    )


class _StubLLM:
    """Returns a different payload based on what's in the user message."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.classify_doc_payload: object = {}
        self.section_payload: object = []

    async def complete_json(self, system, user, model=None, temperature=0.2):
        self.calls.append((system, user))
        if "Classify every section" in system or "classifying the sections" in system:
            return self.classify_doc_payload, None
        if "Classify every claim" in user or "role and type of each claim" in system:
            return self.section_payload, None
        return {}, None


# ─── Citation regex (deterministic) ─────────────────

def test_citation_regex_catches_author_year_parenthetical() -> None:
    text = "The slowdown is documented (Koomey, 2015) and in Mytton & Ashtine (2022)."
    matches = list(_CITATION_RE.finditer(text))
    assert len(matches) == 2
    assert matches[0].group("year") == "2015"
    assert matches[1].group("year") == "2022"


def test_citation_regex_handles_et_al() -> None:
    text = "Esmaeilzadeh et al. (2011) identify the breakdown."
    matches = list(_CITATION_RE.finditer(text))
    assert len(matches) == 1
    assert "Esmaeilzadeh" in matches[0].group("authors")
    assert matches[0].group("year") == "2011"


def test_match_to_source_maps_to_id() -> None:
    lookup = {"koomey_2015": "koomey_2015", "koomey": "koomey_2015"}
    assert _match_to_source("Koomey", "2015", lookup) == "koomey_2015"


# ─── Deterministic citation extraction ──────────────

async def test_annotator_extracts_inline_citations(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        sections=[
            Section(
                section_id="s.a", title="A", position=1,
                role=SectionRole.argumentative,
                claim_ids=["cl.a.1"],
            )
        ],
        claims=[
            _make_claim(
                "cl.a.1",
                "The slowdown is well documented (Koomey, 2015) and confirmed by Mytton & Ashtine (2022).",
            )
        ],
        created_at=now,
        modified_at=now,
    )
    annotator = ContextualAnnotator(config, llm=None)  # no LLM -> only citation pass
    await annotator.annotate(graph, known_source_ids={"koomey_2015", "mytton_2022"})
    evidence_sources = {ev.source for ev in graph.claims[0].evidence}
    assert evidence_sources == {"koomey_2015", "mytton_2022"}


async def test_annotator_skips_unknown_citations(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        sections=[
            Section(section_id="s.a", title="A", position=1,
                    role=SectionRole.argumentative, claim_ids=["cl.a.1"])
        ],
        claims=[_make_claim("cl.a.1", "Based on Smith (1999).")],
        created_at=now, modified_at=now,
    )
    annotator = ContextualAnnotator(config, llm=None)
    await annotator.annotate(graph, known_source_ids={"koomey_2015"})
    # Smith (1999) not in known sources -> no evidence added.
    assert graph.claims[0].evidence == []


# ─── LLM-assisted thesis + section role classification ──

async def test_annotator_reassigns_references_section(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        thesis_statement="Some document title.",
        sections=[
            Section(section_id="s.a", title="Introduction", position=1,
                    role=SectionRole.argumentative, claim_ids=["cl.a.1"]),
            Section(section_id="s.b", title="References", position=2,
                    role=SectionRole.argumentative, claim_ids=["cl.b.1"]),
        ],
        claims=[
            _make_claim("cl.a.1", "Our argument starts here.", section_id="s.a"),
            _make_claim("cl.b.1", "Andrae, A. (2015). Some paper title.", section_id="s.b"),
        ],
        created_at=now, modified_at=now,
    )
    llm = _StubLLM()
    llm.classify_doc_payload = {
        "thesis": {
            "statement": "A properly derived thesis in one sentence.",
            "source": "synthesised",
            "confidence": "high",
        },
        "sections": [
            {"section_id": "s.a", "role": "argumentative"},
            {"section_id": "s.b", "role": "references"},
        ],
    }
    # No per-section reclassification needed for this test.
    llm.section_payload = []

    annotator = ContextualAnnotator(config, llm)
    await annotator.annotate(graph, known_source_ids=set())

    refs_section = next(s for s in graph.sections if s.section_id == "s.b")
    assert refs_section.role == SectionRole.references
    assert graph.thesis_statement == "A properly derived thesis in one sentence."


async def test_annotator_assigns_claim_roles(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        thesis_statement="Thesis.",
        sections=[
            Section(section_id="s.a", title="Body", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1", "cl.a.2", "cl.a.3"])
        ],
        claims=[
            _make_claim("cl.a.1", "I classify forecasts into two camps."),
            _make_claim("cl.a.2", "Koomey (2015) documents the slowdown."),
            _make_claim("cl.a.3", "Efficiency is the highest-leverage question."),
        ],
        created_at=now, modified_at=now,
    )
    llm = _StubLLM()
    llm.classify_doc_payload = {
        "thesis": {"statement": "Thesis.", "source": "extracted", "confidence": "high"},
        "sections": [{"section_id": "s.a", "role": "argumentative"}],
    }
    llm.section_payload = [
        {"claim_id": "cl.a.1", "role": "setup", "type": "user_synthesis"},
        {"claim_id": "cl.a.2", "role": "evidence", "type": "empirical"},
        {"claim_id": "cl.a.3", "role": "conclusion", "type": "user_synthesis"},
    ]

    annotator = ContextualAnnotator(config, llm)
    await annotator.annotate(graph, known_source_ids=set())

    by_id = {c.claim_id: c for c in graph.claims}
    assert "role:setup" in by_id["cl.a.1"].tags
    assert "role:evidence" in by_id["cl.a.2"].tags
    assert "role:conclusion" in by_id["cl.a.3"].tags
    assert by_id["cl.a.1"].type == ClaimType.user_synthesis
    assert by_id["cl.a.1"].author_origin is True
    assert by_id["cl.a.2"].type == ClaimType.empirical


async def test_annotator_infers_supports_within_section(tmp_path: Path) -> None:
    """Setup/evidence/mechanism claims should `supports` the section's
    conclusion claim; complication should `qualifies` it; counterargument
    should `contradicts`."""
    config = _make_config(tmp_path)
    now = _now()
    section = Section(
        section_id="s.a", title="Body", position=1,
        role=SectionRole.argumentative,
        claim_ids=["cl.a.1", "cl.a.2", "cl.a.3", "cl.a.4", "cl.a.5"],
    )
    claims = [
        _make_claim("cl.a.1", "Setup claim."),
        _make_claim("cl.a.2", "Evidence A."),
        _make_claim("cl.a.3", "Complication."),
        _make_claim("cl.a.4", "Counterargument."),
        _make_claim("cl.a.5", "My conclusion."),
    ]
    claims[0].tags = ["role:setup"]
    claims[1].tags = ["role:evidence"]
    claims[2].tags = ["role:complication"]
    claims[3].tags = ["role:counterargument"]
    claims[4].tags = ["role:conclusion"]
    claims[4].type = ClaimType.user_synthesis
    claims[4].author_origin = True
    thesis_claim = _make_claim("cl.thesis", "The thesis.")
    thesis_claim.type = ClaimType.user_synthesis
    thesis_claim.author_origin = True
    thesis_claim.section_id = "s.thesis"

    graph = AuthorGraph(
        project_name="t",
        thesis_statement="The thesis.",
        sections=[
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            section,
        ],
        claims=[thesis_claim, *claims],
        created_at=now, modified_at=now,
    )

    annotator = ContextualAnnotator(config, llm=None)
    await annotator.annotate(graph, known_source_ids=set())

    by_pair = {(r.from_claim, r.to_claim): r.type for r in graph.relationships}
    assert by_pair.get(("cl.a.1", "cl.a.5")) and by_pair[("cl.a.1", "cl.a.5")].value == "supports"
    assert by_pair.get(("cl.a.2", "cl.a.5")) and by_pair[("cl.a.2", "cl.a.5")].value == "supports"
    assert by_pair.get(("cl.a.3", "cl.a.5")) and by_pair[("cl.a.3", "cl.a.5")].value == "qualifies"
    assert by_pair.get(("cl.a.4", "cl.a.5")) and by_pair[("cl.a.4", "cl.a.5")].value == "contradicts"
    # Section conclusion (user_synthesis + author_origin) supports the thesis.
    assert by_pair.get(("cl.a.5", "cl.thesis")) and by_pair[("cl.a.5", "cl.thesis")].value == "supports"


async def test_annotator_survives_llm_failure(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        sections=[Section(section_id="s.a", title="A", position=1,
                          role=SectionRole.argumentative, claim_ids=["cl.a.1"])],
        claims=[_make_claim("cl.a.1", "A claim.")],
        created_at=now, modified_at=now,
    )

    class _BadLLM:
        async def complete_json(self, system, user, model=None, temperature=0.2):
            raise RuntimeError("model down")

    annotator = ContextualAnnotator(config, _BadLLM())
    # Must not raise.
    await annotator.annotate(graph, known_source_ids=set())
    # Graph still valid.
    assert graph.claims[0].claim_id == "cl.a.1"
