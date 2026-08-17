"""The rule library.

Each function is a self-contained check. Read them as the compliance
requirements they encode - a coder or auditor should be able to follow any one
of them without reading the rest of the system.
"""
from __future__ import annotations

import re
from typing import Iterator

from .engine import FindingOut, RuleContext, rule

_LATERAL = re.compile(r"\b(right|left|bilateral|both)\b", re.IGNORECASE)


def _cfg(ctx: RuleContext, rule_id: str) -> dict:
    return ctx.config.get(rule_id, {}) or {}


def _title(ctx: RuleContext, rule_id: str, default: str, **fmt) -> str:
    template = _cfg(ctx, rule_id).get("title", default)
    try:
        return template.format(**fmt)
    except (KeyError, IndexError):
        return default


# --------------------------------------------------------------------------
# Documentation support
# --------------------------------------------------------------------------

@rule("unsupported_code")
def unsupported_code(ctx: RuleContext) -> Iterator[FindingOut]:
    """A code on the claim with nothing in the record behind it.

    This is the single most consequential check in the system. An unsupported
    code is the definition of an improper payment, and in aggregate it is what
    False Claims Act cases are built from.
    """
    for line in ctx.claim_codes:
        if line.evidence:
            continue
        # Coder-entered codes are still expected to be traceable; if the engine
        # found no supporting text, the coder should point at where it is.
        yield FindingOut(
            rule_id="unsupported_code",
            title=_title(ctx, "unsupported_code",
                         f"Code {line.code} has no supporting documentation",
                         code=line.code),
            detail=(
                f"{line.system} {line.code} ({line.description or 'no descriptor'}) "
                f"is on the claim but no text in the record was matched to it. "
                f"Origin: {line.origin}."
            ),
            suggested_action=(
                "Either identify the documentation that supports this code and attach "
                "it as evidence, or remove the line before submission."
            ),
            codes_involved=[line.code],
        )


@rule("negated_or_uncertain_evidence")
def negated_or_uncertain_evidence(ctx: RuleContext) -> Iterator[FindingOut]:
    """Codes whose only support is negated, hypothetical, or somebody else's history."""
    bad = {"negated", "not_patient", "hypothetical", "uncertain"}
    for line in ctx.claim_codes:
        if not line.evidence:
            continue
        assertions = line.assertions
        if assertions - bad:
            continue                       # at least one solid mention exists
        worst = next(a for a in ("negated", "not_patient", "hypothetical", "uncertain")
                     if a in assertions)
        readable = {
            "negated": "negated",
            "not_patient": "another person's (family) history",
            "hypothetical": "hypothetical/contingent",
            "uncertain": "uncertain (rule-out / possible)",
        }[worst]
        yield FindingOut(
            rule_id="negated_or_uncertain_evidence",
            title=_title(ctx, "negated_or_uncertain_evidence",
                         f"Code {line.code} is supported only by {readable} language",
                         code=line.code, assertion=readable),
            detail=(
                f"Every mention supporting {line.code} was classified as {readable}. "
                + ("Uncertain diagnoses may not be coded in the outpatient setting "
                   "(ICD-10-CM Guideline IV.H); inpatient rules differ (II.H)."
                   if worst == "uncertain" else
                   "This condition is not established for this patient at this encounter.")
            ),
            suggested_action="Remove the code, or confirm the condition with the provider.",
            codes_involved=[line.code],
            evidence=line.evidence[:3],
        )


@rule("missing_provider_attestation")
def missing_provider_attestation(ctx: RuleContext) -> Iterator[FindingOut]:
    cfg = _cfg(ctx, "missing_provider_attestation")
    markers = cfg.get("signature_markers", [])
    if any(m.lower() in ctx.lower for m in markers):
        return
    yield FindingOut(
        rule_id="missing_provider_attestation",
        title=_title(ctx, "missing_provider_attestation",
                     "No provider signature or attestation found"),
        detail=(
            "No signature, e-signature, or attestation language was located in the "
            "document. Medicare requires a legible, dated provider signature for "
            "services to be considered rendered; an unsigned note supports nothing."
        ),
        suggested_action=(
            "Confirm the signed version of the note is on file before submitting. "
            "If the scan is missing the final page, re-request the full document."
        ),
    )


