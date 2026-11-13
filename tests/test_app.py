"""The simplified Streamlit app: start page, workbook explanation, dashboard bar, chat. No charts."""

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


def test_workbook_page(app, expected):
    assert not app.exception
    text = _markdown(app)
    assert "Fixture Records" in text and f"{expected['data']['entities']:,} records" in text
    assert "02 Jan 2021 to 20 May 2026" in text
    for d in expected["workbook"]["dashboards"]:
        assert d in text
    assert "Insights Assistant" in text and "Claude" not in text and "effort" not in text


def test_nothing_extra_on_the_page(app):
    """Feedback: no charts, no dashboard/sheet tabs, no findings banners, no metric tiles, no filters."""
    assert not app.get("plotly_chart") and not app.metric and not app.multiselect
    assert not app.get("button_group")           # no page tabs; dashboards are picked from one dropdown
    assert len(app.selectbox) == 1 and app.selectbox[0].key.startswith("dash_pick::")
    text = _markdown(app)
    for gone in ("Findings", "Exact definition", "Analysis of", "Worksheets"):
        assert gone not in text, gone


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


def test_close_workbook_deletes_local_copy(app):
    first = app.session_state["lw"].workspace.root
    assert first.exists()
    app.button(key="close_wb").click().run()
    assert not app.exception and app.session_state["lw"] is None
    assert not first.exists()                       # local copy deleted


def test_no_demo_and_no_usage_panel(app):
    assert not [b for b in app.button if "demo" in (b.key or "")]
    assert not any("Usage today" in e.label or "What was sent" in e.label for e in app.expander)


def test_dashboard_bar_lists_every_dashboard_view(app, expected):
    bar = app.selectbox[0]
    assert bar.options[0] == "Whole workbook"
    names = set(expected["workbook"]["dashboards"])
    assert set(bar.options[1:]) <= names and len(bar.options) > 1


def test_selecting_a_dashboard_shows_its_preview_explanation_and_questions(app):
    bar = app.selectbox[0]
    pick = bar.options[1]
    bar.set_value(pick).run()
    assert not app.exception
    text = _markdown(app)
    assert pick in text and "In depth" in text and "What it shows" in text
    assert app.get("image"), "the preview image Tableau saved should be shown"
    assert any("small preview Tableau stores" in c.value for c in app.caption)
    assert any(f"Asking about: **{pick}**" in c.value for c in app.caption)
    bar.set_value("Whole workbook").run()
    assert "In brief" in _markdown(app) and "In depth" not in _markdown(app)


def test_question_carries_the_selected_dashboard(fixture_twbx, monkeypatch, tmp_path):
    monkeypatch.setenv("TABBOT_AUTOLOAD", str(fixture_twbx))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("DRY_RUN", "true")
    from tabbot import config

    config.get_settings.cache_clear()
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    bar = at.selectbox[0]
    pick = bar.options[1]
    bar.set_value(pick).run()
    sugs = [x.label for x in at.button if (x.key or "").startswith("sug")]
    assert sugs and sugs[0].startswith("Explain this dashboard")      # suggestions follow the selection
    at.chat_input[0].set_value("What does this dashboard show?").run()
    assert not at.exception
    log = at.session_state["chat_log"]
    assert log[0]["about"] == pick
    import json
    sent = [json.loads(x) for x in (tmp_path / "audit" / "outbound.jsonl").read_text(encoding="utf-8").splitlines()]
    assert f"selected the dashboard '{pick}'" in json.dumps(sent[-1]["request"])
    config.get_settings.cache_clear()
