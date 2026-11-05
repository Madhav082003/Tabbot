"""Optional: check the parser against facts established by a manual analysis of a private PV workbook.

Point TABBOT_PRIVATE_PV at that .twbx to run it; skipped otherwise. The workbook itself is never committed.
"""
import os
from pathlib import Path

import pytest

from tabbot.tparse import parse_workbook

PV = os.environ.get("TABBOT_PRIVATE_PV")
pytestmark = pytest.mark.skipif(not PV or not Path(PV).exists(), reason="TABBOT_PRIVATE_PV not set")


def test_matches_manual_analysis():
    m = parse_workbook(PV)
    assert (len(m.worksheets), len(m.dashboards), len(m.hidden_worksheets)) == (88, 13, 87)
    ds = m.datasources[0]
    assert len(ds.physical_fields) == 56 and len(m.calculated_fields) == 14
    used = {f.caption for f in m.calculated_fields if f.used_by}
    assert used == {"Parsed Date", "Age Group", "Parse Quarter"}
    s101 = m.worksheet("Sheet 101")
    enc = {e.channel: e.field for p in s101.panes for e in p.encodings}
    assert enc["color"].derivation == "ctd" and enc["color"].caption == "Parsed Date"   # counts dates (finding 1)
    s105 = m.worksheet("Sheet 105")
    assert s105.chart_type.startswith("heatmap") and s105.filters[0].member_count == 27
    assert m.size_report["token_estimate_rough"] > 400_000
    assert not m.unresolved_refs
