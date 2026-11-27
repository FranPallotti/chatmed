"""M4 AC6 opt-in: two separate sessions against the real Azure deployment (synthetic data only).

3 pairs per demo question: each session is `run_full_session` (create → upload the synthetic
CIOMS → ask → close) with the real model for extraction and the agent. Both sessions of every
pair must be `verified` and give identical `facts_used` and the same answer fingerprint; the
extraction facts must be identical too. Skipped by default. Run with:
    uv run pytest -m azure tests/test_cross_session_azure.py
Needs `.env` with the endpoint/deployment and an Entra ID login (DefaultAzureCredential).
The tenant DB is the fake repository that mirrors the acme seed (tests/fakes.py).
"""

from __future__ import annotations

import pytest

from chatmed.config import Settings
from chatmed.extraction import make_client
from chatmed.parsing import PdfiumParser
from chatmed.pipeline import Services, SessionRun, run_full_session
from tests.conftest import FIXTURES
from tests.fakes import FakeTenantRepository
from tests.pipeline_support import DEMO, FIXTURE, make_store

pytestmark = pytest.mark.azure

PAIRS = 3


async def _session(services: Services, question: str) -> SessionRun:
    return await run_full_session(services, "acme", [FIXTURES / FIXTURE], "cioms", [question])


@pytest.mark.parametrize("question", list(DEMO))
async def test_two_real_sessions_give_the_same_facts(question: str) -> None:
    settings = Settings()  # type: ignore[call-arg]
    client = make_client(settings.azure_openai_endpoint, settings.azure_openai_api_version)
    services = Services(
        settings=settings,
        store=make_store(),
        parser=PdfiumParser(),
        model_client=client,
        repo_factory=FakeTenantRepository,
    )
    try:
        for _ in range(PAIRS):
            a = await _session(services, question)
            b = await _session(services, question)
            assert a.session_id != b.session_id
            answers = (a.answers[0], b.answers[0])
            assert [x.outcome for x in answers] == ["verified", "verified"], [
                x.reason for x in answers
            ]
            assert a.facts == b.facts  # extraction: identical facts in both sessions
            assert answers[0].facts_used == answers[1].facts_used
            assert answers[0].fingerprint == answers[1].fingerprint
    finally:
        await client.close()
