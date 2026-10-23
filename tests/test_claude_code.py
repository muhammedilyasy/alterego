"""Tests for the claude-code provider (Claude Code CLI backend).

No real CLI or subscription is needed: a stub shell script stands in for
the `claude` binary and prints canned `--output-format json` envelopes.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from alterego.config import Settings
from alterego.llm import LLMClient


def _make_stub(tmp_path: Path, result_text: str) -> str:
    envelope = json.dumps({"type": "result", "result": result_text})
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\nprintf '%s' '{envelope}'\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _settings(tmp_path: Path, stub: str) -> Settings:
    return Settings(
        provider="claude-code",
        claude_code_bin=stub,
        agent_home=tmp_path / "home",
    )


def test_provider_env_normalisation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "claude_code")
    settings = Settings.from_env()
    assert settings.provider == "claude-code"
    assert settings.claude_code_bin  # defaults to "claude"


def test_tool_call_roundtrip(tmp_path: Path) -> None:
    directive = json.dumps(
        {"tool_call": {"name": "get_time", "arguments": {"tz": "UTC"}}}
    )
    client = LLMClient(_settings(tmp_path, _make_stub(tmp_path, directive)))
    reply = client.complete(
        [{"role": "user", "content": "What time is it?"}],
        tools=[{"name": "get_time", "description": "now", "parameters": {"type": "object"}}],
    )
    assert reply["tool_calls"][0]["name"] == "get_time"
    assert reply["tool_calls"][0]["arguments"] == {"tz": "UTC"}


def test_plain_text_answer(tmp_path: Path) -> None:
    client = LLMClient(_settings(tmp_path, _make_stub(tmp_path, "Hello there.")))
    reply = client.complete([{"role": "user", "content": "hi"}], tools=[])
    assert reply["content"] == "Hello there."
    assert "tool_calls" not in reply


def test_parse_fenced_directive() -> None:
    text = '```json\n{"tool_call": {"name": "todo_list", "arguments": {}}}\n```'
    reply = LLMClient._parse_claude_code_text(text)
    assert reply["tool_calls"][0]["name"] == "todo_list"


def test_missing_binary_is_a_clean_error(tmp_path: Path) -> None:
    from alterego.llm import LLMError

    settings = _settings(tmp_path, str(tmp_path / "no-such-claude"))
    client = LLMClient(settings)
    with pytest.raises(LLMError, match="not found"):
        client.complete([{"role": "user", "content": "hi"}], tools=[])
