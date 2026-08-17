"""HTTP API."""
from __future__ import annotations

import csv
import io
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit.cdi import draft_query
from ..audit.engine import load_rule_config
from ..coding.terminology import load_terminology
from ..config import LLM_ENABLED, STORAGE_DIR, SUPPORTED_CODE_SYSTEMS
from ..db import (
    AuditEvent,
    CodeLine,
    Document,
    Encounter,
    Finding,
    ProviderQuery,
    SessionLocal,
    log_event,
)
from ..service import process_upload, run_coding, run_encounter_audit

router = APIRouter(prefix="/api")

SEVERITY_ORDER = {"blocker": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _check_system(system: str, code: str) -> str:
    """Reject anything that is not a code from a supported code set.

    The pipeline only ever proposes ICD-10-CM, so this guards the one door left
    open: a coder (or a script) posting a code by hand.
    """
    system = (system or "").strip().upper()
    if system not in SUPPORTED_CODE_SYSTEMS:
        raise HTTPException(
            400,
            f"{system or 'unknown'} is not a supported code system. This deployment "
            f"codes diagnoses only ({', '.join(sorted(SUPPORTED_CODE_SYSTEMS))}).",
        )
    if code not in load_terminology().dx:
        raise HTTPException(400, f"{code} is not in the loaded ICD-10-CM table.")
    return system


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- serialisers ------------------------------------------------------------

def capture_pages(evidence: list[dict] | None) -> list[int]:
    """Every page a code was captured from, in document order."""
    pages = {int(e["page"]) for e in (evidence or [])
             if e.get("page") not in (None, "")}
    return sorted(pages)


def first_capture_page(evidence: list[dict] | None) -> int | None:
    """The page of the earliest supporting mention - where the code was captured.

    Evidence is stored in match order, not document order, so the first page is
    the one belonging to the lowest character offset rather than evidence[0].
    """
    spans = [e for e in (evidence or []) if e.get("page") not in (None, "")]
    if not spans:
        return None
    return int(min(spans, key=lambda e: e.get("char_start", 0))["page"])


def capture_label(code: str, dos: str | None, page: int | None) -> str:
    """The capture line a coder reads: 'DOS- 05/02/2025   Pg-1   ICD- N1831'.

    The code is rendered without its decimal point, which is how it travels on
    a claim; `code` keeps the canonical dotted form for display and lookup.
    """
    return (f"DOS- {dos or '—'}   Pg-{page if page is not None else '—'}   "
            f"ICD- {(code or '').replace('.', '')}")


def code_json(c: CodeLine, dos: str | None = None) -> dict:
    page = first_capture_page(c.evidence)
    return {
        "id": c.id, "system": c.system, "code": c.code, "description": c.description,
        "rank": c.rank, "units": c.units, "modifiers": c.modifiers or [],
        "linked_dx": c.linked_dx or [], "origin": c.origin, "status": c.status,
        "confidence": c.confidence, "evidence": c.evidence or [],
        "reasoning": c.reasoning,
        "dos": dos,
        "page": page,
        "pages": capture_pages(c.evidence),
        "capture": capture_label(c.code, dos, page),
    }


def finding_json(f: Finding) -> dict:
    return {
        "id": f.id, "rule_id": f.rule_id, "category": f.category, "severity": f.severity,
        "title": f.title, "detail": f.detail, "suggested_action": f.suggested_action,
        "risk_score": f.risk_score, "codes_involved": f.codes_involved or [],
        "evidence": f.evidence or [], "citation": f.citation, "status": f.status,
        "dismiss_reason": f.dismiss_reason,
    }


def encounter_json(enc: Encounter, doc: Document, *, full: bool = False) -> dict:
    findings = sorted(enc.findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9),
                                                   -(f.risk_score or 0)))
    open_findings = [f for f in findings if f.status == "open"]
    data = {
        "id": enc.id,
        "document_id": doc.id,
        "filename": doc.filename,
        "page_count": doc.page_count,
        "source_kind": doc.source_kind,
        "ocr_pages": doc.ocr_pages,
        "mean_ocr_confidence": doc.mean_ocr_confidence,
        "status": enc.status,
        "patient_ref": enc.patient_ref,
        "patient_age": enc.patient_age,
        "patient_sex": enc.patient_sex,
        "date_of_service": enc.date_of_service,
        "place_of_service": enc.place_of_service,
        "payer": enc.payer,
        "provider_npi": enc.provider_npi,
        "created_at": enc.created_at.isoformat() if enc.created_at else None,
        "counts": {
            "diagnoses": sum(1 for c in enc.codes if c.system == "ICD10CM"
                             and c.status != "rejected"),
            "findings_open": len(open_findings),
            "blockers": sum(1 for f in open_findings if f.severity == "blocker"),
            "high": sum(1 for f in open_findings if f.severity == "high"),
        },
        "risk_score": round(sum(f.risk_score or 0 for f in open_findings), 2),
    }
    if full:
        codes = sorted(enc.codes, key=lambda c: (c.system != "ICD10CM",
                                                 c.rank if c.rank else 99, c.code))
        data["codes"] = [code_json(c, enc.date_of_service) for c in codes]
        data["findings"] = [finding_json(f) for f in findings]
        data["pages"] = doc.pages or []
        data["sections"] = doc.sections or {}
    return data


