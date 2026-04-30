"""Formality checks. Minor. Default mode: suggest_changes."""

from __future__ import annotations

from ..graph.models import AuditFlag, Cluster, FlagCategory, Severity
from .base import AuditCheck
from .patterns import contraction, rhetorical_question


class FormalityCheck(AuditCheck):
    category = FlagCategory.formality
    default_severity = Severity.minor

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        flags: list[AuditFlag] = []

        if self.voice.register.contractions == "forbidden":
            for start, end, text in contraction(prose):
                flags.append(
                    self._make_flag(
                        cluster=cluster,
                        rule_id="formality.contraction",
                        offending_text=text,
                        char_start=start,
                        char_end=end,
                        rule_description="Contraction found; voice forbids contractions.",
                        suggestion="Expand to the full form.",
                    )
                )

        for start, end, text in rhetorical_question(prose):
            flags.append(
                self._make_flag(
                    cluster=cluster,
                    rule_id="formality.rhetorical_question",
                    offending_text=text[:120],
                    char_start=start,
                    char_end=end,
                    rule_description="Rhetorical question in body text.",
                    suggestion="Restate as a claim or proposition.",
                )
            )
        return flags
