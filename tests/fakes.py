"""Test doubles: a fake chat-completions client and a fake tenant repository (no network).

`FakeModelClient.from_recordings(folder)` serves hand-written model responses from
`fixtures/recorded/*.json`. There is no recording mode on purpose: no code path writes model
output to disk (rule 1). A recording names a fixture, the parser that reads it, the doc type
and the field keys; the fake rebuilds the exact user message for that fixture and answers only
a request with that message and those fields, so a changed fixture, parser or message format
fails loudly ("no recording") instead of returning a stale answer.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from chatmed.extraction import DocTypeSchema, user_message
from chatmed.parsing import PdfiumParser, TesseractOcrParser
from chatmed.parsing.base import DocumentParser
from tests.conftest import FIXTURES, find_tesseract

Key = tuple[tuple[str, ...], str]

# Mirrors the cioms field_definitions in db/seed/tenant_acme.sql (checked against the real DB
# in tests/test_tenant_db.py when Postgres is running).


def _row(
    key: str,
    label: str,
    description: str,
    data_type: str,
    *,
    required: bool = False,
    multiple: bool = False,
    allowed: list[str] | None = None,
    unit: str | None = None,
    join: str | None = None,
    checkbox: bool = False,
    box_labels: list[str] | None = None,
) -> dict[str, Any]:
    """One field_definitions row, shaped like the repository returns it."""
    return {
        "key": key,
        "label": label,
        "description": description,
        "data_type": data_type,
        "required": required,
        "multiple": multiple,
        "allowed_values": allowed,
        "unit": unit,
        "join_target": join,
        "checkbox": checkbox,
        "box_labels": box_labels or [],  # NOT NULL DEFAULT '{}' in the schema
    }


YES_NO_NA = ["yes", "no", "na"]
SERIOUSNESS_LABELS = [
    "patient died",
    "involved or prolonged inpatient hospitalisation",
    "involved persistent or significant disability or incapacity",
    "life threatening",
]
REPORT_TYPES = ["initial", "followup"]

ACME_CIOMS_ROWS: list[dict[str, Any]] = [
    _row(
        "subject_id",
        "Subject ID",
        "Clinical study subject identifier (SUBJID, e.g. 01001-004); "
        "usually near the patient section or in the narrative",
        "string",
        required=True,
        join="adsl.subjid",
    ),
    _row("country", "Country", "Box 1a: country where the reaction occurred, as written", "string"),
    _row("age", "Age", "Box 2a: age of the patient in years", "integer", unit="years"),
    _row(
        "sex",
        "Sex",
        "Box 3: sex of the patient (F = female, M = male)",
        "enum",
        allowed=["F", "M", "U"],
    ),
    _row(
        "reaction_onset_date",
        "Reaction onset",
        "Boxes 4-6: date the reaction started",
        "date",
        required=True,
    ),
    _row(
        "reaction_description",
        "Reaction description",
        "Boxes 7+13: description of the reaction(s), including relevant tests and lab data",
        "string",
        required=True,
    ),
    _row(
        "patient_died",
        "Patient died",
        'Boxes 8-12: seriousness criterion "patient died" is checked',
        "boolean",
        checkbox=True,
        box_labels=["patient died"],
    ),
    _row(
        "hospitalisation",
        "Hospitalisation",
        'Boxes 8-12: "involved or prolonged inpatient hospitalisation" is checked',
        "boolean",
        checkbox=True,
        box_labels=["involved or prolonged inpatient hospitalisation"],
    ),
    _row(
        "disability",
        "Disability or incapacity",
        'Boxes 8-12: "involved persistent or significant disability or incapacity" is checked',
        "boolean",
        checkbox=True,
        box_labels=["involved persistent or significant disability or incapacity"],
    ),
    _row(
        "life_threatening",
        "Life threatening",
        'Boxes 8-12: "life threatening" is checked',
        "boolean",
        checkbox=True,
        box_labels=["life threatening"],
    ),
    _row(
        "suspect_drug",
        "Suspect drug",
        "Box 14 only: suspect drug name(s), one per entry; never the concomitant drugs of box 22",
        "string",
        required=True,
        multiple=True,
    ),
    _row(
        "daily_dose", "Daily dose", "Box 15: daily dose of the suspect drug, as written", "string"
    ),
    _row("route", "Route", "Box 16: route of administration", "string"),
    _row("indication", "Indication", "Box 17: indication for use", "string"),
    _row(
        "therapy_start_date",
        "Therapy start",
        "Box 18: start date of therapy with the suspect drug",
        "date",
    ),
    _row(
        "dechallenge",
        "Abated after stopping",
        "Box 20: did the reaction abate after stopping the drug?",
        "enum",
        allowed=YES_NO_NA,
        checkbox=True,
        box_labels=["Did reaction abate after stopping drug?"],
    ),
    _row(
        "rechallenge",
        "Reappeared after reintroduction",
        "Box 21: did the reaction reappear after reintroduction?",
        "enum",
        allowed=YES_NO_NA,
        checkbox=True,
        box_labels=["Did reaction reappear after reintroduction?"],
    ),
    _row(
        "concomitant_drugs",
        "Concomitant drugs",
        "Box 22: concomitant drugs, one per entry (exclude those used to treat the reaction)",
        "string",
        multiple=True,
    ),
    _row(
        "mfr_control_no",
        "Manufacturer control no.",
        "Box 24b: manufacturer control number of the case",
        "string",
    ),
    _row("date_received", "Date received", "Box 24c: date received by manufacturer", "date"),
    _row(
        "report_type",
        "Report type",
        "Box 25a: initial or follow-up report",
        "enum",
        allowed=REPORT_TYPES,
        checkbox=True,
        box_labels=["Report type"],
    ),
]

# Mirrors db/seed/tenant_globex.sql (smaller field set, different checkbox fields).
GLOBEX_CIOMS_ROWS: list[dict[str, Any]] = [
    _row(
        "subject_id",
        "Subject ID",
        "Clinical study subject identifier (SUBJID, e.g. 20101-007)",
        "string",
        required=True,
        join="adsl.subjid",
    ),
    _row(
        "reaction_onset_date",
        "Reaction onset",
        "Boxes 4-6: date the reaction started",
        "date",
        required=True,
    ),
    _row(
        "reaction_description",
        "Reaction description",
        "Boxes 7+13: description of the reaction(s)",
        "string",
        required=True,
    ),
    _row(
        "serious",
        "Serious",
        "Any of boxes 8-12 (died, hospitalisation, disability, life threatening) is checked",
        "boolean",
        checkbox=True,
        box_labels=SERIOUSNESS_LABELS,
    ),
    _row(
        "suspect_drug",
        "Suspect drug",
        "Box 14 only: suspect drug name(s), one per entry; never the concomitant drugs of box 22",
        "string",
        required=True,
        multiple=True,
    ),
    _row(
        "report_type",
        "Report type",
        "Box 25a: initial or follow-up report",
        "enum",
        allowed=REPORT_TYPES,
        checkbox=True,
        box_labels=["Report type"],
    ),
]


ROWS_BY_TENANT = {"acme": ACME_CIOMS_ROWS, "globex": GLOBEX_CIOMS_ROWS}
SUBJIDS_BY_TENANT = {
    "acme": ["01001-001", "01001-002", "01001-004", "01002-001", "01002-003"],
    "globex": ["20101-007", "20102-002"],
}


class FakeTenantRepository:
    """In-memory stand-in for PostgresTenantRepository, bound to one tenant like the real one."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.calls: list[str] = []

    async def field_schema(self, doc_type: str) -> DocTypeSchema:
        self.calls.append("field_schema")
        return DocTypeSchema.from_rows(self.tenant_id, doc_type, ROWS_BY_TENANT[self.tenant_id])

    async def subject_ids(self) -> list[str]:
        self.calls.append("subject_ids")
        return list(SUBJIDS_BY_TENANT[self.tenant_id])


