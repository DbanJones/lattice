---
name: academic
description: Engineering academic writing in the Cambridge tradition. Designed for journal articles, thesis chapters, and review papers. Built from Allwood's Cambridge Engineering writing class plus Williams, Sword, Schimel, Graff and Birkenstein, and Serrenho's supervisor feedback patterns.

# ───────────────────────────────────────────────────────
# 1. ARCHITECTURE: what shape the document takes
# ───────────────────────────────────────────────────────
architecture:
  template: six_element_paper
    # six_element_paper: Allwood's six moves (Context, Literature, Proposal,
    #   Test design, Results, Discussion). Hourglass shape required.
    # nature_compressed: first three folded into Introduction, methods at end
    # review_paper: catalogue + multiple-cuts + synthesis structure
    # freeform: no template, author owns structure entirely

  hourglass_required: true
    # When true, assembler checks that opening context width matches
    # closing discussion width. Wide-open intro + narrow conclusion
    # triggers an "overpromising" flag.

  killer_graph_first: true
    # Figure marked [central_contribution] anchors section ordering.
    # The section that introduces the central figure becomes the
    # narrative spine; other sections sequence to support it.

  skim_targets_must_be_strongest:
    - title
    - abstract
    - end_of_literature_review
    - end_of_conclusion
    - figure_captions
    # The auditor weights examiner review more heavily on these locations.

  signposting:
    section_open: "motivation_and_structure"
    section_close: "resolution"
    paragraph_open: "topic_first"
    metadiscourse_density: minimal
    # Headings do most signposting. Avoid "in this section we will".

# ───────────────────────────────────────────────────────
# 2. CITATION: how sources participate in the argument
# ───────────────────────────────────────────────────────
citation:
  engagement_level: name_claim_relevance
    # name_only: "(Smith 2022)"
    # name_claim: "Smith (2022) found X"
    # name_claim_relevance: "Smith (2022) found X. This matters here because Y"
    # The Graff & Birkenstein template. Required for academic voice.

  reporting_verbs:
    require_variety: true
    direct_evidence: [demonstrates, shows, establishes, measured]
    correlational: [indicates, suggests, found, observed, reported]
    theoretical: [implies, argues, contends, proposes]
    speculative: [may, might, could, appears to]
    # Renderer chooses verb based on claim confidence level.

  synthesis_threshold: 3
    # When 3+ sources cluster on a single claim or theme, the renderer
    # MUST produce a synthesis paragraph rather than sequential citations.
    # Pattern: "Three lines of evidence suggest X: A's measurements,
    # B's modelling, C's observations. They disagree on magnitude but
    # converge on direction."

  forbid_catalogue_pattern: true
    # Auditor flags any sequence of three sentences each citing one
    # different source without synthesis between them.

  positioning_required_for:
    - thesis_claims
    - gap_statements
    - novel_methodology_claims
    # These claims must use a They Say / I Say move:
    # "While X argues..., I contend..."
    # "Building on X's framework..."
    # "In contrast to X..."

  citation_purposes_allowed:
    - support_specific_claim
    - establish_specific_gap
    - credit_prior_contribution
    # Citations not serving one of these purposes are flagged as padding.

# ───────────────────────────────────────────────────────
# 3. REGISTER: prose texture
# ───────────────────────────────────────────────────────
register:
  formality: formal
  sentence_length: varied
    # Earn long sentences with short ones. Avoid 3+ consecutive long
    # sentences (Moran's rhythm rule).
  sentence_length_target_distribution:
    short: 0.30        # under 12 words
    medium: 0.50       # 12-25 words
    long: 0.20         # 25+ words
  hedge_density: calibrated
    # Match hedge to evidence type. See reporting_verbs above.
  lexicon: discipline
  first_person: sparing
  contractions: forbidden

# ───────────────────────────────────────────────────────
# 4. STANCE
# ───────────────────────────────────────────────────────
stance:
  default: objective
  user_synthesis_stance: explicit_opinion
    # Author claims rendered with "I argue", "I contend", "in my view"
  unsupported_synthesis_treatment: flag_as_opinion
    # No corpus support means explicit opinion framing.
  counterclaim_treatment: steelman
    # Strongest form of opposing argument before any rebuttal.
  uncertainty_display: explicit
    # Foreground what is not known.

