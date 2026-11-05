"""Plain-language descriptions of workbooks, dashboards, worksheets and filters.

These are deterministic summaries built from the model. They are what an LLM (or a person) reads first; they
contain field names and filter structure, and member values only for short lists.
"""
from __future__ import annotations

from collections import Counter

from .fieldref import FieldRef
from .model import Action, Dashboard, FilterDef, WorkbookModel, Worksheet

GEO = {"Latitude (generated)", "Longitude (generated)", "Geometry (generated)"}


def _label(r: FieldRef) -> str:
    return r.label()


def _measures(ws: Worksheet) -> list[FieldRef]:
    seen, out = set(), []
    for r in list(ws.rows.refs) + list(ws.cols.refs) + [e.field for p in ws.panes for e in p.encodings]:
        if r.is_measure and r.raw not in seen:
            seen.add(r.raw)
            out.append(r)
    return out


def infer_chart_type(ws: Worksheet) -> str:
    mark = (ws.mark or "Automatic").lower()
    row_m, col_m = bool(ws.rows.measures), bool(ws.cols.measures)
    row_d, col_d = bool(ws.rows.dimensions), bool(ws.cols.dimensions)
    fields = {r.field for r in ws.all_refs()}
    channels = {e.channel for p in ws.panes for e in p.encodings}
    dual = any(s.tree is not None and s.tree.op == "+" for s in (ws.rows, ws.cols))
    no_shelves = not (ws.rows.refs or ws.cols.refs)
    if fields & GEO or mark in ("multipolygon", "map"):
        base = "map"
    elif mark == "pie" and dual:
        return "donut chart (dual-axis pie)"      # the common SUM(0) + SUM(0) donut technique
    elif mark == "pie":
        base = "pie chart"
    elif no_shelves and mark in ("automatic", "square", "circle") and "size" in channels:
        base = "treemap or packed bubbles"
    elif no_shelves and channels and channels <= {"text", "tooltip", "color", "lod"} and "text" in channels:
        text_fields = [e.field for p in ws.panes for e in p.encodings if e.channel == "text"]
        base = "KPI / single number" if all(f.is_measure for f in text_fields) else "text list / label card"
    elif no_shelves and not channels:
        base = "empty or layout sheet"
    elif mark == "line":
        base = "line chart"
    elif mark == "area":
        base = "area chart"
    elif mark == "bar":
        base = "bar chart"
    elif mark == "square":
        base = "heatmap (highlight table)" if "color" in channels else "square marks"
    elif mark in ("circle", "shape"):
        base = "scatter plot" if (row_m and col_m) else "dot plot"
    elif mark == "text":
        base = "text table"
    elif mark == "gantt" or mark == "ganttbar":
        base = "gantt chart"
    else:  # Automatic: mimic Tableau's choice
        cont_date = any(r.kind == "date_trunc" for r in ws.cols.refs + ws.rows.refs)
        if row_m and col_m:
            base = "scatter plot"
        elif (row_m or col_m) and cont_date:
            base = "line chart"
        elif row_m or col_m:
            base = "bar chart"
        elif ws.uses_measure_names:
            base = "text table (multiple measures)"
        elif "text" in channels and not (row_d or col_d):
            base = "KPI / single number"
        elif "text" in channels:
            base = "text table"
        else:
            base = "table or layout sheet"
    if dual:
        base += " with multiple/dual axes"
    return base


def describe_action_filter(fd: FilterDef, name: str | None = None) -> str:
    """What an action filter does, from the action it is linked to - never guessed from its name."""
    name = name or fd.field.caption or fd.field.field
    if fd.action_status == "linked":
        return f"{name}: filtered by clicks via the '{fd.action_caption}' action"
    if fd.action_status == "tooltip":
        return f"{name}: set by the sheet that shows this one in its tooltip (viz in tooltip)"
    if fd.action_status == "missing":
        return f"{name}: left over from a removed action ({fd.action_ref}), no effect"
    return f"{name}: left over from a removed action (it names no action), no effect"


