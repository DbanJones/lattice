---
name: REPLACE_WITH_VOICE_NAME
description: One sentence describing the voice and its intended use.

# extends: academic        # uncomment to inherit from another voice

# ───────────────────────────────────────────────────────
# 1. ARCHITECTURE
# ───────────────────────────────────────────────────────
architecture:
  template: freeform
    # Options: six_element_paper, nature_compressed, review_paper,
    #          policy_brief, journalistic_feature, freeform
  hourglass_required: false
  killer_graph_first: false
  skim_targets_must_be_strongest: []
  signposting:
    section_open: any
    section_close: any
    paragraph_open: any
    metadiscourse_density: light

# ───────────────────────────────────────────────────────
# 2. CITATION
# ───────────────────────────────────────────────────────
citation:
  engagement_level: name_claim
    # Options: name_only, name_claim, name_claim_relevance
  reporting_verbs:
    require_variety: false
    direct_evidence: [shows, demonstrates]
    correlational: [suggests, indicates]
    theoretical: [argues]
    speculative: [may, could]
  synthesis_threshold: 3
  forbid_catalogue_pattern: true
  positioning_required_for: []
  citation_purposes_allowed: [support_specific_claim]

# ───────────────────────────────────────────────────────
# 3. REGISTER
# ───────────────────────────────────────────────────────
register:
  formality: neutral
  sentence_length: varied
  sentence_length_target_distribution:
    short: 0.33
    medium: 0.34
    long: 0.33
  hedge_density: calibrated
  lexicon: discipline
  first_person: sparing
  contractions: forbidden

# ───────────────────────────────────────────────────────
# 4. STANCE
# ───────────────────────────────────────────────────────
stance:
  default: objective
  user_synthesis_stance: cautious
  unsupported_synthesis_treatment: flag_as_opinion
  counterclaim_treatment: acknowledge
  uncertainty_display: explicit

# ───────────────────────────────────────────────────────
# 5. ATTRIBUTION
# ───────────────────────────────────────────────────────
attribution:
  style: harvard_inline
  first_mention: full
  multiple_sources: synthesise
  quote_threshold_words: 25
  page_specificity: when_exact

# ───────────────────────────────────────────────────────
# 6. PARAGRAPH
# ───────────────────────────────────────────────────────
paragraph:
  shape: claim_evidence_implication
  length_sentences: [3, 7]
  length_words_max: 200
  topic_sentence_required: true
  topic_sentence_position: first
  cohesion: old_to_new
  forbidden_paragraph_openers: []
  paragraph_open_varies: true

# ───────────────────────────────────────────────────────
# 7. ROLE TEMPLATES
# ───────────────────────────────────────────────────────
role_templates:
  setup: "REPLACE: how to render setup claims"
  evidence: "REPLACE: how to render evidence claims"
  mechanism: "REPLACE: how to render mechanism claims"
  limit: "REPLACE: how to render limit claims"
  complication: "REPLACE: how to render complication claims"
  counterargument: "REPLACE: how to render counterargument claims"
  synthesis: "REPLACE: how to render synthesis claims"
  conclusion: "REPLACE: how to render conclusion claims"

# ───────────────────────────────────────────────────────
# 8. TRANSITIONS
# ───────────────────────────────────────────────────────
transitions:
  supports: ["building on"]
  contradicts: ["in contrast"]
  qualifies: ["though"]
  extends: ["further"]
  depends_on: ["assuming"]
  is_counterexample_to: ["except"]

# ───────────────────────────────────────────────────────
# 9. PROHIBITIONS
# ───────────────────────────────────────────────────────
prohibitions:
  - em_dashes
  # Add banned words and phrases here

# ───────────────────────────────────────────────────────
# 10. PREFERENCES
# ───────────────────────────────────────────────────────
preferences:
  - active_voice

# ───────────────────────────────────────────────────────
# 11. FIGURES
# ───────────────────────────────────────────────────────
figures:
  caption_required: true
  caption_self_contained: true
  first_mention_interprets: true
  numbering: arabic
  list_of_figures: false
  central_contribution_marker: true

# ───────────────────────────────────────────────────────
# 12. STATISTICS
# ───────────────────────────────────────────────────────
statistics:
  no_arithmetic_for_reader: true
  reference_before_appearance: true
  named_entities_accurate: true

# ───────────────────────────────────────────────────────
# 13. FLAG DEFAULT MODES
# ───────────────────────────────────────────────────────
flag_default_modes:
  architecture_missing_section: rewrite
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
---

# Voice name notes

Replace this section with notes describing what makes this voice
distinctive, common failure modes, and when to use or not use it.

## What this voice does differently

## What this voice gets wrong by default

## When to use

## When not to use

## Sources this voice draws on
