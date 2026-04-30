"""Document assembly: concatenate cluster prose into the final markdown output.

Per Fix 1 of the pipeline-integrity brief, this stage acts as the second
delivery gate: it refuses to write to ``outputs/`` when any blocking
condition is present (failed clusters, unresolved markers, missing
sections, missing closing section, register bleed, or unresolved
critical audit flags). When blocked, it writes a status file to
``.lattice/delivery_blocked.md`` and returns ``None`` instead of a path.
"""

from __future__ import annotations

from pathlib import Path

from ..auditor.readiness import DocumentReadinessCheck, ReadinessReport
from ..graph.models import Severity, SectionRole
from ..graph.store import GraphStore
from ..voice.parser import Voice


class DocumentFinaliser:
    def __init__(self, project_path: Path, store: GraphStore, voice: Voice) -> None:
        self.project_path = Path(project_path)
        self.store = store
        self.voice = voice
        self.drafts_dir = self.project_path / ".lattice" / "drafts" / voice.name
        self.output_dir = self.project_path / "outputs"

    # ─── public entry point ─────────────────────

    def finalise(self) -> Path | None:
        """Concatenate cluster prose into the final output.

        Returns the output path on success, ``None`` if delivery is
        blocked. When blocked, writes ``.lattice/delivery_blocked.md``
        with the readiness summary so the author can see what to fix.
        """
        readiness = self._readiness_report()
        critical_flags = self._unresolved_critical_flags()

        if not readiness.is_ready or critical_flags:
            self._write_blocked_status(readiness, critical_flags)
            return None

        return self._concatenate_and_write()

    # ─── readiness gate ──────────────────────────

    def _readiness_report(self) -> ReadinessReport:
        return DocumentReadinessCheck(
            store=self.store,
            voice=self.voice,
            project_path=self.project_path,
        ).check()

    def _unresolved_critical_flags(self) -> list:
        flags = self.store.list_audit_flags(self.voice.name)
        return [
            f for f in flags
            if f.severity == Severity.critical and f.decision is None
        ]

    def _write_blocked_status(self, readiness: ReadinessReport, critical_flags: list) -> None:
        status_path = self.project_path / ".lattice" / "delivery_blocked.md"
        status_path.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = ["# Delivery blocked", ""]
        if not readiness.is_ready:
            lines.append("## Readiness check")
            lines.append("")
            lines.append(readiness.summary)
            lines.append("")
        if critical_flags:
            lines.append(
                f"## Critical audit flags unresolved: {len(critical_flags)}"
            )
            lines.append("")
            lines.append(
                "Run `lattice flags <project> --voice <voice>` to review."
            )
            lines.append("")
            for f in critical_flags[:20]:
                lines.append(f"- **{f.rule_id}** in cluster `{f.cluster_id or '-'}`")
                snippet = (f.offending_text or "").replace("\n", " ")[:120]
                if snippet:
                    lines.append(f"  - offending: `{snippet}`")
                if f.suggestion:
                    lines.append(f"  - suggestion: {f.suggestion}")
            if len(critical_flags) > 20:
                lines.append(f"- ... and {len(critical_flags) - 20} more")
        status_path.write_text("\n".join(lines), encoding="utf-8")

    # ─── successful path ────────────────────────

    def _concatenate_and_write(self) -> Path:
        graph = self.store.get_graph()
        all_clusters = self.store.list_clusters()
        lines: list[str] = []

        lines.append(f"# {graph.project_name}")
        lines.append("")

        for section in graph.sections:
            if section.role == SectionRole.references:
                continue
            section_clusters = sorted(
                (c for c in all_clusters if c.section_id == section.section_id),
                key=lambda c: c.position,
            )
            if not section_clusters:
                continue
            lines.append(f"## {section.title}")
            lines.append("")
            for cluster in section_clusters:
                prose_path = self.drafts_dir / f"cluster_{cluster.cluster_id}.md"
                if not prose_path.exists():
                    # Should never happen if readiness passed, but be defensive.
                    lines.append(f"_[cluster {cluster.cluster_id} not yet rendered]_")
                    lines.append("")
                    continue
                prose = prose_path.read_text(encoding="utf-8").rstrip()
                lines.append(prose)
                lines.append("")

        if self.voice.figures.list_of_figures:
            all_figures = [
                (section.title, fig_id)
                for section in graph.sections
                for fig_id in section.figure_ids
            ]
            if all_figures:
                lines.append("## List of Figures")
                lines.append("")
                for i, (section_title, fig_id) in enumerate(all_figures, start=1):
                    lines.append(f"{i}. {fig_id} (in: {section_title})")
                lines.append("")

        output = "\n".join(lines).rstrip() + "\n"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / f"paper.{self.voice.name}.md"
        out_path.write_text(output, encoding="utf-8")
        # Clear any stale block status from a previous run.
        block_path = self.project_path / ".lattice" / "delivery_blocked.md"
        if block_path.exists():
            block_path.unlink()
        return out_path
