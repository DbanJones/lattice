"""Edit proposer: produces tracked-change diffs for suggest-changes flags.

Distinct from the renderer in prompt and purpose. Surgical edits only.
See docs/PROMPTS.md "Stage 9 (edit proposer)".
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Protocol

from ..graph.models import (
    AuditFlag,
    Confidence,
    EditProposal,
    EditStatus,
    EditType,
    FlagDecision,
)
from ..graph.store import GraphStore
from ..utils.config import Config
from ..voice.parser import Voice


class _LLMProtocol(Protocol):
    async def complete_json(
        self, system: str, user: str, model: str | None = None, temperature: float = 0.2
    ) -> tuple[object, object]: ...


_SYSTEM_PROMPT = """\
You are not generating new prose. You are proposing surgical edits to existing prose to address one specific flag while preserving everything else.

Rules:
- Do not propose edits beyond what the flag requires.
- Do not rewrite the cluster.
- Preserve voice, claims, citations, and arguments outside the flagged region.
- Each edit's "original" field must match the prose exactly (character-perfect).
- Each edit has a clear rationale tied to the flag.

Return JSON: [
  {
    "type": "replace|insert|delete|split_paragraph|merge_paragraphs|reorder_sentences",
    "original": "exact text being changed",
    "proposed": "replacement text",
    "rationale": "one sentence",
    "confidence": "high|medium|low"
  }
]
"""


_TYPE_MAP = {t.value: t for t in EditType}
_CONF_MAP = {c.value: c for c in Confidence}


class EditProposer:
    def __init__(
        self,
        config: Config,
        store: GraphStore,
        llm: _LLMProtocol,
        voice: Voice,
    ) -> None:
        self.config = config
        self.store = store
        self.llm = llm
        self.voice = voice

    async def propose_for_flag(self, flag: AuditFlag, prose: str) -> list[EditProposal]:
        payload, _ = await self.llm.complete_json(
            system=_SYSTEM_PROMPT,
            user=_build_user_prompt(flag, prose, self.voice),
            model=self.config.model_for_stage("edit_proposer"),
            temperature=0.4,
        )
        return _parse_proposals(payload, flag)

    async def propose_for_accepted_flags(self) -> dict[str, list[EditProposal]]:
        """For every flag with decision=accept_suggest_changes, produce proposals."""
        flags = self.store.list_audit_flags(self.voice.name)
        targeted = [
            f for f in flags if f.decision == FlagDecision.accept_suggest_changes
        ]
        if not targeted:
            return {}

        drafts_dir = self.config.project_path / ".lattice" / "drafts" / self.voice.name
        tasks: list[asyncio.Task] = []
        cluster_ids: list[str] = []

        async def _one(flag: AuditFlag) -> tuple[str, list[EditProposal]]:
            prose_path = drafts_dir / f"cluster_{flag.cluster_id}.md"
            if not prose_path.exists():
                return flag.cluster_id, []
            prose = prose_path.read_text(encoding="utf-8")
            try:
                proposals = await self.propose_for_flag(flag, prose)
            except Exception as exc:
                proposals = [_fallback_proposal(flag, exc)]
            return flag.cluster_id, proposals

        results = await asyncio.gather(*[_one(f) for f in targeted])

        grouped: dict[str, list[EditProposal]] = {}
        for cid, proposals in results:
            grouped.setdefault(cid, []).extend(proposals)

        for cid, proposals in grouped.items():
            # Merge with any existing proposals for this cluster.
            existing = self.store.list_edit_proposals(cid)
            self.store.save_edit_proposals(cid, existing + proposals)
        return grouped


def _build_user_prompt(flag: AuditFlag, prose: str, voice: Voice) -> str:
    return f"""<flag>
Rule: {flag.rule_id}
Description: {flag.rule_description}
Severity: {flag.severity.value}
Location: paragraph={flag.prose_location.paragraph_index} char={flag.prose_location.char_start}-{flag.prose_location.char_end}
Offending text: {flag.offending_text!r}
Suggestion: {flag.suggestion}
</flag>

<voice_rules>
engagement_level: {voice.citation.engagement_level}
forbid_catalogue_pattern: {voice.citation.forbid_catalogue_pattern}
formality: {voice.register.formality}
hedge_density: {voice.register.hedge_density}
</voice_rules>

<full_cluster_prose>
{prose}
</full_cluster_prose>

Propose edits to fix this flag. Surgical only.
"""


def _parse_proposals(payload: object, flag: AuditFlag) -> list[EditProposal]:
    if not isinstance(payload, list):
        return []
    now = datetime.now(timezone.utc)
    proposals: list[EditProposal] = []
    for i, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        try:
            etype = _TYPE_MAP.get(str(item.get("type", "")).lower(), EditType.replace)
            conf = _CONF_MAP.get(str(item.get("confidence", "medium")).lower(), Confidence.medium)
            proposals.append(
                EditProposal(
                    proposal_id=f"e.{now.strftime('%Y%m%dT%H%M%S')}.{i:03d}",
                    cluster_id=flag.cluster_id,
                    flag_id=flag.flag_id,
                    type=etype,
                    original_text=str(item.get("original", "") or ""),
                    proposed_text=str(item.get("proposed", "") or ""),
                    rationale=str(item.get("rationale", "") or ""),
                    rule_id=flag.rule_id,
                    confidence=conf,
                    status=EditStatus.pending,
                    created_at=now,
                )
            )
        except Exception:
            continue
    return proposals


def _fallback_proposal(flag: AuditFlag, exc: Exception) -> EditProposal:
    now = datetime.now(timezone.utc)
    return EditProposal(
        proposal_id=f"e.{now.strftime('%Y%m%dT%H%M%S')}.err",
        cluster_id=flag.cluster_id,
        flag_id=flag.flag_id,
        type=EditType.replace,
        original_text="",
        proposed_text="",
        rationale=f"proposer_error: {type(exc).__name__}: {exc}",
        rule_id=flag.rule_id,
        confidence=Confidence.low,
        status=EditStatus.deferred,
        created_at=now,
    )
