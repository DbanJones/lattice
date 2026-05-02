"""Phase 1 tests: richer scaffold ingest, scaffold report, malformed-tag diagnostics.

Companion to ``test_ingester.py`` (which covers the legacy tag vocabulary).
These tests cover the additions made in Phase 1: explicit ``[type:]``,
``[importance:]``, ``[scope:]``, ``[evidence_status:]``, the new relationship
tags ``[qualifies:]`` / ``[extends:]`` / ``[depends_on:]`` / ``[pivot:]``, and
the per-claim diagnostic report.
"""
from __future__ import annotations

import json
from pathlib import Path

from lattice.graph.models import (
    ClaimType,
    EvidenceStatus,
    RelationshipType,
)
from lattice.ingester.markdown import MarkdownOutlineIngester
from lattice.utils.config import Config


async def _ingest(outline: str, tmp_path: Path) -> tuple:
    (tmp_path / "config.yml").write_text(
        "default_voice: academic\n", encoding="utf-8"
    )
    config = Config.load(tmp_path)
    ingester = MarkdownOutlineIngester(config)
    outline_path = tmp_path / "outline.md"
    outline_path.write_text(outline, encoding="utf-8")
    graph = await ingester.ingest(outline_path, project_name="test")
    return graph, ingester


# ─── [type: ...] tag ───────────────────────────────────


async def test_explicit_type_tag_overrides_default(tmp_path: Path) -> None:
    """A bullet without MY VIEW prefix should default to empirical, but
    ``[type: definition]`` should take precedence."""
    graph, _ = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - A definition. [type: definition]\n"
        "  - An empirical claim. [type: empirical]\n"
        "  - A methodological claim. [type: methodological]\n"
        "  - A normative claim. [type: normative]\n"
        "  - A synthesis. [type: user_synthesis]\n",
        tmp_path,
    )
    by_id = {c.claim_id: c for c in graph.claims}
    assert by_id["cl.a.1"].type == ClaimType.definition
    assert by_id["cl.a.2"].type == ClaimType.empirical
    assert by_id["cl.a.3"].type == ClaimType.methodological
    assert by_id["cl.a.4"].type == ClaimType.normative
    assert by_id["cl.a.5"].type == ClaimType.user_synthesis


async def test_unknown_type_tag_warns_and_keeps_default(tmp_path: Path) -> None:
    """Malformed tags should fail loudly via a scaffold warning rather than
    silently swallowing the claim."""
    graph, ingester = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - A claim. [type: bogus_type]\n",
        tmp_path,
    )
    # The claim still exists (we don't drop it).
    claim = graph.claims[-1]
    # Falls back to the prefix-derived default (empirical here).
    assert claim.type == ClaimType.empirical
    # And the report has the warning.
    warning_codes = {w.code for w in ingester.last_report.warnings}
    assert "unknown_claim_type" in warning_codes


# ─── [importance: ...] tag ─────────────────────────────


async def test_importance_tag_sets_float(tmp_path: Path) -> None:
    graph, _ = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - A claim. [type: empirical] [importance: 0.85]\n",
        tmp_path,
    )
    claim = graph.claims[-1]
    assert claim.importance == 0.85


async def test_malformed_importance_warns_and_uses_default(tmp_path: Path) -> None:
    graph, ingester = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - A claim. [importance: nonsense]\n",
        tmp_path,
    )
    claim = graph.claims[-1]
    assert claim.importance == 0.5  # the model default
    codes = {w.code for w in ingester.last_report.warnings}
    assert "malformed_importance" in codes


async def test_out_of_range_importance_clamps_and_warns(tmp_path: Path) -> None:
    graph, ingester = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - High. [importance: 5.0]\n"
        "  - Low.  [importance: -1.0]\n",
        tmp_path,
    )
    high = next(c for c in graph.claims if c.statement == "High.")
    low = next(c for c in graph.claims if c.statement == "Low.")
    assert high.importance == 1.0
    assert low.importance == 0.0
    codes = [w.code for w in ingester.last_report.warnings]
    assert codes.count("importance_out_of_range") == 2


# ─── [evidence_status: ...] tag ────────────────────────


