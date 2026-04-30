"""Examiner review: eight-question reviewer pass. Deferred to polish milestone.

When implemented, this stage uses Opus 4.7 and writes the advisory
output to .lattice/examiner_reviews/<timestamp>.md.
"""

from __future__ import annotations

from pathlib import Path

from ..graph.models import AuditFlag, Cluster, FlagCategory, Severity
from .base import AuditCheck


class ExaminerReview(AuditCheck):
    category = FlagCategory.examiner
    default_severity = Severity.critical

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        return []

    async def review_document(
        self, full_text: str, thesis: str, captions: list[str]
    ) -> Path | None:
        # Not implemented in the M4 deterministic slice.
        return None
