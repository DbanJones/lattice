"""Mechanism-boilerplate auditor.

Detects post-render prose that has the *shape* of a mechanism explanation
("X operates through Y", "creates asymmetric outcomes", "compresses
useful life") without actually carrying causal content. Boilerplate of
this form is what the renderer falls back on when the source claim and
its bound passages do not contain a real mechanism — the LLM, told to
"develop the mechanism in 2-4 sentences", produces plausible-sounding
prose that adds words without insight.

Emits a flag per match with default_mode=rewrite. The author resolves
via the existing flag-review TUI; accepted rewrites mark the cluster
dirty and re-render with stricter prompting.

Heuristic, not LLM-bound. Keeps the audit cheap; false positives are
low-cost (an extra flag the author dismisses).
"""

from __future__ import annotations

import re

from ..graph.models import AuditFlag, Cluster, EditMode, FlagCategory, Severity
from .base import AuditCheck, iter_paragraphs


# Patterns that indicate mechanism-shaped boilerplate. The structure is
# typically: a generic abstract-noun subject + verb + abstract-noun object,
# arranged to *look like* a causal explanation while saying very little.
#
# Each entry is (regex, rule_id_suffix, brief_description).
_BOILERPLATE_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"\bthe mechanism (?:operates|works) through\b", re.IGNORECASE),
        "mechanism_operates_through",
        "Generic 'the mechanism operates through ...' construction.",
    ),
    (
        re.compile(r"\bthe mechanism is straightforward\b", re.IGNORECASE),
        "mechanism_is_straightforward",
        "'The mechanism is straightforward' is filler — if it is, "
        "show it without the meta-introduction.",
    ),
    (
        re.compile(
            r"\bcreates? (?:asymmetric|divergent|systematic) "
            r"(?:outcomes?|pathways?|patterns?|effects?|consequences?)\b",
            re.IGNORECASE,
        ),
        "creates_abstract_outcomes",
        "Abstract-noun causal claim ('creates asymmetric outcomes') "
        "without naming the operative mechanism.",
    ),
    (
        re.compile(
            r"\bcompress(?:es|ing)? (?:economic |operational |asset |competitive )?"
            r"(?:useful )?(?:life|cycle|window|horizon)s?\b",
            re.IGNORECASE,
        ),
        "compresses_useful_life",
        "Generic 'compresses useful life' phrasing — name the specific "
        "shortening, not the abstraction.",
    ),
    (
        re.compile(
            r"\b(?:signals?|reveal(?:s|ed|ing)?) "
            r"(?:future operational \w+|"
            r"(?:\w+\s+){0,3}through (?:optionality|revealed preference|abstract \w+))\b",
            re.IGNORECASE,
        ),
        "signals_through_phrasing",
        "'Signals through optionality / reveal X through optionality' "
        "constructions — abstract; specify what is actually being signalled.",
    ),
    (
        re.compile(
            r"\b(?:embeds?|encodes?) (?:divergent|asymmetric|differential) "
            r"\w+",
            re.IGNORECASE,
        ),
        "embeds_divergent",
        "'Embeds divergent futures / encodes asymmetric expectations' — "
        "abstract restatement; name the specific divergence.",
    ),
    (
        re.compile(
            r"\bshifts? (?:recognition|attribution|the burden) "
            r"(?:of|to|from) ",
            re.IGNORECASE,
        ),
        "shifts_recognition",
        "'Shifts recognition of X forward in time' — vague; name the "
        "specific accounting move and what it conceals.",
    ),
    (
        re.compile(r"\bcreates? a measurement illusion\b", re.IGNORECASE),
        "measurement_illusion",
        "'Creates a measurement illusion' is descriptive label rather "
        "than mechanism — name what is mismeasured and why.",
    ),
    (
        re.compile(
            r"\b(?:through|via) "
            r"(?:overcapacity|utilisation|capital|operational|accounting|temporal) "
            r"dynamics\b",
            re.IGNORECASE,
        ),
        "abstract_dynamics",
        "'Through X dynamics' — generic; describe what actually happens.",
    ),
    (
        re.compile(
            r"\bdecoupl(?:es|ing|ed) [^.]{1,80} through "
            r"(?:utilisation|capacity|adjustment|optimisation|optionality|"
            r"strategic \w+)\b",
            re.IGNORECASE,
        ),
        "decoupling_through",
        "'Decoupling X from Y through Z' — abstract; name the actual "
        "mechanism that breaks the linkage.",
    ),
    (
        re.compile(
            r"\b(?:capital|infrastructure|technological|financial|temporal) "
            r"(?:flexibility|optionality|discretion|elasticity) "
            r"(?:creates?|introduces?|produces?)\b",
            re.IGNORECASE,
        ),
        "abstract_quality_creates",
        "Generic 'X flexibility creates Y' construction without "
        "naming the specific mechanism the flexibility provides.",
    ),

    # ─── Phase 2 patterns: abstract-noun-verb-abstract-noun shapes ──
    # These catch the wikipedia-summary cadence the LLM produces under
    # elaboration directives — sentences that read as causal explanation
    # but say very little. They typically chain abstract nouns through
    # generic causal verbs, often capped with "through a mechanism that".

    (
        re.compile(
            r"\bthrough (?:a |the )?mechanism(?: that| rooted| reflecting| of| operating)?\b",
            re.IGNORECASE,
        ),
        "through_a_mechanism",
        "'Through a mechanism that...' is the canonical mechanism-shaped "
        "filler — name the actual mechanism rather than gesturing at one.",
    ),
    (
        re.compile(
            r"\bcreates? (?:a )?[\w\s\-]{1,40}?"
            r"(?:illusion|coupling|ambiguity|asymmetry|volatility|misalignment|"
            r"opportunity|inefficiency|distortion|optimisation|optimization|"
            r"capacity|tension|friction|constraint|equilibrium)\b",
            re.IGNORECASE,
        ),
        "creates_abstract_concept",
        "'Creates X coupling / illusion / ambiguity' construction — "
        "the noun is the label, not the mechanism. State what produces "
        "the effect concretely.",
    ),
    (
        re.compile(
            r"\b(?:decouples?|couples?|amplif(?:y|ies))"
            r" [^.]{1,40} from [^.]{1,40} through\b",
            re.IGNORECASE,
        ),
        "decouples_from_through",
        "'Decouples X from Y through Z' — abstract; name the operative "
        "principle rather than the relational frame.",
    ),
    (
        re.compile(
            r"\b(?:persist|persists|remain|remains)"
            r" through (?:institutional|organisational|organizational|"
            r"structural|systemic|cultural|market) (?:inertia|forces|"
            r"pressures|incentives|misalignment|dynamics)\b",
            re.IGNORECASE,
        ),
        "persists_through_institutional",
        "'Persists through institutional inertia' — abstract diagnosis. "
        "Name the specific incentive or actor whose behaviour sustains "
        "the inefficiency.",
    ),
    (
        re.compile(
            r"\b(?:prioritise|prioritize|optimise|optimize|favour|favor)"
            r"s? [\w\s\-]{1,40}? over [\w\s\-]{1,40}? through\b",
            re.IGNORECASE,
        ),
        "prioritise_x_over_y_through",
        "'Prioritises X over Y through a mechanism' — generic; describe "
        "the actual decision logic the actors apply.",
    ),
    (
        re.compile(
            r"\b(?:reveal(?:s|ed|ing)?|expose(?:s|d)?|exhibit(?:s|ed)?) "
            r"(?:operator |investor |market |industry |systemic )?"
            r"(?:uncertainty|confidence|preference|expectations|conviction) "
            r"(?:about|over|regarding|through)\b",
            re.IGNORECASE,
        ),
        "reveals_abstract_belief",
        "'Reveals operator uncertainty about X' — abstract belief-talk. "
        "Quote or paraphrase the actor's actual signal.",
    ),
    (
        re.compile(
            r"\b(?:inflates?|deflates?|distorts?) (?:actual |true |reported |effective )"
            r"[\w\s\-]{1,40}? (?:beyond|above|below|relative to)\b",
            re.IGNORECASE,
        ),
        "inflates_actual_beyond",
        "'Inflates actual X beyond Y' — abstract magnitude framing. "
        "State the figure or factor by which the distortion runs.",
    ),
    (
        re.compile(
            r"\b(?:eliminat(?:e|es|ing)|destroy(?:s|ed|ing)?|undermin(?:e|es|ing)) "
            r"\w+ (?:motivation|incentive|rationale|justification) "
            r"(?:through|via)\b",
            re.IGNORECASE,
        ),
        "eliminates_motivation_through",
        "'Eliminates X motivation through Y misalignment' — abstract "
        "incentive-talk. Name the specific actor and the saving they "
        "do not capture.",
    ),
    (
        re.compile(
            r"\bin a way that (?:national|aggregate|conventional|standard|"
            r"published|existing|grid|continental) \w+ cannot \w+\b",
            re.IGNORECASE,
        ),
        "in_a_way_that_x_cannot",
        "'In a way that national averages cannot capture' — vague "
        "negation. State what the averages miss and what the correct "
        "calculation would be.",
    ),
]


