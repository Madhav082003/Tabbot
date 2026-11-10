"""Value maps and 'what does this measure count' - read from formulas alone (no data)."""

from tabbot.explain.calcinfo import counts_what, value_map

REPORT_TYPE = '''CASE [Classification]
  WHEN "Clinical Trial" THEN "Solicited"
  WHEN "Spontaneous"    THEN "Spontaneous"
  WHEN "Regulatory"     THEN "Spontaneous"
  WHEN "Literature"     THEN "Literature Spontaneous"
END'''


def test_case_without_else_lists_every_value_and_null():
    vm = value_map(REPORT_TYPE)
    assert vm["maps_field"] == "Classification" and len(vm["rules"]) == 4
    assert vm["possible_values"] == ["Solicited", "Spontaneous", "Literature Spontaneous", "Null"]
    assert vm["other_values"].startswith("no ELSE") and "Null" in vm["other_values"]
    assert vm["merged"] == ["Spontaneous <- Spontaneous, Regulatory"]


def test_case_else_field_keeps_values_and_lists():
    vm = value_map('CASE [Country] WHEN "USA" THEN "United States" ELSE [Country] END')
    assert "kept unchanged" in vm["other_values"] and "Null" not in vm["possible_values"]
    vm = value_map("CASE [X] WHEN 'a', 'b' THEN 'AB' WHEN 'c' THEN NULL END")
    assert vm["rules"][0]["when"] == ["a", "b"] and vm["possible_values"] == ["AB", "Null"]


def test_if_chains_and_keywords_inside_names():
    vm = value_map('IF [Region] = "East" OR [Region]="North" THEN "A" '
                   'ELSEIF [Region] IN ("West","South") THEN "B" ELSE "C" END')
    assert vm["possible_values"] == ["A", "B", "C"] and vm["rules"][1]["when"] == ["West", "South"]
    # "Case" inside a field name is not the CASE keyword
    assert value_map('IF [Case Seriousness] = "Serious" THEN 1 ELSE 0 END')["maps_field"] == "Case Seriousness"
    # not a value mapping: comparisons, nested IFs, mixed fields
    assert value_map('IF [Age] < 18 THEN "child" ELSE "adult" END') is None
    assert value_map('IF [A] = "x" THEN "1" ELSEIF [B] = "y" THEN "2" END') is None
    assert value_map('IF [A] = "x" THEN IF [B] = "y" THEN "1" END END') is None


def test_counts_what_follows_flags_and_lods():
    f = {"Serious": 'IF [Seriousness] = "Serious" THEN 1 ELSE 0 END',
         "Other": "IIF([Seriousness] = 'Non-Serious', 1, 0)",
         "Total": "[Serious] + [Other]",
         "Total(Country)": "{EXCLUDE [Seriousness] : SUM([Total])}",
         "Cases": "COUNTD([Caseid])"}
    lk = f.get
    assert counts_what("SUM", "Serious", lk).startswith("rows")
    assert counts_what(None, "Total(Country)", lk).startswith("rows")
    assert counts_what(None, "Cases", lk).startswith("distinct Caseid")
    assert counts_what("COUNT", "Caseid", lk).startswith("rows with a Caseid value")
    assert counts_what("SUM", "Sales", lk) is None
