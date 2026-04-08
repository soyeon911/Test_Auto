"""
LLM Client Factory — provider-agnostic wrapper.

Reads `agent.provider` from config and returns a unified client with
a single method:

    client.generate(system_prompt: str, user_prompt: str) -> str

Supported providers
-------------------
  gemini     → google-generativeai   (pip install google-generativeai)
  anthropic  → anthropic             (pip install anthropic)
  openai     → openai                (pip install openai)

The required API-key environment variable is declared in config:
  agent.api_key_env: GEMINI_API_KEY   # (or ANTHROPIC_API_KEY, OPENAI_API_KEY …)
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any


# ─── Abstract base ─────────────────────────────────────────────────────────────

class BaseLLMClient(ABC):
    """Minimal interface all provider clients must satisfy."""

    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Return generated text given system + user prompts."""


# ─── Gemini ───────────────────────────────────────────────────────────────────

class GeminiClient(BaseLLMClient):
    def __init__(self, model: str, max_tokens: int, api_key: str):
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError(
                "google-generativeai is not installed.\n"
                "Run: pip install google-generativeai"
            )
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(
            model_name=model,
            generation_config={"max_output_tokens": max_tokens},
        )

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        # Gemini fuses system + user into a single prompt
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        response = self._model.generate_content(full_prompt)
        return response.text.strip()


# ─── Anthropic ────────────────────────────────────────────────────────────────

class AnthropicClient(BaseLLMClient):
    def __init__(self, model: str, max_tokens: int, api_key: str):
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "anthropic is not installed.\n"
                "Run: pip install anthropic"
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text.strip()


# ─── OpenAI ───────────────────────────────────────────────────────────────────

class OpenAIClient(BaseLLMClient):
    def __init__(self, model: str, max_tokens: int, api_key: str):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai is not installed.\n"
                "Run: pip install openai"
            )
        self._client = OpenAI(api_key=api_key)  # type: ignore[arg-type]
        self._model = model
        self._max_tokens = max_tokens

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return (response.choices[0].message.content or "").strip()


# ─── Factory ──────────────────────────────────────────────────────────────────

_PROVIDERS: dict[str, type[BaseLLMClient]] = {
    "gemini": GeminiClient,
    "anthropic": AnthropicClient,
    "openai": OpenAIClient,
}


def create_llm_client(config: dict) -> BaseLLMClient:
    """
    Build and return the right LLM client from config.

    Reads:
      config.agent.provider    → which SDK to use
      config.agent.model       → model name / ID
      config.agent.max_tokens  → generation cap
      config.agent.api_key_env → env-var name that holds the API key
    """
    agent_cfg: dict[str, Any] = config.get("agent", {})
    provider = agent_cfg.get("provider", "gemini").lower()
    model = agent_cfg.get("model", "gemini-2.0-flash")
    max_tokens = int(agent_cfg.get("max_tokens", 4096))
    key_env = agent_cfg.get("api_key_env", _default_key_env(provider))

    api_key = os.environ.get(key_env, "")
    if not api_key:
        raise EnvironmentError(
            f"API key env var '{key_env}' is not set.\n"
            f"Run: set {key_env}=<your-key>  (Windows) "
            f"or  export {key_env}=<your-key>  (Linux/macOS)"
        )

    cls = _PROVIDERS.get(provider)
    if cls is None:
        raise ValueError(
            f"Unknown provider '{provider}'. "
            f"Supported: {list(_PROVIDERS)}"
        )

    print(f"[LLMClient] Using provider={provider}, model={model}")
    return cls(model=model, max_tokens=max_tokens, api_key=api_key)


def _default_key_env(provider: str) -> str:
    return {
        "gemini": "GEMINI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(provider, f"{provider.upper()}_API_KEY")
