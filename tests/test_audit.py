"""Audit rules. Each test states the compliance requirement it protects."""
from __future__ import annotations

import pytest

from app.audit import rules as _rules  # noqa: F401  (registers rules)
from app.audit.cdi import draft_query
from app.audit.engine import CodeLineView, build_context, run_audit


def audit(text="", codes=(), **kw):
    ctx = build_context(
        text=text, sections=kw.pop("sections", {}), pages=kw.pop("pages", []),
        codes=list(codes), **kw,
    )
    return {f.rule_id: f for f in run_audit(ctx)}


def line(code, system="ICD10CM", **kw):
    kw.setdefault("evidence", [{"quote": "documented", "assertion": "present",
                                "char_start": 0, "char_end": 10}])
    return CodeLineView(system=system, code=code, **kw)


BASE_TEXT = "Electronically signed by A. Whitfield, MD. " + "Assessment and plan. " * 30


def test_unsupported_code_is_a_blocker():
    """A code with no documentation behind it is an improper payment."""
    out = audit(BASE_TEXT, [CodeLineView(system="ICD10CM", code="I10", evidence=[])])
    assert "unsupported_code" in out
    assert out["unsupported_code"].severity == "blocker"


def test_negated_evidence_blocks_the_code():
    """'Denies chest pain' must never become R07.9."""
    ev = [{"quote": "He denies chest pain.", "assertion": "negated",
           "char_start": 0, "char_end": 20}]
    out = audit(BASE_TEXT, [line("R07.9", evidence=ev)])
    assert "negated_or_uncertain_evidence" in out
    assert out["negated_or_uncertain_evidence"].severity == "blocker"


def test_family_history_evidence_blocks_the_code():
    ev = [{"quote": "Mother with breast cancer.", "assertion": "not_patient",
           "char_start": 0, "char_end": 25}]
    out = audit(BASE_TEXT, [line("C50.911", evidence=ev)])
    assert "negated_or_uncertain_evidence" in out


def test_present_mention_alongside_negated_one_is_fine():
    ev = [{"quote": "denies pneumonia", "assertion": "negated", "char_start": 0, "char_end": 5},
          {"quote": "treating pneumonia", "assertion": "present", "char_start": 6, "char_end": 12}]
    out = audit(BASE_TEXT, [line("J18.9", evidence=ev)])
    assert "negated_or_uncertain_evidence" not in out


def test_ncci_bundling_is_detected():
    """76942 is included in 20611; billing both is unbundling."""
    out = audit(BASE_TEXT, [line("20611", "CPT"), line("76942", "CPT")])
    f = out["ncci_bundling_conflict"]
    assert f.severity == "blocker"
    assert set(f.codes_involved) == {"20611", "76942"}


def test_ncci_modifier_required_when_override_is_allowed():
    out = audit(BASE_TEXT, [line("29881", "CPT"), line("29874", "CPT")])
    assert "ncci_modifier_required" in out
    assert "ncci_bundling_conflict" not in out


def test_ncci_modifier_present_clears_the_finding():
    out = audit(BASE_TEXT, [line("29881", "CPT"),
                            line("29874", "CPT", modifiers=["XS"])])
    assert "ncci_modifier_required" not in out


def test_em_with_minor_procedure_requires_modifier_25():
    out = audit(BASE_TEXT, [line("99214", "CPT"), line("20610", "CPT")])
    assert "em_with_minor_procedure_needs_25" in out
    cleared = audit(BASE_TEXT, [line("99214", "CPT", modifiers=["25"]),
                                line("20610", "CPT")])
    assert "em_with_minor_procedure_needs_25" not in cleared


def test_mue_exceeded():
    out = audit(BASE_TEXT, [line("20610", "CPT", units=5)])
    assert "mue_exceeded" in out
    assert "20610" in out["mue_exceeded"].codes_involved


def test_sex_conflict_is_a_blocker():
    out = audit(BASE_TEXT, [line("N40.1")], patient_sex="F", patient_age=60)
    assert out["sex_conflict"].severity == "blocker"


def test_age_conflict():
    out = audit(BASE_TEXT, [line("99397", "CPT")], patient_age=42)
    assert "age_conflict" in out


def test_medical_necessity_requires_a_supporting_diagnosis():
    """An A1c without a diabetes-family diagnosis is not medically necessary."""
    out = audit(BASE_TEXT, [line("83036", "CPT"), line("M54.50")])
    assert "medical_necessity_unsupported" in out
    ok = audit(BASE_TEXT, [line("83036", "CPT"), line("E11.9")])
    assert "medical_necessity_unsupported" not in ok


def test_specificity_upgrade_fires_on_documented_detail():
    text = BASE_TEXT + " Echocardiogram shows an EF of 35 percent with global hypokinesis."
    out = audit(text, [line("I50.9", rank=1)])
    f = out["specificity_upgrade_available"]
    assert "I50.22" in f.codes_involved
    assert f.severity == "high"


def test_specificity_not_suggested_when_specific_code_already_present():
    text = BASE_TEXT + " EF of 35 percent."
    out = audit(text, [line("I50.9", rank=1), line("I50.22")])
    assert "specificity_upgrade_available" not in out


