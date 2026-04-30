"""Contextual annotator: LLM-assisted post-processing of a parsed outline.

The deterministic ingester (markdown.py / docx.py) extracts structure and
honours any explicit tag vocabulary. But real author scaffolds rarely use
that vocabulary. The annotator fills in the gaps. Passes (in order):

1. **Inline citations** — deterministic regex pass that pulls
   `Author (year)` / `(Author, year)` patterns out of claim prose and
   adds Evidence stubs for matched source_ids. Always runs.
2. **Thesis + section roles** — one LLM call per document. Extracts or
   synthesises the thesis statement, and classifies every section
   (bibliographies become SectionRole.references so the assembler skips
   them).
3. **Claim role + type per section** — one LLM call per renderable
   section. Assigns role_in_cluster hints (setup / evidence / mechanism
   / narrative / complication / synthesis / conclusion) and reclassifies
   first-person / opinion claims ("I classify", "I argue", "MY VIEW")
   as user_synthesis.
4. **Relationship inference** — deterministic role-chain pass plus one
   LLM call per renderable section to surface non-obvious supports /
   contradicts / qualifies / extends edges.
5. **Mechanism extraction** — batched LLM calls (60 claims per batch)
   capturing the causal middle link for analytical claims that lack one.
6. **Argued thesis + claim importance** — one LLM call per document.
   Derives the thesis the body actually argues (may diverge from the
   heading) and scores each claim's importance to that argued thesis.

If no LLM client is available, only (1) and the deterministic part of (4)
run; LLM passes are skipped silently, and model defaults remain in place
(``importance=0.5``, ``thesis_argued=None``).
"""

from __future__ import annotations

import asyncio
import re
from typing import Iterable, Protocol

from datetime import datetime, timezone

from ..graph.models import (
    AuthorGraph,
    BindingStrength,
    Claim,
    ClaimType,
    ClusterRole,
    Evidence,
    Relationship,
    RelationshipStrength,
    RelationshipType,
    Section,
    SectionRole,
)
from ..utils.config import Config


class _LLMProtocol(Protocol):
    async def complete_json(
        self, system: str, user: str, model: str | None = None, temperature: float = 0.2
    ) -> tuple[object, object]: ...


_CLAIM_ROLES = [r.value for r in ClusterRole]
_CLAIM_TYPES = [t.value for t in ClaimType]

# Citation regex: catches
#   Smith (2022)  |  Smith & Jones (2021)  |  Smith et al. (2020)
#   (Smith, 2022) |  (Smith & Jones, 2021) |  (Smith et al., 2020)
# Also handles " - 2022", "2022a", "2023b" year suffixes.
_CITATION_RE = re.compile(
    r"""
    \b
    (?P<authors>
        [A-Z][A-Za-z\-]+                                    # first author surname
        (?:\s*&\s*[A-Z][A-Za-z\-]+)?                        # optional & coauthor
        (?:\s+et\s+al\.?)?                                   # optional et al.
    )
    \s*
    [(,]                                                     # opening '(' or ', '
    \s*
    (?P<year>\d{4}[a-z]?)                                    # year (optional suffix)
    (?:[,)]|\b)
    """,
    re.VERBOSE,
)


