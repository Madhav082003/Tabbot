"""Regression: two threads querying the same store at once (Streamlit overlapping reruns) must not fail.

Before the fix this raised duckdb ResourceInUseException: 'connection is busy with another call'.
"""

import threading

import pytest

from tabbot.config import load_settings
from tabbot.loader import load_workbook
from tabbot.views.rebuild import rebuild_worksheet


@pytest.fixture(scope="module")
def lw(fixture_twbx, tmp_path_factory):
    settings = load_settings().model_copy(update={"workspace_dir": tmp_path_factory.mktemp("ws")})
    loaded = load_workbook(fixture_twbx, settings=settings)
    yield loaded
    loaded.close()


def test_parallel_rebuilds_from_many_threads(lw):
    errors: list[BaseException] = []
    results: dict[tuple[int, str], int] = {}
    sheets = [w.name for w in lw.model.worksheets]
    start = threading.Barrier(8)

    def work(i: int) -> None:
        try:
            start.wait()
            for _ in range(3):
                for s in sheets:
                    r = rebuild_worksheet(lw.model, s, lw.store, lw.table_for(None), lw.profile_for(None))
                    assert r.status == "exact", (s, r.reasons)
                    results[(i, s)] = len(r.data)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    # every thread got the same answers
    for s in sheets:
        assert len({results[(i, s)] for i in range(8)}) == 1


def test_each_thread_has_its_own_cursor(lw):
    seen = []
    t = threading.Thread(target=lambda: seen.append(lw.store.con))
    t.start()
    t.join()
    assert seen[0] is not lw.store.con
