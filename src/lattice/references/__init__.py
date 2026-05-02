"""Reference / citation management.

End-to-end pipeline for academic-paper citations:

1. ``scanner`` — detect the citation system in use; extract every
   inline citation, footnote, and bibliography entry from the document.
2. ``matcher`` — link inline / footnote citations to ``Source`` records;
   resolve ``Ibid.`` / ``op. cit.`` against the preceding distinct
   citation.
3. ``verifier`` — check parsed metadata against Crossref + OpenAlex;
   surface field-level discrepancies and propose canonical values.
4. ``filler`` — interactive walkthrough of accept-or-reject decisions
   for each disagreeing field.
5. ``rewriter`` — given a document + a target style, rewrite every
   inline citation and the bibliography in place. Deterministic, no
   LLM, instant style switching.

The single source of truth across the pipeline is
``DocumentCitations`` (in ``graph.models``), persisted to
``.lattice/document_citations.json``.
"""