def describe_action(a: Action, model: WorkbookModel | None = None) -> str:
    """'State Filter: filter action - selecting a mark in Sale Map filters the other sheets of Overview'."""
    act = {"on-select": "selecting", "on-hover": "hovering over", "on-menu": "choosing from the menu on"}.get(
        a.activation or "", "selecting")
    src = f"'{a.source_worksheet}'" if a.source_worksheet else "any sheet"
    where = f" of dashboard '{a.source_dashboard}'" if a.source_dashboard else ""
    target = a.params.get("target") or a.source_dashboard or ""
    excl = [x for x in (a.params.get("exclude") or "").split(",") if x]
    fields = a.params.get("field-captions") or ("all fields" if a.params.get("special-fields") == "all" else "")
    on = f" (by {fields})" if fields else ""
    not_on = f", except {', '.join(excl)}" if excl else ""
    label = a.caption or a.name
    if a.kind == "filter":
        return (f"{label}: filter action - {act} a mark in {src}{where} filters the sheets of '{target}'{not_on}"
                f"{on}")
    if a.kind == "highlight":
        return (f"{label}: highlight action - {act} a mark in {src}{where} highlights matching marks in "
                f"'{target}'{not_on}{on}; it does not filter anything")
    if a.kind == "url":
        return f"{label}: opens a web link when {act} a mark in {src}{where}"
    if a.kind == "go-to-sheet":
        return f"{label}: {act} a mark in {src}{where} opens '{target}'"
    return f"{label}: {a.kind} action from {src}{where}"


def describe_filter(fd: FilterDef, max_members: int = 8) -> str:
    name = fd.field.caption or fd.field.field
    if fd.field.kind not in ("dimension", "bare", "special") and fd.field.derivation:
        name = fd.field.label()
    m = fd.mode
    def sample() -> str:
        vals = ["(null)" if v == "%null%" else v for v in fd.members[:max_members]]
        more = f", … +{fd.member_count - max_members}" if fd.member_count > max_members else ""
        return ", ".join(vals) + more
    ctx = " [context filter]" if fd.context else ""
    if m == "include" and fd.keeps_all_values:
        nulls = "all values" if "%null%" in fd.members else "all non-null values"
        return f"{name}: keeps {nulls} ({sample()}) - no practical effect{ctx}"
    if m == "include":
        return f"{name}: keeps {fd.member_count} value(s) ({sample()}){ctx}"
    if m == "exclude":
        return f"{name}: excludes {fd.member_count} value(s) ({sample()}){ctx}"
    if m == "exclude-null":
        return f"{name}: excludes null/empty values{ctx}"
    if m == "all":
        return f"{name}: all values (filter shown but not restricting){ctx}"
    if m in ("top", "bottom"):
        t = fd.top or {}
        return f"{name}: {m} {t.get('count')} by {t.get('by')}{ctx}"
    if m == "range":
        r = fd.range or {}
        if "min" in r:
            lo, hi, inc = r.get("min"), r.get("max"), r.get("included_values") or "in-range"
            if lo is None and hi is None:
                return f"{name}: {inc} values only{ctx}" if inc != "all" else f"{name}: all values{ctx}"
            if lo is None:
                return f"{name}: at most {hi} ({inc}){ctx}"
            if hi is None:
                return f"{name}: at least {lo} ({inc}){ctx}"
            return f"{name}: between {lo} and {hi} ({inc}){ctx}"
        return f"{name}: from {r.get('from')} to {r.get('to')}{ctx}"
    if m == "relative":
        r = fd.relative or {}
        return f"{name}: relative date, {r.get('period_type')} {r.get('first')} to {r.get('last')} (anchor {r.get('anchor')}){ctx}"
    if m == "condition":
        return f"{name}: condition {fd.condition}{ctx}"
    if m == "action":
        return describe_action_filter(fd, name) + ctx
    if m == "set":
        return f"keeps members of the set '{name}'{ctx}"
    if m == "crossjoin":
        return f"{name}: multi-field selection{ctx}"
    return f"{name}: filter ({', '.join(fd.functions) or fd.kind}){ctx}"


def describe_worksheet(ws: Worksheet, model: WorkbookModel | None = None) -> str:
    parts = [f"Worksheet '{ws.name}'"]
    if ws.title and not ws.title_is_placeholder and ws.title != ws.name:
        parts[0] += f" (title: '{ws.title}')"
    chart = ws.chart_type or infer_chart_type(ws)
    parts.append(f"is {'an' if chart[:1].lower() in 'aeiou' else 'a'} {chart}.")
    if ws.rows.raw:
        parts.append(f"Rows: {ws.rows.describe()}.")
    if ws.cols.raw:
        parts.append(f"Columns: {ws.cols.describe()}.")
    enc = {}
    for p in ws.panes:
        for e in p.encodings:
            enc.setdefault(e.channel, [])
            lbl = _label(e.field)
            if lbl not in enc[e.channel]:
                enc[e.channel].append(lbl)
    if enc:
        parts.append("Marks: " + "; ".join(f"{ch} = {', '.join(v)}" for ch, v in enc.items()) + ".")
    if ws.filters:
        parts.append("Filters: " + " | ".join(describe_filter(f) for f in ws.filters) + ".")
    if ws.table_calcs:
        parts.append("Table calculations: " + ", ".join(
            f"{t.type or 'calculation'} computed along {t.ordering_type or 'default'}" for t in ws.table_calcs) + ".")
    refl = [rl for p in ws.panes for rl in p.reference_lines]
    if refl:
        parts.append(f"Reference lines: {', '.join(r.formula or '?' for r in refl)}.")
    if ws.hidden:
        parts.append("Hidden (shown only inside dashboards).")
    if ws.in_dashboards:
        parts.append(f"Used in: {', '.join(ws.in_dashboards)}.")
    return " ".join(parts)


