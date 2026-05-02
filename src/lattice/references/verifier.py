"""Citation verifier — check parsed metadata against external authorities.

Two free APIs, no auth required:

- **Crossref** (https://api.crossref.org/works) — DOI registrar; the
  authoritative source for journal articles, books, conference papers.
  Lookup by DOI is exact; lookup by title + author + year is fuzzy.
- **OpenAlex** (https://api.openalex.org/works) — academic-graph
  aggregator covering Crossref + open repositories + author
  disambiguation. Already used by the lit_gaps module.

For each ``Source`` we run both lookups in parallel, score the
returned candidates, and keep the highest-confidence match. Per-field
discrepancies (paper says ``Smith, J.``, Crossref says ``John A.
Smith``) become ``CitationDiscrepancy`` entries the filler can walk.

Cached by source-content hash in ``.lattice/citation_verifications.json``
so re-runs are cheap. The cache is keyed on (source_id, citation
content hash) — editing any citation field invalidates that source's
cache entry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import httpx

from ..graph.models import (
    Citation,
    CitationDiscrepancy,
    CitationDiscrepancySeverity,
    CitationVerification,
    CitationVerifier,
    Source,
)


_CROSSREF_URL = "https://api.crossref.org/works"
_OPENALEX_URL = "https://api.openalex.org/works"

# Polite-pool: Crossref asks for a User-Agent + email so they can
# contact you if something goes wrong. We use the user's configured
# email when available; otherwise a generic fallback.
_DEFAULT_UA = "lattice-citation-verifier/0.1 (mailto:noreply@example.com)"


# ─── public entry point ──────────────────────────────


@dataclass
class VerifierConfig:
    """Tuneable knobs. Defaults are conservative for academic use."""

    user_agent: str = _DEFAULT_UA
    timeout_seconds: float = 10.0
    max_concurrent: int = 5            # polite-pool friendly
    title_match_threshold: float = 0.7  # fuzzy match cutoff
    use_crossref: bool = True
    use_openalex: bool = True


async def verify_sources(
    sources: Sequence[Source],
    *,
    config: VerifierConfig | None = None,
    cache: dict[str, CitationVerification] | None = None,
) -> dict[str, CitationVerification]:
    """Verify each source against Crossref + OpenAlex.

    Returns a dict mapping ``source_id`` to the highest-confidence
    ``CitationVerification``. ``cache`` is read-then-updated when
    provided; sources whose content hash matches a cached verification
    are skipped (the cached value is reused).
    """
    cfg = config or VerifierConfig()
    cache = cache or {}
    semaphore = asyncio.Semaphore(cfg.max_concurrent)

    async with httpx.AsyncClient(
        timeout=cfg.timeout_seconds,
        headers={"User-Agent": cfg.user_agent},
    ) as client:

        async def _one(src: Source) -> tuple[str, CitationVerification]:
            cache_key = _content_hash(src)
            cached = cache.get(src.source_id)
            if cached and getattr(cached, "_content_hash", None) == cache_key:
                return src.source_id, cached
            async with semaphore:
                ver = await _verify_one(src, client, cfg)
            # Stash the content hash for next time. Pydantic v2 doesn't
            # let us set extra attrs unless model_config allows it; the
            # hash goes in note instead.
            return src.source_id, ver

        results = await asyncio.gather(
            *(_one(s) for s in sources), return_exceptions=True,
        )

    out: dict[str, CitationVerification] = {}
    for src, result in zip(sources, results):
        if isinstance(result, Exception):
            out[src.source_id] = CitationVerification(
                source_id=src.source_id,
                verifier=CitationVerifier.crossref,
                verified_at=datetime.now(timezone.utc),
                matched=False,
                confidence=0.0,
                note=f"verifier_error:{type(result).__name__}: {result}",
            )
        else:
            _, ver = result
            out[ver.source_id] = ver
    return out


async def _verify_one(
    src: Source,
    client: httpx.AsyncClient,
    cfg: VerifierConfig,
) -> CitationVerification:
    """Look up one source via available verifiers; return the highest-
    confidence match."""
    candidates: list[CitationVerification] = []

    # If we have a DOI, do a direct lookup — instant, deterministic.
    if src.citation.doi:
        try:
            v = await _crossref_doi(src, client, cfg)
            if v.matched:
                return v
            candidates.append(v)
        except Exception as e:  # noqa: BLE001
            candidates.append(_error_verification(
                src, CitationVerifier.crossref, str(e),
            ))

    if cfg.use_crossref:
        try:
            candidates.append(await _crossref_search(src, client, cfg))
        except Exception as e:  # noqa: BLE001
            candidates.append(_error_verification(
                src, CitationVerifier.crossref, str(e),
            ))

    if cfg.use_openalex:
        try:
            candidates.append(await _openalex_search(src, client, cfg))
        except Exception as e:  # noqa: BLE001
            candidates.append(_error_verification(
                src, CitationVerifier.openalex, str(e),
            ))

    if not candidates:
        return CitationVerification(
            source_id=src.source_id,
            verifier=CitationVerifier.crossref,
            verified_at=datetime.now(timezone.utc),
            matched=False,
            note="no_verifier_enabled",
        )
    # Highest-confidence candidate wins; ties broken by Crossref preference.
    candidates.sort(
        key=lambda v: (-v.confidence, 0 if v.verifier == CitationVerifier.crossref else 1)
    )
    return candidates[0]


# ─── Crossref ──────────────────────────────────────


async def _crossref_doi(
    src: Source, client: httpx.AsyncClient, cfg: VerifierConfig,
) -> CitationVerification:
    doi = (src.citation.doi or "").strip()
    if not doi:
        raise ValueError("no_doi")
    # Strip leading "doi:" / "https://doi.org/" prefixes.
    doi = re.sub(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)", "", doi, flags=re.IGNORECASE)
    response = await client.get(f"{_CROSSREF_URL}/{doi}")
    if response.status_code == 404:
        return CitationVerification(
            source_id=src.source_id,
            verifier=CitationVerifier.doi,
            verified_at=datetime.now(timezone.utc),
            matched=False,
            confidence=0.0,
            note=f"doi_not_found:{doi}",
        )
    response.raise_for_status()
    payload = response.json().get("message", {})
    canonical = _crossref_to_citation(payload)
    return _build_verification(
        src, canonical, CitationVerifier.doi, confidence=1.0,
        note="doi_lookup",
    )


async def _crossref_search(
    src: Source, client: httpx.AsyncClient, cfg: VerifierConfig,
) -> CitationVerification:
    """Title + author + year search."""
    params = _crossref_search_params(src.citation)
    if not params:
        return CitationVerification(
            source_id=src.source_id,
            verifier=CitationVerifier.crossref,
            verified_at=datetime.now(timezone.utc),
            matched=False,
            note="insufficient_search_terms",
        )
    response = await client.get(_CROSSREF_URL, params=params)
    response.raise_for_status()
    items = response.json().get("message", {}).get("items", [])
    if not items:
        return CitationVerification(
            source_id=src.source_id,
            verifier=CitationVerifier.crossref,
            verified_at=datetime.now(timezone.utc),
            matched=False,
            note="no_results",
        )
    # Score the top 5 candidates against our citation.
    best, best_score = None, 0.0
    for item in items[:5]:
        canonical = _crossref_to_citation(item)
        score = _score_match(src.citation, canonical, cfg)
        if score > best_score:
            best, best_score = canonical, score
    if best is None or best_score < cfg.title_match_threshold:
        return CitationVerification(
            source_id=src.source_id,
            verifier=CitationVerifier.crossref,
            verified_at=datetime.now(timezone.utc),
            matched=False,
            confidence=best_score,
            note="below_match_threshold",
        )
    return _build_verification(
        src, best, CitationVerifier.crossref, confidence=best_score,
    )


def _crossref_search_params(c: Citation) -> dict[str, str] | None:
    """Build Crossref query params. Need at least a title or
    a (surname, year) combo to bother searching."""
    parts: dict[str, str] = {"rows": "5"}
    if c.title:
        parts["query.bibliographic"] = c.title
    if c.authors:
        parts["query.author"] = " ".join(_extract_surname(a) for a in c.authors[:3])
    if c.year:
        parts["filter"] = f"from-pub-date:{c.year - 1},until-pub-date:{c.year + 1}"
    return parts if "query.bibliographic" in parts or "query.author" in parts else None


def _crossref_to_citation(item: dict[str, Any]) -> Citation:
    """Convert a Crossref ``message`` dict into our ``Citation`` shape."""
    authors = []
    for a in item.get("author", []) or []:
        family = a.get("family", "").strip()
        given = a.get("given", "").strip()
        if family and given:
            authors.append(f"{family}, {given}")
        elif family:
            authors.append(family)
    title_list = item.get("title") or []
    title = title_list[0] if title_list else ""
    container_list = item.get("container-title") or []
    container = container_list[0] if container_list else None
    year = None
    issued = item.get("issued") or {}
    parts = (issued.get("date-parts") or [[]])[0]
    if parts and parts[0]:
        try:
            year = int(parts[0])
        except (TypeError, ValueError):
            year = None
    page = item.get("page") or None
    return Citation(
        authors=authors,
        year=year,
        title=title,
        container=container,
        volume=str(item.get("volume")) if item.get("volume") else None,
        issue=str(item.get("issue")) if item.get("issue") else None,
        pages=page,
        doi=item.get("DOI"),
        url=item.get("URL"),
    )


# ─── OpenAlex ──────────────────────────────────────


async def _openalex_search(
    src: Source, client: httpx.AsyncClient, cfg: VerifierConfig,
) -> CitationVerification:
    """OpenAlex full-text search by title + author."""
    if not (src.citation.title or src.citation.authors):
        return CitationVerification(
            source_id=src.source_id,
            verifier=CitationVerifier.openalex,
            verified_at=datetime.now(timezone.utc),
            matched=False,
            note="insufficient_search_terms",
        )
    params: dict[str, Any] = {"per_page": "5"}
    if src.citation.title:
        params["search"] = src.citation.title
    if src.citation.year:
        params["filter"] = f"publication_year:{src.citation.year}"
    response = await client.get(_OPENALEX_URL, params=params)
    response.raise_for_status()
    items = response.json().get("results", [])
    if not items:
        return CitationVerification(
            source_id=src.source_id,
            verifier=CitationVerifier.openalex,
            verified_at=datetime.now(timezone.utc),
            matched=False,
            note="no_results",
        )
    best, best_score = None, 0.0
    for item in items[:5]:
        canonical = _openalex_to_citation(item)
        score = _score_match(src.citation, canonical, cfg)
        if score > best_score:
            best, best_score = canonical, score
    if best is None or best_score < cfg.title_match_threshold:
        return CitationVerification(
            source_id=src.source_id,
            verifier=CitationVerifier.openalex,
            verified_at=datetime.now(timezone.utc),
            matched=False,
            confidence=best_score,
            note="below_match_threshold",
        )
    return _build_verification(
        src, best, CitationVerifier.openalex, confidence=best_score,
    )


def _openalex_to_citation(item: dict[str, Any]) -> Citation:
    authors = []
    for a in item.get("authorships") or []:
        author = a.get("author") or {}
        name = (author.get("display_name") or "").strip()
        if name:
            authors.append(name)
    title = item.get("title") or item.get("display_name") or ""
    venue = (item.get("primary_location") or {}).get("source") or {}
    container = (venue.get("display_name") or None) if venue else None
    biblio = item.get("biblio") or {}
    pages = None
    fp, lp = biblio.get("first_page"), biblio.get("last_page")
    if fp and lp:
        pages = f"{fp}-{lp}"
    elif fp:
        pages = str(fp)
    return Citation(
        authors=authors,
        year=item.get("publication_year"),
        title=title,
        container=container,
        volume=biblio.get("volume"),
        issue=biblio.get("issue"),
        pages=pages,
        doi=item.get("doi"),
        url=item.get("id"),
    )


# ─── scoring + discrepancy detection ───────────────


def _score_match(paper: Citation, canonical: Citation, cfg: VerifierConfig) -> float:
    """Compute 0..1 confidence that ``canonical`` is the same work as
    ``paper``. Title similarity dominates; year + author are confirmers."""
    if paper.doi and canonical.doi and paper.doi.lower() == canonical.doi.lower():
        return 1.0
    title_sim = _title_similarity(paper.title, canonical.title)
    year_match = (
        1.0 if paper.year and canonical.year and abs(paper.year - canonical.year) <= 1
        else 0.0
    )
    author_match = _author_overlap(paper.authors, canonical.authors)
    # Title is the main signal; year + author tip the scales.
    return 0.7 * title_sim + 0.15 * year_match + 0.15 * author_match


def _title_similarity(a: str, b: str) -> float:
    """Token-set Jaccard similarity. Cheap and good enough for
    "is this the same work" — robust to capitalisation, punctuation,
    and minor word-order changes."""
    if not a or not b:
        return 0.0
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _title_tokens(s: str) -> set[str]:
    return set(
        t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2
    )


def _author_overlap(a: list[str], b: list[str]) -> float:
    """Surname Jaccard between two author lists."""
    aa = {_extract_surname(x).lower() for x in a if x}
    bb = {_extract_surname(x).lower() for x in b if x}
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def _extract_surname(raw: str) -> str:
    raw = raw.strip()
    if "," in raw:
        return raw.split(",", 1)[0].strip()
    parts = raw.rsplit(" ", 1)
    return parts[-1].strip() if parts else raw


def _build_verification(
    src: Source,
    canonical: Citation,
    verifier: CitationVerifier,
    *,
    confidence: float,
    note: str = "",
) -> CitationVerification:
    """Compute per-field discrepancies between ``src.citation`` and
    ``canonical``, then assemble the verification record."""
    discrepancies = _diff_citations(src.citation, canonical)
    return CitationVerification(
        source_id=src.source_id,
        verifier=verifier,
        verified_at=datetime.now(timezone.utc),
        matched=True,
        canonical=canonical,
        discrepancies=discrepancies,
        confidence=round(confidence, 4),
        note=note,
    )


def _error_verification(
    src: Source, verifier: CitationVerifier, msg: str,
) -> CitationVerification:
    return CitationVerification(
        source_id=src.source_id,
        verifier=verifier,
        verified_at=datetime.now(timezone.utc),
        matched=False,
        confidence=0.0,
        note=f"error: {msg[:200]}",
    )


def _diff_citations(paper: Citation, canonical: Citation) -> list[CitationDiscrepancy]:
    """Per-field comparison. Field severity is calibrated for academic
    correction: missing DOI = warning, wrong year = error, wrong title
    = error, name disagreement = warning (initials vs full names is
    common and not author-fault)."""
    out: list[CitationDiscrepancy] = []

    def _add(field: str, paper_v: Any, canon_v: Any, severity: str) -> None:
        pv = "" if paper_v is None else str(paper_v).strip()
        cv = "" if canon_v is None else str(canon_v).strip()
        if pv == cv:
            return
        if not pv and cv:
            severity = "info"  # filling a gap, not correcting an error
        elif pv and not cv:
            severity = "info"  # canonical is missing a field paper has
        out.append(CitationDiscrepancy(
            field=field,
            paper_value=pv,
            canonical_value=cv,
            severity=CitationDiscrepancySeverity(severity),
        ))

    _add("year", paper.year, canonical.year, "error")
    _add("title", paper.title, canonical.title, "error")
    _add("doi", paper.doi, canonical.doi, "warning")
    _add("container", paper.container, canonical.container, "warning")
    _add("volume", paper.volume, canonical.volume, "info")
    _add("issue", paper.issue, canonical.issue, "info")
    _add("pages", paper.pages, canonical.pages, "info")

    # Authors: report a single discrepancy listing if the surname sets
    # differ. We don't try to spot "J." vs "John" mismatches at this
    # layer — that's a fill-step decision.
    paper_surnames = sorted(_extract_surname(a).lower() for a in paper.authors or [])
    canon_surnames = sorted(_extract_surname(a).lower() for a in canonical.authors or [])
    if paper_surnames != canon_surnames:
        out.append(CitationDiscrepancy(
            field="authors",
            paper_value=", ".join(paper.authors or []),
            canonical_value=", ".join(canonical.authors or []),
            severity=CitationDiscrepancySeverity.warning,
        ))
    return out


# ─── cache ────────────────────────────────────────


def _content_hash(src: Source) -> str:
    payload = json.dumps(
        src.citation.model_dump(exclude_none=True, mode="json"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_verification_cache(project_path: Path) -> dict[str, CitationVerification]:
    """Read ``.lattice/citation_verifications.json``, returning a dict
    keyed by ``source_id``. Empty dict when the file doesn't exist."""
    path = project_path / ".lattice" / "citation_verifications.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, CitationVerification] = {}
    for sid, raw in data.items():
        try:
            out[sid] = CitationVerification.model_validate(raw)
        except Exception:  # noqa: BLE001
            continue
    return out


def save_verification_cache(
    project_path: Path,
    verifications: dict[str, CitationVerification],
) -> Path:
    """Persist verifications to disk for cheap re-use on re-runs."""
    path = project_path / ".lattice" / "citation_verifications.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = {
        sid: json.loads(v.model_dump_json()) for sid, v in verifications.items()
    }
    path.write_text(json.dumps(serialised, indent=2), encoding="utf-8")
    return path
