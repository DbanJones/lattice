"""Shadow mapper, sub-stage 2: cluster extracted claims by topic.

MVP uses token-overlap (Jaccard on content words) as the clustering signal.
Embedding-based clustering via sentence-transformers is a future
enhancement — the API is stable, only the internal similarity function
would change.
"""

from __future__ import annotations

import re
from typing import Iterable


_STOP = frozenset(
    "the a an of in on at to for and or but with by from as is are was were be "
    "been being have has had do does did this that these those it its their "
    "there which who whose what whom how when where why between among".split()
)


class ShadowClusterer:
    def __init__(self, similarity_threshold: float = 0.25) -> None:
        self.similarity_threshold = similarity_threshold

    async def cluster(self, all_claims: dict[str, list[dict]]) -> list[dict]:
        """Group claims by topic coherence.

        Input: {source_id: [{statement, passage_id, type, confidence, tags}]}
        Output: list of {topic, claim_ids (synthetic), claims (full dicts), sources}
        """
        # Flatten, stamp each claim with a synthetic ID.
        flat: list[tuple[str, dict]] = []
        for source_id, claims in all_claims.items():
            for i, claim in enumerate(claims, start=1):
                if "error" in claim:
                    continue
                claim_id = f"sc.{source_id}.{i}"
                claim = {**claim, "claim_id": claim_id, "source_id": source_id}
                flat.append((claim_id, claim))

        if not flat:
            return []

        # Greedy clustering by Jaccard overlap on content tokens.
        clusters: list[list[dict]] = []
        cluster_token_sets: list[set[str]] = []

        for _claim_id, claim in flat:
            claim_tokens = _tokens(claim["statement"])
            if not claim_tokens:
                continue
            placed = False
            for i, cluster_tokens in enumerate(cluster_token_sets):
                overlap = claim_tokens & cluster_tokens
                union = claim_tokens | cluster_tokens
                jac = len(overlap) / len(union) if union else 0.0
                if jac >= self.similarity_threshold:
                    clusters[i].append(claim)
                    cluster_token_sets[i] |= claim_tokens
                    placed = True
                    break
            if not placed:
                clusters.append([claim])
                cluster_token_sets.append(set(claim_tokens))

        # Synthesise topic labels from dominant tokens per cluster.
        result: list[dict] = []
        for i, cluster_claims in enumerate(clusters):
            topic_label = _topic_label(cluster_token_sets[i])
            result.append(
                {
                    "cluster_id": f"shadow_cluster_{i + 1}",
                    "topic": topic_label,
                    "claim_ids": [c["claim_id"] for c in cluster_claims],
                    "claims": cluster_claims,
                    "sources": sorted({c["source_id"] for c in cluster_claims}),
                }
            )
        return result


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text.lower())
        if t not in _STOP
    }


def _topic_label(token_set: set[str]) -> str:
    # Pick the longest 3 tokens as a topic hint — not semantically
    # meaningful, but stable and useful as a label.
    if not token_set:
        return "uncategorised"
    ordered = sorted(token_set, key=lambda t: (-len(t), t))
    return " ".join(ordered[:3])
