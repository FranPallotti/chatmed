"""Page text for questions the extracted fields do not cover (D2, M3 AC5).

Small documents go to the model whole. Larger ones are searched with BM25 over pages, kept in
memory only for the duration of the call (no index is stored). Ties are broken by document
sha256, then page number, so the same question always returns the same pages (rule 3).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from chatmed.models import ParsedDocument

K1 = 1.5
B = 0.75
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class PageHit:
    doc_sha: str
    page: int
    text: str
    score: float | None  # None when the whole text was returned


def tokenize(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", text).casefold())


def _all_pages(docs: list[ParsedDocument]) -> list[PageHit]:
    ordered = sorted(docs, key=lambda d: d.sha256)
    return [PageHit(d.sha256, p.number, p.text, None) for d in ordered for p in d.pages]


def bm25(query: str, pages: list[PageHit], top_k: int) -> list[PageHit]:
    """Okapi BM25; only pages sharing at least one query term are returned."""
    terms = sorted(set(tokenize(query)))
    docs = [Counter(tokenize(p.text)) for p in pages]
    if not terms or not docs:
        return []
    avg_len = sum(sum(c.values()) for c in docs) / len(docs) or 1.0
    df = {t: sum(1 for c in docs if t in c) for t in terms}
    scored: list[PageHit] = []
    for page, counts in zip(pages, docs, strict=True):
        length = sum(counts.values())
        score = 0.0
        for t in terms:  # sorted terms: the float sum is computed in a fixed order
            tf = counts.get(t, 0)
            if tf:
                idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / avg_len))
        if score > 0:
            scored.append(PageHit(page.doc_sha, page.page, page.text, round(score, 6)))
    scored.sort(key=lambda h: (-(h.score or 0.0), h.doc_sha, h.page))
    return scored[:top_k]


def relevant_pages(
    query: str, docs: list[ParsedDocument], *, fulltext_max_chars: int, top_k: int
) -> list[PageHit]:
    """Every page if the documents are small enough, else the BM25 top-k pages."""
    pages = _all_pages(docs)
    if sum(len(p.text) for p in pages) <= fulltext_max_chars:
        return pages
    return bm25(query, pages, top_k)
