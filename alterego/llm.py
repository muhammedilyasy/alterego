"""LLM chat client with tool calling for three provider families.

The third, "claude-code", has no HTTP API at all: it thinks through the
locally installed Claude Code CLI (`claude -p`), which signs in with a
Claude subscription instead of an API key. alterego renders the whole
conversation into one prompt, forbids the CLI's own tools, and reads back
either a final answer or a single JSON tool-call directive, which alterego
then executes with its own tools (so approvals and memory still apply).

One internal message format is used everywhere in alterego (OpenAI-flavoured):

    {"role": "system",    "content": "..."}
    {"role": "user",      "content": "..."}
    {"role": "assistant", "content": "..." | None,
                          "tool_calls": [{"id": ..., "name": ..., "arguments": {...}}]}
    {"role": "tool",      "tool_call_id": ..., "name": ..., "content": "..."}

Tools are described internally as:

    {"name": ..., "description": ..., "parameters": {<JSON schema>}}

`LLMClient.complete(messages, tools)` returns one internal assistant message.
Conversions to OpenAI's `tool_calls` wire format and to Anthropic's
`tool_use`/`tool_result` content blocks happen here and nowhere else.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import httpx

from .config import Settings

Message = dict[str, Any]
ToolSpec = dict[str, Any]


class LLMError(RuntimeError):
    """Raised for transport/API errors; the agent loop surfaces it as text."""


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        # trust_env=False: ambient proxy variables must not leak into (or
        # break) API calls; the configured base URL is the whole story.
        self._http = httpx.Client(timeout=settings.request_timeout, trust_env=False)

    # ------------------------------------------------------------- public
    def complete(self, messages: list[Message], tools: list[ToolSpec] | None = None) -> Message:
        if self.settings.provider == "anthropic":
            return self._complete_anthropic(messages, tools or [])
        if self.settings.provider == "claude-code":
            return self._complete_claude_code(messages, tools or [])
        return self._complete_openai(messages, tools or [])

    # --------------------------------------------------------- claude-code
    def _complete_claude_code(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        prompt = self._render_claude_code_prompt(messages, tools)
        scratch = self.settings.agent_home / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        command = [
            self.settings.claude_code_bin,
            "-p", prompt,
            "--output-format", "json",
            "--no-session-persistence",
            "--allowedTools", "",
        ]
        if self.settings.model:
            command += ["--model", self.settings.model]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                cwd=str(scratch),
                timeout=max(self.settings.request_timeout, 60.0),
            )
        except FileNotFoundError as exc:
            raise LLMError(
                "Claude Code CLI not found. Install it with "
                "`npm install -g @anthropic-ai/claude-code`, then run `claude` "
                "once and sign in with your Claude subscription."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"Claude Code CLI timed out after {exc.timeout:.0f}s") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise LLMError(
                f"Claude Code CLI exited {proc.returncode}: {detail} "
                "(if this is an auth error, run `claude` once and sign in)"
            )
        return self._parse_claude_code_output(proc.stdout)

    @staticmethod
    def _render_claude_code_prompt(messages: list[Message], tools: list[ToolSpec]) -> str:
        parts: list[str] = []
        for message in messages:
            if message["role"] == "system" and message.get("content"):
                parts.append(message["content"])
        if tools:
            catalogue = "\n".join(
                f"- {t['name']}: {t.get('description', '')} "
                f"Arguments JSON schema: {json.dumps(t.get('parameters', {}))}"
                for t in tools
            )
            parts.append(
                "You are operating inside another program that gives you tools. "
                "You cannot call tools directly; the program calls them for you.\n"
                f"Available tools:\n{catalogue}"
            )
        for message in messages:
            role = message["role"]
            if role == "system":
                continue
            if role == "user":
                parts.append(f"USER:\n{message.get('content', '')}")
            elif role == "assistant":
                if message.get("content"):
                    parts.append(f"ASSISTANT:\n{message['content']}")
                for call in message.get("tool_calls") or []:
                    parts.append(
                        f"ASSISTANT CALLED TOOL {call['name']} "
                        f"with arguments {json.dumps(call.get('arguments', {}))}"
                    )
            elif role == "tool":
                parts.append(
                    f"TOOL RESULT ({message.get('name', 'tool')}):\n{message.get('content', '')}"
                )
        if tools:
            parts.append(
                "Produce ONLY the next assistant message. If a tool is needed, reply "
                "with exactly one JSON object and nothing else:\n"
                '{"tool_call": {"name": "<tool name>", "arguments": {<arguments>}}}\n'
                "If no tool is needed, reply with the final answer as plain text."
            )
        else:
            parts.append("Reply with the answer as plain text.")
        return "\n\n".join(parts)

    @classmethod
    def _parse_claude_code_output(cls, stdout: str) -> Message:
        text = stdout.strip()
        try:
            envelope = json.loads(text)
            if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
                text = envelope["result"].strip()
        except (ValueError, TypeError):
            pass  # older/plain CLI output: treat stdout itself as the reply
        return cls._parse_claude_code_text(text)

    @staticmethod
    def _parse_claude_code_text(text: str) -> Message:
        decoder = json.JSONDecoder()
        for idx, char in enumerate(text):
            if char != "{":
                continue
            try:
                obj, end = decoder.raw_decode(text[idx:])
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("tool_call"), dict):
                call = obj["tool_call"]
                name = call.get("name", "")
                args = call.get("arguments", {})
                if name and isinstance(args, dict):
                    preamble = text[:idx].strip()
                    return {
                        "role": "assistant",
                        "content": preamble or None,
                        "tool_calls": [
                            {"id": "claude-code-1", "name": name, "arguments": args}
                        ],
                    }
                break
        return {"role": "assistant", "content": text}

    # ------------------------------------------------------------- openai
    def _complete_openai(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [self._to_openai(m) for m in messages],
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
                for t in tools
            ]
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        data = self._post(url, payload, headers)
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected OpenAI-style response: {json.dumps(data)[:400]}") from exc
        result: Message = {"role": "assistant", "content": choice.get("content")}
        calls: list[dict[str, Any]] = []
        for call in choice.get("tool_calls") or []:
            fn = call.get("function", {})
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": raw_args}
            calls.append({"id": call.get("id", ""), "name": fn.get("name", ""), "arguments": args})
        if calls:
            result["tool_calls"] = calls
        return result

    @staticmethod
    def _to_openai(message: Message) -> dict[str, Any]:
        role = message["role"]
        if role == "assistant" and message.get("tool_calls"):
            return {
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c.get("arguments", {})),
                        },
                    }
                    for c in message["tool_calls"]
                ],
            }
        if role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.get("tool_call_id", ""),
                "content": message.get("content", ""),
            }
        return {"role": role, "content": message.get("content", "")}

    # ---------------------------------------------------------- anthropic
    def _complete_anthropic(self, messages: list[Message], tools: list[ToolSpec]) -> Message:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        for message in messages:
            if message["role"] == "system":
                if message.get("content"):
                    system_parts.append(message["content"])
                continue
            converted.extend(self._to_anthropic(message))
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": 4096,
            "messages": converted,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
                }
                for t in tools
            ]
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self.settings.api_key:
            headers["x-api-key"] = self.settings.api_key
        url = self.settings.base_url.rstrip("/") + "/v1/messages"
        data = self._post(url, payload, headers)
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": block.get("input", {}) or {},
                    }
                )
        result: Message = {
            "role": "assistant",
            "content": "".join(text_parts) if text_parts else None,
        }
        if calls:
            result["tool_calls"] = calls
        return result

    @staticmethod
    def _to_anthropic(message: Message) -> list[dict[str, Any]]:
        role = message["role"]
        if role == "user":
            return [{"role": "user", "content": message.get("content", "")}]
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in message.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call.get("arguments", {}),
                    }
                )
            return [{"role": "assistant", "content": blocks or ""}]
        if role == "tool":
            return [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id", ""),
                            "content": message.get("content", ""),
                        }
                    ],
                }
            ]
        return []

    # -------------------------------------------------------------- http
    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"Request to {url} failed: {exc}") from exc
        if response.status_code >= 400:
            body = response.text[:600]
            raise LLMError(f"{url} returned HTTP {response.status_code}: {body}")
        try:
            return response.json()
        except ValueError as exc:
            raise LLMError(f"Non-JSON response from {url}: {response.text[:300]}") from exc
