"""Differ: compare author graph and shadow graph → ShadowDiff entries.

Five diff types per SPEC §5.5:
1. unsupported_author_claim     — author claim whose cited sources yield weak/none bindings
2. contradicting_corpus_evidence — shadow contains a claim that contradicts an author claim
3. corpus_suggested_claim       — shadow claim with no semantic counterpart in author graph
4. structural_difference        — author section shape differs from shadow clustering
5. untouched_source             — source indexed but never cited by the author graph
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import (
    AuthorGraph,
    BindingStrength,
    Claim,
    RelationshipType,
    ShadowDiff,
    ShadowDiffType,
    Source,
)


_STOP = frozenset(
    "the a an of in on at to for and or but with by from as is are was were be "
    "been being have has had do does did this that these those it its their "
    "there which who whose what whom how when where why between among "
    "about over under up down out off through again further then once".split()
)

_OVERLAP_THRESHOLD = 0.35  # Jaccard on content tokens


class Differ:
    def __init__(self, project_path: Path) -> None:
        self.project_path = Path(project_path)

    def diff(
        self,
        author: AuthorGraph,
        shadow: AuthorGraph,
        sources: list[Source] | None = None,
    ) -> list[ShadowDiff]:
        diffs: list[ShadowDiff] = []
        diffs.extend(self._unsupported_author_claims(author))
        diffs.extend(self._contradicting_corpus_evidence(author, shadow))
        diffs.extend(self._corpus_suggested_claims(author, shadow))
        diffs.extend(self._structural_differences(author, shadow))
        if sources is not None:
            diffs.extend(self._untouched_sources(author, sources))
        return diffs

    # ─── 1. Unsupported author claims ──────────────────

    def _unsupported_author_claims(self, author: AuthorGraph) -> list[ShadowDiff]:
        out: list[ShadowDiff] = []
        for claim in author.claims:
            if not claim.evidence:
                continue
            # Flag when every cited source gave none/weak binding.
            if all(
                ev.binding_strength in (BindingStrength.none_, BindingStrength.weak)
                for ev in claim.evidence
            ):
                out.append(
                    _diff(
                        ShadowDiffType.unsupported_author_claim,
                        author_claim_id=claim.claim_id,
                        finding=(
                            f"Claim cites {len(claim.evidence)} source(s) but no "
                            "binding is strong; enricher did not find a supporting passage."
                        ),
                    )
                )
        return out

    # ─── 2. Contradicting corpus evidence ──────────────

    def _contradicting_corpus_evidence(
        self, author: AuthorGraph, shadow: AuthorGraph
    ) -> list[ShadowDiff]:
        out: list[ShadowDiff] = []
        # Look for shadow relationships of type 'contradicts' where the 'to' claim
        # semantically matches an author claim.
        shadow_by_id = {c.claim_id: c for c in shadow.claims}
        for rel in shadow.relationships:
            if rel.type != RelationshipType.contradicts:
                continue
            shadow_claim = shadow_by_id.get(rel.from_claim)
            shadow_target = shadow_by_id.get(rel.to_claim)
            if not shadow_claim or not shadow_target:
                continue
            match = _best_match(shadow_target.statement, author.claims)
            if match is None:
                continue
            out.append(
                _diff(
                    ShadowDiffType.contradicting_corpus_evidence,
                    author_claim_id=match.claim_id,
                    finding=(
                        f"Shadow claim {shadow_claim.claim_id!r} contradicts a corpus "
                        f"claim that matches this author claim."
                    ),
                    severity="important",
                )
            )
        return out

    # ─── 3. Corpus-suggested claims ────────────────────

    def _corpus_suggested_claims(
        self, author: AuthorGraph, shadow: AuthorGraph
    ) -> list[ShadowDiff]:
        out: list[ShadowDiff] = []
        for shadow_claim in shadow.claims:
            if _best_match(shadow_claim.statement, author.claims) is not None:
                continue
            out.append(
                _diff(
                    ShadowDiffType.corpus_suggested_claim,
                    author_claim_id=None,
                    finding=(
                        f"Shadow surfaced a claim the author has not made: "
                        f"{shadow_claim.statement[:120]!r}"
                    ),
                )
            )
        return out

    # ─── 4. Structural differences ─────────────────────

    def _structural_differences(
        self, author: AuthorGraph, shadow: AuthorGraph
    ) -> list[ShadowDiff]:
        out: list[ShadowDiff] = []
        author_sections = len([s for s in author.sections if s.section_id != "s.thesis"])
        shadow_sections = len([s for s in shadow.sections if s.section_id != "s.thesis"])
        if abs(author_sections - shadow_sections) >= 2:
            out.append(
                _diff(
                    ShadowDiffType.structural_difference,
                    author_claim_id=None,
                    finding=(
                        f"Author has {author_sections} body sections; shadow clusters "
                        f"the corpus into {shadow_sections} thematic groups."
                    ),
                )
            )
        return out

    # ─── 5. Untouched sources ──────────────────────────

    def _untouched_sources(
        self, author: AuthorGraph, sources: list[Source]
    ) -> list[ShadowDiff]:
        cited = {
            ev.source
            for claim in author.claims
            for ev in claim.evidence
            if ev.source
        }
        out: list[ShadowDiff] = []
        for src in sources:
            if src.source_id in cited:
                continue
            out.append(
                _diff(
                    ShadowDiffType.untouched_source,
                    author_claim_id=None,
                    finding=f"Source {src.source_id!r} is indexed but not cited by any author claim.",
                )
            )
        return out

    # ─── report writing ────────────────────────────────

    def write_report(self, diffs: list[ShadowDiff]) -> Path:
        reports_dir = self.project_path / ".lattice" / "shadow_reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        out_path = reports_dir / f"{ts}.md"

        lines = [f"# Shadow report — {ts}", "", f"Total flags: **{len(diffs)}**", ""]
        grouped: dict[str, list[ShadowDiff]] = {}
        for d in diffs:
            grouped.setdefault(d.type.value, []).append(d)
        for kind in (
            "unsupported_author_claim",
            "contradicting_corpus_evidence",
            "corpus_suggested_claim",
            "structural_difference",
            "untouched_source",
        ):
            bucket = grouped.get(kind, [])
            if not bucket:
                continue
            lines.append(f"## {kind.replace('_', ' ').title()} ({len(bucket)})")
            lines.append("")
            for d in bucket:
                if d.author_claim_id:
                    lines.append(f"- **{d.diff_id}** on `{d.author_claim_id}`")
                else:
                    lines.append(f"- **{d.diff_id}**")
                lines.append(f"  {d.shadow_finding}")
                lines.append("")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return out_path


# ─── helpers ────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text.lower())
        if t not in _STOP
    }


def _best_match(statement: str, candidates: list[Claim]) -> Claim | None:
    target = _tokens(statement)
    if not target:
        return None
    best: tuple[float, Claim | None] = (0.0, None)
    for c in candidates:
        ct = _tokens(c.statement)
        if not ct:
            continue
        overlap = target & ct
        union = target | ct
        jac = len(overlap) / len(union) if union else 0.0
        if jac > best[0]:
            best = (jac, c)
    return best[1] if best[0] >= _OVERLAP_THRESHOLD else None


def _diff(
    kind: ShadowDiffType,
    author_claim_id: str | None,
    finding: str,
    severity: str = "advisory",
) -> ShadowDiff:
    return ShadowDiff(
        diff_id=f"d.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.{uuid.uuid4().hex[:6]}",
        type=kind,
        author_claim_id=author_claim_id,
        shadow_finding=finding,
        related_shadow_passages=[],
        severity=severity,  # type: ignore[arg-type]
    )
