"""Tests for source-order tracking and the OrderingCheck auditor.

Together these enforce the invariant that the rendered paper preserves
the order the author wrote — closing the bug that produced
'Gap 4 heading after Gap 4 content'.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.auditor.ordering import OrderingCheck
from lattice.graph.models import (
    AuthorGraph,
    Claim,
    ClaimRoleInCluster,
    ClaimType,
    Cluster,
    ClusterRole,
    Confidence,
    Section,
    SectionRole,
)
from lattice.graph.store import GraphStore
from lattice.ingester.annotator import ContextualAnnotator
from lattice.ingester.markdown import MarkdownOutlineIngester
from lattice.utils.config import Config
from lattice.voice.parser import Voice


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _academic_voice() -> Voice:
    return Voice.from_file(
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )


def _config(tmp_path: Path) -> Config:
    (tmp_path / "config.yml").write_text("default_voice: academic\n", encoding="utf-8")
    return Config.load(tmp_path)


# ─── 1. Ingester assigns monotonic source_order ───────────

async def test_markdown_ingester_assigns_monotonic_source_order(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ingester = MarkdownOutlineIngester(config)
    outline_path = tmp_path / "outline.md"
    outline_path.write_text(
        "# THESIS\n\nThe central claim.\n\n"
        "# A. First section\n\n"
        "  - First claim [ref: src_1]\n"
        "  - Second claim [ref: src_2]\n\n"
        "# B. Second section\n\n"
        "  - Third claim [ref: src_3]\n"
        "  - Fourth claim [ref: src_4]\n",
        encoding="utf-8",
    )
    graph = await ingester.ingest(outline_path, project_name="test")

    by_id = {c.claim_id: c for c in graph.claims}
    # Thesis comes first (source_order = 1).
    assert by_id["cl.thesis"].source_order == 1
    # Then A.1, A.2, B.1, B.2 in order.
    assert by_id["cl.a.1"].source_order == 2
    assert by_id["cl.a.2"].source_order == 3
    assert by_id["cl.b.1"].source_order == 4
    assert by_id["cl.b.2"].source_order == 5

    # Each section's claim_ids preserve source order at ingest time.
    section_a = next(s for s in graph.sections if s.section_id == "s.a")
    section_b = next(s for s in graph.sections if s.section_id == "s.b")
    assert [by_id[cid].source_order for cid in section_a.claim_ids] == [2, 3]
    assert [by_id[cid].source_order for cid in section_b.claim_ids] == [4, 5]


# ─── 2. Annotator restores order if mutated ───────────────

async def test_normalise_claim_order_restores_scrambled_section(tmp_path: Path) -> None:
    config = _config(tmp_path)
    annotator = ContextualAnnotator(config, llm=None)

    now = _now()
    claims = [
        Claim(
            claim_id=f"cl.x.{i}",
            statement=f"Claim {i}",
            source_order=i,
            type=ClaimType.empirical,
            confidence=Confidence.medium,
            section_id="s.x",
            created_by="t", created_at=now, modified_at=now,
        )
        for i in range(1, 5)
    ]
    section = Section(
        section_id="s.x", title="X", position=1, role=SectionRole.argumentative,
        claim_ids=["cl.x.4", "cl.x.1", "cl.x.3", "cl.x.2"],
    )
    graph = AuthorGraph(
        project_name="test",
        sections=[section],
        claims=claims,
        relationships=[],
        created_at=now, modified_at=now,
    )

    annotator._normalise_claim_order(graph)

    assert section.claim_ids == ["cl.x.1", "cl.x.2", "cl.x.3", "cl.x.4"]


async def test_normalise_claim_order_legacy_keeps_existing_order(tmp_path: Path) -> None:
    """Claims with source_order=0 retain their relative order (stable sort)."""
    config = _config(tmp_path)
    annotator = ContextualAnnotator(config, llm=None)

    now = _now()
    claims = [
        Claim(
            claim_id=f"cl.x.{i}",
            statement=f"Claim {i}",
            source_order=0,  # legacy
            type=ClaimType.empirical,
            confidence=Confidence.medium,
            section_id="s.x",
            created_by="t", created_at=now, modified_at=now,
        )
        for i in range(1, 4)
    ]
    section = Section(
        section_id="s.x", title="X", position=1, role=SectionRole.argumentative,
        claim_ids=["cl.x.3", "cl.x.1", "cl.x.2"],
    )
    graph = AuthorGraph(
        project_name="test",
        sections=[section], claims=claims, relationships=[],
        created_at=now, modified_at=now,
    )

    annotator._normalise_claim_order(graph)

    # All zeros → stable sort preserves the as-given order.
    assert section.claim_ids == ["cl.x.3", "cl.x.1", "cl.x.2"]


# ─── 3. OrderingCheck flags violations ────────────────────

def _seed_store(tmp_path: Path, *, claim_orders: list[int], claim_id_order: list[str]) -> GraphStore:
    """Build a minimal store with one section and len(orders) claims."""
    (tmp_path / "config.yml").write_text("default_voice: academic\n", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    now = _now()
    claims = [
        Claim(
            claim_id=f"cl.x.{i+1}",
            statement=f"Claim {i+1}",
            source_order=order,
            type=ClaimType.empirical,
            confidence=Confidence.medium,
            section_id="s.x",
            created_by="t", created_at=now, modified_at=now,
        )
        for i, order in enumerate(claim_orders)
    ]
    for c in claims:
        store.save_claim(c)
    section = Section(
        section_id="s.x", title="X", position=1, role=SectionRole.argumentative,
        claim_ids=list(claim_id_order),
    )
    store.save_section(section)
    return store


def test_ordering_check_passes_on_correct_order(tmp_path: Path) -> None:
    store = _seed_store(
        tmp_path,
        claim_orders=[1, 2, 3, 4],
        claim_id_order=["cl.x.1", "cl.x.2", "cl.x.3", "cl.x.4"],
    )
    report = OrderingCheck(store, _academic_voice()).check()
    assert report.is_ordered
    assert report.flags == []


def test_ordering_check_flags_out_of_order_claim_ids(tmp_path: Path) -> None:
    store = _seed_store(
        tmp_path,
        claim_orders=[1, 2, 3, 4],
        # Author wrote 1,2,3,4 but section.claim_ids was scrambled.
        claim_id_order=["cl.x.1", "cl.x.4", "cl.x.2", "cl.x.3"],
    )
    report = OrderingCheck(store, _academic_voice()).check()
    assert not report.is_ordered
    assert any(f.rule_id == "ordering.claim_ids_out_of_order" for f in report.flags)


def test_ordering_check_legacy_graph_is_advisory_only(tmp_path: Path) -> None:
    """All-zero source_order → no flags, single advisory note."""
    store = _seed_store(
        tmp_path,
        claim_orders=[0, 0, 0, 0],
        claim_id_order=["cl.x.4", "cl.x.1", "cl.x.3", "cl.x.2"],
    )
    report = OrderingCheck(store, _academic_voice()).check()
    assert report.is_ordered  # not blocking
    assert report.flags == []
    assert any("source_order=0" in n for n in report.notes)


def test_ordering_check_flags_interleaved_clusters(tmp_path: Path) -> None:
    """Cluster A holds claims [1, 3]; cluster B holds claims [2, 4] — interleaved."""
    (tmp_path / "config.yml").write_text("default_voice: academic\n", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    now = _now()
    claims = [
        Claim(
            claim_id=f"cl.x.{i}",
            statement=f"Claim {i}",
            source_order=i,
            type=ClaimType.empirical, confidence=Confidence.medium,
            section_id="s.x",
            created_by="t", created_at=now, modified_at=now,
        )
        for i in range(1, 5)
    ]
    for c in claims:
        store.save_claim(c)
    store.save_section(Section(
        section_id="s.x", title="X", position=1, role=SectionRole.argumentative,
        claim_ids=["cl.x.1", "cl.x.2", "cl.x.3", "cl.x.4"],
    ))
    # Cluster A: claims 1 and 3 (span 1..3)
    # Cluster B: claims 2 and 4 (span 2..4)  → starts inside A's span
    store.save_cluster(Cluster(
        cluster_id="c.x.1", section_id="s.x", position=1, role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence),
            ClaimRoleInCluster(claim_id="cl.x.3", role_in_cluster=ClusterRole.evidence),
        ],
    ))
    store.save_cluster(Cluster(
        cluster_id="c.x.2", section_id="s.x", position=2, role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(claim_id="cl.x.2", role_in_cluster=ClusterRole.evidence),
            ClaimRoleInCluster(claim_id="cl.x.4", role_in_cluster=ClusterRole.evidence),
        ],
    ))
    report = OrderingCheck(store, _academic_voice()).check()
    assert not report.is_ordered
    assert any(f.rule_id == "ordering.clusters_interleaved" for f in report.flags)


# ─── 4. End-to-end: ingest → assembler renders in source order ────

async def test_assembler_renders_in_source_order_even_if_claim_ids_scrambled(tmp_path: Path) -> None:
    """Belt-and-braces: assembler sorts by source_order before clustering.

    Even if a buggy downstream pass scrambles section.claim_ids, the
    assembler's defensive sort produces clusters in source order.
    """
    from lattice.renderer.assembler import Assembler

    config = _config(tmp_path)
    store = GraphStore.load(tmp_path)
    voice = _academic_voice()
    now = _now()

    claims = [
        Claim(
            claim_id=f"cl.x.{i}",
            statement=f"Claim {i} body words.",
            source_order=i,
            type=ClaimType.user_synthesis,
            confidence=Confidence.high,
            author_origin=True,
            section_id="s.x",
            created_by="t", created_at=now, modified_at=now,
        )
        for i in range(1, 5)
    ]
    for c in claims:
        store.save_claim(c)
    # Save section with scrambled claim_ids.
    store.save_section(Section(
        section_id="s.x", title="X", position=1, role=SectionRole.argumentative,
        claim_ids=["cl.x.4", "cl.x.1", "cl.x.3", "cl.x.2"],
    ))

    assembler = Assembler(config, store, llm=None, voice=voice)
    clusters = await assembler.build_plan()

    # Clusters within s.x should reflect source-order [1, 2, 3, 4]
    section_clusters = sorted(
        (c for c in clusters if c.section_id == "s.x"),
        key=lambda c: c.position,
    )
    flat_claim_ids = [
        entry.claim_id
        for cluster in section_clusters
        for entry in cluster.claim_sequence
    ]
    assert flat_claim_ids == ["cl.x.1", "cl.x.2", "cl.x.3", "cl.x.4"]
