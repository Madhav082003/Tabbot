"""Analyse Tableau calculation formulas without evaluating them.

A small tokenizer for the Tableau calculation language: comments (`//`), string literals ('..' or "..", doubled
quotes escape), field references (`[Field]`, `[ds].[Field]`, `[Parameters].[P]`, with `]]` escapes),
date literals (`#2020-01-01#`), LOD braces and operators. From the tokens we derive what a calculation uses
(functions, fields, parameters, LOD expressions, table-calc functions) and what kind of calculation it is.

Naive regexes over raw formulas mis-count field names such as `[GDP (curr $)]` as function calls; the miner
showed this (docs/PATTERNS.md), which is why everything here works on tokens.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel

AGG_FUNCS = {"SUM", "AVG", "COUNT", "COUNTD", "MIN", "MAX", "MEDIAN", "ATTR", "STDEV", "STDEVP", "VAR", "VARP",
             "PERCENTILE", "COLLECT", "CORR", "COVAR", "COVARP"}
TABLE_CALC_FUNCS = {"WINDOW_SUM", "WINDOW_AVG", "WINDOW_MAX", "WINDOW_MIN", "WINDOW_COUNT", "WINDOW_MEDIAN",
                    "WINDOW_STDEV", "WINDOW_STDEVP", "WINDOW_VAR", "WINDOW_VARP", "WINDOW_PERCENTILE",
                    "WINDOW_CORR", "WINDOW_COVAR", "WINDOW_COVARP", "RUNNING_SUM", "RUNNING_AVG",
                    "RUNNING_COUNT", "RUNNING_MAX", "RUNNING_MIN", "LOOKUP", "PREVIOUS_VALUE", "INDEX", "FIRST",
                    "LAST", "SIZE", "TOTAL", "RANK", "RANK_DENSE", "RANK_MODIFIED", "RANK_PERCENTILE",
                    "RANK_UNIQUE", "SCRIPT_BOOL", "SCRIPT_INT", "SCRIPT_REAL", "SCRIPT_STR"}
KEYWORDS = {"IF", "THEN", "ELSE", "ELSEIF", "END", "CASE", "WHEN", "AND", "OR", "NOT", "IN", "TRUE", "FALSE",
            "NULL", "FIXED", "INCLUDE", "EXCLUDE"}
DATE_FUNCS = {"DATE", "DATETIME", "DATEPARSE", "DATETRUNC", "DATEPART", "DATENAME", "DATEADD", "DATEDIFF",
              "MAKEDATE", "MAKEDATETIME", "TODAY", "NOW", "YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "ISDATE"}


@dataclass
class Token:
    kind: str      # FIELD, QFIELD, STRING, NUMBER, DATE, IDENT, OP, LBRACE, RBRACE, LPAREN, RPAREN, COMMA, COLON
    value: str
    pos: int
    ds: str | None = None


def tokenize(formula: str) -> list[Token]:
    toks: list[Token] = []
    s, i, n = formula or "", 0, len(formula or "")

    def read_bracket(j: int) -> tuple[str, int]:
        buf = []
        j += 1
        while j < n:
            if s[j] == "]":
                if j + 1 < n and s[j + 1] == "]":
                    buf.append("]")
                    j += 2
                    continue
                return "".join(buf), j + 1
            buf.append(s[j])
            j += 1
        return "".join(buf), n

    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif s.startswith("//", i):
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "'\"":
            q, j, buf = c, i + 1, []
            while j < n:
                if s[j] == q:
                    if j + 1 < n and s[j + 1] == q:
                        buf.append(q)
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            toks.append(Token("STRING", "".join(buf), i))
            i = j + 1
        elif c == "[":
            name, j = read_bracket(i)
            if j < n and s[j] == "." and j + 1 < n and s[j + 1] == "[":
                name2, k = read_bracket(j + 1)
                toks.append(Token("QFIELD", name2, i, ds=name))
                i = k
            else:
                toks.append(Token("FIELD", name, i))
                i = j
        elif c == "#":
            j = s.find("#", i + 1)
            j = n if j < 0 else j
            toks.append(Token("DATE", s[i + 1:j], i))
            i = j + 1
        elif c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            m = re.match(r"\d*\.?\d+(?:[eE][+-]?\d+)?", s[i:])
            toks.append(Token("NUMBER", m.group(0), i))
            i += m.end()
        elif c.isalpha() or c == "_":
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", s[i:])
            toks.append(Token("IDENT", m.group(0), i))
            i += m.end()
        elif c in "{}":
            toks.append(Token("LBRACE" if c == "{" else "RBRACE", c, i))
            i += 1
        elif c in "()":
            toks.append(Token("LPAREN" if c == "(" else "RPAREN", c, i))
            i += 1
        elif c == ",":
            toks.append(Token("COMMA", c, i))
            i += 1
        elif c == ":":
            toks.append(Token("COLON", c, i))
            i += 1
        else:
            m = re.match(r"<=|>=|<>|!=|==|&&|\|\||[-+*/%^=<>!&|]", s[i:])
            toks.append(Token("OP", m.group(0) if m else c, i))
            i += m.end() if m else 1
    return toks


class LodExpression(BaseModel):
    type: str                     # FIXED | INCLUDE | EXCLUDE | TABLE (no keyword = FIXED at table level)
    dimensions: list[str] = []
    expression: str = ""


class FormulaInfo(BaseModel):
    formula: str
    kind: str                     # constant | row_level | aggregate | lod | table_calc | empty
    functions: list[str] = []     # distinct, upper-case
    aggregations: list[str] = []
    table_calc_functions: list[str] = []
    date_functions: list[str] = []
    fields: list[str] = []        # referenced field names (internal), excluding parameters
    parameters: list[str] = []    # referenced parameter names
    qualified_fields: list[tuple[str, str]] = []   # ([ds], [field]) references other than Parameters
    lods: list[LodExpression] = []
    has_conditional: bool = False
    string_literals: int = 0
    comment_lines: int = 0


def analyze(formula: str | None) -> FormulaInfo:
    f = formula or ""
    if not f.strip():
        return FormulaInfo(formula=f, kind="empty")
    toks = tokenize(f)
    funcs, aggs, tcs, dfs, fields, params, qfields = [], [], [], [], [], [], []
    cond = False
    strings = 0
    for k, t in enumerate(toks):
        if t.kind == "IDENT":
            up = t.value.upper()
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            if up in ("IF", "CASE", "IIF"):
                cond = True
            if nxt is not None and nxt.kind == "LPAREN" and up not in KEYWORDS:
                funcs.append(up)
                if up in AGG_FUNCS:
                    aggs.append(up)
                if up in TABLE_CALC_FUNCS:
                    tcs.append(up)
                if up in DATE_FUNCS:
                    dfs.append(up)
                if up in ("IIF", "IFNULL", "ZN", "ISNULL"):
                    cond = True
        elif t.kind == "FIELD":
            fields.append(t.value)
        elif t.kind == "QFIELD":
            if t.ds == "Parameters":
                params.append(t.value)
            else:
                qfields.append((t.ds or "", t.value))
                fields.append(t.value)
        elif t.kind == "STRING":
            strings += 1

    lods = _find_lods(f, toks)
    dedup = lambda xs: list(dict.fromkeys(xs))  # noqa: E731
    if tcs:
        kind = "table_calc"
    elif lods:
        kind = "lod"
    elif _has_agg_outside_lod(toks):
        kind = "aggregate"
    elif fields or qfields:
        kind = "row_level"
    else:
        kind = "constant"
    return FormulaInfo(
        formula=f, kind=kind, functions=dedup(funcs), aggregations=dedup(aggs), table_calc_functions=dedup(tcs),
        date_functions=dedup(dfs), fields=dedup(fields), parameters=dedup(params), qualified_fields=dedup(qfields),
        lods=lods, has_conditional=cond, string_literals=strings,
        comment_lines=sum(1 for line in f.splitlines() if line.strip().startswith("//")),
    )


def _find_lods(src: str, toks: list[Token]) -> list[LodExpression]:
    out = []
    for k, t in enumerate(toks):
        if t.kind != "LBRACE":
            continue
        depth, end = 0, None
        for j in range(k, len(toks)):
            if toks[j].kind == "LBRACE":
                depth += 1
            elif toks[j].kind == "RBRACE":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        body = toks[k + 1:end] if end else toks[k + 1:]
        if body and body[0].kind == "IDENT" and body[0].value.upper() in ("FIXED", "INCLUDE", "EXCLUDE"):
            typ = body[0].value.upper()
            colon = next((j for j, b in enumerate(body) if b.kind == "COLON"), None)
            dims = [b.value for b in body[1:colon] if b.kind in ("FIELD", "QFIELD")] if colon else []
            start = body[colon + 1].pos if colon is not None and colon + 1 < len(body) else body[0].pos
        else:
            typ, dims = "TABLE", []
            start = body[0].pos if body else t.pos + 1
        stop = toks[end].pos if end else len(src)
        out.append(LodExpression(type=typ, dimensions=dims, expression=src[start:stop].strip()))
    return out


def _has_agg_outside_lod(toks: list[Token]) -> bool:
    depth = 0
    for k, t in enumerate(toks):
        if t.kind == "LBRACE":
            depth += 1
        elif t.kind == "RBRACE":
            depth = max(0, depth - 1)
        elif depth == 0 and t.kind == "IDENT" and t.value.upper() in AGG_FUNCS:
            if k + 1 < len(toks) and toks[k + 1].kind == "LPAREN":
                return True
    return False


def readable(formula: str | None, captions: dict[str, str]) -> str:
    """Replace internal names inside [brackets] with captions, e.g. [Calculation_123] -> [Parsed Date]."""
    if not formula:
        return ""

    def sub(m: re.Match) -> str:
        inner = m.group(1).replace("]]", "]")
        cap = captions.get(inner)
        return f"[{cap.replace(']', ']]')}]" if cap else m.group(0)
    return re.sub(r"\[((?:[^\]]|\]\])+)\]", sub, formula)
