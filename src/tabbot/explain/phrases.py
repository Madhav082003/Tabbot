"""Plain-English phrases for the building blocks of a workbook (kit model objects).

Measures ("number of cases", "total Sales"), dimensions ("year and quarter of Parsed Date"), filters
("only 27 listed SOC values", "every Event Outcome except 7 values"), chart reading guides and calculated-field
formulas ("Groups AGE into bands: below 1 -> Infant; ...") - all deterministic, all from the definitions.
"""

from __future__ import annotations

import re

from tabbot.tparse.fieldref import PART_RANGE
from tabbot.tparse.fieldref import FieldRef as KRef
from tabbot.tparse.model import FilterDef
from tabbot.tparse.model import Worksheet as KWorksheet

DATE_TYPES = {"date", "datetime"}
PART_WORD = {"Year": "year", "Quarter": "quarter", "Month": "month", "Week": "week", "Day": "day",
             "Weekday": "weekday", "Hour": "hour", "MDY": "date", "Month/Year": "month and year"}


def plural(word: str) -> str:
    if word.endswith("y") and not word.endswith(("ay", "ey", "oy")):
        return word[:-1] + "ies"
    return word if word.endswith("s") else word + "s"


def n_(n: float | int, word: str) -> str:
    return f"{n:,.0f} {word if round(n) == 1 else plural(word)}"


def fmt(v: float | int | None, digits: int = 0) -> str:
    if v is None:
        return "-"
    return f"{v:,.{digits}f}"


def pct(a: float, b: float, digits: int = 0) -> str:
    return f"{(a / b * 100):.{digits}f}%" if b else "-"


def name(r: KRef) -> str:
    return (r.caption or r.field or "").strip()


def is_date_field(r: KRef, datatypes: dict[tuple[str, str], str]) -> bool:
    return (datatypes.get((r.datasource, r.field)) or "").lower() in DATE_TYPES


