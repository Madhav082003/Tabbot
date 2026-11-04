"""Standard analyses on a rebuilt view, generalised from the manual workflow.

All recipes reuse the worksheet's own definition (its measure, its filters, its time field) via an
AnalysisContext, and return a small AnalysisResult (table + summary + notes). They work for any workbook:
the measure is whatever the sheet aggregates (COUNT DISTINCT of an id, SUM of sales ...), the time field is the
sheet's date (or the first per-entity date column), the category is the sheet's main dimension.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from tabbot.data.profile import TableProfile
from tabbot.data.store import Store, qi
from tabbot.views.display import primary_measure
from tabbot.views.rebuild import RebuildResult

GRAIN_ORDER = ["day", "week", "month", "quarter", "year"]
PART_TO_GRAIN = {"year": "year", "quarter": "quarter", "month": "month", "week": "week", "day": "day"}
PARTIAL_TOLERANCE_DAYS = 7
DRIVER_MAX_VALUES = 50
TOPN_VALUES = 10


@dataclass
class AnalysisContext:
    store: Store
    view: str
    where: list[str]
    params: list
    measure_sql: str
    measure_label: str
    date_sql: str | None
    date_label: str | None
    grain: str
    category_sql: str | None
    category_label: str | None
    entity_sql: str | None
    entity_label: str | None
    profile: TableProfile | None
    notes: list[str] = field(default_factory=list)

    def clause(self, extra: list[str] | None = None) -> str:
        parts = list(self.where) + list(extra or [])
        return (" WHERE " + " AND ".join(parts)) if parts else ""

    def period_sql(self, grain: str | None = None) -> str:
        return f"CAST(date_trunc('{grain or self.grain}', {self.date_sql}) AS DATE)"


@dataclass
class AnalysisResult:
    name: str
    title: str
    table: pd.DataFrame = field(default_factory=pd.DataFrame)
    summary: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ok: bool = True


# --------------------------------------------------------------------------------------------- context


def context_for(res: RebuildResult, store: Store, profile: TableProfile | None,
                category: str | None = None) -> AnalysisContext:
    """Build the analysis context from a rebuilt worksheet. `category` = a dimension label to use instead."""
    m = primary_measure(res, "color", "text", "rows", "cols", "size", "label")
    if m is None:
        raise ValueError("this view has no measure to analyse")
    notes: list[str] = []
    tdims = res.time_dims()
    date_sql = date_label = None
    grain = "quarter"
    if tdims:
        date_sql, date_label = tdims[0].date_sql, tdims[0].ref.caption or tdims[0].ref.field
        grains = [PART_TO_GRAIN.get(d.grain or "", "quarter") for d in tdims]
        grain = min(grains, key=GRAIN_ORDER.index)
        if grain in ("day", "week"):
            grain = "month"
    elif profile is not None:
        cand = [c for c in profile.columns if c.kind in ("date", "text_date") and c.entity_level is not False]
        if cand:
            c = cand[0]
            date_sql = (f"CAST(try_strptime({qi(c.name)}, '{c.date_format.format}') AS TIMESTAMP)"
                        if c.date_format else f"CAST({qi(c.name)} AS TIMESTAMP)")
            date_label = c.name
            notes.append(f"the view has no time axis; time comes from {c.name}")
    cats = [d for d in res.dims if not d.date_sql]
    if category:
        cats = [d for d in cats if d.label == category] or cats
    else:
        cats = sorted(cats, key=lambda d: {"rows": 0, "color": 1, "cols": 2}.get(d.role, 3))
    cat = cats[0] if cats else None
    ent = profile.entity if profile is not None else None
    return AnalysisContext(
        store=store, view=res.view, where=list(res.where_sql), params=list(res.where_params),
        measure_sql=m.sql, measure_label=m.label, date_sql=date_sql, date_label=date_label, grain=grain,
        category_sql=cat.sql if cat else None, category_label=cat.label if cat else None,
        entity_sql=qi(ent.field) if ent else None, entity_label=ent.field if ent else None,
        profile=profile, notes=notes)


# --------------------------------------------------------------------------------------------- periods


def _period_end(start: dt.date, grain: str) -> dt.date:
    """Last day of the period that starts at `start`."""
    if grain == "year":
        return dt.date(start.year, 12, 31)
    months = {"quarter": 3, "month": 1}.get(grain)
    if months:
        m = start.month - 1 + months
        nxt = dt.date(start.year + m // 12, m % 12 + 1, 1)
        return nxt - dt.timedelta(days=1)
    if grain == "week":
        return start + dt.timedelta(days=6)
    return start


def _label(p: dt.date, grain: str) -> str:
    if grain == "quarter":
        return f"{p.year} Q{(p.month - 1) // 3 + 1}"
    if grain == "month":
        return f"{p.year}-{p.month:02d}"
    if grain == "year":
        return str(p.year)
    return p.isoformat()


def series(ctx: AnalysisContext, extra: list[str] | None = None, extra_params: list | None = None) -> pd.DataFrame:
    """Measure per period: columns period (date), label, value."""
    if ctx.date_sql is None:
        return pd.DataFrame(columns=["period", "label", "value"])
    p = ctx.period_sql()
    df = ctx.store.df(f"SELECT {p} AS period, {ctx.measure_sql} AS value FROM {ctx.view}"
                      f"{ctx.clause([f'{ctx.date_sql} IS NOT NULL'] + (extra or []))} GROUP BY 1 ORDER BY 1",
                      ctx.params + (extra_params or []))
    df["period"] = pd.to_datetime(df["period"]).dt.date
    df["label"] = [_label(x, ctx.grain) for x in df["period"]]
    return df[["period", "label", "value"]]


def data_end(ctx: AnalysisContext) -> dt.date | None:
    if ctx.date_sql is None:
        return None
    v = ctx.store.scalar(f"SELECT CAST(MAX({ctx.date_sql}) AS DATE) FROM {ctx.view}{ctx.clause()}", ctx.params)
    return v


def partial_period(ctx: AnalysisContext, s: pd.DataFrame | None = None) -> dict | None:
    """Is the last period incomplete? If so, its value at full-period pace."""
    s = series(ctx) if s is None else s
    end = data_end(ctx)
    if s.empty or end is None:
        return None
    last = s["period"].iloc[-1]
    pend = _period_end(last, ctx.grain)
    if end >= pend - dt.timedelta(days=PARTIAL_TOLERANCE_DAYS):
        return None
    elapsed = (end - last).days + 1
    total = (pend - last).days + 1
    value = float(s["value"].iloc[-1])
    return {"period": _label(last, ctx.grain), "data_ends": end.isoformat(), "days_elapsed": elapsed,
            "days_in_period": total, "value": value, "full_period_pace": round(value / elapsed * total, 1)}


def full_periods(ctx: AnalysisContext, s: pd.DataFrame | None = None) -> pd.DataFrame:
    s = series(ctx) if s is None else s
    if partial_period(ctx, s) is not None:
        s = s.iloc[:-1]
    return s


def _window(ctx: AnalysisContext, start: dt.date, end_excl: dt.date) -> tuple[str, list]:
    return f"{ctx.date_sql} >= CAST(? AS TIMESTAMP) AND {ctx.date_sql} < CAST(? AS TIMESTAMP)", \
        [start.isoformat(), end_excl.isoformat()]


def _next(p: dt.date, grain: str) -> dt.date:
    return _period_end(p, grain) + dt.timedelta(days=1)


# --------------------------------------------------------------------------------------------- recipes


def trend(ctx: AnalysisContext) -> AnalysisResult:
    s = series(ctx)
    r = AnalysisResult("trend", f"{ctx.measure_label} per {ctx.grain}", table=s.rename(columns={"value": "value"}))
    if s.empty:
        r.ok, r.notes = False, ["no time axis to build a trend on"]
        return r
    pp = partial_period(ctx, s)
    full = s.iloc[:-1] if pp else s
    r.summary = {"periods": len(s), "first": s["label"].iloc[0], "last": s["label"].iloc[-1],
                 "peak": {"period": full.loc[full["value"].idxmax(), "label"], "value": float(full["value"].max())}
                 if not full.empty else None, "partial_last_period": pp}
    if pp:
        r.notes.append(f"{pp['period']} is incomplete: data ends {pp['data_ends']} ({pp['days_elapsed']} of "
                       f"{pp['days_in_period']} days); {pp['value']:,.0f} so far ≈ {pp['full_period_pace']:,.0f} "
                       f"at full-{ctx.grain} pace")
    return r


def windows_last_vs_previous(ctx: AnalysisContext, n: int = 4) -> tuple[tuple, tuple] | None:
    full = full_periods(ctx)
    if len(full) < 2 * n:
        n = len(full) // 2
    if n < 1:
        return None
    ps = list(full["period"])
    last, prev = ps[-n:], ps[-2 * n:-n]
    return (prev[0], _next(prev[-1], ctx.grain), _label(prev[0], ctx.grain), _label(prev[-1], ctx.grain)), \
        (last[0], _next(last[-1], ctx.grain), _label(last[0], ctx.grain), _label(last[-1], ctx.grain))


def _by_member(ctx: AnalysisContext, member_sql: str, win, extra: list[str] | None = None,
               extra_params: list | None = None) -> pd.Series:
    w, wp = _window(ctx, win[0], win[1])
    df = ctx.store.df(f"SELECT CAST({member_sql} AS VARCHAR) AS k, {ctx.measure_sql} AS v FROM {ctx.view}"
                      f"{ctx.clause([w] + (extra or []))} GROUP BY 1", ctx.params + wp + (extra_params or []))
    df["k"] = df["k"].fillna("Null")
    return df.set_index("k")["v"].astype(float)


def _total(ctx: AnalysisContext, win, extra: list[str] | None = None, extra_params: list | None = None) -> float:
    w, wp = _window(ctx, win[0], win[1])
    v = ctx.store.scalar(f"SELECT {ctx.measure_sql} FROM {ctx.view}{ctx.clause([w] + (extra or []))}",
                         ctx.params + wp + (extra_params or []))
    return float(v or 0)


def period_compare(ctx: AnalysisContext, n: int = 4, windows=None) -> AnalysisResult:
    if ctx.date_sql is None:
        return AnalysisResult("period_compare", "Period comparison", ok=False, notes=["no time field"])
    wins = windows or windows_last_vs_previous(ctx, n)
    if wins is None:
        return AnalysisResult("period_compare", "Period comparison", ok=False, notes=["not enough full periods"])
    prev, last = wins
    title = f"{ctx.measure_label}: {last[2]}–{last[3]} vs {prev[2]}–{prev[3]}"
    r = AnalysisResult("period_compare", title)
    tp, tl = _total(ctx, prev), _total(ctx, last)
    r.summary = {"previous_window": f"{prev[2]}–{prev[3]}", "last_window": f"{last[2]}–{last[3]}",
                 "previous": tp, "last": tl, "change": tl - tp,
                 "change_pct": round((tl - tp) / tp * 100, 1) if tp else None}
    if ctx.category_sql:
        a, b = _by_member(ctx, ctx.category_sql, prev), _by_member(ctx, ctx.category_sql, last)
        t = pd.DataFrame({"previous": a, "last": b}).fillna(0)
        t["change"] = t["last"] - t["previous"]
        t["change_pct"] = (t["change"] / t["previous"].where(t["previous"] > 0) * 100).round(1)
        t = t.sort_values("change", ascending=False)
        t.index.name = ctx.category_label
        r.table = t.reset_index()
        r.summary["top_gainers"] = t.head(3).reset_index().to_dict("records")
        r.summary["top_decliners"] = t[t["change"] < 0].tail(3).reset_index().to_dict("records")
    r.notes.append("windows use full periods only; a value counted per window counts each entity once per window")
    return r


def change_point(ctx: AnalysisContext, min_side: int = 2) -> AnalysisResult:
    full = full_periods(ctx)
    r = AnalysisResult("change_point", "Where the level changed")
    if len(full) < 2 * min_side + 1:
        r.ok, r.notes = False, ["not enough full periods"]
        return r
    v = full["value"].astype(float).tolist()
    best = None
    for k in range(min_side, len(v) - min_side + 1):
        before, after = v[:k], v[k:]
        mb, ma = sum(before) / len(before), sum(after) / len(after)
        score = abs(ma - mb) / max(mb, 1e-9)
        # prefer the most recent strong shift: weigh by how consistent the after-part is
        if best is None or score > best[0]:
            best = (score, k, mb, ma)
    score, k, mb, ma = best
    r.summary = {"change_at": full["label"].iloc[k], "mean_before": round(mb, 1), "mean_after": round(ma, 1),
                 "ratio": round(ma / mb, 2) if mb else None, "periods_before": k, "periods_after": len(v) - k}
    r.table = full.assign(segment=["before"] * k + ["after"] * (len(v) - k))
    return r


def driver_scan(ctx: AnalysisContext, windows=None, focus: str | None = None, top: int = 5) -> AnalysisResult:
    """Which other fields explain the change between two windows (optionally inside one category member)."""
    r = AnalysisResult("driver_scan", "What drove the change")
    if ctx.date_sql is None or ctx.profile is None:
        r.ok, r.notes = False, ["needs a time field and a profiled table"]
        return r
    wins = windows or windows_last_vs_previous(ctx)
    if wins is None:
        r.ok, r.notes = False, ["not enough full periods"]
        return r
    prev, last = wins
    extra, extra_p = [], []
    if focus is None and ctx.category_sql:
        pc = period_compare(ctx, windows=wins)
        if not pc.table.empty:
            col = pc.table.columns[0]
            row = pc.table.iloc[pc.table["change"].abs().argmax()]
            focus = str(row[col])
    if focus is not None and ctx.category_sql:
        extra, extra_p = [f"CAST({ctx.category_sql} AS VARCHAR) = ?"], [focus]
    total_prev = _total(ctx, prev, extra, extra_p)
    total_change = _total(ctx, last, extra, extra_p) - total_prev
    r.summary = {"focus": focus, "focus_field": ctx.category_label if focus else None,
                 "previous_window": f"{prev[2]}–{prev[3]}", "last_window": f"{last[2]}–{last[3]}",
                 "total_change": total_change}
    if total_change == 0:
        r.notes.append("no change between the windows")
        return r
    skip = {ctx.entity_label}
    rows = []
    for c in ctx.profile.columns:
        if c.name in skip or c.sensitive_reasons or c.kind not in ("categorical", "high_cardinality"):
            continue
        if c.kind == "categorical" and not (2 <= c.distinct <= DRIVER_MAX_VALUES):
            continue
        if ctx.category_sql and qi(c.name) == ctx.category_sql:
            continue
        a = _by_member(ctx, qi(c.name), prev, extra, extra_p)
        b = _by_member(ctx, qi(c.name), last, extra, extra_p)
        t = pd.DataFrame({"previous": a, "last": b}).fillna(0)
        t["change"] = t["last"] - t["previous"]
        t["share_of_change"] = t["change"] / total_change
        t["baseline_share"] = t["previous"] / max(total_prev, 1e-9)
        # a driver takes a much bigger part of the change than its usual part of the total
        t["excess"] = t["share_of_change"] - t["baseline_share"]
        t = t.sort_values("excess", ascending=False)
        k, best = t.index[0], t.iloc[0]
        rows.append({"field": c.name, "value": str(k), "previous": float(best["previous"]),
                     "last": float(best["last"]), "change": float(best["change"]),
                     "share_of_change": round(float(best["share_of_change"]), 3),
                     "baseline_share": round(float(best["baseline_share"]), 3),
                     "excess": round(float(best["excess"]), 3),
                     "values": int(c.distinct), "per_entity": bool(c.entity_level),
                     "next": [{"value": str(i), "change": float(t.loc[i, "change"])} for i in t.index[1:3]],
                     "mode": "top-N" if c.kind == "high_cardinality" else "all values"})
    rows.sort(key=lambda x: -x["excess"])
    r.table = pd.DataFrame(rows[:top])
    if rows:
        best = rows[0]
        r.summary["top_driver"] = {k: best[k] for k in ("field", "value", "previous", "last", "change",
                                                        "share_of_change", "baseline_share", "excess")}
    r.notes.append("ranked by excess = share of the change minus usual share of the total (a value that is always "
                   "big is not a driver); for fields that vary within an entity (per_entity = False) one entity can "
                   "count under several values, so shares can exceed 100%")
    return r


def share_shift(ctx: AnalysisContext, k: int = 5) -> AnalysisResult:
    r = AnalysisResult("share_shift", f"Top {k} {ctx.category_label or ''} by share of each year")
    if ctx.date_sql is None or ctx.category_sql is None:
        r.ok, r.notes = False, ["needs a time field and a category"]
        return r
    y = f"year({ctx.date_sql})"
    by = ctx.store.df(f"SELECT {y} AS year, CAST({ctx.category_sql} AS VARCHAR) AS member, {ctx.measure_sql} AS v "
                      f"FROM {ctx.view}{ctx.clause([f'{ctx.date_sql} IS NOT NULL'])} GROUP BY 1, 2", ctx.params)
    tot = ctx.store.df(f"SELECT {y} AS year, {ctx.measure_sql} AS total FROM {ctx.view}"
                       f"{ctx.clause([f'{ctx.date_sql} IS NOT NULL'])} GROUP BY 1", ctx.params)
    t = by.merge(tot, on="year")
    t["share"] = (t["v"] / t["total"]).round(3)
    t = t.sort_values(["year", "v"], ascending=[True, False]).groupby("year").head(k)
    r.table = t.rename(columns={"v": "value"}).reset_index(drop=True)
    years = sorted(t["year"].unique())
    if len(years) >= 2:
        first, lastyr = t[t["year"] == years[0]], t[t["year"] == years[-1]]
        moves = []
        for m in set(first["member"]) | set(lastyr["member"]):
            a = first.loc[first["member"] == m, "share"]
            b = lastyr.loc[lastyr["member"] == m, "share"]
            if len(a) and len(b):
                moves.append({"member": m, "first_share": float(a.iloc[0]), "last_share": float(b.iloc[0]),
                              "change": round(float(b.iloc[0] - a.iloc[0]), 3)})
        r.summary = {"first_year": int(years[0]), "last_year": int(years[-1]),
                     "biggest_moves": sorted(moves, key=lambda x: -abs(x["change"]))[:3]}
    r.notes.append("shares can fall because each record lists fewer categories, not because events got rarer")
    return r


def concentration(ctx: AnalysisContext) -> AnalysisResult:
    r = AnalysisResult("concentration", f"How concentrated {ctx.category_label or 'the view'} is")
    if ctx.category_sql is None:
        r.ok, r.notes = False, ["needs a category"]
        return r
    t = ctx.store.df(f"SELECT CAST({ctx.category_sql} AS VARCHAR) AS member, {ctx.measure_sql} AS value "
                     f"FROM {ctx.view}{ctx.clause()} GROUP BY 1 ORDER BY 2 DESC", ctx.params)
    total = float(ctx.store.scalar(f"SELECT {ctx.measure_sql} FROM {ctx.view}{ctx.clause()}", ctx.params) or 0)
    t["share_of_total"] = (t["value"] / total).round(3) if total else None
    threshold = min(0.01 * total, 60)
    sparse = t[t["value"] <= threshold]
    r.table = t
    r.summary = {"members": len(t), "total": total, "top": t.head(4).to_dict("records"),
                 "sparse_threshold": threshold, "sparse_members": len(sparse),
                 "sparse_examples": sparse["member"].head(5).tolist()}
    if len(sparse):
        r.notes.append(f"{len(sparse)} member(s) have ≤ {threshold:,.0f}: period-by-period reading of them is noise")
    return r


def multiplicity(ctx: AnalysisContext) -> AnalysisResult:
    """Average distinct category values per entity, by year of the entity's first date (e.g. events per case)."""
    r = AnalysisResult("multiplicity", f"{ctx.category_label} per {ctx.entity_label}")
    if not (ctx.entity_sql and ctx.category_sql and ctx.date_sql):
        r.ok, r.notes = False, ["needs an entity, a category and a time field"]
        return r
    t = ctx.store.df(f"SELECT year(first) AS year, AVG(n) AS avg_per_entity, COUNT(*) AS entities FROM ("
                     f"SELECT {ctx.entity_sql}, MIN({ctx.date_sql}) AS first, COUNT(DISTINCT {ctx.category_sql}) AS n "
                     f"FROM {ctx.view}{ctx.clause()} GROUP BY 1) s WHERE first IS NOT NULL GROUP BY 1 ORDER BY 1",
                     ctx.params)
    t["avg_per_entity"] = t["avg_per_entity"].round(2)
    r.table = t
    if not t.empty and (t["avg_per_entity"] == 1).all():
        r.ok, r.notes = False, [f"each {ctx.entity_label} has exactly one {ctx.category_label}"]
        return r
    if len(t) >= 2:
        r.summary = {"first_year": int(t["year"].iloc[0]), "first": float(t["avg_per_entity"].iloc[0]),
                     "last_year": int(t["year"].iloc[-1]), "last": float(t["avg_per_entity"].iloc[-1])}
    return r


