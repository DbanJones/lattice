"""Phase 1 tests for the auto-outliner's diagnostic summary.

The full prompt-output pairing is covered by integration tests; here we
verify the deterministic post-processing: when Claude returns a richly-
tagged outline we surface the correct counts, and when it returns a
flat user_synthesis-only outline we warn loudly so the operator notices."""
from __future__ import annotations

from types import SimpleNamespace

from lattice.ingester.auto_outliner import structure_outline_with_report


class _StubLLM:
    """Returns a fixed canned response."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, system: str, user: str, model=None,
                       temperature: float = 0.4, max_tokens: int = 4096):
        return SimpleNamespace(text=self._text)


_RICH_RESPONSE = """# THESIS

A thesis sentence.

# A. First section [role: introduction]

  - First empirical claim. [type: empirical] [role: evidence] [evidence_status: source_hint] [ref: smith_2020] [importance: 0.8]
  - A definition. [type: definition] [role: setup] [importance: 0.4]
  - MY VIEW: synthesis line. [type: user_synthesis] [importance: 0.95] [supports: thesis]

# B. Mechanisms [role: argumentative]

  - Mechanistic claim. [type: empirical] [mechanism: rising throughput drives Wright's-law decline] [evidence_status: source_hint] [ref: lee_2019]
  - Methodological note. [type: methodological] [qualifies: cl.a.1]

# Z. Conclusion [role: conclusion]

  - Closing. [type: user_synthesis] [supports: thesis]
"""


_FLAT_RESPONSE = """# THESIS

A thesis sentence.

# A. Section

  - One claim. [user_synthesis]
  - Two claim. [user_synthesis]
  - Three claim. [user_synthesis]
  - Four claim. [user_synthesis]

# Z. Conclusion [role: conclusion]

  - Closing. [user_synthesis]
"""


async def test_rich_response_produces_rich_summary() -> None:
    structured, summary = await structure_outline_with_report(
        prose="raw prose here", llm=_StubLLM(_RICH_RESPONSE)
    )
    assert summary.section_count == 3  # A, B, Z (THESIS heading is excluded)
    assert summary.claim_count == 6
    assert summary.typed_claim_count == 6
    assert summary.user_synthesis_claim_count == 2
    assert summary.mechanism_claim_count == 1
    assert summary.evidence_hint_count == 2  # the two source_hint claims
    assert summary.importance_set_count == 3  # 0.8, 0.4, 0.95 (others lack tag)
    assert summary.relationship_tag_count >= 3  # supports, qualifies, supports
    # Sparse-typing warning should NOT fire on a richly-typed response.
    codes = {w.code for w in summary.warnings}
    assert "auto_outliner_sparse_typing" not in codes


async def test_flat_response_warns_about_sparse_typing() -> None:
    """Backwards-compat path: if Claude returns the legacy
    everything-is-user-synthesis shape, the summary should fire a clear
    warning so the operator knows the new richer prompt didn't take."""
    structured, summary = await structure_outline_with_report(
        prose="raw prose", llm=_StubLLM(_FLAT_RESPONSE)
    )
    assert summary.claim_count == 5
    assert summary.typed_claim_count == 0
    codes = {w.code for w in summary.warnings}
    assert "auto_outliner_sparse_typing" in codes


async def test_unknown_type_in_response_warns() -> None:
    weird = """# THESIS

T.

# A. S

  - One. [type: not_a_real_type]

# Z. Conclusion [role: conclusion]

  - Closing. [type: user_synthesis]
"""
    structured, summary = await structure_outline_with_report(
        prose="raw", llm=_StubLLM(weird)
    )
    codes = [w.code for w in summary.warnings]
    assert "auto_outliner_unknown_type" in codes
