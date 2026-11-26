"""M2 AC5: opt-in integration test against the real Azure OpenAI deployment (D9).

Skipped by default (`-m 'not azure'` in pyproject). Run with:
    uv run pytest -m azure tests/test_extraction_azure.py
Needs `.env` with the endpoint/deployment and an Entra ID login (DefaultAzureCredential).
Sends only the synthetic CIOMS fixtures (D12). Field metadata mirrors the acme seed
(`tests/fakes.py`, checked against the real DB in tests/test_tenant_db.py).
"""

from __future__ import annotations

import pytest

from chatmed.config import Settings
from chatmed.extraction import ExtractionResult, JoinStatus, Status, extract_for_tenant, make_client
from chatmed.parsing import PdfiumParser
from tests.conftest import fixture_bytes
from tests.fakes import FakeTenantRepository

pytestmark = pytest.mark.azure


async def _extract(name: str) -> ExtractionResult:
    settings = Settings()  # type: ignore[call-arg]
    client = make_client(settings.azure_openai_endpoint, settings.azure_openai_api_version)
    doc = await PdfiumParser().parse(fixture_bytes(name))
    try:
        return await extract_for_tenant(
            client, FakeTenantRepository("acme"), doc, "cioms", settings=settings
        )
    finally:
        await client.close()


def _by_key(result: ExtractionResult) -> dict[str, list[tuple[str, str | None]]]:
    out: dict[str, list[tuple[str, str | None]]] = {}
    for f in result.facts:
        out.setdefault(f.field_key, []).append((f.status, f.value_text))
    return out


RUNS = 3  # the model itself is not deterministic; code must make the facts so (D6, D13b)


async def test_real_model_initial_form_is_reproducible() -> None:
    results = [await _extract("cioms_initial.pdf") for _ in range(RUNS)]
    first = results[0]
    for other in results[1:]:
        assert other.facts == first.facts  # same document, same facts (D6)

    facts = _by_key(first)
    assert facts["reaction_onset_date"] == [(Status.OK, "2026-06-12")]
    assert facts["suspect_drug"] == [(Status.OK, "Zentrobal")]
    assert facts["hospitalisation"] == [(Status.OK, "true")]  # from the narrative
    assert facts["patient_died"] == [(Status.UNDETERMINABLE, None)]  # label/heading only
    assert facts["dechallenge"] == [(Status.UNDETERMINABLE, None)]  # tick mark only
    subject = next(f for f in first.facts if f.field_key == "subject_id")
    assert (subject.join_status, subject.join_value) == (JoinStatus.JOINED, "01001-004")


async def test_real_model_reports_the_corrected_onset_as_conflict() -> None:
    facts = _by_key(await _extract("cioms_followup.pdf"))
    assert sorted(facts["reaction_onset_date"]) == [
        (Status.CONFLICT, "2026-06-11"),
        (Status.CONFLICT, "2026-06-12"),
    ]
