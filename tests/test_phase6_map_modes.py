"""Phase 6 — advanced visual map modes + bidirectional selection sync.

The mode picker is a JS overlay on the served HTML, so backend tests
exercise:
- The graph-viz endpoint accepts ``?mode=`` and threads it to the JS.
- ``render_html`` exposes ``MAP_MODES`` and includes the mode in
  ``data.meta.mode`` for client-side overlay logic.
- The mode picker UI element is in the rendered HTML.
- The postMessage bridge JS (set-mode + select-claim + node-tapped)
  is shipped in the rendered HTML.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lattice.output.visualise import MAP_MODES, render_html
from lattice.web.app import create_app


# Reuse the seeded project fixture pattern from test_web.
@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from tests.test_web import _seed_project
    _seed_project(tmp_path, "demo")
    return TestClient(create_app(projects_root=tmp_path))


def test_map_modes_constant_lists_six_modes() -> None:
    assert "default" in MAP_MODES
    assert "thesis_support_path" in MAP_MODES
    assert "section_proof_chain" in MAP_MODES
    assert "weak_evidence_zones" in MAP_MODES
    assert "counterargument_map" in MAP_MODES
    assert "unrenderable_clusters" in MAP_MODES
    assert len(MAP_MODES) == 6


def test_render_html_default_mode_in_meta() -> None:
    from datetime import datetime, timezone
    from lattice.graph.models import (
        AuthorGraph, Claim, ClaimType, Confidence, Section, SectionRole,
    )
    now = datetime.now(timezone.utc)
    graph = AuthorGraph(
        project_name="t",
        thesis_statement="The thesis.",
        sections=[Section(
            section_id="s.x", title="X", position=1,
            role=SectionRole.argumentative, claim_ids=["cl.x.1"],
        )],
        claims=[Claim(
            claim_id="cl.x.1", statement="A claim.",
            type=ClaimType.empirical, confidence=Confidence.high,
            section_id="s.x", created_by="t",
            created_at=now, modified_at=now,
        )],
        relationships=[],
        created_at=now, modified_at=now,
    )
    html = render_html(graph, mode="thesis_support_path")
    # The meta payload should record the requested mode.
    assert '"mode": "thesis_support_path"' in html or '"mode":"thesis_support_path"' in html


def test_render_html_mode_picker_ui_present() -> None:
    from datetime import datetime, timezone
    from lattice.graph.models import AuthorGraph
    now = datetime.now(timezone.utc)
    graph = AuthorGraph(
        project_name="t", sections=[], claims=[], relationships=[],
        created_at=now, modified_at=now,
    )
    html = render_html(graph)
    # Picker element + every mode option must ship.
    assert 'id="map-mode-picker"' in html
    for mode in MAP_MODES:
        assert f'value="{mode}"' in html


def test_render_html_postmessage_bridge_present() -> None:
    """The iframe ↔ cockpit selection sync depends on three message
    kinds. The rendered HTML must wire them all."""
    from datetime import datetime, timezone
    from lattice.graph.models import AuthorGraph
    now = datetime.now(timezone.utc)
    graph = AuthorGraph(
        project_name="t", sections=[], claims=[], relationships=[],
        created_at=now, modified_at=now,
    )
    html = render_html(graph)
    assert "lattice:set-mode" in html
    assert "lattice:select-claim" in html
    assert "lattice:node-tapped" in html


def test_render_html_mode_overlay_helpers_present() -> None:
    """Each mode is implemented by a dedicated ``compute*`` helper.
    They must all ship — the mode picker calls them by name."""
    from datetime import datetime, timezone
    from lattice.graph.models import AuthorGraph
    now = datetime.now(timezone.utc)
    graph = AuthorGraph(
        project_name="t", sections=[], claims=[], relationships=[],
        created_at=now, modified_at=now,
    )
    html = render_html(graph)
    assert "computeThesisSupportPath" in html
    assert "computeSectionProofChain" in html
    assert "computeWeakEvidenceZones" in html
    assert "computeCounterargumentMap" in html
    assert "computeUnrenderableClusters" in html


def test_render_html_invalid_mode_falls_back_to_default() -> None:
    from datetime import datetime, timezone
    from lattice.graph.models import AuthorGraph
    now = datetime.now(timezone.utc)
    graph = AuthorGraph(
        project_name="t", sections=[], claims=[], relationships=[],
        created_at=now, modified_at=now,
    )
    html = render_html(graph, mode="nonsense_mode_that_does_not_exist")
    assert '"mode": "default"' in html or '"mode":"default"' in html


def test_graph_viz_endpoint_accepts_mode_query_param(client: TestClient) -> None:
    """The endpoint accepts ``?mode=`` even though the cache is
    mode-agnostic — the JS reads the URL param at runtime."""
    resp = client.get(
        "/api/projects/demo/graph-viz?mode=weak_evidence_zones",
    )
    assert resp.status_code == 200
    assert "id=\"map-mode-picker\"" in resp.text


def test_graph_viz_endpoint_unknown_mode_does_not_500(
    client: TestClient,
) -> None:
    resp = client.get("/api/projects/demo/graph-viz?mode=does_not_exist")
    assert resp.status_code == 200


def test_render_html_meta_lists_available_modes() -> None:
    """The frontend can build a mode picker from the data without
    hardcoding the list."""
    from datetime import datetime, timezone
    from lattice.graph.models import AuthorGraph
    now = datetime.now(timezone.utc)
    graph = AuthorGraph(
        project_name="t", sections=[], claims=[], relationships=[],
        created_at=now, modified_at=now,
    )
    html = render_html(graph)
    for mode in MAP_MODES:
        # Each mode appears at least once in the embedded JSON
        # payload (as part of ``available_modes``).
        assert mode in html