def test_laterality_documented_but_code_unspecified():
    ev = [{"quote": "Injection performed in the right knee.", "assertion": "present",
           "char_start": 0, "char_end": 37}]
    out = audit(BASE_TEXT, [line("M17.10", rank=1, evidence=ev)])
    assert "laterality_documented_code_unspecified" in out


def test_bilateral_language_in_the_lung_exam_does_not_trigger_modifier_50():
    """Scope discipline: 'clear to auscultation bilaterally' is not a bilateral knee."""
    text = ("PHYSICAL EXAM:\nLungs clear to auscultation bilaterally.\n\n"
            "PROCEDURE:\nArthrocentesis of the right knee performed.\n"
            "Electronically signed by A. Whitfield, MD.\n")
    sections = {"physical_exam": [[14, 55]], "procedures": [[57, 110]]}
    out = audit(text, [line("20610", "CPT")], sections=sections)
    assert "bilateral_not_reported" not in out


def test_missing_signature_is_flagged():
    out = audit("ASSESSMENT:\nHypertension. " * 20, [line("I10", rank=1)])
    assert "missing_provider_attestation" in out


def test_signature_present_clears_the_finding():
    out = audit(BASE_TEXT, [line("I10", rank=1)])
    assert "missing_provider_attestation" not in out


def test_missing_primary_diagnosis_when_no_codes():
    out = audit(BASE_TEXT, [])
    assert out["missing_primary_diagnosis"].severity == "blocker"


def test_screening_z_code_must_be_first_listed():
    text = BASE_TEXT + " Screening colonoscopy in an average risk patient."
    out = audit(text, [line("K21.9", rank=1), line("Z12.11", rank=2),
                       line("G0121", "HCPCS")])
    assert "screening_z_code_not_primary" in out


def test_duplicate_lines_without_modifiers():
    out = audit(BASE_TEXT, [line("96372", "CPT"), line("96372", "CPT")])
    assert "duplicate_code_line" in out


def test_duplicate_lines_with_distinct_modifiers_are_allowed():
    out = audit(BASE_TEXT, [line("96372", "CPT", modifiers=["59"]),
                            line("96372", "CPT", modifiers=["XU"])])
    assert "duplicate_code_line" not in out


def test_ocr_low_confidence_warning():
    pages = [{"page": 1, "ocr": True, "confidence": 61.0,
              "char_start": 0, "char_end": 10}]
    out = audit(BASE_TEXT, [line("I10", rank=1)], pages=pages)
    assert "ocr_quality_warning" in out


def test_high_confidence_ocr_does_not_warn():
    pages = [{"page": 1, "ocr": True, "confidence": 95.0,
              "char_start": 0, "char_end": 10}]
    out = audit(BASE_TEXT, [line("I10", rank=1)], pages=pages)
    assert "ocr_quality_warning" not in out


def test_em_level_above_documentation():
    """A level-5 visit on a one-problem, no-data, low-risk note is upcoding."""
    est = {"level": 1, "rationale": ["1 problem addressed", "no data reviewed"]}
    out = audit(BASE_TEXT, [line("99215", "CPT"), line("I10", rank=1)],
                em_estimate=est)
    f = out["em_level_above_documentation"]
    assert f.severity == "blocker"          # 2+ levels apart escalates


def test_em_level_matching_documentation_is_silent():
    est = {"level": 2, "rationale": []}
    out = audit(BASE_TEXT, [line("99213", "CPT"), line("I10", rank=1)],
                em_estimate=est)
    assert "em_level_above_documentation" not in out


def test_rejected_lines_are_excluded_from_the_claim():
    out = audit(BASE_TEXT, [line("20611", "CPT"),
                            line("76942", "CPT", status="rejected")])
    assert "ncci_bundling_conflict" not in out


def test_a_broken_rule_does_not_abort_the_audit(monkeypatch):
    from app.audit.engine import REGISTRY

    def exploding(ctx):
        raise RuntimeError("boom")
        yield

    monkeypatch.setitem(REGISTRY, "unsupported_code", exploding)
    out = audit(BASE_TEXT, [line("I10", rank=1)])
    assert out["unsupported_code"].category == "system"
    assert "missing_provider_attestation" not in out   # other rules still ran


# --- CDI ------------------------------------------------------------------

def test_provider_query_is_non_leading():
    text = ("BNP 1840. Ejection fraction of 35 percent. 2+ pedal edema. "
            "Started furosemide.")
    q = draft_query(text=text, topic="heart failure",
                    needs="acuity and type")
    body = q["body"]
    assert "Clinically undetermined" in body
    assert "not present / not clinically significant" in body.lower() or \
           "not present" in body
    assert "Other (please specify)" in body
    # It must surface the indicators it found, not invent a conclusion.
    assert any("1840" in i or "35 percent" in i for i in q["indicators"])
    assert "should be coded as" not in body.lower()


@pytest.mark.parametrize("topic", ["copd", "anemia", "sepsis", "chronic kidney disease"])
def test_every_query_topic_offers_an_escape_hatch(topic):
    q = draft_query(text="", topic=topic, needs="detail")
    assert "Clinically undetermined" in q["body"]
