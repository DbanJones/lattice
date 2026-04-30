# Lattice

Argument-first long-form writing tool. Turns an author-built argument structure plus a folder of references plus a voice specification into polished long-form prose.

Read the full spec at [`docs/SPEC.md`](docs/SPEC.md) before starting any work.

## Quick orientation for someone new to the codebase

- This is a Python CLI tool. No web UI, no GUI. The interactive layer is a Rich-based terminal UI (TUI).
- The tool talks to Anthropic's Claude API. It expects an `ANTHROPIC_API_KEY` environment variable. Use Claude Sonnet 4.5 by default; configurable per stage.
- The tool reads from a project folder and writes to that project folder. State lives in `.lattice/` inside the project. Outputs land in `outputs/`.
- Nothing in this codebase silently changes the author's argument structure. Every modification flows through a TUI decision the author makes.

## Pipeline at a glance

```
structure + refs + voice
    │
    ▼
ingest, index, enrich, shadow, differ, review
    │
    ▼
working argument graph (.lattice/author_graph.json)
    │
    ▼
plan (assembler builds clusters from graph + voice)
    │
    ▼
render (per-cluster, parallel)
    │
    ▼
audit (categorised flags)
    │
    ▼
flag review TUI: per-flag, choose rewrite or suggest-changes
    │
    ▼
either re-render cluster or apply targeted edits
    │
    ▼
final outputs/paper.<voice>.md
```

## Build order

See [`docs/SPEC.md`](docs/SPEC.md) Part 13. Implement stages in dependency order. Don't try to build everything at once.

The minimum useful end-to-end (MUEE) target: a project with a markdown outline plus 3 PDFs plus the academic voice file should produce a coherent rendered output. Get to MUEE before working on shadow mapper, edit proposer, or polish.

## Key design choices that are non-obvious

1. **Claims are atomic, not paragraphs.** A claim is a single assertion with provenance. The graph is a set of claims plus relationships between them. Prose is generated from the graph, not stored in it.

2. **The renderer's unit is a cluster, not a section.** A cluster is 2-4 claims that render to one or two paragraphs. Sections are scoping context. This is how long-form (10k+ words) stays high-quality.

3. **Voice is a structured config, not a style guide.** Voices are YAML frontmatter plus markdown notes. The parser extracts structured fields; the markdown is for human reference. See `examples/voices/academic.voice.md`.

4. **Two edit modes.** Rewrite regenerates the cluster from the graph. Suggest-changes proposes surgical edits to existing prose. The author chooses per flag. Every flag has a default mode declared in the voice.

5. **The shadow mapper is blind to the author graph.** It only sees the corpus and the thesis. The differ compares the two graphs and produces an advisory report. The author's structure is never silently overwritten.

6. **Critique is advisory.** The auditor never edits the prose. It produces flags. The author decides what to act on.

## Repository layout

```
lattice/
├── README.md                  ← you are here
├── docs/
│   ├── SPEC.md                ← canonical spec, read first
│   ├── DATA_MODEL.md          ← detailed JSON schemas for every entity
│   ├── PROMPTS.md             ← all LLM prompts, one per stage
│   ├── VOICE_FORMAT.md        ← voice file format reference
│   ├── CLI.md                 ← command-line reference
│   └── HANDOFF.md             ← what to build first, in what order
├── src/lattice/
│   ├── __init__.py
│   ├── cli/                   ← typer-based CLI
│   ├── ingester/              ← stage 1a: structure ingestion
│   ├── indexer/               ← stage 1b: source indexing
│   ├── enricher/              ← stage 2a: claim-to-passage binding
│   ├── shadow/                ← stage 2b: shadow mapper
│   ├── differ/                ← stage 3: comparison
│   ├── renderer/              ← stages 5+6: assembler + cluster renderer
│   ├── auditor/               ← stage 7: post-render audit
│   ├── editor/                ← stages 9+ : edit proposer + applier
│   ├── voice/                 ← voice file parser
│   ├── graph/                 ← graph data model + persistence
│   ├── tui/                   ← Rich-based terminal UI
│   └── utils/                 ← shared utilities
├── tests/                     ← pytest
├── examples/
│   ├── voices/                ← canonical voice files
│   └── projects/              ← worked example projects
├── pyproject.toml
└── .env.example
```

## Getting started as the implementer

1. Read `docs/SPEC.md` end to end. Don't skip Part 8 (Voice).
2. Read `docs/HANDOFF.md` for the build order and explicit next-action list.
3. Read `examples/voices/academic.voice.md` to see the canonical voice file format.
4. Read `examples/projects/ict_forecasting/structure/outline.md` to see the worked example.
5. Set up the dev environment (`uv venv`, `uv pip install -e .`, `cp .env.example .env`, add API key).
6. Start at `docs/HANDOFF.md` step 1.

## Status

Specification complete. Implementation not yet started. Empty `src/` directories with `__init__.py` and stub files indicate the intended module structure but contain no working code.
