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


class _AgentEvents(logging.Handler):
    """Agent event names, and the checker's rejection codes (metadata only)."""

    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self.rejections: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.names.append(record.getMessage())
        if record.getMessage() == "agent.answer_rejected":
            self.rejections.append(str(record.__dict__.get("status")))


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
        events = _AgentEvents()
        agent_log = logging.getLogger("chatmed.agent")
        agent_log.addHandler(events)
        agent_log.setLevel(logging.INFO)
        try:
            answer = await run_agent(client, session, ctx, question, settings=settings)
        finally:
            agent_log.removeHandler(events)
        return answer, events.rejections
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


RECON_RUNS = 20  # per document: 40 runs in all
RECON_QUESTIONS = (
    "Does this CIOMS match what ADAE has for this subject?",
    "Does this report agree with the adverse event in the study database?",
)


@pytest.mark.parametrize("fixture", ["cioms_initial.pdf", "cioms_followup.pdf"])
async def test_reconciliation_verifies_on_the_first_attempt(fixture: str) -> None:
    """D14 (agent-v7): the real model only routes; code renders. 20 runs per document, each in
    a fresh session (the extraction is done once per document to save model calls): every run
    verified, on the first attempt, rendered by code, with the same text and facts."""
    settings = Settings()  # type: ignore[call-arg]
    client = make_client(settings.azure_openai_endpoint, settings.azure_openai_api_version)
    repo = FakeTenantRepository("acme")
    events = _AgentEvents()
    agent_log = logging.getLogger("chatmed.agent")
    agent_log.addHandler(events)
    agent_log.setLevel(logging.INFO)
    answers: list[Answer] = []
    try:
        doc = await PdfiumParser().parse(fixture_bytes(fixture))
        result = await extract_for_tenant(client, repo, doc, "cioms", settings=settings)
        for run in range(RECON_RUNS):
            store = InMemorySessionStore(max_docs=5, duckdb_memory_limit="256MB")
            session = store.create("acme")
            try:
                store.put_document(session.id, StoredDocument(doc, "cioms", result.fingerprint))
                load_facts(store.facts_db(session.id), result.facts)
                ctx = ToolContext(
                    facts_db=store.facts_db(session.id),
                    documents=store.get_documents(session.id),
                    repo=repo,
                    fulltext_max_chars=settings.agent_fulltext_max_chars,
                    bm25_top_k=settings.agent_bm25_top_k,
                )
                question = RECON_QUESTIONS[run % len(RECON_QUESTIONS)]
                answers.append(await run_agent(client, session, ctx, question, settings=settings))
            finally:
                store.close(session.id)
    finally:
        agent_log.removeHandler(events)
        await client.close()
    assert [(a.outcome, a.reason) for a in answers] == [("verified", None)] * RECON_RUNS
    assert "agent.answer_rejected" not in events.names
    assert events.names.count("agent.reconciliation_rendered") == RECON_RUNS
    assert len({(a.text, str(a.facts_used)) for a in answers}) == 1
