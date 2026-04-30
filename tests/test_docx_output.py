"""Tests for the DOCX output with Word comments."""
from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from lxml import etree

from lattice.graph.models import (
    AuditFlag, EditMode, FlagCategory, ProseLocation, Severity,
)
from lattice.output.docx_with_comments import write_paper_with_flags


def _mk_flag(offending: str, rule_id: str = "voice.banned_word.approximately") -> AuditFlag:
    return AuditFlag(
        flag_id=f"f.{offending[:8]}",
        category=FlagCategory.voice,
        rule_id=rule_id,
        severity=Severity.standard,
        default_mode=EditMode.suggest_changes,
        cluster_id="c.a.1",
        section_id="s.a",
        prose_location=ProseLocation(paragraph_index=0, char_start=0, char_end=len(offending)),
        offending_text=offending,
        rule_description=f"Prohibited: {rule_id}",
        suggestion="Replace with a plainer word.",
        voice_name="academic",
        created_at=datetime.now(timezone.utc),
    )


def test_docx_writes_headings_and_paragraphs(tmp_path: Path) -> None:
    md = "# Paper\n\n## Section 1\n\nFirst paragraph of section one.\n\n## Section 2\n\nSecond paragraph here.\n"
    out = tmp_path / "paper.docx"
    path, attached = write_paper_with_flags(md, [], out)
    assert path.exists()
    assert attached == 0
    # The saved DOCX is a valid zip; confirm document.xml is present.
    with zipfile.ZipFile(out, "r") as zf:
        names = zf.namelist()
        assert "word/document.xml" in names
        doc_xml = zf.read("word/document.xml").decode("utf-8")
        assert "First paragraph of section one." in doc_xml
        assert "Second paragraph here." in doc_xml
        # No comments.xml since no flags.
        assert "word/comments.xml" not in names


def test_docx_attaches_flag_as_word_comment(tmp_path: Path) -> None:
    md = (
        "# Paper\n\n## Section 1\n\n"
        "The study uses approximately ten sources, which is sufficient.\n"
    )
    flag = _mk_flag("approximately")
    out = tmp_path / "paper.docx"
    path, attached = write_paper_with_flags(md, [flag], out)
    assert attached == 1
    with zipfile.ZipFile(path, "r") as zf:
        # comments.xml added
        assert "word/comments.xml" in zf.namelist()
        comments_xml = zf.read("word/comments.xml")
        root = etree.fromstring(comments_xml)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        comments = root.findall("w:comment", ns)
        assert len(comments) == 1
        # Verify comment body mentions the rule id.
        all_text = "".join(t.text or "" for t in root.iter() if t.tag.endswith("}t"))
        assert "voice.banned_word.approximately" in all_text

        # Document has the commentRangeStart marker.
        doc_xml = zf.read("word/document.xml")
        doc_root = etree.fromstring(doc_xml)
        starts = doc_root.findall(".//w:commentRangeStart", ns)
        assert len(starts) == 1

        # Content-types updated.
        ct_xml = zf.read("[Content_Types].xml")
        assert b"comments+xml" in ct_xml

        # document.xml.rels references the comments part.
        rels_xml = zf.read("word/_rels/document.xml.rels")
        assert b"comments.xml" in rels_xml


def test_docx_comment_falls_back_to_partial_match(tmp_path: Path) -> None:
    md = "# Paper\n\n## Section 1\n\nA quite different sentence in the body.\n"
    # Offending text that doesn't match exactly but shares a prefix.
    flag = _mk_flag("A quite different sentence that was edited")
    out = tmp_path / "paper.docx"
    _, attached = write_paper_with_flags(md, [flag], out)
    assert attached == 1


def test_docx_skips_flag_when_text_not_found(tmp_path: Path) -> None:
    md = "# Paper\n\nNothing relevant here.\n"
    flag = _mk_flag("totally unrelated string")
    out = tmp_path / "paper.docx"
    _, attached = write_paper_with_flags(md, [flag], out)
    assert attached == 0
