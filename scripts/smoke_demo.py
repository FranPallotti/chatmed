"""Smoke test of the demo flow against a running app (M5a AC3). Synthetic fixtures only.

    docker compose up -d --build
    uv run python scripts/smoke_demo.py --tenant acme

Steps: /healthz → create a session → upload the scanned synthetic CIOMS → ask the demo
questions (NDJSON stream) → close the session → check that the id is now `session_closed`.
Prints the outcome, reason, statuses and answer text of each question (synthetic data, to
this terminal only). Exit code 1 on any HTTP error or error event, or, with
`--require-verified`, on a fallback answer.

Standard library only (urllib), so it runs from any Python without extra dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "cioms_initial_scanned.pdf"
QUESTIONS = (
    "Which treatment arm is the subject of this report in?",
    "Was the patient hospitalised?",
    "Does this report match the adverse event recorded in the study database?",
)
TIMEOUT_S = 180  # an upload of a scanned form runs OCR and extraction


class SmokeFailure(Exception):
    pass


def request(
    base: str, method: str, path: str, body: bytes | None = None, content_type: str = ""
) -> tuple[int, bytes]:
    req = urllib.request.Request(base + path, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def expect(status: int, body: bytes, wanted: int, step: str) -> Any:
    if status != wanted:
        raise SmokeFailure(f"{step}: HTTP {status} {body[:200]!r}")
    return json.loads(body) if body else None


def multipart(pdf: bytes, doc_type: str) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="doc_type"\r\n\r\n{doc_type}\r\n',
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="form.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n",
    ]
    body = parts[0].encode() + parts[1].encode() + pdf + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def ask(base: str, sid: str, question: str) -> dict[str, Any]:
    status, raw = request(
        base,
        "POST",
        f"/sessions/{sid}/ask",
        json.dumps({"question": question}).encode(),
        "application/json",
    )
    if status != 200:
        raise SmokeFailure(f"ask: HTTP {status} {raw[:200]!r}")
    events = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    if not events or events[0].get("event") != "started":
        raise SmokeFailure("ask: stream did not start with a 'started' event")
    last = events[-1]
    if last.get("event") == "error":
        raise SmokeFailure(f"ask: error event {last.get('code')}")
    if last.get("event") != "answer":
        raise SmokeFailure("ask: no answer event")
    tools = [e["name"] for e in events if e.get("event") == "tool"]
    return last | {"tools": tools}


def statuses(answer: dict[str, Any]) -> list[str]:
    found = set()
    for fact in answer.get("facts_used", []):
        for key in ("status", "join_status"):
            if isinstance(fact.get("values", {}).get(key), str):
                found.add(fact["values"][key])
    return sorted(found)


def run(base: str, tenant: str, require_verified: bool) -> None:
    expect(*request(base, "GET", "/healthz"), 200, "healthz")
    print("healthz ok")
    created = expect(
        *request(
            base,
            "POST",
            "/sessions",
            json.dumps({"tenant_id": tenant}).encode(),
            "application/json",
        ),
        201,
        "create session",
    )
    sid = created["session_id"]
    print("session created")
    try:
        body, ctype = multipart(FIXTURE.read_bytes(), "cioms")
        doc = expect(
            *request(base, "POST", f"/sessions/{sid}/documents", body, ctype), 200, "upload"
        )
        print(f"uploaded {FIXTURE.name}: pages={doc['page_count']} parser={doc['parser_id']}")
        print(f"  statuses={doc['statuses']} join={doc['join_status']}")
        fallbacks = 0
        for question in QUESTIONS:
            answer = ask(base, sid, question)
            fallbacks += answer["outcome"] != "verified"
            print(f"\nQ: {question}")
            print(
                f"  outcome={answer['outcome']} reason={answer.get('reason')} "
                f"tools={answer['tools']} facts_used={len(answer['facts_used'])} "
                f"statuses={statuses(answer)}"
            )
            print(f"  A: {answer['text']}")
        if require_verified and fallbacks:
            raise SmokeFailure(f"{fallbacks} answer(s) were fallbacks")
    finally:
        status, _ = request(base, "DELETE", f"/sessions/{sid}")
        print(f"\nsession closed (HTTP {status})")
    status, raw = request(
        base,
        "POST",
        f"/sessions/{sid}/ask",
        json.dumps({"question": "x"}).encode(),
        "application/json",
    )
    detail = json.loads(raw).get("detail") if raw else None
    if (status, detail) != (404, "session_closed"):
        raise SmokeFailure(f"after close: HTTP {status} {detail}")
    print("closed id answers 404 session_closed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--require-verified", action="store_true")
    args = parser.parse_args()
    try:
        run(args.base_url.rstrip("/"), args.tenant, args.require_verified)
    except (SmokeFailure, urllib.error.URLError, TimeoutError) as exc:
        print(f"SMOKE FAILED: {exc}")
        return 1
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
