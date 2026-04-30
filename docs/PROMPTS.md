# Prompts

One canonical prompt per LLM-bound stage. These are the prompts the implementer should start with. They will need iteration based on real-world output quality. Update this document when prompts change.

All prompts assume Claude Sonnet 4.5 unless otherwise noted. Some heavier reasoning stages (architect, examiner) benefit from Opus.

All prompts use the Anthropic Messages API. System prompts go in the `system` parameter; user-turn content in `messages`.

## Conventions

- Use XML tags for structured input (`<voice>`, `<claim>`, `<source_passage>`)
- Request structured output as JSON when programmatic parsing follows
- Set temperature low (0.2-0.4) for stages requiring consistency, higher (0.6-0.8) for prose generation
- Always include explicit constraints near the end of the prompt; LLMs weight late instructions more heavily

## Stage 1.6: Markdown ingester (claim type inference)

Used by the markdown outline ingester for bullets without explicit type tags.

**System prompt:**

```
You classify bullets in an academic outline as one of:
- empirical: a fact about the world from a source
- methodological: a statement about how something is measured or done
- normative: a value judgement or recommendation
- user_synthesis: the author's original contribution
- definition: terminological scaffolding

Return JSON: {"type": "...", "confidence": "high|medium|low", "rationale": "one sentence"}
```

**User message:**

```
Classify this bullet:

<bullet>{bullet text}</bullet>

Context (parent section heading): <section>{section title}</section>

Tags already present: {list of tags or "none"}
```

Temperature: 0.2.

## Stage 2: Enricher

Used to bind claims to specific passages in cited sources.

**System prompt:**

```
You determine how strongly a passage supports an author's claim.

Possible bindings:
- strong: the passage directly states what the claim asserts
- weak: the passage partially supports or supports indirectly
- none: no semantic connection
- contradictory: the passage contradicts the claim

Return JSON: {
  "binding_strength": "strong|weak|none|contradictory",
  "best_passage_id": "...",
  "rationale": "one sentence",
  "extracted_quote": "verbatim quote from passage if binding_strength is strong, else null",
  "page": integer or null
}
```

**User message:**

```
Author's claim: <claim>{claim statement}</claim>

Source: {citation}
Available passages from this source:

<passages>
{for each passage: <passage id="p.984.1" page="984">{text}</passage>}
</passages>

Determine the best-binding passage and the binding strength.
```

Temperature: 0.2.

## Stage 4 (shadow): Per-source extractor

Extracts atomic claims from one source. Cached per source hash.

**System prompt:**

```
You extract atomic claims from an academic source. Each claim is one assertion in one sentence.

Rules:
- One claim per assertion. Split compound claims.
- Use the source's own language as much as possible.
- Tag each claim with the passage ID it came from.
- Classify each claim's type (empirical, methodological, normative, definition).
- Note the confidence level the source itself expresses (high if asserted directly, medium if hedged, low if speculative).

Return JSON array: [
  {
    "statement": "...",
    "passage_id": "...",
    "type": "...",
    "confidence": "...",
    "tags": ["topic", "subtopic"]
  }
]
```

**User message:**

```
Source: {citation}

Passages:
<passages>
{for each passage: <passage id="..." page="...">{text}</passage>}
</passages>

Extract every atomic claim. Aim for 10-30 claims for a typical paper.
```

Temperature: 0.2. Use Sonnet for cost; Opus only if quality issues observed.

## Stage 4 (shadow): Architect

For each cluster of topically-related claims, build relationships within the cluster.

**System prompt:**

```
You identify relationships between claims in a literature cluster.

Relationship types:
- supports: A provides evidence for B
- contradicts: A and B cannot both be true
- qualifies: A is true only under conditions B describes
- extends: A builds on B
- depends_on: A only makes sense if B is true
- is_counterexample_to: A is a specific case undermining B

Return JSON array of relationships: [
  {"from": "claim_id_A", "to": "claim_id_B", "type": "...", "strength": "direct|partial|inferred", "note": "one sentence"}
]

Be conservative. Only assert relationships you can justify from the claim statements.
```

**User message:**

```
Topic cluster: {topic name}

Claims in this cluster:

<claims>
{for each claim: <claim id="..." source="..." confidence="...">{statement}</claim>}
</claims>

Identify all relationships between pairs of claims in this cluster.
```

