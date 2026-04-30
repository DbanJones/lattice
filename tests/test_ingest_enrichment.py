"""Tests for the whole-document annotator pass: argued thesis + importance.

Also covers the new ClusterRole.narrative enum value and the model-level
defaults for the new fields.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    AuthorGraph,
    Claim,
    ClaimType,
    ClusterRole,
    Confidence,
    Section,
    SectionRole,
)
from lattice.ingester.annotator import ContextualAnnotator
from lattice.utils.config import Config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _config(tmp_path: Path) -> Config:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    return Config.load(tmp_path)


# ─── Model-level defaults ────────────────────────────

def test_cluster_role_narrative_exists() -> None:
    """The narrative role must be a valid ClusterRole enum value."""
    assert ClusterRole.narrative.value == "narrative"
    assert "narrative" in {r.value for r in ClusterRole}


def test_claim_default_importance_is_half() -> None:
    """Importance defaults to 0.5 — not opinionated until the LLM scores it."""
    now = _now()
    claim = Claim(
        claim_id="cl.x.1", statement="x", type=ClaimType.empirical,
        confidence=Confidence.medium,
        created_by="t", created_at=now, modified_at=now,
    )
    assert claim.importance == 0.5


def test_authorgraph_thesis_argued_defaults_to_none() -> None:
    """Argued thesis is None until the annotator pass runs."""
    now = _now()
    graph = AuthorGraph(
        project_name="t", created_at=now, modified_at=now,
    )
    assert graph.thesis_argued is None
    assert graph.thesis_argued_confidence is None
    assert graph.thesis_argued_note is None


# ─── _derive_thesis_and_importance ───────────────────

class _StubLLM:
    """Returns a single canned payload for the thesis+importance call."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, model=None, temperature=0.2):
        self.calls.append((system, user))
        return self.payload, None


def _seed_graph_with_claims(claim_orders: list[int]) -> AuthorGraph:
    now = _now()
    claims = [
        Claim(
            claim_id=f"cl.x.{i}",
            statement=f"Claim {i} body.",
            source_order=order,
            type=ClaimType.empirical,
            confidence=Confidence.medium,
            section_id="s.x",
            created_by="t", created_at=now, modified_at=now,
        )
        for i, order in enumerate(claim_orders, start=1)
    ]
    section = Section(
        section_id="s.x", title="X", position=1,
        role=SectionRole.argumentative,
        claim_ids=[c.claim_id for c in claims],
    )
    return AuthorGraph(
        project_name="test",
        thesis_statement="Heading thesis text.",
        sections=[section], claims=claims, relationships=[],
        created_at=now, modified_at=now,
    )


async def test_derive_thesis_and_importance_writes_argued_thesis(tmp_path: Path) -> None:
    config = _config(tmp_path)
    graph = _seed_graph_with_claims([1, 2, 3])
    payload = {
        "thesis_argued": {
            "statement": "What the body actually argues.",
            "confidence": 0.85,
            "diverges_from_heading": True,
            "note": "The heading talks about X but body argues Y.",
        },
        "importance": [
            {"claim_id": "cl.x.1", "score": 0.9},
            {"claim_id": "cl.x.2", "score": 0.4},
            {"claim_id": "cl.x.3", "score": 0.2},
        ],
    }
    annotator = ContextualAnnotator(config, llm=_StubLLM(payload))
    await annotator._derive_thesis_and_importance(graph)

    assert graph.thesis_argued == "What the body actually argues."
    assert graph.thesis_argued_confidence == pytest.approx(0.85)
    assert graph.thesis_argued_note == "The heading talks about X but body argues Y."

    importance = {c.claim_id: c.importance for c in graph.claims}
    assert importance["cl.x.1"] == pytest.approx(0.9)
    assert importance["cl.x.2"] == pytest.approx(0.4)
    assert importance["cl.x.3"] == pytest.approx(0.2)


