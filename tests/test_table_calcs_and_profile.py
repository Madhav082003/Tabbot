"""Three fixes from analyst testing, on a small made-up package (20 patients x 6 visits, CSV data):

1. a category whose name matches a sensitive pattern (e.g. a status) is shown; a name-like column that only repeats
   over one patient's rows stays hidden;
2. table calculations are computed on the chart's cells: TOTAL() formulas, percent of total (partitioned the way the
   sheet says), running total, rank; unsupported ones show their underlying value and say so;
3. the overview carries a data profile (nulls, distinct, identifier uniqueness, values of small categories).
"""

from __future__ import annotations

import datetime as dt
import json
import zipfile

import duckdb
import pandas as pd
import pytest

from tabbot.config import load_settings
from tabbot.llm.tools import ToolContext, dispatch
from tabbot.loader import load_workbook
from tabbot.views.calc_translate import Expr, Unsupported, translate_table_calc
from tabbot.views.rebuild import rebuild_worksheet

DS = "federated.clinic"
STATUS = ["Active"] * 10 + ["Closed"] * 5 + ["Unknown"] * 5          # one status per patient, >= 5 each
TREAT = ["A"] * 6 + ["B"] * 4 + ["A"] * 3 + ["B"] * 2 + ["A"] * 3 + ["B"] * 2   # A: 6 active of 12 patients
# name-matched; 6.7 patients per value on average, but G3 belongs to ONE patient: must stay hidden (minimum rule)
GROUP = ["G1"] * 10 + ["G2"] * 9 + ["G3"]


def rows() -> pd.DataFrame:
    out = []
    for p in range(20):
        for v in range(6):
            out.append({"Patient_ID": f"P{p:03d}", "Patient_Name": f"Person {p}", "Patient_Status": STATUS[p],
                        "Patient_Group": GROUP[p],
                        "Treatment": TREAT[p], "Visit_Date": (dt.date(2024, 1, 15) + dt.timedelta(days=31 * v)
                                                              ).isoformat(),
                        "Amount": float(10 * (p % 5) + v)})
    return pd.DataFrame(out)


def col(name: str, dtype: str) -> str:
    return (f"<metadata-record class='column'><remote-name>{name}</remote-name><local-name>[{name}]</local-name>"
            f"<parent-name>[visits.csv]</parent-name><local-type>{dtype}</local-type></metadata-record>")


DATASOURCE = f"""
<datasource caption='Clinic' inline='true' name='{DS}'>
  <connection class='federated'>
    <named-connections><named-connection caption='visits' name='textscan.1'>
      <connection class='textscan' directory='Data/Clinic' filename='visits.csv'/></named-connection>
    </named-connections>
    <relation connection='textscan.1' name='visits.csv' table='[visits#csv]' type='table'/>
    <metadata-records>{col('Patient_ID', 'string')}{col('Patient_Name', 'string')}{col('Patient_Status', 'string')}
      {col('Patient_Group', 'string')}
      {col('Treatment', 'string')}{col('Visit_Date', 'string')}{col('Amount', 'real')}</metadata-records>
  </connection>
  <column caption='Patient Count' datatype='integer' name='[Calculation_1]' role='measure' type='quantitative'>
    <calculation class='tableau' formula='COUNTD([Patient_ID])'/></column>
  <column caption='Share' datatype='real' name='[Calculation_2]' role='measure' type='quantitative'>
    <calculation class='tableau' formula='[Calculation_1] / TOTAL([Calculation_1])'/></column>
</datasource>"""


def sheet(name: str, mark: str, rows_: str = "", cols: str = "", enc: str = "", instances: str = "") -> str:
    return f"""<worksheet name='{name}'><table><view>
      <datasources><datasource name='{DS}'/></datasources>
      <datasource-dependencies datasource='{DS}'>{instances}</datasource-dependencies></view>
      <panes><pane><mark class='{mark}'/><encodings>{enc}</encodings></pane></panes>
      <rows>{rows_}</rows><cols>{cols}</cols></table></worksheet>"""


def ci(inner: str, column: str, deriv: str, tc: str) -> str:
    return (f"<column-instance column='[{column}]' derivation='{deriv}' name='[{inner}]' pivot='key' "
            f"type='quantitative'>{tc}</column-instance>")


R = f"[{DS}]"
SHEETS = (
    sheet("Status Donut", "Pie", enc=f"<color column='{R}.[none:Patient_Status:nk]'/>"
          f"<size column='{R}.[usr:Calculation_1:qk]'/><text column='{R}.[usr:Calculation_2:qk]'/>",
          instances=ci("usr:Calculation_2:qk", "Calculation_2", "User", "<table-calc ordering-type='Rows'/>"))
    + sheet("Status by Treatment", "Bar", rows_=f"{R}.[none:Treatment:nk]", cols=f"{R}.[pcto:ctd:Patient_ID:qk]",
            enc=f"<color column='{R}.[none:Patient_Status:nk]'/>",
            instances=ci("pcto:ctd:Patient_ID:qk", "Patient_ID", "CountD",
                         "<table-calc ordering-type='Rows' type='PctTotal'/>"))
    + sheet("Running Amount", "Line", rows_=f"{R}.[cum:sum:Amount:qk]", cols=f"{R}.[tmn:Visit_Date:qk]",
            instances=ci("cum:sum:Amount:qk", "Amount", "Sum", "<table-calc ordering-type='Rows' type='CumTotal'/>"))
    + sheet("Treatment Rank", "Bar", rows_=f"{R}.[none:Treatment:nk]", cols=f"{R}.[rank:sum:Amount:qk]",
            instances=ci("rank:sum:Amount:qk", "Amount", "Sum", "<table-calc ordering-type='Columns' type='Rank'/>"))
)


