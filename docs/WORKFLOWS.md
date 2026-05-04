# Workflows

Recipes for the patterns academics actually use. Each is a sequence of CLI commands; most have a web UI counterpart on the corresponding tab.

## Switch a paper between journal styles

The killer use case. You submitted to Nature, got rejected, want to resubmit to Energy Policy. Your bibliography is in author-date; the new venue wants Vancouver.

```sh
# 1. Pull the citations out of your rendered paper.
lattice citations scan <project> --voice academic

# 2. Verify each source against Crossref + OpenAlex.
lattice citations verify <project> --email you@institution.edu

# 3. Walk the discrepancies. Accept canonical for missing DOIs / pages;
#    decide title-case + author-list disagreements per source.
lattice citations fill <project> --severity warning

# 4. Restyle for the new venue. < 1 second.
lattice citations restyle <project> --document outputs/paper.academic.md \
    --journal energy_policy --output outputs/paper.energy_policy.md
```

After step 3, the source store is canonical. Subsequent restyles for other venues are just step 4 with a different `--journal` flag. No LLM tokens, no waiting.

If your venue isn't in the starter library:
```sh
lattice citations journals install <project>      # writes voices/journals/*.yml
# Edit voices/journals/<your-venue>.yml — base style + per-journal tweaks.
```

## Write a new chapter from sources

You have an outline plus a folder of PDFs. You want a draft.

```sh
# 1. Set up. Drop PDFs into refs/papers/, write outline.md.
lattice init <project>

# 2. Index sources (no LLM; SHA-256 caches per file).
lattice index <project>

# 3. Parse the outline into the graph.
lattice ingest <project>

# 4. Annotate (LLM): infer claim roles, classify section roles,
#    extract inline citations.
lattice annotate <project>

# 5. Bind claims to source passages.
lattice enrich <project>

# 6. Walk unbound claims and decide each (add binding, mark
#    user_synthesis, mark unbound, or skip).
lattice coverage <project>

# 7. Render.
lattice render <project> --voice academic

# 8. Audit.
lattice audit <project> --voice academic

# 9. Walk audit flags or run autofix.
lattice flags <project> --voice academic
# OR:
lattice autofix <project> --voice academic --level safe
```

Or hands-free:
```sh
lattice run <project> --voice academic
```

## Bring an existing draft into the tool

You have a half-written Word document or markdown file. You want to
keep iterating on it under lattice's structure + audit + restyle
benefits.

```sh
lattice import draft.docx [<project>] [--references zotero.json]
```

The wizard detects what the doc is:

- **Lattice-format markdown** (already has `# THESIS` / `# A.` / `## A.1`)
  → lands directly in `structure/outline.md`. Ready to ingest.
- **Structured DOCX** (Heading 1 / Heading 2 / bullet styles)
  → routed through the DOCX ingester; lands in `structure/outline.md`.
- **Raw prose** (DOCX or markdown without lattice headings)
  → archived to `structure/outline.raw.md`. Run the **Scaffold** activity
  in the web UI (or `lattice annotate <project>`) to auto-structure via
  Claude.

After import, the wizard prints a tailored next-steps list. The
typical follow-on:

```sh
lattice ingest <project>                       # parse the outline
lattice annotate <project>                     # claim roles + types via LLM
lattice render <project> --voice academic       # produce a draft
```

If you have a Zotero / BibTeX / RIS export, pass `--references <file>` to
seed the source store at import time. Otherwise drop PDFs into
`refs/papers/` and run `lattice index <project>`.

## Get a structural review of an existing draft

You finished writing. You want to know if the argument holds together
before submission.

```sh
# 1. Parse + score.
lattice ingest <project>

# 2. Rescaffold proposes structural moves (split sections, reorder,
#    add counter-engagement) based on argument-strength + breadth metrics.
lattice rescaffold <project> --voice academic
# Read outputs/rescaffold_plan.<voice>.md

# 3. For the "add a mechanism" advisories — focused walkthrough.
lattice fill-mechanisms <project> --voice academic --min-importance 0.7

# 4. For the "bind evidence" advisories.
lattice fill-evidence <project> --voice academic --supporters-only

# 5. Re-ingest to see the new metrics.
lattice ingest <project>
lattice rescaffold <project> --voice academic     # diff vs the previous run
```

## Find what you missed

Tell the tool what your thesis is; let it read the corpus blind and
produce its own argument graph; show me the differences.

```sh
lattice shadow <project>                   # builds shadow graph
lattice review <project>                   # walks the diff, accept/reject
```

## Comprehensive review before submission

```sh
lattice voice-review <project> --voice academic    # whole-document voice audit
lattice audit <project> --voice academic            # per-cluster audit flags
lattice rescaffold <project> --voice academic       # structural advisor
lattice citations report <project>                  # citation-state summary
```

The web UI's `Output` tab shows all four in panels.

## Compare two papers / two versions

```sh
lattice compare <project_a> <project_b>
```

Produces a structural + LLM-semantic comparison: shared claims, novel
claims, contradicting claims, structural differences.

## Where to look when something fails

| Symptom | Where |
|---|---|
| `outline_has_no_structure` | The auto-outliner can convert raw prose. Run Scaffold (web) or `lattice annotate` (CLI). |
| Render produces `{MISSING_CLAIM:...}` markers | A claim is unbound. Run `lattice coverage` and resolve, OR mark `[type: user_synthesis]`. |
| `cluster_blocked_readiness` | The readiness check refused delivery. Check `outputs/audit.md` for the blocking flag. |
| Citations all unmatched | Source store is empty or doesn't match. Run `lattice index` first. |
| Crossref returns no results | Check that your source has at least a title or author + year. Otherwise verify by DOI. |
| Web UI says "locked activity" | The activity needs an earlier step. The lock message tells you which one. |
