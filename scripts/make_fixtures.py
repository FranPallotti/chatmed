"""Generate the synthetic PDF fixtures (fake patients only, hard rule 8).

    uv run python scripts/make_fixtures.py      # (re)writes fixtures/*.pdf

`build_fixtures()` returns the PDFs as bytes so tests can check, without writing files, that
generation is reproducible and that the committed fixtures are what this script produces.

Two families of fixtures (see `fixtures/README.md` for every value):
- CIOMS I-style adverse reaction forms (D12) for acme subject 01001-004. `CIOMS_CASE` holds the
  values; they match `db/seed/tenant_acme.sql` (ADSL/ADAE) so documents can be joined to rows.
  `cioms_followup.pdf` states a corrected onset date on page 2 (conflict for extraction).
  The layout follows the public CIOMS I sections; dates are ISO so text and seed match exactly.
- Generic lab reports, kept for the M1 parsing tests only (they are not in any tenant seed).
  `multi_page_report.pdf` repeats hemoglobin with *different* values (13.4 on page 1, 12.9 on
  page 3) and glucose with the *same* value.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
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


CIOMS_CASE: dict[str, str] = {
    "subject_id": "01001-004",
    "country": "Argentina",
    "age": "47",
    "sex": "F",
    "reaction_onset_date": "2026-06-12",
    "corrected_onset_date": "2026-06-11",  # follow-up page 2 only
    "reaction_description": "acute liver injury, ALT 8x ULN",
    "suspect_drug": "Zentrobal",
    "daily_dose": "20 mg daily",
    "route": "oral",
    "indication": "rheumatoid arthritis",
    "therapy_start_date": "2026-03-10",
    "concomitant_drug_1": "Metformin 850 mg",
    "concomitant_drug_2": "Atorvastatin 20 mg",
    "mfr_control_no": "ACM-2026-0042",
    "date_received": "2026-06-15",
}


def _box(checked: bool) -> str:
    return "[X]" if checked else "[ ]"


def cioms_form_lines(report_type: str) -> list[str]:
    """One-page CIOMS I-style form (public section layout) filled with `CIOMS_CASE`.

    Patient initials and date of birth say PRIVACY: they are not extracted (D12) and the
    fixture should not invent personal identifiers it does not need.
    """
    c = CIOMS_CASE
    return [
        "CIOMS FORM - SUSPECT ADVERSE REACTION REPORT",
        f"Study ACM-101    Subject {c['subject_id']}",
        "I. REACTION INFORMATION",
        f"1. Patient initials: PRIVACY    1a. Country: {c['country']}",
        f"2. Date of birth: PRIVACY    2a. Age: {c['age']} years    3. Sex: {c['sex']}",
        f"4-6. Reaction onset: {c['reaction_onset_date']}",
        f"7+13. Describe reaction(s): {c['reaction_description']}",
        "8-12. Check all appropriate to adverse reaction:",
        f"  {_box(False)} patient died",
        f"  {_box(True)} involved or prolonged inpatient hospitalisation",
        f"  {_box(False)} involved persistent or significant disability or incapacity",
        f"  {_box(False)} life threatening",
        "II. SUSPECT DRUG(S) INFORMATION",
        f"14. Suspect drug(s): {c['suspect_drug']}",
        f"15. Daily dose(s): {c['daily_dose']}",
        f"16. Route(s) of administration: {c['route']}",
        f"17. Indication(s) for use: {c['indication']}",
        f"18. Therapy dates (from/to): {c['therapy_start_date']} / 2026-06-14",
        "19. Therapy duration: 96 days",
        f"20. Did reaction abate after stopping drug? {_box(True)} yes {_box(False)} no "
        f"{_box(False)} NA",
        "21. Did reaction reappear after reintroduction? "
        f"{_box(False)} yes {_box(False)} no {_box(True)} NA",
        "III. CONCOMITANT DRUG(S) AND HISTORY",
        f"22. Concomitant drug(s): {c['concomitant_drug_1']}; {c['concomitant_drug_2']}",
        "23. Other relevant history: type 2 diabetes mellitus; hyperlipidaemia",
        "IV. MANUFACTURER INFORMATION",
        "24a. Name and address of manufacturer: Acme Pharma (synthetic)",
        f"24b. Mfr control no.: {c['mfr_control_no']}",
        f"24c. Date received by manufacturer: {c['date_received']}",
        "24d. Report source: study",
        f"25a. Report type: {_box(report_type == 'initial')} initial "
        f"{_box(report_type == 'followup')} followup",
    ]


def cioms_followup_page2() -> list[str]:
    """Continuation page: corrects the onset date stated on page 1 (conflict for extraction)."""
    return [
        "CIOMS FORM - CONTINUATION (FOLLOW-UP)",
        f"Study ACM-101    Subject {CIOMS_CASE['subject_id']}",
        f"Mfr control no.: {CIOMS_CASE['mfr_control_no']}",
        "",
        "Follow-up information:",
        f"Corrected reaction onset date: {CIOMS_CASE['corrected_onset_date']}",
        "(investigator review of source documents).",
        "Patient recovered; ALT back to normal range on 2026-07-20.",
    ]


@dataclass(frozen=True)
class Layout:
    """Font size (pt) and line spacing (mm). Lab reports use the default, CIOMS a denser one."""

    size: int = 12
    spacing: float = 8.0


CIOMS_LAYOUT = Layout(size=10, spacing=6.5)


def _new_pdf() -> FPDF:
    pdf = FPDF(format="A4")
    pdf.set_creation_date(FIXED_DATE)
    pdf.set_auto_page_break(auto=False)
    return pdf


def _add_text_page(pdf: FPDF, lines: list[str], layout: Layout = Layout()) -> None:
    pdf.add_page()
    pdf.set_font("Courier", size=9)
    pdf.text(15, 12, BANNER)
    pdf.set_font("Courier", size=layout.size)
    for i, line in enumerate(lines):
        pdf.text(15, 30 + i * layout.spacing, line)


def _add_scanned_page(pdf: FPDF, lines: list[str], layout: Layout = Layout()) -> None:
    """Typeset the page, rasterize it, and add only the picture: no text layer, like a scan."""
    single = _new_pdf()
    _add_text_page(single, lines, layout)
    src = pdfium.PdfDocument(bytes(single.output()))
    try:
        image = src[0].render(scale=SCAN_DPI / 72, grayscale=True).to_pil()
    finally:
        src.close()
    pdf.add_page()
    pdf.image(image, x=0, y=0, w=pdf.w, h=pdf.h)


SOURCE_KEY = "source-sha256:"


def source_fingerprint(pages: list[list[str]], scanned: frozenset[int], layout: Layout) -> str:
    """Hash of everything a fixture is built from: page lines, which pages are scanned, layout.

    Stored in the PDF Keywords of fixtures with image-only pages. Their raster bytes differ
    across platforms and a one-character edit changes only ~0.001% of the pixels, so the
    committed-vs-generated check compares this hash instead (see fixtures/README.md).
    """
    source = {
        "pages": pages,
        "scanned": sorted(scanned),
        "layout": [layout.size, layout.spacing],
        "scan_dpi": SCAN_DPI,
    }
    data = json.dumps(source, sort_keys=True, ensure_ascii=True).encode()
    return hashlib.sha256(data).hexdigest()


def build_pdf(
    pages: list[list[str]], scanned: frozenset[int] = frozenset(), layout: Layout = Layout()
) -> bytes:
    """One PDF; pages whose 1-based number is in `scanned` are image-only."""
    pdf = _new_pdf()
    if scanned:
        pdf.set_keywords(SOURCE_KEY + source_fingerprint(pages, scanned, layout))
    for number, lines in enumerate(pages, start=1):
        (_add_scanned_page if number in scanned else _add_text_page)(pdf, lines, layout)
    return bytes(pdf.output())


def read_source_fingerprint(pdf_bytes: bytes) -> str | None:
    """The fingerprint stored by `build_pdf`, or None if the PDF has none."""
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        keywords = doc.get_metadata_dict().get("Keywords", "")
    finally:
        doc.close()
    return keywords.removeprefix(SOURCE_KEY) if keywords.startswith(SOURCE_KEY) else None


SCANNED_FIXTURES = frozenset(
    {"scanned_lab_report.pdf", "mixed_report.pdf", "cioms_initial_scanned.pdf"}
)


def build_fixtures() -> dict[str, bytes]:
    """All fixture PDFs by file name. Deterministic on one platform: same bytes on every run.

    Fixtures in `SCANNED_FIXTURES` embed a rasterized page, whose bytes can differ across
    platforms; tests compare those by rendered pixels instead of bytes.
    """
    initial = cioms_form_lines("initial")
    return {
        "lab_report.pdf": build_pdf(LAB_REPORT),
        "multi_page_report.pdf": build_pdf(MULTI_PAGE_REPORT),
        "scanned_lab_report.pdf": build_pdf(LAB_REPORT, scanned=frozenset({1})),
        # page 1 digital, page 2 scanned: exercises per-page routing
        "mixed_report.pdf": build_pdf(MULTI_PAGE_REPORT[:2], scanned=frozenset({2})),
        "cioms_initial.pdf": build_pdf([initial], layout=CIOMS_LAYOUT),
        "cioms_initial_scanned.pdf": build_pdf(
            [initial], scanned=frozenset({1}), layout=CIOMS_LAYOUT
        ),
        "cioms_followup.pdf": build_pdf(
            [cioms_form_lines("followup"), cioms_followup_page2()], layout=CIOMS_LAYOUT
        ),
    }


def main() -> int:
    FIXTURES.mkdir(exist_ok=True)
    for name, data in sorted(build_fixtures().items()):
        (FIXTURES / name).write_bytes(data)
        sys.stdout.write(f"wrote fixtures/{name} ({len(data)} bytes)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
