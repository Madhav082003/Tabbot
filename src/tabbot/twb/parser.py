"""Parse a .twb (Tableau workbook XML) into a WorkbookModel.

Uses defusedxml (the file is untrusted). Element paths follow docs/TABLEAU_REFERENCE.md.
Unknown constructs are recorded in `unsupported`; the parser never fails on them.
"""

from __future__ import annotations

import base64
import re
import statistics
import struct
from pathlib import Path, PureWindowsPath
from xml.etree.ElementTree import Element, tostring

from defusedxml import ElementTree as SafeET

from tabbot.twb.fieldref import FieldRef, parse_ref, parse_shelf
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

FIELD_TOKEN = re.compile(r"\[((?:[^\]]|\]\])+)\]")
LOD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.I)
SORT_TAGS = {"sort", "computed-sort", "natural-sort", "manual-sort", "shelf-sort-v2", "shelf-sorts"}
LEGEND_TYPES = {"color", "size", "shape", "map"}
NUMERIC_TYPES = {"integer", "real", "int", "double", "float", "numeric", "big_int"}


def _text(el: Element | None) -> str | None:
    if el is None:
        return None
    t = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
    return t or None


def _strip_brackets(name: str | None) -> str:
    name = name or ""
    return name[1:-1].replace("]]", "]") if name.startswith("[") and name.endswith("]") else name


