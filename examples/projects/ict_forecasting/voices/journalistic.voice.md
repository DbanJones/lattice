---
name: journalistic
description: Feature journalism voice for long-form magazine pieces, op-eds, and substantive blog posts. Designed to render the same argument graph as a piece of writing that respects the reader's intelligence without demanding academic patience. Draws on Atlantic feature conventions, longform journalism craft, and the clarity tradition (Zinsser, Pinker) without the academic apparatus.

# ───────────────────────────────────────────────────────
# 1. ARCHITECTURE
# ───────────────────────────────────────────────────────
architecture:
  template: journalistic_feature
    # Hook (open with concrete scene or fact)
    # Nut graf (state the argument plainly)
    # Reporting (evidence delivered as story)
    # Close (return to the hook or land the implication)

  hourglass_required: false
    # Journalism is a different shape. Open wide, narrow to the spine,
    # widen again at the close, but width-matching is not enforced.

  killer_graph_first: true
    # If a chart anchors the piece, put it near the top.

  skim_targets_must_be_strongest:
    - headline
    - lede_paragraph
    - nut_graf
    - kicker
    # Different skim targets from academic. Captions matter less because
    # journalistic figures are often illustrative rather than evidentiary.

  signposting:
    section_open: scene_or_claim
    section_close: turn_or_pivot
    paragraph_open: varies
    metadiscourse_density: none
    # No "in this section we will". Subheads do all signposting.

# ───────────────────────────────────────────────────────
# 2. CITATION
# ───────────────────────────────────────────────────────
citation:
  engagement_level: name_claim
    # Name the source, state what they said. Relevance is implicit
    # in the journalism, not spelled out.

  reporting_verbs:
    require_variety: true
    direct_evidence: [showed, found, demonstrated, measured]
    correlational: [observed, reported, documented]
    theoretical: [argued, contended, posited]
    speculative: [suggested, raised the possibility, wondered whether]

  synthesis_threshold: 4
    # Higher than academic. Journalism tolerates more single-source
    # paragraphs because the source's voice is part of the evidence.

  forbid_catalogue_pattern: true
    # Still bad form. Journalism handles multiple sources by braiding
    # them through the narrative, not stacking them.

  positioning_required_for: [thesis_claims]
    # Less rigorous than academic. Only the central argument needs
    # explicit positioning.

  citation_purposes_allowed:
    - support_specific_claim
    - establish_context
    - provide_voice
    - credit_prior_work

# ───────────────────────────────────────────────────────
# 3. REGISTER
# ───────────────────────────────────────────────────────
register:
  formality: neutral
  sentence_length: varied
  sentence_length_target_distribution:
    short: 0.40
    medium: 0.45
    long: 0.15
    # More short sentences than academic. Punchier rhythm.
  hedge_density: light
    # Journalism is bolder. Hedge when warranted, not by default.
  lexicon: plain
    # Avoid discipline jargon. Define technical terms briefly when used.
  first_person: natural
    # The journalist may appear in the piece as a guide, observer,
    # or interlocutor. Not first person plural ("we") academic style.
  contractions: allowed
    # Don't, can't, it's are fine. Don't force formality.

# ───────────────────────────────────────────────────────
# 4. STANCE
# ───────────────────────────────────────────────────────
stance:
  default: instructive
    # The journalist is making an argument and wants the reader to
    # understand and care. Not pretending neutrality.
  user_synthesis_stance: lede
    # Author claims become the spine. They lead, they don't follow.
  unsupported_synthesis_treatment: lead_with
    # Author's claim is the hook. Evidence comes after.
  counterclaim_treatment: acknowledge
    # Steelmanning is academic discipline. Journalism acknowledges
    # opposing views without giving them equal weight.
  uncertainty_display: mention
    # Note uncertainty when it matters. Don't foreground it.

# ───────────────────────────────────────────────────────
# 5. ATTRIBUTION
# ───────────────────────────────────────────────────────
attribution:
  style: embedded
    # No parenthetical citations. Source is named in the prose:
    # "according to a 2024 study from MIT" rather than "(Smith 2024)".
  first_mention: full
    # Full name and affiliation on first mention.
  multiple_sources: synthesise
  quote_threshold_words: 40
    # Longer threshold than academic. Direct quotes are part of the craft.
  page_specificity: never
    # Page numbers belong in academic writing.

