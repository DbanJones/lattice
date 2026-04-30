"""Claim coverage checks. Critical. Default mode: rewrite."""

from __future__ import annotations

import re

from ..graph.models import AuditFlag, Cluster, FlagCategory, Severity
from .base import AuditCheck


_MISSING_CLAIM_RE = re.compile(r"\{MISSING_CLAIM:\s*\"([^\"]+)\"\}")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

# A small stop-word vocabulary used for the overlap heuristic.
_STOP = frozenset(
    "the a an of in on at to for and or but with by from as is are was were "
    "be been being have has had do does did this that these those it its "
    "their there which who whose what whom how when where why".split()
)


class CoverageCheck(AuditCheck):
    category = FlagCategory.coverage
    default_severity = Severity.critical

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        flags: list[AuditFlag] = []

        # {MISSING_CLAIM} markers always flag — these are renderer escape hatches.
        for m in _MISSING_CLAIM_RE.finditer(prose):
            flags.append(
                self._make_flag(
                    cluster=cluster,
                    rule_id="coverage.missing_claim_marker",
                    offending_text=m.group(0),
                    char_start=m.start(),
                    char_end=m.end(),
                    rule_description="Renderer emitted a MISSING_CLAIM marker.",
                    suggestion="Add the claim to the graph, then re-render the cluster.",
                )
            )

        # Orphan-sentence heuristic: each factual-looking sentence should
        # share at least one content word with the statement of some claim
        # in the cluster. Sentences without any overlap are flagged.
        claim_statements = []
        for entry in cluster.claim_sequence:
            try:
                claim = self.store.get_claim(entry.claim_id)
                claim_statements.append(claim.statement)
            except KeyError:
                continue
        if not claim_statements:
            return flags

        claim_tokens = set()
        for s in claim_statements:
            claim_tokens.update(_content_tokens(s))

        pos = 0
        for sent in _SENTENCE_SPLIT.split(prose):
            offset = prose.find(sent, pos)
            if offset < 0:
                offset = pos
            pos = offset + len(sent)
            stripped = sent.strip()
            if len(stripped) < 40:  # skip short/transition sentences
                continue
            if _MISSING_CLAIM_RE.search(stripped):
                continue
            tokens = _content_tokens(stripped)
            if not tokens:
                continue
            if not tokens & claim_tokens:
                flags.append(
                    self._make_flag(
                        cluster=cluster,
                        rule_id="coverage.orphan_sentence",
                        offending_text=stripped[:120],
                        char_start=offset,
                        char_end=offset + len(sent),
                        rule_description="Sentence does not trace to any cluster claim.",
                        suggestion="Link the sentence to a claim, or remove it.",
                    )
                )
        return flags


def _content_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text.lower())
    return {t for t in tokens if t not in _STOP}
