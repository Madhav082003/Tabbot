import sys, pandas as pd
from tableauhyperapi import HyperProcess, Telemetry, Connection
with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": "."}) as hp:
    with Connection(hp.endpoint, sys.argv[1]) as c:
        rows = c.execute_list_query('SELECT "CASEID","INITIAL_RECEIVED_DATE","SOC","PT","REPORT_TYPE","COUNTRY","CLASSIFICATION" FROM "Extract"."Extract"')
df = pd.DataFrame(rows, columns=["case","ird","soc","pt","rtype","country","cls"])
df["date"] = pd.to_datetime(df["ird"], format="%d-%b-%Y")
df["period"] = pd.cut(df["date"], [pd.Timestamp("2021-01-01"), pd.Timestamp("2024-04-01"), pd.Timestamp("2025-04-01"), pd.Timestamp("2025-10-01"), pd.Timestamp("2026-06-30")],
                      labels=["2021-Q1'24", "Q2'24-Q1'25", "Q2'25-Q3'25", "Q4'25-Q2'26"], right=False)
cases = df.groupby("case").agg(period=("period","first"), rtype=("rtype","first"), country=("country","first"),
                               nsoc=("soc","nunique"), npt=("pt","nunique"), q=("date", lambda d: d.min().to_period("Q")))
print("== avg SOCs / PTs per case by period ==")
print(cases.groupby("period", observed=True)[["nsoc","npt"]].mean().round(2).to_string())
print("\n== report type by period (cases) ==")
print(pd.crosstab(cases["rtype"], cases["period"]).to_string())
print("\n== top countries by period (cases) ==")
ct = pd.crosstab(cases["country"], cases["period"])
print(ct.loc[ct.sum(axis=1).sort_values(ascending=False).index[:8]].to_string())
print("\n== report type x quarter, last 6 quarters ==")
x = pd.crosstab(cases["rtype"], cases["q"]); print(x[x.columns[-6:]].to_string())
# PT growth within top SOCs: Q2'24-Q1'25 vs Q2'25-Q1'26
a = df[(df.date >= "2024-04-01") & (df.date < "2025-04-01")]; b = df[(df.date >= "2025-04-01") & (df.date < "2026-04-01")]
for soc in ["General disorders and administration site conditions", "Injury, poisoning and procedural complications", "Social circumstances", "Product issues"]:
    pa = a[a.soc == soc].groupby("pt")["case"].nunique(); pb = b[b.soc == soc].groupby("pt")["case"].nunique()
    d = pd.DataFrame({"prev": pa, "last": pb}).fillna(0).astype(int); d["chg"] = d["last"] - d["prev"]
    print(f"\n-- {soc}: top PT increases (cases, prev 4Q -> last 4Q)")
    print(d.sort_values("chg", ascending=False).head(5).to_string())
