"""Voice consistency check.

For each cluster with prose_state=edited, re-render via the renderer
from the graph (with the same voice), then compute a similarity score
between the current prose and the fresh render. Below threshold flags
drift: the author's edits may have pulled the cluster away from what
the voice prescribes.

MVP similarity = Jaccard over content tokens. Embedding-based
similarity is a future enhancement; the API surface stays the same.
"""

from __future__ import annotations

import re

from ..graph.models import Cluster, ProseState
from ..graph.store import GraphStore
from ..renderer.cluster_renderer import ClusterRenderer
from ..utils.config import Config
from ..utils.llm import ClaudeClient
from ..voice.parser import Voice


_STOP = frozenset(
    "the a an of in on at to for and or but with by from as is are was were be "
    "been being have has had do does did this that these those it its their "
    "there which who whose what whom how when where why".split()
)


class VoiceConsistencyCheck:
    def __init__(
        self,
        config: Config,
        store: GraphStore,
        llm: ClaudeClient,
        voice: Voice,
        drift_threshold: float = 0.35,
    ) -> None:
        self.config = config
        self.store = store
        self.llm = llm
        self.voice = voice
        self.drift_threshold = drift_threshold

    async def check_all_edited(self) -> list[tuple[Cluster, float]]:
        drifted: list[tuple[Cluster, float]] = []
        drafts_dir = (
            self.config.project_path / ".lattice" / "drafts" / self.voice.name
        )
        edited_clusters = [
            c for c in self.store.list_clusters() if c.prose_state == ProseState.edited
        ]
        if not edited_clusters:
            return drifted

        renderer = ClusterRenderer(self.config, self.store, self.llm, self.voice)

        for cluster in edited_clusters:
            current_path = drafts_dir / f"cluster_{cluster.cluster_id}.md"
            if not current_path.exists():
                continue
            current = current_path.read_text(encoding="utf-8")

            # Render a fresh version to a side-file so the canonical prose isn't clobbered.
            fresh_path = drafts_dir / f"cluster_{cluster.cluster_id}.fresh.md"
            fresh = await renderer.render_cluster(cluster.cluster_id, force=True)
            # render_cluster overwrote the canonical file; restore the edited
            # version and keep the fresh render separately for inspection.
            fresh_path.write_text(fresh, encoding="utf-8")
            current_path.write_text(current, encoding="utf-8")
            # And restore the prose_state we trampled.
            cluster.prose_state = ProseState.edited
            self.store.save_cluster(cluster)

            sim = _jaccard(current, fresh)
            if sim < self.drift_threshold:
                drifted.append((cluster, sim))
        return drifted


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text.lower())
        if t not in _STOP
    }


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    union = ta | tb
    if not union:
        return 0.0
    return len(ta & tb) / len(union)
