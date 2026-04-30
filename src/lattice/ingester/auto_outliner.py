"""LLM-driven auto-structurer for raw academic prose.

When a user uploads / pastes raw paper text into ``structure/outline.md``
(no ``# THESIS`` / ``# A.`` markers), the markdown ingester produces an
empty graph and the pipeline fails with ``outline_has_no_structure``.

This module bridges that gap: ``structure_outline`` takes raw prose and
returns a lattice-format markdown outline by asking Claude to extract
the thesis, sections, and per-section claims.

The output is intentionally conservative: only structure that the
ingester can parse, no inline citation tagging. Users can always edit
the result before re-running.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol


_HEADER_RE = re.compile(r"^\s*#\s+(THESIS|[A-Z]\.)", re.MULTILINE)
_BULLET_RE = re.compile(r"^(\s*-\s+.+?)(\s*)$", re.MULTILINE)
_USER_SYNTHESIS_RE = re.compile(r"\[user_synthesis\]", re.IGNORECASE)
_CONCLUSION_TAG_RE = re.compile(r"\[role\s*[:=]\s*conclusion\]", re.IGNORECASE)
_TITLE_CONCLUSION_RE = re.compile(r"^\s*#\s+[A-Z]\.\s+.*\b(conclusion|discussion)\b", re.IGNORECASE | re.MULTILINE)


def looks_like_lattice_outline(text: str) -> bool:
    """True if the text already contains at least one ``# THESIS`` or
    ``# A.``-style header. Used to skip auto-structuring when the user
    has already done it themselves."""
    return bool(_HEADER_RE.search(text))


def has_conclusion_section(text: str) -> bool:
    """True if the outline declares a conclusion section, either via a
    ``[role: conclusion]`` (or ``[role=conclusion]``) tag on the heading
    or a section title that contains 'conclusion' / 'discussion'."""
    return bool(_CONCLUSION_TAG_RE.search(text) or _TITLE_CONCLUSION_RE.search(text))


def has_conclusion_role_tag(text: str) -> bool:
    """True if the outline contains an explicit `[role: conclusion]`
    (or `=` form) tag. Stricter than ``has_conclusion_section`` —
    this only matches the structural tag, not a heading title."""
    return bool(_CONCLUSION_TAG_RE.search(text))


def normalise_to_user_synthesis(text: str) -> tuple[str, int]:
    """Append ``[user_synthesis]`` to every claim bullet that doesn't
    already have it. Returns ``(rewritten_text, claims_changed_count)``.

    This is the deterministic recovery path for outlines that have
    valid lattice structure but use non-user_synthesis tags
    (``[empirical]``, ``[strong]``, etc.) on claims the project has
    no evidence to back. Without this, the renderer's grounding check
    fails on every such claim and the document is refused.
    """
    changed = 0

    def _rewrite_bullet(match: re.Match[str]) -> str:
        nonlocal changed
        body = match.group(1)
        trailing = match.group(2) or ""
        if _USER_SYNTHESIS_RE.search(body):
            return match.group(0)
        # `MY VIEW:` bullets are detected by ingester as user_synthesis
        # already (via the prefix), so don't double-tag them.
        if re.match(r"^\s*-\s+(MY VIEW|COUNTER):", body, re.IGNORECASE):
            return match.group(0)
        changed += 1
        return f"{body} [user_synthesis]{trailing}"

    rewritten = _BULLET_RE.sub(_rewrite_bullet, text)
    return rewritten, changed


def append_conclusion_section(text: str) -> str:
    """Append a default conclusion section if the outline doesn't have
    one. The section gets a single user_synthesis claim that the user
    can replace."""
    section_letters = re.findall(r"^\s*#\s+([A-Z])\.", text, re.MULTILINE)
    next_letter = "Z"
    if section_letters:
        last = max(section_letters)
        if last < "Z":
            next_letter = chr(ord(last) + 1)
    body = text.rstrip() + (
        f"\n\n# {next_letter}. Conclusion [role: conclusion]\n\n"
        f"  - Summary of the argument and its implications. [user_synthesis]\n"
    )
    return body


class _LLMProtocol(Protocol):
    async def complete(
        self,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int = 4096,
    ): ...


_SYSTEM_PROMPT = """You are converting raw academic paper prose into a Lattice outline.

