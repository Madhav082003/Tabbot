"""Compose the written explanation of a workbook (no LLM).

Sections: In brief · What this workbook is about · What is in the data · How the workbook is organised ·
What stands out in the data · The dashboards, one by one · Calculated fields · Problems to fix ·
Questions to explore next. Every number is computed locally (tabbot.explain.facts / tabbot.analysis).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import pandas as pd

from tabbot.analysis import recipes as rc
from tabbot.analysis.lints import run_lints
from tabbot.explain import facts as F
from tabbot.explain.glossary import TERMS, detect_subject, norm
from tabbot.explain.phrases import Phraser, clean_title, explain_formula, how_to_read, join, n_, pct, plural
from tabbot.explain.structure import DashboardGroup, Family, dashboard_groups, main_sheet, sheet_families
from tabbot.views.display import fmt_dim

RECORD_WORDS = ["case", "patient", "order", "customer", "employee", "encounter", "visit", "admission", "claim",
                "transaction", "invoice", "account", "ticket", "incident", "product", "student", "subject"]
PERIODS_BY_GRAIN = {"quarter": 4, "month": 12, "year": 1, "week": 13, "day": 30}
TINY_VIEW = 30          # views with this many records or fewer: list the cells instead of trends
SMALL_PIE = 30


@dataclass
class Section:
    title: str
    body: str
    key: str = ""


@dataclass
class DashboardView:
    """One dashboard (or one view split over several dashboard pages), explained on its own."""
    name: str                      # shown on its button
    pages: list[str]               # the dashboard(s) it is made of, as named in the workbook
    sheets: list[str]              # worksheets on those dashboards
    summary: str                   # two or three plain sentences
    body: str                      # the in-depth explanation (markdown)
    questions: list[str] = field(default_factory=list)


@dataclass
class Report:
    workbook: str
    sections: list[Section] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    dashboards: list[DashboardView] = field(default_factory=list)

    def dashboard(self, name: str) -> DashboardView | None:
        return next((d for d in self.dashboards if d.name == name), None)

    def to_markdown(self) -> str:
        return "\n\n".join(f"## {s.title}\n\n{s.body}" for s in self.sections)

    def to_dict(self) -> dict:
        """Plain data (for saving with a chat, so the explanation can be shown when the workbook is closed)."""
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Report:
        return cls(workbook=d.get("workbook", ""),
                   sections=[Section(**s) for s in d.get("sections", [])],
                   questions=list(d.get("questions", [])),
                   dashboards=[DashboardView(**v) for v in d.get("dashboards", [])])

    def section(self, key: str) -> Section | None:
        return next((s for s in self.sections if s.key == key), None)


def _d(x: dt.date | None) -> str:
    return f"{x:%d %b %Y}" if x else "-"


def _record_word(entity: str | None, subject) -> str:
    if not entity:
        return "row"            # nothing is counted distinctly: one row of the data is one record
    n = norm(entity or "")
    for w in RECORD_WORDS:
        if w in n:
            return w
    return subject.record_word if subject else "record"


def _change(a: float, b: float) -> str:
    if not a:
        return f"{a:,.0f} → {b:,.0f}"
    return f"{a:,.0f} → {b:,.0f} ({(b - a) / a * 100:+.0f}%)"


def _win(label: str) -> str:
    """'2025–2025' -> '2025' (one-period windows)."""
    a, sep, b = str(label).partition("–")
    return a if sep and a == b else str(label)


def _val(v) -> str:
    return "(empty)" if v is None or str(v) in ("Null", "None", "nan", "%null%") else str(v)


class Writer:
    def __init__(self, lw):
        self.lw = lw
        self.kit = lw.kit
        self.b = F.basics(lw)
        names = [f.caption or f.name for d in self.kit.datasources for f in d.fields] + \
                [p.remote_name or "" for d in self.kit.datasources for p in d.physical_fields]
        extra = " ".join([d.caption or "" for d in self.kit.datasources] + [d.name for d in self.kit.dashboards])
        self.subject = detect_subject(names, extra)
        self.word = _record_word(self.b.entity if self.b else None, self.subject)
        dtypes = {(f.datasource, f.name): (f.datatype or "") for d in self.kit.datasources for f in d.fields}
        for d in self.kit.datasources:
            for p in d.physical_fields:
                nm = (p.local_name or "").strip("[]")
                if nm:
                    dtypes.setdefault((d.name, nm), p.local_type or "")
        self.ph = Phraser(self.word, self.b.entity if self.b else None, dtypes)
        self.captions = {f.name: f.caption for d in self.kit.datasources for f in d.fields if f.caption}
        self.families = sheet_families(self.kit)
        self.groups = dashboard_groups(self.kit, self.families)
        self.lints = run_lints(lw)
        self.ctx = F.workbook_context(lw, self.b) if self.b else None
        self.questions: list[str] = []
        self.brief: dict[str, str] = {}
        self.dash_info: dict[str, dict] = {}
        self.views: list[DashboardView] = []

    # ------------------------------------------------------------------ helpers
    def records(self, n: float) -> str:
        return n_(n, self.word)

    def column_names(self) -> set[str]:
        return {c.name for c in self.lw.profile_for(self.b.datasource).columns} if self.b else set()

    def caption_of(self, col: str | None) -> str:
        if not col:
            return ""
        for d in self.kit.datasources:
            for f in d.fields:
                if f.name == col and f.caption:
                    return f.caption
        return col

    def filter_text(self, fd, ds: str | None) -> str | None:
        vals = None
        if fd.field.kind in ("dimension", "bare") and fd.mode in ("include", "exclude") and \
                fd.field.field in self.column_names():
            vals = F.values_in_data(self.lw, ds, fd.field.field)
        return self.ph.filter(fd, vals)

    def terms_used(self) -> list[str]:
        """Glossary entries for terms that appear in field names or dashboard titles of this workbook."""
        words = set()
        for d in self.kit.datasources:
            for f in d.fields:
                words.add(norm(f.caption or f.name))
            for p in d.physical_fields:
                words.add(norm(p.remote_name or ""))
        for d in self.kit.dashboards:
            words |= {w.lower() for w in re.findall(r"\b[A-Z]{2,5}\b", d.name)}
        out = []
        for key, text in TERMS.items():
            if key in words and (len(key) <= 5 or key in {norm(x) for x in self.b.varying + self.b.constant}
                                 if self.b else len(key) <= 5):
                out.append(f"**{key.upper() if len(key) <= 5 else self.caption_of(key.upper())}** - {text}")
        return out

    # ------------------------------------------------------------------ about / data / organisation
    def about(self) -> Section:
        m, b = self.kit, self.b
        dss = [d for d in m.datasources if not d.is_parameters]
        parts = []
        if dss:
            d0 = next((d for d in dss if b and d.name == b.datasource), dss[0])
            conn = sorted({c.cls for c in d0.connections if c.cls and c.cls != "federated"})
            src = {"excel-direct": "an Excel file", "textscan": "a text/CSV file", "hyper": "a Tableau extract",
                   "sqlproxy": "a published Tableau data source", "sqlserver": "a SQL Server database",
                   "postgres": "a PostgreSQL database", "mysql": "a MySQL database",
                   "google-sheets": "a Google Sheet", "dataengine": "an old-style Tableau extract (.tde)"}
            how = join([src.get(c, f"a {c} connection") for c in conn]) or "a data source"
            table = (f" (table *{d0.relation.name}*)" if d0.relation and d0.relation.name
                     and d0.relation.type == "table" else "")
            packaged = (", and a copy of the data is packaged inside the workbook file"
                        if d0.extract and d0.extract.enabled and self.lw.tables else "")
            subj = f"This workbook looks like **{self.subject.label}**. " if self.subject else ""
            parts.append(f"{subj}It is built on the data source **{d0.caption or d0.name}**, which comes from "
                         f"{how}{table}{packaged}.")
            if len(dss) > 1:
                parts.append(f"It also uses {len(dss) - 1} other data source(s): "
                             f"{join([d.caption or d.name for d in dss if d is not d0][:4])}.")
        if b:
            verb = "received" if "receiv" in (b.date_col or "").lower() else "dated"
            span = (f", {verb} between {_d(b.date_from)} and {_d(b.date_to)} (by {self.caption_of(b.date_col)})"
                    if b.date_from else "")
            ent = f" (counted by {b.entity})" if b.entity else ""
            parts.append(f"It holds **{self.records(b.entities)}**{ent}{span}.")
            if b.entity and b.rows_per_entity > 1.2:
                vary = join([self.caption_of(c) for c in b.varying[:4]])
                fixed = join([self.caption_of(c) for c in b.constant[:4]])
                parts.append(
                    f"Each {self.word} takes up about **{b.rows_per_entity:.0f} rows** of the data"
                    + (f", because {vary} can change from row to row within one {self.word}" if vary else "")
                    + (f", while {fixed} stay the same on every row of a {self.word}" if fixed else "")
                    + f". That is why the charts count distinct {plural(self.word)} rather than rows - counting rows "
                      f"would overstate the numbers about {b.rows_per_entity:.0f} times.")
            self.brief["data"] = (f"{(self.subject.label.capitalize() + ': ') if self.subject else ''}"
                                  f"**{self.records(b.entities)}**"
                                  + (f" from {_d(b.date_from)} to {_d(b.date_to)}" if b.date_from else "") + ".")
        elif not self.lw.tables:
            parts.append("The file carries no data extract, so this explanation covers its structure only.")
            self.brief["data"] = "Structure only: the file carries no data extract."
        body = " ".join(parts)
        terms = self.terms_used()
        if terms:
            body += "\n\nTerms used in this workbook:\n\n" + "\n".join(f"- {t}" for t in terms)
        return Section("What this workbook is about", body, "about")

    def data(self) -> Section | None:
        b = self.b
        if not b:
            return None
        prof = self.lw.profile_for(b.datasource)
        kds = F._kit_ds(self.lw, b.datasource)
        usage = {f.name: len(f.used_by) for f in kds.fields} if kds else {}
        cands = [c for c in prof.columns if c.kind == "categorical" and c.name not in b.sensitive
                 and c.name != b.entity and c.distinct >= 2 and usage.get(c.name, 0)]
        if b.category and b.category not in [c.name for c in cands]:
            cands.insert(0, prof.column(b.category))
        cands.sort(key=lambda c: (c.name != b.category, -usage.get(c.name, 0), c.name))
        lines = []
        for c in cands[:7]:
            top, distinct = F.breakdown(self.lw, b, c.name, 4)
            per = "" if c.entity_level else f" (a {self.word} can have several)"
            vals = "; ".join(f"{_val(v)} {n:,} ({pct(n, b.entities)})" for v, n in top)
            more = f"; … {distinct - len(top)} more" if distinct > len(top) else ""
            lines.append(f"- **{self.caption_of(c.name)}** - {distinct} values{per}: {vals}{more}")
        if not lines:
            return None
        sens = [c for c in sorted(b.sensitive) if c != b.entity]
        tail = (f"\n\nFields that look like identifiers, free text or personal details ({join(sens[:6])}"
                f"{'…' if len(sens) > 6 else ''}) are never shown as values here or used to answer questions.") if sens else ""
        intro = (f"The main ways the data is split, with the number of {plural(self.word)} for the largest values "
                 f"(shares are out of all {self.records(b.entities)}; where a {self.word} can have several values "
                 "the shares add up to more than 100%):")
        return Section("What is in the data", intro + "\n\n" + "\n".join(lines) + tail, "data")

    def organisation(self) -> Section:
        m = self.kit
        n_ws, n_db = len(m.worksheets), len(m.dashboards)
        parts = [f"The workbook has **{n_db} dashboard{'s' if n_db != 1 else ''}** built from "
                 f"**{n_ws} worksheet{'s' if n_ws != 1 else ''}**."]
        fam_sheets = sum(len(f.sheets) for f in self.families)
        if self.families:
            items = []
            for f in self.families:
                what = self.ph.what_it_shows(f.example)
                if f.varying_field:
                    what = re.sub(rf" for {re.escape(f.varying_field)} = .+$", "", what)
                    items.append(f"- **{len(f.sheets)} × {f.example.chart_type}**, one per {f.varying_field}: {what}.")
                else:
                    items.append(f"- **{len(f.sheets)} × {f.example.chart_type}** with the same design: {what}.")
            parts.append(f"Most of the sheets repeat a few designs ({fam_sheets} of {n_ws}):\n\n" + "\n".join(items))
            if n_ws - fam_sheets:
                parts.append(f"The other {n_ws - fam_sheets} sheet{'s are' if n_ws - fam_sheets != 1 else ' is'} "
                             "one-off views.")
        split = [g for g in self.groups if len(g.dashboards) > 1]
        if split:
            parts.append(f"{len(split)} of the views are split over several dashboards (pages): " + join(
                [f"*{g.name}* ({' + '.join(str(len(d.sheets)) for d in g.dashboards)} charts)" for g in split])
                + f". So there are really **{len(self.groups)} different views**.")
        hidden = len(m.hidden_worksheets)
        if hidden:
            vis = [w.name for w in m.worksheets if not w.hidden]
            parts.append(f"{hidden} of the {n_ws} worksheets are hidden - they are seen only inside dashboards"
                         + (f"; {join(vis[:3])} can also be opened on its own" if vis and len(vis) <= 3 else "") + ".")
        qf = [(g.name, sorted({r.caption or r.field for d in g.dashboards for r in d.quick_filters}))
              for g in self.groups]
        qf = [(n, f) for n, f in qf if f]
        params = [p.caption or p.name for p in m.parameters]
        inter = []
        if qf:
            inter.append("quick filters on " + join([f"*{n}* ({join(f)})" for n, f in qf]))
        if params:
            inter.append(f"{len(params)} parameter(s) ({join(params[:4])})")
        if m.actions:
            inter.append(f"{len(m.actions)} dashboard action(s) ({join(sorted({a.kind for a in m.actions}))}): "
                         "clicking one chart changes others")
        parts.append(("Interactive parts: " + "; ".join(inter) + ".") if inter else
                     "There are no quick filters, parameters or click actions: the dashboards are static views.")
        fam_txt = ""
        if self.families:
            per = sorted({f.varying_field for f in self.families if f.varying_field})
            fam_txt = (f"; {fam_sheets} of the sheets repeat {len(self.families)} chart design(s)"
                       + (f", one per {join(per)}" if per else ""))
        self.brief["organisation"] = (f"{n_db} dashboards ({len(self.groups)} different views) built from "
                                      f"{n_ws} worksheets{fam_txt}.")
        return Section("How the workbook is organised", "\n\n".join(parts), "organisation")

    # ------------------------------------------------------------------ insights
    def recent_jump(self, ctx) -> str | None:
        full = rc.full_periods(ctx)
        if len(full) < 4:
            return None
        tail = full.tail(8).reset_index(drop=True)
        diffs = tail["value"].diff()
        i = int(diffs.idxmax()) if diffs.notna().any() else None
        if i is None or i == 0:
            return None
        prev, cur = float(tail.loc[i - 1, "value"]), float(tail.loc[i, "value"])
        if prev <= 0 or cur / prev < 1.3 or cur - prev < 5:
            return None
        after = tail.loc[i + 1:, ["label", "value"]].head(2)
        stay = ("; it stayed high afterwards (" + ", ".join(f"{r['label']}: {r['value']:,.0f}"
                                                            for _, r in after.iterrows()) + ")") if len(after) else ""
        return (f"**Biggest recent jump:** {tail.loc[i, 'label']}, from {prev:,.0f} to {cur:,.0f} "
                f"{plural(self.word)} in one quarter ({(cur - prev) / prev * 100:+.0f}%){stay}.")

    def insights(self) -> Section | None:
        b, ctx = self.b, self.ctx
        if not b or not ctx:
            return None
        lines = []
        years = F.per_year(self.lw, b)
        if years:
            lines.append(f"**Volume.** {plural(self.word).capitalize()} per year (by {self.caption_of(b.date_col)}): "
                         + ", ".join(f"{y}: {n:,}" for y, n in years)
                         + (f" ({years[-1][0]} only up to {_d(b.date_to)})" if b.date_to and b.date_to.month < 12
                            else "") + ".")
        tr = rc.trend(ctx)
        pc = rc.period_compare(ctx)
        if pc.ok and pc.summary and pc.summary["previous"]:
            s = pc.summary
            lines.append(f"**Recent change.** The last four full quarters ({s['last_window']}) had "
                         f"{self.records(s['last'])}, against {s['previous']:,.0f} in the four before "
                         f"({s['previous_window']}): {_change(s['previous'], s['last'])}.")
            self.brief["change"] = (f"Volume {'up' if s['last'] >= s['previous'] else 'down'} "
                                    f"{abs(s['change_pct']):.0f}% in the last four full quarters: "
                                    f"{s['previous']:,.0f} → {s['last']:,.0f} {plural(self.word)}.")
        jump = self.recent_jump(ctx)
        if jump:
            lines.append(jump)
            q = re.search(r"\*\* ([^,]+),", jump)
            if q:
                self.questions.append(f"What changed in {q.group(1)} - where do the extra {plural(self.word)} "
                                      "come from?")
        if tr.ok and tr.summary.get("partial_last_period"):
            p = tr.summary["partial_last_period"]
            lines.append(f"**Incomplete last quarter.** The data ends on {_d(b.date_to)}, so {p['period']} has "
                         f"{p['value']:,.0f} {plural(self.word)} so far - about {p['full_period_pace']:,.0f} at the same "
                         "pace. Its lower bar is not a drop.")
        if b.category and pc.ok and not pc.table.empty:
            t, key = pc.table, pc.table.columns[0]
            ups = t[t["change"] > 0].head(4)
            if len(ups):
                lines.append(f"**Fastest-growing {self.caption_of(b.category)} values** (last 4 vs previous 4 full "
                             "quarters): " + "; ".join(f"{r[key]} {_change(r['previous'], r['last'])}"
                                                       for _, r in ups.iterrows()) + ".")
            downs = t[t["change"] < 0]
            if len(downs):
                lines.append("Falling: " + "; ".join(f"{r[key]} {_change(r['previous'], r['last'])}"
                                                     for _, r in downs.tail(3).iloc[::-1].iterrows()) + ".")
            co = rc.concentration(ctx)
            if co.ok and co.summary.get("sparse_members"):
                lines.append(f"{co.summary['sparse_members']} {self.caption_of(b.category)} values have "
                             f"{co.summary['sparse_threshold']:,.0f} {plural(self.word)} or fewer in total: their "
                             "quarter-by-quarter numbers are too small to read trends from.")
        nocat = rc.AnalysisContext(**{**ctx.__dict__, "category_sql": None, "category_label": None})
        dr = rc.driver_scan(nocat)
        if dr.ok and not dr.table.empty:
            strong = dr.table[dr.table["excess"] >= 0.15].head(3)
            if len(strong):
                s = dr.summary
                # the overall direction decides the words: a fall must never be called an increase
                tot = pc.summary if pc.ok and pc.summary else None
                up = (tot["last"] >= tot["previous"]) if tot else True
                change_word = "increase" if up else "decrease"
                other_move = "fell" if up else "rose"
                items = []
                for _, r in strong.iterrows():
                    over = (f" - more than the whole {change_word}, because {plural(self.word)} with other "
                            f"{self.caption_of(r['field'])} values {other_move} at the same time"
                            if r["share_of_change"] > 1.05 else "")
                    if not r["previous"]:
                        over += " (this value did not occur at all in the earlier period)"
                    items.append(f"**{self.caption_of(r['field'])} = {_val(r['value'])}**: {r['previous']:,.0f} → "
                                 f"{r['last']:,.0f}, i.e. {r['share_of_change']:.0%} of the change although it was "
                                 f"{r['baseline_share']:.0%} of the earlier total{over}")
                lines.append(f"**What the change is made of** ({s['last_window']} vs {s['previous_window']}, all "
                             f"fields scanned):\n\n" + "\n".join(f"- {x}" for x in items))
                r0 = strong.iloc[0]
                f0, v0 = self.caption_of(r0["field"]), _val(r0["value"])
                if v0 == "(empty)" and up:
                    lines.append(f"*Possible reading:* much of the increase has no {f0} value, which usually means "
                                 f"newer {plural(self.word)} are not fully processed or coded yet. Compare recent "
                                 f"periods on {f0} with care, and confirm with the data owner.")
                elif r0["excess"] >= 0.4:
                    lines.append(f"*Possible reading:* most of the change comes through one {f0} value, which points "
                                 f"to a change in where or how {plural(self.word)} arrive (for example a new source "
                                 f"or programme) rather than an across-the-board {'rise' if up else 'fall'}. Worth "
                                 "confirming with the data owner.")
                if v0 == "(empty)" and up:
                    self.brief["driver"] = (f"Most of the increase is in {plural(self.word)} with no {f0} value "
                                            f"({r0['previous']:,.0f} → {r0['last']:,.0f}): newer "
                                            f"{plural(self.word)} look less fully coded.")
                else:
                    self.brief["driver"] = (f"Most of the {change_word} comes with {f0} = {v0} "
                                            f"({r0['previous']:,.0f} → {r0['last']:,.0f}).")
                self.questions.append(
                    f"Why do so many recent {plural(self.word)} have no {f0} value?" if v0 == "(empty)" and up
                    else f"Why did {plural(self.word)} with {f0} = {v0} {'grow' if up else 'fall'} so much?")
        if b.category:
            mu = rc.multiplicity(ctx)
            if mu.ok and mu.summary and mu.summary.get("first"):
                s = mu.summary
                change = (s["last"] - s["first"]) / s["first"]
                if abs(change) >= 0.2:
                    word = "fewer" if change < 0 else "more"
                    lines.append(f"**Each {self.word} lists {word} {self.caption_of(b.category)} values than before:** "
                                 f"{s['first']:.2f} on average in {s['first_year']}, {s['last']:.2f} in "
                                 f"{s['last_year']}. *Possible reading:* the share of each {self.caption_of(b.category)} "
                                 f"value {'falls' if change < 0 else 'rises'} partly for that reason, not only because "
                                 f"the events themselves became {'rarer' if change < 0 else 'more common'}.")
        seen_fields = set()
        for cap, fld, val in F.flag_calcs(self.lw, b):
            if fld in seen_fields:
                continue          # 'Non-serious' after 'Serious' only repeats the complement
            seen_fields.add(fld)
            fs = rc.flag_share(ctx, fld, val)
            if not fs.ok or len(fs.table) < 2:
                continue
            sh = fs.table["share"]
            if sh.max() - sh.min() < 0.03:
                continue
            series = ", ".join(f"{r['share']:.0%} ({int(r['year'])})" for _, r in fs.table.iterrows())
            lines.append(f"**{cap}** (the workbook's own calculation: {fld} = {val}) as a share of {plural(self.word)} "
                         f"by year: {series}.")
        return Section("What stands out in the data", "\n\n".join(lines), "insights") if lines else None

    # ------------------------------------------------------------------ dashboards
    def family_numbers(self, fam: Family) -> list[str]:
        out = []
        chart = fam.example.chart_type or ""
        rebuilt = [(w, F.rebuild(self.lw, w.name)) for w in fam.sheets]
        rebuilt = [(w, r) for w, r in rebuilt if r is not None and r.status != "failed" and not r.data.empty]
        if not rebuilt:
            return out

        def value(w):
            return next((fd.members[0] for fd in w.filters if fd.member_count == 1
                         and (fd.field.caption or fd.field.field) == fam.varying_field), w.name)

        if chart.startswith(("pie", "donut")):
            tops = []
            for w, r in rebuilt:
                dim = next((d for d in r.dims if d.role in ("color", "lod", "detail")), r.dims[0] if r.dims else None)
                m = r.measures[0] if r.measures else None
                if dim is None or m is None:
                    continue
                s = r.data.groupby(dim.alias)[m.alias].sum().sort_values(ascending=False)
                tot = float(s.sum())
                if tot > 0:
                    tops.append((value(w), _val(s.index[0]), float(s.iloc[0]) / tot, tot))
            if tops:
                lead = pd.Series([t[1] for t in tops]).value_counts()
                v0, cnt = lead.index[0], int(lead.iloc[0])
                big = [t for t in tops if t[1] == v0 and t[3] >= SMALL_PIE] or [t for t in tops if t[1] == v0]
                lo, hi = min(big, key=lambda t: t[2]), max(big, key=lambda t: t[2])
                out.append(f"**{v0}** is the largest slice in {cnt} of the {len(tops)} pies"
                           + (f"; among pies with at least {SMALL_PIE} {plural(self.word)} its share ranges from "
                              f"{lo[2]:.0%} ({lo[0]}) to {hi[2]:.0%} ({hi[0]})" if len(big) > 1 else "") + ".")
                if len(lead) > 1:
                    out.append("Other pies are led by " + join([f"{v} ({int(c)})" for v, c in lead.iloc[1:4].items()])
                               + ".")
                sizes = sorted(((t[0], t[3]) for t in tops), key=lambda t: -t[1])
                small = [s for s in sizes if s[1] < SMALL_PIE]
                out.append("Largest pies: " + join([f"{v} ({n:,.0f})" for v, n in sizes[:3]]) + "."
                           + (f" {len(small)} pies hold fewer than {SMALL_PIE} {plural(self.word)}, so their splits "
                              f"rest on very few {plural(self.word)}." if small else ""))
        elif chart.startswith(("line", "area")):
            changes = []
            for w, r in rebuilt:
                t = next((d for d in r.dims if d.kind == "date_trunc"), None)
                m = r.measures[0] if r.measures else None
                if t is not None and m is not None:
                    ch = F.series_change(r.data, t.alias, m.alias, self.b.date_to if self.b else None)
                    if ch:
                        changes.append((value(w), ch[0], ch[1]))
            if changes:
                ups = sorted([c for c in changes if c[2] > c[1]], key=lambda c: -(c[2] - c[1]))[:3]
                downs = sorted([c for c in changes if c[2] < c[1]], key=lambda c: c[2] - c[1])[:3]
                if ups:
                    out.append("Biggest rises (last 4 full quarters vs the 4 before): "
                               + "; ".join(f"{v} {_change(a, b)}" for v, a, b in ups) + ".")
                if downs:
                    out.append("Falls: " + "; ".join(f"{v} {_change(a, b)}" for v, a, b in downs) + ".")
        return out

    def _counts_records(self, res) -> bool:
        return bool(res.measures) and res.measures[0].aggregation == "count_distinct" and \
            res.measures[0].ref.field == (self.b.entity if self.b else None)

    def sheet_numbers(self, ws_name: str) -> list[str]:
        res = F.rebuild(self.lw, ws_name)
        if res is None or res.status == "failed" or res.data.empty or not res.measures:
            return []
        out = []
        m = res.measures[0]
        dt_ = res.checks.get("distinct_total") or {}
        total = dt_.get("distinct_total")
        what = self.ph_measure(res)
        if total is not None:
            if self._counts_records(res):
                out.append(f"In total the view covers **{total:,}** {plural(self.word)}.")
            else:
                out.append(f"In total: **{total:,}** ({what}).")
        if total is not None and total <= TINY_VIEW:
            cells = res.data.copy()
            for d in res.dims:
                cells[d.alias] = fmt_dim(cells[d.alias], d)
            nz = cells[pd.to_numeric(cells[m.alias], errors="coerce") > 0]
            if 0 < len(nz) <= 12:
                out.append("Every non-empty cell: " + "; ".join(
                    f"{' '.join(str(x) for x in row[[d.alias for d in res.dims]])}: {row[m.alias]:,.0f}"
                    for _, row in nz.iterrows()) + ".")
            return out
        try:
            ctx = rc.context_for(res, self.lw.store, self.lw.profile_for(res.datasource))
        except ValueError:
            ctx = None
        if ctx and res.time_dims():
            n = PERIODS_BY_GRAIN.get(ctx.grain, 4)
            pc = rc.period_compare(ctx, n=n)
            if pc.ok and pc.summary and pc.summary["previous"]:
                s = pc.summary
                out.append(f"{_win(s['last_window'])} vs {_win(s['previous_window'])}: "
                           f"{_change(s['previous'], s['last'])}.")
                if not pc.table.empty and ctx.category_label:
                    key = pc.table.columns[0]
                    ups = pc.table[pc.table["change"] > 0].head(3)
                    if len(ups):
                        out.append(f"Biggest increases by {ctx.category_label}: " + "; ".join(
                            f"{_val(r[key])} {_change(r['previous'], r['last'])}" for _, r in ups.iterrows()) + ".")
                    downs = pc.table[pc.table["change"] < 0].tail(3)
                    if len(downs):
                        out.append("Decreases: " + "; ".join(
                            f"{_val(r[key])} {_change(r['previous'], r['last'])}"
                            for _, r in downs.iloc[::-1].iterrows()) + ".")
            pp = rc.partial_period(ctx)
            if pp and self.b and self.b.date_to:
                # a pre-truncated date (e.g. a quarter-start calc) ends on the period start; use the real data end
                end = dt.date.fromisoformat(pp["data_ends"])
                if end.day == 1 and self.b.date_to > end:
                    pend = end + dt.timedelta(days=pp["days_in_period"] - 1)
                    real = min(self.b.date_to, pend)
                    if real >= pend:
                        pp = None
                    else:
                        el = (real - end).days + 1
                        pp.update(data_ends=real.isoformat(), days_elapsed=el,
                                  full_period_pace=pp["value"] * pp["days_in_period"] / el)
            if pp:
                if pp["days_elapsed"] >= 0.2 * pp["days_in_period"]:
                    out.append(f"The last column ({pp['period']}) is incomplete - the data stops on {pp['data_ends']} "
                               f"({pp['days_elapsed']} of {pp['days_in_period']} days) - so it looks lower than it will "
                               f"be ({pp['value']:,.0f} so far, about {pp['full_period_pace']:,.0f} at the same pace).")
                else:
                    out.append(f"The last column ({pp['period']}) holds only {pp['days_elapsed']} day(s) of data.")
        # rank only by the measure the analysis really uses, and only when it adds up (a ratio is not a ranking)
        cm = next((x for x in res.measures if ctx is not None and x.sql == ctx.measure_sql), None)
        additive = cm is not None and cm.aggregation in ("sum", "count", "count_distinct")
        if ctx and ctx.category_sql and not res.time_dims() and additive:
            co = rc.concentration(ctx)
            if co.ok and not co.table.empty and float(co.table["value"].head(3).abs().sum()) > 0:
                out.append(f"Largest {ctx.category_label} values by {cm.label}: " + "; ".join(
                    f"{_val(r['member'])} {r['value']:,.0f}" for _, r in co.table.head(3).iterrows()) + ".")
        if dt_ and not dt_.get("cells_can_be_summed") and res.dims:
            who = f"one {self.word}" if self._counts_records(res) else "one value"
            out.append(f"The cells cannot be added up: they total {dt_['sum_of_cells']:,} because {who} can appear "
                       f"in several cells, while there are {dt_['distinct_total']:,} distinct.")
        if res.status == "approximate" and out:
            out.append("*(These numbers are approximate: " + "; ".join(res.reasons[:2]) + ".)*")
        return out

    def ph_measure(self, res) -> str:
        ws = self.kit.worksheet(res.worksheet)
        for r in [e.field for p in ws.panes for e in p.encodings] + list(ws.rows.refs) + list(ws.cols.refs):
            if r.is_measure:
                return self.ph.measure(r)
        return res.measures[0].label

    def filter_lines(self, g: DashboardGroup, fam, main) -> list[str]:
        """Filters shared by every sheet of the dashboard, then the ones only some sheets have (compared sheet by
        sheet - a filter on one chart is never presented as the dashboard's)."""
        if fam is not None:
            sheets = [fam.example]
        else:
            sheets = [w for w in (self.kit.worksheet(n) for n in g.sheets) if w is not None] or [main]
        per: dict[str, list[str]] = {}            # filter text -> sheets that have it
        for w in sheets:
            for fd in w.filters:
                if fam is not None and fam.varying_field and fd.member_count == 1 and \
                        (fd.field.caption or fd.field.field) == fam.varying_field:
                    continue
                t = self.filter_text(fd, w.datasources[0] if w.datasources else None)
                if t and w.name not in per.setdefault(t, []):
                    per[t].append(w.name)
        common = [t for t, on in per.items() if len(on) == len(sheets)]
        out = ["**Built-in filters:** " + "; ".join(common) + "."] if common else []
        some: dict[str, list[str]] = {}
        for t, on in per.items():
            if len(on) < len(sheets):
                names = [self.sheet_title(n) for n in on]
                some.setdefault(join([f"*{x}*" for x in names]), []).append(t)
        for who, ts in some.items():
            out.append(f"**Only on {who}:** " + "; ".join(ts) + " - the other charts here are not filtered this way.")
        clicks = self.click_lines(g)
        if clicks:
            out.append("**Clicking:** " + " ".join(c[:1].upper() + c[1:] for c in clicks))
        return out

    def click_lines(self, g: DashboardGroup) -> list[str]:
        """What clicking does - only from the workbook's actions list (never from filter names)."""
        names = {d.name for d in g.dashboards}
        out = []
        for a in self.kit.actions:
            if a.source_dashboard not in names and a.params.get("target") not in names:
                continue
            src = f"*{self.sheet_title(a.source_worksheet)}*" if a.source_worksheet else "any chart"
            excl = [self.sheet_title(x) for x in (a.params.get("exclude") or "").split(",") if x]
            by = a.params.get("field-captions")
            by_txt = f" by {by.replace(',', ', ')}" if by else ""
            if a.kind == "filter":
                others = "the other charts" + (f" (not *{join(excl)}*)" if excl and not a.source_worksheet else "")
                out.append(f"selecting a mark in {src} filters {others} ('{a.caption or a.name}').")
            elif a.kind == "highlight":
                out.append(f"selecting a mark in {src} highlights matching marks{by_txt} without filtering "
                           f"anything ('{a.caption or a.name}').")
            elif a.kind == "url":
                out.append(f"selecting a mark in {src} opens a web link ('{a.caption or a.name}').")
            elif a.kind == "go-to-sheet":
                out.append(f"selecting a mark in {src} opens another sheet ('{a.caption or a.name}').")
        return out

    def sheet_title(self, name: str) -> str:
        w = self.kit.worksheet(name)
        t = (w.title or "").strip() if w is not None else ""
        return name if not t or w.title_is_placeholder else clean_title(t, self.captions)

    def dashboard_text(self, g: DashboardGroup) -> str:
        m = self.kit
        lines: list[str] = []
        info = self.dash_info.setdefault(g.name, {})
        if len(g.dashboards) > 1:
            lines.append(f"*Split over {len(g.dashboards)} dashboards: "
                         + join([f"'{d.name.strip()}' ({len(d.sheets)} charts)" for d in g.dashboards]) + ".*")
        fam = g.family
        if fam is not None:
            what = self.ph.what_it_shows(fam.example)
            if fam.varying_field:
                what = re.sub(rf" for {re.escape(fam.varying_field)} = .+$", "", what)
                vals = fam.values
                lines.append(f"**What it shows:** {len(fam.sheets)} small charts, one for each {fam.varying_field} "
                             f"({join(vals[:3])}{', …' if len(vals) > 3 else ''}). Each one is {what}.")
                info["what"] = f"{len(fam.sheets)} small charts, one for each {fam.varying_field}; each one is {what}"
            else:
                lines.append(f"**What it shows:** {len(fam.sheets)} charts with the same design: {what}.")
                info["what"] = f"{len(fam.sheets)} charts with the same design: {what}"
            main = fam.example
        else:
            main = main_sheet(m, g.dashboards[0])
            sheets = [m.worksheet(s) for s in g.sheets if m.worksheet(s)]
            if len(sheets) == 1:
                lines.append(f"**What it shows:** {self.ph.what_it_shows(sheets[0])}.")
                info["what"] = self.ph.what_it_shows(sheets[0])
            elif not sheets:
                lines.append("**What it shows:** no worksheets (text, images or other objects only).")
                info["what"] = "no worksheets - text, images or other objects only"
            else:
                info["what"] = (f"{len(sheets)} charts: "
                                + join([f"*{self.sheet_title(w.name)}*" for w in sheets[:4]])
                                + (f" and {len(sheets) - 4} more" if len(sheets) > 4 else ""))
                lines.append(f"**What it shows:** {len(sheets)} charts:")
                for w in sheets[:6]:
                    lines.append(f"- *{self.sheet_title(w.name)}*: {self.ph.what_it_shows(w)}.")
                if len(sheets) > 6:
                    lines.append(f"- … and {len(sheets) - 6} more.")
        if main is not None:
            read = how_to_read(main, self.ph)
            if read:
                lines.append(f"**How to read it:** {read}")
            lines += self.filter_lines(g, fam, main)
        qf = sorted({r.caption or r.field for d in g.dashboards for r in d.quick_filters})
        if qf:
            lines.append(f"**You can filter it by:** {join(qf)}.")
        if self.lw.store is not None:
            nums = self.family_numbers(fam) if fam is not None else (self.sheet_numbers(main.name) if main else [])
            if nums:
                lines.append("**What the data shows:** " + " ".join(nums))
                info["nums"] = nums
        mine = [f for f in self.lints if f.severity != "info" and
                (set(f.dashboards) & {d.name for d in g.dashboards} or set(f.sheets) & set(g.sheets))]
        notes = list(dict.fromkeys(self.short_issue(f, g) for f in mine))
        if notes:
            lines.append("**Watch out:** " + " ".join(notes))
        info["issues"] = len(notes)
        info["main"] = main
        return "\n\n".join(lines)

    # ------------------------------------------------------------------ one dashboard on its own
    def calcs_used_by(self, sheets: list[str]) -> list[str]:
        on = set(sheets)
        out = []
        for d in self.kit.datasources:
            for f in d.fields:
                if f.kind == "calculated" and on & set(f.used_by):
                    out.append(f"- **{f.caption or f.name}** - {self._explain(f)}")
        return out

    def dashboard_questions(self, g: DashboardGroup, info: dict) -> list[str]:
        """Suggested questions built from what this dashboard contains (no names or values are assumed)."""
        sheets = [w for w in (self.kit.worksheet(x) for x in g.sheets) if w is not None]
        qs = ["Explain this dashboard: what does each chart show and how should I read it?"]
        has_time = any(r.kind in ("date_trunc", "date_part") for w in sheets for r in w.all_refs())
        # questions about values only when the app can read this dashboard's data (else they cannot be answered)
        has_data = self.lw.store is not None and any(self.lw.table_for(d) for w in sheets for d in w.datasources)
        if has_time and has_data:
            qs.append("What changed recently in this dashboard, and what is driving it?")
        fam = g.family
        if not has_data:
            qs.append("Which fields and calculations does this dashboard use, and what does each one mean?")
        elif fam is not None and fam.varying_field:
            qs.append(f"Which {fam.varying_field} stands out here, and why?")
        else:
            main = info.get("main")
            prof = self.lw.profile_for(main.datasources[0]) if main is not None and main.datasources else None
            sens = set(prof.sensitive_suggestions) if prof is not None else set()
            cols = {c.name: c for c in prof.columns} if prof is not None else {}
            dims = [r for r in (main.all_refs() if main is not None else []) if r.kind == "dimension"
                    and r.field not in sens and (r.field not in cols or 2 <= cols[r.field].distinct <= 200)]
            if dims:
                qs.append(f"Which {dims[0].caption or dims[0].field} values stand out here?")
        if info.get("issues"):
            qs.append("Is anything on this dashboard misleading, and how should I read it correctly?")
        names = {d.name for d in g.dashboards}
        if any(a.source_dashboard in names for a in self.kit.actions) or any(d.quick_filters for d in g.dashboards):
            qs.append("What can I filter or click on this dashboard, and what changes when I do?")
        return list(dict.fromkeys(qs))[:4]

    def dashboard_view(self, g: DashboardGroup, body: str) -> DashboardView:
        info = self.dash_info.get(g.name, {})
        pages = [d.name for d in g.dashboards]
        parts = []
        what = info.get("what")
        if what:
            parts.append(what[:1].upper() + what[1:] + ".")
        if len(pages) > 1:
            parts.append(f"It is split over {len(pages)} dashboard pages.")
        nums = info.get("nums") or []
        if nums:
            parts.append(nums[0])
        if info.get("issues"):
            parts.append(f"{info['issues']} thing{'s' if info['issues'] > 1 else ''} to watch out for (see below).")
        calcs = self.calcs_used_by(g.sheets)
        full = body + (("\n\n**Calculations used here:**\n\n" + "\n".join(calcs)) if calcs else "")
        return DashboardView(name=g.name.strip(), pages=pages, sheets=list(g.sheets), summary=" ".join(parts),
                             body=full, questions=self.dashboard_questions(g, info))

    def short_issue(self, f, g: DashboardGroup | None = None) -> str:
        if f.code == "L2":
            return "The name promises percentages, but the charts are labelled with counts (no percent calculation)."
        if f.code == "L3":
            return (f"A filter on {f.evidence.get('field')} was copied onto these charts although they do not show "
                    f"{f.evidence.get('field')}: {plural(self.word)} with any other or empty "
                    f"{f.evidence.get('field')} are silently left out{self.l3_impact(f)}.")
        if f.code == "L5":
            return (f"'{f.evidence.get('calc')}' ignores {f.evidence.get('unit_field')}, so "
                    f"{f.evidence.get('affected', 'some')} {plural(self.word)} with ages in other units land in the wrong "
                    "group.")
        return f.message

    def l3_impact(self, f) -> str:
        if not self.b:
            return ""
        fld = f.evidence.get("field")
        col = next((c for c in self.column_names() if self.caption_of(c) == fld or c == fld), None)
        members = None
        for w in self.kit.worksheets:
            if w.name in f.sheets:
                fd = next((x for x in w.filters if (x.field.caption or x.field.field) == fld), None)
                if fd:
                    members = fd.members
                    break
        if not col or members is None:
            return ""
        imp = F.excluded_by_include_filter(self.lw, self.b, col, members)
        return f" ({self.records(imp[0])}, {pct(*imp, 1)} of all)" if imp and imp[0] else ""

    def dashboards(self) -> Section:
        parts = []
        self.views = []
        for g in self.groups:
            body = self.dashboard_text(g)
            parts.append(f"### {g.name}\n\n{body}")
            self.views.append(self.dashboard_view(g, body))
        return Section("The dashboards, one by one", "\n\n".join(parts) or "There are no dashboards.", "dashboards")

    # ------------------------------------------------------------------ calcs, problems, questions
    def _explain(self, f) -> str:
        i = f.formula_info
        return explain_formula(f.formula_readable or f.formula or "", i.kind if i else None, i.fields if i else [],
                               i.lods if i else [], i.table_calc_functions if i else [])

    def calculations(self) -> Section | None:
        calcs = [f for d in self.kit.datasources for f in d.fields if f.kind == "calculated"]
        if not calcs:
            return None
        used = [f for f in calcs if f.used_by]
        unused = [f for f in calcs if not f.used_by]
        db_of = {w.name: w.in_dashboards for w in self.kit.worksheets}
        lines = []
        for f in used:
            dbs = sorted({d.strip() for s in f.used_by for d in db_of.get(s, [])})
            where = (f" Used in: {join(dbs[:3])}" + (f" and {len(dbs) - 3} more" if len(dbs) > 3 else "") + "."
                     if dbs else f" Used by {len(f.used_by)} sheet(s).")
            lines.append(f"- **{f.caption or f.name}** - {self._explain(f)}{where}")
        body = f"{len(calcs)} calculated fields are defined; {len(used)} are used by the charts.\n\n" + "\n".join(lines)
        if unused:
            body += (f"\n\n**Defined but not used by any chart ({len(unused)}):**\n\n"
                     + "\n".join(f"- *{f.caption or f.name}* - {self._explain(f)}" for f in unused))
        return Section("Calculated fields", body, "calcs")

    def problems(self) -> Section | None:
        if not self.lints:
            return None
        sev_word = {"error": "wrong result likely", "warn": "can mislead", "info": "good to know"}
        groups: dict[str, list] = {}
        for f in self.lints:
            groups.setdefault(f.code, []).append(f)
        lines = []
        for code, fs in groups.items():
            f0 = fs[0]
            dbs = sorted({d.strip() for f in fs for d in f.dashboards})
            where = f" *Affects: {join(dbs[:4])}{' and more' if len(dbs) > 4 else ''}.*" if dbs else ""
            if code == "L2":
                what = (f"{len(dbs)} dashboard name(s)" if dbs else
                        f"{len({s for f in fs for s in f.sheets})} sheet title(s)")
                msg = (f"{what} promise percentages or shares, but the charts have no percent-of-total "
                       "calculation: the labels are raw numbers.")
            elif code == "L12":
                lst = join([f"{f.evidence.get('field')} ({f.evidence.get('excluded')})" for f in fs][:5])
                msg = (f"{len(fs)} filters leave out long lists of values ({lst}). Any new value that appears in "
                       "future data will show up automatically, which may not be intended.")
            elif code == "L3":
                msg = f0.message + self.l3_impact(f0).replace(" (", " Here that is ").rstrip(")") + \
                    ("." if self.l3_impact(f0) else "")
            elif code in ("L4",):
                msg = " ".join(f.message for f in fs)
                for f in fs:
                    self.questions.append(f"Which chart really shows {f.evidence.get('named_after')}?")
            else:
                msg = " ".join(dict.fromkeys(f.message for f in fs))
            lines.append(f"- **{f0.title}** ({sev_word[f0.severity]}). {msg}{where}")
        bad = [f for f in self.lints if f.severity in ("error", "warn")]
        if bad:
            worst = next((f for f in self.lints if f.severity == "error"), bad[0])
            self.brief["problems"] = (f"{len({f.code for f in bad})} kinds of definition problem can make charts "
                                      f"misleading - the most serious: *{worst.title.lower()}*.")
        return Section("Problems to fix", "\n".join(lines), "problems")

    def questions_section(self) -> Section:
        qs = list(dict.fromkeys(self.questions))
        b = self.b
        if b and b.category:
            qs.append(f"Which {self.caption_of(b.category)} values grew fastest, and is any of them a concern?")
        flags = F.flag_calcs(self.lw, b) if b else []
        if flags:
            qs.append(f"How has the share of {flags[0][0]} changed over time"
                      + (f", and in which {self.caption_of(b.category)} values?" if b.category else "?"))
        qs.append("Explain one dashboard in more detail.")
        self.questions = qs[:6]
        return Section("Questions to explore next", "\n".join(f"- {q}" for q in self.questions)
                       + "\n\n*Ask them in the chat.*", "questions")


def explain_workbook(lw) -> Report:
    w = Writer(lw)
    about, data, org = w.about(), w.data(), w.organisation()
    ins = w.insights()
    dashes = w.dashboards()
    calcs = w.calculations()
    probs = w.problems()
    qs = w.questions_section()
    order = ["data", "organisation", "change", "driver", "problems"]
    brief = "\n".join(f"- {w.brief[k]}" for k in order if k in w.brief)
    rep = Report(workbook=lw.source_name)
    rep.sections = [s for s in (Section("In brief", brief, "brief") if brief else None, about, data, org, ins,
                                dashes, calcs, probs, qs) if s is not None]
    rep.questions = w.questions
    names: dict[str, int] = {}
    for v in w.views:                       # button labels must be unique
        names[v.name] = names.get(v.name, 0) + 1
        if names[v.name] > 1:
            v.name = f"{v.name} ({names[v.name]})"
    rep.dashboards = w.views
    return rep