class Phraser:
    """Phrases that need workbook context: the record word ("case"), the record id field, data types."""

    def __init__(self, record_word: str = "record", entity_field: str | None = None,
                 datatypes: dict[tuple[str, str], str] | None = None):
        self.record_word = record_word
        self.entity_field = entity_field
        self.datatypes = datatypes or {}

    # ------------------------------------------------------------------ measures and dimensions
    def measure(self, r: KRef) -> str:
        n = name(r)
        if r.kind == "special":
            return "several measures at once (Measure Values)" if n == "Measure Values" else n
        if n == "Number of Records" or r.field == "Number of Records":
            return "number of rows"
        base = n
        if r.kind == "user":
            base = n
        elif r.derivation == "ctd":
            if self.entity_field and r.field == self.entity_field:
                base = f"number of {plural(self.record_word)}"
            elif is_date_field(r, self.datatypes):
                base = f"number of distinct dates in {n}"
            else:
                base = f"number of distinct {n} values"
        elif r.derivation == "cnt":
            base = f"number of rows with a {n} value"
        elif r.derivation == "sum":
            base = f"total {n}"
        elif r.derivation == "avg":
            base = f"average {n}"
        elif r.derivation == "min":
            base = f"lowest {n}"
        elif r.derivation == "max":
            base = f"highest {n}"
        elif r.derivation == "med":
            base = f"median {n}"
        elif r.derivation == "attr":
            base = n
        elif r.derivation in ("std", "stp"):
            base = f"spread (standard deviation) of {n}"
        if r.table_calc:
            base = f"{(r.table_calc_name or 'table calculation').lower()} of the {base}"
        return base

    def dimension(self, r: KRef) -> str:
        n = name(r)
        if r.kind == "date_part":
            rng = PART_RANGE.get(r.derivation_name.lower())
            word = PART_WORD.get(r.derivation_name, r.derivation_name.lower())
            return f"{word} of {n}" + (f" ({rng}, all years added together)" if rng else "")
        if r.kind == "date_trunc":
            part = r.derivation_name.replace("-Trunc", "")
            return f"{n} by {PART_WORD.get(part, part.lower())} (a timeline, one point per period)"
        if r.kind == "set":
            return f"in/out of the set {n}"
        return n

    def dims(self, refs: list[KRef]) -> str:
        """Join dimensions, merging date parts of one field: 'year and quarter of Parsed Date'."""
        out: list[str] = []
        k = 0
        refs = [r for r in refs if not r.is_measure]
        while k < len(refs):
            r = refs[k]
            if r.kind == "date_part":
                parts = [r]
                while k + 1 < len(refs) and refs[k + 1].kind == "date_part" and refs[k + 1].field == r.field:
                    k += 1
                    parts.append(refs[k])
                words = [PART_WORD.get(p.derivation_name, p.derivation_name.lower()) for p in parts]
                txt = f"{' and '.join(words)} of {name(r)}"
                has_year = any(x.derivation == "yr" and x.field == r.field for x in refs)
                rng = PART_RANGE.get(parts[0].derivation_name.lower())
                if not has_year and rng:
                    txt += f" ({rng}, all years added together)"
                out.append(txt)
            else:
                out.append(self.dimension(r))
            k += 1
        return join(out)

    # ------------------------------------------------------------------ worksheets
    def what_it_shows(self, ws: KWorksheet) -> str:
        """One sentence: chart type, measure(s), breakdown, time axis."""
        measures = []
        for r in list(ws.rows.refs) + list(ws.cols.refs) + [e.field for p in ws.panes for e in p.encodings]:
            if r.is_measure and r.raw not in {m.raw for m in measures}:
                measures.append(r)
        by_channel: dict[str, list[KRef]] = {}
        for p in ws.panes:
            for e in p.encodings:
                by_channel.setdefault(e.channel, []).append(e.field)
        rows_d = [r for r in ws.rows.refs if not r.is_measure]
        cols_d = [r for r in ws.cols.refs if not r.is_measure]
        color_d = [r for r in by_channel.get("color", []) if not r.is_measure]
        detail_d = [r for ch in ("lod", "detail", "shape", "wedge-size") for r in by_channel.get(ch, [])
                    if not r.is_measure]
        chart = ws.chart_type or "chart"
        mtxt = join(list(dict.fromkeys(self.measure(m) for m in measures)))
        if not mtxt:
            # Measure Values: the numbers shown are the ones the Measure Names filter keeps
            mn = next((f for f in ws.filters if f.field.field == "Measure Names" and f.members), None)
            mtxt = (f"{len(mn.members)} headline numbers ({join([clean_measure(x) for x in mn.members])})"
                    if mn else "several measures")
        parts = []
        if chart.startswith("pie") or chart.startswith("donut"):
            slices = color_d or detail_d
            parts.append(f"a {chart} splitting the {mtxt} by {self.dims(slices) or 'category'}")
            others = rows_d + cols_d
            if others:
                parts.append(f"with one pie per {self.dims(others)}")
        elif chart.startswith("heatmap"):
            side = [f"{self.dims(rows_d)} down the side" if rows_d else "",
                    f"{self.dims(cols_d)} across the top" if cols_d else ""]
            parts.append(f"a heatmap (a table coloured by value) of the {mtxt}"
                         + (f", with {join([x for x in side if x])}" if rows_d or cols_d else ""))
        elif chart.startswith(("line", "area")):
            time = [r for r in cols_d + rows_d if r.kind in ("date_trunc", "date_part")]
            other = [r for r in cols_d + rows_d if r not in time]
            parts.append(f"a {chart.split(' with')[0]} of the {mtxt}"
                         + (f" over {self.dims(time)}" if time else ""))
            if color_d:
                parts.append(f"with one line per {self.dims(color_d)}")
            if other:
                single = {fd.field.field: fd.members[0] for fd in ws.filters
                          if fd.mode == "include" and fd.member_count == 1}
                fixed = [f"{name(r)} = {single[r.field]}" for r in other if r.field in single]
                free = [r for r in other if r.field not in single]
                if fixed:
                    parts.append(f"for {join(fixed)}")
                if free:
                    parts.append(f"for each {self.dims(free)}")
        elif chart.startswith(("bar", "dot")):
            cats = rows_d + cols_d
            parts.append(f"a {chart.split(' with')[0]} of the {mtxt}" + (f" by {self.dims(cats)}" if cats else ""))
            if color_d:
                parts.append(f"coloured by {self.dims(color_d)}")
        elif chart.startswith("text table") or chart.startswith("table"):
            cats = rows_d + cols_d
            parts.append(f"a table listing the {mtxt}" + (f" for each {self.dims(rows_d)}" if rows_d else "")
                         + (f", across {self.dims(cols_d)}" if cols_d else ""))
            if not cats and color_d:
                parts.append(f"by {self.dims(color_d)}")
        elif chart.startswith("map"):
            parts.append(f"a map of the {mtxt}" + (f" by {self.dims(detail_d + color_d)}" if detail_d or color_d else ""))
        elif chart.startswith("KPI"):
            parts.append(f"a single headline number: the {mtxt}")
        elif chart.startswith("scatter"):
            parts.append(f"a scatter plot comparing {mtxt}" + (f" for each {self.dims(detail_d)}" if detail_d else ""))
        elif chart.startswith("treemap"):
            parts.append(f"a treemap / bubble chart sizing the {mtxt}" + (f" by {self.dims(detail_d + color_d)}"
                                                                          if detail_d or color_d else ""))
        else:
            cats = rows_d + cols_d + color_d + detail_d
            parts.append(f"a {chart} of the {mtxt}" + (f" by {self.dims(cats)}" if cats else ""))
        if ws.table_calcs:
            parts.append("using a table calculation (" + join([t.type or "calculation" for t in ws.table_calcs]) + ")")
        return tidy(" ".join(parts))

    # ------------------------------------------------------------------ filters
    def filter(self, fd: FilterDef, values_in_data: list[str] | None = None, max_list: int = 5) -> str | None:
        """None when the filter does not restrict anything."""
        n = name(fd.field) if fd.field.kind in ("dimension", "bare", "special") else self.dimension(fd.field) \
            if not fd.field.is_measure else self.measure(fd.field)
        vals = ["(empty)" if v == "%null%" else v for v in fd.members]
        m = fd.mode
        ctx = " (context filter)" if fd.context else ""
        if fd.field.kind == "special":
            return None          # Measure Names/Values: chooses which numbers are shown, not which records
        if m == "include":
            if fd.keeps_all_values:
                return None      # keeps true and false: removes only records with no value at all
            if values_in_data is not None and set(values_in_data) <= set(fd.members) and len(values_in_data) > 1:
                return (f"keeps a list of {len(vals)} {n} values that currently covers every {n} in the data, "
                        f"so it does not remove anything today (new values would be left out){ctx}")
            if len(vals) == 1:
                return f"only {n} = {vals[0]}{ctx}"
            if len(vals) <= max_list:
                return f"only {n} = {join(vals, 'or')}{ctx}"
            return f"only {len(vals)} listed {n} values{ctx}"
        if m == "exclude":
            txt = (f"every {n} except {join(vals, 'and')}" if len(vals) <= max_list
                   else f"every {n} except {len(vals)} listed values")
            if values_in_data is not None:
                left = [v for v in values_in_data if v not in fd.members]
                if left and len(left) <= max_list:
                    txt += f" - in this data that leaves only {join(left, 'and')}"
                elif not left:
                    txt += " - in this data that leaves nothing"
            return txt + ctx
        if m == "exclude-null":
            return f"leaves out records with an empty {n}{ctx}"
        if m == "all":
            return None
        if m in ("top", "bottom"):
            t = fd.top or {}
            by = re.sub(r"\[[^\]]*\]\.\[(?:[a-z]+:)?([^:\]]+)(?::[a-z]+)?\]", r"\1", t.get("by") or "")
            return f"only the {m} {t.get('count') or 'N'} {n} values by {by or 'a measure'}{ctx}"
        if m == "range":
            r = fd.range or {}
            lo, hi = r.get("min", r.get("from")), r.get("max", r.get("to"))
            lo = num_text((lo or "").strip("#")) or None
            hi = num_text((hi or "").strip("#")) or None
            if lo and hi:
                return f"{n} between {lo} and {hi}{ctx}"
            if lo:
                return f"{n} from {lo}{ctx}"
            if hi:
                return f"{n} up to {hi}{ctx}"
            inc = r.get("included_values")
            return (f"only non-empty {n}{ctx}" if inc == "non-null" else None)
        if m == "relative":
            r = fd.relative or {}
            return f"a moving date window on {n} ({r.get('period_type')} {r.get('first')} to {r.get('last')}){ctx}"
        if m == "action":
            return None          # clicks are described from the actions list; leftovers have no effect          # leftover from a removed action / viz in tooltip: no effect on the dashboard
        if m == "set":
            return f"only members of the set {n}{ctx}"
        if m == "condition":
            return f"a condition on {n}{ctx}"
        if m == "crossjoin":
            return f"a combined selection over several fields{ctx}"
        return f"a filter on {n} ({m}){ctx}"


