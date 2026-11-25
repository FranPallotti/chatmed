"""Text-layer extraction and page rendering with pypdfium2 (D10).

pypdfium2 is not thread-safe, and parsing runs in worker threads (so CPU work does not block
the event loop). Every call into pdfium therefore holds `PDFIUM_LOCK`.
Everything here works on bytes in memory; nothing is written to disk (hard rule 1).
"""

from __future__ import annotations

import asyncio
import io
import threading

import pypdfium2 as pdfium
from pypdfium2.version import PYPDFIUM_INFO

from chatmed.models import ParsedDocument
from chatmed.parsing.base import ParserError, make_document, normalize_text

PDFIUM_LOCK = threading.Lock()
PDFIUM_ID = f"pdfium-{PYPDFIUM_INFO}"


def _open(pdf_bytes: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(pdf_bytes)
    except pdfium.PdfiumError as exc:  # message is pdfium's error code, not content
        raise ParserError(f"not a readable PDF ({type(exc).__name__})") from None


def page_texts(pdf_bytes: bytes) -> list[str]:
    """Text layer of every page, in page order (empty string for image-only pages)."""
    with PDFIUM_LOCK:
        doc = _open(pdf_bytes)
        try:
            return [normalize_text(doc[i].get_textpage().get_text_range()) for i in range(len(doc))]
        finally:
            doc.close()


def page_count(pdf_bytes: bytes) -> int:
    with PDFIUM_LOCK:
        doc = _open(pdf_bytes)
        try:
            return len(doc)
        finally:
            doc.close()


def render_page_pgm(pdf_bytes: bytes, index: int, dpi: int) -> bytes:
    """Render one page (0-based) to an 8-bit grayscale PGM image held in memory.

    PGM is a 3-line header plus raw pixel rows, so no imaging library is needed (D11).
    pdfium may pad each row (`stride` > `width`); the padding is dropped.
    """
    with PDFIUM_LOCK:
        doc = _open(pdf_bytes)
        try:
            bitmap = doc[index].render(scale=dpi / 72, grayscale=True)
            width, height, stride = bitmap.width, bitmap.height, bitmap.stride
            buffer = bytes(bitmap.buffer)
        finally:
            doc.close()
    rows = b"".join(buffer[r * stride : r * stride + width] for r in range(height))
    return b"P5\n%d %d\n255\n" % (width, height) + rows


def subset_pdf(pdf_bytes: bytes, page_numbers: list[int]) -> bytes:
    """A new PDF (bytes) with only the given 1-based pages, in the given order."""
    with PDFIUM_LOCK:
        src = _open(pdf_bytes)
        out = pdfium.PdfDocument.new()
        try:
            out.import_pages(src, pages=[n - 1 for n in page_numbers])
            buffer = io.BytesIO()
            out.save(buffer)
            return buffer.getvalue()
        finally:
            out.close()
            src.close()


class PdfiumParser:
    """Digital PDFs: reads the embedded text layer. Local and deterministic."""

    parser_id = PDFIUM_ID

    async def parse(self, pdf_bytes: bytes) -> ParsedDocument:
        texts = await asyncio.to_thread(page_texts, pdf_bytes)
        return make_document(pdf_bytes, texts, self.parser_id)
