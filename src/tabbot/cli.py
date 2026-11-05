"""Engine-only command line (no LLM). Commands are added step by step (see docs/WORK_PLAN.md)."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tabbot", description="Tableau Insights Bot - engine commands")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("version", help="print the version")
    ins = sub.add_parser("inspect", help="read a .twbx/.twb and print what was found (no data values)")
    ins.add_argument("file", type=Path)
    ins.add_argument("--hyper", type=Path, action="append", default=[], help="extra .hyper for a plain .twb")
    li = sub.add_parser("lint", help="definition checks on a workbook")
    li.add_argument("file", type=Path)
    an = sub.add_parser("analyze", help="checks + standard analyses on one worksheet")
    an.add_argument("file", type=Path)
    an.add_argument("worksheet")
    an.add_argument("--category", help="dimension label to analyse by (default: the sheet's main dimension)")
    for sp in (ins, li, an):
        sp.add_argument("--keep", action="store_true",
                        help="keep the unpacked copy of the workbook and its data in workspace/ (deleted by default)")
    us = sub.add_parser("usage", help="Claude API usage and estimated cost, per day (from the local usage log)")
    us.add_argument("--last", type=int, default=10, help="also list the last N logged events")
    return p


def cmd_usage(last: int) -> int:
    from tabbot.config import get_settings
    from tabbot.llm.usage import UsageLog

    s = get_settings()
    log = UsageLog(s.workspace_dir / "audit" / "usage.jsonl")
    days = log.by_day()
    print(f"Usage log: {log.path}")
    print(f"Limits: ${s.max_usd_per_question:.2f}/question, ${s.max_usd_per_day:.2f}/day, "
          f"{s.max_calls_per_day} calls/day, {s.max_tool_rounds} calls/question (estimates; the console bill rules)")
    if not days:
        print("No Claude API calls logged yet.")
        return 0
    print(f"\n{'date':<12}{'calls':>7}{'blocked':>9}{'errors':>8}{'input':>12}{'cached':>12}{'output':>10}"
          f"{'≈ USD':>10}")
    for d in days:
        print(f"{d['date']:<12}{d['calls']:>7}{d['blocked']:>9}{d['errors']:>8}{d['input_tokens']:>12,}"
              f"{d['cache_read_input_tokens']:>12,}{d['output_tokens']:>10,}{d['est_usd']:>10.3f}")
    total = log.totals()
    print(f"{'total':<12}{total['calls']:>7}{total['blocked']:>9}{total['errors']:>8}{total['input_tokens']:>12,}"
          f"{total['cache_read_input_tokens']:>12,}{total['output_tokens']:>10,}{total['est_usd']:>10.3f}")
    if last:
        print(f"\nLast {last} events:")
        for e in log.entries()[-last:]:
            detail = e.get("reason") or (f"in {e.get('input_tokens', 0):,} out {e.get('output_tokens', 0):,} "
                                         f"≈ ${e.get('est_usd', 0):.4f} ({e.get('stop_reason')})")
            print(f"  {e['ts']}  {e['event']:<8} q={e.get('question')} r={e.get('round')}  {detail}")
    return 0


def cmd_lint(file: Path, keep: bool = False) -> int:
    from tabbot.analysis.lints import run_lints
    from tabbot.loader import load_workbook

    lw = load_workbook(file)
    findings = run_lints(lw)
    print(f"{len(findings)} finding(s) in {lw.source_name}")
    for f in findings:
        where = ", ".join(f.dashboards or f.sheets)
        print(f"\n[{f.severity.upper()} {f.code}] {f.title}" + (f"  ({where})" if where else ""))
        print(f"  {f.message}")
    lw.close(delete=not keep)
    return 0


def cmd_analyze(file: Path, worksheet: str, category: str | None, keep: bool = False) -> int:
    from tabbot.loader import load_workbook

    lw = load_workbook(file)
    try:
        return _analyze(lw, worksheet, category)
    finally:
        lw.close(delete=not keep)


def _analyze(lw, worksheet: str, category: str | None) -> int:
    from tabbot.analysis.checks import chart_checks
    from tabbot.analysis.recipes import context_for, run_standard
    from tabbot.analysis.summary import key_facts
    from tabbot.views.rebuild import rebuild_worksheet

    ws = lw.model.worksheet(worksheet)
    if ws is None:
        print(f"no worksheet named {worksheet!r}; available: {', '.join(w.name for w in lw.model.worksheets)}")
        return 2
    res = rebuild_worksheet(lw.model, ws, lw.store, lw.table_for(ws.datasource), lw.profile_for(ws.datasource))
    print(f"{ws.name}: {res.chart}, {res.status}" + (f" ({'; '.join(res.reasons)})" if res.reasons else ""))
    if res.status == "failed":
        return 1
    ctx = context_for(res, lw.store, lw.profile_for(ws.datasource), category)
    print(f"measure: {ctx.measure_label} | time: {ctx.date_label} by {ctx.grain} | category: {ctx.category_label}"
          f" | entity: {ctx.entity_label}")
    for c in chart_checks(res, ctx):
        print(f"check [{c.id}]: {c.message}")
    results = run_standard(ctx)
    print("\nKey facts:")
    for fact in key_facts(results, ctx.grain):
        print(f"- {fact}")
    for name in ("period_compare", "driver_scan"):
        r = results[name]
        if r.ok and not r.table.empty:
            print(f"\n{r.title}:")
            print(r.table.drop(columns=["next"], errors="ignore").head(10).to_string(index=False))
    return 0


def cmd_inspect(file: Path, hyper: list[Path], keep: bool = False) -> int:
    from tabbot.loader import load_workbook

    lw = load_workbook(file, extra_extracts=hyper)
    m = lw.model
    out = print
    out(f"Workbook: {lw.source_name}  (Tableau version {m.version}, build {m.source_build})")
    out(f"Workspace: {lw.workspace.root}")
    sr = m.size_report
    out(f"XML size: {sr['total_kb']} KB (~{sr['token_estimate_rough']:,} tokens if sent raw - it never is)")

    out("\n== Data sources ==")
    for ds in m.datasources:
        calcs = [f for f in ds.fields if f.is_calculated]
        out(f"- {ds.caption or ds.name}  [{ds.connection_class}]  extract: {'yes' if ds.has_extract else 'no'}"
            f"  tables: {', '.join(ds.extract_tables) or '-'}")
        out(f"  fields: {len(ds.fields) - len(calcs)} physical, {len(calcs)} calculated")
        for f in calcs:
            used = f"{len(f.used_by_sheets)} sheet(s)" if f.used_by_sheets else "UNUSED"
            out(f"    calc '{f.caption}' ({f.datatype}/{f.role}) - {used}: {f.formula_readable}")
    if m.parameters:
        out(f"- Parameters: {', '.join(p.caption for p in m.parameters)}")

    out("\n== Worksheets ==")
    marks = Counter(w.mark or "?" for w in m.worksheets)
    out(f"{len(m.worksheets)} worksheets ({sum(w.hidden for w in m.worksheets)} hidden); by mark: {dict(marks)}")
    for w in m.worksheets:
        enc = "; ".join(f"{k}={', '.join(r.label for r in v)}" for k, v in w.encodings.items())
        flt = "; ".join(f"{f.field.label} {f.mode}{f' ({f.member_count})' if f.member_count else ''}"
                        for f in w.filters)
        out(f"- {w.name}{' (hidden)' if w.hidden else ''}: {w.mark} | rows: {', '.join(r.label for r in w.rows) or '-'}"
            f" | cols: {', '.join(r.label for r in w.cols) or '-'}")
        if enc:
            out(f"    marks: {enc}")
        if flt:
            out(f"    filters: {flt}")
        for u in w.unsupported:
            out(f"    unsupported: {u}")

    out("\n== Dashboards ==")
    for d in m.dashboards:
        qf = ", ".join(q.field.label for q in d.quick_filters) or "-"
        out(f"- {d.name}: {len(d.sheets)} sheet(s): {', '.join(d.sheets)} | quick filters: {qf}")

    out("\n== Data ==")
    if not lw.tables:
        out("no extract data")
    for t in lw.tables:
        prof = lw.profiles[t.name]
        out(f"- table {t.name} from {t.source_file}: {t.rows:,} rows, {len(t.columns)} columns")
        if prof.entity:
            e = prof.entity
            out(f"  entity to count: {e.field} ({e.distinct:,} distinct, {e.rows_per_entity} rows each; {e.evidence})")
        kinds = Counter(c.kind for c in prof.columns)
        out(f"  column kinds: {dict(kinds)}")
        for c in prof.columns:
            if c.date_format:
                amb = f", AMBIGUOUS with {c.date_format.ambiguous_with}" if c.date_format.ambiguous_with else ""
                out(f"  text date: {c.name} format {c.date_format.format} "
                    f"({c.date_format.parsed_share:.0%} parsed{amb})")
        lvl = [c.name for c in prof.columns if c.entity_level]
        if lvl:
            out(f"  one value per entity: {', '.join(lvl)}")
        if prof.sensitive_suggestions:
            out(f"  suggested sensitive: {', '.join(prof.sensitive_suggestions)}")
    for n in lw.notes + [f"workbook: {u}" for u in m.unsupported]:
        out(f"note: {n}")
    lw.close(delete=not keep)
    if keep:
        out(f"kept local copy: {lw.workspace.root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):   # Windows terminals default to cp1252 and choke on symbols like ≈ →
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if args.command == "version":
        from tabbot import __version__

        print(__version__)
        return 0
    if args.command == "inspect":
        return cmd_inspect(args.file, args.hyper, args.keep)
    if args.command == "lint":
        return cmd_lint(args.file, args.keep)
    if args.command == "analyze":
        return cmd_analyze(args.file, args.worksheet, args.category, args.keep)
    if args.command == "usage":
        return cmd_usage(args.last)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
