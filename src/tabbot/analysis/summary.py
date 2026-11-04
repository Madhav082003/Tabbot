"""Plain-language key facts from analysis results (deterministic text, no AI)."""

from __future__ import annotations

from tabbot.analysis.recipes import AnalysisResult


def _n(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:,.0f}" if abs(f - round(f)) < 1e-9 else f"{f:,.2f}"


def key_facts(results: dict[str, AnalysisResult], grain: str = "period") -> list[str]:
    out: list[str] = []
    t = results.get("trend")
    if t is not None and t.ok and t.summary:
        s = t.summary
        peak = s.get("peak") or {}
        out.append(f"Covers {s['first']} to {s['last']} ({s['periods']} {grain}s); highest full {grain}: "
                   f"{peak.get('period')} ({_n(peak.get('value'))}).")
        out += t.notes
    pc = results.get("period_compare")
    if pc is not None and pc.ok and pc.summary:
        s = pc.summary
        pct = f" ({s['change_pct']:+.0f}%)" if s.get("change_pct") is not None else ""
        out.append(f"{s['last_window']} vs {s['previous_window']}: {_n(s['previous'])} → {_n(s['last'])}{pct}.")
        gain = [g for g in s.get("top_gainers", []) if g.get("change", 0) > 0]
        if gain:
            key = next(k for k in gain[0] if k not in ("previous", "last", "change", "change_pct"))
            out.append("Biggest increases: " + "; ".join(
                f"{g[key]} {_n(g['previous'])} → {_n(g['last'])}" for g in gain) + ".")
        dec = s.get("top_decliners", [])
        if dec:
            key = next(k for k in dec[0] if k not in ("previous", "last", "change", "change_pct"))
            out.append("Decreases: " + "; ".join(f"{g[key]} {_n(g['previous'])} → {_n(g['last'])}" for g in dec)
                       + ".")
    cp = results.get("change_point")
    if cp is not None and cp.ok and cp.summary and cp.summary.get("ratio") and abs(cp.summary["ratio"] - 1) >= 0.3:
        s = cp.summary
        out.append(f"The level shifted at {s['change_at']}: about {s['mean_before']:,.0f} per {grain} before, "
                   f"{s['mean_after']:,.0f} after (×{s['ratio']}).")
    d = results.get("driver_scan")
    if d is not None and d.ok and d.summary.get("top_driver"):
        s, b = d.summary, d.summary["top_driver"]
        where = f" within {s['focus_field']} = {s['focus']}" if s.get("focus") else ""
        out.append(f"Main driver{where}: {b['field']} = {b['value']}, {_n(b['previous'])} → {_n(b['last'])}, "
                   f"{b['share_of_change']:.0%} of the change while usually {b['baseline_share']:.0%} of the total.")
    ss = results.get("share_shift")
    if ss is not None and ss.ok and ss.summary.get("biggest_moves"):
        s = ss.summary
        m = s["biggest_moves"][0]
        out.append(f"Biggest share move {s['first_year']}→{s['last_year']}: {m['member']} "
                   f"{m['first_share']:.0%} → {m['last_share']:.0%} of each year's total.")
    mu = results.get("multiplicity")
    if mu is not None and mu.ok and mu.summary:
        s = mu.summary
        out.append(f"{mu.title}: {s['first']} on average in {s['first_year']}, {s['last']} in {s['last_year']}.")
    co = results.get("concentration")
    if co is not None and co.ok and co.summary.get("sparse_members"):
        out += co.notes
    return out
