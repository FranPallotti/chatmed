"""M1 parsing tests: fixtures (AC1), parsers and routing (AC2), parser_id (AC2b),
missing Tesseract (AC2c) and the Document Intelligence backend with a fake client (AC5)."""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

import pytest

from chatmed.config import Settings
from chatmed.models import ParsedDocument
from chatmed.parsing import (
    DocIntelParser,
    ParserError,
    PdfiumParser,
    RoutingParser,
    TesseractNotAvailable,
    TesseractOcrParser,
    build_parser,
)
from chatmed.parsing.base import make_document
from chatmed.parsing.pdfium import page_count, page_texts, render_page_pgm, subset_pdf
from chatmed.parsing.tesseract_ocr import (
    TesseractInstall,
    _decode_ocr,
    format_parser_id,
    language_data_sha256,
)
from tests.conftest import FIXTURES, ROOT, FakeDocIntelClient, fixture_bytes

sys.path.insert(0, str(ROOT / "scripts"))
from make_fixtures import BANNER, build_fixtures  # noqa: E402

MARKERS = ("Hemoglobin", "13.4", "00123")


# ---------------------------------------------------------------- AC1: fixtures


def test_fixture_generation_is_reproducible_and_committed() -> None:
    built = build_fixtures()
    assert built == build_fixtures()
    for name, data in built.items():
        assert (FIXTURES / name).read_bytes() == data, f"regenerate: make_fixtures.py ({name})"


def test_fixture_shapes() -> None:
    texts = {name: page_texts(data) for name, data in build_fixtures().items()}
    assert len(texts["lab_report.pdf"]) == 1
    assert all(m in texts["lab_report.pdf"][0] for m in MARKERS)
    assert all(BANNER in t for t in texts["lab_report.pdf"] + texts["multi_page_report.pdf"])

    multi = texts["multi_page_report.pdf"]
    assert len(multi) == 3
    assert "13.4" in multi[0] and "12.9" in multi[2]  # conflicting repeated value
    assert "92" in multi[0] and "92" in multi[2]  # same repeated value

    assert texts["scanned_lab_report.pdf"] == [""]  # image only: no text layer
    assert texts["mixed_report.pdf"][0] and texts["mixed_report.pdf"][1] == ""


# ---------------------------------------------------------------- pdfium helpers


async def test_pdfium_reads_text_layer() -> None:
    doc = await PdfiumParser().parse(fixture_bytes("lab_report.pdf"))
    assert all(m in doc.pages[0].text for m in MARKERS)
    assert "\r" not in doc.pages[0].text


def test_render_page_pgm_is_grayscale_at_requested_dpi() -> None:
    image = render_page_pgm(fixture_bytes("lab_report.pdf"), 0, dpi=100)
    magic, size, maxval, pixels = image.split(b"\n", 3)
    width, height = (int(v) for v in size.split())
    assert (magic, maxval) == (b"P5", b"255")  # 8-bit grayscale
    assert abs(width - 595.28 * 100 / 72) <= 1  # A4 width (595.28 pt) at 100 DPI
    assert len(pixels) == width * height  # one byte per pixel, row padding removed


def test_subset_pdf_keeps_only_requested_pages_in_order() -> None:
    multi = fixture_bytes("multi_page_report.pdf")
    sub = subset_pdf(multi, [3, 1])
    texts = page_texts(sub)
    assert page_count(sub) == 2
    assert "page 3 of 3" in texts[0] and "page 1 of 3" in texts[1]


def test_unreadable_pdf_raises_parser_error_without_content() -> None:
    with pytest.raises(ParserError) as info:
        page_texts(b"%PDF-1.7 Hemoglobin 13.4 not really a pdf")
    assert "13.4" not in str(info.value)


# ---------------------------------------------------------------- AC2: routing


class RecordingOcr:
    """Fake OCR parser: remembers what it was sent, returns one fixed text per page."""

    def __init__(self, pages_to_return: int | None = None) -> None:
        self.received: list[bytes] = []
        self.pages_to_return = pages_to_return

    async def parse(self, pdf_bytes: bytes) -> ParsedDocument:
        self.received.append(pdf_bytes)
        n = self.pages_to_return or page_count(pdf_bytes)
        return make_document(pdf_bytes, [f"ocr page {i}" for i in range(1, n + 1)], "fake-ocr-1")


