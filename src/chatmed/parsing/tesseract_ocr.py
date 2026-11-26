"""OCR for pages without a text layer: pypdfium2 renders, Tesseract reads (D10, D11).

Tesseract runs as a subprocess with the page image on stdin and the text on stdout, so
neither the image nor the text is written to a file by us (`pytesseract` uses temp files; D11).
Its stderr is discarded and never logged or raised, in case it echoes content (hard rule 2).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
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

_TESSERACT_VERSION = re.compile(r"^tesseract v?(\d\S*)", re.MULTILINE)
_LEPTONICA = re.compile(r"leptonica-(\S+)")
_TESSDATA_DIR = re.compile(r'"([^"]+)"')  # 'List of available languages in "<dir>" (n):'
# Package locations on Debian/Ubuntu (the M5 image), newest first. Tesseract 4 does not print
# its data directory, so these are needed there.
SYSTEM_TESSDATA_DIRS = (
    Path("/usr/share/tesseract-ocr/5/tessdata"),
    Path("/usr/share/tesseract-ocr/4.00/tessdata"),
    Path("/usr/share/tessdata"),
)


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


def find_tessdata_dir(langs: list[str], *, listed: str | None, prefix: str | None) -> Path:
    """Directory holding every `<lang>.traineddata`; first match wins, in this order:

    1. `TESSDATA_PREFIX` (Tesseract reads it too): the directory itself or its `tessdata/`;
    2. the directory `tesseract --list-langs` printed (Tesseract 5 does, 4 does not);
    3. the standard Debian/Ubuntu package directories.

    Fails loudly instead of fingerprinting the wrong models (AC2b) or none.
    """
    candidates: list[Path] = []
    if prefix:
        candidates += [Path(prefix), Path(prefix) / "tessdata"]
    if listed:
        candidates.append(Path(listed))
    candidates += SYSTEM_TESSDATA_DIRS
    for folder in candidates:
        if all((folder / f"{lang}.traineddata").is_file() for lang in langs):
            return folder
    raise TesseractNotAvailable("cannot find the tesseract language data directory")


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
        # stderr is merged for these two calls only: some 4.x builds print there, and they
        # carry no document content.
        version_out = (await self._run(["--version"], b"", merge_stderr=True)).decode(
            "utf-8", "replace"
        )
        version, leptonica = parse_version_output(version_out)
        list_out = (await self._run(["--list-langs"], b"", merge_stderr=True)).decode(
            "utf-8", "replace"
        )
        listed_dir, installed = parse_list_langs(list_out)
        wanted = self.lang.split("+")
        missing = [lang for lang in wanted if lang not in installed]
        if missing:
            raise TesseractNotAvailable(f"tesseract language data not installed: {missing}")
        tessdata = find_tessdata_dir(
            wanted, listed=listed_dir, prefix=os.environ.get("TESSDATA_PREFIX")
        )
        data_sha = await asyncio.to_thread(language_data_sha256, tessdata, wanted)
        self.install = TesseractInstall(version, leptonica, data_sha)
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

    async def _run(self, args: list[str], stdin: bytes, *, merge_stderr: bool = False) -> bytes:
        if self.cmd is None:
            raise TesseractNotAvailable("tesseract not found on PATH; set TESSERACT_CMD")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cmd,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.DEVNULL,
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


def parse_version_output(output: str) -> tuple[str, str]:
    """(tesseract version, leptonica version) from `tesseract --version`.

    Tesseract 5 prints 'tesseract v5.4.0...', 4.x 'tesseract 4.1.1'; warnings may come first.
    """
    version = _TESSERACT_VERSION.search(output)
    leptonica = _LEPTONICA.search(output)
    if version is None or leptonica is None:
        raise TesseractNotAvailable("cannot read the tesseract/leptonica version")
    return version.group(1), leptonica.group(1)


def parse_list_langs(output: str) -> tuple[str | None, list[str]]:
    """(data directory if printed, installed languages) from `tesseract --list-langs`.

    Tesseract 5: 'List of available languages in "<dir>" (n):'; 4.x: 'List of available
    languages (n):'. Lines before the header (warnings) are ignored.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    header = next((i for i, line in enumerate(lines) if line.startswith("List of")), None)
    if header is None:
        raise TesseractNotAvailable("unexpected output from tesseract --list-langs")
    listed = _TESSDATA_DIR.search(lines[header])
    return (listed.group(1) if listed else None), lines[header + 1 :]


def _decode_ocr(raw: bytes) -> str:
    """Strict UTF-8, but the decode error never escapes: it carries the OCR bytes (rule 2).

    The ParserError is raised outside the `except` block so it has no `__context__` either.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    raise ParserError("tesseract output is not valid UTF-8")
