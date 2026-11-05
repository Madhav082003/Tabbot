"""Tableau field references and shelf expressions.

A field reference looks like `[datasource].[derivation:field:type]`. Real workbooks (docs/PATTERNS.md) use
1- to 6-part references:

    [ds].[Multiple Values]                         bare field (no derivation)          1 part
    [ds].[:Measure Names]                          special field                       2 parts
    [ds].[sum:Sales:qk]                            derivation:field:type               3 parts
    [ds].[ctd:CASEID:vtsum:qk]                     ... with a "total using" modifier   4 parts
    [ds].[rank:sum:Sales:qk]                       table calc over an aggregation      4 parts
    [ds].[avg:F: GDP per capita (curr $):qk]       field name containing a colon       4 parts
    [ds].[pcto:cnt:BMI:qk:1]                       ... with an instance number         5 parts
    [ds].[pcto:sum:F: GDP (curr $):qk:1]                                               6 parts

Names may contain `]`, escaped as `]]`. When the worksheet's `<column-instance>` list is available it is the
authoritative decode and is used to confirm the field and derivation (see parser.py).
"""
from __future__ import annotations

import re
from collections.abc import Iterator

from pydantic import BaseModel

# prefix -> (Tableau's own derivation name as written in <column-instance derivation=...>, kind)
DERIVATIONS: dict[str, tuple[str, str]] = {
    "none": ("None", "dimension"),
    # aggregations
    "sum": ("Sum", "aggregate"), "avg": ("Avg", "aggregate"), "cnt": ("Count", "aggregate"),
    "ctd": ("CountD", "aggregate"), "min": ("Min", "aggregate"), "max": ("Max", "aggregate"),
    "med": ("Median", "aggregate"), "attr": ("Attribute", "aggregate"), "std": ("Stdev", "aggregate"),
    "stp": ("StdevP", "aggregate"), "var": ("Var", "aggregate"), "vrp": ("VarP", "aggregate"),
    "usr": ("User", "user"),            # a calculated field that aggregates inside its formula
    # discrete date parts
    "yr": ("Year", "date_part"), "qr": ("Quarter", "date_part"), "mn": ("Month", "date_part"),
    "wk": ("Week", "date_part"), "dy": ("Day", "date_part"), "wd": ("Weekday", "date_part"),
    "md": ("MDY", "date_part"), "hr": ("Hour", "date_part"), "mi": ("Minute", "date_part"),
    "sc": ("Second", "date_part"), "my": ("Month/Year", "date_part"),
    # continuous date truncations
    "tyr": ("Year-Trunc", "date_trunc"), "tqr": ("Quarter-Trunc", "date_trunc"),
    "tmn": ("Month-Trunc", "date_trunc"), "twk": ("Week-Trunc", "date_trunc"),
    "tdy": ("Day-Trunc", "date_trunc"), "thr": ("Hour-Trunc", "date_trunc"),
    "tmi": ("Minute-Trunc", "date_trunc"), "tsc": ("Second-Trunc", "date_trunc"),
    # sets
    "io": ("InOut", "set"),
}
# quick table calculations that wrap an aggregation: [pcto:sum:Sales:qk]
TABLE_CALCS: dict[str, str] = {
    "pcto": "Percent of total", "pcdf": "Percent difference", "diff": "Difference", "rank": "Rank",
    "cum": "Running total", "fVal": "Table calc (fVal, meaning unconfirmed)", "mavg": "Moving average",
    "pctr": "Percentile", "ytd": "YTD total", "ytdgr": "YTD growth", "yoy": "Year over year growth",
}
TYPE_KEYS = {"nk": "nominal", "ok": "ordinal", "qk": "quantitative"}
MODIFIER_RE = re.compile(r"^vt[a-z]+$")   # "visual total" settings, e.g. vtsum = total using Sum


