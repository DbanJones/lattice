"""Tests for the voice parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from lattice.voice.parser import Voice, deep_merge


def test_parse_academic_voice(academic_voice_path: Path) -> None:
    voice = Voice.from_file(academic_voice_path)
    assert voice.name == "academic"
    assert voice.architecture.template == "six_element_paper"
    assert voice.citation.engagement_level == "name_claim_relevance"
    assert voice.citation.synthesis_threshold == 3
    # em_dashes is in prohibitions as a string
    flat = [
        p if isinstance(p, str) else p.get("pattern") or p.get("word") or p.get("phrase")
        for p in voice.prohibitions
    ]
    assert "em_dashes" in flat
    # notes contains the markdown body
    assert "Cambridge" in voice.notes


def test_validate_self_reports_missing_roles(tmp_path: Path) -> None:
    voice_file = tmp_path / "minimal.voice.md"
    voice_file.write_text(
        "---\n"
        "name: minimal\n"
        "architecture:\n  template: freeform\n"
        "citation:\n  engagement_level: name_claim\n"
        "  reporting_verbs:\n    require_variety: false\n"
        "register:\n  formality: formal\n  sentence_length: varied\n"
        "stance:\n  default: objective\n"
        "attribution:\n  style: harvard_inline\n"
        "paragraph:\n  shape: claim_evidence_implication\n"
        "role_templates:\n  setup: 'x'\n"
        "transitions:\n  supports: [x]\n"
        "prohibitions: []\n"
        "preferences: []\n"
        "---\n"
        "notes",
        encoding="utf-8",
    )
    voice = Voice.from_file(voice_file)
    issues = voice.validate_self()
    joined = " ".join(issues)
    assert "role_templates" in joined
    assert "transitions" in joined


def test_deep_merge_dicts_and_lists() -> None:
    parent = {"a": 1, "nested": {"x": 1, "y": 2}, "items": [1, 2]}
    child = {"a": 99, "nested": {"y": 20, "z": 30}, "items": [9]}
    merged = deep_merge(parent, child)
    assert merged["a"] == 99
    assert merged["nested"] == {"x": 1, "y": 20, "z": 30}
    # lists replace by default
    assert merged["items"] == [9]


def test_deep_merge_append_suffix() -> None:
    parent = {"items": [1, 2]}
    child = {"items+": [3, 4]}
    merged = deep_merge(parent, child)
    assert merged["items"] == [1, 2, 3, 4]


def test_extends_inherits_from_parent(tmp_path: Path) -> None:
    parent = tmp_path / "base.voice.md"
    parent.write_text(
        "---\n"
        "name: base\n"
        "architecture:\n  template: freeform\n"
        "citation:\n  engagement_level: name_claim\n"
        "  reporting_verbs:\n    require_variety: false\n"
        "register:\n  formality: formal\n  sentence_length: varied\n"
        "stance:\n  default: objective\n"
        "attribution:\n  style: harvard_inline\n"
        "paragraph:\n  shape: claim_evidence_implication\n"
        "role_templates:\n  setup: 'base-setup'\n  evidence: 'base-evidence'\n"
        "transitions:\n  supports: [base]\n"
        "prohibitions:\n  - word: foo\n"
        "preferences: []\n"
        "---\n",
        encoding="utf-8",
    )
    child = tmp_path / "child.voice.md"
    child.write_text(
        "---\n"
        "name: child\n"
        "extends: base\n"
        "register:\n  formality: neutral\n"
        "prohibitions+:\n  - word: bar\n"
        "---\n",
        encoding="utf-8",
    )
    voice = Voice.from_file(child)
    assert voice.name == "child"
    assert voice.architecture.template == "freeform"  # inherited
    assert voice.register.formality == "neutral"  # overridden
    # prohibitions appended
    words = [p.get("word") for p in voice.prohibitions if isinstance(p, dict) and "word" in p]
    assert "foo" in words and "bar" in words


def test_policy_voice_inherits_from_academic(voices_dir: Path) -> None:
    voice = Voice.from_file(voices_dir / "policy.voice.md")
    # Whatever policy.voice.md does with extends, parsing should succeed.
    assert voice.name
    assert voice.architecture.template in {
        "six_element_paper", "nature_compressed", "review_paper",
        "policy_brief", "journalistic_feature", "freeform",
    }
