"""Claim coverage checks. Critical. Default mode: rewrite.

Phase 4: when a paragraph trace exists at
``.lattice/paragraph_traces.<voice>.json``, coverage uses the trace's
sentence→claim_ids mapping as the source of truth for orphan
detection. Falls back to the lexical-overlap heuristic when no trace
is available so old projects keep working.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..graph.models import (
    AuditFlag, Cluster, FlagCategory, ParagraphTraceReport, Severity,
)
from .base import AuditCheck


_MISSING_CLAIM_RE = re.compile(r"\{MISSING_CLAIM:\s*\"([^\"]+)\"\}")
# Phase 5 — the renderer emits this marker when a claim's mechanism
# is classified as ``unknown`` (no strong evidence and no author
# origin). The audit treats it as critical: prose with an unsupported
# mechanism marker should never ship, so the marker is the renderer
# saying "I refused to smooth this into prose; the author has to
# decide what to do."
_UNRENDERABLE_MECHANISM_RE = re.compile(
    r"\{UNRENDERABLE_MECHANISM:[^}]*\}"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

# A small stop-word vocabulary used for the overlap heuristic.
_STOP = frozenset(
    "the a an of in on at to for and or but with by from as is are was were "
    "be been being have has had do does did this that these those it its "
    "their there which who whose what whom how when where why".split()
)


class CoverageCheck(AuditCheck):
    category = FlagCategory.coverage
    default_severity = Severity.critical

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        flags: list[AuditFlag] = []

        # {MISSING_CLAIM} markers always flag — these are renderer escape hatches.
        for m in _MISSING_CLAIM_RE.finditer(prose):
            flags.append(
                self._make_flag(
                    cluster=cluster,
                    rule_id="coverage.missing_claim_marker",
                    offending_text=m.group(0),
                    char_start=m.start(),
                    char_end=m.end(),
                    rule_description="Renderer emitted a MISSING_CLAIM marker.",
                    suggestion="Add the claim to the graph, then re-render the cluster.",
                )
            )

        # Phase 5 — {UNRENDERABLE_MECHANISM} markers also always flag.
        # These appear when a claim has a stated mechanism but neither
        # strong evidence nor author origin to back it; the renderer
        # refuses to smooth it into prose, so the audit must surface it.
        for m in _UNRENDERABLE_MECHANISM_RE.finditer(prose):
            flags.append(
                self._make_flag(
                    cluster=cluster,
                    rule_id="coverage.unrenderable_mechanism_marker",
                    offending_text=m.group(0),
                    char_start=m.start(),
                    char_end=m.end(),
                    rule_description=(
                        "Renderer emitted an UNRENDERABLE_MECHANISM marker — "
                        "the claim's mechanism has no strong source backing "
                        "and no author origin."
                    ),
                    suggestion=(
                        "Either bind a source passage that supports the "
                        "mechanism, mark the claim as user_synthesis with "
                        "author_origin=true, or remove the mechanism "
                        "annotation, then re-render."
                    ),
                )
            )

        # Phase 4 — prefer the persisted paragraph trace over the
        # lexical-overlap heuristic when one exists.
        trace = _load_trace_for_cluster(self, cluster.cluster_id)
        if trace is not None:
            flags.extend(self._flag_orphans_from_trace(cluster, prose, trace))
            return flags

        # Fallback: lexical-overlap heuristic against cluster claim
        # statements (preserved verbatim from before Phase 4 so projects
        # without a trace report still get coverage flags).
        claim_statements = []
        for entry in cluster.claim_sequence:
            try:
                claim = self.store.get_claim(entry.claim_id)
                claim_statements.append(claim.statement)
            except KeyError:
                continue
        if not claim_statements:
            return flags

        claim_tokens = set()
        for s in claim_statements:
            claim_tokens.update(_content_tokens(s))

        pos = 0
        for sent in _SENTENCE_SPLIT.split(prose):
            offset = prose.find(sent, pos)
            if offset < 0:
                offset = pos
            pos = offset + len(sent)
            stripped = sent.strip()
            if len(stripped) < 40:  # skip short/transition sentences
                continue
            if _MISSING_CLAIM_RE.search(stripped):
                continue
            tokens = _content_tokens(stripped)
            if not tokens:
                continue
            if not tokens & claim_tokens:
                flags.append(
                    self._make_flag(
                        cluster=cluster,
                        rule_id="coverage.orphan_sentence",
                        offending_text=stripped[:120],
                        char_start=offset,
                        char_end=offset + len(sent),
                        rule_description="Sentence does not trace to any cluster claim.",
                        suggestion="Link the sentence to a claim, or remove it.",
                    )
                )
        return flags

    def _flag_orphans_from_trace(
        self, cluster: Cluster, prose: str, trace,
    ) -> list[AuditFlag]:
        """Flag sentences whose persisted trace lists no claim_ids.
        Skips short transitions and marker sentences (handled separately
        by the {MISSING_CLAIM} regex above)."""
        out: list[AuditFlag] = []
        # Build a paragraph_index → paragraph_char_start map by walking
        # the cluster's prose so the flag offsets stay relative to the
        # full cluster (auditor consumers rely on this).
        para_offsets = _paragraph_offsets(prose)
        for para in trace.paragraphs:
            para_start = para_offsets.get(para.index, 0)
            for sent in para.sentences:
                stripped = sent.text.strip()
                if len(stripped) < 40:
                    continue
                if _MISSING_CLAIM_RE.search(stripped):
                    continue
                if sent.claim_ids:
                    continue
                offset = para_start + sent.char_start
                out.append(self._make_flag(
                    cluster=cluster,
                    rule_id="coverage.orphan_sentence",
                    offending_text=stripped[:120],
                    char_start=offset,
                    char_end=para_start + sent.char_end,
                    rule_description=(
                        "Sentence does not trace to any cluster claim "
                        "(per paragraph_traces report)."
                    ),
                    suggestion="Link the sentence to a claim, or remove it.",
                    paragraph_index=para.index,
                ))
        return out


def _content_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text.lower())
    return {t for t in tokens if t not in _STOP}


def _paragraph_offsets(prose: str) -> dict[int, int]:
    """Return a map from paragraph index to its char offset in prose,
    matching ``trace._split_paragraphs``."""
    out: dict[int, int] = {}
    paragraph_split = re.compile(r"\n\s*\n")
    pos = 0
    p_idx = 0
    for chunk in paragraph_split.split(prose):
        idx = prose.find(chunk, pos)
        if idx < 0:
            idx = pos
        if chunk.strip():
            out[p_idx] = idx
            p_idx += 1
        pos = idx + len(chunk)
    return out


def _load_trace_for_cluster(check: AuditCheck, cluster_id: str):
    """Return the ``ClusterTrace`` for ``cluster_id`` from the persisted
    paragraph_traces report if available, else None.

    Caches the parsed report on the AuditCheck instance for the
    lifetime of this audit pass so each cluster doesn't re-read the
    full report file."""
    cache = getattr(check, "_paragraph_trace_cache", None)
    if cache is not None:
        return cache.clusters.get(cluster_id) if cache else None

    # Locate the report. The store's project_path is private; fall
    # back to walking up from the GraphStore's own project_path.
    project_path = _project_path_from_store(check.store)
    if project_path is None:
        check._paragraph_trace_cache = None  # type: ignore[attr-defined]
        return None
    voice_name = getattr(check.voice, "name", None)
    if not voice_name:
        check._paragraph_trace_cache = None  # type: ignore[attr-defined]
        return None
    target = (
        Path(project_path) / ".lattice"
        / f"paragraph_traces.{voice_name}.json"
    )
    if not target.exists():
        check._paragraph_trace_cache = None  # type: ignore[attr-defined]
        return None
    try:
        report = ParagraphTraceReport.model_validate_json(
            target.read_text(encoding="utf-8")
        )
    except Exception:
        check._paragraph_trace_cache = None  # type: ignore[attr-defined]
        return None
    check._paragraph_trace_cache = report  # type: ignore[attr-defined]
    return report.clusters.get(cluster_id)


def _project_path_from_store(store) -> Path | None:
    """Best-effort: pull the project path off the GraphStore. Public
    GraphStore exposes ``project_path``; defensive in case that
    changes."""
    candidate = getattr(store, "project_path", None)
    if candidate is None:
        return None
    return Path(candidate)
