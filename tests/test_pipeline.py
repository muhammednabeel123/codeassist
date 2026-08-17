"""Extraction, section splitting, and assertion detection."""
from __future__ import annotations

import pytest

from app.coding import context as ctx
from app.pipeline.extract import extract_pdf, locate
from app.pipeline.sections import parse_header, section_at, split_sections


def test_digital_pdf_uses_text_layer(sample_office):
    result = extract_pdf(sample_office)
    assert result.source_kind == "digital"
    assert result.ocr_pages == 0
    assert "Type 2 diabetes mellitus" in result.text
    assert result.sha256


def test_scanned_pdf_falls_back_to_ocr(sample_inpatient):
    result = extract_pdf(sample_inpatient)
    assert result.source_kind == "scanned"
    assert result.ocr_pages == len(result.pages)
    assert result.mean_ocr_confidence is not None
    # OCR is imperfect by nature; assert on recall of key clinical content
    # rather than an exact string match.
    lowered = result.text.lower()
    for token in ("heart failure", "chronic kidney", "ejection fraction"):
        assert token in lowered, f"OCR lost {token!r}"


def test_page_offsets_are_contiguous_and_locatable(sample_office):
    result = extract_pdf(sample_office)
    pages = result.as_page_dicts()
    for p in pages:
        assert p["char_start"] < p["char_end"]
        assert result.text[p["char_start"]:p["char_end"]].strip()
    assert locate(pages, 5) == 1
    assert locate(pages, pages[-1]["char_start"] + 1) == pages[-1]["page"]


def test_sections_are_identified():
    text = (
        "CHIEF COMPLAINT:\nKnee pain.\n\n"
        "PAST MEDICAL HISTORY:\nDiabetes.\n\n"
        "ASSESSMENT AND PLAN:\n1. Knee osteoarthritis.\n"
    )
    sections = split_sections(text)
    names = {s.name for s in sections}
    assert {"chief_complaint", "past_medical_history", "assessment"} <= names
    assert section_at(sections, text.index("Knee osteoarthritis")).name == "assessment"
    # Assessment must outweigh past history when the same term appears in both.
    a = next(s for s in sections if s.name == "assessment")
    h = next(s for s in sections if s.name == "past_medical_history")
    assert a.weight > h.weight


def test_family_history_is_never_codable():
    sections = split_sections("FAMILY HISTORY:\nMother with breast cancer.\n")
    fh = next(s for s in sections if s.name == "family_history")
    assert not fh.codable


def test_dos_from_a_flattened_table_layout():
    """EHR screenshots put 'Date of Service' in a column header and the value in
    the row beneath, so OCR never leaves the two adjacent."""
    text = (
        "John Doe Home (555) 555-1234 Insurance BCBS PPO\n"
        "Male, 45 Y, 01/15/1978 Cell (555) 555-9876\n"
        "S/O Notes Provider Date of Service Start Time End Time\n"
        "OBGYN Jennifer Smith, MD v 05/21/2024 10:30 AM 11:00 AM\n"
    )
    header = parse_header(text)
    assert header["date_of_service"] == "05/21/2024"
    # The date of birth on an earlier line must not win.
    assert header["date_of_service"] != "01/15/1978"


def test_a_date_of_birth_alone_is_not_a_date_of_service():
    """Without a DOS label there is no date of service - guessing one is worse
    than leaving it blank, because it lands on a claim."""
    text = "Patient: John Doe\nMale, 45 Y, 01/15/1978\nHPI: routine follow-up.\n"
    assert "date_of_service" not in parse_header(text)


def test_header_parsing():
    header = parse_header(
        "Age: 58    Sex: Male\nDate of Service: 03/14/2026\n"
        "MRN: RB-884213\nNPI: 1932847561\nPayer: Meridian Health Plan\n"
    )
    assert header["patient_age"] == 58
    assert header["patient_sex"] == "M"
    assert header["date_of_service"] == "03/14/2026"
    assert header["provider_npi"] == "1932847561"
    # The raw MRN must never be stored.
    assert "884213" not in header["patient_ref"]


@pytest.mark.parametrize(
    "sentence,concept,expected",
    [
        ("He denies chest pain today.", "chest pain", "negated"),
        ("There is no evidence of pneumonia.", "pneumonia", "negated"),
        ("Mother with breast cancer.", "breast cancer", "not_patient"),
        ("Return if fever develops.", "fever", "hypothetical"),
        ("Rule out pulmonary embolism.", "pulmonary embolism", "uncertain"),
        ("History of pneumonia in 2019.", "pneumonia", "historical"),
        ("Patient has poorly controlled diabetes.", "diabetes", "present"),
        # A conjunction must terminate the negation's scope.
        ("Denies chest pain but reports dyspnea.", "dyspnea", "present"),
    ],
)
def test_assertion_detection(sentence, concept, expected):
    i = sentence.lower().index(concept)
    assert ctx.classify(sentence, i, i + len(concept)).label == expected


def test_negation_survives_line_wrapping():
    """Notes wrap mid-sentence; a wrap must not break the negation scope."""
    text = "He denies chest pain, denies\nshortness of breath, and denies fever."
    i = text.index("shortness of breath")
    assert ctx.classify(text, i, i + len("shortness of breath")).label == "negated"
