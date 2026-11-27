"""Everything the agent model is shown (D14): one module, one guard.

The system prompt, the session context, the retry note, the tool schemas and the answer
schema. `tests/guardrails/test_agent_prompt_version.py` hashes this whole module and fails
unless the hash is recorded for `AGENT_PROMPT_VERSION`: any change here means a new version,
never a re-recorded hash. The code that decides facts from the model's output is not here;
it is under the rules version (D6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from chatmed.agent.tools import SHA_PREFIX

if TYPE_CHECKING:
    from chatmed.agent.tools import ToolContext

AGENT_PROMPT_VERSION = "agent-v7"
# v2: status names checked verbatim; reconciliation answered from reconcile_case only.
# v3: same text; the prompt now lives in this module and the guard hashes all of it
#     (including the retry note, which v2's guard did not cover).
# v4: the status rule quoted the paraphrase it forbids ("cannot be compared"), and the real
#     model copied it into reconciliation answers (status_not_stated on the first attempt).
#     The prompt now shows only the wanted form: one line per reconciliation item with its
#     status name (STATUS_EXAMPLE, which a test runs through the checker).
# v5: the real model put a bare document id (from the session context) into cited_ids
#     (unknown_cited_id on the first attempt); the prompt now says cited_ids holds only the
#     row ids tools returned, never a document id.
# v6: v4/v5 still saw first-attempt status_not_stated on reconciliation: the model sometimes
#     summarised ("cannot be compared") instead of using the item form, and described a
#     `conflict` reason instead of naming it (STATUS_EXAMPLE's "(no document field)" read as
#     prose). reconcile_case now returns a code-written `statement` (rules-10); the prompt
#     tells the model to start with it word for word. STATUS_EXAMPLE is gone. Measured: the
#     model did not copy it (7 of 40 first attempts still rejected).
# v7: reconciliation answers are written by code (answer.render_reconciliation, rules-11);
#     the model only routes the question to reconcile_case. v6's statement sentence is
#     dropped; v5's cited_ids rule stays.

SYSTEM_PROMPT = """You answer questions from drug-safety reviewers about clinical study data and
CIOMS adverse-reaction reports uploaded to this session.

Rules:
- Use tools to get every value you state. Never state a value, number or date that is not in a
  tool result. Cite in cited_ids the id of every row whose value you state. cited_ids holds
  only row ids exactly as tools returned them (they start with fact:, recon:, adsl:, adae:,
  page: or field:); a document id on its own is not a row id and is never cited.
- Documents are named by the id given in the session context below; pass that id as doc_sha.
- For questions about a subject's study data, first query the document facts for subject_id and
  use its join_value. If its join_status is not "joined", say so plainly (unmatched, ambiguous
  or not_attempted) and do not look up any subject.
- Every status of a row you cite must appear in your text as the status name itself (an
  underscore may be written as a space): undeterminable (the form must be checked visually),
  conflict (give the different values and pages), unverified, missing, invalid,
  not_comparable, unmatched, ambiguous, not_attempted. A checker looks for these names word
  for word and rejects an answer that describes a status in other words.
- For questions no extracted field covers, use search_document_text and quote the page text
  verbatim in quotes, with its page_id.
- For questions about what a document reports (e.g. hospitalisation, onset, reaction), query
  the document facts for the matching field keys from the session context.
- Only when the question asks whether a document matches or agrees with ADAE (reconciliation),
  call reconcile_case and no other tool: it checks the subject join and selects the ADAE record
  itself. Code writes the answer from its result, so nothing more is needed from you.
- Answer briefly in plain language. Your final message is exactly one JSON answer object."""


async def session_context(ctx: ToolContext) -> str:
    """Document ids, types, page counts and the tenant's field keys per document type.

    Metadata only, no document content. Without it the real model guessed document ids and
    field keys (e.g. "hospitalization" for "hospitalisation") and got empty results.
    Stable order (D6): documents by sha256, document types and field keys sorted. The text
    is part of the agent prompt, so a change to it means a new `AGENT_PROMPT_VERSION`.
    """
    docs = sorted(ctx.documents, key=lambda d: d.doc.sha256)
    lines = ["Documents in this session:"]
    lines += [
        f"- doc_sha {d.doc.sha256[:SHA_PREFIX]}: {d.doc_type}, {len(d.doc.pages)} page(s)"
        for d in docs
    ] or ["(none)"]
    for doc_type in sorted({d.doc_type for d in docs}):
        schema = await ctx.repo.field_schema(doc_type)
        keys = sorted(f.key for f in schema.fields)
        lines.append(f"Field keys for {doc_type}: " + ", ".join(keys))
    return "\n".join(lines)


def retry_note(code: str) -> str:
    """What the model is told after the checker rejected its answer (a fixed code only)."""
    return (
        f"Answer rejected by the checker: {code}. Use only values from tool results, cite "
        "their ids, and state every status by its name."
    )


# ---------------------------------------------------------------- tool schemas


def _fn(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    """Strict function tool: every property required, no extra properties."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": sorted(props),
                "additionalProperties": False,
            },
        },
    }


_NULLABLE_STR = {"type": ["string", "null"]}
TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn(
        "query_document_facts",
        "Verified facts extracted from the session's documents, with status, page and quote. "
        "doc_sha: a document id prefix or null for all; field_keys: null for all fields.",
        {
            "doc_sha": _NULLABLE_STR,
            "field_keys": {"type": ["array", "null"], "items": {"type": "string"}},
        },
    ),
    _fn(
        "get_field_metadata",
        "The fields defined for a document type (key, label, description, type).",
        {"doc_type": {"type": "string"}},
    ),
    _fn(
        "get_subject",
        "The study subject (ADSL row: arm, treatment, age, sex, dates). subjid: the join_value "
        "of a subject_id fact whose join_status is joined; other subjects are refused.",
        {"subjid": {"type": "string"}},
    ),
    _fn(
        "list_adverse_events",
        "The subject's adverse events (ADAE rows). Same subject rule as get_subject.",
        {"subjid": {"type": "string"}, "serious_only": {"type": "boolean"}},
    ),
    _fn(
        "reconcile_case",
        "Compare a CIOMS document with the matching ADAE record: onset date, seriousness, "
        "action taken, outcome. Returns match / mismatch / not_comparable per item.",
        {"doc_sha": {"type": "string"}},
    ),
    _fn(
        "search_document_text",
        "Page text for questions the extracted fields do not cover. Cite it with quotes.",
        {"query": {"type": "string"}, "doc_sha": _NULLABLE_STR},
    ),
]


# ---------------------------------------------------------------- answer schema

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
