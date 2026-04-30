# Data Model

Full JSON schemas for every entity in Lattice. All entities are persisted as JSON in `.lattice/`. Use pydantic models for validation; serialise to JSON for storage.

This document is the source of truth for field names, types, and relationships. The spec describes intent; this document describes structure.

## File organisation

Inside `.lattice/`:

```
author_graph.json          single file with arrays for sections, claims, relationships
author_graph_history/
  2026-04-23T10-15-00.json snapshots, append-only
shadow_graph.json          same shape as author_graph.json
source_store.json          single file with array of sources
cluster_plan.json          assembler output, per-voice (key by voice name)
audit_flags.json           latest audit, per-voice
flag_decisions.json        log of all flag decisions
edit_proposals/
  c.gap1.evidence.json     one file per cluster with pending proposals
edit_decisions.json        log of all edit decisions
shadow_reports/
  2026-04-23T14-32-00.md   human-readable report
shadow_decisions.json      log of all shadow flag decisions
drafts/
  academic/
    cluster_c.gap1.evidence.md  one prose file per cluster
cache/
  source_hashes.json
  shadow_extractions/
    masanet_2020.json
runs/
  2026-04-23T14-32-00/
    state.json              for resume
    tokens.json             cost tracking
    errors.log
```

## ID conventions

Use stable, deterministic IDs. Re-running the same operation on the same input produces the same IDs.

- **Source**: `<firstauthor_lastname>_<year>` if author known, else hash-prefixed slug
- **Passage**: `p.<location>.<seq>` where location is page (PDFs), line (markdown), cell (spreadsheets)
- **Claim**: `cl.<topic>.<descriptor>` topic is a section-level keyword, descriptor is a short slug
- **Relationship**: `r.<seq>` simple sequential numbering
- **Section**: `s.<slug>` slug from heading
- **Cluster**: `c.<section>.<role>` or `c.<section>.<role>.<seq>` if multiple
- **Edit proposal**: `e.<timestamp>.<seq>`
- **Audit flag**: `f.<timestamp>.<seq>`

## Entity schemas

### Source

```json
{
  "source_id": "masanet_2020",
  "type": "primary_paper",
  "citation": {
    "authors": ["Masanet, E.", "Shehabi, A.", "Lei, N.", "Smith, S.", "Koomey, J."],
    "year": 2020,
    "title": "Recalibrating global data-center energy-use estimates",
    "container": "Science",
    "volume": "367",
    "issue": "6481",
    "pages": "984-986",
    "doi": "10.1126/science.aba3758",
    "url": null
  },
  "passages": [
    {
      "id": "p.984.1",
      "text": "...",
      "location": {"page": 984, "section": "main", "paragraph": 1},
      "type": "claim",
      "char_count": 245
    }
  ],
  "metadata": {
    "peer_reviewed": true,
    "primary": true,
    "date_added": "2026-02-14T09:00:00Z",
    "file_path": "refs/papers/masanet_2020.pdf",
    "hash": "sha256:abc123...",
    "ocr_used": false,
    "indexer_version": "0.1.0"
  }
}
```

Source `type` values:
- `primary_paper`: peer-reviewed primary research
- `review_paper`: peer-reviewed review or meta-analysis
- `report`: industry, government, or NGO report
- `dataset`: data file with schema
- `web_page`: archived web content
- `note`: author's own notes
- `prior_writing`: author's own published or unpublished work
- `interview`: interview transcript

Passage `type` values:
- `claim`: an assertion the source makes
- `figure_caption`: a figure caption
- `table_cell`: text from a table cell
- `method`: a methods description
- `conclusion`: a concluding statement
- `quote`: a direct quotation
- `data_point`: a numerical fact or statistic

### Claim

```json
{
  "claim_id": "cl.efficiency.koomey_slowdown",
  "statement": "Koomey's Law doubling period lengthened from 1.5 years to 2.6 years over the 2010s",
  "type": "empirical",
  "confidence": "high",
  "evidence": [
    {
      "source": "koomey_2015",
      "passage": "p.3.2",
      "binding_strength": "strong",
      "quote_verbatim": false,
      "quote_text": null,
      "page": 3
    }
  ],
  "scope_conditions": ["desktop and laptop processors, 1990-2013"],
  "counterclaims": ["cl.efficiency.accelerator_era_recovery"],
  "supporting_claims": [],
  "author_origin": false,
  "section_id": "s.gap1",
  "created_by": "structure_ingester",
  "created_at": "2026-04-23T10:15:00Z",
  "modified_at": "2026-04-23T10:15:00Z",
  "tags": ["efficiency", "koomey", "trend"]
}
```

Claim `type` values:
- `empirical`: a fact about the world, source-grounded
- `methodological`: a statement about how something is done or measured
- `normative`: a value judgement
- `user_synthesis`: author's original contribution
- `definition`: terminological scaffolding

