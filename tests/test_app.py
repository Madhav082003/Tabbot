"""The Streamlit app runs headless on every page of the demo workbook without errors."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "src" / "tabbot" / "ui" / "app.py"


@pytest.fixture()
def app(fixture_twbx, monkeypatch, tmp_path):
    monkeypatch.setenv("TABBOT_AUTOLOAD", str(fixture_twbx))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from tabbot import config

    config.get_settings.cache_clear()
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    yield at
    config.get_settings.cache_clear()


def _markdown(at) -> str:
    return " ".join(m.value for m in at.markdown)


def test_overview_loads(app, expected):
    assert not app.exception
    assert app.session_state["page"].endswith("Overview")
    assert expected["data"]["entity"] in _markdown(app)


def test_every_page_renders(app, expected):
    pages = list(expected["workbook"]["dashboards"]) + ["📄 Outcome Bars"]
    for p in pages:
        app.session_state["page"] = p
        app.run()
        assert not app.exception, (p, [e.value for e in app.exception])
        assert "Exact definition" in _markdown(app), p


def test_quick_filter_changes_view(app):
    app.session_state["page"] = "Volume Trend"
    app.run()
    ms = [m for m in app.multiselect if m.label == "CHANNEL"]
    assert ms, "quick filter control missing"
    ms[0].set_value(["Web"]).run()
    assert not app.exception
    assert app.session_state["qf"]["Volume Trend"] == {"[federated.fx01].[none:CHANNEL:nk]": ["Web"]}


def test_findings_page_and_analysis_panel(app):
    app.session_state["page"] = "🔎 Findings"
    app.run()
    assert not app.exception
    text = _markdown(app)
    assert "Sheet name doesn't match what it shows" in text and "Counts dates, not records" in text

    app.session_state["page"] = "Category by Quarter"
    app.run()
    assert not app.exception
    text = _markdown(app)
    assert "CHANNEL = Web" in text          # analysis panel key facts
    assert "incomplete" in text              # partial-period check in the checks bar


def test_chat_panel_without_key_and_claude_navigation(app):
    assert not app.exception
    text = _markdown(app) + " ".join(i.value for i in app.info)
    assert "Ask Claude" in text
    # Claude's show_in_app result is applied on the next run, before widgets exist
    app.session_state["pending_nav"] = ("Volume Trend", {"[federated.fx01].[none:CHANNEL:nk]": ["Web"]})
    app.run()
    assert not app.exception
    assert app.session_state["page"] == "Volume Trend"
    assert app.session_state["qf"]["Volume Trend"] == {"[federated.fx01].[none:CHANNEL:nk]": ["Web"]}
    ms = [m for m in app.multiselect if m.label == "CHANNEL"]
    assert ms and ms[0].value == ["Web"]


def test_chat_dry_run_round_trip(fixture_twbx, monkeypatch, tmp_path):
    monkeypatch.setenv("TABBOT_AUTOLOAD", str(fixture_twbx))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("DRY_RUN", "true")
    from tabbot import config

    config.get_settings.cache_clear()
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    at.chat_input[0].set_value("What is in this workbook?").run()
    assert not at.exception
    log = at.session_state["chat_log"]
    assert log[0]["role"] == "user" and "Dry run" in log[1]["text"]
    assert (tmp_path / "audit" / "outbound.jsonl").exists()
    config.get_settings.cache_clear()


def test_open_new_workbook_deletes_previous_copy(app, tmp_path):
    first = app.session_state["lw"].workspace.root
    assert first.exists()
    app.button(key="demo").click().run()
    assert not app.exception
    assert not first.exists()                       # previous copy deleted
    assert app.session_state["lw"].workspace.root.exists()
    app.button(key="close_wb").click().run()
    assert not app.exception and app.session_state["lw"] is None
