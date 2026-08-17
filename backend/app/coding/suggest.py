"""Code suggestion: chart text -> candidate ICD-10-CM diagnosis lines.

Diagnoses only. Procedure coding (CPT/HCPCS) and the E/M level estimate are
out of scope for this deployment - see SUPPORTED_CODE_SYSTEMS in config.py.
The procedure tables are still loaded, because the audit rules use them to
check any CPT line that reaches the claim from elsewhere, but nothing here
proposes one.

Two extractors are available and they compose:

1. `dictionary_extract` - deterministic terminology matching with section
   weighting and assertion detection. Fast, offline, explainable, and its
   failure modes are predictable. This is the default.
2. `llm_extract` (coding/llm.py) - an LLM reads the note and returns
   structured findings. Better recall on prose that does not use textbook
   phrasing, but requires a BAA and must be constrained to codes that exist in
   the loaded terminology, since models will happily invent plausible codes.

Both produce `Candidate` objects, which the merge step reconciles. Every
candidate carries evidence spans; a suggestion a coder cannot trace back to a
sentence is worse than no suggestion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import MIN_SUGGESTION_CONFIDENCE, SUPPORTED_CODE_SYSTEMS
from ..pipeline.sections import Section, section_at, split_sections
from . import context as ctx
from .terminology import Terminology, compile_index, load_terminology


@dataclass
class Evidence:
    char_start: int
    char_end: int
    quote: str
    why: str
    page: int | None = None
    section: str | None = None
    assertion: str = "present"

    def as_dict(self) -> dict:
        return {
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote": self.quote,
            "why": self.why,
            "page": self.page,
            "section": self.section,
            "assertion": self.assertion,
        }


@dataclass
class Candidate:
    system: str                       # ICD10CM (see SUPPORTED_CODE_SYSTEMS)
    code: str
    description: str
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    reasoning: str = ""
    rank: int | None = None
    units: int = 1
    modifiers: list[str] = field(default_factory=list)
    linked_dx: list[str] = field(default_factory=list)
    origin: str = "suggested"
    # Retained for the audit engine even when confidence is too low to surface.
    assertion: str = "present"
    mention_count: int = 1
    best_section: str = "unknown"


def _norm_desc(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def dictionary_extract(
    text: str,
    sections: list[Section],
    term: Terminology,
    page_of=None,
) -> list[Candidate]:
    """Deterministic terminology matching over the ICD-10-CM table."""
    dx_patterns = compile_index(term.dx_index)

    # code -> aggregation bucket
    buckets: dict[str, Candidate] = {}
    # Track claimed character ranges so a longer phrase suppresses the shorter
    # one it contains ("diabetic polyneuropathy" beats "diabetes").
    claimed: list[tuple[int, int]] = []

    def overlaps(s: int, e: int) -> bool:
        return any(not (e <= cs or s >= ce) for cs, ce in claimed)

    for regex, phrase, code in dx_patterns:
        for m in regex.finditer(text):
            s, e = m.span()
            if overlaps(s, e):
                continue
            sec = section_at(sections, s)
            assertion = ctx.classify(text, s, e)

            if not sec.codable:
                assertion = ctx.Assertion("not_patient", 0.0, sec.name)

            q_start, q_end, quote = ctx.sentence_around(text, s, e)

            # Confidence: section credibility x assertion strength, with a
            # small bonus for exact long phrases over short generic ones.
            phrase_bonus = min(0.12, 0.01 * max(0, len(phrase.split()) - 1))
            conf = min(0.97, (0.45 + 0.5 * sec.weight + phrase_bonus)
                       * assertion.multiplier)

            ev = Evidence(
                char_start=q_start,
                char_end=q_end,
                quote=_norm_desc(quote)[:400],
                why=f'matched "{m.group(0)}" in {sec.name.replace("_", " ")}'
                    + (f" (assertion: {assertion.label}"
                       + (f' via "{assertion.trigger}"' if assertion.trigger else "")
                       + ")" if assertion.label != "present" else ""),
                page=page_of(q_start) if page_of else None,
                section=sec.name,
                assertion=assertion.label,
            )

            existing = buckets.get(code)
            if existing is None:
                buckets[code] = Candidate(
                    system="ICD10CM", code=code,
                    description=term.describe("ICD10CM", code),
                    confidence=round(conf, 3), evidence=[ev],
                    assertion=assertion.label, best_section=sec.name,
                    reasoning=f"Terminology match on '{phrase}'.",
                )
            else:
                existing.mention_count += 1
                if len(existing.evidence) < 5:
                    existing.evidence.append(ev)
                if conf > existing.confidence:
                    existing.confidence = round(conf, 3)
                    existing.assertion = assertion.label
                    existing.best_section = sec.name
            claimed.append((s, e))

    out = list(buckets.values())
    # Corroboration: a diagnosis stated three times across sections is more
    # likely real than a single passing mention.
    for c in out:
        if c.mention_count > 1 and c.assertion == "present":
            c.confidence = round(min(0.97, c.confidence + 0.04 * min(3, c.mention_count - 1)), 3)
            c.reasoning += f" Corroborated by {c.mention_count} mentions."
    return out


# --- Assembly --------------------------------------------------------------

def _rank_diagnoses(dxs: list[Candidate], sections: list[Section]) -> None:
    """Assign a claim rank: the primary diagnosis is the reason for the visit.

    Preference goes to the first diagnosis named in the assessment, which is
    what the guidelines mean by 'first-listed'.
    """
    def sort_key(c: Candidate):
        in_assessment = 0 if c.best_section in {"assessment", "plan"} else 1
        first_offset = min((e.char_start for e in c.evidence), default=10**9)
        return (in_assessment, -c.confidence, first_offset)

    for i, c in enumerate(sorted(dxs, key=sort_key), start=1):
        c.rank = i


def suggest(text: str, page_of=None, use_llm: bool | None = None) -> dict:
    """Full suggestion pass over one document's text. Diagnoses only."""
    term = load_terminology()
    sections = split_sections(text)

    candidates = dictionary_extract(text, sections, term, page_of=page_of)

    llm_notes: list[str] = []
    if use_llm:
        from .llm import llm_extract
        try:
            llm_cands, llm_notes = llm_extract(text, term, sections, page_of=page_of)
            candidates = merge(candidates, llm_cands)
        except Exception as exc:                        # never fail the pass
            llm_notes.append(f"LLM extractor unavailable: {exc}")

    # Belt and braces: an extractor that ever proposed a non-diagnosis code
    # would be a bug, but it must not reach the claim if it did.
    dxs = [c for c in candidates if c.system in SUPPORTED_CODE_SYSTEMS]

    # Hide the non-codable ones from the coder but keep them for auditing.
    surfaced_dx = [c for c in dxs if c.confidence >= MIN_SUGGESTION_CONFIDENCE]
    suppressed = [c for c in dxs if c.confidence < MIN_SUGGESTION_CONFIDENCE]

    _rank_diagnoses(surfaced_dx, sections)

    return {
        "sections": sections,
        "diagnoses": surfaced_dx,
        "suppressed": suppressed,
        "notes": llm_notes,
    }


def merge(primary: list[Candidate], secondary: list[Candidate]) -> list[Candidate]:
    """Union two candidate sets, keeping the stronger confidence and all evidence."""
    index = {(c.system, c.code): c for c in primary}
    for c in secondary:
        key = (c.system, c.code)
        if key not in index:
            index[key] = c
            continue
        existing = index[key]
        existing.evidence.extend(e for e in c.evidence if e.quote not in
                                 {x.quote for x in existing.evidence})
        if c.confidence > existing.confidence:
            existing.confidence = c.confidence
            existing.assertion = c.assertion
        # Agreement between two independent extractors is itself a signal.
        existing.confidence = round(min(0.98, existing.confidence + 0.05), 3)
        existing.reasoning = (existing.reasoning + " " + c.reasoning).strip()
    return list(index.values())
