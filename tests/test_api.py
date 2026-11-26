"""M3 AC6: API (create, upload, ask as NDJSON, close), limits, in-memory uploads, no body logs."""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest
import starlette.formparsers
from fastapi.testclient import TestClient

from chatmed.api import Services, create_app
from chatmed.parsing import PdfiumParser
from chatmed.session import InMemorySessionStore
from tests.conftest import fixture_bytes
from tests.fakes import FakeModelClient, FakeTenantRepository, ScriptedModelClient, final, tool_call
from tests.test_extraction_m2 import make_settings

MARKERS = ("01001-004", "Zentrobal", "ACM-2026-0042", "hospitalised", "SECRET-QUESTION")


@pytest.fixture(scope="module")
def recorded() -> FakeModelClient:
    return FakeModelClient.from_recordings()


def make_client(
    recorded: FakeModelClient, steps: list[Any] | None = None, **settings: Any
) -> TestClient:
    s = make_settings(**settings)
    services = Services(
        settings=s,
        store=InMemorySessionStore(max_docs=s.max_docs_per_session, duckdb_memory_limit="256MB"),
        parser=PdfiumParser(),
        model_client=ScriptedModelClient(
            steps or [], extraction=FakeModelClient(recorded.responses)
        ),
        repo_factory=FakeTenantRepository,
    )
    return TestClient(create_app(services=services, configure=False))


@pytest.fixture
def api(recorded: FakeModelClient) -> Iterator[TestClient]:
    with make_client(recorded) as client:
        yield client


def new_session(client: TestClient, tenant: str = "acme") -> str:
    resp = client.post("/sessions", json={"tenant_id": tenant})
    assert resp.status_code == 201
    return str(resp.json()["session_id"])


def upload(
    client: TestClient, sid: str, name: str = "cioms_initial.pdf", doc_type: str = "cioms"
) -> Any:
    files = {"file": (name, fixture_bytes(name), "application/pdf")}
    return client.post(f"/sessions/{sid}/documents", files=files, data={"doc_type": doc_type})


def events(resp: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in resp.text.splitlines() if line]


# ---------------------------------------------------------------- sessions


@pytest.mark.parametrize("tenant", ["initech", "ACME", "acme dbname=postgres", ""])
def test_tenant_must_be_allowed(api: TestClient, tenant: str) -> None:
    resp = api.post("/sessions", json={"tenant_id": tenant})
    assert resp.status_code == 422


def test_unknown_session_is_404_and_close_is_idempotent(api: TestClient) -> None:
    sid = new_session(api)
    assert api.delete(f"/sessions/{sid}").status_code == 204
    assert api.delete(f"/sessions/{sid}").status_code == 204
    assert upload(api, sid).status_code == 404
    assert api.post(f"/sessions/{sid}/ask", json={"question": "x"}).status_code == 404


# ---------------------------------------------------------------- upload


def test_upload_extracts_and_summarises(api: TestClient) -> None:
    sid = new_session(api)
    resp = upload(api, sid)
    assert resp.status_code == 200
    body = resp.json()
    assert body["sha256"] == hashlib.sha256(fixture_bytes("cioms_initial.pdf")).hexdigest()
    assert body["page_count"] == 1 and body["join_status"] == "joined"
    assert body["statuses"]["undeterminable"] >= 1 and body["statuses"]["ok"] >= 1
    assert not any(m in resp.text for m in MARKERS)  # a summary, not the values


def test_upload_limits(recorded: FakeModelClient) -> None:
    with make_client(recorded, max_upload_mb=1, max_docs_per_session=1) as client:
        sid = new_session(client)
        big = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 1)
        resp = client.post(
            f"/sessions/{sid}/documents",
            files={"file": ("big.pdf", big, "application/pdf")},
            data={"doc_type": "cioms"},
        )
        assert (resp.status_code, resp.json()["detail"]) == (413, "upload_too_large")
        assert upload(client, sid).status_code == 200
        resp = upload(client, sid, "cioms_followup.pdf")
        assert (resp.status_code, resp.json()["detail"]) == (409, "too_many_documents")
    with make_client(recorded, max_pages_per_doc=1) as client:
        resp = upload(client, new_session(client), "cioms_followup.pdf")  # 2 pages
        assert (resp.status_code, resp.json()["detail"]) == (422, "too_many_pages")


PDF = "application/pdf"


@pytest.mark.parametrize(
    ("files", "data", "status", "detail"),
    [
        ({"file": ("a.txt", b"hello", "text/plain")}, {"doc_type": "cioms"}, 415, "pdf_required"),
        ({"file": ("a.pdf", b"%PDF-broken", PDF)}, {"doc_type": "cioms"}, 422, "unreadable_pdf"),
        (None, {"doc_type": "cioms"}, 415, "multipart_required"),
        ({"file": ("a.pdf", b"%PDF-", PDF)}, {}, 422, "file_and_doc_type_required"),
        ({"file": ("a.pdf", b"%PDF-", PDF)}, {"doc_type": "CIOMS;x"}, 422, "invalid_doc_type"),
    ],
)
def test_bad_uploads(
    api: TestClient, files: Any, data: dict[str, str], status: int, detail: str
) -> None:
    sid = new_session(api)
    resp = api.post(f"/sessions/{sid}/documents", files=files, data=data)
    assert (resp.status_code, resp.json()["detail"]) == (status, detail)


def test_unknown_document_type_and_duplicates(api: TestClient) -> None:
    sid = new_session(api)
    resp = upload(api, sid, doc_type="lab_report")
    assert (resp.status_code, resp.json()["detail"]) == (422, "unknown_document_type")
    assert upload(api, sid).status_code == 200
    resp = upload(api, sid)
    assert (resp.status_code, resp.json()["detail"]) == (409, "duplicate_document")


