"""LLM chat client with tool calling for two provider families.

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
        self._http = httpx.Client(timeout=settings.request_timeout)

    # ------------------------------------------------------------- public
    def complete(self, messages: list[Message], tools: list[ToolSpec] | None = None) -> Message:
        if self.settings.provider == "anthropic":
            return self._complete_anthropic(messages, tools or [])
        return self._complete_openai(messages, tools or [])

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
