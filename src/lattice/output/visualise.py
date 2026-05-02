"""Visualise the argument scaffold.

Three views, all derived from the same in-memory AuthorGraph:

- **Rich tree** rendered to the terminal — an at-a-glance hierarchical
  view: thesis → sections → clusters → claims, with role/type tags and
  outgoing relationships shown inline.
- **Mermaid flowchart** written to `outputs/argument_graph.mmd` — opens
  natively in VSCode markdown previews and pastes into mermaid.live.
  Sections become subgraphs, claims become nodes, relationships become
  labelled edges.
- **Standalone HTML** at `outputs/argument_graph.html` using cytoscape.js
  for interactive layout — open in any browser, no install.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from rich.console import Console
from rich.tree import Tree

from ..graph.models import (
    AuthorGraph,
    BindingStrength,
    ClaimType,
    Cluster,
    EvidenceStatus,
    ProseState,
    RelationshipType,
    SectionRole,
)


_TYPE_ICON = {
    ClaimType.empirical: "[E]",
    ClaimType.user_synthesis: "[USER]",
    ClaimType.methodological: "[M]",
    ClaimType.normative: "[N]",
    ClaimType.definition: "[D]",
}

_REL_GLYPH = {
    RelationshipType.supports: "supports",
    RelationshipType.contradicts: "contradicts",
    RelationshipType.qualifies: "qualifies",
    RelationshipType.extends: "extends",
    RelationshipType.depends_on: "depends_on",
    RelationshipType.is_counterexample_to: "counterexample_to",
    RelationshipType.is_evidence_for: "is_evidence_for",
    RelationshipType.unlabelled: "?",
}


# ─── Rich tree (terminal) ─────────────────────────

def render_tree(
    graph: AuthorGraph,
    clusters: list[Cluster] | None = None,
    console: Console | None = None,
) -> None:
    console = console or Console()
    # Prefer the argued thesis (derived from the full claim list) over the
    # heading thesis. Show both when they diverge, so the author sees the
    # gap between what they wrote at the top and what the body argues.
    argued = (graph.thesis_argued or "").strip()
    heading = (graph.thesis_statement or "").strip()
    primary = argued or heading or graph.project_name or "(no thesis)"
    if len(primary) > 100:
        primary = primary[:100] + "..."

    rels_by_from: dict[str, list] = {}
    for rel in graph.relationships:
        rels_by_from.setdefault(rel.from_claim, []).append(rel)

    cluster_lookup: dict[str, list[Cluster]] = {}
    for c in clusters or []:
        cluster_lookup.setdefault(c.section_id, []).append(c)

    thesis_label = (
        f"[bold cyan]Thesis (argued):[/bold cyan] {primary}"
        if argued else f"[bold cyan]Thesis:[/bold cyan] {primary}"
    )
    root = Tree(thesis_label)
    if argued and heading and argued != heading:
        # Surface divergence prominently — the heading thesis is preserved
        # as a child node so the author can compare them at a glance.
        heading_short = heading if len(heading) <= 100 else heading[:100] + "..."
        root.add(f"[yellow]Heading thesis:[/yellow] {heading_short}")
        if graph.thesis_argued_note:
            root.add(f"[dim]drift: {graph.thesis_argued_note}[/dim]")
    for section in graph.sections:
        if section.section_id == "s.thesis":
            continue
        skipped = section.role == SectionRole.references
        section_label = (
            f"[bold]{section.section_id}[/bold]  "
            f"{section.title}  "
            f"[dim]({section.role.value}, {len(section.claim_ids)} claims"
        )
        if section.section_id in cluster_lookup:
            section_label += f", {len(cluster_lookup[section.section_id])} clusters"
        section_label += ")[/dim]"
        if skipped:
            section_label += "  [yellow]\\[SKIPPED][/yellow]"
        section_node = root.add(section_label)

        if skipped:
            continue

        for cluster in sorted(cluster_lookup.get(section.section_id, []), key=lambda c: c.position):
            cluster_node = section_node.add(
                f"[blue]{cluster.cluster_id}[/blue]  "
                f"[dim]role={cluster.role.value}, target={cluster.target_words_min}-{cluster.target_words_max}w[/dim]"
            )
            claim_ids_in_cluster = {e.claim_id for e in cluster.claim_sequence}
            for entry in cluster.claim_sequence:
                claim = next(
                    (c for c in graph.claims if c.claim_id == entry.claim_id), None
                )
                if claim is None:
                    continue
                _add_claim_to_tree(cluster_node, claim, rels_by_from)

        # Also surface claims not in any cluster (rare, but possible).
        cluster_claim_ids = {
            e.claim_id
            for c in cluster_lookup.get(section.section_id, [])
            for e in c.claim_sequence
        }
        leftover = [
            cid for cid in section.claim_ids if cid not in cluster_claim_ids
        ]
        if leftover and not cluster_lookup.get(section.section_id):
            for cid in leftover:
                claim = next(
                    (c for c in graph.claims if c.claim_id == cid), None
                )
                if claim:
                    _add_claim_to_tree(section_node, claim, rels_by_from)

    console.print(root)


def _add_claim_to_tree(parent_node, claim, rels_by_from):
    role_tag = next((t.split(":", 1)[1] for t in claim.tags if t.startswith("role:")), "")
    type_icon = _TYPE_ICON.get(claim.type, "[?]")
    statement = claim.statement.strip()
    if len(statement) > 80:
        statement = statement[:80] + "..."
    tag_str = f"[{role_tag}]" if role_tag else ""
    label = (
        f"[green]{claim.claim_id}[/green] "
        f"[magenta]{type_icon}[/magenta] "
        f"[dim]{tag_str}[/dim] "
        f"{statement}"
    )
    if claim.evidence:
        sources = ", ".join(ev.source for ev in claim.evidence if ev.source)
        if sources:
            label += f"  [dim italic]<- {sources}[/dim italic]"
    claim_node = parent_node.add(label)

    for rel in rels_by_from.get(claim.claim_id, []):
        glyph = _REL_GLYPH.get(rel.type, rel.type.value)
        claim_node.add(f"[yellow]--{glyph}-->[/yellow] {rel.to_claim}")


# ─── Mermaid flowchart ───────────────────────────

def render_mermaid(graph: AuthorGraph) -> str:
    """Produce a Mermaid flowchart string."""
    lines: list[str] = ["```mermaid", "flowchart TB"]

    # Style classes for node types and section roles.
    lines.append("    classDef thesis fill:#fef3c7,stroke:#f59e0b,stroke-width:2px,color:#000")
    lines.append("    classDef userSynthesis fill:#dbeafe,stroke:#3b82f6,color:#000")
    lines.append("    classDef empirical fill:#f3f4f6,stroke:#6b7280,color:#000")
    lines.append("    classDef references fill:#fee2e2,stroke:#dc2626,color:#000")
    lines.append("")

    # Thesis node first. Prefer the argued thesis (derived from the full
    # claim list); fall back to the heading thesis. When they diverge,
    # show both.
    thesis_id = "thesis"
    argued = (graph.thesis_argued or "").strip()
    heading = (graph.thesis_statement or "").strip()
    if argued or heading or any(c.claim_id == "cl.thesis" for c in graph.claims):
        primary = argued or heading or ""
        thesis_text = _short(primary, 120)
        label_inner = f"<b>THESIS</b><br/>{_safe(thesis_text)}"
        if argued and heading and argued != heading:
            label_inner += (
                f"<br/><i>(heading: {_safe(_short(heading, 80))})</i>"
            )
        lines.append(f'    {thesis_id}[/"{label_inner}"/]:::thesis')
        lines.append("")

    claim_node_ids: dict[str, str] = {"cl.thesis": thesis_id}

    for section in graph.sections:
        if section.section_id == "s.thesis":
            continue
        sid = _safe_id(section.section_id)
        is_refs = section.role == SectionRole.references
        title = _safe(_short(f"{section.title}", 80))
        suffix = " [SKIPPED]" if is_refs else ""
        lines.append(f'    subgraph {sid}["{title}{suffix}"]')
        lines.append(f'        direction TB')
        for cid in section.claim_ids:
            claim = next((c for c in graph.claims if c.claim_id == cid), None)
            if claim is None:
                continue
            node_id = _safe_id(claim.claim_id)
            claim_node_ids[claim.claim_id] = node_id
            label = _claim_label(claim)
            shape_open, shape_close = (
                ("(", ")") if claim.type == ClaimType.user_synthesis else ("[", "]")
            )
            class_name = (
                "references" if is_refs else
                "userSynthesis" if claim.type == ClaimType.user_synthesis
                else "empirical"
            )
            lines.append(f'        {node_id}{shape_open}"{label}"{shape_close}:::{class_name}')
        lines.append(f'    end')
        if is_refs:
            lines.append(f'    class {sid} references')
        lines.append("")

    # Edges
    for rel in graph.relationships:
        from_node = claim_node_ids.get(rel.from_claim)
        to_node = claim_node_ids.get(rel.to_claim)
        if not from_node or not to_node:
            continue
        edge_label = _REL_GLYPH.get(rel.type, rel.type.value)
        # Choose arrow style by relationship type.
        if rel.type == RelationshipType.contradicts:
            arrow = f"-.{edge_label}.->"
        else:
            arrow = f"--{edge_label}-->"
        lines.append(f"    {from_node} {arrow} {to_node}")

    lines.append("```")
    return "\n".join(lines) + "\n"


def write_mermaid(graph: AuthorGraph, project_path: Path) -> Path:
    out = Path(project_path) / "outputs" / "argument_graph.mmd"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_mermaid(graph), encoding="utf-8")
    return out


# ─── Standalone HTML (Cytoscape.js) ───────────────

_SECTION_PALETTE = [
    "#dbeafe", "#dcfce7", "#fef3c7", "#fce7f3", "#ede9fe",
    "#fed7aa", "#cffafe", "#f1f5f9", "#fae8ff", "#d1fae5",
]


# ─── Phase 3: enriched visualisation payload ──────


def _evidence_quality(claim) -> str:
    """Reduce a claim's evidence list to a single quality bucket the
    diagram can colour-code."""
    if claim.evidence_status == EvidenceStatus.bound:
        return "bound"
    if claim.evidence_status == EvidenceStatus.source_hint:
        return "source_hint"
    if claim.evidence_status == EvidenceStatus.unbound:
        return "unbound"
    if not claim.evidence:
        return "unbound" if not claim.author_origin else "author"
    strengths = [ev.binding_strength for ev in claim.evidence]
    if any(s == BindingStrength.contradictory for s in strengths):
        return "contradictory"
    if any(s == BindingStrength.strong for s in strengths):
        return "bound"
    if any(s == BindingStrength.weak for s in strengths):
        return "source_hint"
    return "unbound"


def _claim_has_unrenderable_marker(prose_path: Path | None) -> bool:
    if prose_path is None or not prose_path.exists():
        return False
    try:
        text = prose_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "{MISSING_CLAIM" in text or "{CLUSTER_UNRENDERABLE" in text


def build_visualisation_payload(
    graph: AuthorGraph,
    clusters: list[Cluster] | None = None,
    *,
    drafts_dir: Path | None = None,
    audit_flags_by_cluster: dict[str, int] | None = None,
    readiness_blocking_clusters: set[str] | None = None,
    section_palette: list[str] | None = None,
) -> dict:
    """Build the full payload the cytoscape diagram (and any other
    consumer) reads. Pure function over the inputs — no I/O beyond an
    optional ``drafts_dir`` peek for unrenderable markers.

    Top-level keys:

    - ``meta``: counts + lattice version stamp
    - ``sections``: hierarchy (parent, depth, role, color, counts)
    - ``clusters``: per-cluster data (section_id, role, prose_state,
      target_words, claim_count, audit_flag_count, blocks_readiness,
      has_unrenderable_marker, evidence_summary)
    - ``claims``: per-claim data (type, role, importance, mechanism,
      scope_conditions, evidence_status, evidence_quality, evidence,
      cluster_id, dirty_since_last_render, has_unrenderable_marker)
    - ``relationships``: edges (source, target, type, strength, note)
    """
    palette = section_palette or _SECTION_PALETTE
    audit_counts = audit_flags_by_cluster or {}
    blocking = readiness_blocking_clusters or set()
    cluster_list = clusters or []

    # Section colour mapping — top-level sections get a palette colour;
    # subsections inherit from their top-level ancestor.
    by_section_id = {s.section_id: s for s in graph.sections}
    renderable_sections = [
        s for s in graph.sections if s.role != SectionRole.references
    ]
    top_level = [s for s in renderable_sections if not s.parent and s.section_id != "s.thesis"]
    top_colors = {
        s.section_id: palette[i % len(palette)]
        for i, s in enumerate(top_level)
    }

    def _top_ancestor(sid: str) -> str:
        cur = by_section_id.get(sid)
        while cur is not None and cur.parent:
            cur = by_section_id.get(cur.parent)
        return cur.section_id if cur else sid

    section_colors = {
        s.section_id: top_colors.get(_top_ancestor(s.section_id), palette[0])
        for s in renderable_sections
    }
    section_colors["s.thesis"] = "#fef3c7"

    # Cluster index for quick claim → cluster lookup, and per-cluster
    # claim counts.
    cluster_for_claim: dict[str, str] = {}
    cluster_summaries: list[dict] = []
    for cluster in cluster_list:
        for entry in cluster.claim_sequence:
            cluster_for_claim[entry.claim_id] = cluster.cluster_id
        prose_path = (
            (drafts_dir / f"cluster_{cluster.cluster_id}.md")
            if drafts_dir is not None else None
        )
        cluster_summaries.append({
            "id": cluster.cluster_id,
            "section_id": cluster.section_id,
            "role": cluster.role.value,
            "position": cluster.position,
            "prose_state": cluster.prose_state.value,
            "target_words_min": cluster.target_words_min,
            "target_words_max": cluster.target_words_max,
            "claim_count": len(cluster.claim_sequence),
            "audit_flag_count": int(audit_counts.get(cluster.cluster_id, 0)),
            "blocks_readiness": cluster.cluster_id in blocking,
            "has_unrenderable_marker": _claim_has_unrenderable_marker(prose_path),
            "is_dirty": cluster.prose_state == ProseState.dirty,
            "is_failed": cluster.prose_state == ProseState.failed,
        })

    # Sections payload (preserve top-down position order).
    sections_payload: list[dict] = []
    for s in graph.sections:
        depth = (
            0 if s.section_id == "s.thesis"
            else max(0, s.section_id.count(".") - 1)
        )
        cluster_count = sum(
            1 for c in cluster_list if c.section_id == s.section_id
        )
        sections_payload.append({
            "id": s.section_id,
            "title": s.title,
            "parent": s.parent,
            "depth": depth,
            "role": s.role.value,
            "color": section_colors.get(s.section_id, palette[0]),
            "claim_count": len(s.claim_ids),
            "cluster_count": cluster_count,
            "is_excluded": s.role == SectionRole.references,
        })

    # Claims payload — enriched with evidence status, mechanism, scope.
    claims_payload: list[dict] = []
    for claim in graph.claims:
        section = by_section_id.get(claim.section_id) if claim.section_id else None
        is_excluded = section is not None and section.role == SectionRole.references
        cluster_id = cluster_for_claim.get(claim.claim_id)
        has_unrenderable = False
        if cluster_id and drafts_dir is not None:
            has_unrenderable = _claim_has_unrenderable_marker(
                drafts_dir / f"cluster_{cluster_id}.md"
            )
        # Role tag is stored as `role:<value>` in claim.tags.
        role_value = next(
            (t.split(":", 1)[1] for t in claim.tags if t.startswith("role:")),
            "",
        )
        evidence_summary = [
            {
                "source": ev.source,
                "binding": ev.binding_strength.value,
                "page": ev.page,
            }
            for ev in claim.evidence
        ]
        claims_payload.append({
            "id": claim.claim_id,
            "statement": claim.statement,
            "type": claim.type.value,
            "confidence": claim.confidence.value,
            "author_origin": claim.author_origin,
            "section_id": claim.section_id,
            "cluster_id": cluster_id,
            "role": role_value,
            "importance": float(claim.importance),
            "mechanism": claim.mechanism,
            "scope_conditions": list(claim.scope_conditions),
            "evidence_status": (
                claim.evidence_status.value if claim.evidence_status else None
            ),
            "evidence_quality": _evidence_quality(claim),
            "evidence": evidence_summary,
            "color": section_colors.get(claim.section_id or "", palette[0]),
            "tags": list(claim.tags),
            "is_excluded": is_excluded,
            "has_unrenderable_marker": has_unrenderable,
        })

    # Relationship edges — drop edges whose endpoints are in excluded
    # sections (they wouldn't render anyway).
    excluded_claim_ids = {
        c["id"] for c in claims_payload if c["is_excluded"]
    }
    relationships_payload: list[dict] = []
    for rel in graph.relationships:
        if rel.from_claim in excluded_claim_ids or rel.to_claim in excluded_claim_ids:
            continue
        relationships_payload.append({
            "id": rel.rel_id,
            "source": rel.from_claim,
            "target": rel.to_claim,
            "type": rel.type.value,
            "strength": rel.strength.value,
            "note": rel.note or "",
            "created_by": rel.created_by,
        })

    return {
        "meta": {
            "project_name": graph.project_name,
            "thesis_statement": graph.thesis_statement,
            "thesis_argued": graph.thesis_argued,
            "section_count": len(sections_payload),
            "cluster_count": len(cluster_summaries),
            "claim_count": len(claims_payload),
            "relationship_count": len(relationships_payload),
        },
        "sections": sections_payload,
        "clusters": cluster_summaries,
        "claims": claims_payload,
        "relationships": relationships_payload,
    }


def render_html(
    graph: AuthorGraph,
    clusters: list[Cluster] | None = None,
    *,
    drafts_dir: Path | None = None,
    audit_flags_by_cluster: dict[str, int] | None = None,
    readiness_blocking_clusters: set[str] | None = None,
) -> str:
    """Self-contained HTML page with cytoscape.js loading the graph as JSON.

    When ``clusters`` is provided, the diagram includes cluster compound
    nodes and the filter sidebar shows cluster-state filters. When
    ``audit_flags_by_cluster`` / ``readiness_blocking_clusters`` are
    provided, claims inside flagged or blocked clusters get badges.

    For backwards compatibility, all the new arguments are optional —
    callers that pass only ``graph`` get the original flat-node diagram
    (with the enriched per-claim data still attached).
    """
    enriched = build_visualisation_payload(
        graph,
        clusters=clusters,
        drafts_dir=drafts_dir,
        audit_flags_by_cluster=audit_flags_by_cluster,
        readiness_blocking_clusters=readiness_blocking_clusters,
    )

    # Build the cytoscape elements (nodes + edges) from the enriched
    # payload. We also emit section + cluster *compound* nodes when
    # clusters are available — the JS layer toggles them on demand.
    nodes: list[dict] = []
    edges: list[dict] = []
    section_lookup = {s["id"]: s for s in enriched["sections"]}
    cluster_lookup = {c["id"]: c for c in enriched["clusters"]}

    def _include_section(s: dict) -> bool:
        return not s["is_excluded"] and s["id"] != "s.thesis"

    # Thesis node first.
    argued = (graph.thesis_argued or "").strip()
    heading = (graph.thesis_statement or "").strip()
    if argued or heading or any(c["id"] == "cl.thesis" for c in enriched["claims"]):
        primary = argued or heading or ""
        full = primary
        if argued and heading and argued != heading:
            full = (
                f"Argued: {primary}\n\n"
                f"Heading: {heading}"
                + (f"\n\nNote: {graph.thesis_argued_note}" if graph.thesis_argued_note else "")
            )
        nodes.append({
            "data": {
                "id": "cl.thesis",
                "label": "THESIS — " + (_short(primary, 80) if primary else ""),
                "fullText": full,
                "type": "thesis",
                "sectionId": "s.thesis",
                "sectionTitle": "Thesis",
                "color": "#fef3c7",
                "importance": 1.0,
            }
        })

    # Section + cluster compound nodes (only when clusters were passed).
    if clusters:
        for s in enriched["sections"]:
            if not _include_section(s):
                continue
            nodes.append({
                "data": {
                    "id": s["id"],
                    "label": s["title"],
                    "color": s["color"],
                    "type": "section",
                    "depth": s["depth"],
                    "claimCount": s["claim_count"],
                    "clusterCount": s["cluster_count"],
                }
            })
        for c in enriched["clusters"]:
            section = section_lookup.get(c["section_id"])
            if section and not _include_section(section):
                continue
            nodes.append({
                "data": {
                    "id": c["id"],
                    "parent": c["section_id"],
                    "label": f"{c['role']} ({c['claim_count']})",
                    "type": "cluster",
                    "color": (section or {}).get("color", _SECTION_PALETTE[0]),
                    "proseState": c["prose_state"],
                    "auditFlagCount": c["audit_flag_count"],
                    "blocksReadiness": c["blocks_readiness"],
                    "hasUnrenderableMarker": c["has_unrenderable_marker"],
                    "isDirty": c["is_dirty"],
                    "isFailed": c["is_failed"],
                    "targetWordsMin": c["target_words_min"],
                    "targetWordsMax": c["target_words_max"],
                }
            })

    # Claim nodes — non-thesis, non-excluded.
    for claim in enriched["claims"]:
        if claim["is_excluded"] or claim["id"] == "cl.thesis":
            continue
        section = section_lookup.get(claim["section_id"]) if claim["section_id"] else None
        section_title = section["title"] if section else ""
        # Parent is the cluster (so cluster compound nodes group claims)
        # when clusters are present, else the section, else nothing.
        parent_id: str | None = None
        if claim["cluster_id"] and claim["cluster_id"] in cluster_lookup:
            parent_id = claim["cluster_id"]
        elif clusters and claim["section_id"] and section and _include_section(section):
            parent_id = claim["section_id"]
        node_data = {
            "id": claim["id"],
            "label": _short(claim["statement"], 50),
            "fullText": claim["statement"],
            "type": claim["type"],
            "role": claim["role"],
            "evidence": [ev["source"] for ev in claim["evidence"] if ev["source"]],
            "evidenceQuality": claim["evidence_quality"],
            "evidenceStatus": claim["evidence_status"],
            "sectionId": claim["section_id"],
            "sectionTitle": section_title,
            "clusterId": claim["cluster_id"],
            "color": claim["color"],
            "importance": claim["importance"],
            "mechanism": claim["mechanism"],
            "scopeConditions": claim["scope_conditions"],
            "hasUnrenderableMarker": claim["has_unrenderable_marker"],
        }
        if parent_id:
            node_data["parent"] = parent_id
        nodes.append({"data": node_data})

    valid_node_ids = {n["data"]["id"] for n in nodes}
    for rel in enriched["relationships"]:
        if rel["source"] not in valid_node_ids or rel["target"] not in valid_node_ids:
            continue
        edges.append({
            "data": {
                "id": rel["id"],
                "source": rel["source"],
                "target": rel["target"],
                "label": rel["type"],
                "type": rel["type"],
                "strength": rel["strength"],
                "note": rel["note"],
            }
        })

    payload = json.dumps({
        "nodes": nodes,
        "edges": edges,
        "sections": [
            {
                "id": s["id"],
                "parent": s["parent"],
                "title": s["title"],
                "color": s["color"],
                "claimCount": s["claim_count"],
                "clusterCount": s["cluster_count"],
                "depth": s["depth"],
            }
            for s in enriched["sections"]
            if _include_section(s)
        ],
        "clusters": enriched["clusters"],
        "meta": enriched["meta"],
    })
    project_title = html.escape(graph.project_name or "Argument graph")

    return _HTML_TEMPLATE.replace("__TITLE__", project_title).replace(
        "__ELEMENTS__", payload
    )


def write_html(graph: AuthorGraph, project_path: Path) -> Path:
    out = Path(project_path) / "outputs" / "argument_graph.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(graph), encoding="utf-8")
    return out


# ─── helpers ─────────────────────────────────────

def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", s)


def _short(text: str, limit: int = 70) -> str:
    text = (text or "").strip().replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _safe(text: str) -> str:
    """Make text safe to embed in a Mermaid quoted node label."""
    return (
        text
        .replace("\\", "")
        .replace('"', "'")
        .replace("`", "'")
        .replace("\n", " ")
    )


def _claim_label(claim) -> str:
    role_tag = next((t.split(":", 1)[1] for t in claim.tags if t.startswith("role:")), "")
    statement = _short(claim.statement, 70)
    parts: list[str] = []
    if role_tag:
        parts.append(f"<i>{html.escape(role_tag)}</i>")
    parts.append(_safe(statement))
    if claim.evidence:
        srcs = ", ".join(ev.source for ev in claim.evidence[:3] if ev.source)
        if srcs:
            parts.append(f"<small>← {_safe(srcs)}</small>")
    return "<br/>".join(parts)


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__ — argument graph</title>
<script src="https://unpkg.com/cytoscape@3.30.1/dist/cytoscape.min.js"></script>
<style>
  :root {
    --border:#e5e7eb; --muted:#6b7280; --bg:#fafafa; --surface:#fff;
    --accent:#3b82f6; --accent-soft:#dbeafe;
  }
  html, body { margin: 0; height: 100%; font-family: 'Inter', -apple-system, system-ui, sans-serif; background: var(--bg); }
  #toolbar {
    padding: 12px 18px; background: var(--surface); border-bottom: 1px solid var(--border);
    display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }
  #toolbar strong { font-size: 15px; letter-spacing: -0.01em; }
  #toolbar select, #toolbar button {
    padding: 6px 12px; cursor: pointer; border: 1px solid var(--border);
    border-radius: 6px; background: var(--surface); font-size: 12px;
    font-family: inherit; transition: border-color 0.1s;
  }
  #toolbar select:hover, #toolbar button:hover { border-color: var(--accent); }
  #toolbar .label { color: var(--muted); font-size: 12px; }
  #main { display: flex; height: calc(100% - 60px); }
  #cy { flex: 1; min-width: 0; background: var(--bg);
    background-image: radial-gradient(circle, #e5e7eb 1px, transparent 1px);
    background-size: 22px 22px; }
  #sidebar {
    width: 280px; border-left: 1px solid var(--border); background: var(--surface);
    overflow-y: auto; padding: 14px; font-size: 12px;
  }
  #sidebar h4 { margin: 14px 0 6px; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
  #sidebar h4:first-child { margin-top: 0; }
  .legend-row {
    display: flex; align-items: center; gap: 8px; padding: 5px 6px;
    cursor: pointer; border-radius: 4px;
    transition: background 0.12s;
  }
  .legend-row:hover { background: rgba(59, 130, 246, 0.08); }
  .legend-row.selected {
    background: rgba(59, 130, 246, 0.18);
    box-shadow: inset 2px 0 0 #3b82f6;
  }
  .legend-swatch { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
  .legend-text { flex: 1; line-height: 1.4; font-size: 12px; }
  .legend-count {
    color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums;
    background: var(--bg); padding: 1px 6px; border-radius: 99px;
  }
  #section-legend-clear {
    display: none; margin-top: 6px; padding: 4px 8px;
    font-size: 11px; color: var(--accent);
    background: transparent; border: 1px dashed var(--border);
    border-radius: 4px; cursor: pointer; width: 100%;
  }
  #section-legend-clear.visible { display: block; }
  #info {
    padding: 12px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; margin-bottom: 14px; min-height: 60px;
  }
  #info h3 { margin: 0 0 6px 0; font-size: 13px; line-height: 1.35; letter-spacing: -0.01em; }
  #info p { margin: 6px 0; font-size: 12px; line-height: 1.5; }
  #info .meta { color: var(--muted); font-size: 11px; margin-top: 8px;
    padding-top: 8px; border-top: 1px dashed var(--border); }
  #info.empty { color: var(--muted); font-style: italic; padding: 12px; }
  .edge-legend { display: flex; gap: 8px; align-items: center; margin: 5px 0; font-size: 12px; }
  .edge-legend .swatch {
    width: 28px; height: 0; border-top: 2.5px solid #6b7280; flex-shrink: 0;
  }
  .edge-legend.supports .swatch     { border-color: #16a34a; }
  .edge-legend.contradicts .swatch  { border-top-style: dashed; border-color: #dc2626; }
  .edge-legend.extends .swatch      { border-color: #8b5cf6; }
  .edge-legend.qualifies .swatch    { border-top-style: dashed; border-color: #f59e0b; }
  .edge-legend.depends_on .swatch   { border-color: #0891b2; }
  .edge-legend.evidence .swatch     { border-color: #14b8a6; }
  .edge-legend.counter .swatch      { border-top-style: dashed; border-color: #ea580c; }
  .edge-legend.pivot .swatch        { border-top-style: dotted; border-color: #db2777; }
  .edge-legend.unlabelled .swatch   { border-top-style: dotted; border-color: #9ca3af; }
  details summary {
    cursor: pointer; font-size: 11px; color: var(--accent); user-select: none;
  }
  .filter-row {
    display: flex; align-items: center; gap: 6px; padding: 4px 6px;
    cursor: pointer; font-size: 12px; color: #374151;
  }
  .filter-row input { cursor: pointer; }
  .filter-row:hover { background: rgba(59, 130, 246, 0.06); border-radius: 4px; }
  .badge {
    display: inline-block; font-size: 10px; padding: 1px 5px;
    border-radius: 99px; margin-left: 4px; font-weight: 600;
  }
  .badge-warn   { background: #fef3c7; color: #92400e; }
  .badge-error  { background: #fee2e2; color: #991b1b; }
  .badge-info   { background: #dbeafe; color: #1e40af; }
  .badge-muted  { background: #f3f4f6; color: #6b7280; }
</style>
</head>
<body>
<div id="toolbar">
  <strong>__TITLE__</strong>
  <span class="label">click any node for detail · drag to pan · scroll to zoom</span>
  <span class="label">layout:</span>
  <select id="layout-picker">
    <option value="cose" selected>force-directed (cose)</option>
    <option value="hierarchy">hierarchy (thesis at top)</option>
    <option value="concentric">concentric (thesis at center)</option>
    <option value="grid">grid (by section)</option>
  </select>
  <button id="fit-btn">fit</button>
  <button id="relayout-btn">re-layout</button>
</div>
<div id="main">
  <div id="cy"></div>
  <div id="sidebar">
    <div id="info" class="empty">Hover a node to highlight its neighbours · click for detail.</div>
    <h4>Sections</h4>
    <div id="section-legend"></div>
    <button id="section-legend-clear" type="button">clear section filter</button>
    <h4>Edges</h4>
    <div class="edge-legend supports"><span class="swatch"></span> supports</div>
    <div class="edge-legend contradicts"><span class="swatch"></span> contradicts</div>
    <div class="edge-legend extends"><span class="swatch"></span> extends</div>
    <div class="edge-legend qualifies"><span class="swatch"></span> qualifies</div>
    <div class="edge-legend depends_on"><span class="swatch"></span> depends on</div>
    <div class="edge-legend evidence"><span class="swatch"></span> evidence for</div>
    <div class="edge-legend counter"><span class="swatch"></span> counterexample</div>
    <div class="edge-legend pivot"><span class="swatch"></span> interpretive pivot</div>
    <div class="edge-legend unlabelled"><span class="swatch"></span> other</div>
    <h4>Node shapes</h4>
    <div class="legend-row"><span class="legend-swatch" style="background:#fef3c7;border-color:#f59e0b;border-width:2px;"></span><span>Thesis</span></div>
    <div class="legend-row"><span class="legend-swatch" style="background:#fff;border-color:#9ca3af;"></span><span>Empirical</span></div>
    <div class="legend-row"><span class="legend-swatch" style="background:#fff;border-color:#3b82f6;border-radius:50%;"></span><span>User synthesis (round)</span></div>
    <h4>Filters</h4>
    <div id="filter-rows">
      <label class="filter-row"><input type="checkbox" data-filter="unsupported"> only unsupported claims</label>
      <label class="filter-row"><input type="checkbox" data-filter="synthesis"> only user_synthesis claims</label>
      <label class="filter-row"><input type="checkbox" data-filter="weak_evidence"> only weak / source-hint evidence</label>
      <label class="filter-row"><input type="checkbox" data-filter="dirty_clusters"> only claims in dirty / failed clusters</label>
      <label class="filter-row"><input type="checkbox" data-filter="touched_since_render"> only claims touched since last render</label>
    </div>
    <h4>Edge type filter</h4>
    <div id="edge-filter"></div>
  </div>
</div>
<script>
const data = __ELEMENTS__;

// Render section legend. Subsection rows indent by depth so the
// hierarchy reads as a tree. Each row is clickable: clicking a section
// applies a sticky highlight to all of that section's claims (and any
// descendant subsection's claims), dimming everything else. Clicking
// the same row again clears.
const legendEl = document.getElementById('section-legend');

// Build descendants map so clicking a parent section also highlights
// claims in its subsections.
const descendantsById = {};
data.sections.forEach(s => { descendantsById[s.id] = new Set([s.id]); });
data.sections.forEach(s => {
  let p = s.parent;
  while (p) {
    if (descendantsById[p]) descendantsById[p].add(s.id);
    const parentRow = data.sections.find(x => x.id === p);
    p = parentRow ? parentRow.parent : null;
  }
});

let sectionFilterActive = null;  // section_id of the currently-pinned filter

function applySectionFilter(sectionId) {
  if (sectionFilterActive === sectionId) {
    clearSectionFilter();
    return;
  }
  sectionFilterActive = sectionId;
  const validIds = descendantsById[sectionId] || new Set([sectionId]);
  cy.batch(() => {
    cy.nodes().forEach(n => {
      const sid = n.data('sectionId');
      if (validIds.has(sid)) {
        n.removeClass('dimmed').addClass('highlight');
      } else {
        n.removeClass('highlight').addClass('dimmed');
      }
    });
    cy.edges().forEach(e => {
      const src = e.source().data('sectionId');
      const tgt = e.target().data('sectionId');
      // Edge is fully visible only when BOTH endpoints are inside the
      // selected section family — keeps the highlight focused on
      // intra-family argumentation.
      if (validIds.has(src) && validIds.has(tgt)) {
        e.removeClass('dimmed').addClass('highlight');
      } else {
        e.removeClass('highlight').addClass('dimmed');
      }
    });
  });
  legendEl.querySelectorAll('.legend-row').forEach(r =>
    r.classList.toggle('selected', r.dataset.sectionId === sectionId));
  document.getElementById('section-legend-clear').classList.add('visible');
}

function clearSectionFilter() {
  sectionFilterActive = null;
  cy.batch(() => {
    cy.nodes().removeClass('dimmed highlight');
    cy.edges().removeClass('dimmed highlight');
  });
  legendEl.querySelectorAll('.legend-row').forEach(r =>
    r.classList.remove('selected'));
  document.getElementById('section-legend-clear').classList.remove('visible');
}

data.sections.forEach(s => {
  const row = document.createElement('div');
  row.className = 'legend-row';
  row.dataset.sectionId = s.id;
  const indent = (s.depth || 0) * 12;
  row.style.paddingLeft = (6 + indent) + 'px';
  row.innerHTML = `
    <span class="legend-swatch" style="background:${s.color}"></span>
    <span class="legend-text">${s.title}</span>
    <span class="legend-count">${s.claimCount}</span>
  `;
  row.addEventListener('click', () => applySectionFilter(s.id));
  legendEl.appendChild(row);
});

document.getElementById('section-legend-clear')
  .addEventListener('click', clearSectionFilter);

const cy = cytoscape({
  container: document.getElementById('cy'),
  elements: { nodes: data.nodes, edges: data.edges },
  style: [
    // Base node style — soft drop shadow for depth, generous padding,
    // larger and rounder corners for a friendlier look.
    { selector: 'node', style: {
        'label': 'data(label)', 'text-wrap': 'wrap', 'text-max-width': 220,
        'background-color': 'data(color)',
        'border-color': '#cbd5e1', 'border-width': 1,
        'font-family': 'Inter, system-ui, sans-serif',
        'font-size': 'mapData(importance, 0, 1, 10, 13)',
        'font-weight': 500,
        'color': '#111827',
        'padding': 'mapData(importance, 0, 1, 8, 16)',
        'shape': 'round-rectangle',
        'corner-radius': 8,
        'width': 'auto', 'height': 'auto',
        'text-valign': 'center', 'text-halign': 'center',
        'transition-property': 'border-color, border-width, opacity',
        'transition-duration': '0.15s',
    }},
    // Thesis: a prominent, amber-bordered hero node.
    { selector: 'node[type = "thesis"]', style: {
        'background-color': '#fef3c7',
        'border-color': '#f59e0b', 'border-width': 3,
        'font-weight': 700, 'font-size': 14,
        'padding': 18,
    }},
    // User-synthesis claims: a calm blue ring marks them as the
    // author's own analytic moves vs evidence-grounded claims.
    { selector: 'node[type = "user_synthesis"]', style: {
        'border-color': '#3b82f6', 'border-width': 2,
    }},
    // Empirical / methodological claims have neutral borders.
    { selector: 'node[type = "empirical"]', style: { 'border-color': '#94a3b8' }},
    { selector: 'node[type = "methodological"]', style: { 'border-color': '#0891b2' }},
    // Selection + hover states.
    { selector: 'node:selected', style: {
        'border-color': '#111827', 'border-width': 3,
    }},
    { selector: 'node.dimmed', style: { 'opacity': 0.25 }},
    { selector: 'node.highlight', style: {
        'border-color': '#3b82f6', 'border-width': 3,
    }},

    // Edges — base style, then per-type colour. Curved bezier for
    // readability; arrow scaled to match edge width.
    { selector: 'edge', style: {
        'curve-style': 'bezier',
        'target-arrow-shape': 'triangle',
        'arrow-scale': 1.0,
        'width': 1.8,
        'line-color': '#94a3b8',
        'target-arrow-color': '#94a3b8',
        'opacity': 0.55,
        'transition-property': 'line-color, opacity, width',
        'transition-duration': '0.15s',
    }},
    { selector: 'edge[type = "supports"]', style: {
        'line-color': '#16a34a', 'target-arrow-color': '#16a34a', 'opacity': 0.85,
    }},
    { selector: 'edge[type = "contradicts"]', style: {
        'line-color': '#dc2626', 'target-arrow-color': '#dc2626',
        'line-style': 'dashed', 'opacity': 0.9, 'width': 2,
    }},
    { selector: 'edge[type = "extends"]', style: {
        'line-color': '#8b5cf6', 'target-arrow-color': '#8b5cf6', 'opacity': 0.85,
    }},
    { selector: 'edge[type = "qualifies"]', style: {
        'line-color': '#f59e0b', 'target-arrow-color': '#f59e0b',
        'line-style': 'dashed', 'opacity': 0.85,
    }},
    { selector: 'edge[type = "depends_on"]', style: {
        'line-color': '#0891b2', 'target-arrow-color': '#0891b2', 'opacity': 0.85,
    }},
    { selector: 'edge[type = "is_evidence_for"]', style: {
        'line-color': '#14b8a6', 'target-arrow-color': '#14b8a6', 'opacity': 0.85,
    }},
    { selector: 'edge[type = "is_counterexample_to"]', style: {
        'line-color': '#ea580c', 'target-arrow-color': '#ea580c',
        'line-style': 'dashed', 'opacity': 0.9,
    }},
    { selector: 'edge[type = "interpretive_pivot"]', style: {
        'line-color': '#db2777', 'target-arrow-color': '#db2777',
        'line-style': 'dotted', 'opacity': 0.9, 'width': 2,
    }},
    { selector: 'edge[type = "unlabelled"]', style: {
        'line-color': '#9ca3af', 'target-arrow-color': '#9ca3af',
        'line-style': 'dotted', 'opacity': 0.5,
    }},
    // Selection + hover states for edges.
    { selector: 'edge:selected', style: {
        'opacity': 1, 'width': 3,
        'label': 'data(label)', 'font-size': 11,
        'text-background-color': '#fff', 'text-background-opacity': 1,
        'text-background-padding': 3, 'color': '#111827',
    }},
    { selector: 'edge.dimmed', style: { 'opacity': 0.1 }},
    { selector: 'edge.highlight', style: { 'opacity': 1, 'width': 3 }},

    // ── Phase 3: compound section + cluster nodes ──
    // Section compound: a soft tinted container so claims grouped under
    // it are visually subordinate. Label sits at top-left; padding gives
    // children room to breathe.
    { selector: 'node[type = "section"]', style: {
        'background-color': 'data(color)',
        'background-opacity': 0.18,
        'border-color': 'data(color)', 'border-width': 1.5,
        'border-style': 'dashed',
        'shape': 'round-rectangle', 'corner-radius': 12,
        'label': 'data(label)', 'text-valign': 'top', 'text-halign': 'left',
        'font-weight': 600, 'font-size': 11, 'color': '#374151',
        'padding': 18,
        'compound-sizing-wrt-labels': 'include',
    }},
    // Cluster compound: tighter container inside the section. Border
    // colour reflects render state — dirty/failed clusters get a warning
    // border so the diagram immediately signals what's stale.
    { selector: 'node[type = "cluster"]', style: {
        'background-color': '#fff', 'background-opacity': 0.45,
        'border-color': '#cbd5e1', 'border-width': 1,
        'shape': 'round-rectangle', 'corner-radius': 8,
        'label': 'data(label)', 'text-valign': 'top', 'text-halign': 'left',
        'font-size': 10, 'color': '#6b7280', 'font-weight': 500,
        'padding': 10,
    }},
    { selector: 'node[type = "cluster"][?isDirty]', style: {
        'border-color': '#f59e0b', 'border-width': 2, 'border-style': 'dashed',
    }},
    { selector: 'node[type = "cluster"][?isFailed]', style: {
        'border-color': '#dc2626', 'border-width': 2.5,
    }},
    { selector: 'node[type = "cluster"][?blocksReadiness]', style: {
        'border-color': '#dc2626', 'border-width': 2,
    }},
    { selector: 'node[type = "cluster"][?hasUnrenderableMarker]', style: {
        'border-color': '#dc2626', 'border-width': 2.5, 'border-style': 'double',
    }},
    // Claim border tint by evidence quality — weak/unbound claims look
    // visibly different from bound or contradictory ones.
    { selector: 'node[evidenceQuality = "unbound"]', style: { 'border-color': '#fbbf24' }},
    { selector: 'node[evidenceQuality = "source_hint"]', style: { 'border-color': '#fbbf24' }},
    { selector: 'node[evidenceQuality = "contradictory"]', style: { 'border-color': '#dc2626' }},
    // Hidden state used by the filter sidebar.
    { selector: 'node.hidden', style: { 'display': 'none' }},
    { selector: 'edge.hidden', style: { 'display': 'none' }},
  ],
  wheelSensitivity: 0.2,
  layout: { name: 'preset' }, // we'll trigger the real layout on init
});

const layouts = {
  hierarchy: {
    // breadthfirst with directed=false treats edges as bidirectional, so the
    // tree expands outward from the thesis through supports/contradicts edges
    // (which point *into* the thesis, not out of it).
    name: 'breadthfirst', directed: false, padding: 40, spacingFactor: 1.6,
    roots: ['cl.thesis'], fit: true, animate: 300, animationEasing: 'ease-out', grid: false,
  },
  cose: {
    name: 'cose', animate: 300, animationEasing: 'ease-out',
    padding: 50, nodeRepulsion: 22000, idealEdgeLength: 140,
    edgeElasticity: 100, gravity: 0.6, fit: true,
  },
  concentric: {
    name: 'concentric', padding: 40, animate: 300, fit: true,
    concentric: n => n.data('type') === 'thesis' ? 100 : 1,
    levelWidth: () => 1, minNodeSpacing: 30,
  },
  grid: {
    name: 'grid', padding: 30, fit: true, animate: 300,
    sort: (a, b) => (a.data('sectionId') || '').localeCompare(b.data('sectionId') || ''),
  },
};

function applyLayout(name) {
  const opts = layouts[name] || layouts.hierarchy;
  cy.layout(opts).run();
}
// Default: cose force-directed for a more attractive initial render.
// Hierarchy stays available via the layout picker.
applyLayout('cose');

document.getElementById('layout-picker').value = 'cose';
document.getElementById('layout-picker').addEventListener('change', e => applyLayout(e.target.value));
document.getElementById('fit-btn').addEventListener('click', () => cy.fit(undefined, 50));
document.getElementById('relayout-btn').addEventListener('click', () => applyLayout(document.getElementById('layout-picker').value));

// ── Hover: dim non-neighbour nodes/edges so the connectivity of the
// hovered claim becomes visually obvious. When a sticky section
// filter is active (sectionFilterActive != null), skip the transient
// hover dim/restore so we don't clobber the pinned highlight.
cy.on('mouseover', 'node', evt => {
  if (sectionFilterActive !== null) return;
  const node = evt.target;
  const neighbourhood = node.closedNeighborhood();
  cy.elements().not(neighbourhood).addClass('dimmed');
  neighbourhood.removeClass('dimmed').addClass('highlight');
});
cy.on('mouseout', 'node', () => {
  if (sectionFilterActive !== null) return;
  cy.elements().removeClass('dimmed').removeClass('highlight');
});
cy.on('mouseover', 'edge', evt => {
  if (sectionFilterActive !== null) return;
  const edge = evt.target;
  const ends = edge.connectedNodes();
  cy.elements().not(edge.union(ends)).addClass('dimmed');
  edge.addClass('highlight');
  ends.addClass('highlight');
});
cy.on('mouseout', 'edge', () => {
  if (sectionFilterActive !== null) return;
  cy.elements().removeClass('dimmed').removeClass('highlight');
});

const info = document.getElementById('info');
cy.on('tap', 'node', evt => {
  const d = evt.target.data();
  info.classList.remove('empty');
  // Cluster compound node: show plan/state info, not claim text.
  if (d.type === 'cluster') {
    let h = `<h3>${escapeHtml(d.label)}</h3>`;
    const badges = [];
    if (d.isFailed) badges.push(`<span class="badge badge-error">failed</span>`);
    if (d.isDirty) badges.push(`<span class="badge badge-warn">dirty</span>`);
    if (d.blocksReadiness) badges.push(`<span class="badge badge-error">blocks readiness</span>`);
    if (d.hasUnrenderableMarker) badges.push(`<span class="badge badge-error">unrenderable</span>`);
    if (d.auditFlagCount) badges.push(`<span class="badge badge-warn">${d.auditFlagCount} flag(s)</span>`);
    h += `<div class="meta">cluster · ${escapeHtml(d.proseState)} · target ${d.targetWordsMin}-${d.targetWordsMax} words ${badges.join(' ')}</div>`;
    info.innerHTML = h;
    return;
  }
  if (d.type === 'section') {
    let h = `<h3>${escapeHtml(d.label)}</h3>`;
    h += `<div class="meta">section · depth ${d.depth} · ${d.claimCount} claim(s) · ${d.clusterCount} cluster(s)</div>`;
    info.innerHTML = h;
    return;
  }
  let h = `<h3>${escapeHtml(d.label)}</h3>`;
  const badges = [];
  if (d.evidenceQuality === 'unbound') badges.push(`<span class="badge badge-warn">unbound</span>`);
  if (d.evidenceQuality === 'source_hint') badges.push(`<span class="badge badge-info">source hint</span>`);
  if (d.evidenceQuality === 'contradictory') badges.push(`<span class="badge badge-error">contradictory</span>`);
  if (d.hasUnrenderableMarker) badges.push(`<span class="badge badge-error">unrenderable</span>`);
  if (d.evidenceStatus) badges.push(`<span class="badge badge-muted">${escapeHtml(d.evidenceStatus)}</span>`);
  h += `<div class="meta">${escapeHtml(d.sectionTitle || '')} · ${d.type}${d.role ? ' · ' + d.role : ''}`;
  if (typeof d.importance === 'number') {
    h += ` · importance ${d.importance.toFixed(2)}`;
  }
  h += ` ${badges.join(' ')}</div>`;
  if (d.fullText && d.fullText !== d.label) h += `<p>${escapeHtml(d.fullText)}</p>`;
  if (d.mechanism) h += `<p class="meta"><strong>mechanism:</strong> ${escapeHtml(d.mechanism)}</p>`;
  if (d.scopeConditions && d.scopeConditions.length) {
    h += `<p class="meta"><strong>scope:</strong> ${d.scopeConditions.map(escapeHtml).join('; ')}</p>`;
  }
  if (d.evidence && d.evidence.length) h += `<p class="meta">refs: ${d.evidence.map(escapeHtml).join(', ')}</p>`;
  info.innerHTML = h;
});
cy.on('tap', 'edge', evt => {
  const d = evt.target.data();
  info.classList.remove('empty');
  let h = `<h3>${escapeHtml(d.type)}</h3>`;
  h += `<div class="meta">${escapeHtml(d.source)} → ${escapeHtml(d.target)}</div>`;
  if (d.label && d.label !== d.type) h += `<p>${escapeHtml(d.label)}</p>`;
  info.innerHTML = h;
});
cy.on('tap', evt => {
  if (evt.target === cy) {
    info.classList.add('empty');
    info.textContent = 'Hover a node to highlight its neighbours · click for detail.';
  }
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ── Phase 3: claim-state filters ──
// Each checkbox in #filter-rows narrows the visible set to claims
// matching its predicate. Multiple boxes AND together. Cluster compound
// nodes follow their children: if all children are hidden, the parent
// is hidden too.
const filterPredicates = {
  unsupported: n => {
    const t = n.data('type');
    if (t !== 'empirical' && t !== 'methodological' && t !== 'normative' && t !== 'definition') return false;
    const eq = n.data('evidenceQuality');
    return eq === 'unbound' || eq === 'source_hint' || eq === 'contradictory';
  },
  synthesis: n => n.data('type') === 'user_synthesis',
  weak_evidence: n => {
    const eq = n.data('evidenceQuality');
    return eq === 'unbound' || eq === 'source_hint' || eq === 'contradictory';
  },
  dirty_clusters: n => {
    const cid = n.data('clusterId');
    if (!cid) return false;
    const cluster = (data.clusters || []).find(c => c.id === cid);
    return cluster && (cluster.is_dirty || cluster.is_failed || cluster.blocks_readiness);
  },
  // "Touched since render" — claims whose owning cluster is dirty.
  // Same predicate as dirty_clusters until we surface per-claim
  // modified_at vs cluster.last_rendered_at (deferred — needs payload extension).
  touched_since_render: n => {
    const cid = n.data('clusterId');
    if (!cid) return false;
    const cluster = (data.clusters || []).find(c => c.id === cid);
    return cluster && cluster.is_dirty;
  },
};

const enabledFilters = new Set();

function applyFilters() {
  cy.batch(() => {
    cy.elements().removeClass('hidden');
    if (enabledFilters.size === 0 && enabledEdgeTypes.size === 0) return;

    cy.nodes().forEach(n => {
      // Compound nodes (sections / clusters) follow their children.
      if (n.data('type') === 'section' || n.data('type') === 'cluster') return;
      const matches = [...enabledFilters].every(f => filterPredicates[f](n));
      if (!matches) n.addClass('hidden');
    });
    // Hide edges where either endpoint is hidden.
    cy.edges().forEach(e => {
      if (enabledEdgeTypes.size > 0 && !enabledEdgeTypes.has(e.data('type'))) {
        e.addClass('hidden');
        return;
      }
      const sHidden = e.source().hasClass('hidden');
      const tHidden = e.target().hasClass('hidden');
      if (sHidden || tHidden) e.addClass('hidden');
    });
    // Hide compound parents whose children are all hidden.
    cy.nodes('[type = "cluster"], [type = "section"]').forEach(parent => {
      const visibleChildren = parent.children().filter(c => !c.hasClass('hidden'));
      if (parent.children().length > 0 && visibleChildren.length === 0) {
        parent.addClass('hidden');
      }
    });
  });
}

document.querySelectorAll('#filter-rows input[type="checkbox"]').forEach(box => {
  box.addEventListener('change', () => {
    const f = box.dataset.filter;
    if (box.checked) enabledFilters.add(f); else enabledFilters.delete(f);
    applyFilters();
  });
});

// Edge-type filter: per-type checkboxes derived from the data so we
// only show types the document actually uses.
const enabledEdgeTypes = new Set();  // empty = all visible
const edgeTypesPresent = new Set(data.edges.map(e => e.data.type));
const edgeFilterEl = document.getElementById('edge-filter');
[...edgeTypesPresent].sort().forEach(t => {
  const row = document.createElement('label');
  row.className = 'filter-row';
  row.innerHTML = `<input type="checkbox" data-edge-type="${escapeHtml(t)}"> ${escapeHtml(t)}`;
  edgeFilterEl.appendChild(row);
  row.querySelector('input').addEventListener('change', evt => {
    const v = evt.target.dataset.edgeType;
    if (evt.target.checked) enabledEdgeTypes.add(v); else enabledEdgeTypes.delete(v);
    applyFilters();
  });
});
</script>
</body>
</html>
"""
