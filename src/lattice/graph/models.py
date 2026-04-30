"""Pydantic models for every Lattice entity.

This is the source of truth for data shapes. See docs/DATA_MODEL.md for
the full schema documentation.

All models are pydantic v2. They serialise to JSON with .model_dump_json()
and deserialise with Model.model_validate_json().
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────────────────────────────────────
# Source and Passage
# ─────────────────────────────────────────────────────────


class SourceType(str, Enum):
    primary_paper = "primary_paper"
    review_paper = "review_paper"
    report = "report"
    dataset = "dataset"
    web_page = "web_page"
    note = "note"
    prior_writing = "prior_writing"
    interview = "interview"


class PassageType(str, Enum):
    claim = "claim"
    figure_caption = "figure_caption"
    table_cell = "table_cell"
    method = "method"
    conclusion = "conclusion"
    quote = "quote"
    data_point = "data_point"


class PassageLocation(BaseModel):
    page: int | None = None
    section: str | None = None
    paragraph: int | None = None
    line: int | None = None
    cell: str | None = None


class Passage(BaseModel):
    id: str
    text: str
    location: PassageLocation
    type: PassageType
    char_count: int


class Citation(BaseModel):
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    title: str
    container: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    url: str | None = None


class SourceMetadata(BaseModel):
    peer_reviewed: bool = False
    primary: bool = False
    date_added: datetime
    file_path: str
    hash: str
    ocr_used: bool = False
    indexer_version: str = "0.1.0"


class Source(BaseModel):
    source_id: str
    type: SourceType
    citation: Citation
    passages: list[Passage] = Field(default_factory=list)
    metadata: SourceMetadata


# ─────────────────────────────────────────────────────────
# Claim and Evidence
# ─────────────────────────────────────────────────────────


class ClaimType(str, Enum):
    empirical = "empirical"
    methodological = "methodological"
    normative = "normative"
    user_synthesis = "user_synthesis"
    definition = "definition"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"
    speculative = "speculative"


class BindingStrength(str, Enum):
    strong = "strong"
    weak = "weak"
    none_ = "none"
    contradictory = "contradictory"


class Evidence(BaseModel):
    source: str  # source_id
    passage: str  # passage_id
    binding_strength: BindingStrength = BindingStrength.weak
    quote_verbatim: bool = False
    quote_text: str | None = None
    page: int | None = None


class Claim(BaseModel):
    claim_id: str
    statement: str
    # The causal middle link: by what process / under what mechanism
    # the claim holds. Distinct from scope_conditions (when does this
    # hold) and from evidence (what supports this). Populated by the
    # author inline (`[mechanism: ...]`), by the LLM extractor against
    # bound passages, or by hand in the graph checkpoint. Read by the
    # renderer prompt so the LLM develops rather than infers the "how".
    mechanism: str | None = None
    source_order: int = 0
    type: ClaimType
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list)
    scope_conditions: list[str] = Field(default_factory=list)
    counterclaims: list[str] = Field(default_factory=list)
    supporting_claims: list[str] = Field(default_factory=list)
    author_origin: bool = False
    section_id: str | None = None
    # Importance to the document thesis, 0..1. Computed by the
    # whole-document annotator pass after all claims are read; used
    # for visualisation node sizing, renderer word-budget allocation,
    # and surfacing low-importance claims as skip candidates.
    importance: float = 0.5
    created_by: str
    created_at: datetime
    modified_at: datetime
    tags: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────
# Relationship
# ─────────────────────────────────────────────────────────


class RelationshipType(str, Enum):
    supports = "supports"
    contradicts = "contradicts"
    qualifies = "qualifies"
    extends = "extends"
    depends_on = "depends_on"
    is_counterexample_to = "is_counterexample_to"
    is_evidence_for = "is_evidence_for"
    # An analytical move that *reframes* how the target claim should be
    # read — diagnoses an interpretive error, names what the literature is
    # confusing, or shifts which question the target answers. Distinct from
    # `qualifies` (which adds a boundary condition) and `contradicts`
    # (which denies the target). Example: A says "the 10⁶× gap shows room
    # to grow"; B says "reading the gap as room mistakes distance for
    # speed" — B is an interpretive_pivot of A. The renderer should treat
    # an interpretive_pivot pair as a sharp two-move analytical structure
    # rather than two coordinate paragraphs.
    interpretive_pivot = "interpretive_pivot"
    unlabelled = "unlabelled"


class RelationshipStrength(str, Enum):
    direct = "direct"
    partial = "partial"
    inferred = "inferred"


class Relationship(BaseModel):
    rel_id: str
    type: RelationshipType
    from_claim: str = Field(alias="from")
    to_claim: str = Field(alias="to")
    strength: RelationshipStrength = RelationshipStrength.direct
    note: str = ""
    created_by: str
    created_at: datetime

    model_config = ConfigDict(populate_by_name=True)


# ─────────────────────────────────────────────────────────
# Section, Cluster
# ─────────────────────────────────────────────────────────


class SectionRole(str, Enum):
    introduction = "introduction"
    argumentative = "argumentative"
    evidence_synthesis = "evidence_synthesis"
    methodological = "methodological"
    counterargument = "counterargument"
    conclusion = "conclusion"
    appendix = "appendix"
    # references / bibliography / acknowledgements — not rendered as argument prose.
    references = "references"


class Depth(str, Enum):
    skim = "skim"
    standard = "standard"
    deep = "deep"
    rigorous = "rigorous"


class Section(BaseModel):
    section_id: str
    title: str
    parent: str | None = None
    position: int
    role: SectionRole
    thesis_claim: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    cluster_ids: list[str] = Field(default_factory=list)
    figure_ids: list[str] = Field(default_factory=list)
    target_length: int = 800
    depth: Depth = Depth.standard
    voice_override: str | None = None


class ClusterRole(str, Enum):
    setup = "setup"
    evidence = "evidence"
    mechanism = "mechanism"
    # narrative = a concrete example, case study, anecdote, historical
    # parallel, or analogy that adds texture rather than proving a point.
    # Distinct from evidence (which presents a source-bound finding).
    narrative = "narrative"
    limit = "limit"
    complication = "complication"
    counterargument = "counterargument"
    synthesis = "synthesis"
    conclusion = "conclusion"


class ClaimRoleInCluster(BaseModel):
    claim_id: str
    role_in_cluster: ClusterRole
    reporting_verb: str | None = None


class CitationStrategy(BaseModel):
    synthesis_required: bool = False
    synthesis_target_claims: list[str] = Field(default_factory=list)
    positioning_required_for: list[str] = Field(default_factory=list)
    catalogue_forbidden: bool = True
    first_mention_full: list[str] = Field(default_factory=list)


class TokenCount(BaseModel):
    input: int = 0
    output: int = 0


class ProseState(str, Enum):
    not_yet_rendered = "not_yet_rendered"
    generated = "generated"
    edited = "edited"
    dirty = "dirty"
    failed = "failed"
    # Partial render: bound claims rendered, unbound ones marked with
    # {MISSING_CLAIM:...}. Author intervention needed before delivery.
    needs_review = "needs_review"


class Cluster(BaseModel):
    cluster_id: str
    section_id: str
    position: int
    role: ClusterRole
    claim_sequence: list[ClaimRoleInCluster]
    target_words_min: int = 150
    target_words_max: int = 300
    previous_cluster: str | None = None
    next_cluster: str | None = None
    citation_strategy: CitationStrategy = Field(default_factory=CitationStrategy)
    transition_in_hint: str = ""
    transition_out_hint: str = ""
    prose_state: ProseState = ProseState.not_yet_rendered
    prose_file: str | None = None
    last_rendered_at: datetime | None = None
    last_rendered_hash: str | None = None
    last_render_token_count: TokenCount | None = None
    edit_proposals_pending: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────
# Audit Flag and Edit Proposal
# ─────────────────────────────────────────────────────────


class FlagCategory(str, Enum):
    architecture = "architecture"
    citation = "citation"
    coverage = "coverage"
    voice = "voice"
    sentence = "sentence"
    quantification = "quantification"
    paragraph = "paragraph"
    formality = "formality"
    skim_target = "skim_target"
    examiner = "examiner"


class Severity(str, Enum):
    critical = "critical"
    standard = "standard"
    minor = "minor"


class EditMode(str, Enum):
    rewrite = "rewrite"
    suggest_changes = "suggest_changes"
    author_choice = "author_choice"


class FlagDecision(str, Enum):
    accept_rewrite = "accept_rewrite"
    accept_suggest_changes = "accept_suggest_changes"
    reject = "reject"
    defer = "defer"


class ProseLocation(BaseModel):
    paragraph_index: int
    char_start: int
    char_end: int


class AuditFlag(BaseModel):
    flag_id: str
    category: FlagCategory
    rule_id: str
    severity: Severity
    default_mode: EditMode
    cluster_id: str
    section_id: str
    prose_location: ProseLocation
    offending_text: str
    rule_description: str
    suggestion: str
    voice_name: str
    created_at: datetime
    decision: FlagDecision | None = None
    decision_at: datetime | None = None
    decision_rationale: str | None = None


class EditType(str, Enum):
    replace = "replace"
    insert = "insert"
    delete = "delete"
    split_paragraph = "split_paragraph"
    merge_paragraphs = "merge_paragraphs"
    reorder_sentences = "reorder_sentences"


class EditStatus(str, Enum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"
    deferred = "deferred"
    superseded = "superseded"


class EditProposal(BaseModel):
    proposal_id: str
    cluster_id: str
    flag_id: str
    type: EditType
    original_text: str
    proposed_text: str
    rationale: str
    rule_id: str
    confidence: Confidence
    status: EditStatus = EditStatus.pending
    created_at: datetime
    decision: Literal["accepted", "rejected", "deferred"] | None = None
    decision_at: datetime | None = None
    applied_at: datetime | None = None


# ─────────────────────────────────────────────────────────
# Shadow Diff
# ─────────────────────────────────────────────────────────


class ShadowDiffType(str, Enum):
    unsupported_author_claim = "unsupported_author_claim"
    contradicting_corpus_evidence = "contradicting_corpus_evidence"
    corpus_suggested_claim = "corpus_suggested_claim"
    structural_difference = "structural_difference"
    untouched_source = "untouched_source"


class ShadowDiff(BaseModel):
    diff_id: str
    type: ShadowDiffType
    author_claim_id: str | None = None
    shadow_finding: str
    related_shadow_passages: list[dict] = Field(default_factory=list)
    severity: Literal["advisory", "important", "critical"] = "advisory"
    decision: Literal["accept", "accept_with_edit", "reject", "defer"] | None = None
    decision_at: datetime | None = None
    decision_rationale: str | None = None


# ─────────────────────────────────────────────────────────
# Top-level graph container
# ─────────────────────────────────────────────────────────


class AuthorGraph(BaseModel):
    """The full author graph. Single file persistence."""

    project_name: str
    thesis_statement: str | None = None
    # Thesis derived from reading every claim in the document, distinct
    # from thesis_statement (which is what the heading says). Populated
    # by the whole-document annotator pass; may diverge from
    # thesis_statement when the paper argues something different from
    # what the title or THESIS heading claims.
    thesis_argued: str | None = None
    thesis_argued_confidence: float | None = None
    thesis_argued_note: str | None = None
    sections: list[Section] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    created_at: datetime
    modified_at: datetime
