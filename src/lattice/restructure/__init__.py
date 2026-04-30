"""Restructure: suggest a more logically coherent ordering for the
document, applying academic-writing rules to the scaffold.

Output is advisory — never mutates the author graph. The author
decides which suggestions to apply.
"""

from .restructure import (
    ClaimReorder,
    ClusterReorder,
    RestructureReport,
    RestructureSuggestion,
    SectionReorder,
    analyse_structure,
    write_restructure_report,
    read_restructure_report,
)

__all__ = [
    "ClaimReorder",
    "ClusterReorder",
    "RestructureReport",
    "RestructureSuggestion",
    "SectionReorder",
    "analyse_structure",
    "write_restructure_report",
    "read_restructure_report",
]
