"""Hard rule 9 (D12): real challenge samples live in samples/ and are never used by code.

The sample files are real study data, not cleared for external sharing. Code, tests and scripts
must not reference that folder, so nothing can read them by accident, send them to Azure OpenAI,
or turn them into fixtures. Synthetic fixtures in fixtures/ are the only test inputs.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIS = Path(__file__).resolve()
FORBIDDEN_REFERENCES = ("samples/", "samples\\", '"samples"', "'samples'", "sas7bdat", ".xpt")


def test_no_code_references_the_real_samples_folder() -> None:
    offenders: list[str] = []
    for folder in ("src", "tests", "scripts"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            if path.resolve() == THIS:
                continue
            text = path.read_text(encoding="utf-8")
            hits = [ref for ref in FORBIDDEN_REFERENCES if ref in text]
            if hits:
                offenders.append(f"{path.relative_to(ROOT)}: {hits}")
    assert not offenders, f"code must not reference real sample data (rule 9): {offenders}"


def test_samples_folder_is_gitignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "samples/" in ignored
