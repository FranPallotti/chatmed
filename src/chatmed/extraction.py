"""Schema-driven extraction.

tenant field metadata -> strict JSON schema -> LLM (temperature 0, seed)
-> deterministic checks in code (types, evidence quote really on that page, checkbox rule)
-> merge across page windows -> subject join -> facts loaded into the session's in-memory DuckDB.

Nothing here writes document content to disk or logs (rules 1, 2). Model calls are stateless
(rule 6) and carry temperature 0, a fixed seed, the pinned deployment, `PROMPT_VERSION` and a
strict schema (rule 3). What counts as a fact is decided here, in code (rule 4, D13, D13a).
"""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import json
import logging
import random
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import asdict, astuple, dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import openai

from chatmed.models import ParsedDocument, ParsedPage

if TYPE_CHECKING:  # keep the pure parts importable/testable without these at runtime
    import duckdb
    from openai import AsyncAzureOpenAI

    from chatmed.config import Settings
    from chatmed.tenant_db import TenantRepository

log = logging.getLogger(__name__)

PROMPT_VERSION = "extract-v2"  # v2 (D13): OCR noise, interleaved columns, checkbox fields
# Version of the code that turns model output into facts (grounding, checkbox rules, merge,
# subject join). Part of the fingerprint (D6): the same model output can give different facts
# under different rules. tests/guardrails/test_rules_version.py fails if this module changes
# without a bump.
RULES_VERSION = "rules-1"


class ExtractionError(RuntimeError):
    """A model call failed or returned unusable output. Messages never contain content."""


# ---------------------------------------------------------------- metadata


class DataType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    BOOLEAN = "boolean"
    ENUM = "enum"


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    description: str
    data_type: DataType
    required: bool = False
    multiple: bool = False
    allowed_values: tuple[str, ...] = ()
    unit: str | None = None
    join_target: str | None = None
    checkbox: bool = False  # value is a tick mark on the form (D13)
    box_labels: tuple[str, ...] = ()  # text printed next to the box(es) (D13b)


@dataclass(frozen=True)
class DocTypeSchema:
    tenant_id: str
    doc_type: str
    fields: tuple[FieldDef, ...]

    @staticmethod
    def from_rows(tenant_id: str, doc_type: str, rows: list[dict[str, Any]]) -> DocTypeSchema:
        fields = tuple(
            FieldDef(
                key=r["key"],
                label=r["label"],
                description=r["description"],
                data_type=DataType(r["data_type"]),
                required=bool(r["required"]),
                multiple=bool(r["multiple"]),
                allowed_values=tuple(r.get("allowed_values") or ()),
                unit=r.get("unit"),
                join_target=r.get("join_target"),
                checkbox=bool(r.get("checkbox", False)),
                box_labels=tuple(r.get("box_labels") or ()),
            )
            for r in sorted(rows, key=lambda r: r["key"])  # stable order
        )
        return DocTypeSchema(tenant_id, doc_type, fields)

    @property
    def schema_hash(self) -> str:
        payload = json.dumps([asdict(f) for f in self.fields], sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- JSON schema

_EVIDENCE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page", "quote"],
    "properties": {
        "page": {"type": "integer"},
        "quote": {"type": "string"},
    },
}

CHECKBOX_HINT = (
    " [checkbox on the form: ticks are not in the text; answer only from a sentence that states"
    " it in its own words (e.g. in the narrative) and quote that sentence, never the printed"
    " box label or options; otherwise null]"
)


def _value_schema(f: FieldDef) -> dict[str, Any]:
    if f.data_type is DataType.ENUM:
        return {"type": ["string", "null"], "enum": [*f.allowed_values, None]}
    if f.data_type is DataType.BOOLEAN:
        return {"type": ["boolean", "null"]}
    if f.data_type is DataType.INTEGER:
        return {"type": ["integer", "null"]}
    # string, decimal, date come back as text and are parsed exactly in code
    return {"type": ["string", "null"]}


OTHER_STATEMENTS_HINT = (
    "Other places in these pages that state a DIFFERENT value for this field (for example a"
    " later correction), one item each; empty if there are none."
)


