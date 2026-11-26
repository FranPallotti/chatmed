"""Code-first agent loop over stateless chat completions (D4, M3 AC2).

Every call carries the full message list (history lives in the session, rule 6), temperature
0, the fixed seed and the pinned deployment (rule 3), the agent prompt version, strict tools
and a strict answer schema. `parallel_tool_calls=False` keeps tool calls in a stable order.
The loop stops after `AGENT_MAX_ITERATIONS` model calls and then returns the safe fallback.
Logs carry only metadata (rule 2): event names, session id, tool names, counts and timings.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import openai

from chatmed.agent.answer import ANSWER_SCHEMA, Answer, AnswerRejected, fallback_answer, verify
from chatmed.agent.tools import SHA_PREFIX, TOOL_SCHEMAS, ToolContext, dispatch
from chatmed.extraction import RULES_VERSION, RetryPolicy, _retryable

if TYPE_CHECKING:
    from openai import AsyncAzureOpenAI

    from chatmed.config import Settings
    from chatmed.session import Session

log = logging.getLogger(__name__)

AGENT_PROMPT_VERSION = "agent-v2"  # v2: status names are checked verbatim; reconciliation

SYSTEM_PROMPT = """You answer questions from drug-safety reviewers about clinical study data and
CIOMS adverse-reaction reports uploaded to this session.

Rules:
- Use tools to get every value you state. Never state a value, number or date that is not in a
  tool result. Cite in cited_ids the id of every row whose value you state.
- Documents are named by the id given in the session context below; pass that id as doc_sha.
- For questions about a subject's study data, first query the document facts for subject_id and
  use its join_value. If its join_status is not "joined", say so plainly (unmatched, ambiguous
  or not_attempted) and do not look up any subject.
- Every status of a row you cite must appear in your text by its exact name (an underscore
  may be written as a space): undeterminable (the form must be checked visually), conflict
  (give the different values and pages), unverified, missing, invalid, not_comparable,
  unmatched, ambiguous, not_attempted. A checker rejects answers that reword a status, e.g.
  "cannot be compared" instead of "not comparable".
- For questions no extracted field covers, use search_document_text and quote the page text
  verbatim in quotes, with its page_id.
- For questions about what a document reports (e.g. hospitalisation, onset, reaction), query
  the document facts for the matching field keys from the session context.
- Only when the question asks whether a document matches or agrees with ADAE (reconciliation),
  call reconcile_case and no other tool: it checks the subject join and selects the ADAE record
  itself. Answer from its rows and cite only them.
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


def answer_fingerprint(ctx: ToolContext, settings: Settings) -> str:
    """Everything that should make the facts of an answer identical (D6, D14).

    The agent prompt version, the rules version, the pinned deployment and seed, and the
    extraction fingerprint of every document in the session (which already holds the
    document sha256, parser, tenant schema hash and extraction prompt version). No content.
    """
    parts = [
        AGENT_PROMPT_VERSION,
        RULES_VERSION,
        settings.azure_openai_deployment,
        str(settings.model_seed),
        *sorted(d.fingerprint for d in ctx.documents),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]


