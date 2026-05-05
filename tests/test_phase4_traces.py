"""Phase 4 — retrieval-ranked binding + paragraph trace generation.

Covers:
- BM25 passage ranking surfaces relevant passages regardless of their
  position in the source document (the head-of-document bug).
- Evidence rows now carry passage_char_start/_end + confidence when the
  LLM returns an extractable quote.
- ``build_trace_report`` walks rendered drafts and emits a trace mapping
  sentences → claim_ids → source_ids → evidence_spans.
- ``CoverageCheck`` consumes a persisted paragraph_traces.<voice>.json
  when present, replacing the lexical-overlap heuristic.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.auditor.coverage import CoverageCheck
from lattice.enricher.binder import Enricher, rank_passages_bm25
from lattice.graph.models import (
    BindingStrength, Citation, Claim, ClaimRoleInCluster, ClaimType,
    Cluster, ClusterRole, Confidence, Evidence, FlagCategory, Passage,
    PassageLocation, PassageType, ProseState, Section, SectionRole,
    Severity, Source, SourceMetadata, SourceType,
)
from lattice.graph.store import GraphStore
from lattice.renderer.trace import (
    build_cluster_trace, build_trace_report, regenerate_traces,
    write_trace_report,
)
from lattice.utils.config import Config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _passage(pid: str, text: str, page: int = 1) -> Passage:
    return Passage(
        id=pid, text=text, type=PassageType.claim,
        char_count=len(text),
        location=PassageLocation(page=page),
    )


# ─── BM25 ranking ────────────────────────────────────


def test_rank_passages_bm25_surfaces_relevant_past_position_40() -> None:
    """The pre-Phase-4 binder did ``passages[:40]`` which dropped any
    relevant passage that lived past index 40. BM25 ranking should
    surface it regardless of position."""
    # 60 dummy passages with no overlap to the query…
    passages = [
        _passage(f"p.dummy.{i}", "filler paragraph about unrelated topics " * 3)
        for i in range(60)
    ]
    # …and the actual relevant passage sits at index 50.
    relevant = _passage(
        "p.relevant",
        "Koomey's Law doubling period lengthened from 1.5 to 2.6 years.",
    )
    passages.insert(50, relevant)

    ranked = rank_passages_bm25(
        "Koomey's Law slowdown doubling period",
        passages, top_n=10,
    )
    top_ids = [p.id for p, _s in ranked]
    assert "p.relevant" in top_ids
    # And it should be the top hit, not a tail-of-list rescue.
    assert ranked[0][0].id == "p.relevant"


def test_rank_passages_bm25_empty_query_falls_back_to_document_order() -> None:
    """When the query has no content tokens, the ranker degrades to
    document order rather than returning an empty list — consumers
    rely on getting *some* candidates."""
    passages = [_passage(f"p.{i}", "lorem ipsum filler") for i in range(5)]
    ranked = rank_passages_bm25("the a an of", passages, top_n=3)
    assert [p.id for p, _ in ranked] == ["p.0", "p.1", "p.2"]


def test_rank_passages_bm25_handles_empty_passage_list() -> None:
    assert rank_passages_bm25("anything", [], top_n=10) == []


# ─── Evidence span + confidence from binder ──────────


class _StubLLM:
    def __init__(self, payload: dict | Exception) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, model=None, temperature=0.2):
        self.calls.append((system, user))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload, None


def _make_source_with_long_passage(tmp_path: Path) -> Source:
    return Source(
        source_id="src.long",
        type=SourceType.primary_paper,
        citation=Citation(authors=["Author"], year=2020, title="x"),
        passages=[
            _passage(
                "p.42",
                "Background sentence one. The doubling period lengthened "
                "from 1.5 to 2.6 years between 2000 and 2015 according "
                "to careful retrospective analysis.",
            ),
        ],
        metadata=SourceMetadata(
            date_added=_now(), file_path="refs/papers/long.pdf",
            hash="sha256:xyz",
        ),
    )


@pytest.mark.asyncio
async def test_enricher_records_passage_span_and_confidence(
    tmp_path: Path,
) -> None:
    """When the LLM returns an extractable quote, the resulting
    Evidence row should carry the passage char span and a numeric
    confidence in [0, 1]."""
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    source = _make_source_with_long_passage(tmp_path)
    store.save_source(source)
    claim = Claim(
        claim_id="cl.x.1",
        statement="The doubling period lengthened sharply.",
        type=ClaimType.empirical, confidence=Confidence.high,
        evidence=[Evidence(
            source="src.long", passage="",
            binding_strength=BindingStrength.weak,
        )],
        created_by="t", created_at=_now(), modified_at=_now(),
    )
    store.save_claim(claim)

    llm = _StubLLM({
        "binding_strength": "strong",
        "best_passage_id": "p.42",
        "rationale": "states the doubling-period figure directly",
        "extracted_quote":
            "The doubling period lengthened from 1.5 to 2.6 years",
        "page": 1,
    })
    enricher = Enricher(Config.load(tmp_path), store, llm)
    n = await enricher.enrich_all()
    assert n == 1

    updated = store.get_claim("cl.x.1")
    ev = updated.evidence[0]
    assert ev.passage == "p.42"
    assert ev.binding_strength == BindingStrength.strong
    assert ev.passage_char_start is not None and ev.passage_char_end is not None
    # The quote actually appears in the passage text — span should
    # bracket it correctly.
    assert source.passages[0].text[ev.passage_char_start:ev.passage_char_end] \
        .startswith("The doubling period lengthened")
    assert ev.confidence is not None and 0.85 <= ev.confidence <= 1.0


@pytest.mark.asyncio
async def test_enricher_confidence_zero_for_no_match(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    source = _make_source_with_long_passage(tmp_path)
    store.save_source(source)
    claim = Claim(
        claim_id="cl.x.1", statement="A claim.",
        type=ClaimType.empirical, confidence=Confidence.high,
        evidence=[Evidence(
            source="src.long", passage="",
            binding_strength=BindingStrength.weak,
        )],
        created_by="t", created_at=_now(), modified_at=_now(),
    )
    store.save_claim(claim)
    llm = _StubLLM({
        "binding_strength": "none", "best_passage_id": "",
        "rationale": "no support",
    })
    await Enricher(Config.load(tmp_path), store, llm).enrich_all()
    updated = store.get_claim("cl.x.1")
    ev = updated.evidence[0]
    assert ev.binding_strength == BindingStrength.none_
    assert ev.confidence == 0.10  # base confidence floor for "none"


# ─── paragraph trace generation ──────────────────────


def _seed_traceable_project(tmp_path: Path) -> tuple[Path, GraphStore]:
    project = tmp_path / "demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(project)
    now = _now()

    section = Section(
        section_id="s.x", title="X", position=1,
        role=SectionRole.argumentative, claim_ids=["cl.x.1", "cl.x.2"],
    )
    claim_a = Claim(
        claim_id="cl.x.1",
        statement="Energy efficiency gains have stalled in datacentres.",
        type=ClaimType.empirical, confidence=Confidence.high,
        section_id="s.x",
        evidence=[Evidence(
            source="src.koomey", passage="p.1",
            binding_strength=BindingStrength.strong,
            confidence=0.9,
        )],
        created_by="t", created_at=now, modified_at=now,
    )
    claim_b = Claim(
        claim_id="cl.x.2",
        statement="Cooling power dominates the marginal energy cost.",
        type=ClaimType.empirical, confidence=Confidence.medium,
        section_id="s.x",
        created_by="t", created_at=now, modified_at=now,
    )
    store.save_graph(__import__("lattice.graph.models", fromlist=["AuthorGraph"]).AuthorGraph(
        project_name="demo", sections=[section],
        claims=[claim_a, claim_b], relationships=[],
        created_at=now, modified_at=now,
    ))
    cluster = Cluster(
        cluster_id="c.x.1", section_id="s.x", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence),
            ClaimRoleInCluster(claim_id="cl.x.2", role_in_cluster=ClusterRole.evidence),
        ],
        prose_state=ProseState.generated,
    )
    store.save_cluster(cluster)
    drafts_dir = project / ".lattice" / "drafts" / "academic"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    (drafts_dir / "cluster_c.x.1.md").write_text(
        "Energy efficiency gains have stalled in datacentres over the "
        "last decade. Cooling power dominates the marginal energy cost. "
        "Unrelated transitional sentence with nothing in common.",
        encoding="utf-8",
    )
    return project, store


def test_build_trace_report_maps_sentences_to_claims(tmp_path: Path) -> None:
    project, store = _seed_traceable_project(tmp_path)
    report = build_trace_report(project, store, "academic")
    assert "c.x.1" in report.clusters
    cluster_trace = report.clusters["c.x.1"]
    sentences = cluster_trace.paragraphs[0].sentences
    assert len(sentences) == 3

    # First sentence shares "energy", "efficiency", "datacentres" with cl.x.1.
    assert "cl.x.1" in sentences[0].claim_ids
    # Second sentence shares "cooling", "power", "energy", "cost" with cl.x.2.
    assert "cl.x.2" in sentences[1].claim_ids
    # Third sentence has no overlap → orphan.
    assert sentences[2].claim_ids == []

    # Evidence spans surface from claim_a's bound evidence.
    assert any(es.source_id == "src.koomey" for es in sentences[0].evidence_spans)

    # Counts match.
    assert report.paragraph_count == 1
    assert report.sentence_count == 3
    assert report.traced_sentence_count == 2


def test_regenerate_traces_writes_report_file(tmp_path: Path) -> None:
    project, store = _seed_traceable_project(tmp_path)
    target = regenerate_traces(project, store, "academic")
    assert target is not None
    assert target.name == "paragraph_traces.academic.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["voice_name"] == "academic"
    assert "c.x.1" in payload["clusters"]


def test_regenerate_traces_returns_none_when_nothing_rendered(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    (project / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(project)
    out = regenerate_traces(project, store, "academic")
    assert out is None


# ─── coverage check consumes traces ──────────────────


@pytest.mark.asyncio
async def test_coverage_check_uses_trace_when_available(tmp_path: Path) -> None:
    project, store = _seed_traceable_project(tmp_path)
    # Persist the trace so CoverageCheck picks it up.
    target = regenerate_traces(project, store, "academic")
    assert target and target.exists()

    # Build a CoverageCheck with the same store + voice, give it the
    # cluster + prose, and confirm it flags the third (orphan)
    # sentence with the trace-aware rule_description.
    from lattice.voice.parser import Voice
    voice_path = (
        Path(__file__).parent.parent
        / "examples" / "voices" / "academic.voice.md"
    )
    voice = Voice.from_file(voice_path)
    cluster = store.list_clusters()[0]
    prose = (
        project / ".lattice" / "drafts" / "academic"
        / "cluster_c.x.1.md"
    ).read_text(encoding="utf-8")

    config = Config.load(project)
    check = CoverageCheck(config=config, store=store, llm=None, voice=voice)
    flags = await check.check_cluster(cluster, prose)
    orphans = [f for f in flags if f.rule_id == "coverage.orphan_sentence"]
    assert len(orphans) == 1
    assert "paragraph_traces" in orphans[0].rule_description
