"""Tests for Config.load."""
from __future__ import annotations

from pathlib import Path

from lattice.utils.config import Config


def test_config_loads_from_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LATTICE_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("LATTICE_PARALLEL_RENDERS", raising=False)
    (tmp_path / "config.yml").write_text(
        """
default_voice: journalistic
default_model: claude-sonnet-4-5
parallel_renders: 4
model_per_stage:
  renderer: claude-sonnet-4-5
  examiner: claude-opus-4-7
cache_dir: .lattice/cache
output_dir: outputs
""",
        encoding="utf-8",
    )
    config = Config.load(tmp_path)
    assert config.default_voice == "journalistic"
    assert config.parallel_renders == 4
    assert config.model_for_stage("examiner") == "claude-opus-4-7"
    assert config.model_for_stage("unknown_stage") == config.default_model
    assert config.cache_dir == tmp_path / ".lattice/cache"
    assert config.output_dir == tmp_path / "outputs"


def test_config_env_overrides_yaml(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.yml").write_text(
        "default_model: claude-sonnet-4-5\nparallel_renders: 8\n", encoding="utf-8"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("LATTICE_DEFAULT_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("LATTICE_PARALLEL_RENDERS", "2")
    config = Config.load(tmp_path)
    assert config.api_key == "sk-test"
    assert config.default_model == "claude-opus-4-7"
    assert config.parallel_renders == 2


def test_config_no_yaml_uses_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LATTICE_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("LATTICE_PARALLEL_RENDERS", raising=False)
    config = Config.load(tmp_path)
    assert config.default_voice == "academic"
    assert config.parallel_renders == 4
    assert config.model_per_stage == {}
