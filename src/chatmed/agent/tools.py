"""Agent tools (M3 AC1). The tenant comes from the session, never from a tool argument (rule 5).

Each tool returns rows with a stable `id`. Every row is also kept in an `Evidence` registry for
the current question, so the final answer can cite ids and code can rebuild `facts_used` from
what the tools really returned (rule 4): values never come from the model's text.

Errors go back to the model as short codes (`ToolError.code`), never with document text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from chatmed.agent.reconcile import Reconciliation, reconcile, subject_is_joined
from chatmed.agent.retrieval import relevant_pages
from chatmed.extraction import Fact
from chatmed.tenant_db import TenantRepository, UnknownDocumentType

if TYPE_CHECKING:
    import duckdb

    from chatmed.session import StoredDocument

SHA_PREFIX = 12  # how documents are named to the model and in citations


class ToolError(Exception):
    """A short, fixed error code for the model. Never carries document text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EvidenceItem:
    """A row a tool returned: what an answer may cite (`facts_used`)."""

    id: str
    source: str  # document | adsl | adae | field_definitions | reconciliation | page_text
    values: dict[str, Any]
    citation: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "values": self.values,
            "citation": self.citation,
        }


@dataclass
class ToolContext:
    """What tools may touch: this session's facts and documents, and the tenant-bound repo."""

    facts_db: duckdb.DuckDBPyConnection
    documents: list[StoredDocument]
    repo: TenantRepository
    fulltext_max_chars: int
    bm25_top_k: int
    evidence: dict[str, EvidenceItem] = field(default_factory=dict)
    pages: dict[str, tuple[str, int, str]] = field(default_factory=dict)  # page id -> sha, n, text


# ---------------------------------------------------------------- helpers


def _plain(value: Any) -> Any:
    """JSON-safe, deterministic form of DB values (dates ISO, decimals as strings)."""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return value


def _register(ctx: ToolContext, item: EvidenceItem) -> dict[str, Any]:
    ctx.evidence[item.id] = item
    return {"id": item.id, **item.values}


FACT_COLUMNS = (
    "doc_sha, doc_type, field_key, value_text, value_num, value_date, value_bool, unit, page, "
    "quote, status, join_status, join_value"
)
FACTS_SQL = f"""
SELECT {FACT_COLUMNS} FROM facts
WHERE (? IS NULL OR doc_sha = ?) AND (? IS NULL OR list_contains(?, field_key))
ORDER BY doc_sha, field_key, page NULLS FIRST, value_text NULLS FIRST, quote NULLS FIRST, status
"""


def _facts(ctx: ToolContext, doc_sha: str | None, keys: list[str] | None) -> list[Fact]:
    """Read-only, parameterized query over the session's `facts` table."""
    rows = ctx.facts_db.execute(FACTS_SQL, [doc_sha, doc_sha, keys, keys]).fetchall()
    return [Fact(*row) for row in rows]


def _fact_items(facts: list[Fact]) -> list[EvidenceItem]:
    """Ids `fact:<sha12>:<field>:<n>`, numbered in the stable order of `FACTS_SQL`."""
    counters: dict[tuple[str, str], int] = {}
    items = []
    for f in facts:
        key = (f.doc_sha, f.field_key)
        counters[key] = counters.get(key, 0) + 1
        sha = f.doc_sha[:SHA_PREFIX]
        value = f.value_text
        items.append(
            EvidenceItem(
                id=f"fact:{sha}:{f.field_key}:{counters[key]}",
                source="document",
                values={
                    "doc": sha,
                    "doc_type": f.doc_type,
                    "field": f.field_key,
                    "value": value,
                    "unit": f.unit,
                    "status": f.status,
                    "join_status": f.join_status,
                    "join_value": f.join_value,
                },
                citation={"doc": sha, "page": f.page, "quote": f.quote},
            )
        )
    return items


def resolve_doc(ctx: ToolContext, doc_sha: str) -> StoredDocument:
    """A session document by sha256 or by a unique prefix of at least 8 characters."""
    prefix = doc_sha.strip().lower()
    matches = [d for d in ctx.documents if len(prefix) >= 8 and d.doc.sha256.startswith(prefix)]
    if len(matches) != 1:
        raise ToolError("unknown_document")
    return matches[0]


JOINED_SQL = """
SELECT DISTINCT value_text, join_value FROM facts
WHERE join_status = 'joined' AND join_value IS NOT NULL
"""


def _require_joined(ctx: ToolContext, subjid: str) -> str:
    """Study data only for a subject that a document in this session joined to (D13, AC4).

    Accepts the `join_value` (adsl.subjid) or the subject id exactly as printed on a joined
    document fact, and maps the latter to that fact's `join_value`. No other matching: anything
    else, or a subject that is not joined, is refused before the tenant DB is touched.
    """
    wanted = subjid.strip()
    joined = ctx.facts_db.execute(JOINED_SQL).fetchall()
    matches = {join for raw, join in joined if wanted and wanted in (raw, join)}
    if len(matches) != 1:
        raise ToolError("subject_not_joined")
    return matches.pop()


# ---------------------------------------------------------------- tools


async def query_document_facts(
    ctx: ToolContext, doc_sha: str | None, field_keys: list[str] | None
) -> dict[str, Any]:
    sha = resolve_doc(ctx, doc_sha).doc.sha256 if doc_sha else None
    keys = sorted(set(field_keys)) if field_keys else None
    rows = [_register(ctx, item) for item in _fact_items(_facts(ctx, sha, keys))]
    return {"rows": rows}


