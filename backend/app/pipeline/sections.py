"""Clinical section segmentation and header parsing.

Section context is the single highest-leverage signal in autocoding. The same
sentence means different things in different places: "diabetes" under Past
Medical History is not necessarily codable for this encounter, while under
Assessment and Plan it usually is. "chest pain" under Chief Complaint is a
symptom; under Assessment it may be a diagnosis. Everything downstream reads
`Section.weight` to modulate confidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Canonical section -> the header spellings seen in the wild.
SECTION_PATTERNS: dict[str, list[str]] = {
    "chief_complaint": ["chief complaint", "cc", "reason for visit", "presenting complaint"],
    "hpi": ["history of present illness", "hpi", "subjective", "interval history"],
    "past_medical_history": ["past medical history", "pmh", "medical history",
                             "past history", "problem list", "active problems"],
    "past_surgical_history": ["past surgical history", "psh", "surgical history"],
    "family_history": ["family history", "fh"],
    "social_history": ["social history", "sh"],
    "medications": ["medications", "current medications", "medication list",
                    "meds", "discharge medications"],
    "allergies": ["allergies", "allergy", "drug allergies"],
    "review_of_systems": ["review of systems", "ros"],
    "physical_exam": ["physical examination", "physical exam", "objective",
                      "examination", "exam"],
    "vitals": ["vital signs", "vitals"],
    "results": ["laboratory", "labs", "lab results", "imaging", "radiology",
                "diagnostic results", "results", "pathology"],
    "procedures": ["procedure", "procedures", "procedure performed",
                   "operative report", "operation performed", "description of procedure",
                   "procedure note", "interventions"],
    "assessment": ["assessment", "impression", "diagnosis", "diagnoses",
                   "assessment and plan", "a/p", "assessment/plan",
                   "discharge diagnosis", "final diagnosis", "clinical impression"],
    "plan": ["plan", "treatment plan", "disposition", "recommendations",
             "follow up", "follow-up"],
    "hospital_course": ["hospital course", "brief hospital course"],
    "addendum": ["addendum", "attestation", "electronically signed"],
}

# How much a mention inside this section counts toward a codable diagnosis.
SECTION_WEIGHT: dict[str, float] = {
    "assessment": 1.00,
    "plan": 0.90,
    "procedures": 1.00,
    "hospital_course": 0.80,
    "chief_complaint": 0.60,
    "hpi": 0.65,
    "results": 0.55,
    "physical_exam": 0.50,
    "past_medical_history": 0.35,   # history, not necessarily this encounter
    "past_surgical_history": 0.25,
    "medications": 0.30,
    "review_of_systems": 0.25,
    "family_history": 0.05,          # never the patient's own diagnosis
    "social_history": 0.20,
    "allergies": 0.10,
    "vitals": 0.30,
    "addendum": 0.40,
    "unknown": 0.50,
}

# Sections where a finding can never be the patient's own active diagnosis.
NEVER_CODABLE = {"family_history", "allergies"}

_HEADER_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z /&'\-]{1,45})\s*:?\s*$|"        # "Assessment:" on its own line
    r"^\s*(?P<name2>[A-Za-z][A-Za-z /&'\-]{1,45})\s*:\s*(?P<rest>\S.*)$",  # "Assessment: text"
    re.MULTILINE,
)

_LOOKUP: dict[str, str] = {}
for canon, spellings in SECTION_PATTERNS.items():
    for s in spellings:
        _LOOKUP[s] = canon


@dataclass
class Section:
    name: str
    char_start: int
    char_end: int
    header_text: str

    @property
    def weight(self) -> float:
        return SECTION_WEIGHT.get(self.name, SECTION_WEIGHT["unknown"])

    @property
    def codable(self) -> bool:
        return self.name not in NEVER_CODABLE


def _canonical(raw: str) -> str | None:
    key = re.sub(r"[^a-z/ &\-]", "", raw.strip().lower()).strip()
    key = re.sub(r"\s+", " ", key)
    if key in _LOOKUP:
        return _LOOKUP[key]
    # Allow "ASSESSMENT AND PLAN" style compounds and numbered headers.
    for spelling, canon in _LOOKUP.items():
        if len(spelling) >= 5 and key.startswith(spelling):
            return canon
    return None


def split_sections(text: str) -> list[Section]:
    """Find section boundaries. Unmatched leading text becomes 'unknown'."""
    hits: list[tuple[int, int, str, str]] = []  # start, header_end, canon, header_text
    for m in _HEADER_RE.finditer(text):
        raw = m.group("name") or m.group("name2") or ""
        canon = _canonical(raw)
        if not canon:
            continue
        # A header line in ALL CAPS or ending in ':' is a strong signal; a
        # bare capitalised word mid-paragraph is not.
        line = raw.strip()
        is_strong = line.isupper() or bool(m.group("name2")) or bool(m.group("name"))
        if not is_strong:
            continue
        body_start = m.end("rest") - len(m.group("rest")) if m.group("rest") else m.end()
        hits.append((m.start(), body_start, canon, line))

    if not hits:
        return [Section("unknown", 0, len(text), "")]

    sections: list[Section] = []
    if hits[0][0] > 0:
        sections.append(Section("unknown", 0, hits[0][0], ""))
    for i, (h_start, body_start, canon, header) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        if end <= body_start:
            continue
        sections.append(Section(canon, body_start, end, header))
    return sections


def section_at(sections: list[Section], offset: int) -> Section:
    for s in sections:
        if s.char_start <= offset < s.char_end:
            return s
    return Section("unknown", 0, 0, "")


def sections_as_dict(sections: list[Section]) -> dict[str, list[list[int]]]:
    out: dict[str, list[list[int]]] = {}
    for s in sections:
        out.setdefault(s.name, []).append([s.char_start, s.char_end])
    return out


# --- Encounter header parsing ----------------------------------------------

_AGE_RE = re.compile(
    r"\b(?:age[:\s]+(?P<a1>\d{1,3})|(?P<a2>\d{1,3})[\s-]*(?:y\.?o\.?|year[\s-]?old|yo)\b)",
    re.IGNORECASE,
)
_SEX_RE = re.compile(
    r"\b(?:sex|gender)[:\s]+(?P<s1>male|female|m|f)\b|"
    r"\b\d{1,3}[\s-]*(?:y\.?o\.?|year[\s-]?old|yo)[\s,]*(?P<s2>male|female|man|woman|m|f)\b",
    re.IGNORECASE,
)
_DOS_RE = re.compile(
    r"\b(?:date of service|dos|encounter date|visit date|service date|date)[:\s]+"
    r"(?P<d>\d{1,4}[/-]\d{1,2}[/-]\d{1,4}|[A-Z][a-z]{2,8}\s+\d{1,2},\s*\d{4})",
    re.IGNORECASE,
)
# EHR screenshots (eClinicalWorks, Epic printouts) put the label in a column
# header and the value in the row beneath it, so OCR flattens them onto
# different lines and the label is never adjacent to its date. Only the
# unambiguous labels get this treatment - a bare "Date" is far too likely to
# pick up a date of birth or a medication start date from a neighbouring cell.
_DOS_LABEL_RE = re.compile(
    r"\b(?:date of service|d\.?o\.?s\.?|encounter date|visit date|service date)\b",
    re.IGNORECASE,
)
_DATE_TOKEN_RE = re.compile(
    r"\b(?P<d>\d{1,4}[/-]\d{1,2}[/-]\d{1,4}|[A-Z][a-z]{2,8}\s+\d{1,2},\s*\d{4})\b"
)
# How far past the label to keep looking. Wide enough to clear an intervening
# column header row, narrow enough not to wander into the next section.
_DOS_LOOKAHEAD = 200
_MRN_RE = re.compile(r"\b(?:mrn|medical record (?:number|no\.?)|patient id)[:\s#]*(?P<m>[A-Za-z0-9\-]{4,20})", re.IGNORECASE)
_NPI_RE = re.compile(r"\bnpi[:\s#]*(?P<n>\d{10})\b", re.IGNORECASE)
_POS_RE = re.compile(r"\b(?:place of service|pos)[:\s]+(?P<p>\d{2}|[A-Za-z ]{3,30})", re.IGNORECASE)
_PAYER_RE = re.compile(r"\b(?:payer|payor|insurance|primary insurance)[:\s]+(?P<p>[A-Za-z0-9 .\-]{3,40})", re.IGNORECASE)


def _find_dos(head: str) -> str | None:
    """Date of service, label-adjacent first, then across a flattened table row.

    The adjacent form is tried first because it is unambiguous. Only when that
    fails do we look ahead from an explicit DOS label for the first date-shaped
    token, which is what rescues EHR screenshot layouts.
    """
    if m := _DOS_RE.search(head):
        return m.group("d").strip()

    for label in _DOS_LABEL_RE.finditer(head):
        window = head[label.end():label.end() + _DOS_LOOKAHEAD]
        if m := _DATE_TOKEN_RE.search(window):
            return m.group("d").strip()
    return None


def parse_header(text: str) -> dict[str, object]:
    """Pull demographics out of the chart. Age and sex gate several audit rules."""
    head = text[:4000]        # header data is near the top in every format we've seen
    out: dict[str, object] = {}

    if m := _AGE_RE.search(head):
        age = m.group("a1") or m.group("a2")
        if age and 0 <= int(age) <= 120:
            out["patient_age"] = int(age)

    if m := _SEX_RE.search(head):
        raw = (m.group("s1") or m.group("s2") or "").lower()
        if raw.startswith(("m", "man")):
            out["patient_sex"] = "M"
        elif raw.startswith(("f", "wom")):
            out["patient_sex"] = "F"

    if dos := _find_dos(head):
        out["date_of_service"] = dos
    if m := _MRN_RE.search(head):
        # Store a hash-prefix, not the real MRN. Re-identification stays in the
        # source-of-truth system; this app only needs to correlate.
        import hashlib
        out["patient_ref"] = "P-" + hashlib.sha256(m.group("m").encode()).hexdigest()[:10]
    if m := _NPI_RE.search(head):
        out["provider_npi"] = m.group("n")
    if m := _POS_RE.search(head):
        out["place_of_service"] = m.group("p").strip()
    if m := _PAYER_RE.search(head):
        out["payer"] = m.group("p").strip()
    return out
