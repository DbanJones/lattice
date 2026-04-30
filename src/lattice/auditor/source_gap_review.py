"""Source-gap review: compare a rendered paper to a reference document.

Distinct from the per-cluster auditor (style and structure) and the
voice compliance review (whole-document statistics). This pass takes
two documents — the rendered paper and a richer reference text the
author considers authoritative — and surfaces the *content* that the
reference carries but the render lacks.

Categories of gap:

- **quantitative**: specific numbers, percentages, units, or named
  thresholds in the reference but missing from the render
- **named_scholar**: scholars cited by surname in the reference whom
  the render does not engage by name
- **mechanism**: causal pathways or named theories ("dark silicon",
  "Jevons paradox", "split incentive") explained in the reference but
  flat in the render
- **analytical_move**: interpretive pivots ("reading X as Y mistakes
  A for B") in the reference that the render reduces to description
- **arithmetic**: step-by-step calculations in the reference that
  the render abstracts into prose
- **named_example**: concrete examples (Phoenix vs Stockholm,
  Hyperion campus, the 2000s telecom boom) the reference uses to
  ground arguments and the render omits
- **structural**: headings, tables, or scaffolding the reference
  carries that are absent from the render

Output: a markdown report ``outputs/source_gap_review.<voice>.md``
listing each gap with the reference snippet, a brief description, and
a category. The author reads this and decides what to add to the
graph; nothing is auto-injected.

One LLM call per chunk of the reference (chunked because reference
documents are typically 10k+ words). Uses Opus by default since this
is an analytical review pass — quality matters more than cost.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..graph.models import AuthorGraph
from ..utils.config import Config


class _LLMProtocol(Protocol):
    async def complete_json(
        self, system: str, user: str, model: str | None = None, temperature: float = 0.2
    ) -> tuple[object, object]: ...


_VALID_CATEGORIES = {
    "quantitative",
    "named_scholar",
    "mechanism",
    "analytical_move",
    "arithmetic",
    "named_example",
    "structural",
}


@dataclass
class Gap:
    gap_id: str
    category: str
    summary: str
    reference_snippet: str
    suggested_action: str = ""
    # The claim the gap most likely attaches to. May be empty if the LLM
    # could not match it confidently. Used by source-review-apply to
    # decide where to inject accepted gaps.
    target_claim_id: str = ""
    # Author decision (set by source-review-apply, not by review):
    # "accepted" | "rejected" | "deferred" | None.
    decision: str | None = None

    def to_dict(self) -> dict:
        return {
            "gap_id": self.gap_id,
            "category": self.category,
            "summary": self.summary,
            "reference_snippet": self.reference_snippet,
            "suggested_action": self.suggested_action,
            "target_claim_id": self.target_claim_id,
            "decision": self.decision,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Gap":
        return cls(
            gap_id=str(data.get("gap_id") or _short_uid()),
            category=str(data.get("category") or ""),
            summary=str(data.get("summary") or ""),
            reference_snippet=str(data.get("reference_snippet") or ""),
            suggested_action=str(data.get("suggested_action") or ""),
            target_claim_id=str(data.get("target_claim_id") or ""),
            decision=data.get("decision"),
        )


def _short_uid() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class SourceGapReport:
    paper_path: Path
    reference_path: Path
    gaps: list[Gap] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "paper_path": str(self.paper_path),
            "reference_path": str(self.reference_path),
            "gaps": [g.to_dict() for g in self.gaps],
        }, indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "SourceGapReport":
        data = json.loads(text)
        return cls(
            paper_path=Path(data.get("paper_path") or ""),
            reference_path=Path(data.get("reference_path") or ""),
            gaps=[Gap.from_dict(g) for g in (data.get("gaps") or [])],
        )

    @property
    def by_category(self) -> dict[str, list[Gap]]:
        out: dict[str, list[Gap]] = {}
        for g in self.gaps:
            out.setdefault(g.category, []).append(g)
        return out

    def to_markdown(self) -> str:
        lines = [
            f"# Source gap review",
            "",
            f"Comparison of `{self.paper_path.name}` against reference "
            f"`{self.reference_path.name}`.",
            "",
            f"**{len(self.gaps)} gap(s) identified** across "
            f"{len(self.by_category)} categor(y/ies).",
            "",
        ]
        category_order = [
            "analytical_move",
            "mechanism",
            "quantitative",
            "arithmetic",
            "named_scholar",
            "named_example",
            "structural",
        ]
        category_titles = {
            "analytical_move": "Analytical moves the render flattens",
            "mechanism": "Mechanisms / named theories the render lacks",
            "quantitative": "Specific numbers the render omits",
            "arithmetic": "Step-by-step working the render abstracts",
            "named_scholar": "Scholars the render does not engage by name",
            "named_example": "Concrete examples the render omits",
            "structural": "Structure / scaffolding the render lacks",
        }
        by_cat = self.by_category
        for cat in category_order:
            gaps = by_cat.get(cat, [])
            if not gaps:
                continue
            lines.append(f"## {category_titles[cat]}")
            lines.append("")
            lines.append(f"_{len(gaps)} gap(s)._")
            lines.append("")
            for i, g in enumerate(gaps, start=1):
                lines.append(f"### {i}. {g.summary}")
                lines.append("")
                lines.append(f"**Reference text**:")
                lines.append("")
                lines.append(f"> {g.reference_snippet}")
                lines.append("")
                if g.suggested_action:
                    lines.append(f"**Suggested action**: {g.suggested_action}")
                    lines.append("")
        return "\n".join(lines)


_SYSTEM_PROMPT = """\
You compare a rendered academic paper against a richer reference \
document the author considers authoritative, and surface the *content* \
that the reference carries but the rendered paper lacks.

