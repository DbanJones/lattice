"""Pydantic models for the metrics-driven rescaffold planner.

Separate from ``restructure.RestructureSuggestion`` (which is the
LLM-driven section-ordering advisor) — this module captures
deterministic, metric-driven structural moves with predicted score
deltas. Every operation is purely advisory; nothing is ever applied
without explicit author confirmation through a separate apply step.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# Operation kinds — keep small + sharp. Anything more nuanced is an
# Advisory rather than an Operation.
OperationKind = Literal[
    "move_claim",                # move one claim from section A to section B
    "split_section",             # break a section into N subsections
    "merge_sections",            # combine two adjacent sections
    "add_section_stub",          # propose a new (empty) section
    "reorder_within_section",    # change cluster/claim order inside a section
    "promote_to_offcuts",        # advisory removal — claim moves to offcuts.json
]

AdvisoryKind = Literal[
    "bind_evidence",             # weak-grounding supporter needs evidence
    "add_mechanism",             # high-importance empirical claim missing mechanism
    "add_synthesis",             # section closer needs a user_synthesis claim
    "add_methodological_framing",  # claim_type_diversity gap
    "diversify_sources",         # one source over-represented on a claim
    "add_counter_engagement",    # unaddressed counter needs a pivot/qualifier
    "tag_supports_thesis",       # high-importance unconnected claim near thesis
    "infer_relationships",       # run relationship inference to lift type diversity
]


class RescaffoldOperation(BaseModel):
    """A concrete structural move the rescaffold planner proposes.

    Every operation carries a confidence in [0, 1] and an
    ``expected_delta`` map showing which metric sub-scores it would
    move (and by how much). The planner sorts by predicted-delta
    magnitude and presents to the author in priority order.
    """

    op_id: str
    kind: OperationKind
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)

    # Op-specific fields. Most are optional because each kind uses
    # only a subset; the kind drives validation in the planner.
    target_claim_id: str | None = None
    source_section_id: str | None = None
    target_section_id: str | None = None  # may be a synthetic id like "new:counter-engagement"
    new_section_role: str | None = None
    new_section_title: str | None = None
    section_ids_to_merge: list[str] = Field(default_factory=list)
    split_groups: list[list[str]] = Field(default_factory=list)
    # For reorder_within_section — the new claim_id order within the section.
    claim_order: list[str] = Field(default_factory=list)
    # For move_claim — where in the target section's sequence the claim
    # should land (None = append).
    target_position: int | None = None

    # Predicted change in metric sub-scores if this single op were
    # applied in isolation. Keys use dotted form: "strength.score",
    # "strength.counter_handling", "breadth.section_spread", etc.
    expected_delta: dict[str, float] = Field(default_factory=dict)


class RescaffoldAdvisory(BaseModel):
    """A non-structural recommendation the planner surfaces alongside
    operations. Advisories don't reshape the document; they tell the
    author what to do at the claim level (bind evidence, add mechanism,
    diversify sources, etc.) so the next plan iteration scores higher.
    """

    advisory_id: str
    kind: AdvisoryKind
    target_claim_id: str | None = None
    target_section_id: str | None = None
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    suggestion: str = ""


class RescaffoldDiagnosis(BaseModel):
    """Per-sub-score record of what the metrics flagged."""

    dimension: Literal["strength", "breadth"]
    sub_score: str
    value: float
    threshold: float
    severity: Literal["info", "warning", "critical"]
    message: str


class RescaffoldPlan(BaseModel):
    """Top-level artefact persisted to ``.lattice/rescaffold_plan.json``.

    The structural narrative the planner produces:
    - ``diagnosis`` says what the current metrics flag as broken,
    - ``operations`` are the structural moves that would fix it,
    - ``advisories`` are claim-level recommendations to lift sub-scores
      that no single structural move can address,
    - ``current_metrics`` and ``predicted_metrics`` show where the
      argument stands now and where the planner predicts it would land
      if every operation were accepted.
    """

    project_name: str
    voice_name: str
    generated_at: datetime
    diagnosis: list[RescaffoldDiagnosis] = Field(default_factory=list)
    operations: list[RescaffoldOperation] = Field(default_factory=list)
    advisories: list[RescaffoldAdvisory] = Field(default_factory=list)
    proposed_offcuts: list[str] = Field(default_factory=list)
    current_metrics: dict | None = None    # ArgumentMetrics.model_dump()
    predicted_metrics: dict | None = None  # same shape
    # Per-claim claim_size scores included verbatim so the consumer
    # doesn't have to recompute them to interpret the plan.
    claim_sizes: dict[str, float] = Field(default_factory=dict)
    # High-level summary of expected score movement.
    expected_strength_delta: float = 0.0
    expected_breadth_delta: float = 0.0
