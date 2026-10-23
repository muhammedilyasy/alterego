"""Proactive daemon.

Reads AGENT_HOME/watchlist.yaml — a list of checks:

    - name: morning-brief
      every_minutes: 1440
      prompt: "Prepare my morning brief from memory and todos."
    - name: site-uptime
      every_minutes: 30
      shell: "curl -sf https://example.com >/dev/null || echo DOWN https://example.com"

Prompt checks are run through the agent loop. The agent is told to reply
with exactly NOTHING when there is nothing worth surfacing; anything else
becomes a notification. Shell checks notify when they print output or fail.

Notifications are written as markdown files to AGENT_HOME/outbox/ and
printed to stdout. Run state (last run per check) lives in
AGENT_HOME/daemon_state.json so restarts don't re-fire everything.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .agent import Agent
from .config import Settings

NOTHING = "NOTHING"


class Daemon:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.watchlist_path = settings.agent_home / "watchlist.yaml"
        self.state_path = settings.agent_home / "daemon_state.json"

    # ---------------------------------------------------------------- state
    def _load_state(self) -> dict[str, float]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in data.items()}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict[str, float]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # ------------------------------------------------------------- watchlist
    def load_checks(self) -> list[dict[str, Any]]:
        if not self.watchlist_path.is_file():
            return []
        data = yaml.safe_load(self.watchlist_path.read_text(encoding="utf-8")) or {}
        checks = data.get("checks", data if isinstance(data, list) else [])
        valid: list[dict[str, Any]] = []
        for check in checks:
            if isinstance(check, dict) and check.get("name") and (
                check.get("prompt") or check.get("shell")
            ):
                valid.append(check)
        return valid

    def due_checks(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        state = self._load_state()
        due: list[dict[str, Any]] = []
        for check in self.load_checks():
            every = float(check.get("every_minutes", 60)) * 60
            last = state.get(str(check["name"]), 0.0)
            if now - last >= every:
                due.append(check)
        return due

    # --------------------------------------------------------------- running
    def run_check(self, check: dict[str, Any]) -> str | None:
        """Run one check; return notification text or None if nothing to say."""
        name = str(check["name"])
        if check.get("shell"):
            return self._run_shell_check(name, str(check["shell"]))
        prompt = (
            str(check["prompt"])
            + "\n\nIf there is genuinely nothing worth surfacing right now, "
            "reply with exactly NOTHING and nothing else."
        )
        agent = Agent(self.settings)
        result = agent.run(prompt).strip()
        if not result or result.upper() == NOTHING:
            return None
        return result

    def _run_shell_check(self, name: str, command: str) -> str | None:
        try:
            proc = subprocess.run(
                command, shell=True, executable="/bin/bash",
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            return f"Shell check {name!r} timed out after 300s."
        output = (proc.stdout or "").strip()
        errors = (proc.stderr or "").strip()
        if proc.returncode != 0:
            detail = output or errors or f"exit code {proc.returncode}"
            return f"Shell check {name!r} failed: {detail}"
        return output or None

    def notify(self, check_name: str, text: str) -> Path:
        self.settings.outbox_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9-]+", "-", check_name.lower()).strip("-") or "check"
        path = self.settings.outbox_dir / f"{stamp}-{slug}.md"
        path.write_text(f"# {check_name}\n\n{text}\n", encoding="utf-8")
        print(f"\n=== notification: {check_name} ===\n{text}\n(saved to {path})")
        return path

    def run_due(self) -> int:
        """Run every due check once; return how many notifications fired."""
        state = self._load_state()
        fired = 0
        for check in self.due_checks():
            name = str(check["name"])
            try:
                result = self.run_check(check)
            except Exception as exc:  # one bad check must not kill the daemon
                result = f"Check {name!r} errored: {exc}"
            state[name] = time.time()
            self._save_state(state)
            if result:
                self.notify(name, result)
                fired += 1
        return fired

    def loop(self, tick_seconds: int = 30) -> None:
        checks = self.load_checks()
        if not checks:
            print(f"No checks found in {self.watchlist_path}. Run `alterego init` first.")
            return
        print(f"Daemon watching {len(checks)} check(s) from {self.watchlist_path}. Ctrl+C to stop.")
        try:
            while True:
                self.run_due()
                time.sleep(tick_seconds)
        except KeyboardInterrupt:
            print("\nDaemon stopped.")
