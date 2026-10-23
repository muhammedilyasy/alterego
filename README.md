# alterego

A personal AI agent in the style of Meta's Muse: a persistent identity, long-term memory kept as plain markdown, a real toolset, reusable skills/playbooks, and a proactive daemon that checks things on a schedule and surfaces what matters — while you get on with your day.

**Bring your own model.** alterego talks to any OpenAI-compatible API (OpenAI, OpenRouter, Together, Groq, a local Ollama or LM Studio) and to Anthropic's Messages API. The connection is configured purely with environment variables — no code changes, no vendor lock-in.

## Architecture

```
                        ┌────────────────────────────────────────────┐
                        │                AGENT_HOME                  │
                        │  ~/.alterego/                              │
   you ── chat/run ───► │                                            │
                        │  SOUL.md    who the agent is (persona)     │
   cron/systemd ───────►│  USER.md    who you are                    │
        daemon          │  AGENTS.md  working conventions            │
                        │  MEMORY.md  curated long-term memory       │
                        │  memory/    daily logs (YYYY-MM-DD.md)     │
                        │  people/    notes per person               │
                        │  skills/    playbooks (SKILL.md files)     │
                        │  watchlist.yaml   proactive checks         │
                        │  sessions/  transcripts (JSONL)            │
                        │  outbox/    daemon notifications           │
                        └────────────────────────────────────────────┘
                                      │
                        ┌─────────────┴──────────────┐
                        │          Agent loop        │
                        │  system prompt = persona   │
                        │  + memory + skills index   │
                        │  → model → tool calls →    │
                        │  results → model → ...     │
                        └─────────────┬──────────────┘
              ┌───────────────────────┼────────────────────────┐
              ▼                       ▼                        ▼
        Tool registry           LLM client              Daemon
     shell · files · web    OpenAI-compatible OR     watchlist checks
     search · memory ·      Anthropic Messages API   on a schedule →
     todos · time           (one internal format)    outbox notifications
```

Key design choices:

- **One internal message format.** `alterego/llm.py` converts to and from OpenAI `tool_calls` and Anthropic `tool_use`/`tool_result` blocks, so the agent loop never cares which provider is behind it.
- **Memory is files, not a database.** You can read, edit, diff and version your agent's memory with any tool you already have.
- **Approvals are explicit.** State-changing tools (shell, file writes, memory saves, todos) can require your approval. See *Safety* below.
- **The daemon is just the agent, woken up.** Watchlist checks either run a prompt through the same agent loop or run a shell command; noteworthy results land in `outbox/`.

## Quickstart

```bash
git clone <this repo> && cd alterego
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

alterego init        # seeds ~/.alterego with persona templates + examples
cp .env.example .env # then fill in your model connection
alterego doctor      # prints config and test-calls the model
alterego chat        # talk to it
```

Edit `~/.alterego/USER.md` first — the agent works far better once it knows who you are. `SOUL.md` (its persona) and `AGENTS.md` (its working conventions) are yours to shape over time; that shaping *is* the product.

## Connecting a model

Everything is environment variables (or a `.env` file in the working directory or in AGENT_HOME):

| Variable | Meaning |
|---|---|
| `LLM_PROVIDER` | `openai` (any OpenAI-compatible API) or `anthropic` |
| `LLM_API_KEY` | Your key (may be empty for local servers) |
| `LLM_BASE_URL` | API base URL |
| `LLM_MODEL` | Model name |
| `AGENT_HOME` | Data directory (default `~/.alterego`) |
| `AGENT_APPROVAL_MODE` | `ask` (default), `auto` or `deny` |
| `AGENT_MAX_STEPS` | Max tool steps per turn (default 25) |

### OpenAI

