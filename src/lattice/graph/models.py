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


class EvidenceStatus(str, Enum):
    """Author-declared (or auto-derived) state of a claim's evidence backing.

    Distinct from ``BindingStrength`` (which lives on each individual Evidence
    entry). EvidenceStatus is a claim-level summary the author can assert in
    the outline so the scaffold audit can act on intent, not just the absence
    of an Evidence row. The renderer / coverage check treats it as advisory
    and falls back to deriving from the ``evidence`` list when unset.
    """

    unbound = "unbound"           # claim has no evidence yet, intentionally
    source_hint = "source_hint"   # author has a citation hint but no precise binding
    bound = "bound"                # claim is bound to specific passage(s)


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
    # and surfacing low-importance claims as skip candidates. The
    # ingester also accepts an explicit ``[importance: 0.8]`` tag.
    importance: float = 0.5
    # Author's declared evidence state — set by ``[evidence_status: ...]``
    # in the outline, or left None to let downstream code derive it from
    # the ``evidence`` list. Read by the scaffold audit and the renderer
    # readiness gate.
    evidence_status: EvidenceStatus | None = None
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


class ClusterRelationshipContext(BaseModel):
    """A relationship surfaced as context for cluster rendering.

    Captures intra-cluster edges (both endpoints inside the cluster),
    incoming edges (some other cluster's claim → a claim in this cluster),
    and outgoing edges (a claim in this cluster → some other cluster's
    claim). The renderer uses ``intra`` edges to drive paragraph shape
    (e.g. ``interpretive_pivot`` becomes a sharp two-move structure) and
    ``incoming``/``outgoing`` edges to drive transitions.
    """

    rel_id: str
    type: RelationshipType
    strength: RelationshipStrength
    note: str = ""
    direction: Literal["intra", "incoming", "outgoing"]
    from_claim: str
    to_claim: str
    # The cluster on the other end of the edge. ``None`` when the edge
    # is intra-cluster, or when the other endpoint isn't assigned to any
    # cluster (e.g. the thesis claim, or a claim in a references section).
    other_cluster_id: str | None = None
    other_section_id: str | None = None
    # Whether the renderer should treat this edge as load-bearing for
    # prose shape. Intra-cluster pivot/qualifies/contradicts are always
    # True; weak inferred edges may be False.
    affects_rendering: bool = True


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
    # Relationship payload used by the renderer for paragraph shape and
    # transitions. Populated by the assembler from the author graph;
    # optional with default empty list so older serialised plans still
    # load. Re-running the assembler refreshes this list, so it is safe
    # to treat as derivable.
    relationship_context: list[ClusterRelationshipContext] = Field(
        default_factory=list
    )
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
# Scaffold ingest diagnostics
# ─────────────────────────────────────────────────────────


class ScaffoldWarningLevel(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"


class ScaffoldWarning(BaseModel):
    """A diagnostic raised during scaffold ingest.

    Distinct from AuditFlag (which is a post-render finding on prose) — these
    are pre-render parse-time issues: malformed tags, unresolved relationship
    targets, references to citekeys not in the source store, etc.
    """

    level: ScaffoldWarningLevel = ScaffoldWarningLevel.warning
    code: str
    message: str
    claim_id: str | None = None
    section_id: str | None = None
    line: int | None = None
    raw: str | None = None


class ScaffoldClaimReport(BaseModel):
    """Per-claim diagnostics emitted during ingest. Lets the author see what
    the parser actually extracted vs what they wrote, which references didn't
    resolve, and how confident the parser was.

    ``cited_refs`` is the immutable record of every ``[ref:]`` the author
    wrote on this claim. ``unresolved_refs`` is the subset that don't
    resolve to a known indexed source — recomputed from ``cited_refs``
    on each save, so re-running with a different ``known_source_ids`` is
    idempotent (a previously-stripped ref can come back if the source is
    later removed from the index).
    """

    claim_id: str
    section_id: str | None = None
    original_excerpt: str = ""
    extracted_statement: str = ""
    confidence: float = 1.0  # parser's confidence in its extraction, 0..1
    cited_refs: list[str] = Field(default_factory=list)
    unresolved_refs: list[str] = Field(default_factory=list)
    unresolved_targets: list[str] = Field(default_factory=list)
    warnings: list[ScaffoldWarning] = Field(default_factory=list)
    # 1-indexed line number in the source outline file. Used by tools
    # like ``lattice fill-mechanisms`` to locate the bullet for in-place
    # editing without re-parsing. ``None`` for synthetic claims (e.g.
    # the thesis claim, which is built from the THESIS block, not a
    # bullet).
    line: int | None = None


class AutoOutlinerSummary(BaseModel):
    """Embedded inside a ``ScaffoldReport`` when the markdown that was
    parsed was first produced by the LLM auto-outliner. Records how rich
    the LLM's output was so the author can tell whether a flat-looking
    scaffold is a parser problem or a Claude problem."""

    generated_at: datetime
    max_depth: int
    section_count: int = 0
    claim_count: int = 0
    typed_claim_count: int = 0
    user_synthesis_claim_count: int = 0
    mechanism_claim_count: int = 0
    evidence_hint_count: int = 0
    importance_set_count: int = 0
    relationship_tag_count: int = 0
    warnings: list[ScaffoldWarning] = Field(default_factory=list)
    raw_response_preview: str = ""


class ScaffoldReport(BaseModel):
    """Diagnostic artefact persisted to ``.lattice/scaffold_report.json``.

    Captures everything the ingester noticed but couldn't safely escalate to
    a hard parse error. Read by the scaffold audit (Phase 4) and surfaced in
    the web UI so the author can fix issues before drafting.
    """

    project_name: str
    source_file: str = ""
    generated_at: datetime
    parser: str = "markdown_ingester"
    claim_reports: list[ScaffoldClaimReport] = Field(default_factory=list)
    warnings: list[ScaffoldWarning] = Field(default_factory=list)
    # Lightweight summary so consumers can avoid scanning the full payload.
    counts: dict[str, int] = Field(default_factory=dict)
    # Populated when the outline that was parsed had been generated by
    # the LLM auto-outliner — lets the author see how much was inferred
    # from raw prose vs hand-tagged.
    auto_outliner: AutoOutlinerSummary | None = None
    # Strength + breadth metrics computed against the parsed graph.
    # Lazily populated by ``MarkdownOutlineIngester.save_scaffold_report``
    # (uses ``graph.metrics.compute_argument_metrics``); ``None`` when
    # the report was emitted before metrics were wired in or when the
    # caller chose to skip the computation.
    argument_metrics: dict | None = None


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
