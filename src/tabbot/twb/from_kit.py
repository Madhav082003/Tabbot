"""Adapter: the parser kit's rich model (tabbot.tparse) -> the engine model (tabbot.twb.model).

The kit parser (tested on 60 public workbooks) is now the only XML parser. The data engine (view rebuild,
analyses, checks, lints, Claude tools) was written against the smaller engine model; this module converts one
into the other so the engine keeps working unchanged. Rules:
- nothing is invented: every value comes from the kit model;
- constructs the engine cannot compute (sets, bins, groups, table calcs, action filters, top-N, conditions) are
  passed through with their real derivation / mode, so the rebuild marks the sheet "approximate" with a reason.
"""

from __future__ import annotations

import re
from pathlib import PureWindowsPath

from tabbot.tparse import model as K
from tabbot.tparse.fieldref import FieldRef as KRef
from tabbot.twb.fieldref import FieldRef
from tabbot.twb.model import (
    Dashboard,
    DataSource,
    Field,
    Filter,
    QuickFilter,
    Thumbnail,
    WorkbookModel,
    Worksheet,
    Zone,
)

NUMERIC = {"integer", "real", "int", "double", "float", "numeric", "big_int", "decimal"}


def ref(r: KRef | None) -> FieldRef | None:
    """Kit field reference -> engine field reference (same datasource, field, derivation)."""
    if r is None:
        return None
    if r.table_calc:
        deriv = r.table_calc                     # engine kind -> "table_calc" (reported as unsupported)
    elif r.kind == "special":
        deriv = ""
    elif r.kind == "bare":
        deriv = "none"
    else:
        deriv = r.derivation or "none"
    return FieldRef(datasource=r.datasource, derivation=deriv, field=r.field, extra=list(r.modifiers),
                    type_key=r.type_key or "?", raw=r.raw, caption=r.caption)


def _basename(path: str) -> str:
    return PureWindowsPath(path).name if ("\\" in path or ":" in path) else path.rsplit("/", 1)[-1]


def _quote_table(t: str) -> str:
    parts = re.findall(r"\[((?:[^\]]|\]\])+)\]", t)
    return ".".join(f'"{p}"' for p in parts) if parts else f'"{t}"'


def _datasource(kds: K.DataSource) -> DataSource:
    ds = DataSource(name=kds.name, caption=kds.caption)
    direct = [c for c in kds.connections if c.cls and c.cls != "federated"]
    if direct:
        c = direct[0]
        ds.connection_class = c.cls
        loc = c.filename or c.dbname or ""
        if loc:
            ds.connection_file = _basename(loc)
        ds.connection_has_local_path = any(
            bool(re.search(r"[\\/]|^[A-Za-z]:", x.filename or "")) or bool(x.server) for x in kds.connections)
    if kds.extract is not None and kds.extract.enabled:
        ds.has_extract = True
        ds.extract_tables = [_quote_table(t) for t in (kds.extract.hyper_tables or kds.extract.tables)]

    by_name = {f.name: f for f in kds.fields}
    calc_refs: dict[str, list[str]] = {}         # field -> captions of calcs that reference it
    for f in kds.fields:
        if f.formula_info:
            for dep in f.formula_info.fields:
                calc_refs.setdefault(dep, []).append(f.caption or f.name)

    fields: dict[str, Field] = {}
    for p in kds.physical_fields:
        name = (p.local_name or "").strip("[]") or (p.remote_name or "")
        if not name or name in fields:
            continue
        kf = by_name.get(name)
        dtype = (kf.datatype if kf and kf.datatype else p.local_type)
        fields[name] = Field(datasource=kds.name, internal_name=name, caption=(kf.caption if kf else None) or name,
                             datatype=dtype, role=(kf.role if kf and kf.role else
                                                   "measure" if (dtype or "").lower() in NUMERIC else "dimension"),
                             used_by_sheets=list(kf.used_directly_by) if kf else [])
    for kf in kds.fields:
        if kf.name in fields:
            continue
        fields[kf.name] = Field(
            datasource=kds.name, internal_name=kf.name, caption=kf.caption or kf.name, datatype=kf.datatype,
            role=kf.role, is_calculated=kf.kind == "calculated", is_parameter=kf.kind == "parameter",
            formula=kf.formula if kf.kind == "calculated" else None,
            formula_readable=kf.formula_readable if kf.kind == "calculated" else None,
            used_by_sheets=list(kf.used_directly_by))
    for name, f in fields.items():
        f.used_by_calcs = [c for c in dict.fromkeys(calc_refs.get(name, [])) if c != f.caption]
    ds.fields = list(fields.values())
    return ds


def _filter(fd: K.FilterDef) -> Filter:
    mode, members = fd.mode, list(fd.members)
    if mode == "exclude-null":
        mode, members = "exclude", ["%null%"]
    elif mode == "action":
        mode = "all"          # an action filter with nothing selected shows every value
    rng = fd.range or {}
    lo = rng.get("min") if "min" in rng else rng.get("from")
    hi = rng.get("max") if "max" in rng else rng.get("to")
    return Filter(field=ref(fd.field), kind=fd.kind, mode=mode, members=members, member_count=len(members),
                  min=lo, max=hi, raw_function=(fd.functions[0] if fd.functions else None))