class FieldRef(BaseModel):
    raw: str                         # the reference as written, e.g. "[ds].[sum:Sales:qk]"
    datasource: str                  # "ds" (unescaped)
    inner: str                       # "sum:Sales:qk"
    field: str                       # internal field name, e.g. "Sales" or "Calculation_123"
    derivation: str = ""             # "sum", "none", "" for bare refs
    derivation_name: str = ""        # "Sum", "None", "Year-Trunc", "unknown"
    kind: str = "bare"               # dimension|aggregate|user|date_part|date_trunc|set|special|bare|unknown
    table_calc: str | None = None    # "pcto", "rank", ...
    table_calc_name: str | None = None
    modifiers: list[str] = []        # ["vtsum"]
    type_key: str = ""               # nk | ok | qk
    role_type: str | None = None     # nominal | ordinal | quantitative
    instance: int | None = None      # trailing ":1"
    caption: str | None = None       # filled by the parser when the field is known
    is_calculated: bool | None = None
    resolved: bool = False           # True once matched to a field of the data source

    @property
    def is_measure(self) -> bool:
        return self.kind in ("aggregate", "user") or self.table_calc is not None or self.type_key == "qk" and self.kind not in ("dimension", "date_part", "date_trunc")

    def label(self) -> str:
        """Human label like 'SUM(Sales)', 'QUARTER(Order Date)', '% of total SUM(Sales)'."""
        name = self.caption or self.field
        if self.kind == "special":
            return name
        if self.kind in ("dimension", "bare"):
            base = name
        elif self.kind == "date_trunc":
            base = f"{self.derivation_name.replace('-Trunc', '').upper()}({name}) (continuous)"
        elif self.kind == "date_part":
            base = f"{self.derivation_name.upper()}({name})"
        elif self.kind == "user":
            base = f"{name}"
        elif self.kind == "set":
            base = f"IN/OUT({name})"
        elif self.kind == "aggregate":
            base = f"{self.derivation_name.upper().replace('COUNTD', 'COUNTD')}({name})"
        else:
            base = f"{self.derivation}({name})"
        if self.table_calc:
            base = f"{self.table_calc_name} of {base}"
        return base


def unescape(name: str) -> str:
    return name.replace("]]", "]")


