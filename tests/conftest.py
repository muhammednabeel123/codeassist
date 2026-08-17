from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

SAMPLES = ROOT / "samples"


def _ensure_samples() -> None:
    if not (SAMPLES / "01_office_visit_digital.pdf").exists():
        sys.path.insert(0, str(SAMPLES))
        import generate_samples

        generate_samples.main()


@pytest.fixture(scope="session", autouse=True)
def samples():
    _ensure_samples()


@pytest.fixture(scope="session")
def sample_office(samples) -> Path:
    return SAMPLES / "01_office_visit_digital.pdf"


@pytest.fixture(scope="session")
def sample_inpatient(samples) -> Path:
    return SAMPLES / "02_inpatient_scanned.pdf"


@pytest.fixture(scope="session")
def sample_procedure(samples) -> Path:
    return SAMPLES / "03_procedure_note_digital.pdf"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A throwaway SQLite database per test."""
    import app.config as config

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setattr(config, "STORAGE_DIR", tmp_path)

    import importlib

    import app.db as dbmod

    importlib.reload(dbmod)
    dbmod.init_db()
    session = dbmod.SessionLocal()
    try:
        yield session
    finally:
        session.close()
