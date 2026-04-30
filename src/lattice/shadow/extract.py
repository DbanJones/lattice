"""Shadow mapper, sub-stage 1: per-source extraction.

Extracts atomic claims from each indexed source. Cached at
.lattice/cache/shadow_extractions/<source_id>.json keyed on source hash,
so re-running on an unchanged source is free.
"""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

from ..graph.models import Source
from ..utils.config import Config


class _LLMProtocol(Protocol):
    async def complete_json(
        self, system: str, user: str, model: str | None = None, temperature: float = 0.2
    ) -> tuple[object, object]: ...


_SYSTEM_PROMPT = """\
You extract atomic claims from an academic source. Each claim is one assertion in one sentence.

Rules:
- One claim per assertion. Split compound claims.
- Use the source's own language as much as possible.
- Tag each claim with the passage ID it came from.
- Classify each claim's type (empirical, methodological, normative, definition).
- Note the confidence level the source itself expresses (high if asserted directly, medium if hedged, low if speculative).

Return JSON array: [
  {
    "statement": "...",
    "passage_id": "...",
    "type": "empirical|methodological|normative|definition",
    "confidence": "high|medium|low",
    "tags": ["topic", "subtopic"]
  }
]
"""


class ShadowExtractor:
    def __init__(self, config: Config, llm: _LLMProtocol) -> None:
        self.config = config
        self.llm = llm
        self.cache_dir = config.project_path / ".lattice" / "cache" / "shadow_extractions"

    async def extract_all(self, sources: list[Source]) -> dict[str, list[dict]]:
        """Extract claims from every source in parallel.

        Returns {source_id: [claim_dicts]}. Cached per source hash.
        """
        tasks = [self.extract_one(src) for src in sources]
        results = await asyncio.gather(*tasks)
        return {src.source_id: claims for src, claims in zip(sources, results, strict=True)}

    async def extract_one(self, source: Source) -> list[dict]:
        cached = self._read_cache(source)
        if cached is not None:
            return cached

        passages = source.passages[:40]  # cap prompt size
        if not passages:
            return self._write_cache(source, [])

        citation = _format_citation(source)
        passages_xml = "\n".join(
            f'<passage id="{p.id}" page="{p.location.page or ""}">{p.text[:1500]}</passage>'
            for p in passages
        )
        user = (
            f"Source: {citation}\n\n"
            f"Passages:\n<passages>\n{passages_xml}\n</passages>\n\n"
            "Extract every atomic claim. Aim for 10-30 claims for a typical paper."
        )
        try:
            payload, _ = await self.llm.complete_json(
                system=_SYSTEM_PROMPT,
                user=user,
                model=self.config.model_for_stage("shadow_extractor"),
                temperature=0.2,
            )
        except Exception as exc:
            # Cache a minimal failure marker so we don't retry on every run,
            # but keep the marker small enough to re-run manually if needed.
            return self._write_cache(source, [{"error": f"{type(exc).__name__}: {exc}"}])

        if not isinstance(payload, list):
            payload = []
        # Keep only the fields we care about; stamp with the source_id.
        cleaned: list[dict] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            cleaned.append(
                {
                    "source_id": source.source_id,
                    "statement": str(item.get("statement", "") or "").strip(),
                    "passage_id": str(item.get("passage_id", "") or ""),
                    "type": str(item.get("type", "empirical") or "empirical"),
                    "confidence": str(item.get("confidence", "medium") or "medium"),
                    "tags": list(item.get("tags") or []),
                }
            )
        return self._write_cache(source, cleaned)

    # ─── caching ───────────────────────────────────

    def _cache_path(self, source: Source):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / f"{source.source_id}.json"

    def _read_cache(self, source: Source) -> list[dict] | None:
        path = self._cache_path(source)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if data.get("hash") != source.metadata.hash:
            return None
        return data.get("claims", [])

    def _write_cache(self, source: Source, claims: list[dict]) -> list[dict]:
        path = self._cache_path(source)
        path.write_text(
            json.dumps({"hash": source.metadata.hash, "claims": claims}, indent=2),
            encoding="utf-8",
        )
        return claims


def _format_citation(source: Source) -> str:
    cit = source.citation
    parts: list[str] = []
    if cit.authors:
        parts.append(", ".join(cit.authors[:3]) + (" et al." if len(cit.authors) > 3 else ""))
    if cit.year is not None:
        parts.append(f"({cit.year})")
    if cit.title:
        parts.append(cit.title)
    return " ".join(parts) or source.source_id
