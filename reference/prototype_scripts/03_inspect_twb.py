import sys, re, xml.etree.ElementTree as ET
from collections import Counter

path = sys.argv[1]
raw = open(path, "rb").read()
root = ET.fromstring(raw)

print("== Size by top-level section ==")
for child in root:
    size = len(ET.tostring(child))
    print(f"{child.tag:40s} {size/1024:8.1f} KB")

print("\n== Thumbnails ==")
thumbs = list(root.iter("thumbnail"))
print(len(thumbs), "thumbnails,", sum(len(t.text or "") for t in thumbs) // 1024, "KB base64")

print("\n== Data sources ==")
ds_root = root.find("datasources")
for ds in ds_root.findall("datasource"):
    name, cap = ds.get("name"), ds.get("caption")
    print(f"\n-- {cap or name}  (internal: {name})")
    for conn in ds.iter("named-connection"):
        c = conn.find("connection")
        print("   connection:", conn.get("caption"), {k: v for k, v in c.attrib.items() if k in ("class", "server", "dbname", "filename", "schema")})
    for rel in ds.iter("relation"):
        if rel.get("type") == "table":
            print("   table:", rel.get("name"), rel.get("table"))
    cols = ds.findall("column")
    calcs = [c for c in cols if c.find("calculation") is not None]
    print(f"   columns defined: {len(cols)}  (calculated: {len(calcs)})")
    for c in cols:
        calc = c.find("calculation")
        label = c.get("caption") or c.get("name")
        if calc is not None and calc.get("formula"):
            f = re.sub(r"\s+", " ", calc.get("formula"))
            print(f"   CALC [{label}] ({c.get('datatype')}/{c.get('role')}): {f[:300]}")
        elif c.get("param-domain-type"):
            print(f"   PARAM [{label}] type={c.get('datatype')} value={c.get('value')} domain={c.get('param-domain-type')}")
    # metadata columns (physical fields)
    mrs = ds.findall(".//metadata-record[@class='column']")
    if mrs:
        print(f"   physical fields ({len(mrs)}):", ", ".join(
            f"{(m.findtext('remote-name') or '')}:{m.findtext('local-type')}" for m in mrs))

print("\n== Worksheets ==")
for ws in root.find("worksheets") or []:
    print(f"\n-- {ws.get('name')}")
    tbl = ws.find("table")
    view = tbl.find("view")
    title = ws.find(".//layout-options/title")
    if title is not None:
        print("   title:", re.sub(r"\s+", " ", "".join(title.itertext())).strip()[:200])
    print("   rows:", (tbl.findtext("rows") or "").strip()[:250])
    print("   cols:", (tbl.findtext("cols") or "").strip()[:250])
    for pane in tbl.iter("pane"):
        m = pane.find("mark")
        enc = pane.find("encodings")
        encs = [f"{e.tag}={e.get('column')}" for e in (enc if enc is not None else [])]
        print("   mark:", m.get("class") if m is not None else None, "| encodings:", "; ".join(encs)[:300])
    for f in view.findall("filter"):
        print("   filter:", f.get("class"), f.get("column"))
    print("   sorts:", len(view.findall(".//sort")) + len(view.findall(".//computed-sort")) + len(view.findall(".//manual-sort")))

print("\n== Dashboards ==")
dbs = root.find("dashboards")
for db in (dbs if dbs is not None else []):
    size = db.find("size")
    sheets = [z.get("name") for z in db.iter("zone") if z.get("name") and not z.get("type-v2")]
    others = Counter(z.get("type-v2") for z in db.iter("zone") if z.get("type-v2"))
    print(f"-- {db.get('name')}  size={size.attrib if size is not None else None}")
    print("   sheets:", sheets)
    print("   other zones:", dict(others))

print("\n== Actions / windows ==")
acts = root.find("actions")
print("actions:", len(list(acts)) if acts is not None else 0)
for a in (acts if acts is not None else []):
    print("  ", a.get("caption") or a.get("name"), a.find("activation").attrib if a.find("activation") is not None else "")
wins = root.find("windows")
print("hidden worksheets:", [w.get("name") for w in (wins if wins is not None else []) if w.get("hidden") == "true"])
