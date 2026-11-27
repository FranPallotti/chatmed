"""Helpers for the M4 tests: services built from fakes, a gate to hold a model call open.

`make_services` wires the real pipeline (store, parser, agent) to the recorded extraction
outputs and a scripted agent, so tests run the code that serves requests without a network.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Any

from chatmed.pipeline import Services
from chatmed.session import InMemorySessionStore
from tests.conftest import fixture_bytes
from tests.fakes import FakeModelClient, FakeTenantRepository, ScriptedModelClient, final, tool_call
from tests.test_agent_loop import arm_script, hospitalised_script, reconcile_script
from tests.test_extraction_m2 import make_settings

FIXTURE = "cioms_initial.pdf"
SHA = hashlib.sha256(fixture_bytes(FIXTURE)).hexdigest()

# The three M3 demo questions with their scripted tool calls (tests/test_agent_loop.py).
DEMO: dict[str, Callable[[str], list[Any]]] = {
    "What treatment arm is the subject in?": arm_script,
    "Was the subject hospitalised?": hospitalised_script,
    "Does this CIOMS match what ADAE has for this subject?": reconcile_script,
}
# A question that walks the less common log paths: a refused tool call, a rejected answer
# (a value not in facts_used) and the retry.
RETRY_QUESTION = "How old is the patient?"


def retry_script(sha: str) -> list[Any]:
    s = sha[:12]
    return [
        tool_call("get_subject", {"subjid": "01001-001"}),  # not joined: refused with a code
        tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["age"]}),
        final("The patient is 48 years old.", [f"fact:{s}:age:1"]),  # rejected
        final("The patient is 47 years old.", [f"fact:{s}:age:1"]),
    ]


def make_store(**overrides: Any) -> InMemorySessionStore:
    options: dict[str, Any] = {"max_docs": 5, "duckdb_memory_limit": "256MB"} | overrides
    return InMemorySessionStore(**options)


def make_services(
    recorded: FakeModelClient,
    steps: list[Any] | None = None,
    *,
    store: InMemorySessionStore | None = None,
    model_client: Any = None,
    **settings: Any,
) -> Services:
    """Real pipeline, fake model and tenant DB. `steps` script the agent (in order)."""
    from chatmed.parsing import PdfiumParser

    s = make_settings(**settings)
    return Services(
        settings=s,
        store=store or make_store(max_docs=s.max_docs_per_session, max_sessions=s.max_sessions),
        parser=PdfiumParser(),
        model_client=model_client
        or ScriptedModelClient(steps or [], extraction=FakeModelClient(recorded.responses)),
        repo_factory=FakeTenantRepository,
    )


class GatedClient:
    """Wraps a model client; the first call of the chosen kind waits until `release` is set.

    `kind` is "extraction" (requests without tools) or "agent" (requests with tools). Lets a
    test close a session while an upload or a question is waiting on the model (M4 AC2).
    `calls_after_release` counts calls that reached the inner client after the gate opened.
    """

    def __init__(self, inner: Any, kind: str) -> None:
        self.inner = inner
        self.kind = kind
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.chat = self
        self.completions = self

    async def create(self, **request: Any) -> Any:
        is_agent = "tools" in request
        if (self.kind == "agent") == is_agent and not self.entered.is_set():
            self.entered.set()
            await self.release.wait()
        self.calls += 1
        return await self.inner.create(**request)