class MechanismBoilerplateCheck(AuditCheck):
    """Flag mechanism-shaped sentences that lack mechanism content."""

    category: FlagCategory = FlagCategory.voice
    default_severity: Severity = Severity.standard
    default_mode: EditMode = EditMode.rewrite

    async def check_cluster(self, cluster: Cluster, prose: str) -> list[AuditFlag]:
        flags: list[AuditFlag] = []
        for paragraph_idx, para_start, _para_end, paragraph in iter_paragraphs(prose):
            for pattern, rule_suffix, description in _BOILERPLATE_PATTERNS:
                for match in pattern.finditer(paragraph):
                    # Surface the surrounding sentence rather than just the
                    # matched fragment, so the author can read the offence
                    # in context when reviewing flags.
                    sent_start, sent_end = _enclosing_sentence(
                        paragraph, match.start(), match.end()
                    )
                    sentence = paragraph[sent_start:sent_end].strip()
                    flags.append(self._make_flag(
                        cluster=cluster,
                        rule_id=f"voice.boilerplate.{rule_suffix}",
                        offending_text=sentence[:240],
                        char_start=para_start + sent_start,
                        char_end=para_start + sent_end,
                        rule_description=description,
                        suggestion=(
                            "Replace the abstract construction with a specific "
                            "mechanism: name the operative principle, the "
                            "actors involved, or the concrete causal step. If "
                            "no real mechanism is available, drop the sentence "
                            "entirely rather than padding."
                        ),
                        paragraph_index=paragraph_idx,
                    ))
        return flags


_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])|(?<=[.!?])$")


def _enclosing_sentence(text: str, start: int, end: int) -> tuple[int, int]:
    """Return (sent_start, sent_end) bounding the sentence containing
    ``[start:end]`` within ``text``."""
    # Walk back to the start of the sentence.
    sent_start = 0
    for match in _SENTENCE_END_RE.finditer(text, 0, start):
        sent_start = match.end()
    # Walk forward to the end of the sentence.
    sent_end = len(text)
    forward = _SENTENCE_END_RE.search(text, end)
    if forward:
        sent_end = forward.start() + 1
    return sent_start, sent_end