class ContextualAnnotator:
    def __init__(self, config: Config, llm: _LLMProtocol | None) -> None:
        self.config = config
        self.llm = llm

    async def annotate(self, graph: AuthorGraph, known_source_ids: set[str]) -> AuthorGraph:
        """Enrich the graph in place. Returns the same graph for chaining."""
        # Deterministic citation extraction — always runs.
        self._extract_citations(graph, known_source_ids)

        if self.llm is None:
            self._infer_relationships_deterministic(graph)
            self._normalise_claim_order(graph)
            return graph

        # One LLM call for thesis + section role classification.
        await self._classify_document(graph)

        # One LLM call per section for claim-role / type inference,
        # skipping sections that are now flagged references.
        renderable_sections = [
            s for s in graph.sections
            if s.role != SectionRole.references and s.section_id != "s.thesis"
        ]
        await asyncio.gather(
            *[self._annotate_section(s, graph) for s in renderable_sections]
        )

        # After role inference is done, walk the role transitions to infer
        # supports/contradicts/qualifies edges within each section, plus
        # author-synthesis claims that support the document thesis.
        self._infer_relationships_deterministic(graph)

        # One more LLM call per section to surface non-obvious cross-claim
        # edges the role-chain heuristic misses.
        await asyncio.gather(
            *[self._infer_relationships_llm(s, graph) for s in renderable_sections]
        )

        # Mechanism extraction: capture the causal "how" for each
        # analytical claim that doesn't already have one. Batched.
        await self._extract_mechanisms(graph)

        # Whole-document pass: derive the actually-argued thesis (which may
        # diverge from the heading-extracted one) and assign importance
        # scores. One batched LLM call.
        await self._derive_thesis_and_importance(graph)

        # Final pass: re-sort each section's claim_ids by source_order.
        # Defensive — the LLM passes above should not reorder claim_ids,
        # but this guarantees the assembler sees source order regardless.
        self._normalise_claim_order(graph)
        return graph

    def _normalise_claim_order(self, graph: AuthorGraph) -> AuthorGraph:
        """Re-sort each section's claim_ids by Claim.source_order.

        Source order is assigned by the ingester at parse time. If any
        downstream pass mutates claim_ids out of order — or a graph from
        an external pipeline arrives unsorted — this restores the
        document order the author wrote.

        Claims with source_order == 0 (legacy graphs from before the
        field was introduced) keep their existing relative position.
        """
        order_by_id = {c.claim_id: c.source_order for c in graph.claims}
        for section in graph.sections:
            if not section.claim_ids:
                continue
            # Stable sort: zero-ordered claims keep insertion order
            # relative to each other.
            section.claim_ids = sorted(
                section.claim_ids,
                key=lambda cid: order_by_id.get(cid, 0),
            )
        return graph

    # ─── 1 + 2. Thesis + section roles (one LLM call) ──

    async def _classify_document(self, graph: AuthorGraph) -> None:
        has_explicit_thesis = bool(
            next((c for c in graph.claims if c.claim_id == "cl.thesis"), None)
        )

        # Compose a digest of the document: section titles + first claim per section.
        digest_parts: list[str] = []
        for section in graph.sections:
            if section.section_id == "s.thesis":
                continue
            first_claim = next(
                (c for c in graph.claims if c.claim_id in section.claim_ids),
                None,
            )
            snippet = (first_claim.statement[:200] if first_claim else "").strip()
            digest_parts.append(
                f'<section id="{section.section_id}" title="{section.title}">\n'
                f"  first_claim: {snippet}\n"
                f"</section>"
            )
        digest = "\n".join(digest_parts)

        current_thesis = (
            graph.thesis_statement[:400] if graph.thesis_statement else "(none extracted)"
        )

        system = (
            "You are classifying the sections of an academic document scaffold "
            "and extracting or synthesising its thesis. Return strict JSON.\n\n"
            "For each section, choose ONE role from this list:\n"
            f"{_CLAIM_ROLES}\n"
            "plus these section-only values: introduction, argumentative, "
            "evidence_synthesis, methodological, counterargument, conclusion, "
            "appendix, references.\n\n"
            "Use 'references' for bibliographies, reference lists, works-cited, "
            "acknowledgements, or anything that is not argument prose.\n\n"
            "For the thesis, if the existing thesis field reads like the "
            "document TITLE rather than an argument, derive a one-sentence "
            "thesis from the key conclusions and flag source='synthesised'.\n\n"
            "Return JSON exactly:\n"
            '{\n'
            '  "thesis": {"statement": "...", "source": "extracted|synthesised", "confidence": "high|medium|low"},\n'
            '  "sections": [{"section_id": "s.x", "role": "..."}]\n'
            '}'
        )
        user = (
            f"<current_thesis source='{'explicit' if has_explicit_thesis else 'implicit'}'>\n"
            f"{current_thesis}\n</current_thesis>\n\n"
            f"<sections>\n{digest}\n</sections>\n\n"
            "Classify every section and decide whether to keep, extract, or "
            "synthesise the thesis."
        )

        try:
            payload, _ = await self.llm.complete_json(
                system=system,
                user=user,
                model=self.config.model_for_stage("ingester"),
                temperature=0.2,
            )
        except Exception:
            return  # fall back to deterministic state

        if not isinstance(payload, dict):
            return

        thesis_info = payload.get("thesis") or {}
        if isinstance(thesis_info, dict):
            new_thesis = str(thesis_info.get("statement") or "").strip()
            source = str(thesis_info.get("source") or "").strip()
            if new_thesis and source in ("extracted", "synthesised"):
                graph.thesis_statement = new_thesis
                thesis_claim = next(
                    (c for c in graph.claims if c.claim_id == "cl.thesis"), None
                )
                if thesis_claim is not None:
                    thesis_claim.statement = new_thesis

        section_roles = payload.get("sections") or []
        if not isinstance(section_roles, list):
            return
        role_by_id: dict[str, str] = {}
        for entry in section_roles:
            if not isinstance(entry, dict):
                continue
            sid = str(entry.get("section_id") or "")
            role = str(entry.get("role") or "")
            if sid and role:
                role_by_id[sid] = role

        valid_roles = {r.value for r in SectionRole}
        for section in graph.sections:
            assigned = role_by_id.get(section.section_id)
            if assigned and assigned in valid_roles:
                section.role = SectionRole(assigned)

    # ─── 3 + 4. Per-section claim annotation ─────────

    async def _annotate_section(self, section: Section, graph: AuthorGraph) -> None:
        claims_here = [c for c in graph.claims if c.claim_id in section.claim_ids]
        if not claims_here:
            return

        claims_xml = "\n".join(
            f'<claim id="{c.claim_id}" current_type="{c.type.value}" '
            f'current_role="{_current_role_tag(c)}">{c.statement[:400]}</claim>'
            for c in claims_here
        )

        system = (
            "You classify the role and type of each claim in one section of an "
            "academic scaffold. Return strict JSON.\n\n"
            f"Valid roles (role_in_cluster): {_CLAIM_ROLES}\n"
            f"Valid claim types: {_CLAIM_TYPES}\n\n"
            "Rules:\n"
            "- A claim that opens a topic or frames the problem is 'setup'.\n"
            "- A source-grounded factual claim is 'evidence'.\n"
            "- A claim explaining how/why something happens is 'mechanism'.\n"
            "- A concrete example, case study, anecdote, historical parallel, "
            "or analogy that adds texture rather than proving a point is "
            "'narrative'. Examples: 'Phoenix vs Stockholm cooling', 'Burry's "
            "depreciation analysis', 'the 2000s telecom boom parallel'. "
            "Narrative differs from evidence: evidence proves; narrative "
            "illustrates.\n"
            "- A boundary condition is 'limit'.\n"
            "- Contrasting evidence that qualifies a main claim is 'complication'.\n"
            "- An opposing position being steelmanned is 'counterargument'.\n"
            "- A claim that consolidates earlier ones is 'synthesis'.\n"
            "- A restatement of the section's point is 'conclusion'.\n\n"
            "First-person framing ('I argue', 'I classify', 'MY VIEW', 'in my view') "
            "means claim_type = user_synthesis.\n\n"
            "Return JSON: [{\"claim_id\": \"...\", \"role\": \"...\", \"type\": \"...\"}]"
        )
        user = (
            f"<section title={section.title!r}>\n{claims_xml}\n</section>\n\n"
            "Classify every claim."
        )

        try:
            payload, _ = await self.llm.complete_json(
                system=system,
                user=user,
                model=self.config.model_for_stage("ingester"),
                temperature=0.2,
            )
        except Exception:
            return
        if not isinstance(payload, list):
            return

        by_id: dict[str, Claim] = {c.claim_id: c for c in claims_here}
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            cid = str(entry.get("claim_id") or "")
            role = str(entry.get("role") or "")
            ctype = str(entry.get("type") or "")
            claim = by_id.get(cid)
            if claim is None:
                continue
            if role in {r.value for r in ClusterRole}:
                # Replace any existing role:X tag with the inferred one.
                claim.tags = [t for t in claim.tags if not t.startswith("role:")]
                claim.tags.append(f"role:{role}")
            if ctype in {t.value for t in ClaimType}:
                # Never downgrade an author's own claim to a sourced type.
                # author_origin=True means the author wrote this themselves;
                # reclassifying to empirical/methodological would force the
                # renderer to demand evidence bindings the claim never had.
                already_authorial = (
                    claim.type == ClaimType.user_synthesis and claim.author_origin
                )
                if not already_authorial:
                    claim.type = ClaimType(ctype)
                    if claim.type == ClaimType.user_synthesis:
                        claim.author_origin = True

    # ─── 4b. Relationship inference (deterministic) ─

    def _infer_relationships_deterministic(self, graph: AuthorGraph) -> None:
        """Walk role chains within each section to infer obvious edges.

        Rules:
        - Within a section that has a `conclusion` or `synthesis` claim,
          earlier `evidence` / `setup` / `mechanism` claims `supports` it.
        - `complication` and `limit` claims `qualifies` the conclusion.
        - `counterargument` claims `contradicts` the conclusion.
        - A user_synthesis claim with role `conclusion` supports the
          document thesis (cl.thesis).
        - Sequential `evidence` → `mechanism` claims: mechanism `extends`
          evidence (the mechanism explains the evidence).
        """
        if not graph.claims:
            return
        thesis_id = next(
            (c.claim_id for c in graph.claims if c.claim_id == "cl.thesis"),
            None,
        )

        # Track existing edges so we don't duplicate.
        existing: set[tuple[str, str, str]] = {
            (r.from_claim, r.to_claim, r.type.value) for r in graph.relationships
        }
        rel_seq = len(graph.relationships)
        now = datetime.now(timezone.utc)

        def _add(
            from_id: str,
            to_id: str,
            rtype: RelationshipType,
            strength: RelationshipStrength = RelationshipStrength.inferred,
            note: str = "",
        ) -> None:
            nonlocal rel_seq
            if from_id == to_id:
                return
            key = (from_id, to_id, rtype.value)
            if key in existing:
                return
            existing.add(key)
            rel_seq += 1
            graph.relationships.append(
                Relationship(
                    rel_id=f"r.inf.{rel_seq:03d}",
                    type=rtype,
                    **{"from": from_id, "to": to_id},
                    strength=strength,
                    note=note,
                    created_by="annotator_inference",
                    created_at=now,
                )
            )

        for section in graph.sections:
            if section.section_id == "s.thesis" or section.role == SectionRole.references:
                continue
            section_claims = [
                c for c in graph.claims
                if c.claim_id in section.claim_ids
                and "skip" not in c.tags
            ]
            if not section_claims:
                continue
            roles = [_role_of_claim(c) for c in section_claims]

            # Identify the dominant target inside the section: prefer the
            # last conclusion-role claim, otherwise the last synthesis.
            target_idx: int | None = None
            for i, r in enumerate(roles):
                if r == "conclusion":
                    target_idx = i
            if target_idx is None:
                for i, r in enumerate(roles):
                    if r == "synthesis":
                        target_idx = i

            if target_idx is not None:
                target = section_claims[target_idx]
                for i, claim in enumerate(section_claims):
                    if i == target_idx:
                        continue
                    role = roles[i]
                    if role in ("setup", "evidence", "mechanism"):
                        _add(claim.claim_id, target.claim_id, RelationshipType.supports,
                             note=f"{role} supports section {role}-target")
                    elif role in ("complication", "limit"):
                        _add(claim.claim_id, target.claim_id, RelationshipType.qualifies,
                             note=f"{role} qualifies section conclusion")
                    elif role == "counterargument":
                        _add(claim.claim_id, target.claim_id, RelationshipType.contradicts,
                             note="counterargument vs section conclusion")
                    elif role == "synthesis" and i < target_idx:
                        _add(claim.claim_id, target.claim_id, RelationshipType.supports,
                             note="earlier synthesis supports later conclusion")

                # Section conclusion claim that's an author synthesis
                # supports the document thesis.
                if (
                    thesis_id
                    and target.type == ClaimType.user_synthesis
                    and target.author_origin
                ):
                    _add(target.claim_id, thesis_id, RelationshipType.supports,
                         strength=RelationshipStrength.direct,
                         note="section conclusion supports thesis")

            # Mechanism after evidence: mechanism extends evidence.
            for i in range(1, len(section_claims)):
                if roles[i] == "mechanism" and roles[i - 1] == "evidence":
                    _add(section_claims[i].claim_id, section_claims[i - 1].claim_id,
                         RelationshipType.extends,
                         note="mechanism explains preceding evidence")

    # ─── 4c. Relationship inference (LLM, per section) ──

    async def _infer_relationships_llm(
        self, section: Section, graph: AuthorGraph
    ) -> None:
        """Ask the LLM to surface non-obvious supports/contradicts edges
        within a section that the role-chain heuristic doesn't cover."""
        section_claims = [
            c for c in graph.claims
            if c.claim_id in section.claim_ids and "skip" not in c.tags
        ]
        if len(section_claims) < 2:
            return

        # Cap to keep the prompt cheap on big sections.
        section_claims = section_claims[:24]
        claims_xml = "\n".join(
            f'<claim id="{c.claim_id}" type="{c.type.value}">'
            f'{c.statement[:300]}</claim>'
            for c in section_claims
        )

        thesis_text = (graph.thesis_statement or "").strip()
        system = (
            "You identify argument relationships between claims. Be conservative: "
            "only assert a relationship you can justify from the claim text.\n\n"
            "Valid types:\n"
            "- supports: the source claim provides evidence or argument for the target claim\n"
            "- contradicts: the two claims cannot both be true\n"
            "- qualifies: the source claim adds a boundary condition or caveat to the target\n"
            "- extends: the source claim builds on or develops the target\n"
            "- interpretive_pivot: the source claim REFRAMES how the target should "
            "be read — diagnoses an interpretive error, names what the literature is "
            "confusing, or shifts which question the target answers. Use this for "
            "analytical moves like 'reading the 10⁶× gap as room for improvement "
            "mistakes distance for speed' against a target claim about the gap. "
            "Distinct from 'qualifies' (boundary condition) and 'contradicts' "
            "(denial). The renderer treats interpretive_pivot pairs as sharp "
            "analytical two-move structures.\n\n"
            "Return JSON array: "
            '[{"from": "cl.x", "to": "cl.y", "type": "supports|contradicts|qualifies|extends|interpretive_pivot"}]'
        )
        thesis_block = f"<document_thesis>cl.thesis: {thesis_text}</document_thesis>" if thesis_text else ""
        user = (
            f"Section title: {section.title!r}\n"
            f"{thesis_block}\n\n"
            f"<claims>\n{claims_xml}\n</claims>\n\n"
            "Identify relationships between these claims. Edges to cl.thesis are "
            "allowed when a claim directly supports or contradicts the document "
            "thesis. Skip relationships already obvious from sequential ordering."
        )

        try:
            payload, _ = await self.llm.complete_json(
                system=system,
                user=user,
                model=self.config.model_for_stage("ingester"),
                temperature=0.2,
            )
        except Exception:
            return
        if not isinstance(payload, list):
            return

        valid_types = {r.value for r in RelationshipType}
        valid_ids = {c.claim_id for c in graph.claims}
        existing: set[tuple[str, str, str]] = {
            (r.from_claim, r.to_claim, r.type.value) for r in graph.relationships
        }
        rel_seq = len(graph.relationships)
        now = datetime.now(timezone.utc)

        for entry in payload:
            if not isinstance(entry, dict):
                continue
            from_id = str(entry.get("from") or "")
            to_id = str(entry.get("to") or "")
            rtype = str(entry.get("type") or "").lower()
            if from_id == to_id:
                continue
            if from_id not in valid_ids or to_id not in valid_ids:
                continue
            if rtype not in valid_types:
                continue
            key = (from_id, to_id, rtype)
            if key in existing:
                continue
            existing.add(key)
            rel_seq += 1
            graph.relationships.append(
                Relationship(
                    rel_id=f"r.llm.{rel_seq:03d}",
                    type=RelationshipType(rtype),
                    **{"from": from_id, "to": to_id},
                    strength=RelationshipStrength.inferred,
                    note="annotator LLM inference",
                    created_by="annotator_inference",
                    created_at=now,
                )
            )

    # ─── 4d. Mechanism extraction (one batched LLM call) ──

    async def _extract_mechanisms(self, graph: AuthorGraph) -> None:
        """For each analytical claim, capture the causal middle link
        ("how/why this is true") in <=60 words.

        Run for claims that *do analytical work* — role in {evidence,
        mechanism, complication, narrative}. Setup, synthesis, and
        conclusion claims are framing, not mechanism. Skipped if the
        author already supplied a mechanism inline.

        One batched LLM call. Capped at 60 claims per call; chunks for
        larger documents. Skipped silently when no LLM is available."""
        if self.llm is None or not graph.claims:
            return

        analytical_roles = {"evidence", "mechanism", "complication", "narrative"}
        candidates = [
            c for c in graph.claims
            if c.claim_id != "cl.thesis"
            and "skip" not in c.tags
            and (c.mechanism is None or not c.mechanism.strip())
            and _current_role_tag(c) in analytical_roles
        ]
        if not candidates:
            return

        # Chunk to keep the prompt size bounded.
        CHUNK = 60
        chunks = [candidates[i : i + CHUNK] for i in range(0, len(candidates), CHUNK)]

        results: dict[str, str] = {}
        for chunk in chunks:
            chunk_results = await self._extract_mechanisms_chunk(chunk)
            results.update(chunk_results)

        by_id = {c.claim_id: c for c in graph.claims}
        # Phrases that signal padding — the LLM falling back on generic
        # mechanism-shaped prose when the claim doesn't actually carry a
        # mechanism. Reject these before they pollute the renderer.
        boilerplate_markers = (
            "the mechanism operates through",
            "the mechanism is straightforward",
            "operates through",
            " dynamics",  # "X dynamics", "market dynamics", etc.
            "creates a measurement illusion",
            "creates divergent",
            "compresses ",
        )
        for cid, mechanism in results.items():
            claim = by_id.get(cid)
            if claim is None:
                continue
            cleaned = mechanism.strip()
            if not cleaned:
                continue
            words = cleaned.split()
            # Length floor: 12 words minimum for a real mechanism.
            if len(words) < 12:
                continue
            lowered = cleaned.lower()
            if any(marker in lowered for marker in boilerplate_markers):
                continue
            claim.mechanism = cleaned

    async def _extract_mechanisms_chunk(
        self, claims: list[Claim]
    ) -> dict[str, str]:
        """Call the LLM once for one chunk of claims; return claim_id -> mechanism."""
        claims_xml = "\n".join(
            f'<claim id="{c.claim_id}" '
            f'role="{_current_role_tag(c) or "—"}" '
            f'type="{c.type.value}" '
            f'confidence="{c.confidence.value}">'
            f"{c.statement[:500]}</claim>"
            for c in claims
        )

        system = (
            "For each academic claim below, write the *mechanism* — the "
            "causal middle link between the claim's premise and its "
            "conclusion. Answer the question 'by what process does this "
            "hold' or 'why is this true', NOT 'what does this say'.\n\n"
            "Hard rules:\n"
            "- Return an EMPTY STRING when the claim does not carry the "
            "causal information. A claim like 'Dennard scaling broke down "
            "around 2006' does NOT imply a mechanism — it states a fact. "
            "Inferring a mechanism the claim does not contain produces "
            "Wikipedia-style boilerplate and is worse than empty.\n"
            "- Empty is the right answer for any claim where the mechanism "
            "would have to be invented from outside the claim text.\n"
            "- 30 to 60 words ONLY when you have a real mechanism to "
            "name. Never pad to hit the floor.\n"
            "- The mechanism must name a specific operative principle, a "
            "specific causal pathway, or a specific structural reason — "
            "not generic 'X dynamics' or 'Y operates through Z' "
            "constructions.\n"
            "- Do NOT restate the claim. Do NOT introduce new facts. Do "
            "NOT use the phrase 'the mechanism operates through' or "
            "'the mechanism is straightforward'.\n"
            "- Use the same register as the claim (formal, neutral).\n\n"
            "When in doubt, return empty. A missing mechanism is fixable "
            "by the author; a generic one pollutes the prose.\n\n"
            "Return strict JSON: "
            '[{"claim_id": "...", "mechanism": "..."}]'
        )
        user = (
            f"<claims count=\"{len(claims)}\">\n{claims_xml}\n</claims>\n\n"
            "Return one entry per claim_id."
        )

        try:
            payload, _ = await self.llm.complete_json(
                system=system,
                user=user,
                model=self.config.model_for_stage("ingester"),
                temperature=0.2,
            )
        except Exception:
            return {}

        if not isinstance(payload, list):
            return {}

        results: dict[str, str] = {}
        valid_ids = {c.claim_id for c in claims}
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            cid = str(entry.get("claim_id") or "")
            mech = str(entry.get("mechanism") or "")
            if cid in valid_ids and mech:
                results[cid] = mech
        return results

    # ─── 4e. Whole-document thesis + importance (one LLM call) ──

    async def _derive_thesis_and_importance(self, graph: AuthorGraph) -> None:
        """Read every claim and produce two outputs in one LLM call:

        1. The thesis the paper actually argues, derived from claim
           content rather than the heading. Stored on
           ``graph.thesis_argued`` alongside a confidence and a brief
           note on any divergence from ``graph.thesis_statement``.
        2. An importance score 0..1 for every claim, reflecting how
           central it is to the argued thesis. Stored on
           ``Claim.importance``.

        Skipped silently if no LLM client is available or if the call
        fails — the model defaults (``importance=0.5``,
        ``thesis_argued=None``) are non-blocking.
        """
        if self.llm is None or not graph.claims:
            return

        # Skip the thesis claim itself (it's the target, not a contributor).
        scoreable = [c for c in graph.claims if c.claim_id != "cl.thesis"]
        if not scoreable:
            return

        # Compose a compact digest. Keep it ordered by source_order so the
        # LLM sees the document arc, not a random shuffle.
        scoreable.sort(key=lambda c: c.source_order)
        claims_xml = "\n".join(
            f'<claim id="{c.claim_id}" '
            f'role="{_current_role_tag(c) or "—"}" '
            f'type="{c.type.value}" '
            f'section="{c.section_id or ""}">'
            f"{c.statement[:240]}</claim>"
            for c in scoreable[:240]  # hard cap for prompt size
        )

        heading_thesis = (graph.thesis_statement or "").strip() or "(none)"

        system = (
            "You are reviewing the full claim list of an academic paper.\n\n"
            "Two tasks in one response:\n\n"
            "1) Derive the thesis the paper ACTUALLY argues. This may "
            "differ from the heading thesis if the body of claims pulls in "
            "a different direction. Be precise and one sentence.\n\n"
            "2) Score each claim's importance to that argued thesis on a "
            "0.0-1.0 scale:\n"
            "   - 1.0 = central — the paper's argument collapses without it\n"
            "   - 0.7 = load-bearing — removing it weakens the case\n"
            "   - 0.5 = supporting — useful texture but replaceable\n"
            "   - 0.3 = peripheral — could be cut without loss\n"
            "   - 0.1 = padding — restates or pads existing claims\n\n"
            "Return strict JSON:\n"
            "{\n"
            '  "thesis_argued": {\n'
            '    "statement": "...",\n'
            '    "confidence": 0.0-1.0,\n'
            '    "diverges_from_heading": true|false,\n'
            '    "note": "brief explanation of any divergence (or empty)"\n'
            "  },\n"
            '  "importance": [{"claim_id": "...", "score": 0.0-1.0}]\n'
            "}"
        )
        user = (
            f"<heading_thesis>{heading_thesis}</heading_thesis>\n\n"
            f"<claims count=\"{len(scoreable)}\">\n{claims_xml}\n</claims>\n\n"
            "Return the argued thesis and a score for every claim_id listed."
        )

        try:
            payload, _ = await self.llm.complete_json(
                system=system,
                user=user,
                model=self.config.model_for_stage("ingester"),
                temperature=0.2,
            )
        except Exception:
            return

        if not isinstance(payload, dict):
            return

        thesis_info = payload.get("thesis_argued") or {}
        if isinstance(thesis_info, dict):
            statement = str(thesis_info.get("statement") or "").strip()
            if statement:
                graph.thesis_argued = statement
                conf = thesis_info.get("confidence")
                if isinstance(conf, (int, float)):
                    graph.thesis_argued_confidence = max(0.0, min(1.0, float(conf)))
                note = str(thesis_info.get("note") or "").strip()
                if note:
                    graph.thesis_argued_note = note

        importance_entries = payload.get("importance") or []
        if not isinstance(importance_entries, list):
            return
        by_id: dict[str, Claim] = {c.claim_id: c for c in graph.claims}
        for entry in importance_entries:
            if not isinstance(entry, dict):
                continue
            cid = str(entry.get("claim_id") or "")
            score = entry.get("score")
            if cid not in by_id or not isinstance(score, (int, float)):
                continue
            by_id[cid].importance = max(0.0, min(1.0, float(score)))

    # ─── 5. Inline citations (deterministic) ─────────

    def _extract_citations(
        self, graph: AuthorGraph, known_source_ids: set[str]
    ) -> None:
        # Build a lookup from "author_year" and "author" variants to source_id.
        lookup = _build_author_lookup(known_source_ids)
        for claim in graph.claims:
            found: list[Evidence] = []
            seen: set[str] = {ev.source for ev in claim.evidence}
            for match in _CITATION_RE.finditer(claim.statement):
                authors = match.group("authors")
                year = match.group("year")
                source_id = _match_to_source(authors, year, lookup)
                if source_id is None or source_id in seen:
                    continue
                seen.add(source_id)
                found.append(
                    Evidence(
                        source=source_id,
                        passage="",  # enricher will fill
                        binding_strength=BindingStrength.weak,
                    )
                )
            if found:
                claim.evidence.extend(found)