class AgentError(RuntimeError):
    """The model could not be reached or returned unusable output. Carries a code only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def _noop(_event: dict[str, Any]) -> None:
    return None


def build_request(messages: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    """One stateless agent request with the rule 3 settings."""
    return {
        "model": settings.azure_openai_deployment,
        "temperature": 0,
        "seed": settings.model_seed,
        "max_completion_tokens": settings.agent_max_completion_tokens,
        "messages": messages,
        "tools": TOOL_SCHEMAS,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "strict": True, "schema": ANSWER_SCHEMA},
        },
    }


async def call_model(
    client: AsyncAzureOpenAI,
    request: dict[str, Any],
    policy: RetryPolicy,
    *,
    session_id: str,
    iteration: int,
    sleep: Sleep,
    rng: random.Random,
) -> Any:
    """One completion with the M2 retry policy (429/5xx/timeouts). Raises AgentError."""
    http_status: int | None = None
    for attempt in range(1, policy.max_attempts + 1):
        start = time.perf_counter()
        try:
            resp = await client.chat.completions.create(**request, timeout=policy.timeout_seconds)
        except Exception as exc:
            retry, http_status = _retryable(exc)
            if not retry and not isinstance(exc, openai.APIError):
                raise
            _log_call(
                session_id, iteration, attempt, start, http_status, "retry" if retry else "error"
            )
        else:
            _log_call(
                session_id, iteration, attempt, start, 200, "ok", getattr(resp, "usage", None)
            )
            return resp
        if not retry:
            raise AgentError("model_rejected")
        if attempt < policy.max_attempts:
            await sleep(policy.delay(attempt - 1, rng))
    raise AgentError("model_unavailable")


def _log_call(
    session_id: str,
    iteration: int,
    attempt: int,
    start: float,
    http_status: int | None,
    status: str,
    usage: Any = None,
) -> None:
    log.info(
        "agent.llm_call",
        extra={
            "session_id": session_id,
            "prompt_version": AGENT_PROMPT_VERSION,
            "iteration": iteration,
            "attempt": attempt,
            "http_status": http_status,
            "status": status,
            "duration_ms": round((time.perf_counter() - start) * 1000),
            "tokens_in": getattr(usage, "prompt_tokens", None),
            "tokens_out": getattr(usage, "completion_tokens", None),
        },
    )


def _assistant_tool_message(message: Any) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": message.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls
        ],
    }


async def _run_tool(ctx: ToolContext, session_id: str, name: str, arguments: str) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        result = await dispatch(ctx, name, arguments)
    except Exception:  # e.g. tenant DB down: the model gets a code, the log gets the type only
        log.warning(
            "agent.tool_failed", exc_info=True, extra={"session_id": session_id, "tool_name": name}
        )
        result = {"error": "tool_failed"}
    log.info(
        "agent.tool_call",
        extra={
            "session_id": session_id,
            "tool_name": name,
            "status": "error" if "error" in result else "ok",
            "duration_ms": round((time.perf_counter() - start) * 1000),
        },
    )
    return result


def _parse_answer(message: Any, finish_reason: str) -> dict[str, Any]:
    """The final JSON answer. Truncated or refused output is an error; anything else that is
    not exactly one JSON object (seen with the real model: two objects back to back) is a
    rejected answer, which gets the one retry."""
    if finish_reason != "stop" or getattr(message, "refusal", None):
        raise AgentError("model_output_unusable")
    parsed: Any = None
    try:
        parsed = json.loads(message.content or "")
    except json.JSONDecodeError:
        pass
    if not isinstance(parsed, dict):
        raise AnswerRejected("answer_not_one_json_object")
    return parsed


async def run_agent(
    client: AsyncAzureOpenAI,
    session: Session,
    ctx: ToolContext,
    question: str,
    *,
    settings: Settings,
    on_event: ProgressCallback = _noop,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> Answer:
    """Answer one question. Appends the question and the answer text to the session history."""
    rng = rng or random.Random()
    policy = RetryPolicy.from_settings(settings)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": await session_context(ctx)},
        *session.history,
        {"role": "user", "content": question},
    ]
    answer: Answer | None = None
    rejected_once = False
    for iteration in range(1, settings.agent_max_iterations + 1):
        request = build_request(list(messages), settings)  # a snapshot, as sent
        resp = await call_model(
            client,
            request,
            policy,
            session_id=session.id,
            iteration=iteration,
            sleep=sleep,
            rng=rng,
        )
        choice = resp.choices[0]
        message = choice.message
        if getattr(message, "tool_calls", None):
            messages.append(_assistant_tool_message(message))
            for call in message.tool_calls:
                await on_event(
                    {"event": "tool", "name": call.function.name, "iteration": iteration}
                )
                result = await _run_tool(
                    ctx, session.id, call.function.name, call.function.arguments
                )
                content = json.dumps(result, sort_keys=True, ensure_ascii=False)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            continue
        try:
            answer = verify(ctx, _parse_answer(message, choice.finish_reason))
            break
        except AnswerRejected as exc:
            # the rejected output itself is never logged (rule 2): its length and the code only
            log.info(
                "agent.answer_rejected",
                extra={
                    "session_id": session.id,
                    "iteration": iteration,
                    "status": exc.code,
                    "output_chars": len(message.content or ""),
                },
            )
            if rejected_once:
                answer = fallback_answer(ctx, exc.code)
                break
            rejected_once = True
            messages.append({"role": "assistant", "content": message.content})
            messages.append(
                {
                    "role": "user",
                    "content": f"Answer rejected by the checker: {exc.code}. Use only values "
                    "from tool results, cite their ids, and state every status by its name.",
                }
            )
    if answer is None:  # every model call counts, rejected answers included
        answer = fallback_answer(ctx, "max_iterations")
    answer = replace(answer, fingerprint=answer_fingerprint(ctx, settings))
    session.history.extend(
        [{"role": "user", "content": question}, {"role": "assistant", "content": answer.text}]
    )
    log.info(
        "agent.answer",
        extra={
            "session_id": session.id,
            "status": "verified" if answer.verified else (answer.reason or "fallback"),
            "doc_count": len(ctx.documents),
        },
    )
    return answer
