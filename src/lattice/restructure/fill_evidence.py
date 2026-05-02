"""Evidence-walkthrough helper.

Companion to ``fill_mechanisms``. Where ``fill_mechanisms`` walks
empirical/methodological claims missing a `[mechanism: ...]` tag,
``fill_evidence`` walks claims missing real evidence backing — the
``bind_evidence`` advisory class from the rescaffold planner.

For each weakly-grounded claim, the author picks one of four actions:

- **add_ref** — author knows a citekey; append ``[ref: <citekey>]``.
- **set_source_hint** — author has located the source but not bound a
  passage; append ``[evidence_status: source_hint]``.
- **set_unbound** — author explicitly acknowledges the gap; append
  ``[evidence_status: unbound]``.
- **convert_to_synthesis** — claim is actually author-original
  analysis, not an evidence-backed empirical claim; append
  ``[type: user_synthesis]``.

The author graph is NOT mutated — the outline file is the single edit
point. Re-ingest after to refresh the graph.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ..graph.models import (
    AuthorGraph,
    BindingStrength,
    ClaimType,
    EvidenceStatus,
    RelationshipType,
    ScaffoldReport,
)


# Default importance floor — same as fill_mechanisms. The CLI exposes
# --min-importance to filter further on annotated projects.
_DEFAULT_IMPORTANCE_FLOOR = 0.5

_BULLET_RE = re.compile(r"^(\s*-\s+)(.*)$")
_REF_TAG_RE = re.compile(r"\[ref\s*[:=]\s*([^\]]+)\]", re.IGNORECASE)
_EVIDENCE_STATUS_TAG_RE = re.compile(r"\[evidence_status\s*[:=]", re.IGNORECASE)
_TYPE_TAG_RE = re.compile(r"\[type\s*[:=]\s*([^\]]+)\]", re.IGNORECASE)
_USER_SYNTHESIS_FLAG_RE = re.compile(r"\[user_synthesis\]", re.IGNORECASE)


# Edges through which the planner walks support backwards from the
# thesis. Same set as in graph.metrics — kept here so this module
# doesn't need to import the metrics module just for the constant.
_SUPPORTING_EDGE_TYPES: frozenset[RelationshipType] = frozenset({
    RelationshipType.supports,
    RelationshipType.extends,
    RelationshipType.depends_on,
    RelationshipType.is_evidence_for,
})


EvidenceAction = Literal[
    "add_ref",
    "set_source_hint",
    "set_unbound",
    "convert_to_synthesis",
    "skip",
]


@dataclass
class EvidenceCandidate:
    """One claim worth an evidence prompt."""

    claim_id: str
    section_id: str | None
    statement: str
    importance: float
    line: int | None
    original_excerpt: str
    claim_type: str
    current_status: str | None  # None | "unbound" | "source_hint" | "bound"
    has_evidence_rows: bool
    is_supporter: bool  # transitively supports the thesis


@dataclass
class EvidenceEdit:
    """The author's decision for one candidate."""

    candidate: EvidenceCandidate
    action: EvidenceAction
    citekey: str | None = None  # only for add_ref


@dataclass
class FillEvidenceReport:
    project_name: str
    voice_name: str | None
    generated_at: datetime
    candidate_count: int
    edits_applied: int
    edits_skipped: int
    outline_path: str
    snapshot_path: str | None
    edits: list[dict]


# ─── candidate selection ─────────────────────────────


