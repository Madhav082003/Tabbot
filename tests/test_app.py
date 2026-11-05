"""The simplified Streamlit app: start page, workbook description, chat. No charts, no dashboard tabs."""

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
    assert "Ask Claude" in text


def test_nothing_extra_on_the_page(app):
    """Feedback: no charts, no dashboard/sheet tabs, no findings banners, no metric tiles, no filters."""
    assert not app.get("plotly_chart") and not app.metric and not app.multiselect
    assert not app.get("button_group")          # the old page tabs (segmented control)
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


def test_open_new_workbook_deletes_previous_copy(app):
    first = app.session_state["lw"].workspace.root
    assert first.exists()
    app.button(key="demo").click().run()
    assert not app.exception
    assert not first.exists()                       # previous copy deleted
    assert app.session_state["lw"].workspace.root.exists()
    app.button(key="close_wb").click().run()
    assert not app.exception and app.session_state["lw"] is None
