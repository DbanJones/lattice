# Implementation Plan

Sequenced by build order. Each phase lists scope, target files (paths verified
against the current tree), and acceptance criteria. Phases are designed so that
later work depends on the foundations laid by earlier work — ship in order
unless explicitly noted.

Canonical specs: [lattice/README.md](../README.md) and
[lattice/docs/SPEC.md](SPEC.md). Root-level docs (`SPEC_v2.md`, `HANDOFF.md`,
`DATA_MODEL.md`, `PROMPTS.md`, `CLI.md`, `VOICE_FORMAT.md`) are archive-only
once Phase 1 lands.

## Status

| Phase | Status | Tests |
|---|---|---|
| 1 — Product vocabulary cleanup | ✅ Shipped | covered by web tests |
| 2 — Graph visualisation bug fixes | ✅ Shipped | `test_web.py` (3 new) |
| 3 — Revision Cockpit (skeleton) | ✅ Shipped | `test_web.py` (6 new) |
| 4 — Evidence retrieval and trace model | ✅ Shipped | `test_phase4_traces.py` (9) |
| 5 — Rewrite/review proposal structure | ✅ Shipped | `test_phase5_proposals.py` (17) |
| 6 — Advanced visual map modes | ✅ Shipped | `test_phase6_map_modes.py` (9) + `test_phase6_followups.py` (6) |
| 7 — Provenance and versioning | ✅ Shipped | `test_phase7_snapshots.py` (12) |
| 8 — Evaluation suite + Windows test hardening | ⏳ Not started | — |

**Foundation status:** all seven foundational phases shipped. 906 tests pass.
Phase 8 remains as the last item in the original plan; tracked outside this
document.

See each phase below for what specifically landed and the limitations
explicitly deferred.

---

## Phase 1 — Product vocabulary cleanup

**Goal:** make the tool feel like one coherent academic workflow with one
shared activity vocabulary.

**Scope:**

- Strip "Quick / Standard / Deep review" copy from the frontend. Replace with
  the activity model already in `activities.py`: Ingest, Scaffold, Draft, Find
  gaps, Refine, Restructure, Review.
- Add a single shared frontend state model that maps `project_state()` output
  to the next recommended activity.
- Update empty states, primary CTAs, history labels, and progress copy to use
  the activity vocabulary.
- Mark root-level docs as archive-only; add a one-line banner pointing readers
  to `lattice/README.md` and `lattice/docs/SPEC.md`.

**Files:**

- [lattice/src/lattice/web/static/app.js](../src/lattice/web/static/app.js) —
  strip review-depth strings (around line 1352 and other hits); introduce a
  `nextActivity(projectState)` helper; rewrite empty-state copy.
- [lattice/src/lattice/web/activities.py](../src/lattice/web/activities.py) —
  ensure `project_state()` (line 77) returns a `next_activity` field the
  frontend can consume directly.
- [lattice/README.md](../README.md) — confirm activity vocabulary at line 29
  is canonical; cross-reference from root docs.

**Acceptance:**

- No "Quick / Standard / Deep" strings remain in the frontend. Grep is clean.
- Every project screen states the current state and the recommended next
  activity, sourced from one place.

**✅ Shipped.** `project_state()` now returns `next_activity`. The dashboard
status strip, action items, and empty states all consume it. The legacy
Review-tab Run sub-view (the quick/standard/deep form) was removed; the
`runner.py` module is preserved as legacy. Root-level `SPEC_v2.md`,
`HANDOFF.md`, `CLI.md`, `DATA_MODEL.md`, `PROMPTS.md`, `VOICE_FORMAT.md`
carry "Archive only — do not edit" banners pointing at `lattice/docs/`.

---

## Phase 2 — Graph visualisation bug fixes

**Goal:** make the graph view trustworthy before adding new map modes.

**Scope:**

- Fix voice-specific draft marker lookup so visualisation reads
  `.lattice/drafts/<voice>/cluster_*.md` rather than the flat
  `.lattice/drafts` path.
- Vendor Cytoscape locally; remove the unpkg dependency.
- Make missing-claim and unrenderable markers correct in the graph view.
- Surface relationship notes prominently in the edge detail panel.

**Files:**

- [lattice/src/lattice/web/app.py](../src/lattice/web/app.py) — graph endpoint
  around line 1499; pass voice into the marker lookup.
