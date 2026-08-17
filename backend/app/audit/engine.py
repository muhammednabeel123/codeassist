"""Audit rule framework.

A rule is a function that receives an immutable `RuleContext` and yields
`FindingOut` objects. Metadata (severity, title, citation, thresholds) comes
from rules.yaml, so tuning is a config change rather than a deploy. Rules are
pure functions over the context: easy to unit test, and easy to explain to an
auditor asking "why did the system flag this claim?"
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

from ..config import RULES_DIR
from ..coding.terminology import Terminology, load_terminology

RISK_WEIGHT = {"blocker": 1.0, "high": 0.7, "medium": 0.4, "low": 0.2, "info": 0.1}


@dataclass
class CodeLineView:
    """A read-only view of one claim line, whatever its origin."""

    system: str
    code: str
    description: str = ""
    units: int = 1
    modifiers: list[str] = field(default_factory=list)
    linked_dx: list[str] = field(default_factory=list)
    rank: int | None = None
    status: str = "proposed"
    origin: str = "suggested"
    confidence: float | None = None
    evidence: list[dict] = field(default_factory=list)
    id: str | None = None

    @property
    def on_claim(self) -> bool:
        """Lines a payer would see: accepted, or proposed and not yet rejected."""
        return self.status in {"accepted", "proposed"}

    @property
    def assertions(self) -> set[str]:
        return {e.get("assertion", "present") for e in self.evidence} or {"present"}


@dataclass
class RuleContext:
    text: str
    lower: str
    sections: dict[str, list[list[int]]]
    pages: list[dict]
    codes: list[CodeLineView]
    term: Terminology
    patient_age: int | None = None
    patient_sex: str | None = None
    source_kind: str = "digital"
    em_estimate: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)

    # --- helpers rules use constantly ---
    @property
    def claim_codes(self) -> list[CodeLineView]:
        return [c for c in self.codes if c.on_claim]

    @property
    def dx_lines(self) -> list[CodeLineView]:
        return [c for c in self.claim_codes if c.system == "ICD10CM"]

    @property
    def proc_lines(self) -> list[CodeLineView]:
        return [c for c in self.claim_codes if c.system in {"CPT", "HCPCS"}]

    @property
    def dx_codes(self) -> list[str]:
        return [c.code for c in self.dx_lines]

    @property
    def proc_codes(self) -> set[str]:
        return {c.code for c in self.proc_lines}

    def has_phrase(self, *phrases: str) -> str | None:
        for p in phrases:
            if p.lower() in self.lower:
                return p
        return None

    def section_text(self, name: str) -> str:
        out = []
        for start, end in self.sections.get(name, []):
            out.append(self.text[start:end])
        return "\n".join(out)


@dataclass
class FindingOut:
    rule_id: str
    title: str
    detail: str
    suggested_action: str = ""
    # Left empty so rules.yaml supplies the severity; a rule sets it explicitly
    # only when it needs to override the configured default for a specific case.
    severity: str = ""
    category: str = ""
    citation: str = ""
    codes_involved: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    risk_score: float = 0.0

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "detail": self.detail,
            "suggested_action": self.suggested_action,
            "severity": self.severity,
            "category": self.category,
            "citation": self.citation,
            "codes_involved": self.codes_involved,
            "evidence": self.evidence,
            "risk_score": self.risk_score,
        }


RuleFn = Callable[[RuleContext], Iterator[FindingOut]]
REGISTRY: dict[str, RuleFn] = {}


def rule(rule_id: str) -> Callable[[RuleFn], RuleFn]:
    def deco(fn: RuleFn) -> RuleFn:
        REGISTRY[rule_id] = fn
        fn.rule_id = rule_id  # type: ignore[attr-defined]
        return fn
    return deco


@functools.lru_cache(maxsize=1)
def load_rule_config(rules_dir: str | None = None) -> dict[str, dict[str, Any]]:
    path = Path(rules_dir or RULES_DIR) / "rules.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("rules", {}) or {}


def build_context(
    *,
    text: str,
    sections: dict[str, list[list[int]]],
    pages: list[dict],
    codes: list[CodeLineView],
    patient_age: int | None = None,
    patient_sex: str | None = None,
    source_kind: str = "digital",
    em_estimate: dict[str, Any] | None = None,
) -> RuleContext:
    return RuleContext(
        text=text,
        lower=text.lower(),
        sections=sections or {},
        pages=pages or [],
        codes=codes,
        term=load_terminology(),
        patient_age=patient_age,
        patient_sex=patient_sex,
        source_kind=source_kind,
        em_estimate=em_estimate or {},
        config=load_rule_config(),
    )


def run_audit(ctx: RuleContext) -> list[FindingOut]:
    """Execute every enabled rule. One failing rule never blocks the rest."""
    import logging

    log = logging.getLogger(__name__)
    findings: list[FindingOut] = []

    for rule_id, fn in REGISTRY.items():
        cfg = ctx.config.get(rule_id, {})
        if cfg.get("enabled") is False:
            continue
        try:
            for f in fn(ctx) or []:
                f.severity = f.severity or cfg.get("severity", "medium")
                f.category = f.category or cfg.get("category", "other")
                f.citation = f.citation or cfg.get("citation", "")
                if not f.risk_score:
                    f.risk_score = RISK_WEIGHT.get(f.severity, 0.3)
                findings.append(f)
        except Exception as exc:  # a broken rule must not fail the whole audit
            log.exception("rule %s failed", rule_id)
            findings.append(FindingOut(
                rule_id=rule_id,
                title=f"Rule {rule_id} could not be evaluated",
                detail=f"{type(exc).__name__}: {exc}",
                suggested_action="Review this claim manually; an automated check did not run.",
                severity="high",
                category="system",
                risk_score=0.5,
            ))

    order = {"blocker": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    findings.sort(key=lambda f: (order.get(f.severity, 9), -f.risk_score, f.rule_id))
    return findings