async def test_routing_sends_only_scanned_pages_to_ocr() -> None:
    ocr = RecordingOcr()
    doc = await RoutingParser(PdfiumParser(), ocr).parse(fixture_bytes("mixed_report.pdf"))
    assert len(ocr.received) == 1 and page_count(ocr.received[0]) == 1
    assert "MRN: 00123" in doc.pages[0].text  # page 1 from the text layer
    assert doc.pages[1].text == "ocr page 1"  # page 2 from OCR
    assert doc.parser_id == f"routing[{PdfiumParser.parser_id}+fake-ocr-1]"


async def test_routing_digital_document_never_calls_ocr() -> None:
    ocr = RecordingOcr()
    doc = await RoutingParser(PdfiumParser(), ocr).parse(fixture_bytes("multi_page_report.pdf"))
    assert ocr.received == []
    assert doc.parser_id == f"routing[{PdfiumParser.parser_id}]"


async def test_routing_rejects_ocr_page_count_mismatch() -> None:
    parser = RoutingParser(PdfiumParser(), RecordingOcr(pages_to_return=2))
    with pytest.raises(ParserError):
        await parser.parse(fixture_bytes("mixed_report.pdf"))


async def test_tesseract_reads_scanned_page(tesseract_cmd: str) -> None:
    parser = TesseractOcrParser(dpi=300, lang="eng", cmd=tesseract_cmd)
    doc = await parser.parse(fixture_bytes("scanned_lab_report.pdf"))
    assert all(m in doc.pages[0].text for m in MARKERS)


# ---------------------------------------------------------------- AC2b: parser_id


async def test_tesseract_parser_id_has_engine_and_settings(tesseract_cmd: str) -> None:
    parser = TesseractOcrParser(dpi=300, lang="eng", cmd=tesseract_cmd)
    install = await parser.ensure_available()
    assert install.version[0].isdigit() and install.leptonica[0].isdigit()
    assert len(install.data_sha256) == 64
    assert parser.parser_id == (
        f"tesseract-{install.version}-lept{install.leptonica}"
        f"-dpi300-eng-psm3-data{install.data_sha256[:12]}"
    )


INSTALL = TesseractInstall(version="5.4.0", leptonica="1.84.1", data_sha256="a" * 64)


def test_parser_id_changes_with_every_input() -> None:
    variants = [
        (INSTALL, 300, "eng"),
        (INSTALL, 200, "eng"),
        (INSTALL, 300, "eng+spa"),
        (TesseractInstall("5.3.0", "1.84.1", "a" * 64), 300, "eng"),
        (TesseractInstall("5.4.0", "1.82.0", "a" * 64), 300, "eng"),
        (TesseractInstall("5.4.0", "1.84.1", "b" * 64), 300, "eng"),  # other traineddata
    ]
    ids = {format_parser_id(install, dpi=dpi, lang=lang) for install, dpi, lang in variants}
    assert len(ids) == len(variants)


def test_language_data_hash_tracks_file_contents_and_order(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for folder, eng in [(a, b"model-1"), (b, b"model-2")]:
        folder.mkdir()
        (folder / "eng.traineddata").write_bytes(eng)
        (folder / "spa.traineddata").write_bytes(b"spanish")
    assert language_data_sha256(a, ["eng"]) == language_data_sha256(a, ["eng"])
    assert language_data_sha256(a, ["eng"]) != language_data_sha256(b, ["eng"])
    assert language_data_sha256(a, ["eng", "spa"]) != language_data_sha256(a, ["spa", "eng"])
    with pytest.raises(TesseractNotAvailable):
        language_data_sha256(a, ["fra"])


def test_ocr_decode_error_does_not_carry_text() -> None:
    raw = b"Hemoglobin 13.4 \xff"
    with pytest.raises(ParserError) as info:
        _decode_ocr(raw)
    error = info.value
    assert "13.4" not in str(error)
    assert error.__cause__ is None and error.__context__ is None  # no UnicodeDecodeError.object


def test_pdfium_parser_id_has_library_version() -> None:
    from pypdfium2.version import PYPDFIUM_INFO

    assert PdfiumParser.parser_id == f"pdfium-{PYPDFIUM_INFO}"


# ---------------------------------------------------------------- AC2c: missing Tesseract


async def test_missing_binary_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    parser = TesseractOcrParser(dpi=300, lang="eng")
    with pytest.raises(TesseractNotAvailable, match="TESSERACT_CMD"):
        await parser.parse(fixture_bytes("scanned_lab_report.pdf"))


async def test_wrong_binary_path_fails_loudly() -> None:
    parser = TesseractOcrParser(dpi=300, lang="eng", cmd=str(ROOT / "no" / "tesseract.exe"))
    with pytest.raises(TesseractNotAvailable):
        await parser.parse(fixture_bytes("scanned_lab_report.pdf"))


async def test_routing_with_missing_tesseract_never_returns_empty_text() -> None:
    parser = RoutingParser(
        PdfiumParser(), TesseractOcrParser(dpi=300, lang="eng", cmd=str(ROOT / "nope.exe"))
    )
    with pytest.raises(TesseractNotAvailable):
        await parser.parse(fixture_bytes("mixed_report.pdf"))


async def test_missing_language_fails_loudly(tesseract_cmd: str) -> None:
    parser = TesseractOcrParser(dpi=300, lang="zzz", cmd=tesseract_cmd)
    with pytest.raises(TesseractNotAvailable, match="zzz"):
        await parser.ensure_available()


# ---------------------------------------------------------------- build_parser (config)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "azure_openai_endpoint": "https://example.openai.azure.com/",
        "azure_openai_deployment": "pinned",
        "tenant_db_dsn_template": "postgresql://r_{tenant}@localhost/tenant_{tenant}",
    }
    return Settings(_env_file=None, **(base | overrides))  # type: ignore[arg-type]


