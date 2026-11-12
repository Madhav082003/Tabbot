"""Saved chats: the store, restoring a conversation, and the past-chats panel in the app (dry run, nothing sent)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tabbot.chat_store import ChatStore, SavedChat, new_chat_id

APP = Path(__file__).resolve().parents[1] / "src" / "tabbot" / "ui" / "app.py"


def chat(digest="d1", workbook="Sales", log=None, messages=None) -> SavedChat:
    return SavedChat(id=new_chat_id(), workbook=workbook, source_name=f"{workbook}.twbx", digest=digest,
                     created="2026-11-12T10:00:00", updated="", log=log or [], messages=messages or [])


# ------------------------------------------------------------------ store

def test_store_saves_lists_newest_first_and_deletes(tmp_path):
    store = ChatStore(tmp_path / "chats")
    assert store.list() == []
    a = store.save(chat(log=[{"role": "user", "text": "first?"}]))
    b = store.save(chat(workbook="HR"))
    assert [c.id for c in store.list()][0] == b.id and {c.id for c in store.list()} == {a.id, b.id}
    got = store.load(a.id)
    assert got.first_question == "first?" and got.questions == 1 and got.workbook == "Sales"
    store.delete(a.id)
    assert [c.id for c in store.list()] == [b.id]


def test_store_skips_broken_files_and_rejects_odd_ids(tmp_path):
    store = ChatStore(tmp_path)
    store.save(chat())
    (tmp_path / "0123456789ab.json").write_text("{not json", encoding="utf-8")
    assert len(store.list()) == 1
    assert store.load("../../etc/passwd") is None              # never reads outside the chats folder
    with pytest.raises(ValueError):
        store.delete("../outside")


def test_restore_continues_result_ids_and_drops_unanswered_question(tmp_path, fixture_twbx):
    from tabbot.config import load_settings
    from tabbot.llm.orchestrator import ChatSession
    from tabbot.loader import load_workbook

    s = load_settings().model_copy(update={"workspace_dir": tmp_path})
    lw = load_workbook(fixture_twbx, settings=s)
    try:
        history = [
            {"role": "user", "content": [{"type": "text", "text": "q1"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "query_data", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                          "content": json.dumps({"result_id": "R7", "rows": []})}]},
            {"role": "assistant", "content": [{"type": "text", "text": "answer (R7)"}]},
            {"role": "user", "content": [{"type": "text", "text": "never answered"}]},
        ]
        session = ChatSession(lw, s)
        session.restore(history)
        assert len(session.messages) == 4 and session.messages[-1]["role"] == "assistant"
        assert session.tools_ctx.new_id(None) == "R8"            # a new result never reuses an old id
        assert lw.digest and len(lw.digest) == 64 and lw.display_name == "fixture"
    finally:
        lw.close(delete=True)


# ------------------------------------------------------------------ the app

def _start(fixture_twbx, monkeypatch, tmp_path, **env):
    monkeypatch.setenv("TABBOT_AUTOLOAD", str(fixture_twbx))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("DRY_RUN", "true")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from tabbot import config

    config.get_settings.cache_clear()
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    return at, ChatStore(tmp_path / "chats")


@pytest.fixture()
def app(fixture_twbx, monkeypatch, tmp_path):
    yield _start(fixture_twbx, monkeypatch, tmp_path)
    from tabbot import config
    config.get_settings.cache_clear()


@pytest.fixture()
def app_no_copies(fixture_twbx, monkeypatch, tmp_path):
    yield _start(fixture_twbx, monkeypatch, tmp_path, KEEP_WORKBOOK_COPIES="false")
    from tabbot import config
    config.get_settings.cache_clear()


# ------------------------------------------------------------------ the workbook library

def test_library_keeps_one_copy_per_file_and_prunes(tmp_path):
    from tabbot.library import WorkbookLibrary
    src = tmp_path / "Sales.twbx"
    src.write_bytes(b"pk-data")
    lib = WorkbookLibrary(tmp_path / "library")
    d1, d2 = "a" * 64, "b" * 64
    p = lib.put(src, d1)
    assert p.name == "Sales.twbx" and lib.get(d1) == p and lib.put(src, d1) == p
    lib.put(src, d2)
    assert lib.prune({d1}) == 1 and lib.get(d2) is None and lib.get(d1) is not None
    assert lib.get("../../etc") is None                        # never outside the library folder
    lib.delete(d1)
    assert lib.get(d1) is None


def _keys(at, prefix):
    return [b.key for b in at.button if (b.key or "").startswith(prefix)]


def test_a_question_is_saved_and_listed_by_workbook_name(app):
    at, store = app
    assert not at.exception and _keys(at, "new_chat") and not _keys(at, "open__")
    at.chat_input[0].set_value("What is in this workbook?").run()
    saved = store.list()
    assert len(saved) == 1 and saved[0].workbook == "fixture" and saved[0].questions == 1
    assert saved[0].log[0]["text"] == "What is in this workbook?" and "Dry run" in saved[0].log[1]["text"]
    btn = at.button(key=f"open__{saved[0].id}")
    assert btn.label == "fixture"                                    # the name, without .twbx
    at.chat_input[0].set_value("And the dashboards?").run()          # same chat, not a new one
    assert len(store.list()) == 1 and store.list()[0].questions == 2


def test_new_chat_goes_to_the_start_page(app):
    """Opened the wrong workbook: New chat shows the upload page; the open one is one click away."""
    at, _ = app
    at.button(key="new_chat").click().run()
    assert not at.exception and at.session_state["home"] is True and not at.chat_input
    assert any("Open a Tableau workbook" in m.value for m in at.markdown)
    btn = at.button(key="chat_on_open")
    assert btn.label == "or start a new chat on fixture"
    btn.click().run()
    assert at.session_state["home"] is False and at.chat_input and at.session_state["chat_log"] == []


def test_new_chat_then_reopen_restores_it(app):
    at, store = app
    at.chat_input[0].set_value("First question").run()
    cid = store.list()[0].id
    at.button(key="new_chat").click().run()
    assert at.session_state["chat_log"] == [] and at.session_state["saved"] is None
    at.button(key=f"open__{cid}").click().run()
    assert not at.exception and [m["role"] for m in at.session_state["chat_log"]] == ["user", "assistant"]
    assert at.session_state["saved"].id == cid and at.session_state["viewing"] is None
    assert at.chat_input                                              # it can be continued


def test_chat_of_another_workbook_is_read_only_with_a_way_back(app):
    at, store = app
    other = store.save(chat(digest="not-this-file", workbook="Clinic Overview",
                            log=[{"role": "user", "text": "Old question"},
                                 {"role": "assistant", "text": "Old answer"}]))
    at.run()
    at.button(key=f"open__{other.id}").click().run()
    assert not at.exception and at.session_state["viewing"] == other.id
    assert any("To continue this chat" in i.value for i in at.info)
    text = " ".join(m.value for m in at.markdown)
    assert "Old question" in text and "Old answer" in text and not at.chat_input
    at.button(key="back_to_open").click().run()
    assert at.session_state["viewing"] is None and at.chat_input


def test_delete_and_toggle_panel(app):
    at, store = app
    at.chat_input[0].set_value("To be deleted").run()
    cid = store.list()[0].id
    at.button(key=f"del__{cid}").click().run()
    assert store.list() == [] and not _keys(at, "open__") and at.session_state["saved"] is None
    at.button(key="toggle_history").click().run()
    assert not _keys(at, "new_chat")                                   # panel hidden
    at.button(key="toggle_history").click().run()
    assert _keys(at, "new_chat")


def test_a_stopped_answer_keeps_what_was_written(app):
    """The run that streamed the answer was interrupted by Stop: the next run records the partial text."""
    at, store = app
    at.chat_input[0].set_value("First").run()
    at.session_state["chat_log"].append({"role": "user", "text": "Long question", "tools": [],
                                         "about": "Whole workbook"})
    at.session_state["pending_answer"] = {"text": "Half an answer"}
    at.run()
    last = at.session_state["chat_log"][-1]
    assert last["role"] == "assistant" and last["text"] == "Half an answer\n\n_Stopped._"
    assert at.session_state["pending_answer"] is None
    assert store.list()[0].log[-1]["text"].endswith("_Stopped._")          # saved too


def test_header_lines_only_before_the_first_question(app):
    at, _ = app
    assert any("Asking about" in c.value for c in at.caption)
    at.chat_input[0].set_value("Hello").run()
    assert not any("Asking about" in c.value or "Ask anything" in c.value for c in at.caption)


def test_old_chat_reopens_its_workbook_and_continues(app, tmp_path):
    at, store = app
    at.chat_input[0].set_value("Keep me").run()
    c = store.list()[0]
    assert (tmp_path / "library" / c.digest).exists()                        # a copy was kept
    at.button(key="close_wb").click().run()
    assert at.session_state["lw"] is None and (tmp_path / "library" / c.digest).exists()   # a chat still uses it
    at.button(key=f"open__{c.id}").click().run()
    assert not at.exception and at.session_state["lw"] is not None           # reopened automatically
    assert at.session_state["lw"].digest == c.digest and at.session_state["viewing"] is None
    assert at.session_state["saved"].id == c.id and at.session_state["chat_log"][0]["text"] == "Keep me"
    assert at.chat_input and not any("To continue this chat" in i.value for i in at.info)
    at.chat_input[0].set_value("And then?").run()                            # continues the same chat
    assert len(store.list()) == 1 and store.list()[0].questions == 2


def test_deleting_the_last_chat_removes_the_copy(app, tmp_path):
    at, store = app
    at.chat_input[0].set_value("Temp").run()
    c = store.list()[0]
    at.button(key="close_wb").click().run()
    at.run()
    at.button(key=f"del__{c.id}").click().run()
    assert not (tmp_path / "library" / c.digest).exists()


def test_without_copies_an_old_chat_is_read_beside_its_explanation(app_no_copies, tmp_path):
    at, store = app_no_copies
    at.chat_input[0].set_value("Keep me").run()
    cid = store.list()[0].id
    assert not (tmp_path / "library").exists() or not any((tmp_path / "library").iterdir())
    assert store.load(cid).report["sections"]                                # explanation saved with the chat
    at.button(key="close_wb").click().run()
    assert at.session_state["lw"] is None and _keys(at, f"open__{cid}")     # listed on the start page
    at.button(key=f"open__{cid}").click().run()
    text = " ".join(m.value for m in at.markdown)
    assert not at.exception and "Keep me" in text and "In brief" in text     # chat beside the explanation
    bar = at.get("button_group")[0]
    assert bar.options[0] == "Whole workbook" and len(bar.options) > 1       # its dashboards too
    bar.set_value(bar.options[1]).run()
    assert not at.exception and "In depth" in " ".join(m.value for m in at.markdown)


def test_old_chat_without_saved_explanation(app):
    at, store = app
    old = store.save(chat(digest="elsewhere", workbook="Old One", log=[{"role": "user", "text": "Q"},
                                                                     {"role": "assistant", "text": "A"}]))
    at.run()
    at.button(key=f"open__{old.id}").click().run()
    assert not at.exception and any("not saved with this older chat" in i.value for i in at.info)
