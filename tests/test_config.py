"""M0 AC2: settings come from env, are validated, and `.env.example` lists all of them."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from chatmed.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

REQUIRED = {
    "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
    "AZURE_OPENAI_DEPLOYMENT": "gpt-test-pinned",
    "TENANT_DB_DSN_TEMPLATE": "postgresql://chatmed_reader_{tenant}@localhost:5432/tenant_{tenant}",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Clean env with only the required settings, so a developer's shell can't leak in."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def load() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]  # ignore any local .env


def test_loads_from_env_with_defaults(env: pytest.MonkeyPatch) -> None:
    env.setenv("MODEL_SEED", "42")
    env.setenv("MAX_UPLOAD_MB", "5")
    s = load()
    assert s.azure_openai_deployment == "gpt-test-pinned"
    assert s.model_seed == 42
    assert s.max_upload_mb == 5
    assert (s.parser, s.ocr_backend, s.session_store) == ("routing", "local", "memory")


def test_missing_required_setting_fails(env: pytest.MonkeyPatch) -> None:
    env.delenv("AZURE_OPENAI_DEPLOYMENT")
    with pytest.raises(ValidationError):
        load()


@pytest.mark.parametrize(
    ("key", "bad"),
    [("PARSER", "llm-vision"), ("OCR_BACKEND", "tesseract"), ("SESSION_STORE", "redis")],
)
def test_unknown_backend_fails(env: pytest.MonkeyPatch, key: str, bad: str) -> None:
    env.setenv(key, bad)
    with pytest.raises(ValidationError):
        load()


def test_docintel_requires_endpoint(env: pytest.MonkeyPatch) -> None:
    env.setenv("OCR_BACKEND", "docintel")
    with pytest.raises(ValidationError, match="DOCINTEL_ENDPOINT"):
        load()
    env.setenv("DOCINTEL_ENDPOINT", "https://example.cognitiveservices.azure.com/")
    assert load().ocr_backend == "docintel"


def test_env_example_lists_exactly_the_settings() -> None:
    keys = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text("utf-8"), re.MULTILINE))
    assert keys == {name.upper() for name in Settings.model_fields}


def test_env_example_loads_as_valid_settings(env: pytest.MonkeyPatch) -> None:
    """The example's values (and inline comments) must parse with no extra env set."""
    for key in REQUIRED:
        env.delenv(key)
    s = Settings(_env_file=ENV_EXAMPLE)  # type: ignore[call-arg]
    assert s.parser == "routing"
    assert s.docintel_endpoint in (None, "")


@pytest.mark.parametrize(
    "template",
    [
        "postgresql://chatmed_reader@localhost:5432/tenants",  # no {tenant}: model could pick
        "postgresql://chatmed_reader:pw@localhost:5432/tenant_{tenant}",  # secret in userinfo
        "postgresql://chatmed_reader@localhost:5432/tenant_{tenant}?password=pw",  # in query
        "postgresql://chatmed_reader@localhost/tenant_{tenant}?sslmode=require&Password=pw",
        "host=localhost user=chatmed_reader password=pw dbname=tenant_{tenant}",  # keyword DSN
    ],
)
def test_unsafe_tenant_dsn_template_fails(env: pytest.MonkeyPatch, template: str) -> None:
    env.setenv("TENANT_DB_DSN_TEMPLATE", template)
    with pytest.raises(ValidationError, match="TENANT_DB_DSN_TEMPLATE"):
        load()


def test_keyword_dsn_without_password_is_accepted(env: pytest.MonkeyPatch) -> None:
    """`passfile=` must not be mistaken for a password."""
    template = "host=localhost user=chatmed_reader passfile=/x dbname=tenant_{tenant}"
    env.setenv("TENANT_DB_DSN_TEMPLATE", template)
    assert load().tenant_db_dsn_template == template


def test_parsing_settings_defaults_and_overrides(env: pytest.MonkeyPatch) -> None:
    s = load()
    assert (s.parser, s.ocr_dpi, s.ocr_lang, s.tesseract_cmd) == ("routing", 300, "eng", None)
    env.setenv("PARSER", "pdfium")
    env.setenv("OCR_DPI", "200")
    env.setenv("OCR_LANG", "eng+spa")
    env.setenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    s = load()
    assert (s.parser, s.ocr_dpi, s.ocr_lang) == ("pdfium", 200, "eng+spa")
    assert s.tesseract_cmd and s.tesseract_cmd.endswith("tesseract.exe")


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("PARSER", "pymupdf"),  # removed in D10
        ("OCR_DPI", "50"),
        ("OCR_DPI", "1200"),
        ("OCR_LANG", "eng; rm -rf"),  # passed to the tesseract command line
        ("OCR_LANG", "ENG"),
    ],
)
def test_invalid_parsing_settings_fail(env: pytest.MonkeyPatch, key: str, bad: str) -> None:
    env.setenv(key, bad)
    with pytest.raises(ValidationError):
        load()


def test_every_setting_is_fingerprint_or_operational_exactly_once() -> None:
    """D14: a new setting fails here until someone decides whether it can change facts."""
    from chatmed.config import FINGERPRINT_SETTINGS, OPERATIONAL_SETTINGS

    fields = set(Settings.model_fields)
    assert not FINGERPRINT_SETTINGS & OPERATIONAL_SETTINGS
    assert FINGERPRINT_SETTINGS | OPERATIONAL_SETTINGS == fields, (
        f"unclassified: {sorted(fields - FINGERPRINT_SETTINGS - OPERATIONAL_SETTINGS)}, "
        f"unknown: {sorted((FINGERPRINT_SETTINGS | OPERATIONAL_SETTINGS) - fields)}"
    )