def collect_candidates(
    graph: AuthorGraph,
    report: ScaffoldReport,
    *,
    min_importance: float = _DEFAULT_IMPORTANCE_FLOOR,
    supporters_first: bool = True,
) -> list[EvidenceCandidate]:
    """Find every claim that's weakly grounded.

    A claim qualifies when:

    - Its type is empirical / methodological / normative / definition
      (user_synthesis claims are author-grounded by design).
    - It has neither evidence rows nor a non-default ``evidence_status``
      tag, OR its ``evidence_status`` is ``unbound`` / ``source_hint``
      and there are no strong-binding evidence rows.
    - Its importance is at or above ``min_importance``.

    When ``supporters_first`` is True (default), supporters of the
    thesis sort before non-supporters at the same importance — binding
    evidence on a supporter directly raises ``evidence_backing`` and
    therefore the strength score.
    """
    line_by_claim = {cr.claim_id: cr.line for cr in report.claim_reports}
    excerpt_by_claim = {
        cr.claim_id: cr.original_excerpt for cr in report.claim_reports
    }
    supporters = _supporters_of_thesis(graph)

    out: list[EvidenceCandidate] = []
    eligible_types = {
        ClaimType.empirical,
        ClaimType.methodological,
        ClaimType.normative,
        ClaimType.definition,
    }
    for claim in graph.claims:
        if claim.type not in eligible_types:
            continue
        if claim.importance < min_importance:
            continue
        if not _is_weakly_grounded(claim):
            continue
        out.append(EvidenceCandidate(
            claim_id=claim.claim_id,
            section_id=claim.section_id,
            statement=claim.statement,
            importance=float(claim.importance),
            line=line_by_claim.get(claim.claim_id),
            original_excerpt=excerpt_by_claim.get(claim.claim_id, ""),
            claim_type=claim.type.value,
            current_status=(
                claim.evidence_status.value if claim.evidence_status else None
            ),
            has_evidence_rows=bool(claim.evidence),
            is_supporter=claim.claim_id in supporters,
        ))

    def _sort_key(c: EvidenceCandidate) -> tuple:
        if supporters_first:
            return (0 if c.is_supporter else 1, -c.importance, c.claim_id)
        return (-c.importance, c.claim_id)

    out.sort(key=_sort_key)
    return out


def _is_weakly_grounded(claim) -> bool:
    """A claim is weakly grounded when it has no strong-binding
    evidence and no ``evidence_status=bound`` tag."""
    if claim.evidence_status == EvidenceStatus.bound:
        return False
    has_strong = any(
        ev.binding_strength == BindingStrength.strong
        for ev in claim.evidence
    )
    if has_strong and claim.evidence_status is None:
        return False
    # No evidence rows at all → weak. evidence_status in {None,
    # source_hint, unbound} → weak (the author hasn't bound it).
    return True


def _supporters_of_thesis(graph: AuthorGraph) -> set[str]:
    """BFS backwards from cl.thesis through supporting edges. Returns
    the set of transitively-supporting claim_ids."""
    if not any(c.claim_id == "cl.thesis" for c in graph.claims):
        return set()
    inbound: dict[str, list] = defaultdict(list)
    for rel in graph.relationships:
        inbound[rel.to_claim].append(rel)
    seen: set[str] = {"cl.thesis"}
    queue: deque[str] = deque(["cl.thesis"])
    while queue:
        node = queue.popleft()
        for rel in inbound[node]:
            if rel.type not in _SUPPORTING_EDGE_TYPES:
                continue
            if rel.from_claim in seen:
                continue
            seen.add(rel.from_claim)
            queue.append(rel.from_claim)
    seen.discard("cl.thesis")
    return seen


# ─── apply ──────────────────────────────────────────


