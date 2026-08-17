"""Persistence layer.

SQLite for the MVP, Postgres in production - the models are portable.
Note the deliberate separation of `Document` (the artifact) from `Encounter`
(the billable event): one PDF can contain several encounters, and one
encounter can be documented across several PDFs.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import DATABASE_URL

Base = declarative_base()
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _uid() -> str:
    return uuid.uuid4().hex


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Document(Base):
    """One uploaded PDF."""

    __tablename__ = "documents"

    id = Column(String, primary_key=True, default=_uid)
    filename = Column(String, nullable=False)
    stored_path = Column(String, nullable=False)
    sha256 = Column(String, index=True)
    page_count = Column(Integer, default=0)
    # 'digital' | 'scanned' | 'mixed' - decided per page, summarised here
    source_kind = Column(String, default="unknown")
    ocr_pages = Column(Integer, default=0)
    mean_ocr_confidence = Column(Float)
    status = Column(String, default="uploaded")  # uploaded|extracting|extracted|coded|error
    error = Column(Text)
    # Full extracted text plus per-page offsets, so a finding can be pinned
    # to a page and character range for UI highlighting.
    text = Column(Text)
    pages = Column(JSON)  # [{page, char_start, char_end, ocr, confidence, width, height}]
    sections = Column(JSON)  # {"assessment_and_plan": [start, end], ...}
    created_at = Column(DateTime, default=_now)

    encounters = relationship("Encounter", back_populates="document", cascade="all, delete-orphan")


class Encounter(Base):
    """The unit a coder actually works: one claim-in-progress."""

    __tablename__ = "encounters"

    id = Column(String, primary_key=True, default=_uid)
    document_id = Column(String, ForeignKey("documents.id"), nullable=False)
    # Demographics matter: many audit rules are age/sex conditioned.
    patient_ref = Column(String)          # pseudonymised MRN, never the raw one
    patient_age = Column(Integer)
    patient_sex = Column(String)          # 'M' | 'F' | 'U'
    date_of_service = Column(String)
    place_of_service = Column(String)
    payer = Column(String)
    provider_npi = Column(String)
    status = Column(String, default="in_review")  # in_review|ready|submitted|held
    assigned_to = Column(String)
    created_at = Column(DateTime, default=_now)

    document = relationship("Document", back_populates="encounters")
    codes = relationship("CodeLine", back_populates="encounter", cascade="all, delete-orphan")
    findings = relationship("Finding", back_populates="encounter", cascade="all, delete-orphan")


class CodeLine(Base):
    """A single code on the claim - suggested by the engine or entered by a human."""

    __tablename__ = "code_lines"

    id = Column(String, primary_key=True, default=_uid)
    encounter_id = Column(String, ForeignKey("encounters.id"), nullable=False)
    system = Column(String, nullable=False)   # ICD10CM | CPT | HCPCS
    code = Column(String, nullable=False)
    description = Column(Text)
    # dx lines get a rank (primary = 1); procedure lines get units + modifiers
    rank = Column(Integer)
    units = Column(Integer, default=1)
    modifiers = Column(JSON, default=list)
    # which dx lines justify this procedure (medical necessity linkage)
    linked_dx = Column(JSON, default=list)

    origin = Column(String, default="suggested")  # suggested|coder|imported
    status = Column(String, default="proposed")   # proposed|accepted|rejected
    confidence = Column(Float)
    # Where in the document this came from - the coder must be able to click
    # a code and land on the sentence that supports it.
    evidence = Column(JSON, default=list)  # [{page, char_start, char_end, quote, why}]
    reasoning = Column(Text)
    created_at = Column(DateTime, default=_now)

    encounter = relationship("Encounter", back_populates="codes")


class Finding(Base):
    """An audit result: something a human should look at before submission."""

    __tablename__ = "findings"

    id = Column(String, primary_key=True, default=_uid)
    encounter_id = Column(String, ForeignKey("encounters.id"), nullable=False)
    rule_id = Column(String, nullable=False)
    category = Column(String)   # documentation|bundling|specificity|necessity|demographic|units|modifier
    severity = Column(String)   # blocker|high|medium|low|info
    title = Column(String)
    detail = Column(Text)
    suggested_action = Column(Text)
    # dollars-at-risk or denial-likelihood hint, used to sort the worklist
    risk_score = Column(Float, default=0.0)
    codes_involved = Column(JSON, default=list)
    evidence = Column(JSON, default=list)
    citation = Column(String)   # e.g. 'NCCI PTP Ch.1', 'ICD-10-CM Official Guidelines I.B.18'
    status = Column(String, default="open")  # open|accepted|dismissed
    dismiss_reason = Column(Text)
    created_at = Column(DateTime, default=_now)

    encounter = relationship("Encounter", back_populates="findings")


class AuditEvent(Base):
    """Append-only trail. Required for any system touching a claim."""

    __tablename__ = "audit_events"

    id = Column(String, primary_key=True, default=_uid)
    actor = Column(String, default="system")
    action = Column(String, nullable=False)
    entity_type = Column(String)
    entity_id = Column(String)
    payload = Column(JSON)
    at = Column(DateTime, default=_now)


class ProviderQuery(Base):
    """A CDI query letter drafted from a documentation-gap finding."""

    __tablename__ = "provider_queries"

    id = Column(String, primary_key=True, default=_uid)
    encounter_id = Column(String, ForeignKey("encounters.id"))
    finding_id = Column(String, ForeignKey("findings.id"))
    subject = Column(String)
    body = Column(Text)
    compliant = Column(Boolean, default=True)  # non-leading per AHIMA/ACDIS
    status = Column(String, default="draft")
    created_at = Column(DateTime, default=_now)


def init_db() -> None:
    Base.metadata.create_all(engine)


def log_event(session, action: str, entity_type: str = "", entity_id: str = "",
              actor: str = "system", **payload) -> None:
    session.add(
        AuditEvent(action=action, entity_type=entity_type, entity_id=entity_id,
                   actor=actor, payload=payload or None)
    )