# ------------------------------------------------------------------ chart reading guides

def how_to_read(ws: KWorksheet, ph: Phraser) -> str | None:
    c = (ws.chart_type or "")
    labels = any(e.channel == "text" for p in ws.panes for e in p.encodings)
    if c.startswith("heatmap"):
        return ("Each cell is one combination of the row and column values; the number in it is the count, and a "
                "darker colour means a larger number." if labels else
                "Each cell is one combination of the row and column values; a darker colour means a larger number.")
    if c.startswith(("pie", "donut")):
        return ("Each slice is one value; its size is that value's part of the whole" +
                (" and its label shows the count" if labels else "") + ".")
    if c.startswith(("line", "area")):
        return "Each point is one period; follow the line to see the trend over time."
    if c.startswith("bar"):
        return "Each bar is one category; longer bars mean larger values."
    if c.startswith(("text table", "table")):
        return "Each row is one combination of the listed fields, with its value next to it."
    if c.startswith("KPI"):
        return "A single number, usually a headline total."
    return None


# ------------------------------------------------------------------ calculated fields

_OPS = [(r"\s*<>\s*|\s*!=\s*", " is not "), (r"\s*>=\s*", " is at least "), (r"\s*<=\s*", " is at most "),
        (r"\s*==?\s*", " is "), (r"\s*<\s*", " is below "), (r"\s*>\s*", " is above ")]