# --------------------------------------------------------------------------
# Specificity
# --------------------------------------------------------------------------

@rule("unspecified_code_used")
def unspecified_code_used(ctx: RuleContext) -> Iterator[FindingOut]:
    cfg = _cfg(ctx, "unspecified_code_used")
    if len(ctx.text) < int(cfg.get("min_document_chars", 400)):
        return
    for line in ctx.dx_lines:
        concept = ctx.term.dx.get(line.code)
        if not concept or not concept.unspecified:
            continue
        # If a specificity hint already fired for this code, that finding is
        # strictly more useful - don't emit both.
        if any(h.from_code == line.code and h.trigger_keywords
               and any(k.lower() in ctx.lower for k in h.trigger_keywords)
               for h in ctx.term.specificity):
            continue
        yield FindingOut(
            rule_id="unspecified_code_used",
            title=_title(ctx, "unspecified_code_used",
                         f"Unspecified code {line.code} may be avoidable",
                         code=line.code),
            detail=(
                f"{line.code} - {concept.description}. Unspecified codes are "
                f"appropriate only when the record genuinely does not support more "
                f"detail. Payers increasingly deny or downgrade them, and they "
                f"understate risk adjustment."
            ),
            suggested_action=(
                "Re-read the note for the missing detail. If it is truly absent, "
                "consider a provider query rather than defaulting to unspecified."
            ),
            codes_involved=[line.code],
            evidence=line.evidence[:2],
        )


@rule("specificity_upgrade_available")
def specificity_upgrade_available(ctx: RuleContext) -> Iterator[FindingOut]:
    """The documentation supports a better code than the one on the claim."""
    on_claim = {c.code for c in ctx.dx_lines}
    for hint in ctx.term.specificity:
        if hint.from_code not in on_claim:
            continue
        if hint.to_code in on_claim and hint.to_code != hint.from_code:
            continue
        matched = [k for k in hint.trigger_keywords if k.lower() in ctx.lower]
        if not matched:
            continue
        line = next(c for c in ctx.dx_lines if c.code == hint.from_code)
        evidence = []
        for kw in matched[:2]:
            idx = ctx.lower.find(kw.lower())
            if idx == -1:
                continue
            start = max(0, idx - 120)
            end = min(len(ctx.text), idx + len(kw) + 120)
            evidence.append({
                "char_start": start, "char_end": end,
                "quote": re.sub(r"\s+", " ", ctx.text[start:end]).strip(),
                "why": f'documentation contains "{kw}"',
                "page": _page_of(ctx, start),
            })
        target = ctx.term.dx.get(hint.to_code)
        yield FindingOut(
            rule_id="specificity_upgrade_available",
            title=_title(ctx, "specificity_upgrade_available",
                         f"More specific code available: {hint.from_code} -> {hint.to_code}",
                         from_code=hint.from_code, to_code=hint.to_code),
            detail=(
                f"{hint.prompt.rstrip('.')}. Currently coded: {hint.from_code} "
                f"({line.description}). Supported: {hint.to_code}"
                + (f" ({target.description})." if target else ".")
            ),
            suggested_action=f"Replace {hint.from_code} with {hint.to_code}.",
            severity=hint.severity,
            codes_involved=[hint.from_code, hint.to_code],
            evidence=evidence,
        )


@rule("laterality_documented_code_unspecified")
def laterality_documented_code_unspecified(ctx: RuleContext) -> Iterator[FindingOut]:
    for line in ctx.dx_lines:
        concept = ctx.term.dx.get(line.code)
        if not concept:
            continue
        desc = concept.description.lower()
        if not ("unspecified" in desc and any(
                w in desc for w in ("knee", "ear", "eye", "hip", "shoulder", "limb",
                                    "arm", "leg", "hand", "foot", "breast", "kidney"))):
            continue
        # Look for laterality near the mentions that produced this code. The
        # side is often stated in the adjacent sentence rather than the one
        # naming the condition ("Knee osteoarthritis... injected the right
        # knee today"), so widen past the evidence quote itself.
        found = None
        for ev in line.evidence:
            start = max(0, int(ev.get("char_start", 0)) - 250)
            end = min(len(ctx.text), int(ev.get("char_end", 0)) + 250)
            window = ev.get("quote", "") + " " + ctx.text[start:end]
            if m := _LATERAL.search(window):
                found = m.group(1).lower()
                break
        if not found:
            continue
        yield FindingOut(
            rule_id="laterality_documented_code_unspecified",
            title=_title(ctx, "laterality_documented_code_unspecified",
                         f"{line.code}: laterality is documented but the code does not specify it",
                         code=line.code),
            detail=(
                f'The supporting documentation states "{found}", but {line.code} '
                f"({concept.description}) does not carry laterality. Unspecified-side "
                f"codes are a common denial trigger when the side is in the note."
            ),
            suggested_action=f"Select the {found}-side code for this condition.",
            codes_involved=[line.code],
            evidence=line.evidence[:2],
        )


