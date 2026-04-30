r"""Claude Code CLI-backed LLM client.

Shells out to the `claude` binary in print mode (`claude -p`). Auth is
the user's logged-in Claude Code session; no API key required. Token
cost goes against the Claude subscription.

Binary discovery order:
1. `LATTICE_CLAUDE_CMD` env var (explicit override)
2. `shutil.which("claude")` (if on PATH)
3. Known default install locations:
   - `%USERPROFILE%\.local\bin\claude.exe`            (Anthropic native installer)
   - `%LOCALAPPDATA%\Programs\claude\claude.exe`
   - `%APPDATA%\npm\claude.cmd`                        (npm global install)
   - `~/.local/bin/claude`                             (Linux/macOS)

The client exposes the same `complete` / `complete_json` interface as
the previous API-based client, so every stage keeps working unchanged.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    model: str
    stop_reason: str


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


# ─── binary discovery ──────────────────────────────

def _find_claude_bin() -> str | None:
    override = os.environ.get("LATTICE_CLAUDE_CMD")
    if override and Path(override).exists():
        return override
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    candidates = [
        os.path.expandvars(r"%USERPROFILE%\.local\bin\claude.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\claude\claude.exe"),
        os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
        os.path.expanduser("~/.local/bin/claude"),
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def claude_available() -> bool:
    return _find_claude_bin() is not None


# ─── client ────────────────────────────────────────

class ClaudeClient:
    """Shell-out client for the `claude` CLI."""

    def __init__(
        self,
        api_key: str = "",  # kept for call-site compatibility; ignored
        default_model: str = "sonnet",
        parallel: int = 4,
    ) -> None:
        self.default_model = default_model
        # Subprocess calls are heavier than HTTP; cap concurrency to avoid
        # overwhelming the local daemon or hitting session rate limits.
        self._semaphore = asyncio.Semaphore(parallel)
        self._claude_bin = _find_claude_bin()
        if self._claude_bin is None:
            raise RuntimeError(
                "Claude Code CLI not found. Set LATTICE_CLAUDE_CMD to the path "
                "of your `claude` binary, or install Claude Code so `claude` is "
                "on your PATH."
            )

    async def complete(
        self,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.4,  # noqa: ARG002  (claude -p exposes no temperature)
        max_tokens: int = 4096,   # noqa: ARG002  (claude -p exposes no max_tokens)
    ) -> LLMResponse:
        async with self._semaphore:
            return await self._shell_out(system, user, model or self.default_model)

    async def complete_json(
        self,
        system: str,
        user: str,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> tuple[Any, LLMResponse]:
        response = await self.complete(system, user, model=model, temperature=temperature)
        return _extract_json(response.text), response

    async def _shell_out(self, system: str, user: str, model: str) -> LLMResponse:
        args = [
            self._claude_bin,
            "-p",
            "--system-prompt", system,
            "--output-format", "json",
            "--model", model,
            "--no-session-persistence",
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate(input=user.encode("utf-8"))
        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"claude -p failed (exit {proc.returncode}): {stderr_text[:500]}"
            )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        try:
            data = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"claude -p returned non-JSON output: {stdout_text[:500]!r}"
            ) from exc

        if data.get("is_error"):
            raise RuntimeError(f"claude returned error: {data.get('result') or data}")

        usage = data.get("usage") or {}
        return LLMResponse(
            text=str(data.get("result") or ""),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            model=model,
            stop_reason="end_turn",
        )


# ─── JSON-from-prose helper (unchanged from API-era client) ────

def _extract_json(text: str) -> Any:
    """Parse JSON from a model response, stripping optional ```json fences."""
    candidate = text.strip()
    match = _JSON_FENCE_RE.match(candidate)
    if match:
        candidate = match.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Response is not valid JSON:\n{text[:500]}")
