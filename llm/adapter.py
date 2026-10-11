"""OpenAI-compatible LLM adapter (DeepSeek by default; Qwen/OpenAI by editing config/llm.yaml)."""

from __future__ import annotations

import json
import os
from typing import Any

from common.config import load_yaml


class LLMAdapter:
    """Thin wrapper around the openai SDK. `chat` returns a provider-neutral dict."""

    def __init__(self, config: dict[str, Any] | None = None, client: Any = None) -> None:
        self.config = config or load_yaml("llm.yaml")
        self.model = os.environ.get("RUNWAY_LLM_MODEL") or self.config["model"]
        self.base_url = os.environ.get("RUNWAY_LLM_BASE_URL") or self.config["base_url"]
        self.api_key = os.environ.get(self.config["api_key_env"], "")
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=float(self.config.get("timeout_seconds", 60)),
            )
        return self._client

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        """Send messages (OpenAI format) and return {"content", "tool_calls"}."""
        kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=messages,
            temperature=float(self.config.get("temperature", 0.2)),
        )
        if tools:
            kwargs["tools"] = tools
        if self.config.get("provider") == "deepseek" and os.environ.get("RUNWAY_LLM_THINKING") != "on":
            # thinking mode needs reasoning_content echoed back across tool turns; keep it simple
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_invalid_json": tc.function.arguments}
            calls.append({"id": tc.id, "name": tc.function.name, "arguments": args})
        return {"content": msg.content, "tool_calls": calls}


def get_adapter() -> "LLMAdapter | Any":
    """Return the real adapter, or the OfflinePlanner without an API key / RUNWAY_LLM=offline."""
    cfg = load_yaml("llm.yaml")
    if os.environ.get("RUNWAY_LLM", "").lower() == "offline" or not os.environ.get(cfg["api_key_env"]):
        from llm.offline import OfflinePlanner

        return OfflinePlanner()
    return LLMAdapter(cfg)
