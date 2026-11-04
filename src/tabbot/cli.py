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
    return p


def cmd_inspect(file: Path, hyper: list[Path]) -> int:
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
    lw.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        from tabbot import __version__

        print(__version__)
        return 0
    if args.command == "inspect":
        return cmd_inspect(args.file, args.hyper)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