def apply_evidence_edits(
    outline_path: Path,
    edits: list[EvidenceEdit],
    *,
    snapshot: bool = True,
) -> FillEvidenceReport:
    """Apply each edit by appending the appropriate tag to the bullet.

    Idempotent: skips edits that would duplicate an already-present tag
    (e.g. trying to add ``[ref: x]`` to a bullet that already has it,
    or setting a status the bullet already has).

    Snapshots ``outline_path`` to ``<stem>.pre-fill-evidence<suffix>``
    before any edits when ``snapshot=True``.
    """
    text = outline_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    snapshot_path: Path | None = None
    if snapshot:
        snapshot_path = outline_path.parent / (
            outline_path.stem + ".pre-fill-evidence" + outline_path.suffix
        )
        snapshot_path.write_text(text, encoding="utf-8")

    edits_applied: list[dict] = []
    edits_skipped: list[dict] = []

    for edit in edits:
        action = edit.action
        cand = edit.candidate
        if action == "skip":
            edits_skipped.append({
                "claim_id": cand.claim_id,
                "reason": "skipped_by_user",
            })
            continue
        line_no = cand.line
        if line_no is None or line_no < 1 or line_no > len(lines):
            edits_skipped.append({
                "claim_id": cand.claim_id,
                "reason": "no_line_number",
            })
            continue
        original_line = lines[line_no - 1]
        stripped = original_line.rstrip("\r\n")
        if not _BULLET_RE.match(stripped):
            edits_skipped.append({
                "claim_id": cand.claim_id,
                "reason": "line_is_not_a_bullet",
            })
            continue

        new_line, reason = _apply_one(stripped, edit)
        if new_line is None:
            edits_skipped.append({
                "claim_id": cand.claim_id,
                "reason": reason or "no_op",
            })
            continue

        ending = original_line[len(stripped):]
        lines[line_no - 1] = new_line + ending
        edits_applied.append({
            "claim_id": cand.claim_id,
            "section_id": cand.section_id,
            "line": line_no,
            "action": action,
            "citekey": edit.citekey,
        })

    if edits_applied:
        outline_path.write_text("".join(lines), encoding="utf-8")

    return FillEvidenceReport(
        project_name="",  # caller fills
        voice_name=None,
        generated_at=datetime.now(timezone.utc),
        candidate_count=len(edits),
        edits_applied=len(edits_applied),
        edits_skipped=len(edits_skipped),
        outline_path=str(outline_path),
        snapshot_path=str(snapshot_path) if snapshot_path else None,
        edits=edits_applied + edits_skipped,
    )


def _apply_one(stripped: str, edit: EvidenceEdit) -> tuple[str | None, str | None]:
    """Apply one edit to one bullet line. Returns (new_line, None) on
    success or (None, reason) when the edit should be skipped (e.g.
    tag already present). The reason is used for the decisions log."""
    action = edit.action
    if action == "add_ref":
        citekey = (edit.citekey or "").strip()
        if not citekey:
            return None, "missing_citekey"
        # Sanitise — citekeys are underscore-joined, so squeeze
        # whitespace and disallow brackets.
        citekey = _sanitise_citekey(citekey)
        if not citekey:
            return None, "empty_citekey_after_sanitise"
        existing_keys = _existing_ref_keys(stripped)
        if citekey in existing_keys:
            return None, "ref_already_present"
        return f"{stripped} [ref: {citekey}]", None
    if action == "set_source_hint":
        if _EVIDENCE_STATUS_TAG_RE.search(stripped):
            return None, "evidence_status_tag_already_present"
        return f"{stripped} [evidence_status: source_hint]", None
    if action == "set_unbound":
        if _EVIDENCE_STATUS_TAG_RE.search(stripped):
            return None, "evidence_status_tag_already_present"
        return f"{stripped} [evidence_status: unbound]", None
    if action == "convert_to_synthesis":
        if _USER_SYNTHESIS_FLAG_RE.search(stripped):
            return None, "user_synthesis_already_present"
        existing_type = _TYPE_TAG_RE.search(stripped)
        if existing_type:
            current = existing_type.group(1).strip().lower()
            if current == "user_synthesis":
                return None, "user_synthesis_already_present"
            # Replace existing [type: ...] with [type: user_synthesis]
            new = _TYPE_TAG_RE.sub("[type: user_synthesis]", stripped, count=1)
            return new, None
        return f"{stripped} [type: user_synthesis]", None
    return None, f"unknown_action:{action}"


def _existing_ref_keys(line: str) -> set[str]:
    """Return the set of citekeys already in ``[ref: ...]`` tags on
    this line (comma-split, lowercased for comparison)."""
    keys: set[str] = set()
    for match in _REF_TAG_RE.finditer(line):
        for raw in match.group(1).split(","):
            cleaned = raw.strip().lower()
            if cleaned:
                keys.add(cleaned)
    return keys


def _sanitise_citekey(citekey: str) -> str:
    """Citekeys can't contain ``[``, ``]``, commas, or unescaped
    whitespace. Replace bad characters with underscores and collapse."""
    citekey = re.sub(r"[\[\],\s]+", "_", citekey).strip("_").lower()
    return citekey
