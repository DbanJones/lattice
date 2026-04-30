"""References manifest builder.

Aggregates ``Source`` metadata (citation info, what the paper is
about) with per-claim usage data (which claims in this project cite
the source, what binding strength, what cluster they live in) into a
single payload the UI / downstream tooling can render in any
citation style.

The output is intentionally deterministic — no LLM calls — so the
user can switch citation style instantly.

Also persists a per-project ``references.json`` (machine-readable
manifest of cited sources) and ``references.md`` (human-readable
bibliography in every supported style). Both files live at the
project root so they travel with the work and can be inspected
directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..graph.models import AuthorGraph, Source
from ..graph.store import GraphStore
from .citation_formatter import format_citation, supported_styles


def _load_sources(project_path: Path) -> list[Source]:
    """Read ``source_store.json`` from the project. Returns [] if the
    file doesn't exist (project hasn't indexed sources yet)."""
    source_store_path = project_path / ".lattice" / "source_store.json"
    if not source_store_path.exists():
        return []
    try:
        data = json.loads(source_store_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    rows = data if isinstance(data, list) else data.get("sources", [])
    out: list[Source] = []
    for r in rows:
        try:
            out.append(Source.model_validate(r))
        except Exception:  # noqa: BLE001 — skip a corrupt row, not the whole file
            continue
    return out


def _summarise_source(source: Source) -> str:
    """Produce a short prose 'what the paper is about' summary from
    the indexed passages — concatenated abstract + first introductory
    paragraphs, truncated. Deterministic, no LLM. Caller can replace
    with an LLM-generated abstract later if richer summaries are
    desired."""
    if not source.passages:
        return ""
    # First passage is usually the title/abstract page; first 3
    # passages typically contain the abstract + opening paragraphs.
    chunks = [p.text for p in source.passages[:3] if p.text and p.text.strip()]
    blob = " ".join(chunks).strip()
    if len(blob) > 1200:
        blob = blob[:1200].rsplit(" ", 1)[0] + "…"
    return blob


def build_references_manifest(
    project_path: Path,
    style: str = "harvard",
    summary_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a complete reference manifest for the project.

    ``style`` selects the citation style for the rendered ``in_text``
    and ``bibliography`` fields. The raw citation metadata is also
    returned, so the UI can re-format on the fly without another
    backend call.

    ``summary_overrides`` lets the caller supply a hand-written or
    LLM-generated 'about' summary per source_id; otherwise a
    deterministic snippet from the indexed passages is used.
    """
    sources = _load_sources(project_path)
    store = GraphStore.load(project_path)
    graph = store.get_graph()
    clusters = store.list_clusters()

    # Index: which clusters does each claim live in?
    cluster_for_claim: dict[str, list[str]] = {}
    for c in clusters:
        for entry in c.claim_sequence:
            cluster_for_claim.setdefault(entry.claim_id, []).append(c.cluster_id)

    # Build per-source usage: every Evidence binding pointing at a
    # source ⇒ a usage row with the bound claim's full context.
    usage_by_source: dict[str, list[dict[str, Any]]] = {}
    for claim in graph.claims:
        for ev in claim.evidence:
            sid = ev.source
            usage_by_source.setdefault(sid, []).append({
                "claim_id": claim.claim_id,
                "claim_statement": claim.statement[:300],
                "section_id": claim.section_id,
                "cluster_ids": cluster_for_claim.get(claim.claim_id, []),
                "binding_strength": (
                    ev.binding_strength.value
                    if hasattr(ev.binding_strength, "value")
                    else str(ev.binding_strength)
                ),
                "passage_id": ev.passage,
                "quote_text": ev.quote_text,
                "quote_verbatim": ev.quote_verbatim,
                "page": ev.page,
            })

    # Pull AI-enrichment data (if any) so cards can show summary,
    # field position, key findings, and per-claim usage purpose.
    from ..enricher.reference_ai_enrichment import load_enrichment
    enrichment_by_id = load_enrichment(project_path)

    overrides = summary_overrides or {}
    references: list[dict[str, Any]] = []
    for s in sources:
        try:
            formatted = format_citation(s.citation, style)
        except ValueError:
            formatted = format_citation(s.citation, "harvard")

        # Prefer (in order): user-saved override, AI summary, auto extract.
        ai_enrichment = enrichment_by_id.get(s.source_id) or {}
        about = (
            overrides.get(s.source_id)
            or ai_enrichment.get("summary")
            or _summarise_source(s)
        )

        usages = usage_by_source.get(s.source_id, []) or []
        # Annotate each usage with its AI-derived role/explanation
        # if available, so the UI shows "what it's used for".
        purposes_by_claim = {
            p.get("claim_id"): p
            for p in (ai_enrichment.get("usage_purposes") or [])
        }
        for u in usages:
            p = purposes_by_claim.get(u["claim_id"])
            if p:
                u["ai_role"] = p.get("role")
                u["ai_explanation"] = p.get("explanation")

        references.append({
            "source_id": s.source_id,
            "type": s.type.value if hasattr(s.type, "value") else str(s.type),
            "citation": s.citation.model_dump(),
            "metadata": {
                "peer_reviewed": s.metadata.peer_reviewed,
                "primary": s.metadata.primary,
                "file_path": s.metadata.file_path,
                "ocr_used": s.metadata.ocr_used,
                "passage_count": len(s.passages),
            },
            "about": about,
            "used_in_paper": usages,
            "ai": {
                "summary": ai_enrichment.get("summary") or None,
                "key_findings": ai_enrichment.get("key_findings") or [],
                "field_position": ai_enrichment.get("field_position") or None,
                "citation_count_estimate": ai_enrichment.get("citation_count_estimate"),
                "confidence": ai_enrichment.get("confidence") or None,
                "enriched_at": ai_enrichment.get("enriched_at") or None,
            } if ai_enrichment else None,
            "formatted": {
                "style": formatted.style,
                "in_text": formatted.in_text,
                "in_text_narrative": formatted.in_text_narrative,
                "bibliography": formatted.bibliography,
            },
        })

    # Sort: most-used sources first, then alphabetical by first author.
    references.sort(
        key=lambda r: (
            -len(r["used_in_paper"]),
            (r["citation"].get("authors") or ["zzz"])[0].lower(),
        )
    )

    return {
        "style": style,
        "supported_styles": list(supported_styles()),
        "references": references,
        "totals": {
            "source_count": len(references),
            "used_count": sum(1 for r in references if r["used_in_paper"]),
            "total_usages": sum(len(r["used_in_paper"]) for r in references),
        },
    }