def _field_schema(f: FieldDef) -> dict[str, Any]:
    """A multiple field is a list of items. A single field is one item plus
    `other_statements`, so a document that states two different values (e.g. a corrected date
    on a later page of the same window) can surface them and code reports a `conflict`."""
    item: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value", "evidence"],
        "properties": {
            "value": _value_schema(f),
            "evidence": {"anyOf": [_EVIDENCE, {"type": "null"}]},
        },
    }
    desc = f"{f.label}: {f.description}" + (f" (unit: {f.unit})" if f.unit else "")
    if f.checkbox:
        desc += CHECKBOX_HINT
    if f.multiple:
        return {"type": "array", "description": desc, "items": item}
    others = {"type": "array", "description": OTHER_STATEMENTS_HINT, "items": item}
    return {
        **item,
        "required": [*item["required"], "other_statements"],
        "properties": {**item["properties"], "other_statements": others},
        "description": desc,
    }


def build_json_schema(schema: DocTypeSchema) -> dict[str, Any]:
    """Strict-mode schema: every property required, no extra properties."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [f.key for f in schema.fields],
        "properties": {f.key: _field_schema(f) for f in schema.fields},
    }


# ---------------------------------------------------------------- prompt

SYSTEM_PROMPT = """You extract fields from clinical safety documents such as CIOMS I forms.
Rules:
- Use only the page text provided. Never infer, guess or use outside knowledge.
- If a field is not explicitly present, return value null and evidence null.
- The text may come from OCR. It can contain noise characters, and boxes printed side by side
  in columns may be interleaved line by line, so a label and its value can be separated by text
  from a neighbouring box. Match each value to its own label; if unsure, return null.
- evidence.quote must be a short fragment copied verbatim from one place on the page (at most
  25 words, never stitched together from different lines or boxes), and evidence.page must be
  the number of the page it was copied from.
- Checkbox fields: tick marks are NOT in this text, so a printed box label such as
  "patient died" or "yes no NA" says nothing about whether the box is ticked; it is never
  evidence. Search the whole text, especially the narrative, for a sentence that states the
  fact in its own words (e.g. "The patient was hospitalised on ..." or "was not hospitalised"),
  answer from it and quote it. If there is no such sentence, return null.
