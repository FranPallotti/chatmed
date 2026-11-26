"""D6/D14: the agent prompt is versioned, and the version is enforced.

`AGENT_PROMPT_VERSION` is part of the answer fingerprint. What the model is shown is the
system prompt, the session context (built by `session_context`), the tool schemas and the
answer schema. This test hashes all four and fails unless the hash is the one recorded for
the current `AGENT_PROMPT_VERSION`. So a prompt change cannot ship under an old version:
bump `AGENT_PROMPT_VERSION` in `chatmed.agent.loop` and add its hash below.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import sys

from chatmed.agent import loop
from chatmed.agent.answer import ANSWER_SCHEMA
from chatmed.agent.tools import TOOL_SCHEMAS
from tests.guardrails.test_rules_version import PYTHON, _without_docstrings

# One entry per AGENT_PROMPT_VERSION ever released. Never edit an existing hash: bump.
PROMPT_HASHES = {
    "agent-v1": "2b8e34452cb1020fbd99d20bec947a56ae116d7e9c364ac7ec8520c7c02fff61",
    # agent-v2: status names checked verbatim (fixes first-attempt status_not_stated),
    # reconciliation answered from reconcile_case only.
    "agent-v2": "5cec9ce85cfef34fcf7223e969b37ed29a52e9c8f7d665bdac96433f51ffb81d",
}


def prompt_hash() -> str:
    context_code = ast.parse(inspect.getsource(loop.session_context))
    parts = [
        loop.SYSTEM_PROMPT,
        ast.dump(_without_docstrings(context_code)),
        json.dumps(TOOL_SCHEMAS, sort_keys=True),
        json.dumps(ANSWER_SCHEMA, sort_keys=True),
    ]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def test_prompt_changes_come_with_a_version_bump() -> None:
    assert sys.version_info[:2] == PYTHON, "ast.dump differs between Python versions; use 3.12"
    assert loop.AGENT_PROMPT_VERSION in PROMPT_HASHES, (
        f"add PROMPT_HASHES[{loop.AGENT_PROMPT_VERSION!r}] = {prompt_hash()!r}"
    )
    assert prompt_hash() == PROMPT_HASHES[loop.AGENT_PROMPT_VERSION], (
        "the agent prompt changed: bump AGENT_PROMPT_VERSION in chatmed.agent.loop, "
        f"then add its hash here: {prompt_hash()!r}"
    )


def test_every_prompt_version_has_its_own_hash() -> None:
    assert len(set(PROMPT_HASHES.values())) == len(PROMPT_HASHES)
