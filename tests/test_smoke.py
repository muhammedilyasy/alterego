"""Smoke tests — no real API key required.

Covers: config loading from env, tool registry schema validity, a memory
save/search roundtrip in a temporary AGENT_HOME, and the agent loop driven
by a fake LLM that makes one tool call and then answers.
"""

from __future__ import annotations

import json

import pytest

from alterego.agent import Agent
from alterego.config import Settings
from alterego.memory import MemoryStore
from alterego.tools import build_registry


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # keep any developer .env out of the tests
    for var in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_config_defaults(home):
    settings = Settings.from_env()
    assert settings.provider == "openai"
    assert settings.model  # a default is filled in
    assert settings.base_url.startswith("http")
    assert settings.agent_home == home
    assert settings.approval_mode == "ask"


def test_config_from_env(home, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-1234567890")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-5")
    settings = Settings.from_env()
    assert settings.provider == "anthropic"
    assert settings.model == "claude-sonnet-4-5"
    assert "1234" not in settings.masked_key() or settings.masked_key().endswith("7890")


def test_tool_registry_schemas_are_valid_json(home):
    settings = Settings.from_env()
    registry = build_registry(settings, MemoryStore(home))
    specs = registry.specs()
    assert len(specs) >= 10
    names = {spec["name"] for spec in specs}
    for expected in (
        "shell", "read_file", "write_file", "edit_file", "list_dir",
        "web_fetch", "web_search", "memory_save", "memory_search",
        "get_time", "todo_write", "todo_list",
    ):
        assert expected in names
    # Every spec must round-trip through JSON (it goes on the wire verbatim).
    for spec in specs:
        json.loads(json.dumps(spec))
        assert spec["parameters"]["type"] == "object"


def test_tool_errors_come_back_as_text(home):
    settings = Settings.from_env()
    registry = build_registry(settings, MemoryStore(home))
    assert registry.call("no_such_tool", {}).startswith("Error")
    assert "Error" in registry.call("read_file", {"path": "/definitely/not/here.txt"})


def test_memory_roundtrip(home):
    store = MemoryStore(home)
    store.ensure_layout()
    store.save("The user's favourite coffee is a flat white.", scope="main")
    store.save("Shipped the alterego repo today.", scope="daily", title="Work")
    hits = store.search("flat white")
    assert "MEMORY.md" in hits
    assert "flat white" in store.search("coffee")
    assert "No memory matches" in store.search("zzzqqq")
    context = store.load_context()
    assert "flat white" in context


def test_todo_tools_roundtrip(home):
    settings = Settings.from_env()
    registry = build_registry(settings, MemoryStore(home))
    registry.call("todo_write", {"items": [{"text": "Ship it", "status": "in_progress"}]})
    listing = registry.call("todo_list", {})
    assert "Ship it" in listing


class FakeLLM:
    """First call: ask for get_time. Second call: final answer."""

    def __init__(self):
        self.calls = 0
        self.saw_tool_result = False

    def complete(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            assert tools, "agent must offer tools to the model"
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "get_time", "arguments": {}}],
            }
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call_1"
        self.saw_tool_result = True
        return {"role": "assistant", "content": "It is done."}


def test_agent_loop_with_fake_llm(home):
    settings = Settings.from_env()
    settings.approval_mode = "auto"
    fake = FakeLLM()
    agent = Agent(settings, llm=fake)
    answer = agent.run("What time is it?")
    assert answer == "It is done."
    assert fake.saw_tool_result
    assert agent.session_file.is_file()  # transcript persisted


def test_agent_blocks_mutating_tools_when_denied(home):
    settings = Settings.from_env()
    settings.approval_mode = "deny"

    class ShellFakeLLM(FakeLLM):
        def complete(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "name": "write_file",
                         "arguments": {"path": str(home / "nope.txt"), "content": "x"}}
                    ],
                }
            assert "not run" in messages[-1]["content"]
            return {"role": "assistant", "content": "Blocked as expected."}

    agent = Agent(settings, llm=ShellFakeLLM())
    assert agent.run("write a file") == "Blocked as expected."
    assert not (home / "nope.txt").exists()
