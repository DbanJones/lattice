"""Paragraph architecture checks. Standard. Default mode: suggest_changes."""

from __future__ import annotations

from ..graph.models import AuditFlag, Cluster, FlagCategory, Severity
from .base import AuditCheck, iter_paragraphs
from .patterns import continuation_opener


class ParagraphArchitectureCheck(AuditCheck):
    category = FlagCategory.paragraph
    default_severity = Severity.standard

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        flags: list[AuditFlag] = []

        # Continuation-opener check (Moreover, Furthermore, ...).
        for start, end, text in continuation_opener(prose):
            idx = _para_index_at(prose, start)
            flags.append(
                self._make_flag(
                    cluster=cluster,
                    rule_id="paragraph.continuation_opener",
                    offending_text=text,
                    char_start=start,
                    char_end=end,
                    rule_description="Paragraph opens with a continuation connective.",
                    suggestion="Open with the paragraph's topic, not with 'Moreover'/'Furthermore'.",
                    paragraph_index=idx,
                )
            )

        # Length check: flag paragraphs over the voice's max.
        max_words = self.voice.paragraph.length_words_max
        for idx, start, _end, para in iter_paragraphs(prose):
            wc = len(para.split())
            if wc > max_words:
                flags.append(
                    self._make_flag(
                        cluster=cluster,
                        rule_id="paragraph.too_long",
                        offending_text=para[:120] + ("..." if len(para) > 120 else ""),
                        char_start=start,
                        char_end=start + min(120, len(para)),
                        rule_description=f"Paragraph length {wc} words exceeds voice max of {max_words}.",
                        suggestion="Split into two paragraphs or tighten.",
                        paragraph_index=idx,
                    )
                )
        return flags


def _para_index_at(prose: str, char_offset: int) -> int:
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
