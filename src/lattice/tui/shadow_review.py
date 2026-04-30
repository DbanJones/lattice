"""Shadow report review TUI.

Per shadow flag:
- Show flag type and details
- Buttons: accept, accept_with_edit, reject (with rationale), defer

See docs/HANDOFF.md step 18.
"""
from __future__ import annotations
from pathlib import Path
from ..graph.store import GraphStore


class ShadowReviewTUI:
    def __init__(self, store: GraphStore, report_path: Path) -> None:
        self.store = store
        self.report_path = report_path

    def run(self) -> None:
        """Walk shadow report flags. Accepted flags update author graph
        explicitly. Decisions logged to shadow_decisions.json.
        """
        raise NotImplementedError