- [lattice/src/lattice/output/visualise.py](../src/lattice/output/visualise.py)
  — marker assembly around line 301; fix path joining for voice subdirectory.
- [lattice/src/lattice/renderer/cluster_renderer.py](../src/lattice/renderer/cluster_renderer.py)
  — confirm draft filename layout around line 394 matches what the visualiser
  expects; if they have drifted, normalise via a single helper.
- `lattice/src/lattice/web/static/` — vendor Cytoscape under
  `static/vendor/cytoscape/` and update `index.html` to reference it.

**Acceptance:**

- Missing-claim and unrenderable markers display correctly in graph view.
- App runs offline (no unpkg fetch).
- Relationship type and notes appear in the edge inspector.

**✅ Shipped.** `/api/projects/{name}/graph-viz` accepts `?voice=` and resolves
the matching drafts subdirectory; the cache invalidation list now includes
the per-voice cluster files so re-renders propagate. `cytoscape.min.js` is
vendored at `src/lattice/web/static/vendor/cytoscape/`. The edge inspector
puts `note` and `strength` first, with claim labels (not bare IDs).

---

## Phase 3 — Revision Cockpit (skeleton)

**Goal:** replace scattered review/restructure/lit-gap/audit panels with one
working surface.

**Scope:**

- Build a four-pane cockpit: paper preview, argument map, issues/actions,
  source/evidence panel.
- Wire selection sync: clicking a claim opens its text, section, rendered
  paragraph, source bindings, audit flags, and available actions. Clicking
  an audit flag selects its claim and paragraph.
- Action buttons (no-op stubs are acceptable in this phase, but routes must
  exist): add source, edit claim, split claim, merge claim, redraft cluster,
  mark intentional.
- Convert review, restructure, lit-gap, and audit outputs into actionable
  queues consumed by the cockpit, not standalone reports.

**Files:**

- [lattice/src/lattice/web/static/app.js](../src/lattice/web/static/app.js) —
  cockpit shell, selection state, queue rendering.
- [lattice/src/lattice/web/static/index.html](../src/lattice/web/static/index.html)
  and [app.css](../src/lattice/web/static/app.css) — four-pane layout.
- [lattice/src/lattice/web/app.py](../src/lattice/web/app.py) — endpoints that
  return queues (issues, suggestions, gaps) shaped for the cockpit.

**Acceptance:**

- A user can move from diagnosis to action without switching tabs.
- All four queues feed the same selection model.
- Review/restructure/lit-gap/audit panels are removed or hidden behind the
  cockpit.

**✅ Shipped.** Cockpit is the default Output sub-tab. Three new endpoints:
`cockpit-queue`, `cockpit-claim/{id}`, `cockpit/actions/{action}`. Selection
state on `state.cockpit` drives all four panes; queue items, paragraphs, and
graph node taps all sync. Voice-aware (a project with multiple rendered
voices gets a voice picker). Phase 6 added the iframe ↔ cockpit
`postMessage` bridge for bidirectional graph selection.

---

## Phase 4 — Evidence retrieval and trace model

**Goal:** improve academic reliability before improving prose polish.

**Scope:**

- Replace first-40-passage source binding with retrieval-ranked passage
  selection (BM25 or embedding-based; pick the simpler one that works).
- Persist source passage spans and confidence on every evidence binding.
- Add rendered-prose traces: `paragraph -> sentence -> claim_ids ->
  source_ids -> evidence_spans`.
- Use those traces for coverage, visualisation, audit, and rewrite safety.

**Files:**

- [lattice/src/lattice/enricher/binder.py](../src/lattice/enricher/binder.py)
  — replace the head-of-document scan around line 128 with ranked retrieval.
- [lattice/src/lattice/auditor/coverage.py](../src/lattice/auditor/coverage.py)
  — consume the new trace model from line 14 onward.
- [lattice/src/lattice/graph/models.py](../src/lattice/graph/models.py) — add
  `EvidenceSpan` (passage id, char range, confidence) and a paragraph→claim
  trace structure starting at line 1.
- [lattice/src/lattice/output/visualise.py](../src/lattice/output/visualise.py)
  — paper-to-map linking once traces exist (claim node ↔ paragraph
  highlight).

**Acceptance:**

