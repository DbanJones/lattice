"""Sentence craft checks. Standard. Default mode: suggest_changes."""

from __future__ import annotations

import re

from ..graph.models import AuditFlag, Cluster, FlagCategory, Severity
from .base import AuditCheck
from .patterns import expletive_construction_at_sentence_start


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

# A very rough subject-verb detector: look for the first verb-like token
# after a capitalised sentence start. This is deliberately permissive —
# false negatives are better than false positives here.
_COMMON_VERBS_RE = re.compile(
    r"\b(is|are|was|were|has|have|had|does|do|did|shows?|demonstrates?"
    r"|indicates?|suggests?|argues?|reveals?|establishes?|proposes?"
    r"|identifies?|documents?|measured|found|observed|reported|implies?"
    r"|contends?|appears?|drives?|produces?|entails?|constrains?|governs?)\b",
    re.IGNORECASE,
)


class SentenceCraftCheck(AuditCheck):
    category = FlagCategory.sentence
    default_severity = Severity.standard

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        flags: list[AuditFlag] = []

        # Expletive construction at sentence start.
        for start, end, text in expletive_construction_at_sentence_start(prose):
            flags.append(
                self._make_flag(
                    cluster=cluster,
                    rule_id="sentence.expletive_construction",
                    offending_text=text,
                    char_start=start,
                    char_end=end,
                    rule_description="Sentence starts with 'There is/are' or 'It is'.",
                    suggestion="Rewrite so the real subject and action appear up front.",
                )
            )

        # Subject-verb distance check: flag when the first verb appears >10
        # words after the sentence start.
        pos = 0
        for sent in _SENTENCE_SPLIT.split(prose):
            offset = prose.find(sent, pos)
            if offset < 0:
                offset = pos
            pos = offset + len(sent)
            words = sent.split()
            if len(words) < 10:
                continue
            verb_match = _COMMON_VERBS_RE.search(sent)
            if not verb_match:
                continue
            # Count words before the verb.
            prefix = sent[: verb_match.start()]
            word_count = len(prefix.split())
            if word_count > 10:
                flags.append(
                    self._make_flag(
                        cluster=cluster,
                        rule_id="sentence.subject_verb_distance",
                        offending_text=sent[:120] + ("..." if len(sent) > 120 else ""),
                        char_start=offset,
                        char_end=offset + len(sent),
                        rule_description=f"Subject separated from verb by {word_count} words.",
                        suggestion="Move the verb closer to the subject (under 10 words).",
                    )
                )
        return flags
