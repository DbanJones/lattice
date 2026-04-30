"""Per-section literature-gap pipeline.

Pipeline:
  1. For each section: Claude suggests missing works, counter-arguments,
     and recent papers (in parallel across sections).
  2. (Thorough mode) For each suggestion: query OpenAlex to verify the
     work exists, has a matching author, and the year is plausible. The
     verification populates DOI, canonical authors, citation count.

The OpenAlex API is free, requires no auth, and asks callers to
identify themselves via a polite User-Agent. We use the polite pool.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from ..graph.models import AuthorGraph, Section
from ..utils.llm import ClaudeClient


_OPENALEX_BASE = "https://api.openalex.org"
# OpenAlex asks for a polite UA so they can rate-limit per app rather
# than per IP. The mailto address gets routed to the polite pool.
_OPENALEX_UA = "lattice-research-tool (mailto:david.bannister.jones@googlemail.com)"


# ─── output schema ───────────────────────────────


class LitGapSuggestion(BaseModel):
    """A single missing-work suggestion for a section."""
    author: str
    year: int | None = None
    work: str  # title as the LLM gave it
    why_relevant: str
    claim_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    kind: Literal["canonical", "counter_argument", "recent"] = "canonical"
    section_id: str = ""

    # Verification fields, populated by the OpenAlex pass.
    verified: bool = False
    openalex_id: str | None = None
    doi: str | None = None
    canonical_title: str | None = None
    canonical_authors: list[str] = Field(default_factory=list)
    publication_year: int | None = None
    cited_by_count: int | None = None


class SectionGaps(BaseModel):
    section_id: str
    section_title: str
    suggestions: list[LitGapSuggestion] = Field(default_factory=list)


class LitGapsReport(BaseModel):
    project_name: str
    voice_name: str
    generated_at: str
    sections: list[SectionGaps]
    total_suggestions: int = 0
    verified_count: int = 0
    mode: Literal["fast", "thorough"] = "thorough"


# ─── prompt ──────────────────────────────────────


_SYSTEM = """You are a research librarian reviewing one section of an academic paper.

For this section identify works the author SHOULD engage with but doesn't, in three categories:

1. "canonical"        — foundational works in this subfield that any paper here would normally cite
2. "counter_argument" — a standard objection or competing position the section's claims don't address
3. "recent"           — important work from the last five years bearing on these claims

Rules:
- Don't re-suggest works already cited in the section (listed in [cites: ...]).
- Be specific: real authors, real titles, real years. No vague placeholders.
- Prefer canonical / foundational over obscure.
- Don't pad. Three to seven solid suggestions per section is plenty; zero is fine if the section is well-covered.
- Set "confidence" honestly: "high" only if you'd stake your reputation on the work being canonical here.

