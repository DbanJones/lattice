"""Edit applier: applies accepted edit proposals to prose files.

Mechanical. Validates original_text matches before replacement.
Sets cluster.prose_state to 'edited' after applying.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import EditProposal, EditStatus, EditType, ProseState
from ..graph.store import GraphStore


class EditApplier:
    def __init__(self, project_path: Path, store: GraphStore, voice_name: str) -> None:
        self.project_path = Path(project_path)
        self.store = store
        self.voice_name = voice_name
        self.drafts_dir = self.project_path / ".lattice" / "drafts" / voice_name

    def apply_all_accepted(self) -> tuple[int, int]:
        """Apply every accepted proposal. Returns (applied, skipped)."""
        applied = 0
        skipped = 0
        proposals = self.store.list_edit_proposals()
        # Group by cluster so we touch each prose file once per application.
        by_cluster: dict[str, list[EditProposal]] = {}
        for p in proposals:
            if p.status != EditStatus.accepted:
                continue
            if p.applied_at is not None:
                continue
            by_cluster.setdefault(p.cluster_id, []).append(p)

        for cluster_id, cluster_proposals in by_cluster.items():
            for proposal in cluster_proposals:
                if self._apply_one(proposal):
                    applied += 1
                else:
                    skipped += 1

            # After applying, mark the cluster's prose_state as edited.
            try:
                cluster = self.store.get_cluster(cluster_id)
                cluster.prose_state = ProseState.edited
                self.store.save_cluster(cluster)
            except KeyError:
                pass
        return applied, skipped

    def _apply_one(self, proposal: EditProposal) -> bool:
        path = self.drafts_dir / f"cluster_{proposal.cluster_id}.md"
        if not path.exists():
            return False
        prose = path.read_text(encoding="utf-8")

        new_prose = self._apply_proposal_to_text(proposal, prose)
        if new_prose is None:
            # original_text did not match; mark proposal superseded.
            self.store.update_proposal_decision(proposal.proposal_id, "deferred")
            return False

        path.write_text(new_prose, encoding="utf-8")
        # Mark applied in the decision log.
        proposal.applied_at = datetime.now(timezone.utc)
        # Update the proposal file: the store doesn't expose an applied-at setter,
        # so we resave via the same list path by reloading and rewriting.
        self._mark_applied(proposal)
        return True

    def _apply_proposal_to_text(self, proposal: EditProposal, prose: str) -> str | None:
        if proposal.type == EditType.replace:
            if proposal.original_text and proposal.original_text in prose:
                return prose.replace(proposal.original_text, proposal.proposed_text, 1)
            return None
        if proposal.type == EditType.insert:
            # Insert proposed_text immediately after original_text (an anchor).
            if proposal.original_text and proposal.original_text in prose:
                return prose.replace(
                    proposal.original_text,
                    proposal.original_text + proposal.proposed_text,
                    1,
                )
            return None
        if proposal.type == EditType.delete:
            if proposal.original_text and proposal.original_text in prose:
                return prose.replace(proposal.original_text, "", 1)
            return None
        # split/merge/reorder paragraph types need richer handling; fall back
        # to treating `proposed_text` as the whole replacement for the block.
        if proposal.original_text and proposal.original_text in prose:
            return prose.replace(proposal.original_text, proposal.proposed_text, 1)
        return None

    def _mark_applied(self, proposal: EditProposal) -> None:
        path = self.drafts_dir.parent.parent / "edit_proposals" / f"{proposal.cluster_id}.json"
        # Use the store's own read/write path to keep status bookkeeping
        # consistent. We reload, update, save.
        import json
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data.get("proposals", []):
            if entry.get("proposal_id") == proposal.proposal_id:
                entry["applied_at"] = proposal.applied_at.isoformat() if proposal.applied_at else None
                entry["status"] = EditStatus.accepted.value
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