def _member_value(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return raw


def _basename(path: str) -> str:
    return PureWindowsPath(path).name if ("\\" in path or ":" in path) else Path(path).name


# ------------------------------------------------------------------------------------------- data sources


def _parse_datasource(ds_el: Element, unsupported: list[str]) -> DataSource:
    ds = DataSource(name=ds_el.get("name") or "", caption=ds_el.get("caption"))

    conn = ds_el.find("connection")
    inner_conn = ds_el.find("./connection/named-connections/named-connection/connection")
    c = inner_conn if inner_conn is not None else conn
    if c is not None:
        ds.connection_class = c.get("class")
        loc = c.get("filename") or c.get("dbname") or ""
        if loc:
            ds.connection_file = _basename(loc)
            ds.connection_has_local_path = bool(re.search(r"[\\/]|^[A-Za-z]:", loc))
        if c.get("server"):
            ds.connection_has_local_path = True

    ext = ds_el.find("extract")
    if ext is not None and ext.get("enabled", "true") != "false":
        ds.has_extract = True
        for rel in ext.iter("relation"):
            table = rel.get("table")
            if rel.get("type", "table") == "table" and table:
                parts = [p.replace("]]", "]") for p in re.findall(r"\[((?:[^\]]|\]\])+)\]", table)]
                ds.extract_tables.append(".".join(f'"{p}"' for p in parts) if parts else f'"{table}"')

    fields: dict[str, Field] = {}
    # physical fields: metadata records appear under the source connection AND the extract; de-duplicate
    for mr in ds_el.iter("metadata-record"):
        if mr.get("class") != "column":
            continue
        name = _strip_brackets(mr.findtext("local-name")) or (mr.findtext("remote-name") or "")
        if not name or name in fields:
            continue
        dtype = mr.findtext("local-type")
        fields[name] = Field(datasource=ds.name, internal_name=name, caption=name, datatype=dtype,
                             role="measure" if dtype in NUMERIC_TYPES else "dimension")

    for col in ds_el.findall("column"):
        name = _strip_brackets(col.get("name"))
        if not name:
            continue
        calc = col.find("calculation")
        f = fields.get(name) or Field(datasource=ds.name, internal_name=name, caption=name)
        f.caption = col.get("caption") or f.caption
        f.datatype = col.get("datatype") or f.datatype
        f.role = col.get("role") or f.role
        if col.get("param-domain-type"):
            f.is_parameter = True
        if calc is not None:
            if calc.get("formula") is not None:
                f.is_calculated = True
                f.formula = calc.get("formula")
                if LOD_RE.search(f.formula):
                    unsupported.append(f"LOD expression in calculated field '{f.caption}'")
            elif calc.get("class") in ("categorical-bin", "bin"):
                unsupported.append(f"group/bin field '{f.caption}' ({calc.get('class')})")
        fields[name] = f

    # readable formulas + calc-to-calc usage
    by_name = fields
    for f in fields.values():
        if not f.formula:
            continue
        readable = f.formula
        for tok in set(FIELD_TOKEN.findall(f.formula)):
            key = tok.replace("]]", "]")
            target = by_name.get(key)
            if target is None:
                continue
            if target.caption != key:
                readable = readable.replace(f"[{tok}]", f"[{target.caption}]")
            if f.caption not in target.used_by_calcs and target is not f:
                target.used_by_calcs.append(f.caption)
        f.formula_readable = readable

    ds.fields = list(fields.values())
    return ds


# ------------------------------------------------------------------------------------------- filters


def _parse_filter(f_el: Element) -> Filter | None:
    ref = parse_ref(f_el.get("column") or "")
    if ref is None:
        return None
    cls = f_el.get("class") or "unknown"
    flt = Filter(field=ref, kind=cls if cls in ("categorical", "quantitative", "relative-date") else "unknown",
                 mode="unknown")

    if cls == "categorical":
        top = f_el.find("groupfilter")
        if top is None:
            flt.mode = "all"
            return flt
        func = top.get("function")
        flt.raw_function = func
        members = [_member_value(g.get("member")) for g in top.iter("groupfilter")
                   if g.get("function") == "member" and g.get("member") is not None]
        if func == "member":
            flt.mode, flt.members = "include", members
        elif func == "union":
            others = {g.get("function") for g in top.iter("groupfilter")} - {"union", "member"}
            flt.mode, flt.members = ("include", members) if not others else ("unknown", members)
        elif func == "except":
            flt.mode, flt.members = "exclude", members
        elif func == "level-members":
            flt.mode = "all"
        elif func == "range":
            flt.mode, flt.min, flt.max = "range", top.get("from"), top.get("to")
        else:
            flt.mode = "unknown"
    elif cls == "quantitative":
        flt.mode = "range"
        flt.min, flt.max = f_el.findtext("min"), f_el.findtext("max")
        flt.raw_function = f_el.get("included-values")
    flt.member_count = len(flt.members)
    return flt


# ------------------------------------------------------------------------------------------- worksheets


def _parse_worksheet(ws_el: Element) -> Worksheet:
    ws = Worksheet(name=ws_el.get("name") or "", xml_bytes=len(tostring(ws_el)))
    ws.title = _text(ws_el.find("./layout-options/title"))
    table = ws_el.find("table")
    if table is None:
        ws.unsupported.append("worksheet has no <table> element")
        return ws
    view = table.find("view")

    if view is not None:
        names = [d.get("name") for d in view.findall("./datasources/datasource")
                 if d.get("name") and d.get("name") != "Parameters"]
        ws.datasource = names[0] if names else None
        if len(names) > 1:
            ws.unsupported.append(f"uses {len(names)} data sources (blending)")
        for f_el in view.findall("filter"):
            flt = _parse_filter(f_el)
            if flt is None:
                ws.unsupported.append("filter without a field reference")
                continue
            if flt.mode == "unknown":
                ws.unsupported.append(f"filter on '{flt.field.field}' of an unsupported type "
                                      f"({flt.kind}/{flt.raw_function})")
            ws.filters.append(flt)
        ws.sort_count = sum(1 for e in view.iter() if e.tag in SORT_TAGS)

    ws.rows_raw = (table.findtext("rows") or "").strip()
    ws.cols_raw = (table.findtext("cols") or "").strip()
    ws.rows, ws.cols = parse_shelf(ws.rows_raw), parse_shelf(ws.cols_raw)

    encodings: dict[str, list[FieldRef]] = {}
    for pane in table.iter("pane"):
        mark = pane.find("mark")
        if mark is not None and mark.get("class"):
            ws.marks.append(mark.get("class"))
        enc = pane.find("encodings")
        for e in (enc if enc is not None else []):
            ref = parse_ref(e.get("column") or "")
            if ref is not None and ref.raw not in {r.raw for r in encodings.get(e.tag, [])}:
                encodings.setdefault(e.tag, []).append(ref)
    ws.encodings = encodings
    ws.mark = ws.marks[0] if ws.marks else None

    ws.table_calcs = sum(1 for _ in ws_el.iter("table-calc"))
    if ws.table_calcs:
        ws.unsupported.append(f"{ws.table_calcs} table calculation(s)")
    for r in ws.all_refs():
        if r.kind in ("table_calc", "unknown"):
            ws.unsupported.append(f"field reference '{r.raw}' ({r.kind})")
    return ws


# ------------------------------------------------------------------------------------------- dashboards


def _parse_dashboard(db_el: Element, sheet_names: set[str]) -> Dashboard:
    db = Dashboard(name=db_el.get("name") or "")
    db.title = _text(db_el.find("./layout-options/title")) or db.name
    size = db_el.find("size")
    db.size = dict(size.attrib) if size is not None else {}
    layouts = db_el.find("devicelayouts")
    db.has_device_layouts = layouts is not None and layouts.find("devicelayout") is not None

    zones = db_el.find("zones")  # direct child only: device (phone) layouts repeat the zones
    for z in (zones.iter("zone") if zones is not None else []):
        t = z.get("type-v2")
        name = z.get("name")
        if t is None and name in sheet_names:
            if name not in db.sheets:
                db.sheets.append(name)
                db.zones.append(Zone(name=name, **{k: int(float(z.get(k, 0))) for k in ("x", "y", "w", "h")}))
        elif t == "filter":
            ref = parse_ref(z.get("param") or "")
            if ref is not None and ref.raw not in {q.field.raw for q in db.quick_filters}:
                db.quick_filters.append(QuickFilter(field=ref, mode=z.get("mode"), sheet=name))
        elif t in LEGEND_TYPES:
            db.legends += 1
    return db


# ------------------------------------------------------------------------------------------- thumbnails


def _parse_thumbnails(root: Element, out_dir: Path | None) -> list[Thumbnail]:
    thumbs = []
    for i, t in enumerate(root.iter("thumbnail")):
        th = Thumbnail(name=t.get("name") or f"thumbnail_{i}")
        try:
            png = base64.b64decode(t.text or "")
        except (ValueError, TypeError):
            png = b""
        th.bytes = len(png)
        if png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) >= 24:
            th.width, th.height = struct.unpack(">II", png[16:24])
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                f = out_dir / f"{i:03d}.png"
                f.write_bytes(png)
                th.file = str(f)
        thumbs.append(th)
    return thumbs


