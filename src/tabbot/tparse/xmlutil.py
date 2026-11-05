"""Safe loading and normalisation of Tableau workbook XML.

Two quirks found across the corpus (see docs/PATTERNS.md) are handled here, once, so the rest of the parser can
use plain tag and attribute names:

1. Feature-flag prefixes. Tableau may write a tag or attribute as `_.fcp.<Feature>.<true|false>...<real-name>`,
   sometimes nested (`_.fcp.A.false..._.fcp.B.true...style-rule`). The same element can appear twice, once per
   flag state (e.g. `_.fcp.ObjectModelEncapsulateLegacy.false...relation` and `...true...relation`).
   We strip the prefixes and, where a true/false pair of siblings exists, keep the `true` variant
   (the state current Tableau versions write). Attributes: a `true` variant overrides the plain name; a `false`
   variant fills it only if nothing else does.
2. Namespaced attributes. `{http://www.tableausoftware.com/xml/user}ui-marker` becomes `user:ui-marker`.

All parsing goes through defusedxml (no external entities, no entity expansion bombs).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as DET

FCP_RE = re.compile(r"^_\.fcp\.([A-Za-z0-9_]+)\.(true|false)\.\.\.")
USER_NS = "{http://www.tableausoftware.com/xml/user}"
XML_NS = "{http://www.w3.org/XML/1998/namespace}"


@dataclass
class NormalizationReport:
    feature_flags: dict[str, int] = field(default_factory=dict)      # "Feature.true" -> count
    dropped_false_variants: int = 0
    renamed_tags: int = 0
    renamed_attrs: int = 0


def strip_fcp(name: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (real_name, [(feature, 'true'|'false'), ...]) for a possibly flag-prefixed name."""
    flags = []
    while True:
        m = FCP_RE.match(name)
        if not m:
            return name, flags
        flags.append((m.group(1), m.group(2)))
        name = name[m.end():]


def _variant_state(flags: list[tuple[str, str]]) -> str:
    """'true' if every flag is true, 'false' if any is false, '' if unflagged."""
    if not flags:
        return ""
    return "false" if any(state == "false" for _, state in flags) else "true"


def _normalize_attrs(el: Element, rep: NormalizationReport) -> None:
    if not el.attrib:
        return
    new: dict[str, str] = {}
    weak: dict[str, str] = {}
    for key, value in el.attrib.items():
        k = key
        if k.startswith(USER_NS):
            k = "user:" + k[len(USER_NS):]
        elif k.startswith(XML_NS):
            k = "xml:" + k[len(XML_NS):]
        real, flags = strip_fcp(k)
        for f, s in flags:
            rep.feature_flags[f"{f}.{s}"] = rep.feature_flags.get(f"{f}.{s}", 0) + 1
        state = _variant_state(flags)
        if real != key:
            rep.renamed_attrs += 1
        if state == "false":
            weak.setdefault(real, value)
        elif state == "true":
            new[real] = value
        else:
            new.setdefault(real, value)
    for k, v in weak.items():
        new.setdefault(k, v)
    el.attrib.clear()
    el.attrib.update(new)


def normalize(el: Element, rep: NormalizationReport | None = None) -> NormalizationReport:
    """Normalise tag and attribute names in place, recursively."""
    rep = rep or NormalizationReport()
    _normalize_attrs(el, rep)
    children = list(el)
    info = []
    for ch in children:
        real, flags = strip_fcp(ch.tag)
        for f, s in flags:
            rep.feature_flags[f"{f}.{s}"] = rep.feature_flags.get(f"{f}.{s}", 0) + 1
        info.append((ch, real, _variant_state(flags)))
    true_tags = {real for _, real, state in info if state == "true"}
    for ch, real, state in info:
        if state == "false" and real in true_tags:
            el.remove(ch)
            rep.dropped_false_variants += 1
            continue
        if ch.tag != real:
            ch.tag = real
            rep.renamed_tags += 1
        normalize(ch, rep)
    return rep


def load_twb(source: str | Path | bytes) -> tuple[Element, NormalizationReport, int]:
    """Parse a .twb (path or bytes) safely and normalise it. Returns (root, report, size_in_bytes)."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    root = DET.fromstring(data)
    rep = normalize(root)
    return root, rep, len(data)


LINE_BREAK_MARKER = re.compile(r"Æ(?=\s|$)")


def text_of(el: Element | None) -> str:
    """All text inside an element (e.g. a formatted title made of several <run>s), whitespace-collapsed.

    In formatted text Tableau writes a line break as the character 'Æ' followed by a newline, space or tab
    (seen in 450+ runs across the corpus). It is a layout marker, not content, so it is removed here."""
    if el is None:
        return ""
    raw = LINE_BREAK_MARKER.sub(" ", "".join(el.itertext()))
    return re.sub(r"\s+", " ", raw).strip()


def first(el: Element | None, path: str) -> Element | None:
    return None if el is None else el.find(path)


def children(el: Element | None, path: str) -> list[Element]:
    return [] if el is None else el.findall(path)


def strip_quotes(value: str | None) -> str | None:
    """Tableau stores string members as '"Value"' and nulls as '%null%'."""
    if value is None:
        return None
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('""', '"')
    return value


def unescape_member(value: str) -> str:
    """Filter member values escape '%' (because '%null%' is the null marker) and '\\' with a backslash:
    '[ds].[usr:\\% Diff Sales:qk]' means '[ds].[usr:% Diff Sales:qk]'."""
    return re.sub(r"\\(.)", r"\1", value)