Claim `confidence` values: `high`, `medium`, `low`, `speculative`.

Evidence `binding_strength` values: `strong`, `weak`, `none`, `contradictory`. Set by enricher.

### Relationship

```json
{
  "rel_id": "r.001",
  "type": "contradicts",
  "from": "cl.andrae.traffic_proxy_scaling",
  "to": "cl.coroama.fixed_power_dominates",
  "strength": "direct",
  "note": "Andrae scales energy with traffic; Coroama shows network power is largely fixed",
  "created_by": "structure_ingester",
  "created_at": "2026-04-23T10:15:00Z"
}
```

Relationship `type` values:
- `supports`: A provides evidence for B
- `contradicts`: A and B cannot both be true
- `qualifies`: A is true only under conditions B describes
- `extends`: A builds on B
- `depends_on`: A only makes sense if B is true
- `is_counterexample_to`: A is a specific case undermining B
- `is_evidence_for`: A passage directly supports an empirical claim
- `unlabelled`: Argus generic dependency, awaiting author labelling

Relationship `strength` values: `direct`, `partial`, `inferred`.

### Section

```json
{
  "section_id": "s.gap1",
  "title": "Gap 1: Untested efficiency assumptions",
  "parent": "s.root",
  "position": 3,
  "role": "argumentative",
  "thesis_claim": "cl.user.efficiency_is_highest_leverage_gap",
  "claim_ids": [
    "cl.stabilisation.koomey_assumption",
    "cl.koomey.slowdown_documented",
    "cl.esmaeilzadeh.dennard_breakdown",
    "cl.landauer.thermodynamic_floor",
    "cl.sorrell.rebound_effect",
    "cl.user.efficiency_is_highest_leverage_gap"
  ],
  "cluster_ids": ["c.gap1.setup", "c.gap1.evidence", "c.gap1.mechanism", "c.gap1.synthesis"],
  "figure_ids": ["fig.forecast_divergence"],
  "target_length": 800,
  "depth": "deep",
  "voice_override": null
}
```

Section `role` values:
- `introduction`
- `argumentative`
- `evidence_synthesis`
- `methodological`
- `counterargument`
- `conclusion`
- `appendix`

Section `depth` values: `skim`, `standard`, `deep`, `rigorous`.

### Cluster

```json
{
  "cluster_id": "c.gap1.evidence",
  "section_id": "s.gap1",
  "position": 2,
  "role": "evidence",
  "claim_sequence": [
    {
      "claim_id": "cl.koomey.slowdown_documented",
      "role_in_cluster": "evidence",
      "reporting_verb": "documents"
    },
    {
      "claim_id": "cl.esmaeilzadeh.dennard_breakdown",
      "role_in_cluster": "mechanism",
      "reporting_verb": "identify"
    }
  ],
  "target_words_min": 180,
  "target_words_max": 280,
  "previous_cluster": "c.gap1.setup",
  "next_cluster": "c.gap1.mechanism",
  "citation_strategy": {
    "synthesis_required": false,
    "synthesis_target_claims": [],
    "positioning_required_for": [],
    "catalogue_forbidden": true,
    "first_mention_full": ["koomey_2015", "esmaeilzadeh_2011"]
  },
  "transition_in_hint": "Pick up the slowdown topic from the previous cluster's close.",
  "transition_out_hint": "End on a sentence that motivates the mechanism explanation in the next cluster.",
  "prose_state": "generated",
  "prose_file": "drafts/academic/cluster_c.gap1.evidence.md",
  "last_rendered_at": "2026-04-24T11:32:00Z",
  "last_rendered_hash": "sha256:def456...",
  "last_render_token_count": {"input": 2400, "output": 320},
  "edit_proposals_pending": []
}
```

Cluster `role` values: same as claim role-in-cluster:
- `setup`, `evidence`, `mechanism`, `limit`, `complication`, `counterargument`, `synthesis`, `conclusion`

Cluster `prose_state` values:
- `not_yet_rendered`: planned but not generated
- `generated`: from renderer, no edits
- `edited`: has accepted suggest-changes edits
- `dirty`: graph changed since last render
- `failed`: last render attempt failed

### Audit Flag

```json
{
  "flag_id": "f.20260424.045",
  "category": "citation",
  "rule_id": "citation.forbid_catalogue_pattern",
  "severity": "critical",
  "default_mode": "suggest_changes",
  "cluster_id": "c.gap1.evidence",
  "section_id": "s.gap1",
  "prose_location": {
    "paragraph_index": 2,
    "char_start": 156,
    "char_end": 234
  },
  "offending_text": "Several studies have examined this (Jones 2019; Lee 2020; Park 2021).",
  "rule_description": "Three or more sources cited sequentially without synthesis.",
  "suggestion": "Generate a synthesis paragraph naming the three sources and their distinct contributions.",
  "voice_name": "academic",
  "created_at": "2026-04-24T12:00:00Z",
  "decision": null,
  "decision_at": null,
  "decision_rationale": null
}
```