Categories of gap:

- quantitative: specific numbers, percentages, units, or named \
  thresholds in the reference but missing from the render (e.g. "21% of \
  transistors at 8nm", "70% wholesale price collapse", "PUE of 1.1 to 1.2")
- named_scholar: scholars cited by surname in the reference whom the \
  render does not engage by name (e.g. Sorrell, Freitag, Esmaeilzadeh)
- mechanism: causal pathways or named theories explained in the \
  reference but flat or absent in the render (e.g. "dark silicon", \
  "Jevons paradox with backfire vs partial rebound", "split incentive")
- analytical_move: interpretive pivots in the reference that the \
  render reduces to description (e.g. "reading the 10⁶× gap as room \
  for improvement mistakes distance for speed")
- arithmetic: step-by-step calculations in the reference that the \
  render abstracts into prose (e.g. "10 Wh × 200 gCO₂/kWh = 2 g; \
  vs 2,000 gCO₂/kWh = 20 g")
- named_example: concrete examples (cities, named campuses, named \
  events) the reference uses to ground arguments and the render omits
- structural: headings, tables, scaffolding ("Why it matters" frames, \
  numbered subsections) the reference carries that are absent from the \
  render

Be specific. A useful gap entry names what is missing in the render \
*and* quotes the exact reference passage that carries it. Do NOT flag \
gaps where the render covers the same ground in different words. Flag \
only material content the render lacks.

Return JSON: {
  "gaps": [
    {
      "category": "quantitative|named_scholar|mechanism|analytical_move|arithmetic|named_example|structural",
      "summary": "one short sentence naming what is missing",
      "reference_snippet": "verbatim quote from the reference, 30-200 chars",
      "suggested_action": "what the author could add to the graph to recover this (one sentence)",
      "target_claim_id": "the claim_id from the provided list that this gap most plausibly attaches to, or empty string if no good match"
    }
  ]
}

The user turn provides a list of claim_ids with their statements. Use them \
to populate target_claim_id — pick the single claim where injecting the \
missing material would make the most sense. Empty target_claim_id is \
acceptable when the gap is structural (a missing section) or when no \
existing claim is close enough.

