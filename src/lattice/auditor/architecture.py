"""Architecture checks: section structure, hourglass, killer graph.

Per-cluster check is a no-op; document-level checks happen in the runner.
"""

from __future__ import annotations

from ..graph.models import AuditFlag, Cluster, FlagCategory, Severity
from .base import AuditCheck


class ArchitectureCheck(AuditCheck):
    category = FlagCategory.architecture
    default_severity = Severity.critical

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        return []

    async def check_document(self, full_text: str) -> list[AuditFlag]:
        # Document-level hourglass / killer-graph checks are a polish item.
        return []
