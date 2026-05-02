# Handoff: Build Order and Working Notes

> **Status note (2026-04-30):** the original handoff sequence below has been completed and the implementation has moved beyond it. For an accurate description of what runs today — including the web UI, activity model, lit-gaps, restructure, review, and compare features — read [`../README.md`](../README.md) first. This document remains useful as a record of the build order that got us here, and as a checklist for re-implementing in a different runtime.

This document is the bridge between the spec and the code. Read `SPEC.md` first.

## How to use this document

The spec describes the finished system. This document describes how to get there step by step. Each step is small enough to complete and test in isolation. Each step has a clear input (what already exists) and output (what should exist when done).

Work through steps in order. Don't jump ahead. The spec and the data model are stable; the implementation order has been chosen to minimise rework.

## Working environment

- Python 3.11+
- Use `uv` for package management
- Use `typer` for CLI, `rich` for TUI, `pydantic` for data models
- Use `anthropic` Python SDK for Claude API calls
- Use `pypdf`, `python-docx`, `openpyxl`, `markdown-it-py` for source parsing
- All async work uses `asyncio` and `httpx`
- All persistence uses pydantic models serialising to JSON

## Phase 0: Foundation (steps 1-3)

### Step 1: Project skeleton

Confirm the repository structure matches the layout in `README.md`. Run `uv sync`. Confirm `python -c "import lattice"` works.

Output: `src/lattice/__init__.py` defining `__version__ = "0.1.0"` and nothing else.

### Step 2: Configuration loading

Build `src/lattice/utils/config.py`. Loads from `config.yml` in the project folder. Provides typed access to:

- `default_voice`: string
- `default_model`: string (default `claude-sonnet-4-5`)
- `model_per_stage`: dict mapping stage names to model strings
- `parallel_renders`: int (default 8)
- `cache_dir`: path (default `.lattice/cache`)
- `output_dir`: path (default `outputs`)

Loads from environment for secrets:
- `ANTHROPIC_API_KEY`

Output: `Config` class with `Config.load(project_path)` classmethod.

### Step 3: Data model with pydantic

Build `src/lattice/graph/models.py`. Pydantic models for every entity in `DATA_MODEL.md`:

- `Source`, `Passage`
- `Claim`, `Evidence`, `ScopeCondition`
- `Relationship`
- `Section`, `Cluster`, `ClaimRoleInCluster`
- `EditProposal`
- `AuditFlag`
- `ShadowDiff`

Each model has `.to_json()` and `.from_json()` plus `model_validate_json()`.

Build `src/lattice/graph/store.py`. The `GraphStore` class persists models to `.lattice/`. Append-only with version history. Operations:

- `store.load(project_path)` loads everything
- `store.save_claim(claim)` appends to author_graph.json with timestamp
- `store.get_claim(claim_id)` returns latest version
- `store.list_claims(filter)` returns matching claims
- (similar for sources, sections, clusters, etc.)
- `store.snapshot(label)` creates a versioned snapshot in `author_graph_history/`

Output: working data model and persistence layer. Run `pytest tests/test_graph.py` to confirm.

## Phase 1: Inputs (steps 4-7)

### Step 4: CLI scaffold

Build `src/lattice/cli/main.py` with typer. Implement:

- `lattice init <project>`: creates folder structure
- `lattice status <project>`: shows current state

`init` creates: `structure/`, `refs/papers/`, `refs/notes/`, `refs/data/`, `voices/`, `figures/`, `config.yml`, `.gitignore`. Writes a stub `outline.md` and copies `examples/voices/academic.voice.md` into `voices/`.

`status` reads the project folder and reports: number of sources indexed, number of claims, last shadow run, last render, last audit, number of pending flags.

Output: `lattice init my_paper && lattice status my_paper` works end to end.

### Step 5: Source indexer

Build `src/lattice/indexer/`. The `SourceIndexer` reads files from `refs/` and produces source store entries.

Sub-folder dispatch:

- `papers/*.pdf` → `src/lattice/indexer/pdf.py` using pypdf
- `papers/*.docx` → `src/lattice/indexer/docx.py` using python-docx
- `notes/*.md` → `src/lattice/indexer/markdown.py` using markdown-it-py
- `data/*.xlsx` → `src/lattice/indexer/spreadsheet.py` using openpyxl
- `web/*.html` → `src/lattice/indexer/html.py` using BeautifulSoup
- `prior_writing/*` → routed by extension, tagged `author_origin: true`

