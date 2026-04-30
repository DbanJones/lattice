"""Skim-target checks. Deferred to polish milestone."""

from __future__ import annotations

from ..graph.models import AuditFlag, Cluster, FlagCategory, Severity
from .base import AuditCheck


class SkimTargetCheck(AuditCheck):
    category = FlagCategory.skim_target
    default_severity = Severity.critical

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        return []

    async def check_document(self, full_text: str, sections: list) -> list[AuditFlag]:
        return []
