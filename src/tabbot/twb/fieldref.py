"""Decode Tableau field references such as  [federated.pv].[ctd:CASEID:qk].

Form: [<datasource>].[<derivation>:<field>:<type key>]  (sometimes extra parts, e.g. ctd:CASEID:vtsum:qk).
Field names may contain ':' and escaped ']' (written ']]'). See docs/TABLEAU_REFERENCE.md §4.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

# [ds].[inner] where both parts may contain "]]" escapes
REF_RE = re.compile(r"\[((?:[^\]]|\]\])+)\]\.\[((?:[^\]]|\]\])+)\]")

TYPE_KEYS = {"nk": "nominal", "ok": "ordinal", "qk": "quantitative"}

AGGREGATIONS = {
    "ctd": "count_distinct", "cnt": "count", "sum": "sum", "avg": "avg", "min": "min", "max": "max",
    "med": "median", "attr": "attr", "usr": "user_aggregate", "cntd": "count_distinct",
    "stdev": "stdev", "var": "var",
}
DATE_PARTS = {"yr": "year", "qr": "quarter", "mn": "month", "wk": "week", "dy": "day", "wd": "weekday",
              "hr": "hour", "mi": "minute", "sc": "second"}
DATE_TRUNCS = {"tyr": "year", "tqr": "quarter", "tmn": "month", "twk": "week", "tdy": "day",
               "thr": "hour", "tmi": "minute", "tsc": "second"}
TABLE_CALC_PREFIXES = ("pcto", "pcdf", "cum", "diff", "rank", "pctrk", "win", "run", "mavg")


class FieldRef(BaseModel):
    datasource: str
    derivation: str            # e.g. "ctd", "none", "yr"; "" when the reference has no derivation
    field: str                 # internal field name, e.g. "CASEID" or "Calculation_0322741033488384"
    extra: list[str] = []      # middle parts after the field, e.g. ["vtsum"]
    type_key: str              # "nk" | "ok" | "qk" | "?"
    raw: str
    caption: str | None = None  # filled in by the parser from the field index

    @property
    def kind(self) -> str:
        """dimension | measure | date_part | date_trunc | table_calc | special | unknown"""
        d = self.derivation
        if d == "none":
            return "dimension"
        if d in AGGREGATIONS:
            return "measure"
        if d in DATE_PARTS:
            return "date_part"
        if d in DATE_TRUNCS:
            return "date_trunc"
        if d.startswith(TABLE_CALC_PREFIXES):
            return "table_calc"
        if d == "" and self.field.startswith(("Measure Names", "Measure Values", "Multiple Values")):
            return "special"
        return "unknown"

    @property
    def aggregation(self) -> str | None:
        return AGGREGATIONS.get(self.derivation)

    @property
    def date_grain(self) -> str | None:
        return DATE_PARTS.get(self.derivation) or DATE_TRUNCS.get(self.derivation)

    @property
    def label(self) -> str:
        name = self.caption or self.field
        if self.kind == "measure":
            words = {"count_distinct": "Distinct count of", "count": "Count of"}
            if self.aggregation == "user_aggregate":
                return name
            if self.aggregation in words:
                return f"{words[self.aggregation]} {name}"
            return f"{self.aggregation.upper()}({name})"
        if self.kind in ("date_part", "date_trunc"):
            from tabbot.tparse.fieldref import date_label
            return date_label(name, self.date_grain, trunc=self.kind == "date_trunc")
        return name

    def label_in(self, refs: list[FieldRef]) -> str:
        """Label, knowing the other fields on the view (a date part next to YEAR of the same field is not a
        cycle over years)."""
        if self.kind == "date_part" and any(r.field == self.field and r.derivation == "yr" for r in refs):
            from tabbot.tparse.fieldref import date_label
            return date_label(self.caption or self.field, self.date_grain, trunc=False, with_year=True)
        return self.label


def _unescape(s: str) -> str:
    return s.replace("]]", "]")


def parse_inner(datasource: str, inner: str, raw: str | None = None) -> FieldRef:
    parts = inner.split(":")
    raw = raw or f"[{datasource}].[{inner}]"
    if len(parts) >= 3 and parts[-1] in TYPE_KEYS:
        derivation, type_key, middle = parts[0], parts[-1], parts[1:-1]
        extra: list[str] = []
        # extras are short lowercase tokens after the field name (e.g. "vtsum"); field names rarely look like that
        while len(middle) > 1 and re.fullmatch(r"[a-z]{2,8}", middle[-1]):
            extra.insert(0, middle.pop())
        return FieldRef(datasource=datasource, derivation=derivation, field=":".join(middle), extra=extra,
                        type_key=type_key, raw=raw)
    if len(parts) == 2 and parts[0] == "":
        return FieldRef(datasource=datasource, derivation="", field=parts[1], type_key="?", raw=raw)
    return FieldRef(datasource=datasource, derivation="none" if len(parts) == 1 else "unknown:" + parts[0],
                    field=inner if len(parts) == 1 else ":".join(parts[1:]), type_key="?", raw=raw)


def parse_ref(text: str) -> FieldRef | None:
    """Parse one full reference '[ds].[deriv:field:tk]'. Returns None if the text holds no reference."""
    m = REF_RE.search(text or "")
    if not m:
        return None
    return parse_inner(_unescape(m.group(1)), _unescape(m.group(2)), m.group(0))


def parse_shelf(text: str | None) -> list[FieldRef]:
    """All references on a Rows/Columns shelf, in order. '/' (nest) and '*' (cross) both mean group-by-both."""
    return [parse_inner(_unescape(m.group(1)), _unescape(m.group(2)), m.group(0))
            for m in REF_RE.finditer(text or "")]
