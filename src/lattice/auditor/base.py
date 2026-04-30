"""Auditor base class. One subclass per check category.

See docs/HANDOFF.md step 12. SPEC.md Section 5.9 lists every check.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from ..graph.models import (
    AuditFlag,
    Cluster,
    EditMode,
    FlagCategory,
    ProseLocation,
    Severity,
)
from ..graph.store import GraphStore
from ..utils.config import Config
from ..utils.llm import ClaudeClient
from ..voice.parser import Voice


_DEFAULT_MODE_MAP = {
    "rewrite": EditMode.rewrite,
    "suggest_changes": EditMode.suggest_changes,
    "author_choice": EditMode.author_choice,
}


class AuditCheck(ABC):
    """Base for one category of audit check."""

    category: FlagCategory = FlagCategory.voice
    default_severity: Severity = Severity.standard
    default_mode: EditMode = EditMode.suggest_changes

    def __init__(
        self, config: Config, store: GraphStore, llm: ClaudeClient, voice: Voice
    ) -> None:
        self.config = config
        self.store = store
        self.llm = llm
        self.voice = voice

    @abstractmethod
    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        raise NotImplementedError

    def _default_mode_for(self, rule_id: str) -> EditMode:
        raw = self.voice.flag_default_modes.get(rule_id)
        if raw is None:
            return self.default_mode
        return _DEFAULT_MODE_MAP.get(raw, self.default_mode)

    def _make_flag(
        self,
        cluster: Cluster,
        rule_id: str,
        offending_text: str,
        char_start: int,
        char_end: int,
        rule_description: str,
        suggestion: str = "",
        severity: Severity | None = None,
        paragraph_index: int = 0,
    ) -> AuditFlag:
        return AuditFlag(
            flag_id=f"f.{_ts()}.{_short_uid()}",
            category=self.category,
            rule_id=rule_id,
            severity=severity or self.default_severity,
            default_mode=self._default_mode_for(rule_id),
            cluster_id=cluster.cluster_id,
            section_id=cluster.section_id,
            prose_location=ProseLocation(
                paragraph_index=paragraph_index,
                char_start=char_start,
                char_end=char_end,
            ),
            offending_text=offending_text,
            rule_description=rule_description,
            suggestion=suggestion,
            voice_name=self.voice.name,
            created_at=datetime.now(timezone.utc),
        )


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _short_uid() -> str:
    return uuid.uuid4().hex[:6]


def iter_paragraphs(prose: str) -> list[tuple[int, int, int, str]]:
    """Return (paragraph_index, char_start, char_end, text) for each non-empty paragraph."""
    results: list[tuple[int, int, int, str]] = []
    pos = 0
    idx = 0
    for para in prose.split("\n\n"):
        start = pos
        end = pos + len(para)
        if para.strip():
            results.append((idx, start, end, para))
            idx += 1
        # else: preserve index so caller can relate to source-order
        pos = end + 2  # include the "\n\n" separator
    return results
