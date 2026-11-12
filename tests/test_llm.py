"""Claude layer without an API key: a fake client returns scripted responses."""

import json
import os

import pytest

from tabbot.config import load_settings
from tabbot.llm.orchestrator import ChatSession
from tabbot.llm.provider import FALLBACK_BETA
from tabbot.llm.tools import ToolContext, dispatch, tool_definitions
from tabbot.loader import load_workbook


@pytest.fixture(scope="module")
def lw(fixture_twbx, tmp_path_factory):
    settings = load_settings().model_copy(update={"workspace_dir": tmp_path_factory.mktemp("ws")})
    loaded = load_workbook(fixture_twbx, settings=settings)
    yield loaded
    loaded.close()


def settings_for(tmp_path, **kw):
    return load_settings().model_copy(update={"workspace_dir": tmp_path, "anthropic_api_key": "test", **kw})


def tool_use(i, name, inp):
    return {"type": "tool_use", "id": f"tu_{i}", "name": name, "input": inp}


def response(blocks, stop):
    return {"content": blocks, "stop_reason": stop,
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50}}


class FakeClient:
    """Checks the audit log already has an entry for every request at the moment it is 'sent'."""

    def __init__(self, script, audit_path):
        self.script, self.audit_path, self.requests = list(script), audit_path, []

    def create(self, **req):
        lines = self.audit_path.read_text(encoding="utf-8").splitlines() if self.audit_path.exists() else []
        assert len(lines) == len(self.requests) + 1, "request sent before it was written to the audit log"
        self.requests.append(json.loads(json.dumps(req, default=str)))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


SCRIPT = [
    response([{"type": "text", "text": "Let me look."},
              tool_use(1, "list_dashboards", {}),
              tool_use(2, "run_analysis", {"sheet": "Category Heatmap", "recipe": "standard"})], "tool_use"),
    response([tool_use(3, "describe_sheet", {"sheet": "Category by Quarter"}),   # a dashboard name
              tool_use(4, "get_chart_data", {"sheet": "No Such Sheet"})], "tool_use"),
    response([{"type": "text", "text": "**Data**: Category C grew 248 → 639 (R1)."}], "end_turn"),
]


def test_tool_loop(lw, tmp_path):
    s = settings_for(tmp_path)
    fake = FakeClient(SCRIPT, tmp_path / "audit" / "outbound.jsonl")
    chat = ChatSession(lw, s, client=fake)
    turn = chat.ask("Key insights?", app_context="Open page: Overview")

    assert turn.text.startswith("**Data**") and turn.stop_reason == "end_turn" and turn.rounds == 3
    assert [t.name for t in turn.tool_calls] == ["list_dashboards", "run_analysis", "describe_sheet",
                                                 "get_chart_data"]
    assert [t.is_error for t in turn.tool_calls] == [False, False, False, True]
    assert turn.usage["input_tokens"] == 300 and turn.usage["cache_read_input_tokens"] == 150

    roles = [m["role"] for m in chat.messages]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    first = chat.messages[0]["content"]
    assert first[0]["text"].startswith("<app_context>") and first[1]["text"] == "Key insights?"
    # all tool results of a turn in ONE user message, matching ids, errors flagged
    r1, r2 = chat.messages[2]["content"], chat.messages[4]["content"]
    assert [b["tool_use_id"] for b in r1] == ["tu_1", "tu_2"] and all(b["type"] == "tool_result" for b in r1)
    assert r2[1]["is_error"] is True and "no worksheet or dashboard" in r2[1]["content"]
    assert '"sheet": "Category Heatmap"' in r2[0]["content"]   # dashboard resolved to its sheet
    # assistant turns appended exactly as returned (append-only history)
    assert chat.messages[1]["content"] == SCRIPT[0]["content"]

    req = fake.requests[0]
    assert req["model"] == s.claude_model and req["output_config"] == {"effort": s.claude_effort}
    assert req["fallbacks"] == "default" and req["betas"] == [FALLBACK_BETA]
    assert req["cache_control"] == {"type": "ephemeral"}
    assert not {"temperature", "top_p", "top_k", "tool_choice", "thinking"} & set(req)
    assert {t["name"] for t in req["tools"]} == {t["name"] for t in tool_definitions()}
    # later requests carry the whole growing history (prefix stays identical for caching)
    assert fake.requests[2]["messages"][:5] == json.loads(json.dumps(chat.messages[:5], default=str))


