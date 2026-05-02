"""Per-cluster renderer.

Three-state dispatch (Fix 2 of the pipeline-integrity brief):

- ``full``  — every claim has strong / weak binding, OR is a
  user_synthesis claim with ``author_origin=True`` (author-grounded,
  doesn't need a source). Render normally.
- ``partial`` — some claims grounded, some not. Render the grounded
  ones, emit ``{MISSING_CLAIM:...}`` markers for gaps. Cluster goes
  to ``prose_state=needs_review``.
- ``unrenderable`` — fewer than half the claims grounded, or fewer
  than 2 grounded claims total. Skip the LLM call entirely. Write a
  single ``{CLUSTER_UNRENDERABLE:...}`` marker. Cluster goes to
  ``prose_state=failed``.

Output is then validated for register bleed (the renderer addressing
the user, asking questions, refusing the task in prose) and for
prohibited meta-commentary. A failed validation marks the cluster as
``prose_state=failed`` rather than caching a bad render.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
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


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


# ─── Renderability ──────────────────────────────────

class Renderability(str, Enum):
    full = "full"
    partial = "partial"
    unrenderable = "unrenderable"


@dataclass
class RenderabilityAssessment:
    state: Renderability
    bound_claims: list[str] = field(default_factory=list)
    unbound_claims: list[str] = field(default_factory=list)
    rationale: str = ""


# ─── Response validation ────────────────────────────

@dataclass
class RenderValidation:
    is_valid: bool
    violations: list[str] = field(default_factory=list)


# Patterns where a renderer is improvising / addressing the user / refusing.
# These are caught in rendered prose and cause the cluster to be re-marked
# failed rather than persisted as a clean render.
_REGISTER_BLEED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bI need to\b", re.IGNORECASE), "first_person_imperative"),
    (re.compile(r"\bI cannot\b", re.IGNORECASE), "renderer_giving_up"),
    (re.compile(r"\bI can'?t\b", re.IGNORECASE), "renderer_giving_up"),
    (re.compile(r"\bI'?m not sure\b", re.IGNORECASE), "renderer_uncertain"),
    (re.compile(r"\bI'?m unable to\b", re.IGNORECASE), "renderer_giving_up"),
    (re.compile(r"\bCould you\b", re.IGNORECASE), "question_to_user"),
    (re.compile(r"\bplease clarify\b", re.IGNORECASE), "request_for_clarification"),
    (re.compile(r"\blet me know\b", re.IGNORECASE), "user_addressing"),
    (re.compile(r"\bbefore proceeding\b", re.IGNORECASE), "process_meta_comment"),
    (re.compile(r"\bthe constraint\b", re.IGNORECASE), "process_meta_comment"),
    # NOTE: `the rule` was previously flagged but caused false positives
    # in legal / judicial / linguistic prose where it is a normal noun
    # phrase. The other patterns ("the prompt", "the voice",
    # "the constraint", "before proceeding") are stronger renderer-
    # meta-commentary signals on their own.
    (re.compile(r"\bthe prompt\b", re.IGNORECASE), "process_meta_comment"),
    (re.compile(r"\bthe voice file\b", re.IGNORECASE), "process_meta_comment"),
]

# Authorial first-person framings the academic voice permits — these are NOT
# violations (they're how user_synthesis claims should render under the
# explicit_opinion stance).
_ALLOWED_AUTHORIAL_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bI argue\b", re.IGNORECASE),
    re.compile(r"\bI contend\b", re.IGNORECASE),
    re.compile(r"\bI have observed\b", re.IGNORECASE),
    re.compile(r"\bin my view\b", re.IGNORECASE),
    re.compile(r"\bI propose\b", re.IGNORECASE),
    re.compile(r"\bI find\b", re.IGNORECASE),
    re.compile(r"\bI classify\b", re.IGNORECASE),
]

_QUESTION_RE = re.compile(r"[^.!?]*\?")


def validate_response(prose: str) -> RenderValidation:
    """Detect register bleed / forbidden meta-commentary in rendered prose.

    Standalone (not a method) so the same validator can run as a guard at
    render time *and* as a deterministic readiness check on stored prose.
    """
    if not prose or not prose.strip():
        return RenderValidation(is_valid=False, violations=["empty_response"])

    violations: list[str] = []

    # 1. Register-bleed pattern scan, with allowed-authorial exemption.
    for pattern, kind in _REGISTER_BLEED_PATTERNS:
        for match in pattern.finditer(prose):
            # Skip the match if it sits inside an allowed authorial frame
            # (e.g. "I argue that the rule is broken" — "the rule" wouldn't
            # be a violation in that context).
            if kind == "process_meta_comment":
                window_start = max(0, match.start() - 30)
                window = prose[window_start : match.end() + 30]
                if any(p.search(window) for p in _ALLOWED_AUTHORIAL_PATTERNS):
                    continue
            violations.append(f"{kind}:{match.group(0).strip()!r}")

    # 2. User-addressing questions — questions that point at the reader,
    # not rhetorical questions reproduced from source material. The voice
    # auditor flags rhetorical-question prohibitions separately at audit
    # time; this validator only kills register bleed.
    prose_no_markers = re.sub(r"\{(?:MISSING_CLAIM|CLUSTER_UNRENDERABLE)[^}]*\}", "", prose)
    prose_no_quotes = re.sub(r'"[^"]*"', "", prose_no_markers)
    for sentence in _SENTENCE_SPLIT_RE.split(prose_no_quotes):
        stripped = sentence.strip()
        if not stripped.endswith("?"):
            continue
        if re.search(r"\b(you|your|let me know|please)\b", stripped, re.IGNORECASE):
            violations.append(f"user_addressing_question:{stripped[:60]!r}")
            break

    return RenderValidation(is_valid=not violations, violations=violations)


# ─── ClusterRenderer ────────────────────────────────

class ClusterRenderer:
    def __init__(
        self,
        config: Config,
        store: GraphStore,
        llm: ClaudeClient,
        voice: Voice,
    ) -> None:
        self.config = config
        self.store = store
        self.llm = llm
        self.voice = voice
        self.drafts_dir = (
            config.project_path / ".lattice" / "drafts" / voice.name
        )

    # ─── public entry point ────────────────────────

    async def render_cluster(self, cluster_id: str, force: bool = False) -> str:
        cluster = self.store.get_cluster(cluster_id)

        # Cache hit only when prior render is in `generated` state. Partial
        # renders (`needs_review`) and failures (`failed`) re-evaluate.
        if cluster.prose_state == ProseState.generated and not force:
            existing = self._read_existing(cluster)
            if existing is not None:
                return existing

        assessment = self.assess_cluster_renderability(cluster)

        if assessment.state == Renderability.unrenderable:
            return self._handle_unrenderable(cluster, assessment)

        prompt = self._build_prompt(cluster, assessment)
        try:
            response = await self.llm.complete(
                system=prompt["system"],
                user=prompt["user"],
                model=self.config.model_for_stage("renderer"),
                temperature=0.6,
                max_tokens=2000,
            )
        except Exception as exc:
            return self._handle_render_failure(cluster, [f"llm_error:{exc}"])

        prose = (response.text or "").strip()
        validation = validate_response(prose)
        if not validation.is_valid:
            return self._handle_render_failure(cluster, validation.violations)

        return self._save_successful_render(cluster, prose, response, assessment)

    # ─── renderability assessment ──────────────────

    def assess_cluster_renderability(
        self, cluster: Cluster
    ) -> RenderabilityAssessment:
        """Decide if the cluster can be rendered before invoking the LLM.

        A claim is "grounded" if it has at least one evidence binding of
        strength strong/weak, OR it is a user_synthesis claim with
        ``author_origin=True`` (the author's own observation, no source
        needed).
        """
        bound: list[str] = []
        unbound: list[str] = []

        for entry in cluster.claim_sequence:
            try:
                claim = self.store.get_claim(entry.claim_id)
            except KeyError:
                unbound.append(entry.claim_id)
                continue
            if self._is_grounded(claim):
                bound.append(claim.claim_id)
            else:
                unbound.append(claim.claim_id)

        total = len(bound) + len(unbound)
        if total == 0:
            return RenderabilityAssessment(
                state=Renderability.unrenderable,
                bound_claims=bound,
                unbound_claims=unbound,
                rationale="cluster has no claims",
            )

        if not unbound:
            return RenderabilityAssessment(
                state=Renderability.full,
                bound_claims=bound,
                unbound_claims=[],
                rationale="all claims grounded",
            )

        bound_pct = len(bound) / total
        if bound_pct < 0.5 or len(bound) < 2:
            return RenderabilityAssessment(
                state=Renderability.unrenderable,
                bound_claims=bound,
                unbound_claims=unbound,
                rationale=(
                    f"only {len(bound)}/{total} claims grounded "
                    f"({bound_pct:.0%}); needs >=50% AND >=2 bound"
                ),
            )

        return RenderabilityAssessment(
            state=Renderability.partial,
            bound_claims=bound,
            unbound_claims=unbound,
            rationale=f"{len(unbound)}/{total} claims unbound; will emit MISSING_CLAIM markers",
        )

    @staticmethod
    def _is_grounded(claim: Claim) -> bool:
        # Author's own observation — no source needed. author_origin is
        # the load-bearing signal here; the type may legitimately be
        # empirical or methodological for an authorial claim that states
        # a fact the author derived themselves.
        if claim.author_origin:
            return True
        # Otherwise: at least one strong/weak evidence binding required.
        return any(
            ev.binding_strength in (BindingStrength.strong, BindingStrength.weak)
            for ev in claim.evidence
        )

    # ─── failure handlers ──────────────────────────

    def _handle_unrenderable(
        self, cluster: Cluster, assessment: RenderabilityAssessment
    ) -> str:
        marker = (
            f'{{CLUSTER_UNRENDERABLE: cluster_id="{cluster.cluster_id}", '
            f'reason="{assessment.rationale}", '
            f'unbound_claims="{",".join(assessment.unbound_claims)}"}}'
        )
        self._save_prose(cluster, marker)
        cluster.prose_state = ProseState.failed
        cluster.last_rendered_at = datetime.now(timezone.utc)
        cluster.last_rendered_hash = hashlib.sha256(marker.encode("utf-8")).hexdigest()
        cluster.prose_file = str(
            self._prose_path(cluster).relative_to(self.config.project_path)
        )
        self.store.save_cluster(cluster)
        return marker

    def _handle_render_failure(
        self, cluster: Cluster, violations: list[str]
    ) -> str:
        violations_str = "; ".join(violations[:5])
        marker = (
            f'{{CLUSTER_UNRENDERABLE: cluster_id="{cluster.cluster_id}", '
            f'reason="render produced register bleed or invalid output: '
            f'{violations_str}"}}'
        )
        self._save_prose(cluster, marker)
        cluster.prose_state = ProseState.failed
        cluster.last_rendered_at = datetime.now(timezone.utc)
        cluster.last_rendered_hash = hashlib.sha256(marker.encode("utf-8")).hexdigest()
        cluster.prose_file = str(
            self._prose_path(cluster).relative_to(self.config.project_path)
        )
        self.store.save_cluster(cluster)
        return marker

    def _save_successful_render(
        self,
        cluster: Cluster,
        prose: str,
        response,
        assessment: RenderabilityAssessment,
    ) -> str:
        self._save_prose(cluster, prose)
        cluster.prose_state = (
            ProseState.generated
            if assessment.state == Renderability.full
            else ProseState.needs_review
        )
        cluster.last_rendered_at = datetime.now(timezone.utc)
        cluster.last_rendered_hash = hashlib.sha256(prose.encode("utf-8")).hexdigest()
        cluster.last_render_token_count = TokenCount(
            input=getattr(response, "input_tokens", 0),
            output=getattr(response, "output_tokens", 0),
        )
        cluster.prose_file = str(
            self._prose_path(cluster).relative_to(self.config.project_path)
        )
        self.store.save_cluster(cluster)
        return prose

    # ─── prompt construction ───────────────────────

    def _build_prompt(
        self, cluster: Cluster, assessment: RenderabilityAssessment
    ) -> dict[str, str]:
        graph = self.store.get_graph()
        sections_by_id = {s.section_id: s for s in graph.sections}
        claims_by_id = {c.claim_id: c for c in graph.claims}
        sources_by_id = {s.source_id: s for s in self.store.list_sources()}

        section = sections_by_id.get(cluster.section_id)
        claims = [
            claims_by_id[entry.claim_id]
            for entry in cluster.claim_sequence
            if entry.claim_id in claims_by_id
        ]

        system = _build_system_prompt(self.voice)
        user = _build_user_prompt(
            cluster=cluster,
            section=section,
            claims=claims,
            claim_entries=cluster.claim_sequence,
            sources_by_id=sources_by_id,
            previous_close=self._read_previous_close(cluster),
            next_cluster=self._lookup_next(cluster),
            architecture_template=self.voice.architecture.template,
            assessment=assessment,
        )
        return {"system": system, "user": user}

    def _read_previous_close(self, cluster: Cluster) -> str:
        if not cluster.previous_cluster:
            return ""
        try:
            prev = self.store.get_cluster(cluster.previous_cluster)
        except KeyError:
            return ""
        # Don't pull in markers as "context".
        if prev.prose_state in (ProseState.failed, ProseState.not_yet_rendered):
            return ""
        path = self._prose_path(prev)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8").strip()
        if "CLUSTER_UNRENDERABLE" in text or "MISSING_CLAIM" in text:
            return ""
        sentences = _SENTENCE_SPLIT_RE.split(text)
        return " ".join(sentences[-2:]).strip()

    def _lookup_next(self, cluster: Cluster) -> Cluster | None:
        if not cluster.next_cluster:
            return None
        try:
            return self.store.get_cluster(cluster.next_cluster)
        except KeyError:
            return None

    # ─── persistence ──────────────────────────────

    def _prose_path(self, cluster: Cluster) -> Path:
        return self.drafts_dir / f"cluster_{cluster.cluster_id}.md"

    def _save_prose(self, cluster: Cluster, prose: str) -> None:
        path = self._prose_path(cluster)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prose + ("\n" if not prose.endswith("\n") else ""), encoding="utf-8")

    def _read_existing(self, cluster: Cluster) -> str | None:
        path = self._prose_path(cluster)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").rstrip("\n")


# ─── prompt builders ────────────────────────────────

def _build_system_prompt(voice: Voice) -> str:
    """Cached system prompt: voice rules that don't change per cluster."""
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
    prohibitions_list = _format_prohibitions(voice.prohibitions)

    return f"""You render a cluster of claims as one or two paragraphs of academic prose, applying a specific voice.

Hard constraints:
- Every factual sentence must trace to a claim from the list provided in the user turn.
- Apply the voice's role templates and transitions exactly.
- Apply the citation strategy. If synthesis is required, write a synthesis paragraph. Do not produce catalogue patterns (three or more sequential single-source citations).
- Match the target word count.
- The opening sentence picks up the previous cluster's closing topic when one is provided.
- The closing sentence supports the next cluster's role.

ABSOLUTE OUTPUT CONSTRAINT:

Your entire output is the prose for this cluster. You must not produce any
other content under any circumstances.

Forbidden in all cases:
- Addressing the user (no "you", no "your", no second person)
- Asking questions in any form
- Explaining what you cannot do
- Noting your reasoning, uncertainty, or limitations
- Using first person to refer to yourself ("I need", "I cannot", "let me")
- References to "the constraint", "the rule", "the prompt", "the voice"
- Any meta-commentary about the rendering process

The voice file may permit limited authorial first-person ("I argue",
"I contend", "I have observed", "in my view"). Use these only for claims
explicitly marked user_synthesis with author_origin=true. Never use first
person to refer to yourself as the renderer.

If a claim cannot be rendered with the constraints provided, your only
valid non-prose output is a structured marker, used INLINE at the position
where the prose would have appeared:

{{MISSING_CLAIM: cluster_id="<id>", claim_id="<id>", description="<what was needed>"}}

No other commentary is permitted in any form. If you have already been told
(via the partial-render dispatch in the user turn) that some claims are
unbound, render the bound claims and emit markers for the unbound ones.
Do not refuse the whole cluster.

Violation of this constraint causes your output to be discarded and
re-rendered with stricter instructions.

<voice_rules>
{json.dumps(voice_snapshot, indent=2, default=str)}
</voice_rules>

<prohibitions_summary>
{prohibitions_list}
</prohibitions_summary>
"""