@pytest.fixture(scope="module")
def lw(tmp_path_factory):
    d = tmp_path_factory.mktemp("clinic")
    twbx = d / "clinic.twbx"
    xml = (f"<?xml version='1.0' encoding='utf-8' ?><workbook version='18.1' "
           f"xmlns:user='http://www.tableausoftware.com/xml/user'><datasources>{DATASOURCE}</datasources>"
           f"<worksheets>{SHEETS}</worksheets></workbook>")
    with zipfile.ZipFile(twbx, "w") as z:
        z.writestr("clinic.twb", xml)
        z.writestr("Data/Clinic/visits.csv", rows().to_csv(index=False))
    out = load_workbook(twbx, settings=load_settings().model_copy(update={"workspace_dir": d / "ws"}))
    yield out
    out.close(delete=True)


def rb(lw, name):
    return rebuild_worksheet(lw.model, name, lw.store, lw.table_for(DS), lw.profile_for(DS))


# ------------------------------------------------------------------ 1. categories vs identifying columns

def test_status_category_is_shown_but_names_stay_hidden(lw):
    prof = lw.profile_for(DS)
    assert prof.entity.field == "Patient_ID"
    assert not prof.column("Patient_Status").sensitive_reasons          # every value shared by >= 5 patients
    # 20 names x 6 rows passes a per-row count, but each name belongs to ONE patient: stays hidden
    assert prof.column("Patient_Name").sensitive_reasons
    assert prof.column("Patient_Group").sensitive_reasons                # one value held by a single patient
    assert prof.column("Patient_ID").sensitive_reasons


def test_threshold_is_configurable(lw):
    from tabbot.data.profile import is_shared_category
    s = load_settings().privacy
    view = lw.store.view(lw.table_for(DS))
    group = lw.profile_for(DS).column("Patient_Group")
    assert not is_shared_category(lw.store, view, group, s, "Patient_ID")
    loose = s.model_copy(update={"category_exemption": s.category_exemption.model_copy(
        update={"min_records_per_value": 1})})
    assert is_shared_category(lw.store, view, group, loose, "Patient_ID")
    off = s.model_copy(update={"category_exemption": s.category_exemption.model_copy(update={"max_distinct": 0})})
    assert not is_shared_category(lw.store, view, lw.profile_for(DS).column("Patient_Status"), off, "Patient_ID")


def test_tools_return_status_labels_and_refuse_names(lw):
    ctx = ToolContext(lw=lw)
    out, err = dispatch(ctx, "get_chart_data", {"sheet": "Status Donut"})
    assert not err and "[hidden]" not in out and {"Active", "Closed", "Unknown"} <= set(out.replace('"', " ").split())
    q, err = dispatch(ctx, "query_data", {"dimensions": [{"field": "Patient_Status"}, {"field": "Treatment"}]})
    assert not err and "Closed" in q
    _, err = dispatch(ctx, "query_data", {"dimensions": [{"field": "Patient_Name"}]})
    assert err


def test_query_data_percent_of_total(lw):
    out, err = dispatch(ToolContext(lw=lw), "query_data", {"dimensions": [{"field": "Patient_Status"}],
                                                          "percent_of_total": True})
    d = json.loads(out)
    assert not err and d["total"] == 20
    shares = {r["Patient_Status"]: r["share of total"] for r in d["rows"]}
    assert shares == {"Active": 0.5, "Closed": 0.25, "Unknown": 0.25}
    # filtered: the total is what the filter keeps
    out, _ = dispatch(ToolContext(lw=lw), "query_data", {"dimensions": [{"field": "Patient_Status"}],
                                                        "filters": [{"field": "Treatment", "values": ["A"]}],
                                                        "percent_of_total": True})
    d = json.loads(out)
    assert d["total"] == 12 and {r["Patient_Status"]: r["share of total"] for r in d["rows"]}["Active"] == 0.5


# ------------------------------------------------------------------ 2. table calculations

def test_total_formula_gives_each_slice_its_share(lw):
    r = rb(lw, "Status Donut")
    assert r.status == "exact", r.reasons
    share = next(m for m in r.measures if m.plan is not None)
    got = dict(zip(r.data["d0"], r.data[share.alias].round(4), strict=True))
    assert got == {"Active": 0.5, "Closed": 0.25, "Unknown": 0.25}          # 10, 5, 5 of 20 patients
    assert r.table_calcs and "over the whole view" in r.table_calcs[0]
    assert not [c for c in r.data.columns if "__" in c]                      # helper columns removed


