"""Application entry point."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .audit import rules as _rules  # noqa: F401  (registers the rule functions)
from .config import ALLOWED_ORIGINS, FRONTEND_DIR
from .db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(
    title="CodeAssist",
    description=(
        "Decision support for medical coders: scan a chart, get evidence-linked "
        "code suggestions, then audit the claim before it goes out."
    ),
    version="0.1.0",
)

# Tighten this in any real deployment - a wildcard origin on a PHI-bearing API
# is not acceptable outside local development. Set CA_ALLOWED_ORIGINS when the
# frontend is hosted separately from this API (see config.py).
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    from .audit.engine import REGISTRY
    from .coding.terminology import load_terminology

    term = load_terminology()
    logging.getLogger(__name__).info(
        "loaded %d diagnoses, %d procedures, %d PTP edits, %d audit rules",
        len(term.dx), len(term.proc), len(term.ptp), len(REGISTRY),
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(FRONTEND_DIR / "index.html"))
