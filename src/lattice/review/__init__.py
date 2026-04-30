"""Review: supervisor-style critique of the rendered paper.

Produces two artefacts:
  - ``outputs/review.{voice}.md`` — overall + per-section + per-cluster
    critique with comments (supervisor voice).
  - ``outputs/review_track_changes.{voice}.md`` — the rendered paper
    with word-level diffs (``<del>``/``<ins>``) showing the
    supervisor's suggested revisions.
"""

from .review import (
    ClusterRevision,
    ReviewReport,
    SectionCritique,
    produce_review,
    read_review_report,
    write_review_artefacts,
)

__all__ = [
    "ClusterRevision",
    "ReviewReport",
    "SectionCritique",
    "produce_review",
    "read_review_report",
    "write_review_artefacts",
]