# ───────────────────────────────────────────────────────
# 6. PARAGRAPH
# ───────────────────────────────────────────────────────
paragraph:
  shape: narrative
    # Scene or specific claim, then development, then turn.
    # Journalism paragraphs are shorter and more varied in shape than
    # academic. Sometimes one sentence is a paragraph.
  length_sentences: [1, 6]
    # One-sentence paragraphs are allowed. Used for emphasis or pivot.
  length_words_max: 180
    # Shorter than academic. Tighter rhythm.
  topic_sentence_required: false
    # Journalism opens paragraphs with hooks, scenes, or specifics.
    # The topic is often delivered later in the paragraph.
  topic_sentence_position: any
  cohesion: any
    # Can be old-to-new or scene-to-claim or narrative beat.
  forbidden_paragraph_openers: []
    # No restrictions. Including starting with "And" or "But", which
    # is fine in journalism and forbidden in academic.
  paragraph_open_varies: true

# ───────────────────────────────────────────────────────
# 7. ROLE TEMPLATES
# ───────────────────────────────────────────────────────
role_templates:
  setup: |
    Open with a scene, a concrete fact, or a striking quotation. Avoid
    abstract opening. Make the reader want to know what comes next.

  evidence: |
    Deliver evidence as story when possible. Name the source by full name
    and brief affiliation. Quote sparingly but meaningfully. Show, don't
    just tell. Numbers are fine but should be made tangible: not "data
    centre energy use is 1 percent of global electricity" but "data
    centres now consume more electricity than Argentina".

  mechanism: |
    Explain the why. Use analogy when the technical detail would lose
    the reader. Avoid jargon; if a term is necessary, define it in one
    short clause.

  limit: |
    Acknowledge what isn't known. "But" and "yet" are fine. Don't dwell.

  complication: |
    Introduce as a turn in the story. The reader should feel the piece
    pivot.

  counterargument: |
    Acknowledge briefly. One paragraph at most. Then return to the spine.

  synthesis: |
    Land the implication. What does this mean for the reader?

  conclusion: |
    Return to the hook if possible. Or end on a vivid forward-looking
    image. Avoid summary; the reader has read the piece.

# ───────────────────────────────────────────────────────
# 8. TRANSITIONS
# ───────────────────────────────────────────────────────
transitions:
  supports: ["which is why", "and that means", "the same is true for"]
  contradicts: ["but", "and yet", "the catch is", "here's the problem"]
  qualifies: ["with the caveat that", "though it's worth noting", "subject to"]
  extends: ["going further", "and beyond that", "the bigger picture is"]
  depends_on: ["assuming that", "if", "where this rests on"]
  is_counterexample_to: ["except when", "consider"]

# ───────────────────────────────────────────────────────
# 9. PROHIBITIONS
# ───────────────────────────────────────────────────────
prohibitions:
  # Punctuation
  - em_dashes
    # Author preference applies across all voices.

  # Marketing language stays banned even in journalism
  - word: "groundbreaking"
  - word: "revolutionary"
  - word: "game-changer"
  - phrase: "cutting-edge"

  # Lazy intensifiers
  - word: "very"
    instruction: "Either quantify or remove."
  - word: "really"
    instruction: "Often deletable."

  # Cliches journalism falls into
  - phrase: "in today's world"
  - phrase: "now more than ever"
  - phrase: "at the end of the day"
  - phrase: "perfect storm"
  - phrase: "tip of the iceberg"
  - phrase: "Pandora's box"
  - word: "literally"
    context: as_intensifier
  - phrase: "begs the question"
    note: "Almost always misused; means raises the question."

  # AI tells
  - phrase: "It's worth noting that"
  - phrase: "It's important to remember that"
  - phrase: "delve into"
  - phrase: "navigate the complexities"

  # Academic phrases out of place in journalism
  - phrase: "this paper argues"
  - phrase: "in this article we"
  - word: "utilise"
    replacement: "use"
  - word: "facilitate"
    replacement: "help"

  # Stacked hedges still bad
  - pattern: stacked_hedges

