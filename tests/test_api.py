"""End-to-end: upload a real PDF through the HTTP API and work the claim."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import importlib

    import app.config as config

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path/'api.db'}")
    monkeypatch.setattr(config, "STORAGE_DIR", tmp_path)

    import app.db as dbmod

    importlib.reload(dbmod)

    import app.api.routes as routes
    import app.service as service

    importlib.reload(service)
    importlib.reload(routes)

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def upload(client, path):
    with open(path, "rb") as fh:
        r = client.post("/api/documents",
                        files={"file": (path.name, fh, "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_office_note_produces_codes_and_findings(client, sample_office):
    enc = upload(client, sample_office)
    assert enc["patient_age"] == 58
    assert enc["patient_sex"] == "M"
    assert enc["source_kind"] == "digital"

    codes = {c["code"] for c in enc["codes"]}
    assert "E11.9" in codes            # type 2 diabetes, stated in the A/P
    assert "I10" in codes              # essential hypertension
    assert "R07.9" not in codes        # "denies chest pain" must not be coded
    assert "G47.33" not in codes       # father's sleep apnea is not the patient's
    # Diagnoses only: the arthrocentesis in this note is documented but not coded.
    assert {c["system"] for c in enc["codes"]} == {"ICD10CM"}
    assert "20610" not in codes

    rules = {f["rule_id"] for f in enc["findings"]}
    assert "specificity_upgrade_available" in rules

    # Every suggested code must be traceable to text in the document.
    for c in enc["codes"]:
        if c["origin"] == "suggested":
            assert c["evidence"], f"{c['code']} has no evidence"
            for ev in c["evidence"]:
                assert ev["char_start"] < ev["char_end"]
                assert ev["page"]


def test_scanned_note_is_ocred_and_coded(client, sample_inpatient):
    enc = upload(client, sample_inpatient)
    assert enc["source_kind"] == "scanned"
    assert enc["ocr_pages"] == enc["page_count"]
    codes = {c["code"] for c in enc["codes"]}
    assert "I50.9" in codes
    rules = {f["rule_id"] for f in enc["findings"]}
    assert "documentation_gap_query" in rules
    assert "missing_provider_attestation" in rules


def test_a_procedure_note_still_yields_diagnoses_only(client, sample_procedure):
    """The scope guarantee, tested where it is hardest: a note that is entirely
    about a procedure must still produce nothing but ICD-10-CM."""
    enc = upload(client, sample_procedure)
    assert enc["counts"]["diagnoses"] >= 1
    assert {c["system"] for c in enc["codes"]} == {"ICD10CM"}

    # With no procedure lines on the claim, the procedure-side rules - which
    # remain registered - have nothing to fire on.
    rules = {f["rule_id"] for f in enc["findings"]}
    assert not rules & {"ncci_bundling_conflict", "ncci_modifier_required",
                        "mue_exceeded", "em_with_minor_procedure_needs_25",
                        "medical_necessity_unsupported",
                        "procedure_not_linked_to_dx",
                        "em_level_above_documentation"}


def test_blocking_finding_prevents_export(client, sample_office):
    enc = upload(client, sample_office)
    # A hand-added code with nothing in the record behind it is the archetypal
    # blocker.
    enc = client.post(f"/api/encounters/{enc['id']}/codes",
                      json={"system": "ICD10CM", "code": "N40.1"}).json()
    blocker = next(f for f in enc["findings"] if f["rule_id"] == "unsupported_code")
    assert blocker["severity"] == "blocker"
    assert "N40.1" in blocker["codes_involved"]

    r = client.get(f"/api/encounters/{enc['id']}/export")
    assert r.status_code == 409

    # Remove the unsupported line and clear any other blocker the note raised;
    # export then succeeds.
    added = next(c for c in enc["codes"] if c["code"] == "N40.1")
    enc = client.delete(f"/api/codes/{added['id']}").json()
    for f in enc["findings"]:
        if f["severity"] == "blocker" and f["status"] == "open":
            enc = client.patch(f"/api/findings/{f['id']}",
                               json={"status": "dismissed",
                                     "dismiss_reason": "reviewed with the coder"}).json()

    r = client.get(f"/api/encounters/{enc['id']}/export")
    assert r.status_code == 200, r.text
    claim = r.json()
    assert claim["diagnoses"][0]["pointer"] == "A"
    assert claim["code_systems"] == ["ICD10CM"]
    assert "service_lines" not in claim      # diagnoses only

    # Every exported code carries its date of service and source page.
    d = claim["diagnoses"][0]
    assert d["dos"] == enc["date_of_service"]
    assert d["page"] in d["pages"]
    assert d["capture"] == f"DOS- {d['dos']}   Pg-{d['page']}   ICD- {d['code'].replace('.', '')}"

    r = client.get(f"/api/encounters/{enc['id']}/export?fmt=csv")
    assert r.status_code == 200
    assert r.text.splitlines()[0] == "pointer,dos,page,diagnosis,description,capture"


def test_captured_codes_carry_dos_and_page(client, sample_office):
    """A coder must be able to read off where and when a code was captured."""
    enc = upload(client, sample_office)
    assert enc["date_of_service"]

    dx = next(c for c in enc["codes"] if c["code"] == "E11.9")
    assert dx["dos"] == enc["date_of_service"]
    assert dx["page"] is not None
    assert dx["page"] in dx["pages"]
    # The page shown is the earliest supporting mention, not evidence[0], which
    # is stored in match order.
    assert dx["page"] == min(e["page"] for e in dx["evidence"])
    assert dx["capture"] == f"DOS- {enc['date_of_service']}   Pg-{dx['page']}   ICD- E119"


def test_capture_line_degrades_when_dos_or_page_is_unknown(client, sample_office):
    """A hand-added code has no evidence and so no page; the line still renders."""
    enc = upload(client, sample_office)
    enc = client.patch(f"/api/encounters/{enc['id']}",
                       json={"date_of_service": None}).json()
    enc = client.post(f"/api/encounters/{enc['id']}/codes",
                      json={"system": "ICD10CM", "code": "N40.1"}).json()
    added = next(c for c in enc["codes"] if c["code"] == "N40.1")
    assert added["page"] is None and added["pages"] == []
    assert added["capture"] == "DOS- —   Pg-—   ICD- N401"


def test_procedure_codes_cannot_be_added_by_hand(client, sample_office):
    """The API is the last door into the claim; it has to hold the same line."""
    enc = upload(client, sample_office)
    r = client.post(f"/api/encounters/{enc['id']}/codes",
                    json={"system": "CPT", "code": "20610"})
    assert r.status_code == 400
    assert "ICD10CM" in r.json()["detail"]

    # A code that does not exist in the ICD-10-CM table is refused too.
    r = client.post(f"/api/encounters/{enc['id']}/codes",
                    json={"system": "ICD10CM", "code": "Z99.999"})
    assert r.status_code == 400


def test_coder_decisions_survive_a_recode(client, sample_office):
    enc = upload(client, sample_office)
    target = next(c for c in enc["codes"] if c["code"] == "I10")
    enc = client.patch(f"/api/codes/{target['id']}",
                       json={"status": "rejected"}).json()
    enc = client.post(f"/api/encounters/{enc['id']}/recode").json()
    again = next(c for c in enc["codes"] if c["code"] == "I10")
    assert again["status"] == "rejected"


def test_dismissing_a_finding_requires_a_reason(client, sample_office):
    enc = upload(client, sample_office)
    f = enc["findings"][0]
    r = client.patch(f"/api/findings/{f['id']}", json={"status": "dismissed"})
    assert r.status_code == 400
    r = client.patch(f"/api/findings/{f['id']}",
                     json={"status": "dismissed",
                           "dismiss_reason": "payer does not require this"})
    assert r.status_code == 200


def test_manual_code_entry_and_reaudit(client, sample_office):
    enc = upload(client, sample_office)
    enc = client.post(f"/api/encounters/{enc['id']}/codes",
                      json={"system": "ICD10CM", "code": "N40.1"}).json()
    added = next(c for c in enc["codes"] if c["code"] == "N40.1")
    assert added["origin"] == "coder"
    # A male-only code on a male patient is fine; flip the demographics and the
    # sex-conflict rule must fire on re-audit.
    enc = client.patch(f"/api/encounters/{enc['id']}",
                       json={"patient_sex": "F"}).json()
    assert "sex_conflict" in {f["rule_id"] for f in enc["findings"]}


def test_audit_trail_records_every_change(client, sample_office):
    enc = upload(client, sample_office)
    code = enc["codes"][0]
    client.patch(f"/api/codes/{code['id']}", json={"status": "accepted"})
    events = client.get(f"/api/encounters/{enc['id']}/trail").json()["events"]
    actions = {e["action"] for e in events}
    assert {"document.ingested", "encounter.coded", "encounter.audited",
            "code.updated"} <= actions


def test_terminology_search(client):
    r = client.get("/api/codes/search?q=copd")
    results = r.json()["results"]
    codes = {x["code"] for x in results}
    assert "J44.9" in codes and "J44.1" in codes
    # Search must not offer a code the API would then refuse to accept.
    assert {x["system"] for x in results} == {"ICD10CM"}
    assert client.get("/api/codes/search?q=arthrocentesis").json()["results"] == []
    assert client.get("/api/codes/search?q=copd&system=CPT").json()["results"] == []


def test_rules_endpoint_lists_the_registry(client):
    rules = client.get("/api/rules").json()["rules"]
    ids = {r["rule_id"] for r in rules}
    assert {"unsupported_code", "ncci_bundling_conflict",
            "medical_necessity_unsupported"} <= ids
    assert all(r["citation"] for r in rules)


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"
