"""M1b: synthetic CIOMS I fixtures for acme subject 01001-004 (D12).

AC1 shapes and values, AC2 consistency with the acme seed and fixtures/README.md,
AC3 OCR of the scanned form (parser contract cases are in tests/contracts/).
"""

from __future__ import annotations

import pytest
from make_fixtures import CIOMS_CASE, build_fixtures

from chatmed.parsing import TesseractOcrParser
from chatmed.parsing.pdfium import page_texts
from tests.conftest import FIXTURES, ROOT, fixture_bytes

SEED = ROOT / "db" / "seed" / "tenant_acme.sql"
SUBJECT = CIOMS_CASE["subject_id"]
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
    assert f"Subject {SUBJECT}" in page
    assert CIOMS_CASE["corrected_onset_date"] not in page


def test_only_hospitalisation_is_checked() -> None:
    (page,) = _texts("cioms_initial.pdf")
    assert "[X] involved or prolonged inpatient hospitalisation" in page
    for criterion in (
        "patient died",
        "involved persistent or significant disability or incapacity",
        "life threatening",
    ):
        assert f"[ ] {criterion}" in page


def test_dechallenge_rechallenge_and_report_type() -> None:
    (page,) = _texts("cioms_initial.pdf")
    assert "stopping drug? [X] yes [ ] no [ ] NA" in page
    assert "reintroduction? [ ] yes [ ] no [X] NA" in page
    assert "[X] initial [ ] followup" in page


def test_scanned_form_has_no_text_layer() -> None:
    assert _texts("cioms_initial_scanned.pdf") == [""]


def test_followup_corrects_onset_date_on_page_two() -> None:
    page1, page2 = _texts("cioms_followup.pdf")
    assert "[ ] initial [X] followup" in page1
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
    for marker in (SUBJECT, ONSET, DRUG, CIOMS_CASE["mfr_control_no"]):
        assert marker in text


@pytest.mark.parametrize("name", ["cioms_initial.pdf", "cioms_followup.pdf"])
def test_digital_cioms_text_is_stable(name: str) -> None:
    assert page_texts(fixture_bytes(name)) == page_texts(build_fixtures()[name])
