"""Tableau-style dashboard viewer (Streamlit). Run:  streamlit run src/tabbot/ui/app.py

Engine logic lives in tabbot.* ; this file only lays things out. Session state keys:
  lw      LoadedWorkbook            page     selected page (dashboard name, sheet or overview) - S5 sets it too
  qf      {page: {field_raw: [values]}} quick-filter selections
  cache   rebuild results           compare  {sheet: CompareReport}     chat_open  bool
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import streamlit as st

from tabbot.config import get_settings
from tabbot.loader import LoadedWorkbook, load_workbook
from tabbot.twb.model import Dashboard, Zone
from tabbot.ui.charts import figure_for
from tabbot.views.compare import compare_with_tableau
from tabbot.views.rebuild import RebuildResult, field_values, rebuild_worksheet

OVERVIEW = "ℹ️ Overview"
SHEET_PREFIX = "📄 "
DASH_HEIGHT_PX = 760

CSS = """
<style>
  #MainMenu, footer, header [data-testid="stToolbar"] {visibility: hidden;}
  .block-container {padding-top: 0.6rem; padding-bottom: 1rem; max-width: 100%;}
  .tb-bar {background: #1F3A5F; color: #fff; padding: 10px 16px; border-radius: 6px; margin-bottom: 8px;
           display: flex; align-items: baseline; gap: 14px; font-family: 'Segoe UI', Arial, sans-serif;}
  .tb-bar .brand {font-weight: 600; font-size: 18px; letter-spacing: .2px;}
  .tb-bar .wb {font-size: 13px; opacity: .85;}
  .tb-title {font-family: 'Segoe UI', Arial, sans-serif; font-size: 20px; font-weight: 600; color: #333;
             margin: 4px 0 2px 0;}
  .tb-sheet {font-family: 'Segoe UI', Arial, sans-serif; font-size: 13px; font-weight: 600; color: #444;}
  .tb-badge {display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 10px; margin-left: 6px;
             font-weight: 500; vertical-align: middle;}
  .ok {background: #E3F1E6; color: #2E7D32;} .warn {background: #FFF3D6; color: #9A6700;}
  .bad {background: #FDE2E1; color: #B42318;} .ver {background: #E3EEFA; color: #1F4E79;}
  .tb-check {font-size: 12px; color: #555; background: #F7F7F7; border-left: 3px solid #F28E2B;
             padding: 6px 10px; margin: 6px 0; border-radius: 3px;}
  div[data-testid="stVerticalBlockBorderWrapper"] {background: #fff;}
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


def _check_lines(res: RebuildResult) -> list[str]:
    out = []
    c = res.checks
    for col, n in (c.get("unparsed_dates") or {}).items():
        if n:
            out.append(f"{n:,} values of {col} could not be read as dates and are missing from the time axis")
    for field, n in (c.get("filter_members_not_in_data") or {}).items():
        out.append(f"filter on {field} lists {n} value(s) that do not exist in the data")
    dt = c.get("distinct_total")
    if dt and not dt["cells_can_be_summed"]:
        out.append(f"cells cannot be added up: they sum to {dt['sum_of_cells']:,} but there are "
                   f"{dt['distinct_total']:,} distinct in total (one record appears in several cells)")
    if res.truncated:
        out.append("view is very large; only part of it is shown")
    return out


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
            for line in _check_lines(res):
                st.markdown(f"<div class='tb-check'>{line}</div>", unsafe_allow_html=True)
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
    cols = st.columns(min(len(d.quick_filters), 4))
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
    _render_quick_filters(lw, d, page)
    checks: list[str] = []
    for row in _rows_of_zones(d):
        cols = st.columns([max(z.w, 1) for z in row])
        for col, z in zip(cols, row, strict=True):
            with col:
                h = max(260, int(z.h / 100_000 * DASH_HEIGHT_PX)) if z.h else 340
                _render_sheet(lw, z.name, page, h)
                res = _result(lw, z.name, page)
                checks += [f"<b>{_sheet_title(lw, z.name)}</b>: {c}" for c in _check_lines(res)]
    if checks:
        st.markdown("<div class='tb-check'>⚑ " + "<br>⚑ ".join(checks) + "</div>", unsafe_allow_html=True)
    thumb = next((t for t in lw.model.thumbnails if t.name == d.name and t.file), None)
    if thumb is not None:
        with st.expander("Tableau's own preview of this dashboard (from the file)"):
            st.image(thumb.file, width=min(thumb.width or 192, 400))


def _render_overview(lw: LoadedWorkbook) -> None:
    m = lw.model
    st.markdown(f"<div class='tb-title'>{lw.source_name}</div>", unsafe_allow_html=True)
    c = st.columns(4)
    c[0].metric("Dashboards", len(m.dashboards))
    c[1].metric("Worksheets", len(m.worksheets), f"{sum(w.hidden for w in m.worksheets)} hidden",
                delta_color="off")
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


def _render_chat_panel() -> None:
    with st.container(border=True):
        st.markdown("**Ask Claude**")
        st.caption("Chat is connected in step S5. It will explain these dashboards, cite the numbers, and switch "
                   "the view for you.")
        st.chat_input("Ask about this workbook…", disabled=True, key="chat_disabled")


# --------------------------------------------------------------------------------------------- page


def main() -> None:
    st.set_page_config(page_title="Tableau Insights", page_icon="📊", layout="wide",
                       initial_sidebar_state="collapsed")
    st.markdown(CSS, unsafe_allow_html=True)
    for k, v in (("qf", {}), ("cache", {}), ("compare", {}), ("chat_open", True)):
        st.session_state.setdefault(k, v)

    auto = os.environ.get("TABBOT_AUTOLOAD")
    if auto and st.session_state.get("lw") is None:
        _load(Path(auto))
        st.session_state.loaded_digest = "autoload"

    lw: LoadedWorkbook | None = st.session_state.get("lw")
    top = st.columns([7, 2, 1.3])
    with top[0]:
        st.markdown(f"<div class='tb-bar'><span class='brand'>📊 Tableau Insights</span>"
                    f"<span class='wb'>{lw.source_name if lw else 'no workbook open'}</span></div>",
                    unsafe_allow_html=True)
    with top[1], st.popover("📂 Open workbook", width="stretch"):
        up = st.file_uploader("Tableau workbook (.twbx)", type=["twbx", "twb"], key="upload")
        if up is not None:
            _load_upload(up)
        if st.button("Try the demo workbook", key="demo", width="stretch"):
            _load_demo()
            st.rerun()
    with top[2]:
        label = "Hide chat ▸" if st.session_state.chat_open else "◂ Chat"
        if st.button(label, width="stretch", key="chat_toggle"):
            st.session_state.chat_open = not st.session_state.chat_open
            st.rerun()

    lw = st.session_state.get("lw")
    if lw is None:
        st.info("Open a Tableau workbook (.twbx) with **📂 Open workbook**, or try the demo workbook.")
        return

    pages = [OVERVIEW] + [d.name for d in lw.model.dashboards] + \
        [SHEET_PREFIX + w.name for w in lw.model.worksheets if not w.hidden]
    if st.session_state.get("page") not in pages:
        st.session_state.page = OVERVIEW
    st.segmented_control("Pages", pages, key="page", label_visibility="collapsed")
    page = st.session_state.get("page") or OVERVIEW

    main_col, chat_col = (st.columns([3.3, 1]) if st.session_state.chat_open else (st.container(), None))
    with main_col:
        if page == OVERVIEW:
            _render_overview(lw)
        elif page.startswith(SHEET_PREFIX):
            name = page[len(SHEET_PREFIX):]
            st.markdown(f"<div class='tb-title'>{_sheet_title(lw, name)}</div>", unsafe_allow_html=True)
            _render_sheet(lw, name, page, 480)
        else:
            _render_dashboard(lw, lw.model.dashboard(page))
    if chat_col is not None:
        with chat_col:
            _render_chat_panel()


main()
