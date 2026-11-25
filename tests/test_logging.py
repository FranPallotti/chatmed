"""M0 AC3: the central log handler emits only allow-listed metadata, never content."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from chatmed.logconfig import MAX_STR_LEN, make_handler

SECRET = "Hemoglobin 13.4 g/dL for MRN 00123"  # synthetic stand-in for document content


@pytest.fixture
def capture() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    """A logger tree whose parent has the real handler, isolated from the root logger."""
    stream = io.StringIO()
    parent = logging.getLogger("chatmed_test_parent")
    parent.handlers = [make_handler(stream)]
    parent.setLevel(logging.DEBUG)
    parent.propagate = False
    yield parent, stream
    parent.handlers = []


def lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_arbitrary_extra_payload_is_dropped(capture: tuple[logging.Logger, io.StringIO]) -> None:
    log, stream = capture
    log.info(
        "doc_parsed",
        extra={"text": SECRET, "quote": SECRET, "session_id": "s-1", "page_count": 3},
    )
    out = stream.getvalue()
    assert "13.4" not in out and "Hemoglobin" not in out
    [record] = lines(stream)
    assert record["event"] == "doc_parsed"
    assert record["session_id"] == "s-1" and record["page_count"] == 3
    assert "text" not in record and "quote" not in record


def test_format_args_are_never_interpolated(capture: tuple[logging.Logger, io.StringIO]) -> None:
    log, stream = capture
    log.warning("value was %s", SECRET)
    assert "13.4" not in stream.getvalue()
    assert lines(stream)[0]["event"] == "value was %s"


def test_exception_message_is_not_logged(capture: tuple[logging.Logger, io.StringIO]) -> None:
    log, stream = capture
    try:
        raise ValueError(SECRET)
    except ValueError:
        log.exception("extraction_failed", extra={"session_id": "s-1"})
    assert "13.4" not in stream.getvalue()
    [record] = lines(stream)
    assert record["error_type"] == "ValueError"
    assert "test_logging.py" in str(record["error_where"])


def test_allowed_key_with_unsafe_value_is_dropped(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    log, stream = capture
    log.info(
        "x",
        extra={"status": {"text": SECRET}, "tenant_id": "a" * (MAX_STR_LEN + 1), "attempt": 2},
    )
    [record] = lines(stream)
    assert "status" not in record and "tenant_id" not in record
    assert record["attempt"] == 2


def test_records_from_child_loggers_are_filtered(
    capture: tuple[logging.Logger, io.StringIO],
) -> None:
    _, stream = capture
    child = logging.getLogger("chatmed_test_parent.extraction")
    child.info("window_done", extra={"page_text": SECRET, "duration_ms": 12.5})
    assert "13.4" not in stream.getvalue()
    assert lines(stream)[0]["duration_ms"] == 12.5