def _clean(expr: str) -> str:
    s = re.sub(r"//[^\n]*", " ", expr or "")
    return re.sub(r"\s+", " ", s).strip()


def cond_text(cond: str) -> str:
    s = _clean(cond)
    s = re.sub(r"ISNULL\(\s*\[([^\]]+)\]\s*\)", r"\1 is empty", s, flags=re.I)
    s = re.sub(r"NOT\s+ISNULL\(\s*\[([^\]]+)\]\s*\)", r"\1 is not empty", s, flags=re.I)
    s = re.sub(r"\[([^\]]+)\]", r"\1", s)
    for pat, rep in _OPS:
        s = re.sub(pat, rep, s)
    s = re.sub(r'"([^"]*)"', r"'\1'", s)
    s = re.sub(r"\bAND\b", "and", s)
    s = re.sub(r"\bOR\b", "or", s)
    return s


def value_text(v: str) -> str:
    v = _clean(v)
    m = re.fullmatch(r'"([^"]*)"|\'([^\']*)\'', v)
    if m:
        return f"'{m.group(1) if m.group(1) is not None else m.group(2)}'"
    v = re.sub(r"\b(?:FLOAT|INT|STR)\(\s*(\[[^\]]+\])\s*\)", r"\1", v, flags=re.I)   # FLOAT([AGE]) -> AGE
    v = re.sub(r'"([^"]*)"', r"'\1'", v)
    return re.sub(r"\[([^\]]+)\]", r"\1", v)


def _if_chain(f: str) -> list[tuple[str | None, str]] | None:
    """'IF c1 THEN v1 ELSEIF c2 THEN v2 ELSE v3 END' -> [(c1,v1),(c2,v2),(None,v3)]; None if nested."""
    s = _clean(f)
    if not re.match(r"^IF\b", s, re.I) or not re.search(r"\bEND$", s, re.I):
        return None
    body = s[2:-3].strip()
    if re.search(r"\b(IF|CASE)\b", body, re.I):     # nested: keep the plain fallback
        return None
    tokens = re.split(r"\b(THEN|ELSEIF|ELSE)\b", body, flags=re.I)
    out, cond, k = [], tokens[0], 1
    while k < len(tokens):
        kw = tokens[k].upper()
        val = tokens[k + 1] if k + 1 < len(tokens) else ""
        if kw == "THEN":
            out.append((cond.strip(), val.strip()))
        elif kw == "ELSEIF":
            cond = val
        elif kw == "ELSE":
            out.append((None, val.strip()))
        k += 2
    return out or None