# ───────────────────────────────────────────────────────
# 5. ATTRIBUTION (formatting only)
# ───────────────────────────────────────────────────────
attribution:
  style: harvard_inline
  first_mention: full
  multiple_sources: synthesise
    # When 2+ sources support same claim, prefer one synthesis sentence
    # over (Smith 2022; Jones 2023; Lee 2024) parenthetical pile-up.
  quote_threshold_words: 25
    # Quotations longer than this become block quotes or get paraphrased.
  page_specificity: when_exact
    # Page numbers required for direct quotations and specific data points.

# ───────────────────────────────────────────────────────
# 6. PARAGRAPH: rhetorical flow
# ───────────────────────────────────────────────────────
paragraph:
  shape: claim_evidence_implication
    # Schimel's micro-story at paragraph level: opening (what this is about),
    # action (what happens), resolution (what we take away).

  length_sentences: [4, 8]
  length_words_max: 250
    # Paragraphs over 250 words usually contain more than one idea.

  topic_sentence_required: true
  topic_sentence_position: first
    # Schimel: open paragraphs with their topic, not with continuation.

  cohesion: old_to_new
    # Williams's principle. Each sentence begins with old information
    # (topic position), ends with new (stress position). Next sentence
    # picks up the new and carries it forward.

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
    # Continuation connectives belong mid-paragraph, not at the start.
    # Allwood: "Another" or "In addition" both mean "I just thought of
    # something else".

  paragraph_open_varies: true
    # Forbid three paragraphs in a row opening with the same construction.

# ───────────────────────────────────────────────────────
# 7. ROLE TEMPLATES: how each claim role renders
# ───────────────────────────────────────────────────────
role_templates:
  setup: |
    Establish context without overclaiming. Prefer declarative openings.
    Avoid rhetorical questions. The setup names the problem; it does
    not yet position against the literature.

  evidence: |
    Lead with the source as a named subject when the engagement_level is
    name_claim_relevance. Use a reporting verb matched to the claim's
    confidence. State the specific finding, not a generic gesture at
    the topic. Then link the finding to the present argument with one
    sentence: "This matters here because..."

  mechanism: |
    Use causal verbs: drives, produces, entails, constrains, governs.
    Avoid "leads to" and "results in" where a stronger verb fits.
    Mechanism claims should name the entity and the action: "Dennard
    scaling produced predictable per-transistor power reductions" not
    "There was a reduction in per-transistor power because of Dennard
    scaling."

  limit: |
    Frame as boundary condition, not as objection. Use "subject to",
    "conditional on", "within the bounds of", "below the threshold
    where". "However" and "yet" are acceptable but not for every limit.

  complication: |
    Introduce as contrasting evidence, not as full contradiction.
    "Though", "while", "even as". Distinct from counterargument: a
    complication qualifies the claim; a counterargument opposes it.

  counterargument: |
    Steelman before rebutting. One sentence presenting the strongest
    form of the opposing view, then one to two sentences addressing it.
    Never strawman. The reader should be able to hold the opposing
    view in mind from your prose alone.

  synthesis: |
    Name what has been established. "On this reading", "taken together",
    "the evidence converges on". Synthesis sentences should not introduce
    new claims; they should consolidate ones already made.

  conclusion: |
    Restate the claim in stronger form than the setup. End on the
    emphatic information: the new contribution sits in the stress
    position. If a section follows, signal what it will do; if not,
    end on the claim itself.

# ───────────────────────────────────────────────────────
# 8. RELATIONSHIP TRANSITIONS
# ───────────────────────────────────────────────────────
transitions:
  supports:
    - "building on"
    - "consistent with"
    - "extending"
    - "in line with"
  contradicts:
    - "against this"
    - "in contrast"
    - "challenges"
    - "cuts the other way"
  qualifies:
    - "though"
    - "subject to"
    - "conditional on"
    - "within the bounds of"
  extends:
    - "developing"
    - "pushing further"
    - "beyond this"
  depends_on:
    - "premised on"
    - "resting on"
    - "requires that"
  is_counterexample_to:
    - "an exception arises in"
    - "cuts against"

