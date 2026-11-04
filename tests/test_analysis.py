"""Analysis recipes and chart checks vs the planted story and independent pandas calculations."""

import pandas as pd
import pytest

from tabbot.analysis.checks import chart_checks
from tabbot.analysis.recipes import (
    change_point,
    concentration,
    context_for,
    driver_scan,
    flag_share,
    multiplicity,
    partial_period,
    period_compare,
    run_standard,
    share_shift,
    trend,
)
from tabbot.analysis.summary import key_facts
from tabbot.config import load_settings
from tabbot.loader import load_workbook
from tabbot.views.rebuild import rebuild_worksheet


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
    df["q"] = df["date"].dt.to_period("Q")
    return df


def ctx_for(lw, sheet, category=None):
    res = rebuild_worksheet(lw.model, sheet, lw.store, lw.table_for(None), lw.profile_for(None))
    return res, context_for(res, lw.store, lw.profile_for(None), category)


def test_context_from_heatmap(lw):
    _, ctx = ctx_for(lw, "Category Heatmap")
    assert ctx.grain == "quarter" and ctx.category_label == "CATEGORY" and ctx.entity_label == "RECORD_ID"
    assert ctx.measure_label == "Distinct count of RECORD_ID" and ctx.where  # the sheet's own filter is reused


def test_trend_and_partial_period(lw, raw, expected):
    _, ctx = ctx_for(lw, "Records Trend")
    t = trend(ctx)
    ref = raw.groupby("q")["RECORD_ID"].nunique()
    assert t.table["value"].astype(int).tolist() == ref.tolist()
    pp = partial_period(ctx)
    assert pp["period"] == "2026 Q2" and pp["data_ends"] == expected["data"]["date_max"]
    assert pp["days_elapsed"] == 50 and pp["days_in_period"] == 91
    assert pp["full_period_pace"] == pytest.approx(pp["value"] / 50 * 91, abs=0.1)


def test_period_compare_matches_pandas(lw, raw):
    _, ctx = ctx_for(lw, "Category Heatmap")
    pc = period_compare(ctx, n=4)
    prev = raw[raw["q"].between(pd.Period("2024Q2"), pd.Period("2025Q1"))]
    last = raw[raw["q"].between(pd.Period("2025Q2"), pd.Period("2026Q1"))]
    assert pc.summary["previous_window"] == "2024 Q2–2025 Q1" and pc.summary["last_window"] == "2025 Q2–2026 Q1"
    assert pc.summary["previous"] == prev["RECORD_ID"].nunique() and pc.summary["last"] == last["RECORD_ID"].nunique()
    got = pc.table.set_index("CATEGORY")
    for cat in raw["CATEGORY"].unique():
        assert got.loc[cat, "previous"] == prev[prev["CATEGORY"] == cat]["RECORD_ID"].nunique()
        assert got.loc[cat, "last"] == last[last["CATEGORY"] == cat]["RECORD_ID"].nunique()
    assert pc.table.iloc[0]["CATEGORY"] == "Category C"  # planted surge is the top gainer


def test_change_point_finds_planted_start(lw):
    _, ctx = ctx_for(lw, "Records Trend")
    cp = change_point(ctx)
    assert cp.summary["change_at"] == "2025 Q3" and cp.summary["ratio"] > 1.8


def test_driver_scan_ranks_planted_driver_first(lw, expected):
    story = expected["story"]
    _, ctx = ctx_for(lw, "Category Heatmap")
    d = driver_scan(ctx)
    assert d.summary["focus"] == story["surge_category"]
    top = d.summary["top_driver"]
    assert (top["field"], top["value"]) == (story["driver_field"], story["driver_value"])
    assert top["excess"] > 0.5 and d.table.iloc[1]["excess"] < 0.2  # clear winner, not a near tie
    # the dominant-but-ordinary value (e.g. AGE_UNIT = Years) must not rank as a driver
    assert "AGE_UNIT" not in d.table["field"].head(3).tolist()
    # without a category the driver is still found
    _, ctx2 = ctx_for(lw, "Records Trend")
    assert driver_scan(ctx2).summary["top_driver"]["value"] == story["driver_value"]


def test_driver_scan_never_uses_sensitive_columns(lw):
    _, ctx = ctx_for(lw, "Records Trend")
    fields = set(driver_scan(ctx, top=50).table["field"])
    assert not fields & {"RECORD_ID", "NOTES"}


def test_share_concentration_multiplicity_flag(lw, raw):
    _, ctx = ctx_for(lw, "Category Heatmap")
    ss = share_shift(ctx)
    assert ss.summary["biggest_moves"][0]["member"] == "Category C"
    co = concentration(ctx)
    assert co.summary["members"] == 8 and co.summary["total"] == raw["RECORD_ID"].nunique()
    mu = multiplicity(ctx)
    ref = raw.groupby("RECORD_ID").agg(n=("CATEGORY", "nunique"), d=("date", "min"))
    ref = ref.groupby(ref["d"].dt.year)["n"].mean().round(2)
    assert mu.table.set_index("year")["avg_per_entity"].to_dict() == pytest.approx(ref.to_dict())
    fs = flag_share(ctx, "SEVERITY", "Serious")
    assert fs.ok and fs.table["share"].between(0, 1).all()


def test_trivial_multiplicity_is_skipped(lw):
    _, ctx = ctx_for(lw, "Channel Trend")
    assert not multiplicity(ctx).ok  # one channel per record: nothing to say


def test_chart_checks(lw):
    res, ctx = ctx_for(lw, "Category Heatmap")
    ids = {c.id for c in chart_checks(res, ctx)}
    assert {"double_count", "partial_period"} <= ids
    res, ctx = ctx_for(lw, "Gender Pie")
    assert not {c.id for c in chart_checks(res, ctx)} & {"double_count", "partial_period"}


def test_key_facts_tell_the_story(lw, expected):
    _, ctx = ctx_for(lw, "Category Heatmap")
    facts = " ".join(key_facts(run_standard(ctx), ctx.grain))
    assert "2025 Q3" in facts and "CHANNEL = Web" in facts and "incomplete" in facts