- Dates: YYYY-MM-DD. Decimals: digits with '.' as decimal separator, no unit.
- For fields that can appear several times, return one item per occurrence.
- For single-value fields, if the pages state a different value somewhere else (for example a
  correction on a later page), put the first in value/evidence and each different one in
  other_statements, with its own evidence."""


def _render_pages(pages: tuple[ParsedPage, ...]) -> str:
    return "\n\n".join(f"[Page {p.number}]\n{p.text}" for p in pages)


def user_message(doc_type: str, pages: tuple[ParsedPage, ...]) -> str:
    """The user turn for one window: document type and the numbered page texts."""
    return f"Document type: {doc_type}\n\nPages:\n{_render_pages(pages)}"


def _windows(pages: tuple[ParsedPage, ...], max_chars: int) -> list[tuple[ParsedPage, ...]]:
    """Deterministic page windows (characters as a cheap proxy for tokens)."""
    out: list[tuple[ParsedPage, ...]] = []
    cur: list[ParsedPage] = []
    size = 0
    for p in sorted(pages, key=lambda p: p.number):
        if cur and size + len(p.text) > max_chars:
            out.append(tuple(cur))
            cur, size = [], 0
        cur.append(p)
        size += len(p.text)
    if cur:
        out.append(tuple(cur))
    return out


# ---------------------------------------------------------------- verification


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    return re.sub(r"\s+", " ", s).strip()


@dataclass(frozen=True)
class PageLines:
    """A page split into non-empty lines, kept both as printed and normalized for matching."""

    printed: tuple[str, ...]  # whitespace collapsed, case kept
    starts: tuple[int, ...]  # offset of each normalized line in `joined`
    joined: str  # normalized lines joined by one space

    @staticmethod
    def of(text: str) -> PageLines:
        printed, normed, starts, offset = [], [], [], 0
        for line in text.splitlines():
            n = _norm(line)
            if not n:
                continue
            printed.append(re.sub(r"\s+", " ", line).strip())
            normed.append(n)
            starts.append(offset)
            offset += len(n) + 1
        return PageLines(tuple(printed), tuple(starts), " ".join(normed))


def ground_quote(lines: PageLines, quote: str) -> str | None:
    """The full page line(s) holding the quote's first occurrence, or None if it is not there.

    The model picks how much of a line to quote, and that varies between runs ("Age: 47 years"
    vs "Age: 47 years 3. Sex: F"). Storing the whole line(s) instead makes the evidence a
    function of the page, so the same document gives the same facts (D6, D13c).
    """
    needle = _norm(quote)
    at = lines.joined.find(needle) if needle else -1
    if at < 0:
        return None
    first = bisect.bisect_right(lines.starts, at) - 1
    last = bisect.bisect_right(lines.starts, at + len(needle) - 1) - 1
    return " ".join(lines.printed[first : last + 1])


class Status(StrEnum):
    OK = "ok"
    MISSING = "missing"
    UNVERIFIED = "unverified"
    INVALID = "invalid"
    CONFLICT = "conflict"
    UNDETERMINABLE = "undeterminable"  # checkbox field without a written statement (D13)


class JoinStatus(StrEnum):
    JOINED = "joined"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    NOT_ATTEMPTED = "not_attempted"  # subject_id not verified; the reason is its status (D13a)


@dataclass(frozen=True)
class Fact:
    doc_sha: str
    doc_type: str
    field_key: str
    value_text: str | None
    value_num: Decimal | None
    value_date: date | None
    value_bool: bool | None
    unit: str | None
    page: int | None
    quote: str | None
    status: str  # a Status value
    join_status: str | None = None  # a JoinStatus value, only for subject id fields
    join_value: str | None = None  # the matched adsl.subjid when joined


def _empty(f: FieldDef, doc: ParsedDocument, doc_type: str, status: Status) -> Fact:
    return Fact(doc.sha256, doc_type, f.key, None, None, None, None, f.unit, None, None, status)


def _typed(f: FieldDef, raw: Any) -> tuple[str, Decimal | None, date | None, bool | None]:
    """Return (normalized text, num, date, bool). Raises ValueError if invalid."""
    if f.data_type is DataType.INTEGER:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError("not an integer")
        return str(raw), Decimal(raw), None, None
    if f.data_type is DataType.DECIMAL:
        try:
            d = Decimal(str(raw).strip())
        except InvalidOperation as e:
            raise ValueError("not a decimal") from e
        return str(d), d, None, None
    if f.data_type is DataType.DATE:
        parsed = date.fromisoformat(str(raw).strip())
        return parsed.isoformat(), None, parsed, None
    if f.data_type is DataType.BOOLEAN:
        if not isinstance(raw, bool):
            raise ValueError("not a boolean")
        return str(raw).lower(), None, None, raw
    if f.data_type is DataType.ENUM and raw not in f.allowed_values:
        raise ValueError("not an allowed value")
    return str(raw).strip(), None, None, None


def _check_item(
    f: FieldDef,
    item: dict[str, Any],
    doc: ParsedDocument,
    doc_type: str,
    pages_by_num: dict[int, PageLines],
) -> Fact | None:
    raw, ev = item.get("value"), item.get("evidence")
    if raw is None:
        return None
    page: int | None = ev.get("page") if ev else None
    quote: str | None = ev.get("quote") if ev else None
    try:
        text, num, dt, bl = _typed(f, raw)
    except ValueError:
        return Fact(
            doc_sha=doc.sha256,
            doc_type=doc_type,
            field_key=f.key,
            unit=f.unit,
            value_text=str(raw),
            value_num=None,
            value_date=None,
            value_bool=None,
            page=page,
            quote=quote,
            status=Status.INVALID,
        )
    # Grounding check: the quote must really be on the page the model claims. A grounded quote
    # is stored as the full line(s) it sits on (D13c).
    line = (
        ground_quote(pages_by_num[page], quote)
        if quote is not None and page is not None and page in pages_by_num
        else None
    )
    if line is None:
        status = Status.UNVERIFIED
    elif f.checkbox and is_label_quote(f, quote):
        # Checked on the model's own words too: a label fragment on a line shared with other
        # text (OCR columns) must not pass because the line has extra words (D13c).
        status = Status.UNDETERMINABLE
    else:
        status = Status.OK
    return Fact(
        doc_sha=doc.sha256,
        doc_type=doc_type,
        field_key=f.key,
        unit=f.unit,
        value_text=text,
        value_num=num,
        value_date=dt,
        value_bool=bl,
        page=page,
        quote=line if line is not None else quote,
        status=status,
    )


# ---------------------------------------------------------------- checkbox rule (D13, D13a)

# Words that carry no meaning of their own. Negations ("no", "not", "never") are NOT here:
# "not hospitalised" is a statement.
STOP_WORDS = frozenset(
    "a an and are as at be been by for from in into is it of on or than that the this to was "
    "were with".split()
)


def _tokens(text: str) -> list[str]:
    """Lowercase ASCII words: accents removed, punctuation and symbols as separators."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    return re.findall(r"[a-z0-9]+", plain)


