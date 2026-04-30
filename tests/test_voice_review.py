"""Tests for the document-level voice compliance review."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.auditor.voice_review import (
    Finding, VoiceComplianceReport, VoiceComplianceReview, review_document,
)
from lattice.graph.models import (
    AuthorGraph, Claim, ClaimType, Confidence, Section, SectionRole,
)
from lattice.graph.store import GraphStore
from lattice.voice.parser import Voice


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _academic_voice() -> Voice:
    return Voice.from_file(
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )


def _bare_store(tmp_path: Path) -> GraphStore:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    return GraphStore.load(tmp_path)


def _findings(report: VoiceComplianceReport, rule: str) -> list[Finding]:
    return [f for f in report.findings if f.rule == rule]


# ─── Register ──────────────────────────────────

def test_register_contractions_pass(tmp_path):
    voice = _academic_voice()
    text = "# Title\n\n## Section\n\nThe slowdown is documented across the literature.\n"
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "register.contractions")[0]
    assert f.compliance == "pass"


def test_register_contractions_fails_when_present(tmp_path):
    voice = _academic_voice()
    text = "# Title\n\n## Section\n\nIt's an established result and we can't ignore it.\n"
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "register.contractions")[0]
    assert f.compliance == "fail"
    assert "contraction" in f.summary.lower()


def test_register_first_person_frequency(tmp_path):
    voice = _academic_voice()
    text = (
        "# Title\n\n## Section\n\n"
        "I argue that this matters. I contend that it goes further. "
        "I propose a framework. I find the data convincing. "
        "I observe a slowdown. I classify the camps."
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "register.first_person_frequency")[0]
    # Sparing band is 0-10%; this is 100%, should fail.
    assert f.compliance == "fail"


def test_register_sentence_length_distribution(tmp_path):
    voice = _academic_voice()
    # 6 short sentences = 100% short, way off the 30/50/20 target.
    text = (
        "# Title\n\n## Section\n\n"
        "The result holds. The data is clear. The methods are sound. "
        "The outcome stands. The case is open. The conclusion follows."
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "register.sentence_length_distribution")[0]
    assert f.compliance in ("warning", "fail")


# ─── Paragraph ────────────────────────────────

def test_paragraph_opener_variety_passes_when_varied(tmp_path):
    voice = _academic_voice()
    text = (
        "# Title\n\n## Section\n\n"
        "Academic forecasts diverge widely. They reflect different assumptions.\n\n"
        "Stabilisation models assume continued efficiency. They project flat growth.\n\n"
        "Explosion models assume the trend stalls. They project rapid growth."
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "paragraph.opener_variety")[0]
    assert f.compliance == "pass"


def test_paragraph_opener_variety_flags_repetition(tmp_path):
    voice = _academic_voice()
    text = (
        "# Title\n\n## Section\n\n"
        "Forecasts diverge by an order of magnitude.\n\n"
        "Forecasts disagree on the underlying drivers.\n\n"
        "Forecasts cluster around two dominant assumptions.\n\n"
        "Forecasts vary in scope and methodology."
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "paragraph.opener_variety")[0]
    assert f.compliance in ("warning", "fail")


def test_paragraph_too_long_fails(tmp_path):
    voice = _academic_voice()
    very_long = " ".join(["word"] * 400)
    text = f"# Title\n\n## Section\n\n{very_long}"
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "paragraph.length_words_max")[0]
    assert f.compliance != "pass"


# ─── Citation ─────────────────────────────────

def test_citation_synthesis_threshold_flags_unsynthesised(tmp_path):
    voice = _academic_voice()
    text = (
        "# Title\n\n## Section\n\n"
        "Jones (2019) measured X. Lee (2020) reported similar results. "
        "Park (2021) confirmed the finding. Kim (2022) extended the analysis."
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "citation.synthesis_threshold")[0]
    assert f.compliance == "fail"


def test_citation_synthesis_threshold_passes_with_synthesis_language(tmp_path):
    voice = _academic_voice()
    text = (
        "# Title\n\n## Section\n\n"
        "Three lines of evidence converge: Jones (2019), Lee (2020), and Park "
        "(2021) all find the same pattern, taken together painting a coherent picture."
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "citation.synthesis_threshold")[0]
    assert f.compliance == "pass"


def test_citation_reporting_verb_variety(tmp_path):
    voice = _academic_voice()
    # Same verb four times — repetition rate ~100%.
    text = (
        "# Title\n\n## Section\n\n"
        "Jones (2019) suggests X. Lee (2020) suggests Y. "
        "Park (2021) suggests Z. Kim (2022) suggests W."
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "citation.reporting_verb_variety")[0]
    assert f.compliance != "pass"


# ─── Architecture ────────────────────────────

def test_architecture_skim_title_pass_when_present(tmp_path):
    voice = _academic_voice()
    text = "# A title is here\n\n## Section\n\nBody.\n"
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "architecture.skim_target.title")[0]
    assert f.compliance == "pass"


def test_architecture_hourglass_balanced(tmp_path):
    voice = _academic_voice()
    text = (
        "# Title\n\n"
        "## Opening\n\n"
        "Para A.\n\nPara B.\n\nPara C.\n\n"
        "## Body\n\n"
        "Body para.\n\n"
        "## Closing\n\n"
        "Conclusion para A.\n\nConclusion para B.\n\nConclusion para C.\n"
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "architecture.hourglass_shape")[0]
    assert f.compliance == "pass"


def test_architecture_hourglass_imbalanced(tmp_path):
    voice = _academic_voice()
    text = (
        "# Title\n\n"
        "## Wide opening\n\n"
        + "\n\n".join([f"Para {i}." for i in range(10)]) + "\n\n"
        "## Body\n\nBody para.\n\n"
        "## Narrow closing\n\nFinal.\n"
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "architecture.hourglass_shape")[0]
    assert f.compliance != "pass"


# ─── Skim targets ────────────────────────────

def test_skim_end_of_conclusion_strength(tmp_path):
    voice = _academic_voice()
    text = (
        "# Title\n\n## Body\n\nBody content.\n\n"
        "## Conclusion\n\n"
        + " ".join(["Word"] * 40) + ". The argument concludes that efficiency assumptions matter most.\n"
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "skim_target.end_of_conclusion_strength")[0]
    assert f.compliance == "pass"


def test_skim_end_of_conclusion_weak_when_short(tmp_path):
    voice = _academic_voice()
    text = "# Title\n\n## Body\n\nBody.\n\n## Conclusion\n\nDone.\n"
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    f = _findings(review, "skim_target.end_of_conclusion_strength")[0]
    assert f.compliance == "warning"


# ─── End-to-end review_document ─────────────

def test_review_document_writes_markdown(tmp_path):
    project = tmp_path / "proj"
    (project / "outputs").mkdir(parents=True)
    (project / "config.yml").write_text("", encoding="utf-8")
    paper = project / "outputs" / "paper.academic.md"
    paper.write_text(
        "# Title\n\n## Body\n\nA short paragraph here.\n\n"
        "## Conclusion\n\n"
        + " ".join(["Word"] * 40) + ". The argument concludes that the matter is settled.\n",
        encoding="utf-8",
    )
    store = GraphStore.load(project)
    voice = _academic_voice()
    report, out_path = review_document(project, store, voice)
    assert out_path is not None
    assert out_path.exists()
    assert "Voice compliance review" in out_path.read_text(encoding="utf-8")
    assert report.findings  # at least some findings recorded


def test_review_document_handles_no_paper(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(project)
    voice = _academic_voice()
    report, out_path = review_document(project, store, voice)
    assert out_path is None
    assert report.findings == []


def test_to_markdown_groups_by_layer(tmp_path):
    voice = _academic_voice()
    text = (
        "# Title\n\n## Body\n\n"
        "It's a finding. We can't ignore it.\n"
    )
    review = VoiceComplianceReview(_bare_store(tmp_path), voice, text, tmp_path).review()
    md = review.to_markdown()
    assert "register" in md.lower()
    # Overall verdict surfaced.
    assert "Overall" in md