# --------------------------------------------------------------------------
# Bundling, modifiers, units
# --------------------------------------------------------------------------

@rule("ncci_bundling_conflict")
def ncci_bundling_conflict(ctx: RuleContext) -> Iterator[FindingOut]:
    codes = ctx.proc_codes
    for edit in ctx.term.ptp_for(codes):
        if edit.modifier_allowed:
            continue                       # handled by ncci_modifier_required
        yield FindingOut(
            rule_id="ncci_bundling_conflict",
            title=_title(ctx, "ncci_bundling_conflict",
                         f"{edit.column2} is bundled into {edit.column1}",
                         column1=edit.column1, column2=edit.column2),
            detail=(
                f"{edit.column2} and {edit.column1} are both on the claim. "
                f"{edit.rationale}. This edit does not permit a modifier override, "
                f"so reporting both will be denied and may be treated as unbundling."
            ),
            suggested_action=f"Remove {edit.column2} and report {edit.column1} alone.",
            codes_involved=[edit.column1, edit.column2],
        )


@rule("ncci_modifier_required")
def ncci_modifier_required(ctx: RuleContext) -> Iterator[FindingOut]:
    cfg = _cfg(ctx, "ncci_modifier_required")
    accepted = {m.upper() for m in cfg.get("accepted_modifiers", ["59", "XE", "XS", "XP", "XU"])}
    by_code = {c.code: c for c in ctx.proc_lines}
    for edit in ctx.term.ptp_for(set(by_code)):
        if not edit.modifier_allowed:
            continue
        line = by_code[edit.column2]
        if {m.upper() for m in line.modifiers} & accepted:
            continue
        yield FindingOut(
            rule_id="ncci_modifier_required",
            title=_title(ctx, "ncci_modifier_required",
                         f"{edit.column2} with {edit.column1} requires a distinct-service modifier",
                         column1=edit.column1, column2=edit.column2),
            detail=(
                f"{edit.rationale}. The edit permits an override when the services "
                f"are genuinely separate, but only with a modifier documenting that."
            ),
            suggested_action=(
                f"If the documentation shows a separate site, session, or lesion, append "
                f"the most specific X{{EPSU}} modifier (preferred over 59) to {edit.column2}. "
                f"If not, remove {edit.column2}."
            ),
            codes_involved=[edit.column1, edit.column2],
        )


@rule("mue_exceeded")
def mue_exceeded(ctx: RuleContext) -> Iterator[FindingOut]:
    for line in ctx.proc_lines:
        concept = ctx.term.proc.get(line.code)
        if not concept or line.units <= concept.mue:
            continue
        yield FindingOut(
            rule_id="mue_exceeded",
            title=_title(ctx, "mue_exceeded",
                         f"{line.code}: {line.units} units exceeds the MUE of {concept.mue}",
                         code=line.code, units=line.units, mue=concept.mue),
            detail=(
                f"{line.code} ({concept.description}) is reported with {line.units} "
                f"units against a Medically Unlikely Edit of {concept.mue}. Units above "
                f"the MUE are denied outright on most edit types."
            ),
            suggested_action=(
                "Verify the units against the documentation. If the quantity is correct "
                "and clinically justified, this needs a documented rationale and, where "
                "the MAI permits, separate lines with appropriate modifiers."
            ),
            codes_involved=[line.code],
        )