# ─── helpers ────────────────────────────────────────

def _current_role_tag(claim: Claim) -> str:
    for tag in claim.tags:
        if tag.startswith("role:"):
            return tag.split(":", 1)[1]
    return ""


def _role_of_claim(claim: Claim) -> str:
    """Best-effort role lookup with sensible defaults for untagged claims."""
    role = _current_role_tag(claim)
    if role:
        return role
    # Default inference if untagged.
    if claim.type == ClaimType.user_synthesis:
        return "synthesis"
    return "evidence"


def _build_author_lookup(source_ids: Iterable[str]) -> dict[str, str]:
    """Map candidate citation keys to source_id.

    E.g. source_id "koomey_2015" is indexed under:
      - "koomey_2015"
      - "koomey" (if unique)
    """
    sources = list(source_ids)
    lookup: dict[str, str] = {}
    # Unique prefix keys (e.g. just "koomey" if only one source starts that way).
    prefix_counts: dict[str, int] = {}
    for sid in sources:
        prefix = sid.split("_")[0].lower()
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
    for sid in sources:
        sid_lower = sid.lower()
        lookup[sid_lower] = sid
        prefix = sid_lower.split("_")[0]
        if prefix_counts.get(prefix) == 1:
            lookup[prefix] = sid
    return lookup


def _match_to_source(
    authors: str, year: str, lookup: dict[str, str]
) -> str | None:
    # Normalise: "Koomey" -> "koomey", "Mytton & Ashtine" -> ["mytton", "ashtine"]
    # "Smith et al." -> ["smith"]
    raw = authors.lower().strip()
    raw = re.sub(r"\s+et\s+al\.?", "", raw).strip()
    first_surname = re.split(r"\s*&\s*", raw)[0].strip()
    candidates = [
        f"{first_surname}_{year}",
        f"{first_surname}_{year[:4]}",  # strip trailing letter
        first_surname,
    ]
    for key in candidates:
        if key in lookup:
            return lookup[key]
    return None
