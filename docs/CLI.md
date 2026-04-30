# CLI Reference

All commands take a project path as the first positional argument. Most also take `--voice <name>` to specify which voice to use.

> The web UI is the primary surface in current builds (`lattice serve`). Most CLI commands have a more discoverable counterpart on the Activities tab. See [`../README.md`](../README.md) for the activity model. CLI commands below remain available for scripting and CI.

## Web UI

### `lattice serve [--projects-root <path>] [--host 127.0.0.1] [--port 5173]`

Start the FastAPI app. Defaults to projects root `~/lattice/`, host `127.0.0.1`, port `5173`. Open the printed URL in a browser.

## Lifecycle commands

### `lattice init <project>`

Scaffold a new project. Creates `structure/`, `refs/{papers,notes,data,prior_writing,web}/`, `voices/`, `figures/`, `config.yml`, `.gitignore`, and an empty `outline.md`. Copies `examples/voices/academic.voice.md` into `voices/`.

### `lattice status <project>`

Print current state: indexed sources, claim count, last shadow run, last render per voice, last audit, count of pending flags by category and voice.

## Per-stage commands

### `lattice ingest <project>`

Rebuild author_graph from `structure/`. Auto-detects format (markdown, docx, argus json) by file extension.

### `lattice index <project> [--force]`

Rebuild source_store from `refs/`. Skips files whose hash is unchanged unless `--force`.

### `lattice enrich <project>`

Bind author claims to source passages. One LLM call per claim per cited source.

### `lattice shadow <project> [--blind]`

Build shadow graph and report. Default thesis-anchored. `--blind` ignores even the thesis.

### `lattice review <project>`

Interactive shadow report TUI. One flag at a time: accept, accept with edits, reject, defer.

### `lattice plan <project> --voice <n>`

Build cluster plan from working graph and voice. Validates architecture template; flags structural issues.

### `lattice render <project> --voice <n> [--cluster <id>] [--section <id>] [--force]`

Render clusters to prose. Without `--cluster` or `--section`, renders all dirty clusters. With `--force`, re-renders all.

### `lattice audit <project> --voice <n>`

Run all audit checks on the latest rendered document for the given voice. Produces `audit_flags.json`, `audit.md`, `examiner_review.md`.

### `lattice flags <project> --voice <n>`

Flag review TUI. Per-flag: accept (in default mode or toggle), reject, defer. Bulk operations supported.

### `lattice propose <project> --voice <n>`

For every flag with decision `accept_suggest_changes`, run the edit proposer. Produces edit proposals in `.lattice/edit_proposals/`.

### `lattice edits <project> --voice <n>`

Edit review TUI. Per-edit-proposal: accept, reject, edit-the-proposal, defer.

### `lattice apply <project> --voice <n>`

Apply accepted edit proposals to the prose files. Sets affected clusters to `prose_state: edited`.

### `lattice consistency <project> --voice <n>`

For each cluster with `prose_state: edited`, re-render from graph and compare. Flag drift below threshold.

## Pipeline commands

### `lattice run <project> --voice <n>`

Full pipeline through audit, with two interactive checkpoints (after shadow, after audit).

Sequence: ingest, index, enrich, shadow, review (interactive), plan, render, audit, flags (interactive). If any flags accepted as suggest_changes: propose, edits (interactive), apply.

### `lattice run-clean <project> --voice <n>`

Discard `.lattice/` caches and rerun full pipeline.

### `lattice resume <project>`

Resume an interrupted run from the last successful stage.

## Voice management

### `lattice voices list <project>`

List voices in `voices/` with their description and architecture template.

### `lattice voices new <project> <name>`

Scaffold a new voice file from `examples/voices/template.voice.md`.

### `lattice voices validate <file>`

Check a voice file for syntactic and semantic errors. Prints the merged configuration for voices using `extends:`.

## Misc

### `lattice diff <project> <before_snapshot> <after_snapshot>`

Show graph changes between two history snapshots.

### `lattice export <project> --to argus`

Export the working graph to Argus-compatible JSON.

## Global flags

- `--verbose / --quiet`: control output verbosity
- `--dry-run`: don't write any files

## Environment variables

- `ANTHROPIC_API_KEY`: required
- `LATTICE_DEFAULT_MODEL`: override the default model
- `LATTICE_PARALLEL_RENDERS`: override max concurrent renders

## Exit codes

- 0: success
- 1: stage failed (recoverable; use `resume`)
- 2: configuration error
- 3: validation error
- 4: API error not recoverable by retry