def _meaningful_words(text: str) -> list[str]:
    """Tokens minus stop words and OCR noise (1-2 character tokens that are not all digits)."""
    return [w for w in _tokens(text) if w not in STOP_WORDS and (len(w) > 2 or w.isdigit())]


def _label_vocabulary(f: FieldDef) -> frozenset[str]:
    """Words of the field's own label, description and options: what the form itself prints.

    Hyphenated words also count joined ("follow-up" -> "followup"), as OCR and forms vary.
    """
    sources = [f.label, f.description, *f.allowed_values]
    words: set[str] = set()
    for source in sources:
        words.update(_tokens(source))
        words.update(_tokens(source.replace("-", "")))
    return frozenset(words)


def _contains_box_label(f: FieldDef, words: set[str]) -> bool:
    """True if the words include every meaningful word of one printed box label (D13b).

    Catches a quote of a form heading that ends in the label ("8-12. Check all appropriate to
    adverse reaction: patient died"): it has extra words, so the D13a rule alone accepts it.
    """
    for label in f.box_labels:
        label_words = set(_meaningful_words(label))
        if label_words and label_words <= words:
            return True
    return False


def is_label_quote(f: FieldDef, quote: str | None) -> bool:
    """True if the quote is printed form text rather than a written statement.

    That is: it adds no meaningful word beyond the field's own label/description/options
    (D13a), or it contains all meaningful words of one of the field's box labels (D13b).
    Such a quote can never decide a checkbox field. A quote with no meaningful words counts too.
    Known limitation (D13b): a narrative that repeats a box label word for word is rejected.
    """
    words = _meaningful_words(quote or "")
    vocabulary = _label_vocabulary(f)
    return all(word in vocabulary for word in words) or _contains_box_label(f, set(words))


def _resolve_checkbox(
    f: FieldDef, candidates: list[Fact], doc: ParsedDocument, doc_type: str
) -> list[Fact]:
    """`ok` only from a grounded written statement; otherwise `undeterminable`, never missing."""
    statements = [c for c in candidates if c.status == Status.OK and not is_label_quote(f, c.quote)]
    if not statements:
        return [_empty(f, doc, doc_type, Status.UNDETERMINABLE)]
    return _merge_ok(f, statements, doc, doc_type)


# ---------------------------------------------------------------- merge


def _order(c: Fact) -> tuple[int, str, str]:
    """Total order on candidates: page, value, then quote. Without the quote, two items with the
    same page and value would keep the model's item order, and the stored evidence with it."""
    return (c.page or 0, c.value_text or "", c.quote or "")


def _first_per_value(facts: list[Fact]) -> list[Fact]:
    """One fact per distinct value: the first in `_order` (earliest page, then quote)."""
    seen: set[str] = set()
    out = []
    for c in sorted(facts, key=_order):
        if (c.value_text or "") not in seen:
            seen.add(c.value_text or "")
            out.append(c)
    return out