# ───────────────────────────────────────────────────────
# 10. PREFERENCES
# ───────────────────────────────────────────────────────
preferences:
  - active_voice
  - characters_in_subjects
  - actions_in_verbs
  - end_on_emphatic_information
  - positive_form_over_negative
  - quantify_when_specific_else_make_tangible
    # Different from academic. Journalism prefers "more than Argentina"
    # to "1.2 percent" when the percentage doesn't land.
  - british_english
  - oxford_comma_no
  - vary_sentence_length_aggressively

# ───────────────────────────────────────────────────────
# 11. FIGURES
# ───────────────────────────────────────────────────────
figures:
  caption_required: true
  caption_self_contained: true
    # Even more important in journalism; readers skim.
  first_mention_interprets: true
  numbering: arabic
  list_of_figures: false
  central_contribution_marker: true

# ───────────────────────────────────────────────────────
# 12. STATISTICS
# ───────────────────────────────────────────────────────
statistics:
  no_arithmetic_for_reader: true
    # Even more strict. Pre-compute and make tangible.
  reference_before_appearance: true
  named_entities_accurate: true
  prefer_comparison_to_percentage: true
    # 'More than Argentina' beats '1.2 percent of global electricity'
    # for impact.

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
    # Less critical here than academic; journalism doesn't require it.
  paragraph_continuation_opener: suggest_changes
  paragraph_too_long: suggest_changes
  formality_contraction: suggest_changes
    # Still flagged but author often rejects in journalism.
  skim_target_weak: rewrite
    # Critical: weak lede or kicker is fatal in journalism.
  examiner_review_concern: author_choice
---

# Journalistic voice notes

This voice renders the same argument graph as a feature for a general
audience. It assumes a reader who is intelligent but busy, sceptical but
willing to be convinced, and who will close the tab if the opening
doesn't grab them.

## What this voice does differently from academic

Three significant differences.

First, the structure is hook plus nut graf plus reporting plus close,
not the six-element paper. The argument graph is the same; the
projection is different. A claim that lives in the literature review
section in the academic projection becomes part of the reporting in the
journalistic projection, often delivered through a quotation or a
specific instance.

Second, citations are embedded in the prose, not parenthetical. "According
to a 2024 MIT study, data centre electricity use grew 12 percent" is
preferred to "Data centre electricity use grew 12 percent (Smith 2024)".
The source is part of the sentence, not a footnote. First mentions get
full name and affiliation; subsequent mentions can be shortened.

Third, the prose is shorter and punchier. Sentence length distribution
target is 40 percent short, 45 percent medium, 15 percent long. Paragraphs
can be a single sentence when emphasis warrants. Topic sentences are
optional; opening with a scene or specific is often better.

## What this voice gets wrong by default

- Sometimes drops into AI-tell language ("delve into", "navigate the
  complexities"). The prohibitions catch most of these but iteration
  may surface more.

- Tends to over-quote on first attempts. The renderer has been known
  to use direct quotes where strong paraphrase would land better.
  Voice's quote_threshold_words is 40 specifically to push back on this.

- Falls into "in today's increasingly complex world" openings. Banned
  but recurs. Worth flagging in run reports.

## When to use this voice

- Magazine features
- Op-eds and substantive blog posts
- Substack-style longform
- Newspaper op-eds (consider opinion.voice.md for sharper opinion writing)
- Conference keynote scripts (consider speech.voice.md for spoken word)

## When not to use this voice

- Journal articles, thesis chapters, academic submissions (academic.voice.md)
- Policy briefings (policy.voice.md)
- Internal corporate writing (no voice, write it yourself)
- Twitter, social media (different beast entirely)

## Sources this voice draws on

- Atlantic feature conventions
- Zinsser, On Writing Well
- Pinker, The Sense of Style
- Longform journalism handbooks
- Ben Yagoda, How to Not Write Bad
