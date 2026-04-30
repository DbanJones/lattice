"""Tests for the graph -> outline-markdown serializer."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimType, Confidence, Evidence,
    Relationship, RelationshipStrength, RelationshipType, Section, SectionRole,
)
from lattice.graph.serialize_outline import serialize_graph_to_outline
from lattice.ingester.markdown import MarkdownOutlineIngester
from lattice.utils.config import Config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_serializer_emits_thesis_marker() -> None:
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        thesis_statement="Forecasts diverge because of assumption.",
        sections=[
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
        ],
        claims=[
            Claim(claim_id="cl.thesis", statement="Forecasts diverge because of assumption.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, created_by="t",
                  created_at=now, modified_at=now, tags=["thesis"]),
        ],
        created_at=now, modified_at=now,
    )
    out = serialize_graph_to_outline(graph)
    assert "# THESIS" in out
    assert "Forecasts diverge because of assumption." in out


def test_serializer_marks_references_section_as_skip() -> None:
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        thesis_statement=None,
        sections=[
            Section(section_id="s.a", title="Body", position=1,
                    role=SectionRole.argumentative, claim_ids=["cl.a.1"]),
            Section(section_id="s.b", title="References", position=2,
                    role=SectionRole.references, claim_ids=["cl.b.1"]),
        ],
        claims=[
            Claim(claim_id="cl.a.1", statement="A claim.",
                  type=ClaimType.empirical, confidence=Confidence.medium,
                  section_id="s.a", created_by="t", created_at=now, modified_at=now),
            Claim(claim_id="cl.b.1", statement="Author, Y. (2020). Title.",
                  type=ClaimType.empirical, confidence=Confidence.medium,
                  section_id="s.b", created_by="t", created_at=now, modified_at=now),
        ],
        created_at=now, modified_at=now,
    )
    out = serialize_graph_to_outline(graph)
    assert "# B. References [role: references]" in out


def test_serializer_emits_ref_tag_and_role_tag() -> None:
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        sections=[Section(section_id="s.a", title="Body", position=1,
                          role=SectionRole.argumentative, claim_ids=["cl.a.1"])],
        claims=[
            Claim(claim_id="cl.a.1",
                  statement="Koomey's slowdown documented.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  evidence=[Evidence(source="koomey_2015", passage="",
                                     binding_strength=BindingStrength.weak)],
                  section_id="s.a", created_by="t",
                  created_at=now, modified_at=now,
                  tags=["role:evidence"]),
        ],
        created_at=now, modified_at=now,
    )
    out = serialize_graph_to_outline(graph)
    assert "[ref: koomey_2015]" in out
    assert "[role: evidence]" in out
    assert "[strong]" in out


def test_serializer_emits_my_view_prefix() -> None:
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        sections=[Section(section_id="s.a", title="Body", position=1,
                          role=SectionRole.argumentative, claim_ids=["cl.a.1"])],
        claims=[
            Claim(claim_id="cl.thesis", statement="Thesis.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, created_by="t",
                  created_at=now, modified_at=now),
            Claim(claim_id="cl.a.1",
                  statement="My synthesis claim.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.a", created_by="t",
                  created_at=now, modified_at=now),
        ],
        relationships=[
            Relationship(rel_id="r.1", type=RelationshipType.supports,
                         **{"from": "cl.a.1", "to": "cl.thesis"},
                         strength=RelationshipStrength.direct, note="",
                         created_by="t", created_at=now),
        ],
        created_at=now, modified_at=now,
    )
    out = serialize_graph_to_outline(graph)
    assert "MY VIEW: My synthesis claim" in out


async def test_round_trip_through_markdown_ingester(tmp_path: Path) -> None:
    """Serialize a graph, re-parse the output, confirm sections + claims preserved."""
    now = _now()
    graph = AuthorGraph(
        project_name="t",
        thesis_statement="A thesis in one sentence.",
        sections=[
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="First section", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1", "cl.a.2"]),
        ],
        claims=[
            Claim(claim_id="cl.thesis", statement="A thesis in one sentence.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, created_by="t",
                  created_at=now, modified_at=now, tags=["thesis"]),
            Claim(claim_id="cl.a.1", statement="First body claim.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  evidence=[Evidence(source="smith_2020", passage="",
                                     binding_strength=BindingStrength.weak)],
                  section_id="s.a", created_by="t",
                  created_at=now, modified_at=now,
                  tags=["role:evidence"]),
            Claim(claim_id="cl.a.2", statement="My counter.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.a", created_by="t",
                  created_at=now, modified_at=now),
        ],
        relationships=[
            Relationship(rel_id="r.1", type=RelationshipType.contradicts,
                         **{"from": "cl.a.2", "to": "cl.thesis"},
                         strength=RelationshipStrength.direct, note="",
                         created_by="t", created_at=now),
        ],
        created_at=now, modified_at=now,
    )
    text = serialize_graph_to_outline(graph)
    outline_file = tmp_path / "outline.md"
    outline_file.write_text(text, encoding="utf-8")
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    config = Config.load(tmp_path)
    reparsed = await MarkdownOutlineIngester(config).ingest(outline_file, project_name="t")

    assert reparsed.thesis_statement == "A thesis in one sentence."
    section_titles = {s.section_id: s.title for s in reparsed.sections}
    assert "s.a" in section_titles
    # MY VIEW round-trip: the counter's contradict-thesis relationship comes back.
    counter = next(c for c in reparsed.claims if "counter" in c.statement.lower())
    rels = [r for r in reparsed.relationships if r.from_claim == counter.claim_id]
    assert any(r.type == RelationshipType.contradicts and r.to_claim == "cl.thesis" for r in rels)