# ------------------------------------------------------------------------------------------- size report


def _size_report(root: Element, raw_len: int, worksheets: list[Worksheet]) -> dict:
    sections = {child.tag: round(len(tostring(child)) / 1024, 1) for child in root}
    ws_sizes = sorted(((w.xml_bytes, w.name) for w in worksheets), reverse=True)
    ws_root = root.find("worksheets")
    filter_bytes = sum(len(tostring(f)) for f in ws_root.iter("filter")) if ws_root is not None else 0
    return {
        "total_kb": round(raw_len / 1024, 1),
        "token_estimate_rough": int(raw_len / 3.5),
        "sections_kb": sections,
        "largest_worksheets_kb": [(n, round(b / 1024, 1)) for b, n in ws_sizes[:10]],
        "median_worksheet_kb": round(statistics.median([b for b, _ in ws_sizes]) / 1024, 1) if ws_sizes else 0,
        "filters_kb": round(filter_bytes / 1024, 1),
    }


# ------------------------------------------------------------------------------------------- entry point


def parse_workbook(twb_path: Path | str, thumbnails_dir: Path | None = None) -> WorkbookModel:
    raw = Path(twb_path).read_bytes()
    root = SafeET.fromstring(raw)
    model = WorkbookModel(version=root.get("version"), source_build=root.get("source-build"))

    ds_root = root.find("datasources")
    for ds_el in (ds_root.findall("datasource") if ds_root is not None else []):
        ds = _parse_datasource(ds_el, model.unsupported)
        if ds.name == "Parameters":
            for f in ds.fields:
                f.is_parameter = True
            model.parameters.extend(ds.fields)
        else:
            model.datasources.append(ds)
    if model.parameters:
        model.unsupported.append(f"{len(model.parameters)} parameter(s) (shown, not used in redraws)")

    ws_root = root.find("worksheets")
    model.worksheets = [_parse_worksheet(w) for w in (ws_root.findall("worksheet") if ws_root is not None else [])]

    hidden = {w.get("name") for w in root.iter("window")
              if w.get("class") == "worksheet" and w.get("hidden") == "true"}
    for ws in model.worksheets:
        ws.hidden = ws.name in hidden

    # resolve captions and record usage
    for ws in model.worksheets:
        for ref in ws.all_refs():
            f = model.field(ref.datasource, ref.field)
            if f is not None:
                ref.caption = f.caption
                if ws.name not in f.used_by_sheets:
                    f.used_by_sheets.append(ws.name)

    names = {w.name for w in model.worksheets}
    db_root = root.find("dashboards")
    db_els = db_root.findall("dashboard") if db_root is not None else []
    model.dashboards = [_parse_dashboard(d, names) for d in db_els]
    for db in model.dashboards:
        for q in db.quick_filters:
            f = model.field(q.field.datasource, q.field.field)
            if f is not None:
                q.field.caption = f.caption

    model.thumbnails = _parse_thumbnails(root, thumbnails_dir)
    actions = root.find("actions")
    model.actions = len(list(actions)) if actions is not None else 0
    model.size_report = _size_report(root, len(raw), model.worksheets)
    return model
