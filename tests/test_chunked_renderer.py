"""Tests for the chunked renderer."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    BindingStrength, Citation, Claim, ClaimRoleInCluster, ClaimType, Cluster,
    ClusterRole, Confidence, Evidence, Passage, PassageLocation, PassageType,
    ProseState, Section, SectionRole, Source, SourceMetadata, SourceType,
)
from lattice.graph.store import GraphStore
from lattice.renderer.chunked_renderer import ChunkedRenderer
from lattice.utils.config import Config
from lattice.voice.parser import Voice


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _academic_voice() -> Voice:
    return Voice.from_file(
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.input_tokens = 1500
        self.output_tokens = 800
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0
        self.model = "mock"
        self.stop_reason = "end_turn"


class _StubLLM:
    """Returns a single canned JSON payload for every chunked call."""

    def __init__(self, payloads: list) -> None:
        self.payloads = list(payloads)
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, model=None, temperature=0.6):
        self.calls.append((system, user))
        if not self.payloads:
            return [], _FakeResp("[]")
        payload = self.payloads.pop(0)
        import json as _json
        return payload, _FakeResp(_json.dumps(payload))


def _build_environment(
    tmp_path: Path,
    n_clusters: int = 12,
    *,
    bound: bool = True,
) -> tuple[GraphStore, Config, Voice, list[str]]:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    voice = _academic_voice()
    config = Config.load(tmp_path)

    # One section, n_clusters clusters of 2 claims each.
    claims: list[Claim] = []
    cluster_ids: list[str] = []
    sections = [Section(
        section_id="s.x", title="Body", position=1,
        role=SectionRole.argumentative,
        claim_ids=[],
    )]
    for i in range(n_clusters):
        cid_a = f"cl.x.{2*i+1}"
        cid_b = f"cl.x.{2*i+2}"
        claims.extend([
            Claim(
                claim_id=cid_a,
                statement=f"Claim {2*i+1} body.",
                type=ClaimType.user_synthesis if not bound else ClaimType.empirical,
                confidence=Confidence.high,
                evidence=[
                    Evidence(source="src", passage="p.1.1", binding_strength=BindingStrength.strong),
                ] if bound else [],
                author_origin=not bound,
                section_id="s.x",
                created_by="t", created_at=_now(), modified_at=_now(),
            ),
            Claim(
                claim_id=cid_b,
                statement=f"Claim {2*i+2} body.",
                type=ClaimType.user_synthesis if not bound else ClaimType.empirical,
                confidence=Confidence.high,
                evidence=[
                    Evidence(source="src", passage="p.1.1", binding_strength=BindingStrength.strong),
                ] if bound else [],
                author_origin=not bound,
                section_id="s.x",
                created_by="t", created_at=_now(), modified_at=_now(),
            ),
        ])
        sections[0].claim_ids.extend([cid_a, cid_b])
        cluster = Cluster(
            cluster_id=f"c.x.{i+1}", section_id="s.x", position=i+1,
            role=ClusterRole.evidence,
            claim_sequence=[
                ClaimRoleInCluster(claim_id=cid_a, role_in_cluster=ClusterRole.evidence),
                ClaimRoleInCluster(claim_id=cid_b, role_in_cluster=ClusterRole.evidence),
            ],
        )
        store.save_cluster(cluster)
        cluster_ids.append(cluster.cluster_id)

    for s in sections:
        store.save_section(s)
    for c in claims:
        store.save_claim(c)
    store.save_source(Source(
        source_id="src",
        type=SourceType.primary_paper,
        citation=Citation(authors=["X"], year=2020, title="X"),
        passages=[Passage(
            id="p.1.1", text="Source text.",
            location=PassageLocation(page=1), type=PassageType.claim, char_count=10,
        )],
        metadata=SourceMetadata(
            date_added=_now(), file_path="x", hash="sha256:x",
        ),
    ))
    return store, config, voice, cluster_ids


# ─── Chunk grouping ────────────────────────────

async def test_chunking_respects_min_max(tmp_path):
    store, config, voice, _ = _build_environment(tmp_path, n_clusters=21)
    llm = _StubLLM([])
    renderer = ChunkedRenderer(config, store, llm, voice, min_chunk=8, max_chunk=20)
    chunks = renderer._build_chunks(store.list_clusters())
    assert len(chunks) >= 1
    sizes = [len(c.clusters) for c in chunks]
    # No chunk exceeds max_chunk.
    assert all(s <= 20 for s in sizes)
    # 21 / 20 = 2 chunks (max), with sizes summing to 21.
    assert sum(sizes) == 21


async def test_chunking_splits_oversized_section(tmp_path):
    store, config, voice, _ = _build_environment(tmp_path, n_clusters=30)
    llm = _StubLLM([])
    renderer = ChunkedRenderer(config, store, llm, voice, min_chunk=8, max_chunk=10)
    chunks = renderer._build_chunks(store.list_clusters())
    sizes = [len(c.clusters) for c in chunks]
    assert sum(sizes) == 30
    assert all(s <= 10 for s in sizes)


# ─── End-to-end ───────────────────────────────

async def test_chunked_render_produces_per_cluster_files(tmp_path):
    store, config, voice, cluster_ids = _build_environment(tmp_path, n_clusters=10)
    payload = [
        {"cluster_id": cid, "prose": f"Prose for {cid} as part of a chunk."}
        for cid in cluster_ids
    ]
    llm = _StubLLM([payload])
    renderer = ChunkedRenderer(config, store, llm, voice, min_chunk=8, max_chunk=20)
    results = await renderer.render_all()
    assert len(results) == 10
    # One LLM call for the whole set.
    assert len(llm.calls) == 1
    # Per-cluster prose files exist.
    drafts = tmp_path / ".lattice" / "drafts" / voice.name
    for cid in cluster_ids:
        assert (drafts / f"cluster_{cid}.md").exists()


async def test_chunked_render_unrenderable_skips_llm(tmp_path):
    """Clusters whose claims are unbound and not author-grounded never enter the chunk."""
    store, config, voice, cluster_ids = _build_environment(tmp_path, n_clusters=3, bound=False)
    # Override the claims to be empirical with no evidence -> unrenderable.
    for cid in cluster_ids:
        cluster = store.get_cluster(cid)
        for entry in cluster.claim_sequence:
            claim = store.get_claim(entry.claim_id)
            claim.type = ClaimType.empirical
            claim.author_origin = False
            claim.evidence = []
            store.save_claim(claim)
    llm = _StubLLM([[]])  # no payloads should be needed
    renderer = ChunkedRenderer(config, store, llm, voice, min_chunk=2, max_chunk=20)
    results = await renderer.render_all()
    assert all("CLUSTER_UNRENDERABLE" in r for r in results.values())
    assert len(llm.calls) == 0


async def test_chunked_render_caches_generated(tmp_path):
    store, config, voice, cluster_ids = _build_environment(tmp_path, n_clusters=3)
    # Pre-populate prose files and mark generated so they should be cached.
    drafts = tmp_path / ".lattice" / "drafts" / voice.name
    drafts.mkdir(parents=True, exist_ok=True)
    for cid in cluster_ids:
        (drafts / f"cluster_{cid}.md").write_text("cached prose", encoding="utf-8")
        cluster = store.get_cluster(cid)
        cluster.prose_state = ProseState.generated
        store.save_cluster(cluster)

    llm = _StubLLM([])
    renderer = ChunkedRenderer(config, store, llm, voice, min_chunk=2, max_chunk=20)
    results = await renderer.render_all()
    assert all(r == "cached prose" for r in results.values())
    assert len(llm.calls) == 0


async def test_chunked_render_force_recompute(tmp_path):
    store, config, voice, cluster_ids = _build_environment(tmp_path, n_clusters=3)
    drafts = tmp_path / ".lattice" / "drafts" / voice.name
    drafts.mkdir(parents=True, exist_ok=True)
    for cid in cluster_ids:
        (drafts / f"cluster_{cid}.md").write_text("stale", encoding="utf-8")
        cluster = store.get_cluster(cid)
        cluster.prose_state = ProseState.generated
        store.save_cluster(cluster)

    payload = [{"cluster_id": cid, "prose": f"fresh {cid}"} for cid in cluster_ids]
    llm = _StubLLM([payload])
    renderer = ChunkedRenderer(config, store, llm, voice, min_chunk=2, max_chunk=20)
    results = await renderer.render_all(force=True)
    assert len(llm.calls) == 1
    for cid in cluster_ids:
        assert "fresh" in results[cid]


async def test_chunked_render_register_bleed_marks_cluster_failed(tmp_path):
    store, config, voice, cluster_ids = _build_environment(tmp_path, n_clusters=3)
    payload = [
        {"cluster_id": cluster_ids[0], "prose": "Clean prose for first cluster."},
        {"cluster_id": cluster_ids[1], "prose": "I cannot proceed with this cluster."},
        {"cluster_id": cluster_ids[2], "prose": "Clean prose for third cluster."},
    ]
    llm = _StubLLM([payload])
    renderer = ChunkedRenderer(config, store, llm, voice, min_chunk=2, max_chunk=20)
    results = await renderer.render_all()
    assert "CLUSTER_UNRENDERABLE" in results[cluster_ids[1]]
    assert "Clean prose" in results[cluster_ids[0]]
    assert "Clean prose" in results[cluster_ids[2]]


async def test_chunked_render_missing_cluster_in_response(tmp_path):
    """If the LLM returns a partial response, missing clusters get failure markers."""
    store, config, voice, cluster_ids = _build_environment(tmp_path, n_clusters=3)
    payload = [
        {"cluster_id": cluster_ids[0], "prose": "First cluster prose."},
        # cluster_ids[1] omitted entirely
        {"cluster_id": cluster_ids[2], "prose": "Third cluster prose."},
    ]
    llm = _StubLLM([payload])
    renderer = ChunkedRenderer(config, store, llm, voice, min_chunk=2, max_chunk=20)
    results = await renderer.render_all()
    assert "CLUSTER_UNRENDERABLE" in results[cluster_ids[1]]
    assert "First cluster prose" in results[cluster_ids[0]]
    assert "Third cluster prose" in results[cluster_ids[2]]
