"""Chunked renderer: groups multiple clusters into a single LLM call.

The per-cluster renderer in ``cluster_renderer.py`` makes one LLM call per
cluster (typically 2–4 claims, ~250 words). It keeps argument-graph
traceability tight, but at the cost of context: each call sees only its
cluster's claims and the previous cluster's last sentences. For long
documents this fragments prose, hurts cross-cluster cohesion, and
multiplies subprocess overhead.

This module renders **chunks** of 8–20 clusters in a single LLM call.
Claude sees the full argument flow within the chunk, can do callbacks,
varied paragraph rhythm, and synthesis across clusters — but the output
is parsed back into per-cluster prose files so the audit, edit
proposer, and finaliser keep working unchanged.

Pipeline:
1. Pre-assess each cluster (Fix 2 dispatch). Unrenderable clusters skip
   the LLM and get a ``{CLUSTER_UNRENDERABLE}`` marker as before.
2. Group renderable clusters into chunks, respecting section boundaries.
3. For each chunk, fire one LLM call with all the cluster context.
   Claude returns JSON: ``[{cluster_id, prose}]``.
4. For each returned prose, run the standard ``validate_response`` Fix-2
   guard. Failures get the failure marker.
5. Save prose per-cluster, update prose_state per-cluster, just like the
   original renderer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import (
    BindingStrength,
    Claim,
    ClaimType,
    Cluster,
    ProseState,
    Section,
    Source,
    TokenCount,
)
from ..graph.store import GraphStore
from ..utils.config import Config
from ..utils.llm import ClaudeClient
from ..voice.parser import Voice
from .cluster_renderer import (
    ClusterRenderer,
    Renderability,
    RenderabilityAssessment,
    validate_response,
)


@dataclass
class _Chunk:
    chunk_id: str
    clusters: list[Cluster]
    section_titles: list[str]


class ChunkedRenderer:
    def __init__(
        self,
        config: Config,
        store: GraphStore,
        llm: ClaudeClient,
        voice: Voice,
        *,
        # Defaults are tuned for Claude Code's `claude -p` output budget.
        # Each cluster targets ~250 words but with the elaboration directives
        # the LLM expands well beyond that. 8-cluster chunks produced
        # truncated JSON responses; 5-cluster chunks did so intermittently
        # under aggressive elaboration. 3-4 clusters per chunk is the
        # safe default — preserves most cross-cluster cohesion while
        # leaving headroom under the standard output cap. Override via the
        # CLI flags if your model-stage budget is higher.
        min_chunk: int = 3,
        max_chunk: int = 4,
    ) -> None:
        self.config = config
        self.store = store
        self.llm = llm
        self.voice = voice
        self.min_chunk = min_chunk
        self.max_chunk = max_chunk
        self._cluster_renderer = ClusterRenderer(config, store, llm, voice)
        self.drafts_dir = (
            config.project_path / ".lattice" / "drafts" / voice.name
        )

    # ─── public entry point ────────────────────────

    async def render_all(
        self,
        force: bool = False,
        progress=None,
    ) -> dict[str, str]:
        """Render every cluster. Returns {cluster_id: prose_or_marker}.

        Order:
        1. Pre-assess each cluster (skip unrenderable, write marker).
        2. Filter out clusters whose prose is already cached and not force.
        3. Group remaining clusters into chunks.
        4. Render each chunk concurrently.

        ``progress`` is an optional callback object (see
        ``cli/progress.py::_CallbackProtocol``). When supplied, the
        renderer reports per-chunk completion as work proceeds.
        """
        all_clusters = self.store.list_clusters()
        all_clusters.sort(key=lambda c: (self._section_position(c.section_id), c.position))

        results: dict[str, str] = {}
        clusters_to_render: list[tuple[Cluster, RenderabilityAssessment]] = []

        # 1 + 2: assessment and cache filtering.
        for cluster in all_clusters:
            if (
                cluster.prose_state == ProseState.generated
                and not force
                and self._prose_path(cluster).exists()
            ):
                results[cluster.cluster_id] = self._read_prose(cluster) or ""
                continue
            assessment = self._cluster_renderer.assess_cluster_renderability(cluster)
            if assessment.state == Renderability.unrenderable:
                marker = self._cluster_renderer._handle_unrenderable(cluster, assessment)
                results[cluster.cluster_id] = marker
                continue
            clusters_to_render.append((cluster, assessment))

        if not clusters_to_render:
            return results

        # 3: chunking.
        chunks = self._build_chunks([c for c, _ in clusters_to_render])
        assessments_by_id = {c.cluster_id: a for c, a in clusters_to_render}

        if progress is not None:
            progress.begin("render", total=len(chunks))

        # 4: render each chunk. Sequential by default — we want each chunk's
        # `previous_close` to reflect the prior chunk's close. (Could go
        # parallel without inter-chunk transitions if speed mattered more.)
        previous_close = ""
        for i, chunk in enumerate(chunks, start=1):
            if progress is not None:
                progress.update_status(
                    "render",
                    f"chunk {i}/{len(chunks)} ({len(chunk.clusters)} clusters)",
                )
            chunk_results = await self._render_chunk(chunk, assessments_by_id, previous_close)
            results.update(chunk_results)
            if progress is not None:
                progress.advance("render", status=f"chunk {i}/{len(chunks)} done")
            # Pick up the close of the last successful prose for the next chunk.
            for cluster in reversed(chunk.clusters):
                prose = chunk_results.get(cluster.cluster_id, "")
                if prose and "CLUSTER_UNRENDERABLE" not in prose and "MISSING_CLAIM" not in prose:
                    previous_close = _last_sentences(prose, n=2)
                    break

        if progress is not None:
            progress.end("render", status=f"{len(chunks)} chunks rendered")

        return results

    # ─── chunk grouping ────────────────────────────

    def _build_chunks(self, clusters: list[Cluster]) -> list[_Chunk]:
        """Group clusters into chunks of [min_chunk, max_chunk], respecting
        section boundaries where possible. A section larger than max_chunk
        is split; sections smaller than min_chunk merge with neighbours.
        """
        if not clusters:
            return []

        # First, group by section.
        by_section: dict[str, list[Cluster]] = {}
        section_order: list[str] = []
        for c in clusters:
            if c.section_id not in by_section:
                section_order.append(c.section_id)
                by_section[c.section_id] = []
            by_section[c.section_id].append(c)

        chunks: list[_Chunk] = []
        current: list[Cluster] = []

        def _flush(force_split: bool = False) -> None:
            nonlocal current
            if not current:
                return
            if force_split or len(current) >= self.min_chunk:
                chunks.append(self._make_chunk(current))
                current = []

        for sid in section_order:
            section_clusters = by_section[sid]
            # If this section alone is big, split it into max-sized pieces.
            if len(section_clusters) > self.max_chunk:
                _flush(force_split=True)
                for start in range(0, len(section_clusters), self.max_chunk):
                    piece = section_clusters[start : start + self.max_chunk]
                    chunks.append(self._make_chunk(piece))
                continue

            # Otherwise tentatively add to the current chunk.
            if len(current) + len(section_clusters) > self.max_chunk:
                _flush(force_split=True)
            current.extend(section_clusters)
            if len(current) >= self.min_chunk:
                _flush()

        _flush(force_split=True)  # final trailing chunk
        return chunks

    def _make_chunk(self, clusters: list[Cluster]) -> _Chunk:
        sections_by_id = {s.section_id: s for s in self.store.list_sections()}
        section_titles: list[str] = []
        seen: set[str] = set()
        for c in clusters:
            sid = c.section_id
            if sid in seen:
                continue
            seen.add(sid)
            sec = sections_by_id.get(sid)
            section_titles.append(sec.title if sec else sid)
        chunk_id = f"chunk.{clusters[0].cluster_id}_to_{clusters[-1].cluster_id}"
        return _Chunk(chunk_id=chunk_id, clusters=clusters, section_titles=section_titles)

    # ─── per-chunk LLM call ──────────────────────

    async def _render_chunk(
        self,
        chunk: _Chunk,
        assessments_by_id: dict[str, RenderabilityAssessment],
        previous_close: str,
    ) -> dict[str, str]:
        prompt = self._build_chunk_prompt(chunk, assessments_by_id, previous_close)
        try:
            payload, response = await self.llm.complete_json(
                system=prompt["system"],
                user=prompt["user"],
                model=self.config.model_for_stage("renderer"),
                temperature=0.6,
            )
        except Exception as exc:
            return self._handle_chunk_failure(chunk, str(exc))

        cluster_count = max(1, len(chunk.clusters))
        per_cluster_input = getattr(response, "input_tokens", 0) // cluster_count
        per_cluster_output = getattr(response, "output_tokens", 0) // cluster_count

        # Expected payload: list of {cluster_id, prose}.
        prose_by_id: dict[str, str] = {}
        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                cid = str(entry.get("cluster_id") or "")
                prose = str(entry.get("prose") or "").strip()
                if cid and prose:
                    prose_by_id[cid] = prose

        results: dict[str, str] = {}
        for cluster in chunk.clusters:
            assessment = assessments_by_id.get(cluster.cluster_id)
            prose = prose_by_id.get(cluster.cluster_id, "")
            if not prose:
                marker = self._cluster_renderer._handle_render_failure(
                    cluster, [f"chunk_render_missing:{chunk.chunk_id}"]
                )
                results[cluster.cluster_id] = marker
                continue
            validation = validate_response(prose)
            if not validation.is_valid:
                marker = self._cluster_renderer._handle_render_failure(
                    cluster, validation.violations
                )
                results[cluster.cluster_id] = marker
                continue
            self._save_successful(
                cluster, prose, per_cluster_input, per_cluster_output, assessment
            )
            results[cluster.cluster_id] = prose
        return results

    def _handle_chunk_failure(self, chunk: _Chunk, reason: str) -> dict[str, str]:
        results: dict[str, str] = {}
        for cluster in chunk.clusters:
            marker = self._cluster_renderer._handle_render_failure(
                cluster, [f"chunk_call_failed:{reason[:200]}"]
            )
            results[cluster.cluster_id] = marker
        return results

    # ─── prompt construction ──────────────────────

    def _build_chunk_prompt(
        self,
        chunk: _Chunk,
        assessments_by_id: dict[str, RenderabilityAssessment],
        previous_close: str,
    ) -> dict[str, str]:
        graph = self.store.get_graph()
        claims_by_id = {c.claim_id: c for c in graph.claims}
        sources_by_id = {s.source_id: s for s in self.store.list_sources()}
        sections_by_id = {s.section_id: s for s in graph.sections}

        # System prompt: voice rules + chunked-render contract.
        system = _build_chunked_system_prompt(self.voice)

        # User prompt: list every cluster in the chunk with full context,
        # plus the previous chunk's close for transition.
        cluster_blocks: list[str] = []
        for cluster in chunk.clusters:
            assessment = assessments_by_id.get(cluster.cluster_id)
            section = sections_by_id.get(cluster.section_id)
            block = _format_cluster_block(
                cluster=cluster,
                section=section,
                claims_by_id=claims_by_id,
                sources_by_id=sources_by_id,
                assessment=assessment,
            )
            cluster_blocks.append(block)
        clusters_xml = "\n\n".join(cluster_blocks)

        chunk_word_total = sum(c.target_words_max for c in chunk.clusters)
        chunk_word_low = sum(c.target_words_min for c in chunk.clusters)

        user = f"""<chunk_context>
