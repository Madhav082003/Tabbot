"""The chat loop: send -> run requested tools locally -> send results back -> ... until Claude answers.

Rules (CLAUDE.md data boundary, Claude API guidance, spend safety):
- before every request: spend limits are checked (per question, per day, calls per day, rounds per question);
  a request that could pass a limit is NOT sent and the reason is shown and logged;
- every request is written to the audit log BEFORE it is sent; dry-run writes and never sends;
- every response's token usage and estimated cost go to the usage log;
- loop guard: the same tool call (same name and input) repeated in one question is not re-run; a third
  repeat stops the question;
- history is append-only: each assistant turn is appended exactly as returned (thinking blocks included);
- all tool results of one turn go back in ONE user message, failures as is_error results;
- refusals and token limits are reported, never hidden.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from tabbot.config import Settings
from tabbot.llm.audit import AuditLog
from tabbot.llm.chatlog import ChatLog, data_status
from tabbot.llm.prompts import SYSTEM_PROMPT
from tabbot.llm.provider import MessagesClient, build_request, make_messages_client
from tabbot.llm.tools import ToolContext, UIAction, dispatch, tool_definitions
from tabbot.llm.usage import TOKEN_KEYS, Limits, UsageLog, check_before_send, cost_usd

REPEAT_WARN = 2   # 2nd identical call: not re-run, Claude is told to use the earlier result
REPEAT_STOP = 3   # 3rd identical call: the question is stopped


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
    est_usd: float = 0.0
    error: str | None = None
    blocked: str | None = None          # why a request was not sent (spend limit / loop guard)
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
        self.session_id = uuid.uuid4().hex[:10]
        self.tools_ctx = ToolContext(lw=lw, max_rows=settings.privacy.max_rows, max_chars=settings.privacy.max_chars)
        self.tools = tool_definitions()
        self.messages: list[dict] = []
        audit_dir = settings.workspace_dir / "audit"
        self.audit = AuditLog(audit_dir / "outbound.jsonl")
        self.usage = UsageLog(audit_dir / "usage.jsonl")
        self.chatlog = ChatLog(settings.workspace_dir / "logs", self.session_id, getattr(lw, "source_name", "?"))
        self.limits = Limits(max_usd_per_question=settings.max_usd_per_question,
                             max_usd_per_day=settings.max_usd_per_day,
                             max_calls_per_day=settings.max_calls_per_day,
                             max_rounds_per_question=settings.max_tool_rounds)
        self._client = client
        self._last_request_chars = 0

    @property
    def client(self) -> MessagesClient:
        if self._client is None:
            self._client = make_messages_client(self.settings.anthropic_api_key)
        return self._client

    def _request(self) -> dict:
        return build_request(model=self.settings.claude_model, system=SYSTEM_PROMPT, tools=self.tools,
                             messages=self.messages, effort=self.settings.claude_effort,
                             max_tokens=self.settings.max_output_tokens, use_fallback=self.settings.use_fallback)

    def _log(self, event: str, question: str, rnd: int, **kw) -> None:
        self.usage.append({"event": event, "session": self.session_id, "question": question, "round": rnd,
                           "model": self.settings.claude_model, **kw})

    def ask(self, text: str, app_context: str | None = None) -> Turn:
        qid = uuid.uuid4().hex[:8]
        self.chatlog.question(qid, text, app_context, data_status(self.lw), self.settings.claude_model,
                              self.settings.dry_run)
        turn = self._ask(qid, text, app_context)
        self.chatlog.answer(qid, turn.text, turn.rounds, turn.usage, turn.est_usd,
                            stopped=turn.blocked or turn.error or (turn.stop_reason if turn.stop_reason
                                                                   in ("refusal", "max_tokens") else None))
        return turn

    def _ask(self, qid: str, text: str, app_context: str | None) -> Turn:
        content: list[dict] = []
        if app_context:
            content.append({"type": "text", "text": f"<app_context>{app_context}</app_context>"})
        content.append({"type": "text", "text": text})
        self.messages.append({"role": "user", "content": content})
        turn = Turn(text="")
        self.tools_ctx.actions = []
        seen: Counter[str] = Counter()
        rnd = 0
        cached_chars = self._last_request_chars   # prefix of the conversation already in the prompt cache

        while True:
            req = self._request()
            req_chars = len(json.dumps(req, default=str))
            reason = check_before_send(self.usage, self.limits, model=req["model"], request_chars=req_chars,
                                       max_tokens=req["max_tokens"], question_usd=turn.est_usd,
                                       question_rounds=rnd, cached_chars=cached_chars)
            if reason:
                self._log("blocked", qid, rnd + 1, reason=reason)
                turn.blocked = reason
                turn.text = (("Stopped to protect your credit before sending another request: " + reason + ".")
                             + ("\n\nPartial work so far is above; ask a narrower question to continue."
                                if rnd else ""))
                if rnd == 0:
                    self.messages.pop()   # nothing was sent for this question
                turn.actions = list(self.tools_ctx.actions)
                return turn
            rnd += 1
            turn.rounds = rnd
            self.audit.write(req, dry_run=self.settings.dry_run)          # BEFORE sending
            self.chatlog.sent(qid, rnd, req_chars, len(req["messages"]))
            if self.settings.dry_run:
                turn.dry_run = True
                turn.text = ("Dry run: the request was written to the audit log and not sent. "
                             "See workspace/logs/ (chat log) and workspace/audit/outbound.jsonl (full request).")
                self.messages.pop()  # keep history valid for the next real request
                return turn
            try:
                resp = self.client.create(**req)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user, history stays consistent
                turn.error = _friendly_error(exc)
                turn.text = turn.error
                self._log("error", qid, rnd, reason=f"{type(exc).__name__}: {str(exc)[:200]}")
                if self.messages and self.messages[-1]["role"] == "user" and rnd == 1:
                    self.messages.pop()
                return turn

            cached_chars = self._last_request_chars = req_chars
            blocks = [_block_dict(b) for b in (_get(resp, "content") or [])]
            self.messages.append({"role": "assistant", "content": blocks})   # append-only, as returned
            stop = _get(resp, "stop_reason")
            turn.stop_reason = stop
            u = _get(resp, "usage")
            call_usage = {k: int((_get(u, k) if u is not None else 0) or 0) for k in TOKEN_KEYS}
            served = _get(resp, "model") or req["model"]
            call_usd = cost_usd(served, call_usage)
            for k, v in call_usage.items():
                turn.usage[k] = turn.usage.get(k, 0) + v
            turn.est_usd = round(turn.est_usd + call_usd, 6)
            self._log("call", qid, rnd, served_by=served, stop_reason=stop, est_usd=call_usd, **call_usage)
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
            self.chatlog.received(qid, rnd, texts, [b for b in blocks if b.get("type") == "tool_use"], stop,
                                  call_usage, call_usd)

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

            results, stop_loop = [], False
            for tu in tool_uses:   # tools are local and fast; run in order, return all results together
                key = tu["name"] + json.dumps(tu.get("input") or {}, sort_keys=True, default=str)
                seen[key] += 1
                if seen[key] >= REPEAT_STOP:
                    stop_loop = True
                if seen[key] >= REPEAT_WARN:
                    out, is_err = json.dumps({"error": "this exact call was already made in this question; use "
                                                       "its earlier result instead of repeating it"}), True
                else:
                    out, is_err = dispatch(self.tools_ctx, tu["name"], tu.get("input"))
                self.chatlog.tool_result(qid, rnd, tu["name"], tu.get("input") or {}, out, is_err)
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out,
                                **({"is_error": True} if is_err else {})})
                turn.tool_calls.append(ToolCall(name=tu["name"], input=tu.get("input") or {}, is_error=is_err,
                                                preview=_preview(out, is_err)))
            self.messages.append({"role": "user", "content": results})   # keeps history valid
            if stop_loop:
                reason = "Claude repeated the same tool call 3 times (loop guard)"
                self._log("blocked", qid, rnd + 1, reason=reason)
                turn.blocked = reason
                turn.text = ("\n\n".join(t for t in texts if t.strip()) + "\n\n" if texts else "") + \
                    f"Stopped: {reason}. Try asking more specifically."
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