def describe_dashboard(db: Dashboard, model: WorkbookModel) -> str:
    lines = [f"{'Story' if db.kind == 'storyboard' else 'Dashboard'} '{db.name}'"
             + (f" titled '{db.title}'" if db.title and db.title != db.name and db.title != '<Sheet Name>' else "")
             + f" shows {len(db.sheets)} sheet(s)."]
    for s in db.sheets:
        ws = model.worksheet(s)
        lines.append(f"- {describe_worksheet(ws, model) if ws else s + ' (not a worksheet)'}")
    if db.quick_filters:
        lines.append("Quick filters: " + ", ".join(r.caption or r.field for r in db.quick_filters) + ".")
    if db.parameter_controls:
        lines.append("Parameter controls: " + ", ".join(db.parameter_controls) + ".")
    if db.text_blocks:
        lines.append("Text: " + " / ".join(t[:120] for t in db.text_blocks[:5]) + ".")
    if db.story_points:
        lines.append("Story points: " + ", ".join(db.story_points) + ".")
    extras = []
    if db.legends:
        extras.append(f"{db.legends} legend(s)")
    if db.images:
        extras.append(f"{db.images} image(s)")
    if db.device_layouts:
        extras.append(f"phone/tablet layouts: {', '.join(db.device_layouts)}")
    if extras:
        lines.append("Also: " + "; ".join(extras) + ".")
    return "\n".join(lines)


def overview(model: WorkbookModel) -> dict:
    calcs = model.calculated_fields
    kinds = Counter(f.formula_info.kind for f in calcs if f.formula_info)
    return {
        "source": model.source,
        "tableau": {"version": model.version, "build": model.source_build, "platform": model.source_platform},
        "datasources": [{
            "name": d.name, "caption": d.caption,
            "connections": sorted({c.cls for c in d.connections if c.cls}),
            "published": d.is_published,
            "extract": bool(d.extract and d.extract.enabled),
            "tables": [o.name for o in d.objects] or ([d.relation.name] if d.relation and d.relation.name else []),
            "relationships": len(d.relationships),
            "fields": len([f for f in d.fields if f.kind != "parameter"]),
            "physical_fields": len(d.physical_fields),
            "calculated": len([f for f in d.fields if f.kind == "calculated"]),
            "bins": len([f for f in d.fields if f.kind == "bin"]),
            "groups": len([f for f in d.fields if f.kind == "group"]),
            "sets": len(d.sets),
        } for d in model.datasources if not d.is_parameters],
        "parameters": [p.caption or p.name for p in model.parameters],
        "calculations": {"total": len(calcs), "by_kind": dict(kinds),
                         "lod": sum(len(f.formula_info.lods) for f in calcs if f.formula_info),
                         "unused": [f.caption or f.name for f in calcs if not f.used_by]},
        "worksheets": len(model.worksheets),
        "hidden_worksheets": len(model.hidden_worksheets),
        "chart_types": dict(Counter(w.chart_type for w in model.worksheets)),
        "dashboards": [{"name": d.name, "kind": d.kind, "sheets": len(d.sheets),
                        "quick_filters": len(d.quick_filters)} for d in model.dashboards],
        "actions": dict(Counter(a.kind for a in model.actions)),
        "filters": dict(Counter(f.mode for w in model.worksheets for f in w.filters)),
        "size": {k: model.size_report.get(k) for k in ("total_bytes", "thumbnail_bytes", "filter_bytes",
                                                          "median_worksheet_bytes", "token_estimate_rough")},
        "unresolved_refs": len(model.unresolved_refs),
        "unsupported": model.unsupported[:20],
        "warnings": model.warnings[:20],
    }
