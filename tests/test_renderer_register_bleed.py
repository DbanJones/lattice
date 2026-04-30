"""Acceptance tests for Fix 2: renderer must refuse rather than improvise."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.graph.models import (
    BindingStrength, Citation, Claim, ClaimRoleInCluster, ClaimType, Cluster,
    ClusterRole, Confidence, Evidence, Passage, PassageLocation, PassageType,
    ProseState, Section, SectionRole, Source, SourceMetadata, SourceType,
)
from lattice.graph.store import GraphStore
from lattice.renderer.cluster_renderer import (
    ClusterRenderer, Renderability, validate_response,
)
from lattice.utils.config import Config
from lattice.voice.parser import Voice


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _academic_voice() -> Voice:
    return Voice.from_file(
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.input_tokens = 500
        self.output_tokens = 250
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0
        self.model = "mock"
        self.stop_reason = "end_turn"


class _StubLLM:
    def __init__(self, response_text: str = "Default rendered prose.") -> None:
        self._text = response_text
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system, user, model=None, temperature=0.6, max_tokens=4096):
        self.calls.append((system, user))
        return _FakeResp(self._text)


def _mk_source(tmp_path: Path) -> Source:
    return Source(
        source_id="koomey_2015",
        type=SourceType.primary_paper,
        citation=Citation(authors=["Koomey"], year=2015, title="Energy"),
        passages=[
            Passage(
                id="p.1.1", text="Slowdown documented.",
                location=PassageLocation(page=1), type=PassageType.claim, char_count=20,
            )
        ],
        metadata=SourceMetadata(
            date_added=_now(), file_path="refs/papers/koomey_2015.pdf", hash="sha256:x"
        ),
    )


def _mk_claim(cid: str, *, bound: bool = True, user_synth: bool = False) -> Claim:
    evidence = []
    if bound and not user_synth:
        evidence = [
            Evidence(
                source="koomey_2015", passage="p.1.1",
                binding_strength=BindingStrength.strong,
            )
        ]
    return Claim(
        claim_id=cid,
        statement=f"Statement for {cid}.",
        type=ClaimType.user_synthesis if user_synth else ClaimType.empirical,
        confidence=Confidence.high,
        evidence=evidence,
        author_origin=user_synth,
        section_id="s.x",
        created_by="test",
        created_at=_now(), modified_at=_now(),
    )


def _build_env(tmp_path: Path, claims: list[Claim], cluster_id: str = "c.x.1") -> tuple[GraphStore, Config, Voice, Cluster]:
    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    store = GraphStore.load(tmp_path)
    voice = _academic_voice()
    config = Config.load(tmp_path)

    store.save_section(Section(
        section_id="s.x", title="Body", position=1,
        role=SectionRole.argumentative, claim_ids=[c.claim_id for c in claims],
    ))
    for c in claims:
        store.save_claim(c)
    store.save_source(_mk_source(tmp_path))

    cluster = Cluster(
        cluster_id=cluster_id, section_id="s.x", position=1,
        role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(claim_id=c.claim_id, role_in_cluster=ClusterRole.evidence)
            for c in claims
        ],
    )
    store.save_cluster(cluster)
    return store, config, voice, cluster


# ─── Renderability assessment ─────────────────────

@pytest.fixture
def cluster_with_no_bindings(tmp_path):
    claims = [_mk_claim(f"cl.x.{i}", bound=False) for i in range(1, 4)]
    return _build_env(tmp_path, claims)


@pytest.fixture
def cluster_with_partial_bindings(tmp_path):
    claims = [
        _mk_claim("cl.x.1", bound=True),
        _mk_claim("cl.x.2", bound=True),
        _mk_claim("cl.x.3", bound=False),
        _mk_claim("cl.x.4", bound=False),
    ]
    return _build_env(tmp_path, claims)


@pytest.fixture
def cluster_with_full_bindings(tmp_path):
    claims = [_mk_claim(f"cl.x.{i}", bound=True) for i in range(1, 5)]
    return _build_env(tmp_path, claims)


# ─── End-to-end render dispatch ───────────────────

async def test_unrenderable_cluster_skips_llm(cluster_with_no_bindings):
    store, config, voice, cluster = cluster_with_no_bindings
    llm = _StubLLM("This should NEVER be returned.")
    renderer = ClusterRenderer(config, store, llm, voice)

    result = await renderer.render_cluster(cluster.cluster_id)

    assert "CLUSTER_UNRENDERABLE" in result
    assert len(llm.calls) == 0  # LLM never invoked
    refreshed = store.get_cluster(cluster.cluster_id)
    assert refreshed.prose_state == ProseState.failed


async def test_partial_cluster_renders_with_markers(cluster_with_partial_bindings):
    store, config, voice, cluster = cluster_with_partial_bindings
    response_text = (
        "Bound claims render as prose. "
        '{MISSING_CLAIM: cluster_id="c.x.1", claim_id="cl.x.3", description="needed"} '
        "More prose."
    )
    llm = _StubLLM(response_text)
    renderer = ClusterRenderer(config, store, llm, voice)

    result = await renderer.render_cluster(cluster.cluster_id)

    assert "MISSING_CLAIM" in result
    assert len(llm.calls) == 1
    refreshed = store.get_cluster(cluster.cluster_id)
    assert refreshed.prose_state == ProseState.needs_review


async def test_full_cluster_renders_normally(cluster_with_full_bindings):
    store, config, voice, cluster = cluster_with_full_bindings
    llm = _StubLLM("Clean academic prose with no markers and no register bleed.")
    renderer = ClusterRenderer(config, store, llm, voice)

    result = await renderer.render_cluster(cluster.cluster_id)

    assert "MISSING_CLAIM" not in result
    assert "CLUSTER_UNRENDERABLE" not in result
    refreshed = store.get_cluster(cluster.cluster_id)
    assert refreshed.prose_state == ProseState.generated


async def test_register_bleed_is_rejected(cluster_with_full_bindings):
    store, config, voice, cluster = cluster_with_full_bindings
    llm = _StubLLM("I need to clarify something before proceeding.")
    renderer = ClusterRenderer(config, store, llm, voice)

    result = await renderer.render_cluster(cluster.cluster_id)

    refreshed = store.get_cluster(cluster.cluster_id)
    assert refreshed.prose_state == ProseState.failed
    assert "CLUSTER_UNRENDERABLE" in result


async def test_user_synthesis_with_author_origin_counts_as_grounded(tmp_path):
    """user_synthesis claims with author_origin=True don't need source bindings."""
    claims = [_mk_claim(f"cl.x.{i}", user_synth=True) for i in range(1, 4)]
    store, config, voice, cluster = _build_env(tmp_path, claims)
    llm = _StubLLM("All claims are author-grounded; render normally.")
    renderer = ClusterRenderer(config, store, llm, voice)

    assessment = renderer.assess_cluster_renderability(cluster)
    assert assessment.state == Renderability.full

    result = await renderer.render_cluster(cluster.cluster_id)
    refreshed = store.get_cluster(cluster.cluster_id)
    assert refreshed.prose_state == ProseState.generated
    assert "CLUSTER_UNRENDERABLE" not in result