def test_dry_run_never_sends(lw, tmp_path):
    s = settings_for(tmp_path, dry_run=True)

    class Boom:
        def create(self, **_):
            raise AssertionError("dry run must not send")

    chat = ChatSession(lw, s, client=Boom())
    turn = chat.ask("hello")
    assert turn.dry_run and "Dry run" in turn.text and chat.messages == []
    entry = json.loads((tmp_path / "audit" / "outbound.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["dry_run"] is True and entry["request"]["messages"][0]["content"][-1]["text"] == "hello"
    assert "test" not in json.dumps(entry)  # the API key is never logged


def test_errors_and_refusal(lw, tmp_path):
    s = settings_for(tmp_path)
    fake = FakeClient([RuntimeError("Could not resolve authentication method. Expected api_key")],
                      tmp_path / "audit" / "outbound.jsonl")
    chat = ChatSession(lw, s, client=fake)
    turn = chat.ask("hi")
    assert turn.error and "API key" in turn.text and chat.messages == []  # history stays valid

    fake2 = FakeClient([response([], "refusal")], tmp_path / "audit2" / "outbound.jsonl")
    s2 = s.model_copy(update={"workspace_dir": tmp_path / "w2"})
    fake2.audit_path = tmp_path / "w2" / "audit" / "outbound.jsonl"
    chat2 = ChatSession(lw, s2, client=fake2)
    assert "declined" in chat2.ask("hi").text


def _events(blocks):
    """The stream events the API sends for a reply made of these blocks (text split into small deltas)."""
    from types import SimpleNamespace as N
    out = []
    for b in blocks:
        out.append(N(type="content_block_start", content_block=N(type=b["type"])))
        if b["type"] == "text":
            t = b["text"]
            out += [N(type="content_block_delta", delta=N(type="text_delta", text=t[i:i + 4]))
                    for i in range(0, len(t), 4)]
        out.append(N(type="content_block_stop"))
    return out


class FakeStreamClient(FakeClient):
    """Like the real client.beta.messages: has .stream(); create() must not be used when streaming exists."""

    def create(self, **req):
        raise AssertionError("streaming client: create() should not be called")

    def stream(self, **req):
        resp = FakeClient.create(self, **req)   # audit check + scripted reply
        events = _events(resp["content"])

        class Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                return iter(events)

            def get_final_message(self):
                return resp
        return Ctx()


def test_streaming_sends_text_as_it_arrives(lw, tmp_path):
    s = settings_for(tmp_path)
    fake = FakeStreamClient(SCRIPT, tmp_path / "audit" / "outbound.jsonl")
    events = []
    turn = ChatSession(lw, s, client=fake).ask("Key insights?", on_event=lambda k, d: events.append((k, d)))

    final = SCRIPT[2]["content"][0]["text"]
    assert turn.text == final and turn.rounds == 3 and len(fake.requests) == 3
    kinds = [k for k, _ in events]
    # progress note of round 1, then the 4 tools announced before they run, then the answer in many pieces
    assert kinds[0] == "text" and "".join(d for k, d in events[:kinds.index("tool")]) == "Let me look."
    assert [d["name"] for k, d in events if k == "tool"] == ["list_dashboards", "run_analysis", "describe_sheet",
                                                              "get_chart_data"]
    last_tool = max(i for i, (k, _) in enumerate(events) if k == "tool")
    answer = [d for k, d in events[last_tool + 1:] if k == "text"]
    assert len(answer) > 3 and "".join(answer) == final


def test_streaming_display_errors_never_break_the_answer(lw, tmp_path):
    s = settings_for(tmp_path)

    def broken(kind, data):
        raise RuntimeError("display failed")
    turn = ChatSession(lw, s, client=FakeStreamClient(SCRIPT, tmp_path / "audit" / "outbound.jsonl")).ask(
        "Key insights?", on_event=broken)
    assert turn.text.startswith("**Data**") and turn.error is None


def test_mid_stream_fallback_history(lw, tmp_path):
    """After a mid-stream fallback, the declined part's thinking and tool calls are neither echoed nor run."""
    s = settings_for(tmp_path)
    reply = response([{"type": "thinking", "thinking": "", "signature": "a"},
                      {"type": "text", "text": "Part one. "},
                      tool_use(9, "get_findings", {}),
                      {"type": "fallback", "from": {"model": "x"}, "to": {"model": "y"}},
                      {"type": "text", "text": "Part two."}], "end_turn")
    chat = ChatSession(lw, s, client=FakeStreamClient([reply], tmp_path / "audit" / "outbound.jsonl"))
    turn = chat.ask("hi")
    kept = [b["type"] for b in chat.messages[-1]["content"]]
    assert kept == ["text", "fallback", "text"] and turn.tool_calls == []
    assert turn.text == "Part one. \n\nPart two."


def test_typewriter_paces_and_catches_up():
    from tabbot.ui.typewriter import BASE_CPS, Typewriter
    frames, waits = [], []
    tw = Typewriter(frames.append, waits.append)
    text = "The quick brown fox jumps over the lazy dog. " * 3
    tw.feed(text[:10])
    tw.feed(text[10:])
    assert frames[-1] == text and len(frames) > 5                       # shown in several steps, in order
    assert all(b.startswith(a) for a, b in zip(frames, frames[1:], strict=False))
    assert abs(sum(waits) - len(text) / BASE_CPS) < len(text) / BASE_CPS  # about the base pace, never slower
    # a big burst is typed faster per character than a trickle (the display never lags far behind)
    slow, fast = [], []
    Typewriter(lambda t: None, slow.append).feed("word " * 10)
    Typewriter(lambda t: None, fast.append).feed("word " * 400)
    assert sum(fast) / 2000 < sum(slow) / 50
    tw.reset()
    assert frames[-1] == "" and tw.shown == ""


def test_tool_definitions_are_valid():
    defs = tool_definitions()
    assert len({d["name"] for d in defs}) == len(defs) == 6
    assert "show_in_app" not in {d["name"] for d in defs}
    for d in defs:
        assert d["input_schema"]["type"] == "object" and len(d["description"].split(". ")) >= 2


SENSITIVE_MARKERS = ["R00001", "R00002", "#1-1", "jdoe", "Client Data", "C:/Users", "fixture_source.xlsx"]


def test_no_sensitive_values_leak(lw):
    ctx = ToolContext(lw=lw)
    calls = [("list_dashboards", {}), ("get_findings", {})]
    for ws in lw.model.worksheets:
        calls += [("describe_sheet", {"sheet": ws.name}), ("get_chart_data", {"sheet": ws.name}),
                  ("run_analysis", {"sheet": ws.name, "recipe": "standard"})]
    calls += [("query_data", {"dimensions": [{"field": f}]}) for f in ("NOTES", "RECORD_ID", "CHANNEL", "AGE_UNIT")]
    calls += [("query_data", {"filters": [{"field": "RECORD_ID", "values": ["R00001"]}]}),
              ("run_analysis", {"sheet": "Records Trend", "recipe": "flag_share", "flag_field": "NOTES",
                                "flag_value": "x"})]
    outputs = {f"{n} {i}": dispatch(ctx, n, i) for n, i in calls}
    for key, (out, _) in outputs.items():
        for marker in SENSITIVE_MARKERS:
            if marker == "R00001" and "query_data" in key and "R00001" in key:
                continue  # the request itself names it; the answer must be an error (checked below)
            assert marker not in out, (key, marker)
    assert outputs["query_data {'dimensions': [{'field': 'NOTES'}]}"][1] is True
    assert outputs["query_data {'dimensions': [{'field': 'RECORD_ID'}]}"][1] is True
    assert outputs["query_data {'filters': [{'field': 'RECORD_ID', 'values': ['R00001']}]}"][1] is True
    assert all(len(o) <= ctx.max_chars + 20 for o, _ in outputs.values())


def test_overview_is_compact_and_groups_patterns(lw):
    ctx = ToolContext(lw=lw)
    out, err = dispatch(ctx, "list_dashboards", {})
    d = json.loads(out)
    assert not err and len(out) < 6000 and "truncated" not in out
    ds = d["data_sources"][0]
    assert ds["record"]["field"] == "RECORD_ID" and "sensitive (values never shared)" in ds["fields"]
    assert any(c.get("unused") for c in ds["calculated_fields"])
    detail = next(x for x in d["dashboards"] if x["name"] == "Category Detail")
    # Category A / Category B share one shape (line, one-value CATEGORY filter): shown once each, not grouped (<3)
    assert {s.get("sheet") for s in detail["sheets"]} >= {"Category A", "Category B"}


def test_repeated_sheets_become_one_pattern():
    from tabbot.llm.tools import _sheets_summary
    from tabbot.twb.fieldref import parse_ref
    from tabbot.twb.model import Filter, Worksheet

    def pie(cat):
        return Worksheet(name=f"{cat} pie", title=cat, mark="Pie",
                         encodings={"color": [parse_ref("[d].[none:GENDER:nk]")],
                                    "size": [parse_ref("[d].[ctd:CASEID:qk]")]},
                         filters=[Filter(field=parse_ref("[d].[none:SOC:nk]"), kind="categorical", mode="include",
                                         members=[cat], member_count=1)])
    out = _sheets_summary([pie(c) for c in ("Cardiac", "Eye", "Skin", "Vascular")])
    assert len(out) == 1 and out[0]["repeated_sheets"] == 4 and out[0]["one_per"] == "SOC"


@pytest.mark.llm
@pytest.mark.skipif(os.environ.get("TABBOT_LIVE_TEST") != "1", reason="live test: set TABBOT_LIVE_TEST=1 to run")
def test_live_insights(lw, tmp_path, expected):
    # strict limits: one question, at most 6 calls and about $0.40
    s = load_settings().model_copy(update={"workspace_dir": tmp_path, "max_tool_rounds": 6,
                                           "max_usd_per_question": 0.40})
    if not s.anthropic_api_key:
        pytest.skip("no ANTHROPIC_API_KEY in .env")
    chat = ChatSession(lw, s)
    turn = chat.ask("What are the key insights on the Category by Quarter dashboard, and what drove the change?",
                    app_context="Open page: Category by Quarter")
    assert turn.error is None, turn.text
    text = turn.text
    assert expected["story"]["driver_value"] in text and "Category C" in text
    assert "Data" in text and any(t.name == "run_analysis" for t in turn.tool_calls)


def test_chat_log_records_what_went_and_came_back(lw, tmp_path):
    s = settings_for(tmp_path, anthropic_api_key="sk-ant-SENTINEL-not-a-key")
    fake = FakeClient(list(SCRIPT), tmp_path / "audit" / "outbound.jsonl")
    chat = ChatSession(lw, s, client=fake)
    chat.ask("Key insights?", app_context="Open page: Overview")
    logs = tmp_path / "logs"
    md = next(logs.glob("chat_*.md")).read_text(encoding="utf-8")
    events = [json.loads(x) for x in next(logs.glob("chat_*.jsonl")).read_text(encoding="utf-8").splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "question" and kinds[-1] == "answer"
    assert kinds.count("sent") == 3 and kinds.count("received") == 3 and kinds.count("tool_result") == 4
    q = events[0]
    assert q["question"] == "Key insights?" and q["app_context"] == "Open page: Overview" and q["data"]
    # the tool results in the log are exactly what was sent back to Claude
    sent_back = [b["content"] for m in chat.messages if m["role"] == "user" for b in m["content"]
                 if b.get("type") == "tool_result"]
    assert [e["result"] for e in events if e["event"] == "tool_result"] == sent_back
    assert events[-1]["text"].startswith("**Data**")
    assert "Key insights?" in md and "run_analysis" in md and "Final answer" in md
    raw = md + next(logs.glob("chat_*.jsonl")).read_text(encoding="utf-8")
    assert "SENTINEL" not in raw                                    # the API key is never logged


def test_chat_log_dry_run(lw, tmp_path):
    s = settings_for(tmp_path, dry_run=True)
    ChatSession(lw, s, client=FakeClient([], tmp_path / "audit" / "outbound.jsonl")).ask("hello")
    events = [json.loads(x) for x in next((tmp_path / "logs").glob("chat_*.jsonl")).read_text(
        encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == ["question", "sent", "answer"] and events[0]["dry_run"] is True


def test_formulas_are_sent_whole(lw):
    from tabbot.llm.tools import FORMULA_MAX, _formula

    class F:
        formula = None
        formula_readable = "CASE [A] " + " ".join(f'WHEN "v{i}" THEN "w{i}"' for i in range(30)) + " END"
    out = _formula(F())
    assert out["formula"] == F.formula_readable and len(F.formula_readable) > 160
    assert len(out["values"]["rules"]) == 30 and "formula_truncated" not in out
    F.formula_readable = "x" * (FORMULA_MAX + 5)
    assert _formula(F())["formula_truncated"] == f"first {FORMULA_MAX} of {FORMULA_MAX + 5} characters"


def test_cap_keeps_json_whole(lw):
    ctx = ToolContext(lw=lw, max_rows=200, max_chars=2000)
    payload = {"name": "x", "items": [{"sheet": f"Sheet {i}", "shows": "a bar chart " * 5} for i in range(80)],
               "small": [1, 2, 3]}
    out = json.loads(ctx.cap(payload))                      # still valid JSON
    assert len(json.dumps(out)) <= 2100 and out["items"][-1].endswith("more not shown")
    shown = len(out["items"]) - 1
    assert out["items"][-1] == f"… {80 - shown} more not shown" and out["small"] == [1, 2, 3]
