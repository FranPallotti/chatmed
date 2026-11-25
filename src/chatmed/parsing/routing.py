"""Per-page routing: text layer when there is one, OCR only for the pages that need it."""

from __future__ import annotations

import asyncio

from chatmed.models import ParsedDocument, ParsedPage
from chatmed.parsing.base import DocumentParser, ParserError, sha256_hex
from chatmed.parsing.pdfium import PdfiumParser, subset_pdf


class RoutingParser:
    """Digital pages via pdfium; image-only pages via the configured OCR parser.

    A page "has a text layer" if pdfium returns any non-whitespace text for it. Only the
    image-only pages are sent to OCR (as an in-memory sub-PDF), which keeps OCR cost down and
    means a digital-only document never depends on the OCR backend.

    `parser_id` lists the OCR backend only when it was used, e.g. `routing[pdfium-5.15.2]` or
    `routing[pdfium-5.15.2+tesseract-5.4.0-lept1.84.1-dpi300-eng-psm3-data<sha12>]`.
    """

    def __init__(self, text: PdfiumParser, ocr: DocumentParser) -> None:
        self.text = text
        self.ocr = ocr

    async def parse(self, pdf_bytes: bytes) -> ParsedDocument:
        digital = await self.text.parse(pdf_bytes)
        scanned = [p.number for p in digital.pages if not p.text.strip()]
        if not scanned:
            return ParsedDocument(digital.sha256, digital.pages, f"routing[{digital.parser_id}]")

        sub_pdf = await asyncio.to_thread(subset_pdf, pdf_bytes, scanned)
        ocr_doc = await self.ocr.parse(sub_pdf)
        if len(ocr_doc.pages) != len(scanned):
            raise ParserError("OCR backend returned a different number of pages")
        ocr_text = {number: page.text for number, page in zip(scanned, ocr_doc.pages, strict=True)}

        pages = tuple(ParsedPage(p.number, ocr_text.get(p.number, p.text)) for p in digital.pages)
        parser_id = f"routing[{digital.parser_id}+{ocr_doc.parser_id}]"
        return ParsedDocument(sha256=sha256_hex(pdf_bytes), pages=pages, parser_id=parser_id)