def _bands(chain: list[tuple[str | None, str]]) -> str | None:
    """Turn 'IF ISNULL([X]) ... ELSEIF [X] < 1 THEN a ELSEIF [X] < 12 THEN b ...' into bands of X."""
    field, lo, parts = None, None, []
    for cond, val in chain:
        if cond is None:
            parts.append(f"{fmt_num(lo)} and above -> {value_text(val)}" if lo is not None else
                         f"anything else -> {value_text(val)}")
            continue
        m_null = re.fullmatch(r"ISNULL\(\s*\[([^\]]+)\]\s*\)", _clean(cond), re.I)
        m_lt = re.fullmatch(r"\[([^\]]+)\]\s*(<|<=)\s*(-?\d+(?:\.\d+)?)", _clean(cond))
        if m_null:
            field = field or m_null.group(1)
            if m_null.group(1) != field:
                return None
            parts.append(f"empty -> {value_text(val)}")
        elif m_lt:
            if field is not None and m_lt.group(1) != field:
                return None
            field = m_lt.group(1)
            hi = float(m_lt.group(3))
            rng = (f"below {fmt_num(hi)}" if lo is None else f"{fmt_num(lo)} to under {fmt_num(hi)}") \
                if m_lt.group(2) == "<" else (f"up to {fmt_num(hi)}" if lo is None else f"{fmt_num(lo)} to {fmt_num(hi)}")
            parts.append(f"{rng} -> {value_text(val)}")
            lo = hi
        else:
            return None
    if field is None or len(parts) < 3:
        return None
    return f"Groups {field} into bands: " + "; ".join(parts) + "."


def fmt_num(x: float | None) -> str:
    if x is None:
        return "-"
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def explain_formula(formula: str, kind: str | None = None, fields: list[str] | None = None,
                    lods: list | None = None, table_calc_functions: list[str] | None = None) -> str:
    """Plain-English description of a (readable) calculated-field formula."""
    f = _clean(formula)
    if not f:
        return "An empty calculation."
    m = re.fullmatch(r"DATE\(\s*\[([^\]]+)\]\s*\)", f, re.I)
    if m:
        return f"Turns {m.group(1)} into a date value (used when the date is stored as text)."
    m = re.fullmatch(r"DATETRUNC\(\s*'(\w+)'\s*,\s*\[([^\]]+)\]\s*\)", f, re.I)
    if m:
        return f"The {m.group(1).lower()} that {m.group(2)} falls in (dated to the first day of that {m.group(1).lower()})."
    m = re.fullmatch(r"DATEPARSE\(\s*'([^']*)'\s*,\s*\[([^\]]+)\]\s*\)", f, re.I)
    if m:
        return f"Reads {m.group(2)} as a date written in the pattern '{m.group(1)}'."
    m = re.fullmatch(r"COUNTD\(\s*IF\s+(.+?)\s+THEN\s+\[([^\]]+)\]\s*END\s*\)", f, re.I)
    if m:
        return f"Counts distinct {m.group(2)} values where {cond_text(m.group(1))}."
    m = re.fullmatch(r"(COUNTD|COUNT|SUM|AVG|MIN|MAX|MEDIAN)\(\s*\[([^\]]+)\]\s*\)", f, re.I)
    if m:
        word = {"COUNTD": "Number of distinct", "COUNT": "Number of rows with a value in", "SUM": "Total",
                "AVG": "Average", "MIN": "Lowest", "MAX": "Highest", "MEDIAN": "Median"}[m.group(1).upper()]
        return f"{word} {m.group(2)}."
    m = re.fullmatch(r"COUNT\(\s*\[([^\]]+)\]\s*\)\s*/\s*COUNTD\(\s*\[([^\]]+)\]\s*\)", f, re.I)
    if m:
        return (f"Rows with a {m.group(1)} value divided by the number of distinct {m.group(2)} - i.e. the "
                f"average number of rows per {m.group(2)} (it counts rows, so repeated rows inflate it).")
    m = re.fullmatch(r"(SUM|AVG|COUNTD|COUNT)\((.+?)\)\s*/\s*(SUM|AVG|COUNTD|COUNT)\((.+)\)", f, re.I)
    if m and "(" not in m.group(2) and "(" not in m.group(4):
        return f"A ratio: {m.group(1).lower()} of {value_text(m.group(2))} divided by {m.group(3).lower()} of {value_text(m.group(4))}."
    m = re.fullmatch(r"IF\s+(.+?)\s+THEN\s+(.+?)\s+END", f, re.I)
    if m and not re.search(r"\b(ELSEIF|ELSE|IF|CASE)\b", m.group(2), re.I) and not re.search(r"\bIF\b", m.group(1), re.I):
        return f"Equals {value_text(m.group(2))} when {cond_text(m.group(1))}; otherwise empty."
    chain = _if_chain(f)
    if chain:
        bands = _bands(chain)
        if bands:
            return bands
        steps = [(f"if {cond_text(c)} -> {value_text(v)}" if c else f"otherwise -> {value_text(v)}")
                 for c, v in chain[:8]]
        more = f" (+{len(chain) - 8} more)" if len(chain) > 8 else ""
        return "Assigns a value by rule: " + "; ".join(steps) + more + "."
    m = re.fullmatch(r"CASE\s+\[([^\]]+)\]\s+(.+?)\s+END", f, re.I)
    if m:
        whens = re.findall(r"WHEN\s+(.+?)\s+THEN\s+(.+?)(?=\s+WHEN|\s+ELSE|$)", m.group(2), re.I)
        if whens:
            ex = "; ".join(f"{value_text(a)} -> {value_text(b)}" for a, b in whens[:3])
            return f"Maps each {m.group(1)} value to a new value ({len(whens)} rules, e.g. {ex})."
    if re.search(r'\+\s*"[^"]*"\s*\+', f) and re.search(r"\bSTR\(", f, re.I):
        used = list(dict.fromkeys(re.findall(r"\[([^\]]+)\]", f)))
        return f"Builds a text label by joining {join(used)}."
    lods = lods or []
    if lods:
        lod = lods[0]
        dims = join(getattr(lod, "dimensions", []) or [])
        return (f"A level-of-detail calculation ({lod.type}): computes {value_text(lod.expression)} "
                + (f"once per {dims}" if dims else "for the whole table") + ", whatever the chart shows.")
    if table_calc_functions:
        return (f"A table calculation ({join(table_calc_functions)}): computed from the values in the chart itself "
                "(for example a running total or rank), so it changes with the chart's layout.")
    used = list(dict.fromkeys(re.findall(r"\[([^\]]+)\]", f)))
    if kind == "aggregate":
        return f"A summary calculation over {join(used) or 'the data'}: {value_text(f)}."
    if kind == "row_level":
        return f"Calculated on every row from {join(used) or 'other fields'}: {value_text(f)}."
    if kind == "constant":
        return f"A constant value: {value_text(f)}."
    return f"Calculation: {value_text(f)}."


