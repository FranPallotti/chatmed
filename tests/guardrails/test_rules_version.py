"""D6: the code that decides output from the same inputs is versioned, and that is enforced.

`RULES_VERSION` is part of the extraction and answer fingerprints. This test hashes the AST of
every module that decides output (parsing, grounding, checkbox rules D13a/D13b, merge, subject
join, tenant queries, agent tools, reconciliation, answer checker, agent loop; the list and
the reasons are in D6) and fails if that hash is not the one recorded for the current
`RULES_VERSION`. So a rule change cannot ship under an old version: bump `RULES_VERSION` in
`chatmed.extraction` and add its hash below. What the agent model is shown is under the agent
prompt version instead (`test_agent_prompt_version.py`); `test_guard_coverage.py` checks that
every module is under exactly one of the two, or listed as not deciding output.

The hash ignores comments, formatting and docstrings (they do not change behaviour), and it
covers whole modules, so any code change there needs a bump, even one that is not a rule.
`ast.dump` output differs between Python minor versions; the project pins 3.12.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import sys

from chatmed import extraction

# Order matters for the hash: append, never reorder.
RULE_MODULES = (
    "chatmed.extraction",  # extraction prompt + schema, grounding, checkbox rules, merge, join
    "chatmed.agent.reconcile",  # adae record selection, item comparison (D14)
    "chatmed.agent.answer",  # answer checker, facts_used closure, fallback text
    "chatmed.agent.tools",  # tool rows and ids, printed-id mapping, joined subject in the unit
    "chatmed.agent.retrieval",  # whole text vs BM25, page order
    "chatmed.agent.loop",  # one retry, iteration and time limits, answer fingerprint
    "chatmed.tenant_db.repository",  # which tenant rows and columns the tools see
    "chatmed.parsing.base",  # text normalisation shared by all parsers
    "chatmed.parsing.pdfium",  # text layer
    "chatmed.parsing.tesseract_ocr",  # local OCR settings and output mapping
    "chatmed.parsing.docintel",  # Document Intelligence result mapping
    "chatmed.parsing.routing",  # which pages go to OCR
)
PYTHON = (3, 12)

# One entry per RULES_VERSION ever released. Never edit an existing hash: bump the version.
RULES_HASHES = {
    "rules-1": "431d274170fc5e86d0771124d2f9cf78d4fba691e8bc8cfd6ed1048dc5bc27c4",
    # rules-2 (D14): adds chatmed.agent.reconcile (adae record selection, item comparison).
    "rules-2": "9c65d6c469853771994461c557bfe403e8c1615c6f43de7eae680ff5e1ceb6d0",
    # rules-3 (M3 review): every output-deciding module, see RULE_MODULES and D6.
    "rules-3": "c9dfac3ef89ff0882210a7013a411b3fd8c05e97e60b08725aed0575f4c3fb31",
    # rules-4: fingerprint by FINGERPRINT_SETTINGS value, outcome verified|fallback (D14).
    "rules-4": "4cc2e314f976d1462aa088752958b99b7bc4b1099428683cf2ad99fb44c0d3df",
    # rules-5: extraction fingerprint over FINGERPRINT_SETTINGS values too (D6).
    "rules-5": "15c4628a6d5faa343e94690964dc53e5270eb04d03c44b10554d2585d9f89d64",
    # rules-6: answer fingerprint over tenant, history and tenant reads; status negation over
    # the whole clause, hyphenated words excluded; ambiguous source dates decide no reading.
    "rules-6": "a282aba3734f1efd6ce94e8f5d14178749ec86c0a90cd07c347d14deb34ac303",
}


def _without_docstrings(tree: ast.Module) -> ast.Module:
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return tree


def modules_hash(modules: tuple[str, ...]) -> str:
    """sha256 over the docstring-free AST dumps of `modules`, in that order."""
    digest = hashlib.sha256()
    for name in modules:
        source = inspect.getsource(importlib.import_module(name))
        digest.update(name.encode())
        digest.update(ast.dump(_without_docstrings(ast.parse(source))).encode())
    return digest.hexdigest()


def rules_hash() -> str:
    return modules_hash(RULE_MODULES)


def test_rule_changes_come_with_a_rules_version_bump() -> None:
    assert sys.version_info[:2] == PYTHON, "ast.dump differs between Python versions; use 3.12"
    assert extraction.RULES_VERSION in RULES_HASHES, (
        f"add RULES_HASHES[{extraction.RULES_VERSION!r}] = {rules_hash()!r}"
    )
    assert rules_hash() == RULES_HASHES[extraction.RULES_VERSION], (
        "the rule modules changed: bump RULES_VERSION in chatmed.extraction, "
        f"then add its hash here: {rules_hash()!r}"
    )


def test_every_rules_version_has_its_own_hash() -> None:
    assert len(set(RULES_HASHES.values())) == len(RULES_HASHES)


def test_docstrings_and_comments_do_not_change_the_hash() -> None:
    plain = ast.parse("def f(x):\n    return x + 1\n")
    documented = ast.parse('def f(x):\n    """Why."""\n    # note\n    return x + 1\n')
    assert ast.dump(_without_docstrings(plain)) == ast.dump(_without_docstrings(documented))


def test_code_changes_do_change_the_hash() -> None:
    a = ast.parse("def f(x):\n    return x + 1\n")
    b = ast.parse("def f(x):\n    return x + 2\n")
    assert ast.dump(_without_docstrings(a)) != ast.dump(_without_docstrings(b))
