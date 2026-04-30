"""Audit runner: orchestrates every check, aggregates flags, writes report."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from ..graph.models import AuditFlag, FlagCategory
from ..graph.store import GraphStore
from ..utils.config import Config
from ..utils.llm import ClaudeClient
from ..voice.parser import Voice
from .architecture import ArchitectureCheck
from .boilerplate import MechanismBoilerplateCheck
from .citation import CitationCheck
from .coverage import CoverageCheck
from .examiner import ExaminerReview
from .formality import FormalityCheck
from .paragraph import ParagraphArchitectureCheck
from .quantification import QuantificationCheck
from .sentence import SentenceCraftCheck
from .skim import SkimTargetCheck
from .voice import VoiceComplianceCheck


class AuditRunner:
    def __init__(
        self,
        config: Config,
        store: GraphStore,
        llm: ClaudeClient | None,
        voice: Voice,
    ) -> None:
        self.config = config
        self.store = store
        self.llm = llm
        self.voice = voice
        self._checks = [
            ArchitectureCheck(config, store, llm, voice),
            CitationCheck(config, store, llm, voice),
            CoverageCheck(config, store, llm, voice),
            VoiceComplianceCheck(config, store, llm, voice),
            SentenceCraftCheck(config, store, llm, voice),
            QuantificationCheck(config, store, llm, voice),
            ParagraphArchitectureCheck(config, store, llm, voice),
            FormalityCheck(config, store, llm, voice),
            SkimTargetCheck(config, store, llm, voice),
            MechanismBoilerplateCheck(config, store, llm, voice),
        ]
        self.examiner = ExaminerReview(config, store, llm, voice)

    async def run(self) -> list[AuditFlag]:
        all_flags: list[AuditFlag] = []
        clusters = self.store.list_clusters()
        drafts_dir = self.config.project_path / ".lattice" / "drafts" / self.voice.name

        for cluster in clusters:
            prose_path = drafts_dir / f"cluster_{cluster.cluster_id}.md"
            if not prose_path.exists():
                continue
            prose = prose_path.read_text(encoding="utf-8")
            cluster_flags: list[list[AuditFlag]] = await asyncio.gather(
                *[check.check_cluster(cluster, prose) for check in self._checks],
                return_exceptions=False,
            )
            for fs in cluster_flags:
                all_flags.extend(fs)

        self.store.save_audit_flags(self.voice.name, all_flags)
        self.write_audit_report(all_flags)
        return all_flags

    def write_audit_report(self, flags: list[AuditFlag]) -> Path:
        audit_dir = self.config.project_path / ".lattice" / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        report_path = audit_dir / f"audit.{self.voice.name}.md"

        by_category: dict[FlagCategory, list[AuditFlag]] = defaultdict(list)
        for flag in flags:
            by_category[flag.category].append(flag)

        total = len(flags)
        lines = [
            f"# Audit report — voice: {self.voice.name}",
            "",
            f"Total flags: **{total}**",
            "",
        ]
        by_severity = defaultdict(int)
        for flag in flags:
            by_severity[flag.severity.value] += 1
        if by_severity:
            lines.append("## Severity summary")
            lines.append("")
            for sev in ("critical", "standard", "minor"):
                if sev in by_severity:
                    lines.append(f"- {sev}: {by_severity[sev]}")
            lines.append("")

        for category in FlagCategory:
            cat_flags = by_category.get(category, [])
            if not cat_flags:
                continue
            lines.append(f"## {category.value} ({len(cat_flags)})")
            lines.append("")
            for flag in cat_flags:
                lines.append(
                    f"- **{flag.rule_id}** [{flag.severity.value}, default={flag.default_mode.value}]"
                )
                lines.append(
                    f"  cluster={flag.cluster_id}  para={flag.prose_location.paragraph_index}"
                )
                text = flag.offending_text.replace("\n", " ")
                lines.append(f"  offending: `{text[:140]}`")
                if flag.suggestion:
                    lines.append(f"  suggestion: {flag.suggestion}")
                lines.append("")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path
