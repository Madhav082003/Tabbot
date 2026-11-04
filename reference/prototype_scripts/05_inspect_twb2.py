import sys, re, xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
ds = root.find("datasources").find("datasource")

# internal name -> caption
names = {}
for c in ds.findall("column"):
    if c.get("caption"):
        names[c.get("name").strip("[]")] = c.get("caption")
print("== Calc internal names ==")
for k, v in names.items():
    if k.startswith("Calculation_"):
        print(f"  {k} -> {v}")

def nice(s):
    s = s or ""
    s = s.replace("[federated.pv].", "")
    for k, v in names.items():
        s = s.replace(k, v)
    return s

def members(f):
    out = []
    for g in f.iter("groupfilter"):
        m = g.get("member")
        if m:
            out.append(m)
    funcs = {g.get("function") for g in f.iter("groupfilter")}
    excl = any(g.get("function") == "except" for g in f.iter("groupfilter"))
    return out, funcs, excl

ws_all = root.find("worksheets").findall("worksheet")

print("\n== Table calcs anywhere in worksheets ==")
tc = [(w.get("name"), t.attrib) for w in ws_all for t in w.iter("table-calc")]
print(len(tc), "table-calc elements")
for n, a in tc[:15]:
    print("  ", n, a)
pref = set(re.findall(r"\[(\w+):[^\]]+:[a-z]{2}\]", ET.tostring(root.find("worksheets"), encoding="unicode")))
print("field derivation prefixes used:", sorted(pref))

print("\n== SOC filter per IRD / pie sheet (first 40) ==")
mismatch = []
for w in ws_all:
    n = w.get("name")
    if n.startswith("Sheet"):
        continue
    for f in w.find("table").find("view").findall("filter"):
        if "SOC" in (f.get("column") or ""):
            m, funcs, excl = members(f)
            vals = [x.strip('"') for x in m]
            base = re.sub(r"( - IRD( \(\d+\))?|-age group)$", "", n)
            title_el = w.find(".//layout-options/title")
            title = re.sub(r"\s+", " ", "".join(title_el.itertext())).strip() if title_el is not None else "(none)"
            if vals != [base] or title.rstrip(" Æ") not in (base, "(none)"):
                mismatch.append((n, vals, title, funcs))
print("sheets whose SOC filter or title doesn't match the sheet name:")
for x in mismatch:
    print("  ", x)

print("\n== GENDER filter on pies ==")
seen = set()
for w in ws_all:
    for f in w.find("table").find("view").findall("filter"):
        if "GENDER" in (f.get("column") or ""):
            m, funcs, excl = members(f)
            key = (tuple(m), tuple(sorted(x or "" for x in funcs)))
            seen.add(key)
for s in seen:
    print("  ", s)

print("\n== Sheets 101-107 ==")
for w in ws_all:
    n = w.get("name")
    if not n.startswith("Sheet"):
        continue
    tbl = w.find("table")
    print(f"\n-- {n}")
    t = w.find(".//layout-options/title")
    if t is not None:
        print("   title:", re.sub(r"\s+", " ", "".join(t.itertext())).strip()[:200])
    print("   rows:", nice(tbl.findtext("rows")))
    print("   cols:", nice(tbl.findtext("cols")))
    for p in tbl.iter("pane"):
        m = p.find("mark"); e = p.find("encodings")
        print("   mark:", m.get("class") if m is not None else None, "|", "; ".join(f"{x.tag}={nice(x.get('column'))}" for x in (e if e is not None else [])))
    for f in tbl.find("view").findall("filter"):
        m, funcs, excl = members(f)
        extra = ""
        if f.get("class") == "quantitative":
            extra = f" min={f.findtext('min')} max={f.findtext('max')} incl={f.get('included-values')}"
        print(f"   filter [{f.get('class')}] {nice(f.get('column'))}: {('EXCEPT ' if excl else '')}{[x for x in m][:12]}{' ...(+%d)' % (len(m)-12) if len(m) > 12 else ''}{extra}")
    for s in tbl.find("view").iter():
        if s.tag in ("sort", "computed-sort", "natural-sort", "manual-sort", "shelf-sort-v2"):
            print("   sort:", s.tag, {k: nice(v) for k, v in s.attrib.items()})

print("\n== Dashboard titles ==")
for db in root.find("dashboards"):
    for z in db.iter("zone"):
        if z.get("type-v2") == "title":
            pass
    t = db.find(".//layout-options/title")
    print("  ", db.get("name"), "|", re.sub(r"\s+", " ", "".join(t.itertext())).strip()[:150] if t is not None else "")
