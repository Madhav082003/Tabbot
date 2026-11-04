"""Tools Claude can call. Each returns JSON built from workbook structure or aggregates - never rows.

Data boundary (enforced here):
- no tool returns extract rows, file paths, raw XML or SQL;
- values of sensitive columns (ids, free text, name-like fields; see config/privacy.yaml and the profile) are
  never returned: they cannot be grouped by or filtered on; filter member lists of such fields become counts;
- every result is capped (rows and characters) with an explicit truncation note;
- data-bearing results get an id (R1, R2 ...) that Claude must cite.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field, ValidationError

from tabbot.analysis import recipes as rc
from tabbot.analysis.checks import chart_checks
from tabbot.analysis.lints import run_lints
from tabbot.analysis.summary import key_facts
from tabbot.data.store import qi
from tabbot.twb.fieldref import FieldRef
from tabbot.twb.model import Worksheet
from tabbot.views.calc_translate import Unsupported, _to_date
from tabbot.views.display import fmt_dim
from tabbot.views.rebuild import _Ctx, rebuild_worksheet

FIELD_TOKEN_CHARS = "[]"
MAX_MEMBERS_SHOWN = 30


# --------------------------------------------------------------------------------------------- inputs


class NoInput(BaseModel):
    model_config = {"extra": "forbid"}


class SheetInput(BaseModel):
    model_config = {"extra": "forbid"}
    sheet: str = Field(description="Worksheet name exactly as listed by list_dashboards.")


class ChartDataInput(SheetInput):
    quick_filters: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Optional dashboard quick-filter selections: field label -> values to keep, e.g. "
                    '{"CHANNEL": ["Web"]}. Empty means all values.')


class AnalysisInput(SheetInput):
    recipe: Literal["standard", "trend", "period_compare", "change_point", "driver_scan", "share_shift",
                    "concentration", "multiplicity", "flag_share"] = Field(
        description="standard = trend + period comparison + change point + drivers + shares + concentration.")
    category: str | None = Field(default=None, description="Dimension label to break down by (default: the "
                                                            "sheet's main dimension).")
    focus: str | None = Field(default=None, description="driver_scan only: a category member to explain.")
    periods: int = Field(default=4, ge=1, le=12, description="period_compare: N last full periods vs N before.")
    flag_field: str | None = Field(default=None, description="flag_share only: field name.")
    flag_value: str | None = Field(default=None, description="flag_share only: value of that field.")
    quick_filters: dict[str, list[str]] = Field(default_factory=dict,
                                                description="Optional quick-filter selections, as in get_chart_data.")


class FindingsInput(BaseModel):
    model_config = {"extra": "forbid"}
    dashboard: str | None = Field(default=None, description="Only findings affecting this dashboard.")


class QueryDimension(BaseModel):
    model_config = {"extra": "forbid"}
    field: str = Field(description="Field caption or column name.")
    date_part: Literal["year", "quarter", "month"] | None = Field(
        default=None, description="For date fields: group by this part (quarter is returned as a period start).")


class QueryFilter(BaseModel):
    model_config = {"extra": "forbid"}
    field: str
    op: Literal["in", "not_in"] = "in"
    values: list[str]
    date_part: Literal["year", "quarter", "month"] | None = None


class QueryInput(BaseModel):
    model_config = {"extra": "forbid"}
    dimensions: list[QueryDimension] = Field(default_factory=list, max_length=3)
    measure: Literal["count_distinct_entity", "count_distinct", "count", "sum", "avg", "min", "max"] = \
        "count_distinct_entity"
    measure_field: str | None = Field(default=None, description="Needed unless measure is count_distinct_entity.")
    filters: list[QueryFilter] = Field(default_factory=list, max_length=6)
    limit: int = Field(default=50, ge=1, le=200)


class ShowInput(BaseModel):
    model_config = {"extra": "forbid"}
    page: str = Field(description="Dashboard name, or a visible sheet name, to show in the app.")
    quick_filters: dict[str, list[str]] = Field(
        default_factory=dict, description="Optional quick-filter selections to apply on that dashboard: "
                                          "field label -> values. Empty clears the filters.")


TOOL_SPECS: list[tuple[str, type[BaseModel], str]] = [
    ("list_dashboards", NoInput,
     "Overview of the open Tableau workbook: data source, what one record is, date range, every dashboard with "
     "its sheets (chart type and whether the redraw is exact), visible sheets, and finding counts. Call this "
     "first in a conversation. Returns structure only, no data values."),
    ("describe_sheet", SheetInput,
     "The definition of one worksheet: rows, columns, marks (colour/size/label fields), filters, chart type, "
     "whether the redraw is exact or approximate (and why), and its chart checks (partial periods, cells that "
     "cannot be summed...). Use it to explain what a chart shows before quoting its numbers."),
    ("get_chart_data", ChartDataInput,
     "The aggregated numbers behind one chart, exactly as the app draws it (long format, capped at 200 rows), "
     "with a result id to cite. Optional quick filters narrow it the way the dashboard dropdowns do. Use for "
     "specific values; for trends and changes prefer run_analysis."),
    ("run_analysis", AnalysisInput,
     "Standard analyses on a chart's own measure, filters and time field: trend with partial-period pace, last N "
     "vs previous N full periods by member, change point, driver scan (which other field explains a change, "
     "ranked by excess share), share shift by year, concentration, items per record, flag share. Returns small "
     "summary tables and key facts with a result id. Use 'standard' to get the main story of a dashboard."),
    ("get_findings", FindingsInput,
     "Rule-based definition problems found in the workbook (misnamed sheets, counting dates instead of records, "
     "percent titles over counts, copied filters, calculations ignoring a unit column, unused calculations, "
     "hidden sheets...). Optionally limited to one dashboard. Cite these when explaining why a chart may mislead."),
    ("query_data", QueryInput,
     "A custom aggregate over the workbook's data when no chart answers the question: up to 3 dimensions "
     "(dates by year/quarter/month), one measure (default: number of distinct records), include/exclude "
     "filters. Identifier, free-text and personal fields cannot be grouped or filtered by. Returns at most 200 "
     "rows with a result id."),
    ("show_in_app", ShowInput,
     "Switch the user's screen to a dashboard (or visible sheet) and optionally set its quick filters, so the "
     "user sees what you are talking about. Use it when you discuss a specific dashboard or a filtered view. "
     "Returns whether the switch was accepted."),
]


def tool_definitions() -> list[dict]:
    """Anthropic tool definitions, schemas generated from the pydantic models (never hand-written twice)."""
    out = []
    for name, model, desc in TOOL_SPECS:
        schema = model.model_json_schema()
        schema.pop("title", None)
        out.append({"name": name, "description": desc, "input_schema": schema})
    return out


# --------------------------------------------------------------------------------------------- helpers


def _clean(v: Any) -> Any:
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else round(v, 4)
    if hasattr(v, "item"):
        return _clean(v.item())
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat() if v == v.normalize() else v.isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _records(df: pd.DataFrame, limit: int) -> tuple[list[dict], bool]:
    rows = [_clean(r) for r in df.head(limit).to_dict("records")]
    return rows, len(df) > limit


class ToolError(Exception):
    """Shown to Claude as an is_error tool result."""


@dataclass
class UIAction:
    page: str
    quick_filters: dict[str, list[str]]   # field raw ref -> values


@dataclass
class ToolContext:
    lw: Any                                  # LoadedWorkbook
    max_rows: int = 200
    max_chars: int = 12000
    results: dict[str, pd.DataFrame] = field(default_factory=dict)
    actions: list[UIAction] = field(default_factory=list)
    _lints: list | None = None
    _n: int = 0

    # ---- lookups
    def new_id(self, df: pd.DataFrame) -> str:
        self._n += 1
        rid = f"R{self._n}"
        self.results[rid] = df
        return rid

    def sheet(self, name: str) -> Worksheet:
        ws = self.lw.model.worksheet(name)
        if ws is None:
            low = {w.name.lower(): w for w in self.lw.model.worksheets}
            ws = low.get(name.strip().lower())
        if ws is None:
            raise ToolError(f"no worksheet named {name!r}; call list_dashboards for the names")
        return ws

    def lints(self):
        if self._lints is None:
            self._lints = run_lints(self.lw)
        return self._lints

    def sensitive_columns(self, datasource: str | None) -> set[str]:
        prof = self.lw.profile_for(datasource)
        return set(prof.sensitive_suggestions) if prof else set()

    def field_is_sensitive(self, datasource: str | None, internal: str, seen: set | None = None) -> bool:
        sens = self.sensitive_columns(datasource)
        if internal in sens:
            return True
        f = self.lw.model.field(datasource, internal) if datasource else None
        if f is not None and f.is_calculated and f.formula:
            seen = seen or set()
            if internal in seen:
                return False
            seen.add(internal)
            import re

            for tok in re.findall(r"\[((?:[^\]]|\]\])+)\]", f.formula):
                if self.field_is_sensitive(datasource, tok.replace("]]", "]"), seen):
                    return True
        return False

    def quick_filter_refs(self, sheet: Worksheet | None, labels: dict[str, list[str]]) -> dict[str, list[str]]:
        """Map {field label: values} to {field raw ref: values} using the dashboards' quick filters and
        the sheet's own filters."""
        if not labels:
            return {}
        candidates: list[FieldRef] = []
        for d in self.lw.model.dashboards:
            if sheet is None or sheet.name in d.sheets:
                candidates += [q.field for q in d.quick_filters]
        if sheet is not None:
            candidates += [f.field for f in sheet.filters]
        out = {}
        for label, values in labels.items():
            ref = next((r for r in candidates if label in (r.label, r.caption, r.field)), None)
            if ref is None:
                raise ToolError(f"no quick filter called {label!r} for this view; available: "
                                f"{sorted({r.label for r in candidates})}")
            if self.field_is_sensitive(ref.datasource, ref.field):
                raise ToolError(f"{label!r} is a sensitive field and cannot be filtered by value")
            out[ref.raw] = [str(v) for v in values]
        return out

    def rebuild(self, ws: Worksheet, qf: dict[str, list[str]] | None = None):
        lw = self.lw
        return rebuild_worksheet(lw.model, ws, lw.store, lw.table_for(ws.datasource), lw.profile_for(ws.datasource),
                                 qf or {})

    def cap(self, payload: dict) -> str:
        text = json.dumps(_clean(payload), ensure_ascii=False, default=str)
        if len(text) <= self.max_chars:
            return text
        for key in ("rows", "table"):
            while key in payload and isinstance(payload[key], list) and len(payload[key]) > 5 \
                    and len(text) > self.max_chars:
                payload[key] = payload[key][: max(5, len(payload[key]) // 2)]
                payload["truncated"] = f"output cut to {len(payload[key])} rows to fit; narrow the request"
                text = json.dumps(_clean(payload), ensure_ascii=False, default=str)
        return text[: self.max_chars] + ("…[truncated]" if len(text) > self.max_chars else "")


# --------------------------------------------------------------------------------------------- tools


def _sheet_summary(ctx: ToolContext, ws: Worksheet) -> dict:
    res = ctx.rebuild(ws)
    return {"sheet": ws.name, "title": (ws.title or "").replace("<Sheet Name>", ws.name) or ws.name,
            "chart": res.chart, "redraw": res.status, "hidden": ws.hidden}


def t_list_dashboards(ctx: ToolContext, _: NoInput) -> dict:
    lw, m = ctx.lw, ctx.lw.model
    sources = []
    for ds in m.datasources:
        prof = lw.profile_for(ds.name)
        item: dict[str, Any] = {"name": ds.caption or ds.name, "connection": ds.connection_class,
                                "has_extract": ds.has_extract,
                                "calculated_fields": [f.caption for f in ds.fields if f.is_calculated]}
        if prof is not None:
            item["rows"] = prof.rows
            if prof.entity:
                item["record"] = {"field": prof.entity.field, "distinct": prof.entity.distinct,
                                  "rows_per_record": prof.entity.rows_per_entity}
            item["fields"] = [{"name": c.name, "kind": c.kind, "distinct": c.distinct,
                               "one_value_per_record": c.entity_level, "sensitive": bool(c.sensitive_reasons)}
                              for c in prof.columns]
            dcols = [c for c in prof.columns if c.date_format]
            if dcols:
                c = dcols[0]
                rng = lw.store.con.execute(
                    f"SELECT CAST(MIN(try_strptime({qi(c.name)}, ?)) AS DATE), "
                    f"CAST(MAX(try_strptime({qi(c.name)}, ?)) AS DATE) FROM {lw.store.view(prof.table)}",
                    [c.date_format.format, c.date_format.format]).fetchone()
                item["date_range"] = {"field": c.name, "from": rng[0], "to": rng[1]}
        sources.append(item)
    findings = ctx.lints()
    return {
        "workbook": lw.source_name,
        "data_sources": sources,
        "dashboards": [{"name": d.name, "quick_filters": [q.field.label for q in d.quick_filters],
                        "sheets": [_sheet_summary(ctx, m.worksheet(s)) for s in d.sheets]} for d in m.dashboards],
        "visible_sheets": [w.name for w in m.worksheets if not w.hidden],
        "findings": {sev: sum(f.severity == sev for f in findings) for sev in ("error", "warn", "info")},
        "notes": lw.notes + m.unsupported,
    }


def t_describe_sheet(ctx: ToolContext, inp: SheetInput) -> dict:
    ws = ctx.sheet(inp.sheet)
    res = ctx.rebuild(ws)
    filters = []
    for f in ws.filters:
        item: dict[str, Any] = {"field": f.field.label, "mode": f.mode, "values": f.member_count}
        if f.members and f.member_count <= MAX_MEMBERS_SHOWN and \
                not ctx.field_is_sensitive(f.field.datasource, f.field.field):
            item["members"] = f.members
        filters.append(item)
    prof = ctx.lw.profile_for(ws.datasource)
    actx = None
    if res.status != "failed" and res.measures and ctx.lw.store is not None:
        try:
            actx = rc.context_for(res, ctx.lw.store, prof)
        except ValueError:
            actx = None
    return {
        "sheet": ws.name, "title": ws.title, "hidden": ws.hidden,
        "dashboards": [d.name for d in ctx.lw.model.dashboards if ws.name in d.sheets],
        "tableau_mark": ws.mark, "chart": res.chart,
        "rows_shelf": [r.label for r in ws.rows], "columns_shelf": [r.label for r in ws.cols],
        "marks": {k: [r.label for r in v] for k, v in ws.encodings.items()},
        "filters": filters,
        "redraw": res.status, "why_approximate": res.reasons,
        "checks": [c.model_dump(exclude={"evidence"}) for c in chart_checks(res, actx)],
    }


def t_get_chart_data(ctx: ToolContext, inp: ChartDataInput) -> dict:
    ws = ctx.sheet(inp.sheet)
    qf = ctx.quick_filter_refs(ws, inp.quick_filters)
    res = ctx.rebuild(ws, qf)
    if res.status == "failed":
        raise ToolError("this sheet could not be rebuilt: " + "; ".join(res.reasons))
    df = res.data.copy()
    hidden_dims = []
    for d in res.dims:
        if ctx.field_is_sensitive(res.datasource, d.ref.field):
            df[d.alias] = "[hidden]"
            hidden_dims.append(d.label)
        else:
            df[d.alias] = fmt_dim(df[d.alias], d)
    df = df.rename(columns={d.alias: d.label for d in res.dims} | {m.alias: m.label for m in res.measures})
    rid = ctx.new_id(df)
    rows, cut = _records(df, ctx.max_rows)
    out = {"result_id": rid, "sheet": ws.name, "chart": res.chart, "redraw": res.status,
           "why_approximate": res.reasons, "applied_quick_filters": res.applied_quick_filters,
           "columns": list(df.columns), "row_count": len(df), "rows": rows,
           "checks": [c.message for c in chart_checks(res, None)]}
    if cut:
        out["truncated"] = f"showing {ctx.max_rows} of {len(df)} rows; use run_analysis or query_data to summarise"
    if hidden_dims:
        out["hidden_fields"] = f"values of sensitive field(s) {hidden_dims} are not shared"
    return out


def _result_payload(r: rc.AnalysisResult, limit: int = 30) -> dict:
    rows, cut = _records(r.table.drop(columns=["next"], errors="ignore"), limit) if not r.table.empty else ([], False)
    out = {"title": r.title, "ok": r.ok, "summary": r.summary, "notes": r.notes, "table": rows}
    if cut:
        out["table_truncated"] = f"first {limit} of {len(r.table)} rows"
    return out


def t_run_analysis(ctx: ToolContext, inp: AnalysisInput) -> dict:
    ws = ctx.sheet(inp.sheet)
    qf = ctx.quick_filter_refs(ws, inp.quick_filters)
    res = ctx.rebuild(ws, qf)
    if res.status == "failed" or not res.measures:
        raise ToolError("this sheet has no rebuildable measure to analyse")
    prof = ctx.lw.profile_for(ws.datasource)
    try:
        actx = rc.context_for(res, ctx.lw.store, prof, inp.category)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if actx.category_sql and inp.category is None and actx.category_label:
        cat_ref = next((d.ref for d in res.dims if d.label == actx.category_label), None)
        if cat_ref is not None and ctx.field_is_sensitive(res.datasource, cat_ref.field):
            actx.category_sql = actx.category_label = None
    if inp.recipe == "flag_share" and (not inp.flag_field or inp.flag_value is None):
        raise ToolError("flag_share needs flag_field and flag_value")
    if inp.recipe == "flag_share" and inp.flag_field in ctx.sensitive_columns(ws.datasource):
        raise ToolError(f"{inp.flag_field!r} is a sensitive field")
    base = {"sheet": ws.name, "redraw": res.status, "why_approximate": res.reasons,
            "measure": actx.measure_label, "time_field": actx.date_label, "grain": actx.grain,
            "category": actx.category_label, "record": actx.entity_label, "context_notes": actx.notes}
    if inp.recipe == "standard":
        results = rc.run_standard(actx)
        tables = {k: v.table for k, v in results.items() if not v.table.empty}
        rid = ctx.new_id(pd.concat(tables, names=["analysis"]) if tables else pd.DataFrame())
        return base | {"result_id": rid, "key_facts": key_facts(results, actx.grain),
                       "analyses": {k: _result_payload(v, 12) for k, v in results.items()}}
    fn = {
        "trend": lambda: rc.trend(actx),
        "period_compare": lambda: rc.period_compare(actx, n=inp.periods),
        "change_point": lambda: rc.change_point(actx),
        "driver_scan": lambda: rc.driver_scan(actx, focus=inp.focus),
        "share_shift": lambda: rc.share_shift(actx),
        "concentration": lambda: rc.concentration(actx),
        "multiplicity": lambda: rc.multiplicity(actx),
        "flag_share": lambda: rc.flag_share(actx, inp.flag_field, inp.flag_value),
    }[inp.recipe]
    r = fn()
    rid = ctx.new_id(r.table)
    return base | {"result_id": rid, "analysis": _result_payload(r)}


def t_get_findings(ctx: ToolContext, inp: FindingsInput) -> dict:
    fs = ctx.lints()
    if inp.dashboard:
        fs = [f for f in fs if inp.dashboard in f.dashboards or not (f.dashboards or f.sheets)]
    private = {"members", "file"}  # member lists and file names stay local
    return {"findings": [f.model_dump(exclude={"evidence"}) | {"evidence": {k: v for k, v in f.evidence.items()
                                                                            if k not in private}}
                         for f in fs]}


def t_query_data(ctx: ToolContext, inp: QueryInput) -> dict:
    lw = ctx.lw
    if lw.store is None:
        raise ToolError("this workbook has no extract data")
    ds = next((d.name for d in lw.model.datasources if lw.profile_for(d.name) is not None), None)
    table, prof = lw.table_for(ds), lw.profile_for(ds)
    qctx = _Ctx(lw.model, ds, lw.store, table, prof)
    sens = ctx.sensitive_columns(ds)

    def internal(name: str) -> str:
        if name in qctx.columns:
            return name
        f = next((x for d in lw.model.datasources if d.name == ds for x in d.fields
                  if name in (x.caption, x.internal_name)), None)
        if f is None:
            raise ToolError(f"unknown field {name!r}; see list_dashboards for field names")
        return f.internal_name

    def dim_expr(name: str, part: str | None) -> str:
        iname = internal(name)
        if ctx.field_is_sensitive(ds, iname):
            raise ToolError(f"{name!r} is a sensitive field (id, free text or personal): it can be counted but "
                            "not grouped or filtered by value")
        try:
            e = qctx.resolve(iname)
        except Unsupported as exc:
            raise ToolError(str(exc)) from None
        if e.is_agg:
            raise ToolError(f"{name!r} is an aggregate calculation; use it via a chart instead")
        if part:
            d = _to_date(e, "TIMESTAMP").sql
            return f"CAST(date_trunc('{part}', {d}) AS DATE)" if part != "year" else f"year({d})"
        return e.sql

    sel, groups, params = [], [], []
    for i, d in enumerate(inp.dimensions):
        sel.append(f"{dim_expr(d.field, d.date_part)} AS g{i}")
        groups.append(f"g{i}")
    if inp.measure == "count_distinct_entity":
        if prof is None or prof.entity is None:
            raise ToolError("no record identifier detected; choose another measure")
        mexpr, mlabel = f"COUNT(DISTINCT {qi(prof.entity.field)})", f"Distinct count of {prof.entity.field}"
    else:
        if not inp.measure_field:
            raise ToolError("measure_field is required for this measure")
        e = qctx.resolve(internal(inp.measure_field))
        agg = {"count_distinct": "COUNT(DISTINCT {})", "count": "COUNT({})", "sum": "SUM({})", "avg": "AVG({})",
               "min": "MIN({})", "max": "MAX({})"}[inp.measure]
        if inp.measure in ("sum", "avg", "min", "max") and internal(inp.measure_field) in sens:
            raise ToolError("cannot aggregate the values of a sensitive field")
        mexpr, mlabel = agg.format(e.sql), f"{inp.measure}({inp.measure_field})"
    where = []
    for f in inp.filters:
        expr = dim_expr(f.field, f.date_part)
        if not f.values:
            continue
        ph = ", ".join("?" for _ in f.values)
        where.append(f"CAST({expr} AS VARCHAR) {'IN' if f.op == 'in' else 'NOT IN'} ({ph})")
        params += [str(v) for v in f.values]
    sql = f"SELECT {', '.join(sel + [mexpr + ' AS m'])} FROM {qctx.view}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if groups:
        sql += " GROUP BY " + ", ".join(groups) + " ORDER BY m DESC"
    sql += f" LIMIT {inp.limit + 1}"
    try:
        df = lw.store.df(sql, params)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"query failed: {str(exc).splitlines()[0][:160]}") from None
    df.columns = [f"{d.field}{' (' + d.date_part + ')' if d.date_part else ''}" for d in inp.dimensions] + [mlabel]
    cut = len(df) > inp.limit
    df = df.head(inp.limit)
    rid = ctx.new_id(df)
    rows, _ = _records(df, inp.limit)
    out = {"result_id": rid, "columns": list(df.columns), "rows": rows, "row_count": len(df)}
    if cut:
        out["truncated"] = f"more than {inp.limit} groups; showing the largest {inp.limit}"
    return out


def t_show_in_app(ctx: ToolContext, inp: ShowInput) -> dict:
    m = ctx.lw.model
    d = m.dashboard(inp.page)
    if d is None:
        w = m.worksheet(inp.page)
        if w is None or w.hidden:
            raise ToolError(f"no dashboard or visible sheet called {inp.page!r}")
        ctx.actions.append(UIAction(page=f"📄 {w.name}", quick_filters={}))
        return {"shown": w.name}
    refs: dict[str, list[str]] = {}
    if inp.quick_filters:
        cands = {q.field.label: q.field for q in d.quick_filters} | {q.field.caption: q.field for q in d.quick_filters
                                                                     if q.field.caption}
        for label, values in inp.quick_filters.items():
            if label not in cands:
                raise ToolError(f"dashboard {d.name!r} has no quick filter {label!r}; it has "
                                f"{[q.field.label for q in d.quick_filters]}")
            refs[cands[label].raw] = [str(v) for v in values]
    ctx.actions.append(UIAction(page=d.name, quick_filters=refs))
    return {"shown": d.name, "quick_filters": inp.quick_filters}


HANDLERS = {
    "list_dashboards": t_list_dashboards, "describe_sheet": t_describe_sheet, "get_chart_data": t_get_chart_data,
    "run_analysis": t_run_analysis, "get_findings": t_get_findings, "query_data": t_query_data,
    "show_in_app": t_show_in_app,
}
INPUTS = {name: model for name, model, _ in TOOL_SPECS}


def dispatch(ctx: ToolContext, name: str, raw_input: Any) -> tuple[str, bool]:
    """Run one tool. Returns (JSON text, is_error). Never raises."""
    if name not in HANDLERS:
        return json.dumps({"error": f"unknown tool {name!r}"}), True
    try:
        inp = INPUTS[name].model_validate(raw_input or {})
        return ctx.cap(HANDLERS[name](ctx, inp)), False
    except ValidationError as exc:
        return json.dumps({"error": "invalid input", "details": exc.errors(include_url=False,
                                                                          include_context=False)}, default=str), True
    except ToolError as exc:
        return json.dumps({"error": str(exc)}), True
    except Exception as exc:  # noqa: BLE001 - a tool bug must not break the chat
        return json.dumps({"error": f"tool failed: {type(exc).__name__}: {str(exc)[:200]}"}), True
