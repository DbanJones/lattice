"""Argus JSON ingester.

Maps Argus entities to Lattice entities per SPEC.md Section 4.1.

See docs/HANDOFF.md step 7.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..graph.models import AuthorGraph


class ArgusIngester:
    async def ingest(self, file_path: Path, project_name: str) -> AuthorGraph:
        """Parse an Argus export.

        Mapping:
        - thesis node       -> Section(role=introduction) + Claim(type=user_synthesis, thesis=True)
        - argument node     -> Section(role=argumentative)
        - claim node        -> Claim(type=empirical or user_synthesis based on evidence)
        - counter-claim     -> Claim + Relationship(type=contradicts, to=parent)
        - note node         -> attached as metadata on parent claim, not rendered
        - evidences         -> Evidence on each Claim
        - references        -> Source stubs (matched against indexed sources later)
        - parent-child      -> Section containment + claim ordering
        - edges (dependency)-> Relationship(type=unlabelled, prompt for label later)
        """
        raise NotImplementedError
