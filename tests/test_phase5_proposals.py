"""Phase 5 — rewrite quality + structured review proposals.

Covers:
- ``classify_mechanism_support`` distinguishes source-supported,
  author-specified, unknown, and none.
- The chunked renderer's system prompt includes the mechanism-discipline
  rules and the per-claim ``mechanism_support`` attribute.
- Redraft-mode preludes prepend a mode-specific instruction to the prompt.
- ``verify_claims_preserved`` flags rewrites that drop required claims.
- The supervisor review pipeline emits ``ReviewProposal`` items keyed
  to claim/source/relationship IDs and the cockpit-queue surfaces them.
- The ``accept-proposal`` / ``reject-proposal`` cockpit actions persist
  decisions and hide decided proposals from the active queue.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from lattice.graph.models import (
    AuthorGraph, BindingStrength, Citation, Claim, ClaimRoleInCluster,
    ClaimType, Cluster, ClusterRole, Confidence, Evidence,
    MechanismSupport, ProposalKind, ProposalStatus, ProseState,
    RedraftMode, Relationship, RelationshipStrength, RelationshipType,
    Section, SectionRole, Source, SourceMetadata, SourceType,
)
from lattice.graph.store import GraphStore
from lattice.renderer.chunked_renderer import (
    _build_chunked_system_prompt,
    classify_mechanism_support,
    verify_claims_preserved,
)
from lattice.review.review import (
    ClusterRevision, _attach_source_ids, _classify_proposal_kind,
    _derive_proposal,
)
from lattice.voice.parser import Voice
from lattice.web.app import create_app


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── mechanism support classification ────────────────


def test_classify_mechanism_support_no_mechanism_returns_none() -> None:
    claim = Claim(
        claim_id="cl.x", statement="A claim.",
        type=ClaimType.empirical, confidence=Confidence.high,
        created_by="t", created_at=_now(), modified_at=_now(),
    )
    assert classify_mechanism_support(claim) == MechanismSupport.none


def test_classify_mechanism_support_strong_evidence_is_source_supported() -> None:
    claim = Claim(
        claim_id="cl.x", statement="A claim.",
        mechanism="A causes B because C.",
        type=ClaimType.empirical, confidence=Confidence.high,
        evidence=[Evidence(
            source="src.1", passage="p.1",
            binding_strength=BindingStrength.strong,
        )],
        created_by="t", created_at=_now(), modified_at=_now(),
    )
    assert classify_mechanism_support(claim) == MechanismSupport.source_supported


def test_classify_mechanism_support_author_origin_without_evidence_is_author_specified() -> None:
    claim = Claim(
        claim_id="cl.x", statement="A claim.",
        mechanism="My analytical move.",
        type=ClaimType.user_synthesis, confidence=Confidence.medium,
        author_origin=True,
        created_by="t", created_at=_now(), modified_at=_now(),
    )
    assert classify_mechanism_support(claim) == MechanismSupport.author_specified


def test_classify_mechanism_support_no_backing_is_unknown() -> None:
    claim = Claim(
        claim_id="cl.x", statement="A claim.",
        mechanism="A speculative chain.",
        type=ClaimType.empirical, confidence=Confidence.medium,
        evidence=[Evidence(
            source="src.1", passage="p.1",
            binding_strength=BindingStrength.weak,
        )],
        created_by="t", created_at=_now(), modified_at=_now(),
    )
    assert classify_mechanism_support(claim) == MechanismSupport.unknown


# ─── prompt content ──────────────────────────────────


def _voice() -> Voice:
    voice_path = (
        Path(__file__).parent.parent
        / "examples" / "voices" / "academic.voice.md"
    )
    return Voice.from_file(voice_path)


def test_chunked_system_prompt_includes_mechanism_discipline() -> None:
    prompt = _build_chunked_system_prompt(_voice())
    assert "MECHANISM DISCIPLINE" in prompt
    assert "source_supported" in prompt
    assert "author_specified" in prompt
    assert "UNRENDERABLE_MECHANISM" in prompt


def test_chunked_system_prompt_compression_mode_prepends_prelude() -> None:
    prompt = _build_chunked_system_prompt(_voice(), RedraftMode.compression)
    assert "REDRAFT MODE: compression" in prompt
    # And the mechanism rules are still present.
    assert "MECHANISM DISCIPLINE" in prompt


def test_chunked_system_prompt_no_mode_has_no_prelude() -> None:
    prompt = _build_chunked_system_prompt(_voice(), None)
    assert "REDRAFT MODE" not in prompt


# ─── claim preservation ──────────────────────────────


def _claim(cid: str, statement: str) -> Claim:
    return Claim(
        claim_id=cid, statement=statement,
        type=ClaimType.empirical, confidence=Confidence.high,
        created_by="t", created_at=_now(), modified_at=_now(),
    )


def _cluster(cid: str, claim_ids: list[str]) -> Cluster:
    return Cluster(
        cluster_id=cid, section_id="s.x", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(claim_id=c, role_in_cluster=ClusterRole.evidence)
            for c in claim_ids
        ],
        prose_state=ProseState.generated,
    )


def test_verify_claims_preserved_returns_empty_when_all_present() -> None:
    claims = {
        "cl.1": _claim("cl.1", "Energy efficiency gains in datacentres."),
        "cl.2": _claim("cl.2", "Cooling power dominates marginal costs."),
    }
    cluster = _cluster("c.1", ["cl.1", "cl.2"])
    prose = (
        "Energy efficiency gains in datacentres have stalled. "
        "Cooling power dominates the marginal costs of operation."
    )
    assert verify_claims_preserved(cluster, prose, claims) == []


def test_verify_claims_preserved_flags_dropped_claim() -> None:
    claims = {
        "cl.1": _claim("cl.1", "Energy efficiency gains in datacentres stalled."),
        "cl.2": _claim(
            "cl.2", "Cooling power dominates marginal costs in inference workloads.",
        ),
    }
    cluster = _cluster("c.1", ["cl.1", "cl.2"])
    # Only cl.1 survives.
    prose = "Energy efficiency gains in datacentres have stalled."
    dropped = verify_claims_preserved(cluster, prose, claims)
    assert dropped == ["cl.2"]


# ─── review proposal derivation ──────────────────────


def test_classify_proposal_kind_picks_mechanism_for_causal_comments() -> None:
    assert _classify_proposal_kind(
        "the mechanism here needs to be spelled out"
    ) == ProposalKind.mechanism
    assert _classify_proposal_kind(
        "tighten this sentence — it meanders"
    ) == ProposalKind.clarity
    assert _classify_proposal_kind(
        "engage the source rather than name-dropping it"
    ) == ProposalKind.citation
    assert _classify_proposal_kind("something else") == ProposalKind.other


def test_derive_proposal_carries_cluster_claims_and_relationship_ids() -> None:
    revision = ClusterRevision(
        cluster_id="c.1", section_id="s.x", section_title="X",
        original_prose="orig", revised_prose="revised",
        comment="tighten this opening", severity="suggestion",
    )
    cluster = _cluster("c.1", ["cl.1", "cl.2"])
    # Synthesise a relationship_context entry the cluster carries on
    # disk in real pipelines.
    rel_ctx = MagicMock()
    rel_ctx.rel_id = "r.1"
    cluster.relationship_context = [rel_ctx]
    proposal = _derive_proposal(revision, cluster)
    assert proposal.cluster_id == "c.1"
    assert proposal.section_id == "s.x"
    assert proposal.affects_claim_ids == ["cl.1", "cl.2"]
    assert proposal.affects_relationship_ids == ["r.1"]
    assert proposal.kind == ProposalKind.clarity
    assert proposal.status == ProposalStatus.pending
    assert proposal.before_text == "orig"
    assert proposal.after_text == "revised"
    # Stable IDs: same inputs → same proposal_id.
    assert _derive_proposal(revision, cluster).proposal_id == proposal.proposal_id


def test_attach_source_ids_pulls_from_claim_evidence() -> None:
    now = _now()
    graph = AuthorGraph(
        project_name="t", sections=[], relationships=[],
        claims=[
            Claim(
                claim_id="cl.1", statement="x",
                type=ClaimType.empirical, confidence=Confidence.high,
                created_by="t", created_at=now, modified_at=now,
                evidence=[
                    Evidence(source="src.A", passage="p.1",
                             binding_strength=BindingStrength.strong),
                    Evidence(source="src.B", passage="p.2",
                             binding_strength=BindingStrength.weak),
                ],
            ),
            Claim(
                claim_id="cl.2", statement="y",
                type=ClaimType.empirical, confidence=Confidence.medium,
                created_by="t", created_at=now, modified_at=now,
                evidence=[Evidence(
                    source="src.B", passage="p.3",
                    binding_strength=BindingStrength.weak,
                )],
            ),
        ],
        created_at=now, modified_at=now,
    )
    revision = ClusterRevision(
        cluster_id="c.1", section_id="s.x", section_title="X",
        original_prose="o", revised_prose="r", comment="x",
        severity="suggestion",
    )
    cluster = _cluster("c.1", ["cl.1", "cl.2"])
    p = _derive_proposal(revision, cluster)
    _attach_source_ids([p], graph)
    # Order preserves first-seen, no duplicates.
    assert p.affects_source_ids == ["src.A", "src.B"]


# ─── cockpit endpoint surfaces proposals + decisions ─


def _seed_project_with_review(tmp_path: Path) -> Path:
    project = tmp_path / "demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / "config.yml").write_text("autocorrect: safe\n", encoding="utf-8")
    voices_dir = project / "voices"
    voices_dir.mkdir(exist_ok=True)
    voice_src = (
        Path(__file__).parent.parent / "examples"
        / "voices" / "academic.voice.md"
    )
    (voices_dir / "academic.voice.md").write_text(
        voice_src.read_text(encoding="utf-8"), encoding="utf-8")

    store = GraphStore.load(project)
    now = _now()
    section = Section(
        section_id="s.x", title="X", position=1,
        role=SectionRole.argumentative, claim_ids=["cl.x.1"],
    )
    claim = Claim(
        claim_id="cl.x.1", statement="A claim.",
        type=ClaimType.empirical, confidence=Confidence.high,
        section_id="s.x", created_by="t", created_at=now, modified_at=now,
        evidence=[Evidence(
            source="src.smith", passage="p.1",
            binding_strength=BindingStrength.strong,
        )],
    )
    store.save_graph(AuthorGraph(
        project_name="demo", sections=[section], claims=[claim],
        relationships=[], created_at=now, modified_at=now,
    ))
    cluster = Cluster(
        cluster_id="c.x.1", section_id="s.x", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[ClaimRoleInCluster(
            claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence,
        )],
        prose_state=ProseState.generated,
    )
    store.save_cluster(cluster)

    review_path = project / "outputs" / "review.academic.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({
        "project_name": "demo", "voice_name": "academic",
        "generated_at": "2026-05-05T00:00:00Z", "mode": "thorough",
        "overall_critique": "needs work",
        "section_critiques": [],
        "cluster_revisions": [],
        "proposals": [{
            "proposal_id": "prop.test_one",
            "kind": "clarity", "cluster_id": "c.x.1",
            "section_id": "s.x",
            "affects_claim_ids": ["cl.x.1"],
            "affects_source_ids": ["src.smith"],
            "affects_relationship_ids": [],
            "before_text": "old prose",
            "after_text": "new prose",
            "comment": "tighten this opening",
            "severity": "suggestion",
            "status": "pending",
        }],
    }), encoding="utf-8")
    return project


def test_cockpit_queue_surfaces_review_proposals_with_affects(
    tmp_path: Path,
) -> None:
    _seed_project_with_review(tmp_path)
    client = TestClient(create_app(projects_root=tmp_path))
    resp = client.get("/api/projects/demo/cockpit-queue")
    assert resp.status_code == 200
    data = resp.json()
    review_items = [it for it in data["items"] if it["kind"] == "review_proposal"]
    assert len(review_items) == 1
    r = review_items[0]
    assert r["target_claim_id"] == "cl.x.1"
    assert r["target_cluster_id"] == "c.x.1"
    assert r["affects_claim_ids"] == ["cl.x.1"]
    assert r["affects_source_ids"] == ["src.smith"]
    assert "accept-proposal" in r["actions"]
    assert "reject-proposal" in r["actions"]


def test_cockpit_action_accept_proposal_persists_decision(
    tmp_path: Path,
) -> None:
    project = _seed_project_with_review(tmp_path)
    client = TestClient(create_app(projects_root=tmp_path))
    resp = client.post(
        "/api/projects/demo/cockpit/actions/accept-proposal",
        json={
            "proposal_id": "prop.test_one",
            "cluster_id": "c.x.1",
            "voice": "academic",
            "rationale": "looks good",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "accepted"
    assert body["proposal_id"] == "prop.test_one"

    decisions_path = project / ".lattice" / "proposal_decisions.academic.json"
    assert decisions_path.exists()
    log = json.loads(decisions_path.read_text(encoding="utf-8"))
    assert log["voice"] == "academic"
    assert len(log["decisions"]) == 1
    assert log["decisions"][0]["decision"] == "accepted"


def test_cockpit_queue_hides_decided_proposals(tmp_path: Path) -> None:
    project = _seed_project_with_review(tmp_path)
    # Pre-write an accept decision.
    decisions = {
        "voice": "academic",
        "decisions": [{
            "proposal_id": "prop.test_one",
            "cluster_id": "c.x.1",
            "decision": "accepted",
            "decided_at": "2026-05-05T00:00:00Z",
            "decided_by": "user",
            "rationale": "",
        }],
    }
    (project / ".lattice").mkdir(parents=True, exist_ok=True)
    (project / ".lattice" / "proposal_decisions.academic.json").write_text(
        json.dumps(decisions), encoding="utf-8")

    client = TestClient(create_app(projects_root=tmp_path))
    resp = client.get("/api/projects/demo/cockpit-queue")
    assert resp.status_code == 200
    data = resp.json()
    review_items = [it for it in data["items"] if it["kind"] == "review_proposal"]
    assert review_items == []


def test_cockpit_action_reject_proposal_overrides_prior_accept(
    tmp_path: Path,
) -> None:
    """Accepting then rejecting the same proposal should leave a single
    'rejected' record — no stale 'accepted' history that confuses the
    queue filter."""
    project = _seed_project_with_review(tmp_path)
    client = TestClient(create_app(projects_root=tmp_path))
    payload = {
        "proposal_id": "prop.test_one",
        "cluster_id": "c.x.1",
        "voice": "academic",
    }
    client.post(
        "/api/projects/demo/cockpit/actions/accept-proposal", json=payload,
    )
    resp = client.post(
        "/api/projects/demo/cockpit/actions/reject-proposal",
        json={**payload, "rationale": "actually no"},
    )
    assert resp.status_code == 200
    log = json.loads(
        (project / ".lattice" / "proposal_decisions.academic.json")
        .read_text(encoding="utf-8")
    )
    assert len(log["decisions"]) == 1
    assert log["decisions"][0]["decision"] == "rejected"
    assert log["decisions"][0]["rationale"] == "actually no"


def test_cockpit_action_accept_without_proposal_id_400(tmp_path: Path) -> None:
    _seed_project_with_review(tmp_path)
    client = TestClient(create_app(projects_root=tmp_path))
    resp = client.post(
        "/api/projects/demo/cockpit/actions/accept-proposal", json={},
    )
    assert resp.status_code == 400