def _build_user_prompt(
    *,
    cluster: Cluster,
    section: Section | None,
    claims: list[Claim],
    claim_entries,
    sources_by_id: dict[str, Source],
    previous_close: str,
    next_cluster: Cluster | None,
    architecture_template: str,
    assessment: RenderabilityAssessment,
) -> str:
    claims_xml = _format_claims_xml(claims, claim_entries, sources_by_id, assessment)
    cit = cluster.citation_strategy
    section_title = section.title if section else "(no section)"
    section_role = section.role.value if section else "unknown"

    prev_block = previous_close or "This is the first cluster in its section."
    next_role = next_cluster.role.value if next_cluster else "none (last cluster)"

    if assessment.state == Renderability.partial:
        partial_note = (
            f"\n<render_mode>PARTIAL: claims [{', '.join(assessment.unbound_claims)}] "
            "are unbound. Render the bound ones and emit "
            "{MISSING_CLAIM: cluster_id, claim_id, description} for the "
            "unbound ones at their nominal positions. Do not refuse the "
            "whole cluster.</render_mode>\n"
        )
    else:
        partial_note = "\n<render_mode>FULL: every claim is grounded.</render_mode>\n"

    relationships_block = _format_relationship_context(cluster)

    return f"""<section_context>
Section: {section_title}
Section role: {section_role}
Architecture template: {architecture_template}
</section_context>

<cluster_role>
Cluster role: {cluster.role.value}
Target words: {cluster.target_words_min}-{cluster.target_words_max}
</cluster_role>

<previous_cluster_close>
{prev_block}
</previous_cluster_close>
{partial_note}
<claims>
{claims_xml}
</claims>

<relationships>
{relationships_block}
</relationships>

<citation_strategy>
synthesis_required: {str(cit.synthesis_required).lower()}
synthesis_target_claims: {cit.synthesis_target_claims or "none"}
positioning_required_for: {cit.positioning_required_for or "none"}
catalogue_forbidden: {str(cit.catalogue_forbidden).lower()}
first_mention_full: {cit.first_mention_full or "none"}
</citation_strategy>

<transition_out>
Next cluster's role: {next_role}
Hint: {cluster.transition_out_hint or "End on an emphatic sentence."}
</transition_out>

Render the cluster now. **Hitting the target_words band is mandatory, not
advisory.** Treat the claim text as the *spine* of an argument and develop it,
do not summarise the source. Specifically:

- Open with the claim's framing, then develop the mechanism behind it
  in 2-4 sentences (the "how" or "why" the claim holds). If a `<mechanism>`
  block is provided on a claim, treat it as the seed: develop and EXTEND
  it — name the operative principle, walk through the causal chain,
  address the obvious objection. Do not paraphrase the mechanism block;
  expand it. If no mechanism block is present, infer the mechanism from
  the claim and source text.
- Cite the supporting evidence with engagement — name the author in the
  sentence, state the specific finding, and link it to the present
  argument with one sentence.
- Where the source claim names a phenomenon, define the term for the
  reader the first time it appears, in one bracketed phrase or one
  short sentence.
- For claims with role="narrative" (case study, analogy, historical
  parallel, concrete example), do not summarise: name the actors, give
  the concrete numbers or dates, walk through what happened or how the
  parallel maps. A narrative claim earns its place by being vivid and
  specific, not by being short.
- Higher-importance claims (importance >= 0.7) deserve more development
  depth. Low-importance claims (<= 0.3) should be stated cleanly and
  moved past, not dwelt on.
- Where the source claim hints at implications, develop them in 2-3
  sentences — for whom, on what timescale, with what magnitude.
- For claims tagged `arithmetic="preserve_verbatim"`, reproduce the
  step-by-step working from the source: the actual numbers, the unit
  conversions, the multiplications. Reader auditability beats prose
  flow. Do not abstract "10 Wh × 200 gCO₂/kWh = 2 g" into "the device
  draws minimal carbon"; show the calculation.
- When a claim relates to the next via `interpretive_pivot`, render
  them as a sharp two-move structure rather than two coordinate
  paragraphs. The first claim states what the literature does; the
  second names the analytical error in that move ("Reading X as Y
  mistakes A for B"). Preserve the diagnostic sentence — it is the
  argument, not flavour.
- End on a sentence that supports the next cluster's role; set up the
  transition explicitly, do not just stop.

The output should be **substantially longer than the source claim text** —
typically 1.5x to 2x the source length. If your output is at or below the
source length, you have summarised rather than developed and you must
expand.

Output prose only, no commentary.
"""


