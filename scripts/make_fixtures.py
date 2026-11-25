"""Generate the synthetic PDF fixtures (fake patients only, hard rule 8).

    uv run python scripts/make_fixtures.py      # (re)writes fixtures/*.pdf

`build_fixtures()` returns the PDFs as bytes so tests can check, without writing files, that
generation is reproducible and that the committed fixtures are what this script produces.

Values are chosen to line up with the rest of the POC:
- MRN 00123 is the synthetic patient Ana Testa in `db/seed/tenant_acme.sql`.
- "Hemoglobin", "13.4" and "00123" are the markers the M4 log guardrail looks for.
- `multi_page_report.pdf` repeats hemoglobin with *different* values (13.4 on page 1, 12.9 on
  page 3) for conflict tests, and glucose with the *same* value (for de-duplication tests).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pypdfium2 as pdfium
from fpdf import FPDF

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
FIXED_DATE = datetime(2026, 1, 1, tzinfo=UTC)  # PDFs embed a creation date; pin it
BANNER = "SYNTHETIC TEST DOCUMENT - NOT A REAL PATIENT"
SCAN_DPI = 200

LAB_REPORT: list[list[str]] = [
    [
        "ACME Clinical Laboratory",
        "Laboratory Report",
        "Patient: Ana Testa",
        "MRN: 00123",
        "Date of birth: 1985-03-14",
        "Collection date: 2026-11-01",
        "",
        "Test                Result      Unit     Reference",
        "Hemoglobin          13.4        g/dL     12.0 - 15.5",
        "Glucose (fasting)   92          mg/dL    70 - 99",
        "HIV 1/2 Ag/Ab       Negative",
        "",
        "Validated by: Dr. Example Reviewer",
    ]
]

MULTI_PAGE_REPORT: list[list[str]] = [
    [
        "ACME Clinical Laboratory",
        "Cumulative Report - page 1 of 3",
        "Patient: Ana Testa",
        "MRN: 00123",
        "Collection date: 2026-11-01",
        "",
        "Hemoglobin          13.4        g/dL",
        "Glucose (fasting)   92          mg/dL",
    ],
    [
        "ACME Clinical Laboratory",
        "Cumulative Report - page 2 of 3",
        "Comments",
        "Sample received in good condition.",
        "No interfering substances detected.",
    ],
    [
        "ACME Clinical Laboratory",
        "Cumulative Report - page 3 of 3",
        "Repeat measurement",
        "Hemoglobin          12.9        g/dL",
        "Glucose (fasting)   92          mg/dL",
    ],
]


def _new_pdf() -> FPDF:
    pdf = FPDF(format="A4")
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_auto_page_break(auto=False)
    return pdf


def _add_text_page(pdf: FPDF, lines: list[str]) -> None:
    pdf.add_page()
    pdf.set_font("Courier", size=9)
    pdf.text(15, 12, BANNER)
    pdf.set_font("Courier", size=12)
    for i, line in enumerate(lines):
        pdf.text(15, 30 + i * 8, line)


def _add_scanned_page(pdf: FPDF, lines: list[str]) -> None:
    """Typeset the page, rasterize it, and add only the picture: no text layer, like a scan."""
    single = _new_pdf()
    _add_text_page(single, lines)
    src = pdfium.PdfDocument(bytes(single.output()))
    try:
        image = src[0].render(scale=SCAN_DPI / 72, grayscale=True).to_pil()
    finally:
        src.close()
    pdf.add_page()
    pdf.image(image, x=0, y=0, w=pdf.w, h=pdf.h)


def build_pdf(pages: list[list[str]], scanned: frozenset[int] = frozenset()) -> bytes:
    """One PDF; pages whose 1-based number is in `scanned` are image-only."""
    pdf = _new_pdf()
    for number, lines in enumerate(pages, start=1):
        (_add_scanned_page if number in scanned else _add_text_page)(pdf, lines)
    return bytes(pdf.output())


def build_fixtures() -> dict[str, bytes]:
    """All fixture PDFs by file name. Deterministic: same bytes on every run."""
    return {
        "lab_report.pdf": build_pdf(LAB_REPORT),
        "multi_page_report.pdf": build_pdf(MULTI_PAGE_REPORT),
        "scanned_lab_report.pdf": build_pdf(LAB_REPORT, scanned=frozenset({1})),
        # page 1 digital, page 2 scanned: exercises per-page routing
        "mixed_report.pdf": build_pdf(MULTI_PAGE_REPORT[:2], scanned=frozenset({2})),
    }


def main() -> int:
    FIXTURES.mkdir(exist_ok=True)
    for name, data in sorted(build_fixtures().items()):
        (FIXTURES / name).write_bytes(data)
        sys.stdout.write(f"wrote fixtures/{name} ({len(data)} bytes)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
