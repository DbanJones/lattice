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
    ClaimType,
    Cluster,
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


def render_html(graph: AuthorGraph) -> str:
    """Self-contained HTML page with cytoscape.js loading the graph as JSON.

    Design: claims are flat nodes (no compound parents — they wreck cose
    layout when sections are large). Each claim is colour-coded by its
    section; the section legend lives in a fixed sidebar. Skipped
    sections (e.g. references) are excluded from the graph entirely.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    sections_legend: list[dict] = []

    # Map section_id -> color for claim coloring + legend.
    # Subsections inherit their top-level ancestor's colour so all of
    # ``s.c``'s claims (including those in ``s.c.1``, ``s.c.1.2``)
    # paint as one family — keeps the graph readable when a paper has
    # three levels of nesting.
    renderable_sections = [
        s for s in graph.sections
        if s.role != SectionRole.references and s.section_id != "s.thesis"
    ]
    top_level = [s for s in renderable_sections if not s.parent]
    top_colors = {
        s.section_id: _SECTION_PALETTE[i % len(_SECTION_PALETTE)]
        for i, s in enumerate(top_level)
    }

    def _top_ancestor_id(sid: str) -> str:
        # Walk up via the parent map until we hit a section with no parent.
        by_id = {s.section_id: s for s in graph.sections}
        cur = by_id.get(sid)
        while cur is not None and cur.parent:
            cur = by_id.get(cur.parent)
        return cur.section_id if cur else sid

    section_colors = {
        s.section_id: top_colors.get(_top_ancestor_id(s.section_id), _SECTION_PALETTE[0])
        for s in renderable_sections
    }

    # Thesis node first (always at the top of the layout). Prefer the
    # argued thesis (derived from the full claim list) for the displayed
    # text; keep the heading thesis available in the detail panel.
    argued = (graph.thesis_argued or "").strip()
    heading = (graph.thesis_statement or "").strip()
    if argued or heading or any(c.claim_id == "cl.thesis" for c in graph.claims):
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

    for section in renderable_sections:
        # Section depth is encoded in the section_id (``s.c`` = 0,
        # ``s.c.1`` = 1, ``s.c.1.2`` = 2). The legend uses depth to
        # indent subsection entries so the reader can see the nesting.
        depth = (
            0 if section.section_id == "s.thesis"
            else max(0, section.section_id.count(".") - 1)
        )
        sections_legend.append({
            "id": section.section_id,
            "parent": section.parent,
            "title": section.title,
            "color": section_colors[section.section_id],
            "claimCount": len(section.claim_ids),
            "depth": depth,
        })
        for cid in section.claim_ids:
            claim = next((c for c in graph.claims if c.claim_id == cid), None)
            if claim is None:
                continue
            nodes.append({
                "data": {
                    "id": claim.claim_id,
                    "label": _short(claim.statement, 50),
                    "fullText": claim.statement,
                    "type": claim.type.value,
                    "role": next(
                        (t.split(":", 1)[1] for t in claim.tags if t.startswith("role:")),
                        "",
                    ),
                    "evidence": [ev.source for ev in claim.evidence if ev.source],
                    "sectionId": section.section_id,
                    "sectionTitle": section.title,
                    "color": section_colors[section.section_id],
                    "importance": float(claim.importance),
                }
            })

    valid_node_ids = {n["data"]["id"] for n in nodes}
    for rel in graph.relationships:
        if rel.from_claim not in valid_node_ids or rel.to_claim not in valid_node_ids:
            continue  # skip edges to nodes we excluded (e.g. into references)
        edges.append({
            "data": {
                "id": rel.rel_id,
                "source": rel.from_claim,
                "target": rel.to_claim,
                "label": rel.type.value,
                "type": rel.type.value,
            }
        })

    payload = json.dumps({
        "nodes": nodes,
        "edges": edges,
        "sections": sections_legend,
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
  let h = `<h3>${escapeHtml(d.label)}</h3>`;
  h += `<div class="meta">${escapeHtml(d.sectionTitle || '')} · ${d.type}${d.role ? ' · ' + d.role : ''}`;
  if (typeof d.importance === 'number') {
    h += ` · importance ${d.importance.toFixed(2)}`;
  }
  h += `</div>`;
  if (d.fullText && d.fullText !== d.label) h += `<p>${escapeHtml(d.fullText)}</p>`;
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
</script>
</body>
</html>
"""
