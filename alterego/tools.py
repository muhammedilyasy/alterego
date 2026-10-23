"""Tool registry.

Every tool has a JSON-schema description (sent to the model) and a Python
implementation that takes a dict of arguments and returns a string.
Implementations never raise: errors come back as text so the model can
react to them.

Tools are flagged ``mutating`` when they change state (shell, file writes,
memory saves, todos). The agent loop asks for approval before running a
mutating tool unless the approval mode says otherwise.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Settings
from .memory import MemoryStore

ToolFn = Callable[[dict[str, Any]], str]

MAX_OUTPUT = 12000

# Patterns that are refused outright unless approval mode is "auto".
# This is a speed bump, not a sandbox — see README "Safety".
DESTRUCTIVE_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/(?:\s|$|\*)",  # rm -rf / ...
    r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r",  # rm -rf (any target)
    r"\bmkfs\b",
    r"\bdd\b[^|]*\bof=/dev/",
    r":\(\)\s*\{",  # fork bomb
    r"\bshutdown\b|\breboot\b|\bpoweroff\b",
    r"\bchmod\s+-R\s+777\s+/",
    r">\s*/dev/sd[a-z]",
]


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text) - limit} more chars]"


class _TextExtractor(HTMLParser):
    """Minimal HTML -> text: drops script/style, keeps readable text."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [ln.strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return html
    return parser.text()


