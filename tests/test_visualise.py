"""Tests for the argument graph visualisations."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from lattice.graph.models import (
    AuthorGraph, BindingStrength, Claim, ClaimType, Confidence, Evidence,
    Relationship, RelationshipStrength, RelationshipType, Section, SectionRole,
)
from lattice.output.visualise import (
    render_html, render_mermaid, render_tree, write_html, write_mermaid,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_graph() -> AuthorGraph:
    now = _now()
    return AuthorGraph(
        project_name="t",
        thesis_statement="Forecasts diverge because of assumption.",
        sections=[
            Section(section_id="s.thesis", title="Thesis", position=0,
                    role=SectionRole.introduction, claim_ids=["cl.thesis"]),
            Section(section_id="s.a", title="Body", position=1,
                    role=SectionRole.argumentative,
                    claim_ids=["cl.a.1", "cl.a.2"]),
            Section(section_id="s.b", title="References", position=2,
                    role=SectionRole.references, claim_ids=["cl.b.1"]),
        ],
        claims=[
            Claim(claim_id="cl.thesis", statement="Forecasts diverge because of assumption.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, created_by="t",
                  created_at=now, modified_at=now, tags=["thesis"]),
            Claim(claim_id="cl.a.1", statement="Koomey's slowdown is documented.",
                  type=ClaimType.empirical, confidence=Confidence.high,
                  evidence=[Evidence(source="koomey_2015", passage="",
                                     binding_strength=BindingStrength.weak)],
                  section_id="s.a", created_by="t", created_at=now, modified_at=now,
                  tags=["role:evidence"]),
            Claim(claim_id="cl.a.2", statement="My synthesis claim.",
                  type=ClaimType.user_synthesis, confidence=Confidence.high,
                  author_origin=True, section_id="s.a",
                  created_by="t", created_at=now, modified_at=now,
                  tags=["role:conclusion"]),
            Claim(claim_id="cl.b.1", statement="Author, A. (2020). Title.",
                  type=ClaimType.empirical, confidence=Confidence.medium,
                  section_id="s.b", created_by="t",
                  created_at=now, modified_at=now),
        ],
        relationships=[
            Relationship(rel_id="r.1", type=RelationshipType.supports,
                         **{"from": "cl.a.2", "to": "cl.thesis"},
                         strength=RelationshipStrength.direct, note="",
                         created_by="t", created_at=now),
            Relationship(rel_id="r.2", type=RelationshipType.contradicts,
                         **{"from": "cl.a.1", "to": "cl.a.2"},
                         strength=RelationshipStrength.direct, note="",
                         created_by="t", created_at=now),
        ],
        created_at=now, modified_at=now,
    )


def test_tree_renders_without_error() -> None:
    graph = _make_graph()
    console = Console(record=True)
    render_tree(graph, clusters=[], console=console)
    out = console.export_text()
    assert "Thesis" in out
    assert "cl.a.1" in out
    assert "cl.a.2" in out
    # References section flagged as skipped.
    assert "SKIPPED" in out


def test_mermaid_includes_thesis_and_section_subgraphs() -> None:
    graph = _make_graph()
    out = render_mermaid(graph)
    assert out.startswith("```mermaid")
    assert out.rstrip().endswith("```")
    assert "flowchart TB" in out
    # Sections become subgraphs.
    assert "subgraph s_a" in out
    # Claim nodes use safe IDs.
    assert "cl_a_1" in out
    assert "cl_a_2" in out
    # Edges with labels.
    assert "supports" in out
    assert "contradicts" in out
    # References section marked SKIPPED in label.
    assert "SKIPPED" in out


def test_mermaid_supports_edge_uses_solid_arrow() -> None:
    graph = _make_graph()
    out = render_mermaid(graph)
    # supports = solid arrow `--supports-->`
    assert "--supports-->" in out
    # contradicts = dashed arrow `-..->`
    assert "-.contradicts.->" in out


def test_html_contains_cytoscape_and_elements() -> None:
    graph = _make_graph()
    out = render_html(graph)
    assert "<title>" in out and "argument graph" in out
    assert "cytoscape" in out
    # Element data embedded as JSON
    assert '"id": "cl.thesis"' in out or '"id":"cl.thesis"' in out
    # Section legend present
    assert "section-legend" in out
    # References section is excluded — it shouldn't appear in the embedded data
    assert '"id": "cl.b.1"' not in out and '"id":"cl.b.1"' not in out
    assert '"id": "s.b"' not in out and '"id":"s.b"' not in out
    # Layout switcher present
    assert "layout-picker" in out
    # Renderable sections show up as legend entries
    assert '"title": "Body"' in out or '"title":"Body"' in out


def test_write_files(tmp_path: Path) -> None:
    graph = _make_graph()
    mmd = write_mermaid(graph, tmp_path)
    htm = write_html(graph, tmp_path)
    assert mmd.exists() and mmd.suffix == ".mmd"
    assert htm.exists() and htm.suffix == ".html"
    assert "flowchart" in mmd.read_text(encoding="utf-8")
    assert "<html" in htm.read_text(encoding="utf-8")