@rule("em_with_minor_procedure_needs_25")
def em_with_minor_procedure_needs_25(ctx: RuleContext) -> Iterator[FindingOut]:
    """An E/M billed the same day as a minor procedure needs modifier 25."""
    em_lines = [c for c in ctx.proc_lines
                if (p := ctx.term.proc.get(c.code)) and p.is_em]
    minor = [c for c in ctx.proc_lines
             if (p := ctx.term.proc.get(c.code)) and p.is_minor_procedure]
    if not em_lines or not minor:
        return
    for em in em_lines:
        if "25" in [m.strip() for m in em.modifiers]:
            continue
        yield FindingOut(
            rule_id="em_with_minor_procedure_needs_25",
            title=_title(ctx, "em_with_minor_procedure_needs_25",
                         f"E/M {em.code} reported with same-day procedure "
                         f"{minor[0].code} needs modifier 25",
                         em_code=em.code, proc_code=minor[0].code),
            detail=(
                f"{em.code} is on the claim with minor procedure(s) "
                f"{', '.join(m.code for m in minor)} (global period <= 10 days). "
                f"Without modifier 25 the E/M is bundled into the procedure's global "
                f"package and will be denied."
            ),
            suggested_action=(
                "If the E/M was a significant, separately identifiable service beyond "
                "the usual pre/post work of the procedure, append modifier 25 to "
                f"{em.code} and make sure the note documents that separate work. "
                "If it was not, remove the E/M."
            ),
            codes_involved=[em.code] + [m.code for m in minor],
        )


@rule("bilateral_not_reported")
def bilateral_not_reported(ctx: RuleContext) -> Iterator[FindingOut]:
    cfg = _cfg(ctx, "bilateral_not_reported")
    triggers = [t.lower() for t in cfg.get("triggers", ["bilateral"])]
    # Scope matters: "clear to auscultation bilaterally" in the lung exam says
    # nothing about how a knee injection was performed. Only procedure-bearing
    # sections count, and "bilaterally" is not "bilateral".
    scope = " ".join(
        ctx.section_text(name) for name in ("procedures", "assessment", "plan")
    ).lower()
    if not scope:
        scope = ctx.lower
    hit = next((t for t in triggers
                if re.search(r"\b" + re.escape(t) + r"\b", scope)), None)
    if not hit:
        return
    for line in ctx.proc_lines:
        concept = ctx.term.proc.get(line.code)
        if not concept or not concept.bilateral_eligible:
            continue
        mods = {m.strip() for m in line.modifiers}
        if "50" in mods or {"LT", "RT"} <= mods or line.units >= 2:
            continue
        yield FindingOut(
            rule_id="bilateral_not_reported",
            title=_title(ctx, "bilateral_not_reported",
                         f"{line.code}: bilateral service documented but not reported as bilateral",
                         code=line.code),
            detail=(
                f'The record contains "{hit}" and {line.code} '
                f"({concept.description}) is a bilateral-eligible unilateral code "
                f"reported with 1 unit and no 50/LT/RT modifier. This under-reports "
                f"the work actually performed."
            ),
            suggested_action=(
                f"Confirm the service was performed bilaterally, then report {line.code} "
                f"with modifier 50 (or LT and RT on separate lines) per payer preference."
            ),
            codes_involved=[line.code],
        )


@rule("duplicate_code_line")
def duplicate_code_line(ctx: RuleContext) -> Iterator[FindingOut]:
    seen: dict[str, list] = {}
    for line in ctx.claim_codes:
        seen.setdefault(line.code, []).append(line)
    for code, lines in seen.items():
        if len(lines) < 2:
            continue
        mod_sets = [tuple(sorted(m.strip().upper() for m in l.modifiers)) for l in lines]
        if len(set(mod_sets)) == len(mod_sets) and all(mod_sets):
            continue                       # distinguished by modifiers
        yield FindingOut(
            rule_id="duplicate_code_line",
            title=_title(ctx, "duplicate_code_line",
                         f"{code} appears on more than one line without a "
                         f"distinguishing modifier", code=code),
            detail=f"{code} appears {len(lines)} times with the same (or no) modifiers.",
            suggested_action=(
                "Consolidate into one line with the correct unit count, or add the "
                "modifier that distinguishes the services."
            ),
            codes_involved=[code],
        )