def flag_share(ctx: AnalysisContext, field_name: str, value: str) -> AnalysisResult:
    """Share of entities with at least one row where field = value, by year of first date."""
    r = AnalysisResult("flag_share", f"Share of {ctx.entity_label} with {field_name} = {value}")
    if not (ctx.entity_sql and ctx.date_sql):
        r.ok, r.notes = False, ["needs an entity and a time field"]
        return r
    f = qi(field_name)
    t = ctx.store.df(f"SELECT year(first) AS year, AVG(CAST(flag AS DOUBLE)) AS share, COUNT(*) AS entities FROM ("
                     f"SELECT {ctx.entity_sql}, MIN({ctx.date_sql}) AS first, "
                     f"BOOL_OR(CAST({f} AS VARCHAR) = ?) AS flag FROM {ctx.view}{ctx.clause()} GROUP BY 1) s "
                     f"WHERE first IS NOT NULL GROUP BY 1 ORDER BY 1", [value] + ctx.params)
    t["share"] = t["share"].round(3)
    r.table = t
    return r


def run_standard(ctx: AnalysisContext) -> dict[str, AnalysisResult]:
    """The default analysis pack for a dashboard's main sheet (what the manual analysis did)."""
    out = {"trend": trend(ctx), "period_compare": period_compare(ctx), "change_point": change_point(ctx)}
    cp = out["change_point"]
    out["concentration"] = concentration(ctx)
    out["share_shift"] = share_shift(ctx)
    out["multiplicity"] = multiplicity(ctx)
    out["driver_scan"] = driver_scan(ctx)
    if cp.ok and cp.summary.get("periods_after", 0) >= 1:
        out["change_point"].notes.append("compare before/after with period_compare for the members that moved")
    return out
