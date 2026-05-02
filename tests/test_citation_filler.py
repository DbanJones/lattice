"""Tests for the citation filler — Phase D."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from lattice.graph.models import (
    Citation, CitationDecision, CitationDiscrepancy,
    CitationDiscrepancySeverity, CitationVerification, CitationVerifier,
    Source, SourceMetadata, SourceType,
)
from lattice.references.filler import (
    apply_decisions,
    append_decisions,
    collect_fill_candidates,
    FillCandidate,
    FillDecision,
    load_decisions,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _src(source_id: str, **kw) -> Source:
    base = {"title": "X", "year": 2020, "authors": ["Smith, J."]}
    base.update(kw)
    return Source(
        source_id=source_id,
        type=SourceType.primary_paper,
        citation=Citation(**base),
        metadata=SourceMetadata(
            date_added=_now(), file_path=f"refs/{source_id}.pdf",
            hash="sha256:abc",
        ),
    )


def _v(source_id: str, *, discrepancies, matched=True) -> CitationVerification:
    return CitationVerification(
        source_id=source_id,
        verifier=CitationVerifier.crossref,
        verified_at=_now(),
        matched=matched,
        canonical=Citation(title="Canonical X", year=2020),
        discrepancies=discrepancies,
        confidence=0.9,
    )


# ─── candidate selection ────────────────────────


def test_unmatched_verifications_yield_no_candidates() -> None:
    v = CitationVerification(
        source_id="s",
        verifier=CitationVerifier.crossref,
        verified_at=_now(),
        matched=False,
    )
    cands = collect_fill_candidates({"s": v})
    assert cands == []


def test_each_discrepancy_becomes_one_candidate() -> None:
    diffs = [
        CitationDiscrepancy(field="year", paper_value="2019",
                            canonical_value="2020",
                            severity=CitationDiscrepancySeverity.error),
        CitationDiscrepancy(field="doi", paper_value="",
                            canonical_value="10.1234/x",
                            severity=CitationDiscrepancySeverity.info),
    ]
    cands = collect_fill_candidates({"s": _v("s", discrepancies=diffs)})
    assert len(cands) == 2


def test_candidates_sorted_by_severity_first() -> None:
    diffs = [
        CitationDiscrepancy(field="doi", paper_value="",
                            canonical_value="10.1234/x",
                            severity=CitationDiscrepancySeverity.info),
        CitationDiscrepancy(field="year", paper_value="2019",
                            canonical_value="2020",
                            severity=CitationDiscrepancySeverity.error),
        CitationDiscrepancy(field="container", paper_value="X",
                            canonical_value="Y",
                            severity=CitationDiscrepancySeverity.warning),
    ]
    cands = collect_fill_candidates({"s": _v("s", discrepancies=diffs)})
    severities = [c.severity for c in cands]
    assert severities == [
        CitationDiscrepancySeverity.error,
        CitationDiscrepancySeverity.warning,
        CitationDiscrepancySeverity.info,
    ]


def test_already_decided_fields_skipped() -> None:
    diffs = [
        CitationDiscrepancy(field="year", paper_value="2019",
                            canonical_value="2020",
                            severity=CitationDiscrepancySeverity.error),
    ]
    decisions = [
        CitationDecision(
            source_id="s", field="year", action="accept_canonical",
            paper_value="2019", canonical_value="2020",
            chosen_value="2020", decided_at=_now(),
            verifier=CitationVerifier.crossref,
        )
    ]
    cands = collect_fill_candidates(
        {"s": _v("s", discrepancies=diffs)}, decided=decisions,
    )
    assert cands == []


def test_severity_floor_filters() -> None:
    diffs = [
        CitationDiscrepancy(field="year", paper_value="2019",
                            canonical_value="2020",
                            severity=CitationDiscrepancySeverity.error),
        CitationDiscrepancy(field="pages", paper_value="",
                            canonical_value="12-19",
                            severity=CitationDiscrepancySeverity.info),
    ]
    cands = collect_fill_candidates(
        {"s": _v("s", discrepancies=diffs)},
        severity_floor=CitationDiscrepancySeverity.warning,
    )
    fields = [c.field for c in cands]
    assert fields == ["year"]  # info-level dropped


def test_candidate_marks_gap_fill() -> None:
    """A discrepancy where paper has nothing but canonical has
    something is a "gap fill" rather than a correction."""
    diff = CitationDiscrepancy(
        field="doi", paper_value="", canonical_value="10.1234/x",
        severity=CitationDiscrepancySeverity.info,
    )
    cands = collect_fill_candidates({"s": _v("s", discrepancies=[diff])})
    assert cands[0].is_gap_fill is True


# ─── apply decisions ────────────────────────────


def test_accept_canonical_writes_canonical_value() -> None:
    src = _src("s", year=2019)
    cand = FillCandidate(
        source_id="s", field="year", paper_value="2019",
        canonical_value="2020",
        severity=CitationDiscrepancySeverity.error,
        verifier=CitationVerifier.crossref,
    )
    updated, log = apply_decisions(
        [src], [FillDecision(candidate=cand, action="accept_canonical")],
    )
    assert updated["s"].citation.year == 2020
    assert log[0].action == "accept_canonical"
    assert log[0].chosen_value == "2020"


def test_reject_keeps_paper_value() -> None:
    src = _src("s", year=2019)
    cand = FillCandidate(
        source_id="s", field="year", paper_value="2019",
        canonical_value="2020",
        severity=CitationDiscrepancySeverity.error,
        verifier=CitationVerifier.crossref,
    )
    updated, log = apply_decisions(
        [src], [FillDecision(candidate=cand, action="reject")],
    )
    assert updated["s"].citation.year == 2019  # untouched
    assert log[0].action == "reject"


def test_manual_override_uses_chosen_value() -> None:
    src = _src("s", year=2019)
    cand = FillCandidate(
        source_id="s", field="year", paper_value="2019",
        canonical_value="2020",
        severity=CitationDiscrepancySeverity.error,
        verifier=CitationVerifier.crossref,
    )
    updated, log = apply_decisions(
        [src],
        [FillDecision(candidate=cand, action="manual_override",
                      chosen_value="2018")],
    )
    assert updated["s"].citation.year == 2018
    assert log[0].chosen_value == "2018"


def test_skip_yields_no_log_entry() -> None:
    src = _src("s")
    cand = FillCandidate(
        source_id="s", field="year", paper_value="2020",
        canonical_value="2021",
        severity=CitationDiscrepancySeverity.warning,
        verifier=CitationVerifier.crossref,
    )
    _, log = apply_decisions([src], [FillDecision(candidate=cand, action="skip")])
    assert log == []


def test_apply_doesnt_mutate_input_sources() -> None:
    src = _src("s", year=2019)
    cand = FillCandidate(
        source_id="s", field="year", paper_value="2019",
        canonical_value="2020",
        severity=CitationDiscrepancySeverity.error,
        verifier=CitationVerifier.crossref,
    )
    apply_decisions(
        [src], [FillDecision(candidate=cand, action="accept_canonical")],
    )
    # Original is untouched.
    assert src.citation.year == 2019


def test_apply_writes_doi_field() -> None:
    src = _src("s", doi=None)
    cand = FillCandidate(
        source_id="s", field="doi", paper_value="",
        canonical_value="10.1234/x",
        severity=CitationDiscrepancySeverity.info,
        verifier=CitationVerifier.crossref,
    )
    updated, _ = apply_decisions(
        [src], [FillDecision(candidate=cand, action="accept_canonical")],
    )
    assert updated["s"].citation.doi == "10.1234/x"


def test_apply_writes_authors_split_on_comma() -> None:
    src = _src("s", authors=["Smith, J."])
    cand = FillCandidate(
        source_id="s", field="authors",
        paper_value="Smith, J.", canonical_value="Smith, John, Jones, Kira",
        severity=CitationDiscrepancySeverity.warning,
        verifier=CitationVerifier.crossref,
    )
    updated, _ = apply_decisions(
        [src], [FillDecision(candidate=cand, action="accept_canonical")],
    )
    # Comma-split — authors is a list of names.
    assert "Smith" in updated["s"].citation.authors[0]


# ─── decision log persistence ───────────────────


def test_decision_log_round_trip(tmp_path: Path) -> None:
    (tmp_path / ".lattice").mkdir()
    decisions = [
        CitationDecision(
            source_id="s1", field="year", action="accept_canonical",
            paper_value="2019", canonical_value="2020",
            chosen_value="2020", decided_at=_now(),
            verifier=CitationVerifier.crossref,
        ),
    ]
    append_decisions(tmp_path, decisions)
    loaded = load_decisions(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].source_id == "s1"


def test_append_decisions_is_additive(tmp_path: Path) -> None:
    (tmp_path / ".lattice").mkdir()
    d1 = CitationDecision(
        source_id="s1", field="year", action="accept_canonical",
        paper_value="2019", canonical_value="2020", chosen_value="2020",
        decided_at=_now(), verifier=CitationVerifier.crossref,
    )
    d2 = CitationDecision(
        source_id="s2", field="title", action="reject",
        paper_value="X", canonical_value="Y", chosen_value="X",
        decided_at=_now(), verifier=CitationVerifier.crossref,
    )
    append_decisions(tmp_path, [d1])
    append_decisions(tmp_path, [d2])
    loaded = load_decisions(tmp_path)
    assert len(loaded) == 2
    assert {d.source_id for d in loaded} == {"s1", "s2"}


def test_load_decisions_empty_when_missing(tmp_path: Path) -> None:
    assert load_decisions(tmp_path) == []