async def test_build_parser_pdfium() -> None:
    assert isinstance(await build_parser(_settings(parser="pdfium")), PdfiumParser)


async def test_build_parser_routing_with_tesseract(tesseract_cmd: str) -> None:
    parser = await build_parser(_settings(tesseract_cmd=tesseract_cmd))
    assert isinstance(parser, RoutingParser) and isinstance(parser.ocr, TesseractOcrParser)


async def test_build_parser_fails_at_startup_without_tesseract() -> None:
    with pytest.raises(TesseractNotAvailable):
        await build_parser(_settings(tesseract_cmd=str(ROOT / "nope.exe")))


async def test_build_parser_routing_with_docintel() -> None:
    parser = await build_parser(
        _settings(
            ocr_backend="docintel", docintel_endpoint="https://x.cognitiveservices.azure.com/"
        )
    )
    assert isinstance(parser, RoutingParser) and isinstance(parser.ocr, DocIntelParser)


# ---------------------------------------------------------------- AC5: Document Intelligence


async def test_docintel_sends_bytes_and_deletes_result_after_reading() -> None:
    client = FakeDocIntelClient()
    pdf = fixture_bytes("multi_page_report.pdf")
    doc = await DocIntelParser(client).parse(pdf)

    kinds = [kind for kind, _ in client.calls]
    assert kinds == ["analyze", "result", "delete"]
    analyze = client.calls[0][1]
    assert analyze["body"] == pdf  # the bytes themselves: no blob upload, no URL
    assert analyze["model_id"] == "prebuilt-read"
    assert client.calls[2][1] == "op-1"  # the result that was read is the one deleted

    assert [p.number for p in doc.pages] == [1, 2, 3]  # fake returns pages reversed
    assert "page 1 of 3" in doc.pages[0].text
    assert doc.parser_id == "docintel-prebuilt-read-2024-11-30"
    assert doc.sha256 == hashlib.sha256(pdf).hexdigest()


async def test_docintel_deletes_result_even_when_reading_fails() -> None:
    client = FakeDocIntelClient(fail_on_result=True)
    with pytest.raises(RuntimeError):
        await DocIntelParser(client).parse(fixture_bytes("lab_report.pdf"))
    assert client.calls[-1] == ("delete", "op-1")


async def test_docintel_failed_delete_keeps_original_error_and_logs_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeDocIntelClient(fail_on_result=True, fail_on_delete=True)
    pdf = fixture_bytes("lab_report.pdf")
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="service error"):
        await DocIntelParser(client).parse(pdf)
    (record,) = [r for r in caplog.records if r.msg == "docintel_delete_failed"]
    assert record.doc_sha256 == hashlib.sha256(pdf).hexdigest()  # type: ignore[attr-defined]
    assert record.status == "result_may_persist"  # type: ignore[attr-defined]


async def test_docintel_failed_delete_after_success_fails_the_parse(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeDocIntelClient(fail_on_delete=True)
    with caplog.at_level(logging.WARNING), pytest.raises(ParserError, match="could not be deleted"):
        await DocIntelParser(client).parse(fixture_bytes("lab_report.pdf"))
    assert [r.msg for r in caplog.records] == ["docintel_delete_failed"]


def test_docintel_rejects_missing_pages() -> None:
    from types import SimpleNamespace

    result = SimpleNamespace(
        pages=[SimpleNamespace(page_number=1, lines=[]), SimpleNamespace(page_number=3, lines=[])]
    )
    with pytest.raises(ParserError):
        DocIntelParser._page_texts(result)