def _merge_ok(f: FieldDef, ok: list[Fact], doc: ParsedDocument, doc_type: str) -> list[Fact]:
    """One fact per distinct value. Multiple: all of them; single: the value, or every distinct
    value marked `conflict` (never picked). Repeats of a value never change the count."""
    distinct = _first_per_value(ok)
    if f.multiple or len(distinct) == 1:
        return distinct
    return [replace(c, status=Status.CONFLICT) for c in distinct]  # surface it, never pick one


def _merge(f: FieldDef, candidates: list[Fact], doc: ParsedDocument, doc_type: str) -> list[Fact]:
    """Fixed rules, so the same candidates always give the same facts."""
    candidates = sorted(candidates, key=_order)
    if f.checkbox:
        return _resolve_checkbox(f, candidates, doc, doc_type)
    ok = [c for c in candidates if c.status == Status.OK]
    if ok:
        return _merge_ok(f, ok, doc, doc_type)
    if candidates:  # only invalid/unverified values: keep them, flagged
        return candidates
    return [_empty(f, doc, doc_type, Status.MISSING)]


def _items(val: Any) -> list[dict[str, Any]]:
    """All value items for one field: a list, or one item plus its `other_statements`."""
    if isinstance(val, list):
        return [v for v in val if isinstance(v, dict)]
    if isinstance(val, dict):
        others = val.get("other_statements") or []
        return [val, *(o for o in others if isinstance(o, dict))]
    return []


def verify_and_merge(
    schema: DocTypeSchema, doc: ParsedDocument, outputs: list[dict[str, Any]]
) -> list[Fact]:
    pages_by_num = {p.number: PageLines.of(p.text) for p in doc.pages}
    facts: list[Fact] = []
    for f in schema.fields:
        cands: list[Fact] = []
        for out in outputs:
            for item in _items(out.get(f.key)):
                fact = _check_item(f, item, doc, schema.doc_type, pages_by_num)
                if fact:
                    cands.append(fact)
        facts.extend(_merge(f, cands, doc, schema.doc_type))
    return facts


# ---------------------------------------------------------------- subject join (D13, D13a)

SUBJECT_JOIN_TARGET = "adsl.subjid"


def normalize_subject_id(raw: str) -> str:
    """Upper case; any run of non-alphanumerics becomes one '-'; no leading/trailing '-'."""
    text = unicodedata.normalize("NFKC", raw).upper()
    return re.sub(r"[^0-9A-Z]+", "-", text).strip("-")


def match_subject(raw: str, subjids: list[str]) -> tuple[JoinStatus, str | None]:
    """Exact match, or the SUBJID as a suffix after a separator. No fuzzy matching (D13).

    "101-01001-004" and "101 / 01001_004" both match "01001-004"; "1-004" does not, because
    the match must start at a separator boundary of the extracted id.
    """
    wanted = normalize_subject_id(raw)
    matches = sorted(
        {
            s
            for s in subjids
            if (n := normalize_subject_id(s)) and (wanted == n or wanted.endswith("-" + n))
        }
    )
    if len(matches) == 1:
        return JoinStatus.JOINED, matches[0]
    return (JoinStatus.AMBIGUOUS if matches else JoinStatus.UNMATCHED), None


def apply_subject_join(schema: DocTypeSchema, facts: list[Fact], subjids: list[str]) -> list[Fact]:
    """Set join_status on subject id facts. Joins only a verified (`ok`) id (D13a):
    conflict -> ambiguous; unverified/invalid/missing -> not_attempted (reason: fact status).
    """
    join_keys = {f.key for f in schema.fields if f.join_target == SUBJECT_JOIN_TARGET}
    out = []
    for fact in facts:
        if fact.field_key not in join_keys:
            out.append(fact)
        elif fact.status == Status.OK and fact.value_text is not None:
            status, subjid = match_subject(fact.value_text, subjids)
            out.append(replace(fact, join_status=status, join_value=subjid))
        elif fact.status == Status.CONFLICT:
            out.append(replace(fact, join_status=JoinStatus.AMBIGUOUS))
        else:
            out.append(replace(fact, join_status=JoinStatus.NOT_ATTEMPTED))
    return out


