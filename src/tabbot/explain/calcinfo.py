"""What a calculated field produces and what a measure really counts - read from the formula, no data needed.

value_map(formula)  CASE [X] WHEN ... / IF [X] = ... ELSEIF ... chains that map one field's values to labels:
                    every rule, the possible outputs, and what happens to values no rule lists (no ELSE ->
                    they become Null and show as their own unlabelled group).
counts_what(...)    what a measure on a chart counts: rows, distinct values, or a sum - following calculated
                    fields and LOD expressions down to the base aggregation (SUM of a 1/0 flag counts rows).

Works on readable formulas (fields written as [Caption]); nested or unusual formulas return None so the
caller falls back to the plain formula.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_COMMENT = re.compile(r"//[^\n]*")
_LIT = r'"[^"]*"|\'[^\']*\'|-?\d+(?:\.\d+)?|NULL'
_FIELD = r"\[((?:[^\]]|\]\])+)\]"


def _clean(f: str | None) -> str:
    return re.sub(r"\s+", " ", _COMMENT.sub(" ", f or "")).strip()


def _lit(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _out(v: str) -> str | None:
    """A THEN/ELSE value as shown: a literal's text, a field's name in brackets, anything else as written."""
    v = v.strip()
    if re.fullmatch(_LIT, v, re.I):
        return None if v.upper() == "NULL" else _lit(v)
    return v


def _bare(s: str) -> str:
    """The formula without its strings and [field names], so keyword checks never match inside them."""
    return re.sub(rf"\"[^\"]*\"|'[^']*'|{_FIELD}", " ", s)


def _split(body: str, words: str) -> list[str]:
    """Split on keywords outside quotes and brackets."""
    parts = re.split(rf'("[^"]*"|\'[^\']*\'|\[(?:[^\]]|\]\])*\]|\b(?:{words})\b)', body, flags=re.I)
    out, cur = [], ""
    for p in parts:
        if p and re.fullmatch(rf"(?:{words})", p, re.I):
            out += [cur, p.upper()]
            cur = ""
        else:
            cur += p or ""
    return out + [cur]


def _case(f: str) -> dict | None:
    m = re.fullmatch(rf"CASE\s+{_FIELD}\s+(WHEN\b.+)\s+END", f, re.I)
    if not m or re.search(r"\b(CASE|IF|IIF)\b", _bare(m.group(2)), re.I):
        return None
    toks = _split(m.group(2), "WHEN|THEN|ELSE")
    rules, other, k = [], "", 1
    while k < len(toks) - 1:
        kw, val = toks[k], toks[k + 1].strip()
        if kw == "WHEN":
            if k + 3 >= len(toks) or toks[k + 2] != "THEN":
                return None
            whens = [w for w in re.split(r"\s*,\s*", val) if w]
            if not all(re.fullmatch(_LIT, w, re.I) for w in whens):
                return None
            rules.append(([_lit(w) for w in whens], _out(toks[k + 3])))
            k += 4
        elif kw == "ELSE":
            other = val
            k += 2
        else:
            return None
    return _result(m.group(1).replace("]]", "]"), rules, other) if rules else None


def _cond_values(cond: str, field: str | None) -> tuple[str, list[str]] | None:
    """'[X] = "a" OR [X] = "b"' or '[X] IN ("a", "b")' -> ('X', ['a', 'b'])."""
    vals, fld = [], field
    for part in re.split(r"\s+OR\s+", cond.strip(), flags=re.I):
        part = part.strip()
        if part.startswith("(") and part.endswith(")") and not re.search(r"\bIN\s*\(", part, re.I):
            part = part[1:-1].strip()
        m = re.fullmatch(rf"{_FIELD}\s*==?\s*({_LIT})", part, re.I)
        m_in = re.fullmatch(rf"{_FIELD}\s+IN\s*\((.+)\)", part, re.I)
        if m:
            name, got = m.group(1), [_lit(m.group(2))]
        elif m_in:
            got = [w for w in re.split(r"\s*,\s*", m_in.group(2)) if w]
            if not all(re.fullmatch(_LIT, w, re.I) for w in got):
                return None
            name, got = m_in.group(1), [_lit(w) for w in got]
        else:
            return None
        name = name.replace("]]", "]")
        if fld is not None and name != fld:
            return None
        fld = name
        vals += got
    return (fld, vals) if fld else None


def _if(f: str) -> dict | None:
    m = re.fullmatch(r"IF\s+(.+)\s+END", f, re.I)
    if not m or re.search(r"\b(CASE|IIF|IF)\b", _bare(m.group(1)), re.I):
        return None
    toks = _split(m.group(1), "THEN|ELSEIF|ELSE")
    rules, other, field, cond, k = [], "", None, toks[0], 1
    while k < len(toks) - 1:
        kw, val = toks[k], toks[k + 1]
        if kw == "THEN":
            cv = _cond_values(cond, field)
            if cv is None:
                return None
            field = cv[0]
            rules.append((cv[1], _out(val)))
        elif kw == "ELSEIF":
            cond = val
        elif kw == "ELSE":
            other = val.strip()
        k += 2
    return _result(field, rules, other) if rules and field else None


def _result(field: str, rules: list[tuple[list[str], str | None]], other: str) -> dict:
    outputs = list(dict.fromkeys(o for _, o in rules if o is not None))
    if not other:
        rest = (f"no ELSE: any other {field} value (including empty or a different spelling) becomes Null - "
                "on a chart it shows as its own unlabelled group unless a filter removes it")
    elif re.fullmatch(_FIELD, other) and other[1:-1].replace("]]", "]") == field:
        rest = f"any other {field} value is kept unchanged"
    else:
        o = _out(other)
        rest = f"any other {field} value becomes {o!r}" if o is not None else f"any other {field} value becomes Null"
        if o is not None and o not in outputs:
            outputs.append(o)
    merged = [o for o in outputs if sum(len(w) for w, x in rules if x == o) > 1]
    out = {"maps_field": field,
           "rules": [{"when": w if len(w) > 1 else w[0], "then": o} for w, o in rules],
           "possible_values": outputs + (["Null"] if not other or _out(other) is None else []),
           "other_values": rest}
    if merged:
        out["merged"] = [f"{o} <- " + ", ".join(v for w, x in rules if x == o for v in w) for o in merged]
    return out


def value_map(formula: str | None) -> dict | None:
    f = _clean(formula)
    if not f:
        return None
    return _case(f) if re.match(r"CASE\b", f, re.I) else _if(f) if re.match(r"IF\b", f, re.I) else None


# ------------------------------------------------------------------ what a measure counts

_FLAG = re.compile(r"IF\s+(.+?)\s+THEN\s+1\s+ELSE\s+0\s+END|IIF\(\s*(.+?)\s*,\s*1\s*,\s*0\s*\)", re.I)


def _is_flag(formula: str, lookup: Callable[[str], str | None], depth: int = 0) -> bool:
    """A row-level 1/0 flag, or a sum of such flags ([A] + [B])."""
    f = _clean(formula)
    if _FLAG.fullmatch(f):
        return True
    names = re.fullmatch(rf"{_FIELD}(?:\s*\+\s*{_FIELD})*", f)
    if names and depth < 5:
        refs = [r.replace("]]", "]") for r in re.findall(_FIELD, f)]
        return all((lookup(r) is not None) and _is_flag(lookup(r) or "", lookup, depth + 1) for r in refs)
    return False


def counts_what(aggregation: str | None, field: str, lookup: Callable[[str], str | None], depth: int = 0) -> str | None:
    """What AGG(field) counts. aggregation: SUM, COUNT, COUNTD, ... or None for an aggregate calculated field.
    lookup(caption) -> that field's readable formula, or None for a plain data column."""
    if depth > 6:
        return None
    agg = (aggregation or "").upper()
    formula = lookup(field)
    if agg == "COUNTD":
        return f"distinct {field} values (each counted once)"
    if agg == "COUNT":
        return (f"rows with a {field} value - every row counts, so a {field} that appears on several rows is "
                "counted several times")
    if agg == "SUM" and formula and _is_flag(formula, lookup):
        return f"rows (SUM of the 1/0 flag {field}), not distinct records"
    if agg or not formula:
        return None
    f = _clean(formula)                         # an aggregate calculation: look inside it
    m = re.fullmatch(r"\{\s*(?:FIXED|INCLUDE|EXCLUDE)\b[^:]*:\s*(.+)\}", f, re.I)
    if m:
        f = m.group(1).strip()
    m = re.fullmatch(rf"(COUNTD|COUNT|SUM)\(\s*{_FIELD}\s*\)", f, re.I)
    if m:
        return counts_what(m.group(1), m.group(2).replace("]]", "]"), lookup, depth + 1)
    m = re.fullmatch(rf"{_FIELD}", f)
    if m:
        return counts_what(None, m.group(1).replace("]]", "]"), lookup, depth + 1)
    return None
