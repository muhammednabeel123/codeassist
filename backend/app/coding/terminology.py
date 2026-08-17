"""Terminology and edit tables, loaded from CSV.

Why CSV and not a hardcoded dict: code sets change quarterly (ICD-10-CM on
1 Oct, CPT on 1 Jan, NCCI quarterly) and CPT/HCPCS descriptors are licensed
content. Your organisation drops its licensed release into `reference/` and
nothing in the application code changes. The files shipped here are a small
demo subset with paraphrased descriptors - see docs/LICENSING.md.
"""
from __future__ import annotations

import csv
import functools
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import REFERENCE_DIR


def _split(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split("|") if v.strip()]


def _int(value: str, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class DxConcept:
    code: str
    description: str
    unspecified: bool
    sex: str | None
    age_min: int
    age_max: int
    keywords: list[str] = field(default_factory=list)

    @property
    def chapter_prefix(self) -> str:
        return self.code[:3]


@dataclass
class ProcConcept:
    code: str
    system: str                     # CPT | HCPCS
    description: str
    keywords: list[str]
    global_days: int
    mue: int
    sex: str | None
    age_min: int
    age_max: int
    category: str
    bilateral_eligible: bool
    professional_component: bool
    em_level: int | None
    em_type: str | None

    @property
    def is_em(self) -> bool:
        return self.category == "em"

    @property
    def is_minor_procedure(self) -> bool:
        """0- or 10-day global => an E/M on the same day needs modifier 25."""
        return self.category in {"procedure", "endoscopy", "therapy"} and self.global_days <= 10


@dataclass
class PtpEdit:
    column1: str
    column2: str
    modifier_allowed: bool
    rationale: str


@dataclass
class SpecificityHint:
    from_code: str
    to_code: str
    trigger_keywords: list[str]
    prompt: str
    severity: str


@dataclass
class NecessityPolicy:
    code: str
    allowed_prefixes: list[str]
    policy: str

    def satisfied_by(self, dx_codes: list[str]) -> bool:
        return any(
            dx.upper().startswith(pref.upper())
            for dx in dx_codes
            for pref in self.allowed_prefixes
        )


@dataclass
class Terminology:
    dx: dict[str, DxConcept]
    proc: dict[str, ProcConcept]
    ptp: list[PtpEdit]
    specificity: list[SpecificityHint]
    necessity: dict[str, NecessityPolicy]
    # keyword -> codes, longest phrase first so "diabetic polyneuropathy" wins
    # over "diabetes" at the same offset
    dx_index: list[tuple[str, str]] = field(default_factory=list)
    proc_index: list[tuple[str, str]] = field(default_factory=list)

    def ptp_for(self, codes: set[str]) -> list[PtpEdit]:
        return [e for e in self.ptp if e.column1 in codes and e.column2 in codes]

    def describe(self, system: str, code: str) -> str:
        if system == "ICD10CM":
            c = self.dx.get(code)
            return c.description if c else ""
        p = self.proc.get(code)
        return p.description if p else ""


def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _load_proc(rows: list[dict[str, str]], system: str) -> dict[str, ProcConcept]:
    out: dict[str, ProcConcept] = {}
    for r in rows:
        code = (r.get("code") or "").strip()
        if not code:
            continue
        out[code] = ProcConcept(
            code=code,
            system=system,
            description=(r.get("description") or "").strip(),
            keywords=_split(r.get("keywords", "")),
            global_days=_int(r.get("global_days"), 0) or 0,
            mue=_int(r.get("mue"), 1) or 1,
            sex=(r.get("sex") or "").strip().upper() or None,
            age_min=_int(r.get("age_min"), 0) or 0,
            age_max=_int(r.get("age_max"), 120) or 120,
            category=(r.get("category") or "other").strip(),
            bilateral_eligible=(r.get("bilateral_eligible") or "0").strip() == "1",
            professional_component=(r.get("professional_component") or "0").strip() == "1",
            em_level=_int(r.get("em_level")),
            em_type=(r.get("em_type") or "").strip() or None,
        )
    return out


@functools.lru_cache(maxsize=1)
def load_terminology(reference_dir: str | None = None) -> Terminology:
    base = Path(reference_dir) if reference_dir else REFERENCE_DIR

    dx: dict[str, DxConcept] = {}
    for r in _read(base / "icd10cm.csv"):
        code = (r.get("code") or "").strip()
        if not code:
            continue
        dx[code] = DxConcept(
            code=code,
            description=(r.get("description") or "").strip(),
            unspecified=(r.get("unspecified") or "0").strip() == "1",
            sex=(r.get("sex") or "").strip().upper() or None,
            age_min=_int(r.get("age_min"), 0) or 0,
            age_max=_int(r.get("age_max"), 120) or 120,
            keywords=_split(r.get("keywords", "")),
        )

    proc = _load_proc(_read(base / "cpt.csv"), "CPT")
    proc.update(_load_proc(_read(base / "hcpcs.csv"), "HCPCS"))

    ptp = [
        PtpEdit(
            column1=r["column1"].strip(),
            column2=r["column2"].strip(),
            modifier_allowed=(r.get("modifier_allowed") or "0").strip() == "1",
            rationale=(r.get("rationale") or "").strip(),
        )
        for r in _read(base / "ncci_ptp.csv")
        if r.get("column1")
    ]

    specificity = [
        SpecificityHint(
            from_code=r["from_code"].strip(),
            to_code=r["to_code"].strip(),
            trigger_keywords=_split(r.get("trigger_keywords", "")),
            prompt=(r.get("prompt") or "").strip(),
            severity=(r.get("severity") or "medium").strip(),
        )
        for r in _read(base / "specificity.csv")
        if r.get("from_code")
    ]

    necessity = {
        r["code"].strip(): NecessityPolicy(
            code=r["code"].strip(),
            allowed_prefixes=_split(r.get("allowed_icd10_prefixes", "")),
            policy=(r.get("policy") or "").strip(),
        )
        for r in _read(base / "necessity.csv")
        if r.get("code")
    }

    term = Terminology(dx=dx, proc=proc, ptp=ptp, specificity=specificity,
                       necessity=necessity)

    dx_index = [(kw.lower(), c.code) for c in dx.values() for kw in c.keywords]
    # Also index the descriptor itself when it is short enough to be a phrase
    # someone would actually write in a note.
    for c in dx.values():
        d = c.description.lower()
        if len(d) <= 40 and "unspecified" not in d:
            dx_index.append((d, c.code))
    term.dx_index = sorted(set(dx_index), key=lambda kv: -len(kv[0]))

    proc_index = [(kw.lower(), p.code) for p in proc.values() for kw in p.keywords]
    term.proc_index = sorted(set(proc_index), key=lambda kv: -len(kv[0]))
    return term


def compile_index(index: list[tuple[str, str]]) -> list[tuple[re.Pattern[str], str, str]]:
    """Word-boundary patterns for each keyword phrase, longest first."""
    out = []
    for phrase, code in index:
        # Tolerate variable whitespace and an optional trailing 's'.
        pattern = r"\b" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"s?\b"
        out.append((re.compile(pattern, re.IGNORECASE), phrase, code))
    return out