# ---------------------------------------------------------------- LLM call


def make_client(endpoint: str, api_version: str) -> AsyncAzureOpenAI:
    """Entra ID auth via managed identity / az login: no API keys in code.

    `max_retries=0`: retries are ours (`call_with_retries`), so they are logged and bounded.
    """
    from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
    from openai import AsyncAzureOpenAI

    provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        api_version=api_version,
        azure_ad_token_provider=provider,
        max_retries=0,
    )


@dataclass(frozen=True)
class RetryPolicy:
    """Attempts (including the first), backoff with full jitter, and a per-call timeout."""

    max_attempts: int = 4
    base_seconds: float = 1.0
    max_seconds: float = 20.0
    timeout_seconds: float = 60.0

    @staticmethod
    def from_settings(settings: Settings) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=settings.llm_max_attempts,
            base_seconds=settings.llm_backoff_base_seconds,
            max_seconds=settings.llm_backoff_max_seconds,
            timeout_seconds=settings.llm_timeout_seconds,
        )

    def delay(self, retry: int, rng: random.Random) -> float:
        """Full jitter: uniform(0, min(cap, base * 2**retry)). Timing only, never output."""
        return rng.uniform(0, min(self.max_seconds, self.base_seconds * 2**retry))


Sleep = Callable[[float], Awaitable[None]]


def _retryable(exc: Exception) -> tuple[bool, int | None]:
    """(retry?, http status). 429, 5xx, timeouts and connection errors are transient."""
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500, exc.status_code
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return True, None
    return False, None


async def call_with_retries(
    client: AsyncAzureOpenAI,
    request: dict[str, Any],
    policy: RetryPolicy,
    *,
    doc_sha256: str,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> Any:
    """One chat completion with bounded retries. Logs metadata only; raises ExtractionError.

    The ExtractionError is raised outside the `except` block so it carries no chained SDK
    exception (whose message or body is not ours to vouch for).
    """
    rng = rng or random.Random()
    http_status: int | None = None
    for attempt in range(1, policy.max_attempts + 1):
        start = time.perf_counter()
        try:
            resp = await client.chat.completions.create(**request, timeout=policy.timeout_seconds)
        except Exception as exc:  # classified below; anything else is re-raised as-is
            retry, http_status = _retryable(exc)
            if not retry and not isinstance(exc, openai.APIError):
                raise
            _log_call(doc_sha256, attempt, start, http_status, "retry" if retry else "error")
        else:
            usage = getattr(resp, "usage", None)
            _log_call(doc_sha256, attempt, start, 200, "ok", usage)
            return resp
        if not retry:
            raise ExtractionError(f"model call rejected (http {http_status})")
        if attempt < policy.max_attempts:
            await sleep(policy.delay(attempt - 1, rng))
    raise ExtractionError(
        f"model call failed after {policy.max_attempts} attempts (http {http_status})"
    )


def _log_call(
    doc_sha256: str,
    attempt: int,
    start: float,
    http_status: int | None,
    status: str,
    usage: Any = None,
) -> None:
    log.info(
        "llm_call",
        extra={
            "doc_sha256": doc_sha256,
            "prompt_version": PROMPT_VERSION,
            "attempt": attempt,
            "http_status": http_status,
            "status": status,
            "duration_ms": round((time.perf_counter() - start) * 1000),
            "tokens_in": getattr(usage, "prompt_tokens", None),
            "tokens_out": getattr(usage, "completion_tokens", None),
        },
    )


def _parse_output(resp: Any) -> dict[str, Any]:
    """The JSON object from a finished, non-refused completion; ExtractionError otherwise.

    Raised outside `except`: a JSONDecodeError keeps the whole model output in `.doc`.
    """
    choice = resp.choices[0]
    if choice.finish_reason != "stop" or getattr(choice.message, "refusal", None):
        raise ExtractionError(f"model output unusable (finish_reason={choice.finish_reason})")
    parsed: Any = None
    try:
        parsed = json.loads(choice.message.content or "")
    except json.JSONDecodeError:
        pass
    if not isinstance(parsed, dict):
        raise ExtractionError("model output is not a JSON object")
    return parsed


def build_request(
    schema: DocTypeSchema,
    json_schema: dict[str, Any],
    pages: tuple[ParsedPage, ...],
    *,
    deployment: str,
    seed: int,
    max_completion_tokens: int,
) -> dict[str, Any]:
    """The chat completion request for one window: rule 3 settings, stateless (rule 6)."""
    user = user_message(schema.doc_type, pages)
    return {
        "model": deployment,  # pinned model version behind this deployment
        "temperature": 0,
        "seed": seed,
        "max_completion_tokens": max_completion_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "strict": True, "schema": json_schema},
        },
    }


