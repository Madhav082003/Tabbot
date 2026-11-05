"""Run the parser over a folder of workbooks and report what it could not interpret.

Use it whenever you add new workbooks to the corpus: anything new shows up as unresolved references, unsupported
constructs or warnings, which tells you exactly where the parser needs work.

    python tools/parser_coverage.py corpus/xml --out reports/parser_coverage.json
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tabbot.twb import overview, parse_workbook  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--out", default="reports/parser_coverage.json")
    ap.add_argument("--exclude-private", action="store_true", help="skip files whose name starts with 'p'")
    args = ap.parse_args()

    files = sorted(Path(args.folder).glob("*.twb")) + sorted(Path(args.folder).glob("*.twbx"))
    if args.exclude_private:
        files = [f for f in files if not f.stem.startswith("p")]
    rows, totals = [], collections.Counter()
    chart_types, filter_modes, calc_kinds, actions = (collections.Counter() for _ in range(4))
    t0 = time.time()
    for f in files:
        try:
            m = parse_workbook(f)
        except Exception as exc:
            rows.append({"workbook": f.stem, "error": f"{type(exc).__name__}: {exc}"})
            totals["errors"] += 1
            continue
        ov = overview(m)
        refs = sum(len(w.all_refs()) for w in m.worksheets)
        totals.update(workbooks=1, worksheets=len(m.worksheets), dashboards=len(m.dashboards),
                      datasources=len(m.datasources), refs=refs, unresolved=len(m.unresolved_refs),
                      calcs=len(m.calculated_fields), filters=sum(len(w.filters) for w in m.worksheets))
        chart_types.update(ov["chart_types"])
        filter_modes.update(ov["filters"])
        calc_kinds.update(ov["calculations"]["by_kind"])
        actions.update(ov["actions"])
        rows.append({"workbook": f.stem, "version": m.version, "build": m.source_build,
                     "worksheets": len(m.worksheets), "dashboards": len(m.dashboards), "refs": refs,
                     "unresolved": m.unresolved_refs, "unsupported": m.unsupported, "warnings": m.warnings})
    report = {"seconds": round(time.time() - t0, 2), "totals": dict(totals),
              "resolution_rate": round(1 - totals["unresolved"] / max(totals["refs"], 1), 4),
              "chart_types": dict(chart_types.most_common()), "filter_modes": dict(filter_modes.most_common()),
              "calculation_kinds": dict(calc_kinds.most_common()), "action_kinds": dict(actions.most_common()),
              "workbooks": rows}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report[k] for k in ("seconds", "totals", "resolution_rate", "chart_types",
                                             "filter_modes", "calculation_kinds", "action_kinds")}, indent=1))


if __name__ == "__main__":
    main()
