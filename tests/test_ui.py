"""M5a AC2 (`/healthz`) and AC4 (static UI served by FastAPI, no new dependency)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from importlib import resources

import pytest
from fastapi.testclient import TestClient

from chatmed.agent.answer import HONESTY_STATUSES
from tests.fakes import FakeModelClient
from tests.test_api import make_client, new_session

# The installed package, as served (in the image there is no src/ tree, only the venv).
STATIC = resources.files("chatmed") / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def recorded() -> FakeModelClient:
    return FakeModelClient.from_recordings()


@pytest.fixture
def api(recorded: FakeModelClient) -> Iterator[TestClient]:
    with make_client(recorded) as client:
        yield client


# ---------------------------------------------------------------- AC2: liveness


def test_healthz_is_fixed_and_has_no_tenant_data(api: TestClient) -> None:
    new_session(api)  # an open session changes nothing
    resp = api.get("/healthz")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


# ---------------------------------------------------------------- AC4: the page


@pytest.mark.parametrize(
    ("path", "media_type"),
    [("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")],
)
def test_ui_files_are_served_with_a_strict_csp(api: TestClient, path: str, media_type: str) -> None:
    resp = api.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(media_type)
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_page_loads_only_same_origin_files(api: TestClient) -> None:
    html = api.get("/").text
    assert re.findall(r'(?:src|href)="([^"]+)"', html) == ["/app.css", "/app.js"]
    assert not re.search(r"<script\b[^>]*>\s*[^<\s]", html)  # no inline script
    assert not re.search(r"\son[a-z]+\s*=", html)  # no inline event handlers
    assert not re.search(r"\sstyle\s*=", html)  # no inline styles (CSP style-src 'self')


def test_every_response_is_no_store(api: TestClient) -> None:
    """Answers and facts must not land in a browser or proxy disk cache (rule 1)."""
    sid = new_session(api)
    for resp in (
        api.get("/"),
        api.get("/healthz"),
        api.post("/sessions", json={"tenant_id": "acme"}),
        api.post(f"/sessions/{sid}/ask", json={"question": "x"}),
        api.get("/sessions/nope/ask"),  # 405, still no-store
    ):
        assert resp.headers["cache-control"] == "no-store", resp.request.url


def test_ui_never_renders_html_or_stores_anything() -> None:
    """Server values are rendered as text (no XSS from document content) and nothing is kept
    in browser storage or cookies (rule 1: the page holds content only in memory)."""
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
        "caches.",
        "eval(",
    ):
        assert forbidden not in APP_JS, forbidden


def test_ui_marks_every_honesty_status_visibly() -> None:
    """Every status the user must be told about has a warning style in the UI."""
    marked = dict(re.findall(r"(\w+): \"(good|warn|bad)\"", APP_JS))
    for status in HONESTY_STATUSES | {"mismatch", "fallback"}:
        assert marked.get(status) in {"warn", "bad"}, status
    assert marked["ok"] == "good" and marked["joined"] == "good"


def test_ui_has_the_required_controls() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for element in ('id="tenant"', 'type="file"', 'id="question"', 'id="close-btn"'):
        assert element in html
    assert "<select" not in html  # tenant is a text field, validated by the server (D14)
    for endpoint in ('"/sessions"', "/documents`", "/ask`", 'method: "DELETE"'):
        assert endpoint in APP_JS


def test_static_files_ship_inside_the_package() -> None:
    """Served from `chatmed/static` via importlib.resources, so the wheel/image has them."""

    names = {p.name for p in (resources.files("chatmed") / "static").iterdir()}
    assert {"index.html", "app.js", "app.css"} <= names
