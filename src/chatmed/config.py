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

from pydantic import Field, model_validator
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

    # Parsing (fixed per deployment; part of the reproducibility fingerprint via parser_id)
    parser: Literal["routing", "pdfium"] = "routing"
    ocr_backend: Literal["local", "docintel"] = "local"  # local = Tesseract (D10)
    docintel_endpoint: str | None = None
    ocr_dpi: int = Field(default=300, ge=72, le=600)
    ocr_lang: str = Field(default="eng", pattern=r"^[a-z_]{3,}(\+[a-z_]{3,})*$")
    tesseract_cmd: str | None = None  # None/empty: find `tesseract` on PATH

    # Session (only the in-memory store exists; encrypted_blob is a stretch goal)
    session_store: Literal["memory"] = "memory"
    session_ttl_seconds: int = Field(default=1800, gt=0)
    max_docs_per_session: int = Field(default=5, gt=0)
    max_pages_per_doc: int = Field(default=30, gt=0)
    max_upload_mb: int = Field(default=20, gt=0)
    extraction_max_chars: int = Field(default=60_000, gt=0)
    duckdb_memory_limit: str = "1GB"

    # Tenant DB (D8): one database per tenant, chosen by code from the session's tenant.
    # Read-only role; no password in the DSN (Entra token in Azure, PGPASSWORD env locally).
    tenant_db_dsn_template: str
    tenants: str = "acme,globex"  # comma-separated allow-list (D13a); see `tenant_ids`

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once. Tests build `Settings(...)` directly instead."""
    return Settings()