def clean_measure(v: str) -> str:
    """'[federated.x].[sum:Sales:qk]' -> 'SUM(Sales)'; a caption is kept as it is."""
    m = re.match(r"^\[[^\]]*\]\.\[(?:([a-z]+):)?([^\]:]+)(?::[a-z]+)?\]$", v.strip())
    if not m:
        return v
    agg, fld = m.group(1), m.group(2)
    words = {"sum": "SUM", "avg": "AVG", "cnt": "COUNT", "ctd": "COUNTD", "min": "MIN", "max": "MAX"}
    return f"{words[agg]}({fld})" if agg in words else fld


def num_text(v: str) -> str:
    """'-0.2168640535982099' -> '-0.217'; dates and text unchanged."""
    try:
        x = float(v)
    except ValueError:
        return v
    if x == int(x) and abs(x) < 1e15:
        return f"{int(x):,}" if abs(x) >= 10000 else str(int(x))
    return f"{x:.3g}" if abs(x) < 1000 else f"{x:,.0f}"


def tidy(text: str) -> str:
    """Grammar after templating: 'a area' -> 'an area', 'the the' -> 'the'."""
    text = re.sub(r"\b(the) the\b", r"\1", text)
    return re.sub(r"\b([Aa]) (?=[aeiouAEIOU])", lambda m: m.group(1) + "n ", text)


TITLE_FIELD = re.compile(r"<\[[^\]]*\]\.\[(?:[a-z]+:)?([^\]:]+)(?::[a-z]+)?\]>")


def clean_title(title: str | None, captions: dict[str, str] | None = None) -> str:
    """Sheet titles can insert field values: '<[federated.x].[State]>' -> '<State>' (with its caption)."""
    if not title:
        return ""
    return TITLE_FIELD.sub(lambda m: f"<{(captions or {}).get(m.group(1), m.group(1))}>", title).strip()


def join(items: list[str], last: str = "and") -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" {last} " + items[-1]