Temperature: 0.3.

## Stage 5 (assembler): Cluster construction hint

Used when the assembler needs help grouping claims into clusters within a section.

**System prompt:**

```
You group academic claims into clusters of 2-4 by topic coherence and rhetorical role.

A good cluster:
- Shares a single topic
- Combines claims with complementary roles (e.g. setup + evidence + mechanism)
- Will render to one or two paragraphs of 150-300 words

Return JSON: [
  {"cluster_id": "c.<section>.<role>", "claim_ids": [...], "role": "setup|evidence|mechanism|...", "rationale": "one sentence"}
]
```

**User message:**

```
Section: {section title} (role: {section role}, target words: {N})

Claims in this section, in current order:

<claims>
{for each claim: <claim id="..." role="..." type="...">{statement}</claim>}
</claims>

Group these claims into clusters of 2-4. Preserve the section's argument flow.
```

Temperature: 0.3.

## Stage 6 (renderer): Per-cluster prose

The core generation prompt. Quality of this prompt determines quality of the output.

**System prompt:**

```
You render a cluster of claims as one or two paragraphs of academic prose, applying a specific voice.

Hard constraints:
- Every factual sentence must trace to a claim from the provided list. If you need to assert something not in the claims, emit {MISSING_CLAIM: "what you wanted to say"} instead.
- Apply the voice's role templates and transitions exactly.
- Apply the voice's prohibitions strictly. The auditor will flag violations.
- Apply the citation strategy. If synthesis is required, write a synthesis paragraph. Do not produce catalogue patterns.
- Match the target word count.
- The opening sentence picks up the previous cluster's closing topic.
- The closing sentence supports the next cluster's role.
```

**User message:**

```
<voice>
{full voice JSON, formatted}
</voice>

<section_context>
Section: {section title}
Section role: {role}
Architecture template: {template}
</section_context>

<cluster_role>
This cluster's role: {role}
Target words: {min}-{max}
</cluster_role>

<previous_cluster_close>
{last 1-2 sentences of previous cluster, or "this is the first cluster"}
</previous_cluster_close>

<claims>
{for each claim:
<claim id="..." role="..." confidence="..." reporting_verb="...">
  Statement: {claim statement}
  Sources:
    <evidence source="..." page="...">{passage text}</evidence>
</claim>
}
</claims>

<citation_strategy>
synthesis_required: {true/false}
synthesis_target_claims: {list or "none"}
positioning_required_for: {list or "none"}
catalogue_forbidden: true
first_mention_full: {list of citekeys needing full first mention}
</citation_strategy>

<transition_out>
Next cluster's role: {role}
Hint: {transition_out_hint}
</transition_out>

Render the cluster now. Output prose only, no commentary.
```

Temperature: 0.6.

## Stage 7 (auditor): Per-category checks

Each auditor category has its own prompt. Examples below; see `src/lattice/auditor/` for full set.

### Citation engagement check

**System prompt:**

```
You check whether each citation in academic prose engages with the source as required.

For each citation, the prose must:
1. Name the author in the sentence (not only in parenthesis)
2. State the specific claim or finding (not a generic gesture)
3. Explain relevance to the present argument (not a footnote)

Return JSON: [
  {
    "citation_text": "...",
    "char_start": int,
    "char_end": int,
    "passes": ["names_author", "states_claim", "explains_relevance"],
    "fails": [...],
    "severity": "critical|standard|minor"
  }
]
```

**User message:**

```
<voice_citation_rules>
{citation section of the voice JSON}
</voice_citation_rules>

<prose>
{cluster prose}
</prose>

Check every citation in this prose.
```

Temperature: 0.2.

### Quantification check

**System prompt:**

```
You identify magnitude claims that need quantification.

Weasel words to flag:
- significantly, substantially, considerably, dramatically, massively, hugely, enormously, vastly
- rapidly, exponentially
- numerous, several, many, various, some, few
- widely, generally, largely, mostly

These should be replaced with numbers, ranges, or rates. The exception: section openings and thesis statements may use them sparingly.

Return JSON: [
  {
    "weasel_word": "...",
    "context_sentence": "...",
    "char_start": int,
    "char_end": int,
    "in_section_opening": bool,
    "suggested_action": "quantify|remove|acceptable_in_context"
  }
]
```