async def test_evidence_status_tag_recognised(tmp_path: Path) -> None:
    graph, _ = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - With hint. [evidence_status: source_hint] [ref: smith_2020]\n"
        "  - Unbound. [evidence_status: unbound]\n"
        "  - Bound. [evidence_status: bound]\n"
        "  - Default (None).\n",
        tmp_path,
    )
    by_id = {c.claim_id: c for c in graph.claims}
    assert by_id["cl.a.1"].evidence_status == EvidenceStatus.source_hint
    assert by_id["cl.a.2"].evidence_status == EvidenceStatus.unbound
    assert by_id["cl.a.3"].evidence_status == EvidenceStatus.bound
    assert by_id["cl.a.4"].evidence_status is None


async def test_unknown_evidence_status_warns(tmp_path: Path) -> None:
    graph, ingester = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - X. [evidence_status: maybe]\n",
        tmp_path,
    )
    assert graph.claims[-1].evidence_status is None
    codes = {w.code for w in ingester.last_report.warnings}
    assert "unknown_evidence_status" in codes


# ─── [scope: ...] tag ──────────────────────────────────


async def test_scope_tag_populates_scope_conditions(tmp_path: Path) -> None:
    graph, _ = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - A claim. [scope: condition one, condition two]\n",
        tmp_path,
    )
    claim = graph.claims[-1]
    assert claim.scope_conditions == ["condition one", "condition two"]


# ─── new relationship tags ─────────────────────────────


async def test_qualifies_extends_depends_on_pivot_create_relationships(
    tmp_path: Path,
) -> None:
    graph, _ = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Anchor\n\n"
        "  - First claim. [type: empirical]\n"
        "  - Second claim. [qualifies: cl.a.1]\n"
        "  - Third claim. [extends: cl.a.1]\n"
        "  - Fourth claim. [depends_on: cl.a.1]\n"
        "  - Fifth claim. [pivot: cl.a.1]\n",
        tmp_path,
    )
    edges = {(r.from_claim, r.type, r.to_claim) for r in graph.relationships}
    assert ("cl.a.2", RelationshipType.qualifies, "cl.a.1") in edges
    assert ("cl.a.3", RelationshipType.extends, "cl.a.1") in edges
    assert ("cl.a.4", RelationshipType.depends_on, "cl.a.1") in edges
    assert ("cl.a.5", RelationshipType.interpretive_pivot, "cl.a.1") in edges


async def test_pivot_synonym_for_interpretive_pivot(tmp_path: Path) -> None:
    """Both ``[pivot: x]`` and the long form ``[interpretive_pivot: x]``
    produce the same RelationshipType."""
    graph, _ = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - One.\n"
        "  - Two. [pivot: cl.a.1]\n"
        "  - Three. [interpretive_pivot: cl.a.1]\n",
        tmp_path,
    )
    pivots = [
        r for r in graph.relationships
        if r.type == RelationshipType.interpretive_pivot
    ]
    assert len(pivots) == 2
    assert {r.from_claim for r in pivots} == {"cl.a.2", "cl.a.3"}


async def test_unresolved_relationship_target_warns(tmp_path: Path) -> None:
    graph, ingester = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - One. [qualifies: cl.does_not_exist]\n",
        tmp_path,
    )
    codes = {w.code for w in ingester.last_report.warnings}
    assert "unresolved_relationship_target" in codes
    # The relationship is still recorded (the renderer can decide what to
    # do with a dangling edge); the warning is the intent signal.
    assert any(
        r.to_claim == "cl.does_not_exist" for r in graph.relationships
    )


async def test_forward_reference_resolves(tmp_path: Path) -> None:
    """A claim can reference a claim that appears later in the file —
    the parser resolves targets after the full file is read."""
    graph, ingester = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - First. [qualifies: cl.a.2]\n"
        "  - Second.\n",
        tmp_path,
    )
    codes = {w.code for w in ingester.last_report.warnings}
    assert "unresolved_relationship_target" not in codes


# ─── raw prose with citations does not collapse ────────


