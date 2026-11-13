"""Rebuild a worksheet's numbers from its own definition (shelves, marks, filters, calcs) on the local extract.

Definition -> SQL:  dimensions (rows/cols/colour/detail ...) -> GROUP BY,  measures (ctd/sum/avg/usr ...) ->
aggregates,  filters -> WHERE / HAVING,  date parts -> year()/quarter()/...,  calcs -> calc_translate.
Every result says whether it is "exact" (every part understood) or "approximate" (with reasons), plus
sanity checks. We never present a redraw as verified; that needs a comparison with Tableau (views/compare.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from tabbot.data.profile import TableProfile
from tabbot.data.store import Store, qi
from tabbot.twb.fieldref import FieldRef, parse_ref
from tabbot.twb.model import Filter, TableCalcSpec, WorkbookModel, Worksheet
from tabbot.views import table_calc as tcalc
from tabbot.views.calc_translate import (
    Expr,
    TableCalcPlan,
    Unsupported,
    _to_date,
    has_table_calc,
    translate,
    translate_table_calc,
)

MAX_ROWS = 20_000
DIM_ROLES = ("rows", "cols", "color", "lod", "detail", "shape", "size", "text", "label", "tooltip", "path")
PART_SQL = {"year": "year", "quarter": "quarter", "month": "month", "week": "week", "day": "day",
            "weekday": "dayofweek", "hour": "hour", "minute": "minute", "second": "second"}
TRUNC_DATE = {"year", "quarter", "month", "week", "day"}
NULL_MEMBER = "%null%"


@dataclass
class DimSpec:
    alias: str
    label: str
    role: str
    kind: str                 # dimension | date_part | date_trunc
    grain: str | None
    ref: FieldRef
    sql: str
    date_sql: str | None = None   # underlying date (TIMESTAMP) expression for date parts/truncations


@dataclass
class MeasureSpec:
    alias: str
    label: str
    roles: list[str]
    aggregation: str | None
    ref: FieldRef
    sql: str                                  # a plain aggregate (for a table calculation: its first underlying one)
    plan: TableCalcPlan | None = None         # set when this measure is a table calculation
    spec: TableCalcSpec | None = None         # how it runs (compute along), from the sheet
    base_label: str = ""                      # label of `sql` when it differs from the measure (table calcs)

    @property
    def analysis_label(self) -> str:
        return self.base_label or self.label


@dataclass
class RebuildResult:
    worksheet: str
    mark: str | None
    chart: str                               # heatmap | line | area | bar | pie | scatter | table | none
    dims: list[DimSpec] = field(default_factory=list)
    measures: list[MeasureSpec] = field(default_factory=list)
    data: pd.DataFrame = field(default_factory=pd.DataFrame)   # columns = aliases (d0.., m0..)
    status: str = "exact"                    # exact | approximate | failed
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    sql: str = ""
    truncated: bool = False
    title: str | None = None
    applied_quick_filters: dict[str, list[str]] = field(default_factory=dict)
    table_calcs: list[str] = field(default_factory=list)   # table calculations computed here, in plain words
    # what analyses need to re-query the same view: table view name, WHERE clauses and their parameters
    datasource: str | None = None
    view: str | None = None
    where_sql: list[str] = field(default_factory=list)
    where_params: list = field(default_factory=list)

    def time_dims(self) -> list[DimSpec]:
        return [d for d in self.dims if d.date_sql]

    def where_clause(self) -> str:
        return (" WHERE " + " AND ".join(self.where_sql)) if self.where_sql else ""

    def dims_by_role(self, *roles: str) -> list[DimSpec]:
        return [d for d in self.dims if d.role in roles]

    def measures_by_role(self, *roles: str) -> list[MeasureSpec]:
        return [m for m in self.measures if any(r in roles for r in m.roles)]

    def labelled(self) -> pd.DataFrame:
        """Data with readable column names."""
        names = {d.alias: d.label for d in self.dims} | {m.alias: m.label for m in self.measures}
        return self.data.rename(columns=names)


class _Ctx:
    """Field resolution for one data source/table."""

    def __init__(self, model: WorkbookModel, datasource: str | None, store: Store, table: str,
                 profile: TableProfile):
        self.model, self.ds, self.store, self.table, self.profile = model, datasource, store, table, profile
        self.view = store.view(table)
        self.columns = dict(store.columns(table))
        self.cache: dict[str, Expr] = {}
        self.stack: list[str] = []
        self.text_dates_used: set[str] = set()

    def resolve(self, name: str) -> Expr:
        if name in self.cache:
            return self.cache[name]
        if name in self.columns:
            dtype = self.columns[name].upper()
            col = self.profile.column(name) if self.profile else None
            fmt = col.date_format.format if col is not None and col.date_format else None
            if fmt:
                self.text_dates_used.add(name)
            e = Expr(qi(name), is_str=dtype.startswith("VARCHAR"), is_date=dtype.startswith(("DATE", "TIMESTAMP")),
                     text_date_format=fmt)
        else:
            f = self.model.field(self.ds, name) if self.ds else None
            if f is None:  # formulas sometimes use captions
                f = next((x for d in self.model.datasources if d.name == self.ds for x in d.fields
                          if x.caption == name), None)
            if f is None:
                raise Unsupported(f"field [{name}] is not in the extract")
            if f.is_parameter:
                raise Unsupported(f"parameter [{f.caption}]")
            if not f.is_calculated or not f.formula:
                if f.internal_name in self.columns:
                    return self.resolve(f.internal_name)
                raise Unsupported(f"field [{f.caption}] is not in the extract")
            if name in self.stack:
                raise Unsupported(f"circular calculation [{f.caption}]")
            self.stack.append(name)
            try:
                inner = translate(f.formula, self.resolve)
            except Unsupported as exc:
                raise Unsupported(f"calculation [{f.caption}]: {exc}") from None
            finally:
                self.stack.pop()
            e = Expr(f"({inner.sql})", is_agg=inner.is_agg, is_str=inner.is_str, is_date=inner.is_date)
        self.cache[name] = e
        return e

    # expression for a field reference with its derivation (no aggregation)
    def dim_sql(self, ref: FieldRef) -> tuple[str, str, str | None]:
        base = self.resolve(ref.field)
        if base.is_agg:
            raise Unsupported(f"[{ref.label}] is an aggregate used as a dimension")
        kind = ref.kind
        if kind == "dimension":
            return base.sql, "dimension", None
        if kind == "date_part":
            d = _to_date(base, "TIMESTAMP").sql
            part = ref.date_grain
            if part not in PART_SQL:
                raise Unsupported(f"date part {ref.derivation}")
            sql = f"{PART_SQL[part]}({d})" + (" + 1" if part == "weekday" else "")
            return sql, "date_part", part
        if kind == "date_trunc":
            d = _to_date(base, "TIMESTAMP").sql
            part = ref.date_grain
            sql = f"CAST(date_trunc('{part}', {d}) AS DATE)" if part in TRUNC_DATE else f"date_trunc('{part}', {d})"
            return sql, "date_trunc", part
        raise Unsupported(f"field reference {ref.raw} ({kind})")

    def calc(self, name: str):
        """The calculated field behind a reference (by internal name or caption), or None."""
        f = self.model.field(self.ds, name) if self.ds else None
        if f is None:
            f = next((x for d in self.model.datasources if d.name == self.ds for x in d.fields if x.caption == name),
                     None)
        return f if f is not None and f.is_calculated and f.formula else None

    def table_calc_plan(self, name: str, prefix: str) -> TableCalcPlan | None:
        """Two-stage plan for a calculated field that uses table calculation functions; None if it does not."""
        f = self.calc(name)
        if f is None or not has_table_calc(f.formula):
            return None
        if name in self.stack:
            raise Unsupported(f"circular calculation [{f.caption}]")
        self.stack.append(name)
        try:
            return translate_table_calc(f.formula, self.resolve, prefix)
        except Unsupported as exc:
            raise Unsupported(f"calculation [{f.caption}]: {exc}") from None
        finally:
            self.stack.pop()

    def date_sql(self, ref: FieldRef) -> str:
        """The underlying date of a date-part / date-trunc reference, as a TIMESTAMP expression."""
        return _to_date(self.resolve(ref.field), "TIMESTAMP").sql

    def measure_sql(self, ref: FieldRef) -> str:
        agg = ref.aggregation
        base = self.resolve(ref.field)
        if agg == "user_aggregate":
            if not base.is_agg:
                return f"SUM({base.sql})"
            return base.sql
        if base.is_agg:
            raise Unsupported(f"{ref.label}: aggregation applied to an already aggregated calculation")
        x = _to_date(base, "DATE").sql if base.text_date_format else base.sql
        return {
            "count_distinct": f"COUNT(DISTINCT {x})", "count": f"COUNT({x})", "sum": f"SUM({x})",
            "avg": f"AVG({x})", "min": f"MIN({x})", "max": f"MAX({x})", "median": f"MEDIAN({x})",
            "attr": f"CASE WHEN MIN({x}) = MAX({x}) THEN MIN({x}) END",
            "stdev": f"STDDEV_SAMP({x})", "var": f"VAR_SAMP({x})",
        }[agg]


def _member(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == "#" and v[-1] == "#":
        v = v[1:-1]
    return v


def _filter_clause(ctx: _Ctx, f: Filter, members_override: list[str] | None = None) -> tuple[str, list, bool]:
    """Return (sql, params, is_having)."""
    ref = f.field
    if ref.kind == "measure":
        expr, having = ctx.measure_sql(ref), True
    else:
        expr, _, _ = ctx.dim_sql(ref)
        having = False
    if members_override is not None:
        mode, members = "include", members_override
    else:
        mode, members = f.mode, f.members
    if mode in ("include", "exclude"):
        has_null = NULL_MEMBER in members
        vals = [_member(m) for m in members if m != NULL_MEMBER]
        txt = f"CAST({expr} AS VARCHAR)"
        ph = ", ".join("?" for _ in vals)
        if mode == "include":
            parts = ([f"{txt} IN ({ph})"] if vals else []) + ([f"{expr} IS NULL"] if has_null else [])
            return (f"({' OR '.join(parts)})" if parts else "FALSE"), vals, having
        if not vals:
            return (f"{expr} IS NOT NULL" if has_null else "TRUE"), [], having
        if has_null:
            return f"({expr} IS NOT NULL AND {txt} NOT IN ({ph}))", vals, having
        return f"({expr} IS NULL OR {txt} NOT IN ({ph}))", vals, having
    if mode == "range":
        parts, params = [], []
        for bound, op in ((f.min, ">="), (f.max, "<=")):
            if bound is None or bound == "":
                continue
            b = _member(bound)
            try:
                params.append(float(b))
                parts.append(f"{expr} {op} ?")
            except ValueError:
                params.append(b)
                parts.append(f"CAST({expr} AS VARCHAR) {op} ?" if not ref.date_grain and ref.kind == "dimension"
                             else f"{expr} {op} CAST(? AS TIMESTAMP)")
        return (" AND ".join(parts) or "TRUE"), params, having
    if mode == "all":
        return "TRUE", [], having
    raise Unsupported(f"filter on {ref.label} ({f.kind}/{f.raw_function})")


def _chart_kind(ws: Worksheet, dims: list[DimSpec], measures: list[MeasureSpec]) -> str:
    mark = (ws.mark or "").lower()
    shelf_measures = [m for m in measures if "rows" in m.roles or "cols" in m.roles]
    if not measures and not dims:
        return "none"
    if mark == "pie":
        return "pie"
    if mark in ("line",):
        return "line"
    if mark == "area":
        return "area"
    if mark in ("bar", "ganttbar"):
        return "bar"
    if mark in ("circle", "shape"):
        return "scatter" if len(shelf_measures) >= 2 else ("bar" if shelf_measures else "heatmap")
    if mark == "square":
        return "heatmap" if not shelf_measures else "bar"
    if mark == "automatic":
        if shelf_measures:
            time_axis = any(d.kind == "date_trunc" and d.role in ("rows", "cols") for d in dims)
            return "line" if time_axis else "bar"
        return "table"
    return "table"


def rebuild_worksheet(model: WorkbookModel, ws: Worksheet | str, store: Store | None, table: str | None,
                      profile: TableProfile | None,
                      quick_filters: dict[str, list[str]] | None = None) -> RebuildResult:
    """Rebuild one worksheet. `quick_filters` maps a field-ref raw string to the selected values (include)."""
    if isinstance(ws, str):
        ws = model.worksheet(ws)
    res = RebuildResult(worksheet=ws.name, mark=ws.mark, chart="none", title=ws.title)
    if store is None or table is None:
        res.status = "failed"
        res.reasons.append("no extract data for this worksheet's data source")
        return res
    ctx = _Ctx(model, ws.datasource, store, table, profile)
    res.reasons.extend(ws.unsupported)

    # ---- collect dimensions and measures by role (deduplicated by raw reference)
    placements: list[tuple[str, FieldRef]] = [("rows", r) for r in ws.rows] + [("cols", r) for r in ws.cols]
    for enc, refs in ws.encodings.items():
        placements += [(enc, r) for r in refs]
    dim_by_raw: dict[str, DimSpec] = {}
    meas_by_raw: dict[str, MeasureSpec] = {}
    for role, ref in placements:
        try:
            if ref.kind in ("measure", "table_calc"):
                if ref.raw in meas_by_raw:
                    meas_by_raw[ref.raw].roles.append(role)
                    continue
                if role == "tooltip":
                    continue  # tooltip-only measures do not change the view
                meas_by_raw[ref.raw] = _measure(ctx, ws, res, ref, role, f"m{len(meas_by_raw)}")
            elif ref.kind in ("dimension", "date_part", "date_trunc"):
                if ref.raw in dim_by_raw:
                    continue
                sql, kind, grain = ctx.dim_sql(ref)
                dim_by_raw[ref.raw] = DimSpec(alias=f"d{len(dim_by_raw)}", label=ref.label, role=role, kind=kind,
                                              grain=grain, ref=ref, sql=sql,
                                              date_sql=ctx.date_sql(ref) if kind != "dimension" else None)
            else:
                res.reasons.append(f"skipped {ref.label} ({ref.kind}) on {role}")
        except Unsupported as exc:
            res.reasons.append(f"{role}: {exc}")
    res.dims, res.measures = list(dim_by_raw.values()), list(meas_by_raw.values())
    res.chart = _chart_kind(ws, res.dims, res.measures)
    if not res.measures and not res.dims:
        res.status = "failed"
        res.reasons.append("nothing on the view could be rebuilt")
        return res

    # ---- filters (+ dashboard quick-filter selections, which replace the sheet's own filter on that field)
    qf = {k: v for k, v in (quick_filters or {}).items() if v}
    where, having, params_w, params_h = [], [], [], []
    seen = set()
    for f in ws.filters:
        override = None
        for raw, vals in qf.items():
            if raw == f.field.raw or (_same_field(raw, f.field)):
                override = vals
                seen.add(raw)
        if f.action_status is not None and override is None:
            continue      # click-driven (shows everything until a click) or a leftover with no effect
        try:
            sql, p, is_having = _filter_clause(ctx, f, override)
        except Unsupported as exc:
            res.reasons.append(f"filter ignored: {exc}")
            continue
        (having if is_having else where).append(sql)
        (params_h if is_having else params_w).extend(p)
    for raw, vals in qf.items():
        if raw in seen:
            continue
        ref = parse_ref(raw)
        if ref is None or (ws.datasource and ref.datasource != ws.datasource):
            continue
        try:
            sql, p, is_having = _filter_clause(ctx, Filter(field=ref, kind="categorical", mode="include"), vals)
        except Unsupported:
            continue
        (having if is_having else where).append(sql)
        (params_h if is_having else params_w).extend(p)
        res.applied_quick_filters[ref.label] = vals

    # ---- query
    sel = [f"{d.sql} AS {d.alias}" for d in res.dims]
    for m in res.measures:
        if m.plan is None:
            sel.append(f"{m.sql} AS {m.alias}")
        else:      # a table calculation: its underlying aggregates per cell; the calculation itself comes after
            sel += [f'{a} AS "{m.plan.agg_col(k)}"' for k, a in enumerate(m.plan.aggregates)]
    sql = f"SELECT {', '.join(sel)} FROM {ctx.view}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if res.dims and res.measures:
        sql += " GROUP BY " + ", ".join(d.alias for d in res.dims)
    elif res.dims:
        sql = sql.replace("SELECT ", "SELECT DISTINCT ", 1)
    if having:
        sql += " HAVING " + " AND ".join(having)
    if res.dims:
        sql += " ORDER BY " + ", ".join(f"{d.alias} NULLS LAST" for d in res.dims)
    sql += f" LIMIT {MAX_ROWS + 1}"
    res.sql = sql
    try:
        df = store.df(sql, params_w + params_h)
    except Exception as exc:  # noqa: BLE001 - surface the SQL problem as a failed rebuild
        res.status = "failed"
        res.reasons.append(f"query failed: {str(exc).splitlines()[0][:200]}")
        return res
    df = _table_calcs(ctx, ws, res, df, where, params_w, bool(having))
    if len(df) > MAX_ROWS:
        df, res.truncated = df.iloc[:MAX_ROWS], True
        res.reasons.append(f"view has more than {MAX_ROWS:,} cells; showing the first {MAX_ROWS:,}")
    res.data = df

    res.datasource, res.view, res.where_sql, res.where_params = ws.datasource, ctx.view, where, params_w

    # ---- sanity checks
    res.checks = _checks(ctx, ws, res, where, params_w)
    if res.reasons:
        res.status = "approximate"
    return res


def _measure(ctx: _Ctx, ws: Worksheet, res: RebuildResult, ref: FieldRef, role: str, alias: str) -> MeasureSpec:
    """One measure of the view. A table calculation gets a plan (computed on the cells after the query); a quick
    table calculation that cannot be computed shows its underlying aggregate and says so."""
    spec = ws.table_calc_specs.get(ref.raw)
    if ref.table_calc:
        base = ref.model_copy(update={"table_calc": None})
        base_sql = ctx.measure_sql(base)
        try:
            plan = tcalc.quick_plan(ref.table_calc, base_sql, alias)
        except Unsupported as exc:
            res.reasons.append(f"{role}: table calculation '{ref.label}' is not computed here ({exc}); the numbers "
                               f"shown are the underlying {base.label}")
            return MeasureSpec(alias=alias, label=f"{base.label} (before the table calculation)", roles=[role],
                               aggregation=base.aggregation, ref=ref, sql=base_sql)
        return MeasureSpec(alias=alias, label=ref.label, roles=[role], aggregation=None, ref=ref, sql=base_sql,
                           plan=plan, spec=spec, base_label=base.label)
    if ref.aggregation == "user_aggregate":
        plan = ctx.table_calc_plan(ref.field, alias)
        if plan is not None:
            return MeasureSpec(alias=alias, label=ref.label, roles=[role], aggregation=None, ref=ref,
                               sql=plan.aggregates[0] if plan.aggregates else "COUNT(*)", plan=plan, spec=spec,
                               base_label=f"the value {ref.label} is calculated from")
    return MeasureSpec(alias=alias, label=ref.label, roles=[role], aggregation=ref.aggregation, ref=ref,
                       sql=ctx.measure_sql(ref))


def _table_calcs(ctx: _Ctx, ws: Worksheet, res: RebuildResult, df: pd.DataFrame, where: list[str], params: list,
                 has_having: bool) -> pd.DataFrame:
    """Compute every table calculation on the cells. One that cannot be computed shows its first underlying
    aggregate instead, with a reason saying so (the analyst can still work from those numbers)."""
    for m in res.measures:
        if m.plan is None:
            continue
        what = (tcalc.QUICK.get(m.ref.table_calc) if m.ref.table_calc
                else "a table calculation using " + ", ".join(dict.fromkeys(m.plan.functions)))
        try:
            part, addr, how = tcalc.split_dims(m.spec, res.dims)
            tot = ctx.store.df(tcalc.totals_sql(m.plan, part, ctx.view, where), params) if m.plan.totals else None
            df = tcalc.apply(m.plan, m.alias, df, part, addr, tot)
        except Exception as exc:  # noqa: BLE001 - Unsupported or a DuckDB error: fall back, never fail the view
            res.reasons.append(f"table calculation '{m.label}' could not be computed here "
                               f"({str(exc).splitlines()[0][:160]}); the numbers shown are the value it is "
                               "calculated from, before the table calculation")
            df = df.rename(columns={m.plan.agg_col(0): m.alias})
            df = df.drop(columns=[c for c in df.columns if c.startswith(f"{m.alias}__")])
            if m.alias not in df.columns:            # e.g. INDEX(): nothing underneath to show
                df[m.alias] = None
            m.label = f"{m.analysis_label} (before the table calculation)"
            m.plan = None
            continue
        res.table_calcs.append(f"{m.label}: {what}, computed here {how}")
        if m.plan.order_dependent and ws.sort_count:
            res.reasons.append(f"'{m.label}' depends on the order of the marks; it was computed in ascending order "
                               "of the dimensions, and this sheet has its own sort, so Tableau may order differently")
        if m.plan.totals and has_having:
            res.reasons.append(f"'{m.label}': its total is computed before this sheet's measure filters")
    return df


def _same_field(raw: str, ref: FieldRef) -> bool:
    r = parse_ref(raw)
    return r is not None and r.datasource == ref.datasource and r.field == ref.field and r.derivation == ref.derivation


def _checks(ctx: _Ctx, ws: Worksheet, res: RebuildResult, where: list[str], params: list) -> dict:
    checks: dict = {"rows": len(res.data)}
    # text dates that failed to parse (they silently drop out of date axes)
    bad = {}
    for col in sorted(ctx.text_dates_used):
        fmt = ctx.profile.column(col).date_format.format
        n = ctx.store.scalar(f"SELECT COUNT(*) FROM {ctx.view} WHERE {qi(col)} IS NOT NULL "
                             f"AND try_strptime({qi(col)}, ?) IS NULL", [fmt])
        bad[col] = int(n)
    if bad:
        checks["unparsed_dates"] = bad
    # include-filter members that do not exist in the data (e.g. a renamed category)
    missing = {}
    for f in ws.filters:
        if f.mode != "include" or not f.members or f.field.kind == "measure":
            continue
        try:
            expr, _, _ = ctx.dim_sql(f.field)
        except Unsupported:
            continue
        vals = [_member(m) for m in f.members if m != NULL_MEMBER]
        if not vals:
            continue
        present = {r[0] for r in ctx.store.con.execute(
            f"SELECT DISTINCT CAST({expr} AS VARCHAR) FROM {ctx.view} WHERE CAST({expr} AS VARCHAR) IN "
            f"({', '.join('?' for _ in vals)})", vals).fetchall()}
        gone = [v for v in vals if v not in present]
        if gone:
            missing[f.field.label] = len(gone)
    if missing:
        checks["filter_members_not_in_data"] = missing
    # can the cells be added up? (one entity counted in several cells)
    for m in res.measures:
        if m.aggregation == "count_distinct" and res.dims and not res.data.empty:
            cond = (" WHERE " + " AND ".join(where)) if where else ""
            total = ctx.store.scalar(f"SELECT {m.sql} FROM {ctx.view}{cond}", params)
            cell_sum = int(pd.to_numeric(res.data[m.alias], errors="coerce").fillna(0).sum())
            checks["distinct_total"] = {"measure": m.label, "distinct_total": int(total or 0),
                                        "sum_of_cells": cell_sum,
                                        "cells_can_be_summed": cell_sum == int(total or 0)}
            break
    checks["empty"] = res.data.empty
    return checks


def field_values(model: WorkbookModel, ref: FieldRef, store: Store | None, table: str | None,
                 profile: TableProfile | None, limit: int = 500) -> list[str]:
    """Distinct values of a field (as text, same form the filters compare against), for quick-filter dropdowns."""
    if store is None or table is None:
        return []
    ctx = _Ctx(model, ref.datasource, store, table, profile)
    try:
        expr, _, _ = ctx.dim_sql(ref)
    except Unsupported:
        return []
    rows = store.con.execute(
        f"SELECT CAST(raw AS VARCHAR) FROM (SELECT DISTINCT {expr} AS raw FROM {ctx.view} WHERE {expr} IS NOT NULL) "
        f"ORDER BY raw LIMIT {int(limit)}").fetchall()
    return [r[0] for r in rows]
