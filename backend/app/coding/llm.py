"""Optional LLM extractor.

Disabled unless CA_LLM_ENABLED=1. Three guardrails matter more than the prompt:

1. **Closed vocabulary.** The model is asked for codes, and every returned code
   is checked against the loaded ICD-10-CM table. Anything not in it is dropped
   and reported as a note. Models produce well-formed, plausible, and
   nonexistent ICD-10 codes with total confidence.
2. **Mandatory verbatim evidence.** The model must return the exact substring
   it relied on. We locate that substring in the document; if it is not found,
   the finding is discarded as a hallucination. This turns "trust the model"
   into "trust the quote, which we verified."
3. **Diagnoses only.** The prompt asks for ICD-10-CM and the parser accepts
   nothing else, because an instruction alone is not an enforcement mechanism.
"""
from __future__ import annotations

import json
import logging
import re

from ..config import LLM_API_KEY, LLM_MODEL
from ..pipeline.sections import Section, section_at
from .suggest import Candidate, Evidence
from .terminology import Terminology

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a certified medical coding assistant. You read a \
clinical note and extract codable diagnoses.

Rules you must follow:
- Report ICD-10-CM diagnoses ONLY. Do not report CPT or HCPCS procedure codes, \
and do not report an E/M level - procedures are out of scope here and anything \
you return that is not a diagnosis will be discarded.
- Only report conditions that are ACTIVE and ADDRESSED at this encounter. Never \
report negated findings ("denies chest pain"), a relative's condition ("mother \
had breast cancer"), hypotheticals ("return if fever"), or uncertain diagnoses \
in an outpatient note ("rule out PE").
- For every item, quote the EXACT text from the note that supports it, copied \
character for character. Do not paraphrase the quote.
- Prefer the most specific code the documentation supports. Do not infer \
specificity that is not written down.
- If the documentation is too vague to support a specific code, say so in \
`documentation_gap` instead of guessing.

Return ONLY valid JSON with this shape:
{
  "diagnoses": [
    {"code": "E11.42", "description": "...", "quote": "exact text from note",
     "confidence": 0.0-1.0, "reasoning": "why this code"}
  ],
  "documentation_gaps": [
    {"issue": "...", "quote": "exact text from note",
     "query_to_provider": "non-leading question for the physician"}
  ]
}"""


def _locate(text: str, quote: str) -> tuple[int, int] | None:
    """Find the model's quote in the document, tolerating whitespace drift."""
    quote = (quote or "").strip()
    if len(quote) < 8:
        return None
    idx = text.find(quote)
    if idx != -1:
        return idx, idx + len(quote)
    # Whitespace-insensitive retry - OCR line breaks defeat exact matching.
    pattern = r"\s+".join(re.escape(tok) for tok in quote.split())
    if m := re.search(pattern, text, re.IGNORECASE):
        return m.span()
    return None


def llm_extract(
    text: str,
    term: Terminology,
    sections: list[Section],
    page_of=None,
    max_chars: int = 60_000,
) -> tuple[list[Candidate], list[str]]:
    notes: list[str] = []
    if not LLM_API_KEY:
        return [], ["LLM extractor enabled but ANTHROPIC_API_KEY is not set."]

    try:
        from anthropic import Anthropic
    except ImportError:
        return [], ["LLM extractor enabled but the `anthropic` package is not installed."]

    client = Anthropic(api_key=LLM_API_KEY)
    body = text[:max_chars]
    if len(text) > max_chars:
        notes.append(f"Note truncated to {max_chars} characters for the LLM pass.")

    resp = client.messages.create(
        model=LLM_MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"<clinical_note>\n{body}\n</clinical_note>"}],
    )
    raw = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return [], ["LLM returned no parsable JSON."]
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return [], [f"LLM JSON parse error: {exc}"]

    # Guardrail 3: diagnoses only. A model that ignores the instruction and
    # returns procedures anyway must not get them onto the claim by the back door.
    if data.get("procedures"):
        notes.append(
            f"Dropped {len(data['procedures'])} LLM procedure code(s) - this "
            f"deployment codes ICD-10-CM diagnoses only."
        )

    out: list[Candidate] = []
    for item in data.get("diagnoses", []) or []:
        code = str(item.get("code", "")).strip().upper()
        if not code:
            continue
        # Guardrail 1: closed vocabulary.
        if code not in term.dx:
            notes.append(f"Dropped LLM code {code} - not present in the loaded ICD-10-CM table.")
            continue

        # Guardrail 2: verified evidence.
        span = _locate(text, item.get("quote", ""))
        if span is None:
            notes.append(
                f"Dropped LLM code {code} - its supporting quote was not found "
                f"in the document (possible hallucination)."
            )
            continue
        s, e = span
        sec = section_at(sections, s)
        out.append(Candidate(
            system="ICD10CM",
            code=code,
            description=term.describe("ICD10CM", code) or str(item.get("description", "")),
            confidence=round(min(0.95, float(item.get("confidence", 0.7))), 3),
            evidence=[Evidence(
                char_start=s, char_end=e, quote=text[s:e][:400],
                why="LLM extraction, quote verified against the document",
                page=page_of(s) if page_of else None, section=sec.name,
            )],
            reasoning=str(item.get("reasoning", ""))[:500],
            best_section=sec.name,
        ))

    for gap in data.get("documentation_gaps", []) or []:
        notes.append("GAP: " + str(gap.get("issue", ""))[:300])
    return out, notes
