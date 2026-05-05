"""Phase 4 — paragraph trace generator.

Walks the rendered cluster prose files under
``.lattice/drafts/<voice>/`` and emits a paragraph trace report
to ``.lattice/paragraph_traces.<voice>.json`` mapping every
paragraph and sentence back to the graph claims and source
evidence spans it derives from. The report is consumed by:

  - the auditor's coverage check (a sentence whose trace lists no
    claim_ids is an orphan)
  - the visualiser (paper-to-map highlight wiring)
  - rewrite-safety checks in Phase 5 (a rewrite that drops a
    paragraph trace's claim_ids needs an explicit author decision)

Pure derivation pass: no LLM calls, no I/O beyond reading the
existing draft prose files and writing the trace JSON. The
sentence→claim mapping uses lexical overlap against the cluster's
known claim statements — the same heuristic the coverage check
already runs, hoisted here so every consumer agrees on which
sentence belongs to which claim.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import (
    Claim,
    Cluster,
    ClusterTrace,
    Evidence,
    EvidenceSpan,
    ParagraphTrace,
    ParagraphTraceReport,
    SentenceTrace,
)
from ..graph.store import GraphStore


# Same regex the coverage check uses, kept in sync so traces and
# audits agree on sentence boundaries.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
_CONTENT_TOKEN = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_MARKER_RE = re.compile(r"\{(?:MISSING_CLAIM|CLUSTER_UNRENDERABLE)[^}]*\}")
_STOP = frozenset(
    "the a an of in on at to for and or but with by from as is are was were "
    "be been being have has had do does did this that these those it its "
    "their there which who whose what whom how when where why".split()
)


def _content_tokens(text: str) -> set[str]:
    return {t for t in (m.group(0).lower() for m in _CONTENT_TOKEN.finditer(text))
            if t not in _STOP}


def _evidence_to_span(ev: Evidence) -> EvidenceSpan:
    return EvidenceSpan(
        source_id=ev.source,
        passage_id=ev.passage,
        passage_char_start=ev.passage_char_start,
        passage_char_end=ev.passage_char_end,
        binding_strength=ev.binding_strength,
        confidence=ev.confidence,
        quote_text=ev.quote_text,
    )


def _split_paragraphs(prose: str) -> list[tuple[str, int]]:
    """Split prose into (paragraph_text, char_start_in_prose) pairs.
    Empty paragraphs are dropped; offsets are preserved into the
    original prose so consumers can highlight by char range."""
    out: list[tuple[str, int]] = []
    pos = 0
    for chunk in _PARAGRAPH_SPLIT.split(prose):
        # Find the chunk's position in the original prose, starting
        # from the cursor we last placed. ``str.find`` is O(n) but n
        # is small (a paragraph) and clusters are small (3-6 paras).
        idx = prose.find(chunk, pos)
        if idx < 0:
            idx = pos
        if chunk.strip():
            out.append((chunk, idx))
        pos = idx + len(chunk)
    return out


def _split_sentences(paragraph: str) -> list[tuple[str, int, int]]:
    """Split a paragraph into (sentence_text, start_offset, end_offset)
    triples. Offsets are within the paragraph, not the cluster prose."""
    out: list[tuple[str, int, int]] = []
    pos = 0
    parts = _SENTENCE_SPLIT.split(paragraph)
    for part in parts:
        if not part:
            continue
        idx = paragraph.find(part, pos)
        if idx < 0:
            idx = pos
        out.append((part, idx, idx + len(part)))
        pos = idx + len(part)
    return out


def build_cluster_trace(
    cluster: Cluster, prose: str, claims_by_id: dict[str, Claim],
) -> ClusterTrace:
    """Derive a ``ClusterTrace`` from a single cluster's rendered prose.

    Each sentence is mapped to the cluster claims whose statements share
    at least one content word with it, and each matched claim's evidence
    rows are surfaced as ``EvidenceSpan`` entries. Sentences containing
    a ``{MISSING_CLAIM}`` / ``{CLUSTER_UNRENDERABLE}`` marker are
    explicitly empty-traced so coverage can flag them.
    """
    cluster_claim_ids = [e.claim_id for e in cluster.claim_sequence]
    cluster_claims = [claims_by_id[c] for c in cluster_claim_ids if c in claims_by_id]

    # Pre-compute (claim_id, content_tokens) for fast per-sentence lookup.
    claim_token_index: list[tuple[str, set[str]]] = [
        (c.claim_id, _content_tokens(c.statement)) for c in cluster_claims
    ]

    paragraphs: list[ParagraphTrace] = []
    for p_idx, (para_text, _para_start) in enumerate(_split_paragraphs(prose)):
        sentences: list[SentenceTrace] = []
        for s_idx, (sent_text, s_start, s_end) in enumerate(_split_sentences(para_text)):
            stripped = sent_text.strip()
            if not stripped:
                continue
            if _MARKER_RE.search(stripped):
                # Marker sentences are intentionally untraced — the
                # coverage check picks them up via the marker regex.
                sentences.append(SentenceTrace(
                    index=s_idx, text=sent_text,
                    char_start=s_start, char_end=s_end,
                    claim_ids=[], source_ids=[], evidence_spans=[],
                ))
                continue
            tokens = _content_tokens(stripped)
            matched_claim_ids: list[str] = []
            for claim_id, claim_tokens in claim_token_index:
                if tokens & claim_tokens:
                    matched_claim_ids.append(claim_id)
            source_ids: list[str] = []
            spans: list[EvidenceSpan] = []
            for cid in matched_claim_ids:
                claim = claims_by_id.get(cid)
                if not claim:
                    continue
                for ev in claim.evidence:
                    if ev.source and ev.source not in source_ids:
                        source_ids.append(ev.source)
                    spans.append(_evidence_to_span(ev))
            sentences.append(SentenceTrace(
                index=s_idx, text=sent_text,
                char_start=s_start, char_end=s_end,
                claim_ids=matched_claim_ids,
                source_ids=source_ids,
                evidence_spans=spans,
            ))
        paragraphs.append(ParagraphTrace(
            index=p_idx, text=para_text,
            cluster_id=cluster.cluster_id,
            section_id=cluster.section_id,
            sentences=sentences,
        ))
    return ClusterTrace(
        cluster_id=cluster.cluster_id,
        section_id=cluster.section_id,
        paragraphs=paragraphs,
    )


def build_trace_report(
    project_path: Path, store: GraphStore, voice_name: str,
) -> ParagraphTraceReport:
    """Walk every rendered cluster under
    ``.lattice/drafts/<voice>/`` and produce a full report.

    Clusters without a draft file are silently skipped — the report
    only describes prose that actually exists. The caller is
    responsible for deciding when to (re)generate; this function
    always rebuilds from scratch.
    """
    drafts_dir = project_path / ".lattice" / "drafts" / voice_name
    graph = store.get_graph()
    claims_by_id = {c.claim_id: c for c in graph.claims}
    clusters = store.list_clusters()

    report = ParagraphTraceReport(
        project_name=graph.project_name,
        voice_name=voice_name,
        generated_at=datetime.now(timezone.utc),
    )

    if not drafts_dir.exists():
        return report

    for cluster in clusters:
        prose_path = drafts_dir / f"cluster_{cluster.cluster_id}.md"
        if not prose_path.exists():
            continue
        try:
            prose = prose_path.read_text(encoding="utf-8")
        except OSError:
            continue
        ctrace = build_cluster_trace(cluster, prose, claims_by_id)
        report.clusters[cluster.cluster_id] = ctrace
        for p in ctrace.paragraphs:
            report.paragraph_count += 1
            for s in p.sentences:
                report.sentence_count += 1
                if s.claim_ids:
                    report.traced_sentence_count += 1

    return report


def write_trace_report(
    report: ParagraphTraceReport, project_path: Path,
) -> Path:
    """Persist the report to ``.lattice/paragraph_traces.<voice>.json``
    and return the written path."""
    target = (
        project_path / ".lattice"
        / f"paragraph_traces.{report.voice_name}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return target


def regenerate_traces(
    project_path: Path, store: GraphStore, voice_name: str,
) -> Path | None:
    """Rebuild and persist the paragraph trace report. Convenience
    wrapper used by the finaliser. Returns the written path, or
    ``None`` when there's nothing rendered yet."""
    report = build_trace_report(project_path, store, voice_name)
    if report.paragraph_count == 0:
        return None
    return write_trace_report(report, project_path)