async def get_field_metadata(ctx: ToolContext, doc_type: str) -> dict[str, Any]:
    try:
        schema = await ctx.repo.field_schema(doc_type)
    except UnknownDocumentType:
        raise ToolError("unknown_document_type") from None
    rows = []
    for f in schema.fields:
        item = EvidenceItem(
            id=f"field:{doc_type}:{f.key}",
            source="field_definitions",
            values={
                "key": f.key,
                "label": f.label,
                "description": f.description,
                "data_type": str(f.data_type),
                "multiple": f.multiple,
                "checkbox": f.checkbox,
                "allowed_values": list(f.allowed_values) if f.allowed_values else None,
            },
            citation={"table": "field_definitions", "key": f"{doc_type}.{f.key}"},
        )
        rows.append(_register(ctx, item))
    return {"rows": rows}


def _adsl_item(row: dict[str, Any]) -> EvidenceItem:
    values = {k: _plain(v) for k, v in sorted(row.items())}
    return EvidenceItem(
        f"adsl:{row['subjid']}", "adsl", values, {"table": "adsl", "key": row["subjid"]}
    )


def _adae_item(row: dict[str, Any]) -> EvidenceItem:
    key = f"{row['usubjid']}:{row['aeseq']}"
    values = {k: _plain(v) for k, v in sorted(row.items())}
    return EvidenceItem(f"adae:{key}", "adae", values, {"table": "adae", "key": key})


async def get_subject(ctx: ToolContext, subjid: str) -> dict[str, Any]:
    row = await ctx.repo.subject(_require_joined(ctx, subjid))
    if row is None:
        raise ToolError("subject_not_found")
    return {"rows": [_register(ctx, _adsl_item(row))]}


async def list_adverse_events(ctx: ToolContext, subjid: str, serious_only: bool) -> dict[str, Any]:
    rows = await ctx.repo.adverse_events(_require_joined(ctx, subjid), serious_only)
    return {"rows": [_register(ctx, _adae_item(r)) for r in rows]}


def _recon_items(sha: str, result: Reconciliation) -> list[EvidenceItem]:
    record_key = f"{result.record['usubjid']}:{result.record['aeseq']}" if result.record else None
    items = []
    for it in result.items:
        items.append(
            EvidenceItem(
                id=f"recon:{sha[:SHA_PREFIX]}:{it.item}",
                source="reconciliation",
                values={
                    "item": it.item,
                    "status": it.status,
                    "reason": it.reason,
                    "document_value": it.document_value,
                    "record_value": it.record_value,
                    "day_difference": it.day_difference,
                    "record_selection": result.tier,
                },
                citation={"doc": sha[:SHA_PREFIX], "table": "adae", "key": record_key},
            )
        )
    return items


async def reconcile_case(ctx: ToolContext, doc_sha: str) -> dict[str, Any]:
    sha = resolve_doc(ctx, doc_sha).doc.sha256
    facts = _facts(ctx, sha, None)
    subjid = subject_is_joined(facts)
    ae_rows = await ctx.repo.adverse_events(subjid, False) if subjid else []
    result = reconcile(facts, ae_rows)
    # The joined subject_id fact the reconciliation rests on, with the same id that
    # query_document_facts gives it (numbered before filtering): always part of the
    # reconciliation unit in facts_used, whether or not the model cites it (D14).
    subject = [
        item
        for item in _fact_items([f for f in facts if f.field_key == "subject_id"])
        if item.values["join_status"] == "joined"
    ]
    rows = [_register(ctx, item) for item in subject]
    rows += [_register(ctx, item) for item in _recon_items(sha, result)]
    if result.record is not None:
        rows.append(_register(ctx, _adae_item(result.record)))
    return {"rows": rows}


async def search_document_text(ctx: ToolContext, query: str, doc_sha: str | None) -> dict[str, Any]:
    """Fallback for questions no field covers. Quotes from these pages are grounded later."""
    docs = [resolve_doc(ctx, doc_sha)] if doc_sha else ctx.documents
    hits = relevant_pages(
        query,
        [d.doc for d in docs],
        fulltext_max_chars=ctx.fulltext_max_chars,
        top_k=ctx.bm25_top_k,
    )
    pages = []
    for hit in hits:
        page_id = f"page:{hit.doc_sha[:SHA_PREFIX]}:{hit.page}"
        ctx.pages[page_id] = (hit.doc_sha, hit.page, hit.text)
        pages.append({"page_id": page_id, "text": hit.text})
    return {"pages": pages}


# ---------------------------------------------------------------- dispatch
# (the tool schemas the model sees are in chatmed.agent.prompt)


async def dispatch(ctx: ToolContext, name: str, arguments: str) -> dict[str, Any]:
    """Run one tool call. Bad input or a failing tool becomes `{"error": code}`."""
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"error": "bad_arguments"}
    if not isinstance(args, dict):
        return {"error": "bad_arguments"}
    try:
        match name:
            case "query_document_facts":
                return await query_document_facts(ctx, args.get("doc_sha"), args.get("field_keys"))
            case "get_field_metadata":
                return await get_field_metadata(ctx, str(args["doc_type"]))
            case "get_subject":
                return await get_subject(ctx, str(args["subjid"]))
            case "list_adverse_events":
                return await list_adverse_events(
                    ctx, str(args["subjid"]), bool(args.get("serious_only"))
                )
            case "reconcile_case":
                return await reconcile_case(ctx, str(args["doc_sha"]))
            case "search_document_text":
                return await search_document_text(ctx, str(args["query"]), args.get("doc_sha"))
            case _:
                return {"error": "unknown_tool"}
    except ToolError as exc:
        return {"error": exc.code}
    except (KeyError, TypeError):
        return {"error": "bad_arguments"}
