"""Optional OCR backend: Azure Document Intelligence (D1; off by default, open question 1).

Retention is minimized: the PDF bytes go in the request body (no blob upload, no URL), and the
analyze result is deleted from the service right after it is read, even if reading fails.
If the delete itself fails, the service keeps the result until its own expiry (24 h), so that is
logged as metadata (`docintel_delete_failed`) and never hides the original error.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, Protocol

from chatmed.models import ParsedDocument
from chatmed.parsing.base import ParserError, make_document, normalize_text, sha256_hex

if TYPE_CHECKING:
    from azure.ai.documentintelligence.aio import DocumentIntelligenceClient

log = logging.getLogger(__name__)

MODEL_ID = "prebuilt-read"  # text lines only; cheapest model. Part of parser_id.


class _AnalyzeClient(Protocol):
    """The two client calls we use; lets tests pass a fake without network access."""

    def begin_analyze_document(self, model_id: str, body: Any, **kwargs: Any) -> Awaitable[Any]: ...

    def delete_analyze_result(
        self, model_id: str, result_id: str, **kwargs: Any
    ) -> Awaitable[None]: ...


class DocIntelParser:
    def __init__(self, client: _AnalyzeClient, model_id: str = MODEL_ID) -> None:
        self.client = client
        self.model_id = model_id

    async def parse(self, pdf_bytes: bytes) -> ParsedDocument:
        poller = await self.client.begin_analyze_document(
            self.model_id, io.BytesIO(pdf_bytes), content_type="application/octet-stream"
        )
        result_id = poller.details["operation_id"]
        doc_sha256 = sha256_hex(pdf_bytes)
        try:
            result = await poller.result()
            texts = self._page_texts(result)
            api_version = str(result.api_version)
        except BaseException:
            await self._delete_result(result_id, doc_sha256)  # never masks the error being raised
            raise
        if not await self._delete_result(result_id, doc_sha256):
            raise ParserError("Document Intelligence result could not be deleted")
        return make_document(pdf_bytes, texts, f"docintel-{self.model_id}-{api_version}")

    async def _delete_result(self, result_id: str, doc_sha256: str) -> bool:
        """Delete the stored analyze result; on failure log metadata only and return False.

        Failing the parse when the delete fails (on the success path) makes the retention gap
        visible to the caller instead of silently leaving a copy on the service.
        """
        try:
            await self.client.delete_analyze_result(model_id=self.model_id, result_id=result_id)
        except Exception:
            log.warning(
                "docintel_delete_failed",
                exc_info=True,
                extra={"doc_sha256": doc_sha256, "status": "result_may_persist"},
            )
            return False
        return True

    @staticmethod
    def _page_texts(result: Any) -> list[str]:
        """Lines per page, pages sorted by number; every page 1..n must be present."""
        pages = sorted(result.pages or [], key=lambda p: p.page_number)
        if [p.page_number for p in pages] != list(range(1, len(pages) + 1)):
            raise ParserError("Document Intelligence returned non-contiguous pages")
        return [normalize_text("\n".join(line.content for line in p.lines or [])) for p in pages]


def make_docintel_client(endpoint: str) -> DocumentIntelligenceClient:
    """Entra ID auth (managed identity / az login): no keys in code (hard rule 7)."""
    from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
    from azure.identity.aio import DefaultAzureCredential

    return DocumentIntelligenceClient(endpoint=endpoint, credential=DefaultAzureCredential())
