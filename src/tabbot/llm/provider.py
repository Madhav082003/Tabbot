"""The only module that talks to the network: the Anthropic API client.

Requests use the beta Messages endpoint so the server-side refusal fallback can be enabled
(`fallbacks="default"`, beta `server-side-fallback-2026-07-01`): if a safety classifier declines a request,
the API re-runs it on Anthropic's recommended fallback model instead of returning a refusal.
"""

from __future__ import annotations

from typing import Any, Protocol

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class MessagesClient(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class LLMNotConfigured(RuntimeError):
    """No API key / credentials available."""


def make_messages_client(api_key: str | None, timeout: float = 120.0, max_retries: int = 3) -> MessagesClient:
    """Return an object with `.create(**request)` that sends one Messages API request."""
    import anthropic  # imported here so the rest of the app never needs it

    client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=max_retries) if api_key \
        else anthropic.Anthropic(timeout=timeout, max_retries=max_retries)
    return client.beta.messages


def build_request(*, model: str, system: str, tools: list[dict], messages: list[dict], effort: str,
                  max_tokens: int = 16000, use_fallback: bool = True) -> dict:
    """The exact request body (what gets audited is exactly what gets sent)."""
    req: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "tools": tools,
        "messages": messages,
        "output_config": {"effort": effort},
        # automatic prompt caching: caches the growing prefix (tools + system + history) across tool rounds
        "cache_control": {"type": "ephemeral"},
    }
    if use_fallback:
        req["betas"] = [FALLBACK_BETA]
        req["fallbacks"] = "default"
    return req
