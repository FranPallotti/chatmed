"""M2 extraction: config (AC1), retries (AC3), recorded responses (AC4), subject join (AC7),
checkbox rule (AC8) and prompt (AC9). No network: FakeModelClient serves fixtures/recorded."""

from __future__ import annotations

import logging
import random
from dataclasses import replace
from datetime import date
from typing import Any

import duckdb
import httpx2 as httpx  # openai >= 3 uses httpx2; only to build SDK errors
import openai
import pytest

from chatmed.config import Settings
from chatmed.extraction import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    DataType,
    DocTypeSchema,
    ExtractionError,
    Fact,
    FieldDef,
    JoinStatus,
    PageLines,
    RetryPolicy,
    Status,
    apply_subject_join,
    build_json_schema,
    call_with_retries,
    extract_document,
    extract_for_tenant,
    fingerprint,
    ground_quote,
    is_label_quote,
    load_facts,
    match_subject,
    normalize_subject_id,
    verify_and_merge,
)
from chatmed.models import ParsedDocument, ParsedPage
from chatmed.parsing import PdfiumParser, TesseractOcrParser
from tests.conftest import fixture_bytes
from tests.fakes import (
    ACME_CIOMS_ROWS,
    GLOBEX_CIOMS_ROWS,
    SUBJIDS_BY_TENANT,
    FakeModelClient,
    FakeTenantRepository,
)

SECRET_MARKERS = ("01001-004", "Zentrobal", "ACM-2026-0042", "2026-06-12", "hospitalised")


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "azure_openai_endpoint": "https://example.openai.azure.com/",
        "azure_openai_deployment": "pinned-deployment",
        "model_seed": 7,
        "tenant_db_dsn_template": "postgresql://r_{tenant}@localhost/tenant_{tenant}",
    }
    return Settings(_env_file=None, **(base | overrides))  # type: ignore[arg-type]


async def no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture(scope="module")
def recorded() -> FakeModelClient:
    """Sync fixture: from_recordings parses the fixtures with its own event loop."""
    return FakeModelClient.from_recordings()


@pytest.fixture
def client(recorded: FakeModelClient) -> FakeModelClient:
    return FakeModelClient(recorded.responses)  # fresh request log per test


async def _pdf(name: str) -> ParsedDocument:
    return await PdfiumParser().parse(fixture_bytes(name))


async def _extract(
    client: FakeModelClient, name: str, tenant: str = "acme", doc: ParsedDocument | None = None
) -> dict[str, list[Fact]]:
    doc = doc or await _pdf(name)
    repo = FakeTenantRepository(tenant)
    result = await extract_for_tenant(client, repo, doc, "cioms", settings=make_settings())  # type: ignore[arg-type]
    by_key: dict[str, list[Fact]] = {}
    for fact in result.facts:
        by_key.setdefault(fact.field_key, []).append(fact)
    return by_key


def _status(facts: dict[str, list[Fact]], key: str) -> set[str]:
    return {f.status for f in facts[key]}


# ---------------------------------------------------------------- AC1: config on every call


