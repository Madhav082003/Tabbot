"""The chat loop: send -> run requested tools locally -> send results back -> ... until Claude answers.

Rules (CLAUDE.md data boundary, Claude API guidance):
- every request is written to the audit log BEFORE it is sent; dry-run writes and never sends;
- history is append-only: each assistant turn is appended exactly as returned (thinking blocks included);
- all tool results of one turn go back in ONE user message, failures as is_error results;
- refusals and token limits are reported, never hidden.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from tabbot.config import Settings
from tabbot.llm.audit import AuditLog
from tabbot.llm.prompts import SYSTEM_PROMPT
from tabbot.llm.provider import MessagesClient, build_request, make_messages_client
from tabbot.llm.tools import ToolContext, UIAction, dispatch, tool_definitions


@dataclass
class ToolCall:
    name: str
    input: dict
    is_error: bool
    preview: str          # short text for the UI


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    actions: list[UIAction] = field(default_factory=list)
    stop_reason: str | None = None
    rounds: int = 0
    usage: dict = field(default_factory=dict)
    error: str | None = None
    dry_run: bool = False


def _block_dict(b: Any) -> dict:
    if isinstance(b, dict):
        return b
    if hasattr(b, "to_dict"):
        return b.to_dict()
    return b.model_dump(mode="json", exclude_none=True)


def _get(o: Any, k: str, default=None):
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


class ChatSession:
    def __init__(self, lw, settings: Settings, client: MessagesClient | None = None):
        self.lw = lw
        self.settings = settings
        self.tools_ctx = ToolContext(lw=lw, max_rows=settings.privacy.max_rows, max_chars=settings.privacy.max_chars)
        self.tools = tool_definitions()
        self.messages: list[dict] = []
        self.audit = AuditLog(settings.workspace_dir / "audit" / "outbound.jsonl")
        self._client = client

    @property
    def client(self) -> MessagesClient:
        if self._client is None:
            self._client = make_messages_client(self.settings.anthropic_api_key)
        return self._client

    def _request(self) -> dict:
        return build_request(model=self.settings.claude_model, system=SYSTEM_PROMPT, tools=self.tools,
                             messages=self.messages, effort=self.settings.claude_effort,
                             use_fallback=self.settings.use_fallback)

    def ask(self, text: str, app_context: str | None = None) -> Turn:
        content: list[dict] = []
        if app_context:
            content.append({"type": "text", "text": f"<app_context>{app_context}</app_context>"})
        content.append({"type": "text", "text": text})
        self.messages.append({"role": "user", "content": content})
        turn = Turn(text="")
        self.tools_ctx.actions = []

        for rnd in range(1, self.settings.max_tool_rounds + 1):
            turn.rounds = rnd
            req = self._request()
            self.audit.write(req, dry_run=self.settings.dry_run)          # BEFORE sending
            if self.settings.dry_run:
                turn.dry_run = True
                turn.text = ("Dry run: the request was written to the audit log and not sent. "
                             "Open 'What was sent to Claude' to inspect it.")
                self.messages.pop()  # keep history valid for the next real request
                return turn
            try:
                resp = self.client.create(**req)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user, history stays consistent
                turn.error = _friendly_error(exc)
                turn.text = turn.error
                if self.messages and self.messages[-1]["role"] == "user" and rnd == 1:
                    self.messages.pop()
                return turn

            blocks = [_block_dict(b) for b in (_get(resp, "content") or [])]
            self.messages.append({"role": "assistant", "content": blocks})   # append-only, as returned
            stop = _get(resp, "stop_reason")
            turn.stop_reason = stop
            u = _get(resp, "usage")
            if u is not None:
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    turn.usage[k] = turn.usage.get(k, 0) + (_get(u, k) or 0)
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]

            if stop == "refusal":
                turn.text = ("Claude declined this request (safety classifier). Try rephrasing the question."
                             + ("\n\n" + "\n".join(texts) if texts else ""))
                return turn
            if stop == "pause_turn":
                continue
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            if stop != "tool_use" or not tool_uses:
                turn.text = "\n\n".join(t for t in texts if t.strip())
                if stop == "max_tokens":
                    turn.text += "\n\n_(answer cut off at the length limit)_"
                turn.actions = list(self.tools_ctx.actions)
                return turn

            results = []
            for tu in tool_uses:   # tools are local and fast; run in order, return all results together
                out, is_err = dispatch(self.tools_ctx, tu["name"], tu.get("input"))
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out,
                                **({"is_error": True} if is_err else {})})
                turn.tool_calls.append(ToolCall(name=tu["name"], input=tu.get("input") or {}, is_error=is_err,
                                                preview=_preview(out, is_err)))
            self.messages.append({"role": "user", "content": results})

        turn.text = "Stopped after the maximum number of tool rounds without a final answer."
        turn.actions = list(self.tools_ctx.actions)
        return turn


def _preview(out: str, is_err: bool) -> str:
    try:
        d = json.loads(out)
    except ValueError:
        return out[:80]
    if is_err:
        return f"error: {d.get('error', '')}"[:120]
    bits = []
    if "result_id" in d:
        bits.append(d["result_id"])
    if "row_count" in d:
        bits.append(f"{d['row_count']} rows")
    if "shown" in d:
        bits.append(f"showing {d['shown']}")
    return ", ".join(bits)


def _friendly_error(exc: Exception) -> str:
    name = type(exc).__name__
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return f"Request failed ({name})."
    if isinstance(exc, anthropic.AuthenticationError):
        return "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in C:\\tabbot\\.env and restart the app."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "This API key is not allowed to use the selected model. Check the key's workspace or the model."
    if isinstance(exc, anthropic.NotFoundError):
        return "Model not found. Check CLAUDE_MODEL in C:\\tabbot\\.env."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limit or credit limit reached. Wait a minute, or check the account's credit balance."
    if isinstance(exc, anthropic.BadRequestError):
        return f"The request was rejected: {getattr(exc, 'message', str(exc))[:300]}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude API error {exc.status_code}. Try again in a moment."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the Claude API. Check the internet connection."
    if "api_key" in str(exc).lower() or "auth" in str(exc).lower():
        return "No Anthropic API key found. Put ANTHROPIC_API_KEY=... in C:\\tabbot\\.env and restart the app."
    return f"Request failed ({name}): {str(exc)[:200]}"
