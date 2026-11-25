"""Types shared between the parser and the extraction step."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedPage:
    number: int  # 1-based
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    sha256: str  # hash of the original PDF bytes
    pages: tuple[ParsedPage, ...]
    parser_id: str  # e.g. "pymupdf-1.24" or "docintel-layout-2024-11-30"
