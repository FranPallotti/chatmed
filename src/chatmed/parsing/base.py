"""The parser interface and the small helpers every implementation shares."""

from __future__ import annotations

import hashlib
from typing import Protocol

from chatmed.models import ParsedDocument, ParsedPage


class DocumentParser(Protocol):
    """Turns PDF bytes into page texts, in memory.

    Contract (tests/contracts/test_parser_contract.py): same bytes -> identical result;
    pages 1-based and in order; `sha256` of the original bytes; `parser_id` names the backend
    and every setting that can change the text (it is part of the reproducibility fingerprint).
    """

    async def parse(self, pdf_bytes: bytes) -> ParsedDocument: ...


class ParserError(RuntimeError):
    """A document could not be parsed. Messages never contain document text (hard rule 2)."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_text(text: str) -> str:
    """Only line endings and trailing form feeds/whitespace: content is left untouched."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\x0c \n")


def make_document(pdf_bytes: bytes, texts: list[str], parser_id: str) -> ParsedDocument:
    """Build a ParsedDocument with 1-based page numbers in the given (page) order."""
    pages = tuple(ParsedPage(number=i, text=t) for i, t in enumerate(texts, start=1))
    return ParsedDocument(sha256=sha256_hex(pdf_bytes), pages=pages, parser_id=parser_id)
