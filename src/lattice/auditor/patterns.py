"""Named pattern detectors used by the audit checks.

Each detector returns a list of (start, end, match_text) tuples.
"""

from __future__ import annotations

import re


_HEDGE_WORDS = (
    "may", "might", "could", "perhaps", "possibly", "somewhat",
    "likely", "probably", "apparently", "seemingly", "tend",
)

_CONTINUATION_OPENERS = (
    "Moreover,", "Furthermore,", "Additionally,", "Similarly,",
    "Likewise,", "Equally,", "Then,", "Another", "In addition",
)

_CONTRACTION_RE = re.compile(
    r"\b(?:don|can|won|shouldn|couldn|wouldn|isn|aren|wasn|weren|hasn|haven|hadn"
    r"|doesn|didn|it|that|there|here|he|she|we|they|you|I|let|who|what|when|where|why|how)"
    r"(?:'s|'re|'ve|'ll|'d|'t|'m)\b",
    re.IGNORECASE,
)

_SENTENCE_START_RE = re.compile(r"(?:^|(?<=[.!?]\s))[A-Z][^.!?]*")
_EXPLETIVE_RE = re.compile(
    r"\b(There\s+(?:is|are|was|were|has been|have been)\b|It\s+(?:is|was)\b)",
)

# A very light split-infinitive heuristic: "to <adverb> <verb-ish>"
_SPLIT_INFINITIVE_RE = re.compile(
    r"\bto\s+(?:quickly|slowly|carefully|boldly|gently|easily|merely|actually|"
    r"really|hardly|barely|truly|just|simply|only|deeply|clearly|fully)\s+\w+",
    re.IGNORECASE,
)

_RHETORICAL_Q_HINT_RE = re.compile(
    r"(?:^|(?<=[.!?]\s))(?:Why|How|What|Who|When|Where|Isn't|Aren't|Shouldn't|Couldn't|Wouldn't|Don't|Doesn't)\b[^.!?]{0,200}\?"
)

# "Smith (2022)" style citations.
_CITATION_RE = re.compile(r"\b([A-Z][a-zA-Z\-]+(?:\s+et\s+al\.?)?)\s*\((\d{4}[a-z]?)\)")


def stacked_hedges(text: str) -> list[tuple[int, int, str]]:
    """Three or more hedge words within a 60-char window."""
    matches: list[tuple[int, int, str]] = []
    hits: list[tuple[int, int, str]] = []
    for word in _HEDGE_WORDS:
        for m in re.finditer(rf"\b{word}\b", text, re.IGNORECASE):
            hits.append((m.start(), m.end(), m.group(0)))
    hits.sort()
    for i in range(len(hits) - 2):
        if hits[i + 2][1] - hits[i][0] <= 60:
            start = hits[i][0]
            end = hits[i + 2][1]
            matches.append((start, end, text[start:end]))
    return _dedupe(matches)


def expletive_construction_at_sentence_start(text: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for sentence_match in _SENTENCE_START_RE.finditer(text):
        start = sentence_match.start()
        snippet = text[start : start + 40]
        m = _EXPLETIVE_RE.match(snippet)
        if m:
            matches.append((start + m.start(), start + m.end(), m.group(0)))
    return matches


def contraction(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in _CONTRACTION_RE.finditer(text)]


def split_infinitive(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in _SPLIT_INFINITIVE_RE.finditer(text)]


def rhetorical_question(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in _RHETORICAL_Q_HINT_RE.finditer(text)]


def continuation_opener(text: str) -> list[tuple[int, int, str]]:
    """Paragraph-start continuation openers (Moreover, Furthermore, ...)."""
    matches: list[tuple[int, int, str]] = []
    # Paragraph boundaries: start-of-text or following a blank line.
    for para_match in re.finditer(r"(?:\A|\n\s*\n)(\s*)([^\n]+)", text):
        para_start = para_match.start(2)
        snippet = para_match.group(2)
        for opener in _CONTINUATION_OPENERS:
            if snippet.startswith(opener):
                matches.append((para_start, para_start + len(opener), opener))
                break
    return matches


def catalogue_pattern(text: str) -> list[tuple[int, int, str]]:
    """Three or more citations in a row without synthesis between them.

    Heuristic: find citations; if 3+ distinct citations appear within a
    single sentence or adjacent sentences with no connective synthesis
    language between them, flag as a catalogue.
    """
    citations = list(_CITATION_RE.finditer(text))
    if len(citations) < 3:
        return []
    matches: list[tuple[int, int, str]] = []
    for i in range(len(citations) - 2):
        a, b, c = citations[i], citations[i + 1], citations[i + 2]
        # All within ~250 chars
        if c.end() - a.start() > 250:
            continue
        # Look for synthesis language in a window spanning from shortly
        # before the first citation to shortly after the last — synthesis
        # framing can appear on either side of the citation cluster.
        window_start = max(0, a.start() - 80)
        window_end = min(len(text), c.end() + 140)
        window = text[window_start:window_end]
        if re.search(
            r"\b(converge|synthesis|together|three lines|disagree|agree on|point to|"
            r"converges?|support(?:ed)? by|reinforce)\b",
            window,
            re.IGNORECASE,
        ):
            continue
        matches.append((a.start(), c.end(), text[a.start() : c.end()]))
    return _dedupe(matches)


def _dedupe(matches: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    seen = set()
    out: list[tuple[int, int, str]] = []
    for m in matches:
        key = (m[0], m[1])
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out