async def test_every_call_uses_rule_3_settings_from_config(client: FakeModelClient) -> None:
    await _extract(client, "cioms_initial.pdf")
    (request,) = client.requests
    assert request["model"] == "pinned-deployment"
    assert request["temperature"] == 0 and request["seed"] == 7
    assert request["max_completion_tokens"] == 4000
    assert request["timeout"] == 60.0
    fmt = request["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert "store" not in request  # rule 6: stateless
    assert request["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}


async def test_windows_follow_extraction_max_chars(client: FakeModelClient) -> None:
    doc = await _pdf("cioms_followup.pdf")
    schema = DocTypeSchema.from_rows("acme", "cioms", ACME_CIOMS_ROWS)
    fake = FakeModelClient(default={})
    settings = make_settings(extraction_max_chars=100)  # each page is its own window
    await extract_document(fake, doc, schema, [], settings=settings)  # type: ignore[arg-type]
    users = [r["messages"][1]["content"] for r in fake.requests]
    assert len(users) == 2 and "[Page 1]" in users[0] and "[Page 2]" in users[1]


# ---------------------------------------------------------------- AC3: retries, timeout, logs


def _status_error(code: int) -> openai.APIStatusError:
    response = httpx.Response(code, request=httpx.Request("POST", "https://example.invalid"))
    cls = openai.RateLimitError if code == 429 else openai.APIStatusError
    return cls("error", response=response, body=None)


def _request() -> dict[str, Any]:
    return {"model": "m", "messages": [], "response_format": {}}


async def test_retries_429_and_5xx_with_growing_jittered_backoff() -> None:
    fake = FakeModelClient(errors=[_status_error(429), _status_error(503)], default={})
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    policy = RetryPolicy(max_attempts=4, base_seconds=1.0, max_seconds=20.0)
    resp = await call_with_retries(
        fake,
        _request(),
        policy,
        doc_sha256="abc",
        sleep=sleep,
        rng=random.Random(0),  # type: ignore[arg-type]
    )
    assert resp.choices[0].finish_reason == "stop"
    assert len(fake.requests) == 3 and len(sleeps) == 2
    assert 0 <= sleeps[0] <= 1.0 and 0 <= sleeps[1] <= 2.0  # full jitter, doubling cap


def test_backoff_never_exceeds_the_cap() -> None:
    policy = RetryPolicy(base_seconds=1.0, max_seconds=5.0)
    rng = random.Random(1)
    assert all(policy.delay(retry, rng) <= 5.0 for retry in range(10) for _ in range(50))


async def test_client_errors_are_not_retried() -> None:
    fake = FakeModelClient(errors=[_status_error(400)], default={})
    with pytest.raises(ExtractionError, match="http 400"):
        await call_with_retries(fake, _request(), RetryPolicy(), doc_sha256="abc", sleep=no_sleep)  # type: ignore[arg-type]
    assert len(fake.requests) == 1


async def test_gives_up_after_max_attempts_without_chained_errors() -> None:
    errors = [_status_error(500) for _ in range(3)]
    fake = FakeModelClient(errors=errors, default={})
    with pytest.raises(ExtractionError, match="after 3 attempts") as info:
        await call_with_retries(
            fake,
            _request(),
            RetryPolicy(max_attempts=3),
            doc_sha256="abc",
            sleep=no_sleep,  # type: ignore[arg-type]
        )
    assert info.value.__cause__ is None and info.value.__context__ is None


async def test_timeouts_and_connection_errors_are_retried() -> None:
    req = httpx.Request("POST", "https://example.invalid")
    fake = FakeModelClient(errors=[openai.APITimeoutError(req)], default={})
    await call_with_retries(fake, _request(), RetryPolicy(), doc_sha256="abc", sleep=no_sleep)  # type: ignore[arg-type]
    assert len(fake.requests) == 2


@pytest.mark.parametrize(
    ("finish_reason", "content"),
    [("length", '{"a": 1'), ("stop", "not json Zentrobal 01001-004"), ("stop", "[1, 2]")],
)
async def test_unusable_output_raises_without_content(finish_reason: str, content: str) -> None:
    from types import SimpleNamespace

    from chatmed.extraction import _parse_output

    message = SimpleNamespace(content=content, refusal=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason, message=message)])
    with pytest.raises(ExtractionError) as info:
        _parse_output(resp)
    assert "Zentrobal" not in str(info.value) and info.value.__context__ is None


