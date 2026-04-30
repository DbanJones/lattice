"""Tests for the mechanism-boilerplate audit check."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.auditor.boilerplate import MechanismBoilerplateCheck
from lattice.graph.models import (
    ClaimRoleInCluster,
    Cluster,
    ClusterRole,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stub_cluster() -> Cluster:
    return Cluster(
        cluster_id="c.x.1",
        section_id="s.x",
        position=1,
        role=ClusterRole.evidence,
        claim_sequence=[
            ClaimRoleInCluster(claim_id="cl.x.1", role_in_cluster=ClusterRole.evidence)
        ],
    )


def _check_factory(tmp_path: Path):
    """Build a check with stub voice/store/config — patterns don't depend on these."""
    from lattice.utils.config import Config
    from lattice.graph.store import GraphStore
    from lattice.voice.parser import Voice

    (tmp_path / "config.yml").write_text("", encoding="utf-8")
    config = Config.load(tmp_path)
    store = GraphStore.load(tmp_path)  # empty store — fine for these tests

    voice_path = (
        Path(__file__).parent.parent / "examples" / "voices" / "academic.voice.md"
    )
    voice = Voice.from_file(voice_path)
    return MechanismBoilerplateCheck(config, store, llm=None, voice=voice)


# ─── Pattern-by-pattern coverage ────────────────────


@pytest.mark.parametrize("prose,expected_rule_substring", [
    (
        "The mechanism operates through capital deployment signals.",
        "mechanism_operates_through",
    ),
    (
        "The mechanism is straightforward: extending depreciation reduces charges.",
        "mechanism_is_straightforward",
    ),
    (
        "Workload migration creates asymmetric outcomes across regions.",
        "creates_abstract_outcomes",
    ),
    (
        "Infrastructure flexibility creates asymmetric energy outcomes.",
        "abstract_quality_creates",
    ),
    (
        "Generational performance step-changes compress economic useful life "
        "below accounting assumptions.",
        "compresses_useful_life",
    ),
    (
        "Short-duration lease terms reveal confidence through optionality.",
        "signals_through_phrasing",
    ),
    (
        "The accounting embeds divergent futures into the same ledgers.",
        "embeds_divergent",
    ),
    (
        "Extending depreciation periods shifts recognition of capital consumption "
        "forward in time.",
        "shifts_recognition",
    ),
    (
        "Workload migration creates a measurement illusion.",
        "measurement_illusion",
    ),
    (
        "Supply-demand mismatch triggers collapse through overcapacity dynamics.",
        "abstract_dynamics",
    ),
    (
        "This decouples capital waste from proportional energy waste through "
        "utilisation adjustment.",
        "decoupling_through",
    ),
    (
        "Capital flexibility creates asymmetric outcomes across the buildout.",
        "abstract_quality_creates",
    ),
    # ─── Phase 2 patterns: abstract-noun-verb-abstract-noun ──
    (
        "Power density increases overwhelm efficiency improvements through a "
        "mechanism that reflects organisational structure.",
        "through_a_mechanism",
    ),
    (
        "Workload migration creates a measurement illusion across regions.",
        "creates_abstract_concept",
    ),
    (
        "Proof-of-work consensus creates deterministic energy-computation coupling.",
        "creates_abstract_concept",
    ),
    (
        "Idle power consumption decouples energy from utilisation through "
        "hardware design.",
        "decouples_from_through",
    ),
    (
        "Thermal management inefficiencies persist through institutional inertia.",
        "persists_through_institutional",
    ),
    (
        "Location decisions prioritise financial over thermodynamic optimisation through cost.",
        "prioritise_x_over_y_through",
    ),
    (
        "Short-duration lease terms reveal operator uncertainty about long-term value.",
        "reveals_abstract_belief",
    ),
    (
        "Off-grid diesel dependence inflates actual carbon intensity beyond grid factors.",
        "inflates_actual_beyond",
    ),
    (
        "Split incentives eliminate efficiency motivation through cost-benefit misalignment.",
        "eliminates_motivation_through",
    ),
    (
        "Diesel combustion contributes emissions in a way that national averages cannot capture.",
        "in_a_way_that_x_cannot",
    ),
])
async def test_each_boilerplate_pattern_fires(
    tmp_path: Path, prose: str, expected_rule_substring: str
) -> None:
    check = _check_factory(tmp_path)
    flags = await check.check_cluster(_stub_cluster(), prose)
    rules = [f.rule_id for f in flags]
    assert any(expected_rule_substring in r for r in rules), (
        f"expected a rule containing {expected_rule_substring!r} in {rules}"
    )


# ─── Negative cases ─────────────────────────────────


async def test_specific_mechanism_passes(tmp_path: Path) -> None:
    """Real mechanism prose — names the actors, the causal pathway, the
    specific consequence — should not fire."""
    check = _check_factory(tmp_path)
    prose = (
        "Dennard scaling held that as transistors shrank, their power "
        "density remained constant. When the relationship broke down "
        "around 2006, processors could no longer power all transistors "
        "simultaneously without exceeding thermal limits, forcing "
        "designers to leave fractions of the chip dark — the dark "
        "silicon phenomenon Esmaeilzadeh et al. (2011) measured at 21% "
        "transistor utilisation on an 8nm chip."
    )
    flags = await check.check_cluster(_stub_cluster(), prose)
    assert flags == [], f"expected no flags on specific mechanism prose; got {flags}"


async def test_empty_prose_no_flags(tmp_path: Path) -> None:
    check = _check_factory(tmp_path)
    flags = await check.check_cluster(_stub_cluster(), "")
    assert flags == []


# ─── Flag shape ─────────────────────────────────────


async def test_flag_carries_enclosing_sentence(tmp_path: Path) -> None:
    """The offending_text on a flag should be the enclosing sentence,
    not just the matched substring — so the reviewer reads the offence in
    context."""
    check = _check_factory(tmp_path)
    prose = (
        "The Hyperion campus is sized for 5 GW. The mechanism operates through "
        "off-balance-sheet financing. This obscures capex from forecasters."
    )
    flags = await check.check_cluster(_stub_cluster(), prose)
    assert len(flags) == 1
    # The enclosing sentence — not just "the mechanism operates through" —
    # should be in offending_text.
    assert "off-balance-sheet financing" in flags[0].offending_text


async def test_default_mode_is_rewrite(tmp_path: Path) -> None:
    """Boilerplate should be rewritten, not surgically edited."""
    check = _check_factory(tmp_path)
    prose = "The mechanism operates through capital signals."
    flags = await check.check_cluster(_stub_cluster(), prose)
    assert len(flags) == 1
    assert flags[0].default_mode.value == "rewrite"