- Evidence binding picks the most relevant passages for a claim, not just
  early-document ones.
- Every rendered analytical claim is traceable to graph claims and source
  evidence spans.
- Coverage and audit run against the trace model, not heuristic guesses.

**✅ Shipped.** BM25 ranking in `binder.py` replaces `passages[:40]`; tests
plant a relevant passage at index 50 and confirm it surfaces top-1.
`Evidence` carries optional `passage_char_start`/`_end`/`confidence`. New
`renderer/trace.py` writes `.lattice/paragraph_traces.<voice>.json`
mapping every paragraph → sentence → claim_ids → source_ids → evidence
spans; `assembler_finalise.py` regenerates traces after every successful
finalise; `auditor/coverage.py` consumes the trace when present. Audit's
broader trace consumption (beyond coverage) and visualisation's
paper↔map highlighting from traces are explicit deferred follow-ups.

---

## Phase 5 — Rewrite/review proposal structure

**Goal:** make rewriting preserve argument structure, evidence, and academic
voice; make supervisor review reviewable.

**Scope:**

- Update render prompts to distinguish source-supported, author-specified,
  and unknown mechanisms. Renderer must not invent causal links when graph
  evidence is weak.
- Make supervisor review suggestions graph-aware: each proposed edit must
  state which claim, source, or relationship it affects.
- Convert review output into structured proposals (typed objects, not free
  text) the cockpit can accept/reject.
- Add redraft modes: conservative polish, argument repair, evidence
  integration, compression, expansion.

**Files:**

- [lattice/src/lattice/renderer/chunked_renderer.py](../src/lattice/renderer/chunked_renderer.py)
  — prompt logic at line 485; mechanism discipline rules.
- [lattice/src/lattice/review/review.py](../src/lattice/review/review.py) —
  structured proposal output starting around line 260.
- [lattice/src/lattice/references/rewriter.py](../src/lattice/references/rewriter.py)
  — line 67; align rewrite paths with the proposal model.
- [lattice/src/lattice/editor/proposer.py](../src/lattice/editor/proposer.py)
  and [applier.py](../src/lattice/editor/applier.py) — accept/reject pipeline
  for structured proposals.

**Acceptance:**

- Rewrites preserve all required claims; missing claims fail the rewrite.
- Unsupported mechanisms are flagged, not smoothed into prose.
- Review output is a list of typed proposals the cockpit renders one-by-one
  with per-proposal accept/reject.

**✅ Shipped.** `classify_mechanism_support` + the `MECHANISM DISCIPLINE`
prompt block + the `coverage.unrenderable_mechanism_marker` audit rule
close the loop end-to-end. Five `RedraftMode` variants prepend mode-
specific preludes to the renderer's system prompt. `verify_claims_preserved`
ships as a pure helper. `ReviewProposal` (with `affects_claim_ids` /
`affects_source_ids` / `affects_relationship_ids`) is derived per cluster
revision; cockpit accept/reject persists to
`.lattice/proposal_decisions.<voice>.json`. Phase 7 wires
`verify_claims_preserved` into the redraft pipeline — failed clusters get
`prose_state=failed` and the user reverts via the snapshot from the
History view.

---

## Phase 6 — Advanced visual map modes

**Goal:** turn the graph from a network viewer into an academic argument map.

**Scope:**

- Map modes (toggle in the cockpit graph pane): thesis support path, section
  proof chain, weak evidence zones, counterargument map, unrenderable
  clusters.
- Reuse Phase 4 traces and Phase 5 proposal model for highlighting (e.g.,
  weak-evidence zones come from binding confidence; unrenderable clusters
  come from renderer output).
- Paper-to-map linking polish: bidirectional highlight (claim ↔ paragraph)
  driven by selection state in the cockpit.

**Files:**

- [lattice/src/lattice/output/visualise.py](../src/lattice/output/visualise.py)
  — mode-specific layout and styling.
- [lattice/src/lattice/web/static/app.js](../src/lattice/web/static/app.js) —
  mode picker; selection sync between paper preview and graph pane.

**Acceptance:**

- Each map mode answers a single question (e.g. "what supports the thesis?",
  "where is evidence thin?").
- Selection in either pane highlights the counterpart.

