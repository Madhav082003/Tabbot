"""Formula translation: check semantics by running the SQL in DuckDB on a tiny table."""

import duckdb
import pytest

from tabbot.data.store import qi
from tabbot.views.calc_translate import Expr, Unsupported, translate

ROWS = [  # id, age, unit, sev, name, d (text date)
    ("a", 5, "Years", "Serious", "Ann", "30-SEP-2021"),
    ("a", 5, "Years", "Non-serious", "Ann", "30-SEP-2021"),
    ("b", 40, "Years", "Serious", "Bob", "02-JAN-2024"),
    ("c", None, None, "Non-serious", None, "15-MAY-2026"),
    ("d", 70, "Months", "Non-serious", "Dee", "01-DEC-2025"),
]


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    c.execute("CREATE TABLE t (ID VARCHAR, AGE BIGINT, UNIT VARCHAR, SEV VARCHAR, NAME VARCHAR, D VARCHAR)")
    c.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?)", ROWS)
    return c


def resolve(name: str) -> Expr:
    if name == "D":
        return Expr(qi(name), is_str=True, text_date_format="%d-%b-%Y")
    if name not in {"ID", "AGE", "UNIT", "SEV", "NAME"}:
        raise Unsupported(f"field [{name}] is not in the extract")
    return Expr(qi(name), is_str=name in {"ID", "UNIT", "SEV", "NAME"})


def run(con, formula: str, agg: bool = False):
    e = translate(formula, resolve)
    assert e.is_agg == agg
    sql = f"SELECT {e.sql} FROM t" + ("" if agg else " ORDER BY rowid")
    return [r[0] for r in con.execute(sql).fetchall()]


def test_if_elseif_else_and_isnull(con):
    f = ('IF ISNULL([AGE]) THEN "Not Reported" ELSEIF [AGE] < 18 THEN "Under 18" '
         'ELSEIF [AGE] < 65 THEN "18 to 64" ELSE "65 and above" END')
    assert run(con, f) == ["Under 18", "Under 18", "18 to 64", "Not Reported", "65 and above"]


def test_countd_if_is_aggregate(con):
    assert run(con, 'COUNTD(IF [SEV] = "Serious" THEN [ID] END)', agg=True) == [2]
    assert run(con, "COUNT([SEV]) / COUNTD([ID])", agg=True) == [pytest.approx(5 / 4)]


def test_text_date_and_date_functions(con):
    import datetime as dt

    assert run(con, "DATE([D])")[0] == dt.date(2021, 9, 30)
    assert run(con, "DATETRUNC('quarter', DATE([D]))")[2] == dt.date(2024, 1, 1)
    assert run(con, "DATEPART('quarter', DATE([D]))") == [3, 3, 1, 2, 4]
    assert run(con, "YEAR([D])") == [2021, 2021, 2024, 2026, 2025]


def test_strings_case_iif_and_comments(con):
    assert run(con, '[NAME] + " (" + [UNIT] + ")"')[0] == "Ann (Years)"
    assert run(con, 'CASE [UNIT] WHEN "Years" THEN 1 WHEN "Months" THEN 2 ELSE 0 END') == [1, 1, 1, 0, 2]
    assert run(con, 'IIF([AGE] > 30, "old", "young")') == ["young", "young", "old", None, "old"]
    assert run(con, "// a comment\n[AGE] * 2 // trailing") == [10, 10, 80, None, 140]
    assert run(con, "UPPER(LEFT([NAME], 2))")[0] == "AN"
    assert run(con, "IFNULL([NAME], 'n/a')")[3] == "n/a"
    assert run(con, "ZN([AGE]) / 0") == [None] * 5  # Tableau returns null on division by zero


@pytest.mark.parametrize("formula, reason", [
    ("{FIXED [ID] : SUM([AGE])}", "level-of-detail"),
    ("RUNNING_SUM(SUM([AGE]))", "table calculation"),
    ("SOMEFUNC([AGE])", "function SOMEFUNC"),
    ("[Parameters].[Parameter 1] * 2", "parameter"),
    ("[Missing Field] + 1", "not in the extract"),
    ("IF [AGE] > 1 THEN 1", "ended unexpectedly"),
])
def test_unsupported_is_reported_not_guessed(formula, reason):
    with pytest.raises(Unsupported, match=reason):
        translate(formula, resolve)
