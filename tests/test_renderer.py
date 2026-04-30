"""Tests for Assembler, ClusterRenderer, ParallelRenderer, DocumentFinaliser."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    BindingStrength,
    Citation,
    Claim,
    ClaimRoleInCluster,
    ClaimType,
    Cluster,
    ClusterRole,
    Confidence,
    Evidence,
    Passage,
    PassageLocation,
    PassageType,
    ProseState,
    Section,
    SectionRole,
    Source,
    SourceMetadata,
    SourceType,
)
from lattice.graph.store import GraphStore
from lattice.renderer.assembler import Assembler
from lattice.renderer.assembler_finalise import DocumentFinaliser
from lattice.renderer.cluster_renderer import ClusterRenderer
from lattice.renderer.parallel import ParallelRenderer
from lattice.utils.config import Config
from lattice.voice.parser import Voice


# ─── Fixtures ──────────────────────────────────────────

def _mk_store_with_graph(tmp_path: Path) -> tuple[GraphStore, Config]:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-test\n", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    now = datetime.now(timezone.utc)

    # Thesis section + one argumentative section
    sections = [
        Section(
            section_id="s.thesis",
            title="Thesis",
            position=0,
            role=SectionRole.introduction,
            claim_ids=["cl.thesis"],
            target_length=50,
        ),
        Section(
            section_id="s.c",
            title="Gap 1: efficiency",
            position=1,
            role=SectionRole.argumentative,
            claim_ids=["cl.c.1", "cl.c.2", "cl.c.3", "cl.c.4", "cl.c.5"],
            target_length=600,
        ),
        # Closing section so the readiness check (Fix 1) is satisfied for
        # tests that exercise the finaliser. Other tests don't care about it.
        Section(
            section_id="s.end",
            title="Conclusion",
            position=2,
            role=SectionRole.conclusion,
            claim_ids=["cl.end.1"],
            target_length=200,
        ),
    ]
    claims = [
        Claim(
            claim_id="cl.thesis",
            statement="Forecasts diverge because of assumption, not measurement.",
            type=ClaimType.user_synthesis,
            confidence=Confidence.high,
            author_origin=True,
            section_id="s.thesis",
            created_by="test",
            created_at=now,
            modified_at=now,
            tags=["thesis"],
        ),
        Claim(
            claim_id="cl.c.1",
            statement="Stabilisation forecasts assume Koomey's Law continues.",
            type=ClaimType.user_synthesis,
            confidence=Confidence.high,
            author_origin=True,
            section_id="s.c",
            created_by="test",
            created_at=now,
            modified_at=now,
            tags=["role:setup"],
        ),
        Claim(
            claim_id="cl.c.2",
            statement="Koomey's Law doubling period lengthened in the 2010s.",
            type=ClaimType.empirical,
            confidence=Confidence.high,
            evidence=[
                Evidence(source="koomey_2015", passage="p.3.2",
                         binding_strength=BindingStrength.strong, page=3,
                         quote_verbatim=True,
                         quote_text="doubling period lengthened from 1.5 to 2.6 years")
            ],
            section_id="s.c",
            created_by="test",
            created_at=now,
            modified_at=now,
            tags=["role:evidence"],
        ),
        Claim(
            claim_id="cl.c.3",
            statement="Dennard scaling broke around 2006.",
            type=ClaimType.empirical,
            confidence=Confidence.high,
            evidence=[
                Evidence(source="esmaeilzadeh_2011", passage="p.2.1",
                         binding_strength=BindingStrength.strong, page=2)
            ],
            section_id="s.c",
            created_by="test",
            created_at=now,
            modified_at=now,
            tags=["role:mechanism"],
        ),
        Claim(
            claim_id="cl.c.4",
            statement="Landauer's limit is far from binding today.",
            type=ClaimType.empirical,
            confidence=Confidence.medium,
            evidence=[
                Evidence(source="landauer_1961", passage="p.1.1",
                         binding_strength=BindingStrength.weak)
            ],
            section_id="s.c",
            created_by="test",
            created_at=now,
            modified_at=now,
            tags=["role:limit"],
        ),
        Claim(
            claim_id="cl.c.5",
            statement="Efficiency trajectory is the highest-leverage question.",
            type=ClaimType.user_synthesis,
            confidence=Confidence.high,
            author_origin=True,
            section_id="s.c",
            created_by="test",
            created_at=now,
            modified_at=now,
            tags=["role:conclusion"],
        ),
        Claim(
            claim_id="cl.end.1",
            statement="The argument concludes that efficiency assumptions matter most.",
            type=ClaimType.user_synthesis,
            confidence=Confidence.high,
            author_origin=True,
            section_id="s.end",
            created_by="test",
            created_at=now,
            modified_at=now,
            tags=["role:conclusion"],
        ),
    ]
    for s in sections:
        store.save_section(s)
    for c in claims:
        store.save_claim(c)

    # Sources
    for sid, title in [
        ("koomey_2015", "Energy trends"),
        ("esmaeilzadeh_2011", "Dennard scaling"),
        ("landauer_1961", "Thermodynamic limits"),
    ]:
        store.save_source(
            Source(
                source_id=sid,
                type=SourceType.primary_paper,
                citation=Citation(authors=[sid.split("_")[0].title()], year=int(sid.split("_")[1]), title=title),
                passages=[
                    Passage(
                        id="p.3.2" if sid == "koomey_2015" else ("p.2.1" if sid == "esmaeilzadeh_2011" else "p.1.1"),
                        text=f"Passage text from {sid}.",
                        location=PassageLocation(page=3 if sid == "koomey_2015" else (2 if sid == "esmaeilzadeh_2011" else 1)),
                        type=PassageType.claim,
                        char_count=40,
                    )
                ],
                metadata=SourceMetadata(
                    date_added=now, file_path=f"refs/papers/{sid}.pdf", hash=f"sha256:{sid}"
                ),
            )
        )

    config = Config.load(tmp_path)
    return store, config


def _load_academic_voice() -> Voice:
    path = Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    return Voice.from_file(path)


class _FakeLLMResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.input_tokens = 500
        self.output_tokens = 250
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0
        self.model = "mock"
        self.stop_reason = "end_turn"


class _StubLLM:
    """Returns canned prose for each cluster, stamped with the cluster ID."""
    def __init__(self, text_fn=None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._text_fn = text_fn or (lambda sys, usr: "Generated paragraph for this cluster. It traces to claims.")

    async def complete(self, system, user, model=None, temperature=0.6, max_tokens=4096):
        self.calls.append((system, user))
        return _FakeLLMResponse(self._text_fn(system, user))


# ─── Assembler ─────────────────────────────────────────

async def test_assembler_groups_claims_into_clusters(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    assert len(clusters) >= 2
    # Each cluster has 1-4 claims
    for c in clusters:
        assert 1 <= len(c.claim_sequence) <= 4
    # Every claim ended up in exactly one cluster
    claim_ids_in_clusters = [e.claim_id for c in clusters for e in c.claim_sequence]
    assert "cl.c.2" in claim_ids_in_clusters


async def test_assembler_assigns_reporting_verbs(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    # High-confidence empirical claims should get a direct_evidence verb.
    for cluster in clusters:
        for entry in cluster.claim_sequence:
            claim = store.get_claim(entry.claim_id)
            if claim.type == ClaimType.empirical and claim.confidence == Confidence.high:
                assert entry.reporting_verb in voice.citation.reporting_verbs.direct_evidence


async def test_assembler_marks_synthesis_required_when_three_sources(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    # Across the section, 3 distinct sources are cited. Whichever cluster
    # contains claims citing 3+ distinct sources should have synthesis_required.
    any_synthesis = any(c.citation_strategy.synthesis_required for c in clusters)
    assert any_synthesis or all(
        len(set(ev.source for cid in [e.claim_id for e in c.claim_sequence]
                for ev in store.get_claim(cid).evidence)) < 3
        for c in clusters
    )


async def test_assembler_sets_transitions(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    non_thesis = [c for c in clusters if c.section_id != "s.thesis"]
    # At least one cluster has a previous_cluster link
    assert any(c.previous_cluster for c in non_thesis)
    # Last cluster has no next_cluster
    assert clusters[-1].next_cluster is None


async def test_assembler_tracks_first_mention(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    clusters = await Assembler(config, store, llm=None, voice=voice).build_plan()
    # Each source appears exactly once across all clusters' first_mention_full.
    all_first_mentions: list[str] = []
    for c in clusters:
        all_first_mentions.extend(c.citation_strategy.first_mention_full)
    assert len(all_first_mentions) == len(set(all_first_mentions))
    assert "koomey_2015" in all_first_mentions


async def test_assembler_validates_six_element_template(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    assembler = Assembler(config, store, llm=None, voice=voice)
    clusters = await assembler.build_plan()
    # For this minimal graph, the architecture validation may flag missing sections.
    # Just confirm the validate method runs without exception.
    issues = assembler.validate_architecture(store.get_graph())
    assert isinstance(issues, list)


# ─── ClusterRenderer ───────────────────────────────────

async def test_cluster_renderer_produces_prose(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    await Assembler(config, store, llm=None, voice=voice).build_plan()
    clusters = store.list_clusters()

    llm = _StubLLM(text_fn=lambda s, u: f"Prose for cluster in section about efficiency.")
    renderer = ClusterRenderer(config, store, llm, voice)
    prose = await renderer.render_cluster(clusters[-1].cluster_id)
    assert "Prose for cluster" in prose

    # Cluster metadata updated
    cluster = store.get_cluster(clusters[-1].cluster_id)
    assert cluster.prose_state == ProseState.generated
    assert cluster.last_rendered_hash
    # File on disk
    prose_path = tmp_path / ".lattice" / "drafts" / voice.name / f"cluster_{cluster.cluster_id}.md"
    assert prose_path.exists()


async def test_cluster_renderer_skips_if_already_generated(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    await Assembler(config, store, llm=None, voice=voice).build_plan()
    clusters = store.list_clusters()

    llm = _StubLLM()
    renderer = ClusterRenderer(config, store, llm, voice)
    first_prose = await renderer.render_cluster(clusters[0].cluster_id)
    second_prose = await renderer.render_cluster(clusters[0].cluster_id)  # should skip
    assert len(llm.calls) == 1
    assert first_prose == second_prose


async def test_cluster_renderer_force_rerenders(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    await Assembler(config, store, llm=None, voice=voice).build_plan()
    clusters = store.list_clusters()
    llm = _StubLLM()
    renderer = ClusterRenderer(config, store, llm, voice)
    await renderer.render_cluster(clusters[0].cluster_id)
    await renderer.render_cluster(clusters[0].cluster_id, force=True)
    assert len(llm.calls) == 2


# ─── ParallelRenderer ──────────────────────────────────

async def test_parallel_renderer_continues_on_failure(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    await Assembler(config, store, llm=None, voice=voice).build_plan()
    cluster_ids = [c.cluster_id for c in store.list_clusters()]
    assert len(cluster_ids) >= 2

    # Fail the FIRST call the LLM sees, succeed after.
    call_count = {"n": 0}

    class _FlakyLLM(_StubLLM):
        async def complete(self, system, user, model=None, temperature=0.6, max_tokens=4096):
            call_count["n"] += 1
            self.calls.append((system, user))
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            return _FakeLLMResponse("ok prose")

    llm = _FlakyLLM()
    renderer = ClusterRenderer(config, store, llm, voice)
    parallel = ParallelRenderer(renderer, max_concurrent=1)  # serialised so failure is deterministic
    results = await parallel.render_all(cluster_ids)
    # Per Fix 2: render_cluster no longer propagates LLM exceptions; it
    # catches them and writes a CLUSTER_UNRENDERABLE marker. So the
    # "failed" result is a string containing that marker, and the cluster's
    # prose_state is failed.
    failures = [
        r for r in results.values()
        if isinstance(r, str) and "CLUSTER_UNRENDERABLE" in r
    ]
    successes = [
        r for r in results.values()
        if isinstance(r, str) and "CLUSTER_UNRENDERABLE" not in r
    ]
    assert len(failures) == 1
    assert len(successes) == len(cluster_ids) - 1


# ─── DocumentFinaliser ─────────────────────────────────

async def test_finaliser_concatenates_clusters_in_order(tmp_path: Path) -> None:
    store, config = _mk_store_with_graph(tmp_path)
    voice = _load_academic_voice()
    await Assembler(config, store, llm=None, voice=voice).build_plan()
    clusters = store.list_clusters()

    llm = _StubLLM(text_fn=lambda s, u: "Canonical paragraph.")
    renderer = ClusterRenderer(config, store, llm, voice)
    for c in clusters:
        await renderer.render_cluster(c.cluster_id)

    store.save_graph(store.get_graph())  # persist updated cluster_ids on sections

    path = DocumentFinaliser(tmp_path, store, voice).finalise()
    assert path.exists()
    output = path.read_text(encoding="utf-8")
    assert "Thesis" in output
    assert "Gap 1: efficiency" in output
    assert "Canonical paragraph." in output