async def test_derive_thesis_and_importance_clamps_out_of_range_scores(tmp_path: Path) -> None:
    config = _config(tmp_path)
    graph = _seed_graph_with_claims([1, 2])
    payload = {
        "thesis_argued": {"statement": "x", "confidence": 1.5, "note": ""},
        "importance": [
            {"claim_id": "cl.x.1", "score": 1.7},   # over
            {"claim_id": "cl.x.2", "score": -0.4},  # under
        ],
    }
    annotator = ContextualAnnotator(config, llm=_StubLLM(payload))
    await annotator._derive_thesis_and_importance(graph)

    assert graph.thesis_argued_confidence == 1.0
    importance = {c.claim_id: c.importance for c in graph.claims}
    assert importance["cl.x.1"] == 1.0
    assert importance["cl.x.2"] == 0.0


async def test_derive_thesis_and_importance_noop_without_llm(tmp_path: Path) -> None:
    """No LLM client → method returns silently, defaults intact."""
    config = _config(tmp_path)
    graph = _seed_graph_with_claims([1, 2])
    annotator = ContextualAnnotator(config, llm=None)
    await annotator._derive_thesis_and_importance(graph)

    assert graph.thesis_argued is None
    assert all(c.importance == 0.5 for c in graph.claims)


async def test_derive_thesis_and_importance_noop_on_empty_graph(tmp_path: Path) -> None:
    """No claims → no LLM call, no error."""
    config = _config(tmp_path)
    now = _now()
    graph = AuthorGraph(project_name="t", created_at=now, modified_at=now)
    stub = _StubLLM({})
    annotator = ContextualAnnotator(config, llm=stub)
    await annotator._derive_thesis_and_importance(graph)

    assert stub.calls == []
    assert graph.thesis_argued is None


async def test_derive_thesis_and_importance_skips_thesis_claim(tmp_path: Path) -> None:
    """The thesis claim is the target of importance, not a contributor —
    excluded from the LLM input so it doesn't get scored against itself."""
    config = _config(tmp_path)
    now = _now()
    thesis_claim = Claim(
        claim_id="cl.thesis", statement="Thesis text.", source_order=1,
        type=ClaimType.user_synthesis, confidence=Confidence.high,
        author_origin=True, section_id="s.thesis",
        created_by="t", created_at=now, modified_at=now,
    )
    other_claim = Claim(
        claim_id="cl.x.1", statement="Body claim.", source_order=2,
        type=ClaimType.empirical, confidence=Confidence.medium,
        section_id="s.x",
        created_by="t", created_at=now, modified_at=now,
    )
    graph = AuthorGraph(
        project_name="t",
        thesis_statement="Thesis text.",
        sections=[Section(
            section_id="s.x", title="X", position=1,
            role=SectionRole.argumentative,
            claim_ids=["cl.x.1"],
        )],
        claims=[thesis_claim, other_claim], relationships=[],
        created_at=now, modified_at=now,
    )

    stub = _StubLLM({
        "thesis_argued": {"statement": "argued", "confidence": 0.7, "note": ""},
        "importance": [{"claim_id": "cl.x.1", "score": 0.6}],
    })
    annotator = ContextualAnnotator(config, llm=stub)
    await annotator._derive_thesis_and_importance(graph)

    # cl.thesis must not appear in the user message digest.
    assert len(stub.calls) == 1
    user_msg = stub.calls[0][1]
    assert "cl.thesis" not in user_msg
    assert "cl.x.1" in user_msg
    # Thesis claim importance unchanged (still 0.5 default).
    by_id = {c.claim_id: c for c in graph.claims}
    assert by_id["cl.thesis"].importance == 0.5
    assert by_id["cl.x.1"].importance == pytest.approx(0.6)


async def test_derive_thesis_and_importance_handles_llm_failure(tmp_path: Path) -> None:
    """If the LLM call raises, defaults are preserved — no crash."""

    class _FailingLLM:
        async def complete_json(self, system, user, model=None, temperature=0.2):
            raise RuntimeError("boom")

    config = _config(tmp_path)
    graph = _seed_graph_with_claims([1, 2])
    annotator = ContextualAnnotator(config, llm=_FailingLLM())
    await annotator._derive_thesis_and_importance(graph)

    assert graph.thesis_argued is None
    assert all(c.importance == 0.5 for c in graph.claims)
