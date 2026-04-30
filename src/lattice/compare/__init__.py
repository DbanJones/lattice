"""Cross-project comparison: scaffolds, theses, and claim equivalence.

Reuses the existing ``differ`` engine for structural overlap and adds
an LLM semantic pass that pairs claims across two author graphs.
"""

from .semantic import (
    ComparisonReport,
    SemanticComparer,
    SemanticPair,
    ThesisComparison,
    compare_projects,
)

__all__ = [
    "ComparisonReport",
    "SemanticComparer",
    "SemanticPair",
    "ThesisComparison",
    "compare_projects",
]