async def _raw_upload(
    recorded: FakeModelClient, chunks: list[bytes], content_length: int | None
) -> tuple[int, dict[str, Any], int]:
    """POST an upload straight into the ASGI app: (status, json body, chunks the app read)."""
    s = make_settings(max_upload_mb=1)
    store = InMemorySessionStore(max_docs=5, duckdb_memory_limit="256MB")
    services = Services(
        settings=s,
        store=store,
        parser=PdfiumParser(),
        model_client=ScriptedModelClient([], extraction=FakeModelClient(recorded.responses)),
        repo_factory=FakeTenantRepository,
    )
    app = create_app(services=services, configure=False)
    sid = store.create("acme").id
    headers = [(b"content-type", b"multipart/form-data; boundary=xyz")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": f"/sessions/{sid}/documents", "raw_path": b"",
        "query_string": b"", "headers": headers, "client": ("t", 1), "server": ("t", 80),
    }  # fmt: skip
    read = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal read
        if read < len(chunks):
            read += 1
            return {"type": "http.request", "body": chunks[read - 1], "more_body": True}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    store.close(sid)
    return status, json.loads(body), read


async def test_declared_oversized_body_is_refused_before_reading(
    recorded: FakeModelClient,
) -> None:
    chunks = [b"0" * 1024] * 4
    status, body, read = await _raw_upload(recorded, chunks, content_length=2 * 1024 * 1024)
    assert (status, body["detail"], read) == (413, "upload_too_large", 0)


async def test_oversized_body_without_content_length_stops_at_the_limit(
    recorded: FakeModelClient,
) -> None:
    """Chunked upload, no Content-Length: the byte counter ends the read past MAX_UPLOAD_MB."""
    chunk = b"0" * (256 * 1024)
    chunks = [chunk] * 40  # 10 MB offered, limit 1 MB (+ multipart overhead)
    status, body, read = await _raw_upload(recorded, chunks, content_length=None)
    assert (status, body["detail"]) == (413, "upload_too_large")
    assert read == 5  # 4 chunks = exactly 1 MB fit; the 5th crosses the limit; nothing after


async def test_understated_content_length_is_caught_by_the_counter(
    recorded: FakeModelClient,
) -> None:
    chunks = [b"0" * (512 * 1024)] * 6
    status, body, read = await _raw_upload(recorded, chunks, content_length=100)
    assert (status, body["detail"], read) == (413, "upload_too_large", 3)


def test_large_upload_is_parsed_in_memory(
    recorded: FakeModelClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 1: Starlette's UploadFile spools > 1 MB to disk; our parser never touches tempfile."""

    def no_temp(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a temporary file was created")

    for name in ("SpooledTemporaryFile", "NamedTemporaryFile", "TemporaryFile", "mkstemp"):
        monkeypatch.setattr(tempfile, name, no_temp)
    # Starlette binds its own name at import; patch it too, so `request.form()` would fail here.
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", no_temp)
    padded = fixture_bytes("cioms_initial.pdf") + b"\n%" + b"0" * (2 * 1024 * 1024)
    with make_client(recorded, max_upload_mb=5) as client:
        sid = new_session(client)
        resp = client.post(
            f"/sessions/{sid}/documents",
            files={"file": ("padded.pdf", padded, "application/pdf")},
            data={"doc_type": "cioms"},
        )
        assert resp.status_code == 200
        assert resp.json()["sha256"] == hashlib.sha256(padded).hexdigest()


# ---------------------------------------------------------------- ask (NDJSON) and logs


def test_ask_streams_metadata_events_then_the_verified_answer(
    recorded: FakeModelClient, caplog: pytest.LogCaptureFixture
) -> None:
    sha = hashlib.sha256(fixture_bytes("cioms_initial.pdf")).hexdigest()[:12]
    steps = [
        tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["hospitalisation"]}),
        final(
            "Yes. The report states the patient was hospitalised on 2026-06-13 (page 1).",
            [f"fact:{sha}:hospitalisation:1"],
        ),
    ]
    with caplog.at_level(logging.DEBUG), make_client(recorded, steps) as client:
        sid = new_session(client)
        assert upload(client, sid).status_code == 200
        resp = client.post(f"/sessions/{sid}/ask", json={"question": "SECRET-QUESTION?"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/x-ndjson")
        out = events(resp)
        assert [e["event"] for e in out] == ["started", "tool", "answer"]
        assert out[1] == {"event": "tool", "name": "query_document_facts", "iteration": 1}
        assert out[2]["verified"] is True
        assert [f["id"] for f in out[2]["facts_used"]] == [f"fact:{sha}:hospitalisation:1"]
        client.delete(f"/sessions/{sid}")
    for record in caplog.records:  # no body, question, value or answer in any log record
        dumped = record.getMessage() + repr(
            {k: v for k, v in record.__dict__.items() if k not in ("exc_info", "msg", "args")}
        )
        assert not any(m in dumped for m in MARKERS), record.getMessage()


def test_model_failure_is_an_error_event(recorded: FakeModelClient) -> None:
    import httpx2 as httpx
    import openai

    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid"))
    error = openai.BadRequestError("Zentrobal", response=response, body=None)
    with make_client(recorded, [error]) as client:
        sid = new_session(client)
        resp = client.post(f"/sessions/{sid}/ask", json={"question": "Arm?"})
        assert events(resp) == [
            {"event": "started"},
            {"event": "error", "code": "model_rejected"},
        ]
