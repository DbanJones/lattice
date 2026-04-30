# Handoff: Build Order and Working Notes

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
