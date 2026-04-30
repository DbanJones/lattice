"""Tests for the Differ."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from lattice.differ.diff import Differ
from lattice.graph.models import (
    AuthorGraph, BindingStrength, Citation, Claim, ClaimType, Confidence,
    Evidence, Relationship, RelationshipStrength, RelationshipType, Section,
    SectionRole, ShadowDiffType, Source, SourceMetadata, SourceType,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mk_claim(cid: str, statement: str, evidence=None, ctype=ClaimType.empirical) -> Claim:
    return Claim(
        claim_id=cid,
        statement=statement,
        type=ctype,
        confidence=Confidence.medium,
        evidence=evidence or [],
        created_by="test", created_at=_now(), modified_at=_now(),
    )


def _mk_graph(name: str, claims: list[Claim], rels: list[Relationship] | None = None) -> AuthorGraph:
    return AuthorGraph(
        project_name=name,
        sections=[Section(section_id="s.a", title="A", position=1, role=SectionRole.argumentative, claim_ids=[c.claim_id for c in claims])],
        claims=claims,
        relationships=rels or [],
        created_at=_now(), modified_at=_now(),
    )


def test_differ_flags_unsupported_author_claim(tmp_path: Path) -> None:
    author = _mk_graph("t", [
        _mk_claim("cl.1", "Koomey slowdown accelerated in the 2010s.",
                  evidence=[Evidence(source="koomey_2015", passage="", binding_strength=BindingStrength.weak)]),
    ])
    shadow = _mk_graph("shadow", [])
    diffs = Differ(tmp_path).diff(author, shadow)
    kinds = [d.type for d in diffs]
    assert ShadowDiffType.unsupported_author_claim in kinds


def test_differ_ignores_strongly_bound_author_claim(tmp_path: Path) -> None:
    author = _mk_graph("t", [
        _mk_claim("cl.1", "A well-supported claim.",
                  evidence=[Evidence(source="src_1", passage="p.1.1",
                                     binding_strength=BindingStrength.strong)]),
    ])
    shadow = _mk_graph("shadow", [])
    diffs = Differ(tmp_path).diff(author, shadow)
    assert not any(d.type == ShadowDiffType.unsupported_author_claim for d in diffs)


def test_differ_flags_contradicting_corpus_evidence(tmp_path: Path) -> None:
    author = _mk_graph("t", [
        _mk_claim("cl.1", "Koomey's Law doubling period lengthened in the 2010s.",
                  evidence=[Evidence(source="koomey_2015", passage="p.1.1",
                                     binding_strength=BindingStrength.strong)]),
    ])
    # Shadow contains a claim that contradicts the author's claim.
    shadow_claims = [
        _mk_claim("sc.1", "Accelerator-era architectures recovered the Koomey doubling trajectory."),
        _mk_claim("sc.2", "Koomey's Law doubling period lengthened in the 2010s."),
    ]
    shadow_rels = [
        Relationship(
            rel_id="r.shadow.1",
            type=RelationshipType.contradicts,
            **{"from": "sc.1", "to": "sc.2"},
            strength=RelationshipStrength.direct,
            note="",
            created_by="shadow", created_at=_now(),
        )
    ]
    shadow = _mk_graph("shadow", shadow_claims, shadow_rels)
    diffs = Differ(tmp_path).diff(author, shadow)
    assert any(d.type == ShadowDiffType.contradicting_corpus_evidence for d in diffs)


def test_differ_flags_corpus_suggested_claim(tmp_path: Path) -> None:
    author = _mk_graph("t", [_mk_claim("cl.1", "Author's only claim about widgets.")])
    shadow = _mk_graph("shadow", [
        _mk_claim("sc.1", "Author's only claim about widgets."),  # matches → not suggested
        _mk_claim("sc.2", "A totally different topic: data-centre liquid cooling at scale."),
    ])
    diffs = Differ(tmp_path).diff(author, shadow)
    suggested = [d for d in diffs if d.type == ShadowDiffType.corpus_suggested_claim]
    assert any("liquid cooling" in d.shadow_finding for d in suggested)


def test_differ_flags_untouched_source(tmp_path: Path) -> None:
    author = _mk_graph("t", [
        _mk_claim("cl.1", "Claim that cites one source.",
                  evidence=[Evidence(source="cited", passage="p.1.1",
                                     binding_strength=BindingStrength.strong)]),
    ])
    shadow = _mk_graph("shadow", [])
    sources = [
        Source(
            source_id=sid,
            type=SourceType.primary_paper,
            citation=Citation(authors=[], year=2020, title=sid),
            passages=[],
            metadata=SourceMetadata(
                date_added=_now(), file_path=f"refs/papers/{sid}.pdf", hash=f"sha256:{sid}"
            ),
        )
        for sid in ("cited", "untouched")
    ]
    diffs = Differ(tmp_path).diff(author, shadow, sources=sources)
    untouched = [d for d in diffs if d.type == ShadowDiffType.untouched_source]
    assert any("untouched" in d.shadow_finding for d in untouched)
    assert not any("cited" in d.shadow_finding and "untouched" not in d.shadow_finding for d in untouched)


def test_differ_writes_report(tmp_path: Path) -> None:
    author = _mk_graph("t", [_mk_claim("cl.1", "A claim.",
                                       evidence=[Evidence(source="x", passage="",
                                                          binding_strength=BindingStrength.weak)])])
    shadow = _mk_graph("shadow", [])
    differ = Differ(tmp_path)
    diffs = differ.diff(author, shadow)
    path = differ.write_report(diffs)
    assert path.exists()
    assert "Shadow report" in path.read_text(encoding="utf-8")
