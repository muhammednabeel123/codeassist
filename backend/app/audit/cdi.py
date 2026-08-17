"""Provider query drafting.

A query is only compliant if it is non-leading: it presents the clinical
indicators found in the record and asks the provider to clarify, without
suggesting the answer that pays more. This module builds that structure
mechanically so the coder cannot accidentally write a leading query, and it
always includes "clinically undetermined" and "other" as options - the absence
of those options is what makes a query leading.
"""
from __future__ import annotations

import re

# Clinical indicators worth surfacing back to the provider, by topic.
INDICATOR_PATTERNS: dict[str, list[str]] = {
    "heart failure": [r"\bef\b[^.\n]{0,20}\d{1,2}\s*%", r"ejection fraction[^.\n]{0,25}\d{1,2}",
                      r"\bbnp\b[^.\n]{0,20}\d+", r"\bpro-?bnp\b[^.\n]{0,20}\d+",
                      r"pedal edema", r"orthopnea", r"jvd", r"lasix|furosemide"],
    "copd": [r"\bfev1\b[^.\n]{0,20}\d+", r"increased sputum", r"wheez\w+",
             r"prednisone", r"nebuliz\w+", r"o2 sat[^.\n]{0,15}\d{2}"],
    "anemia": [r"\bhgb\b[^.\n]{0,15}\d+\.?\d*", r"\bhemoglobin\b[^.\n]{0,15}\d+\.?\d*",
               r"\bferritin\b[^.\n]{0,15}\d+", r"\bmcv\b[^.\n]{0,15}\d+",
               r"transfus\w+", r"iron (?:sucrose|infusion|supplement)"],
    "chronic kidney disease": [r"\begfr\b[^.\n]{0,20}\d+", r"creatinine[^.\n]{0,20}\d+\.?\d*",
                               r"\bbun\b[^.\n]{0,15}\d+", r"nephrology"],
    "sepsis": [r"lactate[^.\n]{0,15}\d+\.?\d*", r"blood cultures?", r"\bwbc\b[^.\n]{0,15}\d+",
               r"pressors?", r"vasopressor", r"\bsirs\b", r"temp(?:erature)?[^.\n]{0,15}\d{2,3}"],
    "pneumonia": [r"infiltrate", r"consolidation", r"sputum culture", r"chest x-?ray",
                  r"antibiotic", r"ceftriaxone|azithromycin|levofloxacin"],
    "obesity": [r"\bbmi\b[^.\n]{0,15}\d{2}\.?\d*", r"weight[^.\n]{0,15}\d{2,3}\s*(?:kg|lb)"],
    "malnutrition": [r"albumin[^.\n]{0,15}\d\.?\d*", r"weight loss", r"\bbmi\b[^.\n]{0,15}\d{2}",
                     r"dietitian|nutrition consult"],
}

# Answer options offered per topic. "Clinically undetermined" and "Other" are
# mandatory and appended automatically.
OPTIONS: dict[str, list[str]] = {
    "heart failure": ["Acute systolic (HFrEF)", "Chronic systolic (HFrEF)",
                      "Acute diastolic (HFpEF)", "Chronic diastolic (HFpEF)",
                      "Acute on chronic combined systolic and diastolic",
                      "Chronic combined systolic and diastolic"],
    "copd": ["COPD without exacerbation", "COPD with acute exacerbation",
             "COPD with acute lower respiratory infection"],
    "anemia": ["Iron deficiency anemia", "Anemia of chronic disease",
               "Acute blood loss anemia", "Chronic blood loss anemia",
               "Vitamin B12 / folate deficiency anemia", "Anemia due to chemotherapy"],
    "chronic kidney disease": ["CKD stage 1", "CKD stage 2", "CKD stage 3a", "CKD stage 3b",
                               "CKD stage 4", "CKD stage 5", "ESRD on dialysis"],
    "sepsis": ["Sepsis due to a specified organism (please name)",
               "Severe sepsis with acute organ dysfunction (please specify the organ)",
               "Septic shock", "Bacteremia without sepsis",
               "Systemic infection without sepsis"],
    "pneumonia": ["Community-acquired pneumonia, organism unidentified",
                  "Pneumonia due to a specified organism (please name)",
                  "Aspiration pneumonia", "Healthcare-associated pneumonia"],
    "obesity": ["Overweight", "Obesity (class 1)", "Obesity (class 2)",
                "Morbid/severe obesity (class 3)"],
    "malnutrition": ["Mild protein-calorie malnutrition",
                     "Moderate protein-calorie malnutrition",
                     "Severe protein-calorie malnutrition",
                     "Cachexia", "No malnutrition present"],
}

MANDATORY_OPTIONS = ["Clinically undetermined",
                     "Other (please specify)",
                     "The condition is not present / not clinically significant"]


def _indicators(text: str, topic: str, limit: int = 8) -> list[str]:
    out: list[str] = []
    for pattern in INDICATOR_PATTERNS.get(topic, []):
        for m in re.finditer(pattern, text, re.IGNORECASE):
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            if snippet not in out:
                out.append(snippet)
            if len(out) >= limit:
                return out
    return out


def draft_query(*, text: str, topic: str, needs: str,
                date_of_service: str | None = None,
                provider_npi: str | None = None) -> dict:
    """Build a compliant provider query for a documentation gap."""
    indicators = _indicators(text, topic)
    options = OPTIONS.get(topic, []) + MANDATORY_OPTIONS

    lines = [
        "PROVIDER DOCUMENTATION CLARIFICATION REQUEST",
        "",
        f"Date of service: {date_of_service or '[date of service]'}",
        f"Provider: {('NPI ' + provider_npi) if provider_npi else '[provider]'}",
        "",
        "This is a request for clarification only. It is not a request to change "
        "your clinical judgement, and no specific diagnosis is being suggested.",
        "",
        f"In reviewing this record we identified documentation of \"{topic}\" without "
        f"{needs}.",
        "",
    ]

    if indicators:
        lines.append("Clinical indicators present in the record:")
        lines += [f"  - {snippet}" for snippet in indicators]
        lines.append("")

    lines += [
        f"Based on your clinical judgement and the above, can you further specify "
        f"{needs}?",
        "",
        "Possible responses (this list is not exhaustive; any of these may be "
        "correct, and it is equally appropriate to indicate that the answer cannot "
        "be determined):",
    ]
    lines += [f"  [ ] {opt}" for opt in options]
    lines += [
        "",
        "Please document your response in the medical record (an addendum or "
        "progress note), not only on this form, so that the record itself supports "
        "the final coding.",
        "",
        "Provider signature: ____________________   Date: ____________",
    ]

    return {
        "subject": f"Clarification requested: {topic}",
        "body": "\n".join(lines),
        "compliant": True,
        "indicators": indicators,
        "options": options,
    }
