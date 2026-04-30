"""Literature-gap analysis: find missing canonical works per section.

For each section of the author graph, asks Claude what canonical
works, standard counter-arguments, and recent papers the section
should engage with but doesn't. Optionally verifies each suggestion
against OpenAlex so the report excludes hallucinated citations.
"""

from .gaps import (
    LitGapSuggestion,
    LitGapsReport,
    SectionGaps,
    find_lit_gaps,
    write_lit_gaps_report,
)

__all__ = [
    "LitGapSuggestion",
    "LitGapsReport",
    "SectionGaps",
    "find_lit_gaps",
    "write_lit_gaps_report",
]
