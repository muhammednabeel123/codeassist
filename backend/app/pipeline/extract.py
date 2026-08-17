"""PDF -> text, with per-page provenance.

The design answer to "we don't know what kind of PDFs we'll get": decide per
page, not per file. A single faxed chart routinely mixes a digitally generated
cover sheet with photographed progress notes. Each page is probed for a usable
text layer and falls back to OCR independently, and the result records which
path was taken so the UI can warn the coder when they are reading OCR output.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import OCR_DPI, OCR_ENABLED, OCR_LANG, TEXT_LAYER_MIN_CHARS

log = logging.getLogger(__name__)


@dataclass
class PageResult:
    page: int
    text: str
    ocr: bool
    confidence: float | None = None      # mean tesseract word confidence, 0-100
    width: float | None = None
    height: float | None = None
    char_start: int = 0
    char_end: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ExtractResult:
    text: str
    pages: list[PageResult]
    source_kind: str                      # digital | scanned | mixed
    ocr_pages: int
    mean_ocr_confidence: float | None
    sha256: str

    def as_page_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "page": p.page,
                "char_start": p.char_start,
                "char_end": p.char_end,
                "ocr": p.ocr,
                "confidence": p.confidence,
                "width": p.width,
                "height": p.height,
                "warnings": p.warnings,
            }
            for p in self.pages
        ]


# Headers/footers repeated on every page add noise to entity extraction and
# inflate false positives ("HISTORY" in a banner is not a clinical section).
_NOISE = re.compile(
    r"^(page\s+\d+\s+of\s+\d+|confidential.*|printed\s+on.*|"
    r"this\s+document\s+contains.*)$",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    out = []
    for raw in text.splitlines():
        line = raw.rstrip()
        # De-hyphenate words split across a line break.
        if _NOISE.match(line.strip()):
            continue
        out.append(line)
    joined = "\n".join(out)
    joined = re.sub(r"(\w)-\n(\w)", r"\1\2", joined)
    joined = re.sub(r"[ \t]{3,}", "   ", joined)
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined.strip()


def _ocr_page(pdf_path: Path, page_no: int) -> tuple[str, float | None, list[str]]:
    """Render one page and OCR it. Returns (text, mean_confidence, warnings)."""
    warnings: list[str] = []
    if not OCR_ENABLED:
        return "", None, ["ocr_disabled"]
    try:
        import pytesseract
        from pdf2image import convert_from_path
        from PIL import ImageOps
    except ImportError as exc:  # pragma: no cover - deployment problem, not logic
        return "", None, [f"ocr_unavailable: {exc}"]

    try:
        images = convert_from_path(
            str(pdf_path), dpi=OCR_DPI, first_page=page_no, last_page=page_no
        )
    except Exception as exc:
        log.warning("render failed p%s: %s", page_no, exc)
        return "", None, [f"render_failed: {exc}"]
    if not images:
        return "", None, ["render_empty"]

    img = images[0]
    # Greyscale + autocontrast measurably helps on faxes without the cost of a
    # full deskew/denoise stage. Swap in OpenCV preprocessing if your scans are
    # rotated or heavily speckled.
    img = ImageOps.autocontrast(img.convert("L"))

    try:
        data = pytesseract.image_to_data(
            img, lang=OCR_LANG, output_type=pytesseract.Output.DICT
        )
    except Exception as exc:
        log.warning("ocr failed p%s: %s", page_no, exc)
        return "", None, [f"ocr_failed: {exc}"]

    words, confs, lines, current_line, last_key = [], [], [], [], None
    for i, word in enumerate(data.get("text", [])):
        if not word.strip():
            continue
        conf = float(data["conf"][i]) if data["conf"][i] not in ("-1", -1) else None
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if last_key is not None and key != last_key:
            lines.append(" ".join(current_line))
            current_line = []
        current_line.append(word)
        last_key = key
        words.append(word)
        if conf is not None:
            confs.append(conf)
    if current_line:
        lines.append(" ".join(current_line))

    mean_conf = round(sum(confs) / len(confs), 1) if confs else None
    if mean_conf is not None and mean_conf < 70:
        warnings.append(f"low_ocr_confidence:{mean_conf}")
    if len(words) < 15:
        warnings.append("very_little_text_recovered")
    return "\n".join(lines), mean_conf, warnings


def extract_pdf(pdf_path: str | Path) -> ExtractResult:
    """Extract text from a PDF, OCR'ing only the pages that need it."""
    import pdfplumber

    pdf_path = Path(pdf_path)
    sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

    pages: list[PageResult] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            try:
                layer = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
            except Exception as exc:
                log.warning("text layer failed p%s: %s", idx, exc)
                layer = ""
            layer = _clean(layer)

            # An image-only page yields a near-empty layer. Some EHR exports
            # also embed a garbage layer (a single form-field artifact), which
            # the length threshold catches too.
            if len(layer) >= TEXT_LAYER_MIN_CHARS:
                pages.append(
                    PageResult(page=idx, text=layer, ocr=False,
                               width=page.width, height=page.height)
                )
                continue

            ocr_text, conf, warns = _ocr_page(pdf_path, idx)
            ocr_text = _clean(ocr_text)
            # Keep whichever route actually produced more content.
            if len(layer) > len(ocr_text):
                pages.append(
                    PageResult(page=idx, text=layer, ocr=False, width=page.width,
                               height=page.height,
                               warnings=warns + ["thin_text_layer_kept"])
                )
            else:
                pages.append(
                    PageResult(page=idx, text=ocr_text, ocr=True, confidence=conf,
                               width=page.width, height=page.height, warnings=warns)
                )

    # Stitch pages into one string, recording offsets so any character index in
    # the full text can be resolved back to a page.
    buf, cursor = [], 0
    for p in pages:
        p.char_start = cursor
        buf.append(p.text)
        cursor += len(p.text)
        buf.append("\n\n")
        cursor += 2
        p.char_end = p.char_start + len(p.text)
    full_text = "".join(buf).rstrip()

    ocr_pages = sum(1 for p in pages if p.ocr)
    if ocr_pages == 0:
        kind = "digital"
    elif ocr_pages == len(pages):
        kind = "scanned"
    else:
        kind = "mixed"
    confs = [p.confidence for p in pages if p.ocr and p.confidence is not None]

    return ExtractResult(
        text=full_text,
        pages=pages,
        source_kind=kind,
        ocr_pages=ocr_pages,
        mean_ocr_confidence=round(sum(confs) / len(confs), 1) if confs else None,
        sha256=sha,
    )


def locate(pages: list[dict[str, Any]], char_start: int) -> int:
    """Map a character offset in the stitched text back to a page number."""
    for p in pages:
        if p["char_start"] <= char_start < p["char_end"]:
            return int(p["page"])
    return int(pages[-1]["page"]) if pages else 1
