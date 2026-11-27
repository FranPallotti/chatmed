"""Memory under load inside the image (M5a AC1): N sessions uploading a scanned CIOMS at once.

Runs the real app (HTTP, multipart parsing, routing parser, Tesseract with
OCR_MAX_CONCURRENCY, in-memory sessions with DuckDB) with a fake model replaying the recorded
extraction and a fake tenant repository, inside the test image under the target limits:

    docker build --target test -t chatmed:test .
    docker run --rm --memory 4g --cpus 2 \
        -e AZURE_OPENAI_ENDPOINT=https://example.invalid/ -e AZURE_OPENAI_DEPLOYMENT=fake \
        -e TENANT_DB_DSN_TEMPLATE=postgresql://r_{tenant}@x/t_{tenant} \
        chatmed:test python scripts/measure_memory.py --sessions 8

Sample `docker stats` from the host while it runs for the externally observed peak. This script
prints the cgroup's own counters (memory.current/peak) and timings only: metadata, no content.
Synthetic fixture only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # tests.fakes (only in the test image / a checkout)
sys.path.insert(0, str(ROOT / "scripts"))

import uvicorn  # noqa: E402
from smoke_demo import multipart  # noqa: E402

from chatmed.api import create_app  # noqa: E402
from chatmed.config import Settings  # noqa: E402
from chatmed.parsing import build_parser  # noqa: E402
from chatmed.pipeline import Services  # noqa: E402
from chatmed.session import InMemorySessionStore  # noqa: E402
from tests.fakes import FakeModelClient, FakeTenantRepository  # noqa: E402

CGROUP = Path("/sys/fs/cgroup")
FIXTURE = ROOT / "fixtures" / "cioms_initial_scanned.pdf"
PORT = 8765
MB = 1024 * 1024


# cgroup v2 name -> v1 name (Docker Desktop on WSL2 still uses v1).
_V1 = {
    "memory.current": "memory/memory.usage_in_bytes",
    "memory.peak": "memory/memory.max_usage_in_bytes",
}


def cgroup_mb(name: str) -> int | None:
    for path in (CGROUP / name, CGROUP / _V1[name]):
        if path.is_file():
            return int(path.read_text().split()[0]) // MB
    return None


def post(path: str, body: bytes, content_type: str) -> tuple[int, dict[str, object]]:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=body, method="POST")
    req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.status, json.loads(resp.read())


def one_session(tenant: str) -> float:
    """Create a session and upload the scanned form; returns seconds for the upload."""
    _, created = post("/sessions", json.dumps({"tenant_id": tenant}).encode(), "application/json")
    body, ctype = multipart(FIXTURE.read_bytes(), "cioms")
    start = time.perf_counter()
    status, doc = post(f"/sessions/{created['session_id']}/documents", body, ctype)
    assert status == 200 and doc["page_count"] == 1, status
    return time.perf_counter() - start


async def main(sessions: int) -> None:
    settings = Settings()  # defaults: MAX_SESSIONS=8, OCR_MAX_CONCURRENCY=2, 128MB DuckDB
    services = Services(
        settings=settings,
        store=InMemorySessionStore(
            max_docs=settings.max_docs_per_session,
            duckdb_memory_limit=settings.duckdb_memory_limit,
            max_sessions=settings.max_sessions,
            ttl_seconds=settings.session_ttl_seconds,
        ),
        parser=await build_parser(settings),
        model_client=await asyncio.to_thread(FakeModelClient.from_recordings),
        repo_factory=FakeTenantRepository,
    )
    app = create_app(services=services, configure=False)
    server = uvicorn.Server(uvicorn.Config(app, port=PORT, log_config=None, log_level="error"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    print(f"idle: memory.current={cgroup_mb('memory.current')} MB", flush=True)
    start = time.perf_counter()
    uploads = await asyncio.gather(
        *(asyncio.to_thread(one_session, "acme") for _ in range(sessions))
    )
    total = time.perf_counter() - start
    print(
        f"{sessions} concurrent scanned uploads (max_sessions={settings.max_sessions}, "
        f"ocr_max_concurrency={settings.ocr_max_concurrency}): {total:.1f} s total, "
        f"slowest upload {max(uploads):.1f} s",
        flush=True,
    )
    print(f"after: memory.current={cgroup_mb('memory.current')} MB", flush=True)
    print(f"cgroup memory.peak={cgroup_mb('memory.peak')} MB (includes page cache)", flush=True)
    server.should_exit = True
    await serving


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sessions", type=int, default=8)
    asyncio.run(main(parser.parse_args().sessions))