@dataclass(frozen=True)
class ExtractionResult:
    facts: list[Fact]
    fingerprint: str
    system_fingerprints: tuple[str | None, ...]  # to detect backend model changes


def fingerprint(doc: ParsedDocument, schema: DocTypeSchema, deployment: str, seed: int) -> str:
    """Everything that should make the output identical. No document content inside."""
    parts = [
        doc.sha256,
        doc.parser_id,
        schema.tenant_id,
        schema.doc_type,
        schema.schema_hash,
        deployment,
        PROMPT_VERSION,
        RULES_VERSION,
        str(seed),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


async def extract_document(
    client: AsyncAzureOpenAI,
    doc: ParsedDocument,
    schema: DocTypeSchema,
    subject_ids: list[str],
    *,
    settings: Settings,
    sleep: Sleep = asyncio.sleep,
) -> ExtractionResult:
    """Extract, verify, merge and join one document. Windows run sequentially (stable order).

    `subject_ids` are the tenant's `adsl.subjid` values, used only by the code-side join.
    """
    json_schema = build_json_schema(schema)
    policy = RetryPolicy.from_settings(settings)
    outputs, fps = [], []
    for window in _windows(doc.pages, settings.extraction_max_chars):
        request = build_request(
            schema,
            json_schema,
            window,
            deployment=settings.azure_openai_deployment,
            seed=settings.model_seed,
            max_completion_tokens=settings.extraction_max_completion_tokens,
        )
        resp = await call_with_retries(client, request, policy, doc_sha256=doc.sha256, sleep=sleep)
        outputs.append(_parse_output(resp))
        fps.append(getattr(resp, "system_fingerprint", None))
    facts = apply_subject_join(schema, verify_and_merge(schema, doc, outputs), subject_ids)
    log.info(
        "extraction_done",
        extra={"doc_sha256": doc.sha256, "prompt_version": PROMPT_VERSION, "status": "ok"},
    )
    return ExtractionResult(
        facts=facts,
        fingerprint=fingerprint(doc, schema, settings.azure_openai_deployment, settings.model_seed),
        system_fingerprints=tuple(fps),
    )


async def extract_for_tenant(
    client: AsyncAzureOpenAI,
    repo: TenantRepository,
    doc: ParsedDocument,
    doc_type: str,
    *,
    settings: Settings,
) -> ExtractionResult:
    """Load the tenant's field metadata and subject ids (repo is bound to the session's
    tenant, rule 5), then extract."""
    schema = await repo.field_schema(doc_type)
    needs_join = any(f.join_target == SUBJECT_JOIN_TARGET for f in schema.fields)
    subjids = await repo.subject_ids() if needs_join else []
    return await extract_document(client, doc, schema, subjids, settings=settings)


# ---------------------------------------------------------------- DuckDB

FACTS_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    doc_sha VARCHAR, doc_type VARCHAR, field_key VARCHAR,
    value_text VARCHAR, value_num DECIMAL(18, 6), value_date DATE, value_bool BOOLEAN,
    unit VARCHAR, page INTEGER, quote VARCHAR, status VARCHAR,
    join_status VARCHAR, join_value VARCHAR
)"""


def load_facts(con: duckdb.DuckDBPyConnection, facts: list[Fact]) -> None:
    """con must be the session's in-memory connection (duckdb.connect(':memory:'))."""
    con.execute(FACTS_DDL)
    if facts:
        con.executemany(
            "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [astuple(f) for f in facts],
        )
