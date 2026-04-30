# Voice File Format

A voice is a single markdown file with YAML frontmatter and free-text notes. Voices live in `voices/`. The frontmatter is the structured config the tool consumes; the markdown body is reference notes for the human author.

## File naming

`<name>.voice.md`. The name appears in `--voice <name>` CLI flag.

## Frontmatter sections

The YAML frontmatter has 13 top-level keys. Order in the file is recommended but not required. The parser validates each section.

### 1. Top-level metadata

```yaml
name: academic
description: Free text describing the voice and its intended use.
```

### 2. architecture (required)

Document-level structural template.

```yaml
architecture:
  template: six_element_paper
    # Options: six_element_paper, nature_compressed, review_paper,
    #         policy_brief, journalistic_feature, freeform
  hourglass_required: true        # bool
  killer_graph_first: true         # bool
  skim_targets_must_be_strongest:  # list
    - title
    - abstract
    - end_of_literature_review
    - end_of_conclusion
    - figure_captions
  signposting:
    section_open: motivation_and_structure   # or none
    section_close: resolution                 # or none
    paragraph_open: topic_first               # or freeform
    metadiscourse_density: minimal            # minimal | light | heavy
```

### 3. citation (required)

How sources participate in the argument.

```yaml
citation:
  engagement_level: name_claim_relevance
    # Options: name_only, name_claim, name_claim_relevance
  reporting_verbs:
    require_variety: true
    direct_evidence: [demonstrates, shows, establishes, measured]
    correlational: [indicates, suggests, found, observed, reported]
    theoretical: [implies, argues, contends, proposes]
    speculative: [may, might, could, appears to]
  synthesis_threshold: 3
  forbid_catalogue_pattern: true
  positioning_required_for: [thesis_claims, gap_statements, novel_methodology_claims]
  citation_purposes_allowed:
    - support_specific_claim
    - establish_specific_gap
    - credit_prior_contribution
```

### 4. register (required)

Prose texture.

```yaml
register:
  formality: formal              # formal | neutral | conversational | urgent
  sentence_length: varied        # short | medium | long | varied
  sentence_length_target_distribution:
    short: 0.30                  # under 12 words
    medium: 0.50                 # 12-25 words
    long: 0.20                   # 25+ words
  hedge_density: calibrated      # none | light | calibrated | heavy
  lexicon: discipline            # plain | discipline | elevated
  first_person: sparing          # forbidden | sparing | natural | primary
  contractions: forbidden        # forbidden | allowed
```

### 5. stance (required)

Claim positioning.

```yaml
stance:
  default: objective                  # objective | advocating | sceptical | instructive
  user_synthesis_stance: explicit_opinion  # cautious | confident | explicit_opinion | lede
  unsupported_synthesis_treatment: flag_as_opinion
    # Options: flag_as_opinion, lead_with, omit, prompt_author
  counterclaim_treatment: steelman    # dismiss | acknowledge | steelman
  uncertainty_display: explicit       # hide | mention | explicit | foreground
```

### 6. attribution (required)

Citation formatting only. Citation strategy is in the `citation` section above.

```yaml
attribution:
  style: harvard_inline       # harvard_inline | footnote | hyperlink | embedded | none
  first_mention: full         # full | short
  multiple_sources: synthesise # group | list | synthesise
  quote_threshold_words: 25
  page_specificity: when_exact # always | when_exact | never
```

### 7. paragraph (required)

Rhetorical flow.

```yaml
paragraph:
  shape: claim_evidence_implication
    # Options: claim_evidence_implication, narrative, question_led, deductive, inductive
  length_sentences: [4, 8]
  length_words_max: 250
  topic_sentence_required: true
  topic_sentence_position: first
  cohesion: old_to_new
  forbidden_paragraph_openers:
    - "Moreover,"
    - "Furthermore,"
    - "Additionally,"
    - "Similarly,"
    - "Likewise,"
    - "Equally,"
    - "Then,"
    - "Another"
    - "In addition"
  paragraph_open_varies: true
```

### 8. role_templates (required)

Per-role rendering instructions. These are passed to the renderer for each claim being rendered.

```yaml
role_templates:
  setup: |
    Establish context without overclaiming. Prefer declarative openings.
    Avoid rhetorical questions.
  evidence: |
    Lead with the source as a named subject when engagement_level is
    name_claim_relevance. Use a reporting verb matched to the claim's
    confidence. State the specific finding. Then link to the present
    argument.
  mechanism: |
    Use causal verbs: drives, produces, entails, constrains, governs.
  limit: |
    Frame as boundary condition, not as objection.
  complication: |
    Introduce as contrasting evidence, not full contradiction.
  counterargument: |
    Steelman before rebutting.
  synthesis: |
    Name what has been established. Do not introduce new claims.
  conclusion: |
    Restate the claim in stronger form. End on the emphatic information.
```

### 9. transitions (required)

Per-relationship-type connective phrases.

```yaml
transitions:
  supports: ["building on", "consistent with", "extending", "in line with"]
  contradicts: ["against this", "in contrast", "challenges", "cuts the other way"]
  qualifies: ["though", "subject to", "conditional on", "within the bounds of"]
  extends: ["developing", "pushing further", "beyond this"]
  depends_on: ["premised on", "resting on", "requires that"]
  is_counterexample_to: ["an exception arises in", "cuts against"]
```

