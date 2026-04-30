"""Parallel cluster rendering with concurrency control.

For long-form documents (60+ clusters), parallel rendering is essential.

See docs/HANDOFF.md step 11.
"""
from __future__ import annotations
import asyncio
from .cluster_renderer import ClusterRenderer


class ParallelRenderer:
    def __init__(self, renderer: ClusterRenderer, max_concurrent: int = 8) -> None:
        self.renderer = renderer
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def render_all(self, cluster_ids: list[str], force: bool = False) -> dict:
        """Render every cluster in parallel up to max_concurrent.
        Returns {cluster_id: prose or Exception}.
        Failed clusters do not block others.
        """
        async def _one(cid: str):
            async with self.semaphore:
                try:
                    return await self.renderer.render_cluster(cid, force=force)
                except Exception as e:
                    return e
        results = await asyncio.gather(*[_one(cid) for cid in cluster_ids])
        return dict(zip(cluster_ids, results, strict=True))
