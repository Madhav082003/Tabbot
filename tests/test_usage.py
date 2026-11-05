"""Spend safety: usage logging, limits checked before sending, loop guard, no real key in tests."""

import datetime as dt
import json

import pytest

from tabbot.cli import main
from tabbot.config import load_settings
from tabbot.llm.orchestrator import ChatSession
from tabbot.llm.usage import UsageLog, cost_usd, estimate_request_usd
from tabbot.loader import load_workbook


@pytest.fixture(scope="module")
def lw(fixture_twbx, tmp_path_factory):
    settings = load_settings().model_copy(update={"workspace_dir": tmp_path_factory.mktemp("ws")})
    loaded = load_workbook(fixture_twbx, settings=settings)
    yield loaded
    loaded.close()


def settings_for(tmp_path, **kw):
    return load_settings().model_copy(update={"workspace_dir": tmp_path, "anthropic_api_key": "test", **kw})


def resp(blocks, stop, inp=1000, out=200, cached=0):
    return {"content": blocks, "stop_reason": stop, "model": "claude-opus-5-5",
            "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cached,
                      "cache_creation_input_tokens": 0}}


class Scripted:
    def __init__(self, items):
        self.items, self.calls = list(items), 0

    def create(self, **_):
        self.calls += 1
        return self.items.pop(0)


class Endless:
    """A misbehaving model: always asks for another (different) tool call."""

    def __init__(self):
        self.calls = 0

    def create(self, **_):
        self.calls += 1
        return resp([{"type": "tool_use", "id": f"t{self.calls}", "name": "describe_sheet",
                      "input": {"sheet": f"Sheet {self.calls}"}}], "tool_use")


class Never:
    calls = 0

    def create(self, **_):
        raise AssertionError("must not be called")


def usage_lines(tmp_path):
    p = tmp_path / "audit" / "usage.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_cost_math():
    # Claude Opus 5.5: $4 in, $20 out, $0.20 cache read per million tokens
    u = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 500_000}
    assert cost_usd("claude-opus-5-5", u) == pytest.approx(4 + 2 + 0.10)
    assert cost_usd("some-future-model", {"input_tokens": 1_000_000}) == pytest.approx(10)  # unknown = expensive
    assert estimate_request_usd("claude-opus-5-5", 35_000, 8000) == pytest.approx((10_000 * 4 + 8000 * 20) / 1e6)


def test_every_call_is_logged_with_tokens_and_cost(lw, tmp_path):
    client = Scripted([
        resp([{"type": "tool_use", "id": "a", "name": "list_dashboards", "input": {}}], "tool_use", 2000, 100),
        resp([{"type": "text", "text": "Done (R1)."}], "end_turn", 300, 150, cached=1800),
    ])
    chat = ChatSession(lw, settings_for(tmp_path), client=client)
    turn = chat.ask("hi")
    lines = usage_lines(tmp_path)
    assert [e["event"] for e in lines] == ["call", "call"] and client.calls == 2
    assert lines[0]["input_tokens"] == 2000 and lines[1]["cache_read_input_tokens"] == 1800
    assert all(e["session"] == chat.session_id and e["question"] == lines[0]["question"] for e in lines)
    assert turn.est_usd == pytest.approx(sum(e["est_usd"] for e in lines))
    assert UsageLog(tmp_path / "audit" / "usage.jsonl").today()["calls"] == 2


def test_per_question_limit_blocks_before_sending(lw, tmp_path):
    chat = ChatSession(lw, settings_for(tmp_path, max_usd_per_question=0.001), client=Never())
    turn = chat.ask("hi")
    assert turn.blocked and "per-question limit" in turn.text and chat.messages == []
    assert [e["event"] for e in usage_lines(tmp_path)] == ["blocked"]
    assert not (tmp_path / "audit" / "outbound.jsonl").exists()  # nothing was even prepared for sending


def test_daily_limits_block(lw, tmp_path):
    log = UsageLog(tmp_path / "audit" / "usage.jsonl")
    log.append({"event": "call", "est_usd": 4.99, "input_tokens": 1})
    chat = ChatSession(lw, settings_for(tmp_path, max_usd_per_day=5.0), client=Never())
    assert "daily limit" in chat.ask("hi").text

    log2 = UsageLog(tmp_path / "b" / "audit" / "usage.jsonl")
    for _ in range(3):
        log2.append({"event": "call", "est_usd": 0.0})
    chat2 = ChatSession(lw, settings_for(tmp_path / "b", max_calls_per_day=3), client=Never())
    assert "API calls is reached" in chat2.ask("hi").text


def test_yesterday_does_not_count_today(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl")
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    with log.path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"date": yesterday, "event": "call", "est_usd": 9.0}) + "\n")
    assert log.today()["est_usd"] == 0 and log.totals()["est_usd"] == 9.0


def test_runaway_model_is_stopped_by_round_limit(lw, tmp_path):
    client = Endless()
    chat = ChatSession(lw, settings_for(tmp_path, max_tool_rounds=3), client=client)
    turn = chat.ask("hi")
    assert client.calls == 3 and turn.blocked and "MAX_TOOL_ROUNDS" in turn.text
    assert [e["event"] for e in usage_lines(tmp_path)] == ["call", "call", "call", "blocked"]
    assert chat.messages[-1]["role"] == "user"  # history stays valid (ends with the tool results)


def test_loop_guard_stops_identical_repeats(lw, tmp_path):
    same = {"type": "tool_use", "name": "describe_sheet", "input": {"sheet": "Gender Pie"}}
    client = Scripted([resp([{**same, "id": f"x{i}"}], "tool_use") for i in range(5)])
    chat = ChatSession(lw, settings_for(tmp_path), client=client)
    turn = chat.ask("hi")
    assert client.calls == 3 and "loop guard" in turn.text
    assert [t.is_error for t in turn.tool_calls] == [False, True, True]  # repeats are not re-run


def test_tests_never_see_a_real_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-ant-real-looking-key\n", encoding="utf-8")
    assert load_settings(env_file=env).anthropic_api_key is None


def test_cli_usage(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from tabbot import config

    config.get_settings.cache_clear()
    assert main(["usage"]) == 0 and "No Claude API calls" in capsys.readouterr().out
    UsageLog(tmp_path / "audit" / "usage.jsonl").append({"event": "call", "est_usd": 0.0123, "input_tokens": 500,
                                                          "output_tokens": 50, "question": "q1", "round": 1})
    assert main(["usage"]) == 0
    out = capsys.readouterr().out
    assert "0.012" in out and "total" in out
    config.get_settings.cache_clear()