# ─── Validator pattern coverage ─────────────────

@pytest.mark.parametrize("forbidden", [
    "I need to clarify",
    "Could you clarify",
    "I cannot proceed",
    "Please let me know",
    "the constraint requires",
    "the prompt asks",
])
def test_validator_catches_each_pattern(forbidden):
    result = validate_response(f"Some prose. {forbidden}. More prose.")
    assert not result.is_valid, f"expected violations for {forbidden!r}, got none"
    # Check that some content word from the forbidden phrase shows up in
    # the violation message (the matched substring is tied to the pattern,
    # not necessarily the test's first word).
    forbidden_words = {w.lower() for w in forbidden.split() if len(w) > 2}
    violations_lower = " ".join(result.violations).lower()
    assert any(word in violations_lower for word in forbidden_words), (
        f"none of {forbidden_words} matched in violations: {result.violations}"
    )


def test_validator_allows_authorial_first_person():
    result = validate_response("I argue that efficiency is the limiting factor.")
    assert result.is_valid


def test_validator_allows_i_contend():
    result = validate_response("I contend that the existing forecasts are wrong.")
    assert result.is_valid


def test_validator_flags_user_addressing_question():
    result = validate_response(
        "This is a sentence. Could you clarify what you mean?"
    )
    assert not result.is_valid
    assert any("question" in v for v in result.violations)


def test_validator_allows_rhetorical_question_in_body():
    """Rhetorical questions from source content are an audit-time concern,
    not a render-time kill (otherwise faithful reproduction breaks)."""
    result = validate_response(
        "This is a sentence. Will hardware efficiency gains continue? "
        "Whether this trend holds determines the forecast range."
    )
    assert result.is_valid


def test_validator_allows_question_in_quoted_material():
    result = validate_response(
        'The author asks "what does this mean?" before answering.'
    )
    assert result.is_valid


def test_validator_rejects_empty_response():
    result = validate_response("")
    assert not result.is_valid
    assert "empty_response" in result.violations