SUPPORTED_FILTERS = {"include", "exclude", "all", "range"}


def _worksheet(kws: K.Worksheet, model: K.WorkbookModel) -> Worksheet:
    ws = Worksheet(name=kws.name, title=None if kws.title_is_placeholder else kws.title,
                   rows_raw=kws.rows.raw, cols_raw=kws.cols.raw, hidden=kws.hidden, xml_bytes=kws.xml_bytes,
                   sort_count=len(kws.sorts), table_calcs=len(kws.table_calcs))
    if kws.title_is_placeholder and kws.title:
        ws.title = kws.title
    sources = [d for d in kws.datasources if d != "Parameters"]
    ws.datasource = sources[0] if sources else None
    if len(sources) > 1:
        ws.unsupported.append(f"uses {len(sources)} data sources (blending)")
    ws.rows = [ref(r) for r in kws.rows.refs]
    ws.cols = [ref(r) for r in kws.cols.refs]
    ws.marks = [p.mark for p in kws.panes if p.mark]
    ws.mark = ws.marks[0] if ws.marks else None
    enc: dict[str, list[FieldRef]] = {}
    for p in kws.panes:
        for e in p.encodings:
            r = ref(e.field)
            if r.raw not in {x.raw for x in enc.get(e.channel, [])}:
                enc.setdefault(e.channel, []).append(r)
    ws.encodings = enc
    for fd in kws.filters:
        f = _filter(fd)
        ws.filters.append(f)
        if f.mode not in SUPPORTED_FILTERS:
            ws.unsupported.append(f"filter on '{fd.field.caption or fd.field.field}' of an unsupported type "
                                  f"({fd.kind}/{fd.mode})")
    if kws.table_calcs:
        ws.unsupported.append(f"{len(kws.table_calcs)} table calculation(s)")
    for kr in list(kws.rows.refs) + list(kws.cols.refs) + [e.field for p in kws.panes for e in p.encodings]:
        if kr.table_calc:
            ws.unsupported.append(f"table calculation '{kr.label()}' (not computed in redraws)")
        elif kr.kind == "unknown":
            ws.unsupported.append(f"field reference '{kr.raw}' (unknown derivation)")
    if kws.rows.error or kws.cols.error:
        ws.unsupported.append("a shelf could not be fully parsed")
    if f"worksheet {kws.name!r} has no <table>" in model.warnings:
        ws.unsupported.append("worksheet has no <table> element")
    ws.unsupported = list(dict.fromkeys(ws.unsupported))
    return ws


def _dashboard(kdb: K.Dashboard) -> Dashboard:
    db = Dashboard(name=kdb.name, title=kdb.title or kdb.name, sheets=list(kdb.sheets), legends=kdb.legends,
                   has_device_layouts=bool(kdb.device_layouts), size=dict(kdb.size))
    seen = set()
    for z in kdb.zones:
        if z.kind in ("sheet", "sheet-or-object") and z.name in kdb.sheets and z.name not in seen:
            seen.add(z.name)
            db.zones.append(Zone(name=z.name, x=z.x or 0, y=z.y or 0, w=z.w or 0, h=z.h or 0))
        elif z.kind == "filter" and z.field is not None:
            r = ref(z.field)
            if r.raw not in {q.field.raw for q in db.quick_filters}:
                db.quick_filters.append(QuickFilter(field=r, mode=z.mode, sheet=z.name))
    return db


def to_engine(km: K.WorkbookModel) -> WorkbookModel:
    m = WorkbookModel(version=km.version, source_build=km.source_build)
    for kds in km.datasources:
        if kds.is_parameters:
            continue
        m.datasources.append(_datasource(kds))
    m.parameters = [Field(datasource="Parameters", internal_name=p.name, caption=p.caption or p.name,
                          datatype=p.datatype, is_parameter=True) for p in km.parameters]
    m.worksheets = [_worksheet(w, km) for w in km.worksheets]
    m.dashboards = [_dashboard(d) for d in km.dashboards]
    m.thumbnails = [Thumbnail(name=t.name, width=t.width, height=t.height, bytes=t.base64_bytes * 3 // 4)
                    for t in km.thumbnails]
    m.actions = len(km.actions)
    sr = km.size_report or {}
    m.size_report = {
        "total_kb": round(sr.get("total_bytes", 0) / 1024, 1),
        "token_estimate_rough": sr.get("token_estimate_rough", 0),
        "sections_kb": {k: round(v / 1024, 1) for k, v in (sr.get("sections") or {}).items()},
        "largest_worksheets_kb": [(x["name"], round(x["bytes"] / 1024, 1)) for x in sr.get("largest_worksheets", [])],
        "median_worksheet_kb": round(sr.get("median_worksheet_bytes", 0) / 1024, 1),
        "filters_kb": round(sr.get("filter_bytes", 0) / 1024, 1),
    }
    m.unsupported = list(dict.fromkeys(km.unsupported + [w for w in km.warnings if "failed" in w]))
    if km.parameters:
        m.unsupported.append(f"{len(km.parameters)} parameter(s) (shown, not used in redraws)")
    for kds in km.datasources:
        for f in kds.fields:
            if f.formula_info and f.formula_info.lods:
                m.unsupported.append(f"LOD expression in calculated field '{f.caption or f.name}'")
    return m