Lattice outlines have a strict, parser-friendly format. Every line and tag matters because a deterministic parser reads this file:

    # THESIS

    [One-sentence thesis statement.]

    # A. [First section heading]

      - [Claim 1, written as a single declarative sentence.] [user_synthesis]
      - [Claim 2.] [user_synthesis]
      - MY VIEW: [Author's analytical synthesis.] [user_synthesis]

    # B. [Next section heading]

      - [Claim.] [user_synthesis]

    # Z. Conclusion [role: conclusion]

      - [Closing claim that ties the argument back to the thesis.] [user_synthesis]

Rules you MUST follow:
1. Always begin with `# THESIS` followed by a blank line and exactly one sentence.
2. Use `# A.`, `# B.`, `# C.`, ... for each subsequent section. The letter MUST be followed by a period and a space.
3. Each claim is a `  - ` bullet (two spaces, a dash, a space) followed by the claim sentence and at least one tag in square brackets.
4. **Tag EVERY claim with `[user_synthesis]`.** This signals that the claim is the author's own restating of the argument, which makes the claim renderable without external evidence bindings (we don't have a sources library yet for this project). Other tags (`[strong]`, `[empirical]`, etc.) require evidence bindings the project doesn't have, so they will fail to render.
5. **The final section MUST be `# Z. Conclusion [role: conclusion]`** (use the next available letter and the literal tag `[role: conclusion]` — note the COLON, not equals). Lattice's voice template requires a closing section.
6. Do NOT invent claims that aren't in the source. Stay faithful to what the source paper argues.
7. Do NOT include source citations, references lists, figure captions, or author affiliations — only the argumentative spine.
8. Section headings should be short (3-7 words). Aim for 4-7 sections total, including the conclusion.
9. Output ONLY the markdown. No preamble, no explanation, no code fences.
"""


def _user_prompt(prose: str) -> str:
    # Truncate aggressively if the document is huge — first ~24k chars
    # is normally enough to capture the abstract + introduction +
    # methods + first results, which is where the thesis and section
    # structure live.
    trimmed = prose.strip()
    if len(trimmed) > 24000:
        trimmed = trimmed[:24000] + "\n\n[...document truncated for length...]"
    return (
        "Convert the following raw paper text into a Lattice outline. "
        "Extract the central thesis, the major sections, and the key "
        "claims under each section.\n\n"
        "---BEGIN PAPER---\n"
        f"{trimmed}\n"
        "---END PAPER---\n"
    )


async def structure_outline(prose: str, llm: _LLMProtocol) -> str:
    """Ask Claude to convert raw paper prose into a lattice outline.

    Returns the structured markdown. Raises ``RuntimeError`` if the
    model output doesn't look like a lattice outline (defensive — we'd
    rather fail loudly than save garbage to disk)."""
    response = await llm.complete(
        system=_SYSTEM_PROMPT,
        user=_user_prompt(prose),
    )
    structured = (response.text or "").strip()
    # Strip code fences if the model wrapped its output despite our
    # instruction.
    if structured.startswith("```"):
        # Remove the opening fence (and optional language tag) and the
        # trailing fence.
        lines = structured.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        structured = "\n".join(lines).strip()

    if not looks_like_lattice_outline(structured):
        raise RuntimeError(
            "Auto-outliner returned text without `# THESIS` or `# A.` "
            "headers; refusing to overwrite outline.md. First 500 "
            f"chars: {structured[:500]!r}"
        )
    return structured


def write_structured_outline(
    project_path: Path, structured: str, raw_text: str
) -> Path:
    """Persist the structured outline to ``structure/outline.md`` and
    archive the original raw text to ``structure/outline.raw.md`` so
    the user can inspect what was converted."""
    structure_dir = project_path / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    raw_path = structure_dir / "outline.raw.md"
    if not raw_path.exists():  # don't clobber an earlier raw save
        raw_path.write_text(raw_text, encoding="utf-8")
    target = structure_dir / "outline.md"
    target.write_text(structured, encoding="utf-8")
    return target
