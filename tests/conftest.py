"""Shared pytest fixtures.

Provides:
- example_project: temp copy of examples/projects/ict_forecasting
- mock_claude_client: ClaudeClient stub returning canned responses
- store: GraphStore on a fresh temp project
- voice: parsed academic voice from examples/voices/
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


@pytest.fixture
def example_project(tmp_path: Path) -> Path:
    """Copy the ict_forecasting example project to a temp directory."""
    src = EXAMPLES_DIR / "projects" / "ict_forecasting"
    dst = tmp_path / "ict_forecasting"
    shutil.copytree(src, dst)
    (dst / ".lattice").mkdir(exist_ok=True)
    return dst


@pytest.fixture
def voices_dir() -> Path:
    return EXAMPLES_DIR / "voices"


@pytest.fixture
def academic_voice_path(voices_dir: Path) -> Path:
    return voices_dir / "academic.voice.md"


class MockClaudeClient:
    """Stub for ClaudeClient. Returns canned responses by call signature.

    Tests register responses via .register(system_prefix, response).
    """
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses: list[tuple[str, Any]] = []

    def register(self, system_prefix: str, response: Any) -> None:
        self._responses.append((system_prefix, response))

    async def complete(
        self,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int = 4096,
    ) -> Any:
        self.calls.append({"system": system, "user": user, "model": model})
        for prefix, resp in self._responses:
            if system.startswith(prefix):
                return resp
        raise ValueError(f"No mock response registered for system: {system[:60]}...")

    async def complete_json(
        self,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> tuple[dict, Any]:
        result = await self.complete(system, user, model, temperature)
        if hasattr(result, "text"):
            import json
            return json.loads(result.text), result
        return result, None


@pytest.fixture
def mock_llm() -> MockClaudeClient:
    return MockClaudeClient()
