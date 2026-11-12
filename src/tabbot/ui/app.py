"""Tableau Insights (Streamlit). Run:  streamlit run src/tabbot/ui/app.py

Open a workbook, read a written explanation of it, ask the assistant about it. Past chats are listed on the left
(saved in workspace/chats/, on this computer) and can be read at any time; a chat continues, with its context,
when its workbook is open. Engine logic lives in tabbot.* ; this file only lays things out.

Session state keys:
  lw            LoadedWorkbook | None      chat       ChatSession | None (the conversation sent to Claude)
  chat_log      [{role, text, tools, meta, about}] (what the chat panel shows)
  saved         SavedChat | None (the current chat as stored)      viewing    id of a past chat shown read-only
  show_history  bool (left panel open)     upload_n   int (bumped after a load: gives the uploaders fresh keys)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import logging
import os
import re
import shutil
import time
from pathlib import Path

import streamlit as st

from tabbot.chat_store import DAY_GROUPS, ChatStore, SavedChat, day_group, start_chat
from tabbot.config import get_settings
from tabbot.library import WorkbookLibrary
from tabbot.llm.orchestrator import ChatSession
from tabbot.loader import LoadedWorkbook, load_workbook
from tabbot.ui.typewriter import Typewriter
from tabbot.workspace import purge_workspaces

log = logging.getLogger(__name__)

CSS = """
<style>
  /* remove Streamlit's own header strip: it overlapped the top of the page */
  header[data-testid="stHeader"] {display: none !important;}
  #MainMenu, footer {visibility: hidden;}
  .block-container {padding: 0.9rem 1.4rem 1rem 1.4rem !important; max-width: 100% !important;}
  html, body, [class*="css"] {font-family: 'Segoe UI', 'Benton Sans', Arial, sans-serif;}

  .st-key-appbar {background: #1F3A5F; border-radius: 8px; padding: 10px 14px 10px 10px; min-height: 52px;
                  justify-content: center;}
  .st-key-appbar p {color: #fff; margin: 0;}
  .st-key-appbar .brand {font-weight: 600; font-size: 18px; letter-spacing: .2px; color: #fff;}
  .st-key-appbar .wb {font-size: 13px; color: #C9D6E8; margin-left: 12px;}
  .st-key-appbar button {background: rgba(255,255,255,.10) !important; color: #fff !important;
                         border: 1px solid rgba(255,255,255,.35) !important;}
  .st-key-appbar button:hover {background: rgba(255,255,255,.22) !important;}
  .st-key-appbar button p {color: #fff !important;}
  .st-key-toggle_history button {border: none !important; background: transparent !important; font-size: 20px;
                                 padding: 0 6px !important; min-height: 0 !important;}
  .st-key-toggle_history button p {font-size: 20px !important;}

  /* past chats, on the left (like a chat app's sidebar) */
  .st-key-history_pane {background: #F7F8FA; border: 1px solid #E3E7ED; border-radius: 8px;
                        padding: 8px 8px; overflow-y: auto !important; overflow-x: hidden; flex-wrap: nowrap;
                        /* the whole page height (Streamlit overrides `height`; min = max fixes it) */
                        min-height: calc(100vh - 1.9rem) !important; max-height: calc(100vh - 1.9rem) !important;}
  .st-key-history_pane .st-key-toggle_history button p {color: #1F3A5F !important;}
  .st-key-history_pane .st-key-toggle_history {margin-bottom: 6px;}
  .st-key-history_pane [class*="st-key-open__"] button {justify-content: flex-start !important; text-align: left;
      border: none !important; background: transparent; padding: 5px 10px !important; min-height: 0;
      border-radius: 8px; color: #1F2937;}
  .st-key-history_pane [class*="st-key-open__"] button > div {justify-content: flex-start !important; width: 100%;}
  .st-key-history_pane [class*="st-key-open__"] button:hover {background: #E9EDF2 !important;}
  .st-key-history_pane [class*="st-key-open__"] button p {font-size: 14px; text-align: left; color: #1F2937;
      white-space: normal; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2;
      -webkit-box-orient: vertical; line-height: 1.3;}       /* long names wrap to a second line */
  .st-key-history_pane [class*="st-key-open__"] button[kind="primary"] {background: #DDE4EE !important;}
  .st-key-history_pane [class*="st-key-open__"] button[kind="primary"] p {color: #1F3A5F; font-weight: 600;}
  .st-key-new_chat button {border: 1px solid #C9D3E0 !important; background: #fff !important;}
  .st-key-history_pane {gap: 2px !important;}
  .st-key-history_pane .tb-recents {font-size: 12px; color: #6B7280; font-weight: 600; margin: 14px 0 0 8px;
                                    padding-bottom: 8px;}
  .st-key-history_pane [class*="st-key-del__"] button {border: none !important; background: transparent !important;
      color: #6B7280 !important; padding: 4px 2px !important; min-height: 0;}
  .st-key-history_pane [class*="st-key-del__"] button:hover {color: #B42318 !important;}
  .st-key-appbar [data-testid="stMarkdownContainer"] {display: flex; align-items: center; min-height: 34px;}

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
  /* questions: a grey bubble on the right; answers: plain text */
  [class*="st-key-umsg_"] {background: #F1F3F6; border-radius: 14px; padding: 8px 14px !important; gap: 2px !important;
                           align-self: flex-end; width: fit-content !important; max-width: 85%; margin: 6px 0;}
  [class*="st-key-umsg_"] p {margin: 0;}
  /* Streamlit gives text blocks a -16px bottom margin: inside the bubble that pushed the text out of the grey */
  [class*="st-key-umsg_"] [data-testid="stMarkdownContainer"] {margin-bottom: 0 !important;}
  [class*="st-key-amsg_"] {padding: 2px 2px 8px 2px;}
  /* while an answer streams: the stop symbol takes the place of the input's send arrow */
  .st-key-chat_pane {position: relative;}
  .st-key-stop_answer {position: absolute !important; right: 26px; bottom: 26px; z-index: 5; width: auto !important;}
  .st-key-stop_answer button {width: 32px; height: 32px; min-height: 32px !important; padding: 0 !important;
                              border-radius: 8px !important; background: #1F3A5F !important; border: none !important;}
  .st-key-stop_answer button span {color: #fff !important; margin: 0 !important;}
  .st-key-chat_pane:has(.st-key-stop_answer) [data-testid="stChatInputSubmitButton"] {visibility: hidden;}

  /* the dashboard selector: a searchable dropdown, fits any number of dashboards */
  .st-key-dashbar {padding: 2px 0 0 0;}
  .st-key-dashbar .tb-dash-label {font-size: 14px; font-weight: 600; color: #1F3A5F;}

  /* the header: workbook name and a one-line summary of its data */
  .st-key-appbar .wbmeta {font-size: 12px; color: #AFC0D6; margin-left: 10px;}

  /* chat list: the delete icon shows when the row is hovered (and asks before deleting) */
  .st-key-history_pane [class*="st-key-del__"] {opacity: 0; transition: opacity .12s;}
  .st-key-history_pane [data-testid="stHorizontalBlock"]:hover [class*="st-key-del__"] {opacity: 1;}
  .st-key-history_pane [class*="st-key-confirm__"] {background: #FFF4F2; border-radius: 8px; padding: 6px 8px;}

  /* suggested questions: small chips above the input */
  .st-key-sugs button {border-radius: 16px !important; padding: 3px 12px !important; min-height: 0 !important;
                       border: 1px solid #D4DAE3 !important; background: #fff !important;}
  .st-key-sugs button p {font-size: 13px !important; white-space: normal; text-align: left;}
  .st-key-sugs button:hover {border-color: #1F3A5F !important;}

  /* under each answer: copy and thumbs up/down */
  [class*="st-key-acts_"] {margin-top: -6px; opacity: .75;}
  [class*="st-key-acts_"]:hover {opacity: 1;}
  .tb-copy {border: none; background: transparent; cursor: pointer; font-size: 16px; color: #4B5563;
            padding: 2px 6px; border-radius: 6px;}
  .tb-copy:hover {background: #EEF1F5; color: #1F3A5F;}
  .st-key-chat_head {gap: 4px;}

  /* readable grey text (timestamps, notes, the input placeholder) */
  [data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {color: #4B5563 !important;}
  [data-testid="stChatInput"] textarea::placeholder {color: #6B7280 !important; opacity: 1 !important;}
  .st-key-history_pane .tb-recents {color: #4B5563 !important;}
  .tb-upload-note {max-width: 480px; margin: 8px auto 0 auto; text-align: center; font-size: 13px; color: #4B5563;}
  .tb-dash-title {font-size: 21px; font-weight: 600; color: #1F3A5F; margin: 4px 0 0 0;}
  .tb-dash-sub {font-size: 13px; color: #666; margin: 0 0 6px 0;}
  .tb-summary {background: #F3F6FA; border-left: 3px solid #1F3A5F; border-radius: 4px; padding: 10px 14px;
               margin: 6px 0 10px 0; font-size: 15px;}

  .tb-title {font-size: 22px; font-weight: 600; color: #1F3A5F; margin: 6px 0 4px 0;}
  .tb-hero {text-align: center; max-width: 860px; margin: 4px auto 10px auto;}   /* text on two lines */
  .tb-hero h1 {font-size: 30px; font-weight: 650; color: #1F3A5F; margin: 0 0 6px 0; padding: 0;}
  .tb-hero p {font-size: 15px; color: #555; margin: 0;}

  /* the upload box: a grey square with the upload button in the middle */
  [class*="st-key-upload_card"] {max-width: 480px; margin: 0 auto;}
  .st-key-same_wb {align-items: center;}
  .st-key-same_wb button p {color: #1F3A5F; text-decoration: underline;}
  [class*="st-key-upload_card"] [data-testid="stFileUploader"] label {justify-content: center; width: 100%;}
  [class*="st-key-upload_card"] [data-testid="stFileUploaderDropzone"] {
      aspect-ratio: 1 / 1; display: flex !important; flex-direction: column !important;
      justify-content: center !important; align-items: center !important; gap: 14px;
      background: #F1F3F6; border: 1.5px dashed #C3CBD6; border-radius: 14px; padding: 24px;}
  [class*="st-key-upload_card"] [data-testid="stFileUploaderDropzone"] > span {order: 0; flex: 0 0 auto !important;
      margin: 0 !important;}
  [class*="st-key-upload_card"] [data-testid="stFileUploaderDropzoneInstructions"] {order: 1; flex: 0 0 auto !important;
      margin: 0 !important; text-align: center;}
</style>
"""


def pane_css(top_px: int, chat_top_px: int | None = None) -> str:
    """The panes fill the window below the bar(s) above them (the chat pane may start higher)."""
    h = f"calc(100vh - {top_px}px)"
    hc = f"calc(100vh - {chat_top_px if chat_top_px is not None else top_px}px)"
    return (f"<style>.st-key-left_pane {{height: {h} !important; max-height: {h} !important;}} "
            f"div:has(> .st-key-chat_pane) {{height: {hc} !important;}}</style>")


WHOLE = "Whole workbook"                     # the first box: the full explanation
PANE_TOP = 124                               # app bar + margins above the panes (px)
DASHBAR_H = 62                               # the dashboard selector (40px) and the gaps around it

NO_PAGE_SCROLL = """
<style>
  [data-testid="stMain"], [data-testid="stAppViewContainer"] {overflow: hidden !important;}
  .block-container {padding-bottom: 0 !important;}
</style>
"""

# The chat messages scroll inside their own pane; keep it at the bottom while an answer streams in (and after it
# is drawn), unless the reader has scrolled up to read something - then leave them where they are.
FOLLOW_CHAT_JS = """
<script>
(function () {
  const w = window, doc = w.document;
  if (w.__tbFollowChat) return;
  w.__tbFollowChat = true;
  let queued = false;
  function follow() {
    queued = false;
    const el = doc.querySelector('.st-key-chat_msgs');
    if (!el) return;
    if (!el.dataset.tbBound) {
      el.dataset.tbBound = '1';
      el.addEventListener('scroll', () => {
        el.dataset.tbStick = (el.scrollHeight - el.scrollTop - el.clientHeight < 80) ? '1' : '0';
      });
    }
    if (el.dataset.tbStick !== '0') el.scrollTop = el.scrollHeight;
  }
  new w.MutationObserver(() => { if (!queued) { queued = true; w.requestAnimationFrame(follow); } })
    .observe(doc.body, {childList: true, subtree: true, characterData: true});
  follow();
})();
</script>
"""

# Streamlit's hover tooltips (e.g. a chat's first question in the chat list) can stay open after a click: the click
# redraws the list, the item that opened the tooltip is replaced, and nothing is left to close it. So: whenever the
# mouse is not over an item that has a tooltip, any open tooltip is hidden - for good: hovering an item opens a new
# tooltip of its own, so a hidden (stale) one is never shown again.
TOOLTIP_FIX_JS = """
<script>
(function () {
  const w = window, doc = w.document;
  if (w.__tbTipFix) return;
  w.__tbTipFix = true;
  let last = 0;
  function layerOf(el) {             // the tooltip's own layer: the element directly inside the page body
    while (el && el.parentElement && el.parentElement !== doc.body) el = el.parentElement;
    return el;
  }
  doc.addEventListener('pointermove', (e) => {
    const now = Date.now();
    if (now - last < 100) return;
    last = now;
    const over = e.target && e.target.closest ? e.target.closest('[data-testid="stTooltipHoverTarget"]') : null;
    if (over) return;
    doc.querySelectorAll('[data-testid="stTooltipContent"]').forEach((tip) => {
      const layer = layerOf(tip);
      if (layer && !layer.dataset.tbHidden) {
        layer.style.visibility = 'hidden';
        layer.dataset.tbHidden = '1';
      }
    });
  }, {passive: true});
  // copy button under an answer: puts the answer text on the clipboard
  doc.addEventListener('click', (e) => {
    const b = e.target && e.target.closest ? e.target.closest('.tb-copy') : null;
    if (!b) return;
    const text = b.getAttribute('data-copy') || '';
    const done = () => { b.textContent = '✓'; setTimeout(() => { b.textContent = '⧉'; }, 1500); };
    if (w.navigator.clipboard && w.isSecureContext) {
      w.navigator.clipboard.writeText(text).then(done, () => {});
    } else {
      const t = doc.createElement('textarea');
      t.value = text; doc.body.appendChild(t); t.select();
      try { doc.execCommand('copy'); done(); } catch (err) {}
      t.remove();
    }
  });
})();
</script>
"""

ASSISTANT_NAME = "Insights Assistant"       # the chat's name on screen (one place to change it)
HISTORY_W, MAIN_W, CHAT_W = 0.42, 1.25, 1   # column widths: past chats | explanation | chat
CHAT_W_ASKED = 1.5                          # the chat's width once questions are asked (answers need the room)

SUGGESTIONS = ["Explain this workbook: what is it for and what does each dashboard show?",
               "What are the key insights in the data?",
               "Is anything wrong in how this workbook is built?"]


def _store() -> ChatStore:
    return ChatStore(get_settings().workspace_dir / "chats")


def _library() -> WorkbookLibrary:
    return WorkbookLibrary(get_settings().workspace_dir / "library")


def _prune_library() -> None:
    """Keep a workbook copy only while a saved chat (or the open workbook) uses it."""
    lw: LoadedWorkbook | None = st.session_state.get("lw")
    keep = {c.digest for c in _store().list()} | ({lw.digest} if lw is not None else set())
    _library().prune(keep)


# --------------------------------------------------------------------------------------------- chats


def _new_chat() -> None:
    """Start an empty chat (the previous one is already saved)."""
    st.session_state.chat, st.session_state.chat_log, st.session_state.saved = None, [], None
    st.session_state.viewing, st.session_state.asked_about = None, None


def _open_chat(c: SavedChat) -> None:
    """Show a past chat. On its own workbook it continues, with its context. If another workbook (or none) is open,
    its workbook is reopened from the kept copy; without a copy the chat is shown read-only."""
    st.session_state.home = False
    lw: LoadedWorkbook | None = st.session_state.get("lw")
    if (lw is None or lw.digest != c.digest) and get_settings().keep_workbook_copies:
        copy = _library().get(c.digest)
        if copy is not None:
            st.session_state.viewing = c.id           # _load continues this chat once the workbook is open
            try:
                with st.spinner(f"Opening {c.workbook}…"):
                    _load(copy)
                st.session_state.loaded_digest = c.digest[:16]   # re-uploading the same file changes nothing
                return
            except Exception as exc:  # noqa: BLE001 - fall back to reading the chat
                log.exception("kept workbook copy could not be opened")
                st.session_state.viewing = c.id
                st.error(f"The kept copy of {c.workbook} could not be opened ({type(exc).__name__}).")
                return
        lw = st.session_state.get("lw")
    if lw is not None and lw.digest == c.digest:
        session = ChatSession(lw, get_settings())
        session.restore(c.messages)
        st.session_state.chat, st.session_state.chat_log, st.session_state.saved = session, list(c.log), c
        st.session_state.viewing = None
        st.session_state.asked_about = next((m.get("about") for m in reversed(c.log) if m.get("about")), None)
    else:
        st.session_state.viewing = c.id


def _save_chat(lw: LoadedWorkbook) -> None:
    """Store the current chat after each answer (a new chat is created on its first question)."""
    try:
        saved: SavedChat | None = st.session_state.get("saved")
        if saved is None or saved.digest != getattr(lw, "digest", ""):
            saved = start_chat(lw)
        chat: ChatSession | None = st.session_state.get("chat")
        saved.log = list(st.session_state.chat_log)
        saved.messages = list(chat.messages) if chat is not None else []
        rep = st.session_state.get(f"report::{lw.workspace.id}")
        if rep is not None and hasattr(rep, "to_dict"):
            saved.report = rep.to_dict()
        st.session_state.saved = _store().save(saved)
    except Exception:  # noqa: BLE001 - a chat that cannot be saved must never break the answer
        log.exception("chat could not be saved")


def _when(iso: str) -> str:
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{t:%d %b %H:%M}" if t.date() == dt.date.today() or t.year == dt.date.today().year else f"{t:%d %b %Y}"


def _toggle_button() -> None:
    """☰: hides the chat list (shown at the top of the list) or brings it back (shown in the app bar)."""
    if st.button("☰", key="toggle_history",
                 help=("Hide" if st.session_state.show_history else "Show") + " past chats"):
        st.session_state.show_history = not st.session_state.show_history
        st.rerun()


def _short_when(iso: str) -> str:
    """'14:05' for today, '12 Nov' otherwise: tells apart chats of one workbook without crowding the list."""
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return ""
    return f"{t:%H:%M}" if t.date() == dt.date.today() else f"{t:%d %b}"


def _render_history() -> None:
    chats = _store().list()
    saved: SavedChat | None = st.session_state.get("saved")
    current = st.session_state.get("viewing") or (saved.id if saved is not None else None)
    names = [c.workbook for c in chats]
    with st.container(key="history_pane"):
        _toggle_button()
        if st.button("＋ New chat", key="new_chat", width="stretch"):
            _new_chat()
            st.session_state.home = True      # back to the start page: open another workbook, or the same one
            st.rerun()
        if not chats:
            st.markdown("<div class='tb-recents'>Recents</div>", unsafe_allow_html=True)
            st.caption("Your chats will appear here.")
        groups: dict[str, list[SavedChat]] = {}
        for c in chats:                                  # newest first, grouped by when they were last used
            groups.setdefault(day_group(c.updated), []).append(c)
        for group in DAY_GROUPS:
            if group not in groups:
                continue
            st.markdown(f"<div class='tb-recents'>{group}</div>", unsafe_allow_html=True)
            for c in groups[group]:
                _history_row(c, current, names)


def _history_row(c: SavedChat, current: str | None, names: list[str]) -> None:
    """One chat in the list: its name (first question on hover); 🗑 shows on hover and asks before deleting."""
    if st.session_state.get("confirm_del") == c.id:
        with st.container(key=f"confirm__{c.id}"):
            st.caption(f"Delete this chat about {c.workbook}?")
            row = st.columns(2, gap="small")
            if row[0].button("Delete", key=f"del_yes__{c.id}", type="primary", width="stretch"):
                st.session_state.confirm_del = None
                _store().delete(c.id)
                if c.id == current:
                    _new_chat()
                _prune_library()                         # its workbook copy goes when no chat needs it
                st.rerun()
            if row[1].button("Cancel", key=f"del_no__{c.id}", width="stretch"):
                st.session_state.confirm_del = None
                st.rerun()
        return
    label = c.workbook + (f" · {_short_when(c.created)}" if names.count(c.workbook) > 1 else "")
    tip = (f"{c.first_question[:120]}\n\n{_when(c.updated)} · {c.questions} question(s)"
           if c.first_question else _when(c.updated))
    row = st.columns([6, 1], gap=None, vertical_alignment="center")
    with row[0]:
        if st.button(label, key=f"open__{c.id}", width="stretch", help=tip,
                     type="primary" if c.id == current else "tertiary"):
            _open_chat(c)
            st.rerun()
    with row[1]:
        if st.button("🗑", key=f"del__{c.id}", help="Delete this chat", type="tertiary"):
            st.session_state.confirm_del = c.id
            st.rerun()


def _user_bubble(i: int, text: str, about: str | None = None) -> None:
    """A question: a grey bubble on the right, the question only (the dashboard it was asked about is still sent
    to the assistant and saved with the chat, just not shown)."""
    with st.container(key=f"umsg_{i}"):
        st.markdown(_md_text(text))


def _md_text(text: str) -> str:
    """Chat text for st.markdown: '$' would start a maths formula ('$0.08 … $0.16' turned into code), so escape it."""
    return text.replace("\\$", "$").replace("$", "\\$")


def _render_messages(entries: list[dict], actions: bool = False) -> None:
    """The conversation. actions=True (the live chat): copy and thumbs up/down under each answer."""
    for i, m in enumerate(entries):
        if m["role"] == "user":
            _user_bubble(i, m["text"], m.get("about"))
            continue
        with st.container(key=f"amsg_{i}"):    # an answer: plain text (tool calls and cost stay in the logs only)
            st.markdown(_md_text(m["text"]))
            if actions:
                _answer_actions(i, m)


def _answer_actions(i: int, m: dict) -> None:
    saved: SavedChat | None = st.session_state.get("saved")
    key = f"fb__{saved.id if saved is not None else 'new'}__{i}"
    with st.container(key=f"acts_{i}", horizontal=True, vertical_alignment="center", gap="small"):
        # copy: a plain button the page script handles (the text sits in an escaped attribute, never as markup)
        st.html(f"<button class='tb-copy' title='Copy the answer' data-copy='{html.escape(m['text'], quote=True)}'>"
                "⧉</button>", width="content")
        st.feedback("thumbs", key=key, default={"up": 1, "down": 0}.get(m.get("feedback")),
                    on_change=_set_feedback, args=(i, key))


def _set_feedback(i: int, key: str) -> None:
    """Thumbs up/down: kept with the answer in the saved chat (for testing how good answers are)."""
    entries = st.session_state.get("chat_log") or []
    if 0 <= i < len(entries):
        v = st.session_state.get(key)
        entries[i]["feedback"] = None if v is None else ("up" if v == 1 else "down")
        lw = st.session_state.get("lw")
        if lw is not None:
            _save_chat(lw)


def _render_past_chat(c: SavedChat, lw: LoadedWorkbook | None) -> None:
    """A past chat whose workbook is not open, laid out like an open workbook: the explanation saved with the chat
    in the middle, the conversation on the right. Opening the same workbook again continues the chat."""
    from tabbot.explain.report import Report

    rep = None
    if c.report:
        try:
            rep = Report.from_dict(c.report)
        except (TypeError, KeyError):
            rep = None
    st.markdown(NO_PAGE_SCROLL, unsafe_allow_html=True)
    left, right = st.columns([MAIN_W, CHAT_W])
    with left:
        key = f"dash_pick::past::{c.id}"
        has_bar = _render_dashbar_for(rep, key)
        with st.container(key="left_pane"):
            view = _selected_from(rep, key)
            if rep is None:
                st.markdown(f"<div class='tb-title'>{html.escape(c.workbook)}</div>", unsafe_allow_html=True)
                st.info("The explanation was not saved with this older chat. Open the workbook to see it.")
            elif view is not None:
                _render_dashboard(view, c.workbook, None)
            else:
                _render_report_text(rep, c.workbook,
                                    note=f"Saved with this chat on {_when(c.updated)}. Written on this computer "
                                         "from the workbook and its data - no AI call was made for this text.")
    st.markdown(pane_css(PANE_TOP + (DASHBAR_H if has_bar else 0), PANE_TOP), unsafe_allow_html=True)
    with right, st.container(border=True, key="chat_pane"):
        st.markdown(f"**{ASSISTANT_NAME}**")
        st.caption(f"Chat from {_when(c.created)} · {c.questions} question(s)")
        with st.container(border=False, key="chat_msgs"):
            _render_messages(c.log)
        st.info(f"To continue this chat, open **{c.workbook}** again (the same file).")
        up = st.file_uploader(f"Open {c.workbook}", type=["twbx", "twb"], key=f"upload_view_{_upload_n()}",
                              label_visibility="collapsed")
        if up is not None and _load_upload(up):
            st.rerun()
        if lw is not None and st.button(f"Back to {Path(lw.source_name).stem}", key="back_to_open"):
            st.session_state.viewing = None
            st.rerun()


# --------------------------------------------------------------------------------------------- loading


def _upload_n() -> int:
    return st.session_state.get("upload_n", 0)


def _load(path: Path) -> None:
    _close_workbook()
    lw = load_workbook(path, settings=get_settings())
    st.session_state.lw = lw
    st.session_state.home = False
    if get_settings().keep_workbook_copies:
        try:
            _library().put(path, lw.digest)              # so its chats can reopen it later
        except OSError:
            log.exception("workbook copy could not be kept")
    st.session_state.upload_n = _upload_n() + 1          # fresh, empty upload boxes: the file is not re-read
    wanted = st.session_state.get("viewing")
    waiting = _store().load(wanted) if wanted else None
    if waiting is not None and waiting.digest == lw.digest:
        _open_chat(waiting)                             # the workbook a past chat needs: continue that chat
    else:
        _new_chat()


def _close_workbook() -> None:
    """Release the open workbook and delete its local copy (unpacked files and data)."""
    old: LoadedWorkbook | None = st.session_state.get("lw")
    if old is not None:
        old.close(delete=True)
        for k in [k for k in st.session_state if str(k).endswith(f"::{old.workspace.id}")]:
            del st.session_state[k]                     # its explanation and dashboard choice
    st.session_state.lw = None
    st.session_state.loaded_digest = None


@st.cache_resource(show_spinner=False)
def _purge_leftovers_once() -> int:
    """At server start: delete unpacked workbooks left by earlier runs (sessions live in memory only) and kept
    workbook copies that no saved chat uses any more."""
    s = get_settings()
    WorkbookLibrary(s.workspace_dir / "library").prune({c.digest for c in ChatStore(s.workspace_dir / "chats").list()})
    return purge_workspaces(s.workspace_dir)


def _load_upload(uploaded) -> bool:
    """Open an uploaded workbook. True when a new workbook was opened (the caller then reruns the page); False when
    this file is already open or failed - so a file left in an upload box never makes the page rerun forever."""
    data = uploaded.getvalue()
    digest = hashlib.sha256(data).hexdigest()[:16]
    if digest == st.session_state.get("loaded_digest") and st.session_state.get("home") \
            and st.session_state.get("lw") is not None:
        st.session_state.home = False         # the open workbook chosen again from the start page: a new chat on it
        st.session_state.upload_n = _upload_n() + 1
        return True
    if digest in (st.session_state.get("loaded_digest"), st.session_state.get("failed_digest")):
        return False
    incoming = get_settings().workspace_dir / "_incoming" / digest
    incoming.mkdir(parents=True, exist_ok=True)
    path = incoming / Path(uploaded.name).name
    path.write_bytes(data)
    try:
        with st.spinner("Reading workbook and extract…"):
            _load(path)
        st.session_state.loaded_digest = digest
        st.session_state.failed_digest = None
        return True
    except Exception as exc:  # noqa: BLE001 - show the problem, keep the app alive
        st.session_state.failed_digest = digest
        st.error(f"Could not open {uploaded.name}: {exc}")
        return False
    finally:
        shutil.rmtree(incoming, ignore_errors=True)


# --------------------------------------------------------------------------------------------- pages


def _report(lw: LoadedWorkbook):
    """The written explanation, built once per opened workbook (all local, no Claude call)."""
    key = f"report::{lw.workspace.id}"
    if key not in st.session_state:
        from tabbot.explain.report import explain_workbook
        try:
            st.session_state[key] = explain_workbook(lw)
        except Exception as e:  # noqa: BLE001 - never break the page over the explanation
            log.exception("explanation failed")
            st.session_state[key] = e
    return st.session_state[key]


def _render_workbook(lw: LoadedWorkbook) -> None:
    with st.spinner("Reading the workbook and writing the explanation…"):
        rep = _report(lw)
    if isinstance(rep, Exception):
        st.markdown(f"<div class='tb-title'>{html.escape(lw.source_name)}</div>", unsafe_allow_html=True)
        st.warning(f"The written explanation could not be built ({type(rep).__name__}). You can still ask the "
                   f"{ASSISTANT_NAME} on the right.")
        return
    _render_report_text(rep, _name(lw))


def _name(lw: LoadedWorkbook) -> str:
    """The workbook's name as people say it: the file name without .twbx / .twb."""
    return Path(lw.source_name).stem


def _meta(lw: LoadedWorkbook) -> str:
    """'334 rows · Jan 2017 – Nov 2019' (computed once per opened workbook), or why there are no numbers."""
    key = f"meta::{lw.workspace.id}"
    if key not in st.session_state:
        text = "structure only - no data in the file"
        if lw.store is not None:
            try:
                from tabbot.explain.facts import basics
                b = basics(lw)
                if b is not None:            # the same count the explanation uses: records when it counts them
                    text = f"{b.entities:,} records" if b.entity else f"{b.rows:,} rows"
                    if b.date_from and b.date_to:
                        text += f" · {b.date_from:%b %Y} – {b.date_to:%b %Y}"
            except Exception:  # noqa: BLE001 - a header line must never break the page
                log.exception("workbook summary line failed")
        st.session_state[key] = text
    return st.session_state[key]


def _render_report_text(rep, title: str, note: str | None = None) -> None:
    """The written explanation (live, or the snapshot saved with a chat)."""
    st.markdown(f"<div class='tb-title'>{html.escape(title)}</div>", unsafe_allow_html=True)
    st.caption(note or "Written on this computer from the workbook and its data - no AI call was made for this "
                       "text.")
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


def _render_dashboard(view, source_name: str, imgs: list[tuple[str, bytes]] | None) -> None:
    """One dashboard's explanation. imgs=None: the workbook is not open, so its preview images are not at hand."""
    st.markdown(f"<div class='tb-dash-title'>{html.escape(view.name)}</div>", unsafe_allow_html=True)
    sub = f"Dashboard in {source_name}"
    if len(view.pages) > 1:
        sub += f" · split over {len(view.pages)} pages"
    st.markdown(f"<div class='tb-dash-sub'>{html.escape(sub)}</div>", unsafe_allow_html=True)
    if imgs is None:
        st.caption("Tableau's preview image is shown when the workbook is open.")
    elif imgs:
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
    return _selected_from(_report(lw), _pick_key(lw))


def _selected_from(rep, key: str):
    pick = st.session_state.get(key)
    if rep is None or isinstance(rep, Exception) or not pick or pick == WHOLE:
        return None
    return rep.dashboard(pick)


def _pick_key(lw: LoadedWorkbook) -> str:
    return f"dash_pick::{lw.workspace.id}"


def _render_dashbar(lw: LoadedWorkbook) -> bool:
    return _render_dashbar_for(_report(lw), _pick_key(lw))


def _render_dashbar_for(rep, key: str) -> bool:
    """A searchable dropdown of the dashboards (pages of one view share an entry) - it fits any number of them,
    however long their names. Returns True when it is shown."""
    if rep is None or isinstance(rep, Exception) or not rep.dashboards:
        return False
    names = [WHOLE] + [v.name for v in rep.dashboards]
    if st.session_state.get(key) not in names:
        st.session_state[key] = WHOLE
    n = len(rep.dashboards)
    with st.container(key="dashbar", horizontal=True, vertical_alignment="center", gap="small"):
        st.markdown("<span class='tb-dash-label'>Dashboard</span>", unsafe_allow_html=True, width="content")
        st.selectbox("Dashboard", names, key=key, label_visibility="collapsed", width=460,
                     help="Type to search the dashboards")
        st.caption(f"{n} dashboard{'s' if n != 1 else ''}", width="content")
    return True


def _render_start_page() -> None:
    st.markdown("<div class='tb-hero'><h1>Open a Tableau workbook</h1>"
                "<p>Upload a packaged workbook to get a written explanation of it, then ask questions about it. "
                "The workbook is read on this computer; only its structure and summary numbers are used to "
                "answer questions.</p></div>", unsafe_allow_html=True)
    with st.container(key=f"upload_card_{_upload_n()}"):
        up = st.file_uploader("Upload a workbook (.twbx or .twb)", type=["twbx", "twb"],
                              key=f"upload_main_{_upload_n()}")
        if up is not None and _load_upload(up):
            st.rerun()
        st.markdown("<div class='tb-upload-note'>A packaged <b>.twbx</b> includes its data, so you can ask about "
                    "numbers. A <b>.twb</b> holds only the design: the assistant can explain its charts, fields and "
                    "filters, but not give numbers.</div>", unsafe_allow_html=True)
    lw: LoadedWorkbook | None = st.session_state.get("lw")
    if lw is not None:                # "New chat" from an open workbook: a fresh chat on it is one click away
        with st.container(key="same_wb"):
            if st.button(f"or start a new chat on {Path(lw.source_name).stem}", key="chat_on_open",
                         type="tertiary"):
                st.session_state.home = False
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


def _tool_label(call: dict) -> str:
    """What the assistant is doing, in plain words, while a tool runs."""
    inp = call.get("input") or {}
    sheet = f" '{inp['sheet']}'" if inp.get("sheet") else ""
    return {"list_dashboards": "Reading the workbook overview",
            "describe_sheet": f"Looking at{sheet or ' a sheet'}",
            "get_chart_data": f"Reading the numbers behind{sheet or ' a chart'}",
            "run_analysis": f"Analysing{sheet or ' the data'}",
            "get_findings": "Checking how the workbook is built",
            "query_data": "Querying the data"}.get(call.get("name"), "Working")


def _ask(lw: LoadedWorkbook, question: str, box, view=None, stop_slot=None) -> None:
    entries = st.session_state.chat_log
    about = view.name if view is not None else WHOLE
    entries.append({"role": "user", "text": question, "tools": [], "about": about})
    st.session_state.asked_about = about
    # if the page reruns before the answer is complete (Stop pressed), the next run finds this and records what
    # was written so far (see _finish_stopped)
    st.session_state.pending_answer = {"text": ""}
    with box:
        _user_bubble(len(entries) - 1, question, about)
        with st.container(key=f"amsg_{len(entries)}"):
            status, body = st.empty(), st.empty()
            status.caption("Thinking…")
            # any click reruns the page, and that interrupts the answer; the button sits over the input's arrow
            (stop_slot or st).button("", icon=":material/stop:", key="stop_answer", help="Stop answering")
            tw = Typewriter(lambda t: body.markdown(_md_text(t) + " ▌") if t else body.empty())
            now = {"status": "Thinking…", "tick": 0.0}

            def on_event(kind: str, data) -> None:
                if kind == "tool":                  # text before a tool call was a progress note: replace it
                    tw.reset()
                    st.session_state.pending_answer["text"] = ""
                    now["status"] = f"{_tool_label(data)}…"
                    status.caption(now["status"])
                elif kind == "text":
                    if not tw.shown:
                        status.empty()
                    tw.feed(data)
                    st.session_state.pending_answer["text"] = tw.shown
                elif kind == "tick" and not tw.shown and time.monotonic() - now["tick"] > 0.4:
                    now["tick"] = time.monotonic()   # a screen update: lets a Stop click interrupt the wait
                    status.caption(now["status"])

            turn = _chat_session(lw).ask(question, _context(lw, view), on_event=on_event)
    st.session_state.pending_answer = None
    tools = [f"{'⚠️' if t.is_error else '🔧'} {t.name}({', '.join(f'{k}={v}' for k, v in t.input.items())})"
             + (f" → {t.preview}" if t.preview else "") for t in turn.tool_calls]
    meta = ""
    if turn.usage:
        u = turn.usage
        meta = (f"{turn.rounds} call(s) · in {u.get('input_tokens', 0):,} "
                f"(+{u.get('cache_read_input_tokens', 0):,} cached) · out {u.get('output_tokens', 0):,} "
                f"· ≈ {_usd(turn.est_usd, 3)}")
    entries.append({"role": "assistant", "text": turn.text or "_(no answer)_", "tools": tools, "meta": meta})
    _save_chat(lw)
    st.rerun()


def _finish_stopped(lw: LoadedWorkbook) -> None:
    """The previous run was interrupted while an answer was being written (Stop): keep what was written, marked."""
    p = st.session_state.get("pending_answer")
    if not p:
        return
    st.session_state.pending_answer = None
    text = (p.get("text") or "").strip()
    st.session_state.chat_log.append({"role": "assistant", "tools": [], "meta": "",
                                      "text": (text + "\n\n" if text else "") + "_Stopped._"})
    _save_chat(lw)


def _render_chat(lw: LoadedWorkbook) -> None:
    settings = get_settings()
    with st.container(border=True, key="chat_pane"):
        view = _selected_view(lw)
        about = view.name if view is not None else WHOLE
        fresh = not st.session_state.chat_log
        with st.container(key="chat_head", horizontal=True, vertical_alignment="center"):
            st.markdown(f"**{ASSISTANT_NAME}**", width="stretch")
            wide = bool(st.session_state.get("wide_chat"))
            if st.button("", icon=":material/close_fullscreen:" if wide else ":material/open_in_full:",
                         key="wide_chat_btn", type="tertiary",
                         help="Show the explanation again" if wide else "Wider chat (hide the explanation)"):
                st.session_state.wide_chat = not wide
                st.rerun()
        head_slot = st.empty()       # only before the first question: then the conversation has the room
        if fresh:
            head_slot.caption(f"Asking about: **{about}**")
        if lw.store is None or not lw.ds_tables:
            st.warning(("A .twb file holds only the design, never the data. " if lw.source_name.lower().endswith(".twb")
                        else "The data file is not inside this workbook. ")
                       + "Answers can explain the charts, fields and filters, but not give numbers. "
                       "For numbers, upload the packaged workbook (.twbx) - in Tableau: File → Export Packaged "
                       "Workbook.", icon="ℹ️")
        ready = bool(settings.anthropic_api_key) or settings.dry_run
        if settings.dry_run:
            st.caption("Test mode: nothing is sent")
        if not ready:
            st.info("Add your Anthropic API key to start chatting:\n\n1. Copy `C:\\tabbot\\.env.example` to "
                    "`.env`\n2. Paste the key after `ANTHROPIC_API_KEY=`\n3. Restart the app")
        box = st.container(border=False, key="chat_msgs")
        with box:
            intro_slot = st.empty()
            if fresh:
                intro_slot.caption(f"Ask anything about this {'dashboard' if view is not None else 'workbook'}. Only "
                                   "its structure and summary numbers are used - never the data rows.")
            _render_messages(st.session_state.chat_log, actions=True)
        pending = None
        sug_slot = st.empty()        # emptied while an answer streams, so the answer has the room
        # suggestions follow the selection; they come back whenever another dashboard is picked
        if ready and (not st.session_state.chat_log or st.session_state.get("asked_about") != about):
            rep = _report(lw)
            if view is not None:
                sugs = view.questions
            else:
                sugs = [SUGGESTIONS[0]] + (rep.questions[:3] if not isinstance(rep, Exception) else SUGGESTIONS[1:])
            # two or three compact chips just above the input (not full-width buttons competing with answers)
            with sug_slot.container(key="sugs", horizontal=True, gap="small"):
                for i, sug in enumerate(sugs[:3]):
                    if st.button(sug, key=f"sug{i}::{about}", width="content"):
                        pending = sug
        q = st.chat_input("Ask about this dashboard…" if view is not None else "Ask about this workbook…",
                          key="chat_in", disabled=not ready)
        stop_slot = st.empty()       # while an answer streams: a stop symbol in place of the input's arrow
        question = q or pending
        if question:
            sug_slot.empty()
            head_slot.empty()        # the question is asked: the intro lines go at once, not after the answer
            intro_slot.empty()
            _ask(lw, question, box, view, stop_slot)
        # every request is still written to workspace/audit/outbound.jsonl before it is sent


# --------------------------------------------------------------------------------------------- page


def _render_appbar(lw: LoadedWorkbook | None) -> None:
    """The bar above the page. When the chat list is hidden, its ☰ button lives here."""
    with st.container(key="appbar"):
        hidden = not st.session_state.show_history
        top = st.columns([0.35, 7.6, 1.8] if hidden else [7.95, 1.8], vertical_alignment="center")
        if hidden:
            with top[0]:
                _toggle_button()
            top = top[1:]
        with top[0]:
            wb = (f"<span class='wb'>{html.escape(_name(lw))}</span>"
                  f"<span class='wbmeta'>{html.escape(_meta(lw))}</span>") if lw is not None else ""
            st.markdown(f"<span class='brand'>📊 Tableau Insights</span>{wb}", unsafe_allow_html=True)
        if lw is not None:
            # a fresh key after each load: the popover comes back closed instead of covering the new workbook
            with top[1], st.popover("📂 Open workbook", width="stretch", key=f"open_wb_{_upload_n()}"):
                up = st.file_uploader("Tableau workbook (.twbx or .twb)", type=["twbx", "twb"],
                                      key=f"upload_{_upload_n()}")
                if up is not None and _load_upload(up):
                    st.rerun()
                if st.button("Close workbook", key="close_wb", width="stretch",
                             help="Closes it and deletes its unpacked files; a copy stays only if a saved chat "
                                  "uses it"):
                    _close_workbook()
                    _new_chat()
                    _prune_library()
                    st.rerun()


def main() -> None:
    st.set_page_config(page_title="Tableau Insights", page_icon="📊", layout="wide",
                       initial_sidebar_state="collapsed")
    st.markdown(CSS, unsafe_allow_html=True)
    _purge_leftovers_once()
    for k, v in (("chat", None), ("chat_log", []), ("saved", None), ("viewing", None), ("show_history", True)):
        st.session_state.setdefault(k, v)

    auto = os.environ.get("TABBOT_AUTOLOAD")
    if auto and not st.session_state.get("autoloaded"):   # once per session, so "Close workbook" sticks
        st.session_state.autoloaded = True
        _load(Path(auto))
        st.session_state.loaded_digest = "autoload"

    if st.session_state.get("lw") is not None:
        _finish_stopped(st.session_state.lw)          # before the chat list is drawn, so it is listed right away
    # two areas, like a chat app: the chat list down the whole left side (when shown), everything else to its
    # right - the app bar starts after the list and stretches to the left edge when the list is hidden
    if st.session_state.show_history:
        side, body = st.columns([HISTORY_W, MAIN_W + CHAT_W], gap="small")
        with side:
            _render_history()
    else:
        body = st.container()
    with body:
        _render_body()
    # fixed scripts written here (never workbook or user text), run in the page itself
    st.html(TOOLTIP_FIX_JS, unsafe_allow_javascript=True)


def _render_body() -> None:
    home = bool(st.session_state.get("home"))              # "New chat" pressed: the start page
    _render_appbar(None if home else st.session_state.get("lw"))
    lw: LoadedWorkbook | None = st.session_state.get("lw")
    viewing = _store().load(st.session_state.viewing) if st.session_state.viewing else None
    if st.session_state.viewing and viewing is None:      # deleted meanwhile
        st.session_state.viewing = None

    if viewing is not None:                                # a past chat, its workbook not open
        _render_past_chat(viewing, lw)
        return
    if lw is None or home:
        st.markdown(pane_css(PANE_TOP), unsafe_allow_html=True)
        _render_start_page()
        return

    st.markdown(NO_PAGE_SCROLL, unsafe_allow_html=True)
    with st.spinner("Reading the workbook and writing the explanation…"):
        _report(lw)
    # panel 2 = dashboard selector + explanation; panel 3 = the assistant, starting right under the app bar.
    # Once questions are asked the chat gets more room; "wider chat" hides the explanation altogether.
    if st.session_state.get("wide_chat"):
        st.markdown(pane_css(PANE_TOP, PANE_TOP), unsafe_allow_html=True)
        _render_chat(lw)
    else:
        asked = bool(st.session_state.chat_log)
        left, right = st.columns([MAIN_W, CHAT_W_ASKED if asked else CHAT_W])
        with left:
            has_bar = _render_dashbar(lw)
            with st.container(key="left_pane"):
                view = _selected_view(lw)
                if view is None:
                    _render_workbook(lw)
                else:
                    _render_dashboard(view, _name(lw), _previews(lw, view.pages))
        st.markdown(pane_css(PANE_TOP + (DASHBAR_H if has_bar else 0), PANE_TOP), unsafe_allow_html=True)
        with right:
            _render_chat(lw)
    # a fixed script written here (never workbook or user text), run in the page itself
    st.html(FOLLOW_CHAT_JS, unsafe_allow_javascript=True)


main()
