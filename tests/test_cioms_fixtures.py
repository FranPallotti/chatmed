"""Synthetic CIOMS I fixtures for acme subject 01001-004 (D12, D13).

M1b: AC1 shapes and values, AC2 consistency with the acme seed and fixtures/README.md,
AC3 OCR of the scanned form (parser contract cases are in tests/contracts/).
M2 AC6: like the real form, a prefixed subject id, checkboxes drawn as graphics (never text),
and a narrative sentence that is the only text source for hospitalisation.
"""

from __future__ import annotations

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
import pytest
from make_fixtures import CIOMS_CASE, build_fixtures

from chatmed.parsing import TesseractOcrParser
from chatmed.parsing.pdfium import page_texts
from tests.conftest import FIXTURES, ROOT, fixture_bytes

SEED = ROOT / "db" / "seed" / "tenant_acme.sql"
PRINTED_ID = CIOMS_CASE["subject_id"]  # "101-01001-004": site prefix + SUBJID
SUBJECT = CIOMS_CASE["subjid"]  # "01001-004": adsl.subjid
ONSET = CIOMS_CASE["reaction_onset_date"]
DRUG = CIOMS_CASE["suspect_drug"]


def _texts(name: str) -> list[str]:
    return page_texts(fixture_bytes(name))


# ---------------------------------------------------------------- AC1: shapes and values


def test_initial_form_has_every_case_value() -> None:
    (page,) = _texts("cioms_initial.pdf")
    expected = {k: v for k, v in CIOMS_CASE.items() if k != "corrected_onset_date"}
    missing = [key for key, value in expected.items() if value not in page]
    assert not missing
    assert f"Subject ID {PRINTED_ID}" in page
    assert CIOMS_CASE["corrected_onset_date"] not in page


BOXES = 12  # 4 seriousness criteria + 3 dechallenge + 3 rechallenge + 2 report type
CHECKED = 4  # hospitalisation, dechallenge yes, rechallenge NA, report type


def _drawn_paths(name: str) -> list[int]:
    """Vector path objects per page: each box is one rectangle, each X two lines."""
    doc = pdfium.PdfDocument(fixture_bytes(name))
    try:
        return [len(list(page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_PATH]))) for page in doc]
    finally:
        doc.close()


@pytest.mark.parametrize("name", ["cioms_initial.pdf", "cioms_followup.pdf"])
def test_checkboxes_are_graphics_not_text(name: str) -> None:
    page = _texts(name)[0]
    for marker in ("[X]", "[ ]", "{X}", "{ }", "[x]"):
        assert marker not in page
    assert "involved or prolonged inpatient hospitalisation" in page  # the label is text
    assert _drawn_paths(name)[0] == BOXES + 2 * CHECKED


def test_narrative_is_the_only_text_source_for_hospitalisation() -> None:
    (page,) = _texts("cioms_initial.pdf")
    assert f"The patient was hospitalised on {CIOMS_CASE['hospitalised_on']}." in page
    statements = [line for line in page.splitlines() if "hospitalis" in line]
    assert len(statements) == 2  # the narrative sentence and the printed box label


def test_scanned_form_has_no_text_layer() -> None:
    assert _texts("cioms_initial_scanned.pdf") == [""]


def test_followup_corrects_onset_date_on_page_two() -> None:
    page1, page2 = _texts("cioms_followup.pdf")
    assert "25a. Report type: initial followup" in page1  # the tick itself is a graphic
    assert f"Reaction onset: {ONSET}" in page1
    assert f"Corrected reaction onset date: {CIOMS_CASE['corrected_onset_date']}" in page2
    assert ONSET not in page2  # the conflict is between pages, not within one


# ---------------------------------------------------------------- AC2: seed and README


def _seed_rows_for(usubjid: str) -> list[str]:
    return [line for line in SEED.read_text(encoding="utf-8").splitlines() if usubjid in line]


def test_fixture_case_matches_acme_seed() -> None:
    rows = _seed_rows_for(f"ACM-101-{SUBJECT}")
    adsl = [r for r in rows if f"'{SUBJECT}'" in r]
    assert len(adsl) == 1, "subject must exist once in adsl"
    assert DRUG in adsl[0]  # treatment arm
    assert f"'{CIOMS_CASE['therapy_start_date']}'" in adsl[0]  # first dose date
    assert any(f"'{ONSET}'" in r and "liver injury" in r for r in rows), "adae onset row"


def test_fixtures_readme_lists_every_value() -> None:
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    missing = [value for value in CIOMS_CASE.values() if value not in readme]
    assert not missing
    assert "db/seed/tenant_acme.sql" in readme
    for name in build_fixtures():
        assert name in readme


# ---------------------------------------------------------------- AC3: OCR of the scanned form


async def test_tesseract_reads_scanned_cioms(tesseract_cmd: str) -> None:
    parser = TesseractOcrParser(dpi=300, lang="eng", cmd=tesseract_cmd)
    doc = await parser.parse(fixture_bytes("cioms_initial_scanned.pdf"))
    text = doc.pages[0].text
    for marker in (PRINTED_ID, ONSET, DRUG, CIOMS_CASE["mfr_control_no"]):
        assert marker in text


@pytest.mark.parametrize("name", ["cioms_initial.pdf", "cioms_followup.pdf"])
def test_digital_cioms_text_is_stable(name: str) -> None:
    assert page_texts(fixture_bytes(name)) == page_texts(build_fixtures()[name])
