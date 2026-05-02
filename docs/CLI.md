# CLI Reference

All commands take a project path as the first positional argument. Most stage commands also take `--voice <name>`.

> The web UI (`lattice serve`) is the primary surface in current builds; most CLI commands have a more discoverable counterpart on the Activities tab. See [`../README.md`](../README.md) for the activity model. The CLI commands below remain available for scripting and CI.

## Web UI

### `lattice serve [--projects-root <path>] [--host 127.0.0.1] [--port 5173] [--reload]`

Start the FastAPI app. Defaults: projects root `~/lattice/`, host `127.0.0.1`, port `5173`. Open the printed URL in a browser. `--reload` enables uvicorn auto-reload for development.

## Lifecycle commands

### `lattice init <project>`

Scaffold a new project. Creates `structure/`, `refs/{papers,notes,data,prior_writing,web}/`, `voices/`, `figures/`, `config.yml`, `.gitignore`, and an empty `outline.md`. Copies `examples/voices/academic.voice.md` into `voices/`.

### `lattice status <project>`

Print current state: indexed sources, claim count, section count, relationship count, cluster count, last shadow run, last render per voice, last audit, count of pending flags by category and voice.

## Per-stage commands

### `lattice ingest <project>`

Rebuild author_graph from `structure/`. Auto-detects format (markdown, docx, argus json) by file extension.

### `lattice annotate <project>`

Run the contextual annotator over the parsed graph: extract inline citations, classify thesis and section roles, infer per-section claim roles and types, infer relationships (deterministic role-chain pass + per-section LLM pass), extract mechanisms for analytical claims, derive `thesis_argued`, score every claim's `importance`. Writes `structure/outline.annotated.md` so the author can review and edit. Idempotent — preserves any author-supplied tags.

### `lattice index <project> [--force]`

Rebuild source_store from `refs/`. Skips files whose SHA256 hash is unchanged unless `--force`.

### `lattice enrich <project>`

Bind author claims to source passages. One LLM call per claim per cited source. Sets each Evidence's `binding_strength` to `strong`, `weak`, `none`, or `contradictory`.

### `lattice coverage <project>`

Interactive TUI for walking unbound or contradictory claims. Per claim: add a binding, mark as `author_origin`, or skip. Run before `render` to avoid the unrenderable-cluster state.

### `lattice shadow <project> [--blind]`

Build shadow graph and report. Default: thesis-anchored. `--blind` ignores even the thesis. Requires `ANTHROPIC_API_KEY`.

### `lattice review <project> [--accept <diff_id>] [--reject <diff_id>] [--rationale <str>]`

List shadow diffs and apply decisions. Accepted diffs update the author graph (logged to `shadow_decisions.json`).

### `lattice plan <project> --voice <n>`

Build cluster plan from working graph and voice. Validates architecture template; flags structural issues. Deterministic, no LLM.

### `lattice render <project> --voice <n> [--cluster <id>] [--section <id>] [--force] [--mode chunked|cluster] [--chunk-min 3] [--chunk-max 4] [--max-passes 3] [--no-progress]`

Render clusters to prose.

- Default mode is `chunked` (groups 4-5 clusters per LLM call so the model sees the full argument flow within the chunk).
- `--mode cluster` (or `--per-cluster`) forces the original single-cluster path; useful for surgical re-renders of one cluster via `--cluster <id>`.
- Without `--cluster` or `--section`, renders all dirty clusters. With `--force`, re-renders all.
- After rendering, runs the readiness check; if `Config.autocorrect` is `safe` or `aggressive`, autofix runs and the document is re-rendered until readiness passes or `--max-passes` is reached.

### `lattice audit <project> --voice <n>`

Run all audit checks on the latest rendered document for the given voice. Produces `audit_flags.json`, `audit.md`, and `examiner_review.md`. Includes deterministic checks (architecture, sentence, paragraph, coverage, ordering, formality, boilerplate, readiness) and LLM-bound checks (citation engagement, examiner).

### `lattice flags <project> --voice <n> [--accept <id>] [--reject <id>] [--accept-all-category <cat>] [--reject-all-minor]`

