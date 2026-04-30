"""Tests for the Enricher (claim → passage binding)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.enricher.binder import Enricher
from lattice.graph.models import (
    BindingStrength,
    Citation,
    Claim,
    ClaimType,
    Confidence,
    Evidence,
    Passage,
    PassageLocation,
    PassageType,
    Source,
    SourceMetadata,
    SourceType,
)
from lattice.graph.store import GraphStore
from lattice.utils.config import Config


class _StubLLM:
    """Mock that returns a predetermined payload for every complete_json call."""
    def __init__(self, payload: dict | Exception) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, model=None, temperature=0.2):
        self.calls.append((system, user))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload, None


def _make_store_with_data(tmp_path: Path) -> tuple[GraphStore, Claim]:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    now = datetime.now(timezone.utc)

    source = Source(
        source_id="koomey_2015",
        type=SourceType.primary_paper,
        citation=Citation(authors=["Koomey"], year=2015, title="Energy trends"),
        passages=[
            Passage(
                id="p.3.2",
                text="Koomey's Law doubling period lengthened from 1.5 to 2.6 years.",
                location=PassageLocation(page=3, paragraph=2),
                type=PassageType.claim,
                char_count=60,
            ),
            Passage(
                id="p.4.1",
                text="Efficiency gains have plateaued in many workloads.",
                location=PassageLocation(page=4, paragraph=1),
                type=PassageType.claim,
                char_count=50,
            ),
        ],
        metadata=SourceMetadata(
            date_added=now, file_path="refs/papers/koomey_2015.pdf", hash="sha256:xyz"
        ),
    )
    store.save_source(source)

    claim = Claim(
        claim_id="cl.c.2",
        statement="Koomey's Law slowdown accelerated in the 2010s.",
        type=ClaimType.empirical,
        confidence=Confidence.high,
        evidence=[Evidence(source="koomey_2015", passage="", binding_strength=BindingStrength.weak)],
        created_by="test",
        created_at=now,
        modified_at=now,
    )
    store.save_claim(claim)
    return store, claim


async def test_enricher_updates_claim_with_strong_binding(tmp_path: Path) -> None:
    store, _claim = _make_store_with_data(tmp_path)
    config = Config.load(tmp_path)
    llm = _StubLLM({
        "binding_strength": "strong",
        "best_passage_id": "p.3.2",
        "rationale": "Direct statement of the doubling period lengthening.",
        "extracted_quote": "doubling period lengthened from 1.5 to 2.6 years",
        "page": 3,
    })
    updated = await Enricher(config, store, llm).enrich_all()
    assert updated == 1
    refreshed = store.get_claim("cl.c.2")
    assert len(refreshed.evidence) == 1
    ev = refreshed.evidence[0]
    assert ev.binding_strength == BindingStrength.strong
    assert ev.passage == "p.3.2"
    assert ev.page == 3
    assert ev.quote_verbatim
    assert "doubling period" in (ev.quote_text or "")


async def test_enricher_rejects_passage_id_not_in_source(tmp_path: Path) -> None:
    store, _ = _make_store_with_data(tmp_path)
    config = Config.load(tmp_path)
    llm = _StubLLM({
        "binding_strength": "strong",
        "best_passage_id": "p.999.1",  # doesn't exist
        "rationale": "bad",
        "extracted_quote": "x",
        "page": 1,
    })
    await Enricher(config, store, llm).enrich_all()
    refreshed = store.get_claim("cl.c.2")
    assert refreshed.evidence[0].binding_strength == BindingStrength.none_
    assert refreshed.evidence[0].passage == ""


async def test_enricher_handles_unknown_source(tmp_path: Path) -> None:
    store, _ = _make_store_with_data(tmp_path)
    now = datetime.now(timezone.utc)
    store.save_claim(
        Claim(
            claim_id="cl.unknown",
            statement="Claim citing a missing source.",
            type=ClaimType.empirical,
            confidence=Confidence.medium,
            evidence=[Evidence(source="missing_2099", passage="")],
            created_by="test", created_at=now, modified_at=now,
        )
    )
    config = Config.load(tmp_path)
    llm = _StubLLM({"binding_strength": "strong", "best_passage_id": "p.1.1"})
    await Enricher(config, store, llm).enrich_all()
    refreshed = store.get_claim("cl.unknown")
    assert refreshed.evidence[0].binding_strength == BindingStrength.none_
    # The stub should NOT have been called for the missing source.
    # (But it may have been called for the other claim.)
    assert all("missing_2099" not in c[1] for c in llm.calls)


async def test_enricher_survives_llm_exception(tmp_path: Path) -> None:
    store, _ = _make_store_with_data(tmp_path)
    config = Config.load(tmp_path)
    llm = _StubLLM(RuntimeError("model unavailable"))
    await Enricher(config, store, llm).enrich_all()
    refreshed = store.get_claim("cl.c.2")
    assert refreshed.evidence[0].binding_strength == BindingStrength.none_
    assert "enricher_error" in (refreshed.evidence[0].quote_text or "")
