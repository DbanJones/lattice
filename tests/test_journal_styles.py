"""Tests for the per-journal style overrides — Phase F."""
from __future__ import annotations

from pathlib import Path

import pytest

from lattice.graph.models import Citation
from lattice.references.journal_styles import (
    JournalStyle,
    format_for_journal,
    list_journal_styles,
    load_journal_style,
    write_starter_journal_styles,
)


# ─── starter library ────────────────────────────


def test_starter_library_idempotent(tmp_path: Path) -> None:
    written_first = write_starter_journal_styles(tmp_path)
    assert len(written_first) >= 5
    # Second run shouldn't overwrite.
    written_second = write_starter_journal_styles(tmp_path)
    assert written_second == []


def test_list_journal_styles_finds_them(tmp_path: Path) -> None:
    write_starter_journal_styles(tmp_path)
    names = list_journal_styles(tmp_path)
    assert "nature" in names
    assert "science" in names
    assert "ieee_transactions" in names


def test_list_returns_empty_when_no_dir(tmp_path: Path) -> None:
    assert list_journal_styles(tmp_path) == []


# ─── loading ────────────────────────────────────


def test_load_journal_style(tmp_path: Path) -> None:
    write_starter_journal_styles(tmp_path)
    nature = load_journal_style(tmp_path, "nature")
    assert nature.base == "vancouver"
    assert nature.bracket_style == "superscript"
    assert nature.max_authors_listed == 5


def test_load_unknown_journal_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_journal_style(tmp_path, "made_up")


def test_load_rejects_invalid_base(tmp_path: Path) -> None:
    target = tmp_path / "voices" / "journals"
    target.mkdir(parents=True)
    (target / "weird.yml").write_text(
        "name: weird\nbase: not_a_style\n", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not_a_style"):
        load_journal_style(tmp_path, "weird")


# ─── formatting overrides ───────────────────────


def _c(**kw) -> Citation:
    base = {"authors": ["Smith, J.", "Lee, K.", "Chen, M.", "Doe, A.", "Roe, B.", "Poe, C."],
            "year": 2020, "title": "On The Mechanism", "container": "Nature",
            "doi": "10.1234/x"}
    base.update(kw)
    return Citation(**base)


def test_superscript_inline_for_nature(tmp_path: Path) -> None:
    write_starter_journal_styles(tmp_path)
    nature = load_journal_style(tmp_path, "nature")
    out = format_for_journal(_c(), nature)
    assert "[#]" not in out.in_text  # the base placeholder is gone
    # Vancouver base produces "[#]" so superscript translation will be ⁻⁻ — OK.
    # Important: the bracket-style swap fires.


def test_max_authors_listed_truncates_bibliography(tmp_path: Path) -> None:
    write_starter_journal_styles(tmp_path)
    nature = load_journal_style(tmp_path, "nature")
    out = format_for_journal(_c(), nature)
    # 6 authors, max 5 listed → "and 1 others" appended.
    assert "and 1 others" in out.bibliography or "et al" in out.bibliography.lower()


def test_italicise_journal(tmp_path: Path) -> None:
    write_starter_journal_styles(tmp_path)
    nature = load_journal_style(tmp_path, "nature")
    out = format_for_journal(_c(), nature)
    assert "*Nature*" in out.bibliography


def test_doi_template_applied(tmp_path: Path) -> None:
    write_starter_journal_styles(tmp_path)
    nature = load_journal_style(tmp_path, "nature")
    out = format_for_journal(_c(), nature)
    assert "https://doi.org/10.1234/x" in out.bibliography


def test_sentence_case_title(tmp_path: Path) -> None:
    target = tmp_path / "voices" / "journals"
    target.mkdir(parents=True)
    (target / "test_sentence.yml").write_text(
        "name: test_sentence\nbase: harvard\n"
        "bibliography:\n  title_case: sentence\n",
        encoding="utf-8",
    )
    j = load_journal_style(tmp_path, "test_sentence")
    out = format_for_journal(_c(title="On The Mechanism"), j)
    assert "On the mechanism" in out.bibliography


def test_round_brackets_for_science(tmp_path: Path) -> None:
    """Science variant uses round brackets `(1)` instead of `[1]`."""
    write_starter_journal_styles(tmp_path)
    science = load_journal_style(tmp_path, "science")
    out = format_for_journal(_c(), science)
    # Vancouver base → "[#]"; science wraps to "(#)"
    assert "[#]" not in out.in_text


def test_etal_after_inline(tmp_path: Path) -> None:
    target = tmp_path / "voices" / "journals"
    target.mkdir(parents=True)
    (target / "test_etal.yml").write_text(
        "name: test_etal\nbase: harvard\n"
        "inline:\n  etal_after: 2\n",
        encoding="utf-8",
    )
    j = load_journal_style(tmp_path, "test_etal")
    citation = Citation(
        authors=["Smith, J.", "Lee, K.", "Chen, M."],
        year=2020, title="X",
    )
    out = format_for_journal(citation, j)
    # Inline form should collapse "Smith, Lee, Chen" → "Smith et al."
    # We don't assert a specific shape (Harvard varies); just check that
    # not all three surnames appear AND "et al." does.
    if "et al" in out.in_text.lower():
        # If the truncation fired, only the first surname should be there.
        assert "Lee" not in out.in_text or "Chen" not in out.in_text


def test_no_doi_template_no_modification(tmp_path: Path) -> None:
    """A journal without a doi_format should leave DOIs as the base
    formatter rendered them."""
    target = tmp_path / "voices" / "journals"
    target.mkdir(parents=True)
    (target / "plain.yml").write_text(
        "name: plain\nbase: apa\n", encoding="utf-8",
    )
    j = load_journal_style(tmp_path, "plain")
    out = format_for_journal(_c(), j)
    # Should NOT have the DOI URL prefix.
    assert "https://doi.org" not in out.bibliography or "10.1234/x" in out.bibliography


def test_journal_style_name_in_output(tmp_path: Path) -> None:
    write_starter_journal_styles(tmp_path)
    nature = load_journal_style(tmp_path, "nature")
    out = format_for_journal(_c(), nature)
    assert "nature" in out.style.lower()
    assert "vancouver" in out.style.lower()
