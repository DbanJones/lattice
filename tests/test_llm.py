"""Tests for the Claude Code CLI-backed ClaudeClient.

Never invokes the real `claude` binary — every subprocess is mocked.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lattice.utils.llm import ClaudeClient, _extract_json, _find_claude_bin


# ─── _extract_json helper (unchanged behaviour) ──

def test_extract_json_plain() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_code_fence() -> None:
    assert _extract_json('```json\n{"a": 2}\n```') == {"a": 2}


def test_extract_json_with_prose_around() -> None:
    assert _extract_json('Here is:\n\n{"a": 3}\n\nDone.') == {"a": 3}


def test_extract_json_array() -> None:
    assert _extract_json('[1, 2, 3]') == [1, 2, 3]


def test_extract_json_raises_on_non_json() -> None:
    with pytest.raises(ValueError):
        _extract_json("not json at all")


# ─── binary discovery ────────────────────────────

def test_find_bin_respects_env_override(monkeypatch, tmp_path: Path) -> None:
    fake = tmp_path / "claude.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("LATTICE_CLAUDE_CMD", str(fake))
    assert _find_claude_bin() == str(fake)


def test_find_bin_returns_none_when_missing(monkeypatch) -> None:
    monkeypatch.delenv("LATTICE_CLAUDE_CMD", raising=False)
    with patch("shutil.which", return_value=None):
        with patch("pathlib.Path.exists", return_value=False):
            assert _find_claude_bin() is None


# ─── ClaudeClient.complete() ─────────────────────

def _fake_claude_response(
    result: str = "ok",
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int = 0,
    cache_create: int = 0,
    is_error: bool = False,
) -> bytes:
    return json.dumps({
        "type": "result",
        "subtype": "success" if not is_error else "error",
        "is_error": is_error,
        "result": result,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_create,
        },
    }).encode("utf-8")


async def _patch_subprocess(stdout_bytes: bytes, returncode: int = 0, stderr: bytes = b""):
    """Return a fake create_subprocess_exec that yields the given stdout."""
    fake_proc = MagicMock()
    fake_proc.returncode = returncode
    fake_proc.communicate = AsyncMock(return_value=(stdout_bytes, stderr))
    return AsyncMock(return_value=fake_proc)


async def test_complete_parses_claude_json(tmp_path: Path, monkeypatch) -> None:
    fake_bin = tmp_path / "claude.exe"
    fake_bin.write_bytes(b"")
    monkeypatch.setenv("LATTICE_CLAUDE_CMD", str(fake_bin))

    client = ClaudeClient(default_model="sonnet")
    with patch(
        "asyncio.create_subprocess_exec",
        await _patch_subprocess(_fake_claude_response(result="Hello back.")),
    ):
        resp = await client.complete(system="be terse", user="hi")
    assert resp.text == "Hello back."
    assert resp.input_tokens == 100
    assert resp.output_tokens == 20
    assert resp.model == "sonnet"


async def test_complete_passes_system_prompt_and_model(tmp_path: Path, monkeypatch) -> None:
    fake_bin = tmp_path / "claude.exe"
    fake_bin.write_bytes(b"")
    monkeypatch.setenv("LATTICE_CLAUDE_CMD", str(fake_bin))

    client = ClaudeClient(default_model="sonnet")
    captured_args: list[str] = []

    async def _fake_exec(*args, **kwargs):
        captured_args.extend(args)
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(_fake_claude_response(), b""))
        return proc

    with patch("asyncio.create_subprocess_exec", _fake_exec):
        await client.complete(system="SYSPROMPT", user="USERMSG", model="opus")

    joined = " ".join(captured_args)
    assert "--system-prompt" in captured_args
    assert "SYSPROMPT" in captured_args
    assert "--model" in captured_args
    assert "opus" in captured_args
    assert "--output-format" in captured_args
    assert "--no-session-persistence" in captured_args


async def test_complete_passes_user_prompt_via_stdin(tmp_path: Path, monkeypatch) -> None:
    fake_bin = tmp_path / "claude.exe"
    fake_bin.write_bytes(b"")
    monkeypatch.setenv("LATTICE_CLAUDE_CMD", str(fake_bin))

    client = ClaudeClient(default_model="sonnet")
    stdin_captured: dict = {}

    async def _fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = 0

        async def _communicate(input=None):
            stdin_captured["input"] = input
            return (_fake_claude_response(), b"")

        proc.communicate = _communicate
        return proc

    with patch("asyncio.create_subprocess_exec", _fake_exec):
        await client.complete(system="s", user="the-user-prompt")

    assert stdin_captured["input"] == b"the-user-prompt"


async def test_complete_raises_on_nonzero_exit(tmp_path: Path, monkeypatch) -> None:
    fake_bin = tmp_path / "claude.exe"
    fake_bin.write_bytes(b"")
    monkeypatch.setenv("LATTICE_CLAUDE_CMD", str(fake_bin))
    client = ClaudeClient()

    with patch(
        "asyncio.create_subprocess_exec",
        await _patch_subprocess(b"", returncode=1, stderr=b"auth failed"),
    ):
        with pytest.raises(RuntimeError, match="auth failed"):
            await client.complete(system="s", user="u")


async def test_complete_raises_on_is_error_true(tmp_path: Path, monkeypatch) -> None:
    fake_bin = tmp_path / "claude.exe"
    fake_bin.write_bytes(b"")
    monkeypatch.setenv("LATTICE_CLAUDE_CMD", str(fake_bin))
    client = ClaudeClient()

    with patch(
        "asyncio.create_subprocess_exec",
        await _patch_subprocess(_fake_claude_response(result="nope", is_error=True)),
    ):
        with pytest.raises(RuntimeError):
            await client.complete(system="s", user="u")


async def test_complete_json_parses_result(tmp_path: Path, monkeypatch) -> None:
    fake_bin = tmp_path / "claude.exe"
    fake_bin.write_bytes(b"")
    monkeypatch.setenv("LATTICE_CLAUDE_CMD", str(fake_bin))
    client = ClaudeClient()

    with patch(
        "asyncio.create_subprocess_exec",
        await _patch_subprocess(_fake_claude_response(result='{"k": 42}')),
    ):
        payload, resp = await client.complete_json(system="s", user="u")
    assert payload == {"k": 42}
    assert resp.text == '{"k": 42}'


def test_missing_binary_raises(monkeypatch) -> None:
    monkeypatch.delenv("LATTICE_CLAUDE_CMD", raising=False)
    with patch("lattice.utils.llm._find_claude_bin", return_value=None):
        with pytest.raises(RuntimeError, match="Claude Code CLI not found"):
            ClaudeClient()
