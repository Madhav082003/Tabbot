"""Local copies of workbooks and their data must not pile up in workspace/."""

import zipfile

import pytest

from tabbot.cli import main
from tabbot.config import load_settings
from tabbot.loader import load_workbook
from tabbot.workspace import UnsafeArchiveError, purge_workspaces


def _dirs(base):
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []


def test_close_with_delete_removes_copy(fixture_twbx, tmp_path):
    s = load_settings().model_copy(update={"workspace_dir": tmp_path})
    lw = load_workbook(fixture_twbx, settings=s)
    assert (lw.workspace.parquet).exists() and any(lw.workspace.parquet.iterdir())
    lw.close(delete=True)
    assert not lw.workspace.root.exists() and _dirs(tmp_path) == []


def test_failed_load_leaves_nothing(tmp_path):
    s = load_settings().model_copy(update={"workspace_dir": tmp_path / "ws"})
    bad = tmp_path / "bad.twbx"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("ok.twb", "<workbook/>")
        z.writestr("../evil.txt", "x")
    with pytest.raises(UnsafeArchiveError):
        load_workbook(bad, settings=s)
    broken = tmp_path / "broken.twb"
    broken.write_text("<workbook><not-closed>", encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017 - any parse error; the point is what is left behind
        load_workbook(broken, settings=s)
    assert _dirs(tmp_path / "ws") == []


@pytest.mark.parametrize("cmd", [["inspect"], ["lint"], ["analyze", "_", "Category Heatmap"]])
def test_cli_cleans_up_unless_keep(cmd, fixture_twbx, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    from tabbot import config

    config.get_settings.cache_clear()
    args = [cmd[0], str(fixture_twbx)] + cmd[2:]
    assert main(args) == 0
    assert _dirs(tmp_path) == []
    assert main(args + ["--keep"]) == 0
    assert len(_dirs(tmp_path)) == 1
    capsys.readouterr()
    config.get_settings.cache_clear()


def test_purge_keeps_audit_and_demo(tmp_path):
    for name in ("audit", "_demo", "abc123", "def456"):
        (tmp_path / name).mkdir()
    assert purge_workspaces(tmp_path) == 2
    assert _dirs(tmp_path) == ["_demo", "audit"]
