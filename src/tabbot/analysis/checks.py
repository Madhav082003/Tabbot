"""Checks on one rebuilt chart: the ways a reader could be misled by it."""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel

from tabbot.analysis.recipes import AnalysisContext, partial_period
from tabbot.views.rebuild import RebuildResult

COLOUR_SKEW_RATIO = 10


class Check(BaseModel):
    id: str              # partial_period, double_count, colour_scale_skew, unparsed_dates, filter_members ...
    severity: str        # info | warn
    sheet: str
    message: str
    evidence: dict = {}


def chart_checks(res: RebuildResult, ctx: AnalysisContext | None) -> list[Check]:
    out: list[Check] = []
    c = res.checks or {}
    for col, n in (c.get("unparsed_dates") or {}).items():
        if n:
            out.append(Check(id="unparsed_dates", severity="warn", sheet=res.worksheet,
                             message=f"{n:,} values of {col} could not be read as dates and are missing from the "
                                     "time axis", evidence={"column": col, "values": n}))
    for f, n in (c.get("filter_members_not_in_data") or {}).items():
        out.append(Check(id="filter_members", severity="info", sheet=res.worksheet,
                         message=f"the filter on {f} lists {n} value(s) that do not exist in the data",
                         evidence={"field": f, "missing": n}))
    dt = c.get("distinct_total")
    if dt and not dt["cells_can_be_summed"]:
        out.append(Check(id="double_count", severity="warn", sheet=res.worksheet,
                         message=f"cells cannot be added up: they sum to {dt['sum_of_cells']:,} but there are "
                                 f"{dt['distinct_total']:,} distinct in total (one record appears in several cells, "
                                 f"about {dt['sum_of_cells'] / max(dt['distinct_total'], 1):.1f}× overstated)",
                         evidence=dt))
    if ctx is not None and res.time_dims():
        pp = partial_period(ctx)
        if pp:
            out.append(Check(id="partial_period", severity="warn", sheet=res.worksheet,
                             message=f"the last period ({pp['period']}) is incomplete: data ends {pp['data_ends']} "
                                     f"({pp['days_elapsed']} of {pp['days_in_period']} days), so it looks lower than "
                                     f"it is ({pp['value']:,.0f} so far ≈ {pp['full_period_pace']:,.0f} at full pace)",
                             evidence=pp))
    if res.chart == "heatmap" and res.measures and not res.data.empty:
        v = pd.to_numeric(res.data[res.measures[0].alias], errors="coerce")
        nz = v[v > 0]
        if len(nz) >= 5 and nz.median() > 0 and nz.max() / nz.median() > COLOUR_SKEW_RATIO:
            out.append(Check(id="colour_scale_skew", severity="info", sheet=res.worksheet,
                             message=f"the colour scale is stretched by large cells (max {nz.max():,.0f} vs median "
                                     f"{nz.median():,.0f}); small cells look empty even when they are not",
                             evidence={"max": float(nz.max()), "median": float(nz.median())}))
    if res.truncated:
        out.append(Check(id="truncated", severity="info", sheet=res.worksheet,
                         message="the view is very large; only part of it is shown"))
    return out
