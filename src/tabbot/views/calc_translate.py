"""Translate Tableau calculated-field formulas into DuckDB SQL expressions.

Covers the common subset: literals, field references, arithmetic/comparison/logic, IF/ELSEIF/ELSE/END,
CASE/WHEN, IIF, date functions (DATE, DATETRUNC, DATEPART, YEAR...), null handling, string and math functions,
and aggregates (SUM, COUNT, COUNTD, AVG, MIN, MAX, MEDIAN, ATTR ...).
Anything else (LOD expressions, table calculations, parameters, unknown functions) raises Unsupported:
we never guess.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


class Unsupported(Exception):
    """The formula uses something we cannot translate exactly."""


@dataclass
class Expr:
    sql: str
    is_agg: bool = False
    is_str: bool = False
    is_date: bool = False
    text_date_format: str | None = None   # set when the expression is a text column holding dates


# --------------------------------------------------------------------------------------------- tokenizer

TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*)
  | (?P<string>"(?:[^"]|"")*"|'(?:[^']|'')*')
  | (?P<field>\[(?:[^\]]|\]\])+\](?:\.\[(?:[^\]]|\]\])+\])?)
  | (?P<date>\#[^#]+\#)
  | (?P<number>\d+\.\d*|\.\d+|\d+)
  | (?P<op><=|>=|<>|!=|==|=|<|>|\+|-|\*|/|%|\^)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<lbrace>\{)
  | (?P<rbrace>\})
  | (?P<colon>:)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
""", re.X)


