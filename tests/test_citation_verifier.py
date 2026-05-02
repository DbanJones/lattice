"""Tests for the citation verifier — Phase C.

HTTP is mocked via httpx.MockTransport so tests don't hit Crossref /
OpenAlex. Tests cover: DOI lookup, title-search match scoring, per-
field discrepancy detection, cache round-trip, and graceful failure
when the API returns no results.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from lattice.graph.models import (
    Citation, CitationDiscrepancySeverity, CitationVerification,
    CitationVerifier, Source, SourceMetadata, SourceType,
)
from lattice.references.verifier import (
    VerifierConfig,
    _diff_citations,
    _score_match,
    _title_similarity,
    load_verification_cache,
    save_verification_cache,
    verify_sources,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _src(source_id: str, **kwargs) -> Source:
    citation_kwargs = {"title": "X", "authors": [], "year": None}
    citation_kwargs.update(kwargs)
    return Source(
        source_id=source_id,
        type=SourceType.primary_paper,
        citation=Citation(**citation_kwargs),
        passages=[],
        metadata=SourceMetadata(
            date_added=_now(), file_path=f"refs/{source_id}.pdf",
            hash="sha256:abc",
        ),
    )


# ─── pure-function tests (no HTTP) ───────────────


def test_title_similarity_basic() -> None:
    assert _title_similarity("On the Mechanism", "on the mechanism") == 1.0
    assert _title_similarity("On the Mechanism", "Completely different paper") < 0.3
    # Empty / missing.
    assert _title_similarity("", "anything") == 0.0


def test_title_similarity_robust_to_punctuation() -> None:
    a = "Recalibrating Global Data-Center Energy-Use Estimates"
    b = "recalibrating global data center energy use estimates"
    assert _title_similarity(a, b) > 0.9


def test_score_match_doi_short_circuit() -> None:
    paper = Citation(
        title="Anything", doi="10.1126/science.aba3758",
    )
    canon = Citation(
        title="Different", doi="10.1126/science.aba3758",
    )
    assert _score_match(paper, canon, VerifierConfig()) == 1.0


def test_score_match_combines_signals() -> None:
    """Title similarity dominates; year + authors confirm."""
    paper = Citation(
        title="On the Mechanism", year=2020,
        authors=["Smith, J.", "Lee, K."],
    )
    canonical = Citation(
        title="On the Mechanism", year=2020,
        authors=["Smith, John", "Lee, Kira"],
    )
    score = _score_match(paper, canonical, VerifierConfig())
    assert score > 0.95


def test_diff_citations_detects_field_mismatches() -> None:
    paper = Citation(
        title="On the Mechanism", year=2020,
        authors=["Smith, J."], pages="12-19",
    )
    canonical = Citation(
        title="On the Mechanism", year=2021,  # wrong year
        authors=["Smith, J."], pages="12-19",
        doi="10.1126/science.aba3758",  # canonical has DOI; paper doesn't
    )
    diffs = _diff_citations(paper, canonical)
    by_field = {d.field: d for d in diffs}
    assert "year" in by_field
    assert by_field["year"].severity == CitationDiscrepancySeverity.error
    # Missing DOI on paper side → info, not error (filling a gap).
    assert by_field["doi"].severity == CitationDiscrepancySeverity.info


def test_diff_citations_no_diffs_when_identical() -> None:
    c1 = Citation(title="X", year=2020, authors=["Smith, J."])
    c2 = Citation(title="X", year=2020, authors=["Smith, J."])
    assert _diff_citations(c1, c2) == []


def test_author_surname_set_diff_flagged() -> None:
    """When surname sets differ, authors get a discrepancy entry."""
    paper = Citation(title="X", year=2020, authors=["Smith, J."])
    canonical = Citation(
        title="X", year=2020,
        authors=["Smith, John", "Jones, Kira"],  # extra author
    )
    diffs = _diff_citations(paper, canonical)
    assert any(d.field == "authors" for d in diffs)


# ─── HTTP mocked: Crossref DOI lookup ────────────


def _mock_transport(routes: dict[str, dict]) -> httpx.MockTransport:
    """Build a MockTransport that responds based on the URL path."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for prefix, payload in routes.items():
            if prefix in url:
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_crossref_doi_lookup_returns_canonical(monkeypatch) -> None:
    src = _src(
        "smith_2020", title="Old Title", year=2019, authors=["Smith, J."],
        doi="10.1234/test.1",
    )
    routes = {
        "api.crossref.org/works/10.1234/test.1": {
            "message": {
                "DOI": "10.1234/test.1",
                "title": ["Real Title"],
                "author": [{"family": "Smith", "given": "John A."}],
                "issued": {"date-parts": [[2020]]},
                "container-title": ["Nature"],
                "volume": "580",
                "issue": "1",
                "page": "12-19",
            }
        }
    }
    transport = _mock_transport(routes)

    # Patch the AsyncClient to use the mock transport.
    import lattice.references.verifier as verifier_mod
    original = verifier_mod.httpx.AsyncClient

    def _factory(*a, **kw):
        return original(transport=transport, *a, **kw)

    monkeypatch.setattr(verifier_mod.httpx, "AsyncClient", _factory)

    result = await verify_sources([src])
    v = result["smith_2020"]
    assert v.matched is True
    assert v.confidence == 1.0
    assert v.canonical is not None
    assert v.canonical.year == 2020
    assert v.canonical.title == "Real Title"
    # Year discrepancy surfaces as an error.
    year_diff = next(d for d in v.discrepancies if d.field == "year")
    assert year_diff.severity == CitationDiscrepancySeverity.error
    assert year_diff.canonical_value == "2020"
    assert year_diff.paper_value == "2019"


