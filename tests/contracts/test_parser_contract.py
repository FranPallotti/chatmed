"""Contract every DocumentParser must pass (M1 AC3, AC4).

Same bytes -> identical ParsedDocument; pages 1-based, contiguous and in order (one per PDF
page); sha256 of the original bytes; a non-empty parser_id.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable

import pytest

from chatmed.parsing import (
    DocIntelParser,
    DocumentParser,
    PdfiumParser,
    RoutingParser,
    TesseractOcrParser,
)
from chatmed.parsing.pdfium import page_count
from tests.conftest import FakeDocIntelClient, find_tesseract, fixture_bytes

Factory = Callable[[], Awaitable[DocumentParser]]


async def _pdfium() -> DocumentParser:
    return PdfiumParser()


async def _tesseract() -> DocumentParser:
    cmd = find_tesseract()
    if cmd is None:
        pytest.skip("Tesseract not installed (set TESSERACT_CMD or add it to PATH)")
    return TesseractOcrParser(dpi=300, lang="eng", cmd=cmd)


async def _routing_tesseract() -> DocumentParser:
    ocr = await _tesseract()
    return RoutingParser(PdfiumParser(), ocr)


async def _docintel() -> DocumentParser:
    return DocIntelParser(FakeDocIntelClient())


async def _routing_docintel() -> DocumentParser:
    return RoutingParser(PdfiumParser(), DocIntelParser(FakeDocIntelClient()))


CASES: list[tuple[str, Factory, str]] = [
    ("pdfium", _pdfium, "lab_report.pdf"),
    ("pdfium", _pdfium, "multi_page_report.pdf"),
    ("tesseract", _tesseract, "scanned_lab_report.pdf"),
    ("routing+tesseract", _routing_tesseract, "lab_report.pdf"),
    ("routing+tesseract", _routing_tesseract, "mixed_report.pdf"),
    ("routing+tesseract", _routing_tesseract, "scanned_lab_report.pdf"),
    ("docintel(fake)", _docintel, "multi_page_report.pdf"),
    ("routing+docintel(fake)", _routing_docintel, "mixed_report.pdf"),
]


@pytest.mark.parametrize(
    ("factory", "fixture"),
    [(f, fx) for _, f, fx in CASES],
    ids=[f"{name}-{fx}" for name, _, fx in CASES],
)
async def test_parser_contract(factory: Factory, fixture: str) -> None:
    pdf = fixture_bytes(fixture)
    parser = await factory()

    first = await parser.parse(pdf)
    second = await parser.parse(pdf)

    assert first == second  # AC3: same bytes -> identical ParsedDocument
    assert [p.number for p in first.pages] == list(range(1, page_count(pdf) + 1))  # AC4
    assert first.sha256 == hashlib.sha256(pdf).hexdigest()  # AC4: original bytes
    assert first.parser_id  # AC4
    assert all(isinstance(p.text, str) for p in first.pages)