# ───────────────────────────────────────────────────────
# 9. PROHIBITIONS: hard rules the auditor enforces
# ───────────────────────────────────────────────────────
prohibitions:

  # Punctuation
  - em_dashes
    # Author preference plus AI tell.

  # Banned words (Allwood's list)
  - word: "issues"
    replacement_options: [problems, constraints, limitations, barriers]
  - word: "challenges"
    replacement_options: [problems, constraints, limitations]
  - word: "perspective"
    replacement: "rewrite the sentence"
  - word: "successful"
    replacement_options: [effective, demonstrated, validated]
  - word: "seems"
    replacement_options: [indicates, suggests]
  - word: "highlight"
    word_class: verb
    replacement_options: [reveals, examines, emphasises]
  - word: "relatable"
    replacement_options: [accessible, clear, intuitive]
  - phrase: "in terms of"
    instruction: "Re-order the sentence; the phrase signals wrong word order."
  - phrase: "with respect to"
    instruction: "Re-order the sentence; the phrase signals wrong word order."
  - phrase: "making it"
    instruction: "Re-order the sentence; the phrase signals wrong word order."
  - phrase: "there is a need to"
    instruction: "Name who needs to act."

  # Weasel words for unwarranted certainty (Amazon's list)
  - word: "clearly"
  - word: "obviously"
  - phrase: "of course"
  - phrase: "needless to say"
  - phrase: "it should be noted that"
  - word: "interestingly"
  - word: "importantly"

  # Weak openers (Moran)
  - phrase: "It is important to note that"
  - phrase: "It is worth noting that"
  - phrase: "The purpose of this paper is to"
    context: paragraph_opening
  - phrase: "In this paper, we"
    context: paragraph_opening
  - phrase: "As mentioned earlier"

  # Inflated vocabulary (Zinsser)
  - word: "utilise"
    replacement: "use"
  - word: "facilitate"
    replacement_options: [help, ease]
  - word: "endeavour"
    replacement: "try"
  - word: "sufficient"
    replacement: "enough"
  - word: "numerous"
    replacement: "many"
  - word: "commence"
    replacement: "begin"
  - word: "terminate"
    replacement: "end"
  - word: "approximately"
    replacement: "about"
  - word: "methodology"
    replacement: "method"
    note: "methodology is the study of methods"
  - word: "functionality"
    replacement: "function"
  - word: "conceptualise"
    replacement: "conceive"
  - word: "operationalise"
    replacement: "apply"
  - word: "problematise"
    replacement: "question"

  # Cluttered phrases (Williams, Zinsser)
  - phrase: "due to the fact that"
    replacement: "because"
  - phrase: "in order to"
    replacement: "to"
  - phrase: "at this point in time"
    replacement_options: [now, currently]
  - phrase: "in the event that"
    replacement: "if"
  - phrase: "a large number of"
    replacement: "many"
  - phrase: "the vast majority of"
    replacement: "most"
  - phrase: "has the ability to"
    replacement: "can"
  - phrase: "in spite of the fact that"
    replacement_options: [although, despite]
  - phrase: "on the basis of"
    replacement: "based on"
  - phrase: "with regard to"
    replacement: "regarding"
  - phrase: "in the context of"
    replacement_options: [in, during]
  - phrase: "prior to"
    replacement: "before"
  - phrase: "subsequent to"
    replacement: "after"
  - phrase: "in close proximity to"
    replacement: "near"
  - phrase: "is indicative of"
    replacement_options: [indicates, suggests]
  - phrase: "take into consideration"
    replacement: "consider"
  - phrase: "in light of the fact that"
    replacement_options: [because, since]
  - phrase: "given the fact that"
    replacement: "since"

  # Empty verb plus nominalisation (Williams)
  - phrase: "made a decision"
    replacement: "decided"
  - phrase: "provided an explanation"
    replacement: "explained"
  - phrase: "reached a conclusion"
    replacement: "concluded"
  - phrase: "had an influence on"
    replacement: "influenced"
  - phrase: "took action"
    replacement: "acted"
  - phrase: "carried out an investigation"
    replacement: "investigated"

  # Redundancies (Strunk)
  - phrase: "completely eliminate"
    replacement: "eliminate"
  - phrase: "future plans"
    replacement: "plans"
  - phrase: "past history"
    replacement: "history"
  - phrase: "end result"
    replacement: "result"
  - phrase: "basic fundamentals"
    replacement: "fundamentals"
  - phrase: "exact same"
    replacement: "the same"
  - phrase: "new innovation"
    replacement: "innovation"
  - phrase: "still remains"
    replacement: "remains"

  # Empty intensifiers (Allwood, Serrenho)
  - word: "very"
    instruction: "Either quantify the claim or remove the word."
  - word: "really"
    instruction: "Either quantify the claim or remove the word."
  - word: "extremely"
    instruction: "Either quantify the claim or remove the word."
  - word: "quite"
  - word: "fairly"
  - word: "rather"
  - word: "somewhat"

  # Colloquialisms and phrasal verbs
  - phrase: "a lot of"
    replacement_options: [many, numerous]
  - phrase: "get rid of"
    replacement_options: [eliminate, remove]
  - phrase: "find out"
    replacement_options: [determine, ascertain]
  - phrase: "kind of"
  - phrase: "sort of"
  - phrase: "carry out"
    replacement_options: [conduct, perform]
  - phrase: "bring about"
    replacement: "cause"
  - phrase: "come up with"
    replacement_options: [propose, develop]
  - phrase: "look into"
    replacement: "investigate"
  - phrase: "set up"
    replacement: "establish"
  - phrase: "point out"
    replacement_options: [indicate, note]

  # Marketing language
  - word: "groundbreaking"
    replacement_options: [novel, significant]
  - word: "revolutionary"
    replacement: "transformative"
  - word: "game-changer"
    replacement: "transformative"
  - phrase: "cutting-edge"
    replacement: "state-of-the-art"

  # Stacked hedges
  - pattern: stacked_hedges
    description: "Three or more hedge words in a single clause"
    example_bad: "may possibly tend to perhaps"
    example_good: "may"

  # Expletive constructions
  - pattern: expletive_construction_at_sentence_start
    description: "Sentences beginning 'There is', 'There are', 'It is'"
    examples_bad:
      - "There are three factors that influence..."
      - "It is clear that the system failed."
    examples_good:
      - "Three factors influence..."
      - "The system failed."

  # Rhetorical questions
  - pattern: rhetorical_question
    description: "Questions in body text"
    instruction: "Restate as claim or proposition."

  # Contractions
  - pattern: contraction
    examples: [don't, can't, it's, we've, they're, won't, isn't]
    instruction: "Expand all contractions."

  # Split infinitives
  - pattern: split_infinitive
    instruction: "Move adverb out of the infinitive."

# ───────────────────────────────────────────────────────
# 10. PREFERENCES: soft guides
# ───────────────────────────────────────────────────────
preferences:

  - active_voice
    # Default. Passive only when:
    # - agent unknown or irrelevant
    # - agent less important than action
    # - topic continuity requires it
    # - methods section convention

  - characters_in_subjects
    # The subject of a sentence should name a character (researcher,
    # system, mechanism, dataset). Williams's principle.

  - actions_in_verbs
    # The verb should name the action, not "perform" or "conduct"
    # an abstract noun version of it.

  - subject_verb_distance_under_10_words
    # When subject and verb are separated by more than ~10 words,
    # readers lose the sentence framework.

  - end_on_emphatic_information
    # Stress position is the end of the sentence. Most important
    # new information goes there.

  - positive_form_over_negative
    # "different" not "not the same"; "forgot" not "did not remember".

  - parallel_structure_in_lists
    # Items in lists or comparisons share grammatical form.

  - quantify_magnitude_claims
    # "34 percent" not "significantly". Specific numbers replace
    # weasel words wherever possible.

  - british_english
  - oxford_comma_no
  - numeric_over_written
    # "1,500" not "fifteen hundred"
  - parenthetical_over_dashes

  - acronym_define_at_first_use
    # Spell out full term, abbreviation in parentheses, then use
    # abbreviation exclusively. Re-define in abstract.
    # Skip abbreviation if term used fewer than 3 times.

  - tense_consistency_within_paragraph
    # Past for completed actions. Present for established facts and
    # figure references. Present perfect for ongoing literature.
    # Future only for recommendations. Do not flip without reason.

# ───────────────────────────────────────────────────────
# 11. FIGURES
# ───────────────────────────────────────────────────────
figures:
  caption_required: true
  caption_self_contained: true
    # The caption must work as a standalone argument. A skim-reader
    # who reads only captions should grasp the contribution.

  first_mention_interprets: true
    # The prose introducing a figure must say what it shows, not
    # merely point to it. "Figure 3 plots the divergence" fails.
    # "Figure 3 shows that the highest-end and lowest-end forecasts
    # disagree by a factor of 24 across the 2024-2030 horizon" works.

  numbering: arabic
  list_of_figures: true

  central_contribution_marker: true
    # Figures marked [central_contribution] in the outline anchor the
    # killer-graph-first ordering at the assembler stage.

# ───────────────────────────────────────────────────────
# 12. STATISTICAL PRESENTATION
# ───────────────────────────────────────────────────────
statistics:
  no_arithmetic_for_reader: true
    # Pre-compute relevant ratios. Don't make the reader multiply
    # "15 percent of US hydro is in California" by "9 percent of
    # California is hydro". Calculate and state the result.

  reference_before_appearance: true
    # Every figure and table is referenced in the text before it
    # appears.

  named_entities_accurate: true
    # IEA is intergovernmental, not commercial. WHO is intergovernmental.
    # UN is intergovernmental. Mislabelling these triggers an audit flag.

# ───────────────────────────────────────────────────────
# 13. REVIEW PAPER MODE (extension of architecture)
# ───────────────────────────────────────────────────────
review_paper:
  # When architecture.template = review_paper, these additional
  # constraints apply.

  multiple_cuts_required:
    - history          # how knowledge built up and why
    - techniques       # what approaches have been used
    - results          # what insights emerged
    - synthesis        # where we are relative to where we want to be
    - gaps             # what we do not know

  per_source_treatment:
    - what_authors_claim
    - what_evidence_provided
    - how_author_evaluates
    # Schimel's three-part bullet for each reviewed source. Evaluation
    # is the point. Claim plus evidence plus evaluation, not just the
    # first two.

  data_information_knowledge_hierarchy: enforced
    # The review must move from data (numbers and results) to information
    # (heuristics and categories) to knowledge (what we take away).
    # Reviews that stop at information are catalogues, not knowledge.

---

# Academic voice notes

This voice is the working configuration for engineering academic writing
in the Cambridge tradition. It is the voice for thesis chapters, journal
submissions, and review papers. It is not for blog posts, op-eds, policy
briefings, or magazine features.

## What this voice is doing differently from a generic academic style

Three things distinguish this voice from a generic "academic writing"
template.

First, citations are arguments, not footnotes. Every citation must name
the author, state their specific claim, and link the claim to the present
argument. A sentence ending in (Smith 2022) without naming Smith is
flagged for rewrite. This is the rule the supervisor specifically called
out, and it is enforced at the renderer (citation strategy) and at the
auditor (citation engagement check).

Second, the architecture is the six-element paper. The assembler will
not produce a section ordering that lacks any of context, literature,
proposal, test design, results, discussion (in their compressed or full
forms). For review papers, the architecture switches to multiple-cuts
plus synthesis.

Third, magnitude claims must be quantified. "Significantly" without a
number triggers an audit flag. "Many" without a count triggers an audit
flag. The voice trusts the reader to handle numbers and distrusts
adjectives that pretend to be measurements.

## When this voice gets it wrong

Some failure modes worth watching for:

- Over-hedges high-confidence claims because the reporting-verb table
  defaults toward the cautious end. If a claim is marked confidence:high,
  the renderer should reach for "demonstrates" not "suggests".

- Tries to synthesise across only two sources when the synthesis
  threshold is set to three. Two-source synthesis can be valuable; the
  threshold is a default, not a ceiling.

- Renders user_synthesis claims with too many "I argue" markers in
  sequence. After the first explicit-opinion framing in a paragraph,
  subsequent sentences in that paragraph can drop the "I" without losing
  attribution.

- Sometimes leaves connective phrases ("furthermore", "moreover") at
  paragraph starts despite the prohibition, because the LLM falls into
  conventional academic patterns. The auditor catches these but they
  recur. Worth flagging in run reports.

## When not to use this voice

- Blog posts, op-eds, magazine features (use journalistic.voice.md)
- Policy briefings for non-academic audiences (use policy.voice.md)
- Conference abstracts (consider abstract.voice.md, sharper than this)
- Internal team notes, slide bullets, anything informal

## Sources this voice draws on

- Allwood, J. Cambridge Engineering academic writing class materials.
- Amazon. Six-pager writing discipline.
- Graff, G. & Birkenstein, C. (2010). They Say / I Say.
- Moran, J. (2018). First You Write a Sentence.
- Pinker, S. (2014). The Sense of Style.
- Schimel, J. (2012). Writing Science.
- Serrenho, A. Supervisor feedback patterns on engineering drafts.
- Strunk, W. Jr. & White, E.B. (2000). The Elements of Style.
- Sword, H. (2012). Stylish Academic Writing.
- Williams, J.M. & Bizup, J. (2016). Style: Lessons in Clarity and Grace.
- Zinsser, W. (2006). On Writing Well.
