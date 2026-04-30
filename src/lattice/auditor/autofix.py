"""Autofix pipeline: chain audit-flag acceptance, edit proposing, and
applying without requiring per-flag manual review.

Executes within the bounds set by ``Config.autocorrect``. The setting
gates *what* is touched; this module simply enforces the policy:

- ``none``       — refuses to autofix anything; raises a clear error so
                   the caller falls back to manual review.
- ``safe``       — accepts pending audit flags whose ``default_mode`` is
                   ``suggest_changes`` (mechanical prose nits — weasel
                   words, citation engagement, formality), runs
                   ``propose`` for them, accepts the resulting edit
                   proposals, applies them. Never deletes content,
                   never marks clusters dirty for re-render.
- ``aggressive`` — runs the safe pass AND additionally accepts pending
                   flags whose default_mode is ``rewrite``, marking the
                   affected clusters as dirty so the next ``render`` call
                   regenerates them. Also deletes orphan sentences when
                   no claim attachment is feasible.

Never mutates the author graph (no claims added, no relationships
changed). The author's argument structure remains untouched; only prose
is autocorrected.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..editor.applier import EditApplier
from ..editor.proposer import EditProposer
from ..graph.models import EditMode, ProseState
from ..graph.store import GraphStore
from ..utils.config import Config
from ..utils.llm import ClaudeClient
from ..voice.parser import Voice


@dataclass
class AutofixResult:
    accepted_suggest_changes: int = 0
    accepted_rewrite: int = 0
    proposals_generated: int = 0
    proposals_accepted: int = 0
    edits_applied: int = 0
    edits_skipped: int = 0
    orphan_sentences_deleted: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return (
            self.edits_applied
            + self.orphan_sentences_deleted
            + self.accepted_rewrite  # marks clusters dirty for re-render
        )

    def summary_line(self) -> str:
        parts: list[str] = []
        if self.accepted_suggest_changes:
            parts.append(f"{self.accepted_suggest_changes} suggest_changes flag(s) accepted")
        if self.accepted_rewrite:
            parts.append(f"{self.accepted_rewrite} rewrite flag(s) accepted (clusters now dirty)")
        if self.proposals_generated:
            parts.append(f"{self.proposals_generated} edit proposal(s) generated")
        if self.edits_applied:
            parts.append(f"{self.edits_applied} edit(s) applied")
        if self.edits_skipped:
            parts.append(f"{self.edits_skipped} edit(s) skipped (text moved)")
        if self.orphan_sentences_deleted:
            parts.append(f"{self.orphan_sentences_deleted} orphan sentence(s) deleted")
        return ", ".join(parts) or "no changes"


def run_autofix(
    config: Config,
    store: GraphStore,
    voice: Voice,
    llm: ClaudeClient | None = None,
    progress=None,
) -> AutofixResult:
    """Synchronous entry point for the CLI. Use ``run_autofix_async``
    when calling from inside a running event loop (e.g. the web
    runner) — calling this from inside a loop raises
    ``RuntimeError: asyncio.run() cannot be called from a running
    event loop``."""
    return asyncio.run(run_autofix_async(config, store, voice, llm, progress))


async def run_autofix_async(
    config: Config,
    store: GraphStore,
    voice: Voice,
    llm: ClaudeClient | None,
    progress=None,
) -> AutofixResult:
    result = AutofixResult()
    level = config.autocorrect

    if level == "none":
        result.notes.append(
            "autocorrect=none: refusing to autofix. Resolve flags manually "
            "via `lattice flags` / `propose` / `apply`, or raise the "
            "autocorrect level in config.yml."
        )
        return result

    pending_flags = [
        f for f in store.list_audit_flags(voice.name)
        if f.decision is None
    ]
    if not pending_flags:
        result.notes.append("No pending flags to autofix.")
        return result

    if progress is not None:
        progress.begin("autofix", total=4, status=f"{len(pending_flags)} pending flags")
        progress.update_status("autofix", "accepting flags")

    # 1. Accept suggest_changes flags (always, for safe and aggressive).
    suggest_flag_ids: list[str] = []
    rewrite_flag_ids: list[str] = []
    for f in pending_flags:
        if f.default_mode == EditMode.suggest_changes:
            suggest_flag_ids.append(f.flag_id)
        elif f.default_mode == EditMode.rewrite and level == "aggressive":
            rewrite_flag_ids.append(f.flag_id)

    for fid in suggest_flag_ids:
        store.update_flag_decision(fid, "accept_suggest_changes")
    result.accepted_suggest_changes = len(suggest_flag_ids)

    for fid in rewrite_flag_ids:
        store.update_flag_decision(fid, "accept_rewrite")
        # Mark the affected cluster dirty so the next render regenerates it.
        flag = next(f for f in pending_flags if f.flag_id == fid)
        try:
            cluster = store.get_cluster(flag.cluster_id)
            cluster.prose_state = ProseState.dirty
            store.save_cluster(cluster)
        except KeyError:
            pass
    result.accepted_rewrite = len(rewrite_flag_ids)

    if progress is not None:
        progress.advance("autofix",
                         status=f"accepted {result.accepted_suggest_changes} suggest_changes, "
                                f"{result.accepted_rewrite} rewrite")

    # 2. Propose edits for the suggest_changes flags. Requires LLM.
    if suggest_flag_ids and llm is None:
        result.notes.append(
            "Cannot generate edit proposals without an LLM client; "
            "suggest_changes flags accepted but unfilled."
        )
        if progress is not None:
            progress.advance("autofix", status="no LLM — skipping proposals")
            progress.advance("autofix", status="no LLM — skipping apply")
    elif suggest_flag_ids and llm is not None:
        if progress is not None:
            progress.update_status(
                "autofix",
                f"proposing edits for {len(suggest_flag_ids)} flag(s)",
            )
        proposer = EditProposer(config, store, llm, voice)
        grouped = await proposer.propose_for_accepted_flags()
        result.proposals_generated = sum(len(v) for v in grouped.values())
        if progress is not None:
            progress.advance("autofix",
                             status=f"{result.proposals_generated} proposal(s) generated")

        # 3. Auto-accept every generated proposal. We rely on the
        #    proposer to be surgical — its prompt tells it not to rewrite
        #    the cluster — so blanket-accepting is appropriate at the
        #    safe level.
        for proposals in grouped.values():
            for p in proposals:
                store.update_proposal_decision(p.proposal_id, "accepted")
                result.proposals_accepted += 1

        # 4. Apply.
        if progress is not None:
            progress.update_status(
                "autofix",
                f"applying {result.proposals_accepted} edit(s) to prose files",
            )
        applier = EditApplier(
            config.project_path, store, voice_name=voice.name
        )
        applied, skipped = applier.apply_all_accepted()
        result.edits_applied = applied
        result.edits_skipped = skipped
        if progress is not None:
            progress.advance("autofix",
                             status=f"{applied} applied, {skipped} skipped")
    else:
        # No suggest_flag_ids — advance through phases 2-4 to keep the bar
        # honest at 4/4.
        if progress is not None:
            progress.advance("autofix", n=3, status="no suggest_changes flags")

    # 5. Aggressive: also delete orphan sentences. Orphan sentences are
    #    flagged by the coverage check; the autofix policy at this level
    #    is "if there's no claim to attach it to, the sentence shouldn't
    #    be there." This is content loss, so safe-level never does it.
    if level == "aggressive":
        deleted = _delete_orphan_sentences(config, store, voice)
        result.orphan_sentences_deleted = deleted

    if progress is not None:
        progress.end("autofix", status=result.summary_line() or "no changes")

    return result


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _delete_orphan_sentences(
    config: Config,
    store: GraphStore,
    voice: Voice,
) -> int:
    """Remove sentences flagged as ``coverage.orphan_sentence``."""
    deleted = 0
    drafts_dir = config.project_path / ".lattice" / "drafts" / voice.name
    flags = store.list_audit_flags(voice.name)
    orphan_flags = [
        f for f in flags
        if f.rule_id.startswith("coverage.orphan_sentence")
    ]
    if not orphan_flags:
        return 0

    # Group by cluster so we read each prose file once.
    by_cluster: dict[str, list] = {}
    for f in orphan_flags:
        by_cluster.setdefault(f.cluster_id, []).append(f)

    for cluster_id, cluster_flags in by_cluster.items():
        path = drafts_dir / f"cluster_{cluster_id}.md"
        if not path.exists():
            continue
        prose = path.read_text(encoding="utf-8")
        new_prose = prose
        for f in cluster_flags:
            offending = (f.offending_text or "").strip().strip(".")
            if not offending:
                continue
            # Walk sentences; remove the first one matching the offending
            # prefix. The flag may carry a truncated snippet, so prefix
            # match is the right test.
            sentences = _SENTENCE_END_RE.split(new_prose)
            kept: list[str] = []
            removed_one = False
            for sentence in sentences:
                stripped = sentence.strip()
                if not removed_one and stripped.startswith(offending[:60]):
                    removed_one = True
                    deleted += 1
                    continue
                kept.append(sentence)
            new_prose = " ".join(kept).strip()
        if new_prose != prose:
            path.write_text(
                new_prose + ("\n" if not new_prose.endswith("\n") else ""),
                encoding="utf-8",
            )

    return deleted
