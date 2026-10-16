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
            call = {"id": tc.id, "name": tc.function.name, "arguments": args}
            if getattr(tc, "extra_content", None):
                call["extra_content"] = tc.extra_content
            calls.append(call)
        return {"content": msg.content, "tool_calls": calls}


def get_adapter(mode: str | None = None) -> "LLMAdapter | Any":
    """Select a provider per session or from RUNWAY_LLM; unconfigured defaults run offline."""
    cfg = load_yaml("llm.yaml")
    selected = (mode or os.environ.get("RUNWAY_LLM") or cfg["provider"]).lower()
    if selected == "gemini":
        cfg = load_yaml("gemini.yaml")
    elif selected not in {"offline", cfg["provider"]}:
        raise ValueError(f"Unknown LLM mode: {selected}")
    if selected != "offline" and not os.environ.get(cfg["api_key_env"], "").strip():
        if mode or os.environ.get("RUNWAY_LLM"):
            raise ValueError(f"Set {cfg['api_key_env']} in .env and restart Runway to use {selected}.")
        selected = "offline"
    if selected == "offline":
        from llm.offline import OfflinePlanner

        return OfflinePlanner()
    return LLMAdapter(cfg)
