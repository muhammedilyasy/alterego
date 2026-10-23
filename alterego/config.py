"""Configuration for alterego.

Everything is configured through environment variables (optionally via a
``.env`` file in the current directory or in AGENT_HOME):

    LLM_PROVIDER        "openai" (any OpenAI-compatible API) or "anthropic"
    LLM_API_KEY         API key for the provider (may be empty for local servers)
    LLM_BASE_URL        Base URL, e.g. https://openrouter.ai/api/v1,
                        http://localhost:11434/v1 (Ollama), https://api.anthropic.com
    LLM_MODEL           Model name, e.g. gpt-4o-mini, claude-sonnet-4-5, llama3.1
    AGENT_HOME          Data directory (default ~/.alterego)
    AGENT_APPROVAL_MODE "ask" (default), "auto" (allow everything) or
                        "deny" (block all state-changing tools)
    AGENT_MAX_STEPS     Max tool-calling steps per turn (default 25)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
}

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}


def _load_dotenv() -> None:
    """Load a .env file (current directory first, then AGENT_HOME).

    Real environment variables always win; this only fills in gaps.
    """
    candidates = [Path.cwd() / ".env"]
    home = os.environ.get("AGENT_HOME")
    if home:
        candidates.append(Path(home) / ".env")
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            os.environ.setdefault(key, value)
        break


@dataclass
class Settings:
    provider: str = "openai"  # "openai" | "anthropic"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    agent_home: Path = field(default_factory=lambda: Path.home() / ".alterego")
    approval_mode: str = "ask"  # "ask" | "auto" | "deny"
    max_steps: int = 25
    request_timeout: float = 120.0

    @classmethod
    def from_env(cls, load_dotenv: bool = True) -> "Settings":
        if load_dotenv:
            _load_dotenv()
        provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
        if provider in ("openai-compatible", "openai_compatible"):
            provider = "openai"
        if provider not in DEFAULT_MODELS:
            raise ValueError(
                f"LLM_PROVIDER must be 'openai' or 'anthropic', got {provider!r}"
            )
        approval = os.environ.get("AGENT_APPROVAL_MODE", "ask").strip().lower()
        if approval not in ("ask", "auto", "deny"):
            raise ValueError(
                f"AGENT_APPROVAL_MODE must be ask|auto|deny, got {approval!r}"
            )
        agent_home = Path(
            os.environ.get("AGENT_HOME", str(Path.home() / ".alterego"))
        ).expanduser()
        return cls(
            provider=provider,
            api_key=os.environ.get("LLM_API_KEY", ""),
            base_url=os.environ.get("LLM_BASE_URL", "") or DEFAULT_BASE_URLS[provider],
            model=os.environ.get("LLM_MODEL", "") or DEFAULT_MODELS[provider],
            agent_home=agent_home,
            approval_mode=approval,
            max_steps=int(os.environ.get("AGENT_MAX_STEPS", "25")),
        )

    @property
    def memory_dir(self) -> Path:
        return self.agent_home / "memory"

    @property
    def skills_dir(self) -> Path:
        return self.agent_home / "skills"

    @property
    def sessions_dir(self) -> Path:
        return self.agent_home / "sessions"

    @property
    def outbox_dir(self) -> Path:
        return self.agent_home / "outbox"

    def masked_key(self) -> str:
        if not self.api_key:
            return "(not set)"
        if len(self.api_key) <= 8:
            return "********"
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"

    def summary(self) -> str:
        return (
            f"provider      : {self.provider}\n"
            f"model         : {self.model}\n"
            f"base_url      : {self.base_url}\n"
            f"api_key       : {self.masked_key()}\n"
            f"agent_home    : {self.agent_home}\n"
            f"approval_mode : {self.approval_mode}\n"
            f"max_steps     : {self.max_steps}"
        )
