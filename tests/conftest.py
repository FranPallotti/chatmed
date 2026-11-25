"""Shared test helpers: fixture PDFs, the Tesseract binary (optional) and a fake DocIntel client."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chatmed.parsing.pdfium import page_texts

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
WINDOWS_DEFAULT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


def find_tesseract() -> str | None:
    """TESSERACT_CMD, then PATH, then the default Windows install location."""
    candidates = [os.environ.get("TESSERACT_CMD"), shutil.which("tesseract")]
    if WINDOWS_DEFAULT.exists():
        candidates.append(str(WINDOWS_DEFAULT))
    return next((c for c in candidates if c), None)


@pytest.fixture(scope="session")
def tesseract_cmd() -> str:
    cmd = find_tesseract()
    if cmd is None:
        pytest.skip("Tesseract not installed (set TESSERACT_CMD or add it to PATH)")
    return cmd


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeDocIntelClient:
    """Records calls; 'OCRs' by reading the PDF text layer so results are deterministic.

    For image-only pages (no text layer) it returns `scanned_text` for each page.
    """

    def __init__(self, *, fail_on_result: bool = False, scanned_text: str = "OCR TEXT") -> None:
        self.fail_on_result = fail_on_result
        self.scanned_text = scanned_text
        self.calls: list[tuple[str, Any]] = []
        self._counter = 0

    async def begin_analyze_document(self, model_id: str, body: Any, **kwargs: Any) -> Any:
        data = body.getvalue()
        self.calls.append(("analyze", {"model_id": model_id, "body": data, **kwargs}))
        self._counter += 1
        operation_id = f"op-{self._counter}"
        texts = [t or self.scanned_text for t in page_texts(data)]
        fail = self.fail_on_result
        calls = self.calls

        async def result() -> Any:
            calls.append(("result", operation_id))
            if fail:
                raise RuntimeError("service error")
            pages = [
                SimpleNamespace(
                    page_number=i, lines=[SimpleNamespace(content=line) for line in t.split("\n")]
                )
                for i, t in enumerate(texts, start=1)
            ]
            return SimpleNamespace(pages=list(reversed(pages)), api_version="2024-11-30")

        return SimpleNamespace(details={"operation_id": operation_id}, result=result)

    async def delete_analyze_result(self, model_id: str, result_id: str, **kwargs: Any) -> None:
        self.calls.append(("delete", result_id))