def tokenize(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise Unsupported(f"cannot read formula near {text[pos:pos + 15]!r}")
        kind = m.lastgroup
        if kind == "lbrace":
            raise Unsupported("level-of-detail expression ({FIXED/INCLUDE/EXCLUDE ...})")
        if kind not in ("ws", "comment"):
            out.append((kind, m.group()))
        pos = m.end()
    return out


# --------------------------------------------------------------------------------------------- functions

AGGS = {"SUM": "SUM", "AVG": "AVG", "MEDIAN": "MEDIAN", "COUNT": "COUNT", "STDEV": "STDDEV_SAMP",
        "STDEVP": "STDDEV_POP", "VAR": "VAR_SAMP", "VARP": "VAR_POP"}
TABLE_CALC_FUNCS = {"RUNNING_SUM", "RUNNING_AVG", "RUNNING_COUNT", "RUNNING_MAX", "RUNNING_MIN", "WINDOW_SUM",
                    "WINDOW_AVG", "WINDOW_MAX", "WINDOW_MIN", "WINDOW_COUNT", "WINDOW_MEDIAN", "LOOKUP",
                    "PREVIOUS_VALUE", "INDEX", "FIRST", "LAST", "SIZE", "RANK", "RANK_DENSE", "RANK_MODIFIED",
                    "RANK_UNIQUE", "RANK_PERCENTILE", "TOTAL", "WINDOW_STDEV", "WINDOW_VAR", "WINDOW_PERCENTILE"}
DATE_PARTS = {"year", "quarter", "month", "week", "day", "hour", "minute", "second", "weekday", "dayofyear",
              "iso-year", "iso-week"}
ICU_TO_STRPTIME = [("yyyy", "%Y"), ("yy", "%y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"), ("dd", "%d"),
                   ("HH", "%H"), ("hh", "%I"), ("mm", "%M"), ("ss", "%S"), ("a", "%p")]


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _part(e: Expr) -> str:
    m = re.fullmatch(r"'([A-Za-z-]+)'", e.sql)
    if not m or m.group(1).lower() not in DATE_PARTS:
        raise Unsupported(f"date part must be a literal like 'quarter', got {e.sql}")
    return m.group(1).lower()


def _to_date(e: Expr, kind: str = "DATE") -> Expr:
    if e.text_date_format:
        return Expr(f"CAST(try_strptime({e.sql}, {_sql_str(e.text_date_format)}) AS {kind})", is_agg=e.is_agg,
                    is_date=True)
    if e.is_date:
        return Expr(f"CAST({e.sql} AS {kind})", is_agg=e.is_agg, is_date=True)
    return Expr(f"TRY_CAST({e.sql} AS {kind})", is_agg=e.is_agg, is_date=True)


def _icu(fmt: str) -> str:
    out = fmt
    for a, b in ICU_TO_STRPTIME:
        out = out.replace(a, b)
    return out


def call(name: str, args: list[Expr]) -> Expr:
    u = name.upper()
    n = len(args)
    agg = any(a.is_agg for a in args)

    def need(k: int | tuple[int, ...]) -> None:
        ks = (k,) if isinstance(k, int) else k
        if n not in ks:
            raise Unsupported(f"{u} expects {k} argument(s), got {n}")

    a = [x.sql for x in args]
    if u in TABLE_CALC_FUNCS:
        raise Unsupported(f"table calculation function {u}")
    if u in AGGS:
        need(1)
        return Expr(f"{AGGS[u]}({a[0]})", is_agg=True)
    if u == "COUNTD":
        need(1)
        return Expr(f"COUNT(DISTINCT {a[0]})", is_agg=True)
    if u in ("MIN", "MAX"):
        if n == 1:
            return Expr(f"{u}({a[0]})", is_agg=True, is_str=args[0].is_str, is_date=args[0].is_date)
        need(2)
        return Expr(f"{'LEAST' if u == 'MIN' else 'GREATEST'}({a[0]}, {a[1]})", is_agg=agg)
    if u == "ATTR":
        need(1)
        return Expr(f"CASE WHEN MIN({a[0]}) = MAX({a[0]}) THEN MIN({a[0]}) END", is_agg=True, is_str=args[0].is_str)
    if u in ("DATE", "DATETIME"):
        need(1)
        return _to_date(args[0], "DATE" if u == "DATE" else "TIMESTAMP")
    if u == "DATEPARSE":
        need(2)
        fmt = re.fullmatch(r"'(.*)'", a[0], re.S)
        if not fmt:
            raise Unsupported("DATEPARSE format must be a literal")
        return Expr(f"try_strptime({a[1]}, {_sql_str(_icu(fmt.group(1).replace(chr(39) * 2, chr(39))))})",
                    is_agg=agg, is_date=True)
    if u == "DATETRUNC":
        need((2, 3))
        d = _to_date(args[1], "TIMESTAMP")
        part = _part(args[0])
        if part not in ("year", "quarter", "month", "week", "day", "hour", "minute", "second"):
            raise Unsupported(f"DATETRUNC part {part}")
        trunc = f"date_trunc('{part}', {d.sql})"
        as_date = part in ("year", "quarter", "month", "week", "day")
        return Expr(f"CAST({trunc} AS DATE)" if as_date else trunc, is_agg=agg, is_date=True)
    if u == "DATEPART":
        need((2, 3))
        part = _part(args[0])
        d = _to_date(args[1], "TIMESTAMP").sql
        sqlpart = {"weekday": "dayofweek", "iso-year": "isoyear", "iso-week": "week", "dayofyear": "dayofyear"}
        expr = f"date_part('{sqlpart.get(part, part)}', {d})"
        return Expr(f"({expr} + 1)" if part == "weekday" else expr, is_agg=agg)
    if u == "DATENAME":
        need((2, 3))
        part = _part(args[0])
        d = _to_date(args[1], "TIMESTAMP").sql
        fmt = {"month": "%B", "weekday": "%A"}.get(part)
        return Expr(f"strftime({d}, '{fmt}')" if fmt else f"CAST(date_part('{part}', {d}) AS VARCHAR)",
                    is_agg=agg, is_str=True)
    if u == "DATEADD":
        need(3)
        part = _part(args[0])
        d = _to_date(args[2], "TIMESTAMP").sql
        unit = {"quarter": ("month", 3), "week": ("day", 7)}.get(part, (part, 1))
        return Expr(f"({d} + ({a[1]}) * {unit[1]} * INTERVAL 1 {unit[0].upper()})", is_agg=agg, is_date=True)
    if u == "DATEDIFF":
        need((3, 4))
        part = _part(args[0])
        return Expr(f"date_diff('{part}', {_to_date(args[1], 'TIMESTAMP').sql}, {_to_date(args[2], 'TIMESTAMP').sql})",
                    is_agg=agg)
    if u in ("YEAR", "QUARTER", "MONTH", "DAY", "WEEK"):
        need(1)
        return Expr(f"{u.lower()}({_to_date(args[0], 'TIMESTAMP').sql})", is_agg=agg)
    if u == "TODAY":
        return Expr("current_date", is_date=True)
    if u == "NOW":
        return Expr("now()", is_date=True)
    if u == "ISNULL":
        need(1)
        return Expr(f"({a[0]} IS NULL)", is_agg=agg)
    if u == "IFNULL":
        need(2)
        return Expr(f"COALESCE({a[0]}, {a[1]})", is_agg=agg, is_str=args[0].is_str)
    if u == "ZN":
        need(1)
        return Expr(f"COALESCE({a[0]}, 0)", is_agg=agg)
    if u == "IIF":
        need((3, 4))
        unknown = a[3] if n == 4 else "NULL"
        return Expr(f"CASE WHEN {a[0]} THEN {a[1]} WHEN NOT ({a[0]}) THEN {a[2]} ELSE {unknown} END",
                    is_agg=agg, is_str=args[1].is_str)
    simple = {"UPPER": "upper", "LOWER": "lower", "TRIM": "trim", "LTRIM": "ltrim", "RTRIM": "rtrim",
              "LEN": "length", "ABS": "abs", "CEILING": "ceil", "FLOOR": "floor", "SQRT": "sqrt", "LN": "ln",
              "EXP": "exp", "SIGN": "sign"}
    if u in simple:
        need(1)
        return Expr(f"{simple[u]}({a[0]})", is_agg=agg, is_str=u in ("UPPER", "LOWER", "TRIM", "LTRIM", "RTRIM"))
    if u == "ROUND":
        need((1, 2))
        return Expr(f"round({a[0]}, {a[1] if n == 2 else 0})", is_agg=agg)
    if u in ("POWER",):
        need(2)
        return Expr(f"power({a[0]}, {a[1]})", is_agg=agg)
    if u == "LOG":
        need((1, 2))
        return Expr(f"log({a[0]})" if n == 1 else f"(ln({a[0]}) / ln({a[1]}))", is_agg=agg)
    if u == "DIV":
        need(2)
        return Expr(f"({a[0]} // {a[1]})", is_agg=agg)
    if u in ("LEFT", "RIGHT"):
        need(2)
        return Expr(f"{u.lower()}({a[0]}, CAST({a[1]} AS BIGINT))", is_agg=agg, is_str=True)
    if u == "MID":
        need((2, 3))
        return Expr(f"substr({a[0]}, CAST({a[1]} AS BIGINT)" + (f", CAST({a[2]} AS BIGINT))" if n == 3 else ")"),
                    is_agg=agg, is_str=True)
    if u in ("CONTAINS", "STARTSWITH", "ENDSWITH"):
        need(2)
        fn = {"CONTAINS": "contains", "STARTSWITH": "starts_with", "ENDSWITH": "ends_with"}[u]
        return Expr(f"{fn}({a[0]}, {a[1]})", is_agg=agg)
    if u == "REPLACE":
        need(3)
        return Expr(f"replace({a[0]}, {a[1]}, {a[2]})", is_agg=agg, is_str=True)
    if u == "SPLIT":
        need(3)
        return Expr(f"split_part({a[0]}, {a[1]}, CAST({a[2]} AS BIGINT))", is_agg=agg, is_str=True)
    if u == "FIND":
        need((2, 3))
        return Expr(f"strpos({a[0]}, {a[1]})", is_agg=agg)
    if u == "STR":
        need(1)
        return Expr(f"CAST({a[0]} AS VARCHAR)", is_agg=agg, is_str=True)
    if u == "INT":
        need(1)
        return Expr(f"CAST(trunc(TRY_CAST({a[0]} AS DOUBLE)) AS BIGINT)", is_agg=agg)
    if u == "FLOAT":
        need(1)
        return Expr(f"TRY_CAST({a[0]} AS DOUBLE)", is_agg=agg)
    raise Unsupported(f"function {u}")


# --------------------------------------------------------------------------------------------- parser

Resolver = Callable[[str], Expr]


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], resolve: Resolver):
        self.t, self.i, self.resolve = tokens, 0, resolve

    def peek(self, k: int = 0) -> tuple[str, str] | None:
        j = self.i + k
        return self.t[j] if j < len(self.t) else None

    def kw(self, word: str) -> bool:
        p = self.peek()
        return p is not None and p[0] == "ident" and p[1].upper() == word

    def take(self, kind: str | None = None, value: str | None = None) -> tuple[str, str]:
        p = self.peek()
        if p is None:
            raise Unsupported("formula ended unexpectedly")
        if (kind and p[0] != kind) or (value and p[1].upper() != value):
            raise Unsupported(f"unexpected {p[1]!r} in formula")
        self.i += 1
        return p

    def parse(self) -> Expr:
        e = self.expr()
        if self.peek() is not None:
            raise Unsupported(f"unexpected {self.peek()[1]!r} after expression")
        return e

    def expr(self) -> Expr:
        return self.or_()

    def or_(self) -> Expr:
        e = self.and_()
        while self.kw("OR"):
            self.take()
            r = self.and_()
            e = Expr(f"({e.sql} OR {r.sql})", is_agg=e.is_agg or r.is_agg)
        return e

    def and_(self) -> Expr:
        e = self.not_()
        while self.kw("AND"):
            self.take()
            r = self.not_()
            e = Expr(f"({e.sql} AND {r.sql})", is_agg=e.is_agg or r.is_agg)
        return e

    def not_(self) -> Expr:
        if self.kw("NOT"):
            self.take()
            e = self.not_()
            return Expr(f"(NOT {e.sql})", is_agg=e.is_agg)
        return self.cmp()

    def cmp(self) -> Expr:
        e = self.add()
        p = self.peek()
        if p and p[0] == "op" and p[1] in ("=", "==", "<>", "!=", "<", "<=", ">", ">="):
            self.take()
            r = self.add()
            op = {"==": "=", "!=": "<>"}.get(p[1], p[1])
            return Expr(f"({e.sql} {op} {r.sql})", is_agg=e.is_agg or r.is_agg)
        return e

    def add(self) -> Expr:
        e = self.mul()
        while (p := self.peek()) and p[0] == "op" and p[1] in ("+", "-"):
            self.take()
            r = self.mul()
            if p[1] == "+" and (e.is_str or r.is_str):
                e = Expr(f"({e.sql} || {r.sql})", is_agg=e.is_agg or r.is_agg, is_str=True)
            else:
                e = Expr(f"({e.sql} {p[1]} {r.sql})", is_agg=e.is_agg or r.is_agg)
        return e

    def mul(self) -> Expr:
        e = self.unary()
        while (p := self.peek()) and p[0] == "op" and p[1] in ("*", "/", "%", "^"):
            self.take()
            r = self.unary()
            sql = f"power({e.sql}, {r.sql})" if p[1] == "^" else \
                f"({e.sql} / NULLIF({r.sql}, 0))" if p[1] == "/" else f"({e.sql} {p[1]} {r.sql})"
            e = Expr(sql, is_agg=e.is_agg or r.is_agg)
        return e

    def unary(self) -> Expr:
        p = self.peek()
        if p and p[0] == "op" and p[1] == "-":
            self.take()
            e = self.unary()
            return Expr(f"(-{e.sql})", is_agg=e.is_agg)
        return self.atom()

    def atom(self) -> Expr:
        p = self.take()
        kind, val = p
        if kind == "number":
            return Expr(val)
        if kind == "string":
            q = val[0]
            return Expr(_sql_str(val[1:-1].replace(q * 2, q)), is_str=True)
        if kind == "date":
            return Expr(f"TRY_CAST({_sql_str(val.strip('#'))} AS DATE)", is_date=True)
        if kind == "field":
            if "].[" in val:
                raise Unsupported(f"reference to another data source or parameter {val}")
            return self.resolve(val[1:-1].replace("]]", "]"))
        if kind == "lparen":
            e = self.expr()
            self.take("rparen")
            return Expr(f"({e.sql})", is_agg=e.is_agg, is_str=e.is_str, is_date=e.is_date)
        if kind == "ident":
            u = val.upper()
            if u in ("TRUE", "FALSE"):
                return Expr(u)
            if u == "NULL":
                return Expr("NULL")
            if u == "IF":
                return self.if_()
            if u == "CASE":
                return self.case_()
            if self.peek() and self.peek()[0] == "lparen":
                self.take()
                args: list[Expr] = []
                if not (self.peek() and self.peek()[0] == "rparen"):
                    args.append(self.expr())
                    while self.peek() and self.peek()[0] == "comma":
                        self.take()
                        args.append(self.expr())
                self.take("rparen")
                return call(val, args)
        raise Unsupported(f"unexpected {val!r} in formula")

    def if_(self) -> Expr:
        parts, agg, is_str = [], False, False
        cond = self.expr()
        self.take("ident", "THEN")
        res = self.expr()
        parts.append(f"WHEN {cond.sql} THEN {res.sql}")
        agg, is_str = cond.is_agg or res.is_agg, res.is_str
        else_sql = None
        while True:
            if self.kw("ELSEIF"):
                self.take()
                c = self.expr()
                self.take("ident", "THEN")
                r = self.expr()
                parts.append(f"WHEN {c.sql} THEN {r.sql}")
                agg, is_str = agg or c.is_agg or r.is_agg, is_str or r.is_str
            elif self.kw("ELSE"):
                self.take()
                r = self.expr()
                else_sql, agg, is_str = r.sql, agg or r.is_agg, is_str or r.is_str
            else:
                break
        self.take("ident", "END")
        sql = "CASE " + " ".join(parts) + (f" ELSE {else_sql}" if else_sql is not None else "") + " END"
        return Expr(sql, is_agg=agg, is_str=is_str)

    def case_(self) -> Expr:
        subject = self.expr()
        parts, agg, is_str, else_sql = [], subject.is_agg, False, None
        while self.kw("WHEN"):
            self.take()
            v = self.expr()
            self.take("ident", "THEN")
            r = self.expr()
            parts.append(f"WHEN {v.sql} THEN {r.sql}")
            agg, is_str = agg or r.is_agg, is_str or r.is_str
        if self.kw("ELSE"):
            self.take()
            r = self.expr()
            else_sql, is_str = r.sql, is_str or r.is_str
        self.take("ident", "END")
        if not parts:
            raise Unsupported("CASE without WHEN")
        sql = f"CASE {subject.sql} " + " ".join(parts) + (f" ELSE {else_sql}" if else_sql else "") + " END"
        return Expr(sql, is_agg=agg, is_str=is_str)


def translate(formula: str, resolve: Resolver) -> Expr:
    """Translate one formula. `resolve(name)` returns the Expr for a field reference [name]."""
    if formula is None or not formula.strip():
        raise Unsupported("empty formula")
    return _Parser(tokenize(formula), resolve).parse()