def test_percent_of_total_restarts_on_every_row_for_table_across(lw):
    r = rb(lw, "Status by Treatment")
    assert r.status == "exact", r.reasons
    m = r.measures[0]
    assert m.label == "Percent of total of Distinct count of Patient_ID"
    df = r.data.rename(columns={"d0": "treat", "d1": "status", m.alias: "pct"})
    assert df.groupby("treat")["pct"].sum().round(6).tolist() == [1.0, 1.0]      # partition = Rows (Treatment)
    a_active = df[(df.treat == "A") & (df.status == "Active")]["pct"].item()
    assert a_active == pytest.approx(6 / 12)                                      # 6 of the 12 A patients


def test_running_total_and_rank(lw):
    raw = rows()
    r = rb(lw, "Running Amount")
    assert r.status == "exact", r.reasons
    vals = r.data[r.measures[0].alias].tolist()
    assert vals == sorted(vals) and vals[-1] == pytest.approx(raw["Amount"].sum())
    r = rb(lw, "Treatment Rank")
    ranks = dict(zip(r.data["d0"], r.data[r.measures[0].alias], strict=True))
    by = raw.groupby("Treatment")["Amount"].sum()
    assert ranks[by.idxmax()] == 1 and ranks[by.idxmin()] == 2


def test_tools_report_computed_table_calcs(lw):
    out, err = dispatch(ToolContext(lw=lw), "describe_sheet", {"sheet": "Status by Treatment"})
    d = json.loads(out)
    assert not err and d["redraw"] == "exact" and "percent of total" in d["table_calculations"][0]


# ------------------------------------------------------------------ 3. the data profile

def test_overview_profile_answers_caveat_checks(lw):
    out, err = dispatch(ToolContext(lw=lw), "list_dashboards", {})
    prof = json.loads(out)["data_sources"][0]["data_profile"]
    cols = prof["columns"]
    assert prof["rows"] == 120 and cols["Patient_ID"]["rows_per_record"] == 6
    assert cols["Patient_Status"]["values"] == ["Active", "Closed", "Unknown"]
    assert cols["Patient_Name"]["values_shared"] is False and "values" not in cols["Patient_Name"]
    assert "Person 1" not in out and "P001" not in out
    assert cols["Visit_Date"]["range"] == ["2024-01-15", "2024-06-18"]


# ------------------------------------------------------------------ table-calc formulas, checked in DuckDB

CELLS = pd.DataFrame({"g": ["x", "x", "x", "y", "y"], "k": [1, 2, 3, 1, 2], "v": [5.0, 1.0, 3.0, 4.0, 4.0]})


def plan_for(formula: str):
    def resolve(name):
        if name == "V":
            return Expr('"v"')
        if name == "Total V":
            return Expr('SUM("v")', is_agg=True)
        raise Unsupported(f"field [{name}] is not in the extract")
    return translate_table_calc(formula, resolve, "m0")


def run_plan(formula: str) -> list:
    """Stage 1 on one row per cell (SUM over a single row = the value), stage 2 partitioned by g, ordered by k."""
    from tabbot.views.calc_translate import OVER_P, OVER_PO
    p = plan_for(formula)
    assert not p.totals
    con = duckdb.connect()
    con.register("t", CELLS)
    stage1 = ", ".join(f'{a} AS "{p.agg_col(i)}"' for i, a in enumerate(p.aggregates))
    cells = con.execute(f"SELECT g, k, {stage1} FROM t GROUP BY g, k").df()
    con.register("c", cells)
    sql = p.sql.replace(OVER_PO, "PARTITION BY g ORDER BY k").replace(OVER_P, "PARTITION BY g")
    return [r[0] for r in con.execute(f"SELECT {sql} FROM c ORDER BY g, k").fetchall()]


def test_formula_table_calcs_run_in_duckdb():
    assert run_plan("RUNNING_SUM(SUM([V]))") == [5, 6, 9, 4, 8]
    assert run_plan("WINDOW_MAX([Total V])") == [5, 5, 5, 4, 4]
    assert run_plan("RANK(SUM([V]), 'asc')") == [3, 1, 2, 1, 1]
    assert run_plan("INDEX()") == [1, 2, 3, 1, 2]
    assert run_plan("SUM([V]) - LOOKUP(SUM([V]), -1)") == [None, -4, 2, None, 0]
    assert run_plan("ROUND(SUM([V]) / WINDOW_SUM(SUM([V])), 2)") == [0.56, 0.11, 0.33, 0.5, 0.5]


@pytest.mark.parametrize("formula, reason", [
    ("PREVIOUS_VALUE(0) + SUM([V])", "PREVIOUS_VALUE"),
    ("[V] / TOTAL(SUM([V]))", "not aggregated"),                       # a row-level field next to a table calc
    ("SUM(RUNNING_SUM(SUM([V])))", "SUM of a table calculation"),
    ("WINDOW_SUM(SUM([V]), -2, 0)", "WINDOW_SUM"),
])
def test_unsupported_table_calcs_are_reported(formula, reason):
    with pytest.raises(Unsupported, match=reason):
        plan_for(formula)
