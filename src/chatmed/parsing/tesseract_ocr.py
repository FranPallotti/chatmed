"""OCR for pages without a text layer: pypdfium2 renders, Tesseract reads (D10, D11).

Tesseract runs as a subprocess with the page image on stdin and the text on stdout, so
neither the image nor the text is written to a file by us (`pytesseract` uses temp files; D11).
Its stderr is discarded and never logged or raised, in case it echoes content (hard rule 2).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from chatmed.models import ParsedDocument
from chatmed.parsing.base import ParserError, make_document, normalize_text
from chatmed.parsing.pdfium import page_count, render_page_pgm

PSM = 3  # Tesseract page segmentation mode: fully automatic, no OSD. Part of parser_id.
PAGE_TIMEOUT_S = 120.0
DATA_HASH_LEN = 12  # hex chars of the language-data hash kept in parser_id

_LEPTONICA = re.compile(r"leptonica-(\S+)")
_TESSDATA_DIR = re.compile(r'"([^"]+)"')  # 'List of available languages in "<dir>" (n):'


class TesseractNotAvailable(ParserError):
    """The binary or the configured language is missing: fail loudly, never return ''."""


@dataclass(frozen=True)
class TesseractInstall:
    """What the installed engine contributes to the OCR text, beyond our own settings.

    The same Tesseract version can ship different `.traineddata` models (Windows installer vs
    Debian package), and Leptonica decodes and binarizes the image, so all three are recorded.
    """

    version: str
    leptonica: str
    data_sha256: str  # over the traineddata files of the configured languages, in order


def format_parser_id(install: TesseractInstall, *, dpi: int, lang: str) -> str:
    """Everything that can change the OCR text (AC2b); goes into the reproducibility fingerprint."""
    data = install.data_sha256[:DATA_HASH_LEN]
    return (
        f"tesseract-{install.version}-lept{install.leptonica}-dpi{dpi}-{lang}-psm{PSM}-data{data}"
    )


def language_data_sha256(tessdata_dir: Path, langs: list[str]) -> str:
    """One hash over each language's name and traineddata digest, in the configured order."""
    combined = hashlib.sha256()
    for lang in langs:
        path = tessdata_dir / f"{lang}.traineddata"
        try:
            digest = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            raise TesseractNotAvailable(f"cannot read language data for {lang!r}") from None
        combined.update(lang.encode() + b"\0" + digest)
    return combined.hexdigest()


class TesseractOcrParser:
    """OCRs every page of the PDF it is given (RoutingParser sends it only scanned pages)."""

    def __init__(self, *, dpi: int, lang: str, cmd: str | None = None) -> None:
        self.dpi = dpi
        self.lang = lang
        self.cmd = cmd or shutil.which("tesseract")
        self.install: TesseractInstall | None = None

    @property
    def parser_id(self) -> str:
        if self.install is None:
            raise TesseractNotAvailable("call ensure_available() first")
        return format_parser_id(self.install, dpi=self.dpi, lang=self.lang)

    async def ensure_available(self) -> TesseractInstall:
        """Check binary and languages once and fingerprint them; called at startup."""
        if self.install is not None:
            return self.install
        if not self.cmd:
            raise TesseractNotAvailable("tesseract not found on PATH; set TESSERACT_CMD")
        version_out = (await self._run(["--version"], b"")).decode("utf-8", "replace")
        version = version_out.splitlines()[0].split()[-1].lstrip("v")  # "tesseract v5.4.0"
        leptonica = _LEPTONICA.search(version_out)
        if leptonica is None:
            raise TesseractNotAvailable("cannot read the leptonica version from tesseract")

        list_out = (await self._run(["--list-langs"], b"")).decode("utf-8", "replace")
        header, *langs = list_out.splitlines()
        missing = [lang for lang in self.lang.split("+") if lang not in langs]
        if missing:
            raise TesseractNotAvailable(f"tesseract language data not installed: {missing}")
        tessdata = _TESSDATA_DIR.search(header)
        if tessdata is None:
            raise TesseractNotAvailable("cannot find the tesseract language data directory")
        data_sha = await asyncio.to_thread(
            language_data_sha256, Path(tessdata.group(1)), self.lang.split("+")
        )
        self.install = TesseractInstall(version, leptonica.group(1), data_sha)
        return self.install

    async def parse(self, pdf_bytes: bytes) -> ParsedDocument:
        await self.ensure_available()
        count = await asyncio.to_thread(page_count, pdf_bytes)
        texts = []
        for index in range(count):  # sequential: stable order, bounded memory
            image = await asyncio.to_thread(render_page_pgm, pdf_bytes, index, self.dpi)
            args = ["stdin", "stdout", "-l", self.lang, "--dpi", str(self.dpi), "--psm", str(PSM)]
            texts.append(normalize_text(_decode_ocr(await self._run(args, image))))
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


def _decode_ocr(raw: bytes) -> str:
    """Strict UTF-8, but the decode error never escapes: it carries the OCR bytes (rule 2).

    The ParserError is raised outside the `except` block so it has no `__context__` either.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    raise ParserError("tesseract output is not valid UTF-8")
