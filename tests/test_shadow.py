"""Tests for the shadow mapper sub-stages and orchestrator."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    Citation, Passage, PassageLocation, PassageType,
    Source, SourceMetadata, SourceType,
)
from lattice.shadow.architect import ShadowArchitect
from lattice.shadow.cluster import ShadowClusterer
from lattice.shadow.extract import ShadowExtractor
from lattice.shadow import ShadowMapper
from lattice.utils.config import Config


def _mk_source(tmp_path: Path, sid: str, passages: list[str]) -> Source:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    return Source(
        source_id=sid,
        type=SourceType.primary_paper,
        citation=Citation(authors=["Author"], year=2020, title=sid),
        passages=[
            Passage(
                id=f"p.1.{i + 1}",
                text=text,
                location=PassageLocation(page=1, paragraph=i + 1),
                type=PassageType.claim,
                char_count=len(text),
            )
            for i, text in enumerate(passages)
        ],
        metadata=SourceMetadata(
            date_added=datetime.now(timezone.utc),
            file_path=f"refs/papers/{sid}.pdf",
            hash=f"sha256:{sid}-v1",
        ),
    )


class _StubLLM:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, model=None, temperature=0.2):
        self.calls.append((system, user))
        return self.payload, None


# ─── ShadowExtractor ────────────────────────────────

async def test_extractor_caches_per_source_hash(tmp_path: Path) -> None:
    config = Config.load(tmp_path)
    llm = _StubLLM([
        {"statement": "Claim one.", "passage_id": "p.1.1", "type": "empirical", "confidence": "high"},
        {"statement": "Claim two.", "passage_id": "p.1.2", "type": "empirical", "confidence": "medium"},
    ])
    extractor = ShadowExtractor(config, llm)
    src = _mk_source(tmp_path, "src_a", ["First passage.", "Second passage."])
    first = await extractor.extract_one(src)
    assert len(first) == 2

    # Second call should hit the cache — the LLM is not invoked again.
    second = await extractor.extract_one(src)
    assert second == first
    assert len(llm.calls) == 1


async def test_extractor_reextracts_when_source_hash_changes(tmp_path: Path) -> None:
    config = Config.load(tmp_path)
    llm = _StubLLM([{"statement": "x", "passage_id": "p.1.1", "type": "empirical", "confidence": "medium"}])
    extractor = ShadowExtractor(config, llm)
    src = _mk_source(tmp_path, "src_b", ["p"])
    await extractor.extract_one(src)
    # Simulate source change by bumping its hash.
    src.metadata.hash = "sha256:src_b-v2"
    await extractor.extract_one(src)
    assert len(llm.calls) == 2


async def test_extractor_survives_llm_error(tmp_path: Path) -> None:
    config = Config.load(tmp_path)

    class _BadLLM:
        async def complete_json(self, system, user, model=None, temperature=0.2):
            raise RuntimeError("model unavailable")

    extractor = ShadowExtractor(config, _BadLLM())
    src = _mk_source(tmp_path, "src_err", ["p"])
    result = await extractor.extract_one(src)
    assert result and "error" in result[0]


# ─── ShadowClusterer ───────────────────────────────

async def test_clusterer_groups_by_topic_overlap() -> None:
    claims = {
        "src_a": [
            {"statement": "Koomey's Law doubling period lengthened.", "passage_id": "p.1", "type": "empirical", "confidence": "high"},
            {"statement": "Efficiency gains plateaued after 2010.", "passage_id": "p.2", "type": "empirical", "confidence": "medium"},
        ],
        "src_b": [
            {"statement": "Koomey's Law doubling slowdown documented.", "passage_id": "p.1", "type": "empirical", "confidence": "high"},
            {"statement": "Data-centre liquid cooling deployment scaled.", "passage_id": "p.2", "type": "empirical", "confidence": "medium"},
        ],
    }
    clusters = await ShadowClusterer().cluster(claims)
    # Koomey claims should cluster together; cooling claim is separate.
    # At minimum we see >1 cluster.
    assert len(clusters) >= 2
    koomey_cluster = next(
        (c for c in clusters if any("Koomey" in x["statement"] for x in c["claims"])),
        None,
    )
    assert koomey_cluster and len(koomey_cluster["claims"]) >= 2


# ─── ShadowArchitect ───────────────────────────────

async def test_architect_builds_graph_with_relationships(tmp_path: Path) -> None:
    config = Config.load(tmp_path)
    llm = _StubLLM([
        {"from": "sc.src_a.1", "to": "sc.src_b.1", "type": "supports",
         "strength": "direct", "note": "same finding"},
    ])
    clusters = [
        {
            "cluster_id": "shadow_cluster_1",
            "topic": "koomey slowdown",
            "claim_ids": ["sc.src_a.1", "sc.src_b.1"],
            "claims": [
                {"claim_id": "sc.src_a.1", "source_id": "src_a", "statement": "Slowdown A.",
                 "passage_id": "p.1", "type": "empirical", "confidence": "high"},
                {"claim_id": "sc.src_b.1", "source_id": "src_b", "statement": "Slowdown B.",
                 "passage_id": "p.1", "type": "empirical", "confidence": "high"},
            ],
            "sources": ["src_a", "src_b"],
        }
    ]
    graph = await ShadowArchitect(config, llm).build(clusters, thesis="Test thesis.")
    assert graph.thesis_statement == "Test thesis."
    assert len(graph.claims) == 2
    assert len(graph.relationships) == 1
    assert graph.relationships[0].type.value == "supports"


# ─── ShadowMapper end-to-end (mocked) ───────────────

async def test_mapper_runs_end_to_end(tmp_path: Path) -> None:
    config = Config.load(tmp_path)

    class _SequentialLLM:
        """Return an extraction payload first, then architect payloads."""
        def __init__(self) -> None:
            self.step = 0
            self.calls: list = []

        async def complete_json(self, system, user, model=None, temperature=0.2):
            self.calls.append((system, user))
            if "Extract every atomic claim" in user:
                return [
                    {"statement": "Claim from this source.", "passage_id": "p.1.1",
                     "type": "empirical", "confidence": "high"},
                ], None
            return [], None

    sources = [
        _mk_source(tmp_path, "a", ["Claim from source A."]),
        _mk_source(tmp_path, "b", ["Claim from source B about completely different topic."]),
    ]
    graph = await ShadowMapper(config, _SequentialLLM()).build(sources, thesis="Test thesis.")
    assert graph.thesis_statement == "Test thesis."
    # 2 sources × 1 extracted claim each = 2 claims
    assert len(graph.claims) == 2
    assert len(graph.sections) >= 1
