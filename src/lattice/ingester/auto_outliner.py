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


# Top-level (``# A.``), nested subsection (``## A.1``,
# ``### A.1.2``), or the THESIS marker. Any of these means the file
# already contains a lattice outline and the auto-structurer should
# leave it alone.
_HEADER_RE = re.compile(
    r"^\s*#+\s+(THESIS|[A-Z](?:\.\d+)*\.?)\s",
    re.MULTILINE,
)
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


_SYSTEM_PROMPT_TEMPLATE = """You are converting raw academic paper prose into a Lattice outline.

Lattice outlines have a strict, parser-friendly format. Every line and tag matters because a deterministic parser reads this file:

    # THESIS

    [One-sentence thesis statement.]

    # A. [First section heading]

      - [Claim 1, written as a single declarative sentence.] [user_synthesis]
      - [Claim 2.] [user_synthesis]
      - MY VIEW: [Author's analytical synthesis.] [user_synthesis]

    ## A.1 [Subsection heading, only if the section has multiple distinct themes]

      - [Sub-claim 1.] [user_synthesis]
      - [Sub-claim 2.] [user_synthesis]

    ### A.1.1 [Sub-subsection, only when warranted by depth of source material]

      - [Deep claim.] [user_synthesis]

    # B. [Next section heading]

      - [Claim.] [user_synthesis]

    # Z. Conclusion [role: conclusion]

      - [Closing claim that ties the argument back to the thesis.] [user_synthesis]

Rules you MUST follow:
1. Always begin with `# THESIS` followed by a blank line and exactly one sentence.
2. Top-level sections use `# A.`, `# B.`, `# C.`, ... — the letter MUST be followed by a period and a space.
3. **{NESTING_RULE}**
4. Each claim is a `  - ` bullet (two spaces, a dash, a space) followed by the claim sentence and at least one tag in square brackets.
5. **Tag EVERY claim with `[user_synthesis]`.** This signals that the claim is the author's own restating of the argument, which makes the claim renderable without external evidence bindings. Other tags (`[strong]`, `[empirical]`, etc.) require evidence bindings the project may not have.
6. **The final section MUST be `# Z. Conclusion [role: conclusion]`** (use the next available letter and the literal tag `[role: conclusion]` — note the COLON, not equals). Lattice's voice template requires a closing section.
7. **Be thorough — extract every distinct claim the paper makes**, not just a high-level summary. A typical 10-30 page paper produces 40-100+ claims across its sections. Don't compress aggressively; the author can prune later.
8. Let the source's structure dictate the breadth: papers with 8-10 distinct argumentative moves should produce 8-10 sections. Don't artificially cap section count.
9. Do NOT invent claims that aren't in the source. Stay faithful to what the source argues.
10. Do NOT include source citations, references lists, figure captions, or author affiliations — only the argumentative spine.
11. Section headings should be short (3-7 words).
12. Output ONLY the markdown. No preamble, no explanation, no code fences.
"""


_NESTING_RULES = {
    1: (
        "Use a flat structure — top-level sections only (``# A.``, "
        "``# B.``, ...). Do NOT use ``##`` or ``###`` subsection "
        "headings. All claims for a section live directly under that "
        "section's ``# X. Title`` heading."
    ),
    2: (
        "Use nested subsections (``## A.1 Title``) whenever a top-level "
        "section covers more than one distinct theme. A section that "
        "ends up with more than ~6 claims almost always benefits from "
        "being split into 2-3 subsections grouped by theme. Do NOT use "
        "third-level ``###`` headings — keep nesting to two levels max."
    ),
    3: (
        "Use nested subsections (``## A.1 Title``, ``### A.1.1 Title``) "
        "whenever a top-level section covers more than one distinct "
        "theme, and a subsection covers more than one sub-theme. A "
        "section with >6 claims almost always benefits from being split "
        "into 2-3 subsections; a subsection with >6 claims often "
        "benefits from a further split."
    ),
}


def _system_prompt(max_depth: int) -> str:
    """Compose the system prompt with the right nesting rule for the
    requested ``max_depth`` (1 = flat, 2 = subsections, 3 = sub-sub)."""
    rule = _NESTING_RULES.get(max_depth, _NESTING_RULES[2])
    return _SYSTEM_PROMPT_TEMPLATE.replace("{NESTING_RULE}", rule)


def _user_prompt(prose: str) -> str:
    # Send up to 100k chars (~25k tokens) so Claude sees the whole paper
    # for typical academic-length documents. Earlier 24k cap forced
    # Claude to summarise from the abstract + intro alone, missing
    # claims from later sections. The model handles 200k+ token context
    # comfortably; this is only truncated for genuinely huge inputs.
    trimmed = prose.strip()
    if len(trimmed) > 100_000:
        trimmed = trimmed[:100_000] + "\n\n[...document truncated for length...]"
    return (
        "Convert the following raw paper text into a Lattice outline. "
        "Extract the central thesis, every distinct argumentative "
        "section, and every claim made in each section. Use nested "
        "subsections (``## A.1``, ``### A.1.1``) when a section "
        "covers more than one theme. Be thorough — don't summarise "
        "to a sketch.\n\n"
        "---BEGIN PAPER---\n"
        f"{trimmed}\n"
        "---END PAPER---\n"
    )


async def structure_outline(
    prose: str,
    llm: _LLMProtocol,
    max_depth: int = 2,
) -> str:
    """Ask Claude to convert raw paper prose into a lattice outline.

    ``max_depth`` controls how deep the section tree is allowed to go:
      1 = flat, ``# A.`` only
      2 = subsections, ``# A.`` + ``## A.1`` (default)
      3 = sub-sub, also allows ``### A.1.1``

    Returns the structured markdown. Raises ``RuntimeError`` if the
    model output doesn't look like a lattice outline (defensive — we'd
    rather fail loudly than save garbage to disk)."""
    response = await llm.complete(
        system=_system_prompt(max_depth),
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
