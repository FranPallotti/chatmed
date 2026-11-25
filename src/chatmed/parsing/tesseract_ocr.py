"""OCR for pages without a text layer: pypdfium2 renders, Tesseract reads (D10, D11).

Tesseract runs as a subprocess with the page image on stdin and the text on stdout, so
neither the image nor the text ever touches disk (`pytesseract` uses temp files; D11).
Its stderr is discarded and never logged or raised, in case it echoes content (hard rule 2).
"""

from __future__ import annotations

import asyncio
import shutil

from chatmed.models import ParsedDocument
from chatmed.parsing.base import ParserError, make_document, normalize_text
from chatmed.parsing.pdfium import page_count, render_page_pgm

PSM = 3  # Tesseract page segmentation mode: fully automatic, no OSD. Part of parser_id.
PAGE_TIMEOUT_S = 120.0


class TesseractNotAvailable(ParserError):
    """The binary or the configured language is missing: fail loudly, never return ''."""


class TesseractOcrParser:
    """OCRs every page of the PDF it is given (RoutingParser sends it only scanned pages)."""

    def __init__(self, *, dpi: int, lang: str, cmd: str | None = None) -> None:
        self.dpi = dpi
        self.lang = lang
        self.cmd = cmd or shutil.which("tesseract")
        self._version: str | None = None

    @property
    def parser_id(self) -> str:
        """Version, DPI, language and PSM: anything that can change the OCR text (AC2b)."""
        if self._version is None:
            raise TesseractNotAvailable("call ensure_available() first")
        return f"tesseract-{self._version}-dpi{self.dpi}-{self.lang}-psm{PSM}"

    async def ensure_available(self) -> str:
        """Check the binary and language once; called at startup and before first use."""
        if self._version is not None:
            return self._version
        if not self.cmd:
            raise TesseractNotAvailable("tesseract not found on PATH; set TESSERACT_CMD")
        version_out = await self._run(["--version"], b"")
        first_line = version_out.decode("utf-8", "replace").splitlines()[0]  # "tesseract v5.4.0"
        version = first_line.split()[-1].lstrip("v")
        langs = (await self._run(["--list-langs"], b"")).decode("utf-8", "replace").split()
        missing = [lang for lang in self.lang.split("+") if lang not in langs]
        if missing:
            raise TesseractNotAvailable(f"tesseract language data not installed: {missing}")
        self._version = version
        return version

    async def parse(self, pdf_bytes: bytes) -> ParsedDocument:
        await self.ensure_available()
        count = await asyncio.to_thread(page_count, pdf_bytes)
        texts = []
        for index in range(count):  # sequential: stable order, bounded memory
            image = await asyncio.to_thread(render_page_pgm, pdf_bytes, index, self.dpi)
            args = ["stdin", "stdout", "-l", self.lang, "--dpi", str(self.dpi), "--psm", str(PSM)]
            out = await self._run(args, image)
            texts.append(normalize_text(out.decode("utf-8")))
        return make_document(pdf_bytes, texts, self.parser_id)

    async def _run(self, args: list[str], stdin: bytes) -> bytes:
        if self.cmd is None:
            raise TesseractNotAvailable("tesseract not found on PATH; set TESSERACT_CMD")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cmd,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise TesseractNotAvailable(f"cannot start tesseract ({type(exc).__name__})") from None
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(stdin), PAGE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ParserError("tesseract timed out") from None
        if proc.returncode != 0:
            raise ParserError(f"tesseract failed with exit code {proc.returncode}")
        return stdout
