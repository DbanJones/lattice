# Lattice

Argument-first long-form writing tool. Turns an author-built argument structure plus a folder of references plus a voice specification into polished long-form prose, with a web UI for an activity-driven workflow.

> **Status:** working end-to-end. CLI, FastAPI web UI, and a cytoscape-based interactive graph are all live. The original [`docs/SPEC.md`](docs/SPEC.md) is still the canonical design doc; for what's been added or changed since, see [Implementation deltas](#implementation-deltas) at the bottom of this README.

## What it does

You give Lattice an outline plus (optionally) a corpus of source PDFs and a voice specification. It builds an explicit graph of claims and relationships, generates prose from that graph one cluster at a time, then runs a series of activities to audit the result against academic-writing norms and surface gaps.

```
outline + references + voice
    │
    ▼
ingest → graph (sections / claims / relationships, up to 3 levels deep)
    │
    ▼
plan → cluster_plan (renderable units of 2-5 claims)
    │
    ▼
draft → outputs/paper.<voice>.md (chunked, voice-aware, parallelisable)
    │
    ├── refine     → audit + autofix loop + voice review
    ├── find_gaps  → per-section literature gaps via Claude + OpenAlex verification
    ├── restructure → academic-writing-rules audit of section + cluster ordering
    └── review     → supervisor-style critique with track-changes
```

## The activity model

The web UI is organised around seven distinct activities (six core authoring activities plus a deterministic re-parse). Each is a separate code path with a focused input and output; you can run them individually, chain them via Full Review, or call them from the CLI.

| Activity | Inputs | Output | Notes |
|---|---|---|---|
| **Ingest** | `outline.md` | `author_graph.json` + `cluster_plan.json` | Deterministic re-parse, no LLM. ~1 second. Use after editing the outline. |
| **Scaffold** | `outline.md` (raw or structured) | Same as ingest, plus relationship inference (Thorough) and reference extraction | Auto-heals raw prose into lattice format via Claude. Section depth picker (1/2/3) controls how deep `## A.1` / `### A.1.1` headings can go. |
| **Draft** | `cluster_plan.json` | `outputs/paper.<voice>.md` | Chunked render, parallel by default. Auto-recovers failed clusters with smaller chunks. |
| **Find gaps** | rendered paper + scaffold | `outputs/lit_gaps.<voice>.json` | Per-section: Claude suggests canonical works, counter-arguments, and recent literature; OpenAlex verifies (Thorough) so phantom citations get caught. |
| **Refine** | rendered paper + audit flags | updated paper + voice review | Audit + (Thorough) autofix convergence loop. Stops when no flag categories change. |
| **Restructure** | scaffold (graph + cluster plan) | `outputs/restructure.<voice>.json` | Advisory only. Audits section + cluster ordering against academic-writing rules; lists specific operations (move/swap/merge/split) with confidence levels. Never mutates the graph. |
| **Review** | rendered paper + scaffold | `outputs/review.<voice>.md` + `review_track_changes.<voice>.md` | Supervisor-style: per-cluster revision (with word-level `<del>`/`<ins>` diffs), per-section critique, and an overall assessment. |

There's also a **Compare** flow on the projects list page: pick two projects, run structural + LLM-semantic claim pairing across their graphs.

## State machine

A project is in one of five states; activity availability is gated by state.

| State | Marker | Means |
|---|---|---|
| **S0** Empty | folder + voice file only | Nothing parsed yet |
| **S1** Raw | `outline.md` exists but no headers | Prose dropped in, not structured |
| **S2** Scaffolded | `.lattice/author_graph.json` + `.lattice/cluster_plan.json` | Sections + claims + clusters known |
| **S3** Drafted | `outputs/paper.<voice>.md` exists | Prose rendered |
| **S4** Reviewed | `.lattice/audit/*.json` has flags | Audit has run |

Locked activities show their unlock condition rather than disappearing — you can see what's available and why.

## Web UI

```
lattice serve   # default: http://127.0.0.1:5173
```

Four tabs per project:

- **Dashboard** — at-a-glance status + change log
- **Activities** — Start (the activity cards) · Running (live timeline) · History
- **Sources** — Outline (tree view, supports nesting) · Graph (interactive cytoscape) · Source files · References
- **Output** — Audit flags · Voice review · Lit gaps · Restructure · Review · Change log

The header has a **Full Review →** split button that runs your selected sequence of activities in order, and a `↻ Re-ingest now` quick-action in its dropdown for the deterministic case.

