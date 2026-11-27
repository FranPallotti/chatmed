"""D6/D14: what the agent model is shown is versioned, and the version is enforced.

`AGENT_PROMPT_VERSION` is part of the answer fingerprint. Since agent-v3 everything the model
is shown (system prompt, session context, retry note, tool schemas, answer schema) lives in
`chatmed.agent.prompt`, and this test hashes that whole module's AST, like the rules guard.
It fails unless the hash is the one recorded for the current `AGENT_PROMPT_VERSION`. A
recorded hash is never edited or re-recorded: any change means a new version and a new entry.
"""

from __future__ import annotations

import sys

from chatmed.agent import prompt
from tests.guardrails.test_rules_version import PYTHON, modules_hash

PROMPT_MODULES = ("chatmed.agent.prompt",)

# One entry per AGENT_PROMPT_VERSION ever released. Never edit an existing hash: bump.
# agent-v1 and agent-v2 were hashed with the earlier method (prompt text, the session_context
# function and the two schemas, then in loop/tools/answer); kept as history.
PROMPT_HASHES = {
    "agent-v1": "2b8e34452cb1020fbd99d20bec947a56ae116d7e9c364ac7ec8520c7c02fff61",
    # agent-v2: status names checked verbatim (fixes first-attempt status_not_stated),
    # reconciliation answered from reconcile_case only.
    "agent-v2": "5cec9ce85cfef34fcf7223e969b37ed29a52e9c8f7d665bdac96433f51ffb81d",
    # agent-v3: same text as v2, moved into chatmed.agent.prompt; whole module hashed
    # (now also covers the retry note).
    "agent-v3": "47318072bcbf3b167046d1e07bc83c7f9480e1bf02a110b863ce9ef3b0c328dd",
    # agent-v4 (M5a): no quoted paraphrase of a status in the prompt; one line per
    # reconciliation item with its status name (STATUS_EXAMPLE).
    "agent-v4": "0dc277aa9a0695197c3f73aa087d9378feaf565cc1c027a1b9529656ec15c5b6",
    # agent-v5 (M5a): cited_ids holds only row ids (never a bare document id).
    "agent-v5": "a1d5098b752741806e6e1da0b073e636011fa90b98834f9eb38d068b5625fe82",
    # agent-v6 (M5a): reconciliation answers start with reconcile_case's code-written
    # statement (rules-10); STATUS_EXAMPLE removed.
    "agent-v6": "e51fd56b5f0b9e0d9944b0ed7bce06e48c1afc4a3b02b0cb085492d41fcb509d",
    # agent-v7 (M5a): reconciliation answers rendered by code (rules-11); the model only
    # routes to reconcile_case. v6's statement sentence dropped; v5's cited_ids rule kept.
    "agent-v7": "8be36dd47d68c9da3d631364f8ff1cf5c9ac33126a1c390512ccef0807c3f958",
}


def prompt_hash() -> str:
    return modules_hash(PROMPT_MODULES)


def test_prompt_changes_come_with_a_version_bump() -> None:
    assert sys.version_info[:2] == PYTHON, "ast.dump differs between Python versions; use 3.12"
    assert prompt.AGENT_PROMPT_VERSION in PROMPT_HASHES, (
        f"add PROMPT_HASHES[{prompt.AGENT_PROMPT_VERSION!r}] = {prompt_hash()!r}"
    )
    assert prompt_hash() == PROMPT_HASHES[prompt.AGENT_PROMPT_VERSION], (
        "the agent prompt changed: bump AGENT_PROMPT_VERSION in chatmed.agent.prompt, "
        f"then add its hash here: {prompt_hash()!r}"
    )


def test_every_prompt_version_has_its_own_hash() -> None:
    assert len(set(PROMPT_HASHES.values())) == len(PROMPT_HASHES)
