"""Turn the model's final answer into a verified answer (M3 AC3, AC4; rule 4).

The model ends with `{text, cited_ids, quotes}` (strict JSON). Code then:
1. builds `facts_used` from the cited ids, taking values from the tool results it holds;
2. grounds each quote on the page the tools returned (the stored quote is the full line, D13c);
3. rejects text whose numbers or dates are not in `facts_used`;
4. rejects text that cites a fact with an honesty status (`undeterminable`, `conflict`, ...)
   without stating that status, so a status is never papered over (AC4).
A rejected answer is retried once by the loop; after that, `fallback_answer` is returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from chatmed.agent.tools import SHA_PREFIX, EvidenceItem, ToolContext
from chatmed.extraction import PageLines, ground_quote

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "cited_ids": {"type": "array", "items": {"type": "string"}},
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"page_id": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["page_id", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["cited_ids", "quotes", "text"],
    "additionalProperties": False,
}

# Statuses the user must be told about when a cited row carries them (AC4).
HONESTY_STATUSES = frozenset(
    {
        "undeterminable",
        "conflict",
        "unverified",
        "missing",
        "invalid",
        "unmatched",
        "ambiguous",
        "not_attempted",
        "not_comparable",
    }
)

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w])")


@dataclass(frozen=True)
class Answer:
    text: str
    facts_used: list[dict[str, Any]]
    verified: bool  # False: the deterministic fallback
    reason: str | None = None  # why the fallback was used
    fingerprint: str | None = None  # set by the loop: agent prompt, rules, model, documents


class AnswerRejected(Exception):
    """A short code saying why; sent back to the model for its one retry."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return [] if value is None or isinstance(value, bool) else [str(value)]


def _as_decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "."))
    except InvalidOperation:
        return None


def numbers_and_dates(text: str) -> tuple[set[str], set[str]]:
    """(ISO dates, other numbers) in `text`; numbers inside dates are not counted twice."""
    dates = set(_DATE.findall(text))
    rest = _DATE.sub(" ", text)
    return dates, set(_NUMBER.findall(rest))


def unsupported_numbers(text: str, items: list[EvidenceItem]) -> list[str]:
    """Numbers/dates in `text` that appear nowhere in the values or citations of `items`."""
    sources = " ".join(s for it in items for s in _strings([it.values, it.citation]))
    allowed_dates, _ = numbers_and_dates(sources)
    allowed_tokens = set(re.findall(r"\d+(?:[.,]\d+)?", sources))
    allowed_decimals = {d for t in allowed_tokens if (d := _as_decimal(t)) is not None}
    dates, numbers = numbers_and_dates(text)
    bad = sorted(d for d in dates if d not in allowed_dates)
    for n in sorted(numbers):
        if n not in allowed_tokens and _as_decimal(n) not in allowed_decimals:
            bad.append(n)
    return bad


def _status_words(item: EvidenceItem) -> set[str]:
    found = {
        str(item.values.get(k))
        for k in ("status", "join_status", "reason")
        if item.values.get(k) is not None
    }
    return {w for v in found for w in v.split(",") if w in HONESTY_STATUSES}


def unstated_statuses(text: str, items: list[EvidenceItem]) -> list[str]:
    """Honesty statuses of cited rows that the text does not name (AC4)."""
    lowered = text.casefold()
    missing = set()
    for item in items:
        for word in _status_words(item):
            if word not in lowered and word.replace("_", " ") not in lowered:
                missing.add(word)
    return sorted(missing)


def _quote_item(ctx: ToolContext, page_id: str, quote: str, n: int) -> EvidenceItem:
    """A quote from page text the tools returned, grounded and stored as full line(s)."""
    page = ctx.pages.get(page_id)
    if page is None:
        raise AnswerRejected("unknown_page_id")
    sha, number, text = page
    line = ground_quote(PageLines.of(text), quote)
    if line is None:
        raise AnswerRejected("ungrounded_quote")
    return EvidenceItem(
        id=f"quote:{sha[:SHA_PREFIX]}:{number}:{n}",
        source="page_text",
        values={"quote": line},
        citation={"doc": sha[:SHA_PREFIX], "page": number, "quote": line},
    )


def expand_citations(ctx: ToolContext, cited: set[str]) -> list[str]:
    """Cited ids plus everything a cited reconciliation stands on, sorted (D14, option A).

    A reconciliation is one unit: citing any `recon:<doc>:` row cites all of that document's
    reconciliation items, the adae record they were compared with, and the document's joined
    `subject_id` fact the join rests on (added by code, cited or not). Which rows the model
    chose to cite varied between real-model runs; this makes `facts_used` depend only on the
    tool results, and the status check then forces every `not_comparable` into the text.
    """
    expanded = set(cited)
    for doc in {c.split(":")[1] for c in cited if c.startswith("recon:")}:
        for key, item in ctx.evidence.items():
            if key.startswith(f"recon:{doc}:"):
                expanded.add(key)
                if item.citation.get("key"):
                    expanded.add(f"adae:{item.citation['key']}")
            elif key.startswith(f"fact:{doc}:subject_id:") and (
                item.values.get("join_status") == "joined"
            ):
                expanded.add(key)
    return sorted(expanded)


def verify(ctx: ToolContext, output: dict[str, Any]) -> Answer:
    """A verified Answer, or AnswerRejected with a short code."""
    text = str(output.get("text", "")).strip()
    if not text:
        raise AnswerRejected("empty_answer")
    cited = set(output.get("cited_ids") or [])
    if any(c not in ctx.evidence for c in cited):
        raise AnswerRejected("unknown_cited_id")
    items = [ctx.evidence[c] for c in expand_citations(ctx, cited)]
    for n, q in enumerate(output.get("quotes") or [], start=1):
        items.append(_quote_item(ctx, str(q.get("page_id", "")), str(q.get("quote", "")), n))
    if unsupported_numbers(text, items):
        raise AnswerRejected("value_not_in_facts_used")
    if unstated_statuses(text, items):
        raise AnswerRejected("status_not_stated")
    return Answer(text=text, facts_used=[it.as_dict() for it in items], verified=True)


def _describe(item: EvidenceItem) -> str:
    shown = {k: v for k, v in item.values.items() if v is not None and k != "doc"}
    body = ", ".join(f"{k}={v}" for k, v in shown.items())
    cite = ", ".join(f"{k} {v}" for k, v in item.citation.items() if v is not None)
    return f"- {item.id}: {body} [{cite}]"


def fallback_answer(ctx: ToolContext, reason: str) -> Answer:
    """Deterministic text built only from tool results; states every status as it is."""
    items = [ctx.evidence[k] for k in sorted(ctx.evidence)]
    if not items:
        text = "I could not produce a verified answer, and no facts were retrieved."
    else:
        lines = [_describe(it) for it in items]
        text = "I could not produce a verified answer. These are the facts retrieved:\n"
        text += "\n".join(lines)
    return Answer(
        text=text, facts_used=[it.as_dict() for it in items], verified=False, reason=reason
    )
