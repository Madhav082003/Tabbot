"""Table calculations on a rebuilt view: computed on the chart's cells, the way Tableau does.

A table calculation (percent of total, running total, rank, difference, or a formula using TOTAL/RUNNING_SUM/
WINDOW_*/RANK/INDEX/LOOKUP) is computed after the normal aggregation, over the cells of the view:

- partition = the dimensions where it restarts; addressing = the dimensions it runs along. They come from the
  sheet's <table-calc ordering-type>: Rows = "Table (across)" (restarts on every row: partition = the dimensions on
  Rows), Columns = "Table (down)" (partition = the dimensions on Columns), Field = runs along the named fields.
  Mapping verified on the 60-workbook corpus (sheets with dimensions only on Rows use 'Columns', and vice versa).
  Marks-card dimensions (colour, detail ...) are addressed, so a pie's percent of total is each slice's share.
- TOTAL(x) re-aggregates x over the whole partition from the data (so TOTAL(COUNTD(...)) is a true distinct count,
  as in Tableau), not by adding up cells.

Anything outside this (other compute-along settings, unsupported functions) raises Unsupported; the caller then
shows the underlying aggregate and says the table calculation was not computed.
"""

from __future__ import annotations

import re

import duckdb
import pandas as pd

from tabbot.twb.model import TableCalcSpec
from tabbot.views.calc_translate import OVER_P, OVER_PO, TableCalcPlan, Unsupported

QUICK = {"pcto": "percent of total (a fraction: 0.25 = 25%)", "cum": "running total", "rank": "rank",
         "diff": "difference from the previous", "pcdf": "percent difference from the previous"}
ALONG = {"Rows": "across the table (restarting on every row)", "Columns": "down the table (restarting in every column)"}


def quick_plan(kind: str, base_sql: str, prefix: str) -> TableCalcPlan:
    """A quick table calculation (pcto:sum:Sales:qk ...) over one aggregate, as a two-stage plan."""
    if kind not in QUICK:
        raise Unsupported(f"quick table calculation '{kind}'")
    p = TableCalcPlan(prefix=prefix, aggregates=[base_sql], functions=[kind])
    a = f'"{p.agg_col(0)}"'
    prev = f"LAG({a}) OVER ({OVER_PO})"
    if kind == "pcto":
        p.totals = [a]
        p.sql = f'({a} / NULLIF("{p.total_col(0)}", 0))'
    elif kind == "cum":
        p.sql = f"SUM({a}) OVER ({OVER_PO} ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
    elif kind == "rank":
        p.sql = f"RANK() OVER ({OVER_P} ORDER BY {a} DESC NULLS LAST)"
    elif kind == "diff":
        p.sql = f"({a} - {prev})"
    else:
        p.sql = f"(({a} - {prev}) / NULLIF(ABS({prev}), 0))"
    p.order_dependent = kind in ("cum", "diff", "pcdf")
    return p


def split_dims(spec: TableCalcSpec | None, dims: list) -> tuple[list, list, str]:
    """(partition dims, addressing dims, plain description) for one table calculation."""
    ot = (spec.ordering_type if spec else None) or "Rows"        # Tableau's default: Table (across)
    if ot == "Rows":
        part = [d for d in dims if d.role == "rows"]
    elif ot == "Columns":
        part = [d for d in dims if d.role == "cols"]
    elif ot == "Field":
        fields = set(spec.ordering_fields if spec else [])
        addr = [d for d in dims if d.ref.field in fields]
        if not addr:
            raise Unsupported("it runs along a field that is not on this view")
        part = [d for d in dims if d not in addr]
    else:
        raise Unsupported(f"compute-along setting '{ot}'")
    addr = [d for d in dims if d not in part]
    if ot == "Field":
        how = "along " + ", ".join(d.label for d in addr)
    elif not part:
        how = "over the whole view"
    else:
        how = ALONG[ot]
    return part, addr, how


def apply(plan: TableCalcPlan, alias: str, df: pd.DataFrame, part: list, addr: list,
          totals_df: pd.DataFrame | None) -> pd.DataFrame:
    """Add column `alias` (the table calculation) to the cells and drop the plan's helper columns."""
    p_sql = ("PARTITION BY " + ", ".join(d.alias for d in part)) if part else ""
    o_sql = ("ORDER BY " + ", ".join(f"{d.alias} NULLS LAST" for d in addr)) if addr else ""
    con = duckdb.connect(":memory:")
    try:
        cells = df.reset_index(drop=True).copy()
        cells["__rn"] = range(len(cells))
        con.register("cells", cells)
        src = "cells"
        if plan.totals:
            con.register("tot", totals_df)
            on = " AND ".join(f"c.{d.alias} IS NOT DISTINCT FROM t.{d.alias}" for d in part) or "TRUE"
            tot_cols = []
            for j, arg in enumerate(plan.totals):
                expr = re.sub(rf'"({re.escape(plan.prefix)}__a\d+)"', r't."\1"', arg)
                tot_cols.append(f'({expr}) AS "{plan.total_col(j)}"')
            con.execute(f"CREATE TEMP TABLE c2 AS SELECT c.*, {', '.join(tot_cols)} FROM cells c "
                        f"LEFT JOIN tot t ON {on}")
            src = "c2"
        sql = plan.sql.replace(OVER_PO, f"{p_sql} {o_sql}".strip()).replace(OVER_P, p_sql)
        out = con.execute(f"SELECT *, ({sql}) AS {alias} FROM {src} ORDER BY __rn").df()
    finally:
        con.close()
    helpers = [c for c in out.columns if c == "__rn" or c.startswith(f"{plan.prefix}__")]
    return out.drop(columns=helpers)


def totals_sql(plan: TableCalcPlan, part: list, view: str, where: list[str]) -> str:
    """The plan's aggregates re-computed once per partition, straight from the data (for TOTAL)."""
    sel = [f"{d.sql} AS {d.alias}" for d in part] + [f'{a} AS "{plan.agg_col(k)}"'
                                                     for k, a in enumerate(plan.aggregates)]
    sql = f"SELECT {', '.join(sel)} FROM {view}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if part:
        sql += " GROUP BY " + ", ".join(d.alias for d in part)
    return sql
