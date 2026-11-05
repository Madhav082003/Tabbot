"""Calculation analysis on formulas shaped like the ones in the corpus."""
from tabbot.tparse.formula import analyze, readable, tokenize


def test_countd_if_is_aggregate():
    i = analyze('COUNTD( IF [EVENT_SERIOUSNESS] = "Serious" THEN [CASEID] END )')
    assert i.kind == "aggregate" and i.aggregations == ["COUNTD"] and i.fields == ["EVENT_SERIOUSNESS", "CASEID"]
    assert i.has_conditional and i.string_literals == 1


def test_field_names_with_parentheses_are_not_functions():
    i = analyze("SUM([GDP (curr $)]) / SUM([Population])")
    assert i.functions == ["SUM"] and "GDP (curr $)" in i.fields


def test_comments_and_strings_ignored():
    i = analyze('// SUM([x]) in a comment\n"COUNT([y])" + [z]')
    assert i.kind == "row_level" and i.functions == [] and i.fields == ["z"] and i.comment_lines == 1


def test_lod():
    i = analyze("{ FIXED [Customer Name] : MIN([Order Date]) }")
    assert i.kind == "lod" and i.lods[0].type == "FIXED" and i.lods[0].dimensions == ["Customer Name"]
    assert "MIN([Order Date])" in i.lods[0].expression
    t = analyze("{ MAX([Sales]) }")
    assert t.lods[0].type == "TABLE"


def test_table_calc_and_parameters():
    i = analyze("ROUND(INDEX() / 3 - 0.6,0) + 1")
    assert i.kind == "table_calc" and i.table_calc_functions == ["INDEX"]
    p = analyze("([Parameters].[Commission Rate]*[Sales])/100")
    assert p.parameters == ["Commission Rate"] and p.fields == ["Sales"] and p.kind == "row_level"


def test_constant_and_date_literals():
    assert analyze("50000").kind == "constant"
    i = analyze("[Order Date] >= #2020-01-01#")
    assert any(t.kind == "DATE" for t in tokenize("[Order Date] >= #2020-01-01#")) and i.kind == "row_level"


def test_readable_replaces_internal_names():
    out = readable("DATETRUNC('quarter', [Calculation_0322741033488384])", {"Calculation_0322741033488384": "Parsed Date"})
    assert out == "DATETRUNC('quarter', [Parsed Date])"
