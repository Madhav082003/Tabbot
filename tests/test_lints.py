"""Every planted definition problem is found on the right sheet, and nothing else is flagged."""

import pytest

from tabbot.analysis.lints import run_lints, unit_companions
from tabbot.cli import main
from tabbot.config import load_settings
from tabbot.loader import load_workbook


@pytest.fixture(scope="module")
def findings(fixture_twbx, tmp_path_factory):
    settings = load_settings().model_copy(update={"workspace_dir": tmp_path_factory.mktemp("ws")})
    lw = load_workbook(fixture_twbx, settings=settings)
    out = {f.code: f for f in run_lints(lw)}
    out["_all"] = run_lints(lw)
    out["_lw"] = lw
    yield out
    lw.close()


def test_exact_set_of_findings(findings):
    codes = sorted(f.code for f in findings["_all"])
    assert codes == ["L1", "L10", "L12", "L14", "L2", "L3", "L4", "L5", "L6"]  # one each, nothing extra


def test_l1_counts_dates(findings, expected):
    f = findings["L1"]
    assert f.sheets == [expected["planted_problems"]["counts_dates"]["sheet"]]


def test_l2_percent_name(findings, expected):
    assert findings["L2"].dashboards == [expected["planted_problems"]["percent_name_without_percent"]["dashboard"]]


def test_l3_copied_filter(findings, expected):
    p = expected["planted_problems"]["copied_filter"]
    f = findings["L3"]
    assert f.sheets == [p["sheet"]] and f.evidence["field"] == p["field"]


def test_l4_misnamed_sheet(findings, expected):
    p = expected["planted_problems"]["misnamed_sheet"]
    f = findings["L4"]
    assert f.severity == "error" and f.sheets == [p["sheet"]] and f.evidence["filter_keeps"] == p["filters"]


def test_l5_unit_ignored(findings, expected):
    p = expected["planted_problems"]["unit_ignored"]
    f = findings["L5"]
    assert (f.evidence["calc"], f.evidence["field"], f.evidence["unit_field"]) == (p["calc"], p["field"],
                                                                                  p["unit_field"])
    assert f.evidence["affected"] == expected["data"]["entities_with_non_year_age"]


def test_l6_unused(findings, expected):
    assert sorted(findings["L6"].evidence["calcs"]) == sorted(expected["planted_problems"]["unused_calcs"])


def test_info_findings(findings, expected):
    assert findings["L10"].evidence["hidden"] == expected["planted_problems"]["hidden_sheets"]
    assert findings["L12"].sheets == [expected["planted_problems"]["large_exclude_filter"]["sheet"]]
    assert "Users" not in findings["L14"].message  # the path itself is never repeated


def test_unit_companions_generic(findings):
    assert unit_companions(findings["_lw"].model) == {("federated.fx01", "AGE"): "AGE_UNIT"}


def test_cli_lint_and_analyze(fixture_twbx, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from tabbot import config

    config.get_settings.cache_clear()
    assert main(["lint", str(fixture_twbx)]) == 0
    out = capsys.readouterr().out
    assert "9 finding(s)" in out and "[ERROR L4]" in out
    assert main(["analyze", str(fixture_twbx), "Category Heatmap"]) == 0
    out = capsys.readouterr().out
    assert "CHANNEL = Web" in out and "partial_period" in out
    assert main(["analyze", str(fixture_twbx), "No Such Sheet"]) == 2
    config.get_settings.cache_clear()
