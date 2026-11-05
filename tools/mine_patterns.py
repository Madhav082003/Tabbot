"""Mine structural patterns from a folder of Tableau .twb files.

Answers "what does Tableau XML actually look like across many real workbooks?" so the parser is built on
evidence, not guesses. For every pattern it records in how many workbooks it appears.

Usage:
    python tools/mine_patterns.py corpus/xml --manifest corpus/manifest.json --out reports/patterns.json

Outputs a JSON report; docs/PATTERNS.md is the human summary written from it.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

from defusedxml import ElementTree as ET

REF_RE = re.compile(r"\[([^\[\]]+)\]\.\[([^\[\]]+)\]")
FUNC_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,40})\s*\(")
LOD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.I)
TABLE_CALC_FUNCS = {"WINDOW_SUM", "WINDOW_AVG", "WINDOW_MAX", "WINDOW_MIN", "WINDOW_COUNT", "WINDOW_MEDIAN",
                    "WINDOW_STDEV", "WINDOW_VAR", "WINDOW_PERCENTILE", "WINDOW_CORR", "WINDOW_COVAR",
                    "RUNNING_SUM", "RUNNING_AVG", "RUNNING_COUNT", "RUNNING_MAX", "RUNNING_MIN", "LOOKUP",
                    "PREVIOUS_VALUE", "INDEX", "FIRST", "LAST", "SIZE", "TOTAL", "RANK", "RANK_DENSE",
                    "RANK_MODIFIED", "RANK_PERCENTILE", "RANK_UNIQUE"}

# (tag, attribute) pairs whose VALUES are worth counting (categorical vocabularies)
VALUE_ATTRS = {
    ("connection", "class"), ("relation", "type"), ("relation", "join"), ("column", "datatype"),
    ("column", "role"), ("column", "type"), ("column", "param-domain-type"), ("column", "aggregation"),
    ("column", "default-type"), ("calculation", "class"), ("mark", "class"), ("filter", "class"),
    ("filter", "filter-group"), ("groupfilter", "function"), ("groupfilter", "ui-enumeration"),
    ("groupfilter", "ui-marker"), ("groupfilter", "ui-domain"), ("zone", "type-v2"), ("zone", "type"),
    ("zone", "mode"), ("window", "class"), ("column-instance", "derivation"), ("column-instance", "type"),
    ("column-instance", "pivot"), ("table-calc", "ordering-type"), ("table-calc", "type"),
    ("action", "caption"), ("activation", "type"), ("source", "type"), ("command", "command"),
    ("sort", "class"), ("sort", "direction"), ("shelf-sort-v2", "shelf"), ("encoding", "attr"),
    ("style-rule", "element"), ("format", "attr"), ("size", "sizing-mode"), ("datasource", "inline"),
    ("datasource", "hasconnection"), ("extract", "enabled"), ("refresh", "increment-enabled"),
    ("pane", "selection-relaxation-option"), ("reference-line", "formula"), ("reference-line", "scope"),
    ("devicelayout", "auto-generated"), ("layout-options", "x"), ("group", "ui-builder"),
    ("relationship", "type"), ("object", "caption"), ("field", "aggregation"),
}
SKIP_VALUE_COUNT = {("action", "caption"), ("object", "caption"), ("layout-options", "x")}


def norm_path(stack: list[str]) -> str:
    out: list[str] = []
    for tag in stack:
        if out and (out[-1] == tag or out[-1] == tag + "*"):
            out[-1] = tag + "*"
        else:
            out.append(tag)
    return "/".join(out)


def walk(el, stack, paths, attrs, values):
    stack.append(el.tag)
    p = norm_path(stack)
    paths[p] += 1
    for a, v in el.attrib.items():
        attrs[(el.tag, a)] += 1
        if (el.tag, a) in VALUE_ATTRS and (el.tag, a) not in SKIP_VALUE_COUNT:
            values[(el.tag, a)][v[:60]] += 1
    for ch in el:
        walk(ch, stack, paths, attrs, values)
    stack.pop()


def split_ref(inner: str) -> tuple[str, str, list[str], str]:
    parts = inner.split(":")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2:-1], parts[-1]
    return "", inner, [], ""


def mine_one(path: Path) -> dict:
    root = ET.parse(path).getroot()
    paths, attrs, values = collections.Counter(), collections.Counter(), collections.defaultdict(collections.Counter)
    walk(root, [], paths, attrs, values)

    refs = collections.Counter()      # derivation prefixes
    typekeys = collections.Counter()
    extras = collections.Counter()
    nparts = collections.Counter()
    ref_sources = collections.Counter()
    shelf_ops = collections.Counter()

    def scan_refs(text: str, where: str):
        for ds, inner in REF_RE.findall(text or ""):
            parts = inner.split(":")
            nparts[len(parts)] += 1
            ref_sources[where] += 1
            if len(parts) >= 3:
                d, f, ex, t = split_ref(inner)
                refs[d] += 1
                typekeys[t] += 1
                for e in ex:
                    extras[e] += 1
            else:
                refs["<bare>"] += 1

    for ws in root.iter("worksheet"):
        for shelf in ("rows", "cols"):
            for el in ws.iter(shelf):
                txt = el.text or ""
                scan_refs(txt, shelf)
                stripped = REF_RE.sub("R", txt)
                for ch in re.findall(r"[^\sR]", stripped):
                    shelf_ops[ch] += 1
        for enc in ws.iter("encodings"):
            for e in enc:
                scan_refs(e.get("column", ""), "encoding")
        for f in ws.iter("filter"):
            scan_refs(f.get("column", ""), "filter")
    for z in root.iter("zone"):
        if z.get("param"):
            scan_refs(z.get("param"), "zone-param")

    # column-instance: Tableau's own decode of a field reference
    ci_map = collections.Counter()
    for ci in root.iter("column-instance"):
        name = ci.get("name", "")
        m = re.match(r"\[([^:\]]+):", name)
        if m:
            ci_map[(m.group(1), ci.get("derivation", ""), ci.get("type", ""))] += 1

    funcs, lods, tcalc_funcs, formulas = collections.Counter(), collections.Counter(), collections.Counter(), 0
    calc_classes = collections.Counter()
    param_refs = 0
    for calc in root.iter("calculation"):
        calc_classes[calc.get("class", "")] += 1
        fml = calc.get("formula")
        if not fml:
            continue
        formulas += 1
        for fn in FUNC_RE.findall(fml):
            funcs[fn] += 1
            if fn in TABLE_CALC_FUNCS:
                tcalc_funcs[fn] += 1
        for lod in LOD_RE.findall(fml):
            lods[lod.upper()] += 1
        if "[Parameters]." in fml:
            param_refs += 1

    sizes = {}
    total = len(path.read_bytes())
    for ch in root:
        sizes[ch.tag] = len(ET.tostring(ch))

    dashboards = root.find("dashboards")
    worksheets = root.find("worksheets")
    windows = root.find("windows")
    hidden = sum(1 for w in (windows if windows is not None else []) if w.get("hidden") == "true")
    return {
        "paths": paths, "attrs": attrs, "values": values,
        "refs": refs, "typekeys": typekeys, "extras": extras, "nparts": nparts, "ref_sources": ref_sources,
        "shelf_ops": shelf_ops, "column_instances": ci_map,
        "funcs": funcs, "lods": lods, "table_calc_funcs": tcalc_funcs, "formulas": formulas,
        "calc_classes": calc_classes, "param_refs": param_refs,
        "sizes": sizes, "total_bytes": total,
        "counts": {
            "worksheets": len(list(worksheets)) if worksheets is not None else 0,
            "dashboards": len(list(dashboards)) if dashboards is not None else 0,
            "datasources": len(root.findall("./datasources/datasource")),
            "hidden_windows": hidden,
            "thumbnails": len(list(root.iter("thumbnail"))),
            "table_calc_elements": len(list(root.iter("table-calc"))),
            "actions": len(root.findall("./actions/action")),
            "parameters_datasource": any(d.get("name") == "Parameters" for d in root.iter("datasource")),
        },
    }


def merge(results: dict[str, dict], key: str, nested: bool = False) -> dict:
    """Return {item: [occurrences, workbooks]} (nested: {(tag,attr): {value: [occ, wbs]}})."""
    if nested:
        out = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
        for r in results.values():
            for ta, ctr in r[key].items():
                for v, c in ctr.items():
                    out[ta][v][0] += c
                    out[ta][v][1] += 1
        return {f"{t}@{a}": dict(sorted(d.items(), key=lambda kv: -kv[1][1])) for (t, a), d in out.items()}
    out = collections.defaultdict(lambda: [0, 0])
    for r in results.values():
        for k, c in r[key].items():
            out[k][0] += c
            out[k][1] += 1
    keyfmt = (lambda k: "|".join(map(str, k)) if isinstance(k, tuple) else str(k))
    return {keyfmt(k): v for k, v in sorted(out.items(), key=lambda kv: (-kv[1][1], -kv[1][0]))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("xml_dir")
    ap.add_argument("--manifest")
    ap.add_argument("--out", default="reports/patterns.json")
    ap.add_argument("--exclude-private", action="store_true", help="skip workbooks whose id starts with p")
    args = ap.parse_args()

    files = sorted(Path(args.xml_dir).glob("*.twb"))
    if args.exclude_private:
        files = [f for f in files if not f.stem.startswith("p")]
    results, errors = {}, {}
    for f in files:
        try:
            results[f.stem] = mine_one(f)
        except Exception as exc:
            errors[f.stem] = f"{type(exc).__name__}: {exc}"

    domains = {}
    if args.manifest:
        for e in json.loads(Path(args.manifest).read_text()):
            domains[e["id"]] = e.get("domain")

    report = {
        "workbooks": len(results), "errors": errors,
        "domains": collections.Counter(domains.get(k, "?") for k in results),
        "per_workbook_counts": {k: r["counts"] | {"total_bytes": r["total_bytes"], "domain": domains.get(k)}
                                for k, r in results.items()},
        "element_paths": merge(results, "paths"),
        "attributes": merge(results, "attrs"),
        "attribute_values": merge(results, "values", nested=True),
        "ref_derivations": merge(results, "refs"),
        "ref_type_keys": merge(results, "typekeys"),
        "ref_extras": merge(results, "extras"),
        "ref_part_counts": merge(results, "nparts"),
        "ref_sources": merge(results, "ref_sources"),
        "shelf_operators": merge(results, "shelf_ops"),
        "column_instance_decode": merge(results, "column_instances"),
        "formula_functions": merge(results, "funcs"),
        "lod_expressions": merge(results, "lods"),
        "table_calc_functions": merge(results, "table_calc_funcs"),
        "calculation_classes": merge(results, "calc_classes"),
        "formulas_total": sum(r["formulas"] for r in results.values()),
        "formulas_with_parameters": sum(r["param_refs"] for r in results.values()),
        "section_bytes_share": {},
    }
    share = collections.defaultdict(list)
    for r in results.values():
        tot = r["total_bytes"] or 1
        for sec, b in r["sizes"].items():
            share[sec].append(b / tot)
    report["section_bytes_share"] = {s: {"workbooks": len(v), "median_share": round(sorted(v)[len(v) // 2], 3),
                                         "max_share": round(max(v), 3)} for s, v in share.items()}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=1, default=str))
    print(f"mined {len(results)} workbooks ({len(errors)} errors) -> {args.out}")


if __name__ == "__main__":
    main()