Each indexer implements `Indexer.index(file_path) -> Source`. Returns a `Source` with stable passage IDs. IDs are `p.<page>.<seq>` for PDFs, `p.<line>.<seq>` for markdown, etc. See `DATA_MODEL.md` for full ID conventions.

Hash each file with SHA256. Skip re-indexing if hash unchanged. Cache hashes in `.lattice/cache/source_hashes.json`.

CLI: `lattice index <project>`.

No LLM calls in this stage except OCR fallback. OCR uses `tesseract` if available; otherwise flag the file as needs-manual-extraction.

Output: PDFs in `refs/papers/` produce `source_store.json` with passages.

### Step 6: Markdown outline ingester

Build `src/lattice/ingester/markdown.py`. Parses the outline syntax from spec Section 4.2.

Recognised tags: `[ref:]`, `[user_synthesis]`, `[weak/strong/contested]`, `[role:]`, `[depth:]`, `[words:]`, `[skip]`, `[central_contribution]`. Recognised prefixes: `THESIS`, `COUNTER:`, `MY VIEW:`. Recognised section markers: `# A.`, `# B.`, etc.

Output: `author_graph.json` with sections, claims, and basic relationships.

For untagged bullets, infer claim type from content using a simple LLM call (see `PROMPTS.md` for the ingester prompt). Surface inferences for review in the next step.

CLI: `lattice ingest <project>`.

### Step 7: Argus ingester

Build `src/lattice/ingester/argus.py`. Reads Argus JSON export, maps to internal entities per spec Section 4.1.

For edges typed as generic `dependency`, leave them as type `unlabelled` initially. The TUI step in Phase 4 will prompt for labels.

DOCX outline ingester is a good Phase 1 finisher but can be deferred to Phase 5.

Output: Argus JSON in `structure/` produces equivalent author_graph.json.

## Phase 2: Voice and rendering (steps 8-11)

### Step 8: Voice file parser

Build `src/lattice/voice/parser.py`. Reads a `.voice.md` file with YAML frontmatter. Parses frontmatter into a `Voice` pydantic model with all 13 layers. Markdown body is preserved as `notes` field.

The `Voice` model validates:

- All required fields present
- Architecture template is a known value
- Engagement level is a known value
- Prohibitions list items have either `word` or `phrase` field
- Role templates cover all standard roles

Output: `voice = Voice.from_file("voices/academic.voice.md")` works.

### Step 9: Enricher

Build `src/lattice/enricher/`. For each claim with source citekeys, find the most relevant passage in each cited source.

Per-claim prompt: "Given this claim and the passages from this source, identify which passage best supports the claim. Return strong_bind, weak_bind, no_bind, or contradictory_bind plus the passage ID and a one-sentence justification."

Updates the claim in place with bound passages and confidence levels.

CLI: `lattice enrich <project>`.

### Step 10: Assembler

Build `src/lattice/renderer/assembler.py`. The hardest stage in Phase 2.

For each section in the author graph:

1. Walk the claim sequence. Group claims into clusters of 2-4 by topic coherence (use embedding similarity within a section as a hint, then group by relationship density).
2. Assign each cluster a role based on its claims' roles.
3. Compute target word range for each cluster from the section's `target_length` divided across clusters.
4. For each cluster, pre-compute citation strategy:
   - List sources cited by claims in the cluster
   - If 3+ sources cluster on same topic, mark `synthesis_required: true`
   - For thesis/gap/novel-method claims, mark `positioning_required`
   - Assign reporting verbs from the voice's `citation.reporting_verbs` table based on each claim's confidence
5. For each adjacent cluster pair, set the next cluster's `previous_cluster` field.

Architecture validation runs first: check the section structure matches the voice's architecture template. Flag missing sections.

Output: `cluster_plan.json`. CLI: `lattice plan <project> --voice academic`.

### Step 11: Renderer

Build `src/lattice/renderer/cluster_renderer.py`. The core of the tool.

Given one cluster, the voice, and the source store, produce one or two paragraphs of prose.

