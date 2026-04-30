"""Semantic comparison of two author graphs.

Pairs claims across two graphs that express the same idea in
different words. Layered on top of the deterministic ``Differ`` so the
output combines structural overlap (Jaccard tokens) with semantic
equivalence (LLM judgment).

The single public entry point is ``compare_projects(a, b, llm, mode)``.
``mode='fast'`` skips the LLM pairing and returns a structural report
only; ``mode='thorough'`` runs the LLM pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..graph.models import AuthorGraph, Claim
from ..graph.store import GraphStore
from ..utils.llm import ClaudeClient


PairRelationship = Literal[
    "equivalent",   # same claim, different wording
    "refines",      # one paper is more specific than the other
    "contradicts",  # the two claims oppose each other
    "related",      # share a topic but make different points
]


class SemanticPair(BaseModel):
    """A pair of claims the LLM identified as related across graphs."""
    claim_a_id: str
    claim_a_text: str
    claim_b_id: str
    claim_b_text: str
    relationship: PairRelationship
    confidence: Literal["high", "medium", "low"] = "medium"
    rationale: str = ""


class ThesisComparison(BaseModel):
    """LLM judgment on whether the two papers' theses align."""
    thesis_a: str
    thesis_b: str
    agreement: Literal[
        "same",         # papers argue the same thing
        "complementary",  # different angles on the same topic
        "opposing",     # papers argue against each other
        "unrelated",    # different topics
    ]
    summary: str


class StructuralSummary(BaseModel):
    project_name: str
    section_count: int
    claim_count: int
    relationship_count: int
    thesis_statement: str | None = None
    section_titles: list[str] = Field(default_factory=list)


class ComparisonReport(BaseModel):
    """Full output of a project-vs-project comparison."""
    project_a: StructuralSummary
    project_b: StructuralSummary
    thesis_comparison: ThesisComparison | None = None
    pairs: list[SemanticPair] = Field(default_factory=list)
    unique_a: list[dict[str, str]] = Field(default_factory=list)
    unique_b: list[dict[str, str]] = Field(default_factory=list)
    mode: Literal["fast", "thorough"] = "thorough"


# ─── prompts ─────────────────────────────────────


_PAIRING_SYSTEM = """You compare two argument scaffolds.

You receive two lists of claims. Find pairs where a claim in list A
expresses an idea that also appears in list B (possibly worded
differently). Classify each pair:

- "equivalent": same claim, different wording
- "refines":    one is a more specific case of the other
- "contradicts": the claims directly oppose each other
- "related":    share a topic but make different points

Only emit a pair when there's a real connection — do not pair claims
that merely belong to the same broad subject. Most claims will be
unique to one side.

Respond with strict JSON:
{
  "pairs": [
    {
      "a_id": "<claim id from A>",
      "b_id": "<claim id from B>",
      "relationship": "equivalent | refines | contradicts | related",
      "confidence": "high | medium | low",
      "rationale": "<one short sentence>"
    }
  ]
}
No prose outside the JSON. No fenced code block."""


_THESIS_SYSTEM = """You compare two thesis statements.

Decide whether the two theses are:
- "same":          arguing for the same conclusion
- "complementary": addressing different angles of the same topic
- "opposing":      arguing against each other
- "unrelated":     different subjects entirely

Respond with strict JSON:
{
  "agreement": "same | complementary | opposing | unrelated",
  "summary":   "<two-sentence explanation>"
}
No prose outside the JSON."""


# ─── comparer ────────────────────────────────────


