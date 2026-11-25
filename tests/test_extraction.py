"""Offline tests: schema building, grounding checks and merge rules (no Azure calls)."""

from decimal import Decimal

import duckdb

from chatmed.extraction import DocTypeSchema, build_json_schema, load_facts, verify_and_merge
from chatmed.models import ParsedDocument, ParsedPage

ROWS = [
    {
        "key": "patient_mrn",
        "label": "MRN",
        "description": "Medical record number",
        "data_type": "string",
        "required": True,
        "multiple": False,
        "join_target": "patients.mrn",
    },
    {
        "key": "hemoglobin",
        "label": "Hemoglobin",
        "description": "Hb result",
        "data_type": "decimal",
        "required": False,
        "multiple": False,
        "unit": "g/dL",
    },
    {
        "key": "collected_on",
        "label": "Collection date",
        "description": "Sample date",
        "data_type": "date",
        "required": True,
        "multiple": False,
    },
    {
        "key": "hiv_result",
        "label": "HIV test",
        "description": "HIV screening result",
        "data_type": "enum",
        "required": False,
        "multiple": False,
        "allowed_values": ["positive", "negative"],
    },
]

DOC = ParsedDocument(
    sha256="abc123",
    parser_id="pymupdf-test",
    pages=(
        ParsedPage(1, "Patient MRN: 00123\nCollected: 2026-11-02"),
        ParsedPage(2, "Hemoglobin   13.4 g/dL\nOther notes"),
    ),
)


def test_schema_is_strict_and_stable():
    s1 = DocTypeSchema.from_rows("t1", "lab_report", ROWS)
    s2 = DocTypeSchema.from_rows("t1", "lab_report", list(reversed(ROWS)))
    assert s1.schema_hash == s2.schema_hash  # row order doesn't matter
    js = build_json_schema(s1)
    assert js["additionalProperties"] is False
    assert sorted(js["required"]) == sorted(r["key"] for r in ROWS)
    assert js["properties"]["hiv_result"]["properties"]["value"]["enum"] == [
        "positive",
        "negative",
        None,
    ]


def test_grounding_and_types():
    schema = DocTypeSchema.from_rows("t1", "lab_report", ROWS)
    out = {
        "patient_mrn": {"value": "00123", "evidence": {"page": 1, "quote": "Patient MRN: 00123"}},
        "hemoglobin": {"value": "13.4", "evidence": {"page": 2, "quote": "Hemoglobin 13.4 g/dL"}},
        # hallucinated: quote not on the claimed page
        "collected_on": {
            "value": "2026-11-02",
            "evidence": {"page": 2, "quote": "Collected: 2026-11-02"},
        },
        "hiv_result": {"value": None, "evidence": None},
    }
    facts = {f.field_key: f for f in verify_and_merge(schema, DOC, [out])}
    assert facts["patient_mrn"].status == "ok"
    assert facts["hemoglobin"].value_num == Decimal("13.4")  # whitespace normalized
    assert facts["collected_on"].status == "unverified"
    assert facts["hiv_result"].status == "missing"


def test_conflict_across_windows():
    schema = DocTypeSchema.from_rows("t1", "lab_report", ROWS)
    doc = ParsedDocument(
        "x", (ParsedPage(1, "Hemoglobin 13.4"), ParsedPage(2, "Hemoglobin 12.9")), "p"
    )
    w1 = {"hemoglobin": {"value": "13.4", "evidence": {"page": 1, "quote": "Hemoglobin 13.4"}}}
    w2 = {"hemoglobin": {"value": "12.9", "evidence": {"page": 2, "quote": "Hemoglobin 12.9"}}}
    hb = [f for f in verify_and_merge(schema, doc, [w1, w2]) if f.field_key == "hemoglobin"]
    assert {f.status for f in hb} == {"conflict"} and len(hb) == 2


def test_load_into_in_memory_duckdb():
    schema = DocTypeSchema.from_rows("t1", "lab_report", ROWS)
    out = {
        "hemoglobin": {"value": "13.4", "evidence": {"page": 2, "quote": "Hemoglobin 13.4 g/dL"}}
    }
    con = duckdb.connect(":memory:")
    load_facts(con, verify_and_merge(schema, DOC, [out]))
    row = con.execute(
        "SELECT value_num, unit FROM facts WHERE field_key = 'hemoglobin' AND status = 'ok'"
    ).fetchone()
    assert row == (Decimal("13.400000"), "g/dL")
