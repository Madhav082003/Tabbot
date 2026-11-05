"""Parse a .twb / .twbx into the engine model.

The XML is read by the parser kit (tabbot.tparse, tested on 60 public workbooks) and converted to the engine
model by tabbot.twb.from_kit. `parse_both` also returns the kit's rich model, which the explanation engine uses.
"""

from __future__ import annotations

from pathlib import Path

from tabbot.tparse import parse_workbook as _kit_parse
from tabbot.tparse.model import WorkbookModel as KitModel
from tabbot.twb.from_kit import to_engine
from tabbot.twb.model import WorkbookModel


def parse_both(path: Path | str) -> tuple[WorkbookModel, KitModel]:
    kit = _kit_parse(path)
    return to_engine(kit), kit


def parse_workbook(path: Path | str, thumbnails_dir: Path | None = None) -> WorkbookModel:
    """`thumbnails_dir` is accepted for compatibility; preview images are no longer written to disk."""
    return parse_both(path)[0]