class Tool:
    def __init__(self, name: str, description: str, parameters: dict, fn: ToolFn, mutating: bool):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.fn = fn
        self.mutating = mutating

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, parameters: dict, fn: ToolFn, mutating: bool = False) -> None:
        self._tools[name] = Tool(name, description, parameters, fn, mutating)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def is_mutating(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.mutating)

    def specs(self) -> list[dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any] | None) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool {name!r}."
        try:
            return _truncate(str(tool.fn(arguments or {})))
        except Exception as exc:  # tools report, never raise
            return f"Error running {name}: {exc}"


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _str_prop(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def build_registry(settings: Settings, memory: MemoryStore) -> ToolRegistry:
    """Create the default registry bound to this agent's home directory."""
    registry = ToolRegistry()
    todos_file = settings.agent_home / "todos.json"

    # ---------------------------------------------------------------- shell
    def shell(args: dict[str, Any]) -> str:
        command = str(args.get("command", "")).strip()
        if not command:
            return "Error: empty command."
        timeout = float(args.get("timeout", 60))
        if settings.approval_mode != "auto":
            for pattern in DESTRUCTIVE_PATTERNS:
                if re.search(pattern, command):
                    return (
                        "Error: command refused — it matches a destructive pattern "
                        f"({pattern!r}). Set AGENT_APPROVAL_MODE=auto to allow it."
                    )
        try:
            proc = subprocess.run(
                command,
                shell=True,
                executable="/bin/bash",
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout}s: {command}"
        out = (proc.stdout or "") + (proc.stderr or "")
        return f"exit={proc.returncode}\n{out}".strip()

    registry.register(
        "shell",
        "Run a shell command (bash) and return its exit code and output. "
        "Use for system tasks, git, builds and checks. Destructive patterns "
        "are refused unless approval mode is 'auto'.",
        _schema(
            {
                "command": _str_prop("The command to run."),
                "timeout": {"type": "number", "description": "Timeout in seconds (default 60)."},
            },
            ["command"],
        ),
        shell,
        mutating=True,
    )

    # ---------------------------------------------------------------- files
    def read_file(args: dict[str, Any]) -> str:
        path = Path(str(args["path"])).expanduser()
        if not path.is_file():
            return f"Error: no such file: {path}"
        return path.read_text(encoding="utf-8", errors="replace")

    def write_file(args: dict[str, Any]) -> str:
        path = Path(str(args["path"])).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args.get("content", "")), encoding="utf-8")
        return f"Wrote {path} ({path.stat().st_size} bytes)."

    def edit_file(args: dict[str, Any]) -> str:
        path = Path(str(args["path"])).expanduser()
        if not path.is_file():
            return f"Error: no such file: {path}"
        old, new = str(args["old_text"]), str(args["new_text"])
        text = path.read_text(encoding="utf-8")
        if old not in text:
            return f"Error: old_text not found in {path}."
        if text.count(old) > 1:
            return f"Error: old_text occurs more than once in {path}; be more specific."
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"Edited {path}."

    def list_dir(args: dict[str, Any]) -> str:
        path = Path(str(args.get("path", "."))).expanduser()
        if not path.is_dir():
            return f"Error: not a directory: {path}"
        entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        lines = [f"{'d' if p.is_dir() else 'f'} {p.name}" for p in entries]
        return "\n".join(lines) if lines else f"{path} is empty."

    registry.register(
        "read_file", "Read a UTF-8 text file and return its contents.",
        _schema({"path": _str_prop("File path.")}, ["path"]), read_file,
    )
    registry.register(
        "write_file", "Write (create or overwrite) a text file with the given content.",
        _schema({"path": _str_prop("File path."), "content": _str_prop("Full file content.")}, ["path", "content"]),
        write_file, mutating=True,
    )
    registry.register(
        "edit_file", "Replace an exact, unique occurrence of old_text with new_text in a file.",
        _schema(
            {"path": _str_prop("File path."), "old_text": _str_prop("Exact text to replace."), "new_text": _str_prop("Replacement text.")},
            ["path", "old_text", "new_text"],
        ),
        edit_file, mutating=True,
    )
    registry.register(
        "list_dir", "List the entries of a directory (d = directory, f = file).",
        _schema({"path": _str_prop("Directory path (default '.').")}), list_dir,
    )

    # ------------------------------------------------------------------ web
    def web_fetch(args: dict[str, Any]) -> str:
        url = str(args["url"])
        try:
            response = httpx.get(
                url,
                timeout=30,
                follow_redirects=True,
                headers={"User-Agent": "alterego/0.1 (+personal agent)"},
            )
        except httpx.HTTPError as exc:
            return f"Error fetching {url}: {exc}"
        if response.status_code >= 400:
            return f"Error: {url} returned HTTP {response.status_code}."
        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            return html_to_text(response.text)
        return response.text

    def web_search(args: dict[str, Any]) -> str:
        query = str(args["query"])
        url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote_plus(query)
        try:
            response = httpx.get(
                url, timeout=30, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) alterego/0.1"},
            )
        except httpx.HTTPError as exc:
            return f"Error searching: {exc}"
        if response.status_code >= 400:
            return f"Error: search returned HTTP {response.status_code}."
        results: list[str] = []
        # DuckDuckGo lite rows: <a class='result-link' href=...>title</a> and
        # <td class='result-snippet'>snippet</td>
        links = re.findall(r"class=['\"]result-link['\"][^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", response.text, re.S)
        snippets = re.findall(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", response.text, re.S)
        for i, (href, title) in enumerate(links[:8]):
            title = re.sub(r"<[^>]+>", "", title).strip()
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
            results.append(f"{i + 1}. {title}\n   {href}\n   {snippet}")
        return "\n".join(results) if results else f"No results for {query!r}."

    registry.register(
        "web_fetch", "Fetch a URL and return its readable text content.",
        _schema({"url": _str_prop("The URL to fetch.")}, ["url"]), web_fetch,
    )
    registry.register(
        "web_search", "Search the web via DuckDuckGo and return the top results with snippets.",
        _schema({"query": _str_prop("Search query.")}, ["query"]), web_search,
    )

    # --------------------------------------------------------------- memory
    def memory_save(args: dict[str, Any]) -> str:
        scope = str(args.get("scope", "daily"))
        path = memory.save(str(args["text"]), scope=scope, title=args.get("title"))
        return f"Saved to {path}."

    def memory_search(args: dict[str, Any]) -> str:
        return memory.search(str(args["query"]))

    registry.register(
        "memory_save",
        "Save a durable note to memory. scope: 'daily' (default, today's log), "
        "'main' (curated MEMORY.md) or 'people/<name>' for a person note.",
        _schema(
            {"text": _str_prop("The note to save."), "scope": _str_prop("daily | main | people/<name>"), "title": _str_prop("Optional heading for daily notes.")},
            ["text"],
        ),
        memory_save, mutating=True,
    )
    registry.register(
        "memory_search", "Keyword-search all memory files (MEMORY.md, daily logs, people notes).",
        _schema({"query": _str_prop("Keywords to search for.")}, ["query"]), memory_search,
    )

    # ----------------------------------------------------------------- misc
    def get_time(args: dict[str, Any]) -> str:
        now = datetime.now().astimezone()
        return now.strftime("%A %Y-%m-%d %H:%M:%S %Z (UTC%z)")

    registry.register("get_time", "Return the current local date and time.", _schema({}), get_time)

    # ---------------------------------------------------------------- todos
    def _load_todos() -> list[dict[str, Any]]:
        if todos_file.is_file():
            try:
                data = json.loads(todos_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
            except (ValueError, OSError):
                pass
        return []

    def _save_todos(items: list[dict[str, Any]]) -> None:
        todos_file.parent.mkdir(parents=True, exist_ok=True)
        todos_file.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def todo_write(args: dict[str, Any]) -> str:
        items = args.get("items")
        if not isinstance(items, list):
            return "Error: 'items' must be a list of {text, status} objects."
        cleaned = [
            {"text": str(item.get("text", "")), "status": str(item.get("status", "pending"))}
            for item in items
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ]
        _save_todos(cleaned)
        return f"Saved {len(cleaned)} todo item(s) to {todos_file}."

    def todo_list(args: dict[str, Any]) -> str:
        items = _load_todos()
        if not items:
            return "No todos."
        marks = {"pending": "[ ]", "in_progress": "[~]", "blocked": "[!]", "completed": "[x]", "cancelled": "[-]"}
        return "\n".join(
            f"{marks.get(item.get('status', 'pending'), '[ ]')} {item.get('text', '')}" for item in items
        )

    registry.register(
        "todo_write", "Replace the whole todo list with the given items ({text, status} each).",
        _schema(
            {"items": {"type": "array", "items": {"type": "object"}, "description": "Full todo list."}},
            ["items"],
        ),
        todo_write, mutating=True,
    )
    registry.register("todo_list", "Show the current todo list.", _schema({}), todo_list)

    return registry
