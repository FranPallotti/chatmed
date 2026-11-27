"""scripts/check.py: env files for pytest and the skip summary (M5a follow-up)."""

from __future__ import annotations

from pathlib import Path

import check  # scripts/ is on sys.path (tests/conftest.py)
import pytest


def test_env_file_parsing(tmp_path: Path) -> None:
    env = tmp_path / ".env"  # synthetic values only
    env.write_text(
        "# comment\n"
        "\n"
        "PLAIN=value\n"
        "INLINE=20   # a comment\n"
        'QUOTED="a # not a comment"\n'
        "SINGLE='x'\n"
        "EMPTY=\n"
        "URL=postgresql://r_{tenant}@localhost:5432/t_{tenant}\n"
        "not a line\n"
        "1BAD=x\n",
        encoding="utf-8",
    )
    assert check.read_env_file(env) == {
        "PLAIN": "value",
        "INLINE": "20",
        "QUOTED": "a # not a comment",
        "SINGLE": "x",
        "EMPTY": "",
        "URL": "postgresql://r_{tenant}@localhost:5432/t_{tenant}",
    }


def test_shell_environment_wins_over_env_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("CHATMED_CHECK_A=file\nCHATMED_CHECK_B=file\n", encoding="utf-8")
    monkeypatch.setattr(check, "ENV_FILES", (env, tmp_path / "missing.env"))
    monkeypatch.setenv("CHATMED_CHECK_A", "shell")
    monkeypatch.delenv("CHATMED_CHECK_B", raising=False)
    merged = check.pytest_env()
    assert (merged["CHATMED_CHECK_A"], merged["CHATMED_CHECK_B"]) == ("shell", "file")


def test_skip_summary_counts_by_reason() -> None:
    output = [
        "SKIPPED [6] tests\\test_tenant_db.py:98: tenant DB not reachable (OperationalError)\n",
        "SKIPPED [4] tests/test_tenant_db.py:107: tenant DB not reachable (OperationalError)\n",
        "SKIPPED [1] tests/guardrails/test_container.py: host only: reads Dockerfile\n",
        "437 passed, 11 skipped in 3.2s\n",
    ]
    assert check.skip_summary(output) == {
        "tenant DB not reachable (OperationalError)": 10,
        "host only: reads Dockerfile": 1,
    }
