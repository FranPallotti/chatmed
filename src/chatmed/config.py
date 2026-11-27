"""Settings loaded from environment variables (and a local `.env` in development).

Why a single typed object: backends (parser, OCR, session store) are chosen once per
deployment, never per request, and reproducibility depends on pinned values (deployment,
seed). Validating them at startup makes a bad config fail loudly instead of drifting.
There are deliberately no API-key fields: auth is Entra ID via DefaultAzureCredential.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TENANT_PLACEHOLDER = "{tenant}"
TENANT_ID = re.compile(r"^[a-z0-9_]+$")  # D13a: checked before a tenant is put in a DSN
# libpq accepts a password as URL userinfo, as a `?password=` query parameter, or as a
# `password=` keyword in key/value DSNs; the regex covers the last two.
_DSN_PASSWORD_PARAM = re.compile(r"(^|[\s?&])password\s*=", re.IGNORECASE)


class Settings(BaseSettings):
    """All runtime configuration. Every field must also appear in `.env.example`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # unrelated env vars (PATH, ...) are not errors
        case_sensitive=False,
        frozen=True,
    )

    # Model (rule 3: pinned deployment + fixed seed)
    azure_openai_endpoint: str
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_deployment: str
    model_seed: int = 7
    # Model calls (M2 AC3): per-call timeout, attempts incl. the first, backoff with full jitter
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_attempts: int = Field(default=4, ge=1, le=10)
    llm_backoff_base_seconds: float = Field(default=1.0, gt=0)
    llm_backoff_max_seconds: float = Field(default=20.0, gt=0)
    extraction_max_completion_tokens: int = Field(default=4000, gt=0)
    # Agent loop (M3, D14)
    agent_max_iterations: int = Field(default=8, ge=1, le=20)
    agent_max_completion_tokens: int = Field(default=1500, gt=0)
    agent_fulltext_max_chars: int = Field(default=20_000, gt=0)  # above this: BM25 over pages
    agent_bm25_top_k: int = Field(default=3, ge=1, le=10)
    # Whole question (all model calls, retries, tools): below the 30 s verified-answer target
    agent_question_timeout_seconds: float = Field(default=25.0, gt=0)

    # Parsing (fixed per deployment; part of the reproducibility fingerprint via parser_id)
    parser: Literal["routing", "pdfium"] = "routing"
    ocr_backend: Literal["local", "docintel"] = "local"  # local = Tesseract (D10)
    docintel_endpoint: str | None = None
    ocr_dpi: int = Field(default=300, ge=72, le=600)
    ocr_lang: str = Field(default="eng", pattern=r"^[a-z_]{3,}(\+[a-z_]{3,})*$")
    tesseract_cmd: str | None = None  # None/empty: find `tesseract` on PATH
    # Pages OCRed at once across all sessions (M5a AC1); each Tesseract peaks near 290 MB
    ocr_max_concurrency: int = Field(default=2, ge=1, le=16)

    # Session (only the in-memory store exists; encrypted_blob is a stretch goal)
    session_store: Literal["memory"] = "memory"
    session_ttl_seconds: int = Field(default=1800, gt=0)  # idle time before the wipe (D15)
    session_sweep_interval_seconds: float = Field(default=60.0, gt=0)  # TTL sweeper period
    # open sessions at once; bounds memory (M4 AC3). 8 fits 4 GiB (M5a AC1, architecture.md)
    max_sessions: int = Field(default=8, gt=0)
    max_docs_per_session: int = Field(default=5, gt=0)
    max_pages_per_doc: int = Field(default=30, gt=0)
    max_upload_mb: int = Field(default=20, gt=0)
    extraction_max_chars: int = Field(default=60_000, gt=0)
    duckdb_memory_limit: str = "128MB"  # per session; see architecture.md

    # Tenant DB (D8): one database per tenant, chosen by code from the session's tenant.
    # Read-only role; no password in the DSN: PGPASSWORD_<TENANT> (Azure, from Key Vault) or
    # the shared PGPASSWORD (local), read by chatmed.tenant_db.credentials (D17).
    tenant_db_dsn_template: str
    tenants: str = "acme,globex"  # comma-separated allow-list (D13a); see `tenant_ids`

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # Application Insights (M5a AC5): unset or empty = telemetry off. SecretStr so it never
    # shows up in a repr of the settings.
    applicationinsights_connection_string: SecretStr | None = None

    @property
    def tenant_ids(self) -> frozenset[str]:
        """Tenants this deployment serves; a session's tenant must be one of them (D13a)."""
        return frozenset(t.strip() for t in self.tenants.split(",") if t.strip())

    @model_validator(mode="after")
    def _tenants_are_safe(self) -> Self:
        if not self.tenant_ids or not all(TENANT_ID.match(t) for t in self.tenant_ids):
            raise ValueError("TENANTS must be a comma-separated list matching ^[a-z0-9_]+$")
        return self

    @model_validator(mode="after")
    def _docintel_needs_endpoint(self) -> Self:
        """Fail at startup rather than at the first scanned upload."""
        if self.ocr_backend == "docintel" and not self.docintel_endpoint:
            raise ValueError("DOCINTEL_ENDPOINT is required when OCR_BACKEND=docintel")
        return self

    @model_validator(mode="after")
    def _tenant_dsn_template_is_safe(self) -> Self:
        """Tenant comes from the session (rule 5); no secrets in config (rule 7)."""
        if TENANT_PLACEHOLDER not in self.tenant_db_dsn_template:
            raise ValueError(f"TENANT_DB_DSN_TEMPLATE must contain {TENANT_PLACEHOLDER}")
        dsn = self.tenant_db_dsn_template
        if _DSN_PASSWORD_PARAM.search(dsn) or ("://" in dsn and urlsplit(dsn).password):
            raise ValueError("TENANT_DB_DSN_TEMPLATE must not contain a password")
        return self