Build the prompt per the spec Section 5.8 template. Critical elements:
- Voice constraints (full role templates, transitions, prohibitions for the cluster's claim types)
- Architecture role (from section)
- Cluster role (setup/evidence/etc.)
- Previous cluster's closing sentences (if any)
- Each claim with its passage text, page numbers, assigned reporting verb
- Citation strategy (synthesis required, positioning required, catalogue forbidden)
- Transition out hint
- Target word range
- Constraint: every factual sentence must trace to a claim ID

Make the API call. If the response contains `{MISSING_CLAIM:` markers, log them as flags but proceed. Save the prose to `.lattice/drafts/<voice>/cluster_<id>.md`. Update the cluster's `prose_state: generated` and `last_rendered_hash`.

Build `src/lattice/renderer/parallel.py` for concurrent cluster rendering with `asyncio.Semaphore(parallel_renders)`.

Build `src/lattice/renderer/assembler_finalise.py` to concatenate cluster prose into `outputs/paper.<voice>.md`.

CLI: `lattice render <project> --voice academic`. Optional: `--cluster <id>` to render a single cluster.

After this step, you have minimum useful end-to-end. Test with `examples/projects/ict_forecasting/`.

## Phase 3: Audit and edit (steps 12-15)

### Step 12: Auditor

Build `src/lattice/auditor/`. One module per check category:

- `architecture.py`: section structure, hourglass shape, killer graph
- `citation.py`: engagement, catalogue patterns, reporting verb variety
- `coverage.py`: claim trace from sentences
- `voice.py`: prohibitions, banned words
- `sentence.py`: subject-verb distance, expletive constructions, active voice
- `quantification.py`: weasel words, hedging strength, named entities
- `paragraph.py`: topic sentences, cohesion, openers, length
- `formality.py`: contractions, rhetorical questions, tense
- `skim.py`: title, abstract, gap statement, captions
- `examiner.py`: six-question advisory review

Each check returns a list of `AuditFlag` objects with category, severity, default mode, rule ID, cluster ID, and the offending text snippet.

Aggregate to `audit_flags.json`. Write a human-readable summary to `audit.md` with checks grouped per the structure in Section 5.9. Examiner review separately to `examiner_review.md`.

CLI: `lattice audit <project> --voice academic`.

### Step 13: Flag review TUI

Build `src/lattice/tui/flag_review.py`. Rich-based TUI showing each flag with:

- Affected prose snippet (highlighted)
- Rule that fired
- Default edit mode
- Toggle to switch mode
- Buttons: accept, reject, defer

Bulk operations supported: accept all of category X, accept all critical, etc.

Decisions logged to `.lattice/flag_decisions.json`. Accepted flags routed: rewrite-mode flags create a `cluster_dirty` marker; suggest-changes-mode flags queue for the edit proposer.

CLI: `lattice flags <project> --voice academic`.

### Step 14: Edit proposer

Build `src/lattice/editor/proposer.py`. For each suggest-changes flag, generate edit proposals.

Per-flag prompt per Section 5.11. Returns structured JSON edit proposals. Save to `.lattice/edit_proposals/<cluster_id>.json`.

CLI: `lattice propose <project> --voice academic`.

### Step 15: Edit review TUI and applier

Build `src/lattice/tui/edit_review.py`. Per-edit accept/reject/edit-the-proposal/defer. Decisions logged.

Build `src/lattice/editor/applier.py`. Applies accepted edits to the prose file. Validates that `original_text` exists in the prose before replacing. Updates cluster's `prose_state: edited`.

CLI: `lattice edits <project> --voice academic` for review; `lattice apply <project> --voice academic` to apply accepted edits.

## Phase 4: Shadow mapping (steps 16-18)

### Step 16: Shadow mapper

Build `src/lattice/shadow/`. The most expensive stage.

Three sub-stages:
- `extract.py`: per-source extraction of atomic claims (LLM-bound, cached per source hash)
- `cluster.py`: group extracted claims by topic (embedding-based)
- `architect.py`: build relationships within and across clusters, identify thesis-relevant subgraph

Per-source extraction is parallelisable. Cache extractions in `.lattice/cache/shadow_extractions/<source_id>.json`. Re-extract only on source change.

Cluster sub-stage uses sentence-transformers embeddings (or Anthropic's embedding API if preferred) to group claims, then an LLM validation pass.

Architect sub-stage runs an LLM call per cluster pair within the same theme to identify relationships.

Output: `shadow_graph.json`. CLI: `lattice shadow <project>`.

### Step 17: Differ

Build `src/lattice/differ/`. Compares author graph and shadow graph. Produces a structured shadow report per Section 5.5.

Output: `.lattice/shadow_reports/<timestamp>.md`. CLI: `lattice differ <project>`.

### Step 18: Shadow review TUI

Build `src/lattice/tui/shadow_review.py`. For each flag in the shadow report: accept, accept with edit, reject (with rationale), defer.

Accepted flags update the author graph. The system never silently revises the author graph; every change is an explicit author decision logged to `shadow_decisions.json`.

CLI: `lattice review <project>`.

## Phase 5: Polish (steps 19-22)

### Step 19: DOCX ingester

Build `src/lattice/ingester/docx.py`. Reads bullet hierarchies from Word documents using python-docx. Preserves the same tag vocabulary as the markdown ingester.

### Step 20: Voice consistency check

Build `src/lattice/auditor/consistency.py`. For each cluster with `prose_state: edited`, run the renderer fresh and compute embedding similarity. Flag drift below threshold.

CLI: `lattice consistency <project> --voice academic`.

### Step 21: Argus exporter

Build `src/lattice/graph/export_argus.py`. Reverse of the Argus ingester. Outputs Argus-compatible JSON for the working graph.

CLI: `lattice export <project> --to argus`.

### Step 22: Run command and resume

Build `src/lattice/cli/run.py`. Orchestrates the full pipeline: ingest, index, enrich, shadow, review (interactive checkpoint), plan, render, audit, flag review (interactive checkpoint).

Build `src/lattice/utils/resume.py`. Persists per-stage state. `lattice resume <project>` continues from the last successful stage.

## Testing strategy

For each module, build at least:
- One unit test of the happy path with mocked LLM calls
- One test of the error path
- One integration test that exercises the module against `examples/projects/ict_forecasting/`

Set up `tests/conftest.py` with a fixture that loads the example project into a temp directory.

LLM-bound tests should use a `MockClaudeClient` that returns canned responses, defined per test. Don't make real API calls in tests.

## What's deliberately not in this handoff

- Performance optimisation beyond parallelism. Optimise after correctness.
- Token cost tracking dashboards. Track costs per run in a simple log.
- Multi-language support. English only in v1.
- Multi-author projects.
- Cloud sync.
- Anything in spec Part 15 (out of scope).

## Critical reminder

The author owns the structure. Every modification to the author graph flows through an explicit TUI decision. The system never silently revises. If you find yourself building a stage that updates the author graph without author approval, stop and rethink.

## Phase 6: Post-MVP additions

The following modules were added after Phases 0-5 were complete. They extend the pipeline rather than replacing it; the original 22-step build order is unchanged.

### Contextual annotator (`ingester/annotator.py`)

Sits between Step 6 (markdown ingest) and Step 9 (enricher). The deterministic ingester captures structure, tags, and explicit relationships. The annotator fills in the gaps the author did not annotate by hand:

1. Inline citation extraction (deterministic regex)
2. Thesis + section-role classification (one LLM call)
3. Per-section claim role + type inference (one LLM call per section)
4. Relationship inference — deterministic role-chain pass plus one LLM call per section
5. Mechanism extraction — captures the causal middle link for analytical claims (batched LLM)
6. Argued thesis + claim importance — derives `thesis_argued` and scores every claim's `importance` (one LLM call)

CLI: `lattice annotate <project>`. Idempotent — preserves any author-supplied tags. Skipped LLM passes leave model defaults intact (`importance=0.5`, `thesis_argued=None`).

### Outline serializer (`graph/serialize_outline.py`)

Inverse of the markdown ingester. Writes `structure/outline.annotated.md` so the author can read everything the annotator inferred and edit it back. Round-tripping the annotated outline through the ingester yields the same graph.

### Chunked renderer (`renderer/chunked_renderer.py`)

Replaces the per-cluster renderer (Step 11) as the production rendering path. Groups 4-8 clusters into a single LLM call so the model sees the full argument flow within the chunk. The per-cluster renderer remains for surgical re-renders of single clusters via `--cluster <id>`. Both paths now expose `mechanism` and `importance` and apply the same elaboration directives — diverging only in chunk-level features (callbacks, varied paragraph rhythm).

CLI: `lattice render <project> --voice <voice>` uses chunked by default; `--per-cluster` (or `--mode cluster`) forces the single-cluster path.

### Document readiness check (`auditor/readiness.py`)

Sits between Step 11 (render) and Step 12 (audit). Verifies a document is fit to deliver before any audit work runs. Blocking conditions: failed or unrendered clusters, unresolved `{MISSING_CLAIM:...}` / `{CLUSTER_UNRENDERABLE:...}` markers, missing required sections, sections with no prose, register bleed, source-order violations.

CLI: `lattice readiness <project> --voice <voice>`. The pipeline runner refuses to advance to finalise until readiness returns `is_ready=True`.

### Ordering check (`auditor/ordering.py`)

Verifies the source-order invariants that guarantee the rendered paper matches the order the author wrote: sections monotonic by position, `claim_ids` monotonic by `Claim.source_order`, cluster spans contiguous and non-overlapping. Ships as a sub-check of the readiness pass and as a standalone auditor.

### Voice compliance review (`auditor/voice_review.py`)

Whole-document voice audit, distinct from the per-cluster checks of Phase 3. Audits aggregate statistics that only make sense at the document scale: sentence-length distribution, opener variety, reporting-verb variety, hourglass shape, end-of-conclusion strength, thesis drift between `thesis_statement` and `thesis_argued`. Output: `outputs/voice_review.<voice>.md`.

CLI: `lattice voice-review <project> --voice <voice>`.

### Visualisation + DOCX with comments (`output/`)

Renders the author graph as an interactive HTML/Mermaid tree (`output/visualise.py`) and exports the assembled paper as a DOCX with audit flags inlined as Word comments (`output/docx_with_comments.py`). Both are post-render, optional outputs.

### Coverage review TUI (`tui/coverage_review.py`)

Sits alongside the flag and edit review TUIs. Surfaces claims with no evidence binding and lets the author add bindings, mark `author_origin`, or skip the claim before the renderer hits the unrenderable state.

### Source-gap review (`auditor/source_gap_review.py` + `auditor/source_gap_apply.py`)

Compares the rendered paper against a richer reference document the author considers authoritative (e.g. an earlier full draft, a primary source compendium, the human-written long form), and surfaces specific content the reference carries but the render lacks. Categories: `quantitative`, `named_scholar`, `mechanism`, `analytical_move`, `arithmetic`, `named_example`, `structural`. Each gap is tagged with a `target_claim_id` (the claim the gap most plausibly attaches to), enabling structured application.

Two-stage workflow:

1. `lattice source-review <project> --voice <voice> --reference <path>` — runs the comparison and writes both `outputs/source_gap_review.<voice>.md` (human-readable) and `.lattice/source_gap_review.<voice>.json` (machine-readable, with per-gap target_claim_ids and decision slots).

2. `lattice source-review-apply <project> --voice <voice>` — walks undecided gaps interactively (or via `--batch --accept-all-with-targets` for triage). Accepted gaps are injected per category: `mechanism` gaps set `Claim.mechanism` on the target; `quantitative` / `arithmetic` / `named_scholar` / `named_example` gaps append the reference snippet as `Evidence.quote_text` (bound weakly to `expanded_lit_review`). `analytical_move` and `structural` gaps are logged for manual handling — these require an `interpretive_pivot` relationship or a new section, which the author adds by hand.

Decisions persist to the JSON report so re-runs skip already-decided gaps. Every apply pass appends to `.lattice/source_gap_decisions.json` (audit log). The graph is saved after each pass; nothing is silently revised.

### Autofix pipeline (`auditor/autofix.py`)

Chains audit-flag acceptance → edit proposing → edit applying without per-flag manual review. Behaviour gated by `Config.autocorrect` (set in `config.yml`):

- `none` — refuses to autofix; author resolves every flag manually.
- `safe` (default) — accepts flags whose `default_mode=suggest_changes` (mechanical prose nits — weasel words, citation engagement, formality), proposes edits, auto-accepts the proposals, applies them. Never deletes content. Never mutates the graph.
- `aggressive` — runs the safe pass; additionally accepts `default_mode=rewrite` flags (clusters marked dirty for re-render); deletes orphan sentences flagged by the coverage check. Still never mutates the graph (no claims added, no relationships changed).

CLI: `lattice autofix <project> --voice <voice>` runs the pipeline standalone. The `--level` flag overrides the config setting per-invocation. The `lattice render` command also invokes autofix when finalise refuses, retrying delivery after the autofix pass — so a render call with `autocorrect: aggressive` can self-heal mechanical issues without manual intervention.

### Mechanism boilerplate auditor (`auditor/boilerplate.py`)

Post-render heuristic check. Detects mechanism-shaped prose ("X operates through Y", "creates asymmetric outcomes", "compresses useful life", "embeds divergent futures") that the LLM falls back on when source claims and bound passages do not contain real causal information. Each match emits an `AuditFlag` with `default_mode=rewrite` so accepted flags trigger re-rendering with stricter prompting via the existing flag-review loop.

Wired into the standard audit run; appears as `voice.boilerplate.<rule_suffix>` flags. Heuristic only — false positives are low-cost (an extra flag the author dismisses) and the false negatives that slip through can still be caught by the source-gap review pass.

## Phase 7: Web UI and activity model

After Phase 6, the surface area shifted from CLI-first to a FastAPI web UI organised around six verb-named activities. The CLI still works; the web UI is now the primary surface.

### Web UI (`web/app.py`, `web/static/`)

`lattice serve` boots a FastAPI app (default `127.0.0.1:5173`) with WebSocket-streamed activity progress and a static frontend (vanilla JS + cytoscape). Four tabs per project: Dashboard · Activities · Sources · Output.

### Activity dispatcher (`web/activities.py`)

Replaces the spec's `quick / standard / deep` review levels. Seven verb-named entry points, each independently runnable and re-runnable: `ingest`, `scaffold`, `draft`, `find_gaps`, `refine`, `restructure`, `review`. Activity availability is gated by the project state (S0–S4), derived from filesystem markers — locked activities show their unlock condition rather than disappearing.

### Lit-gaps (`lit_gaps/gaps.py`)

Per-section literature-gap analysis. Claude suggests canonical works, counter-arguments, and recent literature; OpenAlex verifies citations so phantom references get caught. Replaces the original "source-gap-vs-reference-doc" check as the default literature review tool. Output: `outputs/lit_gaps.<voice>.json`.

### Restructure (`restructure/restructure.py`)

Advisory analysis of section + cluster ordering against academic-writing rules. Lists specific operations (move, swap, merge, split) with confidence levels. Never mutates the graph. Output: `outputs/restructure.<voice>.json`.

### Review (`review/review.py`)

Supervisor-style critique pipeline producing per-cluster word-level track-changes (via `difflib`), per-section critiques, and an overall assessment. Outputs: `outputs/review.<voice>.md` and `outputs/review_track_changes.<voice>.md`.

### Compare (`compare/semantic.py`)

Cross-project structural diff plus LLM-driven thesis comparison and claim pairing across two `author_graph.json` files. Surfaced on the projects list page in the web UI.

### Per-section parallel relationship inference

The original single-call relationship inference (capped at "10–30 relationships") was rewritten to chunk by section so each one gets full output budget. A 51-section paper now gets 51 parallel calls and produces ~1–3 connections per claim instead of <0.3.

### Auto-outliner (`ingester/auto_outliner.py`)

LLM-bound healer for raw prose drops. Invoked when the project starts in S1 (raw outline). Bumped truncation 24k → 100k chars; removed the "4–7 sections" cap; added subsection support (up to 3 levels deep); added a `max_depth` parameter the UI exposes as a Section depth picker on Scaffold.

## Phase 8: Argument metrics + rescaffold planner

A diagnostic + planning layer over the author graph. Computed deterministically at ingest time; surfaced in the scaffold report and via dedicated CLI commands.

### Argument metrics (`graph/metrics.py`)

Two scores, each with sub-scores and human-readable observations:

- **`ArgumentStrength`** — how well does the body prove the thesis? Five sub-scores: `direct_support` (claims pointing at thesis, saturating at 5), `reachable_support` (transitive BFS through supports/extends/depends_on/is_evidence_for), `evidence_backing` (avg evidence quality across the supporting subgraph), `counter_handling` (fraction of contradictors that are themselves contradicted/qualified/pivoted), `depth` (avg backward path length). Aggregated by weighted average; weights tuned so evidence backing dominates.
- **`ArgumentBreadth`** — how wide is the argument? Six sub-scores: `section_diversity`, `source_diversity` (distinct citekeys × Shannon entropy), `claim_type_diversity` (5 types: empirical/methodological/normative/definition/user_synthesis), `relationship_type_diversity` (8 sticky+core types), `mechanism_coverage`, `section_spread` (`1 − max_section_share`).

Persisted in `ScaffoldReport.argument_metrics` so the diagram + CLI can read them without recomputation.

### Per-claim claim_size (`graph/claim_size.py`)

Pure function combining importance (40%), evidence count, mechanism presence, scope specificity, and relationship in/out-degree into a 0–1 weight. Used by the rescaffold planner to:

- Decide whether a claim warrants its own paragraph (cluster) vs merging
- Place skim-target claims (the heaviest in each section opens it)
- Identify offcut candidates (size ≤ 0.2 + no inbound = aside)

### Rescaffold planner (`restructure/rescaffold_planner.py`)

Metrics-driven structural advisor. Given an `AuthorGraph` plus its `ArgumentMetrics`, proposes:

- **Operations** (`RescaffoldOperation`): `move_claim`, `split_section`, `merge_sections`, `add_section_stub`, `reorder_within_section`, `promote_to_offcuts`. Each carries a confidence and an `expected_delta` map showing predicted metric movement.
- **Advisories** (`RescaffoldAdvisory`): `bind_evidence`, `add_mechanism`, `add_synthesis`, `add_methodological_framing`, `diversify_sources`, `add_counter_engagement`, `tag_supports_thesis`, `infer_relationships`. Claim-level recommendations no single structural move can address.
- **Diagnosis** (`RescaffoldDiagnosis`): per-sub-score record of what fired and why.

Predicts metric deltas by applying operations to a deep copy of the graph and re-running `compute_argument_metrics`. Conservatism guardrails:

- Offcut path is suppressed entirely on edge-poor graphs (`< 0.3 relationships per body claim`) — the right action is `lattice annotate`, not bulk deletion.
- `infer_relationships` advisory elevates to confidence 1.0 in that case so the diagnosis bubbles to the top.
- Offcut count capped at half-the-body, with at least 2 non-offcut body claims preserved.
- `split_section` falls back to a 2-way claim-size-balanced split when sticky-edge components are all singletons.

CLI: `lattice rescaffold <project> --voice <name>`. Output: `.lattice/rescaffold_plan.json` + `outputs/rescaffold_plan.<voice>.md`. Apply step (`lattice rescaffold-apply`) is scheduled for 2026-05-16 as a remote agent.

### Focused walkthrough commands

The dominant rescaffold-planner advisory class on real, well-scaffolded papers is `add_mechanism`. Walking advisories one-by-one through the full rescaffold-apply UX is heavy, so two focused commands sit beside it:

- **`lattice fill-mechanisms`** (`restructure/fill_mechanisms.py`) — walks empirical/methodological claims missing a `[mechanism: ...]` tag (importance ≥ `--min-importance`, default 0.5). Inline prompt by default, `--editor` launches `$EDITOR`. Snapshots `outline.md` to `outline.pre-fill-mechanisms.md`. Idempotent. Decisions logged to `.lattice/fill_mechanisms_decisions.json`.
- **`lattice fill-evidence`** (`restructure/fill_evidence.py`) — walks weakly-grounded claims (no Evidence rows OR `evidence_status` ∈ {unbound, source_hint}). Per claim, choose: `r` add `[ref: <citekey>]` · `h` set `[evidence_status: source_hint]` · `u` set `[evidence_status: unbound]` · `s` convert to `[type: user_synthesis]` · enter to skip · `q` to quit. Supporters of the thesis sort first. `--supporters-only` filters to that subset.

Both edit `outline.md` in place; the author graph is NOT mutated. Re-ingest after running so the graph picks up the new tags.

### `ScaffoldClaimReport.line`

The markdown ingester now records the 1-indexed line number of each bullet in `ScaffoldClaimReport.line`. Used by `fill-mechanisms` and `fill-evidence` to locate bullets for in-place editing without re-parsing.
