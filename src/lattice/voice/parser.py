"""Voice file parser.

A voice file is markdown with YAML frontmatter. The frontmatter parses
into a structured Voice model. The markdown body is preserved as `notes`.

See docs/VOICE_FORMAT.md for the full format spec.
See examples/voices/academic.voice.md for a worked example.

IMPLEMENTATION NOTES FOR THE CODING AGENT:

1. Use python-frontmatter or roll your own with yaml + a separator regex.
2. Validate against the Voice pydantic model below.
3. Support `extends:` for voice inheritance with deep merge.
4. Surface validation errors clearly (line numbers if possible).

The Voice model intentionally allows `extra = "allow"` because voices may
gain new fields over time. Don't fail on unknown fields; warn.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Literal

# Silence pydantic's "register shadows BaseModel attribute" warning.
# The field is named `register` to match the voice YAML schema; renaming
# would break the public API.
warnings.filterwarnings(
    "ignore",
    message=r'Field name "register".*shadows an attribute.*',
    category=UserWarning,
)

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────────────────────────────────────
# Voice sub-models
# ─────────────────────────────────────────────────────────


class ArchitectureConfig(BaseModel):
    template: Literal[
        "six_element_paper",
        "nature_compressed",
        "review_paper",
        "policy_brief",
        "journalistic_feature",
        "freeform",
    ]
    hourglass_required: bool = False
    killer_graph_first: bool = False
    skim_targets_must_be_strongest: list[str] = Field(default_factory=list)
    signposting: dict[str, str] = Field(default_factory=dict)


class ReportingVerbs(BaseModel):
    require_variety: bool = False
    direct_evidence: list[str] = Field(default_factory=list)
    correlational: list[str] = Field(default_factory=list)
    theoretical: list[str] = Field(default_factory=list)
    speculative: list[str] = Field(default_factory=list)


class CitationConfig(BaseModel):
    engagement_level: Literal["name_only", "name_claim", "name_claim_relevance"]
    reporting_verbs: ReportingVerbs
    synthesis_threshold: int = 3
    forbid_catalogue_pattern: bool = True
    positioning_required_for: list[str] = Field(default_factory=list)
    citation_purposes_allowed: list[str] = Field(default_factory=list)


class RegisterConfig(BaseModel):
    formality: Literal["formal", "neutral", "conversational", "urgent"]
    sentence_length: Literal["short", "medium", "long", "varied"]
    sentence_length_target_distribution: dict[str, float] = Field(default_factory=dict)
    hedge_density: Literal["none", "light", "calibrated", "heavy"] = "calibrated"
    lexicon: Literal["plain", "discipline", "elevated"] = "discipline"
    first_person: Literal["forbidden", "sparing", "natural", "primary"] = "sparing"
    contractions: Literal["forbidden", "allowed"] = "forbidden"


class StanceConfig(BaseModel):
    default: Literal["objective", "advocating", "sceptical", "instructive"] = "objective"
    user_synthesis_stance: Literal["cautious", "confident", "explicit_opinion", "lede"] = "cautious"
    unsupported_synthesis_treatment: Literal["flag_as_opinion", "lead_with", "omit", "prompt_author"] = "flag_as_opinion"
    counterclaim_treatment: Literal["dismiss", "acknowledge", "steelman"] = "steelman"
    uncertainty_display: Literal["hide", "mention", "explicit", "foreground"] = "explicit"


class AttributionConfig(BaseModel):
    style: Literal["harvard_inline", "footnote", "hyperlink", "embedded", "none"]
    first_mention: Literal["full", "short"] = "full"
    multiple_sources: Literal["group", "list", "synthesise"] = "synthesise"
    quote_threshold_words: int = 25
    page_specificity: Literal["always", "when_exact", "never"] = "when_exact"


class ParagraphConfig(BaseModel):
    shape: str = "claim_evidence_implication"
    length_sentences: list[int] = Field(default_factory=lambda: [4, 8])
    length_words_max: int = 250
    topic_sentence_required: bool = True
    topic_sentence_position: Literal["first", "first_or_second", "any"] = "first"
    cohesion: Literal["old_to_new", "any"] = "old_to_new"
    forbidden_paragraph_openers: list[str] = Field(default_factory=list)
    paragraph_open_varies: bool = True


class FiguresConfig(BaseModel):
    caption_required: bool = True
    caption_self_contained: bool = True
    first_mention_interprets: bool = True
    numbering: Literal["arabic", "roman", "section_dot_number"] = "arabic"
    list_of_figures: bool = True
    central_contribution_marker: bool = True


class StatisticsConfig(BaseModel):
    no_arithmetic_for_reader: bool = True
    reference_before_appearance: bool = True
    named_entities_accurate: bool = True


class ReviewPaperConfig(BaseModel):
    multiple_cuts_required: list[str] = Field(default_factory=list)
    per_source_treatment: list[str] = Field(default_factory=list)
    data_information_knowledge_hierarchy: Literal["enforced", "recommended", "off"] = "enforced"
    synthesis_density: Literal["low", "medium", "high"] = "high"
    multi_source_per_paragraph_target: float = 2.5


# ─────────────────────────────────────────────────────────
# Voice top-level model
# ─────────────────────────────────────────────────────────


class Voice(BaseModel):
    """A parsed voice file."""

    # protected_namespaces=() silences the "register shadows BaseModel attribute" warning.
    model_config = ConfigDict(extra="allow", protected_namespaces=())

    name: str
    description: str = ""
    extends: str | None = None

    architecture: ArchitectureConfig
    citation: CitationConfig
    register: RegisterConfig
    stance: StanceConfig
    attribution: AttributionConfig
    paragraph: ParagraphConfig
    role_templates: dict[str, str] = Field(default_factory=dict)
    transitions: dict[str, list[str]] = Field(default_factory=dict)
    prohibitions: list[Any] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    figures: FiguresConfig = Field(default_factory=FiguresConfig)
    statistics: StatisticsConfig = Field(default_factory=StatisticsConfig)
    review_paper: ReviewPaperConfig | None = None
    flag_default_modes: dict[str, str] = Field(default_factory=dict)

    notes: str = ""  # markdown body

    @classmethod
    def from_file(cls, path: Path) -> "Voice":
        """Parse a .voice.md file: YAML frontmatter + markdown body."""
        import yaml

        text = path.read_text(encoding="utf-8")
        frontmatter, body = _split_frontmatter(text)
        if frontmatter is None:
            raise ValueError(
                f"{path}: no YAML frontmatter (expected '---' delimiters)."
            )

        data = yaml.safe_load(frontmatter) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: frontmatter must be a YAML mapping.")

        # Voice inheritance: if `extends:` is present, load parent and deep-merge.
        parent_name = data.pop("extends", None)
        if parent_name:
            parent_path = path.parent / f"{parent_name}.voice.md"
            if not parent_path.exists():
                raise ValueError(
                    f"{path}: parent voice '{parent_name}' not found at {parent_path}."
                )
            parent = cls.from_file(parent_path)
            parent_dict = parent.model_dump()
            parent_dict.pop("notes", None)
            data = deep_merge(parent_dict, data)

        data["notes"] = body.strip()
        return cls.model_validate(data)

    def validate_self(self) -> list[str]:
        """Return a list of validation issues (empty if valid)."""
        issues: list[str] = []
        required_roles = {
            "setup", "evidence", "mechanism", "limit",
            "complication", "counterargument", "synthesis", "conclusion",
        }
        missing_roles = required_roles - set(self.role_templates.keys())
        if missing_roles:
            issues.append(
                f"role_templates missing required roles: {sorted(missing_roles)}"
            )
        required_transitions = {
            "supports", "contradicts", "qualifies",
            "extends", "depends_on", "is_counterexample_to",
        }
        missing_transitions = required_transitions - set(self.transitions.keys())
        if missing_transitions:
            issues.append(
                f"transitions missing required types: {sorted(missing_transitions)}"
            )
        for i, p in enumerate(self.prohibitions):
            if isinstance(p, str):
                continue
            if isinstance(p, dict) and any(k in p for k in ("word", "phrase", "pattern")):
                continue
            issues.append(
                f"prohibitions[{i}] must be a string or have word/phrase/pattern: {p!r}"
            )
        return issues


# ─── helpers ─────────────────────────────────────────

_FRONTMATTER_RE = __import__("re").compile(
    r"^---\s*\n(.*?)\n---\s*\n?(.*)$", __import__("re").DOTALL
)


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    return match.group(1), match.group(2)


def deep_merge(parent: dict, child: dict) -> dict:
    """Deep-merge `child` onto `parent` for voice inheritance.

    - Scalars: child wins.
    - Dicts: merged recursively.
    - Lists: child replaces by default; `<key>+:` appends to the parent's list.
    """
    result: dict = {k: v for k, v in parent.items()}
    for key, value in child.items():
        if key.endswith("+"):
            base_key = key[:-1]
            existing = result.get(base_key, [])
            if not isinstance(existing, list):
                existing = []
            result[base_key] = existing + list(value or [])
            continue
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