## Repository layout

```
lattice/
├── README.md                  ← you are here
├── docs/
│   ├── SPEC.md                ← canonical design spec
│   ├── DATA_MODEL.md          ← JSON schemas for every entity
│   ├── PROMPTS.md             ← LLM prompts (mostly current; some have been refined since)
│   ├── VOICE_FORMAT.md        ← voice file format reference
│   ├── CLI.md                 ← command-line reference
│   └── HANDOFF.md             ← original build order — implementation has now moved past this
├── src/lattice/
│   ├── __init__.py
│   ├── cli/                   ← typer CLI
│   ├── ingester/              ← outline parser + auto-outliner (Claude)
│   ├── indexer/               ← source-file indexing (PDF/DOCX/MD/HTML/XLSX)
│   ├── enricher/              ← claim-to-passage binding + relationship inference
│   ├── shadow/                ← shadow mapper (corpus → blind graph)
│   ├── differ/                ← author graph vs shadow graph diff
│   ├── renderer/              ← assembler (clusters) + chunked renderer
│   ├── auditor/               ← post-render flags + autofix
│   ├── editor/                ← edit proposer + applier
│   ├── compare/               ← cross-project comparison (structural + LLM semantic)
│   ├── lit_gaps/              ← per-section literature-gap analysis + OpenAlex verification
│   ├── restructure/           ← advisory document-restructure analysis
│   ├── review/                ← supervisor-style review with track changes
│   ├── voice/                 ← voice file parser
│   ├── graph/                 ← graph data model + persistence
│   ├── output/                ← paper finalisation, DOCX export, cytoscape graph viz
│   ├── tui/                   ← Rich terminal UI
│   ├── web/                   ← FastAPI app + activity dispatcher + static frontend
│   └── utils/                 ← shared utilities (config, llm, resume)
├── tests/                     ← pytest
├── examples/
│   ├── voices/                ← canonical voice files (academic, journalistic, policy)
│   └── projects/              ← worked example: ict_forecasting
├── pyproject.toml
└── .env.example
```

## Getting started

1. Set up: `uv venv && uv pip install -e .`
2. `cp .env.example .env` and add `ANTHROPIC_API_KEY`
3. Make sure the Claude Code CLI is on `PATH` (Lattice shells out to it)
4. `lattice serve` and open http://127.0.0.1:5173
5. Click **+ New project** and paste an outline (or a raw paper text — the auto-outliner will structure it for you)
6. Click the **Scaffold** activity card → Thorough → Start

The minimum useful end-to-end target: a project with a markdown outline + ~3 PDFs + the academic voice file → coherent rendered output. Get to that before worrying about the more advanced activities.

## Key design choices

1. **Claims are atomic, not paragraphs.** The graph is a set of claims plus relationships; prose is generated from the graph, not stored in it.
2. **The renderer's unit is a cluster, not a section.** Clusters of 2-4 claims render to one or two paragraphs. Sections are scoping context. This is how long-form (10k+ words) stays high-quality.
3. **Voice is structured config, not a style guide.** YAML frontmatter + markdown notes; the parser extracts structured fields.
4. **Two edit modes.** *Rewrite* regenerates the cluster from the graph; *suggest-changes* proposes surgical edits. The author chooses per flag.
5. **The shadow mapper is blind to the author graph.** It builds a graph from the corpus alone; the differ compares them. The author's structure is never silently overwritten.
6. **Critique is advisory.** The auditor never edits prose; it produces flags. Restructure never moves sections; it lists suggestions. Review never replaces the paper; it produces a track-changes copy alongside.
7. **State gates the surface.** Locked activities show their unlock condition, not nothing. The whole map is always visible.
8. **Section depth is configurable up to 3 levels.** The ingester parses `# A.`, `## A.1`, `### A.1.1`; the auto-outliner takes a `max_depth` parameter to cap how nested it'll go.

## Implementation deltas

The original [`docs/SPEC.md`](docs/SPEC.md) describes a CLI-only tool with a Rich-based TUI. The current implementation includes everything in the spec plus the following additions:

