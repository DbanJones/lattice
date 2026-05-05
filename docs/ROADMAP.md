# Roadmap

Prioritised by `(impact × adoption-unlock) ÷ effort`. Phases can run in
parallel; each exits when its acceptance criteria pass.

> **Status (May 2026):** the seven argument-first revision-foundation phases
> tracked in [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) (vocabulary
> cleanup, graph fixes, Revision Cockpit, evidence retrieval + traces,
> rewrite proposals, map modes, provenance + versioning) are **shipped**.
> 906 tests pass. The roadmap below is the longer-term vector on top of
> that foundation; Phase 8 of the implementation plan (evaluation suite +
> Windows test hardening) is the only foundational item still open and
> tracks separately.

## Phase 1 — Make the strongest piece visible (1–2 weeks)

The citation pipeline is the most differentiated work in the tool and is
currently CLI-only. Phase 1 exposes existing capability rather than
building new — high impact, low cost.

- **1A. Citations in the web UI** — `Citations` tab with Scan / Verify /
  Fill / Restyle panes mirroring the CLI. ~3–4 days.
- **1B. BibTeX/RIS export** — `lattice references export --format bib`.
  Unlocks LaTeX users. ~1 day.
- **1C. Reference-manager import** — `lattice references import` reads
  Zotero CSL-JSON / BibTeX / RIS. The single biggest adoption unlock —
  most academics already have a curated library elsewhere. ~2–3 days.
- **1D. Complete the example project** — add 3 actual PDFs to
  `examples/projects/ict_forecasting/refs/papers/` so a new user can
  run the example end-to-end. Half a day.
- **1E. Cheat sheet** — `docs/CHEAT_SHEET.md` (done).
- **1F. Actionable error messages** — `LatticeError` class with `code` +
  `next_step`; convert existing string codes. ~1 day.

**If you can do only one thing, do 1A.**

## Phase 2 — On-ramps (2–3 weeks)

The tool can't grow until academics can get *into* it without 30 minutes
of setup.

- **2A. Import-an-existing-paper wizard** — `lattice import <docx>` →
  pandoc → auto-outliner → reference extraction → land in S2 ready to
  edit. ~3–4 days.
- **2B. Web UI first-run experience** — three-step onboarding panel +
  tour overlays. ~2 days.
- **2C. Workflow recipes in the docs** — done in `docs/WORKFLOWS.md`.
- **2D. Reduce the CLI surface** — group 36 commands under verb
  namespaces (`scaffold`, `draft`, `review`, `citations`, `references`,
  `voices`). Keep deprecated aliases for one minor version. ~2 days.

## Phase 3 — Editorial depth (3–4 weeks)

The tool's metrics and auditor work at document scale. Real revision
happens at section scale. Bridge the gap.

- **3A. Section-level metrics** — `compute_argument_metrics` returns
  per-section scores. ✅ Shipped.
- **3B. Per-section rescaffold** — `lattice rescaffold --section <id>`
  scopes operations + advisories. ✅ Shipped.
- **3C. Trust score per section** — `lattice trust` combines metric +
  audit-flag density + readiness blocks + voice review failures into
  one 0–1 number per section. ✅ Shipped.
- **Web UI heatmap** — `Sources → Heatmap` tab renders the per-section
  metrics + trust scores as a colour-coded table with a "scope
  rescaffold to this section" action. ✅ Shipped.
- **3D. Section-level diffs** (DEFERRED) — three-way diff between
  previous render, current render, accepted edits. The cluster
  prose-file infrastructure already exists; this is mostly UI work.
  ~2 days.

## Phase 4 — STEM completeness (2–3 weeks)

Half of academic publishing is STEM.

- **4A. LaTeX equation pass-through** — equations are verbatim across
  renderer / auditor / restyle. ~2 days.
- **4B. Figures as first-class objects** — `Figure` model with caption,
  cross-references, auto-renumber on restructure. ~5 days.
- **4C. Tables** — same shape as figures. ~2 days after 4B.

## Phase 5 — Collaboration (4+ weeks)

Real academic work is collaborative. Highest cost; defer until earlier
phases are solid.

- **5A. Native DOCX track-changes round-trip** — proper Word XML
  track-changes elements; parse changed DOCX back into per-cluster
  edits. ~1–2 weeks.
- **5B. Project sharing** — start with `lattice export --bundle` (async
  collaboration via email); add git-backed sync; defer cloud sync. ~2
  days for bundles, 1 week for git, months for cloud.
- **5C. Multi-author voice support** — per-author voice overrides;
  `[author: x]` tag. ~1 week.

## What we won't build

1. **A custom prose editor.** Use Word, Docs, Obsidian, Cursor. Stay
   markdown-first.
2. **A Zotero replacement.** Integrate; don't compete.
3. **A grammar / general-style checker.** Lattice's value is academic
   rules (engagement, hourglass, mechanism coverage); keep the focus.
4. **A real-time collaborative editor.** Google Docs solves this.
5. **More auto-fixes that mutate without confirmation.** The decisions
   discipline is the strength; preserve it.
6. **More LLM-driven anything until existing LLM steps prove their
   per-token value.** Reach for the LLM only when nothing else works.

## Cross-cuts

Every phase ships with:

- Tests (current baseline: 681 passing).
- Snapshots before edits + decisions appended to the JSON log.
- Defensive parsing — drop bad rows / surface warnings rather than fail.
- Pure functions where possible; CLI-thin / library-thick separation.
- README + CLI.md updates at phase exit.
