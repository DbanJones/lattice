"""Tests for the Typer CLI (M1: init, status, index)."""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from lattice.cli.main import app

runner = CliRunner()


def test_init_scaffolds_expected_folders(tmp_path: Path) -> None:
    project = tmp_path / "paper"
    result = runner.invoke(app, ["init", str(project)])
    assert result.exit_code == 0, result.output
    for expected in [
        "structure",
        "refs/papers",
        "refs/notes",
        "refs/data",
        "refs/prior_writing",
        "refs/web",
        "voices",
        "figures",
        ".lattice",
    ]:
        assert (project / expected).is_dir(), f"missing {expected}"
    assert (project / "config.yml").exists()
    assert (project / ".gitignore").exists()
    assert (project / "structure" / "outline.md").exists()
    assert (project / "voices" / "academic.voice.md").exists()


def test_init_refuses_to_overwrite_nonempty_dir(tmp_path: Path) -> None:
    project = tmp_path / "paper"
    project.mkdir()
    (project / "stuff.txt").write_text("keep me", encoding="utf-8")
    result = runner.invoke(app, ["init", str(project)])
    assert result.exit_code != 0


def test_status_on_empty_project(tmp_path: Path) -> None:
    project = tmp_path / "paper"
    runner.invoke(app, ["init", str(project)])
    result = runner.invoke(app, ["status", str(project)])
    assert result.exit_code == 0, result.output
    assert "Indexed sources" in result.output
    assert "Claims" in result.output


def test_index_picks_up_markdown_notes(tmp_path: Path) -> None:
    project = tmp_path / "paper"
    runner.invoke(app, ["init", str(project)])
    (project / "refs" / "notes" / "observation.md").write_text(
        "# Observation\n\nA claim I want to track.\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["index", str(project)])
    assert result.exit_code == 0, result.output
    assert "Indexed 1 source" in result.output


def test_index_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "paper"
    runner.invoke(app, ["init", str(project)])
    (project / "refs" / "notes" / "a.md").write_text(
        "# A\n\nFirst.\n", encoding="utf-8"
    )
    first = runner.invoke(app, ["index", str(project)])
    assert "Indexed 1 source" in first.output
    second = runner.invoke(app, ["index", str(project)])
    assert "Indexed 0 source" in second.output
    assert "skipped 1" in second.output
