"""Rebuild the 'SOC By Quarter' dashboard (Sheet 105) from the extract.

Sheet 105: rows = SOC, cols = YEAR/QUARTER(Parsed Date), mark = COUNTD(CASEID)
Parsed Date = DATE([INITIAL_RECEIVED_DATE]); SOC filter = 27 listed SOCs.
"""
import sys
import pandas as pd
from tableauhyperapi import HyperProcess, Telemetry, Connection

hyper_path, out_dir = sys.argv[1], sys.argv[2]
SOCS = ["Blood and lymphatic system disorders", "Cardiac disorders", "Congenital, familial and genetic disorders",
        "Ear and labyrinth disorders", "Endocrine disorders", "Eye disorders", "Gastrointestinal disorders",
        "General disorders and administration site conditions", "Hepatobiliary disorders", "Immune system disorders",
        "Infections and infestations", "Injury, poisoning and procedural complications", "Investigations",
        "Metabolism and nutrition disorders", "Musculoskeletal and connective tissue disorders",
        "Neoplasms benign, malignant and unspecified (incl cysts and polyps)", "Nervous system disorders",
        "Pregnancy, puerperium and perinatal conditions", "Product issues", "Psychiatric disorders",
        "Renal and urinary disorders", "Reproductive system and breast disorders",
        "Respiratory, thoracic and mediastinal disorders", "Skin and subcutaneous tissue disorders",
        "Social circumstances", "Surgical and medical procedures", "Vascular disorders"]

with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": out_dir}) as hp:
    with Connection(hp.endpoint, hyper_path) as c:
        rows = c.execute_list_query('SELECT "CASEID", "INITIAL_RECEIVED_DATE", "SOC", "EVENT_SERIOUSNESS" FROM "Extract"."Extract"')
df = pd.DataFrame(rows, columns=["case", "ird", "soc", "ser"])

df["date"] = pd.to_datetime(df["ird"], format="%d-%b-%Y", errors="coerce")
print("unparsed dates:", df["date"].isna().sum(), df.loc[df["date"].isna(), "ird"].unique()[:5])
print("date range:", df["date"].min().date(), "->", df["date"].max().date())
print("cases with >1 distinct initial received date:", (df.groupby("case")["ird"].nunique() > 1).sum())

all_socs = set(df["soc"].dropna().unique())
print("SOCs in data not in filter:", all_socs - set(SOCS), "| null SOC rows:", df["soc"].isna().sum())
print("filter SOCs absent from data:", set(SOCS) - all_socs)

v = df[df["soc"].isin(SOCS)].copy()
v["q"] = v["date"].dt.to_period("Q")
print("distinct cases total:", df["case"].nunique(), "| in view:", v["case"].nunique())

grid = v.groupby(["soc", "q"])["case"].nunique().unstack(fill_value=0)
grid.to_csv(f"{out_dir}/soc_by_quarter_grid.csv")
print("\nquarters:", len(grid.columns), list(map(str, grid.columns)))

soc_cases = v.groupby("soc")["case"].nunique().sort_values(ascending=False)
n = v["case"].nunique()
print("\n== distinct cases per SOC (all quarters) ==")
for s, k in soc_cases.items():
    print(f"  {k:5d}  {k/n:6.1%}  {s}")

per_q = v.groupby("q")["case"].nunique()
print("\n== distinct cases per quarter (any SOC) ==")
print(per_q.to_string())

yr = v.assign(y=v["date"].dt.year).groupby("y")["case"].nunique()
print("\n== distinct cases per year ==\n", yr.to_string())

# cases per SOC: avg number of SOCs per case
print("\navg SOCs per case:", round(v.groupby("case")["soc"].nunique().mean(), 2),
      "| sum of grid cells:", int(grid.values.sum()), "vs distinct cases:", n)

# peak cell
peak = grid.stack().sort_values(ascending=False).head(8)
print("\n== top cells ==\n", peak.to_string())

# growth: last 4 full quarters vs prior 4 full quarters per SOC
qs = sorted(grid.columns)
full = qs[:-1] if v["date"].max() < qs[-1].end_time.normalize() - pd.Timedelta(days=7) else qs
last4, prev4 = full[-4:], full[-8:-4]
print("\nlast4:", [str(q) for q in last4], "prev4:", [str(q) for q in prev4])
g = pd.DataFrame({"prev4": grid[prev4].sum(axis=1), "last4": grid[last4].sum(axis=1)})
g["chg"] = g["last4"] - g["prev4"]
g["pct"] = (g["chg"] / g["prev4"].replace(0, pd.NA)).astype(float)
print(g.sort_values("chg", ascending=False).to_string())
print("total any-SOC cases prev4 vs last4:",
      v[v["q"].isin(prev4)]["case"].nunique(), v[v["q"].isin(last4)]["case"].nunique())

# rank stability: top 5 SOCs per year
print("\n== top 5 SOCs per year ==")
yg = v.assign(y=v["date"].dt.year).groupby(["y", "soc"])["case"].nunique()
for y in sorted(v["date"].dt.year.unique()):
    top = yg.loc[y].sort_values(ascending=False).head(5)
    tot = v[v["date"].dt.year == y]["case"].nunique()
    print(y, f"(cases {tot})", "; ".join(f"{s.split(' and ')[0][:28]} {k} ({k/tot:.0%})" for s, k in top.items()))

# seriousness share per year (bonus context)
cs = v.groupby("case").agg(ser=("ser", lambda s: (s == "Serious").any()), y=("date", lambda d: d.min().year))
print("\n== share of cases with any serious event, by year ==\n", cs.groupby("y")["ser"].mean().round(3).to_string())

# low-count SOCs
print("\nSOCs with <=10 cases total:", list(soc_cases[soc_cases <= 10].items()))
