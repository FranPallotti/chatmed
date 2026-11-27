"""PDF parsing. The implementation is chosen once from config, never per request."""

from __future__ import annotations

from chatmed.config import Settings
from chatmed.parsing.base import DocumentParser, ParserError
from chatmed.parsing.docintel import DocIntelParser, make_docintel_client
from chatmed.parsing.pdfium import PdfiumParser
from chatmed.parsing.routing import RoutingParser
from chatmed.parsing.tesseract_ocr import TesseractNotAvailable, TesseractOcrParser

__all__ = [
    "DocIntelParser",
    "DocumentParser",
    "ParserError",
    "PdfiumParser",
    "RoutingParser",
    "TesseractNotAvailable",
    "TesseractOcrParser",
    "build_parser",
]


async def build_parser(settings: Settings) -> DocumentParser:
    """Build the configured parser; call at startup.

    With the local OCR backend this checks the Tesseract binary and language right away,
    so a missing install fails at startup instead of on the first scanned upload (AC2c).
    """
    if settings.parser == "pdfium":
        return PdfiumParser()
    ocr: DocumentParser
    if settings.ocr_backend == "docintel":
        if not settings.docintel_endpoint:  # Settings validation already enforces this
            raise ParserError("DOCINTEL_ENDPOINT is required when OCR_BACKEND=docintel")
        ocr = DocIntelParser(make_docintel_client(settings.docintel_endpoint))
    else:
        tesseract = TesseractOcrParser(
            dpi=settings.ocr_dpi,
            lang=settings.ocr_lang,
            cmd=settings.tesseract_cmd or None,
            max_concurrency=settings.ocr_max_concurrency,
        )
        await tesseract.ensure_available()
        ocr = tesseract
    return RoutingParser(PdfiumParser(), ocr)