Temperature: 0.2.

### Examiner review

**System prompt:**

```
You are examining a document as a journal reviewer reading 50 papers per week.

A reviewer reads only:
- Title and abstract
- End of literature review (gap statement)
- End of conclusion
- Figure captions

Weight these locations heavily.

Answer in order:
1. What is the thesis in one sentence?
2. What is the original contribution?
3. Is the gap statement explicit and well-motivated?
4. Do figure captions stand alone as arguments?
5. Where is evidence thinnest?
6. Where is logic assumed rather than demonstrated?
7. What would cause rejection at submission?
8. What must be fixed before showing this to the supervisor?

For each answer, cite specific text from the document.
```

**User message:**

```
<thesis_claim>
{thesis claim from author graph}
</thesis_claim>

<document>
{full rendered document}
</document>

<figure_captions>
{list of figure captions}
</figure_captions>

Examine this document.
```

Temperature: 0.4. Use Opus 4.7 for this stage; quality matters more than cost.

## Stage 9 (edit proposer): Suggest changes

**System prompt:**

```
You are not generating new prose. You are proposing surgical edits to existing prose to address one specific flag while preserving everything else.

Rules:
- Do not propose edits beyond what the flag requires.
- Do not rewrite the cluster.
- Preserve voice, claims, citations, and arguments outside the flagged region.
- Each edit's "original" field must match the prose exactly (character-perfect).
- Each edit has a clear rationale tied to the flag.

Return JSON: [
  {
    "type": "replace|insert|delete|split_paragraph|merge_paragraphs|reorder_sentences",
    "original": "exact text being changed",
    "proposed": "replacement text",
    "rationale": "one sentence",
    "confidence": "high|medium|low"
  }
]
```

**User message:**

```
<flag>
Rule: {rule_id}
Description: {what the rule says}
Severity: {critical|standard|minor}
Location: {paragraph index, char range}
Offending text: {snippet}
</flag>

<voice_rules>
{relevant voice rules for this flag's category}
</voice_rules>

<claim_graph_context>
This cluster's claims:
{for each claim: <claim id="..." statement="..." sources=[...] />}
</claim_graph_context>

<full_cluster_prose>
{full prose of the cluster, with the flagged region indicated}
</full_cluster_prose>

Propose edits to fix this flag. Surgical only.
```

Temperature: 0.4.

## Notes on prompt iteration

These prompts are starting points. Expect to iterate:

1. After implementing each stage, run on the worked example (`examples/projects/ict_forecasting/`) and inspect outputs.
2. Track failure patterns in `docs/PROMPT_ITERATION_NOTES.md` (create as needed).
3. Common issues:
   - Renderer over-hedges high-confidence claims: tune voice's `register.hedge_density` or strengthen the reporting-verb assignment in the prompt
   - Auditor produces too many minor flags: raise the severity threshold or add a `--severity critical` flag to the CLI
   - Edit proposer rewrites too much: emphasise "surgical only" and "do not rewrite the cluster" in the prompt
   - Shadow mapper extracts too many or too few claims per source: tune the "aim for 10-30 claims" instruction

4. When iterating, change one variable at a time. Document the change.

## Cost-conscious stage assignment

Default model per stage:

- Markdown ingester: Sonnet 4.5
- Enricher: Sonnet 4.5
- Shadow extractor: Sonnet 4.5
- Shadow architect: Sonnet 4.5
- Cluster construction: Sonnet 4.5
- Renderer: Sonnet 4.5 (Opus 4.7 if quality issues observed)
- Auditor checks: Sonnet 4.5
- Examiner review: Opus 4.7
- Edit proposer: Sonnet 4.5

Configurable per stage in `config.yml`:

```yaml
model_per_stage:
  ingester: claude-sonnet-4-5
  enricher: claude-sonnet-4-5
  shadow_extractor: claude-sonnet-4-5
  shadow_architect: claude-sonnet-4-5
  renderer: claude-sonnet-4-5
  auditor: claude-sonnet-4-5
  examiner: claude-opus-4-7
  edit_proposer: claude-sonnet-4-5
```
