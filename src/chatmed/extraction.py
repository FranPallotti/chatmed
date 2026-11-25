"""Schema-driven extraction.

tenant field metadata -> strict JSON schema -> LLM (temperature 0, seed)
-> deterministic checks in code (types, evidence quote really on that page)
-> merge across page windows -> facts loaded into the session's in-memory DuckDB.

Nothing here writes document content to disk or logs.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, astuple, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from chatmed.models import ParsedDocument, ParsedPage

if TYPE_CHECKING:  # keep the pure parts importable/testable without the SDK
    import duckdb
    from openai import AsyncAzureOpenAI

PROMPT_VERSION = "extract-v1"


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


def _value_schema(f: FieldDef) -> dict[str, Any]:
    if f.data_type is DataType.ENUM:
        return {"type": ["string", "null"], "enum": [*f.allowed_values, None]}
    if f.data_type is DataType.BOOLEAN:
        return {"type": ["boolean", "null"]}
    if f.data_type is DataType.INTEGER:
        return {"type": ["integer", "null"]}
    # string, decimal, date come back as text and are parsed exactly in code
    return {"type": ["string", "null"]}


def _field_schema(f: FieldDef) -> dict[str, Any]:
    item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value", "evidence"],
        "properties": {
            "value": _value_schema(f),
            "evidence": {"anyOf": [_EVIDENCE, {"type": "null"}]},
        },
    }
    desc = f"{f.label}: {f.description}" + (f" (unit: {f.unit})" if f.unit else "")
    if f.multiple:
        return {"type": "array", "description": desc, "items": item}
    return {**item, "description": desc}


def build_json_schema(schema: DocTypeSchema) -> dict[str, Any]:
    """Strict-mode schema: every property required, no extra properties."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [f.key for f in schema.fields],
        "properties": {f.key: _field_schema(f) for f in schema.fields},
    }


# ---------------------------------------------------------------- prompt

SYSTEM_PROMPT = """You extract fields from medical documents.
Rules:
- Use only the page text provided. Never infer, guess or use outside knowledge.
- If a field is not explicitly present, return value null and evidence null.
- evidence.quote must be copied verbatim from the page (at most 25 words) and
  evidence.page must be the number of the page it was copied from.
- Dates: YYYY-MM-DD. Decimals: digits with '.' as decimal separator, no unit.
- For fields that can appear several times, return one item per occurrence."""


def _render_pages(pages: tuple[ParsedPage, ...]) -> str:
    return "\n\n".join(f"[Page {p.number}]\n{p.text}" for p in pages)


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
    status: str  # ok | missing | unverified | invalid | conflict


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
    pages_by_num: dict[int, str],
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
            status="invalid",
        )
    # Grounding check: the quote must really be on the page the model claims.
    grounded = (
        quote is not None
        and page is not None
        and page in pages_by_num
        and _norm(quote) in pages_by_num[page]
    )
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
        quote=quote,
        status="ok" if grounded else "unverified",
    )


def _merge(f: FieldDef, candidates: list[Fact], doc: ParsedDocument, doc_type: str) -> list[Fact]:
    """Fixed rules, so the same candidates always give the same facts."""
    candidates = sorted(candidates, key=lambda c: (c.page or 0, c.value_text or ""))
    ok = [c for c in candidates if c.status == "ok"]
    if f.multiple:
        seen: set[str] = set()
        out = []
        for c in ok:
            if (c.value_text or "") not in seen:
                seen.add(c.value_text or "")
                out.append(c)
        return out or _missing(f, doc, doc_type, candidates)
    distinct = sorted({c.value_text or "" for c in ok})
    if len(distinct) == 1:
        return [ok[0]]  # earliest page as evidence
    if len(distinct) > 1:  # surface the conflict instead of picking one
        return [Fact(**{**asdict(c), "status": "conflict"}) for c in ok]
    return _missing(f, doc, doc_type, candidates)


def _missing(f: FieldDef, doc: ParsedDocument, doc_type: str, candidates: list[Fact]) -> list[Fact]:
    if candidates:  # only invalid/unverified values: keep them, flagged
        return candidates
    return [
        Fact(doc.sha256, doc_type, f.key, None, None, None, None, f.unit, None, None, "missing")
    ]


def verify_and_merge(
    schema: DocTypeSchema, doc: ParsedDocument, outputs: list[dict[str, Any]]
) -> list[Fact]:
    pages_by_num = {p.number: _norm(p.text) for p in doc.pages}
    facts: list[Fact] = []
    for f in schema.fields:
        cands: list[Fact] = []
        for out in outputs:
            val = out.get(f.key)
            items = val if isinstance(val, list) else [val] if val else []
            for item in items:
                fact = _check_item(f, item, doc, schema.doc_type, pages_by_num)
                if fact:
                    cands.append(fact)
        facts.extend(_merge(f, cands, doc, schema.doc_type))
    return facts


# ---------------------------------------------------------------- LLM call


def make_client(endpoint: str, api_version: str) -> AsyncAzureOpenAI:
    """Entra ID auth via managed identity / az login: no API keys in code."""
    from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
    from openai import AsyncAzureOpenAI

    provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AsyncAzureOpenAI(
        azure_endpoint=endpoint, api_version=api_version, azure_ad_token_provider=provider
    )


async def _extract_window(
    client: AsyncAzureOpenAI,
    deployment: str,
    schema: DocTypeSchema,
    json_schema: dict[str, Any],
    pages: tuple[ParsedPage, ...],
    seed: int,
) -> tuple[dict[str, Any], str | None]:
    user = f"Document type: {schema.doc_type}\n\nPages:\n{_render_pages(pages)}"
    resp = await client.chat.completions.create(
        model=deployment,  # pinned model version behind this deployment
        temperature=0,  # note: reasoning models ignore/forbid temperature
        seed=seed,
        max_completion_tokens=4000,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "extraction", "strict": True, "schema": json_schema},
        },
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content), resp.system_fingerprint


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
        str(seed),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


async def extract_document(
    client: AsyncAzureOpenAI,
    deployment: str,
    doc: ParsedDocument,
    schema: DocTypeSchema,
    *,
    seed: int = 7,
    max_chars: int = 60_000,
) -> ExtractionResult:
    json_schema = build_json_schema(schema)
    outputs, fps = [], []
    for window in _windows(doc.pages, max_chars):  # sequential = stable order
        out, fp = await _extract_window(client, deployment, schema, json_schema, window, seed)
        outputs.append(out)
        fps.append(fp)
    return ExtractionResult(
        facts=verify_and_merge(schema, doc, outputs),
        fingerprint=fingerprint(doc, schema, deployment, seed),
        system_fingerprints=tuple(fps),
    )


# ---------------------------------------------------------------- DuckDB

FACTS_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    doc_sha VARCHAR, doc_type VARCHAR, field_key VARCHAR,
    value_text VARCHAR, value_num DECIMAL(18, 6), value_date DATE, value_bool BOOLEAN,
    unit VARCHAR, page INTEGER, quote VARCHAR, status VARCHAR
)"""


def load_facts(con: duckdb.DuckDBPyConnection, facts: list[Fact]) -> None:
    """con must be the session's in-memory connection (duckdb.connect(':memory:'))."""
    con.execute(FACTS_DDL)
    if facts:
        con.executemany(
            "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [astuple(f) for f in facts],
        )
