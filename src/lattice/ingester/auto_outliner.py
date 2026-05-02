"""LLM-driven auto-structurer for raw academic prose.

When a user uploads / pastes raw paper text into ``structure/outline.md``
(no ``# THESIS`` / ``# A.`` markers), the markdown ingester produces an
empty graph and the pipeline fails with ``outline_has_no_structure``.

This module bridges that gap: ``structure_outline`` takes raw prose and
returns a lattice-format markdown outline by asking Claude to extract
the thesis, sections, and per-section claims.

The output now includes richer per-claim metadata (claim type, role,
mechanism, citation hints, source-span excerpts) so downstream stages
have something to work with before the enricher runs. The markdown is
still the editable artifact — Claude emits the richer tags using the
vocabulary the markdown ingester already parses, so there is one source
of truth.

``structure_outline`` keeps its old return type (markdown string) for
callers that don't care about diagnostics; ``structure_outline_with_report``
returns the same markdown plus an ``AutoOutlinerReport`` summarising
what was extracted and any tag-shape issues spotted in the response.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..graph.models import (
    AutoOutlinerSummary,
    ScaffoldWarning,
    ScaffoldWarningLevel,
)

# Re-export the model under its old name so external callers that
# imported ``AutoOutlinerReport`` keep working.
AutoOutlinerReport = AutoOutlinerSummary


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

Lattice outlines have a strict, parser-friendly format. Every line and tag matters because a deterministic parser reads this file. Your job is to produce a richly-tagged scaffold that downstream stages can build on, not a flat summary.

Example shape (see Tag vocabulary below for the full list):

    # THESIS

    [One-sentence thesis statement.]

    # A. [First section heading] [role: introduction]

      - [Claim 1: an empirical finding from the source, declarative.] [type: empirical] [role: evidence] [evidence_status: source_hint] [ref: smith_2020] [importance: 0.7]
      - [Claim 2: a definition the paper sets up.] [type: definition] [role: setup] [evidence_status: unbound]
      - MY VIEW: [Author's analytical synthesis tying the section back to the thesis.] [type: user_synthesis] [importance: 0.9] [supports: thesis]

    ## A.1 [Subsection heading, only if the section has multiple distinct themes]

      - [Sub-claim with a known mechanism.] [type: empirical] [mechanism: increased throughput drives Wright's-law cost decline] [evidence_status: source_hint] [ref: lee_2019]

    # B. [Next section heading] [role: argumentative]

      - [Claim that qualifies a claim from section A.] [type: methodological] [qualifies: cl.a.1]
      - COUNTER: [A claim from the literature the paper rebuts.] [type: empirical] [contradicts: thesis]

    # Z. Conclusion [role: conclusion]

      - [Closing claim that ties the argument back to the thesis.] [type: user_synthesis] [supports: thesis]

Rules you MUST follow:
1. Always begin with `# THESIS` followed by a blank line and exactly one sentence.
2. Top-level sections use `# A.`, `# B.`, `# C.`, ... — the letter MUST be followed by a period and a space. Add `[role: introduction|argumentative|evidence_synthesis|methodological|counterargument|conclusion]` when it's clear from the section's purpose.
3. **{NESTING_RULE}**
4. Each claim is a `  - ` bullet (two spaces, a dash, a space) followed by the claim sentence and one or more tags in square brackets.
5. **Tag every claim with `[type: ...]`** picking the value that fits: `empirical` (a fact the source asserts), `methodological` (a statement about method/measurement), `normative` (a value judgement), `definition` (terminological scaffolding), `user_synthesis` (the author's own restating). When in doubt between empirical and user_synthesis, prefer `empirical` if the source provides supporting data, `user_synthesis` only when the claim is the author's own analytical move.
6. **For every non-`user_synthesis` claim, declare its evidence state** with one of:
   - `[evidence_status: source_hint]` — you can identify a citation in the source text but the project may not have the corresponding paper indexed yet. Pair with `[ref: <citekey>]` using a stable lowercase slug (e.g. `[ref: koomey_2015]`).
   - `[evidence_status: unbound]` — the claim is in the source but you cannot pin down a specific citation.
   - Omit `evidence_status` if the claim is `[type: user_synthesis]`.
7. **Use `[mechanism: <causal middle link>]`** for analytical/empirical claims that have a clear "X happens *because* Y" structure. Capture the mechanism in the author's own terms — short, declarative, no hedging.
8. **Use `[role: setup|evidence|mechanism|narrative|limit|complication|counterargument|synthesis|conclusion]`** to describe the role the claim plays inside its section. This drives later cluster planning.
9. **Use `[importance: 0.X]`** (a float in [0, 1]) for the claim's weight in the document's overall argument. Default to 0.5 when uncertain. Reserve 0.9+ for thesis-bearing claims and ≤0.3 for asides.
10. **Use relationship tags to wire claims together** when the source makes the link explicit:
    - `[supports: cl.<id>]`, `[contradicts: cl.<id>]`, `[qualifies: cl.<id>]`, `[extends: cl.<id>]`, `[depends_on: cl.<id>]`, `[pivot: cl.<id>]` (for an interpretive pivot — a sharp two-move analytical structure that reframes how the target should be read).
    - Use `[supports: thesis]` / `[contradicts: thesis]` when the link is to the central thesis. Prefix `MY VIEW:` already implies `[supports: thesis]`; prefix `COUNTER:` already implies `[contradicts: thesis]`.
    - You can reference a claim that appears *later* in the outline — the parser resolves targets after the full file is read.
11. **Use `[source_excerpt: "verbatim quote"]`** when the claim is paraphrased from a specific quotable span in the source. Keep excerpts under 200 characters and faithful to the original wording.
12. **Use `[scope: condition]`** for empirical claims with explicit scope conditions (population, time period, measurement regime).
13. **The final section MUST be `# Z. Conclusion [role: conclusion]`** (use the next available letter and the literal tag `[role: conclusion]` — note the COLON, not equals).
14. **Be thorough — extract every distinct claim the paper makes**, not just a high-level summary. A typical 10-30 page paper produces 40-100+ claims across its sections. Don't compress aggressively; the author can prune later.
15. Let the source's structure dictate the breadth: papers with 8-10 distinct argumentative moves should produce 8-10 sections. Don't artificially cap section count.
16. Do NOT invent claims that aren't in the source. Stay faithful to what the source argues. Where you cannot identify a citation but the claim is empirical, mark it `[evidence_status: unbound]` rather than upgrading it to `user_synthesis`.
17. Do NOT include source citations, references lists, figure captions, or author affiliations as claims — only the argumentative spine.
18. Section headings should be short (3-7 words).
19. Output ONLY the markdown. No preamble, no explanation, no code fences.
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


# Tag families the report inspects. Keep in sync with the markdown
# ingester — we only count tags the parser actually understands.
_TYPE_VALUES = {"empirical", "methodological", "normative", "user_synthesis", "definition"}
_RELATIONSHIP_TAG_NAMES = {
    "supports", "contradicts", "qualifies", "extends",
    "depends_on", "pivot", "interpretive_pivot",
}
_TAG_INSPECT_RE = re.compile(r"\[([^\]]+)\]")


def _summarise_outline(
    structured: str, max_depth: int, raw_response: str
) -> AutoOutlinerSummary:
    """Walk the structured markdown and produce a summary of how rich
    the extraction was. Pure inspection — no parsing of claim semantics
    beyond counting tags by family."""
    report = AutoOutlinerSummary(
        generated_at=datetime.now(timezone.utc),
        max_depth=max_depth,
        raw_response_preview=raw_response[:500],
    )

    section_count = 0
    bullet_lines = []
    for line in structured.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") and not stripped.startswith("# THESIS"):
            section_count += 1
        elif stripped.startswith("- "):
            bullet_lines.append(stripped)

    report.section_count = section_count
    report.claim_count = len(bullet_lines)

    for bullet in bullet_lines:
        tags_in_line: dict[str, str] = {}
        flags_in_line: set[str] = set()
        for match in _TAG_INSPECT_RE.finditer(bullet):
            body = match.group(1).strip()
            sep_index = -1
            for sep in (":", "="):
                idx = body.find(sep)
                if idx >= 0 and (sep_index == -1 or idx < sep_index):
                    sep_index = idx
            if sep_index >= 0:
                key = body[:sep_index].strip()
                val = body[sep_index + 1 :].strip()
                tags_in_line[key] = val
            else:
                flags_in_line.add(body)

        type_value = tags_in_line.get("type")
        if type_value:
            report.typed_claim_count += 1
            if type_value not in _TYPE_VALUES:
                report.warnings.append(
                    ScaffoldWarning(
                        level=ScaffoldWarningLevel.warning,
                        code="auto_outliner_unknown_type",
                        message=(
                            f"Claude emitted [type: {type_value}], which is "
                            "not in the accepted vocabulary. The ingester "
                            "will fall back to its default."
                        ),
                        raw=bullet,
                    )
                )
        if type_value == "user_synthesis" or "user_synthesis" in flags_in_line:
            report.user_synthesis_claim_count += 1
        if "mechanism" in tags_in_line:
            report.mechanism_claim_count += 1
        if "ref" in tags_in_line or tags_in_line.get("evidence_status") == "source_hint":
            report.evidence_hint_count += 1
        if "importance" in tags_in_line:
            try:
                value = float(tags_in_line["importance"])
            except ValueError:
                report.warnings.append(
                    ScaffoldWarning(
                        level=ScaffoldWarningLevel.warning,
                        code="auto_outliner_bad_importance",
                        message=(
                            f"Claude emitted non-numeric "
                            f"[importance: {tags_in_line['importance']}]"
                        ),
                        raw=bullet,
                    )
                )
            else:
                if 0.0 <= value <= 1.0:
                    report.importance_set_count += 1
        for rel_tag in _RELATIONSHIP_TAG_NAMES:
            if rel_tag in tags_in_line:
                report.relationship_tag_count += 1

    # Quality signal: if Claude tagged < 50% of claims with [type:], the
    # output is much closer to the legacy "everything is user_synthesis"
    # collapse than the richer scaffold we want.
    if report.claim_count and report.typed_claim_count / report.claim_count < 0.5:
        report.warnings.append(
            ScaffoldWarning(
                level=ScaffoldWarningLevel.warning,
                code="auto_outliner_sparse_typing",
                message=(
                    f"Only {report.typed_claim_count}/{report.claim_count} "
                    "claims got an explicit [type: ...] tag. The ingest "
                    "result will lean heavily on default empirical/user_"
                    "synthesis inference."
                ),
            )
        )

    return report


async def _call_claude(prose: str, llm: _LLMProtocol, max_depth: int) -> str:
    """Shared LLM call + sanitisation used by both ``structure_outline``
    and ``structure_outline_with_report``. Returns the cleaned markdown."""
    response = await llm.complete(
        system=_system_prompt(max_depth),
        user=_user_prompt(prose),
    )
    structured = (response.text or "").strip()
    # Strip code fences if the model wrapped its output despite our
    # instruction.
    if structured.startswith("```"):
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
    rather fail loudly than save garbage to disk).

    Use ``structure_outline_with_report`` if you want a summary of what
    Claude produced (tag richness, warnings) alongside the markdown.
    """
    return await _call_claude(prose, llm, max_depth)


async def structure_outline_with_report(
    prose: str,
    llm: _LLMProtocol,
    max_depth: int = 2,
) -> tuple[str, AutoOutlinerSummary]:
    """Same as ``structure_outline`` plus an ``AutoOutlinerSummary``
    describing how rich the extraction was. Used by activities that
    persist a scaffold report alongside the outline."""
    structured = await _call_claude(prose, llm, max_depth)
    report = _summarise_outline(structured, max_depth, raw_response=structured)
    return structured, report


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