Flag `category` values:
- `architecture`, `citation`, `coverage`, `voice`, `sentence`, `quantification`, `paragraph`, `formality`, `skim_target`, `examiner`

Flag `severity` values: `critical`, `standard`, `minor`.

Flag `default_mode` values: `rewrite`, `suggest_changes`, `author_choice`.

Flag `decision` values (after author review): `accept_rewrite`, `accept_suggest_changes`, `reject`, `defer`, null (pending).

### Edit Proposal

```json
{
  "proposal_id": "e.20260424.001",
  "cluster_id": "c.gap1.evidence",
  "flag_id": "f.20260424.045",
  "type": "replace",
  "original_text": "Several studies have examined this (Jones 2019; Lee 2020; Park 2021).",
  "proposed_text": "Three lines of evidence converge on this point: Jones's spectroscopic measurements, Lee's thermodynamic modelling, and Park's field observations. They disagree on magnitude but agree on direction.",
  "rationale": "Catalogue pattern violates synthesis_threshold rule. Synthesis paragraph required when 3+ sources cluster.",
  "rule_id": "citation.forbid_catalogue_pattern",
  "confidence": "high",
  "status": "pending",
  "created_at": "2026-04-24T12:30:00Z",
  "decision": null,
  "decision_at": null,
  "applied_at": null
}
```

Edit proposal `type` values:
- `replace`: substitute one text span with another
- `insert`: add text at a specific position
- `delete`: remove a text span
- `split_paragraph`: split one paragraph into two at a specified point
- `merge_paragraphs`: merge two adjacent paragraphs
- `reorder_sentences`: reorder sentences within a paragraph

Edit proposal `status` values: `pending`, `accepted`, `rejected`, `deferred`, `superseded`.

### Voice (parsed)

The voice file is markdown with YAML frontmatter. Parsed into:

```json
{
  "name": "academic",
  "description": "Engineering academic writing in the Cambridge tradition.",
  "architecture": {
    "template": "six_element_paper",
    "hourglass_required": true,
    "killer_graph_first": true,
    "skim_targets_must_be_strongest": ["title", "abstract", "end_of_literature_review", "end_of_conclusion", "figure_captions"],
    "signposting": {
      "section_open": "motivation_and_structure",
      "section_close": "resolution",
      "paragraph_open": "topic_first",
      "metadiscourse_density": "minimal"
    }
  },
  "citation": {
    "engagement_level": "name_claim_relevance",
    "reporting_verbs": {
      "require_variety": true,
      "direct_evidence": ["demonstrates", "shows", "establishes", "measured"],
      "correlational": ["indicates", "suggests", "found", "observed", "reported"],
      "theoretical": ["implies", "argues", "contends", "proposes"],
      "speculative": ["may", "might", "could", "appears to"]
    },
    "synthesis_threshold": 3,
    "forbid_catalogue_pattern": true,
    "positioning_required_for": ["thesis_claims", "gap_statements", "novel_methodology_claims"],
    "citation_purposes_allowed": ["support_specific_claim", "establish_specific_gap", "credit_prior_contribution"]
  },
  "register": {...},
  "stance": {...},
  "attribution": {...},
  "paragraph": {...},
  "role_templates": {...},
  "transitions": {...},
  "prohibitions": [...],
  "preferences": [...],
  "figures": {...},
  "statistics": {...},
  "review_paper": {...},
  "flag_default_modes": {...},
  "notes": "raw markdown body of the voice file"
}
```

See `examples/voices/academic.voice.md` for the canonical example.

### Shadow Diff

```json
{
  "diff_id": "d.20260424.001",
  "type": "unsupported_author_claim",
  "author_claim_id": "cl.user.efficiency_is_highest_leverage_gap",
  "shadow_finding": "No corpus passage directly supports this claim. Two passages are tangentially related.",
  "related_shadow_passages": [
    {"source": "koomey_2015", "passage": "p.4.1"}
  ],
  "severity": "advisory",
  "decision": null,
  "decision_at": null
}
```

Shadow diff `type` values:
- `unsupported_author_claim`
- `contradicting_corpus_evidence`
- `corpus_suggested_claim`
- `structural_difference`
- `untouched_source`

## Persistence rules

- All writes are append-only when possible. Use snapshots for state changes.
- All entities have `created_at`. Modifications create a new version, not in-place edits, except for derived/cached fields.
- All decisions (flag, edit, shadow) are logged in append-only decision logs. Never delete decisions; supersede them with new ones.
- All LLM-bound stages save token counts to `runs/<timestamp>/tokens.json` for cost tracking.
