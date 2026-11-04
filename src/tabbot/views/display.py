"""Shape rebuilt results for display: readable dimension values and Tableau-style crosstabs."""

from __future__ import annotations

import calendar

import pandas as pd

from tabbot.views.rebuild import DimSpec, MeasureSpec, RebuildResult


def fmt_dim(s: pd.Series, d: DimSpec, for_axis: bool = False) -> pd.Series:
    if d.kind == "date_part":
        if d.grain == "quarter":
            return s.map(lambda v: f"Q{int(v)}" if pd.notna(v) else "Null")
        if d.grain == "month":
            return s.map(lambda v: calendar.month_abbr[int(v)] if pd.notna(v) else "Null")
        if d.grain == "weekday":
            return s.map(lambda v: calendar.day_abbr[(int(v) - 2) % 7] if pd.notna(v) else "Null")
        return s.map(lambda v: str(int(v)) if pd.notna(v) else "Null")
    if d.kind == "date_trunc":
        if for_axis:
            return pd.to_datetime(s)
        ts = pd.to_datetime(s)
        if d.grain == "quarter":
            return ts.map(lambda t: f"{t.year} Q{t.quarter}" if pd.notna(t) else "Null")
        if d.grain == "month":
            return ts.dt.strftime("%Y-%m").fillna("Null")
        if d.grain == "year":
            return ts.dt.year.astype("Int64").astype(str).replace("<NA>", "Null")
        return ts.dt.strftime("%Y-%m-%d").fillna("Null")
    return s.map(lambda v: "Null" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))


def join_dims(df: pd.DataFrame, dims: list[DimSpec], sep: str) -> pd.Series:
    if not dims:
        return pd.Series(["All"] * len(df), index=df.index)
    parts = [fmt_dim(df[d.alias], d) for d in dims]
    out = parts[0]
    for p in parts[1:]:
        out = out + sep + p
    return out


def primary_measure(res: RebuildResult, *roles: str) -> MeasureSpec | None:
    for r in roles:
        m = res.measures_by_role(r)
        if m:
            return m[0]
    return res.measures[0] if res.measures else None


def pivot(res: RebuildResult) -> tuple[pd.DataFrame, MeasureSpec | None, list[str]]:
    """Rows dims x cols dims table of the main measure (what Tableau shows as a crosstab)."""
    notes: list[str] = []
    m = primary_measure(res, "color", "text", "label", "size", "rows", "cols")
    df = res.data
    if m is None or df.empty:
        return pd.DataFrame(), m, notes
    row_dims = res.dims_by_role("rows")
    col_dims = res.dims_by_role("cols")
    others = [d for d in res.dims if d not in row_dims and d not in col_dims]
    if others:  # detail/colour dimensions add rows in Tableau's crosstab
        row_dims = row_dims + others
    r = join_dims(df, row_dims, " | ")
    c = join_dims(df, col_dims, " ") if col_dims else pd.Series([m.label] * len(df), index=df.index)
    t = pd.DataFrame({"r": r, "c": c, "v": pd.to_numeric(df[m.alias], errors="coerce")})
    row_order = list(dict.fromkeys(r))
    col_order = list(dict.fromkeys(c))
    pv = t.pivot_table(index="r", columns="c", values="v", aggfunc="first")
    pv = pv.reindex(index=row_order, columns=col_order)
    pv.index.name = " | ".join(d.label for d in row_dims) or ""
    pv.columns.name = None
    return pv, m, notes
