import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
ds = root.find("datasources").find("datasource")
def paths(parent, tags, prefix, depth=0, out=None, seen=None):
    out = [] if out is None else out
    for ch in parent:
        p = f"{prefix}/{ch.tag}"
        if ch.tag in tags and p not in out:
            out.append(p)
        if depth < 6:
            paths(ch, tags, p, depth + 1, out)
    return out
print(paths(ds, {"named-connection", "relation", "metadata-record", "extract", "column", "calculation"}, "datasource"))
db = [d for d in root.find("dashboards") if d.get("name") == "SOC By Quarter"][0]
print(paths(db, {"zone", "devicelayout", "size"}, "dashboard")[:8])
w = root.find("windows")[0]; print("window attrs:", dict(list(w.attrib.items())[:4]))
t = next(root.iter("thumbnail")); print("thumbnail attrs:", t.attrib)