- **Web UI** ([`src/lattice/web/`](src/lattice/web/)) — FastAPI app, WebSocket-streamed activity progress, cytoscape interactive graph with section-filter click-to-highlight, six-tab project layout
- **Activity model** ([`src/lattice/web/activities.py`](src/lattice/web/activities.py)) — replaces the old level-based `quick/standard/deep` runs with verb-oriented entry points (ingest, scaffold, draft, find_gaps, refine, restructure, review). Each is independently runnable and re-runnable
- **Nested sections (up to 3 levels)** — `Section.parent` is now populated by the markdown ingester; cluster IDs and claim IDs disambiguate via underscore-joined paths so top-level IDs stay backward-compatible
- **Lit gaps** ([`src/lattice/lit_gaps/`](src/lattice/lit_gaps/)) — replaces the original "source-gap-vs-reference-doc" check. Uses Claude per-section with OpenAlex verification (Thorough) to surface canonical works the paper isn't engaging with
- **Restructure** ([`src/lattice/restructure/`](src/lattice/restructure/)) — advisory analysis of section + cluster ordering against academic-writing rules; never mutates the graph
- **Review** ([`src/lattice/review/`](src/lattice/review/)) — supervisor-style critique pipeline producing per-cluster track-changes (word-level diffs via `difflib`), per-section critiques, and an overall assessment
- **Compare** ([`src/lattice/compare/`](src/lattice/compare/)) — cross-project analysis: structural summary plus LLM-driven thesis comparison and claim-pairing across two `author_graph.json` files
- **Per-section parallel relationship inference** — the original single-call inference capped at "10-30 relationships" was rewritten to chunk by section so each one gets full output budget. A 51-section paper now gets 51 parallel calls, producing ~1-3 connections per claim instead of <0.3
- **Auto-outliner improvements** — bumped truncation from 24k → 100k chars, removed the "4-7 sections" hard cap, added subsection support, added a `max_depth` parameter the UI exposes as a Section depth picker on Scaffold
- **Argument metrics** ([`src/lattice/graph/metrics.py`](src/lattice/graph/metrics.py)) — strength + breadth scores computed at ingest time, persisted in `.lattice/scaffold_report.json`. Strength has five sub-scores (direct support, reachable support, evidence backing, counter handling, depth); breadth has six (section diversity, source diversity, claim type diversity, relationship type diversity, mechanism coverage, section spread). Each emits human-readable observations alongside the number so the UI can show *why* a score is what it is.
- **Per-claim claim_size** ([`src/lattice/graph/claim_size.py`](src/lattice/graph/claim_size.py)) — combines importance (40%), evidence count, mechanism, scope specificity, and relationship density into a 0–1 weight used to drive cluster boundaries, skim-target placement, and offcut decisions.
- **Rescaffold planner** ([`src/lattice/restructure/rescaffold_planner.py`](src/lattice/restructure/rescaffold_planner.py)) — metrics-driven structural advisor. For each weak metric sub-score, generates structural operations (split section, add stub, reorder, move-to-offcuts) and claim-level advisories (bind evidence, add mechanism, diversify sources, address counters). Predicts metric deltas by applying operations to an in-memory copy and re-running the metric pass. Pure analysis — never mutates the graph. CLI: `lattice rescaffold`. Output: `.lattice/rescaffold_plan.json` + `outputs/rescaffold_plan.<voice>.md`.
- **Focused walkthrough commands** — `lattice fill-mechanisms` walks empirical/methodological claims missing a `[mechanism: ...]` tag; `lattice fill-evidence` walks weakly-grounded claims with four binding actions (`[ref:]`, source-hint, unbound, convert-to-synthesis). Both edit `outline.md` in place with snapshots, idempotent, decisions logged. The planner's per-real-paper findings showed the dominant work on healthy scaffolds is at the *advisory* level, not the structural level — these commands optimise for that case directly.
- **Citation management pipeline** ([`src/lattice/references/`](src/lattice/references/)) — full reference-manager workflow built for academics who switch citation formats between submissions: `scanner` (extracts every inline / footnote / bibliography citation, detects the system in use), `matcher` (links citations to Sources, resolves Ibid./op. cit.), `verifier` (Crossref + OpenAlex parallel lookup, per-field discrepancy detection, content-hash caching), `filler` (interactive accept/reject for each disagreeing field), `rewriter` (restyle the whole document in any target style — deterministic, no LLM, instant), and `journal_styles` (per-journal overrides via `voices/journals/*.yml`, with a starter library covering Nature, Science, IEEE Transactions, BJPS, Energy Policy). CLI: `lattice citations scan|verify|fill|restyle|report|journals`.

Original docs in `docs/` describe the design intent; for ground truth on what runs today, read the code and this README.
