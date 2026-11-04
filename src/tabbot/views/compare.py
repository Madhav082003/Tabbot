"""Compare a rebuilt worksheet with numbers exported from Tableau itself.

Accepts what Tableau gives you:
- "Download > Crosstab" (CSV; often UTF-16 and tab-separated): the view as a grid, row headers on the left;
- "Download > Data" / summary data (CSV, long format: one column per field).
Strategy: (1) crosstab row-by-row match on row labels; (2) long format: find the numeric column whose values match
our measure values as a multiset. The best strategy wins. Nothing here leaves the machine.
"""

from __future__ import annotations

import io
from collections import Counter
from dataclasses import dataclass, field

import pandas as pd

from tabbot.views.display import pivot
from tabbot.views.rebuild import RebuildResult


@dataclass
class CompareReport:
    status: str                     # match | mismatch | unknown
    method: str                     # crosstab | long | none
    matched: int = 0
    compared: int = 0
    differences: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def summary(self) -> str:
        unit = "rows" if self.method == "crosstab" else "values"
        if self.status == "match":
            return f"Matches Tableau: {self.matched}/{self.compared} {unit}"
        if self.status == "mismatch":
            return f"Differs from Tableau: {self.compared - self.matched} of {self.compared} " \
                   f"{'rows' if self.method == 'crosstab' else 'values'} differ"
        return f"Could not compare: {self.note}"


def read_tableau_csv(raw: bytes) -> pd.DataFrame:
    for enc in ("utf-16", "utf-8-sig", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and raw[:2] not in (b"\xff\xfe", b"\xfe\xff"):
                continue
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("cannot decode the CSV file")
    first = text.splitlines()[0] if text else ""
    sep = "\t" if first.count("\t") > first.count(",") else ","
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=False, header=None)


def _num(s: str) -> float | None:
    s = (s or "").strip().replace(",", "").replace("%", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm(v: float) -> float:
    return round(float(v), 4)


def _crosstab(res: RebuildResult, grid: pd.DataFrame) -> CompareReport:
    pv, m, _ = pivot(res)
    if pv.empty:
        return CompareReport("unknown", "crosstab", note="nothing to compare")
    ours = {}
    for label, row in pv.iterrows():
        key = str(label).split(" | ")[-1].strip()
        ours[key] = [_norm(v) for v in row.tolist() if pd.notna(v)]
    rep = CompareReport("unknown", "crosstab")
    for _, row in grid.iterrows():
        cells = [str(c) for c in row.tolist()]
        labels = [c for c in cells if _num(c) is None and c.strip()]
        nums = [_norm(v) for v in (_num(c) for c in cells) if v is not None]
        key = next((lbl.strip() for lbl in reversed(labels) if lbl.strip() in ours), None)
        if key is None or not nums:
            continue
        rep.compared += 1
        if nums == ours[key]:
            rep.matched += 1
        else:
            diff = [f"{a:g}≠{b:g}" for a, b in zip(nums, ours[key], strict=False) if a != b][:4]
            more = "" if len(nums) == len(ours[key]) else f" (Tableau {len(nums)} cells, ours {len(ours[key])})"
            rep.differences.append(f"{key}: {', '.join(diff) or 'different cells'}{more}")
    if rep.compared:
        rep.status = "match" if rep.matched == rep.compared else "mismatch"
        missing = len(ours) - rep.compared
        if missing > 0 and rep.status == "match":
            rep.note = f"{missing} of our rows were not found in the file"
    return rep


def _long(res: RebuildResult, table: pd.DataFrame) -> CompareReport:
    if not res.measures or table.empty:
        return CompareReport("unknown", "long", note="nothing to compare")
    header, body = table.iloc[0].tolist(), table.iloc[1:]
    best = CompareReport("unknown", "long", note="no numeric column matched")
    for m in res.measures:
        ours = Counter(_norm(v) for v in pd.to_numeric(res.data[m.alias], errors="coerce").dropna())
        if not ours:
            continue
        for j in range(table.shape[1]):
            cells = [str(v).strip() for v in body.iloc[:, j]]
            vals = [_num(v) for v in cells]
            # only columns that are entirely numeric (blanks allowed)
            non_numeric = any(v is None and c for v, c in zip(vals, cells, strict=True))
            if not cells or non_numeric or all(v is None for v in vals):
                continue
            theirs = Counter(_norm(v) for v in vals if v is not None)
            common = sum((ours & theirs).values())
            total = max(sum(ours.values()), sum(theirs.values()))
            if common > best.matched:
                best = CompareReport("match" if common == total else "mismatch", "long", matched=common,
                                     compared=total, note=f"column '{header[j]}' vs {m.label}")
                if best.status == "mismatch":
                    extra = list((theirs - ours).elements())[:5]
                    best.differences = [f"values only in Tableau: {', '.join(f'{v:g}' for v in extra)}"] if extra \
                        else []
                    lack = list((ours - theirs).elements())[:5]
                    if lack:
                        best.differences.append(f"values only in rebuild: {', '.join(f'{v:g}' for v in lack)}")
    return best


def compare_with_tableau(res: RebuildResult, raw: bytes) -> CompareReport:
    try:
        table = read_tableau_csv(raw)
    except Exception as exc:  # noqa: BLE001
        return CompareReport("unknown", "none", note=f"could not read the file ({exc})")
    a = _crosstab(res, table)
    b = _long(res, table)
    candidates = [r for r in (a, b) if r.status != "unknown"]
    if not candidates:
        return CompareReport("unknown", "none", note="the file's rows/values did not line up with this chart")
    return max(candidates, key=lambda r: (r.status == "match", r.matched / max(r.compared, 1), r.compared))
