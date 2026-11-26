"""D6: the code that turns model output into facts is versioned, and the version is enforced.

`RULES_VERSION` is part of the extraction fingerprint. This test hashes the AST of the modules
that hold the rules (grounding, checkbox rules D13a/D13b, merge, subject join, and since
rules-2 the SAE reconciliation of D14) and fails if
that hash is not the one recorded for the current `RULES_VERSION`. So a rule change cannot ship
under an old version: bump `RULES_VERSION` in `chatmed.extraction` and add its hash below.

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

RULE_MODULES = ("chatmed.extraction", "chatmed.agent.reconcile")
PYTHON = (3, 12)

# One entry per RULES_VERSION ever released. Never edit an existing hash: bump the version.
RULES_HASHES = {
    "rules-1": "431d274170fc5e86d0771124d2f9cf78d4fba691e8bc8cfd6ed1048dc5bc27c4",
    # rules-2 (D14): adds chatmed.agent.reconcile (adae record selection, item comparison).
    "rules-2": "9c65d6c469853771994461c557bfe403e8c1615c6f43de7eae680ff5e1ceb6d0",
}


def _without_docstrings(tree: ast.Module) -> ast.Module:
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return tree


def rules_hash() -> str:
    """sha256 over the docstring-free AST dumps of RULE_MODULES, in that order."""
    digest = hashlib.sha256()
    for name in RULE_MODULES:
        source = inspect.getsource(importlib.import_module(name))
        digest.update(name.encode())
        digest.update(ast.dump(_without_docstrings(ast.parse(source))).encode())
    return digest.hexdigest()


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
