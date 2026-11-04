import zipfile

import pytest

from tabbot.workspace import UnsafeArchiveError, new_workspace, open_upload, safe_extract


def _zip(path, members: dict[str, bytes]):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


@pytest.mark.parametrize("bad", ["../evil.txt", "a/../../evil.txt", "/abs/evil.txt", "C:/evil.txt"])
def test_rejects_path_traversal_and_absolute(tmp_path, bad):
    z = _zip(tmp_path / "x.twbx", {"ok.twb": b"<workbook/>", bad: b"x"})
    dest = tmp_path / "out"
    with pytest.raises(UnsafeArchiveError):
        safe_extract(z, dest)
    assert not (dest / "ok.twb").exists(), "nothing may be written when any member is unsafe"


def test_rejects_too_large(tmp_path):
    z = _zip(tmp_path / "big.twbx", {"a.twb": b"0" * (2 * 1024 * 1024)})
    with pytest.raises(UnsafeArchiveError):
        safe_extract(z, tmp_path / "out", max_unzipped_mb=1)


def test_rejects_zip_bomb_ratio(tmp_path):
    z = _zip(tmp_path / "bomb.twbx", {"a.twb": b"\0" * (12 * 1024 * 1024)})
    with pytest.raises(UnsafeArchiveError):
        safe_extract(z, tmp_path / "out")


def test_rejects_too_many_members(tmp_path):
    z = _zip(tmp_path / "many.twbx", {f"f{i}.txt": b"x" for i in range(501)})
    with pytest.raises(UnsafeArchiveError):
        safe_extract(z, tmp_path / "out")


def test_open_fixture(tmp_path, fixture_twbx):
    ws = new_workspace(tmp_path)
    pkg = open_upload(fixture_twbx, ws)
    assert pkg.workbook.name == "fixture.twb"
    assert [p.name for p in pkg.extracts] == ["federated.hyper"]
    assert (ws.upload / "fixture.twbx").exists()
    assert fixture_twbx.exists()  # original untouched


def test_rejects_unknown_type(tmp_path):
    f = tmp_path / "x.xlsx"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        open_upload(f, new_workspace(tmp_path))
