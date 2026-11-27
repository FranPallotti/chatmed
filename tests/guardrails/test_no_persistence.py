"""Guardrails for hard rules 1 and 2, enabled in M4 (AC5).

A whole session runs through `chatmed.pipeline.run_full_session` (create → upload → ask →
close), the code the API serves, with the synthetic CIOMS form for acme subject 01001-004
(D12, fixtures/README.md), the recorded extraction outputs and a scripted agent (no network).
The questions are the three demo questions plus one that takes the less common paths (a
refused tool call, a rejected answer and its retry).

Rule 1, no files: the run happens in an empty working dir, with `TMP`/`TEMP`/`TMPDIR`
pointing to an empty dir (the process's system temp dir for Python and for native libraries
that honour those variables); both are watched. On top, a Python audit hook records every
file opened for writing, directory created or file renamed anywhere in the process while
the run is armed. The real system temp dir is not snapshotted: other processes write there
all the time, so a snapshot of it would make the test flaky without testing our code.

Rule 2, no content in logs: every log record of the run (DEBUG and up, all loggers) is
checked twice. First as the production handler prints it (`make_handler`: filter + JSON).
Then raw, before any filter (message, args and every `extra` attribute), so the code is
shown not to even hand content to the logger.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chatmed.logconfig import make_handler
from chatmed.pipeline import SessionRun, run_full_session
from tests.conftest import FIXTURES
from tests.fakes import FakeModelClient
from tests.pipeline_support import DEMO, FIXTURE, RETRY_QUESTION, SHA, make_services, retry_script

DOC_TYPE = "cioms"
QUESTIONS = [*DEMO, RETRY_QUESTION]
# Values printed in the fixture PDF (fixtures/README.md); none may appear in any log.
SECRET_MARKERS = [
    "01001-004",
    "Zentrobal",
    "ACM-2026-0042",
    "2026-06-12",
    "2026-06-13",
    "Argentina",
    "acute liver injury",
    "ALT 8x ULN",
    "hospitalised",
    "rheumatoid arthritis",
    "Metformin",
    "Atorvastatin",
]
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

# --------------------------------------------------------------------- audit hook
# Audit hooks cannot be removed, so one hook is installed for the whole test process and
# only records while `_WRITES` is a list (armed).
_WRITES: list[str] | None = None


def _is_write(event: str, args: tuple[Any, ...]) -> str | None:
    if event == "open":
        path, mode, flags = args
        if isinstance(path, int) or path is None:
            return None  # an already-open file descriptor
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
            isinstance(flags, int) and flags & WRITE_FLAGS
        )
        return str(path) if writes else None
    if event in ("os.mkdir", "os.rename", "os.replace", "os.link", "os.symlink"):
        return str(args[0])
    return None


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _WRITES is None:
        return
    path = _is_write(event, args)
    # Bytecode caches are written by the import system, never with document data.
    if path is not None and not path.endswith((".pyc", "__pycache__")):
        _WRITES.append(f"{event}:{path}")


sys.addaudithook(_audit)


@pytest.fixture(scope="module")
def recorded() -> FakeModelClient:
    return FakeModelClient.from_recordings()


def _steps() -> list[Any]:
    """The agent script for QUESTIONS, in order."""
    by_question = {**DEMO, RETRY_QUESTION: retry_script}
    return [step for q in QUESTIONS for step in by_question[q](SHA)]


async def _run(recorded: FakeModelClient) -> SessionRun:
    services = make_services(recorded, _steps())
    run = await run_full_session(services, "acme", [FIXTURES / FIXTURE], DOC_TYPE, QUESTIONS)
    assert [a.outcome for a in run.answers] == ["verified"] * len(QUESTIONS)
    assert run.documents[0]["sha256"] == SHA
    return run


def _snapshot(dirs: list[Path]) -> set[Path]:
    return {p for d in dirs for p in d.rglob("*")}


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Path]]:
    """An empty working dir and an empty system temp dir for the run."""
    work, systemp = tmp_path / "work", tmp_path / "systemp"
    work.mkdir()
    systemp.mkdir()
    monkeypatch.chdir(work)
    for var in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(var, str(systemp))
    monkeypatch.setattr(tempfile, "tempdir", None)  # recomputed from the variables above
    assert Path(tempfile.gettempdir()) == systemp
    yield [work, systemp]


# --------------------------------------------------------------------- rule 1


async def test_full_run_writes_no_files(
    recorded: FakeModelClient, isolated_dirs: list[Path]
) -> None:
    global _WRITES
    await _run(recorded)  # warm-up: lazy imports and caches happen outside the armed window
    before = _snapshot(isolated_dirs)
    _WRITES = []
    try:
        await _run(recorded)
        writes = list(_WRITES)
    finally:
        _WRITES = None
    created = _snapshot(isolated_dirs) - before
    assert not created, f"files written during a session: {sorted(created)[:5]}"
    assert not writes, f"file writes during a session: {writes[:5]}"


def test_audit_hook_sees_a_write(tmp_path: Path) -> None:
    """Control: the hook does catch an ordinary write, so the empty list above means something."""
    global _WRITES
    _WRITES = []
    try:
        (tmp_path / "probe.txt").write_text("x")
        os.mkdir(tmp_path / "probe-dir")
        writes = list(_WRITES)
    finally:
        _WRITES = None
    assert any("probe.txt" in w for w in writes) and any("probe-dir" in w for w in writes)


# --------------------------------------------------------------------- rule 2


class _Raw(logging.Handler):
    """Copies every record's texts as it arrives: the production handler's filter mutates the
    same record object afterwards, so keeping the object would check a cleaned record."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.events: list[str] = []
        self.texts: list[str] = []

    def handle(self, record: logging.LogRecord) -> bool:  # before filters, unlike emit
        self.events.append(record.getMessage())
        self.texts.extend(_raw_texts(record))
        return True

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - handle() is used
        pass


_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


def _raw_texts(record: logging.LogRecord) -> list[str]:
    """Message, args and every `extra` attribute, as text (exception info is checked on output:
    the filter keeps only its type and code location)."""
    texts = [str(record.msg), repr(record.args)]
    texts += [repr(v) for k, v in record.__dict__.items() if k not in _STANDARD]
    return texts


async def test_logs_contain_no_document_content(recorded: FakeModelClient) -> None:
    root = logging.getLogger()
    raw = _Raw()
    output = io.StringIO()
    printed = make_handler(output)  # the production handler: filter + JSON formatter
    saved_level = root.level
    root.addHandler(raw)  # first: sees each record before the filter changes it
    root.addHandler(printed)
    root.setLevel(logging.DEBUG)
    try:
        run = await _run(recorded)
    finally:
        root.removeHandler(raw)
        root.removeHandler(printed)
        root.setLevel(saved_level)

    text = output.getvalue()
    events = set(raw.events)
    # Sensitivity: the run really was logged, end to end.
    for expected in ("session.created", "pipeline.document_added", "agent.answer_rejected"):
        assert expected in events
    assert text.count('"event": "session.closed"') == 1 and '"reason": "client"' in text

    markers = SECRET_MARKERS + QUESTIONS + [a.text for a in run.answers]
    assert [m for m in markers if m in text] == [], "document content in the log output"
    leaked = sorted({m for t in raw.texts for m in markers if m in t})
    assert leaked == [], "document content handed to the logger"