sections covered: {", ".join(chunk.section_titles)}
clusters in this chunk: {len(chunk.clusters)}
target word range across the chunk: {chunk_word_low}–{chunk_word_total}
architecture template: {self.voice.architecture.template}
</chunk_context>

<previous_chunk_close>
{previous_close or "This is the first chunk in the document."}
</previous_chunk_close>

<clusters>
{clusters_xml}
</clusters>

Render every cluster above as developed academic prose, in order,
applying the voice rules in the system turn.

Each cluster has a target_words band. **Hitting the target is mandatory,
not advisory.** Claude's default is to track the source length of the
claims; that produces a paper that reads as a summary of the input.
Instead, treat the claim text as the *spine* of an argument and develop
it. Specifically, for each cluster:

- Open with the claim's framing, then develop the mechanism behind it
  in 2-4 sentences (the "how" or "why" the claim holds). If the claim
  carries a `<mechanism>` block, treat that as the seed: develop and
  EXTEND it — name the operative principle, walk through the causal
  chain, address the obvious objection. Do not paraphrase the
  mechanism block; expand it. If no mechanism block is present, infer
  the mechanism from the claim and source text.
- Cite the supporting evidence with engagement — name the author in
  the sentence (Graff & Birkenstein), state the specific finding, and
  link it to the present argument with one sentence
