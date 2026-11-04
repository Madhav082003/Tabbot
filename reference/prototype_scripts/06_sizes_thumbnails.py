import sys, re, base64, struct, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
ws_all = {w.get("name"): w for w in root.find("worksheets")}

print("== totals on 105/106 ==")
for n in ("Sheet 105", "Sheet 106"):
    s = ET.tostring(ws_all[n], encoding="unicode")
    print(n, "grandtotals" in s.lower(), re.findall(r"<(?:grand-?totals?|subtotals?)[^>]*>", s, re.I)[:5], re.findall(r"totals[^ >]*='[^']*'", s)[:5])
f106 = [f for f in ws_all["Sheet 106"].find("table").find("view").findall("filter")]
for f in f106:
    print("106 filter xml:", ET.tostring(f, encoding="unicode")[:800])

print("\n== per-worksheet XML size ==")
sizes = sorted(((len(ET.tostring(w)), n) for n, w in ws_all.items()), reverse=True)
for s, n in sizes[:6]:
    print(f"  {s/1024:7.1f} KB  {n}")
typical = sorted(s for s, _ in sizes)[len(sizes)//2]
print(f"  median {typical/1024:.1f} KB")
filt = sum(len(ET.tostring(f)) for w in ws_all.values() for f in w.iter("filter"))
print(f"filter elements total: {filt/1024:.0f} KB of worksheets {sum(s for s,_ in sizes)/1024:.0f} KB")

total = len(open(sys.argv[1], 'rb').read())
print(f"\nwhole file {total/1024:.0f} KB ~ {total/3.5/1000:.0f}k tokens (rough)")

print("\n== thumbnails ==")
for t in root.iter("thumbnail"):
    png = base64.b64decode(t.text)
    w, h = struct.unpack(">II", png[16:24]) if png[:8] == b"\x89PNG\r\n\x1a\n" else (None, None)
    print(f"  {w}x{h}  {t.get('name')}")
