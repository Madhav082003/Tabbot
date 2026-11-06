"""Parse a Tableau workbook (.twb / .twbx) into a WorkbookModel.

Built from the patterns mined across 60 public workbooks plus one private one (docs/PATTERNS.md). Rules:
- never crash on an unknown construct: record it in model.unsupported / model.warnings and carry on;
- every field reference is resolved to a data-source field where possible; failures go to unresolved_refs;
- no data values are read from extracts here (that is tabbot.data).
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from xml.etree.ElementTree import Element, tostring

from . import formula as fx
from .fieldref import OBJECT_ID_MARKER, FieldRef, find_refs, parse_inner, parse_ref, parse_shelf
from .model import (
    Action,
    BinDef,
    ColumnInstance,
    Connection,
    Dashboard,
    DataSource,
    DrillPath,
    Encoding,
    Extract,
    Field,
    FilterDef,
    GroupDef,
    Pane,
    Parameter,
    PhysicalField,
    ReferenceLine,
    Relation,
    Relationship,
    SetDef,
    SortDef,
    TableCalcDef,
    Thumbnail,
    Window,
    WorkbookModel,
    Worksheet,
    Zone,
)
from .package import PackageInfo, read_workbook
from .xmlutil import children, first, load_twb, strip_quotes, text_of, unescape_member

GENERATED_FIELDS = {"Number of Records", "Measure Names", "Measure Values", "Multiple Values",
                    "Latitude (generated)", "Longitude (generated)", "Geometry (generated)",
                    "Forecast Indicator", "Forecast", "Cluster", "Clusters"}


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _bracket_name(v: str | None) -> str:
    """'[Sales]' -> 'Sales' (with ]] unescaped); bare names returned as-is."""
    if not v:
        return ""
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return v.replace("]]", "]")


# ====================================================================== data sources

def _parse_connection(el: Element, name: str | None = None, caption: str | None = None) -> Connection:
    a = dict(el.attrib)
    known = {"class", "server", "dbname", "schema", "filename", "port", "username", "directory"}
    return Connection(name=name, caption=caption, cls=a.get("class"), server=a.get("server"),
                      dbname=a.get("dbname"), schema_name=a.get("schema"), filename=a.get("filename"),
                      directory=a.get("directory"),
                      port=a.get("port"), username=a.get("username"),
                      other={k: v for k, v in a.items() if k not in known and "password" not in k.lower()})


def _expr_text(el: Element | None) -> str:
    """Render <expression op=...> trees as readable text: '[a] = [b]', 'AND(...)'."""
    if el is None:
        return ""
    kids = [k for k in el if k.tag == "expression"]
    op = el.get("op", "")
    if not kids:
        return op
    parts = [_expr_text(k) for k in kids]
    if len(parts) == 2 and not re.match(r"^[A-Za-z_]+$", op):
        return f"{parts[0]} {op} {parts[1]}"
    return f"{op}({', '.join(parts)})"


def _parse_relation(el: Element) -> Relation:
    rel = Relation(type=el.get("type", "unknown"), name=el.get("name"), table=el.get("table"),
                   connection=el.get("connection"), join=el.get("join"))
    clause = first(el, "clause")
    if clause is not None:
        rel.clause = _expr_text(first(clause, "expression"))
    if rel.type == "text":
        rel.custom_sql = (el.text or "").strip() or None
    rel.columns = [c.get("name", "") for c in children(el, "columns/column") if c.get("name")]
    rel.children = [_parse_relation(c) for c in el.findall("relation")]
    return rel


def _relation_columns(rel: Relation | None) -> set[str]:
    if rel is None:
        return set()
    out = set(rel.columns)
    for c in rel.children:
        out |= _relation_columns(c)
    return out


def _parse_datasource(el: Element, model: WorkbookModel) -> DataSource:
    name = el.get("name", "")
    ds = DataSource(name=name, caption=el.get("caption"), is_parameters=(name == "Parameters"))

    conn = first(el, "connection")
    if conn is not None:
        named = children(conn, "named-connections/named-connection")
        if named:
            for nc in named:
                inner = first(nc, "connection")
                if inner is not None:
                    ds.connections.append(_parse_connection(inner, nc.get("name"), nc.get("caption")))
        if conn.get("class") and conn.get("class") != "federated":
            ds.connections.append(_parse_connection(conn))
        ds.is_published = any(c.cls == "sqlproxy" for c in ds.connections) or conn.get("class") == "sqlproxy"
        top_rel = first(conn, "relation")
        if top_rel is not None:
            ds.relation = _parse_relation(top_rel)
        for mr in children(conn, "metadata-records/metadata-record"):
            if mr.get("class") == "column":
                ds.physical_fields.append(PhysicalField(
                    remote_name=mr.findtext("remote-name"), local_name=mr.findtext("local-name"),
                    local_type=mr.findtext("local-type"), parent=mr.findtext("parent-name"),
                    aggregation=mr.findtext("aggregation")))

    ext = first(el, "extract")
    if ext is not None:
        ext_tables = [r.get("name") or r.get("table") for r in ext.iter("relation") if r.get("type") == "table"]
        ds.extract = Extract(enabled=ext.get("enabled") == "true", tables=[t for t in ext_tables if t],
                             hyper_tables=[r.get("table") for r in ext.iter("relation")
                                           if r.get("type") == "table" and r.get("table")],
                             incremental=(first(ext, "connection/refresh") is not None and
                                          first(ext, "connection/refresh").get("increment-enabled") == "true"),
                             filters=len(ext.findall(".//filter")),
                             dbname=(first(ext, "connection").get("dbname") if first(ext, "connection") is not None
                                     else None))
        # the extract repeats the same fields under its own relation name: merge by local name
        by_local = {(p.local_name or p.remote_name): p for p in ds.physical_fields}
        for mr in ext.iter("metadata-record"):
            if mr.get("class") != "column":
                continue
            local = mr.findtext("local-name") or mr.findtext("remote-name")
            if local in by_local:
                by_local[local].in_extract = True
                continue
            p = PhysicalField(remote_name=mr.findtext("remote-name"), local_name=mr.findtext("local-name"),
                              local_type=mr.findtext("local-type"), parent=mr.findtext("parent-name"),
                              aggregation=mr.findtext("aggregation"), in_extract=True)
            by_local[local] = p
            ds.physical_fields.append(p)

    og = first(el, "object-graph")
    if og is not None:
        for obj in children(og, "objects/object"):
            r = first(obj, "properties/relation")
            if r is not None:
                rel = _parse_relation(r)
                rel.name = obj.get("caption") or rel.name
                rel.object_id = obj.get("id")
                ds.objects.append(rel)
        for rs in children(og, "relationships/relationship"):
            ds.relationships.append(Relationship(
                left_object=(first(rs, "first-end-point").get("object-id") if first(rs, "first-end-point") is not None else None),
                right_object=(first(rs, "second-end-point").get("object-id") if first(rs, "second-end-point") is not None else None),
                expression=_expr_text(first(rs, "expression"))))

    for col in el.findall("column"):
        ds.fields.append(_parse_field(col, name))
    for g in el.findall("group"):
        ds.sets.append(_parse_set(g))
    for dp in children(el, "drill-paths/drill-path"):
        ds.drill_paths.append(DrillPath(name=dp.get("name", ""), fields=[_bracket_name(f.text) for f in dp.findall("field")]))
    for folder in el.findall("folder"):
        ds.folders[folder.get("name", "")] = [_bracket_name(fi.get("name")) for fi in folder.findall("folder-item")]
    return ds


def _parse_field(col: Element, ds_name: str) -> Field:
    f = Field(datasource=ds_name, name=_bracket_name(col.get("name")), caption=col.get("caption"),
              datatype=col.get("datatype"), role=col.get("role"), type=col.get("type"),
              default_aggregation=col.get("aggregation"), hidden=col.get("hidden") == "true",
              semantic_role=col.get("semantic-role"))
    desc = first(col, "desc")
    if desc is not None:
        f.description = text_of(desc) or None
    for a in children(col, "aliases/alias"):
        f.aliases[strip_quotes(a.get("key")) or ""] = a.get("value", "")
    calc = first(col, "calculation")
    if col.get("param-domain-type"):
        f.kind = "parameter"
        f.formula = calc.get("formula") if calc is not None else None
    elif calc is not None:
        cls = calc.get("class", "tableau")
        if cls == "bin":
            f.kind = "bin"
            f.bin = BinDef(source=_bracket_name(calc.get("formula")), size=calc.get("size"),
                           size_parameter=calc.get("size-parameter"), peg=calc.get("peg"))
        elif cls == "categorical-bin":
            f.kind = "group"
            groups = {}
            for b in calc.findall("bin"):
                groups[strip_quotes(b.get("value")) or ""] = len(b.findall("value"))
            f.group = GroupDef(source=_bracket_name(calc.get("column")), groups=groups,
                               other_group=calc.get("new-bin") == "true")
        else:
            f.kind = "calculated"
            f.formula = calc.get("formula")
            f.formula_info = fx.analyze(f.formula)
    elif f.name in GENERATED_FIELDS or (col.get("user:auto-column") is not None):
        f.kind = "generated"
    elif f.name.startswith(OBJECT_ID_MARKER):
        # '[__tableau_internal_object_id__].[Orders_ABC...]': the row count of a logical table
        f.kind = "generated"
        f.caption = f.caption or "rows of " + re.sub(r"_[0-9A-F]{32}$", "", f.name.split("].[")[-1])
    return f


def _parse_set(g: Element) -> SetDef:
    s = SetDef(name=_bracket_name(g.get("name")), caption=g.get("caption"), ui_builder=g.get("user:ui-builder"))
    gf = first(g, "groupfilter")
    if gf is None:
        return s
    funcs = [x.get("function") for x in g.iter("groupfilter")]
    s.level = next((_bracket_name(x.get("level")) for x in g.iter("groupfilter") if x.get("level")), None)
    s.member_count = sum(1 for x in g.iter("groupfilter") if x.get("function") == "member")
    if "end" in funcs or "order" in funcs:
        s.mode = "top"
    elif "filter" in funcs:
        s.mode = "condition"
        s.condition = next((x.get("expression") for x in g.iter("groupfilter") if x.get("expression")), None)
    elif "intersection" in funcs or "except" in funcs and "level-members" not in funcs:
        s.mode = "combined"
    elif s.member_count:
        s.mode = "members"
    return s


# ====================================================================== worksheets

def _collect_scoped_calcs(container: Element, model: WorkbookModel, scope: str, owner: str) -> None:
    """Ad-hoc calculations (typed straight onto a shelf) live in the sheet's <datasource-dependencies>, not in
    the data source. Register them as data-source fields with scope='worksheet' so references resolve."""
    for dep in children(container, "datasource-dependencies"):
        ds = model.datasource(dep.get("datasource", ""))
        if ds is None:
            continue
        known = {f.name for f in ds.fields}
        for col in dep.findall("column"):
            n = _bracket_name(col.get("name"))
            if n and n not in known and first(col, "calculation") is not None:
                fld = _parse_field(col, ds.name)
                fld.scope, fld.defined_in = scope, owner
                ds.fields.append(fld)
                known.add(n)


def _filter_mode(root: Element) -> tuple[str, dict]:
    """Interpret a <groupfilter> tree. Returns (mode, extra)."""
    fn = root.get("function")
    extra: dict = {}
    if fn == "member":
        return "include", extra
    if fn == "union":
        return "include", extra
    if fn == "level-members":
        return "all", extra
    if fn == "except":
        kids = [k.get("function") for k in root]
        if kids == ["level-members", "empty-level"] or (len(kids) == 2 and kids[1] == "empty-level"):
            return "exclude-null", extra
        return "exclude", extra
    if fn == "end":
        order = first(root, "groupfilter")
        extra["top"] = {"end": root.get("end"), "count": _int(root.get("count")), "units": root.get("units"),
                        "by": order.get("expression") if order is not None else None,
                        "direction": order.get("direction") if order is not None else None}
        return ("bottom" if root.get("end") == "bottom" else "top"), extra
    if fn == "order":
        extra["top"] = {"by": root.get("expression"), "direction": root.get("direction")}
        return "top", extra
    if fn == "range":
        extra["range"] = {"from": root.get("from"), "to": root.get("to"), "level": root.get("level")}
        return "range", extra
    if fn == "filter":
        return "condition", {"condition": root.get("expression")}
    if fn == "crossjoin":
        return "crossjoin", extra
    if fn == "empty-level":
        return "exclude-null", extra
    if fn == "reorder-dimensionality":
        return "crossjoin", extra
    return "unknown", extra


def _parse_filter(el: Element, model: WorkbookModel) -> FilterDef | None:
    col = el.get("column", "")
    ref = parse_ref(col)
    if ref is None:
        # e.g. column='[Action (Ship Status,...)]' without a datasource part
        ref = FieldRef(raw=col, datasource="", inner=_bracket_name(col), field=_bracket_name(col), kind="bare")
    kind = el.get("class", "unknown")
    fd = FilterDef(field=ref, kind=kind, mode="unknown", context=el.get("context") == "true",
                   filter_group=el.get("filter-group"))
    if kind == "quantitative":
        fd.mode = "range"
        fd.range = {"min": (el.findtext("min") or None), "max": (el.findtext("max") or None),
                    "included_values": el.get("included-values")}
        return fd
    if kind == "relative-date":
        fd.mode = "relative"
        fd.relative = {"period_type": el.get("period-type-v2") or el.get("period-type"),
                       "first": _int(el.get("first-period")), "last": _int(el.get("last-period")),
                       "anchor": el.get("period-anchor"), "include_future": el.get("include-future"),
                       "include_null": el.get("include-null")}
        return fd
    root = first(el, "groupfilter")
    if root is None:
        ds = model.datasource(ref.datasource)
        if ds is not None and any(s.name == ref.field for s in ds.sets):
            fd.mode = "set"          # keep only members of the set (IN)
        else:
            model.unsupported.append(f"filter without groupfilter: class={kind} column={col[:80]}")
        return fd
    fd.functions = list(dict.fromkeys(g.get("function", "") for g in el.iter("groupfilter")))
    fd.ui_enumeration = root.get("user:ui-enumeration")
    act_ref = next((g.get("user:ui-action-filter") for g in el.iter("groupfilter") if g.get("user:ui-action-filter")),
                   None)
    fd.action_ref = _bracket_name(act_ref) if act_ref else None
    fd.is_action_filter = bool(act_ref) or ref.field.startswith("Action (")
    mode, extra = _filter_mode(root)
    if fd.is_action_filter:
        mode = "action"
    fd.mode = mode
    fd.top = extra.get("top")
    fd.range = extra.get("range")
    fd.condition = extra.get("condition")
    members = []
    for g in el.iter("groupfilter"):
        if g.get("function") == "member" and g.get("member") is not None:
            members.append(g.get("member"))
    fd.includes_null = None
    if members:
        fd.includes_null = ("%null%" in members) if mode == "include" else ("%null%" not in members if mode == "exclude" else None)
    if mode == "exclude-null":
        fd.includes_null = False
    fd.members = [m if m == "%null%" else unescape_member(strip_quotes(m) or "") for m in members]
    fd.member_count = len(fd.members)
    if mode == "unknown":
        model.unsupported.append(f"filter groupfilter function={root.get('function')} on {ref.field[:60]}")
    return fd


def _parse_sorts(view: Element) -> list[SortDef]:
    out = []
    for s in view:
        tag = s.tag
        if tag == "manual-sort":
            out.append(SortDef(kind="manual", field=parse_ref(s.get("column", "")), direction=s.get("direction"),
                               member_count=len(s.findall(".//bucket")) or len(list(s.iter())) - 1))
        elif tag == "computed-sort":
            out.append(SortDef(kind="computed", field=parse_ref(s.get("column", "")), direction=s.get("direction"),
                               using=parse_ref(s.get("using", ""))))
        elif tag == "natural-sort":
            out.append(SortDef(kind="natural", field=parse_ref(s.get("column", "")), direction=s.get("direction")))
        elif tag == "shelf-sorts":
            for ss in s.iter("shelf-sort-v2"):
                out.append(SortDef(kind="shelf", field=parse_ref(ss.get("dimension-to-sort", "")),
                                   direction=ss.get("direction"), using=parse_ref(ss.get("measure-to-sort-by", ""))))
        elif tag == "sort":
            out.append(SortDef(kind=s.get("class", "unknown"), field=parse_ref(s.get("column", "")),
                               direction=s.get("direction")))
    return out


def _parse_table_calc(ci_name: str, tc: Element) -> TableCalcDef:
    return TableCalcDef(column=ci_name, type=tc.get("type"), ordering_type=tc.get("ordering-type"),
                        ordering_fields=[_bracket_name(o.text) for o in tc.findall("order")])


def _parse_worksheet(el: Element, model: WorkbookModel) -> Worksheet:
    ws = Worksheet(name=el.get("name", ""), xml_bytes=len(tostring(el)))
    title_el = first(el, "layout-options/title")
    ws.title = text_of(title_el) or None
    ws.title_is_placeholder = ws.title in (None, "<Sheet Name>")
    table = first(el, "table")
    if table is None:
        model.warnings.append(f"worksheet {ws.name!r} has no <table>")
        return ws
    view = first(table, "view")
    if view is not None:
        for d in children(view, "datasources/datasource"):
            ws.datasources.append(d.get("name", ""))
        _collect_scoped_calcs(view, model, "worksheet", ws.name)
        for dep in children(view, "datasource-dependencies"):
            for ci in dep.findall("column-instance"):
                tc = first(ci, "table-calc")
                ws.column_instances.append(ColumnInstance(
                    name=ci.get("name", ""), column=ci.get("column"), derivation=ci.get("derivation"),
                    type=ci.get("type"), pivot=ci.get("pivot"),
                    table_calc=_parse_table_calc(ci.get("name", ""), tc) if tc is not None else None))
                if tc is not None:
                    ws.table_calcs.append(_parse_table_calc(ci.get("name", ""), tc))
        for f in view.findall("filter"):
            fd = _parse_filter(f, model)
            if fd is not None:
                ws.filters.append(fd)
        ws.sorts = _parse_sorts(view)
    ws.rows = parse_shelf(table.findtext("rows"))
    ws.cols = parse_shelf(table.findtext("cols"))
    for shelf in (ws.rows, ws.cols):
        if shelf.error:
            model.warnings.append(f"{ws.name!r}: shelf not fully parsed ({shelf.error}): {shelf.raw[:80]}")
    pages = first(table, "pages")
    if pages is not None:
        for c in pages:
            ws.pages += [parse_inner(ds, inner) for ds, inner, *_ in find_refs(c.get("column", "") or (c.text or ""))]
    for p in children(table, "panes/pane"):
        mark = first(p, "mark")
        pane = Pane(id=p.get("id"), mark=mark.get("class") if mark is not None else None,
                    customized_label=first(p, "customized-label") is not None,
                    customized_tooltip=first(p, "customized-tooltip") is not None)
        enc = first(p, "encodings")
        for e in (list(enc) if enc is not None else []):
            for ds, inner, s, end in find_refs(e.get("column", "")):
                pane.encodings.append(Encoding(channel=e.tag, field=parse_inner(ds, inner, e.get("column", "")[s:end])))
        for rl in p.findall("reference-line"):
            pane.reference_lines.append(ReferenceLine(formula=rl.get("formula"), scope=rl.get("scope"),
                                                      value_column=parse_ref(rl.get("value-column", "")),
                                                      label=rl.get("label")))
        ws.panes.append(pane)
    ws.annotations = len(table.findall("annotations/annotation"))
    ws.uses_measure_names = any(r.kind == "special" and r.field == "Measure Names" for r in ws.all_refs())
    return ws


# ====================================================================== dashboards

def _zone_kind(z: Element, worksheet_names: set[str]) -> str:
    t = z.get("type-v2") or z.get("type")
    if t:
        return t
    if z.get("name") in worksheet_names:
        return "sheet"
    if z.get("name"):
        return "sheet-or-object"
    return "unknown"


def _walk_zones(zones_el: Element | None, worksheet_names: set[str]) -> list[Zone]:
    out: list[Zone] = []

    def walk(el: Element, depth: int):
        for z in el.findall("zone"):
            kind = _zone_kind(z, worksheet_names)
            param = z.get("param")
            fref = parse_ref(param) if param and param.startswith("[") else None
            text = None
            ft = first(z, "formatted-text")
            if ft is not None:
                text = text_of(ft) or None
            out.append(Zone(id=z.get("id"), kind=kind, name=z.get("name"), param=param, field=fref,
                            mode=z.get("mode"), text=text, depth=depth, x=_int(z.get("x")), y=_int(z.get("y")),
                            w=_int(z.get("w")), h=_int(z.get("h"))))
            walk(z, depth + 1)
    if zones_el is not None:
        walk(zones_el, 0)
    return out


def _parse_dashboard(el: Element, worksheet_names: set[str], dashboard_names: set[str]) -> Dashboard:
    db = Dashboard(name=el.get("name", ""), kind=el.get("type") or "dashboard", xml_bytes=len(tostring(el)))
    db.title = text_of(first(el, "layout-options/title")) or None
    size = first(el, "size")
    db.size = dict(size.attrib) if size is not None else {}
    db.zones = _walk_zones(first(el, "zones"), worksheet_names)
    seen = []
    for z in db.zones:
        if z.kind in ("sheet", "sheet-or-object") and z.name and z.name not in seen:
            seen.append(z.name)
        elif z.kind == "filter" and z.field is not None:
            db.quick_filters.append(z.field)
        elif z.kind == "paramctrl" and z.param:
            refs = find_refs(z.param)
            db.parameter_controls.append(refs[0][1] if refs else _bracket_name(z.param))
        elif z.kind in ("color", "size", "shape", "map-legend", "legend"):
            db.legends += 1
        elif z.kind in ("text", "title") and z.text:
            db.text_blocks.append(z.text)
        elif z.kind == "bitmap":
            db.images += 1
        elif z.kind == "web":
            db.web_objects += 1
    db.sheets = seen
    for dl in children(el, "devicelayouts/devicelayout"):
        db.device_layouts.append(dl.get("name", "device"))
    for sp in el.iter("story-point"):
        db.story_points.append(sp.get("caption") or sp.get("captured-sheet") or "")
    return db


# ====================================================================== actions, windows, thumbnails

COMMAND_KINDS = [("tsl-filter", "filter"), ("brush", "highlight"), ("url", "url"), ("param", "parameter"),
                 ("set", "set"), ("goto", "go-to-sheet"), ("navigate", "go-to-sheet")]


def _parse_action(el: Element) -> Action:
    a = Action(name=_bracket_name(el.get("name")), caption=el.get("caption"))
    act = first(el, "activation")
    a.activation = act.get("type") if act is not None else None
    src = first(el, "source")
    if src is not None:
        a.source_dashboard = src.get("dashboard")
        a.source_worksheet = src.get("worksheet")
        a.excluded_sheets = [x.get("name", "") for x in src.findall("exclude-sheet")]
    cmd = first(el, "command")
    if cmd is not None:
        a.command = cmd.get("command")
        a.params = {p.get("name", ""): p.get("value", "") for p in cmd.findall("param")}
    lc = (a.command or "").lower()
    a.kind = next((k for key, k in COMMAND_KINDS if key in lc), "other")
    link = el.find("link")
    # filter actions also carry <link expression='tsl:...'> (their internal form): only a real link is a URL action
    if link is not None and a.kind == "other" and not (link.get("expression") or "").startswith("tsl:"):
        a.kind = "url"
    return a


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_THUMBNAIL_BYTES = 5_000_000


def _png(text: str | None) -> bytes | None:
    """Decode a <thumbnail> (base64 PNG). Anything that is not a PNG, or is oversized, is dropped."""
    import base64
    import binascii
    if not text or len(text) > MAX_THUMBNAIL_BYTES * 4 // 3 + 16:
        return None
    try:
        data = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if data.startswith(PNG_MAGIC) else None


def _link_action_filters(model: WorkbookModel) -> None:
    """Tie each action filter to the action that drives it (user:ui-action-filter='[ActionN]').
    A filter whose action was deleted - or that names no action - is a leftover and has no effect."""
    by_name = {a.name: a for a in model.actions}
    for ws in model.worksheets:
        for fd in ws.filters:
            if fd.mode == "include" and {m.lower() for m in fd.members if m != "%null%"} == {"true", "false"}:
                fd.keeps_all_values = True       # a true/false field: both values kept = everything but empty
            if not fd.is_action_filter:
                continue
            if fd.field.field.startswith("Tooltip ("):
                fd.action_status = "tooltip"     # viz in tooltip: set by the sheet that shows this one in a tooltip
            elif fd.action_ref is None:
                fd.action_status = "unlinked"
            elif fd.action_ref in by_name:
                fd.action_status = "linked"
                fd.action_caption = by_name[fd.action_ref].caption or fd.action_ref
            else:
                fd.action_status = "missing"


# ====================================================================== resolution

def _resolve(model: WorkbookModel) -> None:
    """Attach captions to references, compute field usage (direct and through calculations)."""
    by_ds: dict[str, dict[str, Field]] = {}
    for ds in model.datasources:
        idx = {f.name: f for f in ds.fields}
        by_ds[ds.name] = idx
    captions_all: dict[str, str] = {}
    for ds in model.datasources:
        for f in ds.fields:
            if f.caption:
                captions_all.setdefault(f.name, f.caption)
    # readable formulas
    for ds in model.datasources:
        for f in ds.fields:
            if f.formula:
                f.formula_readable = fx.readable(f.formula, captions_all)

    physical = {}
    for ds in model.datasources:
        names = {p.local_name.strip("[]") if p.local_name else p.remote_name for p in ds.physical_fields}
        names |= {p.remote_name for p in ds.physical_fields if p.remote_name}
        names |= _relation_columns(ds.relation)
        for o in ds.objects:
            names |= _relation_columns(o)
        physical[ds.name] = names

    sets = {ds.name: {s.name: s for s in ds.sets} for ds in model.datasources}

    def resolve(ref: FieldRef, ws: Worksheet | None = None) -> None:
        if ref.kind == "special" or ref.field in GENERATED_FIELDS:
            ref.resolved = True
            ref.caption = ref.caption or ref.field
            return
        if ref.field.startswith("AdhocCluster"):
            ref.resolved, ref.caption = True, "Clusters"
            return
        if ref.field.startswith("Action (") or ref.field.startswith("Exclusions ("):
            ref.resolved, ref.caption, ref.is_calculated = True, ref.field, False   # generated by an action / exclusion
            return
        if OBJECT_ID_MARKER in ref.raw:
            ref.resolved = True
            ref.caption = "rows of " + re.sub(r"_[0-9A-F]{32}$", "", ref.field)
            return
        s = sets.get(ref.datasource, {}).get(ref.field)
        if s is not None:
            ref.resolved, ref.caption, ref.is_calculated = True, s.caption or s.name, False
            return
        # column-instance gives the true column name for this exact reference
        if ws is not None:
            ci = next((c for c in ws.column_instances if _bracket_name(c.name) == ref.inner), None)
            if ci is not None and ci.column:
                true_field = _bracket_name(ci.column)
                if true_field != ref.field:
                    ref.field = true_field
        idx = by_ds.get(ref.datasource)
        f = idx.get(ref.field) if idx else None
        if f is not None:
            ref.caption, ref.is_calculated, ref.resolved = f.caption, f.kind == "calculated", True
            return
        if ref.datasource == "Parameters":
            ref.resolved = True
            return
        if ref.field in physical.get(ref.datasource, set()):
            ref.resolved, ref.is_calculated = True, False
            return
        model.unresolved_refs.append(ref.raw[:160])

    direct: dict[tuple[str, str], set[str]] = defaultdict(set)
    for ws in model.worksheets:
        for ref in ws.all_refs():
            resolve(ref, ws)
            direct[(ref.datasource, ref.field)].add(ws.name)
        for s in ws.sorts:
            for r in (s.field, s.using):
                if r is not None:
                    resolve(r, ws)
        for p in ws.panes:
            for rl in p.reference_lines:
                if rl.value_column is not None:
                    resolve(rl.value_column, ws)
    for db in model.dashboards:
        for r in db.quick_filters:
            resolve(r)
        for z in db.zones:
            if z.field is not None:
                resolve(z.field)

    # Measure Names filters list measures as references: show them as labels
    for ws in model.worksheets:
        for fd in ws.filters:
            if any(m.startswith("[") and "].[" in m for m in fd.members):
                labels = []
                for mbr in fd.members:
                    r = parse_ref(mbr) if mbr.startswith("[") else None
                    if r is not None:
                        resolve(r, ws)
                        labels.append(r.label())
                    else:
                        labels.append(mbr)
                fd.members = labels
    # parameter controls: internal parameter names -> captions
    pcap = {p.name: (p.caption or p.name) for p in model.parameters}
    for db in model.dashboards:
        db.parameter_controls = [pcap.get(n, n) for n in db.parameter_controls]

    # usage through calculations: if sheet uses calc A and A references B, B is used too
    for ds in model.datasources:
        idx = by_ds[ds.name]
        deps: dict[str, list[str]] = {}
        for f in ds.fields:
            if f.formula_info:
                deps[f.name] = [n for n in f.formula_info.fields if n in idx]
            elif f.bin and f.bin.source in idx:
                deps[f.name] = [f.bin.source]
            elif f.group and f.group.source in idx:
                deps[f.name] = [f.group.source]
        for f in ds.fields:
            f.used_directly_by = sorted(direct.get((ds.name, f.name), set()))
        changed = True
        used = {f.name: set(f.used_directly_by) for f in ds.fields}
        while changed:
            changed = False
            for name, ds_deps in deps.items():
                for d in ds_deps:
                    before = len(used[d])
                    used[d] |= used[name]
                    changed |= len(used[d]) != before
        for f in ds.fields:
            f.used_by = sorted(used[f.name])

    # parameters: usage by calculations
    for p in model.parameters:
        for ds in model.datasources:
            for f in ds.fields:
                if f.formula_info and (p.name in f.formula_info.parameters or (p.caption and p.caption in f.formula_info.parameters)):
                    p.used_by_calcs.append(f.caption or f.name)


# ====================================================================== size report

def _size_report(root: Element, total_bytes: int, model: WorkbookModel) -> dict:
    sections = {ch.tag: len(tostring(ch)) for ch in root}
    filters = sum(len(tostring(f)) for f in root.iter("filter"))
    manual_sorts = sum(len(tostring(s)) for s in root.iter("manual-sort"))
    top_sheets = sorted(((w.xml_bytes, w.name) for w in model.worksheets), reverse=True)[:5]
    sizes = sorted(w.xml_bytes for w in model.worksheets)
    return {
        "total_bytes": total_bytes,
        "sections": dict(sorted(sections.items(), key=lambda kv: -kv[1])),
        "filter_bytes": filters,
        "manual_sort_bytes": manual_sorts,
        "thumbnail_bytes": sum(t.base64_bytes for t in model.thumbnails),
        "median_worksheet_bytes": sizes[len(sizes) // 2] if sizes else 0,
        "largest_worksheets": [{"name": n, "bytes": b} for b, n in top_sheets],
        "token_estimate_rough": round(total_bytes / 3.5),   # rule of thumb, not a tokenizer
    }


# ====================================================================== entry point

def parse_workbook(source: str | Path | bytes, name: str | None = None) -> WorkbookModel:
    """Parse a .twb/.twbx path (or raw .twb bytes) into a WorkbookModel."""
    package: PackageInfo | None = None
    if isinstance(source, (str, Path)):
        data, package = read_workbook(source)
        name = name or Path(source).name
    else:
        data = source
    root, rep, total = load_twb(data)
    model = WorkbookModel(source=name, version=root.get("version"), source_build=root.get("source-build"),
                          source_platform=root.get("source-platform"), original_version=root.get("original-version"),
                          feature_flags=rep.feature_flags)
    if rep.dropped_false_variants:
        model.warnings.append(f"dropped {rep.dropped_false_variants} feature-flag 'false' variants in favour of 'true'")

    for ds_el in children(root, "datasources/datasource"):
        try:
            ds = _parse_datasource(ds_el, model)
        except Exception as exc:  # never fail the whole workbook on one data source
            model.warnings.append(f"datasource {ds_el.get('name')!r} failed: {type(exc).__name__}: {exc}")
            continue
        model.datasources.append(ds)
        if ds.is_parameters:
            for col in ds_el.findall("column"):
                rng = first(col, "range")
                model.parameters.append(Parameter(
                    name=_bracket_name(col.get("name")), caption=col.get("caption"), datatype=col.get("datatype"),
                    domain_type=col.get("param-domain-type"), value=strip_quotes(col.get("value")),
                    members=[strip_quotes(m.get("value")) or "" for m in children(col, "members/member")],
                    range=dict(rng.attrib) if rng is not None else {}))

    windows = {w.get("name"): w for w in children(root, "windows/window")}
    model.windows = [Window(name=n or "", cls=w.get("class"), hidden=w.get("hidden") == "true")
                     for n, w in windows.items()]
    for ws_el in children(root, "worksheets/worksheet"):
        try:
            ws = _parse_worksheet(ws_el, model)
        except Exception as exc:
            model.warnings.append(f"worksheet {ws_el.get('name')!r} failed: {type(exc).__name__}: {exc}")
            continue
        w = windows.get(ws.name)
        ws.hidden = w is not None and w.get("hidden") == "true"
        model.worksheets.append(ws)

    ws_names = {w.name for w in model.worksheets}
    db_names = {d.get("name", "") for d in children(root, "dashboards/dashboard")}
    for db_el in children(root, "dashboards/dashboard"):
        try:
            _collect_scoped_calcs(db_el, model, "dashboard", db_el.get("name", ""))
            model.dashboards.append(_parse_dashboard(db_el, ws_names, db_names))
        except Exception as exc:
            model.warnings.append(f"dashboard {db_el.get('name')!r} failed: {type(exc).__name__}: {exc}")
    for db in model.dashboards:
        for s in db.sheets:
            w = model.worksheet(s)
            if w is not None and db.name not in w.in_dashboards:
                w.in_dashboards.append(db.name)

    model.actions = [_parse_action(a) for a in children(root, "actions/action")]
    _link_action_filters(model)
    model.thumbnails = [Thumbnail(name=t.get("name", "").strip(), width=_int(t.get("width")),
                                  height=_int(t.get("height")), base64_bytes=len(t.text or ""),
                                  png=_png(t.text))
                        for t in children(root, "thumbnails/thumbnail")]

    _resolve(model)
    from .describe import infer_chart_type  # local import: describe depends on the model
    for ws in model.worksheets:
        ws.chart_type = infer_chart_type(ws)
    model.size_report = _size_report(root, total, model)
    from .datafiles import link_data_files
    link_data_files(model, [n for n, _ in package.members] if package is not None and package.is_packaged else [])
    if package is not None and package.is_packaged:
        model.size_report["package_members"] = [{"name": n, "bytes": b} for n, b in package.members]
    for known_unparsed in ("shared-views", "external", "datasource-relationships", "mapsources"):
        if root.find(known_unparsed) is not None:
            model.unsupported.append(f"section <{known_unparsed}> present (not interpreted)")
    return model