Aim for 10-30 high-value gaps. Quality over quantity. Skip trivial \
phrasing differences. Skip gaps where the render's wording differs but \
the content is equivalent. Only flag what the render actually misses.
"""


class SourceGapReview:
    def __init__(self, config: Config, llm: _LLMProtocol) -> None:
        self.config = config
        self.llm = llm

    async def review(
        self,
        paper_path: Path,
        reference_path: Path,
        graph: AuthorGraph | None = None,
    ) -> SourceGapReport:
        paper_text = paper_path.read_text(encoding="utf-8")
        reference_text = reference_path.read_text(encoding="utf-8")

        report = SourceGapReport(paper_path=paper_path, reference_path=reference_path)

        # Reference is typically 10-15k words. Chunk it so each LLM call
        # gets a manageable slice + the full paper for comparison.
        ref_chunks = _chunk_by_words(reference_text, target_words=3500)

        # Compose a compact claim catalogue so the LLM can populate
        # target_claim_id per gap. Skip the thesis claim and any with a
        # `skip` tag.
        claim_catalogue = _format_claim_catalogue(graph) if graph else ""

        for chunk_index, ref_chunk in enumerate(ref_chunks, start=1):
            user = (
                f"<rendered_paper>\n{paper_text}\n</rendered_paper>\n\n"
                + (
                    f"<claims>\n{claim_catalogue}\n</claims>\n\n"
                    if claim_catalogue
                    else ""
                )
                + f"<reference_chunk index='{chunk_index}' of='{len(ref_chunks)}'>\n"
                f"{ref_chunk}\n"
                f"</reference_chunk>\n\n"
                "Identify gaps where the reference chunk carries content the "
                "rendered paper lacks. Return JSON per the schema in the system "
                "turn. For each gap, pick a target_claim_id from the <claims> "
                "list when one is plausibly the right attachment point; "
                "otherwise leave target_claim_id empty. Skip the introduction "
                "/ abstract / references list of the reference if encountered "
                "— focus on argumentative content."
            )
            try:
                payload, _ = await self.llm.complete_json(
                    system=_SYSTEM_PROMPT,
                    user=user,
                    model=self.config.model_for_stage("examiner"),
                    temperature=0.3,
                )
            except Exception as exc:
                # One failed chunk shouldn't kill the whole review.
                report.gaps.append(Gap(
                    gap_id=_short_uid(),
                    category="structural",
                    summary=f"Reference chunk {chunk_index}: review failed",
                    reference_snippet=str(exc)[:200],
                ))
                continue

            if not isinstance(payload, dict):
                continue
            entries = payload.get("gaps") or []
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                cat = str(entry.get("category") or "").strip()
                if cat not in _VALID_CATEGORIES:
                    continue
                summary = str(entry.get("summary") or "").strip()
                snippet = str(entry.get("reference_snippet") or "").strip()
                if not summary or not snippet:
                    continue
                report.gaps.append(Gap(
                    gap_id=_short_uid(),
                    category=cat,
                    summary=summary[:400],
                    reference_snippet=snippet[:600],
                    suggested_action=str(entry.get("suggested_action") or "").strip()[:400],
                    target_claim_id=str(entry.get("target_claim_id") or "").strip(),
                ))

        return report


def _format_claim_catalogue(graph: AuthorGraph) -> str:
    """Compact list of claim_ids + first 160 chars of statement.

    Skips the thesis claim and any with a `skip` tag.
    """
    lines: list[str] = []
    for c in graph.claims:
        if c.claim_id == "cl.thesis":
            continue
        if "skip" in c.tags:
            continue
        statement = " ".join((c.statement or "").split())[:160]
        lines.append(f"  <claim id=\"{c.claim_id}\">{statement}</claim>")
    return "\n".join(lines)


def _chunk_by_words(text: str, target_words: int = 3500) -> list[str]:
    """Split ``text`` into chunks of roughly ``target_words`` words on
    paragraph boundaries. Preserves whole paragraphs."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for para in paragraphs:
        para_words = len(para.split())
        if current_words + para_words > target_words and current:
            chunks.append("\n\n".join(current))
            current = [para]
            current_words = para_words
        else:
            current.append(para)
            current_words += para_words
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def write_report(
    report: SourceGapReport,
    project_path: Path,
    voice_name: str,
) -> Path:
    """Write the review to outputs/source_gap_review.<voice>.md (human
    readable) and .lattice/source_gap_review.<voice>.json (machine
    readable, consumed by source-review-apply).
    """
    md_path = project_path / "outputs" / f"source_gap_review.{voice_name}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(report.to_markdown(), encoding="utf-8")

    json_path = project_path / ".lattice" / f"source_gap_review.{voice_name}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(report.to_json(), encoding="utf-8")
    return md_path


def load_report(project_path: Path, voice_name: str) -> SourceGapReport | None:
    """Load the structured source-gap report. Returns None if not found."""
    json_path = project_path / ".lattice" / f"source_gap_review.{voice_name}.json"
    if not json_path.exists():
        return None
    return SourceGapReport.from_json(json_path.read_text(encoding="utf-8"))


def save_report(
    report: SourceGapReport,
    project_path: Path,
    voice_name: str,
) -> Path:
    """Persist the structured report back to .lattice/ after edits.
    Used by source-review-apply to record decisions."""
    json_path = project_path / ".lattice" / f"source_gap_review.{voice_name}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(report.to_json(), encoding="utf-8")
    return json_path
