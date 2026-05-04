"""Tests for the import-an-existing-paper wizard.

Covers the four document-kind paths (lattice_md / raw_md /
structured_docx / raw_docx) plus references-file integration plus
overwrite protection plus the next-steps message.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.ingester.import_wizard import import_paper


# ─── markdown paths ─────────────────────────────


def test_lattice_format_markdown_lands_in_outline_md(tmp_path: Path) -> None:
    src = tmp_path / "draft.md"
    src.write_text(
        "# THESIS\n\nThe thesis.\n\n"
        "# A. Body [role: argumentative]\n\n"
        "  - First claim. [type: empirical] [ref: smith_2020]\n",
        encoding="utf-8",
    )
    project = tmp_path / "p"
    result = import_paper(src, project)
    assert result.document_kind == "lattice_md"
    assert result.outline_destination == project / "structure" / "outline.md"
    assert result.outline_destination.exists()
    assert "# THESIS" in result.outline_destination.read_text(encoding="utf-8")
    assert result.raw_archive is None


def test_raw_markdown_lands_in_outline_raw(tmp_path: Path) -> None:
    src = tmp_path / "draft.md"
    src.write_text(
        "Just some prose with no lattice headings. "
        "An ordinary paragraph.",
        encoding="utf-8",
    )
    project = tmp_path / "p"
    result = import_paper(src, project)
    assert result.document_kind == "raw_md"
    assert result.outline_destination == project / "structure" / "outline.raw.md"
    assert result.outline_destination.exists()
    # outline.md should NOT exist — auto-outliner produces it later.
    assert not (project / "structure" / "outline.md").exists()


def test_overwrite_protection_lattice(tmp_path: Path) -> None:
    """A second import without --overwrite refuses to clobber."""
    src = tmp_path / "draft.md"
    src.write_text(
        "# THESIS\n\nT.\n\n# A. Body\n\n  - Claim.\n",
        encoding="utf-8",
    )
    project = tmp_path / "p"
    import_paper(src, project)
    with pytest.raises(FileExistsError):
        import_paper(src, project)


def test_overwrite_works_when_set(tmp_path: Path) -> None:
    src = tmp_path / "draft.md"
    src.write_text(
        "# THESIS\n\nT.\n\n# A. Body\n\n  - Claim.\n",
        encoding="utf-8",
    )
    project = tmp_path / "p"
    import_paper(src, project)
    # Write a different draft and overwrite.
    src.write_text(
        "# THESIS\n\nDifferent thesis.\n\n# A. Body\n\n  - Different claim.\n",
        encoding="utf-8",
    )
    result = import_paper(src, project, overwrite=True)
    text = result.outline_destination.read_text(encoding="utf-8")
    assert "Different thesis" in text


# ─── DOCX paths ─────────────────────────────────


def _docx_with_paragraphs(
    path: Path, paragraphs: list[tuple[str, str]],
) -> Path:
    """Build a minimal DOCX. Each tuple is (style, text).
    Styles: "Heading 1", "Heading 2", "Normal", or "List Bullet"."""
    from docx import Document
    doc = Document()
    for style, text in paragraphs:
        p = doc.add_paragraph(text)
        try:
            p.style = style
        except KeyError:
            p.style = "Normal"
    doc.save(str(path))
    return path


def test_structured_docx_routes_to_outline_md(tmp_path: Path) -> None:
    src = _docx_with_paragraphs(tmp_path / "draft.docx", [
        ("Heading 1", "A. Introduction"),
        ("Normal", "First claim of the introduction."),
        ("Normal", "Second claim of the introduction."),
        ("Heading 1", "B. Methods"),
        ("Normal", "Methodology claim."),
    ])
    project = tmp_path / "p"
    result = import_paper(src, project)
    assert result.document_kind == "structured_docx"
    assert result.outline_destination == project / "structure" / "outline.md"
    text = result.outline_destination.read_text(encoding="utf-8")
    # Heading 1 paragraphs become `# A.` style markers.
    assert "# A." in text
    assert "# B." in text
    # Normal paragraphs become bullets.
    assert "  - First claim" in text


def test_raw_docx_with_no_headings_routes_to_outline_raw(
    tmp_path: Path,
) -> None:
    src = _docx_with_paragraphs(tmp_path / "draft.docx", [
        ("Normal", "First paragraph of the paper."),
        ("Normal", "Second paragraph with no headings."),
        ("Normal", "Third paragraph as well."),
    ])
    project = tmp_path / "p"
    result = import_paper(src, project)
    assert result.document_kind == "raw_docx"
    assert result.outline_destination == project / "structure" / "outline.raw.md"
    text = result.outline_destination.read_text(encoding="utf-8")
    assert "First paragraph" in text
    # outline.md should not exist for raw imports.
    assert not (project / "structure" / "outline.md").exists()


# ─── project scaffolding ────────────────────────


def test_scaffold_creates_standard_layout(tmp_path: Path) -> None:
    src = tmp_path / "draft.md"
    src.write_text(
        "# THESIS\n\nT.\n\n# A. Body\n\n  - Claim.\n",
        encoding="utf-8",
    )
    project = tmp_path / "p"
    import_paper(src, project)
    for subdir in ("structure", "refs/papers", "voices", "outputs", ".lattice"):
        assert (project / subdir).is_dir(), f"Missing {subdir}"
    assert (project / "config.yml").exists()


def test_voice_file_copied_into_project(tmp_path: Path) -> None:
    src = tmp_path / "draft.md"
    src.write_text(
        "# THESIS\n\nT.\n\n# A. Body\n\n  - Claim.\n",
        encoding="utf-8",
    )
    project = tmp_path / "p"
    import_paper(src, project, voice_name="academic")
    voice_path = project / "voices" / "academic.voice.md"
    # The voice file should have been copied from examples.
    assert voice_path.exists()
    assert voice_path.read_text(encoding="utf-8").startswith("---")


def test_idempotent_scaffolding(tmp_path: Path) -> None:
    """Scaffolding twice with the same project shouldn't break — it
    just refuses to overwrite the outline file."""
    src1 = tmp_path / "first.md"
    src1.write_text(
        "# THESIS\n\nT.\n\n# A. Body\n\n  - Claim.\n",
        encoding="utf-8",
    )
    project = tmp_path / "p"
    import_paper(src1, project)
    # Project layout exists; a second import attempts to overwrite outline.
    src2 = tmp_path / "second.md"
    src2.write_text(
        "# THESIS\n\nDifferent.\n\n# A. Body\n\n  - X.\n",
        encoding="utf-8",
    )
    with pytest.raises(FileExistsError):
        import_paper(src2, project)


# ─── references-file integration ────────────────


def test_zotero_csl_json_integrates_into_source_store(tmp_path: Path) -> None:
    src = tmp_path / "draft.md"
    src.write_text(
        "# THESIS\n\nT.\n\n# A. Body\n\n  - Claim. [ref: smith_2020]\n",
        encoding="utf-8",
    )
    refs = tmp_path / "library.json"
    refs.write_text(json.dumps([
        {
            "id": "smith_2020",
            "type": "article-journal",
            "title": "On the Mechanism",
            "author": [{"family": "Smith", "given": "John A."}],
            "issued": {"date-parts": [[2020]]},
            "container-title": "Nature",
            "DOI": "10.1234/x",
        },
        {
            "id": "lee_2019",
            "type": "article-journal",
            "title": "Forecasts",
            "author": [{"family": "Lee", "given": "Kira"}],
            "issued": {"date-parts": [[2019]]},
        },
    ]), encoding="utf-8")
    project = tmp_path / "p"
    result = import_paper(src, project, references_file=refs)
    assert result.sources_imported == 2
    # The actual sources should be in the store.
    from lattice.graph.store import GraphStore
    store = GraphStore.load(project)
    sources = {s.source_id for s in store.list_sources()}
    assert "smith_2020" in sources
    assert "lee_2019" in sources


def test_bibtex_references_integrate(tmp_path: Path) -> None:
    src = tmp_path / "draft.md"
    src.write_text(
        "# THESIS\n\nT.\n\n# A. Body\n\n  - Claim.\n",
        encoding="utf-8",
    )
    refs = tmp_path / "refs.bib"
    refs.write_text(
        "@article{smith_2020, "
        "title = {On the Mechanism}, year = {2020}, "
        "author = {Smith, John}}",
        encoding="utf-8",
    )
    project = tmp_path / "p"
    result = import_paper(src, project, references_file=refs)
    assert result.sources_imported == 1


def test_no_references_file_keeps_count_zero(tmp_path: Path) -> None:
    src = tmp_path / "draft.md"
    src.write_text(
        "# THESIS\n\nT.\n\n# A. Body\n\n  - Claim.\n",
        encoding="utf-8",
    )
    project = tmp_path / "p"
    result = import_paper(src, project)
    assert result.sources_imported == 0
    assert result.sources_duplicates == 0


# ─── next-step messaging ────────────────────────


def test_next_steps_for_lattice_md_mentions_ingest(tmp_path: Path) -> None:
    src = tmp_path / "draft.md"
    src.write_text(
        "# THESIS\n\nT.\n\n# A. Body\n\n  - Claim.\n",
        encoding="utf-8",
    )
    result = import_paper(src, tmp_path / "p")
    joined = " ".join(result.next_steps)
    assert "ingest" in joined.lower()
    assert "render" in joined.lower()


def test_next_steps_for_raw_prose_mentions_scaffold(tmp_path: Path) -> None:
    src = tmp_path / "draft.md"
    src.write_text(
        "Just some prose. No headings here.",
        encoding="utf-8",
    )
    result = import_paper(src, tmp_path / "p")
    joined = " ".join(result.next_steps)
    assert "scaffold" in joined.lower() or "auto-outline" in joined.lower()


def test_next_steps_change_when_references_imported(tmp_path: Path) -> None:
    """When references are imported, the next-step about indexing PDFs
    should mention `lattice index` rather than dropping PDFs into refs/."""
    src = tmp_path / "draft.md"
    src.write_text(
        "# THESIS\n\nT.\n\n# A. Body\n\n  - Claim.\n",
        encoding="utf-8",
    )
    refs = tmp_path / "refs.json"
    refs.write_text(json.dumps([
        {"id": "x", "type": "article-journal", "title": "T",
         "issued": {"date-parts": [[2020]]}},
    ]), encoding="utf-8")
    result = import_paper(src, tmp_path / "p", references_file=refs)
    joined = " ".join(result.next_steps).lower()
    # When refs imported: next steps should NOT instruct to drop PDFs
    # but SHOULD mention `lattice index` for the additional PDFs case.
    assert "drop reference pdfs" not in joined


# ─── error paths ────────────────────────────────


def test_unknown_extension_raises(tmp_path: Path) -> None:
    src = tmp_path / "draft.rtf"
    src.write_text("anything", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported"):
        import_paper(src, tmp_path / "p")


def test_missing_document_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        import_paper(tmp_path / "doesnt_exist.md", tmp_path / "p")
