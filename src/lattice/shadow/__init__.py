"""Lattice shadow module: extract → cluster → architect.

Exports a single top-level ShadowMapper that orchestrates the three
sub-stages and returns a shadow AuthorGraph. Per-source extractions
are cached on the source hash, so re-running on an unchanged corpus
does no new work.
"""

from __future__ import annotations

from typing import Protocol

from ..graph.models import AuthorGraph, Source
from ..utils.config import Config
from .architect import ShadowArchitect
from .cluster import ShadowClusterer
from .extract import ShadowExtractor


class _LLMProtocol(Protocol):
    async def complete_json(
        self, system: str, user: str, model: str | None = None, temperature: float = 0.2
    ) -> tuple[object, object]: ...


class ShadowMapper:
    def __init__(self, config: Config, llm: _LLMProtocol) -> None:
        self.config = config
        self.extractor = ShadowExtractor(config, llm)
        self.clusterer = ShadowClusterer()
        self.architect = ShadowArchitect(config, llm)

    async def build(self, sources: list[Source], thesis: str) -> AuthorGraph:
        extracted = await self.extractor.extract_all(sources)
        clusters = await self.clusterer.cluster(extracted)
        return await self.architect.build(clusters, thesis)


__all__ = ["ShadowMapper", "ShadowExtractor", "ShadowClusterer", "ShadowArchitect"]
