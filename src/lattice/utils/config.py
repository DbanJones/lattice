"""Configuration loading.

See docs/HANDOFF.md step 2.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


_VALID_AUTOCORRECT_LEVELS = ("none", "safe", "aggressive")


@dataclass
class Config:
    project_path: Path
    api_key: str
    default_voice: str = "academic"
    default_model: str = "sonnet"
    model_per_stage: dict[str, str] = field(default_factory=dict)
    parallel_renders: int = 4
    cache_dir: Path = field(default=Path(".lattice/cache"))
    output_dir: Path = field(default=Path("outputs"))
    # Autocorrect level — how aggressively the tool fixes audit flags
    # without explicit author review.
    #
    # - none:       finalise hard-fails on critical flags; author resolves
    #               every flag manually via `lattice flags` / `propose` /
    #               `apply`. The original conservative default.
    # - safe:       runs `propose` + `apply` for flags whose default_mode
    #               is `suggest_changes` (mechanical prose fixes — weasel
    #               words, citation engagement, formality). Never deletes
    #               sentences and never mutates the graph. Default.
    # - aggressive: runs the safe pass and additionally accepts
    #               `rewrite`-mode flags (re-renders the affected
    #               clusters) and deletes orphan sentences when no claim
    #               can be added. Still never mutates the graph
    #               structure (no new claims, no relationship changes).
    autocorrect: str = "safe"

    @classmethod
    def load(cls, project_path: Path) -> "Config":
        """Load from project_path/config.yml + .env + environment.

        Order of precedence (low to high):
        1. Defaults (dataclass field defaults)
        2. config.yml
        3. .env (project, then CWD)
        4. environment variables
        """
        project_path = Path(project_path)

        cfg: dict = {}
        config_path = project_path / "config.yml"
        if config_path.exists():
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

        # .env: project first, then CWD. override=False means existing env wins.
        load_dotenv(project_path / ".env", override=False)
        load_dotenv(override=False)

        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

        default_model = os.environ.get(
            "LATTICE_DEFAULT_MODEL",
            cfg.get("default_model", "sonnet"),
        )
        parallel_renders = int(
            os.environ.get("LATTICE_PARALLEL_RENDERS", cfg.get("parallel_renders", 4))
        )

        cache_dir = Path(cfg.get("cache_dir", ".lattice/cache"))
        output_dir = Path(cfg.get("output_dir", "outputs"))
        if not cache_dir.is_absolute():
            cache_dir = project_path / cache_dir
        if not output_dir.is_absolute():
            output_dir = project_path / output_dir

        autocorrect = str(
            os.environ.get(
                "LATTICE_AUTOCORRECT",
                cfg.get("autocorrect", "safe"),
            )
        ).lower().strip()
        if autocorrect not in _VALID_AUTOCORRECT_LEVELS:
            raise ValueError(
                f"Invalid autocorrect level {autocorrect!r}. "
                f"Must be one of: {', '.join(_VALID_AUTOCORRECT_LEVELS)}."
            )

        return cls(
            project_path=project_path,
            api_key=api_key,
            default_voice=cfg.get("default_voice", "academic"),
            default_model=default_model,
            model_per_stage=dict(cfg.get("model_per_stage") or {}),
            parallel_renders=parallel_renders,
            cache_dir=cache_dir,
            output_dir=output_dir,
            autocorrect=autocorrect,
        )

    def model_for_stage(self, stage: str) -> str:
        """Return the configured model for a stage, falling back to default."""
        return self.model_per_stage.get(stage, self.default_model)