# Every Settings field is in exactly one of these two sets (tests/test_config.py).
#
# Fingerprint settings can change facts or answers for the same inputs, so their values go
# into the answer fingerprint (D6, D14). Parsing settings are also in parser_id.
FINGERPRINT_SETTINGS: frozenset[str] = frozenset(
    {
        "azure_openai_endpoint",  # with the deployment name, identifies the model
        "azure_openai_deployment",
        "azure_openai_api_version",  # request semantics (structured outputs, tools)
        "model_seed",
        "extraction_max_chars",  # extraction window size: facts are merged across windows
        "agent_fulltext_max_chars",  # whole text vs BM25 for search_document_text
        "agent_bm25_top_k",  # how many pages the model sees
        "parser",  # page text (also in parser_id)
        "ocr_backend",
        "ocr_dpi",
        "ocr_lang",
        "tenant_db_dsn_template",  # which databases the tenant data comes from
    }
)

# Operational settings only decide whether a verified answer is produced (limits, retries,
# timeouts, admission) or how the service runs; with the same inputs they cannot change the
# facts of a verified answer. They stay out of the fingerprint: a run they cut short ends as
# outcome "fallback" (or an error), and only verified answers claim reproducibility (D14).
OPERATIONAL_SETTINGS: frozenset[str] = frozenset(
    {
        "llm_timeout_seconds",
        "llm_max_attempts",
        "llm_backoff_base_seconds",
        "llm_backoff_max_seconds",
        "extraction_max_completion_tokens",  # truncated output is an error, never partial facts
        "agent_max_iterations",
        "agent_max_completion_tokens",  # truncated answer is an error
        "agent_question_timeout_seconds",
        "docintel_endpoint",  # which resource; the model id is fixed and in parser_id
        "tesseract_cmd",  # where the binary is; its version is in parser_id
        "ocr_max_concurrency",  # when pages are OCRed, never their order or text
        "session_store",
        "session_ttl_seconds",
        "session_sweep_interval_seconds",
        "max_sessions",  # admission limit: reject, never change facts
        "max_docs_per_session",  # admission limits: reject, never change facts
        "max_pages_per_doc",
        "max_upload_mb",
        "duckdb_memory_limit",  # no spill to disk: over the limit a query fails
        "tenants",  # allow-list (admission)
        "log_level",
        "applicationinsights_connection_string",  # where metadata telemetry goes
    }
)


def fingerprint_settings(settings: Settings) -> list[str]:
    """`name=value` of every fingerprint setting, sorted by name: the settings part of both
    fingerprints (extraction and answer, D6). Values only, never secrets (there are none)."""
    return [f"{name}={getattr(settings, name)}" for name in sorted(FINGERPRINT_SETTINGS)]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once. Tests build `Settings(...)` directly instead."""
    return Settings()
