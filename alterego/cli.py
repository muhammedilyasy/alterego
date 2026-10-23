"""Command-line interface.

    alterego init                      seed AGENT_HOME with persona templates
    alterego chat                      interactive REPL
    alterego run "task..." [--yes]     one-shot task
    alterego daemon [--once]          proactive watchlist daemon
    alterego doctor                    print config + test the model connection
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from importlib import resources
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown

from .agent import Agent
from .config import Settings
from .daemon import Daemon
from .llm import LLMClient, LLMError
from .memory import MemoryStore

console = Console()

PERSONA_FILES = ("SOUL.md", "USER.md", "AGENTS.md", "MEMORY.md")


def _templates_root() -> Path | None:
    """Locate the templates directory (repo checkout or installed package)."""
    candidates = [
        Path(__file__).resolve().parent / "templates",          # packaged copy
        Path(__file__).resolve().parent.parent / "templates",   # repo layout
    ]
    for candidate in candidates:
        if (candidate / "SOUL.md").is_file():
            return candidate
    try:  # importlib.resources fallback
        ref = resources.files("alterego").joinpath("templates")
        path = Path(str(ref))
        if (path / "SOUL.md").is_file():
            return path
    except Exception:
        pass
    return None


# --------------------------------------------------------------------- init
def cmd_init(settings: Settings) -> int:
    home = settings.agent_home
    root = _templates_root()
    if root is None:
        console.print("[red]Could not find the templates directory.[/red]")
        return 1
    home.mkdir(parents=True, exist_ok=True)
    for dirname in ("memory", "people", "skills", "sessions", "outbox"):
        (home / dirname).mkdir(exist_ok=True)
    created: list[str] = []
    for filename in PERSONA_FILES:
        dest = home / filename
        if dest.exists():
            continue
        shutil.copyfile(root / filename, dest)
        created.append(filename)
    watchlist = home / "watchlist.yaml"
    if not watchlist.exists():
        shutil.copyfile(root / "watchlist.yaml", watchlist)
        created.append("watchlist.yaml")
    skill_src = root / "skills" / "morning-brief" / "SKILL.md"
    skill_dest = home / "skills" / "morning-brief" / "SKILL.md"
    if skill_src.is_file() and not skill_dest.exists():
        skill_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_src, skill_dest)
        created.append("skills/morning-brief/SKILL.md")
    MemoryStore(home).ensure_layout()
    if created:
        console.print(f"Seeded {home} with: {', '.join(created)}")
    else:
        console.print(f"{home} already initialised — nothing overwritten.")
    console.print(
        "\nNext steps:\n"
        f"  1. Edit {home}/USER.md so the agent knows who you are.\n"
        "  2. Set LLM_PROVIDER / LLM_API_KEY / LLM_MODEL (see README).\n"
        "  3. Run: alterego doctor, then alterego chat."
    )
    return 0


# ------------------------------------------------------------------ doctor
def cmd_doctor(settings: Settings) -> int:
    console.print("[bold]alterego doctor[/bold]\n")
    console.print(settings.summary())
    home = settings.agent_home
    initialised = (home / "SOUL.md").is_file()
    console.print(f"home initialised: {'yes' if initialised else 'no — run `alterego init`'}")
    if not settings.api_key and settings.provider == "anthropic":
        console.print("\n[yellow]No LLM_API_KEY set — cannot test the connection.[/yellow]")
        return 0
    if settings.provider == "claude-code":
        binary = shutil.which(settings.claude_code_bin)
        if binary is None:
            console.print(
                f"\n[red]Claude Code CLI not found[/red] ({settings.claude_code_bin!r}).\n"
                "Install it with `npm install -g @anthropic-ai/claude-code`, "
                "then run `claude` once and sign in with your Claude subscription."
            )
            return 1
        console.print(f"claude binary   : {binary}")
    console.print("\nTesting the model connection with a tiny call...")
    client = LLMClient(settings)
    try:
        reply = client.complete(
            [{"role": "user", "content": "Reply with exactly: OK"}],
            tools=[],
        )
    except LLMError as exc:
        console.print(f"[red]Connection FAILED:[/red] {exc}")
        return 1
    text = (reply.get("content") or "").strip()
    console.print(f"[green]Connection OK[/green] — model replied: {text[:80]!r}")
    return 0


# -------------------------------------------------------------------- chat
def _interactive_approval(tool_name: str, arguments: dict) -> bool:
    console.print(f"\n[yellow]Approval needed[/yellow] — tool: [bold]{tool_name}[/bold]")
    console.print(json.dumps(arguments, indent=2)[:800])
    try:
        answer = input("Allow? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def _make_agent(settings: Settings, yes: bool) -> Agent:
    if yes:
        settings.approval_mode = "auto"
    callback = _interactive_approval if settings.approval_mode == "ask" else None
    return Agent(
        settings,
        approval_callback=callback,
        echo=lambda msg: console.print(f"[dim]{msg}[/dim]"),
    )


def cmd_chat(settings: Settings, yes: bool) -> int:
    agent = _make_agent(settings, yes)
    console.print("[bold]alterego[/bold] — type your message, Ctrl+C or 'exit' to quit.\n")
    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye.")
            return 0
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            console.print("Bye.")
            return 0
        reply = agent.run(user_input)
        console.print("\n[bold]alterego>[/bold]")
        console.print(Markdown(reply) if reply else "(no reply)")
        console.print()


def cmd_run(settings: Settings, task: str, yes: bool) -> int:
    agent = _make_agent(settings, yes)
    reply = agent.run(task)
    console.print(Markdown(reply) if reply else "(no reply)")
    return 0


# ------------------------------------------------------------------ daemon
def cmd_daemon(settings: Settings, once: bool) -> int:
    daemon = Daemon(settings)
    if once:
        fired = daemon.run_due()
        console.print(f"Ran due checks; {fired} notification(s). See {settings.outbox_dir}.")
        return 0
    daemon.loop()
    return 0


# -------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alterego",
        description="A personal AI agent with memory, tools, skills and a proactive daemon.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Seed AGENT_HOME with persona templates and examples.")
    sub.add_parser("doctor", help="Print config and test the model connection.")

    chat_parser = sub.add_parser("chat", help="Interactive chat REPL.")
    chat_parser.add_argument("--yes", action="store_true", help="Auto-approve all tools.")

    run_parser = sub.add_parser("run", help="Run a one-shot task.")
    run_parser.add_argument("task", help="The task, in plain language.")
    run_parser.add_argument("--yes", action="store_true", help="Auto-approve all tools.")

    daemon_parser = sub.add_parser("daemon", help="Run the proactive watchlist daemon.")
    daemon_parser.add_argument("--once", action="store_true", help="Run due checks once and exit.")

    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        return 2

    if args.command == "init":
        return cmd_init(settings)
    if args.command == "doctor":
        return cmd_doctor(settings)
    if args.command == "chat":
        return cmd_chat(settings, args.yes)
    if args.command == "run":
        return cmd_run(settings, args.task, args.yes)
    if args.command == "daemon":
        return cmd_daemon(settings, args.once)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
