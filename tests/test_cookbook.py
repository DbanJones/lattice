"""Tests for the cookbook recipes."""
from __future__ import annotations

from lattice.cli.cookbook import find_recipe, list_recipes


def test_list_recipes_returns_all_recipes() -> None:
    recipes = list_recipes()
    assert len(recipes) >= 5
    slugs = [r.slug for r in recipes]
    assert "restyle" in slugs
    assert "from-sources" in slugs
    assert "import" in slugs


def test_find_recipe_by_slug() -> None:
    r = find_recipe("restyle")
    assert r is not None
    assert "journal" in r.title.lower() or "style" in r.title.lower()
    assert "lattice citations" in " ".join(r.steps).lower()


def test_find_recipe_case_insensitive() -> None:
    assert find_recipe("RESTYLE") is not None
    assert find_recipe("Restyle") is not None


def test_find_recipe_unknown_slug_returns_none() -> None:
    assert find_recipe("not_a_real_recipe") is None


def test_every_recipe_has_concrete_commands() -> None:
    """Every recipe must include at least one runnable lattice command
    in its steps — not just commentary."""
    for recipe in list_recipes():
        runnable = [
            s for s in recipe.steps
            if not s.lstrip().startswith("#")
            and "lattice" in s.lower()
        ]
        assert runnable, f"{recipe.slug}: no runnable commands"


def test_every_recipe_has_summary_and_when_to_use() -> None:
    for recipe in list_recipes():
        assert recipe.summary, f"{recipe.slug}: empty summary"
        assert recipe.when_to_use, f"{recipe.slug}: empty when_to_use"


def test_recipe_slugs_are_unique() -> None:
    slugs = [r.slug for r in list_recipes()]
    assert len(set(slugs)) == len(slugs)


def test_restyle_recipe_mentions_citations_subcommands() -> None:
    """The killer-feature recipe should walk the user through the
    full Phase A→F pipeline."""
    r = find_recipe("restyle")
    joined = " ".join(r.steps).lower()
    assert "scan" in joined
    assert "verify" in joined
    assert "fill" in joined
    assert "restyle" in joined


def test_import_recipe_mentions_lattice_import() -> None:
    r = find_recipe("import")
    joined = " ".join(r.steps).lower()
    assert "lattice import" in joined


def test_rescaffold_recipe_mentions_metrics_loop() -> None:
    """The rescaffold recipe should walk the metrics → planner → fill
    loop the academic-review identified as undocumented."""
    r = find_recipe("rescaffold")
    joined = " ".join(r.steps).lower()
    assert "rescaffold" in joined
    assert "fill-mechanisms" in joined or "fill-evidence" in joined
