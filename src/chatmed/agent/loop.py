"""Code-first agent loop over stateless chat completions (D4, M3 AC2).

Every call carries the full message list (history lives in the session, rule 6), temperature
0, the fixed seed and the pinned deployment (rule 3), the agent prompt version, strict tools
and a strict answer schema. What the model is shown lives in `chatmed.agent.prompt`.
`parallel_tool_calls=False` keeps tool calls in a stable order.
The loop stops after `AGENT_MAX_ITERATIONS` model calls, or when the question's time limit
(`AGENT_QUESTION_TIMEOUT_SECONDS`, over all model calls, retries and tools) runs out, and then
returns the safe fallback (reason `max_iterations` or `deadline`).
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

from chatmed.agent import prompt
from chatmed.agent.answer import Answer, AnswerRejected, fallback_answer, verify
from chatmed.agent.prompt import (
    ANSWER_SCHEMA,
    SYSTEM_PROMPT,
    TOOL_SCHEMAS,
    retry_note,
    session_context,
)
from chatmed.agent.tools import ToolContext, dispatch
from chatmed.config import fingerprint_settings
from chatmed.extraction import RULES_VERSION, RetryPolicy, _retryable

if TYPE_CHECKING:
    from openai import AsyncAzureOpenAI

    from chatmed.config import Settings
    from chatmed.session import Session

log = logging.getLogger(__name__)


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def answer_fingerprint(
    ctx: ToolContext, settings: Settings, *, tenant_id: str, history: list[dict[str, Any]]
) -> str:
    """Everything that should make the facts of a verified answer identical (D6, D14).

    - the agent prompt version, the rules version and every fingerprint setting by value
      (model endpoint, deployment, API version, seed, window and retrieval sizes, parser
      settings, tenant DB);
    - the tenant, also for a session without documents;
    - the extraction fingerprint of every document in the session (document sha256, parser
      id, tenant schema hash, extraction prompt version);
    - a digest of the session history sent with the question, because earlier turns steer
      which tools the model calls;
    - a digest of each tenant DB read the tools made (keyed by call and arguments), because
      tenant study data can change between sessions while its schema stays the same.
    Operational settings (limits, timeouts, retries) are not in it: they only decide whether an
    answer is verified. Only sha256 digests of history and tenant data, never the data.
    """
    parts = [
        prompt.AGENT_PROMPT_VERSION,
        RULES_VERSION,
        *fingerprint_settings(settings),
        f"tenant={tenant_id}",
        *sorted(d.fingerprint for d in ctx.documents),
        f"history={_digest(history)}",
        *(f"read={call}:{digest}" for call, digest in sorted(ctx.tenant_reads.items())),
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
            "prompt_version": prompt.AGENT_PROMPT_VERSION,
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


async def _answer_loop(
    client: AsyncAzureOpenAI,
    session: Session,
    ctx: ToolContext,
    question: str,
    settings: Settings,
    on_event: ProgressCallback,
    sleep: Sleep,
    rng: random.Random,
) -> Answer | None:
    """Model calls and tool calls until a verified answer, the second rejection (fallback) or
    `AGENT_MAX_ITERATIONS` (None). Runs inside the question's time limit (see run_agent)."""
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
            messages.append({"role": "user", "content": retry_note(exc.code)})
    return answer


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
    start = time.perf_counter()
    deadline = asyncio.get_running_loop().time() + settings.agent_question_timeout_seconds
    limit = asyncio.timeout_at(deadline)
    try:
        async with limit:
            answer = await _answer_loop(
                client, session, ctx, question, settings, on_event, sleep, rng
            )
    except TimeoutError:
        if not limit.expired():  # not our deadline: some other timeout, let it surface
            raise
        log.info(
            "agent.deadline",
            extra={
                "session_id": session.id,
                "duration_ms": round((time.perf_counter() - start) * 1000),
            },
        )
        answer = fallback_answer(ctx, "deadline")
    if answer is None:  # every model call counts, rejected answers included
        answer = fallback_answer(ctx, "max_iterations")
    if answer.verified:  # only a verified answer claims reproducibility (D14)
        fingerprint = answer_fingerprint(
            ctx, settings, tenant_id=session.tenant_id, history=session.history
        )
        answer = replace(answer, fingerprint=fingerprint)
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