def _format_claims_xml(
    claims: list[Claim],
    claim_entries,
    sources_by_id: dict[str, Source],
    assessment: RenderabilityAssessment,
) -> str:
    entries_by_id = {e.claim_id: e for e in claim_entries}
    unbound_set = set(assessment.unbound_claims)
    blocks: list[str] = []
    for claim in claims:
        entry = entries_by_id.get(claim.claim_id)
        role = entry.role_in_cluster.value if entry else "evidence"
        reporting = (entry.reporting_verb if entry else None) or "n/a"
        bound_flag = "false" if claim.claim_id in unbound_set else "true"
        evidence_parts: list[str] = []
        for ev in claim.evidence:
            source = sources_by_id.get(ev.source)
            if source is None:
                evidence_parts.append(
                    f'    <evidence source="{ev.source}" page="" binding="{ev.binding_strength.value}">'
                    f'[source not indexed]</evidence>'
                )
                continue
            passage = next(
                (p for p in source.passages if p.id == ev.passage), None
            )
            passage_text = (passage.text[:800] if passage else ev.quote_text or "")
            page = (passage.location.page if passage else ev.page) or ""
            evidence_parts.append(
                f'    <evidence source="{ev.source}" page="{page}" '
                f'binding="{ev.binding_strength.value}">{passage_text}</evidence>'
            )
        evidence_xml = "\n".join(evidence_parts) or "    (no evidence bound)"
        mechanism_block = (
            f"  <mechanism>{claim.mechanism}</mechanism>\n"
            if (claim.mechanism and claim.mechanism.strip())
            else ""
        )
        arithmetic_flag = (
            ' arithmetic="preserve_verbatim"' if "arithmetic" in claim.tags else ""
        )
        blocks.append(
            f'<claim id="{claim.claim_id}" role="{role}" '
            f'confidence="{claim.confidence.value}" reporting_verb="{reporting}" '
            f'grounded="{bound_flag}" type="{claim.type.value}" '
            f'author_origin="{str(claim.author_origin).lower()}" '
            f'importance="{claim.importance:.2f}"{arithmetic_flag}>\n'
            f"  Statement: {claim.statement}\n"
            f"{mechanism_block}"
            f"  Sources:\n{evidence_xml}\n"
            f"</claim>"
        )
    return "\n".join(blocks) or "(no claims)"