def completion(content: str) -> Any:
    message = SimpleNamespace(content=content, refusal=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=message)],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200),
        system_fingerprint="fp_fake",
    )


def _parser(name: str) -> DocumentParser | None:
    if name == "pdfium":
        return PdfiumParser()
    cmd = find_tesseract()
    return TesseractOcrParser(dpi=300, lang="eng", cmd=cmd) if cmd else None


def request_key(request: dict[str, Any]) -> Key:
    fields = request["response_format"]["json_schema"]["schema"]["properties"]
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    return tuple(sorted(fields)), user


class FakeModelClient:
    """Mimics `client.chat.completions.create`; records every request it receives.

    `errors` are raised, in order, before any response is served (to test retries).
    `default` (a JSON-able dict) answers any request without a recording, for tests that do
    not care about the content.
    """

    def __init__(
        self,
        responses: dict[Key, Any] | None = None,
        *,
        errors: list[Exception] | None = None,
        default: dict[str, Any] | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.errors = list(errors or [])
        self.default = default
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.errors:
            raise self.errors.pop(0)
        key = request_key(request) if self.responses else None
        response = self.responses.get(key, self.default) if key else self.default
        if response is None:
            raise LookupError("no recording for this request (fixture or format changed?)")
        return completion(json.dumps(response))

    @classmethod
    def from_recordings(cls, folder: str | Path = FIXTURES / "recorded") -> FakeModelClient:
        """Load every recording whose parser is available (OCR ones need Tesseract)."""
        responses: dict[Key, Any] = {}
        for path in sorted(Path(folder).glob("*.json")):
            rec = json.loads(path.read_text(encoding="utf-8"))
            parser = _parser(rec["parser"])
            if parser is None:
                continue
            doc = asyncio.run(parser.parse((FIXTURES / rec["fixture"]).read_bytes()))
            key = (tuple(sorted(rec["response"])), user_message(rec["doc_type"], doc.pages))
            responses[key] = rec["response"]
        return cls(responses)