Flag review TUI. Per-flag: accept (in default mode or toggle), reject, defer. Bulk operations supported: accept all of category X, reject all minor, etc.

### `lattice propose <project> --voice <n>`

For every flag with decision `accept_suggest_changes`, run the edit proposer. Produces edit proposals in `.lattice/edit_proposals/`.

### `lattice edits <project> --voice <n> [--accept <id>] [--reject <id>]`

Edit review TUI. Per-edit-proposal: accept, reject, edit-the-proposal, defer.

### `lattice apply <project> --voice <n>`

Apply accepted edit proposals to the prose files. Validates that `original_text` exists in the prose before replacing. Sets affected clusters to `prose_state: edited`.

### `lattice autofix <project> --voice <n> [--level none|safe|aggressive]`

Chain flag acceptance → edit proposing → edit applying without per-flag manual review. Behaviour gated by `Config.autocorrect`:

- `none` — refuses to autofix; author resolves every flag manually.
- `safe` (default) — accepts flags whose `default_mode=suggest_changes` (mechanical prose nits — weasel words, citation engagement, formality), proposes edits, auto-accepts the proposals, applies them. Never deletes content. Never mutates the graph.
- `aggressive` — runs the safe pass; additionally accepts `default_mode=rewrite` flags (clusters marked dirty for re-render); deletes orphan sentences flagged by the coverage check. Still never mutates the graph.

`--level` overrides the config setting per-invocation. The `lattice render` command also invokes autofix when finalise refuses, retrying delivery after the autofix pass.

### `lattice voice-review <project> --voice <n>`

Whole-document voice audit, distinct from the per-cluster checks of `audit`. Audits aggregate statistics that only make sense at document scale: sentence-length distribution, opener variety, reporting-verb variety, hourglass shape, end-of-conclusion strength, thesis drift between `thesis_statement` and `thesis_argued`. Output: `outputs/voice_review.<voice>.md`.

### `lattice source-review <project> --voice <n> --reference <path>`

Compare the rendered paper against a richer reference document the author considers authoritative (an earlier full draft, a primary-source compendium, the human-written long form). Surfaces specific content the reference carries but the render lacks. Categories: `quantitative`, `named_scholar`, `mechanism`, `analytical_move`, `arithmetic`, `named_example`, `structural`. Each gap is tagged with a `target_claim_id`. Writes `outputs/source_gap_review.<voice>.md` (human) and `.lattice/source_gap_review.<voice>.json` (machine).

### `lattice source-review-apply <project> --voice <n> [--interactive | --batch] [--accept-all-with-targets] [--only <cats>]`

Walk the source-gap report. Accepted gaps are injected per category: `mechanism` gaps set `Claim.mechanism` on the target; `quantitative` / `arithmetic` / `named_scholar` / `named_example` gaps append the snippet as `Evidence.quote_text`. `analytical_move` and `structural` gaps are logged for manual handling. Decisions persist to the JSON report; re-runs skip already-decided gaps. Every pass appends to `.lattice/source_gap_decisions.json`.

### `lattice consistency <project> --voice <n> [--threshold 0.35]`

For each cluster with `prose_state: edited`, re-render from the graph and compute embedding similarity. Flag drift below threshold.

### `lattice rescaffold <project> --voice <n> [--threshold 0.5]`

Propose a metrics-driven rescaffold of the document. Reads the current author graph, computes argument strength + breadth metrics, and for every sub-score below `--threshold` generates structural operations (split section, add stub, reorder, move-to-offcuts) and claim-level advisories (bind evidence, add mechanism, diversify sources, address counters). Predicts metric deltas if every operation were applied.

Pure analysis — never mutates the graph or the outline. Writes:

- `.lattice/rescaffold_plan.json` — machine-readable plan with per-op predicted deltas + per-claim claim_size scores
- `outputs/rescaffold_plan.<voice>.md` — human-readable scoreboard, diagnosis, operations, advisories, proposed offcuts, top-25 claim-size table

A separate apply step (`lattice rescaffold-apply`, not yet implemented) walks accepted operations and edits the outline after explicit confirmation.