async def test_logs_carry_metadata_only(
    client: FakeModelClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await _extract(client, "cioms_initial.pdf")
    calls = [r for r in caplog.records if r.msg == "llm_call"]
    assert len(calls) == 1
    assert calls[0].tokens_in == 1000 and calls[0].attempt == 1  # type: ignore[attr-defined]
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    leaked = [m for m in SECRET_MARKERS if m in text]
    assert not leaked


# ---------------------------------------------------------------- AC4: recorded responses


async def test_recorded_initial_form(client: FakeModelClient) -> None:
    facts = await _extract(client, "cioms_initial.pdf")
    ok = {k for k, fs in facts.items() if {f.status for f in fs} == {Status.OK}}
    assert ok == {
        "age", "concomitant_drugs", "country", "daily_dose", "date_received",
        "hospitalisation", "indication", "mfr_control_no", "reaction_description",
        "reaction_onset_date", "route", "sex", "subject_id", "suspect_drug",
        "therapy_start_date",
    }  # fmt: skip
    undeterminable = {"dechallenge", "disability", "life_threatening", "patient_died"}
    undeterminable |= {"rechallenge", "report_type"}
    assert {k for k, fs in facts.items() if _status(facts, k) == {"undeterminable"}} == (
        undeterminable
    )
    assert facts["age"][0].value_num == 47
    assert facts["reaction_onset_date"][0].value_date == date(2026, 6, 12)
    # multiple values: deduplicated, ordered by page then value (deterministic, not model order)
    assert [f.value_text for f in facts["concomitant_drugs"]] == [
        "Atorvastatin 20 mg",
        "Metformin 850 mg",
    ]
    subject = facts["subject_id"][0]
    assert (subject.join_status, subject.join_value) == (JoinStatus.JOINED, "01001-004")


async def test_recorded_followup_reports_onset_conflict(client: FakeModelClient) -> None:
    facts = await _extract(client, "cioms_followup.pdf")
    onset = facts["reaction_onset_date"]
    assert _status(facts, "reaction_onset_date") == {"conflict"}
    assert [(f.value_text, f.page) for f in onset] == [("2026-06-12", 1), ("2026-06-11", 2)]
    assert _status(facts, "report_type") == {"undeterminable"}


async def test_recorded_globex_uses_its_own_fields_and_ids(client: FakeModelClient) -> None:
    facts = await _extract(client, "cioms_initial.pdf", tenant="globex")
    assert set(facts) == {r["key"] for r in GLOBEX_CIOMS_ROWS}
    assert facts["subject_id"][0].join_status == JoinStatus.UNMATCHED  # acme subject
    assert _status(facts, "serious") == {"ok"} and facts["serious"][0].value_bool is True


async def test_same_inputs_give_identical_facts(client: FakeModelClient) -> None:
    runs = [await _extract(client, "cioms_followup.pdf") for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


async def test_facts_load_into_in_memory_duckdb(client: FakeModelClient) -> None:
    doc = await _pdf("cioms_initial.pdf")
    repo = FakeTenantRepository("acme")
    result = await extract_for_tenant(client, repo, doc, "cioms", settings=make_settings())  # type: ignore[arg-type]
    con = duckdb.connect(":memory:")
    load_facts(con, result.facts)
    row = con.execute(
        "SELECT join_status, join_value FROM facts WHERE field_key = 'subject_id'"
    ).fetchone()
    assert row == ("joined", "01001-004")
    assert result.fingerprint == fingerprint(
        doc,
        DocTypeSchema.from_rows("acme", "cioms", ACME_CIOMS_ROWS),
        "pinned-deployment",
        7,
    )


async def test_recorded_scanned_form(client: FakeModelClient, tesseract_cmd: str) -> None:
    ocr = TesseractOcrParser(dpi=300, lang="eng", cmd=tesseract_cmd)
    doc = await ocr.parse(fixture_bytes("cioms_initial_scanned.pdf"))
    facts = await _extract(client, "cioms_initial_scanned.pdf", doc=doc)
    assert _status(facts, "hospitalisation") == {"ok"}
    for key in ("dechallenge", "rechallenge", "report_type"):  # quotes are box labels + noise
        assert _status(facts, key) == {"undeterminable"}, key
    assert facts["subject_id"][0].join_value == "01001-004"


# ---------------------------------------------------------------- AC7: subject join


@pytest.mark.parametrize(
    "raw",
    ["01001-004", "101-01001-004", "Subject ID 101-01001-004", "101 / 01001_004", "101.01001.004"],
)
def test_subject_id_matches_with_prefix_or_other_separators(raw: str) -> None:
    assert match_subject(raw, SUBJIDS_BY_TENANT["acme"]) == (JoinStatus.JOINED, "01001-004")


def test_subject_id_without_match() -> None:
    assert match_subject("101-09999-004", SUBJIDS_BY_TENANT["acme"]) == (
        JoinStatus.UNMATCHED,
        None,
    )


def test_subject_suffix_must_start_at_a_separator() -> None:
    assert match_subject("1001-004", ["01001-004"])[0] == JoinStatus.UNMATCHED
    assert match_subject("X01001-004", ["01001-004"])[0] == JoinStatus.UNMATCHED


def test_two_matching_subjects_are_ambiguous() -> None:
    assert match_subject("101-01001-004", ["01001-004", "101-01001-004"]) == (
        JoinStatus.AMBIGUOUS,
        None,
    )


def test_normalize_subject_id() -> None:
    assert normalize_subject_id(" 101 /01001__004 ") == "101-01001-004"


def _subject_facts(*statuses: str) -> tuple[DocTypeSchema, list[Fact]]:
    schema = DocTypeSchema.from_rows("acme", "cioms", ACME_CIOMS_ROWS)
    base = Fact("sha", "cioms", "subject_id", "101-01001-004", None, None, None, None, 1, "q", "ok")
    return schema, [replace(base, status=s) for s in statuses]


@pytest.mark.parametrize("status", ["unverified", "invalid", "missing"])
def test_join_not_attempted_unless_subject_id_is_ok(status: str) -> None:
    schema, facts = _subject_facts(status)
    (fact,) = apply_subject_join(schema, facts, SUBJIDS_BY_TENANT["acme"])
    assert (fact.join_status, fact.join_value) == (JoinStatus.NOT_ATTEMPTED, None)
    assert fact.status == status  # the reason travels with the fact


def test_conflicting_subject_ids_are_ambiguous() -> None:
    schema, facts = _subject_facts("conflict", "conflict")
    joined = apply_subject_join(schema, facts, SUBJIDS_BY_TENANT["acme"])
    assert {(f.join_status, f.join_value) for f in joined} == {(JoinStatus.AMBIGUOUS, None)}


# ---------------------------------------------------------------- AC8: checkbox rule (D13a)

ACME = DocTypeSchema.from_rows("acme", "cioms", ACME_CIOMS_ROWS)
HOSPITALISATION = next(f for f in ACME.fields if f.key == "hospitalisation")


@pytest.mark.parametrize(
    "quote",
    [
        "involved or prolonged inpatient hospitalisation",  # full label
        "prolonged inpatient hospitalisation",  # fragment
        "X] involved or prolonged | inpatient hospitalisation .",  # label + OCR noise
        "Inpatient HOSPITALISATION, prolonged",  # other order and case
        "",
    ],
)
def test_label_quotes_are_not_statements(quote: str) -> None:
    assert is_label_quote(HOSPITALISATION, quote)


@pytest.mark.parametrize(
    "quote", ["The patient was hospitalised on 2026-06-13", "The patient was not hospitalised"]
)
def test_written_statements_are_accepted(quote: str) -> None:
    assert not is_label_quote(HOSPITALISATION, quote)


def _checkbox_fact(quote: str, value: bool) -> list[Fact]:
    schema = DocTypeSchema.from_rows("acme", "cioms", ACME_CIOMS_ROWS)
    doc = ParsedDocument("sha", (ParsedPage(1, f"8-12. {quote}"),), "p")
    out = {"hospitalisation": {"value": value, "evidence": {"page": 1, "quote": quote}}}
    return [f for f in verify_and_merge(schema, doc, [out]) if f.field_key == "hospitalisation"]


@pytest.mark.parametrize(
    "quote",
    [
        "involved or prolonged inpatient hospitalisation",
        "prolonged inpatient hospitalisation",
        "X] involved or prolonged inpatient hospitalisation",
    ],
)
def test_checkbox_from_label_quote_is_undeterminable(quote: str) -> None:
    (fact,) = _checkbox_fact(quote, True)
    assert fact.status == Status.UNDETERMINABLE and fact.value_bool is None


def test_checkbox_from_narrative_is_ok() -> None:
    (fact,) = _checkbox_fact("The patient was hospitalised on 2026-06-13", True)
    assert (fact.status, fact.value_bool) == (Status.OK, True)


def test_written_negation_is_ok_false() -> None:
    (fact,) = _checkbox_fact("The patient was not hospitalised", False)
    assert (fact.status, fact.value_bool) == (Status.OK, False)


def test_checkbox_never_missing_or_unverified() -> None:
    schema = DocTypeSchema.from_rows("acme", "cioms", ACME_CIOMS_ROWS)
    doc = ParsedDocument("sha", (ParsedPage(1, "nothing here"),), "p")
    hallucinated = {"value": True, "evidence": {"page": 1, "quote": "was hospitalised"}}
    for out in ({}, {"hospitalisation": hallucinated}):
        facts = [
            f for f in verify_and_merge(schema, doc, [out]) if f.field_key == "hospitalisation"
        ]
        assert [f.status for f in facts] == [Status.UNDETERMINABLE]


def test_enum_options_count_as_label_words() -> None:
    dechallenge = FieldDef(
        "dechallenge", "Abated after stopping",
        "Box 20: did the reaction abate after stopping the drug?",
        DataType.ENUM,
        allowed_values=("yes", "no", "na"), checkbox=True,
    )  # fmt: skip
    assert is_label_quote(dechallenge, "20. Did reaction abate after stopping drug? yes no NA")
    assert not is_label_quote(dechallenge, "The rash resolved after Zentrobal was stopped")


# ---------------------------------------------------------------- AC8: box labels (D13b)

PATIENT_DIED = next(f for f in ACME.fields if f.key == "patient_died")
HEADING = "8-12. Check all appropriate to adverse reaction: patient died"


def _fact_for(key: str, page_text: str, quote: str, value: bool) -> list[Fact]:
    doc = ParsedDocument("sha", (ParsedPage(1, page_text),), "p")
    out = {key: {"value": value, "evidence": {"page": 1, "quote": quote}}}
    return [f for f in verify_and_merge(ACME, doc, [out]) if f.field_key == key]


def test_heading_ending_in_the_box_label_is_undeterminable() -> None:
    """The real model said patient_died=true from this heading; the D13a rule alone let it in."""
    (fact,) = _fact_for("patient_died", HEADING, HEADING, True)
    assert fact.status == Status.UNDETERMINABLE and fact.value_bool is None


@pytest.mark.parametrize(
    "quote",
    [
        HEADING,
        "X] patient died",  # label + OCR noise
        "Patient   DIED.",  # case, spacing, punctuation
        "check appropriate | patient died | LJ",  # interleaved columns
    ],
)
def test_quote_containing_a_box_label_is_a_label_quote(quote: str) -> None:
    assert is_label_quote(PATIENT_DIED, quote)


def test_statement_with_part_of_the_label_is_accepted() -> None:
    assert not is_label_quote(PATIENT_DIED, "The patient recovered and was discharged")
    (fact,) = _fact_for(
        "patient_died", "The patient did not die.", "The patient did not die", False
    )
    assert (fact.status, fact.value_bool) == (Status.OK, False)


def test_known_limitation_narrative_repeating_the_label_is_undeterminable() -> None:
    """D13b: a written statement that repeats a printed label word for word is rejected too."""
    text = "Unfortunately the patient died on 2026-06-20."
    (fact,) = _fact_for("patient_died", text, text, True)
    assert fact.status == Status.UNDETERMINABLE


def test_any_of_several_box_labels_matches() -> None:
    globex = DocTypeSchema.from_rows("globex", "cioms", GLOBEX_CIOMS_ROWS)
    serious = next(f for f in globex.fields if f.key == "serious")
    assert is_label_quote(serious, "8-12. Check all appropriate: life threatening")
    assert is_label_quote(serious, "involved or prolonged inpatient hospitalisation X]")
    assert not is_label_quote(serious, "The patient was hospitalised on 2026-06-13")


def test_every_seeded_checkbox_field_has_box_labels() -> None:
    """Mirrors the schema CHECK (NOT checkbox OR cardinality(box_labels) > 0)."""
    for rows in (ACME_CIOMS_ROWS, GLOBEX_CIOMS_ROWS):
        for f in DocTypeSchema.from_rows("t", "cioms", rows).fields:
            assert not f.checkbox or f.box_labels, f.key


def test_box_labels_change_the_schema_hash() -> None:
    rows = [dict(r) for r in ACME_CIOMS_ROWS]
    before = DocTypeSchema.from_rows("acme", "cioms", rows).schema_hash
    next(r for r in rows if r["key"] == "patient_died")["box_labels"] = ["died"]
    assert DocTypeSchema.from_rows("acme", "cioms", rows).schema_hash != before


# ---------------------------------------------------------------- AC9: prompt


def test_prompt_mentions_ocr_columns_and_short_quotes() -> None:
    prompt = SYSTEM_PROMPT.lower()
    for words in ("ocr", "noise", "interleaved", "columns", "short fragment", "checkbox"):
        assert words in prompt, words
    assert PROMPT_VERSION == "extract-v2"


def test_checkbox_fields_carry_the_hint_in_the_schema() -> None:
    schema = DocTypeSchema.from_rows("acme", "cioms", ACME_CIOMS_ROWS)
    props = build_json_schema(schema)["properties"]
    assert "checkbox on the form" in props["hospitalisation"]["description"]
    assert "checkbox on the form" not in props["age"]["description"]


def test_prompt_version_changes_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = ParsedDocument("sha", (), "p")
    schema = DocTypeSchema.from_rows("acme", "cioms", ACME_CIOMS_ROWS)
    before = fingerprint(doc, schema, "d", 7)
    monkeypatch.setattr("chatmed.extraction.PROMPT_VERSION", "extract-v3")
    assert fingerprint(doc, schema, "d", 7) != before


# ---------------------------------------------------------------- evidence = full line (D13c)

FORM = (
    "1a. Country: Argentina\n2a. Age:   47 years   3. Sex: F\n\n"
    "7+13. Reaction:\nacute liver injury\n"
)


@pytest.mark.parametrize(
    "quote", ["Age: 47", "2a. Age: 47 years", "47 years 3. Sex", "2A. AGE:\t47 YEARS 3. SEX: F"]
)
def test_any_quote_from_a_line_is_stored_as_that_whole_line(quote: str) -> None:
    assert ground_quote(PageLines.of(FORM), quote) == "2a. Age: 47 years 3. Sex: F"


def test_quote_across_lines_gives_all_of_them() -> None:
    got = ground_quote(PageLines.of(FORM), "Reaction: acute liver")
    assert got == "7+13. Reaction: acute liver injury"


@pytest.mark.parametrize("quote", ["Age: 48", "", "   "])
def test_quote_not_on_the_page_is_not_grounded(quote: str) -> None:
    assert ground_quote(PageLines.of(FORM), quote) is None


def test_first_occurrence_wins() -> None:
    page = PageLines.of("Dose: 20 mg\nother line\nDose: 20 mg daily")
    assert ground_quote(page, "Dose: 20 mg") == "Dose: 20 mg"


def test_runs_with_different_quotes_give_identical_facts() -> None:
    doc = ParsedDocument("sha", (ParsedPage(1, FORM),), "p")
    short = {"age": {"value": 47, "evidence": {"page": 1, "quote": "Age: 47 years"}}}
    long = {"age": {"value": 47, "evidence": {"page": 1, "quote": "2a. Age: 47 years 3. Sex: F"}}}
    assert verify_and_merge(ACME, doc, [short]) == verify_and_merge(ACME, doc, [long])


def test_label_fragment_on_a_shared_line_is_still_undeterminable() -> None:
    """The model's own words are checked too: the line adds words the quote does not have."""
    text = "Concomitant: Metformin 850 mg prolonged inpatient hospitalisation"
    (fact,) = _fact_for("hospitalisation", text, "prolonged inpatient hospitalisation", True)
    assert fact.status == Status.UNDETERMINABLE


# ---------------------------------------------------------------- merge order (D6)

TWO_LINES = "14. Suspect drug: Zentrobal\n2026-06-12 onset\nNarrative: Zentrobal was stopped\n"


def _item(value: Any, quote: str) -> dict[str, Any]:
    return {"value": value, "evidence": {"page": 1, "quote": quote}}


def test_same_value_on_two_lines_keeps_the_same_evidence_in_any_item_order() -> None:
    doc = ParsedDocument("sha", (ParsedPage(1, TWO_LINES),), "p")
    box, narrative = (
        _item("Zentrobal", "Suspect drug: Zentrobal"),
        _item("Zentrobal", "Zentrobal was"),
    )
    a = verify_and_merge(ACME, doc, [{"suspect_drug": [box, narrative]}])
    b = verify_and_merge(ACME, doc, [{"suspect_drug": [narrative, box]}])
    assert a == b
    (fact,) = [f for f in a if f.field_key == "suspect_drug"]
    assert fact.quote == "14. Suspect drug: Zentrobal"  # page, value, then quote order


def test_repeated_value_does_not_add_a_conflict_fact() -> None:
    page = "Onset: 2026-06-12\nCorrected onset: 2026-06-11\nonset 2026-06-12 again\n"
    doc = ParsedDocument("sha", (ParsedPage(1, page),), "p")
    first = _item("2026-06-12", "Onset: 2026-06-12")
    others = [_item("2026-06-11", "Corrected onset: 2026-06-11"), _item("2026-06-12", "again")]
    out = {"reaction_onset_date": {**first, "other_statements": others}}
    facts = [f for f in verify_and_merge(ACME, doc, [out]) if f.field_key == "reaction_onset_date"]
    assert [(f.status, f.value_text, f.quote) for f in facts] == [
        (Status.CONFLICT, "2026-06-11", "Corrected onset: 2026-06-11"),
        (Status.CONFLICT, "2026-06-12", "Onset: 2026-06-12"),
    ]
