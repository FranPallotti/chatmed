"""M5a AC2/AC3: the image holds no secrets and no real data, and runs as a non-root user.

These read the build files as text (no Docker needed). The image itself is exercised by
`docker build --target test` (the OCR tests run inside it) and `scripts/smoke_demo.py`.
This module is listed in `HOST_ONLY_TESTS`: the build files are not in the image.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import HOST_ONLY_TESTS

ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    """Read lazily: in the image this module is collected (then skipped) without the files."""
    return (ROOT / name).read_text(encoding="utf-8")


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _stages() -> dict[str, str]:
    """Dockerfile stage name -> its instructions, in file order."""
    pattern = r"^FROM\s+\S+(?:\s+AS\s+(\S+))?\s*$"
    parts = re.split(pattern, _read("Dockerfile"), flags=re.M | re.I)
    return {name: body for name, body in zip(parts[1::2], parts[2::2], strict=True) if name}


def test_build_context_is_an_allow_list_without_secrets_or_samples() -> None:
    lines = _lines(_read(".dockerignore"))
    assert lines[0] == "*", "everything is excluded first"
    allowed = {ln[1:].rstrip("/") for ln in lines if ln.startswith("!")}
    assert allowed == {"pyproject.toml", "uv.lock", "src", "tests", "fixtures", "scripts"}
    for never in ("**/.env", "**/.env.*", "**/samples"):
        assert never in lines


def test_dockerfile_never_copies_secrets_data_or_the_whole_context() -> None:
    instructions = _lines(_read("Dockerfile"))
    copies = [ln for ln in instructions if ln.upper().startswith(("COPY", "ADD"))]
    assert copies
    for line in copies:
        assert not line.upper().startswith("ADD"), line  # ADD can fetch URLs and unpack
        sources = line.split()[1:-1]
        assert not any(s in {".", "./"} for s in sources), f"whole context copied: {line}"
        assert not re.search(r"\.env|samples|\bdb\b", line), line
    secrets = r"PASSWORD|SECRET|API_KEY|CONNECTION_STRING|AZURE_CLIENT"
    assert not re.search(secrets, "\n".join(instructions))


def test_runtime_is_the_default_target_non_root_and_minimal() -> None:
    stages = _stages()
    assert list(stages)[-1] == "runtime", "plain `docker build .` must build the runtime"
    base, runtime = stages["base"], stages["runtime"]
    assert re.findall(r"^USER\s+(\S+)", runtime, flags=re.M) == ["chatmed"]
    assert 'CMD ["python", "-m", "chatmed.api"]' in runtime  # log_config=None (AC2)
    assert "--no-install-recommends tesseract-ocr tesseract-ocr-eng" in base
    assert "rm -rf /var/lib/apt/lists/*" in base
    assert "TESSDATA_PREFIX=" in base  # explicit tessdata (M1b AC5)
    # Only the venv from the builder: no uv, no source tree, no dev dependencies, no tests.
    assert re.findall(r"^COPY.*$", base + runtime, flags=re.M) == [
        "COPY --from=builder /app/.venv /app/.venv"
    ]
    assert "uv sync --frozen --no-dev --no-editable" in stages["builder"]


def test_test_stage_runs_the_ocr_tests_as_non_root() -> None:
    test = _stages()["test"]
    assert re.findall(r"^USER\s+(\S+)", test, flags=re.M)[-1] == "chatmed"
    assert "tests/test_parsing.py" in test and "tests/test_cioms_fixtures.py" in test
    # Tests are left out of the image run only through HOST_ONLY_TESTS (one list).
    assert "CHATMED_IN_IMAGE=1" in test
    assert "--deselect" not in test and "--ignore" not in test


def test_host_only_list_is_current() -> None:
    """Every entry names an existing module (and test function), so the list cannot rot."""
    for entry in HOST_ONLY_TESTS:
        module, _, function = entry.partition("::")
        path = ROOT / module
        assert path.is_file(), entry
        if function:
            assert re.search(rf"^def {function}\(", path.read_text("utf-8"), re.M), entry


def test_compose_builds_the_runtime_and_mounts_nothing_into_the_app() -> None:
    app = _read("docker-compose.yml").split("\n  app:\n", 1)[1].split("\nvolumes:", 1)[0]
    assert re.search(r"target:\s*runtime", app)
    assert re.search(r"env_file:\s*\n\s*- \.env\b.*\n\s*- db/\.env\b", app)
    assert "volumes:" not in app  # no host folder or volume the app could write to
    assert "read_only: true" in app
    assert "chatmed_reader_{tenant}@postgres:5432" in app  # no password in the DSN
