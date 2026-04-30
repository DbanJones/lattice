"""Edit review TUI.

Per-edit-proposal UI:
- Show original text vs proposed text (diff highlighting)
- Show rationale and confidence
- Buttons: accept, reject, edit-the-proposal, defer

See docs/HANDOFF.md step 15.
"""
from __future__ import annotations
from ..graph.store import GraphStore


class EditReviewTUI:
    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def run(self) -> None:
        """Walk pending edit proposals. Decisions logged to edit_decisions.json."""
        raise NotImplementedError
