"""Voice prohibition checks. Walks voice.prohibitions."""

from __future__ import annotations

import re

from ..graph.models import AuditFlag, Cluster, FlagCategory, Severity
from .base import AuditCheck
from .patterns import (
    contraction,
    continuation_opener,
    expletive_construction_at_sentence_start,
    rhetorical_question,
    split_infinitive,
    stacked_hedges,
)


_PATTERN_MAP = {
    "stacked_hedges": stacked_hedges,
    "expletive_construction_at_sentence_start": expletive_construction_at_sentence_start,
    "contraction": contraction,
    "split_infinitive": split_infinitive,
    "rhetorical_question": rhetorical_question,
    "continuation_opener": continuation_opener,
}


class VoiceComplianceCheck(AuditCheck):
    category = FlagCategory.voice
    default_severity = Severity.standard

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        flags: list[AuditFlag] = []
        for prohibition in self.voice.prohibitions:
            flags.extend(self._check_one(prohibition, cluster, prose))
        return flags

    # ─── dispatch by prohibition shape ────────────────

    def _check_one(self, p, cluster, prose: str) -> list[AuditFlag]:
        # String form: a bare flag (treated as a pattern if known, else a word)
        if isinstance(p, str):
            if p in _PATTERN_MAP:
                return self._flags_from_pattern(p, _PATTERN_MAP[p](prose), cluster, p)
            return self._flags_for_literal(cluster, prose, needle=p, kind="word", rule_id=f"voice.banned_word.{_slug(p)}")

        if not isinstance(p, dict):
            return []

        if "pattern" in p:
            pattern_name = p["pattern"]
            fn = _PATTERN_MAP.get(pattern_name)
            if fn is None:
                return []
            return self._flags_from_pattern(
                pattern_name,
                fn(prose),
                cluster,
                p.get("description") or pattern_name,
            )

        if "word" in p:
            return self._flags_for_literal(
                cluster, prose,
                needle=p["word"],
                kind="word",
                rule_id=f"voice.banned_word.{_slug(p['word'])}",
                suggestion=_format_replacement(p),
            )

        if "phrase" in p:
            return self._flags_for_literal(
                cluster, prose,
                needle=p["phrase"],
                kind="phrase",
                rule_id=f"voice.banned_phrase.{_slug(p['phrase'])}",
                suggestion=p.get("instruction", _format_replacement(p)),
            )
        return []

    # ─── helpers ─────────────────────────────────────

    def _flags_for_literal(
        self, cluster, prose: str, needle: str, kind: str, rule_id: str, suggestion: str = ""
    ) -> list[AuditFlag]:
        flags: list[AuditFlag] = []
        # Whole-word match for words; literal-with-boundaries for phrases.
        if kind == "word":
            pattern = re.compile(rf"\b{re.escape(needle)}\b", re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(needle), re.IGNORECASE)
        for m in pattern.finditer(prose):
            paragraph_idx = _paragraph_index(prose, m.start())
            flags.append(
                self._make_flag(
                    cluster=cluster,
                    rule_id=rule_id,
                    offending_text=m.group(0),
                    char_start=m.start(),
                    char_end=m.end(),
                    rule_description=f"Prohibited {kind}: {needle!r}",
                    suggestion=suggestion,
                    paragraph_index=paragraph_idx,
                )
            )
        return flags

    def _flags_from_pattern(
        self, pattern_name: str, matches: list[tuple[int, int, str]], cluster, description: str
    ) -> list[AuditFlag]:
        flags: list[AuditFlag] = []
        for start, end, text in matches:
            # Map certain patterns to specific rule_ids / categories.
            rule_id = {
                "continuation_opener": "paragraph.continuation_opener",
                "expletive_construction_at_sentence_start": "sentence.expletive_construction",
            }.get(pattern_name, f"voice.pattern.{pattern_name}")
            category = {
                "continuation_opener": FlagCategory.paragraph,
                "expletive_construction_at_sentence_start": FlagCategory.sentence,
            }.get(pattern_name, FlagCategory.voice)
            flag = self._make_flag(
                cluster=cluster,
                rule_id=rule_id,
                offending_text=text,
                char_start=start,
                char_end=end,
                rule_description=description,
                paragraph_index=_paragraph_index_from_prose(cluster, start),
            )
            flag.category = category
            flags.append(flag)
        return flags


def _slug(s: str) -> str:
    return re.sub(r"[^\w]+", "_", s.lower()).strip("_")[:40] or "x"


def _format_replacement(p: dict) -> str:
    if "replacement" in p:
        return f"Replace with: {p['replacement']}"
    if "replacement_options" in p:
        return f"Options: {', '.join(p['replacement_options'])}"
    return ""


def _paragraph_index(prose: str, char_offset: int) -> int:
    pos = 0
    idx = 0
    for para in prose.split("\n\n"):
        end = pos + len(para)
        if char_offset <= end:
            return idx
        pos = end + 2
        if para.strip():
            idx += 1
    return idx


def _paragraph_index_from_prose(cluster, char_offset: int) -> int:
    # Helper is stateless; actual paragraph resolution uses the prose arg
    # in caller contexts where it's available. Default to 0 when unknown.
    return 0
