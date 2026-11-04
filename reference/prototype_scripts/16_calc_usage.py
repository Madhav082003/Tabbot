import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()
ds = root.find("datasources").find("datasource")
calcs = {c.get("name").strip("[]"): c.get("caption") for c in ds.findall("column") if c.find("calculation") is not None}
ws = ET.tostring(root.find("worksheets"), encoding="unicode")
for k, v in calcs.items():
    print(f"{ws.count(k):5d} mentions in worksheets  {v}  ({k})")