### 10. prohibitions (required)

Hard rules. The auditor flags every violation.

```yaml
prohibitions:
  # Punctuation
  - em_dashes

  # Banned words
  - word: "issues"
    replacement_options: [problems, constraints, limitations, barriers]
  - word: "challenges"
    replacement_options: [problems, constraints, limitations]

  # Banned phrases (with context)
  - phrase: "in terms of"
    instruction: "Re-order the sentence."
  - phrase: "It is important to note that"
    context: paragraph_opening

  # Patterns (regex-detectable)
  - pattern: stacked_hedges
    description: "Three or more hedge words in a single clause"
  - pattern: expletive_construction_at_sentence_start
    description: "Sentences beginning 'There is', 'There are', 'It is'"
  - pattern: contraction
    examples: [don't, can't, it's, we've]
```

Three formats supported:

1. **String**: simple flag, looks for exact match
2. **Object with `word` or `phrase`**: structured, allows replacement options
3. **Object with `pattern`**: named pattern, implementation in `src/lattice/auditor/patterns.py`

### 11. preferences (required)

Soft guides. Rendered tries to honour them; auditor reports compliance rates but doesn't flag every failure.

```yaml
preferences:
  - active_voice
  - characters_in_subjects
  - actions_in_verbs
  - subject_verb_distance_under_10_words
  - end_on_emphatic_information
  - positive_form_over_negative
  - parallel_structure_in_lists
  - quantify_magnitude_claims
  - british_english
  - oxford_comma_no
  - numeric_over_written
  - parenthetical_over_dashes
  - acronym_define_at_first_use
  - tense_consistency_within_paragraph
```

### 12. figures (required)

```yaml
figures:
  caption_required: true
  caption_self_contained: true
  first_mention_interprets: true
  numbering: arabic            # arabic | roman | section_dot_number
  list_of_figures: true
  central_contribution_marker: true
```

### 13. statistics (required)

```yaml
statistics:
  no_arithmetic_for_reader: true
  reference_before_appearance: true
  named_entities_accurate: true
```

### 14. review_paper (optional)

Only required when `architecture.template = review_paper`.

```yaml
review_paper:
  multiple_cuts_required:
    - history
    - techniques
    - results
    - synthesis
    - gaps
  per_source_treatment:
    - what_authors_claim
    - what_evidence_provided
    - how_author_evaluates
  data_information_knowledge_hierarchy: enforced
  synthesis_density: high      # low | medium | high
  multi_source_per_paragraph_target: 2.5
```

### 15. flag_default_modes (required)

Per flag type, the default editing mode.

```yaml
flag_default_modes:
  architecture_missing_section: rewrite
  architecture_hourglass_break: rewrite
  citation_engagement_weak: suggest_changes
  citation_catalogue_pattern: suggest_changes
  claim_coverage_orphan_sentence: rewrite
  voice_prohibition_violation: suggest_changes
  voice_banned_word: suggest_changes
  sentence_subject_verb_distance: suggest_changes
  sentence_expletive_construction: suggest_changes
  quantification_unquantified_magnitude: suggest_changes
  paragraph_no_topic_sentence: suggest_changes
  paragraph_continuation_opener: suggest_changes
  paragraph_too_long: suggest_changes
  formality_contraction: suggest_changes
  formality_rhetorical_question: suggest_changes
  skim_target_weak: rewrite
  examiner_review_concern: author_choice
```

## Markdown body (after frontmatter)

The body is free-form notes. The parser extracts it as the voice's `notes` field but does not act on it. Use it for:

- Context: when this voice was created, why, by whom
- Examples: worked paragraphs in this voice, with annotation
- Failure modes: what this voice gets wrong by default
- Sources: the writing tradition or guidance the voice draws on
- Maintenance: changelog of voice modifications

## Validation rules

The parser validates:

1. All required sections present
2. `architecture.template` is a known value
3. `citation.engagement_level` is a known value
4. `prohibitions` items are valid (string OR have `word`/`phrase`/`pattern`)
5. `role_templates` covers at minimum: setup, evidence, mechanism, limit, complication, counterargument, synthesis, conclusion
6. `transitions` covers at minimum the relationship types used in the project's graph

Validation failures are surfaced via `lattice voices validate <file>` with a clear error message per failure.

## Inheritance

Voices may extend another voice with `extends:` at the top level:

```yaml
name: policy
extends: academic
description: Policy briefing voice extending the academic base.

# Override only what differs:
architecture:
  template: policy_brief

register:
  lexicon: plain
  hedge_density: light

prohibitions:
  # Inherited from academic, plus:
  - word: "framework"
    note: "Overused in policy writing."
```

The parser deep-merges, with the child's values taking precedence. Lists are replaced by default; use `+:` suffix to append:

```yaml
prohibitions+:
  - word: "stakeholder"
```

Inheritance is shallow by design. Don't chain more than one level.

## Examples

See `examples/voices/`:

- `academic.voice.md`: canonical academic voice for engineering papers
- `journalistic.voice.md`: feature journalism voice
- `policy.voice.md`: policy brief voice (extends academic)
- `template.voice.md`: empty template for authoring new voices