def scan_brackets(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield (start, end, inner) for every top-level [bracketed] token, honouring ']]' escapes."""
    i, n = 0, len(text)
    while i < n:
        if text[i] == "[":
            j = i + 1
            buf = []
            while j < n:
                if text[j] == "]":
                    if j + 1 < n and text[j + 1] == "]":
                        buf.append("]")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            yield i, j + 1, "".join(buf)
            i = j + 1
        else:
            i += 1


OBJECT_ID_MARKER = "__tableau_internal_object_id__"


def find_refs(text: str) -> list[tuple[str, str, int, int]]:
    """All `[ds].[inner]` references in text: (datasource, inner, start, end), names unescaped.

    Object-model row counts are written with three parts, `[ds].[__tableau_internal_object_id__].[cnt:Orders_ABC:qk]`;
    they are returned as one reference whose inner part is the last bracket (`cnt:Orders_ABC:qk`)."""
    toks = list(scan_brackets(text or ""))
    out = []
    k = 0
    while k < len(toks):
        s, e, inner = toks[k]
        if k + 1 < len(toks) and toks[k + 1][0] == e + 1 and text[e] == ".":
            s2, e2, inner2 = toks[k + 1]
            if (inner2 == OBJECT_ID_MARKER and k + 2 < len(toks) and toks[k + 2][0] == e2 + 1
                    and text[e2] == "."):
                s3, e3, inner3 = toks[k + 2]
                out.append((inner, inner3, s, e3))
                k += 3
                continue
            out.append((inner, inner2, s, e2))
            k += 2
        else:
            k += 1
    return out


def parse_inner(datasource: str, inner: str, raw: str | None = None) -> FieldRef:
    raw = raw or f"[{datasource}].[{inner}]"
    base = dict(raw=raw, datasource=datasource, inner=inner)
    if inner.startswith(":"):
        return FieldRef(**base, field=inner[1:], kind="special", derivation_name="Special")
    parts = inner.split(":")
    if len(parts) < 3:
        return FieldRef(**base, field=inner, kind="bare")
    instance = None
    if parts[-1].isdigit() and len(parts) >= 4 and parts[-2] in TYPE_KEYS:
        instance = int(parts.pop())
    if parts[-1] not in TYPE_KEYS:
        return FieldRef(**base, field=inner, kind="bare")
    type_key = parts.pop()
    deriv = parts.pop(0)
    table_calc = None
    if deriv in TABLE_CALCS and len(parts) >= 2 and (parts[0] in DERIVATIONS or parts[0] in TABLE_CALCS):
        table_calc, deriv = deriv, parts.pop(0)
    modifiers: list[str] = []
    while len(parts) > 1 and MODIFIER_RE.match(parts[-1]):
        modifiers.insert(0, parts.pop())
    field = ":".join(parts)
    if not field:
        return FieldRef(**base, field=inner, kind="bare")
    name, kind = DERIVATIONS.get(deriv, ("unknown", "unknown"))
    return FieldRef(**base, field=field, derivation=deriv, derivation_name=name, kind=kind,
                    table_calc=table_calc, table_calc_name=TABLE_CALCS.get(table_calc) if table_calc else None,
                    modifiers=modifiers, type_key=type_key, role_type=TYPE_KEYS[type_key], instance=instance)


def parse_ref(text: str) -> FieldRef | None:
    """Parse the first `[ds].[inner]` reference in text, or None."""
    refs = find_refs(text)
    if not refs:
        return None
    ds, inner, s, e = refs[0]
    return parse_inner(ds, inner, text[s:e])


# ---------------------------------------------------------------- shelf expressions

class ShelfNode(BaseModel):
    op: str | None = None            # "+", "/", "*" or None for a leaf
    ref: FieldRef | None = None
    items: list[ShelfNode] = []


class Shelf(BaseModel):
    raw: str
    refs: list[FieldRef] = []        # in reading order
    tree: ShelfNode | None = None
    error: str | None = None

    @property
    def dimensions(self) -> list[FieldRef]:
        return [r for r in self.refs if not r.is_measure]

    @property
    def measures(self) -> list[FieldRef]:
        return [r for r in self.refs if r.is_measure]

    def describe(self) -> str:
        def walk(n: ShelfNode) -> str:
            if n.ref is not None:
                return n.ref.label()
            sep = {"+": " + ", "/": " › ", "*": " × "}.get(n.op or "", " ")
            inner = sep.join(walk(i) for i in n.items)
            return inner if len(n.items) == 1 else f"({inner})" if n.op == "+" else inner
        if self.tree is not None:
            return walk(self.tree)
        return ", ".join(r.label() for r in self.refs)


def _tokenize_shelf(text: str) -> list[tuple[str, object]]:
    toks: list[tuple[str, object]] = []
    i, n = 0, len(text)
    refs = {s: (ds, inner, e) for ds, inner, s, e in find_refs(text)}
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif i in refs:
            ds, inner, e = refs[i]
            toks.append(("REF", parse_inner(ds, inner, text[i:e])))
            i = e
        elif c in "()+/*":
            toks.append((c, c))
            i += 1
        else:
            raise ValueError(f"unexpected character {c!r} at {i}")
    return toks


def parse_shelf(text: str | None) -> Shelf:
    """Parse a Rows/Columns shelf. Grammar: expr := term ('+' term)*; term := factor (('/'|'*') factor)*;
    factor := REF | '(' expr ')'. On any surprise, fall back to the flat list of references."""
    raw = (text or "").strip()
    if not raw:
        return Shelf(raw="")
    try:
        toks = _tokenize_shelf(raw)
    except ValueError as exc:
        flat = [parse_inner(ds, inner, raw[s:e]) for ds, inner, s, e in find_refs(raw)]
        return Shelf(raw=raw, refs=flat, tree=None, error=str(exc))
    flat = [t[1] for t in toks if t[0] == "REF"]     # the SAME objects the tree holds, so captions show in both
    try:
        pos = 0

        def peek():
            return toks[pos][0] if pos < len(toks) else None

        def factor() -> ShelfNode:
            nonlocal pos
            t = peek()
            if t == "REF":
                node = ShelfNode(ref=toks[pos][1])
                pos += 1
                return node
            if t == "(":
                pos += 1
                node = expr()
                if peek() != ")":
                    raise ValueError("missing )")
                pos += 1
                return node
            raise ValueError(f"unexpected token {t!r}")

        def term() -> ShelfNode:
            nonlocal pos
            node = factor()
            while peek() in ("/", "*"):
                op = toks[pos][0]
                pos += 1
                rhs = factor()
                node = ShelfNode(op=op, items=(node.items if node.op == op else [node]) + [rhs])
            return node

        def expr() -> ShelfNode:
            nonlocal pos
            node = term()
            while peek() == "+":
                pos += 1
                rhs = term()
                node = ShelfNode(op="+", items=(node.items if node.op == "+" else [node]) + [rhs])
            return node

        tree = expr()
        if pos != len(toks):
            raise ValueError("trailing tokens")
        return Shelf(raw=raw, refs=flat, tree=tree)
    except ValueError as exc:
        return Shelf(raw=raw, refs=flat, tree=None, error=str(exc))