@rule("time_based_code_without_time")
def time_based_code_without_time(ctx: RuleContext) -> Iterator[FindingOut]:
    cfg = _cfg(ctx, "time_based_code_without_time")
    timed = set(cfg.get("time_based_codes", []))
    if not timed:
        return
    has_time = re.search(r"\b\d{1,3}\s*(?:min|minutes)\b", ctx.lower) is not None
    if has_time:
        return
    for line in ctx.proc_lines:
        if line.code not in timed:
            continue
        concept = ctx.term.proc.get(line.code)
        yield FindingOut(
            rule_id="time_based_code_without_time",
            title=_title(ctx, "time_based_code_without_time",
                         f"{line.code} is time-based but no time is documented",
                         code=line.code),
            detail=(
                f"{line.code} ({concept.description if concept else ''}) is defined by "
                f"time spent, and no duration appears anywhere in the record."
            ),
            suggested_action=(
                "Query the provider to document the time spent, or remove the code. "
                "Time-based codes without a documented duration are indefensible on audit."
            ),
            codes_involved=[line.code],
        )


# --------------------------------------------------------------------------
# Demographics
# --------------------------------------------------------------------------

@rule("sex_conflict")
def sex_conflict(ctx: RuleContext) -> Iterator[FindingOut]:
    if not ctx.patient_sex or ctx.patient_sex not in {"M", "F"}:
        return
    for line in ctx.claim_codes:
        concept = (ctx.term.dx.get(line.code) if line.system == "ICD10CM"
                   else ctx.term.proc.get(line.code))
        expected = getattr(concept, "sex", None)
        if not expected or expected == ctx.patient_sex:
            continue
        yield FindingOut(
            rule_id="sex_conflict",
            title=_title(ctx, "sex_conflict",
                         f"{line.code} conflicts with the documented patient sex "
                         f"({ctx.patient_sex})",
                         code=line.code, patient_sex=ctx.patient_sex),
            detail=(
                f"{line.code} ({getattr(concept, 'description', '')}) is restricted to "
                f"sex {expected}, but the record documents {ctx.patient_sex}. Either the "
                f"code is wrong or the demographics captured from the chart are wrong."
            ),
            suggested_action=(
                "Verify the patient's sex in the source system and correct whichever "
                "side is wrong. This edit denies automatically."
            ),
            codes_involved=[line.code],
        )


@rule("age_conflict")
def age_conflict(ctx: RuleContext) -> Iterator[FindingOut]:
    if ctx.patient_age is None:
        return
    for line in ctx.claim_codes:
        concept = (ctx.term.dx.get(line.code) if line.system == "ICD10CM"
                   else ctx.term.proc.get(line.code))
        if concept is None:
            continue
        lo, hi = getattr(concept, "age_min", 0), getattr(concept, "age_max", 120)
        if lo <= ctx.patient_age <= hi:
            continue
        yield FindingOut(
            rule_id="age_conflict",
            title=_title(ctx, "age_conflict",
                         f"{line.code} is outside the expected age range for a "
                         f"{ctx.patient_age}-year-old",
                         code=line.code, patient_age=ctx.patient_age),
            detail=(
                f"{line.code} ({concept.description}) applies to ages {lo}-{hi}; the "
                f"documented age is {ctx.patient_age}."
            ),
            suggested_action=(
                "Confirm the patient's age and select the age-appropriate code "
                "(preventive-medicine and wellness codes are strictly age-banded)."
            ),
            codes_involved=[line.code],
        )


# --------------------------------------------------------------------------
# Medical necessity and linkage
# --------------------------------------------------------------------------

@rule("medical_necessity_unsupported")
def medical_necessity_unsupported(ctx: RuleContext) -> Iterator[FindingOut]:
    dx_codes = ctx.dx_codes
    for line in ctx.proc_lines:
        policy = ctx.term.necessity.get(line.code)
        if not policy:
            continue
        if policy.satisfied_by(dx_codes):
            continue
        yield FindingOut(
            rule_id="medical_necessity_unsupported",
            title=_title(ctx, "medical_necessity_unsupported",
                         f"{line.code} has no diagnosis on the claim that supports "
                         f"medical necessity", code=line.code),
            detail=(
                f"{policy.policy}. Diagnoses currently on the claim: "
                f"{', '.join(dx_codes) or 'none'}. Expected one beginning with: "
                f"{', '.join(policy.allowed_prefixes)}."
            ),
            suggested_action=(
                "Add the supporting diagnosis if it is documented. If no supporting "
                "diagnosis exists, the service is not billable to the payer - consider "
                "an ABN/waiver where applicable."
            ),
            codes_involved=[line.code],
        )


