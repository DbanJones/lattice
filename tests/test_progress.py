"""Tests for the progress display and the NullProgress no-op."""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from lattice.cli.progress import (
    NullProgress,
    ProgressTracker,
    _format_duration,
    progress_or_null,
)


# ─── NullProgress: shape contract ───────────────────


def test_null_progress_supports_full_callback_protocol() -> None:
    """NullProgress should silently accept every callback method without
    raising, matching the ProgressTracker public surface."""
    np = NullProgress()
    np.begin("render", total=4, status="x")
    np.advance("render", n=1, status="x")
    np.update_status("render", "x")
    np.end("render", status="x")
    np.begin_pass(1, 3)
    # Calling end on a phase that was never begun should not raise.
    np.end("never_started")


def test_progress_or_null_yields_null_when_disabled() -> None:
    console = Console(file=io.StringIO())
    with progress_or_null(console, enabled=False, total_passes=3) as p:
        assert isinstance(p, NullProgress)


# ─── ProgressTracker: API tolerance ─────────────────


def test_progress_tracker_handles_unknown_phase_advance_and_end() -> None:
    """Calling advance/end before begin should be harmless — keeps the
    callback contract robust against ordering bugs in callers."""
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    with ProgressTracker(console, total_passes=1) as tracker:
        tracker.advance("never_began")  # no-op, no raise
        tracker.update_status("never_began", "x")  # no-op
        tracker.end("never_began", status="x")  # no-op


def test_progress_tracker_begin_advance_end_cycle() -> None:
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    with ProgressTracker(console, total_passes=1) as tracker:
        tracker.begin("render", total=3, status="starting")
        tracker.advance("render", status="chunk 1/3")
        tracker.advance("render", status="chunk 2/3")
        tracker.advance("render", status="chunk 3/3")
        tracker.end("render", status="done")
        assert "render" in tracker._phases
        # Ended phase should have a finished_at timestamp.
        assert tracker._phases["render"].finished_at is not None


def test_progress_tracker_pass_header_resets_phases() -> None:
    """begin_pass clears the per-pass task table so the new pass renders fresh."""
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    with ProgressTracker(console, total_passes=3) as tracker:
        tracker.begin_pass(1, 3)
        tracker.begin("render", total=2)
        tracker.advance("render")
        tracker.end("render")
        assert "render" in tracker._phases

        tracker.begin_pass(2, 3)
        # Phase table reset.
        assert tracker._phases == {}


# ─── duration formatter ─────────────────────────────


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"),
    (1, "1s"),
    (59, "59s"),
    (60, "1m 00s"),
    (61, "1m 01s"),
    (3599, "59m 59s"),
    (3600, "1h 00m 00s"),
    (3661, "1h 01m 01s"),
])
def test_format_duration(seconds: float, expected: str) -> None:
    assert _format_duration(seconds) == expected


def test_format_duration_clamps_negative() -> None:
    assert _format_duration(-5) == "0s"


# ─── re-begin same phase resets it ─────────────────


def test_re_begin_phase_resets_in_place() -> None:
    """Calling begin on an existing phase should restart it without
    creating a duplicate task — used when the convergence loop reuses
    phase names across passes."""
    console = Console(file=io.StringIO(), force_terminal=False, no_color=True)
    with ProgressTracker(console, total_passes=2) as tracker:
        tracker.begin("audit", total=21)
        tracker.advance("audit", n=10)
        first_task_id = tracker._phases["audit"].task_id

        tracker.begin("audit", total=15, status="pass 2")
        # Same task_id reused.
        assert tracker._phases["audit"].task_id == first_task_id
