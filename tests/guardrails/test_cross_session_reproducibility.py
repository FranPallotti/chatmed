"""M4 AC6: same PDFs + same question in two separate sessions → same facts (the challenge's
requirement, verbatim).

Through the HTTP API, as a client would: create → upload the synthetic CIOMS → ask → close,
then the same again in a new session of the same app (same process, same store), N=5 pairs
per demo question. Every answer must be `verified` and every session must give the same
`facts_used`, the same answer fingerprint and the same extraction fingerprint. The model is
the recorded extraction plus a scripted agent, so this proves our code adds no variation
(ordering, ids, leftovers from an earlier session); the opt-in real-model version is
`tests/test_cross_session_azure.py`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chatmed.api import create_app
from tests.conftest import fixture_bytes
from tests.fakes import FakeModelClient
from tests.pipeline_support import DEMO, FIXTURE, SHA, make_services

PAIRS = 5


@pytest.fixture(scope="module")
def recorded() -> FakeModelClient:
    return FakeModelClient.from_recordings()


def _session(api: TestClient, question: str) -> dict[str, Any]:
    """One whole client session; returns what must be identical across sessions."""
    sid = api.post("/sessions", json={"tenant_id": "acme"}).json()["session_id"]
    files = {"file": (FIXTURE, fixture_bytes(FIXTURE), "application/pdf")}
    upload = api.post(f"/sessions/{sid}/documents", files=files, data={"doc_type": "cioms"})
    assert upload.status_code == 200
    resp = api.post(f"/sessions/{sid}/ask", json={"question": question})
    answer = [json.loads(line) for line in resp.text.splitlines() if line][-1]
    assert api.delete(f"/sessions/{sid}").status_code == 204
    assert answer["event"] == "answer"
    return {
        "session_id": sid,
        "outcome": answer["outcome"],
        "facts_used": answer["facts_used"],
        "fingerprint": answer["fingerprint"],
        "document": upload.json(),
    }


@pytest.mark.parametrize("question", list(DEMO))
def test_two_sessions_give_the_same_facts(recorded: FakeModelClient, question: str) -> None:
    sessions = 2 * PAIRS
    steps = [step for _ in range(sessions) for step in DEMO[question](SHA)]
    services = make_services(recorded, steps)
    with TestClient(create_app(services=services, configure=False)) as api:
        runs = [_session(api, question) for _ in range(sessions)]

    assert len({r["session_id"] for r in runs}) == sessions  # really separate sessions
    assert [r["outcome"] for r in runs] == ["verified"] * sessions
    first = runs[0]
    assert first["facts_used"], "the answer used no facts"
    assert first["fingerprint"]
    for pair in range(PAIRS):
        a, b = runs[2 * pair], runs[2 * pair + 1]
        for run in (a, b):
            assert run["facts_used"] == first["facts_used"]
            assert run["fingerprint"] == first["fingerprint"]
            assert run["document"] == first["document"]  # same extraction fingerprint, statuses
