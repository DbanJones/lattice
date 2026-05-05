"""Phase 7 — version-to-version graph diff.

Computes the structural delta between two argument graphs (or
between a snapshot and current state). Powers the cockpit's
History view: "what changed between this snapshot and the next?"

Distinct from ``differ.diff.Differ`` which compares author vs
shadow graph (a different question entirely). This module is
about temporal versions of the same author graph.
"""

from __future__ import annotations

from typing import Any

from ..graph.models import (
    AuthorGraph,
    ClaimChange,
    Cluster,
    GraphDiff,
    Snapshot,
    Source,
    _ClaimFieldChange,
)


# Fields on a Claim worth tracking in a diff. Excludes timestamps
# (always change) and tags (free-form; surfaced via the field-level
# diff would noise the changelog without informing decisions).
_TRACKED_CLAIM_FIELDS: tuple[str, ...] = (
    "statement",
    "type",
    "confidence",
    "mechanism",
    "scope_conditions",
    "evidence",
    "evidence_status",
    "importance",
    "section_id",
    "author_origin",
)


def _claim_field_value(claim, field: str) -> Any:
    """Pull a comparable representation of a claim field. Pydantic
    models are dumped to dicts so equality checks work and so the
    diff JSON is human-readable in the changelog."""
    raw = getattr(claim, field, None)
    if raw is None:
        return None
    if isinstance(raw, list):
        return [
            x.model_dump(mode="json") if hasattr(x, "model_dump") else x
            for x in raw
        ]
    if hasattr(raw, "model_dump"):
        return raw.model_dump(mode="json")
    if hasattr(raw, "value"):
        return raw.value
    return raw


def _sources_state(snapshot_or_graph) -> list[str]:
    """Extract source IDs from either a Snapshot or an AuthorGraph
    (the latter doesn't carry sources directly; pass the source
    list separately when calling)."""
    if isinstance(snapshot_or_graph, Snapshot):
        return [s.source_id for s in snapshot_or_graph.sources]
    return []


def _clusters_state(snapshot_or_graph) -> list[Cluster]:
    if isinstance(snapshot_or_graph, Snapshot):
        return snapshot_or_graph.clusters
    return []


def diff_graphs(
    before: AuthorGraph | None,
    after: AuthorGraph | None,
    *,
    before_clusters: list[Cluster] | None = None,
    after_clusters: list[Cluster] | None = None,
    before_sources: list[Source] | None = None,
    after_sources: list[Source] | None = None,
) -> GraphDiff:
    """Compute the structural delta between two graphs.

    Sections, claims, relationships, clusters, and sources are
    compared by their stable ID. Claims also get a per-field diff
    on the tracked fields above so the changelog can show "this
    claim's mechanism changed" rather than just "this claim was
    modified".

    Pure function — no I/O, no LLM calls.
    """
    diff = GraphDiff()

    before_section_ids = {s.section_id for s in (before.sections if before else [])}
    after_section_ids = {s.section_id for s in (after.sections if after else [])}
    diff.sections_added = sorted(after_section_ids - before_section_ids)
    diff.sections_removed = sorted(before_section_ids - after_section_ids)

    before_claims = {c.claim_id: c for c in (before.claims if before else [])}
    after_claims = {c.claim_id: c for c in (after.claims if after else [])}
    diff.claims_added = sorted(after_claims.keys() - before_claims.keys())
    diff.claims_removed = sorted(before_claims.keys() - after_claims.keys())
    for cid in sorted(after_claims.keys() & before_claims.keys()):
        before_claim = before_claims[cid]
        after_claim = after_claims[cid]
        field_changes: list[_ClaimFieldChange] = []
        for field in _TRACKED_CLAIM_FIELDS:
            b_val = _claim_field_value(before_claim, field)
            a_val = _claim_field_value(after_claim, field)
            if b_val != a_val:
                field_changes.append(_ClaimFieldChange(
                    field=field, before=b_val, after=a_val,
                ))
        if field_changes:
            diff.claims_modified.append(ClaimChange(
                claim_id=cid,
                section_id=after_claim.section_id,
                fields=field_changes,
            ))

    before_rels = {r.rel_id for r in (before.relationships if before else [])}
    after_rels = {r.rel_id for r in (after.relationships if after else [])}
    diff.relationships_added = sorted(after_rels - before_rels)
    diff.relationships_removed = sorted(before_rels - after_rels)

    if before_sources is not None or after_sources is not None:
        b_src = {s.source_id for s in (before_sources or [])}
        a_src = {s.source_id for s in (after_sources or [])}
        diff.sources_added = sorted(a_src - b_src)
        diff.sources_removed = sorted(b_src - a_src)

    if before_clusters is not None or after_clusters is not None:
        b_cl = {c.cluster_id: c for c in (before_clusters or [])}
        a_cl = {c.cluster_id: c for c in (after_clusters or [])}
        diff.clusters_added = sorted(a_cl.keys() - b_cl.keys())
        diff.clusters_removed = sorted(b_cl.keys() - a_cl.keys())
        for cid in sorted(a_cl.keys() & b_cl.keys()):
            # A cluster is "modified" when its claim_sequence,
            # prose_state, or section_id changed. The full delta is
            # available in the cluster JSON; we just flag the ID.
            b_dump = b_cl[cid].model_dump(mode="json", exclude={"last_rendered_at", "last_rendered_hash", "last_render_token_count"})
            a_dump = a_cl[cid].model_dump(mode="json", exclude={"last_rendered_at", "last_rendered_hash", "last_render_token_count"})
            if b_dump != a_dump:
                diff.clusters_modified.append(cid)

    diff.total_changes = (
        len(diff.sections_added) + len(diff.sections_removed)
        + len(diff.claims_added) + len(diff.claims_removed) + len(diff.claims_modified)
        + len(diff.relationships_added) + len(diff.relationships_removed)
        + len(diff.sources_added) + len(diff.sources_removed)
        + len(diff.clusters_added) + len(diff.clusters_removed) + len(diff.clusters_modified)
    )
    return diff


def diff_snapshots(before: Snapshot, after: Snapshot) -> GraphDiff:
    """Convenience: diff two full snapshots, threading the embedded
    cluster + source state automatically."""
    return diff_graphs(
        before.graph, after.graph,
        before_clusters=before.clusters, after_clusters=after.clusters,
        before_sources=before.sources, after_sources=after.sources,
    )
