"""Subject detection and a small glossary of domain terms, matched on field names.

Generic: each subject is a list of name patterns; the subject that matches the most fields wins (and only when
enough fields match). Terms are explained once, the first time they appear in the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Subject:
    key: str
    label: str                       # "drug-safety (pharmacovigilance) case data"
    record_word: str                 # what one counted record usually is ("case")
    patterns: tuple[str, ...]        # regexes on normalised field names / captions
    min_hits: int = 3


SUBJECTS = [
    Subject("pv", "drug-safety (pharmacovigilance) case data", "case",
            (r"^soc$", r"^pt$", r"^hlt$", r"^hlgt$", r"^llt$", r"meddra", r"seriousness", r"^serious",
             r"event_?outcome", r"causality", r"labell?ing", r"listedness", r"reporter", r"report_?type",
             r"adverse", r"^ae_", r"suspect", r"argus", r"case_?id$", r"caseid", r"dechallenge", r"rechallenge",
             r"protocol", r"unblind")),
    Subject("clinical", "clinical / hospital patient data", "patient",
            (r"patient", r"admission", r"admit", r"discharge", r"diagnos", r"encounter", r"hospital", r"ward",
             r"provider", r"payer", r"procedure", r"length_?of_?stay", r"\blos\b", r"readmi", r"icd", r"bmi",
             r"blood_?pressure", r"glucose", r"triage", r"er_?wait")),
    Subject("epidemic", "disease surveillance (cases, deaths, vaccinations)", "record",
            (r"confirmed", r"deaths?", r"recovered", r"vaccin", r"tests?_?performed", r"positiv", r"covid",
             r"new_?cases", r"total_?cases", r"hospitali[sz]", r"icu")),
    Subject("sales", "sales / retail transaction data", "order",
            (r"^sales$", r"profit", r"order_?id", r"order_?date", r"ship", r"customer", r"product", r"quantity",
             r"discount", r"segment", r"sub_?category", r"revenue", r"unit_?price", r"invoice", r"sku")),
    Subject("hr", "HR / workforce data", "employee",
            (r"employee", r"salary", r"attrition", r"department", r"hire", r"tenure", r"job_?role", r"gender",
             r"performance_?rating", r"overtime")),
    Subject("finance", "financial data", "record",
            (r"revenue", r"cost", r"budget", r"expense", r"forecast", r"variance", r"ebitda", r"margin",
             r"account", r"ledger", r"fiscal")),
]

# term -> explanation (matched on the normalised field name, whole name only)
TERMS: dict[str, str] = {
    "soc": "System Organ Class, the MedDRA grouping of events by body system",
    "pt": "Preferred Term, the MedDRA name of a single medical event",
    "hlt": "High Level Term, a MedDRA group of related Preferred Terms",
    "hlgt": "High Level Group Term, a MedDRA group of High Level Terms",
    "llt": "Lowest Level Term, the most specific MedDRA term as reported",
    "ird": "Initial Received Date, the date the case was first received",
    "initial_received_date": "the date the case was first received",
    "latest_fu_received_date": "the date the latest follow-up report was received",
    "fu_num": "the follow-up report number of the case",
    "event_seriousness": "whether the event met the regulatory definition of 'serious'",
    "labeling": "whether the event is already described in the product's reference safety information",
    "labelling": "whether the event is already described in the product's reference safety information",
    "causality": "the assessed relationship between the product and the event",
    "report_type": "how the report reached the company (for example spontaneous, study, literature)",
    "reporter_verbatim": "the event exactly as the reporter described it (free text)",
    "sdmp": "a standard age-group scheme",
    "los": "length of stay",
    "yoy": "year over year",
    "ytd": "year to date",
}


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def detect_subject(field_names: list[str], extra_text: str = "") -> Subject | None:
    names = [norm(n) for n in field_names] + [norm(w) for w in re.findall(r"[A-Za-z_]+", extra_text or "")]
    best, best_hits = None, 0
    for s in SUBJECTS:
        hits = sum(1 for n in set(names) if any(re.search(p, n) for p in s.patterns))
        if hits >= s.min_hits and hits > best_hits:
            best, best_hits = s, hits
    return best


class Glossary:
    """Explains a term the first time it is mentioned."""

    def __init__(self) -> None:
        self.done: set[str] = set()

    def term(self, name: str) -> str:
        key = norm(name)
        if key in TERMS and key not in self.done:
            self.done.add(key)
            return f"{name} ({TERMS[key]})"
        return name

    def explain_in(self, text: str) -> str:
        """Add an explanation after the first occurrence of known abbreviations inside a title."""
        for word in re.findall(r"\b[A-Z]{2,5}\b", text or ""):
            key = word.lower()
            if key in TERMS and key not in self.done:
                self.done.add(key)
                return text.replace(word, f"{word} ({TERMS[key]})", 1)
        return text