async def test_raw_prose_with_citations_does_not_become_all_user_synthesis(
    tmp_path: Path,
) -> None:
    """Phase 1's central goal: scaffolds with explicit empirical claims
    and ref tags should NOT collapse to all-user_synthesis, the way the
    legacy normalise_to_user_synthesis recovery path did."""
    graph, _ = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Findings\n\n"
        "  - First empirical finding. [type: empirical] [ref: smith_2020]\n"
        "  - Second empirical finding. [type: empirical] [ref: lee_2019]\n"
        "  - Third empirical finding. [type: empirical] [ref: chen_2021]\n"
        "  - MY VIEW: my own synthesis.\n",
        tmp_path,
    )
    user_synth_count = sum(
        1 for c in graph.claims if c.type == ClaimType.user_synthesis
    )
    # Only the thesis and the MY VIEW bullet should be user_synthesis.
    assert user_synth_count == 2
    # The empirical claims must keep their type.
    empirical = [c for c in graph.claims if c.type == ClaimType.empirical]
    assert len(empirical) == 3


# ─── mechanisms survive ingest ─────────────────────────


async def test_mechanism_tag_survives_ingest(tmp_path: Path) -> None:
    graph, _ = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - A claim. [type: empirical] [mechanism: increased throughput drives Wright's-law decline]\n",
        tmp_path,
    )
    claim = graph.claims[-1]
    assert claim.mechanism is not None
    assert "Wright" in claim.mechanism


# ─── source hints surface before enrichment ────────────


async def test_source_hints_visible_in_scaffold_report(tmp_path: Path) -> None:
    """Before the indexer runs, every [ref:] should show up in the scaffold
    report's per-claim ``unresolved_refs`` list — that's how the author
    tells which sources are missing from the corpus."""
    graph, ingester = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - One. [type: empirical] [ref: smith_2020]\n"
        "  - Two. [type: empirical] [ref: lee_2019, chen_2021]\n",
        tmp_path,
    )
    report = ingester.last_report
    # Map per-claim reports for easy lookup; thesis is also in there.
    by_claim = {cr.claim_id: cr for cr in report.claim_reports}
    assert "cl.a.1" in by_claim
    assert by_claim["cl.a.1"].unresolved_refs == ["smith_2020"]
    assert sorted(by_claim["cl.a.2"].unresolved_refs) == ["chen_2021", "lee_2019"]


async def test_known_source_ids_filter_unresolved_refs(tmp_path: Path) -> None:
    """If a ref resolves to a known indexed source, it should drop off
    the unresolved list when ``save_scaffold_report`` is called with
    ``known_source_ids``."""
    graph, ingester = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - One. [type: empirical] [ref: smith_2020, missing_src]\n",
        tmp_path,
    )
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    written = ingester.save_scaffold_report(
        tmp_path, known_source_ids={"smith_2020"}
    )
    assert written is not None
    persisted = json.loads(written.read_text(encoding="utf-8"))
    # Only the unknown ref should remain.
    by_claim = {
        cr["claim_id"]: cr for cr in persisted["claim_reports"]
    }
    assert by_claim["cl.a.1"]["unresolved_refs"] == ["missing_src"]


# ─── scaffold report basics ────────────────────────────


async def test_scaffold_report_persists_with_per_claim_excerpts(
    tmp_path: Path,
) -> None:
    graph, ingester = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - A statement that gets tags stripped. [type: empirical] [ref: smith_2020]\n",
        tmp_path,
    )
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    path = ingester.save_scaffold_report(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["project_name"] == "test"
    assert payload["counts"]["claims"] >= 2  # thesis + the one claim
    by_claim = {cr["claim_id"]: cr for cr in payload["claim_reports"]}
    assert "cl.a.1" in by_claim
    cr = by_claim["cl.a.1"]
    # The original excerpt preserves the [ref: ...] tag the author wrote.
    assert "[ref: smith_2020]" in cr["original_excerpt"]
    # The extracted statement strips tags.
    assert "[" not in cr["extracted_statement"]


async def test_scaffold_report_counts_summary(tmp_path: Path) -> None:
    graph, ingester = await _ingest(
        "# THESIS\n\nX.\n\n"
        "# A. Foo\n\n"
        "  - One. [type: empirical] [ref: a]\n"
        "  - MY VIEW: synth.\n"
        "  - Two. [type: empirical] [mechanism: a causes b]\n",
        tmp_path,
    )
    counts = ingester.last_report.counts
    assert counts["claims"] == 4  # thesis + 3
    assert counts["claims_user_synthesis"] == 2  # thesis + MY VIEW
    assert counts["claims_with_evidence"] == 1
    assert counts["claims_with_mechanism"] == 1