### `lattice fill-mechanisms <project> [--voice academic] [--editor] [--dry-run] [--limit N] [--min-importance 0.5]`

Walk empirical / methodological claims that lack a `[mechanism: ...]` tag. The dominant rescaffold-planner advisory class on real, well-scaffolded papers — running this directly is faster than going through the full rescaffold flow.

For each candidate (sorted by importance descending): show the claim, prompt for a mechanism (or `--editor` to launch `$EDITOR`), append `[mechanism: <text>]` to the bullet in place. Snapshots `structure/outline.md` to `structure/outline.pre-fill-mechanisms.md` before editing. Idempotent — won't double-tag a bullet that already has a mechanism. Sanitises `[`/`]` in mechanism text. Decisions logged to `.lattice/fill_mechanisms_decisions.json`.

The author graph is NOT mutated — re-ingest after running this so the graph picks up the new tags.

### `lattice fill-evidence <project> [--voice academic] [--dry-run] [--limit N] [--min-importance 0.5] [--supporters-only]`

Walk weakly-grounded empirical / methodological / normative / definition claims and bind evidence in place. For each candidate, choose:

- `r` — append `[ref: <citekey>]` (you know which source backs the claim)
- `h` — append `[evidence_status: source_hint]` (you've located the source but haven't bound a passage)
- `u` — append `[evidence_status: unbound]` (explicit acknowledgement of the gap)
- `s` — convert to `[type: user_synthesis]` (claim is your own analysis, not evidence-backed)
- enter — skip · `q` — quit

Supporters of the thesis sort first by default. `--supporters-only` filters to just that subset. Idempotent against duplicate citekeys / status tags. Snapshots to `structure/outline.pre-fill-evidence.md`. Decisions logged to `.lattice/fill_evidence_decisions.json`.

## Pipeline commands

### `lattice run <project> --voice <n> [--with-shadow] [--resume] [--max-passes 3] [--min-delta 5] [--review]`

Hands-free pipeline: annotate → ingest → index → enrich → plan → render → audit → autofix → DOCX export. Iterates the audit/autofix loop up to `--max-passes` times, stopping when the change in flag count drops below `--min-delta`. `--with-shadow` adds the shadow-mapper stage. `--review` adds the supervisor review at the end. `--resume` continues from the last successful stage.

### `lattice run-clean <project> --voice <n>`

Discard `.lattice/` caches and rerun the full pipeline from scratch.

### `lattice resume <project>`

Resume an interrupted run from the last successful stage.

## Visualisation and export

### `lattice graph <project> [--show/--no-show] [--mermaid/--no-mermaid] [--html/--no-html]`

Render the argument scaffold. Default: print a Rich tree to the terminal. `--mermaid` writes a Mermaid diagram to `outputs/`. `--html` writes an interactive cytoscape graph to `outputs/`.

### `lattice export <project> --to argus`

Export the working graph to Argus-compatible JSON.

### `lattice diff <project> <before_snapshot> <after_snapshot>`

Show graph changes between two history snapshots. (Stub — not yet implemented.)

## Voice management

### `lattice voices list <project>`

List voices in `voices/` with their description and architecture template.

### `lattice voices new <project> <name>`

Scaffold a new voice file from `examples/voices/template.voice.md`.

### `lattice voices validate <file>`

Check a voice file for syntactic and semantic errors. Prints the merged configuration for voices using `extends:`.

## Global flags

- `--verbose / --quiet`: control output verbosity
- `--dry-run`: don't write any files

## Environment variables

- `ANTHROPIC_API_KEY`: required (read from `.env` or environment)
- `LATTICE_DEFAULT_MODEL`: override the default model
- `LATTICE_PARALLEL_RENDERS`: override max concurrent renders

## Runtime assumption

Lattice shells out to the `claude` CLI for LLM calls; make sure it is on `PATH`. The `anthropic` SDK is in the dependency list but the production code path uses the subprocess wrapper (`utils/llm.py`).

## Exit codes

- 0: success
- 1: stage failed (recoverable; use `resume`)
- 2: configuration error
- 3: validation error
- 4: API error not recoverable by retry