@pytest.mark.asyncio
async def test_no_results_returns_unmatched(monkeypatch) -> None:
    src = _src("ghost", title="Nonexistent paper xyzpdq")
    routes = {
        "api.crossref.org/works": {"message": {"items": []}},
        "api.openalex.org/works": {"results": []},
    }
    transport = _mock_transport(routes)
    import lattice.references.verifier as verifier_mod
    original = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(verifier_mod.httpx, "AsyncClient", _factory)

    result = await verify_sources([src])
    v = result["ghost"]
    assert v.matched is False
    assert v.canonical is None


@pytest.mark.asyncio
async def test_crossref_search_finds_match_above_threshold(monkeypatch) -> None:
    src = _src(
        "ondm",
        title="On the Mechanism of Carbon Pricing",
        year=2020,
        authors=["Smith, J."],
    )
    routes = {
        "api.crossref.org/works": {
            "message": {
                "items": [
                    {
                        "title": ["On the Mechanism of Carbon Pricing"],
                        "author": [{"family": "Smith", "given": "John"}],
                        "issued": {"date-parts": [[2020]]},
                        "container-title": ["Nature Climate"],
                        "DOI": "10.9876/found",
                        "volume": "580",
                        "page": "12-19",
                    },
                    {
                        "title": ["Some unrelated paper"],
                        "author": [{"family": "Other", "given": "Person"}],
                        "issued": {"date-parts": [[2010]]},
                    },
                ]
            }
        },
        "api.openalex.org/works": {"results": []},
    }
    transport = _mock_transport(routes)
    import lattice.references.verifier as verifier_mod
    original = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(verifier_mod.httpx, "AsyncClient", _factory)

    result = await verify_sources([src])
    v = result["ondm"]
    assert v.matched is True
    assert v.confidence > 0.7
    assert v.canonical is not None
    assert v.canonical.doi == "10.9876/found"
    # Paper had no DOI; canonical adds one — should be an info-level diff.
    doi_diff = next(d for d in v.discrepancies if d.field == "doi")
    assert doi_diff.severity == CitationDiscrepancySeverity.info


# ─── cache ──────────────────────────────────────


def test_cache_round_trip(tmp_path: Path) -> None:
    (tmp_path / ".lattice").mkdir()
    v = CitationVerification(
        source_id="smith_2020",
        verifier=CitationVerifier.crossref,
        verified_at=_now(),
        matched=True,
        confidence=0.95,
        canonical=Citation(title="X", year=2020, authors=["Smith, J."]),
        discrepancies=[],
    )
    save_verification_cache(tmp_path, {"smith_2020": v})
    loaded = load_verification_cache(tmp_path)
    assert "smith_2020" in loaded
    assert loaded["smith_2020"].confidence == 0.95
    assert loaded["smith_2020"].canonical.title == "X"


def test_load_cache_empty_when_missing(tmp_path: Path) -> None:
    assert load_verification_cache(tmp_path) == {}


def test_load_cache_handles_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / ".lattice").mkdir()
    (tmp_path / ".lattice" / "citation_verifications.json").write_text(
        "not json", encoding="utf-8",
    )
    assert load_verification_cache(tmp_path) == {}


# ─── error handling ─────────────────────────────


@pytest.mark.asyncio
async def test_http_error_yields_error_verification(monkeypatch) -> None:
    src = _src("err", title="Anything", year=2020)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    transport = httpx.MockTransport(handler)
    import lattice.references.verifier as verifier_mod
    original = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(verifier_mod.httpx, "AsyncClient", _factory)

    result = await verify_sources([src])
    v = result["err"]
    assert v.matched is False
    assert "error" in v.note.lower() or "below_match_threshold" in v.note
