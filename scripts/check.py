"""Run every check the agent must pass before calling a task done.

Cross-platform (Windows/macOS/Linux):  uv run python scripts/check.py
1. Forbidden-pattern guard over src/ (hard rules in CLAUDE.md).
2. ruff lint + format check, mypy, pytest.

pytest gets `.env` and `db/.env` (when present) in its environment, so the tests that need
the local tenant databases run instead of skipping; variables already set in the shell win.
Tests never send telemetry: `tests/conftest.py` drops APPLICATIONINSIGHTS_CONNECTION_STRING.
At the end, skipped tests are counted by reason, so a skip that hides a check is visible.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ENV_FILES = (ROOT / ".env", ROOT / "db" / ".env")
# pytest -rs summary line: "SKIPPED [3] tests/test_x.py:12: reason" (line number optional).
SKIP_LINE = re.compile(r"^SKIPPED \[(\d+)\] .+?\.py(?::\d+)?: (.*)$")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

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


# scripts/ may print (they are CLI tools), but every other rule applies to them too.
SCRIPT_EXEMPT = {"Use the metadata-only logger, not print"}


def guard() -> list[str]:
    problems = []
    files = [(p, False) for p in sorted(SRC.rglob("*.py"))]
    files += [(p, True) for p in sorted((ROOT / "scripts").rglob("*.py")) if p.name != "check.py"]
    for path, is_script in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "# guard: allow" in line:  # explicit, reviewable exception
                continue
            for pattern, reason in FORBIDDEN:
                if is_script and reason in SCRIPT_EXEMPT:
                    continue
                if re.search(pattern, line):
                    rel = path.relative_to(ROOT)
                    problems.append(f"{rel}:{n}: {reason}\n    {line.strip()}")
    return problems


def run(cmd: list[str]) -> bool:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=ROOT).returncode == 0


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a dotenv file. Comments, blank lines and inline ` # comments` are
    ignored; surrounding quotes are removed. Values are never printed (they hold passwords)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) > 1 and value[0] in {"'", '"'} and value.endswith(value[0]):
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        if ENV_KEY.fullmatch(key):
            values[key] = value
    return values


def pytest_env() -> dict[str, str]:
    """The current environment plus the local env files; the shell wins over the files."""
    loaded: dict[str, str] = {}
    for path in ENV_FILES:
        if path.is_file():
            loaded.update(read_env_file(path))
            print(f"pytest env: loaded {path.parent.name}/{path.name}")
    return loaded | dict(os.environ)


def skip_summary(output: list[str]) -> Counter[str]:
    """Skipped test counts by reason, from pytest's `-rs` short summary."""
    reasons: Counter[str] = Counter()
    for line in output:
        match = SKIP_LINE.match(line.strip())
        if match:
            reasons[match.group(2)] += int(match.group(1))
    return reasons


def run_pytest() -> bool:
    """pytest with the env files loaded, streamed live, then the skips by reason."""
    cmd = ["pytest", "-q", "-rs"]
    print(f"\n$ {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=pytest_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    output = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        output.append(line)
    ok = proc.wait() == 0
    reasons = skip_summary(output)
    print(f"\nSkipped tests by reason ({sum(reasons.values())} total):")
    for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:4d}  {reason}")
    if not reasons:
        print("  none")
    return ok


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
    ):
        ok = run(cmd) and ok
    ok = run_pytest() and ok
    print("\nALL CHECKS PASSED" if ok else "\nCHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
