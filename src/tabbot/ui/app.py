"""Tableau Insights (Streamlit). Run:  streamlit run src/tabbot/ui/app.py

Kept deliberately simple: open a workbook, read a short description of it, ask the assistant about it.
(No charts or dashboard tabs - a written explanation of the workbook comes next.)
Engine logic lives in tabbot.* ; this file only lays things out. Session state keys:
  lw    LoadedWorkbook     chat  ChatSession     chat_log  [{role, text, tools, meta}]
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import shutil
from pathlib import Path

import streamlit as st

from tabbot.config import get_settings
from tabbot.data.store import qi
from tabbot.llm.orchestrator import ChatSession
from tabbot.loader import LoadedWorkbook, load_workbook
from tabbot.workspace import purge_workspaces

log = logging.getLogger(__name__)

CSS = """
<style>
  /* remove Streamlit's own header strip: it overlapped the top of the page */
  header[data-testid="stHeader"] {display: none !important;}
  #MainMenu, footer {visibility: hidden;}
  .block-container {padding: 0.9rem 1.4rem 1rem 1.4rem !important; max-width: 100% !important;}
  html, body, [class*="css"] {font-family: 'Segoe UI', 'Benton Sans', Arial, sans-serif;}

  .st-key-appbar {background: #1F3A5F; border-radius: 8px; padding: 10px 14px 10px 16px; min-height: 52px;
                  justify-content: center;}
  .st-key-appbar p {color: #fff; margin: 0;}
  .st-key-appbar .brand {font-weight: 600; font-size: 18px; letter-spacing: .2px; color: #fff;}
  .st-key-appbar .wb {font-size: 13px; color: #C9D6E8; margin-left: 12px;}
  .st-key-appbar button {background: rgba(255,255,255,.10) !important; color: #fff !important;
                         border: 1px solid rgba(255,255,255,.35) !important;}
  .st-key-appbar button:hover {background: rgba(255,255,255,.22) !important;}
  .st-key-appbar button p {color: #fff !important;}

  /* workbook view: the page stays put, each pane scrolls on its own (heights: see pane_css) */
  .st-key-left_pane {min-height: 380px; overflow-y: auto !important;
                     overflow-x: hidden; padding-right: 10px; flex-wrap: nowrap;}
  div:has(> .st-key-chat_pane) {min-height: 380px;}
  .st-key-chat_pane {height: 100% !important; display: flex !important; flex-direction: column;
                     flex-wrap: nowrap !important;}
  div:has(> .st-key-chat_msgs) {flex: 1 1 auto !important; min-height: 120px; display: flex;
                                flex-direction: column;}
  .st-key-chat_msgs {flex: 1 1 auto !important; min-height: 0; height: 0; overflow-y: auto !important;
                     overflow-x: hidden;}
  .st-key-chat_pane .stButton button {padding: 2px 10px; min-height: 0;}
  .st-key-chat_pane .stButton button p {font-size: 13px;}

  /* the dashboard bar: one row of boxes that scrolls sideways when there are many */
  .st-key-dashbar {padding: 2px 0 0 0;}
  .st-key-dashbar [data-testid="stButtonGroup"] > div {flex-wrap: nowrap !important; overflow-x: auto;
                                                       overflow-y: hidden; padding-bottom: 4px;}
  .st-key-dashbar [data-testid="stButtonGroup"] button {white-space: nowrap; flex-shrink: 0;}
  .tb-dash-title {font-size: 21px; font-weight: 600; color: #1F3A5F; margin: 4px 0 0 0;}
  .tb-dash-sub {font-size: 13px; color: #666; margin: 0 0 6px 0;}
  .tb-summary {background: #F3F6FA; border-left: 3px solid #1F3A5F; border-radius: 4px; padding: 10px 14px;
               margin: 6px 0 10px 0; font-size: 15px;}

  .tb-title {font-size: 22px; font-weight: 600; color: #1F3A5F; margin: 6px 0 4px 0;}
  .tb-hero h1 {font-size: 30px; font-weight: 650; color: #1F3A5F; margin: 0 0 4px 0;}
  .tb-hero p {font-size: 15px; color: #555; margin: 0 0 6px 0;}
</style>
"""

# only on the workbook page: no page-level scrolling (the start page may scroll as usual)
def pane_css(top_px: int) -> str:
    """Both panes fill the window below the bar(s) above them."""
    return (f"<style>.st-key-left_pane {{height: calc(100vh - {top_px}px) !important; "
            f"max-height: calc(100vh - {top_px}px) !important;}} "
            f"div:has(> .st-key-chat_pane) {{height: calc(100vh - {top_px}px) !important;}}</style>")


WHOLE = "Whole workbook"                     # the first box: the full explanation
PANE_TOP = 124                               # app bar + margins above the panes (px)
DASHBAR_H = 68                               # the dashboard bar (38px) and the gaps around it

NO_PAGE_SCROLL = """
<style>
  [data-testid="stMain"], [data-testid="stAppViewContainer"] {overflow: hidden !important;}
  .block-container {padding-bottom: 0 !important;}
</style>
"""

ASSISTANT_NAME = "Insights Assistant"       # the chat's name on screen (one place to change it)

SUGGESTIONS = ["Explain this workbook: what is it for and what does each dashboard show?",
               "What are the key insights in the data?",
               "Is anything wrong in how this workbook is built?"]


# --------------------------------------------------------------------------------------------- loading


def _load(path: Path) -> None:
    _close_workbook()
    st.session_state.lw = load_workbook(path, settings=get_settings())
    st.session_state.chat, st.session_state.chat_log = None, []


def _close_workbook() -> None:
    """Release the open workbook and delete its local copy (unpacked files and data)."""
    old: LoadedWorkbook | None = st.session_state.get("lw")
    if old is not None:
        old.close(delete=True)
    st.session_state.lw = None
    st.session_state.loaded_digest = None


@st.cache_resource(show_spinner=False)
def _purge_leftovers_once() -> int:
    """At server start: delete workbook copies left by earlier runs (sessions live in memory only)."""
    return purge_workspaces(get_settings().workspace_dir)


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


# --------------------------------------------------------------------------------------------- pages


def _date_range(lw: LoadedWorkbook, prof) -> str | None:
    col = next((c for c in prof.columns if c.date_format and c.entity_level is not False), None)
    if col is None or lw.store is None:
        return None
    lo, hi = lw.store.con.execute(
        f"SELECT CAST(MIN(try_strptime({qi(col.name)}, ?)) AS DATE), CAST(MAX(try_strptime({qi(col.name)}, ?)) "
        f"AS DATE) FROM {lw.store.view(prof.table)}", [col.date_format.format, col.date_format.format]).fetchone()
    if lo is None:
        return None
    return f"{lo:%d %b %Y} to {hi:%d %b %Y} (by {col.name})"


def _report(lw: LoadedWorkbook):
    """The written explanation, built once per opened workbook (all local, no Claude call)."""
    key = f"report::{lw.source_name}::{id(lw)}"
    if key not in st.session_state:
        from tabbot.explain.report import explain_workbook
        try:
            st.session_state[key] = explain_workbook(lw)
        except Exception as e:  # noqa: BLE001 - never break the page over the explanation
            log.exception("explanation failed")
            st.session_state[key] = e
    return st.session_state[key]


def _render_workbook(lw: LoadedWorkbook) -> None:
    st.markdown(f"<div class='tb-title'>{lw.source_name}</div>", unsafe_allow_html=True)
    with st.spinner("Reading the workbook and writing the explanation…"):
        rep = _report(lw)
    if isinstance(rep, Exception):
        st.warning(f"The written explanation could not be built ({type(rep).__name__}). You can still ask the "
                   f"{ASSISTANT_NAME} on the right.")
        return
    st.caption("Written on this computer from the workbook and its data - no AI call was made for this text.")
    for sec in rep.sections:
        body = sec.body.replace("$", "\\$")
        if sec.key in ("brief", "about"):
            st.markdown(f"#### {sec.title}\n\n{body}")
        else:
            with st.expander(sec.title, expanded=sec.key in ("insights",)):
                st.markdown(body)


def _previews(lw: LoadedWorkbook, pages: list[str]) -> list[tuple[str, bytes]]:
    """The preview images Tableau saved in the workbook for these dashboards (matched by name)."""
    kit = lw.kit
    by_name = {t.name.strip(): t.png for t in (kit.thumbnails if kit is not None else []) if t.png}
    return [(n, by_name[n.strip()]) for n in pages if n.strip() in by_name]


def _png_width(png: bytes) -> int | None:
    """Pixel width from the PNG header (IHDR), or None."""
    if len(png) >= 24 and png[12:16] == b"IHDR":
        return int.from_bytes(png[16:20], "big") or None
    return None


def _render_dashboard(lw: LoadedWorkbook, view) -> None:
    st.markdown(f"<div class='tb-dash-title'>{html.escape(view.name)}</div>", unsafe_allow_html=True)
    sub = f"Dashboard in {lw.source_name}"
    if len(view.pages) > 1:
        sub += f" · split over {len(view.pages)} pages"
    st.markdown(f"<div class='tb-dash-sub'>{html.escape(sub)}</div>", unsafe_allow_html=True)
    imgs = _previews(lw, view.pages)
    if imgs:
        cols = st.columns(min(len(imgs), 3))
        for i, (name, png) in enumerate(imgs):
            with cols[i % len(cols)]:
                # shown at its real pixel size: stretching a small image only makes it blurry
                st.image(png, width=_png_width(png) or 192, caption=name.strip() if len(imgs) > 1 else None)
        st.caption("This is the small preview Tableau stores inside the workbook: it shows only the top-left part "
                   "of the dashboard, from the last time the workbook was saved.")
    else:
        st.caption("No preview image is saved in this workbook for this dashboard.")
    if view.summary:
        st.markdown(f"<div class='tb-summary'>{_md_inline(view.summary)}</div>", unsafe_allow_html=True)
    st.markdown("#### In depth")
    st.markdown(view.body.replace("$", "\\$"))


def _md_inline(text: str) -> str:
    """Summary text in an HTML box: escape it, keep **bold** and *italic*."""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return re.sub(r"\*(.+?)\*", r"<i>\1</i>", t)


def _selected_view(lw: LoadedWorkbook):
    rep = _report(lw)
    pick = st.session_state.get(_pick_key(lw))
    if isinstance(rep, Exception) or not pick or pick == WHOLE:
        return None
    return rep.dashboard(pick)


def _pick_key(lw: LoadedWorkbook) -> str:
    return f"dash_pick::{id(lw)}"


def _render_dashbar(lw: LoadedWorkbook) -> bool:
    """One box per dashboard (pages of one view share a box). Returns True when the bar is shown."""
    rep = _report(lw)
    if isinstance(rep, Exception) or not rep.dashboards:
        return False
    with st.container(key="dashbar"):
        st.pills("Dashboards", [WHOLE] + [v.name for v in rep.dashboards], selection_mode="single",
                 default=WHOLE, key=_pick_key(lw), label_visibility="collapsed")
    return True


def _render_start_page() -> None:
    st.write("")
    _, mid, _ = st.columns([1, 2.2, 1])
    with mid:
        st.markdown("<div class='tb-hero'><h1>Open a Tableau workbook</h1>"
                    "<p>Upload a packaged workbook to get a written explanation of it, then ask questions about it. "
                    "The workbook is read on this computer; only its structure and summary numbers are used to "
                    "answer questions.</p></div>", unsafe_allow_html=True)
        with st.container(border=True):
            up = st.file_uploader("Upload a packaged workbook (.twbx)", type=["twbx", "twb"], key="upload_main")
            if up is not None:
                _load_upload(up)
                st.rerun()


# --------------------------------------------------------------------------------------------- chat


def _chat_session(lw: LoadedWorkbook) -> ChatSession:
    if st.session_state.get("chat") is None:
        st.session_state.chat = ChatSession(lw, get_settings())
    return st.session_state.chat


def _usd(x: float, digits: int = 2) -> str:
    """Dollar amount for Streamlit markdown (a bare '$' would start a maths formula)."""
    return "\\$" + f"{x:,.{digits}f}"


def _context(lw: LoadedWorkbook, view) -> str:
    ctx = f"Open workbook: {lw.source_name}."
    if view is not None:
        ctx += (f" The user has selected the dashboard '{view.name}' (dashboard name(s) in the workbook: "
                + "; ".join(f"'{p}'" for p in view.pages) + "). Questions that say 'this dashboard' or 'here' are "
                "about it; describe_sheet accepts these dashboard names.")
    else:
        ctx += " The user is looking at the whole workbook (no dashboard selected)."
    return ctx


def _ask(lw: LoadedWorkbook, question: str, box, view=None) -> None:
    log = st.session_state.chat_log
    about = view.name if view is not None else WHOLE
    log.append({"role": "user", "text": question, "tools": [], "about": about})
    st.session_state.asked_about = about
    with box:
        with st.chat_message("user"):
            st.caption(f"About: {about}")
            st.markdown(question)
        with st.chat_message("assistant"), st.spinner("Looking at the workbook…"):
            turn = _chat_session(lw).ask(question, _context(lw, view))
    tools = [f"{'⚠️' if t.is_error else '🔧'} {t.name}({', '.join(f'{k}={v}' for k, v in t.input.items())})"
             + (f" → {t.preview}" if t.preview else "") for t in turn.tool_calls]
    meta = ""
    if turn.usage:
        u = turn.usage
        meta = (f"{turn.rounds} call(s) · in {u.get('input_tokens', 0):,} "
                f"(+{u.get('cache_read_input_tokens', 0):,} cached) · out {u.get('output_tokens', 0):,} "
                f"· ≈ {_usd(turn.est_usd, 3)}")
    log.append({"role": "assistant", "text": turn.text or "_(no answer)_", "tools": tools, "meta": meta})
    st.rerun()


def _render_chat(lw: LoadedWorkbook) -> None:
    settings = get_settings()
    with st.container(border=True, key="chat_pane"):
        view = _selected_view(lw)
        about = view.name if view is not None else WHOLE
        st.markdown(f"**💬 {ASSISTANT_NAME}**")
        st.caption(f"Asking about: **{about}**")
        ready = bool(settings.anthropic_api_key) or settings.dry_run
        if settings.dry_run:
            st.caption("Test mode: nothing is sent")
        if not ready:
            st.info("Add your Anthropic API key to start chatting:\n\n1. Copy `C:\\tabbot\\.env.example` to "
                    "`.env`\n2. Paste the key after `ANTHROPIC_API_KEY=`\n3. Restart the app")
        box = st.container(border=False, key="chat_msgs")
        with box:
            if not st.session_state.chat_log:
                st.caption(f"Ask anything about this {'dashboard' if view is not None else 'workbook'}. Only its "
                           "structure and summary numbers are used - "
                           "never the data rows.")
            for m in st.session_state.chat_log:
                with st.chat_message(m["role"]):
                    if m.get("about"):
                        st.caption(f"About: {m['about']}")
                    st.markdown(m["text"])
                    if m.get("tools"):
                        with st.expander(f"{len(m['tools'])} tool call(s)"):
                            for t in m["tools"]:
                                st.caption(t)
                    if m.get("meta"):
                        st.caption(m["meta"])
        pending = None
        # suggestions follow the selection; they come back whenever another dashboard is picked
        if ready and (not st.session_state.chat_log or st.session_state.get("asked_about") != about):
            rep = _report(lw)
            if view is not None:
                sugs = view.questions
            else:
                sugs = [SUGGESTIONS[0]] + (rep.questions[:3] if not isinstance(rep, Exception) else SUGGESTIONS[1:])
            for i, sug in enumerate(sugs):
                if st.button(sug, key=f"sug{i}::{about}", width="stretch"):
                    pending = sug
        q = st.chat_input("Ask about this dashboard…" if view is not None else "Ask about this workbook…",
                          key="chat_in", disabled=not ready)
        question = q or pending
        if question:
            _ask(lw, question, box, view)
        # every request is still written to workspace/audit/outbound.jsonl before it is sent
        if st.session_state.chat_log and st.button("Clear chat", key="clear_chat"):
            st.session_state.chat, st.session_state.chat_log = None, []
            st.session_state.asked_about = None
            st.rerun()


# --------------------------------------------------------------------------------------------- page


def main() -> None:
    st.set_page_config(page_title="Tableau Insights", page_icon="📊", layout="wide",
                       initial_sidebar_state="collapsed")
    st.markdown(CSS, unsafe_allow_html=True)
    _purge_leftovers_once()
    for k, v in (("chat", None), ("chat_log", [])):
        st.session_state.setdefault(k, v)

    auto = os.environ.get("TABBOT_AUTOLOAD")
    if auto and not st.session_state.get("autoloaded"):   # once per session, so "Close workbook" sticks
        st.session_state.autoloaded = True
        _load(Path(auto))
        st.session_state.loaded_digest = "autoload"

    lw: LoadedWorkbook | None = st.session_state.get("lw")
    with st.container(key="appbar"):
        if lw is None:
            st.markdown("<span class='brand'>📊 Tableau Insights</span>", unsafe_allow_html=True)
        else:
            top = st.columns([8, 1.8], vertical_alignment="center")
            with top[0]:
                st.markdown(f"<span class='brand'>📊 Tableau Insights</span>"
                            f"<span class='wb'>{lw.source_name}</span>", unsafe_allow_html=True)
            with top[1], st.popover("📂 Open workbook", width="stretch"):
                up = st.file_uploader("Tableau workbook (.twbx)", type=["twbx", "twb"], key="upload")
                if up is not None:
                    _load_upload(up)
                    st.rerun()
                if st.button("🗑 Close workbook & delete local copy", key="close_wb", width="stretch"):
                    _close_workbook()
                    st.session_state.chat, st.session_state.chat_log = None, []
                    st.rerun()

    lw = st.session_state.get("lw")
    if lw is None:
        _render_start_page()
        return

    st.markdown(NO_PAGE_SCROLL, unsafe_allow_html=True)
    with st.spinner("Reading the workbook and writing the explanation…"):
        _report(lw)
    has_bar = _render_dashbar(lw)
    st.markdown(pane_css(PANE_TOP + (DASHBAR_H if has_bar else 0)), unsafe_allow_html=True)
    left, right = st.columns([1.2, 1])
    with left, st.container(key="left_pane"):
        view = _selected_view(lw)
        if view is None:
            _render_workbook(lw)
        else:
            _render_dashboard(lw, view)
    with right:
        _render_chat(lw)


main()