def _format_relationship_context(cluster: Cluster) -> str:
    """Render the cluster's ``relationship_context`` as a bullet list the
    LLM can read alongside the ``<claims>`` block. Intra-cluster edges
    come first (they shape paragraph structure); incoming edges next
    (they constrain the opening); outgoing edges last (they constrain
    the close). Edges with ``affects_rendering=False`` are omitted —
    they're metadata for the diagram, not directives for the renderer."""
    if not cluster.relationship_context:
        return "(no recorded relationships touching this cluster)"
    intra = [r for r in cluster.relationship_context if r.direction == "intra" and r.affects_rendering]
    incoming = [r for r in cluster.relationship_context if r.direction == "incoming" and r.affects_rendering]
    outgoing = [r for r in cluster.relationship_context if r.direction == "outgoing" and r.affects_rendering]

    def _row(rel) -> str:
        target = (
            f" → cluster {rel.other_cluster_id}" if rel.other_cluster_id else ""
        )
        note = f" — {rel.note}" if rel.note else ""
        return (
            f"  - {rel.from_claim} -[{rel.type.value} ({rel.strength.value})]-> "
            f"{rel.to_claim}{target}{note}"
        )

    sections: list[str] = []
    if intra:
        sections.append("intra-cluster (use these to shape the paragraph structure):")
        sections.extend(_row(r) for r in intra)
    if incoming:
        sections.append("incoming (the opening should pick these up):")
        sections.extend(_row(r) for r in incoming)
    if outgoing:
        sections.append("outgoing (the close should set these up):")
        sections.extend(_row(r) for r in outgoing)
    if not sections:
        return "(no rendering-affecting relationships)"
    return "\n".join(sections)


def _format_prohibitions(prohibitions: list) -> str:
    lines: list[str] = []
    for p in prohibitions:
        if isinstance(p, str):
            lines.append(f"- {p}")
        elif isinstance(p, dict):
            for key in ("word", "phrase", "pattern"):
                if key in p:
                    lines.append(f"- {key}: {p[key]}")
                    break
    return "\n".join(lines[:60])
