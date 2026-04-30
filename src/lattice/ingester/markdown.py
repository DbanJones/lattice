"""Markdown outline ingester — parses the SPEC §4.2 outline format.

Produces an AuthorGraph with sections, claims, and relationships from a
tag-annotated markdown outline. Deterministic for explicitly-tagged
bullets; untagged bullets default to empirical/low-confidence (a future
enhancement can invoke the LLM inference from PROMPTS.md Stage 1.6).

Section IDs:   s.<letter>                        e.g. s.c
Thesis:        s.thesis  /  cl.thesis
Claim IDs:     cl.<section_letter>.<seq>          e.g. cl.c.3
Rel IDs:       r.<seq>
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ..graph.models import (
    AuthorGraph,
    BindingStrength,
    Claim,
    ClaimType,
    Confidence,
    Depth,
    Evidence,
    Relationship,
    RelationshipStrength,
    RelationshipType,
    Section,
    SectionRole,
)
from ..utils.config import Config


# Section heading: ``# A.``, ``## A.1``, ``### A.1.2`` etc. Up to three
# levels of nesting. The hash count must match the number of components
# in the path (``A`` = 1, ``A.1`` = 2, ``A.1.2`` = 3) — mismatches are
# rejected so the author gets a parse error instead of silently mis-nested
# sections.
# Path component after the hashes can be ``A``, ``A.1``, ``A.1.2``.
# The path is optionally followed by a trailing dot (``# A. Title``,
# legacy single-letter form) but subsection paths typically don't use
# one (``## A.1 Title``). Either way, whitespace separates the path
# from the title.
_SECTION_RE = re.compile(
    r"^(#+)\s+([A-Z](?:\.\d+)*)(?:\.\s+|\s+)(.+?)\s*$"
)
_THESIS_RE = re.compile(r"^#\s*THESIS\s*$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^(\s*)-\s+(.*)$")
_TAG_RE = re.compile(r"\[([^\]]+)\]")
_FIGURE_RE = re.compile(r"^figure\s*\d+", re.IGNORECASE)

_CONFIDENCE_TAGS = {
    "weak": Confidence.low,
    "contested": Confidence.medium,
    "strong": Confidence.high,
}

_SECTION_ROLE_MAP = {
    "introduction": SectionRole.introduction,
    "argumentative": SectionRole.argumentative,
    "evidence_synthesis": SectionRole.evidence_synthesis,
    "methodological": SectionRole.methodological,
    "counterargument": SectionRole.counterargument,
    "conclusion": SectionRole.conclusion,
    "discussion": SectionRole.conclusion,  # treat discussion as conclusion role
    "appendix": SectionRole.appendix,
    "references": SectionRole.references,
}

_DEPTH_MAP = {d.value: d for d in Depth}


class MarkdownOutlineIngester:
    """Parses a tagged markdown outline into an AuthorGraph.

    The `llm` argument is accepted for future LLM-assisted claim-type
    inference but is unused in this deterministic implementation.
    """

    def __init__(self, config: Config, llm: object | None = None) -> None:
        self.config = config
        self.llm = llm

    async def ingest(self, file_path: Path, project_name: str) -> AuthorGraph:
        text = file_path.read_text(encoding="utf-8")
        return self._parse(text, project_name)

    # ─── core parse ────────────────────────────────────

    def _parse(self, text: str, project_name: str) -> AuthorGraph:
        now = datetime.now(timezone.utc)
        graph = AuthorGraph(
            project_name=project_name,
            thesis_statement=None,
            sections=[],
            claims=[],
            relationships=[],
            created_at=now,
            modified_at=now,
        )

        state: dict[str, object] = {
            "mode": None,                # "thesis" | "section" | None
            "section": None,             # current (innermost) Section
            "section_letter": None,      # legacy: top-level letter for claim-id derivation
            "open_sections": [],         # stack of Sections, deepest last; used to set parent
            "thesis_buffer": [],         # collected thesis paragraphs
            "thesis_claim_id": None,     # set once thesis is committed
            "pending_claim": None,       # dict being built
            "claim_seq_per_section": {}, # section_id -> int (was: letter -> int)
            "rel_seq": 0,
            "deferred_rels": [],         # (from_id, target, type) to resolve after pass
            "source_order_seq": 0,       # monotonic claim counter across the whole document
        }

        def _commit_pending_claim() -> None:
            pending = state["pending_claim"]
            if not pending:
                return
            section: Section | None = state["section"]
            if section is None:
                state["pending_claim"] = None
                return
            claim, rels = self._finalise_claim(pending, section, state, now)
            if claim is not None:
                graph.claims.append(claim)
                section.claim_ids.append(claim.claim_id)
                for rel in rels:
                    graph.relationships.append(rel)
            state["pending_claim"] = None

        def _commit_thesis() -> None:
            thesis_text = " ".join(s.strip() for s in state["thesis_buffer"]).strip()
            if not thesis_text:
                return
            graph.thesis_statement = thesis_text
            state["source_order_seq"] += 1
            thesis_claim = Claim(
                claim_id="cl.thesis",
                statement=thesis_text,
                source_order=state["source_order_seq"],
                type=ClaimType.user_synthesis,
                confidence=Confidence.high,
                author_origin=True,
                section_id="s.thesis",
                created_by="markdown_ingester",
                created_at=now,
                modified_at=now,
                tags=["thesis"],
            )
            graph.claims.append(thesis_claim)
            state["thesis_claim_id"] = thesis_claim.claim_id
            graph.sections.insert(
                0,
                Section(
                    section_id="s.thesis",
                    title="Thesis",
                    parent=None,
                    position=0,
                    role=SectionRole.introduction,
                    thesis_claim="cl.thesis",
                    claim_ids=["cl.thesis"],
                    target_length=0,
                    depth=Depth.standard,
                ),
            )

        lines = text.splitlines()
        for raw_line in lines:
            line = raw_line.rstrip()

            if _THESIS_RE.match(line):
                _commit_pending_claim()
                if state["mode"] == "thesis":
                    _commit_thesis()
                state["mode"] = "thesis"
                state["thesis_buffer"] = []
                state["section"] = None
                state["section_letter"] = None
                continue

            section_match = _SECTION_RE.match(line)
            if section_match:
                _commit_pending_claim()
                if state["mode"] == "thesis":
                    _commit_thesis()
                hashes = section_match.group(1)
                path = section_match.group(2)            # e.g. "A", "A.1", "A.1.2"
                raw_title = section_match.group(3)
                section_depth = len(hashes)              # 1 = top, 2 = sub, 3 = sub-sub
                path_components = path.split(".")        # ["A"], ["A", "1"], ["A", "1", "2"]
                # Skip mismatched: e.g. "## A." (depth 2, one path component) is treated
                # as a nesting bug; fall through to the legacy single-letter path so the
                # ingest doesn't crash. Older outlines using "# A." are unaffected.
                if section_depth != len(path_components):
                    # Best-effort: fall back to top-level interpretation if the path
                    # is just a single letter and the heading was deeper than expected.
                    if len(path_components) == 1 and section_depth >= 1:
                        section_depth = 1
                    else:
                        # Genuinely malformed — skip the line so we don't
                        # poison downstream sections with a phantom one.
                        continue

                title, tags = _parse_tags(raw_title)
                role = _SECTION_ROLE_MAP.get(
                    (tags.get("role") or [""])[0], SectionRole.argumentative
                )
                depth_tag = _DEPTH_MAP.get((tags.get("depth") or [""])[0], Depth.standard)
                target_length = int((tags.get("words") or ["800"])[0])

                # Derive section_id and parent. Top-level keeps the legacy
                # "s.<letter>" shape so existing projects don't change ids.
                top_letter = path_components[0]
                if section_depth == 1:
                    section_id = f"s.{top_letter.lower()}"
                    parent_id = None
                else:
                    section_id = (
                        f"s.{top_letter.lower()}." + ".".join(path_components[1:])
                    )
                    # Parent is the path with one fewer component.
                    if section_depth == 2:
                        parent_id = f"s.{top_letter.lower()}"
                    else:
                        parent_id = (
                            f"s.{top_letter.lower()}." +
                            ".".join(path_components[1:-1])
                        )

                # Pop any deeper sections off the open stack — a new
                # heading at this depth ends them.
                stack: list[Section] = state["open_sections"]  # type: ignore[assignment]
                while stack and (stack[-1].section_id.count(".") >= section_id.count(".")):
                    stack.pop()
                # If parent_id is set, the parent must be on the stack (at the
                # right depth). If it isn't, we have an out-of-order heading
                # (e.g. ``## A.1`` with no preceding ``# A.``); pretend it's
                # top-level instead.
                if parent_id is not None and (
                    not stack or stack[-1].section_id != parent_id
                ):
                    # Promote to top-level rather than dropping the section.
                    section_id = f"s.{top_letter.lower()}"
                    parent_id = None
                    section_depth = 1

                section = Section(
                    section_id=section_id,
                    title=title.strip(),
                    parent=parent_id,
                    position=len(graph.sections),
                    role=role,
                    thesis_claim=state["thesis_claim_id"],
                    claim_ids=[],
                    target_length=target_length,
                    depth=depth_tag,
                )
                graph.sections.append(section)
                stack.append(section)
                state["mode"] = "section"
                state["section"] = section
                # ``section_letter`` is kept for legacy claim-id derivation —
                # claims are always tagged with their innermost section's path,
                # but the readable letter prefix stays the top-level one.
                state["section_letter"] = top_letter
                state["claim_seq_per_section"][section_id] = 0
                continue

            bullet_match = _BULLET_RE.match(line)
            if bullet_match and state["mode"] == "section":
                _commit_pending_claim()
                body = bullet_match.group(2)
                state["pending_claim"] = {"raw": body}
                continue

            if state["mode"] == "thesis" and line.strip():
                state["thesis_buffer"].append(line.strip())
                continue

            if state["mode"] == "section" and line.strip() and state["pending_claim"]:
                # continuation of previous bullet
                pending = state["pending_claim"]
                pending["raw"] = pending["raw"] + " " + line.strip()
                continue

            # blank line terminates pending thesis buffer only if we're in thesis mode
            # (otherwise just keep state unchanged)

        # Flush at EOF
        _commit_pending_claim()
        if state["mode"] == "thesis":
            _commit_thesis()

        return graph

    # ─── finalise one claim ───────────────────────────

    def _finalise_claim(
        self,
        pending: dict,
        section: Section,
        state: dict[str, object],
        now: datetime,
    ) -> tuple[Claim | None, list[Relationship]]:
        raw = pending["raw"]
        statement_with_prefix, tags = _parse_tags(raw)

        is_counter = False
        is_my_view = False
        statement = statement_with_prefix.strip()
        stripped = statement
        if stripped.upper().startswith("COUNTER:"):
            is_counter = True
            statement = stripped.split(":", 1)[1].strip()
        elif stripped.upper().startswith("MY VIEW:"):
            is_my_view = True
            statement = stripped.split(":", 1)[1].strip()

        # Figure bullets are special — they attach to section.figure_ids, no Claim.
        if _FIGURE_RE.match(statement):
            fig_slug = _slug(statement)
            section.figure_ids.append(f"fig.{fig_slug}")
            return None, []

        # Sequence per innermost section so subsections get their own
        # claim numbering; the readable claim_id keeps the top-level
        # letter prefix so existing single-level outlines are unchanged
        # (``cl.c.1``), while nested sections get an underscore-joined
        # path (``cl.c_1.1``) — unambiguous and sorts naturally.
        innermost_id = section.section_id  # e.g. "s.c" or "s.c.1"
        state["claim_seq_per_section"].setdefault(innermost_id, 0)
        state["claim_seq_per_section"][innermost_id] += 1
        seq = state["claim_seq_per_section"][innermost_id]
        section_key = innermost_id.removeprefix("s.").replace(".", "_")
        claim_id = f"cl.{section_key}.{seq}"
        state["source_order_seq"] += 1
        claim_source_order = state["source_order_seq"]

        # type
        claim_type = ClaimType.empirical
        if is_my_view or is_counter or "user_synthesis" in tags:
            claim_type = ClaimType.user_synthesis

        # confidence
        confidence = Confidence.medium
        if claim_type == ClaimType.user_synthesis:
            confidence = Confidence.high
        for tag_name in _CONFIDENCE_TAGS:
            if tag_name in tags:
                confidence = _CONFIDENCE_TAGS[tag_name]
                break

        # evidence from [ref: ...]
        evidence: list[Evidence] = []
        for source_id in _split_tag_list(tags.get("ref")):
            evidence.append(
                Evidence(
                    source=source_id,
                    passage="",  # enricher fills
                    binding_strength=BindingStrength.weak,
                )
            )

        # role tag → stored as a claim tag (consumed by assembler later)
        claim_tags: list[str] = []
        for role in tags.get("role", []):
            claim_tags.append(f"role:{role}")
        if "skip" in tags:
            claim_tags.append("skip")
        if "central_contribution" in tags:
            claim_tags.append("central_contribution")
        if "arithmetic" in tags:
            # Tells the renderer to preserve any step-by-step arithmetic
            # in the claim/evidence verbatim rather than abstracting it.
            # Reader auditability beats prose flow for these claims.
            claim_tags.append("arithmetic")

        # Mechanism is free-text — _parse_tags splits values on commas, so
        # rejoin the parts to recover the author's original text.
        mechanism: str | None = None
        if tags.get("mechanism"):
            mechanism = ", ".join(tags["mechanism"]).strip() or None

        claim = Claim(
            claim_id=claim_id,
            statement=statement,
            mechanism=mechanism,
            source_order=claim_source_order,
            type=claim_type,
            confidence=confidence,
            evidence=evidence,
            scope_conditions=[],
            counterclaims=[],
            supporting_claims=[],
            author_origin=(claim_type == ClaimType.user_synthesis),
            section_id=section.section_id,
            created_by="markdown_ingester",
            created_at=now,
            modified_at=now,
            tags=claim_tags,
        )

        # Build relationships — dedup by (type, from, to) to avoid duplicates
        # when MY VIEW's implicit supports-thesis coincides with explicit [supports: thesis].
        rels: list[Relationship] = []
        seen_edges: set[tuple[RelationshipType, str, str]] = set()

        def _add_rel(rtype: RelationshipType, to_id: str) -> None:
            key = (rtype, claim.claim_id, to_id)
            if key in seen_edges:
                return
            seen_edges.add(key)
            state["rel_seq"] += 1
            rels.append(
                Relationship(
                    rel_id=f"r.{state['rel_seq']:03d}",
                    type=rtype,
                    **{"from": claim.claim_id, "to": to_id},
                    strength=RelationshipStrength.direct,
                    note="",
                    created_by="markdown_ingester",
                    created_at=now,
                )
            )

        thesis_id = state.get("thesis_claim_id")
        if is_my_view and thesis_id:
            _add_rel(RelationshipType.supports, thesis_id)
        if is_counter and thesis_id:
            _add_rel(RelationshipType.contradicts, thesis_id)
        for target in _split_tag_list(tags.get("supports")):
            resolved = thesis_id if target.lower() == "thesis" and thesis_id else target
            _add_rel(RelationshipType.supports, resolved)
        for target in _split_tag_list(tags.get("contradicts")):
            _add_rel(RelationshipType.contradicts, target)

        return claim, rels


# ─── tag parsing helpers ─────────────────────────────

def _parse_tags(line: str) -> tuple[str, dict[str, list[str]]]:
    """Extract [key: val, val] and [bare_flag] tags. Returns (remainder, tags)."""
    tags: dict[str, list[str]] = {}
    remainder = line

    for match in _TAG_RE.finditer(line):
        body = match.group(1).strip()
        # Accept either `[key: val]` (canonical) or `[key=val]` (the form
        # an LLM tends to emit). Whichever separator appears first wins.
        sep_index = -1
        for sep in (":", "="):
            idx = body.find(sep)
            if idx >= 0 and (sep_index == -1 or idx < sep_index):
                sep_index = idx
        if sep_index >= 0:
            key = body[:sep_index]
            val = body[sep_index + 1:]
            tags.setdefault(key.strip(), []).extend(
                [v.strip() for v in val.split(",") if v.strip()]
            )
        else:
            tags.setdefault(body, [])

    # Strip tags from the remainder
    remainder = _TAG_RE.sub("", remainder).strip()
    return remainder, tags


def _split_tag_list(values: list[str] | None) -> list[str]:
    return values or []


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w]+", "_", text.lower()).strip("_")
    return slug or "unnamed"
