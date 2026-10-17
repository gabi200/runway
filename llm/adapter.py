"""OpenAI-compatible LLM adapter (DeepSeek by default; Gemini via Vertex; local models via Ollama)."""

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
        if self.config.get("project_env"):
            self.base_url = self.base_url.format(project=os.environ.get(self.config["project_env"], "").strip())
        self.api_key = os.environ.get(self.config.get("api_key_env", ""), "") or self.config.get("api_key", "")
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            header = self.config.get("api_key_header")
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=float(self.config.get("timeout_seconds", 60)),
                max_retries=int(self.config.get("max_retries", 1)),  # SDK default 2 triples the wait before the fallback
                default_headers={header: self.api_key} if header else None,
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
        if self.config.get("api_key_header"):
            from openai import Omit

            kwargs["extra_headers"] = {"Authorization": Omit()}  # the key goes only in api_key_header
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
    if selected in ("gemini", "ollama"):
        cfg = load_yaml(f"{selected}.yaml")
    elif selected not in {"offline", cfg["provider"]}:
        raise ValueError(f"Unknown LLM mode: {selected}")
    required = [cfg[k] for k in ("api_key_env", "project_env") if cfg.get(k)]  # Ollama needs neither
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if selected != "offline" and missing:
        if mode or os.environ.get("RUNWAY_LLM"):
            raise ValueError(f"Set {' and '.join(missing)} in .env and restart Runway to use {selected}.")
        selected = "offline"
    if selected == "offline":
        from llm.offline import OfflinePlanner

        return OfflinePlanner()
    return LLMAdapter(cfg)