```bash
LLM_PROVIDER=openai
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

### OpenRouter

```bash
LLM_PROVIDER=openai
LLM_API_KEY=sk-or-...
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=anthropic/claude-sonnet-4.5
```

### Together / Groq

```bash
# Together: LLM_BASE_URL=https://api.together.xyz/v1
# Groq:     LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_PROVIDER=openai
LLM_API_KEY=...
LLM_MODEL=<a model your provider serves>
```

### Local — Ollama or LM Studio (no key needed)

```bash
LLM_PROVIDER=openai
LLM_API_KEY=
LLM_BASE_URL=http://localhost:11434/v1   # Ollama (LM Studio: http://localhost:1234/v1)
LLM_MODEL=llama3.1
```

> Pick a local model with solid tool-calling support; the agent loop depends on it.

### Anthropic

```bash
LLM_PROVIDER=anthropic
LLM_API_KEY=sk-ant-...
LLM_BASE_URL=https://api.anthropic.com
LLM_MODEL=claude-sonnet-4-5
```

## Usage

```bash
alterego chat                    # interactive REPL (approvals asked in-line)
alterego run "Summarise today's memory notes and save the summary"
alterego run "Check the build" --yes     # one-shot, tools auto-approved
alterego daemon                  # proactive daemon (foreground)
alterego daemon --once           # run due checks once — ideal for cron
alterego doctor                  # config + connection test
```

### Skills

A skill is a folder under `~/.alterego/skills/` with a `SKILL.md`: frontmatter (`name`, `description`) plus a playbook in plain language. The agent sees an index of skills in its system prompt and reads the relevant file before following it. An example (`morning-brief`) is seeded by `alterego init`; the same file lives in this repo at `skills/example-skill/SKILL.md`.

### The proactive daemon

`~/.alterego/watchlist.yaml` lists checks. Each is either:

- a **prompt** check — run through the agent; if there's nothing worth surfacing, the agent replies `NOTHING` and no notification is written; or
- a **shell** check — any stdout (or a non-zero exit) becomes a notification.

Notifications are markdown files in `~/.alterego/outbox/` and are printed to stdout. `alterego init` seeds two clearly-marked examples: a daily morning brief and a website uptime check.

Run it under cron (simplest, no long-lived process):

```cron
*/15 * * * * cd ~/alterego && .venv/bin/alterego daemon --once >> ~/.alterego/daemon.log 2>&1
```

…or as a systemd user service:

```ini
# ~/.config/systemd/user/alterego.service
[Unit]
Description=alterego proactive daemon
After=network-online.target

[Service]
WorkingDirectory=%h/alterego
ExecStart=%h/alterego/.venv/bin/alterego daemon
Restart=on-failure
EnvironmentFile=%h/alterego/.env

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now alterego
```

## Safety & approvals

- Mutating tools are `shell`, `write_file`, `edit_file`, `memory_save` and `todo_write`.
- `AGENT_APPROVAL_MODE=ask` (default): in `chat` you approve each call in-line; anywhere non-interactive (one-shot runs without `--yes`, the daemon) unapproved mutating tools are **denied**, and the model is told so.
- `auto`: everything runs. Sensible only for a trusted, sandboxed setup — the daemon on your own machine with a watchlist you wrote.
- `deny`: no state-changing tool ever runs.
- The `shell` tool additionally refuses a small set of destructive patterns (e.g. `rm -rf /`, `mkfs`, fork bombs) unless the mode is `auto`. This is a speed bump, not a sandbox: an agent with shell access is exactly as powerful as your user account. Run it in a VM or container if that thought bothers you — it should, a little.
- Your API key lives only in your environment/`.env` (gitignored). Memory files are plain markdown on your disk; nothing is sent anywhere except to the model API you configured.

## Project layout

```
alterego/
├── alterego/
│   ├── cli.py        commands: init / chat / run / daemon / doctor
│   ├── agent.py      the tool-calling agent loop + system-prompt builder
│   ├── llm.py        OpenAI-compatible + Anthropic client, one format
│   ├── tools.py      tool registry: shell, files, web, memory, todos, time
│   ├── memory.py     markdown memory store (MEMORY.md, daily logs, people)
│   ├── daemon.py     proactive watchlist runner → outbox notifications
│   ├── config.py     env/.env settings
│   └── templates/    packaged copies of the seed files
├── templates/        SOUL.md / USER.md / AGENTS.md / MEMORY.md / watchlist.yaml
├── skills/
│   └── example-skill/SKILL.md   the morning-brief playbook
├── tests/test_smoke.py          pytest smoke tests (no API key needed)
├── .env.example
└── pyproject.toml
```

## Development

```bash
pip install -e ".[dev]"
pytest            # smoke tests run against a fake LLM — no key required
```

## License

MIT
