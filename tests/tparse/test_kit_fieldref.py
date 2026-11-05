"""Field references and shelves, using reference shapes seen in real workbooks (docs/PATTERNS.md §3)."""
import pytest

from tabbot.tparse.fieldref import find_refs, parse_inner, parse_ref, parse_shelf

CASES = [
    # inner, field, derivation, kind, table_calc, modifiers, type_key, instance
    ("none:SOC:nk", "SOC", "none", "dimension", None, [], "nk", None),
    ("sum:Sales:qk", "Sales", "sum", "aggregate", None, [], "qk", None),
    ("ctd:CASEID:qk", "CASEID", "ctd", "aggregate", None, [], "qk", None),
    ("tqr:Calculation_0322741033488384:qk", "Calculation_0322741033488384", "tqr", "date_trunc", None, [], "qk", None),
    ("yr:Order Date:ok", "Order Date", "yr", "date_part", None, [], "ok", None),
    ("usr:Calculation_5144658905056067585:qk", "Calculation_5144658905056067585", "usr", "user", None, [], "qk", None),
    ("io:BurstoutSet:nk", "BurstoutSet", "io", "set", None, [], "nk", None),
    ("ctd:CASEID:vtsum:qk", "CASEID", "ctd", "aggregate", None, ["vtsum"], "qk", None),
    ("rank:sum:Sales:qk", "Sales", "sum", "aggregate", "rank", [], "qk", None),
    ("fVal:sum:Sales:qk", "Sales", "sum", "aggregate", "fVal", [], "qk", None),
    ("avg:F: GDP per capita (curr $):qk", "F: GDP per capita (curr $)", "avg", "aggregate", None, [], "qk", None),
    ("pcto:cnt:BMI:qk:1", "BMI", "cnt", "aggregate", "pcto", [], "qk", 1),
    ("pcto:sum:F: GDP (curr $):qk:1", "F: GDP (curr $)", "sum", "aggregate", "pcto", [], "qk", 1),
    ("none:AdhocCluster:1:ok", "AdhocCluster:1", "none", "dimension", None, [], "ok", None),
]


@pytest.mark.parametrize("inner,field,deriv,kind,tc,mods,tk,inst", CASES)
def test_parse_inner(inner, field, deriv, kind, tc, mods, tk, inst):
    r = parse_inner("ds", inner)
    assert (r.field, r.derivation, r.kind, r.table_calc, r.modifiers, r.type_key, r.instance) == \
        (field, deriv, kind, tc, mods, tk, inst)


def test_bare_and_special():
    assert parse_inner("ds", "Multiple Values").kind == "bare"
    assert parse_inner("ds", "Latitude (generated)").field == "Latitude (generated)"
    mn = parse_inner("ds", ":Measure Names")
    assert mn.kind == "special" and mn.field == "Measure Names"


def test_unknown_derivation_does_not_crash():
    r = parse_inner("ds", "zzz:Sales:qk")
    assert r.kind == "unknown" and r.field == "Sales"


def test_escaped_bracket_in_name():
    refs = find_refs("[ds].[none:Ratio [x]]:nk]")
    assert refs and refs[0][1] == "none:Ratio [x]:nk"


def test_object_id_triple():
    text = "[federated.1].[__tableau_internal_object_id__].[cnt:Orders_6D2EF74F348B46BDA976A7AEEA6FB5C9:qk]"
    refs = find_refs(text)
    assert len(refs) == 1 and refs[0][0] == "federated.1" and refs[0][1].startswith("cnt:Orders_")
    shelf = parse_shelf(text)
    assert shelf.error is None and len(shelf.refs) == 1


def test_shelf_nesting_crossing_and_dual_axis():
    s = parse_shelf("([ds].[none:HLT:nk] / ([ds].[none:PT:nk] / [ds].[none:COUNTRY:nk]))")
    assert s.error is None and [r.field for r in s.refs] == ["HLT", "PT", "COUNTRY"]
    assert s.tree.op == "/"
    c = parse_shelf("([ds].[none:SOC:nk] * [ds].[tqr:Parsed Date:qk])")
    assert c.tree.op == "*" and len(c.dimensions) == 2
    d = parse_shelf("([ds].[sum:x:qk] + [ds].[usr:Calculation_3002775091869110273:qk])")
    assert d.tree.op == "+" and len(d.measures) == 2


def test_shelf_tree_and_refs_share_objects():
    s = parse_shelf("[ds].[none:SOC:nk]")
    s.refs[0].caption = "System Organ Class"
    assert s.describe() == "System Organ Class"


def test_labels():
    assert parse_ref("[ds].[ctd:CASEID:qk]").label() == "COUNTD(CASEID)"
    assert parse_ref("[ds].[qr:Order Date:ok]").label() == "QUARTER(Order Date) (Q1–Q4, years combined)"
    assert parse_ref("[ds].[pcto:sum:Sales:qk:1]").label() == "Percent of total of SUM(Sales)"