@rule("procedure_not_linked_to_dx")
def procedure_not_linked_to_dx(ctx: RuleContext) -> Iterator[FindingOut]:
    if not ctx.dx_lines:
        return                             # missing_primary_diagnosis covers this
    for line in ctx.proc_lines:
        if line.linked_dx:
            continue
        yield FindingOut(
            rule_id="procedure_not_linked_to_dx",
            title=_title(ctx, "procedure_not_linked_to_dx",
                         f"{line.code} is not linked to any diagnosis line",
                         code=line.code),
            detail=(
                f"{line.code} has no diagnosis pointer. Every service line must point "
                f"to at least one diagnosis that justifies it (claim item 24E)."
            ),
            suggested_action="Link the diagnosis that establishes why this service was needed.",
            codes_involved=[line.code],
        )


@rule("missing_primary_diagnosis")
def missing_primary_diagnosis(ctx: RuleContext) -> Iterator[FindingOut]:
    if any(c.rank == 1 for c in ctx.dx_lines):
        return
    if not ctx.dx_lines:
        yield FindingOut(
            rule_id="missing_primary_diagnosis",
            title=_title(ctx, "missing_primary_diagnosis",
                         "No primary diagnosis is established"),
            detail=(
                "The claim has no diagnosis codes at all. Either the documentation "
                "does not state a codable condition, or extraction failed on this "
                "document (check whether it was read by OCR)."
            ),
            suggested_action="Establish and rank the first-listed diagnosis before submission.",
        )
        return
    yield FindingOut(
        rule_id="missing_primary_diagnosis",
        title="Diagnosis ranking is not established",
        detail=(
            f"{len(ctx.dx_lines)} diagnoses are present but none is ranked first. "
            f"The first-listed diagnosis must be the condition chiefly responsible "
            f"for the encounter."
        ),
        suggested_action="Set the primary diagnosis.",
        severity="high",
    )


@rule("screening_z_code_not_primary")
def screening_z_code_not_primary(ctx: RuleContext) -> Iterator[FindingOut]:
    cfg = _cfg(ctx, "screening_z_code_not_primary")
    screening = set(cfg.get("screening_procedures", []))
    if not (ctx.proc_codes & screening or "Z12.11" in ctx.dx_codes):
        return
    # Only fire when this really looks like a screening encounter.
    if not ctx.has_phrase("screening", "average risk", "asymptomatic"):
        return
    z_line = next((c for c in ctx.dx_lines if c.code.startswith("Z12")), None)
    if z_line is None:
        return
    if z_line.rank == 1:
        return
    yield FindingOut(
        rule_id="screening_z_code_not_primary",
        title=_title(ctx, "screening_z_code_not_primary",
                     f"Screening encounter: {z_line.code} should be the first-listed diagnosis",
                     code=z_line.code),
        detail=(
            f"{z_line.code} is currently ranked {z_line.rank}. For a screening "
            f"encounter the screening Z code is sequenced first; findings discovered "
            f"during the screening are reported as additional diagnoses. Sequencing "
            f"this wrong converts a covered preventive service into a diagnostic one "
            f"and shifts cost to the patient."
        ),
        suggested_action=f"Re-sequence {z_line.code} to the primary position.",
        codes_involved=[z_line.code],
    )


# --------------------------------------------------------------------------
# E/M level
# --------------------------------------------------------------------------

def _em_line(ctx: RuleContext):
    for line in ctx.proc_lines:
        concept = ctx.term.proc.get(line.code)
        if concept and concept.is_em:
            return line, concept
    return None, None


