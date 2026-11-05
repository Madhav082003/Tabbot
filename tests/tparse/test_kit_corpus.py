"""Run the parser over every workbook in the corpus (built by tools/fetch_corpus.py).

Skipped when the corpus is absent. Set TABBOT_CORPUS to its xml/ folder (default: corpus/xml).
"""
import os
from pathlib import Path

import pytest
from defusedxml import ElementTree as ET

from tabbot.tparse import describe_dashboard, describe_worksheet, overview, parse_workbook

CORPUS = Path(os.environ.get("TABBOT_CORPUS", "corpus/xml"))
FILES = sorted(CORPUS.glob("*.twb")) if CORPUS.exists() else []
pytestmark = pytest.mark.skipif(not FILES, reason=f"no corpus at {CORPUS}")


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_workbook_parses_completely(path):
    m = parse_workbook(path)
    raw = ET.parse(path).getroot()
    assert len(m.worksheets) == len(raw.findall("./worksheets/worksheet"))
    assert len(m.dashboards) == len(raw.findall("./dashboards/dashboard"))
    assert len(m.datasources) == len(raw.findall("./datasources/datasource"))
    assert not [w for w in m.warnings if "failed" in w], m.warnings
    for ws in m.worksheets:
        assert ws.chart_type
        assert describe_worksheet(ws, m)
    for db in m.dashboards:
        assert describe_dashboard(db, m)
    overview(m)
    m.model_dump_json()          # everything is serialisable


def test_reference_resolution_rate():
    total = unresolved = 0
    for p in FILES:
        m = parse_workbook(p)
        total += sum(len(w.all_refs()) for w in m.worksheets)
        unresolved += len(m.unresolved_refs)
    assert total > 0 and unresolved / total < 0.01, f"{unresolved}/{total} unresolved"