Output strict JSON, no fenced code block, no prose outside JSON:
{
  "suggestions": [
    {
      "author": "First-author surname (or 'X et al.')",
      "year": 2019,
      "work": "Paper or book title",
      "why_relevant": "One sentence connecting it to the section's claims.",
      "claim_ids": ["c.f.1", "c.f.3"],
      "confidence": "high",
      "kind": "canonical"
    }
  ]
}"""


# ─── per-section LLM call ────────────────────────


async def _suggest_for_section(
    graph: AuthorGraph,
    section: Section,
    llm: ClaudeClient,
) -> list[LitGapSuggestion]:
    """One Claude call per section, returns parsed suggestions."""
    claims = [c for c in graph.claims if c.section_id == section.section_id]
    if not claims:
        return []

    claim_lines: list[str] = []
    for c in claims:
        # Surface what's already cited so the LLM doesn't re-suggest.
        cited = sorted({e.source for e in (c.evidence or []) if e.source})
        cite_str = f" [cites: {', '.join(cited)}]" if cited else ""
        claim_lines.append(f"  {c.claim_id}: {_short(c.statement)}{cite_str}")

    user = (
        f"Paper thesis:\n{graph.thesis_statement or '(not stated)'}\n\n"
        f"Section: \"{section.title}\"\n"
        f"Section role: {section.role.value if hasattr(section.role, 'value') else section.role}\n\n"
        f"Claims in this section:\n" + "\n".join(claim_lines)
    )

    try:
        data, _ = await llm.complete_json(_SYSTEM, user)
    except Exception:
        return []

    out: list[LitGapSuggestion] = []
    for entry in (data.get("suggestions") or []):
        try:
            out.append(LitGapSuggestion(
                author=str(entry.get("author", "")).strip(),
                year=_int_or_none(entry.get("year")),
                work=str(entry.get("work", "")).strip(),
                why_relevant=str(entry.get("why_relevant", "")).strip(),
                claim_ids=[str(x) for x in (entry.get("claim_ids") or []) if x],
                confidence=_clip_confidence(entry.get("confidence")),
                kind=_clip_kind(entry.get("kind")),
                section_id=section.section_id,
            ))
        except Exception:
            continue
    return out


# ─── OpenAlex verification ───────────────────────


async def _verify_with_openalex(
    suggestions: list[LitGapSuggestion],
    client: httpx.AsyncClient,
) -> None:
    """Mutate each suggestion in-place: populate verification fields.

    Conservative matching: requires title token overlap ≥ 0.4 AND a
    matching author surname AND year within ±3. Anything looser starts
    accepting noise (OpenAlex returns ~20M near-matches for short
    titles).
    """
    sem = asyncio.Semaphore(5)  # OpenAlex polite pool rate limit

    async def verify_one(sugg: LitGapSuggestion) -> None:
        if not sugg.work:
            return
        async with sem:
            try:
                resp = await client.get(
                    f"{_OPENALEX_BASE}/works",
                    params={"search": sugg.work, "per-page": 5},
                    headers={"User-Agent": _OPENALEX_UA},
                    timeout=10.0,
                )
            except (httpx.HTTPError, asyncio.TimeoutError):
                return
            if resp.status_code != 200:
                return
            try:
                data = resp.json()
            except ValueError:
                return
            best = _pick_best_match(sugg, data.get("results") or [])
            if best is None:
                return
            sugg.verified = True
            sugg.openalex_id = best.get("id")
            sugg.doi = best.get("doi")
            sugg.canonical_title = best.get("title")
            authors: list[str] = []
            for a in (best.get("authorships") or []):
                name = (a.get("author") or {}).get("display_name")
                if name:
                    authors.append(name)
            sugg.canonical_authors = authors[:8]
            sugg.publication_year = best.get("publication_year")
            sugg.cited_by_count = best.get("cited_by_count")

    await asyncio.gather(*[verify_one(s) for s in suggestions])


def _pick_best_match(
    sugg: LitGapSuggestion,
    results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not results:
        return None
    target_title_tokens = set(_tokens(sugg.work))
    if not target_title_tokens:
        return None

    target_surname = ""
    if sugg.author:
        parts = sugg.author.split()
        if parts:
            target_surname = re.sub(r"[^a-z\-']", "", parts[-1].lower())
    suggested_year = sugg.year

    best: dict[str, Any] | None = None
    best_score = 0.0
    for r in results:
        title = r.get("title") or ""
        result_tokens = set(_tokens(title))
        if not result_tokens:
            continue
        overlap = len(target_title_tokens & result_tokens)
        union = len(target_title_tokens | result_tokens)
        title_score = (overlap / union) if union else 0.0
        if title_score < 0.4:
            continue

        # Year within ±3 (lenient: editions, preprints, online-first).
        result_year = r.get("publication_year")
        if suggested_year and result_year and abs(result_year - suggested_year) > 3:
            continue

        # Author surname must appear in OpenAlex authorships.
        if target_surname:
            surnames: set[str] = set()
            for a in (r.get("authorships") or []):
                name = (a.get("author") or {}).get("display_name", "")
                if not name:
                    continue
                last = name.split()[-1].lower()
                surnames.add(re.sub(r"[^a-z\-']", "", last))
            if target_surname not in surnames:
                continue

        if title_score > best_score:
            best_score = title_score
            best = r
    return best


# ─── public entry point ──────────────────────────


async def find_lit_gaps(
    project_path: Path,
    voice_name: str,
    graph: AuthorGraph,
    llm: ClaudeClient,
    *,
    mode: Literal["fast", "thorough"] = "thorough",
    progress: Any = None,
) -> LitGapsReport:
    """Build a literature-gap report.

    ``mode='fast'`` runs Claude only.
    ``mode='thorough'`` also verifies every suggestion against OpenAlex.

    Returns the report; the caller can persist it via
    :func:`write_lit_gaps_report`.
    """
    sections = list(graph.sections)
    if not sections:
        return LitGapsReport(
            project_name=graph.project_name or project_path.name,
            voice_name=voice_name,
            generated_at=datetime.now(timezone.utc).isoformat(),
            sections=[],
            mode=mode,
        )

    if progress:
        progress.begin(
            "lit_gaps_suggest",
            total=len(sections),
            status="asking Claude per section",
        )

    # Run sections in parallel, but keep results paired with their
    # section_id so completion order doesn't matter.
    async def run_one(s: Section) -> tuple[str, list[LitGapSuggestion]]:
        suggs = await _suggest_for_section(graph, s, llm)
        if progress:
            progress.advance("lit_gaps_suggest")
        return s.section_id, suggs

    by_section: dict[str, list[LitGapSuggestion]] = {s.section_id: [] for s in sections}
    pairs = await asyncio.gather(*[run_one(s) for s in sections])
    for sid, suggs in pairs:
        by_section[sid] = suggs

    if progress:
        progress.end(
            "lit_gaps_suggest",
            status=f"{sum(len(v) for v in by_section.values())} suggestion(s) total",
        )

    all_suggestions = [s for batch in by_section.values() for s in batch]

    if mode == "thorough" and all_suggestions:
        if progress:
            progress.begin(
                "lit_gaps_verify",
                total=len(all_suggestions),
                status=f"verifying {len(all_suggestions)} suggestion(s) on OpenAlex",
            )
        async with httpx.AsyncClient() as client:
            await _verify_with_openalex(all_suggestions, client)
        if progress:
            verified = sum(1 for s in all_suggestions if s.verified)
            progress.end(
                "lit_gaps_verify",
                status=f"{verified}/{len(all_suggestions)} verified",
            )

    section_gaps = [
        SectionGaps(
            section_id=s.section_id,
            section_title=s.title,
            suggestions=by_section.get(s.section_id, []),
        )
        for s in sections
    ]
    return LitGapsReport(
        project_name=graph.project_name or project_path.name,
        voice_name=voice_name,
        generated_at=datetime.now(timezone.utc).isoformat(),
        sections=section_gaps,
        total_suggestions=len(all_suggestions),
        verified_count=sum(1 for s in all_suggestions if s.verified),
        mode=mode,
    )


def write_lit_gaps_report(
    project_path: Path, report: LitGapsReport,
) -> Path:
    """Persist the report to ``outputs/lit_gaps.{voice}.json``."""
    target = project_path / "outputs" / f"lit_gaps.{report.voice_name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return target


def read_lit_gaps_report(
    project_path: Path, voice_name: str,
) -> LitGapsReport | None:
    """Load a previously-written report, or ``None`` if there isn't one."""
    target = project_path / "outputs" / f"lit_gaps.{voice_name}.json"
    if not target.exists():
        return None
    try:
        return LitGapsReport.model_validate_json(
            target.read_text(encoding="utf-8")
        )
    except Exception:
        return None


# ─── helpers ─────────────────────────────────────


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _clip_confidence(v: Any) -> Literal["high", "medium", "low"]:
    return v if v in ("high", "medium", "low") else "medium"


def _clip_kind(v: Any) -> Literal["canonical", "counter_argument", "recent"]:
    return v if v in ("canonical", "counter_argument", "recent") else "canonical"


def _tokens(text: str) -> list[str]:
    text = text.lower()
    return [t for t in re.split(r"[^a-z0-9]+", text) if len(t) > 2]
