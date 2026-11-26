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
