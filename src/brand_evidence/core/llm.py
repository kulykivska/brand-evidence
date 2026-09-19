"""One small client for whichever model the operator chose.

Classification is advisory and optional (§2), so this never raises into the
caller's path and never touches the evidence log. It exists so the choice of
model is configuration rather than a rewrite: a hosted API, a gateway, or a
model on the same machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from brand_evidence.core.logging import get_logger
from brand_evidence.sources.base import MAX_RESPONSE_BYTES, user_agent

log = get_logger(__name__)

# Where the common services live, so a provider name is enough. Anything else
# is reachable with BE_LLM_BASE_URL.
PRESETS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "mistral": "https://api.mistral.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "ollama": "http://localhost:11434",
    "chat": "",
}

# Providers that need no key: a model running on this machine.
KEYLESS = frozenset({"ollama"})

ANTHROPIC_VERSION = "2023-06-01"


class LlmError(RuntimeError):
    """The model could not be reached, or answered with something unusable."""


@dataclass(frozen=True)
class LlmClient:
    """Ask one short question and get one short answer."""

    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""
    timeout: float = 30.0
    max_tokens: int = 64
    # A connection to reuse across a batch. Without one each ask opens its own.
    session: httpx.Client | None = None

    @property
    def url_root(self) -> str:
        root = self.base_url or PRESETS.get(self.provider, "")
        if not root:
            raise LlmError(f"provider {self.provider!r} needs BE_LLM_BASE_URL")
        return root.rstrip("/")

    def ask(self, prompt: str) -> str:
        if self.provider == "anthropic":
            return self._anthropic(prompt)
        if self.provider == "ollama":
            return self._ollama(prompt)
        return self._chat(prompt)

    def _post(
        self, path: str, payload: dict[str, object], headers: dict[str, str]
    ) -> dict[str, Any]:
        url = f"{self.url_root}{path}"
        if not url.startswith(("http://", "https://")):
            raise LlmError(f"refusing to POST to {url!r}: only http and https")
        request_headers = {"User-Agent": user_agent(), **headers}
        if self.session is not None:
            response = self.session.post(url, json=payload, headers=request_headers)
        else:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, json=payload, headers=request_headers)
        if response.status_code >= 400:
            raise LlmError(f"HTTP {response.status_code}: {response.text[:200]}")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise LlmError("the answer was larger than this tool will read")
        data: dict[str, Any] = response.json()
        return data

    def _anthropic(self, prompt: str) -> str:
        data = self._post(
            "/messages",
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION},
        )
        try:
            return "".join(
                str(b.get("text", "")) for b in data["content"] if b.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:
            raise LlmError(f"unexpected answer shape: {exc}") from None

    def _chat(self, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = self._post(
            "/chat/completions",
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers,
        )
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"unexpected answer shape: {exc}") from None
        if isinstance(content, list):
            return "".join(str(part.get("text", "")) for part in content)
        return str(content or "")

    def _ollama(self, prompt: str) -> str:
        data = self._post(
            "/api/chat",
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            {},
        )
        try:
            return str(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise LlmError(f"unexpected answer shape: {exc}") from None
