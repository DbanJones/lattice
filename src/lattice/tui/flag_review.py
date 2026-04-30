"""Flag review TUI using rich.

Per-flag UI:
- Show prose snippet with offending region highlighted
- Show rule that fired
- Show default mode
- Toggle mode (rewrite vs suggest_changes)
- Buttons: accept, reject, defer

Bulk operations: accept all of category X, accept all critical, etc.

See docs/HANDOFF.md step 13.
"""
from __future__ import annotations
from ..graph.store import GraphStore
from ..voice.parser import Voice


class FlagReviewTUI:
    def __init__(self, store: GraphStore, voice: Voice) -> None:
        self.store = store
        self.voice = voice

    def run(self) -> None:
        """Main TUI loop. Reads audit_flags.json, presents one flag at a time.
        Decisions logged to .lattice/flag_decisions.json.
        Accepted flags routed:
        - accept_rewrite -> mark cluster dirty for re-render
        - accept_suggest_changes -> queue for edit proposer
        """
        raise NotImplementedError
