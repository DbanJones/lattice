"""Restructure: suggest a more logically coherent ordering for the
document, applying academic-writing rules to the scaffold.

Output is advisory — never mutates the author graph. The author
decides which suggestions to apply.
"""

from .rescaffold_apply import (
    APPLY_ORDER,
    ApplyResult,
    Decision,
    DecisionsLog,
    OffcutRecord,
    apply_operations,
    apply_to_project,
    decide_batch,
    decide_interactive,
    filter_accepted,
    load_plan,
)
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
    "APPLY_ORDER",
    "ApplyResult",
    "ClaimReorder",
    "ClusterReorder",
    "Decision",
    "DecisionsLog",
    "OffcutRecord",
    "RestructureReport",
    "RestructureSuggestion",
    "SectionReorder",
    "analyse_structure",
    "apply_operations",
    "apply_to_project",
    "decide_batch",
    "decide_interactive",
    "filter_accepted",
    "load_plan",
    "read_restructure_report",
    "write_restructure_report",
]
