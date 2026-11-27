"""Turn the model's final answer into a verified answer (M3 AC3, AC4; rule 4).

The model ends with `{text, cited_ids, quotes}` (strict JSON, `prompt.ANSWER_SCHEMA`). Code then:
1. builds `facts_used` from the cited ids, taking values from the tool results it holds;
2. grounds each quote on the page the tools returned (the stored quote is the full line, D13c);
3. rejects text whose numbers or dates are not in `facts_used`;
4. rejects text that cites a fact with an honesty status (`undeterminable`, `conflict`, ...)
   without stating that status, so a status is never papered over (AC4).
A rejected answer is retried once by the loop; after that, `fallback_answer` is returned.

Reconciliation answers are not written by the model (D14, agent-v7): once the model
has called `reconcile_case`, `render_reconciliation` writes the answer from its rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from chatmed.agent.reconcile import ITEMS
from chatmed.agent.tools import SHA_PREFIX, EvidenceItem, ToolContext
from chatmed.extraction import PageLines, ground_quote

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


@dataclass(frozen=True)
class Answer:
    text: str
    facts_used: list[dict[str, Any]]
    verified: bool  # False: the deterministic fallback
    reason: str | None = None  # why the fallback was used
    fingerprint: str | None = None  # verified answers only: what makes their facts identical

    @property
    def outcome(self) -> str:
        """`verified` (facts reproducible under `fingerprint`) or `fallback` (see `reason`)."""
        return "verified" if self.verified else "fallback"


class AnswerRejected(Exception):
    """A short code saying why; sent back to the model for its one retry."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w])")

# Written dates. ISO first; then "12 June 2026" / "12-JUN-2026" (CIOMS style), "June 12, 2026"
# and numeric "12/06/2026" or "12.06.2026". The year is optional for month-name forms.
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[ \-/]+(?:of +)?{_MONTH}(?:[ \-/,]+(\d{{4}}))?\b", re.I
)
_MONTH_DAY = re.compile(rf"\b{_MONTH}[ \-]+(\d{{1,2}})(?:st|nd|rd|th)?(?:,? +(\d{{4}}))?\b", re.I)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
_MONTHS = {m: n for n, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
)}  # fmt: skip

# (year or None, month, day): one written date can have several readings (12/06 vs 06/12)
Reading = tuple[int | None, int, int]

# Citation and value keys that hold identifiers (sha prefixes, row keys), not statable values:
# their digit runs must not make a number in the text look supported.
_ID_KEYS = frozenset({"doc", "key", "table"})


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [s for k, v in value.items() if k not in _ID_KEYS for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return [] if value is None or isinstance(value, bool) else [str(value)]


def _as_decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "."))
    except InvalidOperation:
        return None


def _valid(year: int | None, month: int, day: int) -> bool:
    try:
        date(year or 2000, month, day)  # 2000 is a leap year: 29 Feb without a year is valid
    except ValueError:
        return False
    return True


def _readings(match: re.Match[str], pattern: re.Pattern[str]) -> list[Reading]:
    """The valid (year, month, day) readings of one written date; [] if none is a real date."""
    if pattern is _ISO_DATE:
        y, m, d = (int(g) for g in match.groups())
        candidates: list[Reading] = [(y, m, d)]
    elif pattern is _NUMERIC_DATE:
        a, b, y = (int(g) for g in match.groups())
        candidates = [(y, b, a), (y, a, b)]  # day/month first, then month/day
    elif pattern is _DAY_MONTH:
        day, month, year = match.groups()
        candidates = [(int(year) if year else None, _MONTHS[month[:3].lower()], int(day))]
    else:
        month, day, year = match.groups()
        candidates = [(int(year) if year else None, _MONTHS[month[:3].lower()], int(day))]
    return [r for r in dict.fromkeys(candidates) if _valid(*r)]


def written_dates(text: str) -> tuple[list[tuple[str, list[Reading]]], str]:
    """(each written date with its readings, the text with those dates blanked out).

    Blanking keeps the day, month and year of a date from being checked again as numbers.
    """
    found: list[tuple[str, list[Reading]]] = []
    for pattern in (_ISO_DATE, _DAY_MONTH, _MONTH_DAY, _NUMERIC_DATE):
        for match in pattern.finditer(text):
            found.append((match.group(0), _readings(match, pattern)))
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return found, text


def _supported(readings: list[Reading], allowed: set[tuple[int, int, int]]) -> bool:
    """A written date is supported if one of its readings is a date in facts_used. Without a
    year, its month and day must match one."""
    for year, month, day in readings:
        if any((year in (None, ay)) and (month, day) == (am, ad) for ay, am, ad in allowed):
            return True
    return False


