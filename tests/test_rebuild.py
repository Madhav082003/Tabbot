"""Rebuilt worksheets vs an independent pandas computation straight from the Parquet copy of the extract."""

import pandas as pd
import pytest

from tabbot.config import load_settings
from tabbot.loader import load_workbook
from tabbot.twb.fieldref import parse_ref
from tabbot.ui.charts import figure_for
from tabbot.views.rebuild import field_values, rebuild_worksheet


@pytest.fixture(scope="module")
def lw(fixture_twbx, tmp_path_factory):
    settings = load_settings().model_copy(update={"workspace_dir": tmp_path_factory.mktemp("ws")})
    loaded = load_workbook(fixture_twbx, settings=settings)
    yield loaded
    loaded.close()


@pytest.fixture(scope="module")
def raw(lw):
    df = pd.read_parquet(lw.tables[0].parquet)
    df["date"] = pd.to_datetime(df["RECEIVED_DATE"], format="%d-%b-%Y")
    return df


def rb(lw, name, qf=None):
    return rebuild_worksheet(lw.model, name, lw.store, lw.table_for(None), lw.profile_for(None), qf)


def test_every_fixture_sheet_is_exact(lw):
    for ws in lw.model.worksheets:
        r = rb(lw, ws.name)
        assert r.status == "exact", (ws.name, r.reasons)
        assert not r.data.empty


def test_heatmap_matches_pandas(lw, raw):
    r = rb(lw, "Category Heatmap")
    assert r.chart == "heatmap"
    got = {(a, int(b), int(c)): int(v) for a, b, c, v in r.data[["d0", "d1", "d2", "m0"]].itertuples(index=False)}
    ref = raw.groupby([raw["CATEGORY"], raw["date"].dt.year, raw["date"].dt.quarter])["RECORD_ID"].nunique()
    assert got == {k: int(v) for k, v in ref.items()}
    dt = r.checks["distinct_total"]
    assert dt["distinct_total"] == raw["RECORD_ID"].nunique() and dt["cells_can_be_summed"] is False
    assert r.checks["unparsed_dates"] == {"RECEIVED_DATE": 0}


def test_line_by_channel_and_quick_filter(lw, raw):
    r = rb(lw, "Channel Trend")
    assert r.chart == "line"
    got = {(pd.Timestamp(q), ch): int(v) for q, ch, v in r.data[["d0", "d1", "m0"]].itertuples(index=False)}
    qstart = raw["date"].dt.to_period("Q").dt.start_time
    ref = raw.groupby([qstart, raw["CHANNEL"]])["RECORD_ID"].nunique()
    assert got == {k: int(v) for k, v in ref.items()}

    web = rb(lw, "Channel Trend", {"[federated.fx01].[none:CHANNEL:nk]": ["Web"]})
    assert set(web.data["d1"]) == {"Web"} and web.applied_quick_filters == {"CHANNEL": ["Web"]}


def test_countd_if_calc(lw, raw):
    r = rb(lw, "Serious Trend")
    got = {pd.Timestamp(q): int(v) for q, v in r.data[["d0", "m0"]].itertuples(index=False)}
    s = raw[raw["SEVERITY"] == "Serious"]
    ref = s.groupby(s["date"].dt.to_period("Q").dt.start_time)["RECORD_ID"].nunique()
    assert {k: v for k, v in got.items() if v} == {k: int(v) for k, v in ref.items()}


def test_pies_and_filters(lw, raw):
    keep = raw[raw["GENDER"].isin(["Female", "Male"])]
    r = rb(lw, "Gender Pie")
    assert r.chart == "pie"
    assert dict(zip(r.data["d0"], r.data["m0"], strict=True)) == keep.groupby("GENDER")["RECORD_ID"].nunique().to_dict()

    def age_group(a):
        if pd.isna(a):
            return "Not Reported"
        return "Under 18" if a < 18 else "18 to 64" if a < 65 else "65 and above"

    r = rb(lw, "Age Group Pie")
    ref = keep.assign(g=keep["AGE"].map(age_group)).groupby("g")["RECORD_ID"].nunique().to_dict()
    assert dict(zip(r.data["d0"], r.data["m0"], strict=True)) == ref


def test_exclude_filter_and_count_of_dates(lw, raw, expected):
    r = rb(lw, "Dates by Subcategory")
    excluded = sorted(raw["SUBCATEGORY"].unique())[:expected["planted_problems"]["large_exclude_filter"]["members"]]
    v = raw[~raw["SUBCATEGORY"].isin(excluded)]
    ref = v.groupby([v["SUBCATEGORY"], v["date"].dt.year])["date"].nunique()
    got = {(a, int(b)): int(m) for a, b, m in r.data[["d0", "d1", "m0"]].itertuples(index=False)}
    assert got == {k: int(x) for k, x in ref.items()}


def test_misnamed_sheet_shows_what_its_filter_says(lw, raw):
    r = rb(lw, "Category B")
    assert r.data["m0"].sum() == raw[raw["CATEGORY"] == "Category D"].groupby(
        raw["date"].dt.to_period("Q"))["RECORD_ID"].nunique().sum()


def test_bar_and_text_table(lw, raw):
    r = rb(lw, "Outcome Bars")
    assert r.chart == "bar"
    assert dict(zip(r.data["d0"], r.data["m0"], strict=True)) == raw.groupby("OUTCOME")["RECORD_ID"].nunique().to_dict()
    assert rb(lw, "Region Table").chart == "table"


def test_figures_build_for_every_sheet(lw):
    for ws in lw.model.worksheets:
        fig, table, _ = figure_for(rb(lw, ws.name))
        assert (fig is not None) != (table is not None and not table.empty), ws.name


def test_field_values(lw):
    vals = field_values(lw.model, parse_ref("[federated.fx01].[none:CHANNEL:nk]"), lw.store, lw.table_for(None),
                        lw.profile_for(None))
    assert vals == ["Email", "Phone", "Web"]
    q = field_values(lw.model, parse_ref("[federated.fx01].[qr:Calculation_1001:ok]"), lw.store,
                     lw.table_for(None), lw.profile_for(None))
    assert q == ["1", "2", "3", "4"]


def test_unsupported_sheet_is_flagged_approximate(lw):
    ws = lw.model.worksheet("Records Trend").model_copy(deep=True)
    ws.name = "Tampered"
    ws.encodings["detail"] = [parse_ref("[federated.fx01].[pcto:sum:AGE:qk]")]
    r = rebuild_worksheet(lw.model, ws, lw.store, lw.table_for(None), lw.profile_for(None))
    assert r.status == "approximate" and any("table_calc" in x for x in r.reasons)
    assert not r.data.empty


def test_no_extract_fails_cleanly(lw):
    r = rebuild_worksheet(lw.model, "Category Heatmap", None, None, None)
    assert r.status == "failed" and r.reasons