@rule("em_level_above_documentation")
def em_level_above_documentation(ctx: RuleContext) -> Iterator[FindingOut]:
    line, concept = _em_line(ctx)
    est = ctx.em_estimate
    if not line or not concept or not est or concept.em_level is None:
        return
    supported = int(est.get("level", 0))
    if concept.em_level <= supported:
        return
    delta = concept.em_level - supported
    yield FindingOut(
        rule_id="em_level_above_documentation",
        title=_title(ctx, "em_level_above_documentation",
                     f"E/M {line.code} appears higher than the documentation supports",
                     code=line.code),
        detail=(
            f"{line.code} is a level-{concept.em_level + 1} service. The documented "
            f"medical decision making supports approximately level {supported + 1}: "
            + "; ".join(est.get("rationale", [])) + "."
        ),
        suggested_action=(
            "Re-read the MDM elements. If the work was genuinely higher, the note must "
            "say so - a provider query is the right remedy, not a level change. "
            "Otherwise reduce the level."
        ),
        severity="blocker" if delta >= 2 else "high",
        codes_involved=[line.code],
        risk_score=0.7 + 0.15 * delta,
    )


@rule("em_level_below_documentation")
def em_level_below_documentation(ctx: RuleContext) -> Iterator[FindingOut]:
    line, concept = _em_line(ctx)
    est = ctx.em_estimate
    if not line or not concept or not est or concept.em_level is None:
        return
    supported = int(est.get("level", 0))
    if supported <= concept.em_level:
        return
    yield FindingOut(
        rule_id="em_level_below_documentation",
        title=_title(ctx, "em_level_below_documentation",
                     f"E/M {line.code} may be under-coded for the documented work",
                     code=line.code),
        detail=(
            f"The documentation supports approximately level {supported + 1} while "
            f"{line.code} is level {concept.em_level + 1}: "
            + "; ".join(est.get("rationale", [])) + "."
        ),
        suggested_action=(
            "Confirm the MDM elements and raise the level if the documentation truly "
            "supports it. Chronic under-coding is a real revenue leak and also "
            "distorts risk adjustment."
        ),
        codes_involved=[line.code],
    )


# --------------------------------------------------------------------------
# Completeness / revenue integrity
# --------------------------------------------------------------------------

@rule("bmi_code_missing")
def bmi_code_missing(ctx: RuleContext) -> Iterator[FindingOut]:
    if not any(c.code.startswith("E66") for c in ctx.dx_lines):
        return
    if any(c.code.startswith("Z68") for c in ctx.dx_lines):
        return
    if not re.search(r"\bbmi\b", ctx.lower):
        return
    yield FindingOut(
        rule_id="bmi_code_missing",
        title=_title(ctx, "bmi_code_missing",
                     "Morbid obesity coded without a BMI (Z68.-) code"),
        detail=(
            "An obesity diagnosis is on the claim and a BMI value appears in the "
            "record, but no Z68.- code is reported. The BMI code may be taken from "
            "any clinician's documentation, but the obesity diagnosis itself must "
            "come from the provider."
        ),
        suggested_action="Add the matching Z68.- code as a secondary diagnosis.",
    )


@rule("long_term_drug_use_missing")
def long_term_drug_use_missing(ctx: RuleContext) -> Iterator[FindingOut]:
    diabetes = any(c.code.startswith(("E10", "E11", "E13")) for c in ctx.dx_lines)
    if not diabetes:
        return
    on_insulin = ctx.has_phrase("insulin", "lantus", "glargine", "novolog", "humalog")
    on_oral = ctx.has_phrase("metformin", "glipizide", "januvia", "jardiance", "sitagliptin")
    present = {c.code for c in ctx.dx_lines}
    missing = []
    if on_insulin and "Z79.4" not in present:
        missing.append(("Z79.4", "long-term insulin use"))
    if on_oral and "Z79.84" not in present:
        missing.append(("Z79.84", "long-term oral hypoglycemic use"))
    for code, label in missing:
        yield FindingOut(
            rule_id="long_term_drug_use_missing",
            title=_title(ctx, "long_term_drug_use_missing",
                         f"{label.capitalize()} documented but {code} not reported"),
            detail=(
                f"The medication list indicates {label}, which is separately reportable "
                f"with {code} and affects risk adjustment."
            ),
            suggested_action=f"Add {code} as a secondary diagnosis.",
            codes_involved=[code],
        )


