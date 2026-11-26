"""M3 AC9: opt-in test against the real Azure deployment (synthetic data only, D12).

The three demo questions, 3 runs each, each run in a fresh session (new extraction, new agent
loop) → identical `facts_used` across runs. The answer wording may differ; the facts may not.
Only verified answers claim reproducibility, so only those are compared (D14); on top, the M3
target is that every run verifies on the first attempt (no `agent.answer_rejected`).
Skipped by default. Run with: uv run pytest -m azure tests/test_agent_azure.py
Needs `.env` with the endpoint/deployment and an Entra ID login (DefaultAzureCredential).
The tenant DB is the fake repository that mirrors the acme seed (tests/fakes.py).
"""

from __future__ import annotations

import logging

import pytest

from chatmed.agent import Answer, ToolContext, run_agent
from chatmed.config import Settings
from chatmed.extraction import extract_for_tenant, load_facts, make_client
from chatmed.parsing import PdfiumParser
from chatmed.session import InMemorySessionStore, StoredDocument
from tests.conftest import fixture_bytes
from tests.fakes import FakeTenantRepository

pytestmark = pytest.mark.azure

RUNS = 3
QUESTIONS = (
    "What treatment arm is the subject in?",
    "Was the subject hospitalised?",
    "Does this CIOMS match what ADAE has for this subject?",
)


class _Rejections(logging.Handler):
    """Counts `agent.answer_rejected` events (metadata only)."""

    def __init__(self) -> None:
        super().__init__()
        self.codes: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "agent.answer_rejected":
            self.codes.append(str(record.__dict__.get("status")))


async def _run(question: str) -> tuple[Answer, list[str]]:
    """One fresh session: (the answer, the checker's rejection codes)."""
    settings = Settings()  # type: ignore[call-arg]
    client = make_client(settings.azure_openai_endpoint, settings.azure_openai_api_version)
    store = InMemorySessionStore(max_docs=5, duckdb_memory_limit="256MB")
    session = store.create("acme")
    repo = FakeTenantRepository("acme")
    try:
        doc = await PdfiumParser().parse(fixture_bytes("cioms_initial.pdf"))
        result = await extract_for_tenant(client, repo, doc, "cioms", settings=settings)
        store.put_document(session.id, StoredDocument(doc, "cioms", result.fingerprint))
        load_facts(store.facts_db(session.id), result.facts)
        ctx = ToolContext(
            facts_db=store.facts_db(session.id),
            documents=store.get_documents(session.id),
            repo=repo,
            fulltext_max_chars=settings.agent_fulltext_max_chars,
            bm25_top_k=settings.agent_bm25_top_k,
        )
        rejections = _Rejections()
        agent_log = logging.getLogger("chatmed.agent")
        agent_log.addHandler(rejections)
        agent_log.setLevel(logging.INFO)
        try:
            answer = await run_agent(client, session, ctx, question, settings=settings)
        finally:
            agent_log.removeHandler(rejections)
        return answer, rejections.codes
    finally:
        store.close(session.id)
        await client.close()


@pytest.mark.parametrize("question", QUESTIONS)
async def test_demo_question_gives_identical_facts_used(question: str) -> None:
    runs = [await _run(question) for _ in range(RUNS)]
    # Reproducibility is claimed for verified answers only (D14): compare those.
    verified = [answer for answer, _ in runs if answer.outcome == "verified"]
    assert len(verified) >= 2, [(a.outcome, a.reason) for a, _ in runs]
    assert verified[0].facts_used, "the answer used no facts"
    for other in verified[1:]:
        assert other.fingerprint == verified[0].fingerprint  # same inputs ...
        assert other.facts_used == verified[0].facts_used  # ... same facts
    # The M3 target on top: every run verified, and on the first attempt.
    assert [(a.outcome, a.reason, codes) for a, codes in runs] == [("verified", None, [])] * RUNS
