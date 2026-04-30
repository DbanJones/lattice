---
name: policy
extends: academic
description: Policy brief voice extending the academic base. Designed for documents read by ministers, civil servants, and senior corporate decision-makers who need findings, evidence, and recommendations rather than literature reviews. Built around the finding-evidence-implication-recommendation structure used by McKinsey, the World Bank, and Whitehall briefings.

# Most settings inherit from academic.voice.md.
# Only fields that differ are specified below.

# ───────────────────────────────────────────────────────
# 1. ARCHITECTURE
# ───────────────────────────────────────────────────────
architecture:
  template: policy_brief
    # Different template. Required structure:
    # 1. Bottom line up front (one paragraph)
    # 2. Findings (numbered, each one paragraph)
    # 3. Evidence (per finding)
    # 4. Implications for decision-makers
    # 5. Recommendations (numbered, actionable)
    # 6. Risks and caveats
    # 7. Annex (technical detail)

  hourglass_required: false
    # Policy briefs invert the academic shape: open narrow with the
    # finding, widen to the evidence, narrow again to the recommendation.

  killer_graph_first: true

  skim_targets_must_be_strongest:
    - bottom_line
    - findings_list
    - recommendations_list
    - executive_summary
    # Policy readers may read only these.

# ───────────────────────────────────────────────────────
# 2. CITATION
# ───────────────────────────────────────────────────────
citation:
  engagement_level: name_claim
    # Less rigorous than academic. Policy readers want the finding,
    # not a literature review.
  synthesis_threshold: 5
    # Higher. Policy briefs synthesise heavily.

# ───────────────────────────────────────────────────────
# 3. REGISTER
# ───────────────────────────────────────────────────────
register:
  formality: formal
  sentence_length: short
    # Shorter than academic. Policy is read at speed.
  sentence_length_target_distribution:
    short: 0.55
    medium: 0.35
    long: 0.10
  hedge_density: light
    # Decision-makers cannot act on hedged findings. Be specific about
    # confidence at the document level, then assert findings cleanly.
  lexicon: plain
    # Even lower than journalism. Treasury reads at GCSE level by design.
  first_person: forbidden
    # Policy briefs do not say "I argue".

# ───────────────────────────────────────────────────────
# 4. STANCE
# ───────────────────────────────────────────────────────
stance:
  default: instructive
  user_synthesis_stance: confident
    # Author's syntheses become "Finding 1", "Finding 2", asserted.
  unsupported_synthesis_treatment: omit
    # Findings without evidence base do not appear in policy briefs.
    # They might appear in the annex as "areas requiring further research".
  counterclaim_treatment: acknowledge
  uncertainty_display: foreground
    # Single dedicated section: "Risks and caveats". Then findings asserted.

# ───────────────────────────────────────────────────────
# 5. ATTRIBUTION
# ───────────────────────────────────────────────────────
attribution:
  style: footnote
    # Numbered footnotes, not inline citations.
  first_mention: short
    # No need for full first mention; footnotes carry detail.
  multiple_sources: group
  page_specificity: when_exact

# ───────────────────────────────────────────────────────
# 6. PARAGRAPH
# ───────────────────────────────────────────────────────
paragraph:
  shape: deductive
    # Topic sentence first, evidence below. The most rigorous version
    # of pyramid principle.
  length_sentences: [2, 5]
  length_words_max: 150
    # Tighter than academic.
  topic_sentence_required: true
  topic_sentence_position: first

# ───────────────────────────────────────────────────────
# 7. ROLE TEMPLATES (overrides where they differ)
# ───────────────────────────────────────────────────────
role_templates:
  setup: |
    State the problem in one or two sentences. No setup, no scene-setting.
    Policy readers want the issue immediately.

  evidence: |
    Lead with the finding. Footnote the source. Quantify if at all possible.
    Avoid quoting; paraphrase to the point.

  conclusion: |
    Either state the implication for decision-makers, or list a numbered
    recommendation. End on action, not on summary.

# ───────────────────────────────────────────────────────
# 8. PROHIBITIONS (additions to academic via +:)
# ───────────────────────────────────────────────────────
prohibitions+:
  # Words overused in policy writing
  - word: "framework"
    note: "Almost always replaceable with a more specific term."
  - word: "stakeholder"
    note: "Name them: ministers, regulators, operators, end-users."
  - word: "ecosystem"
    note: "Rarely accurate; usually means 'set of organisations'."
  - word: "leverage"
    word_class: verb
    replacement: "use"
  - phrase: "going forward"
    replacement_options: [from now, in future]
  - phrase: "on the table"
  - phrase: "low-hanging fruit"
  - phrase: "moving the needle"
  - phrase: "value-add"
    replacement: "added value"
  - phrase: "best practice"
    note: "Cite the specific practice; 'best' is unprovable."

# ───────────────────────────────────────────────────────
# 9. PREFERENCES (overrides)
# ───────────────────────────────────────────────────────
preferences:
  - active_voice
  - characters_in_subjects
  - actions_in_verbs
  - subject_verb_distance_under_8_words
    # Tighter than academic.
  - end_on_emphatic_information
  - quantify_magnitude_claims
  - british_english
  - oxford_comma_no
  - numeric_over_written

# ───────────────────────────────────────────────────────
# 10. FLAG DEFAULT MODES (additions for policy-specific concerns)
# ───────────────────────────────────────────────────────
flag_default_modes:
  policy_finding_unsupported: rewrite
    # Unsupported findings get removed, not edited.
  policy_recommendation_unactionable: suggest_changes
    # Recommendations that don't specify who, what, when get tightened.
  bottom_line_buried: rewrite
    # Findings should be in the first paragraph of each section.
---

# Policy voice notes

This voice extends academic with structural and register changes for
documents read by busy decision-makers. It is harder to write well
than either academic or journalistic because it must combine the rigour
of the former with the brevity of the latter.

## Inheritance behaviour

Policy inherits everything from academic.voice.md and overrides the fields
listed above. The +: suffix on prohibitions appends to academic's list
rather than replacing it.

To check what's effectively in force, run `lattice voices validate
voices/policy.voice.md` which will print the merged configuration.

## What this voice does differently

- Architecture template is policy_brief, not six_element_paper. The
  document opens with bottom line, then findings, then evidence, then
  recommendations.

- Citations are footnoted, not inline parenthetical. This keeps the prose
  scannable.

- Sentence length skews shorter (55 percent short, 35 percent medium,
  10 percent long).

- First person is forbidden entirely. No "I argue", no "we contend".
  Findings are asserted.

- Unsupported author syntheses are omitted, not flagged as opinion. Policy
  readers cannot act on opinion. Genuine opinion belongs in op-eds, which
  use the journalistic voice.

- Stacked banned phrases from policy writing: framework, stakeholder,
  ecosystem, leverage, going forward, low-hanging fruit, best practice.

## When to use

- Government briefings
- Board memos
- Investor decision documents
- Industry association policy submissions
- Regulator-facing analysis

## When not to use

- Journal articles (academic.voice.md)
- Op-eds advocating policy positions (journalistic.voice.md)
- Internal team strategy notes (no voice; bullet points work fine)
