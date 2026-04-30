# Lattice: Argument-First Long-Form Writing Tool

**Version:** 2.0 (original spec) — implementation has moved beyond this; see [`../README.md`](../README.md) and the [Implementation deltas](#implementation-deltas) section below.
**Status:** Build specification — implemented and extended.
**Build target:** A Python CLI tool that turns an author-built argument structure, a folder of support materials, and a voice specification into polished long-form prose. The tool treats argument structure as authoritative, runs a parallel thesis-anchored review of the corpus to surface gaps and contradictions, and renders the working graph through swappable voices. The renderer operates at claim-cluster granularity to handle long-form documents (10,000+ words) reliably, and supports two editing modes: rewrite (regenerate from graph) and suggest-changes (surgical edits to existing prose).

> **Live additions beyond this spec:** the working tool now ships a FastAPI web UI with WebSocket-streamed progress, an interactive cytoscape graph view, and an activity-oriented action model (Ingest · Scaffold · Draft · Find gaps · Refine · Restructure · Review). Section nesting goes 3 levels deep (`# A.` → `## A.1` → `### A.1.1`). New modules: `compare/` (cross-project), `lit_gaps/` (literature-gap analysis with OpenAlex verification), `restructure/` (advisory ordering audit), `review/` (supervisor track-changes). Read the README before reading this spec — the spec is canonical for the data model and the design intent, but several pipeline stages have been factored differently in code.

**Why this exists:** Two earlier attempts at this problem failed in opposite directions. The first trusted the model to remember academic discipline and produced clean prose from loosely grounded evidence. The second forced evidence-first methodology but shipped structurally broken drafts. Lattice moves the discipline into the data model. Argument structure, claims, evidence, and voice are separate artifacts that compose deterministically into prose. Style is a projection function, not a constraint applied during generation.

---

## Part 1: Principles

- **The author owns the structure.** Primary structural input is authoritative. The tool never silently reshapes it.
- **Claims are atoms, not sentences.** Every factual sentence in the output traces to a claim in the graph.
- **Style is pluggable.** Voices are structured configurations. The same graph produces an academic paper or a journalistic piece by swapping one file.
- **Critique is advisory.** The tool runs its own parallel mapping of the corpus and surfaces differences as a report, never as silent revisions.
- **Edits are localised.** Changing a source re-indexes that source. Changing a claim re-renders affected clusters. Nothing cascades beyond what actually changed.
- **The author chooses the editing mode.** Rewrite for substantive change, suggest-changes for polish. Per flag, per cluster.
- **Long-form is paragraph-first.** The renderer's unit is the claim cluster (one or two paragraphs), not the section. Sections are scoping context.
- **Human in the loop at structural decisions, not at word-level polishing.** You review the argument graph, the shadow report, the voice, and edit proposals. You do not review paragraph drafts one at a time.

---

## Part 2: High-level architecture

```
structure/ (one of):              refs/ (many):
  outline.md                        papers/*.pdf
  argument.argus.json               notes/*.md
  draft.docx                        data/*.xlsx
                                    prior_writing/*.md

       │                                 │
       ▼                                 ▼
┌──────────────────┐             ┌──────────────────┐
│ 1a STRUCTURE     │             │ 1b SOURCE        │
│    INGESTER      │             │    INDEXER       │
└────────┬─────────┘             └────────┬─────────┘
         │                                │
         ▼                                ▼
   author_graph.json                source_store.json
         │                                │
         ├───────┐               ┌────────┤
         ▼       ▼               ▼        ▼
┌──────────────────┐             ┌──────────────────┐
│ 2a ENRICHER      │             │ 2b SHADOW        │
│    binds claims  │             │    MAPPER        │
│    to passages   │             │  (thesis-anchored│
│                  │             │   blind to graph)│
└────────┬─────────┘             └────────┬─────────┘
         │                                │
         │           ┌────────────────────┘
         │           │
         ▼           ▼
    ┌──────────────────┐
    │ 3 DIFFER         │
    └────────┬─────────┘
             ▼
      shadow_report.md
             │
             ▼
    ┌──────────────────┐
    │ 4 REVIEW TUI     │
    └────────┬─────────┘
             ▼
      working_graph.json
             │
             ▼
    ┌──────────────────┐
    │ 5 ASSEMBLER      │  ← architecture template (from voice)
    │   builds         │
    │   clusters       │
    └────────┬─────────┘
             ▼
      cluster_plan.json
             │
             ▼
    ┌──────────────────┐
    │ 6 RENDERER       │  ← voices/*.voice.md
    │   per-cluster    │  ← citation strategy
    │   parallel       │  ← role templates
    └────────┬─────────┘
             ▼
      outputs/paper.<voice>.md
             │
             ▼
    ┌──────────────────┐
    │ 7 AUDITOR        │
    └────────┬─────────┘
             ▼
      audit_flags.json
             │
             ▼
    ┌──────────────────┐
    │ 8 FLAG REVIEW    │
    │   author chooses │
    │   per-flag mode  │
    └────────┬─────────┘
             ▼
        ┌────┴────┐
        ▼         ▼
   ┌─────────┐ ┌──────────────────┐
   │ REWRITE │ │ 9 EDIT PROPOSER  │
   │ cluster │ │   suggest-changes│
   └────┬────┘ └────────┬─────────┘
        │               ▼
        │        edit_proposals.json
        │               │
        │               ▼
        │        ┌──────────────────┐
        │        │ EDIT TUI         │
        │        │ accept/reject    │
        │        │ per edit         │
        │        └────────┬─────────┘
        │                 │
        ▼                 ▼
    Updated outputs/paper.<voice>.md
```

---

## Part 3: Data model

Five entities, all persisted as JSON in `.lattice/`. Append-only with version history.

### 3.1 Source

Anything external to the author that can ground a claim. Papers, reports, datasets, interviews, websites, the author's own earlier writing.

```json
{
  "source_id": "masanet_2020",
  "type": "primary_paper",
  "citation": {
    "author": "Masanet et al.",
    "year": 2020,
    "title": "Recalibrating global data-center energy-use estimates",
    "container": "Science",
    "doi": "10.1126/science.aba3758"
  },
  "passages": [
    {"id": "p.984.1", "text": "...", "page": 984, "type": "claim"},
    {"id": "p.985.fig2", "text": "...", "page": 985, "type": "figure_caption"}
  ],
  "metadata": {
    "peer_reviewed": true,
    "primary": true,
    "date_added": "2026-02-14",
    "hash": "sha256:..."
  }
}
```

Passage IDs are derived from location, not extraction order. Re-indexing an unchanged source produces identical IDs.

Passage types: `claim`, `figure_caption`, `table_cell`, `method`, `conclusion`, `quote`, `data_point`.

### 3.2 Claim

```json
{
  "claim_id": "cl.efficiency.koomey_slowdown",
  "statement": "Koomey's Law doubling period lengthened from 1.5 years to 2.6 years over the 2010s",
  "type": "empirical",
  "confidence": "high",
  "evidence": [
    {"source": "koomey_2015", "passage": "p.3.2", "quote_verbatim": false}
  ],
  "scope_conditions": ["desktop and laptop processors, 1990-2013"],
  "counterclaims": ["cl.efficiency.accelerator_era_recovery"],
  "author_origin": false,
  "created_by": "structure_ingester",
  "created_at": "2026-04-23T10:15:00Z"
}
```

Claim types: `empirical`, `methodological`, `normative`, `user_synthesis`, `definition`.

### 3.3 Relationship

```json
{
  "rel_id": "r.001",
  "type": "contradicts",
  "from": "cl.andrae.traffic_proxy_scaling",
  "to": "cl.coroama.fixed_power_dominates",
  "strength": "direct",
  "note": "Andrae scales energy with traffic; Coroama shows network power is largely fixed"
}
```

Types: `supports`, `contradicts`, `qualifies`, `extends`, `depends_on`, `is_counterexample_to`, `is_evidence_for`.

Strength: `direct`, `partial`, `inferred`.

### 3.4 Section

A slot in the outline tree. Contains an ordered sequence of claim IDs grouped into clusters.

```json
{
  "section_id": "s.gap1",
  "title": "Gap 1: Untested efficiency assumptions",
  "parent": "s.root",
  "position": 3,
  "role": "argumentative",
  "thesis": "cl.user.efficiency_is_highest_leverage_gap",
  "clusters": ["c.gap1.setup", "c.gap1.evidence", "c.gap1.mechanism", "c.gap1.synthesis"],
  "figures": ["fig.forecast_divergence"],
  "target_length": 800,
  "depth": "deep"
}
```

Section roles: `introduction`, `argumentative`, `evidence_synthesis`, `methodological`, `counterargument`, `conclusion`, `appendix`.

### 3.5 Cluster (new in v2)

The actual rendering unit. A topologically connected subgraph of 2-4 claims sharing a topic, generating one or two paragraphs.

```json
{
  "cluster_id": "c.gap1.evidence",
  "section_id": "s.gap1",
  "position": 2,
  "role": "evidence",
  "claim_sequence": [
    {"claim": "cl.koomey.slowdown_documented", "role": "evidence"},
    {"claim": "cl.esmaeilzadeh.dennard_breakdown", "role": "mechanism"}
  ],
  "target_words": [180, 280],
  "previous_cluster": "c.gap1.setup",
  "next_cluster": "c.gap1.mechanism",
  "citation_strategy": {
    "synthesis_required": false,
    "positioning_required": [],
    "reporting_verbs": {
      "cl.koomey.slowdown_documented": "documents",
      "cl.esmaeilzadeh.dennard_breakdown": "identify"
    }
  },
  "prose_state": "generated",
  "last_rendered_at": "2026-04-24T11:32:00Z",
  "last_rendered_hash": "sha256:..."
}
```

Cluster roles within a section: `setup`, `evidence`, `mechanism`, `limit`, `complication`, `counterargument`, `synthesis`, `conclusion`.

`prose_state`:
- `generated`: last from renderer, no manual or suggest-change edits since
- `edited`: has accepted suggest-changes edits since last full generation
- `dirty`: graph changed since last render, regeneration needed

### 3.6 Voice

A voice is the configuration that tells the renderer how to project clusters into prose. See Part 8 for full specification.

### 3.7 Edit proposal (new in v2)

```json
{
  "proposal_id": "e.20260424.001",
  "cluster_id": "c.gap1.evidence",
  "flag_id": "f.20260424.045",
  "type": "replace",
  "original_text": "Several studies have examined this (Jones 2019; Lee 2020; Park 2021).",
  "proposed_text": "Three lines of evidence converge on this point: Jones's spectroscopic measurements, Lee's thermodynamic modelling, and Park's field observations. They disagree on magnitude but agree on direction.",
  "rationale": "Catalogue pattern violates synthesis_threshold rule.",
  "rule_id": "citation.forbid_catalogue_pattern",
  "confidence": "high",
  "status": "pending"
}
```

Edit types: `replace`, `insert`, `delete`, `split_paragraph`, `merge_paragraphs`, `reorder_sentences`.

Status: `pending`, `accepted`, `rejected`, `deferred`, `superseded`.

---

## Part 4: Input modes

Author provides structure through one of three modes. All three produce the same internal author graph.

### 4.1 Argus export

Drop `argument.argus.json` into `structure/`. Argus thesis nodes become user_synthesis claims; argument nodes become sections; counter-claim nodes get automatic contradicts relationships; evidences become is_evidence_for relationships; references become source stubs.

On import, the TUI prompts for unlabelled edge relations.

### 4.2 Structured outline

Markdown or DOCX bullet structure with a tag vocabulary:

```markdown
# THESIS
ICT energy forecasts diverge by twenty-fold because each embeds untested
assumptions rather than because the underlying physics disagrees.

# A. The forecast landscape
  - Stabilisation camp: Masanet, Malmodin, IEA assume efficiency offsets growth
    [ref: masanet_2020, malmodin_2018, iea_2025]
  - Explosion camp: Andrae projects 2,800 TWh for wireless alone by 2030
    [ref: andrae_edler_2015]
  - MY VIEW: divergence is assumption-driven, not measurement-driven
    [user_synthesis] [supports: thesis]

# B. Gap 1: Untested efficiency assumptions [depth: deep]
  - Koomey's Law doubling period lengthened from 1.5y to 2.6y
    [ref: koomey_2015] [role: evidence]
  - COUNTER: accelerator-era gains partially compensate
    [user_synthesis] [weak]
  - MY VIEW: this is the highest-leverage unresolved question
    [user_synthesis] [role: conclusion]
```

Tag vocabulary: `[ref:]`, `[user_synthesis]`, `[weak/strong/contested]`, `COUNTER:`, `MY VIEW:`, `[role:]`, `[depth:]`, `[words:]`, `[skip]`, `[central_contribution]` (on figure references).

### 4.3 Refs-only mode

Skip `structure/`. The tool runs full extraction and architect passes over the corpus to propose a graph, presents it for review, then proceeds. Slowest mode, used when the author doesn't know the argument shape in advance.

---

## Part 5: Stage specifications

### 5.1 Structure ingester

Input: one file from `structure/`.
Output: `author_graph.json`.

Mostly deterministic parsing. LLM-assisted only for inferring claim types when not tagged. After ingestion, the TUI surfaces:

- Untagged bullets the ingester classified automatically
- References to citekeys not matching any indexed source
- Edges imported as generic "related"
- Sections with fewer than 3 or more than 15 claims

### 5.2 Source indexer

Input: files in `refs/`.
Output: `source_store.json`.

Mechanical, no LLM unless OCR fallback needed. Sub-folder conventions:

- `refs/papers/`: full academic paper extraction (abstract, sections, figures, tables, references)
- `refs/notes/`: lightweight markdown parsing
- `refs/data/`: schema plus summary statistics
- `refs/prior_writing/`: author's own work, indexed with provenance
- `refs/web/`: archived web pages, URL preserved

Files hashed; unchanged files skip re-indexing.

### 5.3 Enricher

Input: `author_graph.json`, `source_store.json`.
Output: updates `author_graph.json` in place, binding claims to specific passages.

Enrichment results: `strong_bind`, `weak_bind`, `no_bind`, `contradictory_bind`. Never adds or removes claims.

### 5.4 Shadow mapper

Input: `source_store.json`, thesis statement.
Output: `shadow_graph.json`.

Runs an independent argument-mapping pass over the corpus. Sees the thesis but not the author graph. Three sub-stages: extract per source, cluster claims by topic, build relationships and shadow outline.

Per-source extractions cached. Use `--blind-shadow` to ignore even the thesis (rare).

### 5.5 Differ

Input: `author_graph.json`, `shadow_graph.json`.
Output: `.lattice/shadow_reports/<timestamp>.md`.

Five sections in the report:

1. Unsupported author claims
2. Contradicting corpus evidence
3. Corpus-suggested claims missing from author graph
4. Structural differences
5. Source coverage

Every flag has a unique ID. Decisions are logged persistently.

### 5.6 Review TUI

Input: latest shadow report, author graph.
Output: updated `author_graph.json`, `shadow_decisions.json`.

For each flag: accept as-is, accept with edits, reject (with rationale), defer. Bulk operations supported.

### 5.7 Assembler (new responsibility in v2)

Input: working `author_graph.json`, voice file.
Output: `cluster_plan.json`.

Responsibilities:

1. **Architecture validation.** Check the section structure against the voice's `architecture.template`. Flag missing required sections (for `six_element_paper`: context, literature, proposal, test design, results, discussion).

2. **Hourglass check.** For templates requiring hourglass shape, verify opening width matches closing width.

3. **Killer-graph ordering.** Figures marked `[central_contribution]` anchor section sequencing.

4. **Cluster construction.** For each section, walk the claim graph and group claims into clusters of 2-4 by topic coherence and role compatibility. Assign cluster IDs, target word ranges, and roles.

5. **Citation strategy planning.** For each cluster, pre-compute citation strategy:
   - Cluster sources by topic
   - Mark clusters where 3+ sources cluster (synthesis_required)
   - Mark thesis/gap/novel-method claims (positioning_required)
   - Assign reporting verbs per claim based on confidence

6. **Cross-cluster transitions.** For each adjacent cluster pair, set the next cluster's opening hint to pick up the previous cluster's closing topic.

The cluster plan is the contract the renderer fulfils. Author can review the plan in the TUI and adjust cluster boundaries, target lengths, or citation strategy before rendering.

### 5.8 Renderer

Input: `cluster_plan.json` (one cluster at a time), voice file, source store, claim graph.
Output: `outputs/<voice>/cluster_<id>.md`, then assembled into `outputs/paper.<voice>.md`.

Renders one cluster at a time, in parallel. Each cluster is a single LLM call producing 150-300 words. Claim-cluster granularity solves long-form issues:

- Each call's output stays in high-quality generation range
- Cohesion within paragraphs is local, easy to maintain
- Cross-cluster cohesion handled by transition contracts, not regeneration
- Token cost scales linearly with word count
- Wall-clock time dominated by slowest cluster, not the sum

Per-cluster prompt structure:

```
Render this cluster as one or two paragraphs.

VOICE: {full voice spec including role templates, prohibitions, transitions}

ARCHITECTURE ROLE OF SECTION: {literature/gap/proposal/etc.}
ROLE OF THIS CLUSTER WITHIN SECTION: {setup/evidence/mechanism/etc.}

PREVIOUS CLUSTER CLOSING: {last 1-2 sentences of previous cluster, for cohesion}

CLAIMS IN ORDER:
1. {claim text} [role: setup] [confidence: high]
   Sources: {enrichment data with passage text and page numbers}
   Reporting verb: {assigned by assembler}
2. {claim text} [role: mechanism] [qualifies: claim 1]
   ...

CITATION STRATEGY:
- Synthesis required for claims [N, M, P] (cluster on topic X)
- Positioning required for claim Q (thesis claim)
- Catalogue pattern forbidden

TRANSITION OUT:
This cluster is followed by: {brief description of next cluster's role}
End on a sentence that supports that transition.

TARGET LENGTH: {min}-{max} words.

CONSTRAINT: Every factual sentence must trace to a claim ID above.
If you need to assert something not in the claim list, emit
{MISSING_CLAIM: "what you wanted to say"} instead of inventing.
```

For long-form documents (10k+ words), a 12,000-word document decomposes to roughly 60-80 clusters across 8 sections. Parallel rendering with rate limiting completes in 5-10 minutes wall-clock on Max subscription.

### 5.9 Auditor

Input: rendered document, cluster plan, voice file, claim graph, enrichment data.
Output: `audit_flags.json`, `audit.md`, `examiner_review.md`.

Checks distributed by category. Each flag has a category, severity (`critical`, `standard`, `minor`), default edit mode (`rewrite` or `suggest_changes`), the rule that fired, and the affected cluster.

**Architecture checks (critical, default rewrite):**
- All required architecture template sections present
- Hourglass shape preserved
- Killer graph anchors narrative

**Citation engagement checks (critical, default suggest_changes):**
- Every cited claim names author in sentence
- Every cited claim states specific finding
- Every cited claim explains relevance
- No catalogue patterns (3+ sequential single-source citations)
- Reporting verbs vary across consecutive paragraphs

**Claim coverage checks (critical, default rewrite):**
- Every factual sentence traces to a claim ID
- No `{MISSING_CLAIM}` markers remain

**Voice compliance checks (standard, default suggest_changes):**
- All prohibitions observed
- Banned words absent
- Inflated vocabulary absent
- Cluttered phrases absent

**Sentence craft checks (standard, default suggest_changes):**
- Subject-verb distance under 10 words
- Active voice predominates (configurable threshold)
- No expletive constructions at sentence starts
- No empty verb plus nominalisation pile-ups

**Quantification checks (critical, default suggest_changes):**
- Magnitude claims quantified (no "significantly" without numbers)
- Hedging matches evidence strength
- Named entities accurate (IEA intergovernmental, etc.)

**Paragraph architecture checks (standard, default suggest_changes):**
- Topic sentence opens each paragraph
- Old-to-new information flow between sentences
- No continuation connectives at paragraph start
- One paragraph one point (length under 250 words)
- Paragraph openers vary

**Formality checks (minor, default suggest_changes):**
- No contractions
- No colloquialisms
- No rhetorical questions
- Tense consistent within paragraphs

**Skim-target checks (critical, default rewrite):**
- Title strong
- Abstract self-contained, no undefined acronyms
- End of literature review has explicit gap statement
- End of conclusion strongest content
- Figure captions stand alone

**Examiner review (advisory, default depends on issue):**

Six questions, with skim-target weighting:

1. What is the thesis in one sentence?
2. What is the original contribution?
3. Is the gap statement explicit and well-motivated?
4. Do figure captions stand alone as arguments?
5. Where is evidence thinnest?
6. Where is logic assumed rather than demonstrated?
7. What would cause rejection at submission?
8. What must be fixed before showing this to the supervisor?

Output to `examiner_review.md`, highest priority section.

### 5.10 Flag review TUI

Input: `audit_flags.json`.
Output: per-flag decisions logged, accepted flags routed to either rewrite or edit proposer.

For each flag, author sees:

- The affected prose passage (highlighted)
- The rule that fired
- The default mode (rewrite or suggest_changes)
- A toggle to switch mode
- Buttons: accept (in chosen mode), reject (with optional rationale), defer

Bulk operations:

- Accept all suggest-changes flags in section X
- Accept all critical flags
- Switch all my deferrals back to active

### 5.11 Edit proposer (new in v2)

Input: cluster prose, flag, voice spec, claim graph context.
Output: `.lattice/edit_proposals/<cluster_id>.json`.

Runs only when the author chooses suggest-changes mode for a flag. Distinct from the renderer in prompt and in purpose.

Per-cluster prompt structure:

```
You are not generating new prose. You are proposing surgical edits to
existing prose to address a specific flag while preserving everything
else.

CURRENT PROSE:
{full text of the cluster}

FLAG:
Rule: {rule_id}
Description: {what the rule says}
Location: {sentence range or paragraph}

VOICE CONSTRAINTS:
{relevant voice rules}

CLAIM GRAPH CONTEXT:
{claims this cluster covers, with their evidence}

Produce one or more edit proposals, each with:
- type: replace | insert | delete | split_paragraph | merge_paragraphs | reorder_sentences
- original: exact text being changed (must match the prose)
- proposed: replacement text
- rationale: one sentence explaining the edit
- confidence: high | medium | low

Do not propose edits beyond what the flag requires.
Do not rewrite the cluster.
Preserve voice, claims, citations, and arguments outside the flagged region.
```

Edit proposals are returned as JSON. The TUI presents them one at a time with accept/reject/edit-the-proposal/defer buttons.

### 5.12 Edit applier

Input: accepted edit proposals, current prose file.
Output: updated prose file, `decisions/edit_decisions.json`, cluster's `prose_state` set to `edited`.

Mechanical. Applies each accepted edit in order, validates the original text matches before replacement, logs the change.

After application, the cluster's prose_state is `edited`. On next render of that cluster, the renderer prompts: "This cluster has accepted edits. Preserve them, or regenerate from graph?"

### 5.13 Voice consistency check (optional, on-demand)

Input: cluster with prose_state `edited`, voice file, claim graph.
Output: drift score, optional rewrite recommendation.

Runs on demand or on a schedule. For each edited cluster, runs the renderer fresh from the graph and computes semantic similarity between the renderer's output and the current prose. Below a threshold (e.g. 0.7), flags the cluster for optional rewrite.

The author chooses whether to preserve their edits or accept the fresh generation.

---

## Part 6: Project folder

```
my_paper/
├── structure/
│   ├── outline.md              one of:
│   ├── argument.argus.json     Argus export
│   └── draft.docx              bulleted Word doc
├── refs/
│   ├── papers/
│   ├── notes/
│   ├── data/
│   ├── prior_writing/
│   └── web/
├── voices/
│   ├── academic.voice.md
│   ├── journalistic.voice.md
│   └── policy.voice.md
├── figures/
├── config.yml
├── .lattice/
│   ├── author_graph.json
│   ├── author_graph_history/
│   ├── shadow_graph.json
│   ├── source_store.json
│   ├── cluster_plan.json
│   ├── shadow_reports/
│   ├── shadow_decisions.json
│   ├── audit_flags.json
│   ├── edit_proposals/
│   │   └── c.gap1.evidence.json
│   ├── edit_decisions.json
│   ├── drafts/
│   │   └── academic/
│   │       └── cluster_c.gap1.evidence.md
│   ├── cache/
│   │   ├── source_hashes.json
│   │   └── shadow_extractions/
│   ├── audit/
│   ├── examiner_reviews/
│   └── runs/
└── outputs/
    ├── paper.academic.md
    └── pitch.journalistic.md
```

---

## Part 7: CLI

```
lattice init <project>                     scaffold folders, prompt for structure
lattice ingest <project>                   rebuild author_graph from structure/
lattice index <project>                    rebuild source_store from refs/
lattice enrich <project>                   bind author claims to source passages
lattice shadow <project> [--blind]         build shadow graph + report
lattice review <project>                   shadow report TUI
lattice plan <project> --voice <n>         build cluster plan
lattice render <project> --voice <n>       produce one output
lattice audit <project> --voice <n>        run auditor on last render
lattice flags <project> --voice <n>        flag review TUI
lattice propose <project> --voice <n>      run edit proposer for accepted suggest-changes flags
lattice edits <project> --voice <n>        edit review TUI
lattice apply <project> --voice <n>        apply accepted edits to prose
lattice consistency <project> --voice <n>  run voice consistency check on edited clusters
lattice run <project> --voice <n>          full pipeline through audit
lattice run-clean <project> --voice <n>    discard caches and rerun

lattice voices list
lattice voices new <n>
lattice voices validate <file>

lattice diff <project> <before> <after>
lattice status <project>
lattice resume <project>
lattice export <project> --to argus
```

Flags: `--force`, `--cluster <id>`, `--section <id>`, `--dry-run`, `--verbose`, `--quiet`, `--from-clean`.

---

## Part 8: Voice specification

A voice is a single markdown file with structured YAML frontmatter and a free-text section for examples and notes. Voices live in `voices/`. Selected at render time via `--voice <n>`.

### 8.1 Layer hierarchy

1. **architecture**: document template (six_element_paper, nature_compressed, review_paper, policy_brief, journalistic_feature, freeform)
2. **citation**: engagement strategy (engagement_level, reporting_verbs, synthesis_threshold, positioning_required_for, forbid_catalogue_pattern)
3. **register**: prose texture (formality, sentence_length, hedge_density, lexicon, first_person)
4. **stance**: claim positioning (default, user_synthesis_stance, counterclaim_treatment, uncertainty_display, unsupported_synthesis_treatment)
5. **attribution**: citation formatting (style, page_specificity, quote_threshold)
6. **paragraph**: rhetorical flow (shape, length, topic_sentence_required, cohesion, forbidden_paragraph_openers)
7. **role_templates**: per-role rendering instructions
8. **transitions**: per-relationship-type connective phrases
9. **prohibitions**: hard rules with replacement options
10. **preferences**: soft guides
11. **figures**: figure handling
12. **statistics**: presentation rules
13. **review_paper**: extension when architecture is review

### 8.2 Default flag mode table

Voices declare which flag types default to which edit mode:

```yaml
flag_default_modes:
  architecture_missing_section: rewrite
  architecture_hourglass_break: rewrite
  citation_engagement_weak: suggest_changes
  citation_catalogue_pattern: suggest_changes
  citation_no_relevance: suggest_changes
  claim_coverage_orphan_sentence: rewrite
  voice_prohibition_violation: suggest_changes
  voice_banned_word: suggest_changes
  sentence_subject_verb_distance: suggest_changes
  sentence_expletive_construction: suggest_changes
  quantification_unquantified_magnitude: suggest_changes
  paragraph_no_topic_sentence: suggest_changes
  paragraph_continuation_opener: suggest_changes
  paragraph_too_long: suggest_changes
  formality_contraction: suggest_changes
  formality_rhetorical_question: suggest_changes
  skim_target_weak: rewrite
  examiner_review_concern: author_choice
```

### 8.3 Worked example

See `examples/voices/academic.voice.md` for the full canonical academic voice file, with all 13 layers populated based on the Cambridge Engineering writing tradition (Allwood, Williams, Schimel, Sword, Graff and Birkenstein, plus Serrenho's supervisor feedback patterns).

The journalistic and policy voices follow the same template structure with different values.

---

## Part 9: Long-form handling

Documents above 6,000 words follow the cluster-level rendering path described in Section 5.8. Specific behaviours:

- **Cluster sizing.** Default target 150-300 words. Author can override per cluster with `[words: N]` in the outline.
- **Parallel rendering.** Up to 10 concurrent cluster renders, rate-limited by API.
- **Section assembly.** Clusters concatenated within sections with no inter-cluster generation. Cohesion comes from the transition contract.
- **Cohesion validation.** A separate auditor pass after rendering checks each cluster's opening picks up the previous cluster's close. Failures regenerate only the affected cluster with a more explicit transition instruction.
- **Synthesis density for review papers.** When `architecture.template = review_paper`, a `synthesis_density: high` field forces synthesis paragraphs at minimum every 4 clusters and tracks `multi_source_per_paragraph_target` (default 2.5).
- **Resume on failure.** Per-cluster state is persisted. A failed render of one cluster doesn't block the others; the failed cluster can be retried independently.

For a 12,000-word literature review with 30 references: roughly 60-80 clusters, 6-10 minutes wall-clock for a fresh render, 30 seconds per single-cluster regen after edits.

---

## Part 10: Edit cycles

| Edit | Triggers |
|---|---|
| Add PDF to refs/ | Index source, enrich affected claims, re-shadow, re-differ |
| Edit bullet in outline.md | Re-ingest, re-enrich claim, mark cluster dirty |
| Change voice prohibition list | Re-audit only |
| Change voice register | Mark all clusters of that voice dirty |
| Accept shadow flag | Update graph, mark affected clusters dirty |
| Accept rewrite flag | Re-render affected cluster |
| Accept suggest-changes edit | Apply to prose, set cluster prose_state to edited |
| Reject suggest-changes edit | Log decision, no prose change |
| Manually edit prose in outputs/ | Set prose_state to edited, log "external edit" |

---

## Part 11: Resume and failure

State persisted after every LLM call in `.lattice/runs/<timestamp>/state.json`.

Per-stage recovery:

- Indexer file failure: skip, flag, continue
- Extractor passage failure: retry once, flag as "unparsed", continue
- Enricher claim failure: mark `no_bind_reason:error`, continue
- Shadow mapper mid-clustering: restart from partial results
- Renderer cluster failure: retry once, leave blank with marker, continue
- Edit proposer failure: skip flag, log
- Auditor failure: skip check category, log

Never abandon a run on single-stage failure.

---

## Part 12: Cost estimates

For a 12,000-word academic literature review, 30 references:

| Stage | First run | Subsequent runs |
|---|---|---|
| Index 30 PDFs | 5 min, 5k tokens | 30s (cached) |
| Ingest structure | 30s, 3k tokens | same |
| Enrich | 3 min, 40k tokens | 30s per changed claim |
| Shadow map | 10 min, 150k tokens | 2 min (cached extractions) |
| Differ | 1 min, 10k tokens | 30s |
| Plan | 30s, 5k tokens | 10s |
| Render (60-80 clusters, parallel) | 8 min, 200k tokens | per-cluster 30s, 3k tokens |
| Audit | 3 min, 40k tokens | same |
| Edit proposer (10 flags) | 2 min, 30k tokens | per-flag 15s, 3k |
| **Total first run** | 30 min, 480k tokens |  |
| **Typical re-run after edits** | 3 min, 25k tokens |  |

On Max subscription: first runs are noticeable, subsequent runs fine.

---

## Part 13: Build order

1. Scaffold CLI with `typer`, implement `init` and `status`
2. Source indexer (no LLM): PDF/DOCX/MD/XLSX extraction, hashing, stable IDs
3. Structure ingester for markdown outline
4. Argus JSON ingester
5. DOCX ingester
6. Enricher (LLM-bound): test on a known-good outline
7. Voice file format and parser
8. Write academic voice as first canonical example
9. Assembler: architecture validation, cluster construction, citation strategy
10. Renderer for academic voice on a small example
11. Auditor (claim coverage and voice compliance first)
12. Edit proposer
13. Edit applier and TUI
14. Journalistic voice (verify same graph produces meaningfully different output)
15. Shadow mapper (hardest stage)
16. Differ and shadow report TUI
17. Policy voice and examiner review
18. Resume, cache management, diff detection
19. Section-level voice overrides for hybrid documents
20. Argus export path
21. Voice consistency check
22. Polish, testing, user-facing docs

---

## Part 14: Acceptance criteria

Lattice v1 is done when David can:

1. Take an existing Argus project, drop it into `structure/`, drop relevant PDFs into `refs/papers/`, run `lattice run --voice academic`.
2. Read a shadow report with at least 3 useful flags he hadn't considered.
3. Accept some flags, reject others, each logged with rationale.
4. Get a rendered paper comparable in quality to Draft 1 with the content depth of Draft 2.
5. Run with `--voice journalistic` and get a meaningfully different document from the same graph.
6. Edit one bullet in `outline.md`, re-run, see only the affected cluster regenerate.
7. Accept a suggest-changes edit on a citation, see the prose update without regenerating the cluster.
8. Reject another suggest-changes edit, see it stay rejected on the next audit.
9. Add a new PDF, re-run, see shadow report update with new flags.
10. Browse git history of the argument graph across runs.
11. Export the working graph back to Argus JSON.
12. Never see the tool silently revise his argument structure.
13. Generate a 12,000-word literature review without quality degradation.

---

## Part 15: Out of scope for v1

- Web or GUI interface (CLI only)
- Auto-generated figures (manual SVG/PNG drop-in only)
- Voice authoring interface (voices edited as markdown files by hand)
- Collaborative editing
- Cloud sync
- Non-markdown outputs (pandoc pipeline deferred)
- Real-time shadow mapping as the author types
- Automatic corpus discovery
- Automatic Argus edge labelling on import (TUI only)

---

## Implementation deltas

The implementation has moved past this spec in several places. For an up-to-date description of the runtime see [`../README.md`](../README.md). Highlights:

- **Web UI** (FastAPI + WebSockets + cytoscape) replaces the originally-spec'd CLI/TUI as the primary surface; the CLI still works.
- **Activity model**: the spec's `quick / standard / deep` review levels are gone. They've been replaced with seven verb-named entry points (`ingest`, `scaffold`, `draft`, `find_gaps`, `refine`, `restructure`, `review`), each independently runnable and re-runnable, with state-aware locking based on five filesystem-derived states (S0–S4).
- **Nested sections (3 levels)**: `# A.` → `## A.1` → `### A.1.1`. The data model already supported `Section.parent`; the markdown ingester, assembler, hierarchy API, and graph viz now all honour it.
- **Lit-gaps replaces "source-gap-vs-reference-doc"**: Find gaps now does per-section literature-gap analysis using Claude + OpenAlex verification, with no need for an external reference document. The original SourceGapReview module still exists but is unwired from the UI.
- **Restructure**: new advisory module that audits section + cluster ordering against academic-writing rules, never mutates the graph.
- **Review**: new supervisor-style review pipeline producing per-cluster word-level track-changes via `difflib`, per-section critiques, and an overall assessment.
- **Compare**: cross-project structural diff + LLM-driven thesis comparison and claim pairing across two `author_graph.json` files.
- **Per-section parallel relationship inference**: the original single-call inference was rewritten to chunk by section. A 51-section paper now gets 51 parallel calls and produces ~1-3 connections per claim instead of the previous hard-capped <0.3.
- **Auto-outliner**: bumped truncation 24k → 100k chars; removed the "4-7 sections" cap; added subsection support; added a `max_depth` parameter the UI exposes as a Section-depth picker on Scaffold.

---

## End of spec
