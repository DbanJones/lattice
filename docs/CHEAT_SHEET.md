# Cheat sheet

One page. Everything you need to use Lattice without re-reading the spec.

## The mental model

A paper is a **graph of claims** plus **a voice** plus **a corpus of references**. The tool:

1. Parses your structured outline into the graph.
2. Plans clusters (2–4 claims that render to one or two paragraphs).
3. Renders prose from the graph + voice + sources.
4. Audits the rendered prose for academic-writing problems.

The author owns the structure. Every change to the graph flows through an explicit accept/reject decision.

## Concepts

| Term | Meaning |
|---|---|
| **Claim** | One assertion. Atomic. Has a type, importance, evidence, scope. Every factual sentence in the rendered output traces to a claim. |
| **Section** | A heading in the outline (`# A.`, `## A.1`). Contains an ordered list of claims. Has a role (`introduction` / `argumentative` / `conclusion` / ...). |
| **Cluster** | The renderer's unit of work — 2–4 related claims that produce one or two paragraphs. Built by the assembler from claim relationships. |
| **Source** | An external reference (paper, dataset, prior writing). Has a citation, passages, metadata. Lives in `refs/`. |
| **Evidence** | A binding from a claim to a passage in a source. Has a binding strength (strong / weak / none / contradictory). |
| **Relationship** | A typed edge between claims (`supports`, `contradicts`, `qualifies`, `extends`, `depends_on`, `interpretive_pivot`, `is_evidence_for`). |
| **Voice** | A YAML config defining how prose should look (architecture template, register, citation style, prohibitions, ...). Swap voices to swap output style. |

## Tag vocabulary

Every claim bullet can carry tags in `[brackets]`. Order doesn't matter; whitespace tolerant.

### Claim type
```
[type: empirical]         # a fact about the world, source-grounded
[type: methodological]    # a statement about how something is measured
[type: normative]         # a value judgement
[type: definition]        # terminological scaffolding
[type: user_synthesis]    # author's own analytical move
```
Default if not tagged: `empirical` (or `user_synthesis` if the bullet is `MY VIEW:` / `COUNTER:`).

### Importance
```
[importance: 0.85]   # 0..1 — how heavy the claim is in the document's argument
```
Default 0.5. Drives word-budget allocation, skim-target placement, offcut decisions.

### Evidence state
```
[evidence_status: bound]         # bound to a specific passage
[evidence_status: source_hint]   # source identified but not bound
[evidence_status: unbound]       # explicit gap acknowledgement
[ref: smith_2020]                # citekey hint
[ref: smith_2020, lee_2019]      # multiple sources
```

### Relationships
```
[supports: cl.x]            # this claim supports another
[contradicts: cl.x]         # direct rebuttal
[qualifies: cl.x]           # adds boundary condition
[extends: cl.x]             # builds on
[depends_on: cl.x]          # only makes sense if x is true
[pivot: cl.x]               # interpretive pivot — diagnoses analytical error
[supports: thesis]          # alias for the cl.thesis claim
```

### Render hints
```
[role: setup]            # opener of a section
[role: evidence]         # main supporting move
[role: mechanism]        # explains the causal middle
[role: synthesis]        # closer of a section
[role: conclusion]       # closer of the whole document
[mechanism: A causes B because C]   # explicit mechanism text
[scope: condition]       # qualifying scope (when/where the claim holds)
[words: 800]             # word target for a section heading
[depth: deep]            # rendering depth for a section
[skip]                   # don't include this claim in the render
[arithmetic]             # preserve step-by-step working verbatim
[central_contribution]   # for figures — anchors section ordering
```

## Prefix conventions

```
- MY VIEW: The author's own analytical move.
  # implies [type: user_synthesis] AND [supports: thesis]

- COUNTER: A counter-argument worth engaging with.
  # implies [type: user_synthesis] AND [contradicts: thesis]
```

You can override either with explicit tags: `MY VIEW: ... [type: empirical]` is legal — the prefix sets the relationship, the tag overrides the type.

## Section headings

```
# A. Top-level section [role: argumentative] [depth: deep] [words: 1500]
## A.1 Subsection [role: evidence_synthesis]
### A.1.1 Sub-subsection
```

Roles: `introduction`, `argumentative`, `evidence_synthesis`, `methodological`, `counterargument`, `conclusion`, `appendix`, `references`.

The hash count must match the path depth — `## A.1` (depth 2, two path components) is correct; `## A.` (depth 2, one component) is rejected.

## Where things live

```
project/
├── structure/
│   └── outline.md              # YOUR EDIT POINT — the source of truth
├── refs/
│   ├── papers/*.pdf            # academic papers
│   ├── notes/*.md              # your own notes
│   └── prior_writing/*         # author's earlier work
├── voices/
│   └── academic.voice.md       # output style config
├── outputs/                    # what the tool produces (read-only-ish)
│   ├── paper.<voice>.md
│   ├── audit.md
│   └── ...
└── .lattice/                   # tool state (don't edit by hand)
    ├── author_graph.json
    ├── source_store.json
    ├── cluster_plan.json
    ├── audit_flags.json
    └── decisions/              # append-only logs
```

## The three discipline rules

1. **The author owns the structure.** Every change to the graph flows through an accept/reject. Snapshots (`outline.pre-X.md`) precede every automated edit.
2. **Every factual sentence traces to a claim.** The renderer refuses to invent. When grounding fails, it emits `{MISSING_CLAIM: ...}` rather than fabricating.
3. **Decisions are append-only.** What you accepted last week is still recorded. Re-running tools is idempotent against your prior choices.
