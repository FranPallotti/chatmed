# ChatMed app image (M5a AC2). Linux, Python 3.12 slim, Tesseract from the distro.
#
#   docker build -t chatmed:local .                 # runtime image (the last stage)
#   docker build --target test -t chatmed:test .    # runs the OCR/parser tests inside the image
#
# Nothing secret or real is in the build context: .dockerignore lets in only pyproject.toml,
# uv.lock, src/, and (for the test stage) tests/, fixtures/ and scripts/. Settings and secrets
# (.env, db/.env) reach the container as environment variables at run time (compose / ACA).
#
# Stages: `builder` installs the locked dependencies into /app/.venv with uv; `base` is the
# slim Python image plus Tesseract and the copied venv only (no uv, no source tree, no cache);
# `test` adds dev dependencies and the tests on top of `base`; `runtime` (last, so it is the
# default target) is `base` with the non-root user and the start command.

# Debian 13 ships Tesseract 5.5; its version, Leptonica version and the eng.traineddata hash
# all go into parser_id, so a base-image update shows up in the fingerprint (rule 3).
ARG PYTHON_IMAGE=python:3.12-slim-trixie
FROM ghcr.io/astral-sh/uv:0.13.10 AS uv

FROM ${PYTHON_IMAGE} AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Dependencies first (cached layer), then the project itself, installed non-editable so the
# venv is self-contained and src/ is not needed at run time.
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/.venv/bin:$PATH \
    # Explicit, so tessdata discovery never depends on the binary's guess (M1b AC5).
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin chatmed
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv


# Dev dependencies for the test stage, resolved from the same lock.
FROM builder AS test-deps
RUN uv sync --frozen --no-editable

# OCR and parser tests on the image's own Tesseract and tessdata (M5a AC2). The build fails
# if they fail. Synthetic fixtures only. Tests that need repository files kept out of the
# build context skip themselves here (HOST_ONLY_TESTS in tests/conftest.py).
FROM base AS test
COPY --from=test-deps /app/.venv /app/.venv
COPY pyproject.toml ./
COPY tests ./tests
COPY fixtures ./fixtures
COPY scripts ./scripts
ENV CHATMED_IN_IMAGE=1
USER chatmed
RUN python -m pytest -q -rs -p no:cacheprovider \
    tests/test_parsing.py tests/test_cioms_fixtures.py tests/contracts/test_parser_contract.py \
    tests/test_ui.py tests/test_telemetry.py tests/guardrails


FROM base AS runtime
USER chatmed
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
# `python -m chatmed.api` starts uvicorn with log_config=None: only our metadata-only handler.
CMD ["python", "-m", "chatmed.api"]
