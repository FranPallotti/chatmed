"""D6: every output-deciding module is under exactly one version guard.

A module that decides output from the same inputs (same PDFs, tenant data, question and model
output) is hashed either by the rules guard (`RULES_VERSION`, `test_rules_version.py`) or by
the agent prompt guard (`AGENT_PROMPT_VERSION`, `test_agent_prompt_version.py`), never by
both. Every other module must be listed below with the reason it does not decide output. So a
new module fails this test until someone decides which of the three it is.
"""

from __future__ import annotations

import pkgutil

import chatmed
from tests.guardrails.test_agent_prompt_version import PROMPT_MODULES
from tests.guardrails.test_rules_version import RULE_MODULES

NOT_OUTPUT_DECIDING = {
    "chatmed": "package marker",
    "chatmed.models": "shared dataclasses, no logic",
    "chatmed.config": "loads settings; their values (not code) enter both fingerprints (D6)",
    "chatmed.logconfig": "logging only; never changes an answer",
    "chatmed.api": "transport: request parsing, NDJSON events, limits; passes data through",
    "chatmed.pipeline": "orchestration (moved from api, D15): parse, extract, store, ask, "
    "close in order; lifecycle and error codes; passes data through",
    "chatmed.agent": "re-exports",
    "chatmed.parsing": "builds the configured parser; the choice is recorded in parser_id",
    "chatmed.session": "re-exports",
    "chatmed.session.base": "storage interface",
    "chatmed.session.memory": "storage; every consumer sorts documents itself (by sha256)",
    "chatmed.tenant_db": "re-exports",
    "chatmed.tenant_db.credentials": "which password a tenant's read-only role uses (D17); "
    "the role, database and queries are unchanged",
    "chatmed.telemetry": "exports metadata-only logs and request spans; never changes an answer",
}


def _all_modules() -> set[str]:
    found = {"chatmed"}
    for info in pkgutil.walk_packages(chatmed.__path__, prefix="chatmed."):
        found.add(info.name)
    return found


def test_every_module_is_classified() -> None:
    classified = set(RULE_MODULES) | set(PROMPT_MODULES) | set(NOT_OUTPUT_DECIDING)
    missing = sorted(_all_modules() - classified)
    assert not missing, f"classify these modules (rules, prompt or not output-deciding): {missing}"
    stale = sorted(classified - _all_modules())
    assert not stale, f"these listed modules no longer exist: {stale}"


def test_each_module_is_under_one_guard_only() -> None:
    groups = [set(RULE_MODULES), set(PROMPT_MODULES), set(NOT_OUTPUT_DECIDING)]
    for i, a in enumerate(groups):
        for b in groups[i + 1 :]:
            assert not a & b, f"listed twice: {sorted(a & b)}"
    assert len(RULE_MODULES) == len(set(RULE_MODULES))
