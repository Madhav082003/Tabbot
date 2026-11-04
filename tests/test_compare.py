"""'Check against Tableau': compare a rebuild with crosstab / summary-data CSVs like Tableau exports."""

import pytest

from tabbot.config import load_settings
from tabbot.loader import load_workbook
from tabbot.views.compare import compare_with_tableau
from tabbot.views.display import fmt_dim, pivot
from tabbot.views.rebuild import rebuild_worksheet


@pytest.fixture(scope="module")
def heat(fixture_twbx, tmp_path_factory):
    settings = load_settings().model_copy(update={"workspace_dir": tmp_path_factory.mktemp("ws")})
    lw = load_workbook(fixture_twbx, settings=settings)
    r = rebuild_worksheet(lw.model, "Category Heatmap", lw.store, lw.table_for(None), lw.profile_for(None))
    yield r
    lw.close()


def crosstab_csv(res, change=None) -> bytes:
    """Mimic Tableau's Download > Crosstab: UTF-16, tab separated, two header rows (year, quarter)."""
    pv, _, _ = pivot(res)
    years = [c.split(" ")[0] for c in pv.columns]
    quarters = [c.split(" ")[1] for c in pv.columns]
    lines = ["\t".join(["", *years]), "\t".join(["Category", *quarters])]
    for i, (label, row) in enumerate(pv.iterrows()):
        vals = ["" if v != v else f"{int(v):,}" for v in row.tolist()]
        if change and i == change[0]:
            vals[change[1]] = str(int(row.iloc[change[1]]) + 1)
        lines.append("\t".join([label, *vals]))
    return "\n".join(lines).encode("utf-16")


def test_crosstab_match(heat):
    rep = compare_with_tableau(heat, crosstab_csv(heat))
    assert rep.status == "match" and rep.method == "crosstab" and rep.matched == 8


def test_crosstab_mismatch_names_the_row(heat):
    rep = compare_with_tableau(heat, crosstab_csv(heat, change=(2, 3)))
    assert rep.status == "mismatch" and rep.compared - rep.matched == 1
    assert rep.differences and rep.differences[0].startswith("Category C")


def test_long_summary_data_match(heat):
    d = heat.data
    lines = ["CATEGORY,Year of Parsed Date,Quarter of Parsed Date,Distinct count of RECORD_ID"]
    q = fmt_dim(d["d2"], heat.dims[2])
    for a, b, c, v in zip(d["d0"], d["d1"], q, d["m0"], strict=True):
        lines.append(f"{a},{int(b)},{c},{int(v)}")
    rep = compare_with_tableau(heat, "\n".join(lines).encode("utf-8"))
    assert rep.status == "match" and rep.method == "long"


def test_unrelated_file(heat):
    rep = compare_with_tableau(heat, b"hello,world\nfoo,bar\n")
    assert rep.status == "unknown"
