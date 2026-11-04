import sys, re, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
w = [x for x in root.find("worksheets") if x.get("name") == "Sheet 105"][0]
f = w.find("table").find("view").find("filter")
print(ET.tostring(f, encoding="unicode")[:300])
print([g.get("member") for g in f.iter("groupfilter") if g.get("member")])
db = [d for d in root.find("dashboards") if d.get("name") == "SOC By Quarter"][0]
for z in db.iter("zone"):
    if z.get("type-v2") in ("filter", "title"):
        print(z.get("type-v2"), z.get("param"), z.get("mode"), z.get("name"))
t = db.find(".//layout-options/title")
print("title xml:", ET.tostring(t, encoding="unicode")[:400] if t is not None else None)
# extract filters / extract definition
for e in root.iter("extract"):
    print("extract:", e.attrib)
    for fl in e.iter("filter"):
        print("  extract filter:", ET.tostring(fl, encoding="unicode")[:300])
