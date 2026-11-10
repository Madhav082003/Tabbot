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
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field, ValidationError

from tabbot.analysis import recipes as rc
from tabbot.analysis.checks import chart_checks
from tabbot.analysis.lints import run_lints
from tabbot.analysis.summary import key_facts
from tabbot.data.store import qi
from tabbot.explain.calcinfo import counts_what, value_map
from tabbot.twb.fieldref import FieldRef
from tabbot.twb.model import Worksheet
from tabbot.views.calc_translate import Unsupported, _to_date
from tabbot.views.display import fmt_dim
from tabbot.views.rebuild import _Ctx, rebuild_worksheet

FIELD_TOKEN_CHARS = "[]"
MAX_MEMBERS_SHOWN = 30
FORMULA_MAX = 2000          # formulas are sent whole up to this; longer ones say plainly that they were cut
_AGG_WORD = {"cnt": "COUNT", "ctd": "COUNTD", "cntd": "COUNTD", "sum": "SUM"}


def _halve_largest_list(payload: Any) -> bool:
    """Find the list with the most JSON text (anywhere inside) and keep its first half plus a note."""
    best: tuple[int, list] | None = None

    def walk(o: Any) -> None:
        nonlocal best
        if isinstance(o, list):
            real = [x for x in o if not (isinstance(x, str) and x.startswith("… ") and x.endswith("not shown"))]
            if len(real) > 2:
                size = len(json.dumps(o, ensure_ascii=False, default=str))
                if best is None or size > best[0]:
                    best = (size, o)
        for v in (o.values() if isinstance(o, dict) else o if isinstance(o, list) else []):
            walk(v)

    walk(payload)
    if best is None:
        return False
    lst = best[1]
    hidden = 0
    if lst and isinstance(lst[-1], str) and lst[-1].startswith("… ") and lst[-1].endswith("not shown"):
        hidden = int(lst.pop().split()[1])
    keep = max(2, len(lst) // 2)
    hidden += len(lst) - keep
    del lst[keep:]
    lst.append(f"… {hidden} more not shown")
    return True


def _formula(f) -> dict:
    """A calculated field's full readable formula (+ an explicit note if very long) and its value map."""
    text = f.formula_readable or f.formula or ""
    out: dict[str, Any] = {"formula": text[:FORMULA_MAX]}
    if len(text) > FORMULA_MAX:
        out["formula_truncated"] = f"first {FORMULA_MAX} of {len(text)} characters"
    vm = value_map(text)
    if vm is not None:
        out["values"] = vm
    return out


def _formula_lookup(lw, datasource: str | None):
    """caption -> readable formula for the data source's calculated fields (None for plain columns)."""
    ds = lw.model.datasource(datasource) if datasource else None
    by = {f.caption: (f.formula_readable or f.formula) for f in (ds.fields if ds else []) if f.is_calculated}
    return lambda name: by.get(name)


def _sheet_calcs(lw, ws: Worksheet) -> list[dict]:
    """Every calculated field the sheet uses, directly or through other calculations, with full formulas."""
    ds = lw.model.datasource(ws.datasource) if ws.datasource else None
    if ds is None:
        return []
    by_internal = {f.internal_name: f for f in ds.fields if f.is_calculated}
    by_caption = {f.caption: f for f in ds.fields if f.is_calculated}
    todo = [by_internal[r.field] for r in ws.all_refs() if r.field in by_internal]
    seen: dict[str, Any] = {}
    while todo and len(seen) < 25:
        f = todo.pop(0)
        if f.internal_name in seen:
            continue
        seen[f.internal_name] = f
        for name in re.findall(r"\[((?:[^\]]|\]\])+)\]", f.formula_readable or ""):
            g = by_caption.get(name.replace("]]", "]"))
            if g is not None and g.internal_name not in seen:
                todo.append(g)
    return [{"name": f.caption, **_formula(f)} for f in seen.values()]


def _sheet_measures(lw, ws: Worksheet) -> list[dict]:
    """What each measure on the sheet counts (rows, distinct values ...), following calcs and LODs."""
    lookup = _formula_lookup(lw, ws.datasource)
    out, done = [], set()
    places = [("rows", r) for r in ws.rows] + [("columns", r) for r in ws.cols] + \
        [(k, r) for k, v in ws.encodings.items() for r in v]
    for where, r in places:
        if r.kind != "measure" or r.raw in done:
            continue
        done.add(r.raw)
        base = r.caption or r.field
        agg = _AGG_WORD.get(r.derivation) if r.derivation not in ("usr", "attr") else None
        what = counts_what(agg, base, lookup) if (agg or lookup(base)) else None
        if what:
            out.append({"measure": r.label, "on": where, "counts": what})
    return out


# --------------------------------------------------------------------------------------------- inputs


class NoInput(BaseModel):
    model_config = {"extra": "forbid"}


class SheetInput(BaseModel):
    model_config = {"extra": "forbid"}
    sheet: str = Field(description="Worksheet name, or a dashboard name (then its main sheet is used).")


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
    datasource: str | None = Field(default=None, description="Data source name from list_dashboards; default: "
                                   "the one most sheets use.")


TOOL_SPECS: list[tuple[str, type[BaseModel], str]] = [
    ("list_dashboards", NoInput,
     "Compact overview of the open Tableau workbook: the data source (what one record is, how many, date range, "
     "fields grouped by kind), calculated fields with readable formulas, and every dashboard with its sheets - "
     "repeated sheets that differ only by one filter value are grouped into a pattern, the dashboard's "
     "interactions (from the workbook's actions list) and which package file feeds each data source. Call this "
     "first. Returns structure only, no data values."),
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
class UIAction:  # kept for compatibility; nothing creates these now
    page: str
    quick_filters: dict[str, list[str]]   # field raw ref -> values


@dataclass
class ToolContext:
    lw: Any                                  # LoadedWorkbook
    max_rows: int = 200
    max_chars: int = 12000
    results: dict[str, pd.DataFrame] = field(default_factory=dict)
    actions: list[UIAction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    _lints: list | None = None
    _n: int = 0

    # ---- lookups
    def new_id(self, df: pd.DataFrame) -> str:
        self._n += 1
        rid = f"R{self._n}"
        self.results[rid] = df
        return rid

    def sheet(self, name: str) -> Worksheet:
        """A worksheet by name, or a dashboard by name (its only sheet, or the first sheet when several)."""
        m = self.lw.model
        key = name.strip().lower()
        ws = m.worksheet(name) or {w.name.lower(): w for w in m.worksheets}.get(key)
        if ws is not None:
            return ws
        d = m.dashboard(name) or {x.name.lower(): x for x in m.dashboards}.get(key)
        if d is not None and d.sheets:
            if len(d.sheets) > 1:
                self.notes.append(f"dashboard {d.name!r} has {len(d.sheets)} sheets; used the first, "
                                  f"{d.sheets[0]!r}. Its sheets: {d.sheets[:12]}")
            return m.worksheet(d.sheets[0])
        raise ToolError(f"no worksheet or dashboard named {name!r}; call list_dashboards for the names")

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
        for _ in range(40):          # still too big: halve the largest list (saying so) - keeps the JSON whole
            if len(text) <= self.max_chars:
                return text
            if not _halve_largest_list(payload):
                break
            text = json.dumps(_clean(payload), ensure_ascii=False, default=str)
        return text[: self.max_chars] + ("…[truncated]" if len(text) > self.max_chars else "")


# --------------------------------------------------------------------------------------------- tools


def _shape(ws: Worksheet) -> str:
    """What a sheet shows, ignoring which single value its filters keep (used to find repeated sheets)."""
    enc = "; ".join(f"{k}={', '.join(r.label for r in v)}" for k, v in sorted(ws.encodings.items()))
    flt = ", ".join(sorted({f"{f.field.label} {f.mode}" + (" one value" if f.member_count == 1 else "")
                            for f in ws.filters}))
    rows = ", ".join(r.label for r in ws.rows) or "-"
    cols = ", ".join(r.label for r in ws.cols) or "-"
    return f"{ws.mark} | rows: {rows} | columns: {cols} | marks: {enc or '-'}" + (f" | filters: {flt}" if flt else "")


def _title(ws: Worksheet) -> str:
    from tabbot.explain.phrases import clean_title
    t = (ws.title or "").strip()
    return ws.name if not t or "<Sheet Name>" in t else clean_title(t)


def _sheets_summary(sheets: list[Worksheet]) -> list[dict]:
    groups: dict[str, list[Worksheet]] = {}
    for w in sheets:
        groups.setdefault(_shape(w), []).append(w)
    out = []
    for shape, ws in groups.items():
        if len(ws) >= 3:
            varying = [f.field.label for f in ws[0].filters if f.member_count == 1]
            out.append({"pattern": shape, "repeated_sheets": len(ws),
                        "one_per": varying[0] if varying else None,
                        "examples": [_title(w) for w in ws[:3]]})
        else:
            out += [{"sheet": w.name, "title": _title(w), "shows": shape} for w in ws]
    return out


def _fields_summary(prof, sens: set[str]) -> dict:
    groups: dict[str, list[str]] = {}
    label = {"categorical": "categories", "high_cardinality": "detailed codes or text (many values)",
             "text_date": "dates (stored as text)", "date": "dates", "numeric": "numbers",
             "id": "identifiers", "free_text": "free text"}
    for c in prof.columns:
        g = "sensitive (values never shared)" if c.name in sens else label.get(c.kind, c.kind)
        groups.setdefault(g, []).append(f"{c.name} ({c.distinct:,})" if g == "categories" else c.name)
    return groups


def _interactions(lw, dashboard: str) -> list[str]:
    """What clicking does on a dashboard - only from the workbook's actions list, never from filter names."""
    from tabbot.tparse.describe import describe_action
    kit = lw.kit
    if kit is None:
        return []
    acts = [a for a in kit.actions if a.source_dashboard == dashboard or a.params.get("target") == dashboard]
    return [describe_action(a) for a in acts] or ["no actions: clicking a chart does not change the others"]


def t_list_dashboards(ctx: ToolContext, _: NoInput) -> dict:
    lw, m = ctx.lw, ctx.lw.model
    sources = []
    for ds in m.datasources:
        prof = lw.profile_for(ds.name)
        calcs = [{"name": f.caption, **_formula(f),
                  **({"unused": True} if not f.used_by_sheets and not f.used_by_calcs else {})}
                 for f in ds.fields if f.is_calculated]
        item: dict[str, Any] = {"name": ds.caption or ds.name, "connection": ds.connection_class,
                                "has_extract": ds.has_extract, "calculated_fields": calcs}
        if prof is not None:
            item["rows"] = prof.rows
            if prof.entity:
                item["record"] = {"field": prof.entity.field, "distinct": prof.entity.distinct,
                                  "rows_per_record": prof.entity.rows_per_entity}
            item["fields"] = _fields_summary(prof, ctx.sensitive_columns(ds.name))
            dcols = [c for c in prof.columns if c.date_format and c.entity_level is not False]
            if dcols:
                c = dcols[0]
                rng = lw.store.con.execute(
                    f"SELECT CAST(MIN(try_strptime({qi(c.name)}, ?)) AS DATE), "
                    f"CAST(MAX(try_strptime({qi(c.name)}, ?)) AS DATE) FROM {lw.store.view(prof.table)}",
                    [c.date_format.format, c.date_format.format]).fetchone()
                item["date_range"] = {"field": c.name, "from": rng[0], "to": rng[1]}
        sources.append(item)
    findings = ctx.lints()
    on_dash = {s for d in m.dashboards for s in d.sheets}
    out = {
        "workbook": lw.source_name,
        "data_sources": sources,
        "dashboards": [{"name": d.name, "quick_filters": [q.field.label for q in d.quick_filters],
                        "sheet_count": len(d.sheets),
                        "sheets": _sheets_summary([m.worksheet(x) for x in d.sheets]),
                        "interactions": _interactions(lw, d.name)} for d in m.dashboards],
        "data_files": lw.data_files_shared,
        "sheets_not_on_any_dashboard": [w.name for w in m.worksheets if w.name not in on_dash],
        "worksheets_total": len(m.worksheets), "hidden_worksheets": sum(w.hidden for w in m.worksheets),
        "findings": [f"{f.severity}: {f.title}" for f in findings],
        "notes": lw.notes + m.unsupported,
    }
    _fit_formulas(out, ctx.max_chars)
    return out


def _fit_formulas(out: dict, limit: int) -> None:
    """A very large workbook: shorten the longest formulas (saying so) instead of cutting the whole reply."""
    calcs = [c for d in out["data_sources"] for c in d["calculated_fields"]]
    for size in (600, 300, 120):
        if len(json.dumps(_clean(out), ensure_ascii=False, default=str)) <= limit:
            return
        for c in calcs:
            if len(c["formula"]) > size:
                c["formula_truncated"] = (f"shortened to {size} characters to fit; describe_sheet on a sheet using "
                                          "it gives the full formula")
                c["formula"] = c["formula"][:size]
                if "values" in c and len(json.dumps(c["values"])) > size:
                    c.pop("values")


def _shelf(r, ws) -> str:
    """'Order Date by month (timeline) [tmn]' - the label plus Tableau's raw derivation."""
    lab = r.label_in(list(ws.rows) + list(ws.cols))
    return f"{lab} [{r.derivation}]" if r.derivation and r.derivation != "none" else lab


def t_describe_sheet(ctx: ToolContext, inp: SheetInput) -> dict:
    ws = ctx.sheet(inp.sheet)
    res = ctx.rebuild(ws)
    filters = []
    for f in ws.filters:
        item: dict[str, Any] = {"field": f.field.label, "derivation": f.field.derivation or None,
                                "mode": "action" if f.action_status else f.mode, "values": f.member_count}
        if f.note:
            item["effect"] = f.note
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
        "rows_shelf": [_shelf(r, ws) for r in ws.rows], "columns_shelf": [_shelf(r, ws) for r in ws.cols],
        "marks": {k: [r.label for r in v] for k, v in ws.encodings.items()},
        "filters": filters,
        "measures_count": _sheet_measures(ctx.lw, ws),
        "calculations": _sheet_calcs(ctx.lw, ws),
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


def _pick_datasource(lw, wanted: str | None) -> str:
    """The data source to query: the one named, else the one most sheets use. Only data from a file that data
    source is linked to is ever queried; when that file is missing, say which file it is."""
    from tabbot.explain.facts import primary_datasource
    if wanted:
        d = next((d for d in lw.model.datasources if wanted in (d.name, d.caption)), None)
        if d is None:
            raise ToolError(f"unknown data source {wanted!r}; see list_dashboards")
        name = d.name
    else:
        name = primary_datasource(lw)
        if name is None:
            raise ToolError("no data source has readable data")
    if lw.table_for(name) is None:
        label = lw.model.datasource(name).caption or name
        why = [x for x in lw.data_files_shared if x.startswith(label + ":")]
        raise ToolError(f"data source {label!r} has no readable data here" + (f" ({why[0]})" if why else ""))
    return name


def t_query_data(ctx: ToolContext, inp: QueryInput) -> dict:
    lw = ctx.lw
    if lw.store is None:
        raise ToolError("this workbook has no readable data in its package" +
                        (": " + "; ".join(lw.data_files_shared) if lw.data_files_shared else ""))
    ds = _pick_datasource(lw, inp.datasource)
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


HANDLERS = {
    "list_dashboards": t_list_dashboards, "describe_sheet": t_describe_sheet, "get_chart_data": t_get_chart_data,
    "run_analysis": t_run_analysis, "get_findings": t_get_findings, "query_data": t_query_data,
}
INPUTS = {name: model for name, model, _ in TOOL_SPECS}


def dispatch(ctx: ToolContext, name: str, raw_input: Any) -> tuple[str, bool]:
    """Run one tool. Returns (JSON text, is_error). Never raises."""
    if name not in HANDLERS:
        return json.dumps({"error": f"unknown tool {name!r}"}), True
    try:
        inp = INPUTS[name].model_validate(raw_input or {})
        ctx.notes = []
        out = HANDLERS[name](ctx, inp)
        if ctx.notes:
            out = {**out, "notes_from_app": list(ctx.notes)}
        return ctx.cap(out), False
    except ValidationError as exc:
        return json.dumps({"error": "invalid input", "details": exc.errors(include_url=False,
                                                                          include_context=False)}, default=str), True
    except ToolError as exc:
        return json.dumps({"error": str(exc)}), True
    except Exception as exc:  # noqa: BLE001 - a tool bug must not break the chat
        return json.dumps({"error": f"tool failed: {type(exc).__name__}: {str(exc)[:200]}"}), True
