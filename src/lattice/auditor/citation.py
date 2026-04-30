"""Citation engagement check (LLM-bound).

The supervisor's flagged rule. Per voice ``citation.engagement_level``,
each citation in the prose must do three things (Graff & Birkenstein):

1. **names_author** — name the author in the sentence (not only in
   parenthesis)
2. **states_claim** — state the specific claim or finding (not a generic
   gesture at the topic)
3. **explains_relevance** — link the finding to the present argument
   (not a parenthetical footnote)

A citation that fails any of these triggers a critical AuditFlag with
``suggest_changes`` as the default mode — the edit proposer can usually
fix engagement with a surgical rewrite of the surrounding sentence.

Catalogue patterns (3+ sequential single-source citations without
synthesis) are caught deterministically by the shared
``patterns.catalogue_pattern`` detector and surfaced via
``VoiceComplianceCheck`` / ``ParagraphArchitectureCheck``; this module
only addresses per-citation engagement.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from ..graph.models import (
    AuditFlag,
    Cluster,
    EditMode,
    FlagCategory,
    ProseLocation,
    Severity,
)
from .base import AuditCheck


_CITATION_RE = re.compile(
    r"""
    \b
    (?P<authors>
        [A-Z][A-Za-z\-]+                        # first author surname
        (?:\s*&\s*[A-Z][A-Za-z\-]+)?            # optional & coauthor
        (?:\s+et\s+al\.?)?                       # optional et al.
    )
    \s*
    [(,]                                          # opening '(' or ', '
    \s*
    (?P<year>\d{4}[a-z]?)                         # year
    (?:[,)]|\b)
    """,
    re.VERBOSE,
)


_SYSTEM_PROMPT = """\
You check whether each citation in academic prose engages with the
source according to the Graff & Birkenstein three-element rule.

For each citation, the prose must:
1. names_author — name the author in the running sentence, not only
   in parenthesis. e.g. "Smith (2022) shows X" passes; "X has been
   shown (Smith, 2022)" fails.
2. states_claim — state the specific claim or finding, not a generic
   gesture at the topic. "Smith (2022) examined this issue" fails;
   "Smith (2022) demonstrates that X scales with Y" passes.
3. explains_relevance — link the finding to the present argument with
   one sentence ("This matters here because...", "On this reading...",
   "Building on this..."). A citation used purely as a reference
   without explaining why it appears here fails.

Return strict JSON, an array with one object per citation found:
[
  {
    "citation_text": "Smith (2022)",
    "passes": ["names_author", "states_claim", "explains_relevance"],
    "fails": [],
    "severity": "critical|standard|minor"
  }
]

Be conservative. If you cannot tell, treat the element as passing.
Severity: critical if all three fail, standard if two fail, minor if one fails.
Output JSON only — no commentary, no preamble.
"""


class CitationCheck(AuditCheck):
    category = FlagCategory.citation
    default_severity = Severity.critical
    default_mode = EditMode.suggest_changes

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        if self.llm is None:
            return []

        citations = list(_CITATION_RE.finditer(prose))
        if not citations:
            return []

        # Build the structured prompt with each citation tagged.
        citations_xml = "\n".join(
            f'<citation index="{i}" text="{m.group(0)}" '
            f'char_start="{m.start()}" char_end="{m.end()}"/>'
            for i, m in enumerate(citations)
        )
        engagement_level = self.voice.citation.engagement_level
        user = (
            f"<voice_engagement_level>{engagement_level}</voice_engagement_level>\n\n"
            f"<prose>\n{prose}\n</prose>\n\n"
            f"<citations_to_check>\n{citations_xml}\n</citations_to_check>\n\n"
            "Check each citation against the three-element rule and return JSON."
        )

        try:
            payload, _ = await self.llm.complete_json(
                system=_SYSTEM_PROMPT,
                user=user,
                model=self.config.model_for_stage("auditor"),
                temperature=0.2,
            )
        except Exception:
            # If the LLM is unavailable, don't fail the audit — silently skip.
            return []

        return list(_payload_to_flags(payload, citations, cluster, prose, self))


# ─── helpers ────────────────────────────────────────

def _payload_to_flags(
    payload: object,
    citations: list[re.Match],
    cluster: Cluster,
    prose: str,
    check: CitationCheck,
) -> Iterable[AuditFlag]:
    if not isinstance(payload, list):
        return []

    severity_map = {
        "critical": Severity.critical,
        "standard": Severity.standard,
        "minor": Severity.minor,
    }

    flags: list[AuditFlag] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        fails = entry.get("fails") or []
        if not isinstance(fails, list) or not fails:
            continue  # all three elements pass

        citation_text = str(entry.get("citation_text") or "").strip()
        match = _find_match_for_text(citations, citation_text)
        if match is None:
            continue

        # Identify which paragraph (by char offset) the citation lives in,
        # so the resulting flag can land on the right span in the doc.
        paragraph_index = _paragraph_index_at(prose, match.start())

        fails_clean = [str(f) for f in fails if isinstance(f, str)]
        suggestion = _suggestion_for(fails_clean)
        severity = severity_map.get(
            str(entry.get("severity") or "").lower(),
            Severity.critical if len(fails_clean) >= 3
            else Severity.standard if len(fails_clean) == 2
            else Severity.minor,
        )

        flag = check._make_flag(
            cluster=cluster,
            rule_id=f"citation.engagement_weak.{'_'.join(sorted(fails_clean))}",
            offending_text=match.group(0),
            char_start=match.start(),
            char_end=match.end(),
            rule_description=(
                f"Citation does not meet the {check.voice.citation.engagement_level} "
                f"engagement rule: missing {', '.join(fails_clean)}."
            ),
            suggestion=suggestion,
            severity=severity,
            paragraph_index=paragraph_index,
        )
        flags.append(flag)
    return flags


def _find_match_for_text(citations: list[re.Match], text: str) -> re.Match | None:
    """Find the regex match whose text matches the LLM-reported citation_text.

    The LLM may report a slightly different surface form (with or without
    surrounding parens, with extra whitespace, trailing punctuation), so
    normalise both sides aggressively before comparing.
    """
    def _norm(s: str) -> str:
        s = re.sub(r"\s+", " ", s).strip()
        return s.strip("(),.;:").strip()

    target = _norm(text)
    if not target:
        return None
    for m in citations:
        candidate = _norm(m.group(0))
        if candidate == target or target in candidate or candidate in target:
            return m
    return None


def _paragraph_index_at(prose: str, char_offset: int) -> int:
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


def _suggestion_for(fails: list[str]) -> str:
    parts: list[str] = []
    if "names_author" in fails:
        parts.append(
            "Lead with the author as the sentence subject ('Smith (2022) shows...')."
        )
    if "states_claim" in fails:
        parts.append(
            "Replace the generic gesture with the specific finding."
        )
    if "explains_relevance" in fails:
        parts.append(
            "Add one sentence linking the finding to the present argument."
        )
    return " ".join(parts)
