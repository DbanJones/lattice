"""Enrichment coverage report (Fix 3 of the pipeline-integrity brief).

Surfaces unbound claims to the author before rendering. Halts the
pipeline until every unbound claim has an explicit resolution decision.
Without this gate, empirical-sounding claims with no source binding
quietly slip into the renderer, which then either fabricates citations
or flags MISSING_CLAIM markers — both equally bad for the author.

Resolution options (per claim):

- ``mark_user_synthesis`` — author's own observation; flips the claim
  to ``type=user_synthesis`` with ``author_origin=True``. The renderer
  will then count it as grounded under Fix 2.
- ``needs_new_source`` — author commits to dropping a source PDF into
  ``refs/papers/`` and re-running enrich.
- ``soften_to_hedged`` — claim is rewritten to a more hedged form;
  optionally accompanied by a new statement.
- ``remove_from_graph`` — claim is deleted from the graph entirely.
- ``accept_gap`` — author accepts the gap; the renderer will emit a
  MISSING_CLAIM marker and the cluster will be ``needs_review``.
- ``pending`` — no decision yet; pipeline cannot advance to render.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ..graph.models import (
    AuthorGraph,
    BindingStrength,
    Claim,
    ClaimType,
    Confidence,
)
from ..graph.store import GraphStore


# ─── Stats and records ─────────────────────────────

@dataclass
class CoverageStats:
    total_claims: int
    strong_bindings: int
    weak_bindings: int
    no_bindings: int
    contradictory_bindings: int

    @property
    def strong_pct(self) -> float:
        return self.strong_bindings / self.total_claims if self.total_claims else 0.0

    @property
    def coverage_pct(self) -> float:
        bound = self.strong_bindings + self.weak_bindings
        return bound / self.total_claims if self.total_claims else 0.0


class ClaimResolution(str, Enum):
    mark_user_synthesis = "mark_user_synthesis"
    needs_new_source = "needs_new_source"
    soften_to_hedged = "soften_to_hedged"
    remove_from_graph = "remove_from_graph"
    accept_gap = "accept_gap"
    pending = "pending"


@dataclass
class UnboundClaimRecord:
    claim_id: str
    statement: str
    type: ClaimType
    section_id: str | None
    cited_sources: list[str] = field(default_factory=list)
    enrichment_notes: list[str] = field(default_factory=list)
    resolution: ClaimResolution = ClaimResolution.pending
    resolution_at: datetime | None = None
    new_statement: str | None = None  # for soften_to_hedged

    def to_json(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "statement": self.statement,
            "type": self.type.value,
            "section_id": self.section_id,
            "cited_sources": list(self.cited_sources),
            "enrichment_notes": list(self.enrichment_notes),
            "resolution": self.resolution.value,
            "resolution_at": self.resolution_at.isoformat() if self.resolution_at else None,
            "new_statement": self.new_statement,
        }

    @classmethod
    def from_json(cls, data: dict) -> "UnboundClaimRecord":
        return cls(
            claim_id=data["claim_id"],
            statement=data["statement"],
            type=ClaimType(data["type"]),
            section_id=data.get("section_id"),
            cited_sources=list(data.get("cited_sources") or []),
            enrichment_notes=list(data.get("enrichment_notes") or []),
            resolution=ClaimResolution(data.get("resolution") or "pending"),
            resolution_at=(
                datetime.fromisoformat(data["resolution_at"])
                if data.get("resolution_at")
                else None
            ),
            new_statement=data.get("new_statement"),
        )


@dataclass
class CoverageReport:
    stats: CoverageStats
    unbound: list[UnboundClaimRecord] = field(default_factory=list)
    weak_bound: list[UnboundClaimRecord] = field(default_factory=list)
    contradictory: list[UnboundClaimRecord] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def all_resolved(self) -> bool:
        return all(
            c.resolution != ClaimResolution.pending
            for c in self.unbound + self.contradictory
        )

    @property
    def can_proceed_to_render(self) -> bool:
        """Pipeline advances when every unbound and contradictory claim has a decision."""
        return self.all_resolved


# ─── Reporter ──────────────────────────────────────

class EnrichmentReporter:
    """Produces the coverage report and persists it to .lattice/."""

    def __init__(self, store: GraphStore, project_path: Path) -> None:
        self.store = store
        self.project_path = Path(project_path)
        self.report_path = self.project_path / ".lattice" / "enrichment_coverage.json"

    def generate_report(self) -> CoverageReport:
        claims = self.store.list_claims()
        # Don't count author-grounded claims as "unbound" — they're grounded
        # by being explicit author opinions. Same as the renderer's grounding rule.
        renderable_claims = [c for c in claims if c.claim_id != "cl.thesis"]

        unbound = [
            self._claim_to_record(c) for c in renderable_claims
            if self._has_no_binding(c) and not self._is_author_grounded(c)
        ]
        weak = [
            self._claim_to_record(c) for c in renderable_claims
            if self._has_only_weak_binding(c)
        ]
        contradictory = [
            self._claim_to_record(c) for c in renderable_claims
            if self._has_contradictory_binding(c)
        ]

        stats = CoverageStats(
            total_claims=len(renderable_claims),
            strong_bindings=sum(1 for c in renderable_claims if self._has_strong_binding(c)),
            weak_bindings=len(weak),
            no_bindings=len(unbound),
            contradictory_bindings=len(contradictory),
        )

        prior = self._load_prior_decisions()
        for record in unbound + contradictory:
            saved = prior.get(record.claim_id)
            if saved is not None and saved.resolution != ClaimResolution.pending:
                record.resolution = saved.resolution
                record.resolution_at = saved.resolution_at
                record.new_statement = saved.new_statement

        return CoverageReport(
            stats=stats,
            unbound=unbound,
            weak_bound=weak,
            contradictory=contradictory,
        )

    def save_report(self, report: CoverageReport) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stats": asdict(report.stats),
            "generated_at": report.generated_at.isoformat(),
            "unbound": [r.to_json() for r in report.unbound],
            "weak_bound": [r.to_json() for r in report.weak_bound],
            "contradictory": [r.to_json() for r in report.contradictory],
        }
        self.report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def update_resolution(
        self,
        claim_id: str,
        resolution: ClaimResolution,
        new_statement: str | None = None,
    ) -> None:
        """Apply the resolution and persist it.

        For ``mark_user_synthesis``: also flip the claim's type and
        ``author_origin``.
        For ``soften_to_hedged``: also update the claim's statement to
        ``new_statement`` if provided.
        For ``remove_from_graph``: remove the claim from the graph.
        Other resolutions just record the decision.
        """
        # Apply graph-level effects first.
        if resolution == ClaimResolution.mark_user_synthesis:
            try:
                claim = self.store.get_claim(claim_id)
            except KeyError:
                return
            claim.type = ClaimType.user_synthesis
            claim.author_origin = True
            self.store.save_claim(claim)
        elif resolution == ClaimResolution.soften_to_hedged and new_statement:
            try:
                claim = self.store.get_claim(claim_id)
            except KeyError:
                return
            claim.statement = new_statement
            # Hedged claims default to medium confidence unless already low.
            if claim.confidence not in (Confidence.low, Confidence.speculative):
                claim.confidence = Confidence.medium
            self.store.save_claim(claim)
        elif resolution == ClaimResolution.remove_from_graph:
            try:
                self.store.delete_claim(claim_id)
            except Exception:
                return

        # Persist the decision in the report file (regenerate to reflect graph state).
        report = self.generate_report()
        # The previous record may already be gone (remove_from_graph) — append
        # a decision-log style entry so subsequent reports remember.
        record_index: dict[str, UnboundClaimRecord] = {
            r.claim_id: r for r in report.unbound + report.contradictory
        }
        target = record_index.get(claim_id)
        if target is not None:
            target.resolution = resolution
            target.resolution_at = datetime.now(timezone.utc)
            if new_statement:
                target.new_statement = new_statement
        self.save_report(report)
        self._append_decision_log(claim_id, resolution, new_statement)

    # ─── binding-status predicates ───────────────

    @staticmethod
    def _has_strong_binding(claim: Claim) -> bool:
        return any(
            e.binding_strength == BindingStrength.strong for e in claim.evidence
        )

    @staticmethod
    def _has_only_weak_binding(claim: Claim) -> bool:
        if any(e.binding_strength == BindingStrength.strong for e in claim.evidence):
            return False
        return any(
            e.binding_strength == BindingStrength.weak for e in claim.evidence
        )

    @staticmethod
    def _has_no_binding(claim: Claim) -> bool:
        # "No binding" is disjoint from "contradictory binding": a claim with
        # only contradictory evidence belongs in the contradictory bucket so
        # the totals don't double-count.
        if not claim.evidence:
            return True
        return all(
            e.binding_strength == BindingStrength.none_
            for e in claim.evidence
        )

    @staticmethod
    def _has_contradictory_binding(claim: Claim) -> bool:
        return any(
            e.binding_strength == BindingStrength.contradictory
            for e in claim.evidence
        )

    @staticmethod
    def _is_author_grounded(claim: Claim) -> bool:
        return (
            claim.type == ClaimType.user_synthesis
            and claim.author_origin
        )

    # ─── helpers ────────────────────────────────

    @staticmethod
    def _claim_to_record(claim: Claim) -> UnboundClaimRecord:
        return UnboundClaimRecord(
            claim_id=claim.claim_id,
            statement=claim.statement,
            type=claim.type,
            section_id=claim.section_id,
            cited_sources=[e.source for e in claim.evidence if e.source],
            enrichment_notes=[
                e.quote_text for e in claim.evidence
                if e.quote_text and "error" in (e.quote_text or "").lower()
            ],
        )

    def _load_prior_decisions(self) -> dict[str, UnboundClaimRecord]:
        if not self.report_path.exists():
            return {}
        try:
            data = json.loads(self.report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        records: dict[str, UnboundClaimRecord] = {}
        for key in ("unbound", "contradictory"):
            for entry in data.get(key, []) or []:
                try:
                    record = UnboundClaimRecord.from_json(entry)
                    records[record.claim_id] = record
                except Exception:
                    continue
        return records

    def _append_decision_log(
        self,
        claim_id: str,
        resolution: ClaimResolution,
        new_statement: str | None,
    ) -> None:
        log_path = (
            self.project_path / ".lattice" / "enrichment_decisions.json"
        )
        log: list[dict] = []
        if log_path.exists():
            try:
                log = json.loads(log_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log = []
        log.append({
            "claim_id": claim_id,
            "resolution": resolution.value,
            "new_statement": new_statement,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
