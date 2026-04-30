"""Interactive enrichment coverage review TUI.

Walks every unbound and contradictory claim and prompts the author for a
resolution decision. Each decision is persisted via the
``EnrichmentReporter`` so a partially-completed review can be resumed.
"""

from __future__ import annotations

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from ..enricher.report import (
    ClaimResolution,
    CoverageReport,
    EnrichmentReporter,
    UnboundClaimRecord,
)


_RESOLUTION_CHOICES: dict[str, ClaimResolution] = {
    "a": ClaimResolution.mark_user_synthesis,
    "b": ClaimResolution.needs_new_source,
    "c": ClaimResolution.soften_to_hedged,
    "d": ClaimResolution.remove_from_graph,
    "e": ClaimResolution.accept_gap,
    "s": ClaimResolution.pending,
}


class CoverageReviewTUI:
    def __init__(self, reporter: EnrichmentReporter, console: Console | None = None) -> None:
        self.reporter = reporter
        self.console = console or Console()

    # ─── public entry point ────────────────────

    def run(self) -> CoverageReport:
        report = self.reporter.generate_report()
        self._print_summary(report)
        if not report.unbound and not report.contradictory:
            self.console.print("[green]All claims are grounded. No review needed.[/green]")
            return report

        for record in report.unbound:
            if record.resolution != ClaimResolution.pending:
                continue
            self._resolve_one(record, contradictory=False)

        for record in report.contradictory:
            if record.resolution != ClaimResolution.pending:
                continue
            self._resolve_one(record, contradictory=True)

        # Re-generate to reflect any graph mutations from the resolutions.
        report = self.reporter.generate_report()
        self._print_summary(report)
        return report

    # ─── per-claim prompt ─────────────────────

    def _resolve_one(self, record: UnboundClaimRecord, contradictory: bool) -> None:
        self.console.print()
        title = "Contradictory" if contradictory else "Unbound"
        self.console.print(f"[bold]{title} claim {record.claim_id}[/bold]")
        self.console.print(f"  statement: {record.statement}")
        self.console.print(f"  type:      {record.type.value}")
        self.console.print(f"  section:   {record.section_id or '-'}")
        self.console.print(f"  refs:      {', '.join(record.cited_sources) or 'none'}")
        if record.enrichment_notes:
            self.console.print(f"  notes:     {'; '.join(record.enrichment_notes)}")
        self.console.print()
        self.console.print("Resolution options:")
        self.console.print("  [a] mark as user_synthesis (your observation, not from a source)")
        self.console.print("  [b] need to add a new source to refs/")
        self.console.print("  [c] soften to a hedged form (you'll be prompted for the new statement)")
        self.console.print("  [d] remove from graph")
        self.console.print("  [e] accept gap (claim will render with MISSING_CLAIM marker)")
        self.console.print("  [s] skip for now")

        choice = Prompt.ask(
            "choice", choices=list(_RESOLUTION_CHOICES.keys()), default="s"
        )
        resolution = _RESOLUTION_CHOICES[choice]
        if resolution == ClaimResolution.pending:
            return

        new_statement: str | None = None
        if resolution == ClaimResolution.soften_to_hedged:
            new_statement = Prompt.ask(
                "new statement (or blank to keep existing)",
                default=record.statement,
            )

        self.reporter.update_resolution(
            record.claim_id, resolution, new_statement=new_statement
        )
        record.resolution = resolution
        if new_statement:
            record.new_statement = new_statement
        self.console.print(f"[green]Recorded: {resolution.value}[/green]")

    # ─── summary table ───────────────────────

    def _print_summary(self, report: CoverageReport) -> None:
        s = report.stats
        total = max(s.total_claims, 1)
        table = Table(title="Enrichment coverage")
        table.add_column("status")
        table.add_column("count", justify="right")
        table.add_column("percentage", justify="right")
        table.add_row("strong bindings", str(s.strong_bindings), f"{s.strong_pct:.0%}")
        table.add_row("weak bindings", str(s.weak_bindings),
                     f"{s.weak_bindings / total:.0%}")
        table.add_row("no bindings", str(s.no_bindings),
                     f"{s.no_bindings / total:.0%}")
        table.add_row("contradictory", str(s.contradictory_bindings),
                     f"{s.contradictory_bindings / total:.0%}")
        self.console.print(table)
