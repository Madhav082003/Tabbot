"""Tableau-style dashboard viewer (Streamlit). Run:  streamlit run src/tabbot/ui/app.py

Engine logic lives in tabbot.* ; this file only lays things out. Session state keys:
  lw      LoadedWorkbook            page     selected page (dashboard name, sheet or overview) - S5 sets it too
  qf      {page: {field_raw: [values]}} quick-filter selections
  cache   rebuild results           compare  {sheet: CompareReport}     chat_open  bool
  lints   workbook findings         analysis {key: (ctx, {recipe: AnalysisResult})} checks {key: [Check]}
  chat    ChatSession               chat_log [{role, text, tools}]   pending_nav (page, {raw: values}) from Claude
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import streamlit as st

from tabbot.analysis.checks import Check, chart_checks
from tabbot.analysis.lints import Finding, run_lints
from tabbot.analysis.recipes import context_for, run_standard
from tabbot.analysis.summary import key_facts
from tabbot.config import get_settings
from tabbot.llm.audit import AuditLog
from tabbot.llm.orchestrator import ChatSession
from tabbot.loader import LoadedWorkbook, load_workbook
from tabbot.twb.model import Dashboard, Zone
from tabbot.ui.charts import figure_for
from tabbot.views.compare import compare_with_tableau
from tabbot.views.rebuild import RebuildResult, field_values, rebuild_worksheet

OVERVIEW = "ℹ️ Overview"
FINDINGS = "🔎 Findings"
SEV_ICON = {"error": "🟥", "warn": "🟧", "info": "🟦"}
SHEET_PREFIX = "📄 "
DASH_HEIGHT_PX = 760

CSS = """
<style>
  /* remove Streamlit's own header strip: it overlapped the top of the page */
  header[data-testid="stHeader"] {display: none !important;}
  #MainMenu, footer {visibility: hidden;}
  .block-container {padding: 0.9rem 1.4rem 1rem 1.4rem !important; max-width: 100% !important;}
  html, body, [class*="css"] {font-family: 'Segoe UI', 'Benton Sans', Arial, sans-serif;}

  /* app bar */
  .st-key-appbar {background: #1F3A5F; border-radius: 8px; padding: 10px 14px 10px 16px; min-height: 52px;
                  justify-content: center;}
  .st-key-appbar p {color: #fff; margin: 0;}
  .st-key-appbar .brand {font-weight: 600; font-size: 18px; letter-spacing: .2px; color: #fff;}
  .st-key-appbar .wb {font-size: 13px; color: #C9D6E8; margin-left: 12px;}
  .st-key-appbar button {background: rgba(255,255,255,.10) !important; color: #fff !important;
                         border: 1px solid rgba(255,255,255,.35) !important;}
  .st-key-appbar button:hover {background: rgba(255,255,255,.22) !important;}
  .st-key-appbar button p {color: #fff !important;}

  /* page tabs, Tableau style */
  .st-key-pagebar {border-bottom: 1px solid #D9D9D9; margin: 2px 0 6px 0; padding-bottom: 4px;}
  .st-key-pagebar button {border-radius: 4px 4px 0 0 !important; font-size: 13px !important;}

  .tb-title {font-size: 20px; font-weight: 600; color: #333; margin: 4px 0 2px 0;}
  .tb-sheet {font-size: 13px; font-weight: 600; color: #444;}
  .tb-badge {display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 10px; margin-left: 6px;
             font-weight: 500; vertical-align: middle;}
  .ok {background: #E3F1E6; color: #2E7D32;} .warn {background: #FFF3D6; color: #9A6700;}
  .bad {background: #FDE2E1; color: #B42318;} .ver {background: #E3EEFA; color: #1F4E79;}
  .tb-check {font-size: 12px; color: #555; background: #F7F7F7; border-left: 3px solid #F28E2B;
             padding: 6px 10px; margin: 6px 0; border-radius: 3px;}

  /* start page */
  .tb-hero h1 {font-size: 30px; font-weight: 650; color: #1F3A5F; margin: 0 0 4px 0;}
  .tb-hero p {font-size: 15px; color: #555; margin: 0 0 6px 0;}
  .tb-feature {background: #F6F8FB; border-radius: 8px; padding: 12px 14px; height: 100%;}
  .tb-feature b {color: #1F3A5F;}
  .tb-feature span {font-size: 13px; color: #555;}
</style>
"""


# --------------------------------------------------------------------------------------------- loading


def _load(path: Path) -> None:
    old: LoadedWorkbook | None = st.session_state.get("lw")
    if old is not None:
        old.close()
    st.session_state.lw = load_workbook(path, settings=get_settings())
    st.session_state.page = OVERVIEW
    st.session_state.qf, st.session_state.cache, st.session_state.compare = {}, {}, {}
    st.session_state.lints, st.session_state.analysis, st.session_state.checks = None, {}, {}
    st.session_state.chat, st.session_state.chat_log, st.session_state.pending_nav = None, [], None


def _load_upload(uploaded) -> None:
    data = uploaded.getvalue()
    digest = hashlib.sha256(data).hexdigest()[:16]
    if st.session_state.get("loaded_digest") == digest:
        return
    incoming = get_settings().workspace_dir / "_incoming" / digest
    incoming.mkdir(parents=True, exist_ok=True)
    path = incoming / Path(uploaded.name).name
    path.write_bytes(data)
    try:
        with st.spinner("Reading workbook and extract…"):
            _load(path)
        st.session_state.loaded_digest = digest
    except Exception as exc:  # noqa: BLE001 - show the problem, keep the app alive
        st.error(f"Could not open {uploaded.name}: {exc}")
    finally:
        shutil.rmtree(incoming, ignore_errors=True)


def _load_demo() -> None:
    from tabbot.demo_workbook import build

    out = get_settings().workspace_dir / "_demo"
    twbx = out / "fixture.twbx"
    if not twbx.exists():
        build(out)
    with st.spinner("Building demo workbook…"):
        _load(twbx)
    st.session_state.loaded_digest = "demo"


# --------------------------------------------------------------------------------------------- helpers


def _result(lw: LoadedWorkbook, sheet: str, page: str) -> RebuildResult:
    ws = lw.model.worksheet(sheet)
    qf = {k: tuple(v) for k, v in st.session_state.qf.get(page, {}).items() if v}
    key = (sheet, tuple(sorted(qf.items())))
    cache = st.session_state.cache
    if key not in cache:
        cache[key] = rebuild_worksheet(lw.model, ws, lw.store, lw.table_for(ws.datasource),
                                       lw.profile_for(ws.datasource), {k: list(v) for k, v in qf.items()})
    return cache[key]


def _rows_of_zones(d: Dashboard) -> list[list[Zone]]:
    zones = [z for z in d.zones if z.name in d.sheets]
    if not zones or all(z.w == 0 for z in zones):
        zs = [Zone(name=s, w=1, h=46000) for s in d.sheets]
        return [zs[i:i + 2] for i in range(0, len(zs), 2)]
    zones.sort(key=lambda z: (z.y, z.x))
    rows: list[list[Zone]] = []
    for z in zones:
        if rows and abs(rows[-1][0].y - z.y) <= 1500:
            rows[-1].append(z)
        else:
            rows.append([z])
    return rows


def _badge(res: RebuildResult, sheet: str) -> str:
    rep = st.session_state.compare.get(sheet)
    if rep is not None and rep.status == "match":
        return "<span class='tb-badge ver'>✓ Matches Tableau</span>"
    if rep is not None and rep.status == "mismatch":
        return "<span class='tb-badge bad'>✗ Differs from Tableau</span>"
    if res.status == "exact":
        return "<span class='tb-badge ok' title='Every part of the definition was understood. Not yet compared " \
               "with Tableau.'>✓ Exact definition</span>"
    if res.status == "approximate":
        tip = "; ".join(res.reasons)[:400].replace("'", "’")
        return f"<span class='tb-badge warn' title='{tip}'>⚠ Approximate</span>"
    return "<span class='tb-badge bad'>✗ Could not redraw</span>"


def _qf_key(page: str) -> tuple:
    return tuple(sorted((k, tuple(v)) for k, v in st.session_state.qf.get(page, {}).items() if v))


def _checks(lw: LoadedWorkbook, sheet: str, page: str) -> list[Check]:
    key = (sheet, _qf_key(page))
    if key not in st.session_state.checks:
        res = _result(lw, sheet, page)
        ws = lw.model.worksheet(sheet)
        ctx = None
        if res.status != "failed" and res.measures and lw.store is not None:
            try:
                ctx = context_for(res, lw.store, lw.profile_for(ws.datasource))
            except ValueError:
                ctx = None
        st.session_state.checks[key] = chart_checks(res, ctx)
    return st.session_state.checks[key]


def _lints(lw: LoadedWorkbook) -> list[Finding]:
    if st.session_state.get("lints") is None:
        st.session_state.lints = run_lints(lw)
    return st.session_state.lints


def _main_sheet(lw: LoadedWorkbook, d: Dashboard, page: str) -> str | None:
    """The sheet the dashboard's analysis is built on: one with a time axis and a category, else any measure."""
    best = None
    for s in d.sheets:
        res = _result(lw, s, page)
        if res.status == "failed" or not res.measures:
            continue
        score = (bool(res.time_dims()), any(not x.date_sql for x in res.dims))
        if best is None or score > best[0]:
            best = (score, s)
    return best[1] if best else None


def _analysis(lw: LoadedWorkbook, sheet: str, page: str):
    key = (sheet, _qf_key(page))
    if key not in st.session_state.analysis:
        res = _result(lw, sheet, page)
        ws = lw.model.worksheet(sheet)
        ctx = context_for(res, lw.store, lw.profile_for(ws.datasource))
        st.session_state.analysis[key] = (ctx, run_standard(ctx))
    return st.session_state.analysis[key]


def _sheet_title(lw: LoadedWorkbook, sheet: str) -> str:
    ws = lw.model.worksheet(sheet)
    t = (ws.title or "").strip()
    return ws.name if not t or "<Sheet Name>" in t else t.replace("<Sheet Name>", ws.name)


# --------------------------------------------------------------------------------------------- rendering


def _render_sheet(lw: LoadedWorkbook, sheet: str, page: str, height: int) -> None:
    res = _result(lw, sheet, page)
    with st.container(border=True):
        st.markdown(f"<span class='tb-sheet'>{_sheet_title(lw, sheet)}</span>{_badge(res, sheet)}",
                    unsafe_allow_html=True)
        fig, table, notes = figure_for(res, height=height)
        if fig is not None:
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False},
                            key=f"fig::{page}::{sheet}")
        elif table is not None and not table.empty:
            st.dataframe(table, width="stretch", height=min(height, 38 + 35 * len(table)))
        elif res.status == "failed":
            st.warning("This sheet could not be redrawn: " + "; ".join(res.reasons))
        else:
            st.info("No data for the current filters.")
        for n in notes:
            st.caption(n)
        with st.expander("Details · check against Tableau"):
            ws = lw.model.worksheet(sheet)
            marks = "; ".join(f"{k}: " + ", ".join(r.label for r in v) for k, v in ws.encodings.items())
            filters = "; ".join(f"{f.field.label} {f.mode} ({f.member_count})" for f in ws.filters)
            st.markdown(
                f"**Chart:** {res.chart} (Tableau mark: {ws.mark})  \n"
                f"**Rows:** {', '.join(r.label for r in ws.rows) or '–'}  \n"
                f"**Columns:** {', '.join(r.label for r in ws.cols) or '–'}  \n"
                f"**Marks:** {marks or '–'}  \n**Filters:** {filters or '–'}")
            for c in _checks(lw, sheet, page):
                st.markdown(f"<div class='tb-check'>{c.message}</div>", unsafe_allow_html=True)
            if res.reasons:
                st.markdown("**Why approximate:** " + "; ".join(res.reasons))
            st.dataframe(res.labelled(), width="stretch", height=200)
            up = st.file_uploader("Compare with Tableau: upload this sheet's Download → Crosstab (or Data) CSV",
                                  type=["csv", "txt"], key=f"cmp::{page}::{sheet}")
            if up is not None:
                rep = compare_with_tableau(res, up.getvalue())
                st.session_state.compare[sheet] = rep
                (st.success if rep.status == "match" else st.error if rep.status == "mismatch" else st.info)(
                    rep.summary + (f" — {rep.note}" if rep.note else ""))
                for d in rep.differences[:10]:
                    st.caption(d)


def _render_quick_filters(lw: LoadedWorkbook, d: Dashboard, page: str) -> None:
    if not d.quick_filters:
        return
    cols = st.columns(max(4, len(d.quick_filters)))  # compact dropdowns, like Tableau's quick filters
    sel = st.session_state.qf.setdefault(page, {})
    for i, q in enumerate(d.quick_filters):
        ds = q.field.datasource
        values = field_values(lw.model, q.field, lw.store, lw.table_for(ds), lw.profile_for(ds))
        with cols[i % len(cols)]:
            chosen = st.multiselect(q.field.label, values, default=[v for v in sel.get(q.field.raw, []) if v in values],
                                    placeholder="(All)", key=f"qf::{page}::{q.field.raw}")
        if chosen != sel.get(q.field.raw, []):
            sel[q.field.raw] = chosen


def _render_dashboard(lw: LoadedWorkbook, d: Dashboard) -> None:
    page = d.name
    st.markdown(f"<div class='tb-title'>{d.title or d.name}</div>", unsafe_allow_html=True)
    issues = [f for f in _lints(lw) if d.name in f.dashboards and f.severity != "info"]
    if issues:
        st.markdown("<div class='tb-check' style='border-left-color:#E15759'>" + "<br>".join(
            f"{SEV_ICON[f.severity]} <b>{f.title}</b>: {f.message}" for f in issues) + "</div>",
            unsafe_allow_html=True)
    _render_quick_filters(lw, d, page)
    grouped: dict[str, list[str]] = {}
    total_w = max((z.x + z.w for z in d.zones if z.name in d.sheets), default=0)
    for row in _rows_of_zones(d):
        widths, slots, cursor = [], [], 0
        for z in sorted(row, key=lambda z: z.x):
            if total_w and z.x - cursor > 0:      # empty space in the Tableau layout
                widths.append(z.x - cursor)
                slots.append(None)
            widths.append(max(z.w, 1))
            slots.append(z)
            cursor = z.x + z.w
        if total_w and total_w - cursor > 0:
            widths.append(total_w - cursor)
            slots.append(None)
        cols = st.columns(widths)
        for col, z in zip(cols, slots, strict=True):
            if z is None:
                continue
            with col:
                h = max(260, int(z.h / 100_000 * DASH_HEIGHT_PX)) if z.h else 340
                _render_sheet(lw, z.name, page, h)
                for c in _checks(lw, z.name, page):
                    grouped.setdefault(c.message, []).append(_sheet_title(lw, z.name))
    if grouped:
        lines = [f"⚑ {msg} <i>({', '.join(names)})</i>" for msg, names in grouped.items()]
        st.markdown("<div class='tb-check'>" + "<br>".join(lines) + "</div>", unsafe_allow_html=True)
    _render_analysis(lw, d, page)
    thumb = next((t for t in lw.model.thumbnails if t.name == d.name and t.file), None)
    if thumb is not None:
        with st.expander("Tableau's own preview of this dashboard (from the file)"):
            st.image(thumb.file, width=min(thumb.width or 192, 400))


def _render_analysis(lw: LoadedWorkbook, d: Dashboard, page: str) -> None:
    if lw.store is None:
        return
    sheet = _main_sheet(lw, d, page)
    if sheet is None:
        return
    with st.expander(f"📈 Analysis of '{_sheet_title(lw, sheet)}' (computed by the app, not AI)", expanded=True):
        try:
            ctx, results = _analysis(lw, sheet, page)
        except ValueError as exc:
            st.caption(f"No analysis: {exc}")
            return
        for fact in key_facts(results, ctx.grain):
            st.markdown(f"- {fact}")
        tabs = st.tabs(["Change by member", "Drivers", "Trend", "Share by year", "Per record"])
        pc, dr, tr = results["period_compare"], results["driver_scan"], results["trend"]
        with tabs[0]:
            st.caption(pc.title)
            if not pc.table.empty:
                st.dataframe(pc.table, width="stretch", height=240, hide_index=True)
        with tabs[1]:
            if dr.ok and not dr.table.empty:
                within = (f"Within {dr.summary.get('focus_field')} = {dr.summary.get('focus')}, "
                          if dr.summary.get("focus") else "")
                st.caption(f"{within}{dr.summary['last_window']} vs {dr.summary['previous_window']}")
                st.dataframe(dr.table.drop(columns=["next"]), width="stretch", height=240, hide_index=True)
            for n in dr.notes:
                st.caption(n)
        with tabs[2]:
            if not tr.table.empty:
                st.dataframe(tr.table[["label", "value"]], width="stretch", height=240, hide_index=True)
        with tabs[3]:
            ss = results["share_shift"]
            if ss.ok:
                st.dataframe(ss.table, width="stretch", height=240, hide_index=True)
        with tabs[4]:
            mu = results["multiplicity"]
            if mu.ok:
                st.caption(mu.title)
                st.dataframe(mu.table, width="stretch", height=240, hide_index=True)


def _render_findings(lw: LoadedWorkbook) -> None:
    findings = _lints(lw)
    st.markdown("<div class='tb-title'>Workbook findings</div>", unsafe_allow_html=True)
    st.caption("Rule-based checks of how the workbook is defined (computed by the app, not AI). "
               "🟥 wrong result likely · 🟧 can mislead · 🟦 good to know")
    if not findings:
        st.success("No definition problems found.")
    for f in findings:
        with st.container(border=True):
            where = ", ".join(f.dashboards or f.sheets)
            st.markdown(f"{SEV_ICON[f.severity]} **{f.title}** · `{f.code}`" + (f" · _{where}_" if where else ""))
            st.markdown(f.message)
            if f.evidence.get("file"):
                st.caption(f"File: {f.evidence['file']} (shown here only; never sent to Claude)")


def _render_overview(lw: LoadedWorkbook) -> None:
    m = lw.model
    st.markdown(f"<div class='tb-title'>{lw.source_name}</div>", unsafe_allow_html=True)
    c = st.columns(4)
    c[0].metric("Dashboards", len(m.dashboards))
    c[1].metric("Worksheets", len(m.worksheets))
    c[1].caption(f"{sum(w.hidden for w in m.worksheets)} hidden (shown inside dashboards)")
    c[2].metric("Calculated fields", len(m.calculated_fields))
    c[3].metric("Extract rows", f"{sum(t.rows for t in lw.tables):,}" if lw.tables else "no extract")
    for ds in m.datasources:
        prof = lw.profile_for(ds.name)
        with st.container(border=True):
            st.markdown(f"**{ds.caption or ds.name}** · connection `{ds.connection_class}` · "
                        f"extract: {'yes' if ds.has_extract else 'no'}")
            if prof is not None and prof.entity is not None:
                e = prof.entity
                st.markdown(f"Counting **{e.field}**: {e.distinct:,} distinct, {e.rows_per_entity} rows each "
                            f"({e.evidence}).")
            if prof is not None:
                dates = [f"{c.name} ({c.date_format.format})" for c in prof.columns if c.date_format]
                if dates:
                    st.markdown("Text dates read as: " + ", ".join(dates))
                if prof.sensitive_suggestions:
                    st.markdown("Looks sensitive (never sent to Claude as values): "
                                + ", ".join(prof.sensitive_suggestions))
    st.markdown("**Dashboards**")
    for d in m.dashboards:
        st.markdown(f"- **{d.name}** — {len(d.sheets)} sheet(s)"
                    + (f"; filters: {', '.join(q.field.label for q in d.quick_filters)}" if d.quick_filters else ""))
    for n in lw.notes + m.unsupported:
        st.caption(f"Note: {n}")


def _render_start_page() -> None:
    st.write("")
    _, mid, _ = st.columns([1, 2.2, 1])
    with mid:
        st.markdown("<div class='tb-hero'><h1>Open a Tableau workbook</h1>"
                    "<p>Your dashboards are redrawn from the workbook's own data, checked for definition problems "
                    "and analysed — everything runs on this computer.</p></div>", unsafe_allow_html=True)
        with st.container(border=True):
            up = st.file_uploader("Upload a packaged workbook (.twbx)", type=["twbx", "twb"], key="upload_main")
            if up is not None:
                _load_upload(up)
                st.rerun()
            c1, c2 = st.columns([1, 1.4], vertical_alignment="center")
            with c1:
                if st.button("▶ Try the demo workbook", key="demo_main", type="primary", width="stretch"):
                    _load_demo()
                    st.rerun()
            with c2:
                st.caption("Made-up data with a hidden growth story and a few planted definition problems.")
        st.write("")
        f = st.columns(3)
        feats = [("📊 Dashboards, redrawn", "Every sheet rebuilt from its definition and data, with a badge saying "
                  "whether the redraw is exact."),
                 ("🔎 Problems found", "Rule-based checks: misnamed sheets, counting dates, copied filters, "
                  "partial periods…"),
                 ("💬 Ask Claude", "Claude explains the numbers, cites them, and switches the view for you.")]
        for col, (title, text) in zip(f, feats, strict=True):
            col.markdown(f"<div class='tb-feature'><b>{title}</b><br><span>{text}</span></div>",
                         unsafe_allow_html=True)


SUGGESTIONS = ["What are the key insights on this dashboard?", "Is anything wrong in how this workbook is built?",
               "What drove the biggest change?"]


def _chat_session(lw: LoadedWorkbook) -> ChatSession:
    if st.session_state.get("chat") is None:
        st.session_state.chat = ChatSession(lw, get_settings())
    return st.session_state.chat


def _app_context(lw: LoadedWorkbook, page: str) -> str:
    sel = {k: v for k, v in st.session_state.qf.get(page, {}).items() if v}
    labels = {}
    d = lw.model.dashboard(page)
    if d is not None:
        for q in d.quick_filters:
            if q.field.raw in sel:
                labels[q.field.label] = sel[q.field.raw]
    return f"Open page: {page}" + (f"; quick filters: {labels}" if labels else "; no quick filters set")


def _ask(lw: LoadedWorkbook, page: str, question: str, box) -> None:
    log = st.session_state.chat_log
    log.append({"role": "user", "text": question, "tools": []})
    with box:
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"), st.spinner("Looking at the workbook…"):
            turn = _chat_session(lw).ask(question, _app_context(lw, page))
    tools = [f"{'⚠️' if t.is_error else '🔧'} {t.name}({', '.join(f'{k}={v}' for k, v in t.input.items())})"
             + (f" → {t.preview}" if t.preview else "") for t in turn.tool_calls]
    meta = ""
    if turn.usage:
        u = turn.usage
        meta = (f"{turn.rounds} round(s) · in {u.get('input_tokens', 0):,} "
                f"(+{u.get('cache_read_input_tokens', 0):,} cached) · out {u.get('output_tokens', 0):,}")
    log.append({"role": "assistant", "text": turn.text or "_(no answer)_", "tools": tools, "meta": meta,
                "error": bool(turn.error)})
    if turn.actions:
        a = turn.actions[-1]
        st.session_state.pending_nav = (a.page, a.quick_filters)
    st.rerun()


def _render_chat_panel(lw: LoadedWorkbook, page: str) -> None:
    settings = get_settings()
    with st.container(border=True):
        st.markdown("**💬 Ask Claude**")
        ready = bool(settings.anthropic_api_key) or settings.dry_run
        st.caption(f"{settings.claude_model} · effort {settings.claude_effort}"
                   + (" · DRY RUN (nothing is sent)" if settings.dry_run else ""))
        if not ready:
            st.info("Add your Anthropic API key to start chatting:\n\n1. Copy `C:\\tabbot\\.env.example` to "
                    "`.env`\n2. Paste the key after `ANTHROPIC_API_KEY=`\n3. Restart the app")
        box = st.container(height=560, border=False)
        with box:
            if not st.session_state.chat_log:
                st.caption("Claude sees the workbook's structure and summary numbers computed here - never the data "
                           "rows. Every request is logged below before it is sent.")
            for m in st.session_state.chat_log:
                with st.chat_message(m["role"]):
                    st.markdown(m["text"])
                    if m.get("tools"):
                        with st.expander(f"{len(m['tools'])} tool call(s)"):
                            for t in m["tools"]:
                                st.caption(t)
                    if m.get("meta"):
                        st.caption(m["meta"])
        pending = None
        if not st.session_state.chat_log and ready:
            for i, sug in enumerate(SUGGESTIONS):
                if st.button(sug, key=f"sug{i}", width="stretch"):
                    pending = sug
        q = st.chat_input("Ask about this workbook…", key="chat_in", disabled=not ready)
        question = q or pending
        if question:
            _ask(lw, page, question, box)
        with st.expander("What was sent to Claude"):
            entries = AuditLog(settings.workspace_dir / "audit" / "outbound.jsonl").read(last=10)
            if not entries:
                st.caption("Nothing sent yet.")
            for e in reversed(entries):
                st.caption(f"{e['ts']} · {e['chars']:,} chars (~{e['approx_tokens']:,} tokens)"
                           + (" · dry run" if e.get("dry_run") else ""))
            if entries:
                if st.toggle("Show the latest request in full", key="show_req"):
                    st.json(entries[-1]["request"], expanded=False)
            if st.session_state.chat_log and st.button("Clear chat", key="clear_chat"):
                st.session_state.chat, st.session_state.chat_log = None, []
                st.rerun()


# --------------------------------------------------------------------------------------------- page


def main() -> None:
    st.set_page_config(page_title="Tableau Insights", page_icon="📊", layout="wide",
                       initial_sidebar_state="collapsed")
    st.markdown(CSS, unsafe_allow_html=True)
    for k, v in (("qf", {}), ("cache", {}), ("compare", {}), ("chat_open", True), ("lints", None),
                 ("analysis", {}), ("checks", {}), ("chat", None), ("chat_log", []), ("pending_nav", None)):
        st.session_state.setdefault(k, v)

    auto = os.environ.get("TABBOT_AUTOLOAD")
    if auto and st.session_state.get("lw") is None:
        _load(Path(auto))
        st.session_state.loaded_digest = "autoload"

    lw: LoadedWorkbook | None = st.session_state.get("lw")
    with st.container(key="appbar"):
        if lw is None:
            st.markdown("<span class='brand'>📊 Tableau Insights</span>", unsafe_allow_html=True)
        else:
            top = st.columns([7, 1.6, 1.2], vertical_alignment="center")
            with top[0]:
                st.markdown(f"<span class='brand'>📊 Tableau Insights</span>"
                            f"<span class='wb'>{lw.source_name}</span>", unsafe_allow_html=True)
            with top[1], st.popover("📂 Open workbook", width="stretch"):
                up = st.file_uploader("Tableau workbook (.twbx)", type=["twbx", "twb"], key="upload")
                if up is not None:
                    _load_upload(up)
                    st.rerun()
                if st.button("Try the demo workbook", key="demo", width="stretch"):
                    _load_demo()
                    st.rerun()
            with top[2]:
                label = "Hide chat ▸" if st.session_state.chat_open else "◂ Show chat"
                if st.button(label, width="stretch", key="chat_toggle"):
                    st.session_state.chat_open = not st.session_state.chat_open
                    st.rerun()

    lw = st.session_state.get("lw")
    if lw is None:
        _render_start_page()
        return

    nav = st.session_state.get("pending_nav")   # set by Claude's show_in_app; applied before any widget exists
    if nav:
        target, sel = nav
        st.session_state.page = target
        st.session_state.qf[target] = dict(sel)
        d = lw.model.dashboard(target)
        for q in (d.quick_filters if d else []):
            st.session_state[f"qf::{target}::{q.field.raw}"] = list(sel.get(q.field.raw, []))
        st.session_state.pending_nav = None

    pages = [OVERVIEW, FINDINGS] + [d.name for d in lw.model.dashboards] + \
        [SHEET_PREFIX + w.name for w in lw.model.worksheets if not w.hidden]
    if st.session_state.get("page") not in pages:
        st.session_state.page = OVERVIEW
    with st.container(key="pagebar"):
        st.segmented_control("Pages", pages, key="page", label_visibility="collapsed")
    page = st.session_state.get("page") or OVERVIEW

    main_col, chat_col = (st.columns([3.3, 1]) if st.session_state.chat_open else (st.container(), None))
    with main_col:
        if page == OVERVIEW:
            _render_overview(lw)
        elif page == FINDINGS:
            _render_findings(lw)
        elif page.startswith(SHEET_PREFIX):
            name = page[len(SHEET_PREFIX):]
            st.markdown(f"<div class='tb-title'>{_sheet_title(lw, name)}</div>", unsafe_allow_html=True)
            _render_sheet(lw, name, page, 480)
        else:
            _render_dashboard(lw, lw.model.dashboard(page))
    if chat_col is not None:
        with chat_col:
            _render_chat_panel(lw, page)


main()
