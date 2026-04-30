"""Quantification checks. Critical. Default mode: suggest_changes.

Flags weasel words that pretend to be measurements. Relaxes in section
openings (first paragraph of a section) and thesis statements.
"""

from __future__ import annotations

import re

from ..graph.models import AuditFlag, Cluster, FlagCategory, Severity
from .base import AuditCheck


_WEASEL_WORDS = (
    "significantly", "substantially", "considerably", "dramatically",
    "massively", "hugely", "enormously", "vastly",
    "rapidly", "exponentially",
    "numerous", "several", "many", "various", "some", "few",
    "widely", "generally", "largely", "mostly",
)

_WEASEL_RE = re.compile(
    r"\b(" + "|".join(_WEASEL_WORDS) + r")\b",
    re.IGNORECASE,
)

# A number in the same sentence rescues the weasel word.
_NUMBER_RE = re.compile(r"\b\d[\d,\.]*\b|\bpercent\b|%")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


class QuantificationCheck(AuditCheck):
    category = FlagCategory.quantification
    default_severity = Severity.critical

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        flags: list[AuditFlag] = []
        pos = 0
        is_first_paragraph = True
        for para in prose.split("\n\n"):
            if not para.strip():
                pos += len(para) + 2
                continue
            flags.extend(
                self._check_paragraph(cluster, para, pos, is_first_paragraph)
            )
            pos += len(para) + 2
            is_first_paragraph = False
        return flags

    def _check_paragraph(
        self, cluster: Cluster, para: str, para_offset: int, is_opening: bool
    ) -> list[AuditFlag]:
        flags: list[AuditFlag] = []
        # Break paragraph into sentences; check each.
        sentence_start = 0
        for sent in _SENTENCE_SPLIT.split(para):
            sent_offset_in_para = para.find(sent, sentence_start)
            if sent_offset_in_para < 0:
                sent_offset_in_para = sentence_start
            sentence_start = sent_offset_in_para + len(sent)
            has_number = bool(_NUMBER_RE.search(sent))
            if has_number:
                continue
            for m in _WEASEL_RE.finditer(sent):
                # Section-opening sentences get a free pass on weasel words.
                if is_opening and sent_offset_in_para < 3:
                    continue
                global_start = para_offset + sent_offset_in_para + m.start()
                global_end = para_offset + sent_offset_in_para + m.end()
                flags.append(
                    self._make_flag(
                        cluster=cluster,
                        rule_id="quantification.unquantified_magnitude",
                        offending_text=m.group(0),
                        char_start=global_start,
                        char_end=global_end,
                        rule_description=f"Weasel word {m.group(0)!r} without a nearby number.",
                        suggestion="Quantify with a number, range, or rate, or remove the word.",
                    )
                )
        return flags
