"""Markdown memory store.

Layout under AGENT_HOME::

    MEMORY.md            curated long-term memory (edited by hand or the agent)
    memory/YYYY-MM-DD.md daily logs — raw notes appended as the day goes
    people/*.md          one note file per person

The store is deliberately plain files: the user can read, edit and version
them with any tool. Nothing here needs a database.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path


class MemoryStore:
    def __init__(self, agent_home: Path):
        self.home = Path(agent_home)
        self.main_file = self.home / "MEMORY.md"
        self.daily_dir = self.home / "memory"
        self.people_dir = self.home / "people"

    # ------------------------------------------------------------------ io
    def ensure_layout(self) -> None:
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.people_dir.mkdir(parents=True, exist_ok=True)
        if not self.main_file.exists():
            self.main_file.write_text("# MEMORY.md\n\n", encoding="utf-8")

    def daily_path(self, day: date | None = None) -> Path:
        day = day or date.today()
        return self.daily_dir / f"{day.isoformat()}.md"

    # -------------------------------------------------------------- writing
    def save(self, text: str, scope: str = "daily", title: str | None = None) -> Path:
        """Append a note. scope: "daily" (today's log), "main" (MEMORY.md)
        or "people/<slug>" for a person note."""
        self.ensure_layout()
        text = text.strip()
        if scope == "main":
            path = self.main_file
            entry = f"\n{text}\n"
        elif scope.startswith("people/"):
            slug = re.sub(r"[^a-z0-9-]+", "-", scope.split("/", 1)[1].lower()).strip("-")
            path = self.people_dir / f"{slug}.md"
            if not path.exists():
                path.write_text(f"# {slug.replace('-', ' ').title()}\n", encoding="utf-8")
            entry = f"\n{text}\n"
        else:
            path = self.daily_path()
            if not path.exists():
                path.write_text(f"# {date.today().isoformat()}\n", encoding="utf-8")
            stamp = title or "Note"
            entry = f"\n## {stamp}\n\n{text}\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        return path

    # -------------------------------------------------------------- reading
    def load_context(self, max_chars: int = 12000) -> str:
        """Context for the system prompt: MEMORY.md plus today/yesterday logs."""
        parts: list[str] = []
        if self.main_file.is_file():
            text = self.main_file.read_text(encoding="utf-8").strip()
            if text:
                parts.append(f"## MEMORY.md (long-term memory)\n\n{text}")
        for offset in (0, 1):
            day = date.today() - timedelta(days=offset)
            path = self.daily_path(day)
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    label = "today" if offset == 0 else "yesterday"
                    parts.append(f"## Daily log {day.isoformat()} ({label})\n\n{text}")
        people = sorted(self.people_dir.glob("*.md")) if self.people_dir.is_dir() else []
        if people:
            names = ", ".join(p.stem.replace("-", " ") for p in people)
            parts.append(f"## People with notes on file\n\n{names}")
        combined = "\n\n".join(parts)
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "\n...[memory truncated]"
        return combined

    # -------------------------------------------------------------- search
    def _files(self) -> list[Path]:
        files: list[Path] = []
        if self.main_file.is_file():
            files.append(self.main_file)
        if self.daily_dir.is_dir():
            files.extend(sorted(self.daily_dir.glob("*.md")))
        if self.people_dir.is_dir():
            files.extend(sorted(self.people_dir.glob("*.md")))
        return files

    def search(self, query: str, max_results: int = 8) -> str:
        """Simple keyword search over every memory file.

        Returns the best-matching snippets with their file paths, or a
        'no matches' string. Scoring is per-line keyword overlap.
        """
        terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 1]
        if not terms:
            return "memory_search: give at least one keyword of 2+ characters."
        scored: list[tuple[int, str, int, str]] = []
        for path in self._files():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for lineno, line in enumerate(lines, start=1):
                low = line.lower()
                score = sum(1 for t in terms if t in low)
                if score:
                    scored.append((score, str(path), lineno, line.strip()))
        if not scored:
            return f"No memory matches for {query!r}."
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        out = [f"Memory matches for {query!r}:"]
        for score, path, lineno, line in scored[:max_results]:
            if len(line) > 220:
                line = line[:220] + "..."
            out.append(f"- {path}:{lineno} — {line}")
        return "\n".join(out)
