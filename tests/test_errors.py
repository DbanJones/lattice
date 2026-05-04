"""Tests for LatticeError + actionable error catalogue."""
from __future__ import annotations

import pytest

from lattice.utils.errors import (
    LatticeError,
    err_claude_unavailable,
    err_no_document_citations,
    err_no_graph,
    err_no_outline,
    err_no_rendered_paper,
    err_no_sources,
    err_outline_no_structure,
    err_project_not_found,
    err_unknown_journal,
    err_unknown_style,
    err_unknown_voice,
)


def test_lattice_error_carries_diagnosis_and_next_step() -> None:
    err = LatticeError(
        code="x",
        message="Something broke.",
        next_step="Run y to fix it.",
    )
    assert err.code == "x"
    assert err.message == "Something broke."
    assert err.next_step == "Run y to fix it."
    assert err.exit_code == 3  # default


def test_lattice_error_str_returns_message() -> None:
    err = LatticeError(code="x", message="Bad.", next_step="Fix.")
    assert str(err) == "Bad."


def test_lattice_error_to_dict_serialises_for_web() -> None:
    err = LatticeError(
        code="x", message="Bad.", next_step="Fix.",
        exit_code=2, docs_link="docs/X.md",
        context={"foo": "bar"},
    )
    d = err.to_dict()
    assert d["type"] == "lattice_error"
    assert d["code"] == "x"
    assert d["message"] == "Bad."
    assert d["next_step"] == "Fix."
    assert d["exit_code"] == 2
    assert d["docs_link"] == "docs/X.md"
    assert d["context"] == {"foo": "bar"}


def test_lattice_error_can_be_raised() -> None:
    with pytest.raises(LatticeError) as info:
        raise LatticeError(code="x", message="Bad.", next_step="Fix.")
    assert info.value.code == "x"


# ─── catalogue factories ────────────────────────


def test_err_no_outline_has_actionable_next_step() -> None:
    err = err_no_outline("project/structure")
    assert err.code == "no_outline"
    assert "structure/" in err.message or "structure" in err.message
    # Next step must name a concrete command.
    assert "lattice" in err.next_step.lower() or "outline.md" in err.next_step.lower()


def test_every_factory_produces_message_and_next_step() -> None:
    """No factory should produce an empty next_step — the whole point
    is to force every call site to name what to do."""
    factories = [
        lambda: err_no_outline("p"),
        lambda: err_outline_no_structure("p/outline.md"),
        lambda: err_no_sources("p"),
        lambda: err_no_graph("p"),
        lambda: err_unknown_voice("academic", "p/voices/academic.voice.md"),
        lambda: err_no_rendered_paper("p", "academic"),
        lambda: err_no_document_citations("p"),
        lambda: err_unknown_style("xyz", ["a", "b"]),
        lambda: err_unknown_journal("xyz", ["a", "b"]),
        lambda: err_claude_unavailable(),
        lambda: err_project_not_found("p"),
    ]
    for factory in factories:
        err = factory()
        assert err.message, f"{err.code}: empty message"
        assert err.next_step, f"{err.code}: empty next_step"
        assert err.code, f"empty code on {err}"


def test_err_unknown_style_lists_supported_styles() -> None:
    err = err_unknown_style("made_up", ["harvard", "apa", "vancouver"])
    assert "harvard" in err.next_step
    assert "apa" in err.next_step
    assert err.context["requested"] == "made_up"


def test_err_unknown_journal_points_at_install_command() -> None:
    err = err_unknown_journal("nature", [])
    assert "install" in err.next_step.lower()


def test_factory_codes_are_unique() -> None:
    """Every factory should produce a distinct error code so log
    consumers can dispatch on it."""
    factories = [
        err_no_outline("p"),
        err_outline_no_structure("p"),
        err_no_sources("p"),
        err_no_graph("p"),
        err_unknown_voice("a", "b"),
        err_no_rendered_paper("p", "v"),
        err_no_document_citations("p"),
        err_unknown_style("x", []),
        err_unknown_journal("x", []),
        err_claude_unavailable(),
        err_project_not_found("p"),
    ]
    codes = [f.code for f in factories]
    assert len(set(codes)) == len(codes), f"duplicate codes: {codes}"
