"""Regression test on a public breast-cancer workbook (Tableau Public) used for analyst testing.

The file is not committed (*.twbx is git-ignored): put it at tests/fixtures/private/CANCER DASHBOARD ONLINE.twbx or
point TABBOT_CANCER_TWBX at it. Skipped otherwise. Expected values are the analyst's answer key; they live here only,
never in the bot's prompt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tabbot.config import load_settings
from tabbot.llm.tools import ToolContext, dispatch
from tabbot.loader import load_workbook
from tabbot.views.rebuild import rebuild_worksheet

DEFAULT = Path(__file__).parent / "fixtures" / "private" / "CANCER DASHBOARD ONLINE.twbx"
PATH = Path(os.environ.get("TABBOT_CANCER_TWBX") or DEFAULT)
pytestmark = [pytest.mark.private,
              pytest.mark.skipif(not PATH.exists(), reason=f"cancer workbook not found at {PATH}")]


@pytest.fixture(scope="module")
def lw(tmp_path_factory):
    out = load_workbook(PATH, settings=load_settings().model_copy(
        update={"workspace_dir": tmp_path_factory.mktemp("ws")}))
    yield out
    out.close(delete=True)


def rb(lw, sheet):
    ws = lw.model.worksheet(sheet)
    r = rebuild_worksheet(lw.model, ws, lw.store, lw.table_for(ws.datasource), lw.profile_for(ws.datasource))
    assert r.status == "exact", (sheet, r.reasons)
    return r


def test_profile(lw):
    ds = lw.model.datasources[0].name
    prof = lw.profile_for(ds)
    assert prof.rows == 334 and all(c.nulls == 0 for c in prof.columns)
    assert prof.entity.field == "Patient_ID" and prof.entity.distinct == 334     # not the one COUNTD category
    assert not prof.column("Patient_Status").sensitive_reasons                  # labels visible
    assert not prof.column("Insurance_Paid").sensitive_reasons                  # "...Paid" is not an identifier
    out, err = dispatch(ToolContext(lw=lw), "list_dashboards", {})
    cols = json.loads(out)["data_sources"][0]["data_profile"]["columns"]
    assert not err and cols["Patient_ID"]["rows_per_record"] == 1.0 and "nulls" not in cols["Patient_ID"]
    assert cols["Patient_Status"]["values"] == ["Alive", "Dead", "Not confirmed"]


def test_status_counts_are_visible(lw):
    out, err = dispatch(ToolContext(lw=lw), "get_chart_data", {"sheet": "Patient by Status"})
    d = json.loads(out)
    assert not err and "[hidden]" not in out
    got = {r["Patient Status"]: r["Total Count"] for r in d["rows"]}
    assert got == {"Alive": 255, "Dead": 66, "Not confirmed": 13}


def test_tumour_stage_donut_percent_of_total(lw):
    r = rb(lw, "Patient by Tumour Stage")
    m = next(m for m in r.measures if m.plan is not None)
    stage = next(d for d in r.dims if d.label == "Tumour Stage")
    got = {k: round(v * 100, 2) for k, v in zip(r.data[stage.alias], r.data[m.alias], strict=True)}
    assert got == {"I": 19.16, "II": 56.59, "III": 24.25}


def test_cards(lw):
    death = rb(lw, "Death Rate")
    assert round(float(death.data[death.measures[0].alias].iloc[0]), 2) == 19.76
    surgery = rb(lw, "Surgery Type")
    assert int(surgery.data[surgery.measures[0].alias].iloc[0]) == 4


def test_dashboard_text_objects_are_read_once(lw):
    out, err = dispatch(ToolContext(lw=lw), "list_dashboards", {})
    d = next(x for x in json.loads(out)["dashboards"] if x["name"] == "Dashboard 1")
    assert not err and d["title"] == "Breast Cancer Analytics | 2017-2019"
    assert d["text_on_dashboard"].count("Target to Reduce Death Rate by 10%") == 1   # also in the phone layout


def test_last_visit_before_surgery_can_be_checked(lw):
    import pandas as pd
    raw = pd.read_parquet(lw.tables[0].parquet)
    expected = int((pd.to_datetime(raw["date_of_last_visit"]) < pd.to_datetime(raw["Date_of_Surgery"])).sum())
    out, err = dispatch(ToolContext(lw=lw), "query_data", {
        "compare": [{"field": "date_of_last_visit", "op": "<", "other_field": "Date_of_Surgery"}]})
    assert not err and json.loads(out)["rows"][0]["Distinct count of Patient_ID"] == expected


def test_status_can_be_crossed_with_surgery_type(lw):
    out, err = dispatch(ToolContext(lw=lw), "query_data", {
        "dimensions": [{"field": "Surgery_type"}, {"field": "Patient_Status"}]})
    d = json.loads(out)
    assert not err and "Dead" in out
    assert sum(r["Distinct count of Patient_ID"] for r in d["rows"]) == 334
