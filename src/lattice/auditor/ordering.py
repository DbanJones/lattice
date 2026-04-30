"""Ordering check: enforce source-document order through the pipeline.

Verifies three invariants that together guarantee the rendered paper
matches the order the author wrote:

1. Within each section, ``claim_ids`` is monotonic non-decreasing in
   ``Claim.source_order``. A violation means a downstream pass
   reordered claims after ingest.
2. ``graph.sections`` is monotonic non-decreasing in
   ``Section.position``. A violation means sections will render in
   the wrong order regardless of cluster ordering.
3. Within each section, claims grouped into the same cluster occupy a
   contiguous source-order span. A violation means cluster boundaries
   interleave claims from different parts of the source — the symptom
   that produced the Gap-4-heading-after-content bug.

Legacy graphs (where every claim has ``source_order == 0``) are skipped
with a single advisory flag rather than producing one per section.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..graph.models import (
    AuditFlag,
    AuthorGraph,
    Cluster,
    EditMode,
    FlagCategory,
    ProseLocation,
    Severity,
)
from ..graph.store import GraphStore
from ..voice.parser import Voice


_DEFAULT_LOCATION = ProseLocation(paragraph_index=0, char_start=0, char_end=0)


@dataclass
class OrderingReport:
    is_ordered: bool
    flags: list[AuditFlag] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _short_uid() -> str:
    return uuid.uuid4().hex[:6]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OrderingCheck:
    def __init__(self, store: GraphStore, voice: Voice) -> None:
        self.store = store
        self.voice = voice

    def check(self) -> OrderingReport:
        graph = self.store.get_graph()
        clusters = self.store.list_clusters()
        flags: list[AuditFlag] = []
        notes: list[str] = []

        if self._is_legacy_graph(graph):
            notes.append(
                "All claims have source_order=0 — graph predates the "
                "ordering field. Re-ingest to enable strict order checks."
            )
            return OrderingReport(is_ordered=True, flags=flags, notes=notes)

        flags.extend(self._check_sections_monotonic(graph))
        flags.extend(self._check_claim_ids_monotonic(graph))
        flags.extend(self._check_clusters_contiguous(graph, clusters))

        return OrderingReport(
            is_ordered=not flags,
            flags=flags,
            notes=notes,
        )

    # ─── invariants ──────────────────────────────────

    def _is_legacy_graph(self, graph: AuthorGraph) -> bool:
        return all(c.source_order == 0 for c in graph.claims)

    def _check_sections_monotonic(self, graph: AuthorGraph) -> list[AuditFlag]:
        positions = [s.position for s in graph.sections]
        if positions == sorted(positions):
            return []
        return [self._mk_flag(
            rule_id="ordering.sections_out_of_order",
            cluster_id="",
            section_id="",
            description=(
                f"graph.sections is not in monotonic position order: {positions}. "
                "The final paper will render sections in list order, not "
                "position order, so this guarantees an out-of-order document."
            ),
            suggestion=(
                "Re-ingest the source document, or sort graph.sections by "
                "position before persisting."
            ),
        )]

    def _check_claim_ids_monotonic(self, graph: AuthorGraph) -> list[AuditFlag]:
        order_by_id = {c.claim_id: c.source_order for c in graph.claims}
        flags: list[AuditFlag] = []
        for section in graph.sections:
            orders = [
                order_by_id.get(cid, 0)
                for cid in section.claim_ids
                if cid in order_by_id
            ]
            if orders == sorted(orders):
                continue
            # Find first inversion to surface in the flag.
            inversion_at: int | None = None
            for i in range(1, len(orders)):
                if orders[i] < orders[i - 1]:
                    inversion_at = i
                    break
            offending = ", ".join(section.claim_ids[: (inversion_at or len(section.claim_ids))][-3:])
            flags.append(self._mk_flag(
                rule_id="ordering.claim_ids_out_of_order",
                cluster_id="",
                section_id=section.section_id,
                offending_text=offending,
                description=(
                    f"Section {section.title!r} has claim_ids out of source order. "
                    f"Sequence near inversion: {offending}. "
                    "The renderer will produce content in this order, which "
                    "may place introductory claims after their elaborations."
                ),
                suggestion=(
                    "Re-run annotation, which calls _normalise_claim_order at "
                    "the end. If the order is correct in the source markdown "
                    "but wrong here, a downstream pass mutated claim_ids."
                ),
            ))
        return flags

    def _check_clusters_contiguous(
        self, graph: AuthorGraph, clusters: list[Cluster]
    ) -> list[AuditFlag]:
        order_by_id = {c.claim_id: c.source_order for c in graph.claims}
        flags: list[AuditFlag] = []

        clusters_by_section: dict[str, list[Cluster]] = {}
        for cluster in clusters:
            clusters_by_section.setdefault(cluster.section_id, []).append(cluster)

        for section_id, section_clusters in clusters_by_section.items():
            section_clusters_sorted = sorted(section_clusters, key=lambda c: c.position)
            spans: list[tuple[str, int, int]] = []  # (cluster_id, min_order, max_order)
            for cluster in section_clusters_sorted:
                orders = [
                    order_by_id.get(entry.claim_id, 0)
                    for entry in cluster.claim_sequence
                    if entry.claim_id in order_by_id
                ]
                if not orders:
                    continue
                spans.append((cluster.cluster_id, min(orders), max(orders)))

            for prev, curr in zip(spans, spans[1:]):
                prev_id, _, prev_max = prev
                curr_id, curr_min, _ = curr
                if curr_min < prev_max:
                    flags.append(self._mk_flag(
                        rule_id="ordering.clusters_interleaved",
                        cluster_id=curr_id,
                        section_id=section_id,
                        description=(
                            f"Cluster {curr_id} starts at source_order={curr_min} "
                            f"but the preceding cluster {prev_id} extends to "
                            f"source_order={prev_max}. Cluster spans should not "
                            "overlap — this means claims from different parts "
                            "of the source were grouped non-contiguously."
                        ),
                        suggestion=(
                            "Re-run `lattice plan` after ensuring claim_ids "
                            "are in source order; the assembler builds clusters "
                            "by walking claim_ids sequentially."
                        ),
                    ))
        return flags

    # ─── helper ──────────────────────────────────────

    def _mk_flag(
        self,
        *,
        rule_id: str,
        cluster_id: str,
        section_id: str,
        description: str,
        suggestion: str,
        severity: Severity = Severity.critical,
        default_mode: EditMode = EditMode.rewrite,
        offending_text: str = "",
    ) -> AuditFlag:
        return AuditFlag(
            flag_id=f"f.ordering.{_short_uid()}",
            category=FlagCategory.architecture,
            rule_id=rule_id,
            severity=severity,
            default_mode=default_mode,
            cluster_id=cluster_id,
            section_id=section_id,
            prose_location=_DEFAULT_LOCATION,
            offending_text=offending_text,
            rule_description=description,
            suggestion=suggestion,
            voice_name=self.voice.name,
            created_at=_now(),
        )
