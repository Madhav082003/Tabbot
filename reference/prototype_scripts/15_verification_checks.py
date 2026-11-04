"""NEW - written while preparing the technical documentation (not part of the original analysis).

Checks claims and wording from the earlier replies that were not verified in code at the time.
Usage: python3 -I 15_verification_checks.py <path to .twb> <path to .hyper>
"""
import sys
import xml.etree.ElementTree as ET
import pandas as pd
from tableauhyperapi import HyperProcess, Telemetry, Connection

twb_path, hyper_path = sys.argv[1], sys.argv[2]
root = ET.parse(twb_path).getroot()

print("== A. Why each dashboard lists its sheets twice: device (phone) layouts ==")
for db in root.find("dashboards"):
    n_layouts = len(db.findall(".//devicelayout"))
    print(f"  {n_layouts} devicelayout(s)  {db.get('name')}")

with HyperProcess(Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": "."}) as hp:
    with Connection(hp.endpoint, hyper_path) as c:
        t = '"Extract"."Extract"'
        print("\n== B. Row grain of the extract ==")
        print("  rows:", c.execute_scalar_query(f"SELECT COUNT(*) FROM {t}"))
        print("  distinct CASEID:", c.execute_scalar_query(f'SELECT COUNT(DISTINCT "CASEID") FROM {t}'))
        print("  distinct (CASEID, SORT_ID):", c.execute_scalar_query(
            f'SELECT COUNT(*) FROM (SELECT DISTINCT "CASEID", "SORT_ID" FROM {t}) s'))
        print("  fully identical duplicate rows:", c.execute_scalar_query(
            f"SELECT COUNT(*) - (SELECT COUNT(*) FROM (SELECT DISTINCT * FROM {t}) s) FROM {t}"))

        print("\n== C. EVENT_OUTCOME values (what 'Death by Quarter' keeps after its exclude list) ==")
        for v, k in c.execute_list_query(f'SELECT "EVENT_OUTCOME", COUNT(DISTINCT "CASEID") FROM {t} GROUP BY 1 ORDER BY 2 DESC'):
            print(f"  {k:5d} cases  {v}")

        rows = c.execute_list_query(f'SELECT "CASEID", "INITIAL_RECEIVED_DATE", "SOC", "PT", "AGE", "AGE_UNIT", "GENDER" FROM {t}')

df = pd.DataFrame(rows, columns=["case", "ird", "soc", "pt", "age", "unit", "gender"])
df["date"] = pd.to_datetime(df["ird"], format="%d-%b-%Y")
cases = df.groupby("case").agg(date=("date", "min"), nsoc=("soc", "nunique"), npt=("pt", "nunique"),
                               age=("age", "first"), unit=("unit", "first"), gender=("gender", "first"))

print("\n== D. Wording checks on earlier replies ==")
pre24 = cases[cases["date"] < "2024-01-01"]
print(f"  cases received before 2024: {len(pre24)}, avg SOCs {pre24.nsoc.mean():.2f}, avg PTs {pre24.npt.mean():.2f}")
q = cases["date"].dt.to_period("Q").value_counts().sort_index()
print("  2023 quarters:", {str(k): int(v) for k, v in q.items() if k.year == 2023})
days_q2 = (df["date"].max() - pd.Timestamp("2026-04-01")).days + 1
q2 = int(q[pd.Period("2026Q2")])
print(f"  Q2 2026: {q2} cases in {days_q2} days -> full-quarter pace {q2 / days_q2 * 91:.0f}")

print("\n== E. Cases dropped by the gender filter (keeps Female, Male, Undifferentiated) ==")
keep = cases["gender"].isin(["Female", "Male", "Undifferentiated"])
print(f"  dropped: {(~keep).sum()} of {len(cases)} ({(~keep).mean():.1%})", cases.loc[~keep, "gender"].value_counts(dropna=False).to_dict())

print("\n== F. Age Group formula applied to ages not in years ==")
def age_group(a):  # same thresholds as the workbook's [Age Group] calc, which ignores AGE_UNIT
    if pd.isna(a): return "Not Reported"
    if a < 1: return "Infant"
    if a < 12: return "Child"
    if a < 18: return "Adolescent"
    if a < 55: return "Adult (18 to 54)"
    if a < 65: return "Adult (55 to 64)"
    if a < 75: return "Elderly (65 to 74)"
    return "Elderly (75 and above)"
odd = cases[cases["unit"].isin(["Months", "Weeks", "Days"])].copy()
odd["workbook_group"] = odd["age"].map(age_group)
print(f"  cases with age in months/weeks/days: {len(odd)}")
print(odd.groupby(["unit", "workbook_group"]).size().to_string())
