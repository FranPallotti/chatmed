"""Guardrails for hard rules 1 and 2. Enabled once `chatmed.pipeline` exists (milestone M4).

The pipeline module must expose:
    async def run_full_session(pdf_paths: list[Path], doc_type: str, question: str,
                               *, model_client) -> str
which creates a session, uploads, extracts, asks, closes, and returns the answer text.
Tests use a fake model client (fixtures/recorded) so no network is needed.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pytest

pipeline = pytest.importorskip("chatmed.pipeline", reason="enabled in M4")


def _snapshot(dirs: list[Path]) -> set[Path]:
    files: set[Path] = set()
    for d in dirs:
        if d.exists():
            files |= {p for p in d.rglob("*") if p.is_file()}
    return files


@pytest.fixture
def fake_client():
    from tests.fakes import FakeModelClient  # created in M2/M3

    return FakeModelClient.from_recordings("fixtures/recorded")


async def test_full_run_writes_no_files(tmp_path, monkeypatch, fake_client):
    monkeypatch.chdir(tmp_path)
    watched = [tmp_path, Path(tempfile.gettempdir())]
    before = _snapshot(watched)
    await pipeline.run_full_session(
        [Path(os.environ.get("FIXTURE_PDF", "fixtures/lab_report.pdf")).resolve()],
        "lab_report",
        "What is the hemoglobin value?",
        model_client=fake_client,
    )
    created = _snapshot(watched) - before
    assert not created, f"files written during a session: {sorted(created)[:5]}"


async def test_logs_contain_no_document_content(caplog, fake_client):
    secret_markers = ["Hemoglobin", "13.4", "00123"]  # values present in the fixture PDF
    with caplog.at_level(logging.DEBUG):
        await pipeline.run_full_session(
            [Path("fixtures/lab_report.pdf").resolve()],
            "lab_report",
            "What is the hemoglobin value?",
            model_client=fake_client,
        )
    leaked = [m for m in secret_markers for r in caplog.records if m in r.getMessage()]
    assert not leaked, f"document content found in logs: {sorted(set(leaked))}"
