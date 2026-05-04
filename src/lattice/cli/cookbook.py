"""Workflow recipes — the choreography behind the 36 commands.

The CLI is broad. Most users only need 5-10 commands at a time
depending on what they're doing. This module exposes a curated set of
recipes (mirrored from docs/WORKFLOWS.md) so a user can run
``lattice cookbook`` and see "the actual workflow patterns" rather
than the full command list.

Each recipe has:

- name + slug
- one-line summary
- the sequence of CLI commands, each annotated
- pre-conditions (what state the project must be in)
- expected output

Recipes are data; the CLI command renders them as Rich tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Recipe:
    slug: str
    title: str
    summary: str
    when_to_use: str
    steps: list[str]
    pre_conditions: list[str] = field(default_factory=list)
    next: str = ""  # what naturally follows this recipe


_RECIPES: list[Recipe] = [
    Recipe(
        slug="restyle",
        title="Switch a paper between journal styles",
        summary=(
            "You submitted to one venue, got rejected, and want to "
            "resubmit elsewhere. Re-emit the document in the new "
            "venue's citation style."
        ),
        when_to_use=(
            "Bibliography is essentially canonical; just need a new "
            "format. The killer feature."
        ),
        pre_conditions=[
            "Project has a rendered paper (outputs/paper.<voice>.md)",
            "Source store populated (refs in store match inline citations)",
        ],
        steps=[
            "lattice citations scan <project> --voice academic",
            "lattice citations verify <project> --email you@institution.edu",
            "lattice citations fill <project> --severity warning",
            (
                "lattice citations restyle <project> "
                "--style vancouver --output outputs/paper.vancouver.md"
            ),
            (
                "# Or with per-journal overrides (Nature, Science, "
                "IEEE Transactions, etc.):"
            ),
            (
                "lattice citations restyle <project> --journal nature "
                "--output outputs/paper.nature.md"
            ),
        ],
        next=(
            "Subsequent restyles for other venues are just step 4 "
            "with a different --style or --journal flag. < 1 second."
        ),
    ),
    Recipe(
        slug="from-sources",
        title="Write a new chapter from sources",
        summary=(
            "You have an outline + a folder of reference PDFs. You "
            "want a coherent draft you can edit."
        ),
        when_to_use=(
            "Starting a new piece of writing where the structure is "
            "already in your head."
        ),
        pre_conditions=["Outline written in lattice format"],
        steps=[
            "lattice init <project>",
            "# Drop reference PDFs into refs/papers/, then:",
            "lattice index <project>",
            "lattice ingest <project>",
            "lattice annotate <project>",
            "lattice enrich <project>",
            "lattice coverage <project>",
            "lattice render <project> --voice academic",
            "lattice audit <project> --voice academic",
            "lattice flags <project> --voice academic",
            "# OR run hands-free:",
            "lattice run <project> --voice academic",
        ],
        next=(
            "Iterate: edit outline.md, re-run ingest/render. "
            "Use `lattice rescaffold` to get structural advice."
        ),
    ),
    Recipe(
        slug="import",
        title="Bring an existing draft into the tool",
        summary=(
            "You have a half-written Word doc or markdown file. You "
            "want to keep iterating under Lattice's audit + restyle "
            "benefits."
        ),
        when_to_use=(
            "Migrating from Word / Google Docs / plain markdown to "
            "Lattice mid-paper."
        ),
        pre_conditions=[],
        steps=[
            (
                "lattice import <draft.docx> [<project>] "
                "[--references zotero-export.json] [--voice academic]"
            ),
            (
                "# If the draft was structured (headings + bullets), "
                "you're ready to ingest:"
            ),
            "lattice ingest <project>",
            (
                "# If the draft was raw prose, run the Scaffold "
                "activity in the web UI to auto-structure:"
            ),
            "lattice serve",
        ],
        next=(
            "After ingest, the standard workflow applies: annotate → "
            "enrich → render → audit."
        ),
    ),
    Recipe(
        slug="rescaffold",
        title="Get a structural review of an existing draft",
        summary=(
            "You finished writing. You want to know if the argument "
            "holds together before submission."
        ),
        when_to_use=(
            "Pre-submission sanity check; or mid-paper when something "
            "feels off but you can't name what."
        ),
        pre_conditions=["Project ingested (graph exists)"],
        steps=[
            "lattice ingest <project>",
            "lattice rescaffold <project> --voice academic",
            "# Walk the dominant advisory class — missing mechanisms:",
            "lattice fill-mechanisms <project> --min-importance 0.7",
            "# And weakly-grounded supporters:",
            "lattice fill-evidence <project> --supporters-only",
            "lattice ingest <project>",
            "lattice rescaffold <project> --voice academic",
        ],
        next=(
            "Compare strength + breadth scores between rescaffold runs "
            "— see how much your fixes lifted them."
        ),
    ),
    Recipe(
        slug="shadow",
        title="Find what you missed",
        summary=(
            "Tell the tool your thesis; let it read the corpus blind "
            "and produce its own argument graph; show me the differences."
        ),
        when_to_use=(
            "You suspect the corpus has counter-evidence you've "
            "underweighted, or that you've cited the same set of "
            "sources too narrowly."
        ),
        pre_conditions=[
            "refs/papers/ populated and indexed",
            "Project ingested (thesis claim exists)",
        ],
        steps=[
            "lattice shadow <project>",
            "lattice review <project>",
        ],
        next=(
            "Each shadow flag is accept/reject in the TUI. Accepted "
            "flags update the author graph."
        ),
    ),
    Recipe(
        slug="presubmit",
        title="Comprehensive review before submission",
        summary=(
            "Run every quality check the tool has against the rendered "
            "paper. Fix what matters; submit."
        ),
        when_to_use="Final pass before sending the manuscript.",
        pre_conditions=["Rendered paper exists"],
        steps=[
            "lattice voice-review <project> --voice academic",
            "lattice audit <project> --voice academic",
            "lattice rescaffold <project> --voice academic",
            "lattice citations report <project>",
            "# Web UI shows all four in panels under the Output tab:",
            "lattice serve",
        ],
        next=(
            "After fixes, regenerate: render → finalise → DOCX export "
            "with audit flags as inline comments."
        ),
    ),
    Recipe(
        slug="references",
        title="Manage the reference store",
        summary=(
            "Bring an existing reference library into the project; or "
            "ship the canonical bibliography out to LaTeX / another "
            "manager."
        ),
        when_to_use=(
            "First time setting up; or when handing the project off."
        ),
        pre_conditions=[],
        steps=[
            "# Import:",
            (
                "lattice references import <project> "
                "<library.json|.bib|.ris> [--dry-run]"
            ),
            "lattice references list <project>",
            "# Verify against external authorities:",
            "lattice citations verify <project> --email you@institution.edu",
            "# Export for LaTeX or another manager:",
            "lattice references export <project> --format bib --output refs.bib",
            "lattice references export <project> --format csl-json --output zotero.json",
        ],
        next="",
    ),
]


def list_recipes() -> list[Recipe]:
    return list(_RECIPES)


def find_recipe(slug: str) -> Recipe | None:
    slug = slug.strip().lower()
    for r in _RECIPES:
        if r.slug == slug:
            return r
    return None
