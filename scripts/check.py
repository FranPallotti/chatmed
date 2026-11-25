"""Run every check the agent must pass before calling a task done.

Cross-platform (Windows/macOS/Linux):  uv run python scripts/check.py
1. Forbidden-pattern guard over src/ (hard rules in CLAUDE.md).
2. ruff lint + format check, mypy, pytest.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# (regex, reason). Keep this list short and high-signal; add to it when a rule is broken once.
FORBIDDEN: list[tuple[str, str]] = [
    (r"^\s*(import|from)\s+tempfile\b", "Rule 1: no temp files"),
    (r"^\s*(import|from)\s+(pickle|shelve|dill)\b", "Rule 1: no serializing session objects"),
    (r"duckdb\.connect\((?!\s*['\"]:memory:['\"])", "Rule 1: DuckDB must be ':memory:'"),
    (r"store\s*=\s*True", "Rule 6: stateless model calls only"),
    (r"\.beta\.(threads|assistants)", "Rule 6: no server-side threads"),
    (r"temperature\s*=\s*(?!0\b|0\.0\b|settings\.)", "Rule 3: temperature must be 0"),
    (
        r"log\w*\.(debug|info|warning|error|exception|critical)\(.*"
        r"(\.text\b|quote|prompt|messages|value_text|page_text|content)",
        "Rule 2: possible document content in logs",
    ),
    (r"^\s*print\(", "Use the metadata-only logger, not print"),
    (r"api_key\s*=\s*['\"]", "Rule 7: no secrets in code"),
]


def guard() -> list[str]:
    problems = []
    for path in sorted(SRC.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "# guard: allow" in line:  # explicit, reviewable exception
                continue
            for pattern, reason in FORBIDDEN:
                if re.search(pattern, line):
                    rel = path.relative_to(ROOT)
                    problems.append(f"{rel}:{n}: {reason}\n    {line.strip()}")
    return problems


def run(cmd: list[str]) -> bool:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=ROOT).returncode == 0


def main() -> int:
    problems = guard()
    if problems:
        print("Forbidden patterns found:\n" + "\n".join(problems))
    else:
        print("Forbidden-pattern guard: OK")
    ok = not problems
    for cmd in (
        ["ruff", "check", "."],
        ["ruff", "format", "--check", "."],
        ["mypy"],
        ["pytest", "-q"],
    ):
        ok = run(cmd) and ok
    print("\nALL CHECKS PASSED" if ok else "\nCHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
