"""Assertion detection: is this mention real, negated, historical, or someone else's?

A NegEx/ConText-style implementation. This is the difference between a tool
coders trust and one they turn off: "no chest pain", "family history of colon
cancer", "if fever develops, return" and "rule out PE" must never become codes.
Each mention gets an assertion label plus a confidence multiplier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Triggers that look *forward* from the trigger to the concept.
_PRE_NEGATION = [
    "no", "not", "denies", "denied", "without", "negative for", "no evidence of",
    "no signs of", "no sign of", "absent", "free of", "ruled out", "rules out",
    "declines", "declined", "no complaints of", "no history of", "never had",
    "no longer", "resolved", "unremarkable for", "no acute", "no known",
]
# Triggers that look *backward* from the concept to the trigger.
_POST_NEGATION = [
    "is ruled out", "was ruled out", "were ruled out", "has resolved",
    "not present", "is negative", "was negative", "unlikely", "is absent",
]
_UNCERTAIN = [
    "possible", "possibly", "probable", "probably", "suspect", "suspected",
    "suspicion for", "concern for", "concerning for", "questionable",
    "rule out", "r/o", "differential includes", "cannot exclude",
    "may have", "might have", "consider", "versus", "vs", "likely",
    "presumed", "appears to", "evaluate for", "workup for", "screen for",
]
_HISTORICAL = [
    "history of", "hx of", "h/o", "past medical history of", "previous",
    "prior", "status post", "s/p", "remote", "years ago", "in the past",
    "formerly", "resolved", "no longer active",
]
_HYPOTHETICAL = [
    "if", "should", "unless", "in case of", "return if", "call if",
    "watch for", "risk of", "to prevent", "prophylaxis for", "prophylaxis against",
    "avoid", "counseled about", "educated about", "discussed risk of",
]
_NOT_PATIENT = [
    "mother", "father", "sister", "brother", "son", "daughter", "aunt",
    "uncle", "grandmother", "grandfather", "cousin", "family history",
    "fh of", "maternal", "paternal", "sibling", "spouse", "wife", "husband",
]
# Conjunctions that terminate a trigger's scope, so "no chest pain but has
# dyspnea" does not negate dyspnea.
_SCOPE_BREAK = re.compile(
    r"\b(but|however|although|though|except|aside from|other than|otherwise|"
    r"positive for|reports|complains of|endorses|admits)\b|[;.]",
    re.IGNORECASE,
)

WINDOW = 60  # characters of scope for a trigger, per the original NegEx design


@dataclass
class Assertion:
    label: str          # present | negated | uncertain | historical | hypothetical | not_patient
    multiplier: float
    trigger: str | None = None

    @property
    def codable(self) -> bool:
        # ICD-10-CM outpatient guideline IV.H: do not code uncertain diagnoses
        # in the outpatient setting. Inpatient rules differ - see notes below.
        return self.label in {"present", "historical"}


PRESENT = Assertion("present", 1.0)

_MULTIPLIER = {
    "present": 1.0,
    "historical": 0.45,     # codable, but usually as a Z-code / secondary
    "uncertain": 0.20,
    "hypothetical": 0.05,
    "negated": 0.0,
    "not_patient": 0.0,
}


def _compile(triggers: list[str]) -> re.Pattern[str]:
    escaped = sorted((re.escape(t) for t in triggers), key=len, reverse=True)
    return re.compile(r"(?<![a-z])(" + "|".join(escaped) + r")(?![a-z])", re.IGNORECASE)


_RE_PRE_NEG = _compile(_PRE_NEGATION)
_RE_POST_NEG = _compile(_POST_NEGATION)
_RE_UNCERTAIN = _compile(_UNCERTAIN)
_RE_HISTORICAL = _compile(_HISTORICAL)
_RE_HYPOTHETICAL = _compile(_HYPOTHETICAL)
_RE_NOT_PATIENT = _compile(_NOT_PATIENT)


def _scope_clear(text: str) -> bool:
    """True if no scope-breaking token sits between trigger and concept."""
    return _SCOPE_BREAK.search(text) is None


# A sentence ends at punctuation or a blank line. A *single* newline is just
# line wrapping - clinical notes wrap mid-sentence constantly, and treating a
# wrap as a sentence boundary silently destroys negation ("denies\nchest pain").
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n\s*\n|\n\s*(?=[0-9]+[.)]\s|[-*•]\s)")


def _flatten(fragment: str) -> str:
    return re.sub(r"\s*\n\s*", " ", fragment)


def classify(text: str, start: int, end: int) -> Assertion:
    """Label a mention at [start, end) within `text`."""
    left = text[max(0, start - WINDOW):start]
    right = text[end:end + WINDOW]

    # Sentence-local left context: negation does not cross a sentence boundary.
    sentence_left = _flatten(_SENT_SPLIT.split(left)[-1])

    # not_patient wins outright - a relative's disease is never the patient's code.
    if m := _RE_NOT_PATIENT.search(sentence_left):
        if _scope_clear(sentence_left[m.end():]):
            return Assertion("not_patient", _MULTIPLIER["not_patient"], m.group(1))

    if m := _RE_HYPOTHETICAL.search(sentence_left):
        if _scope_clear(sentence_left[m.end():]):
            return Assertion("hypothetical", _MULTIPLIER["hypothetical"], m.group(1))

    for regex, label in ((_RE_PRE_NEG, "negated"), (_RE_UNCERTAIN, "uncertain"),
                         (_RE_HISTORICAL, "historical")):
        # Use the *last* matching trigger before the concept.
        last = None
        for m in regex.finditer(sentence_left):
            last = m
        if last and _scope_clear(sentence_left[last.end():]):
            return Assertion(label, _MULTIPLIER[label], last.group(1))

    sentence_right = _flatten(_SENT_SPLIT.split(right)[0])
    if m := _RE_POST_NEG.search(sentence_right):
        if _scope_clear(sentence_right[:m.start()]):
            return Assertion("negated", _MULTIPLIER["negated"], m.group(1))

    return PRESENT


def sentence_around(text: str, start: int, end: int, pad: int = 200) -> tuple[int, int, str]:
    """The sentence containing a mention, for display as evidence."""
    left = text.rfind(".", max(0, start - pad), start)
    nl = text.rfind("\n", max(0, start - pad), start)
    s = max(left, nl) + 1 if max(left, nl) != -1 else max(0, start - pad)
    right_dot = text.find(".", end, end + pad)
    right_nl = text.find("\n", end, end + pad)
    candidates = [c for c in (right_dot, right_nl) if c != -1]
    e = min(candidates) + 1 if candidates else min(len(text), end + pad)
    return s, e, text[s:e].strip()