@rule("billable_service_not_captured")
def billable_service_not_captured(ctx: RuleContext) -> Iterator[FindingOut]:
    cfg = _cfg(ctx, "billable_service_not_captured")
    watch: dict[str, str] = cfg.get("watch", {}) or {}
    present_dx = set(ctx.dx_codes)
    for dx_code, proc_code in watch.items():
        if dx_code not in present_dx or proc_code in ctx.proc_codes:
            continue
        concept = ctx.term.proc.get(proc_code)
        yield FindingOut(
            rule_id="billable_service_not_captured",
            title=_title(ctx, "billable_service_not_captured",
                         f"Documented service {proc_code} is not on the claim",
                         code=proc_code),
            detail=(
                f"{dx_code} is reported, which commonly accompanies {proc_code} "
                f"({concept.description if concept else ''}). If that service was "
                f"performed and documented, it is separately billable."
            ),
            suggested_action=(
                f"Check the note for the elements {proc_code} requires and add the line "
                f"if they are met. Do not add it speculatively."
            ),
            codes_involved=[dx_code, proc_code],
        )


# --------------------------------------------------------------------------
# CDI / provider queries
# --------------------------------------------------------------------------

@rule("documentation_gap_query")
def documentation_gap_query(ctx: RuleContext) -> Iterator[FindingOut]:
    cfg = _cfg(ctx, "documentation_gap_query")
    gaps: dict = cfg.get("gaps", {}) or {}
    present = {c.code for c in ctx.dx_lines}
    for concept_phrase, spec in gaps.items():
        if concept_phrase.lower() not in ctx.lower:
            continue
        trigger_codes = spec.get("codes", []) or []
        # Fire when the vague code is what we ended up with, or when the concept
        # is mentioned and nothing specific was coded for it.
        vague_on_claim = any(c in present for c in trigger_codes)
        if trigger_codes and not vague_on_claim:
            continue
        idx = ctx.lower.find(concept_phrase.lower())
        start = max(0, idx - 150)
        end = min(len(ctx.text), idx + len(concept_phrase) + 150)
        yield FindingOut(
            rule_id="documentation_gap_query",
            title=_title(ctx, "documentation_gap_query",
                         f"Documentation too vague to support a specific code: {concept_phrase}",
                         topic=concept_phrase),
            detail=(
                f'"{concept_phrase}" is documented but the note does not state '
                f"{spec.get('needs', 'the required detail')}. Without it only the "
                f"unspecified code is supportable."
            ),
            suggested_action=(
                "Send a compliant (non-leading) provider query. A draft is available "
                "on this finding."
            ),
            codes_involved=trigger_codes,
            evidence=[{
                "char_start": start, "char_end": end,
                "quote": re.sub(r"\s+", " ", ctx.text[start:end]).strip(),
                "why": f'concept "{concept_phrase}" documented without required detail',
                "page": _page_of(ctx, start),
            }],
        )


# --------------------------------------------------------------------------
# Data quality
# --------------------------------------------------------------------------

@rule("ocr_quality_warning")
def ocr_quality_warning(ctx: RuleContext) -> Iterator[FindingOut]:
    cfg = _cfg(ctx, "ocr_quality_warning")
    threshold = float(cfg.get("min_confidence", 78))
    for page in ctx.pages:
        if not page.get("ocr"):
            continue
        conf = page.get("confidence")
        if conf is None or conf >= threshold:
            continue
        yield FindingOut(
            rule_id="ocr_quality_warning",
            title=_title(ctx, "ocr_quality_warning",
                         f"Page {page['page']} was read by OCR with low confidence "
                         f"({conf}%)", page=page["page"], confidence=conf),
            detail=(
                f"Page {page['page']} had no usable text layer and OCR returned a mean "
                f"word confidence of {conf}%. Codes derived from this page may rest on "
                f"misread text - a wrong digit in a lab value or a dropped 'no' changes "
                f"the coding entirely."
            ),
            suggested_action=(
                "Read this page visually before accepting any code sourced from it. "
                "Consider requesting a cleaner copy from the sending facility."
            ),
        )


def _page_of(ctx: RuleContext, offset: int) -> int | None:
    for p in ctx.pages:
        if p.get("char_start", 0) <= offset < p.get("char_end", 0):
            return p.get("page")
    return None
