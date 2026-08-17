"""Orchestration: the pipeline that turns an uploaded PDF into a reviewable claim.

Kept separate from the HTTP layer so the same path can be driven by a worker
queue, a batch job, or a test without going through FastAPI.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .audit.engine import CodeLineView, build_context, run_audit
from .coding.suggest import suggest
from .db import CodeLine, Document, Encounter, Finding, log_event
from .pipeline.extract import extract_pdf, locate
from .pipeline.sections import parse_header, sections_as_dict, split_sections

log = logging.getLogger(__name__)


def ingest_document(session, *, filename: str, stored_path: Path,
                    actor: str = "system") -> Document:
    """Extract text and create the Document + Encounter records."""
    doc = Document(filename=filename, stored_path=str(stored_path), status="extracting")
    session.add(doc)
    session.flush()

    try:
        result = extract_pdf(stored_path)
    except Exception as exc:
        doc.status = "error"
        doc.error = f"{type(exc).__name__}: {exc}"
        log.exception("extraction failed for %s", filename)
        session.commit()
        return doc

    doc.sha256 = result.sha256
    doc.page_count = len(result.pages)
    doc.source_kind = result.source_kind
    doc.ocr_pages = result.ocr_pages
    doc.mean_ocr_confidence = result.mean_ocr_confidence
    doc.text = result.text
    doc.pages = result.as_page_dicts()
    doc.sections = sections_as_dict(split_sections(result.text))
    doc.status = "extracted"

    header = parse_header(result.text)
    enc = Encounter(document_id=doc.id, **header)
    session.add(enc)
    log_event(session, "document.ingested", "document", doc.id, actor=actor,
              pages=doc.page_count, source_kind=doc.source_kind,
              ocr_pages=doc.ocr_pages)
    session.commit()
    return doc


def run_coding(session, doc: Document, encounter: Encounter, *,
               use_llm: bool = False, actor: str = "system") -> dict:
    """Suggest codes for an encounter, replacing any prior *unreviewed* suggestions."""
    if not doc.text:
        return {"diagnoses": [], "suppressed": [], "notes": []}

    pages = doc.pages or []

    def page_of(offset: int) -> int:
        return locate(pages, offset)

    result = suggest(doc.text, page_of=page_of, use_llm=use_llm)

    # A coder's accept/reject decisions survive a re-run; machine proposals do not.
    for line in list(encounter.codes):
        if line.origin == "suggested" and line.status == "proposed":
            session.delete(line)
    session.flush()

    decided = {(c.system, c.code) for c in encounter.codes}

    for cand in result["diagnoses"]:
        if (cand.system, cand.code) in decided:
            continue
        session.add(CodeLine(
            encounter_id=encounter.id,
            system=cand.system,
            code=cand.code,
            description=cand.description,
            rank=cand.rank,
            units=cand.units,
            modifiers=list(cand.modifiers),
            linked_dx=list(cand.linked_dx),
            origin="suggested",
            status="proposed",
            confidence=cand.confidence,
            evidence=[e.as_dict() for e in cand.evidence],
            reasoning=cand.reasoning,
        ))

    doc.status = "coded"
    log_event(session, "encounter.coded", "encounter", encounter.id, actor=actor,
              diagnoses=len(result["diagnoses"]),
              suppressed=len(result["suppressed"]))
    session.commit()
    return result


def run_encounter_audit(session, doc: Document, encounter: Encounter,
                        actor: str = "system") -> list[Finding]:
    """Re-run every audit rule against the encounter's current code set."""
    views = [
        CodeLineView(
            id=line.id, system=line.system, code=line.code,
            description=line.description or "", units=line.units or 1,
            modifiers=list(line.modifiers or []), linked_dx=list(line.linked_dx or []),
            rank=line.rank, status=line.status, origin=line.origin,
            confidence=line.confidence, evidence=list(line.evidence or []),
        )
        for line in encounter.codes
    ]

    ctx = build_context(
        text=doc.text or "",
        sections=doc.sections or {},
        pages=doc.pages or [],
        codes=views,
        patient_age=encounter.patient_age,
        patient_sex=encounter.patient_sex,
        source_kind=doc.source_kind or "digital",
        # No E/M estimate: this deployment does not code E/M. The two E/M rules
        # stay registered and go quiet without one.
    )
    results = run_audit(ctx)

    # Preserve coder dispositions across re-audits: a dismissed finding stays
    # dismissed unless the underlying codes changed.
    prior = {(f.rule_id, tuple(sorted(f.codes_involved or []))): f
             for f in encounter.findings}
    for f in list(encounter.findings):
        session.delete(f)
    session.flush()

    out: list[Finding] = []
    for r in results:
        key = (r.rule_id, tuple(sorted(r.codes_involved or [])))
        old = prior.get(key)
        rec = Finding(
            encounter_id=encounter.id,
            rule_id=r.rule_id,
            category=r.category,
            severity=r.severity,
            title=r.title,
            detail=r.detail,
            suggested_action=r.suggested_action,
            risk_score=r.risk_score,
            codes_involved=r.codes_involved,
            evidence=r.evidence,
            citation=r.citation,
            status=old.status if old else "open",
            dismiss_reason=old.dismiss_reason if old else None,
        )
        session.add(rec)
        out.append(rec)

    log_event(session, "encounter.audited", "encounter", encounter.id, actor=actor,
              findings=len(out),
              blockers=sum(1 for f in out if f.severity == "blocker"))
    session.commit()
    return out


def process_upload(session, *, filename: str, stored_path: Path,
                   use_llm: bool = False, actor: str = "system") -> Document:
    """The whole path: extract -> suggest -> audit."""
    doc = ingest_document(session, filename=filename, stored_path=stored_path, actor=actor)
    if doc.status == "error":
        return doc
    encounter = doc.encounters[0]
    run_coding(session, doc, encounter, use_llm=use_llm, actor=actor)
    session.refresh(encounter)
    run_encounter_audit(session, doc, encounter, actor=actor)
    return doc