- Where the source claim names a phenomenon, define the term for the
  reader the first time it appears, in one bracketed phrase or one
  short sentence
- For claims with role="narrative" (case study, analogy, historical
  parallel, concrete example), do not summarise: name the actors,
  give the concrete numbers or dates, walk through what happened or
  how the parallel maps. A narrative claim earns its place by being
  vivid and specific, not by being short.
- Higher-importance claims (importance >= 0.7) deserve more development
  depth — extra mechanism explanation, more concrete texture, fuller
  treatment of implications. Low-importance claims (<= 0.3) should be
  stated cleanly and moved past, not dwelt on.
- For claims tagged `arithmetic="preserve_verbatim"`, reproduce the
  step-by-step working from the source: the actual numbers, the unit
  conversions, the multiplications. Reader auditability beats prose
  flow. Do not abstract "10 Wh × 200 gCO₂/kWh = 2 g" into "the device
  draws minimal carbon"; show the calculation.
- When two adjacent claims relate via `interpretive_pivot` (provided
  in the cluster's claim_sequence relationships), render them as a
  sharp two-move structure rather than two coordinate paragraphs. The
  first claim states what the literature does; the second names the
  analytical error in that move ("Reading X as Y mistakes A for B").
  Preserve the diagnostic sentence — it is the argument, not flavour.
- Where the source claim hints at implications ("this matters because
  ..."), develop the implication in 2-3 sentences — for whom, on what
  timescale, with what magnitude
- Avoid mechanism boilerplate. If a claim's `<mechanism>` block is
  absent, do NOT manufacture a "the mechanism operates through X"
  sentence — say nothing about mechanism rather than padding with
  generic causal language.
- End each cluster on a sentence that supports the next cluster's role
  (set up the transition explicitly, do not just stop)

The output for each cluster should be **substantially longer than the
source claim text** — typically 1.5x to 2x the source length. If your
output is at or below the source length, you have summarised rather
than developed and you must expand.

Return JSON only — an array, one entry per cluster, in the same order:

[
  {{"cluster_id": "c.x.1", "prose": "..."}},
  {{"cluster_id": "c.x.2", "prose": "..."}}
]

Use cross-cluster context freely: callbacks, varied paragraph openers,
transitions between clusters in the same section. The overall chunk
should read as connected, developed academic prose.

Hard constraints (also stated in the system turn):
- every factual sentence in a cluster traces to a claim from that cluster
- if a cluster is marked PARTIAL, render the bound claims and emit
  {{MISSING_CLAIM: cluster_id, claim_id, description}} for unbound ones
- no register bleed (no addressing the user, no first person referring to
  yourself as the renderer, no "Could you", no "the constraint")
- output JSON only, no preamble, no commentary
- hit each cluster's target_words band; do not under-deliver
"""
        return {"system": system, "user": user}

    # ─── persistence helpers ──────────────────────

    def _section_position(self, section_id: str) -> int:
        for s in self.store.list_sections():
            if s.section_id == section_id:
                return s.position
        return 999

    def _prose_path(self, cluster: Cluster) -> Path:
        return self.drafts_dir / f"cluster_{cluster.cluster_id}.md"

    def _read_prose(self, cluster: Cluster) -> str | None:
        path = self._prose_path(cluster)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").rstrip("\n")

    def _save_successful(
        self,
        cluster: Cluster,
        prose: str,
        per_cluster_input_tokens: int,
        per_cluster_output_tokens: int,
        assessment: RenderabilityAssessment | None,
    ) -> None:
        path = self._prose_path(cluster)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prose + ("\n" if not prose.endswith("\n") else ""), encoding="utf-8")
        cluster.prose_state = (
            ProseState.generated
            if assessment is None or assessment.state == Renderability.full
            else ProseState.needs_review
        )
        cluster.last_rendered_at = datetime.now(timezone.utc)
        cluster.last_rendered_hash = hashlib.sha256(prose.encode("utf-8")).hexdigest()
        cluster.last_render_token_count = TokenCount(
            input=per_cluster_input_tokens,
            output=per_cluster_output_tokens,
        )
        cluster.prose_file = str(
            path.relative_to(self.config.project_path)
        )
        self.store.save_cluster(cluster)


# ─── prompt builders (chunk-aware) ─────────────

def _build_chunked_system_prompt(voice: Voice) -> str:
    voice_snapshot = {
        "name": voice.name,
        "register": voice.register.model_dump(),
        "stance": voice.stance.model_dump(),
        "citation": {
            "engagement_level": voice.citation.engagement_level,
            "synthesis_threshold": voice.citation.synthesis_threshold,
            "forbid_catalogue_pattern": voice.citation.forbid_catalogue_pattern,
            "positioning_required_for": voice.citation.positioning_required_for,
            "reporting_verbs": voice.citation.reporting_verbs.model_dump(),
        },
        "attribution": voice.attribution.model_dump(),
        "paragraph": voice.paragraph.model_dump(),
        "role_templates": voice.role_templates,
        "transitions": voice.transitions,
        "prohibitions": voice.prohibitions,
        "preferences": voice.preferences,
    }
    return f"""You render multiple consecutive clusters of an academic document
as connected prose, applying a specific voice. The clusters share a
narrative arc — your job is to render each one well *and* let them flow
into each other within the chunk.

Hard constraints:
- Every factual sentence in a cluster's prose must trace to a claim from
  that cluster's claim list.
- Apply the voice's role templates and transitions exactly.
- Apply the citation strategy: synthesise when 3+ sources cluster on a
  topic; do not produce catalogue patterns.
- Each cluster hits its target_words range; the overall chunk hits the
  total range stated in the user turn.
- Use the chunk as connected prose: vary paragraph openers, do callbacks,
  let evidence in one cluster set up the synthesis in the next.
- Open the first cluster of the chunk by picking up the previous chunk's
  closing topic when one is provided.

ABSOLUTE OUTPUT CONSTRAINT:

Your entire output is JSON. You must not produce any other content.

Forbidden in all cases (including inside the JSON prose values):
- Addressing the user (no "you", no "your", no second person)
- Asking questions in any form (rhetorical questions reproduced from
  source content are acceptable)
- Explaining what you cannot do
- Noting your reasoning, uncertainty, or limitations
- Using first person to refer to yourself ("I need", "I cannot", "let me")
- References to "the constraint", "the rule", "the prompt", "the voice"
- Any meta-commentary about the rendering process

The voice file may permit limited authorial first-person ("I argue",
"I contend", "I have observed", "in my view"). Use these only for claims
explicitly marked user_synthesis with author_origin=true.

If a cluster is marked PARTIAL in the user turn, render the bound claims
and emit a structured marker for the unbound ones, INLINE in that
cluster's prose:

{{MISSING_CLAIM: cluster_id="<id>", claim_id="<id>", description="<what was needed>"}}

Output JSON only. No preamble. No trailing prose. The schema is:

[
  {{"cluster_id": "...", "prose": "..."}},
  ...
]

<voice_rules>
{json.dumps(voice_snapshot, indent=2, default=str)}
</voice_rules>
"""


def _format_cluster_block(
    *,
    cluster: Cluster,
    section: Section | None,
    claims_by_id: dict[str, Claim],
    sources_by_id: dict[str, Source],
    assessment: RenderabilityAssessment | None,
) -> str:
    section_title = section.title if section else "(no section)"
    section_role = section.role.value if section else "unknown"
    state = "PARTIAL" if assessment and assessment.state == Renderability.partial else "FULL"

    cit = cluster.citation_strategy
    claims_xml: list[str] = []
    unbound = set(assessment.unbound_claims) if assessment else set()
    for entry in cluster.claim_sequence:
        claim = claims_by_id.get(entry.claim_id)
        if claim is None:
            continue
        bound = "false" if claim.claim_id in unbound else "true"
        evidence_parts: list[str] = []
        for ev in claim.evidence:
            source = sources_by_id.get(ev.source)
            if source is None:
                evidence_parts.append(
                    f'    <evidence source="{ev.source}" page="" '
                    f'binding="{ev.binding_strength.value}">[source not indexed]</evidence>'
                )
                continue
            passage = next((p for p in source.passages if p.id == ev.passage), None)
            passage_text = (passage.text[:600] if passage else ev.quote_text or "")
            page = (passage.location.page if passage else ev.page) or ""
            evidence_parts.append(
                f'    <evidence source="{ev.source}" page="{page}" '
                f'binding="{ev.binding_strength.value}">{passage_text}</evidence>'
            )
        evidence_xml = "\n".join(evidence_parts) or "    (no evidence bound)"
        reporting = (entry.reporting_verb or "n/a") if entry else "n/a"
        mechanism_block = (
            f"    <mechanism>{claim.mechanism}</mechanism>\n"
            if (claim.mechanism and claim.mechanism.strip())
            else ""
        )
        arithmetic_flag = (
            ' arithmetic="preserve_verbatim"' if "arithmetic" in claim.tags else ""
        )
        claims_xml.append(
            f'  <claim id="{claim.claim_id}" role="{entry.role_in_cluster.value if entry else "evidence"}" '
            f'confidence="{claim.confidence.value}" reporting_verb="{reporting}" '
            f'grounded="{bound}" type="{claim.type.value}" '
            f'author_origin="{str(claim.author_origin).lower()}" '
            f'importance="{claim.importance:.2f}"{arithmetic_flag}>\n'
            f"    Statement: {claim.statement}\n"
            f"{mechanism_block}"
            f"    Sources:\n{evidence_xml}\n"
            f"  </claim>"
        )
    claims_block = "\n".join(claims_xml) or "  (no claims)"

    return f"""<cluster id="{cluster.cluster_id}" section="{section_title}" section_role="{section_role}" role="{cluster.role.value}" target_words="{cluster.target_words_min}-{cluster.target_words_max}" state="{state}">
  <transition_in>{cluster.transition_in_hint or ''}</transition_in>
  <transition_out>{cluster.transition_out_hint or ''}</transition_out>
  <citation_strategy>
    synthesis_required: {str(cit.synthesis_required).lower()}
    positioning_required_for: {cit.positioning_required_for or "none"}
    catalogue_forbidden: {str(cit.catalogue_forbidden).lower()}
    first_mention_full: {cit.first_mention_full or "none"}
  </citation_strategy>
  <claims>
{claims_block}
  </claims>
</cluster>"""


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _last_sentences(text: str, n: int = 2) -> str:
    sentences = _SENTENCE_SPLIT_RE.split(text.strip())
    return " ".join(sentences[-n:]).strip()
