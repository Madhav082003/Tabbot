"""Token usage log and spend limits for every Claude API call.

workspace/audit/usage.jsonl - one line per event:
  {"ts", "date", "event": "call" | "blocked" | "error", "session", "question", "round", "model", "served_by",
   "stop_reason", "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
   "est_usd", "reason"}

Limits (from .env, all checked BEFORE a request is sent):
  LLM_MAX_USD_PER_QUESTION   cost of all rounds of one question
  LLM_MAX_USD_PER_DAY        cost of all calls today (this machine, all sessions)
  LLM_MAX_CALLS_PER_DAY      number of API calls today
  MAX_TOOL_ROUNDS            API calls per question
Costs are estimates from config/pricing.yaml; the console bill is the source of truth.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from tabbot.config import PROJECT_ROOT

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


@lru_cache(maxsize=1)
def _pricing() -> dict:
    p = PROJECT_ROOT / "config" / "pricing.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {"models": data.get("models", {}),
            "unknown": data.get("unknown", {"input": 10, "output": 50, "cache_read": 1, "cache_write": 12.5})}


def price_for(model: str | None) -> dict:
    pr = _pricing()
    return pr["models"].get(model or "", pr["unknown"])


def cost_usd(model: str | None, usage: dict) -> float:
    p = price_for(model)
    return round((usage.get("input_tokens", 0) * p["input"]
                  + usage.get("output_tokens", 0) * p["output"]
                  + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
                  + usage.get("cache_creation_input_tokens", 0) * p["cache_write"]) / 1_000_000, 6)


def estimate_request_usd(model: str, request_chars: int, max_tokens: int) -> float:
    """Pessimistic cost of one request before it is sent: whole prompt uncached + full max_tokens output."""
    p = price_for(model)
    return round((request_chars / 3.5 * p["input"] + max_tokens * p["output"]) / 1_000_000, 6)


@dataclass
class Limits:
    max_usd_per_question: float = 0.50
    max_usd_per_day: float = 5.00
    max_calls_per_day: int = 200
    max_rounds_per_question: int = 8


class UsageLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: dict) -> dict:
        now = dt.datetime.now()
        entry = {"ts": now.isoformat(timespec="seconds"), "date": now.date().isoformat(), **entry}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def totals(self, date: str | None = None, session: str | None = None, question: str | None = None) -> dict:
        t = {k: 0 for k in TOKEN_KEYS} | {"calls": 0, "blocked": 0, "errors": 0, "est_usd": 0.0}
        for e in self.entries():
            if date and e.get("date") != date:
                continue
            if session and e.get("session") != session:
                continue
            if question and e.get("question") != question:
                continue
            if e.get("event") == "call":
                t["calls"] += 1
                t["est_usd"] += float(e.get("est_usd") or 0)
                for k in TOKEN_KEYS:
                    t[k] += int(e.get(k) or 0)
            elif e.get("event") == "blocked":
                t["blocked"] += 1
            elif e.get("event") == "error":
                t["errors"] += 1
        t["est_usd"] = round(t["est_usd"], 4)
        return t

    def by_day(self) -> list[dict]:
        days = sorted({e.get("date") for e in self.entries() if e.get("date")})
        return [{"date": d, **self.totals(date=d)} for d in days]

    def today(self) -> dict:
        return self.totals(date=dt.date.today().isoformat())


def check_before_send(log: UsageLog, limits: Limits, *, model: str, request_chars: int, max_tokens: int,
                      question_usd: float, question_rounds: int) -> str | None:
    """Return a reason to block the request, or None if it may be sent."""
    if question_rounds >= limits.max_rounds_per_question:
        return (f"this question already used {question_rounds} API calls (limit MAX_TOOL_ROUNDS="
                f"{limits.max_rounds_per_question})")
    nxt = estimate_request_usd(model, request_chars, max_tokens)
    if question_usd + nxt > limits.max_usd_per_question:
        return (f"this question has cost about ${question_usd:.3f}; the next call could cost up to ${nxt:.3f}, "
                f"which would pass the per-question limit of ${limits.max_usd_per_question:.2f} "
                "(LLM_MAX_USD_PER_QUESTION)")
    today = log.today()
    if today["calls"] >= limits.max_calls_per_day:
        return f"today's limit of {limits.max_calls_per_day} API calls is reached (LLM_MAX_CALLS_PER_DAY)"
    if today["est_usd"] + nxt > limits.max_usd_per_day:
        return (f"today's estimated spend is ${today['est_usd']:.2f}; the next call could pass the daily limit of "
                f"${limits.max_usd_per_day:.2f} (LLM_MAX_USD_PER_DAY)")
    return None
