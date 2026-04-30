"""Multi-phase Rich progress display for the render+autofix pipeline.

Shows the operator a live picture of the long-running render flow:

    Phase                            Elapsed    Counter      Status
    Rendering clusters (chunked)     0:01:24    3/4          chunk 4 of 4
    Audit                            0:00:05    21/21        complete
    Autofix (suggest_changes)        0:00:28    180 props
    Re-rendering dirty clusters      0:00:12    1/2
    Finalise                         0:00:00                 retrying
    ──────────────────────────────────────────────────────────────────
    Pass 2 of 3                      0:02:09 total elapsed

Each phase is a Rich progress task that becomes visible when its
``begin`` method is called. Phases are NOT animated time bars (LLM call
durations are hard to predict); instead they show a counter ("3/4") and
elapsed time. The bar fills only when ``total`` is set and ``advance``
is called.

The orchestrator passes a ``ProgressTracker`` into the renderer and the
autofix pipeline. Both call back to the tracker as work happens. When
no tracker is supplied, callbacks are no-ops — keeping non-CLI callers
(tests, future API) cheap.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Protocol

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table


PHASE_LABELS = {
    "render": "Rendering clusters",
    "audit": "Auditing prose",
    "autofix": "Autofix (flags → proposals → apply)",
    "rerender": "Re-rendering dirty clusters",
    "finalise": "Finalising document",
}


class _CallbackProtocol(Protocol):
    """Minimal interface every progress-aware caller can rely on. Methods
    are intentionally tolerant to no-op implementations; callers should
    assume the tracker may be absent."""

    def begin(self, phase: str, total: int | None = None, status: str = "") -> None: ...
    def advance(self, phase: str, n: int = 1, status: str = "") -> None: ...
    def update_status(self, phase: str, status: str) -> None: ...
    def end(self, phase: str, status: str = "complete") -> None: ...
    def begin_pass(self, pass_index: int, total_passes: int) -> None: ...


class NullProgress:
    """A tracker that absorbs every call and emits nothing. Used when
    the caller wants the same code path with progress disabled."""

    def begin(self, phase: str, total: int | None = None, status: str = "") -> None:
        pass

    def advance(self, phase: str, n: int = 1, status: str = "") -> None:
        pass

    def update_status(self, phase: str, status: str) -> None:
        pass

    def end(self, phase: str, status: str = "complete") -> None:
        pass

    def begin_pass(self, pass_index: int, total_passes: int) -> None:
        pass


@dataclass
class _PhaseState:
    label: str
    task_id: TaskID
    started_at: float = 0.0
    finished_at: float | None = None
    status: str = ""


class ProgressTracker:
    """Live multi-phase progress display.

    Use as a context manager:

        with ProgressTracker(console=console, total_passes=3) as tracker:
            tracker.begin("render", total=4)
            ...
            tracker.advance("render", status="chunk 1 of 4")
            ...
            tracker.end("render")
    """

    def __init__(
        self,
        console: Console,
        total_passes: int = 1,
    ) -> None:
        self.console = console
        self.total_passes = max(total_passes, 1)
        self._current_pass = 0
        self._started_at = 0.0
        self._phases: dict[str, _PhaseState] = {}
        self._progress = Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[bold]{task.description}[/bold]", justify="left"),
            BarColumn(bar_width=20),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TextColumn("[dim]{task.fields[status]}[/dim]"),
            console=console,
            transient=False,
            expand=False,
        )
        self._live: Live | None = None

    # ─── lifecycle ─────────────────────────────────

    def __enter__(self) -> "ProgressTracker":
        self._started_at = time.monotonic()
        # Render the progress group inside a panel so the pass header is
        # always visible above the task table.
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live is not None:
            # Close out any unfinished phase so it doesn't read as still
            # running in the final summary.
            for phase, state in self._phases.items():
                if state.finished_at is None:
                    state.finished_at = time.monotonic()
            self._live.update(self._render(final=True))
            self._live.__exit__(exc_type, exc, tb)

    # ─── pass header ───────────────────────────────

    def begin_pass(self, pass_index: int, total_passes: int) -> None:
        self._current_pass = pass_index
        self.total_passes = max(total_passes, self.total_passes)
        # Reset existing phases so the next pass renders fresh.
        for state in self._phases.values():
            self._progress.remove_task(state.task_id)
        self._phases.clear()
        self._refresh()

    # ─── per-phase ─────────────────────────────────

    def begin(
        self,
        phase: str,
        total: int | None = None,
        status: str = "",
    ) -> None:
        label = PHASE_LABELS.get(phase, phase)
        if phase in self._phases:
            # Restart the phase fresh.
            state = self._phases[phase]
            self._progress.update(
                state.task_id, completed=0, total=total or 0, status=status,
                description=label,
            )
            state.started_at = time.monotonic()
            state.finished_at = None
            state.status = status
        else:
            task_id = self._progress.add_task(
                description=label,
                total=total if total else None,
                status=status,
                start=True,
            )
            self._phases[phase] = _PhaseState(
                label=label,
                task_id=task_id,
                started_at=time.monotonic(),
                status=status,
            )
        self._refresh()

    def advance(self, phase: str, n: int = 1, status: str = "") -> None:
        state = self._phases.get(phase)
        if state is None:
            return
        if status:
            state.status = status
        self._progress.update(
            state.task_id,
            advance=n,
            status=status or state.status,
        )
        self._refresh()

    def update_status(self, phase: str, status: str) -> None:
        state = self._phases.get(phase)
        if state is None:
            return
        state.status = status
        self._progress.update(state.task_id, status=status)
        self._refresh()

    def end(self, phase: str, status: str = "complete") -> None:
        state = self._phases.get(phase)
        if state is None:
            return
        state.finished_at = time.monotonic()
        state.status = status
        # Mark the bar as fully completed visually if a total was set.
        task = self._progress.tasks[
            next(i for i, t in enumerate(self._progress.tasks) if t.id == state.task_id)
        ]
        if task.total:
            self._progress.update(state.task_id, completed=task.total, status=status)
        else:
            self._progress.update(state.task_id, status=status)
        self._refresh()

    # ─── rendering ─────────────────────────────────

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _render(self, final: bool = False) -> Panel:
        elapsed = _format_duration(time.monotonic() - self._started_at)
        if self.total_passes > 1:
            header = (
                f"Pass [bold cyan]{self._current_pass}[/bold cyan] of "
                f"{self.total_passes}  ·  total elapsed [yellow]{elapsed}[/yellow]"
            )
        else:
            header = f"Total elapsed [yellow]{elapsed}[/yellow]"
        if final:
            header = f"[green]Complete[/green]  ·  {header}"
        body = Group(self._progress)
        return Panel(body, title=header, title_align="left", border_style="blue")


# ─── helpers ────────────────────────────────────────


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s"


@contextmanager
def progress_or_null(
    console: Console,
    enabled: bool,
    total_passes: int = 1,
) -> Iterator[_CallbackProtocol]:
    """Yield a ProgressTracker when enabled, else a NullProgress.

    Lets call sites use the same code path regardless of whether the
    operator wanted a status display.

    The progress tracker creates its own Rich Console with
    ``safe_box=True`` so progress glyphs render reliably on legacy
    Windows consoles (cp1252) that can't decode the default Unicode
    box-drawing characters. The caller's ``console`` is still used for
    everything outside the live display.
    """
    if not enabled:
        yield NullProgress()
        return
    safe_console = Console(
        file=console.file,
        force_terminal=console.is_terminal,
        safe_box=True,
        soft_wrap=False,
    )
    with ProgressTracker(console=safe_console, total_passes=total_passes) as tracker:
        yield tracker
