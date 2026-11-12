"""Show streamed text at an even, readable pace (like Claude's own chat), not in network-sized bursts.

The API delivers text in uneven chunks: sometimes a few characters, sometimes a whole sentence at once. The
typewriter releases it word by word at about BASE_CPS characters per second. When text arrives faster than that,
it speeds up (gently, in proportion to the backlog) so the display never falls far behind the answer.

Pure Python: `render(text_so_far)` and `sleep(seconds)` are passed in, so it is tested without Streamlit.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

BASE_CPS = 180            # medium reading pace, characters per second (~35 words/s)
CATCH_UP_CHARS = 300      # each this many characters waiting adds one more BASE_CPS of speed
MIN_RENDER_GAP = 0.03     # seconds: don't redraw more often than this (a redraw re-renders the markdown)
WORD = re.compile(r"\S+\s*|\s+")


class Typewriter:
    def __init__(self, render: Callable[[str], None], sleep: Callable[[float], None] = time.sleep,
                 cps: float = BASE_CPS):
        self.render, self.sleep, self.cps = render, sleep, cps
        self.shown = ""
        self.pending = ""

    def feed(self, text: str) -> None:
        """Add newly arrived text and type it out (returns when it is all shown)."""
        self.pending += text
        while self.pending:
            step, waited = "", 0.0
            # take whole words until one redraw's worth of time has passed
            while self.pending and waited < MIN_RENDER_GAP:
                m = WORD.match(self.pending)
                word = m.group(0) if m else self.pending
                self.pending = self.pending[len(word):]
                step += word
                waited += len(word) / (self.cps * (1 + (len(self.pending) / CATCH_UP_CHARS)))
            self.shown += step
            self.render(self.shown)
            self.sleep(waited)

    def reset(self) -> None:
        """Forget what was shown (e.g. a progress note before a tool call) and start a fresh answer."""
        self.shown, self.pending = "", ""
        self.render("")
