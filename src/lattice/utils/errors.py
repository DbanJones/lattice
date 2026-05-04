"""Actionable error infrastructure.

Lattice has historically raised ``typer.Exit`` with a string code or a
generic exception with an opaque message. That makes failures hard to
recover from: the user sees ``outline_has_no_structure`` and has no
idea what to do next.

``LatticeError`` carries a structured triplet:

- ``code`` — short machine-readable identifier (``no_outline``,
  ``crossref_unauthenticated``, ``cluster_blocked_readiness``).
- ``message`` — one-sentence diagnosis.
- ``next_step`` — what the user should do, in active voice. May be a
  CLI command, a doc reference, or a brief instruction.

The web UI surfaces all three; the CLI prints ``message`` then
``→ next_step``; the JSON activity events include all three so the
frontend can render contextually.

Usage in CLI handlers:

    raise LatticeError(
        code="no_outline",
        message="No outline file found in structure/.",
        next_step="Drop your outline.md into structure/, or run "
                  "`lattice citations import` to bring in an existing "
                  "draft.",
    )

Catch sites convert to ``typer.Exit`` with the right exit code; web
sites serialise to the activity event payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ExitCode = Literal[0, 1, 2, 3, 4]
# Mirrors docs/CLI.md exit codes:
#   1 — stage failed (recoverable)
#   2 — configuration error
#   3 — validation error
#   4 — API error not recoverable by retry


@dataclass
class LatticeError(Exception):
    """An academic-facing error with diagnosis + next step.

    The message is what's wrong; ``next_step`` is what to do about it.
    Both are required — half the value is forcing every call site to
    name a next step rather than dropping the user at a black wall.
    """

    code: str
    message: str
    next_step: str
    exit_code: ExitCode = 3
    docs_link: str = ""           # optional link into docs/CLI.md or similar
    context: dict = field(default_factory=dict)  # extra structured data for the web UI

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict:
        """Serialise for the web UI's activity-event JSON payload."""
        return {
            "type": "lattice_error",
            "code": self.code,
            "message": self.message,
            "next_step": self.next_step,
            "exit_code": self.exit_code,
            "docs_link": self.docs_link or None,
            "context": self.context or None,
        }


# ─── catalogue of common errors ──────────────────────


# Each entry is a factory function so call sites can fill in context.

def err_no_outline(structure_dir: str) -> LatticeError:
    return LatticeError(
        code="no_outline",
        message=f"No outline file found in {structure_dir}.",
        next_step=(
            "Either drop your outline.md into the structure/ directory, "
            "or run `lattice citations import <docx-or-md>` to convert "
            "an existing draft."
        ),
        exit_code=3,
        docs_link="docs/WORKFLOWS.md#bring-an-existing-draft-into-the-tool",
        context={"structure_dir": structure_dir},
    )


def err_outline_no_structure(outline_path: str) -> LatticeError:
    return LatticeError(
        code="outline_has_no_structure",
        message=(
            f"Outline at {outline_path} parsed but contains no `# THESIS` "
            "or `# A.` headings."
        ),
        next_step=(
            "If the file is raw prose, run the Scaffold activity (web UI) "
            "or `lattice annotate <project>` to auto-structure it via Claude."
        ),
        exit_code=3,
        docs_link="docs/WORKFLOWS.md#bring-an-existing-draft-into-the-tool",
        context={"outline_path": outline_path},
    )


def err_no_sources(project_path: str) -> LatticeError:
    return LatticeError(
        code="no_sources",
        message=f"No sources in the source store at {project_path}.",
        next_step=(
            "Index your reference PDFs with `lattice index <project>`, OR "
            "import an existing reference manager export with `lattice "
            "references import <zotero.json|bib|ris>`."
        ),
        exit_code=3,
        docs_link="docs/WORKFLOWS.md#write-a-new-chapter-from-sources",
    )


def err_no_graph(project_path: str) -> LatticeError:
    return LatticeError(
        code="no_graph",
        message=f"No author graph at {project_path}/.lattice/author_graph.json.",
        next_step="Run `lattice ingest <project>` to parse your outline.",
        exit_code=3,
    )


def err_unknown_voice(voice_name: str, voice_path: str) -> LatticeError:
    return LatticeError(
        code="unknown_voice",
        message=f"Voice file not found: {voice_path}.",
        next_step=(
            f"Check `lattice voices list <project>` for available voices, "
            f"or copy `examples/voices/{voice_name}.voice.md` into your "
            "project's voices/ directory."
        ),
        exit_code=3,
        docs_link="docs/VOICE_FORMAT.md",
        context={"voice_name": voice_name, "voice_path": voice_path},
    )


def err_no_rendered_paper(project_path: str, voice_name: str) -> LatticeError:
    return LatticeError(
        code="no_rendered_paper",
        message=(
            f"No rendered paper at {project_path}/outputs/paper.{voice_name}.md."
        ),
        next_step=f"Run `lattice render <project> --voice {voice_name}` first.",
        exit_code=3,
    )


def err_no_document_citations(project_path: str) -> LatticeError:
    return LatticeError(
        code="no_document_citations",
        message=(
            f"No document_citations.json at {project_path}/.lattice/. "
            "The citation pipeline needs a scan first."
        ),
        next_step="Run `lattice citations scan <project>`.",
        exit_code=3,
        docs_link="docs/WORKFLOWS.md#switch-a-paper-between-journal-styles",
    )


def err_unknown_style(style: str, supported: list[str]) -> LatticeError:
    return LatticeError(
        code="unknown_style",
        message=f"Unknown citation style {style!r}.",
        next_step=f"Use one of: {', '.join(supported)}.",
        exit_code=2,
        context={"requested": style, "supported": supported},
    )


def err_unknown_journal(journal: str, available: list[str]) -> LatticeError:
    return LatticeError(
        code="unknown_journal",
        message=f"Journal style {journal!r} not found in voices/journals/.",
        next_step=(
            f"Run `lattice citations journals list <project>` to see "
            f"available styles, or `lattice citations journals install` "
            f"to drop the starter library."
        ),
        exit_code=3,
        context={"requested": journal, "available": available},
    )


def err_claude_unavailable() -> LatticeError:
    return LatticeError(
        code="claude_unavailable",
        message="Claude Code CLI not on PATH.",
        next_step=(
            "Install Claude Code so `claude` is on PATH, or set "
            "LATTICE_CLAUDE_CMD to the binary path."
        ),
        exit_code=2,
        docs_link="https://claude.ai/code",
    )


def err_project_not_found(path: str) -> LatticeError:
    return LatticeError(
        code="project_not_found",
        message=f"Project directory not found: {path}.",
        next_step=(
            "Check the path. To create a new project, run `lattice init "
            "<project>`."
        ),
        exit_code=3,
    )
