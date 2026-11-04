import pytest

from tabbot.twb.fieldref import parse_ref, parse_shelf


@pytest.mark.parametrize("raw, deriv, field, extra, tk, kind", [
    ("[federated.pv].[ctd:CASEID:qk]", "ctd", "CASEID", [], "qk", "measure"),
    ("[federated.pv].[none:SOC:nk]", "none", "SOC", [], "nk", "dimension"),
    ("[federated.pv].[yr:Calculation_0322741033488384:ok]", "yr", "Calculation_0322741033488384", [], "ok",
     "date_part"),
    ("[federated.pv].[tqr:Calculation_1:qk]", "tqr", "Calculation_1", [], "qk", "date_trunc"),
    ("[federated.pv].[ctd:CASEID:vtsum:qk]", "ctd", "CASEID", ["vtsum"], "qk", "measure"),
    ("[ds].[sum:Sales:qk]", "sum", "Sales", [], "qk", "measure"),
    ("[ds].[pcto:sum:Sales:qk]", "pcto", "sum:Sales", [], "qk", "table_calc"),
    ("[ds].[none:Order Date (copy):nk]", "none", "Order Date (copy)", [], "nk", "dimension"),
    ("[ds].[none:Ratio a:b:nk]", "none", "Ratio a:b", [], "nk", "dimension"),
    ("[ds].[none:Weird]]Name:nk]", "none", "Weird]Name", [], "nk", "dimension"),
    ("[ds].[xyz:Field:nk]", "xyz", "Field", [], "nk", "unknown"),
    ("[ds].[:Measure Names]", "", "Measure Names", [], "?", "special"),
])
def test_parse_ref(raw, deriv, field, extra, tk, kind):
    r = parse_ref(raw)
    assert (r.derivation, r.field, r.extra, r.type_key, r.kind) == (deriv, field, extra, tk, kind)


def test_parse_shelf_nest_and_cross():
    refs = parse_shelf("([ds].[yr:D:ok] / [ds].[qr:D:ok]) * [ds].[none:SOC:nk]")
    assert [(r.derivation, r.field) for r in refs] == [("yr", "D"), ("qr", "D"), ("none", "SOC")]


def test_empty_and_garbage():
    assert parse_shelf("") == [] and parse_shelf(None) == []
    assert parse_ref("not a ref") is None


def test_labels():
    r = parse_ref("[ds].[ctd:CASEID:qk]")
    r.caption = "Case Id"
    assert r.label == "Distinct count of Case Id"
    assert parse_ref("[ds].[qr:D:ok]").label == "QUARTER(D)"
