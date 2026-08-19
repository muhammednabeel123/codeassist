"""Runtime configuration.

Everything that differs between a laptop demo and a real deployment lives here.
No PHI-bearing default is ever a remote service.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # backend/
PROJECT_DIR = BASE_DIR.parent                              # repo root
REFERENCE_DIR = Path(os.getenv("CA_REFERENCE_DIR", BASE_DIR / "reference"))
RULES_DIR = Path(os.getenv("CA_RULES_DIR", BASE_DIR / "rules"))
FRONTEND_DIR = Path(os.getenv("CA_FRONTEND_DIR", PROJECT_DIR / "frontend"))

# Uploaded PDFs. In production this must be an encrypted volume or an
# object store with SSE-KMS + a lifecycle policy, never a container-local path.
STORAGE_DIR = Path(os.getenv("CA_STORAGE_DIR", PROJECT_DIR / "storage"))
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("CA_DATABASE_URL", f"sqlite:///{PROJECT_DIR / 'codeassist.db'}")
# Some providers (Vercel Postgres/Neon among them) hand out connection
# strings starting "postgres://", a scheme SQLAlchemy 2.x no longer
# recognises - it wants the "postgresql://" dialect prefix.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

# --- CORS ---------------------------------------------------------------
# When the frontend is served from the same origin as the API (the default,
# single-container deployment) this never matters. When the frontend is
# split out to its own host (e.g. a static deploy on Vercel while the API
# runs on Render), that origin must be listed here or the browser blocks
# the requests. Comma-separated, no trailing slashes, e.g.:
#   CA_ALLOWED_ORIGINS=https://codeassist.vercel.app,https://codeassist-git-main-you.vercel.app
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CA_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
    ).split(",")
    if o.strip()
]

# --- Code systems -----------------------------------------------------------
# This deployment codes diagnoses only. Every code that reaches a claim line -
# suggested, LLM-extracted, or typed by a coder - must come from the ICD-10-CM
# table in reference/. CPT/HCPCS extraction and the E/M level estimate are
# switched off as a consequence; the procedure-side audit rules stay registered
# so a legacy CPT line already in the database is still checked, but nothing in
# the pipeline creates one.
SUPPORTED_CODE_SYSTEMS = frozenset({"ICD10CM"})

# --- Ingestion tuning -------------------------------------------------------
# If a page's embedded text layer yields fewer than this many characters we
# assume it is a scan and route the page to OCR.
TEXT_LAYER_MIN_CHARS = int(os.getenv("CA_TEXT_LAYER_MIN_CHARS", "120"))
# Rendering DPI for OCR. 300 is the accuracy/latency sweet spot for tesseract
# on faxed charts; 400+ helps only with very small print.
OCR_DPI = int(os.getenv("CA_OCR_DPI", "300"))
OCR_LANG = os.getenv("CA_OCR_LANG", "eng")
OCR_ENABLED = os.getenv("CA_OCR_ENABLED", "1") == "1"

# --- Optional LLM extractor -------------------------------------------------
# Off by default: sending a chart to a third-party API needs a signed BAA and
# an explicit decision by your compliance officer.
LLM_ENABLED = os.getenv("CA_LLM_ENABLED", "0") == "1"
LLM_MODEL = os.getenv("CA_LLM_MODEL", "claude-sonnet-4-5")
LLM_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Suggestions below this confidence are hidden from the coder by default.
MIN_SUGGESTION_CONFIDENCE = float(os.getenv("CA_MIN_CONFIDENCE", "0.35"))
