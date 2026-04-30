"""Acceptance tests for Fix 3: enrichment coverage gates render."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.enricher.report import (
    ClaimResolution, CoverageReport, EnrichmentReporter,
)
from lattice.graph.models import (
    BindingStrength, Claim, ClaimType, Confidence, Evidence, Section, SectionRole,
)
from lattice.graph.store import GraphStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mk_store(tmp_path: Path) -> GraphStore:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    return GraphStore.load(tmp_path)


def _mk_claim(
    cid: str,
    *,
    bound: BindingStrength | None = None,
    contradictory: bool = False,
    user_synth: bool = False,
    section_id: str = "s.a",
) -> Claim:
    evidence: list[Evidence] = []
    if contradictory:
        evidence = [Evidence(source="x", passage="p.1.1",
                             binding_strength=BindingStrength.contradictory)]
    elif bound is not None:
        evidence = [Evidence(source="x", passage="p.1.1", binding_strength=bound)]
    return Claim(
        claim_id=cid,
        statement=f"Statement for {cid}",
        type=ClaimType.user_synthesis if user_synth else ClaimType.empirical,
        confidence=Confidence.medium,
        evidence=evidence,
        author_origin=user_synth,
        section_id=section_id,
        created_by="test",
        created_at=_now(), modified_at=_now(),
    )


def _seed_section_and_claims(store: GraphStore, claims: list[Claim]) -> None:
    store.save_section(Section(
        section_id="s.a", title="Body", position=1,
        role=SectionRole.argumentative,
        claim_ids=[c.claim_id for c in claims],
    ))
    for c in claims:
        store.save_claim(c)


# ─── Stats and counts ─────────────────────────────

def test_report_counts_match_graph_state(tmp_path):
    store = _mk_store(tmp_path)
    claims = [
        _mk_claim("cl.1", bound=BindingStrength.strong),
        _mk_claim("cl.2", bound=BindingStrength.strong),
        _mk_claim("cl.3", bound=BindingStrength.weak),
        _mk_claim("cl.4"),                                        # unbound
        _mk_claim("cl.5"),                                        # unbound
        _mk_claim("cl.6", contradictory=True),                    # contradictory
    ]
    _seed_section_and_claims(store, claims)
    report = EnrichmentReporter(store, tmp_path).generate_report()
    assert report.stats.total_claims == 6
    assert report.stats.strong_bindings == 2
    assert report.stats.weak_bindings == 1
    assert report.stats.no_bindings == 2
    assert report.stats.contradictory_bindings == 1


def test_unbound_claims_appear_in_unbound_list(tmp_path):
    store = _mk_store(tmp_path)
    claims = [
        _mk_claim("cl.1", bound=BindingStrength.strong),
        _mk_claim("cl.2"),                                        # unbound
    ]
    _seed_section_and_claims(store, claims)
    report = EnrichmentReporter(store, tmp_path).generate_report()
    ids = [r.claim_id for r in report.unbound]
    assert ids == ["cl.2"]


def test_user_synthesis_with_author_origin_is_not_unbound(tmp_path):
    """Author-grounded claims don't need source bindings — they're not flagged."""
    store = _mk_store(tmp_path)
    claims = [
        _mk_claim("cl.1", user_synth=True),  # user_synthesis + author_origin
        _mk_claim("cl.2"),                   # genuinely unbound empirical
    ]
    _seed_section_and_claims(store, claims)
    report = EnrichmentReporter(store, tmp_path).generate_report()
    ids = [r.claim_id for r in report.unbound]
    assert ids == ["cl.2"]


# ─── Resolution gate ──────────────────────────────

def test_can_proceed_to_render_false_with_pending(tmp_path):
    store = _mk_store(tmp_path)
    _seed_section_and_claims(store, [_mk_claim("cl.1")])
    report = EnrichmentReporter(store, tmp_path).generate_report()
    assert not report.can_proceed_to_render


def test_can_proceed_to_render_true_when_all_resolved(tmp_path):
    store = _mk_store(tmp_path)
    _seed_section_and_claims(store, [_mk_claim("cl.1")])
    reporter = EnrichmentReporter(store, tmp_path)
    reporter.update_resolution("cl.1", ClaimResolution.accept_gap)
    report = reporter.generate_report()
    assert report.can_proceed_to_render


# ─── Resolution effects on the graph ──────────────

def test_mark_as_user_synthesis_changes_claim_type(tmp_path):
    store = _mk_store(tmp_path)
    _seed_section_and_claims(store, [_mk_claim("cl.1")])
    EnrichmentReporter(store, tmp_path).update_resolution(
        "cl.1", ClaimResolution.mark_user_synthesis
    )
    refreshed = store.get_claim("cl.1")
    assert refreshed.type == ClaimType.user_synthesis
    assert refreshed.author_origin is True
    # And it no longer appears as unbound on a fresh report.
    refreshed_report = EnrichmentReporter(store, tmp_path).generate_report()
    assert "cl.1" not in [r.claim_id for r in refreshed_report.unbound]


def test_remove_from_graph_removes_the_claim(tmp_path):
    store = _mk_store(tmp_path)
    _seed_section_and_claims(store, [_mk_claim("cl.1"), _mk_claim("cl.2")])
    EnrichmentReporter(store, tmp_path).update_resolution(
        "cl.1", ClaimResolution.remove_from_graph
    )
    remaining = [c.claim_id for c in store.list_claims()]
    assert "cl.1" not in remaining
    assert "cl.2" in remaining


def test_soften_to_hedged_updates_statement(tmp_path):
    store = _mk_store(tmp_path)
    _seed_section_and_claims(store, [_mk_claim("cl.1")])
    EnrichmentReporter(store, tmp_path).update_resolution(
        "cl.1", ClaimResolution.soften_to_hedged,
        new_statement="Some studies suggest the trend.",
    )
    refreshed = store.get_claim("cl.1")
    assert refreshed.statement == "Some studies suggest the trend."


def test_accept_gap_records_decision_without_changing_claim(tmp_path):
    store = _mk_store(tmp_path)
    _seed_section_and_claims(store, [_mk_claim("cl.1")])
    EnrichmentReporter(store, tmp_path).update_resolution(
        "cl.1", ClaimResolution.accept_gap
    )
    refreshed = store.get_claim("cl.1")
    assert refreshed.type == ClaimType.empirical  # unchanged
    # But the report now sees the decision.
    report = EnrichmentReporter(store, tmp_path).generate_report()
    record = next(r for r in report.unbound if r.claim_id == "cl.1")
    assert record.resolution == ClaimResolution.accept_gap


# ─── Persistence ───────────────────────────────────

def test_decisions_persist_across_reporter_instances(tmp_path):
    store = _mk_store(tmp_path)
    _seed_section_and_claims(store, [_mk_claim("cl.1"), _mk_claim("cl.2")])
    reporter1 = EnrichmentReporter(store, tmp_path)
    reporter1.update_resolution("cl.1", ClaimResolution.accept_gap)

    reporter2 = EnrichmentReporter(store, tmp_path)
    report = reporter2.generate_report()
    record = next(r for r in report.unbound if r.claim_id == "cl.1")
    assert record.resolution == ClaimResolution.accept_gap


def test_decision_log_appends_history(tmp_path):
    store = _mk_store(tmp_path)
    _seed_section_and_claims(store, [_mk_claim("cl.1")])
    reporter = EnrichmentReporter(store, tmp_path)
    reporter.update_resolution("cl.1", ClaimResolution.accept_gap)
    log_path = tmp_path / ".lattice" / "enrichment_decisions.json"
    assert log_path.exists()
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert log[-1]["claim_id"] == "cl.1"
    assert log[-1]["resolution"] == "accept_gap"


def test_save_report_writes_json(tmp_path):
    store = _mk_store(tmp_path)
    _seed_section_and_claims(store, [_mk_claim("cl.1")])
    reporter = EnrichmentReporter(store, tmp_path)
    report = reporter.generate_report()
    reporter.save_report(report)
    saved = json.loads(reporter.report_path.read_text(encoding="utf-8"))
    assert saved["stats"]["no_bindings"] == 1
    assert saved["unbound"][0]["claim_id"] == "cl.1"