**✅ Shipped.** Five mode overlays in `visualise.py` (`thesis_support_path`,
`section_proof_chain`, `weak_evidence_zones`, `counterargument_map`,
`unrenderable_clusters`). Sidebar mode picker plus a cockpit-pane mode
picker. `postMessage` bridge: `lattice:set-mode` and `lattice:select-claim`
inbound, `lattice:node-tapped` outbound. The Phase-3 follow-up wired the
finaliser to emit `<!-- lattice:cluster <cid> <sid> -->` boundary markers
in the joined paper so paragraph clicks now drive selection too.

---

## Phase 7 — Provenance and versioning

**Goal:** make the tool safe enough for real academic drafting.

**Scope:**

- Snapshot the graph before every major mutation (scaffold, restructure,
  redraft, source rebind, accepted proposal).
- Record the actor (user vs activity vs review) and reason for every change
  to claims, relationships, source bindings, and drafts.
- Argument-graph changelog view that diffs graphs, not files.
- Reversible "apply change" from the cockpit.

**Files:**

- [lattice/src/lattice/graph/store.py](../src/lattice/graph/store.py) —
  snapshot/version model around line 96.
- [lattice/src/lattice/differ/diff.py](../src/lattice/differ/diff.py) — graph
  diff used by the changelog view.
- [lattice/src/lattice/web/app.py](../src/lattice/web/app.py) — endpoints for
  changelog and revert.

**Acceptance:**

- A user can inspect how the paper's argument changed over time.
- No activity silently overwrites important structure without a recoverable
  snapshot.

**✅ Shipped.** New `Snapshot` / `SnapshotKind` / `GraphDiff` / `ClaimChange`
models. `GraphStore` gained `create_snapshot`, `list_snapshots`,
`load_snapshot`, `revert_to_snapshot` (with auto pre-revert). New
`differ/graph_diff.py` with `diff_graphs(before, after)` reporting
section/claim/relationship/source/cluster deltas plus per-field claim
diffs. Five new endpoints under `/api/projects/{name}/snapshots/`.
Activity dispatcher snapshots before every non-ingest verb. Cockpit
"History" sub-view lists snapshots with Diff vs current and Revert.
**Phase 5/7 closeout (#4):** `verify_claims_preserved` is now wired to the
redraft path — when `redraft_mode` is set, clusters that drop required
claims are marked `failed`, the dispatcher emits a `redraft_claim_loss`
progress event, and the finaliser refuses to deliver. Recovery is via
the pre-activity snapshot.

Limitations explicitly deferred:
- snapshot bundles are uncompressed (retention policy + gzip = follow-up)
- auto-revert on redraft failure is left manual (the user reviews first)
- per-cluster reverts not exposed (revert is whole-project)

---

## Phase 8 — Evaluation suite and Windows test hardening

**Goal:** make academic-rewrite quality measurable; make the test suite
reliable on Windows.

**Scope:**

- Fixtures for strong, weak, and messy academic papers.
- Eval tests for: claim preservation, source grounding, citation
  preservation, mechanism discipline, paragraph coherence, thesis drift.
- Fix Windows pytest temp-dir handling so tests run cleanly outside OneDrive
  permission traps (use `tempfile.mkdtemp` outside the synced tree, or set
  `--basetemp` to `%LOCALAPPDATA%`).
- Playwright smoke tests for the cockpit and the graph view.

**Files:**

- `lattice/tests/conftest.py` — temp-dir fixture override.
- `lattice/tests/eval/` (new) — fixtures and graded tests.
- `lattice/tests/web/` (new) — Playwright suite.

**Acceptance:**

- Quality gates are measurable and gate CI.
- Visualisation and web tests pass reliably on Windows.

---

## Cross-phase notes

- **Don't merge ahead of order.** Phase 3 (cockpit) depends on Phase 1
  (vocabulary) and Phase 2 (graph correctness). Phase 5 (rewrite quality)
  depends on Phase 4 (traces). Phase 6 reuses Phase 4 + 5 outputs. Phase 7
  is easier once Phase 5 produces structured proposals.
- **Each phase ends with a working app.** Don't leave a phase half-shipped;
  the cockpit and graph view must remain usable between phases.
- **Treat the activity vocabulary as the contract.** Anything new added to
  the UI must speak in Ingest / Scaffold / Draft / Find gaps / Refine /
  Restructure / Review.
