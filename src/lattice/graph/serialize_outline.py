"""Serialize an AuthorGraph back to outline-markdown.

Inverse of MarkdownOutlineIngester._parse. The output is the same tag
syntax described in SPEC §4.2 — so round-tripping an annotated graph
through this serializer + the ingester yields the same graph.

Used by the `lattice annotate` command to emit a human-readable,
human-editable checkpoint file (`structure/outline.annotated.md`) that
surfaces everything the contextual annotator inferred.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import (
    AuthorGraph,
    ClaimType,
    Confidence,
    RelationshipType,
    Section,
    SectionRole,
)


def serialize_graph_to_outline(graph: AuthorGraph) -> str:
    """Return an outline-markdown string representing the graph."""
    lines: list[str] = []

    # Thesis section: emit as "# THESIS\n\n<statement>"
    thesis_claim = next((c for c in graph.claims if c.claim_id == "cl.thesis"), None)
    if thesis_claim or graph.thesis_statement:
        lines.append("# THESIS")
        lines.append("")
        lines.append((thesis_claim.statement if thesis_claim else graph.thesis_statement) or "")
        lines.append("")

    rel_by_from: dict[str, list] = {}
    for rel in graph.relationships:
        rel_by_from.setdefault(rel.from_claim, []).append(rel)

    # Non-thesis sections in position order
    body_sections = sorted(
        (s for s in graph.sections if s.section_id != "s.thesis"),
        key=lambda s: s.position,
    )
    for section in body_sections:
        lines.append(_format_section_heading(section))
        lines.append("")
        for claim_id in section.claim_ids:
            claim = next((c for c in graph.claims if c.claim_id == claim_id), None)
            if claim is None:
                continue
            lines.append(_format_claim_bullet(claim, rel_by_from.get(claim.claim_id, [])))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_annotated_outline(graph: AuthorGraph, project_path: Path) -> Path:
    """Write `structure/outline.annotated.md` for the given graph."""
    out_path = Path(project_path) / "structure" / "outline.annotated.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(serialize_graph_to_outline(graph), encoding="utf-8")
    return out_path


# ─── formatting helpers ─────────────────────────────

def _format_section_heading(section: Section) -> str:
    letter = section.section_id.removeprefix("s.").upper() or "A"
    heading = f"# {letter}. {section.title}"

    tags: list[str] = []
    # Only emit role if it's not the assembler default (argumentative).
    if section.role != SectionRole.argumentative:
        tags.append(f"[role: {section.role.value}]")
    # depth if non-default.
    if section.depth.value != "standard":
        tags.append(f"[depth: {section.depth.value}]")
    # target_length if non-default.
    if section.target_length and section.target_length != 800:
        tags.append(f"[words: {section.target_length}]")
    # (references sections already carry [role: references]; assembler skips them.)
    if tags:
        heading = f"{heading} {' '.join(tags)}"
    return heading


def _format_claim_bullet(claim, relationships) -> str:
    prefix = ""
    statement = claim.statement.strip()

    # Prefix detection: MY VIEW: / COUNTER:.
    supports_thesis = any(
        r.type == RelationshipType.supports and r.to_claim == "cl.thesis"
        for r in relationships
    )
    contradicts_thesis = any(
        r.type == RelationshipType.contradicts and r.to_claim == "cl.thesis"
        for r in relationships
    )
    if contradicts_thesis and claim.type == ClaimType.user_synthesis:
        prefix = "COUNTER: "
    elif supports_thesis and claim.type == ClaimType.user_synthesis:
        prefix = "MY VIEW: "

    tags: list[str] = []

    # [ref: ...]
    ref_sources = [ev.source for ev in claim.evidence if ev.source]
    if ref_sources:
        tags.append(f"[ref: {', '.join(ref_sources)}]")

    # [user_synthesis] only if not already prefixed by MY VIEW/COUNTER.
    if claim.type == ClaimType.user_synthesis and not prefix:
        tags.append("[user_synthesis]")

    # confidence
    if claim.confidence == Confidence.low:
        tags.append("[weak]")
    elif claim.confidence == Confidence.high and claim.type != ClaimType.user_synthesis:
        tags.append("[strong]")

    # role:X
    role_tag = next((t for t in claim.tags if t.startswith("role:")), None)
    if role_tag:
        role_val = role_tag.split(":", 1)[1]
        tags.append(f"[role: {role_val}]")

    if "skip" in claim.tags:
        tags.append("[skip]")

    # [mechanism: ...] — preserve through round-trip so author edits in
    # the annotated outline survive re-ingest.
    if claim.mechanism and claim.mechanism.strip():
        tags.append(f"[mechanism: {claim.mechanism.strip()}]")

    # [supports: ...] / [contradicts: ...] for non-thesis targets (thesis ones
    # are conveyed by the MY VIEW/COUNTER prefix already).
    other_supports = [
        r.to_claim for r in relationships
        if r.type == RelationshipType.supports and r.to_claim != "cl.thesis"
    ]
    if other_supports:
        tags.append(f"[supports: {', '.join(other_supports)}]")
    other_contradicts = [
        r.to_claim for r in relationships
        if r.type == RelationshipType.contradicts and r.to_claim != "cl.thesis"
    ]
    if other_contradicts:
        tags.append(f"[contradicts: {', '.join(other_contradicts)}]")

    body = prefix + statement
    if tags:
        body = f"{body} {' '.join(tags)}"
    return f"  - {body}"
