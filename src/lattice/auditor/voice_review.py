"""Whole-document voice compliance review.

Distinct from the per-cluster checks in this package (which catch local
violations like banned words, expletive openers, paragraph length).
This pass runs over the assembled document and audits the *aggregate*
statistics that only make sense at the document scale:

- **register**: sentence-length distribution, first-person frequency,
  hedge density, contractions
- **paragraph**: opener-variety (no 3 in a row sharing a construction),
  shape distribution
- **citation**: reporting-verb variety across paragraphs, synthesis-
  threshold compliance, positioning frames for thesis / gap claims
- **architecture**: hourglass shape (opening width vs closing width),
  skim-target strength (title, abstract, end-of-literature gap
  statement, end-of-conclusion strongest content)
- **attribution**: first-mention-full enforcement, page-specificity for
  direct quotes, quote-threshold compliance (block-quote longer quotes)

Each check produces a :class:`Finding` with a compliance verdict
(``pass`` / ``warning`` / ``fail``), a one-line summary, the numerical
detail, and a suggestion when applicable. Findings aggregate into a
:class:`VoiceComplianceReport` written to
``outputs/voice_review.<voice>.md`` alongside the paper.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..graph.models import ClaimType, SectionRole
from ..graph.store import GraphStore
from ..voice.parser import Voice


# ─── Finding model ──────────────────────────────────

@dataclass
class Finding:
    layer: str          # "register" | "paragraph" | ...
    rule: str           # e.g. "register.sentence_length_distribution"
    compliance: str     # "pass" | "warning" | "fail"
    summary: str        # one-line headline
    detail: str = ""    # numbers, ratios, or context
    suggestion: str = ""


@dataclass
class VoiceComplianceReport:
    voice_name: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def fail_count(self) -> int:
        return sum(1 for f in self.findings if f.compliance == "fail")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.compliance == "warning")

    @property
    def pass_count(self) -> int:
        return sum(1 for f in self.findings if f.compliance == "pass")

    @property
    def overall(self) -> str:
        if self.fail_count > 0:
            return "fail"
        if self.warning_count > 0:
            return "warning"
        return "pass"

    def to_markdown(self) -> str:
        emoji = {"pass": "[OK]", "warning": "[!]", "fail": "[FAIL]"}
        lines = [
            f"# Voice compliance review — `{self.voice_name}`",
            "",
            f"**Overall**: {emoji[self.overall]} {self.overall} "
            f"({self.pass_count} pass, {self.warning_count} warning, {self.fail_count} fail)",
            "",
        ]
        by_layer: dict[str, list[Finding]] = {}
        for f in self.findings:
            by_layer.setdefault(f.layer, []).append(f)
        for layer in (
            "register", "paragraph", "citation", "architecture",
            "attribution", "skim_target",
        ):
            findings = by_layer.get(layer, [])
            if not findings:
                continue
            lines.append(f"## {layer}")
            lines.append("")
            for f in findings:
                lines.append(f"### {emoji[f.compliance]} `{f.rule}`")
                lines.append(f"_{f.summary}_")
                lines.append("")
                if f.detail:
                    lines.append(f"- {f.detail}")
                if f.suggestion:
                    lines.append(f"- **Suggestion:** {f.suggestion}")
                lines.append("")
        return "\n".join(lines)


# ─── Reviewer ────────────────────────────────────────

class VoiceComplianceReview:
    def __init__(
        self,
        store: GraphStore,
        voice: Voice,
        document_text: str,
        project_path: Path,
    ) -> None:
        self.store = store
        self.voice = voice
        self.text = document_text
        self.project_path = Path(project_path)
        # Pre-compute the structural views we'll reuse across checks.
        self._paragraphs = _split_paragraphs(self.text)
        self._sections = _split_into_sections(self.text)

    def review(self) -> VoiceComplianceReport:
        report = VoiceComplianceReport(voice_name=self.voice.name)
        report.findings.extend(self._check_register())
        report.findings.extend(self._check_paragraph_layer())
        report.findings.extend(self._check_citation_layer())
        report.findings.extend(self._check_architecture_layer())
        report.findings.extend(self._check_attribution_layer())
        report.findings.extend(self._check_skim_targets())
        return report

    # ─── Register ───────────────────────────────

    def _check_register(self) -> list[Finding]:
        findings: list[Finding] = []
        register = self.voice.register

        sentences = _flatten_sentences(self._paragraphs)
        if sentences:
            buckets = _bucket_by_length(sentences)
            target = register.sentence_length_target_distribution or {
                "short": 0.30, "medium": 0.50, "long": 0.20,
            }
            actual = {
                "short": buckets["short"] / len(sentences),
                "medium": buckets["medium"] / len(sentences),
                "long": buckets["long"] / len(sentences),
            }
            deltas = {k: actual[k] - target.get(k, 0) for k in actual}
            largest = max(abs(d) for d in deltas.values())
            verdict = "pass" if largest <= 0.10 else ("warning" if largest <= 0.20 else "fail")
            findings.append(Finding(
                layer="register",
                rule="register.sentence_length_distribution",
                compliance=verdict,
                summary=(
                    f"distribution short={actual['short']:.0%} "
                    f"medium={actual['medium']:.0%} long={actual['long']:.0%}"
                ),
                detail=(
                    f"target short={target.get('short', 0):.0%} "
                    f"medium={target.get('medium', 0):.0%} long={target.get('long', 0):.0%}; "
                    f"largest deviation {largest:.0%}"
                ),
                suggestion=(
                    "Mix short and long sentences more deliberately; "
                    "earn long sentences with short ones."
                    if verdict != "pass" else ""
                ),
            ))

        # First-person frequency.
        if sentences:
            first_person_count = sum(
                1 for s in sentences if _FIRST_PERSON_RE.search(s)
            )
            ratio = first_person_count / len(sentences)
            target_band = {
                "forbidden": (0, 0),
                "sparing": (0, 0.10),
                "natural": (0.05, 0.30),
                "primary": (0.20, 1.0),
            }.get(register.first_person, (0, 0.10))
            in_band = target_band[0] <= ratio <= target_band[1]
            verdict = "pass" if in_band else ("warning" if ratio <= target_band[1] * 1.5 else "fail")
            findings.append(Finding(
                layer="register",
                rule="register.first_person_frequency",
                compliance=verdict,
                summary=(
                    f"{first_person_count} first-person sentences "
                    f"({ratio:.1%}); target band: {register.first_person} "
                    f"({target_band[0]:.0%}-{target_band[1]:.0%})"
                ),
                suggestion=(
                    "Reduce first-person sentences, or reclassify the claims "
                    "as user_synthesis with author_origin if they really are "
                    "your own opinions."
                    if verdict != "pass" and ratio > target_band[1] else ""
                ),
            ))

        # Hedge density.
        if sentences:
            hedge_count = sum(_count_hedges(s) for s in sentences)
            hedge_per_sentence = hedge_count / len(sentences)
            target_max = {
                "none": 0.05,
                "light": 0.20,
                "calibrated": 0.40,
                "heavy": 0.80,
            }.get(register.hedge_density, 0.40)
            verdict = (
                "pass" if hedge_per_sentence <= target_max
                else ("warning" if hedge_per_sentence <= target_max * 1.5 else "fail")
            )
            findings.append(Finding(
                layer="register",
                rule="register.hedge_density",
                compliance=verdict,
                summary=(
                    f"{hedge_per_sentence:.2f} hedge words per sentence; "
                    f"target {register.hedge_density} (cap {target_max:.2f})"
                ),
                suggestion=(
                    "Trim hedges where evidence is strong; reserve them "
                    "for genuinely uncertain claims."
                    if verdict != "pass" else ""
                ),
            ))

        # Contractions.
        if register.contractions == "forbidden":
            contraction_hits = _CONTRACTION_RE.findall(self.text)
            verdict = "pass" if not contraction_hits else "fail"
            findings.append(Finding(
                layer="register",
                rule="register.contractions",
                compliance=verdict,
                summary=(
                    "no contractions found"
                    if not contraction_hits
                    else f"{len(contraction_hits)} contraction(s) found"
                ),
                detail=(
                    f"examples: {', '.join(contraction_hits[:5])}"
                    if contraction_hits else ""
                ),
                suggestion="Expand contractions to their full form." if contraction_hits else "",
            ))

        return findings

    # ─── Paragraph layer ─────────────────────────

    def _check_paragraph_layer(self) -> list[Finding]:
        findings: list[Finding] = []
        if not self._paragraphs:
            return findings

        # Opener variety: flag if 3+ adjacent paragraphs share the same first
        # 1-2 word opening pattern.
        openers = [_paragraph_opener(p) for p in self._paragraphs]
        worst_run = 1
        current_run = 1
        offending: tuple[int, str] | None = None
        for i in range(1, len(openers)):
            if openers[i] and openers[i] == openers[i - 1]:
                current_run += 1
                if current_run >= 3 and (offending is None or current_run > worst_run):
                    offending = (i, openers[i])
                worst_run = max(worst_run, current_run)
            else:
                current_run = 1
        verdict = "pass" if worst_run < 3 else ("warning" if worst_run == 3 else "fail")
        summary = (
            "no paragraph-opener repetition" if worst_run < 3
            else f"{worst_run} consecutive paragraphs open with {(offending[1] if offending else '')!r}"
        )
        findings.append(Finding(
            layer="paragraph",
            rule="paragraph.opener_variety",
            compliance=verdict,
            summary=summary,
            suggestion=(
                "Restructure successive paragraphs so each opens with a "
                "different topic-positioned phrase."
                if verdict != "pass" else ""
            ),
        ))

        # Mean paragraph length vs voice.paragraph.length_words_max.
        max_words = self.voice.paragraph.length_words_max or 250
        too_long = [p for p in self._paragraphs if len(p.split()) > max_words]
        verdict = (
            "pass" if not too_long
            else ("warning" if len(too_long) <= 2 else "fail")
        )
        findings.append(Finding(
            layer="paragraph",
            rule="paragraph.length_words_max",
            compliance=verdict,
            summary=(
                f"{len(too_long)} paragraph(s) exceed {max_words} words"
                if too_long
                else f"all paragraphs within the {max_words}-word ceiling"
            ),
            suggestion=(
                "Split long paragraphs at the natural turn between "
                "claim and elaboration."
                if too_long else ""
            ),
        ))

        # Paragraph length distribution: average sentences per paragraph
        # against voice.paragraph.length_sentences range.
        target_range = self.voice.paragraph.length_sentences or [4, 8]
        if len(target_range) == 2:
            sentence_counts = [
                len(_split_sentences(p)) for p in self._paragraphs
            ]
            mean_count = sum(sentence_counts) / len(sentence_counts)
            in_range = target_range[0] <= mean_count <= target_range[1]
            verdict = "pass" if in_range else "warning"
            findings.append(Finding(
                layer="paragraph",
                rule="paragraph.length_sentences",
                compliance=verdict,
                summary=(
                    f"mean {mean_count:.1f} sentences/paragraph "
                    f"(target {target_range[0]}-{target_range[1]})"
                ),
            ))

        return findings

    # ─── Citation layer ──────────────────────────

    def _check_citation_layer(self) -> list[Finding]:
        findings: list[Finding] = []

        # 1. Reporting-verb variety across paragraphs.
        all_verbs: list[str] = []
        for p in self._paragraphs:
            all_verbs.extend(_extract_reporting_verbs(p, self.voice))
        if all_verbs:
            counts = Counter(all_verbs)
            top_verb, top_count = counts.most_common(1)[0]
            top_share = top_count / len(all_verbs)
            verdict = "pass" if top_share < 0.3 else ("warning" if top_share < 0.5 else "fail")
            findings.append(Finding(
                layer="citation",
                rule="citation.reporting_verb_variety",
                compliance=verdict,
                summary=(
                    f"{len(counts)} distinct reporting verb(s) across "
                    f"{len(all_verbs)} citation occurrence(s); "
                    f"most common is {top_verb!r} at {top_share:.0%}"
                ),
                suggestion=(
                    "Vary reporting verbs based on claim confidence: "
                    "demonstrate/establish (strong), suggest/indicate "
                    "(moderate), may/might (speculative)."
                    if verdict != "pass" else ""
                ),
            ))

        # 2. Synthesis-threshold compliance: when a paragraph cites N+ distinct
        # sources, did it use synthesis language?
        threshold = self.voice.citation.synthesis_threshold or 3
        offenders: list[str] = []
        for p in self._paragraphs:
            sources = _extract_inline_citations(p)
            distinct = {s.split("(")[0].strip() for s in sources}
            if len(distinct) >= threshold and not _has_synthesis_language(p):
                offenders.append(_short(p, 80))
        verdict = "pass" if not offenders else "fail"
        findings.append(Finding(
            layer="citation",
            rule="citation.synthesis_threshold",
            compliance=verdict,
            summary=(
                f"all multi-source paragraphs synthesise"
                if not offenders
                else f"{len(offenders)} paragraph(s) cite {threshold}+ distinct sources without synthesis language"
            ),
            detail=("e.g. " + offenders[0]) if offenders else "",
            suggestion=(
                "Open the paragraph with a synthesis sentence: "
                "'Three lines of evidence converge: ...' rather than "
                "listing sources sequentially."
                if offenders else ""
            ),
        ))

        # 3. Positioning frames for thesis-relevant claims.
        graph = self.store.get_graph()
        thesis_claims = {
            c.claim_id for c in graph.claims
            if c.type == ClaimType.user_synthesis and c.author_origin
        }
        positioning_phrases = (
            "i argue", "i contend", "i propose", "while", "against this",
            "in contrast to", "building on", "extending",
        )
        if thesis_claims:
            with_frame = 0
            for p in self._paragraphs:
                lower = p.lower()
                if any(phrase in lower for phrase in positioning_phrases):
                    with_frame += 1
            ratio = with_frame / max(len(self._paragraphs), 1)
            verdict = "pass" if ratio >= 0.15 else "warning"
            findings.append(Finding(
                layer="citation",
                rule="citation.positioning_required_for",
                compliance=verdict,
                summary=(
                    f"{with_frame}/{len(self._paragraphs)} paragraphs "
                    f"({ratio:.0%}) use a positioning frame"
                ),
                suggestion=(
                    "Add They Say / I Say frames around your synthesis "
                    "claims so the reader sees where you stand vs the literature."
                    if verdict != "pass" else ""
                ),
            ))

        return findings

    # ─── Architecture layer ──────────────────────

    def _check_architecture_layer(self) -> list[Finding]:
        findings: list[Finding] = []
        if not self._sections:
            return findings

        # Hourglass: opening section paragraph count vs closing section.
        if self.voice.architecture.hourglass_required and len(self._sections) >= 2:
            opening = self._sections[0]
            closing = self._sections[-1]
            opening_paragraphs = _count_paragraphs(opening["body"])
            closing_paragraphs = _count_paragraphs(closing["body"])
            ratio = (
                closing_paragraphs / opening_paragraphs
                if opening_paragraphs else 0
            )
            verdict = (
                "pass" if 0.5 <= ratio <= 1.5
                else ("warning" if 0.33 <= ratio <= 2.0 else "fail")
            )
            findings.append(Finding(
                layer="architecture",
                rule="architecture.hourglass_shape",
                compliance=verdict,
                summary=(
                    f"opening width {opening_paragraphs}, closing width "
                    f"{closing_paragraphs}; ratio {ratio:.2f}"
                ),
                suggestion=(
                    "Tighten the opening or expand the closing so they "
                    "have similar width — the hourglass shape signals "
                    "the document narrows down to its findings."
                    if verdict != "pass" else ""
                ),
            ))

        # Skim-target weighting (just check the targets exist).
        skim_targets = self.voice.architecture.skim_targets_must_be_strongest or []
        title_present = bool(re.search(r"^#\s+\S", self.text, re.MULTILINE))
        if "title" in skim_targets:
            findings.append(Finding(
                layer="architecture",
                rule="architecture.skim_target.title",
                compliance="pass" if title_present else "fail",
                summary="title heading present" if title_present else "no title heading found",
                suggestion="Add a `# Title` heading at the top." if not title_present else "",
            ))

        # Thesis drift: heading thesis vs argued thesis.
        graph = self.store.get_graph()
        argued = (graph.thesis_argued or "").strip()
        heading = (graph.thesis_statement or "").strip()
        if argued and heading:
            same = argued == heading
            verdict = "pass" if same else "warning"
            note = graph.thesis_argued_note or ""
            findings.append(Finding(
                layer="architecture",
                rule="architecture.thesis_drift",
                compliance=verdict,
                summary=(
                    "heading thesis matches what the body argues"
                    if same else
                    "heading thesis diverges from what the body argues"
                ),
                detail=(
                    ""
                    if same else
                    f"Heading: {_short(heading, 120)}\n\nArgued: {_short(argued, 120)}"
                    + (f"\n\nNote: {note}" if note else "")
                ),
                suggestion=(
                    ""
                    if same else
                    "Either rewrite the heading thesis to match the body, "
                    "or sharpen the body claims so the document actually "
                    "argues the heading thesis. Reviewers will read the "
                    "heading first and judge the paper against it."
                ),
            ))

        return findings

    # ─── Attribution layer ──────────────────────

    def _check_attribution_layer(self) -> list[Finding]:
        findings: list[Finding] = []
        attribution = self.voice.attribution

        # Quote threshold: any quoted block longer than threshold words?
        if attribution.quote_threshold_words:
            threshold = attribution.quote_threshold_words
            long_quotes = []
            for match in re.finditer(r'"([^"]{30,})"', self.text):
                quote = match.group(1)
                wc = len(quote.split())
                if wc > threshold:
                    long_quotes.append((wc, _short(quote, 60)))
            verdict = "pass" if not long_quotes else "warning"
            findings.append(Finding(
                layer="attribution",
                rule="attribution.quote_threshold_words",
                compliance=verdict,
                summary=(
                    f"all inline quotes within {threshold}-word threshold"
                    if not long_quotes
                    else f"{len(long_quotes)} inline quote(s) exceed {threshold} words"
                ),
                detail=(
                    f"longest: {long_quotes[0][0]} words — {long_quotes[0][1]}"
                    if long_quotes else ""
                ),
                suggestion=(
                    "Convert long inline quotes to block quotes or "
                    "paraphrase them."
                    if long_quotes else ""
                ),
            ))

        # Page specificity for direct quotes (best-effort heuristic).
        if attribution.page_specificity == "always":
            quoted_without_page = []
            for match in re.finditer(r'"[^"]{20,}"', self.text):
                # Look 100 chars after the quote for "p. N" or page reference.
                tail = self.text[match.end() : match.end() + 100]
                if not re.search(r"\bp\.?\s*\d+|page\s+\d+", tail, re.IGNORECASE):
                    quoted_without_page.append(_short(match.group(0), 50))
            verdict = "pass" if not quoted_without_page else "warning"
            findings.append(Finding(
                layer="attribution",
                rule="attribution.page_specificity",
                compliance=verdict,
                summary=(
                    "all direct quotes have page references"
                    if not quoted_without_page
                    else f"{len(quoted_without_page)} direct quote(s) without a page reference"
                ),
                suggestion=(
                    "Add `(Author year, p. N)` after each direct quote."
                    if quoted_without_page else ""
                ),
            ))

        return findings

    # ─── Skim-target layer ──────────────────────

    def _check_skim_targets(self) -> list[Finding]:
        findings: list[Finding] = []
        targets = set(self.voice.architecture.skim_targets_must_be_strongest or [])
        if not targets:
            return findings

        # End-of-literature gap statement.
        if "end_of_literature_review" in targets and self._sections:
            literature_section = next(
                (s for s in self._sections if "literature" in s["title"].lower() or "review" in s["title"].lower()),
                None,
            )
            if literature_section:
                tail = literature_section["body"][-400:]
                gap_signals = ("gap", "untested", "unaddressed", "missing", "unclear", "unanswered")
                has_gap = any(g in tail.lower() for g in gap_signals)
                findings.append(Finding(
                    layer="skim_target",
                    rule="skim_target.end_of_literature_gap",
                    compliance="pass" if has_gap else "warning",
                    summary=(
                        "literature review ends with a gap statement"
                        if has_gap
                        else "no explicit gap statement at the end of the literature review"
                    ),
                    suggestion=(
                        "Close the literature review with one or two "
                        "sentences naming what is unresolved."
                        if not has_gap else ""
                    ),
                ))

        # End-of-conclusion strongest content.
        if "end_of_conclusion" in targets and self._sections:
            last_section = self._sections[-1]
            body = last_section["body"].strip()
            tail = body[-300:] if len(body) >= 300 else body
            ends_strong = bool(tail) and not tail.rstrip().endswith(("possibly.", "may.", "might.", "could."))
            wc = len(tail.split())
            findings.append(Finding(
                layer="skim_target",
                rule="skim_target.end_of_conclusion_strength",
                compliance="pass" if ends_strong and wc >= 20 else "warning",
                summary=(
                    "conclusion ends emphatically"
                    if ends_strong and wc >= 20
                    else "conclusion ends weakly or is too short to land"
                ),
                detail=f"final {wc} words: {_short(tail, 100)}" if tail else "",
                suggestion=(
                    "Restate the central claim in stronger form at the "
                    "very end. End on the emphatic information."
                    if not (ends_strong and wc >= 20) else ""
                ),
            ))

        return findings


# ─── helpers ────────────────────────────────────────

_FIRST_PERSON_RE = re.compile(r"\b(I|we|my|our|me|us)\b")
_HEDGE_WORDS = (
    "may", "might", "could", "perhaps", "possibly", "somewhat", "seems",
    "appears", "likely", "probably", "tend", "suggest", "indicate",
)
_CONTRACTION_RE = re.compile(
    r"\b(?:don|can|won|shouldn|couldn|wouldn|isn|aren|wasn|weren|hasn|haven|hadn"
    r"|doesn|didn|it|that|there|here|he|she|we|they|you|I)"
    r"(?:'s|'re|'ve|'ll|'d|'t|'m)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_SECTION_HEADING_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_INLINE_CITATION_RE = re.compile(
    r"\b([A-Z][A-Za-z\-]+(?:\s+et\s+al\.?)?(?:\s*&\s*[A-Z][A-Za-z\-]+)?)"
    r"\s*\(\s*(\d{4})"
)


def _split_paragraphs(text: str) -> list[str]:
    paragraphs: list[str] = []
    for chunk in re.split(r"\n\s*\n+", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Skip headings and table separator-only lines.
        if chunk.startswith("#"):
            continue
        if all(set(line) <= {"-", ":", "|", " "} for line in chunk.splitlines() if line.strip()):
            continue
        paragraphs.append(chunk)
    return paragraphs


def _split_into_sections(text: str) -> list[dict]:
    """Return a list of {title, body} dicts in document order, splitting on
    H2 headings."""
    parts = re.split(r"^##\s+(.+)$", text, flags=re.MULTILINE)
    if len(parts) < 3:
        return []
    sections: list[dict] = []
    # parts[0] is preamble (title + intro), parts[1::2] are titles, parts[2::2] are bodies.
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append({"title": title, "body": body})
    return sections


def _flatten_sentences(paragraphs: Iterable[str]) -> list[str]:
    sentences: list[str] = []
    for p in paragraphs:
        sentences.extend(s.strip() for s in _SENTENCE_SPLIT.split(p) if s.strip())
    return sentences


def _split_sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(paragraph) if s.strip()]


def _bucket_by_length(sentences: list[str]) -> dict[str, int]:
    buckets = {"short": 0, "medium": 0, "long": 0}
    for s in sentences:
        wc = len(s.split())
        if wc < 12:
            buckets["short"] += 1
        elif wc <= 25:
            buckets["medium"] += 1
        else:
            buckets["long"] += 1
    return buckets


def _count_hedges(sentence: str) -> int:
    lower = sentence.lower()
    return sum(1 for w in _HEDGE_WORDS if re.search(rf"\b{w}\b", lower))


def _paragraph_opener(paragraph: str) -> str:
    words = paragraph.lstrip().split()
    if not words:
        return ""
    # First word, lowercased, stripped of trailing punctuation.
    # The voice's rule is that successive paragraphs should not open with the
    # same construction; the first word is the cleanest signal of that.
    return words[0].lower().rstrip(",.;:")


def _count_paragraphs(text: str) -> int:
    return len(_split_paragraphs(text))


def _extract_inline_citations(paragraph: str) -> list[str]:
    return [
        f"{m.group(1)}({m.group(2)})"
        for m in _INLINE_CITATION_RE.finditer(paragraph)
    ]


def _extract_reporting_verbs(paragraph: str, voice: Voice) -> list[str]:
    """Find reporting-verb usages in paragraphs that contain inline citations."""
    if not _INLINE_CITATION_RE.search(paragraph):
        return []
    bucket_words: list[str] = []
    rv = voice.citation.reporting_verbs
    for bucket in (rv.direct_evidence, rv.correlational, rv.theoretical, rv.speculative):
        bucket_words.extend(bucket or [])
    found: list[str] = []
    for verb in bucket_words:
        for _ in re.finditer(rf"\b{re.escape(verb)}\b", paragraph, re.IGNORECASE):
            found.append(verb.lower())
    return found


_SYNTHESIS_PHRASES = (
    "lines of evidence", "converge", "synthesis", "taken together",
    "on this reading", "the evidence converges", "these studies",
    "across these studies", "three studies",
)


def _has_synthesis_language(paragraph: str) -> bool:
    lower = paragraph.lower()
    return any(phrase in lower for phrase in _SYNTHESIS_PHRASES)


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


# ─── orchestration ──────────────────────────────────

def review_document(
    project_path: Path,
    store: GraphStore,
    voice: Voice,
) -> tuple[VoiceComplianceReport, Path | None]:
    """Read the rendered paper and run the voice compliance review.

    Returns ``(report, output_path)``. ``output_path`` is the markdown
    review file written to ``outputs/voice_review.<voice>.md``, or
    ``None`` if no rendered paper exists yet.
    """
    paper_path = project_path / "outputs" / f"paper.{voice.name}.md"
    if not paper_path.exists():
        return VoiceComplianceReport(voice_name=voice.name), None
    text = paper_path.read_text(encoding="utf-8")
    report = VoiceComplianceReview(store, voice, text, project_path).review()

    out = project_path / "outputs" / f"voice_review.{voice.name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_markdown(), encoding="utf-8")
    return report, out
