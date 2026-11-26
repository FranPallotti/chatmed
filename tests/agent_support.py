"""Helpers for the M3 agent tests: a session whose facts come from the recorded extractions."""

from __future__ import annotations

from dataclasses import dataclass

from chatmed.agent import ToolContext
from chatmed.config import Settings
from chatmed.extraction import extract_for_tenant, load_facts
from chatmed.parsing import PdfiumParser
from chatmed.session import InMemorySessionStore, Session, StoredDocument
from tests.conftest import fixture_bytes
from tests.fakes import FakeModelClient, FakeTenantRepository
from tests.test_extraction_m2 import make_settings


@dataclass
class AgentSetup:
    settings: Settings
    store: InMemorySessionStore
    session: Session
    repo: FakeTenantRepository
    shas: dict[str, str]  # fixture name -> document sha256

    def ctx(self) -> ToolContext:
        """A fresh tool context (new evidence registry), as the API builds one per question."""
        return ToolContext(
            facts_db=self.store.facts_db(self.session.id),
            documents=self.store.get_documents(self.session.id),
            repo=self.repo,
            fulltext_max_chars=self.settings.agent_fulltext_max_chars,
            bm25_top_k=self.settings.agent_bm25_top_k,
        )


async def setup_session(
    recorded: FakeModelClient,
    names: list[str],
    *,
    tenant: str = "acme",
    settings: Settings | None = None,
) -> AgentSetup:
    """Parse and extract each fixture (recorded model output) into a new in-memory session."""
    settings = settings or make_settings()
    store = InMemorySessionStore(max_docs=5, duckdb_memory_limit="256MB")
    session = store.create(tenant)
    repo = FakeTenantRepository(tenant)
    shas = {}
    for name in names:
        doc = await PdfiumParser().parse(fixture_bytes(name))
        client = FakeModelClient(recorded.responses)
        result = await extract_for_tenant(client, repo, doc, "cioms", settings=settings)  # type: ignore[arg-type]
        store.put_document(session.id, StoredDocument(doc, "cioms", result.fingerprint))
        load_facts(store.facts_db(session.id), result.facts)
        shas[name] = doc.sha256
    return AgentSetup(settings, store, session, repo, shas)