class SemanticComparer:
    def __init__(self, llm: ClaudeClient) -> None:
        self.llm = llm

    async def compare_theses(
        self, thesis_a: str, thesis_b: str,
    ) -> ThesisComparison:
        if not thesis_a.strip() or not thesis_b.strip():
            return ThesisComparison(
                thesis_a=thesis_a,
                thesis_b=thesis_b,
                agreement="unrelated",
                summary="One or both thesis statements are missing.",
            )
        user = (
            f"Thesis A:\n{thesis_a.strip()}\n\n"
            f"Thesis B:\n{thesis_b.strip()}"
        )
        data, _ = await self.llm.complete_json(_THESIS_SYSTEM, user)
        agreement = data.get("agreement", "unrelated")
        if agreement not in ("same", "complementary", "opposing", "unrelated"):
            agreement = "unrelated"
        return ThesisComparison(
            thesis_a=thesis_a,
            thesis_b=thesis_b,
            agreement=agreement,
            summary=str(data.get("summary", "")).strip(),
        )

    async def pair_claims(
        self,
        claims_a: list[Claim],
        claims_b: list[Claim],
    ) -> list[SemanticPair]:
        if not claims_a or not claims_b:
            return []

        # Build a compact prompt: just id + text per claim. The LLM
        # returns ids it sees verbatim so we don't need to round-trip
        # full claim objects.
        a_lines = "\n".join(
            f"  {c.claim_id}: {_short(c.statement)}" for c in claims_a
        )
        b_lines = "\n".join(
            f"  {c.claim_id}: {_short(c.statement)}" for c in claims_b
        )
        user = (
            f"Paper A claims:\n{a_lines}\n\n"
            f"Paper B claims:\n{b_lines}"
        )
        data, _ = await self.llm.complete_json(_PAIRING_SYSTEM, user)

        a_by_id = {c.claim_id: c for c in claims_a}
        b_by_id = {c.claim_id: c for c in claims_b}
        pairs: list[SemanticPair] = []
        for entry in data.get("pairs", []):
            a_id = entry.get("a_id")
            b_id = entry.get("b_id")
            if a_id not in a_by_id or b_id not in b_by_id:
                continue
            relationship = entry.get("relationship", "related")
            if relationship not in (
                "equivalent", "refines", "contradicts", "related"
            ):
                relationship = "related"
            confidence = entry.get("confidence", "medium")
            if confidence not in ("high", "medium", "low"):
                confidence = "medium"
            pairs.append(SemanticPair(
                claim_a_id=a_id,
                claim_a_text=a_by_id[a_id].statement,
                claim_b_id=b_id,
                claim_b_text=b_by_id[b_id].statement,
                relationship=relationship,
                confidence=confidence,
                rationale=str(entry.get("rationale", "")).strip(),
            ))
        return pairs


# ─── public entry point ──────────────────────────


async def compare_projects(
    project_a: Path,
    project_b: Path,
    llm: ClaudeClient | None = None,
    mode: Literal["fast", "thorough"] = "thorough",
) -> ComparisonReport:
    """Build a comparison report for two lattice projects.

    Both projects must have an ``author_graph.json`` (i.e. be at S2 or
    later). ``mode='fast'`` skips LLM calls and returns structural
    summary only; ``mode='thorough'`` adds thesis comparison and claim
    pairing.
    """
    graph_a = _load_graph(project_a)
    graph_b = _load_graph(project_b)

    summary_a = _structural_summary(project_a, graph_a)
    summary_b = _structural_summary(project_b, graph_b)

    report = ComparisonReport(
        project_a=summary_a,
        project_b=summary_b,
        mode=mode,
    )

    if mode == "fast" or llm is None:
        return report

    comparer = SemanticComparer(llm)
    if graph_a.thesis_statement and graph_b.thesis_statement:
        try:
            report.thesis_comparison = await comparer.compare_theses(
                graph_a.thesis_statement, graph_b.thesis_statement,
            )
        except Exception as exc:  # noqa: BLE001
            report.thesis_comparison = ThesisComparison(
                thesis_a=graph_a.thesis_statement or "",
                thesis_b=graph_b.thesis_statement or "",
                agreement="unrelated",
                summary=f"Thesis comparison failed: {exc}",
            )

    try:
        pairs = await comparer.pair_claims(graph_a.claims, graph_b.claims)
    except Exception as exc:  # noqa: BLE001
        pairs = []
        # Surface the failure as a placeholder pair rationale so the
        # frontend doesn't silently show zero matches.
        pairs.append(SemanticPair(
            claim_a_id="",
            claim_a_text="",
            claim_b_id="",
            claim_b_text="",
            relationship="related",
            confidence="low",
            rationale=f"Claim pairing failed: {exc}",
        ))
    report.pairs = pairs

    paired_a = {p.claim_a_id for p in pairs if p.claim_a_id}
    paired_b = {p.claim_b_id for p in pairs if p.claim_b_id}
    report.unique_a = [
        {"claim_id": c.claim_id, "text": c.statement}
        for c in graph_a.claims if c.claim_id not in paired_a
    ]
    report.unique_b = [
        {"claim_id": c.claim_id, "text": c.statement}
        for c in graph_b.claims if c.claim_id not in paired_b
    ]
    return report


# ─── helpers ─────────────────────────────────────


def _load_graph(project_path: Path) -> AuthorGraph:
    graph_path = project_path / ".lattice" / "author_graph.json"
    if not graph_path.exists():
        raise FileNotFoundError(
            f"No author_graph.json at {graph_path} — "
            f"project must be scaffolded first."
        )
    return AuthorGraph.model_validate_json(
        graph_path.read_text(encoding="utf-8")
    )


def _structural_summary(
    project_path: Path, graph: AuthorGraph,
) -> StructuralSummary:
    return StructuralSummary(
        project_name=graph.project_name or project_path.name,
        section_count=len(graph.sections),
        claim_count=len(graph.claims),
        relationship_count=len(graph.relationships),
        thesis_statement=graph.thesis_statement,
        section_titles=[s.title for s in graph.sections],
    )


def _short(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"