def _numeric_form(written: str) -> tuple[int, int, int] | None:
    """(first, second, year) of a numeric date such as "12/06/2026", whatever the separator."""
    match = _NUMERIC_DATE.fullmatch(written)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def unsupported_numbers(text: str, items: list[EvidenceItem]) -> list[str]:
    """Dates and numbers in `text` that are not values or citations of `items` (AC3).

    Dates are compared as dates, whatever format the text writes them in, so "June 6, 2026"
    is not supported by 2026-06-12 even though 6 and 2026 occur in it. Identifiers (document
    sha prefixes, row keys) are not sources: "7652" is not supported by doc `7652ea457661`.

    A source date only supports a date when it has one reading. An ambiguous source such as
    "12/06/2026" (12 June or 6 December) decides neither, so the text may repeat it as
    written (12/06/2026 or 12.06.2026) but may not state either reading as a resolved date.
    The text's own numeric dates may be read either way: one supported reading is enough.
    """
    sources = " ".join(s for it in items for s in _strings([it.values, it.citation]))
    source_dates, _ = written_dates(sources)
    allowed_dates = {
        (y, m, d)
        for _, readings in source_dates
        if len(readings) == 1
        for y, m, d in readings
        if y is not None
    }
    allowed_forms = {form for written, _ in source_dates if (form := _numeric_form(written))}
    allowed_tokens = set(re.findall(r"\d+(?:[.,]\d+)?", sources))
    allowed_decimals = {d for t in allowed_tokens if (d := _as_decimal(t)) is not None}
    dates, rest = written_dates(text)
    bad = sorted(
        {
            written
            for written, r in dates
            if not _supported(r, allowed_dates) and _numeric_form(written) not in allowed_forms
        }
    )
    for n in sorted(set(_NUMBER.findall(rest))):
        if n not in allowed_tokens and _as_decimal(n) not in allowed_decimals:
            bad.append(n)
    return bad


# A status counts as stated only if no negation comes before it in its clause ("no conflict",
# "no evidence of any conflict"). Any word ending in "n't" is a negation too. Erring this way
# costs a retry; erring the other way would hide a status (AC4).
_NEGATIONS = frozenset(
    {
        "no", "not", "never", "without", "nor", "neither", "none", "nothing", "cannot",
        "absence", "absent", "lack", "lacks", "lacking", "free", "zero", "instead", "rather",
    }
)  # fmt: skip
_CLAUSE_BREAK = re.compile(
    r"[.,;:!?()\n–—]"
    r"|\b(?:but|and|or|so|because|since|while|whereas|although|though|however)\b"
)
# Statuses whose own name starts with "not": blanked before looking for negations, so that
# "not comparable because it is missing" still states `missing`.
_NOT_STATUSES = re.compile(r"\bnot[ _](?:comparable|attempted)\b")


def _status_pattern(status: str) -> re.Pattern[str]:
    """The status as a whole word, plural or -ing allowed; a hyphen on either side makes it a
    different word ("conflict-free", "non-conflicting" do not state `conflict`)."""
    return re.compile(r"(?<![\w-])" + status.replace("_", "[ _]") + r"(?:s|ing)?(?![\w-])")


def _negated(clause: str) -> bool:
    words = re.findall(r"[a-z']+", _NOT_STATUSES.sub(" ", clause))
    return any(w in _NEGATIONS or w.endswith("n't") for w in words)


def _states(text: str, status: str) -> bool:
    """True if `text` names `status` at least once with no negation before it in its clause."""
    for match in _status_pattern(status).finditer(text):
        if not _negated(_CLAUSE_BREAK.split(text[: match.start()])[-1]):
            return True
    return False


def _status_words(item: EvidenceItem) -> set[str]:
    found = {
        str(item.values.get(k))
        for k in ("status", "join_status", "reason")
        if item.values.get(k) is not None
    }
    return {w for v in found for w in v.split(",") if w in HONESTY_STATUSES}


def unstated_statuses(text: str, items: list[EvidenceItem]) -> list[str]:
    """Honesty statuses of cited rows that the text does not state (AC4).

    Whole words only ("missing", not "dismissing" or "conflict-free"; "conflicting" counts),
    and a negated mention ("there is no conflict", "no evidence of any conflict", "is not
    missing") does not count as stating it.
    """
    lowered = text.casefold().replace("’", "'")
    return sorted({w for it in items for w in _status_words(it) if not _states(lowered, w)})


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


RECONCILIATION_HEADER = "Reconciliation with the study ADAE record, item by item:"
# Last line of every rendered reconciliation: the answer never covers the rest of a combined
# question ("does it match ADAE, and what arm?"), so it says so instead of staying silent.
RECONCILIATION_SCOPE = (
    "This answer covers only the comparison with the study database; ask anything else as a "
    "separate question."
)


def _recon_line(item: EvidenceItem) -> str:
    """`Onset date: not comparable (conflict).` Status name and reason code, no values."""
    name = str(item.values["item"]).replace("_", " ").capitalize()
    line = f"{name}: {str(item.values['status']).replace('_', ' ')}"
    reason = item.values.get("reason")
    return line + (f" ({str(reason).replace('_', ' ')})." if reason else ".")


def render_reconciliation(ctx: ToolContext, doc: str) -> Answer:
    """The reconciliation answer for document `doc` (sha prefix), written by code (D14).

    The real model kept rewording statuses ("cannot be compared") and describing a
    `conflict` reason instead of naming it, whatever the prompt said (agent-v3..v6), so its
    first answer was rejected. Here the model only routes the question to `reconcile_case`;
    the text is one line per item, in `ITEMS` order, with its status name and reason code,
    then `RECONCILIATION_SCOPE` (the answer covers nothing else the question asked), and
    the answer cites every item (and, through `expand_citations`, the adae record and the
    joined subject fact). No values in the text, so there is no number to check. It still
    goes through `verify`: a unit row with a status the text does not state (e.g. a subject
    fact) gives the fallback, never an unchecked answer.
    """
    rows = [ctx.evidence[k] for i in ITEMS if (k := f"recon:{doc}:{i}") in ctx.evidence]
    lines = [RECONCILIATION_HEADER, *(_recon_line(r) for r in rows), RECONCILIATION_SCOPE]
    text = "\n".join(lines)
    try:
        return verify(ctx, {"text": text, "cited_ids": [r.id for r in rows], "quotes": []})
    except AnswerRejected as exc:
        return fallback_answer(ctx, exc.code)


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