def _load(db: Session, encounter_id: str) -> tuple[Encounter, Document]:
    enc = db.get(Encounter, encounter_id)
    if not enc:
        raise HTTPException(404, "Encounter not found")
    return enc, enc.document


# --- documents --------------------------------------------------------------

@router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    use_llm: bool = Query(False, description="Run the optional LLM extractor"),
    actor: str = Query("coder"),
    db: Session = Depends(get_db),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF uploads are supported.")
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "Empty file.")
    if len(payload) > 80 * 1024 * 1024:
        raise HTTPException(413, "File exceeds the 80 MB limit.")

    dest = STORAGE_DIR / f"{uuid.uuid4().hex}.pdf"
    dest.write_bytes(payload)

    doc = process_upload(db, filename=file.filename, stored_path=dest,
                         use_llm=use_llm and LLM_ENABLED, actor=actor)
    if doc.status == "error":
        raise HTTPException(422, f"Could not read the PDF: {doc.error}")
    db.refresh(doc)
    enc = doc.encounters[0]
    return encounter_json(enc, doc, full=True)


@router.get("/documents/{document_id}/file")
def document_file(document_id: str, db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc or not Path(doc.stored_path).exists():
        raise HTTPException(404, "Document not found")
    return FileResponse(doc.stored_path, media_type="application/pdf",
                        filename=doc.filename)


@router.get("/documents/{document_id}/text")
def document_text(document_id: str, db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    return {"text": doc.text or "", "pages": doc.pages or [],
            "sections": doc.sections or {}, "source_kind": doc.source_kind,
            "ocr_pages": doc.ocr_pages, "mean_ocr_confidence": doc.mean_ocr_confidence}


# --- worklist ---------------------------------------------------------------

@router.get("/encounters")
def list_encounters(
    status: str | None = None,
    sort: str = Query("risk", pattern="^(risk|newest|oldest)$"),
    db: Session = Depends(get_db),
):
    stmt = select(Encounter)
    if status:
        stmt = stmt.where(Encounter.status == status)
    encounters = db.execute(stmt).scalars().all()
    rows = [encounter_json(e, e.document) for e in encounters if e.document]
    if sort == "risk":
        rows.sort(key=lambda r: (-r["counts"]["blockers"], -r["risk_score"]))
    elif sort == "newest":
        rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
    else:
        rows.sort(key=lambda r: r["created_at"] or "")
    return {"encounters": rows, "total": len(rows)}


@router.get("/encounters/{encounter_id}")
def get_encounter(encounter_id: str, db: Session = Depends(get_db)):
    enc, doc = _load(db, encounter_id)
    return encounter_json(enc, doc, full=True)


@router.patch("/encounters/{encounter_id}")
def update_encounter(encounter_id: str, body: dict = Body(...),
                     db: Session = Depends(get_db)):
    enc, doc = _load(db, encounter_id)
    for field in ("status", "assigned_to", "patient_age", "patient_sex",
                  "date_of_service", "place_of_service", "payer"):
        if field in body:
            setattr(enc, field, body[field])
    log_event(db, "encounter.updated", "encounter", enc.id,
              actor=body.get("actor", "coder"), changes=list(body.keys()))
    db.commit()
    # Demographics feed the audit rules, so re-run when they change.
    if {"patient_age", "patient_sex"} & set(body):
        run_encounter_audit(db, doc, enc, actor=body.get("actor", "coder"))
    db.refresh(enc)
    return encounter_json(enc, doc, full=True)


@router.post("/encounters/{encounter_id}/recode")
def recode(encounter_id: str, use_llm: bool = Query(False),
           db: Session = Depends(get_db)):
    enc, doc = _load(db, encounter_id)
    run_coding(db, doc, enc, use_llm=use_llm and LLM_ENABLED, actor="coder")
    db.refresh(enc)
    run_encounter_audit(db, doc, enc, actor="coder")
    db.refresh(enc)
    return encounter_json(enc, doc, full=True)


@router.post("/encounters/{encounter_id}/audit")
def reaudit(encounter_id: str, db: Session = Depends(get_db)):
    enc, doc = _load(db, encounter_id)
    run_encounter_audit(db, doc, enc, actor="coder")
    db.refresh(enc)
    return encounter_json(enc, doc, full=True)


# --- code lines -------------------------------------------------------------

@router.post("/encounters/{encounter_id}/codes")
def add_code(encounter_id: str, body: dict = Body(...), db: Session = Depends(get_db)):
    enc, doc = _load(db, encounter_id)
    term = load_terminology()
    code = (body.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(400, "code is required")
    system = _check_system(body.get("system", "ICD10CM"), code)
    description = body.get("description") or term.describe(system, code)
    line = CodeLine(
        encounter_id=enc.id, system=system, code=code, description=description,
        rank=body.get("rank"), units=int(body.get("units", 1) or 1),
        modifiers=body.get("modifiers", []) or [],
        linked_dx=body.get("linked_dx", []) or [],
        origin="coder", status="accepted", confidence=None,
        evidence=body.get("evidence", []) or [],
        reasoning=body.get("reasoning", "Added by coder."),
    )
    db.add(line)
    log_event(db, "code.added", "code_line", line.id, actor=body.get("actor", "coder"),
              code=code, system=system)
    db.commit()
    db.refresh(enc)
    run_encounter_audit(db, doc, enc, actor=body.get("actor", "coder"))
    db.refresh(enc)
    return encounter_json(enc, doc, full=True)


@router.patch("/codes/{code_id}")
def update_code(code_id: str, body: dict = Body(...), db: Session = Depends(get_db)):
    line = db.get(CodeLine, code_id)
    if not line:
        raise HTTPException(404, "Code line not found")
    enc = line.encounter
    before = {"status": line.status, "units": line.units, "modifiers": line.modifiers}
    # Re-coding a line must not be a way around the code-system restriction.
    if "code" in body or "system" in body:
        _check_system(body.get("system", line.system),
                      (body.get("code") or line.code or "").strip().upper())
    for field in ("status", "rank", "units", "modifiers", "linked_dx", "code",
                  "description", "system"):
        if field in body:
            setattr(line, field, body[field])
    if "code" in body:
        line.origin = "coder"
        if not body.get("description"):
            line.description = load_terminology().describe(line.system, line.code)
    log_event(db, "code.updated", "code_line", line.id, actor=body.get("actor", "coder"),
              code=line.code, before=before,
              after={"status": line.status, "units": line.units,
                     "modifiers": line.modifiers})
    db.commit()
    db.refresh(enc)
    run_encounter_audit(db, enc.document, enc, actor=body.get("actor", "coder"))
    db.refresh(enc)
    return encounter_json(enc, enc.document, full=True)


@router.delete("/codes/{code_id}")
def delete_code(code_id: str, db: Session = Depends(get_db)):
    line = db.get(CodeLine, code_id)
    if not line:
        raise HTTPException(404, "Code line not found")
    enc = line.encounter
    log_event(db, "code.deleted", "code_line", line.id, code=line.code)
    db.delete(line)
    db.commit()
    db.refresh(enc)
    run_encounter_audit(db, enc.document, enc)
    db.refresh(enc)
    return encounter_json(enc, enc.document, full=True)


# --- findings ---------------------------------------------------------------

@router.patch("/findings/{finding_id}")
def update_finding(finding_id: str, body: dict = Body(...), db: Session = Depends(get_db)):
    f = db.get(Finding, finding_id)
    if not f:
        raise HTTPException(404, "Finding not found")
    if "status" in body:
        if body["status"] == "dismissed" and not (body.get("dismiss_reason") or "").strip():
            # A dismissal without a reason is invisible on audit - require one.
            raise HTTPException(400, "A reason is required to dismiss a finding.")
        f.status = body["status"]
        f.dismiss_reason = body.get("dismiss_reason")
    log_event(db, "finding.updated", "finding", f.id, actor=body.get("actor", "coder"),
              rule_id=f.rule_id, status=f.status, reason=f.dismiss_reason)
    db.commit()
    enc = f.encounter
    return encounter_json(enc, enc.document, full=True)


@router.post("/findings/{finding_id}/query")
def generate_query(finding_id: str, db: Session = Depends(get_db)):
    f = db.get(Finding, finding_id)
    if not f:
        raise HTTPException(404, "Finding not found")
    enc = f.encounter
    doc = enc.document
    cfg = load_rule_config().get("documentation_gap_query", {}) or {}
    gaps = cfg.get("gaps", {}) or {}

    topic = None
    lower_title = (f.title or "").lower()
    for key in gaps:
        if key.lower() in lower_title or key.lower() in (f.detail or "").lower():
            topic = key
            break
    if topic is None:
        topic = (f.title or "documentation clarification").split(":")[-1].strip()
    needs = (gaps.get(topic, {}) or {}).get("needs", "the missing clinical detail")

    draft = draft_query(text=doc.text or "", topic=topic, needs=needs,
                        date_of_service=enc.date_of_service,
                        provider_npi=enc.provider_npi)
    q = ProviderQuery(encounter_id=enc.id, finding_id=f.id, subject=draft["subject"],
                      body=draft["body"], compliant=draft["compliant"])
    db.add(q)
    log_event(db, "query.drafted", "provider_query", q.id, topic=topic)
    db.commit()
    return {"id": q.id, **draft}


@router.get("/encounters/{encounter_id}/queries")
def list_queries(encounter_id: str, db: Session = Depends(get_db)):
    rows = db.execute(
        select(ProviderQuery).where(ProviderQuery.encounter_id == encounter_id)
    ).scalars().all()
    return {"queries": [{"id": q.id, "subject": q.subject, "body": q.body,
                         "status": q.status,
                         "created_at": q.created_at.isoformat() if q.created_at else None}
                        for q in rows]}


# --- terminology lookup -----------------------------------------------------

@router.get("/codes/search")
def search_codes(q: str = Query(..., min_length=2), system: str | None = None,
                 limit: int = 25):
    """Look up codes a coder may add - ICD-10-CM only.

    The CPT/HCPCS tables stay out of this result set deliberately: offering a
    code the API would then refuse to accept is worse than not offering it.
    """
    if system and system.upper() not in SUPPORTED_CODE_SYSTEMS:
        return {"results": []}
    term = load_terminology()
    needle = q.strip().lower()
    out = [
        {"system": "ICD10CM", "code": c.code, "description": c.description,
         "unspecified": c.unspecified}
        for c in term.dx.values()
        if needle in c.code.lower() or needle in c.description.lower()
        or any(needle in k for k in c.keywords)
    ]
    out.sort(key=lambda r: (not r["code"].lower().startswith(needle), r["code"]))
    return {"results": out[:limit]}


@router.get("/rules")
def list_rules():
    from ..audit.engine import REGISTRY

    cfg = load_rule_config()
    return {
        "rules": [
            {"rule_id": rid,
             "enabled": cfg.get(rid, {}).get("enabled", True),
             "severity": cfg.get(rid, {}).get("severity", "medium"),
             "category": cfg.get(rid, {}).get("category", "other"),
             "citation": cfg.get(rid, {}).get("citation", ""),
             "doc": (fn.__doc__ or "").strip().split("\n")[0]}
            for rid, fn in sorted(REGISTRY.items())
        ]
    }


# --- export -----------------------------------------------------------------

@router.get("/encounters/{encounter_id}/export")
def export_claim(encounter_id: str, fmt: str = Query("json", pattern="^(json|csv)$"),
                 db: Session = Depends(get_db)):
    """Export the coded diagnoses. Blocked while unresolved blockers remain.

    This is a diagnosis export, not a complete claim: no service lines are
    produced because this deployment does not code procedures. Whatever billing
    system consumes it supplies its own CPT/HCPCS lines and points them at these
    diagnosis pointers.
    """
    enc, doc = _load(db, encounter_id)
    blockers = [f for f in enc.findings if f.severity == "blocker" and f.status == "open"]
    if blockers:
        raise HTTPException(
            409,
            {"error": "Unresolved blocking findings prevent export.",
             "blockers": [{"rule_id": f.rule_id, "title": f.title} for f in blockers]},
        )

    dx = sorted([c for c in enc.codes if c.system == "ICD10CM" and c.status != "rejected"],
                key=lambda c: c.rank or 99)
    pointers = {c.code: chr(ord("A") + i) for i, c in enumerate(dx)}

    claim = {
        "encounter_id": enc.id,
        "patient_ref": enc.patient_ref,
        "date_of_service": enc.date_of_service,
        "place_of_service": enc.place_of_service,
        "payer": enc.payer,
        "rendering_provider_npi": enc.provider_npi,
        "code_systems": sorted(SUPPORTED_CODE_SYSTEMS),
        "diagnoses": [
            {"pointer": pointers[c.code],
             "code": c.code,
             "description": c.description,
             # Each captured code carries the encounter's date of service and
             # the page it was read from, so a reviewer can go straight to it.
             "dos": enc.date_of_service,
             "page": first_capture_page(c.evidence),
             "pages": capture_pages(c.evidence),
             "capture": capture_label(c.code, enc.date_of_service,
                                      first_capture_page(c.evidence))}
            for c in dx
        ],
        "source_document": {"filename": doc.filename, "sha256": doc.sha256,
                            "pages": doc.page_count, "source_kind": doc.source_kind},
    }
    log_event(db, "claim.exported", "encounter", enc.id, fmt=fmt)
    db.commit()

    if fmt == "json":
        return claim

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["pointer", "dos", "page", "diagnosis", "description", "capture"])
    for d in claim["diagnoses"]:
        w.writerow([d["pointer"], d["dos"] or "", d["page"] if d["page"] is not None else "",
                    d["code"], d["description"], d["capture"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="claim-{enc.id[:8]}.csv"'},
    )


# --- dashboard --------------------------------------------------------------

@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    total = db.execute(select(func.count(Encounter.id))).scalar() or 0
    by_severity = dict(
        db.execute(
            select(Finding.severity, func.count(Finding.id))
            .where(Finding.status == "open").group_by(Finding.severity)
        ).all()
    )
    by_rule = db.execute(
        select(Finding.rule_id, func.count(Finding.id))
        .where(Finding.status == "open")
        .group_by(Finding.rule_id).order_by(func.count(Finding.id).desc()).limit(10)
    ).all()
    ocr_docs = db.execute(
        select(func.count(Document.id)).where(Document.ocr_pages > 0)
    ).scalar() or 0
    return {
        "encounters": total,
        "documents": db.execute(select(func.count(Document.id))).scalar() or 0,
        "documents_needing_ocr": ocr_docs,
        "open_findings_by_severity": by_severity,
        "top_rules": [{"rule_id": r, "count": c} for r, c in by_rule],
        "llm_enabled": LLM_ENABLED,
    }


@router.get("/encounters/{encounter_id}/trail")
def audit_trail(encounter_id: str, db: Session = Depends(get_db)):
    enc, doc = _load(db, encounter_id)
    ids = {enc.id, doc.id} | {c.id for c in enc.codes} | {f.id for f in enc.findings}
    rows = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id.in_(ids))
        .order_by(AuditEvent.at.desc()).limit(200)
    ).scalars().all()
    return {"events": [{"at": e.at.isoformat() if e.at else None, "actor": e.actor,
                        "action": e.action, "entity_type": e.entity_type,
                        "payload": e.payload} for e in rows]}