def write_project_references(
    project_path: Path,
    summary_overrides: dict[str, str] | None = None,
    cited_only: bool = True,
) -> dict[str, Path]:
    """Persist the project's references to disk:

    - ``<project>/references.json`` — structured manifest with raw
      citation data, every supported style's formatted output, and the
      per-claim usage trail. Machine-readable; useful for downstream
      tooling and version control.
    - ``<project>/references.md`` — human-readable bibliography
      grouped by section: a citation-key block per source (showing
      the in-text + bibliography form in every style), then a per-
      style alphabetised reference list.

    Returns the paths written so the caller can surface them in run
    results.

    By default only includes sources actually cited by at least one
    claim in this paper. Pass ``cited_only=False`` to write the full
    indexed-source list.
    """
    # Build manifest in every supported style so the JSON file has
    # all formatted variants pre-computed (the user can switch styles
    # by reading from the same file, no re-formatting needed).
    base = build_references_manifest(
        project_path, style="harvard", summary_overrides=summary_overrides,
    )
    references = base["references"]
    if cited_only:
        references = [r for r in references if r["used_in_paper"]]

    # For each source, compute the formatted citation in every style.
    multi_style: dict[str, list[dict[str, Any]]] = {}
    for sty in supported_styles():
        sty_manifest = build_references_manifest(
            project_path, style=sty, summary_overrides=summary_overrides,
        )
        for entry in sty_manifest["references"]:
            multi_style.setdefault(entry["source_id"], []).append(
                {
                    "style": sty,
                    "in_text": entry["formatted"]["in_text"],
                    "in_text_narrative": entry["formatted"]["in_text_narrative"],
                    "bibliography": entry["formatted"]["bibliography"],
                }
            )

    enriched_refs: list[dict[str, Any]] = []
    for r in references:
        enriched_refs.append({
            **r,
            "all_styles": multi_style.get(r["source_id"], []),
        })

    refs_json = {
        "scope": "cited_only" if cited_only else "all_indexed",
        "supported_styles": list(supported_styles()),
        "totals": {
            "cited_source_count": len(enriched_refs),
            "total_usages": sum(len(r["used_in_paper"]) for r in enriched_refs),
        },
        "references": enriched_refs,
    }

    json_path = project_path / "references.json"
    md_path = project_path / "references.md"
    json_path.write_text(
        json.dumps(refs_json, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    md_path.write_text(
        _render_references_markdown(enriched_refs, cited_only),
        encoding="utf-8",
    )
    return {"json": json_path, "md": md_path}


def _render_references_markdown(
    references: list[dict[str, Any]], cited_only: bool
) -> str:
    """Produce a human-readable references file. Layout:

      1. Header note (cited-only vs. all-indexed)
      2. Per-source detail block:
          - source_id, type, peer-reviewed pill, file path
          - "About" summary
          - "Used in this paper for" list of claim usages
          - Citation in every supported style
      3. Bibliography section per style — alphabetised reference list
    """
    lines: list[str] = []
    lines.append("# References")
    lines.append("")
    if cited_only:
        lines.append(
            "_Sources actually cited by claims in this paper. "
            "Switch citation style at the bottom of the file._"
        )
    else:
        lines.append("_All indexed sources (cited and uncited)._")
    lines.append("")
    lines.append(f"**{len(references)} source(s) cited.**")
    lines.append("")

    if not references:
        lines.append(
            "No sources are currently cited in this paper. "
            "Add evidence bindings via `lattice enrich` or by editing "
            "the outline to reference indexed sources."
        )
        return "\n".join(lines) + "\n"

    # Per-source detail blocks.
    for r in references:
        c = r.get("citation", {}) or {}
        meta = r.get("metadata", {}) or {}
        lines.append(f"## `{r['source_id']}` — {c.get('title') or '(untitled)'}")
        lines.append("")
        meta_bits: list[str] = [r.get("type", "source")]
        if meta.get("peer_reviewed"):
            meta_bits.append("peer-reviewed")
        if meta.get("primary"):
            meta_bits.append("primary")
        if c.get("year"):
            meta_bits.append(str(c["year"]))
        lines.append("- **Metadata**: " + " · ".join(meta_bits))
        if c.get("authors"):
            lines.append(f"- **Authors**: {', '.join(c['authors'])}")
        if c.get("container"):
            container = c["container"]
            if c.get("volume"):
                container += f" {c['volume']}"
                if c.get("issue"):
                    container += f"({c['issue']})"
            if c.get("pages"):
                container += f", {c['pages']}"
            lines.append(f"- **Published in**: {container}")
        if c.get("doi"):
            lines.append(f"- **DOI**: https://doi.org/{c['doi']}")
        if meta.get("file_path"):
            lines.append(f"- **File**: `{meta['file_path']}`")
        lines.append("")

        about = (r.get("about") or "").strip()
        if about:
            lines.append("### What this paper is about")
            lines.append("")
            lines.append(about)
            lines.append("")

        usages = r.get("used_in_paper") or []
        lines.append(f"### Used in this paper ({len(usages)} citation(s))")
        lines.append("")
        for u in usages:
            cluster_id = (u.get("cluster_ids") or [None])[0] or "—"
            lines.append(
                f"- `{cluster_id}` · `{u.get('claim_id')}` · "
                f"**{u.get('binding_strength', 'weak')}**"
                + (f" · p. {u['page']}" if u.get("page") else "")
            )
            stmt = (u.get("claim_statement") or "").strip()
            if stmt:
                lines.append(f"  > {stmt}")
            quote = (u.get("quote_text") or "").strip()
            if quote:
                lines.append(f"  - Quote: _\"{quote}\"_")
        lines.append("")

        styles = r.get("all_styles") or []
        if styles:
            lines.append("### In every citation style")
            lines.append("")
            lines.append("| Style | In-text | Bibliography |")
            lines.append("|---|---|---|")
            for s in styles:
                lines.append(
                    f"| {s['style']} | `{s['in_text']}` | "
                    f"{s['bibliography'].replace('|', '\\|')} |"
                )
            lines.append("")

    # Per-style bibliography (alphabetised).
    lines.append("---")
    lines.append("")
    lines.append("## Bibliography by style")
    lines.append("")
    for sty in supported_styles():
        lines.append(f"### {sty.replace('_', ' ').title()}")
        lines.append("")
        rows: list[str] = []
        for r in references:
            for s in (r.get("all_styles") or []):
                if s["style"] == sty:
                    rows.append(s["bibliography"])
                    break
        rows.sort()
        for i, row in enumerate(rows, start=1):
            lines.append(f"{i}. {row}")
        lines.append("")
    return "\n".join(lines) + "\n"
