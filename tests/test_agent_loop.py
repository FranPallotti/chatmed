"""M3 AC2 (loop), AC3 (verified answers), AC4 (statuses stated), AC8 (demo questions)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx  # openai >= 3 uses httpx2; only to build SDK errors
import openai
import pytest

from chatmed.agent import AGENT_PROMPT_VERSION, AgentError, Answer, run_agent
from chatmed.agent.answer import unstated_statuses, unsupported_numbers
from chatmed.agent.tools import EvidenceItem
from tests.agent_support import AgentSetup, setup_session
from tests.fakes import FakeModelClient, ScriptedModelClient, final, tool_call
from tests.test_extraction_m2 import make_settings, no_sleep

MARKERS = ("01001-004", "Zentrobal", "ACM-2026-0042", "2026-06-12", "hospitalised")


@pytest.fixture(scope="module")
def recorded() -> FakeModelClient:
    return FakeModelClient.from_recordings()


async def ask(setup: AgentSetup, client: ScriptedModelClient, question: str) -> Answer:
    return await run_agent(
        client,  # type: ignore[arg-type]
        setup.session,
        setup.ctx(),
        question,
        settings=setup.settings,
        sleep=no_sleep,
    )


def ids(answer: Answer) -> list[str]:
    return [f["id"] for f in answer.facts_used]


def tool_messages(request: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(m["content"]) for m in request["messages"] if m["role"] == "tool"]


# ---------------------------------------------------------------- AC8: the three demo questions


def arm_script(sha: str) -> list[Any]:
    return [
        tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["subject_id"]}),
        tool_call("get_subject", {"subjid": "01001-004"}),
        final(
            "Subject 01001-004 is in the Zentrobal 20 mg arm (actual treatment Zentrobal 20 mg).",
            [f"fact:{sha[:12]}:subject_id:1", "adsl:01001-004"],
        ),
    ]


def hospitalised_script(sha: str) -> list[Any]:
    return [
        tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["hospitalisation"]}),
        final(
            "Yes. The report states the patient was hospitalised on 2026-06-13 (page 1).",
            [f"fact:{sha[:12]}:hospitalisation:1"],
        ),
    ]


def reconcile_script(sha: str) -> list[Any]:
    """agent-v7: the model only calls reconcile_case; code renders the answer (D14)."""
    return [tool_call("reconcile_case", {"doc_sha": sha[:12]})]


async def test_demo_arm_question(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    sha = setup.shas["cioms_initial.pdf"]
    answer = await ask(setup, ScriptedModelClient(arm_script(sha)), "What arm is the subject in?")
    assert answer.verified
    assert ids(answer) == ["adsl:01001-004", f"fact:{sha[:12]}:subject_id:1"]
    adsl = answer.facts_used[0]
    assert adsl["values"]["arm"] == "Zentrobal 20 mg"
    assert adsl["citation"] == {"table": "adsl", "key": "01001-004"}


async def test_demo_hospitalised_question(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    sha = setup.shas["cioms_initial.pdf"]
    client = ScriptedModelClient(hospitalised_script(sha))
    answer = await ask(setup, client, "Was the subject hospitalised?")
    assert answer.verified
    (used,) = answer.facts_used
    assert (used["values"]["status"], used["values"]["value"]) == ("ok", "true")
    assert used["citation"] == {
        "doc": sha[:12],
        "page": 1,
        "quote": "The patient was hospitalised on 2026-06-13.",
    }


async def test_demo_reconciliation_question(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    sha = setup.shas["cioms_initial.pdf"]
    client = ScriptedModelClient(reconcile_script(sha))
    answer = await ask(setup, client, "Does this CIOMS match what ADAE has for this subject?")
    assert answer.verified
    statuses = {
        f["values"]["item"]: f["values"]["status"]
        for f in answer.facts_used
        if f["source"] == "reconciliation"
    }
    assert statuses == {
        "onset_date": "match",
        "seriousness": "match",
        "action_taken": "not_comparable",
        "outcome": "not_comparable",
    }


async def test_demo_reconciliation_on_followup_reports_onset_conflict(
    recorded: FakeModelClient,
) -> None:
    setup = await setup_session(recorded, ["cioms_followup.pdf"])
    s = setup.shas["cioms_followup.pdf"][:12]
    script = reconcile_script(setup.shas["cioms_followup.pdf"])
    answer = await ask(setup, ScriptedModelClient(script), "Does this CIOMS match ADAE?")
    assert answer.verified
    onset = next(f for f in answer.facts_used if f["id"] == f"recon:{s}:onset_date")
    assert (onset["values"]["status"], onset["values"]["reason"]) == ("not_comparable", "conflict")


async def test_same_script_same_facts_used(recorded: FakeModelClient) -> None:
    """Two sessions, same document and question → identical facts_used (D6)."""
    answers = []
    for _ in range(2):
        setup = await setup_session(recorded, ["cioms_initial.pdf"])
        sha = setup.shas["cioms_initial.pdf"]
        answers.append(await ask(setup, ScriptedModelClient(reconcile_script(sha)), "Match?"))
    assert answers[0].facts_used == answers[1].facts_used


# ---------------------------------------------------------------- AC2: loop settings and limits


async def test_every_call_is_stateless_with_rule_3_settings(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    client = ScriptedModelClient(arm_script(setup.shas["cioms_initial.pdf"]))
    await ask(setup, client, "What arm?")
    assert len(client.requests) == 3
    for request in client.requests:
        assert request["model"] == "pinned-deployment"
        assert request["temperature"] == 0 and request["seed"] == 7
        assert request["parallel_tool_calls"] is False
        assert "store" not in request and "thread_id" not in request
        assert request["response_format"]["json_schema"]["strict"] is True
        assert request["messages"][0]["role"] == "system"
    # each call carries the whole conversation so far: no server-side state (rule 6)
    assert [len(r["messages"]) for r in client.requests] == [3, 5, 7]


async def test_history_is_resent_on_the_next_question(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    sha = setup.shas["cioms_initial.pdf"]
    client = ScriptedModelClient(arm_script(sha) + hospitalised_script(sha))
    await ask(setup, client, "What arm?")
    await ask(setup, client, "Hospitalised?")
    second = client.requests[3]["messages"]
    assert [m["role"] for m in second] == ["system", "system", "user", "assistant", "user"]
    assert second[2]["content"] == "What arm?"


async def test_session_context_names_documents_without_content(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf", "cioms_followup.pdf"])
    client = ScriptedModelClient([final("No.", [])])
    await ask(setup, client, "Anything?")
    context = client.requests[0]["messages"][1]["content"]
    pages = {setup.shas["cioms_initial.pdf"]: 1, setup.shas["cioms_followup.pdf"]: 2}
    expected = ["Documents in this session:"] + [
        f"- doc_sha {sha[:12]}: cioms, {pages[sha]} page(s)" for sha in sorted(pages)
    ]
    assert context.splitlines()[:3] == expected
    keys = context.splitlines()[3]
    assert keys.startswith("Field keys for cioms: ") and "hospitalisation" in keys
    assert not any(m in context for m in MARKERS)


async def test_two_json_objects_are_rejected_then_retried(recorded: FakeModelClient) -> None:
    """Seen with the real model: two answer objects back to back in one message."""
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    two = SimpleNamespace(content='{"text": "a"} {"text": "b"}', refusal=None, tool_calls=None)
    bad = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=two)], usage=None)
    client = ScriptedModelClient([lambda _request: bad, final("No.", [])])
    answer = await ask(setup, client, "Anything?")
    assert answer.verified and answer.text == "No."
    assert "answer_not_one_json_object" in client.requests[1]["messages"][-1]["content"]


async def test_max_iterations_ends_with_the_fallback(recorded: FakeModelClient) -> None:
    settings = make_settings(agent_max_iterations=3)
    setup = await setup_session(recorded, ["cioms_initial.pdf"], settings=settings)
    loop = [tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["age"]})] * 5
    client = ScriptedModelClient(loop)
    answer = await ask(setup, client, "Age?")
    assert len(client.requests) == 3
    assert (answer.verified, answer.reason) == (False, "max_iterations")


def _status_error(code: int) -> openai.APIStatusError:
    response = httpx.Response(code, request=httpx.Request("POST", "https://example.invalid"))
    cls = openai.RateLimitError if code == 429 else openai.APIStatusError
    return cls("error", response=response, body=None)


async def test_429_is_retried_and_400_is_not(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    sha = setup.shas["cioms_initial.pdf"]
    client = ScriptedModelClient([_status_error(429), *hospitalised_script(sha)])
    assert (await ask(setup, client, "Hospitalised?")).verified
    client = ScriptedModelClient([_status_error(400)])
    with pytest.raises(AgentError) as info:
        await ask(setup, client, "Hospitalised?")
    assert info.value.code == "model_rejected" and info.value.__cause__ is None


async def test_per_call_timeout_is_passed(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    seen: list[Any] = []

    class Spy(ScriptedModelClient):
        async def create(self, **request: Any) -> Any:
            seen.append(request.pop("timeout"))
            return await super().create(**request)

    client = Spy(hospitalised_script(setup.shas["cioms_initial.pdf"]))
    await ask(setup, client, "Hospitalised?")
    assert seen == [60.0, 60.0]


async def test_tool_errors_reach_the_model_as_codes(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])

    async def broken(_subjid: str) -> Any:
        raise RuntimeError("Zentrobal 01001-004 hospitalised")  # content in the message

    setup.repo.subject = broken  # type: ignore[method-assign]
    script = [
        tool_call("get_subject", {"subjid": "01001-001"}),
        tool_call("get_subject", {"subjid": "01001-004"}),
        final("I could not look up the subject.", []),
    ]
    client = ScriptedModelClient(script)
    await ask(setup, client, "Arm?")
    assert tool_messages(client.requests[2]) == [
        {"error": "subject_not_joined"},
        {"error": "tool_failed"},
    ]


async def test_agent_logs_are_metadata_only(
    recorded: FakeModelClient, caplog: pytest.LogCaptureFixture
) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    sha = setup.shas["cioms_initial.pdf"]
    with caplog.at_level(logging.DEBUG, logger="chatmed"):
        await ask(setup, ScriptedModelClient(reconcile_script(sha)), "Match for 01001-004?")
    records = [r for r in caplog.records if r.name.startswith("chatmed.agent")]
    assert {r.getMessage() for r in records} >= {
        "agent.llm_call",
        "agent.tool_call",
        "agent.answer",
    }
    assert all(
        r.__dict__.get("prompt_version", AGENT_PROMPT_VERSION) == AGENT_PROMPT_VERSION
        for r in records
    )
    for r in records:
        dumped = repr({k: v for k, v in r.__dict__.items() if k != "exc_info"})
        assert not any(m in dumped for m in MARKERS)


# ---------------------------------------------------------------- AC3: code check on answers


async def test_value_not_in_facts_used_is_retried_once(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    s = setup.shas["cioms_initial.pdf"][:12]
    script = [
        tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["age"]}),
        final("The patient is 48 years old.", [f"fact:{s}:age:1"]),
        final("The patient is 47 years old.", [f"fact:{s}:age:1"]),
    ]
    client = ScriptedModelClient(script)
    answer = await ask(setup, client, "Age?")
    assert answer.verified and answer.text == "The patient is 47 years old."
    retry_note = client.requests[2]["messages"][-1]
    assert retry_note["role"] == "user" and "value_not_in_facts_used" in retry_note["content"]


async def test_second_rejection_returns_the_safe_fallback(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    s = setup.shas["cioms_initial.pdf"][:12]
    script = [
        tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["age"]}),
        final("The patient is 48 years old.", [f"fact:{s}:age:1"]),
        final("Onset was 2026-06-01.", [f"fact:{s}:age:1"]),
    ]
    answer = await ask(setup, ScriptedModelClient(script), "Age?")
    assert (answer.verified, answer.reason) == (False, "value_not_in_facts_used")
    assert "48" not in answer.text and "2026-06-01" not in answer.text
    assert f"fact:{s}:age:1: " in answer.text and "value=47" in answer.text
    assert ids(answer) == [f"fact:{s}:age:1"]


async def test_unknown_cited_id_is_rejected(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    script = [final("Arm is placebo.", ["adsl:01001-002"]), final("Arm is placebo.", ["x"])]
    answer = await ask(setup, ScriptedModelClient(script), "Arm?")
    assert (answer.verified, answer.reason) == (False, "unknown_cited_id")
    assert answer.facts_used == []


async def test_page_text_quotes_are_grounded(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_followup.pdf"])
    s = setup.shas["cioms_followup.pdf"][:12]
    script = [
        tool_call("search_document_text", {"query": "corrected onset", "doc_sha": None}),
        final(
            "Page 2 corrects the onset.",
            [],
            [{"page_id": f"page:{s}:2", "quote": "Zentrobal was given IV"}],
        ),
        final(
            "Page 2 corrects the onset date to 2026-06-11.",
            [],
            [{"page_id": f"page:{s}:2", "quote": "corrected reaction onset date"}],
        ),
    ]
    client = ScriptedModelClient(script)
    answer = await ask(setup, client, "Was anything corrected?")
    assert answer.verified
    (quote,) = answer.facts_used
    assert quote["id"] == f"quote:{s}:2:1"
    assert quote["citation"] == {
        "doc": s,
        "page": 2,
        "quote": "Corrected reaction onset date: 2026-06-11",
    }
    assert "ungrounded_quote" in client.requests[2]["messages"][-1]["content"]


def _item(values: dict[str, Any], citation: dict[str, Any] | None = None) -> EvidenceItem:
    return EvidenceItem("x", "document", values, citation or {})


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("Age 47, onset 2026-06-12, page 1.", []),
        ("Age 47.0", []),
        ("Subject 01001-004", []),
        ("Age 74", ["74"]),
        ("Onset 2026-06-21", ["2026-06-21"]),
        ("Dose 20.5 mg", ["20.5"]),
        ("Onset June 12, 2026 (12-JUN-2026, 12/06/2026, 12 June).", []),
        ("Onset June 6, 2026.", ["June 6, 2026"]),  # 6 and 2026 occur, the date does not
        ("Onset 13/06/2026", ["13/06/2026"]),
        ("Onset 31/02/2026", ["31/02/2026"]),  # not a real date
        ("Onset on June 13", ["June 13"]),
        ("Doc 7652, row 457661", ["457661", "7652"]),  # digits of a sha prefix are not values
    ],
)
def test_number_check(text: str, bad: list[str]) -> None:
    items = [
        _item(
            {"doc": "7652ea457661", "value": "47", "join_value": "01001-004"},
            {"doc": "7652ea457661", "page": 1, "key": "457661"},
        ),
        _item({"value": "2026-06-12", "dose": "20 mg"}),
    ]
    assert unsupported_numbers(text, items) == bad


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("Onset 12/06/2026.", []),  # repeated as written: the ambiguity stays visible
        ("Onset 12.06.2026.", []),
        ("Onset 2026-06-12.", ["2026-06-12"]),  # the source does not decide 12 June ...
        ("Onset 2026-12-06.", ["2026-12-06"]),  # ... nor 6 December
        ("Onset 6 December 2026.", ["6 December 2026"]),
        ("Stop 2026-06-25.", []),  # 25/06/2026 has one reading only
    ],
)
def test_ambiguous_source_date_supports_no_reading(text: str, bad: list[str]) -> None:
    items = [_item({"quote": "Onset 12/06/2026"}), _item({"quote": "Stop 25/06/2026"})]
    assert unsupported_numbers(text, items) == bad


# ---------------------------------------------------------------- AC4: statuses stated plainly


@pytest.mark.parametrize(
    ("text", "unstated"),
    [
        ("There is a conflict between pages 1 and 2.", []),
        ("The values are conflicting.", []),
        ("There is no conflict in the onset date.", ["conflict"]),
        ("The onset is not in conflict.", ["conflict"]),
        ("Without any conflict.", ["conflict"]),
        ("No conflict on page 1, but there is a conflict on page 2.", []),
        ("The conflictless report.", ["conflict"]),  # whole words only
        ("The dates are conflict-free.", ["conflict"]),
        ("The values are non-conflicting.", ["conflict"]),
        ("There is no evidence of any conflict.", ["conflict"]),  # negation far back
        ("No real sign of a conflict here.", ["conflict"]),
        ("There isn't really any conflict.", ["conflict"]),
        ("The onset is free of conflict.", ["conflict"]),
        ("No onset change, and there is a conflict in the outcome.", []),
        ("The onset matches and there is a conflict in the outcome.", []),
    ],
)
def test_negated_status_is_not_stated(text: str, unstated: list[str]) -> None:
    assert unstated_statuses(text, [_item({"status": "conflict"})]) == unstated


def test_not_comparable_is_stated_despite_its_own_not() -> None:
    item = _item({"status": "not_comparable", "reason": "no_document_field"})
    for text in (
        "Action taken is not comparable.",
        "There is no document field, so it is not comparable.",
        "(status: not_comparable)",
    ):
        assert unstated_statuses(text, [item]) == [], text
    both = [item, _item({"status": "missing"})]
    for text in (
        "The outcome is not comparable because it is missing.",  # "not" of the status name
        "The outcome is not comparable; it is missing.",
    ):
        assert unstated_statuses(text, both) == [], text
    assert unstated_statuses("Action taken cannot be compared.", [item]) == ["not_comparable"]


# Wordings the real model used instead of a status name. The prompt must never show them:
# agent-v3 quoted "cannot be compared" as a counter-example and the model copied it into
# reconciliation answers, which the checker then rejected (status_not_stated).
STATUS_PARAPHRASES = ("cannot be compared", "can't be compared", "could not be compared")


def test_prompt_never_shows_a_status_paraphrase() -> None:
    from chatmed.agent.prompt import SYSTEM_PROMPT, retry_note

    shown = (SYSTEM_PROMPT + retry_note("status_not_stated")).casefold()
    assert not [p for p in STATUS_PARAPHRASES if p in shown]


def test_prompt_names_exactly_the_row_id_prefixes_tools_return() -> None:
    """agent-v5: the model cited a bare document id once; the prompt lists the real prefixes.
    If a tool gains a new id kind, this fails until the prompt names it (and is bumped)."""
    import inspect
    import re

    from chatmed.agent import tools
    from chatmed.agent.prompt import SYSTEM_PROMPT

    # Evidence row ids only: `_note_read(ctx, f"subject:...")` keys are tenant-read digests.
    source = inspect.getsource(tools)
    in_tools = set(re.findall(r'(?<!_note_read\(ctx, )f"([a-z]+):\{', source))
    line = SYSTEM_PROMPT.split("they start with", 1)[1].split(")", 1)[0]
    assert set(re.findall(r"([a-z]+):", line)) == in_tools


def test_status_check_names() -> None:
    item = _item({"status": "undeterminable", "join_status": "not_attempted"})
    assert unstated_statuses("It is undeterminable; join not attempted.", [item]) == []
    assert unstated_statuses("Yes.", [item]) == ["not_attempted", "undeterminable"]
    assert unstated_statuses("No.", [_item({"status": "ok"})]) == []


async def test_undeterminable_checkbox_must_be_stated(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    s = setup.shas["cioms_initial.pdf"][:12]
    script = [
        tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["dechallenge"]}),
        final("Yes, the reaction abated after stopping the drug.", [f"fact:{s}:dechallenge:1"]),
        final(
            "Dechallenge is undeterminable from the text: the box must be checked visually.",
            [f"fact:{s}:dechallenge:1"],
        ),
    ]
    client = ScriptedModelClient(script)
    answer = await ask(setup, client, "Did the reaction abate after stopping?")
    assert answer.verified and "undeterminable" in answer.text
    assert "status_not_stated" in client.requests[2]["messages"][-1]["content"]


async def test_fallback_states_every_status(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_followup.pdf"])
    s = setup.shas["cioms_followup.pdf"][:12]
    script = [
        tool_call(
            "query_document_facts",
            {"doc_sha": None, "field_keys": ["reaction_onset_date", "dechallenge"]},
        ),
        final("Onset 2026-06-12.", [f"fact:{s}:reaction_onset_date:1"]),
        final("Onset 2026-06-12.", [f"fact:{s}:reaction_onset_date:1"]),
    ]
    answer = await ask(setup, ScriptedModelClient(script), "Onset?")
    assert (answer.verified, answer.reason) == (False, "status_not_stated")
    assert answer.text.count("status=conflict") == 2 and "status=undeterminable" in answer.text


async def test_unmatched_subject_is_stated(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"], tenant="globex")
    s = setup.shas["cioms_initial.pdf"][:12]
    script = [
        tool_call("query_document_facts", {"doc_sha": None, "field_keys": ["subject_id"]}),
        tool_call("get_subject", {"subjid": "01001-004"}),
        final(
            "The subject id on the form is unmatched in the study data, so I cannot give the arm.",
            [f"fact:{s}:subject_id:1"],
        ),
    ]
    client = ScriptedModelClient(script)
    answer = await ask(setup, client, "What arm is the subject in?")
    assert answer.verified
    assert tool_messages(client.requests[2])[-1] == {"error": "subject_not_joined"}
    assert not any(f["source"] in ("adsl", "adae") for f in answer.facts_used)


# ---------------------------------------------------------------- D14: reconciliation is one unit


# ---------------------------------------------------------------- D6/D14: answer fingerprint


async def test_answer_fingerprint_is_stable_and_versioned(
    recorded: FakeModelClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    prints = []
    for _ in range(2):
        setup = await setup_session(recorded, ["cioms_initial.pdf"])
        sha = setup.shas["cioms_initial.pdf"]
        prints.append((await ask(setup, ScriptedModelClient(arm_script(sha)), "Arm?")).fingerprint)
    assert prints[0] == prints[1] and prints[0] is not None and len(prints[0]) == 64
    monkeypatch.setattr("chatmed.agent.prompt.AGENT_PROMPT_VERSION", "agent-v999")
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    sha = setup.shas["cioms_initial.pdf"]
    changed = await ask(setup, ScriptedModelClient(arm_script(sha)), "Arm?")
    assert changed.fingerprint != prints[0]
    # a fallback claims no reproducibility: outcome + reason, no fingerprint (D14)
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    fallback = await ask(setup, ScriptedModelClient([final("x", ["nope"])] * 2), "Arm?")
    assert (fallback.outcome, fallback.reason) == ("fallback", "unknown_cited_id")
    assert fallback.fingerprint is None


async def test_fingerprint_covers_the_session_history(recorded: FakeModelClient) -> None:
    """Earlier turns are sent with the question, so they are part of the fingerprint."""
    prints = []
    for first in ("Anything?", "Anything else?", "Anything?"):
        setup = await setup_session(recorded, ["cioms_initial.pdf"])
        sha = setup.shas["cioms_initial.pdf"]
        await ask(setup, ScriptedModelClient([final("No.", [])]), first)
        answer = await ask(setup, ScriptedModelClient(arm_script(sha)), "Arm?")
        prints.append(answer.fingerprint)
    assert prints[0] == prints[2] != prints[1]


async def test_fingerprint_covers_the_tenant_data_read(recorded: FakeModelClient) -> None:
    """Same schema, same documents, changed adsl row: a different fingerprint."""
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    sha = setup.shas["cioms_initial.pdf"]
    before = await ask(setup, ScriptedModelClient(arm_script(sha)), "Arm?")
    original = setup.repo.subject

    async def changed_subject(subjid: str) -> dict[str, Any] | None:
        row = await original(subjid)
        return {**row, "saffl": "N"} if row else None

    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    setup.repo.subject = changed_subject  # type: ignore[method-assign]
    after = await ask(setup, ScriptedModelClient(arm_script(sha)), "Arm?")
    assert before.outcome == after.outcome == "verified"
    assert before.fingerprint != after.fingerprint


async def test_fingerprint_covers_every_adae_row_reconcile_read(
    recorded: FakeModelClient,
) -> None:
    """reconcile_case reads all of the subject's events to pick one; all of them count."""
    prints = []
    for edit in (False, True):
        setup = await setup_session(recorded, ["cioms_initial.pdf"])
        if edit:
            for row in setup.repo.adae:
                if row["aeseq"] != 2:  # not the record the reconciliation selects
                    row["extra"] = "changed"
        sha = setup.shas["cioms_initial.pdf"]
        answer = await ask(setup, ScriptedModelClient(reconcile_script(sha)), "Match?")
        assert answer.outcome == "verified"
        prints.append((ids(answer), answer.fingerprint))
    assert prints[0][0] == prints[1][0] and prints[0][1] != prints[1][1]


async def test_fingerprint_names_the_tenant_without_documents(recorded: FakeModelClient) -> None:
    prints = []
    for tenant in ("acme", "globex"):
        setup = await setup_session(recorded, [], tenant=tenant)
        prints.append(
            (await ask(setup, ScriptedModelClient([final("No.", [])]), "Hi?")).fingerprint
        )
    assert None not in prints and prints[0] != prints[1]


async def _arm_fingerprint(recorded: FakeModelClient, **settings: Any) -> str | None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"], settings=make_settings(**settings))
    sha = setup.shas["cioms_initial.pdf"]
    answer = await ask(setup, ScriptedModelClient(arm_script(sha)), "Arm?")
    assert answer.outcome == "verified"
    return answer.fingerprint


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("agent_fulltext_max_chars", 1234),
        ("agent_bm25_top_k", 5),
        ("azure_openai_api_version", "2099-01-01"),
        ("tenant_db_dsn_template", "postgresql://other_{tenant}@db2/tenant_{tenant}"),
    ],
)
async def test_fingerprint_settings_change_the_fingerprint(
    recorded: FakeModelClient, name: str, value: Any
) -> None:
    assert await _arm_fingerprint(recorded, **{name: value}) != await _arm_fingerprint(recorded)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("agent_max_iterations", 12),
        ("agent_question_timeout_seconds", 99.0),
        ("llm_max_attempts", 2),
        ("llm_timeout_seconds", 5.0),
    ],
)
async def test_operational_settings_do_not_change_the_fingerprint(
    recorded: FakeModelClient, name: str, value: Any
) -> None:
    assert await _arm_fingerprint(recorded, **{name: value}) == await _arm_fingerprint(recorded)


async def test_session_context_field_keys_are_sorted(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    client = ScriptedModelClient([final("No.", [])])
    await ask(setup, client, "Anything?")
    line = client.requests[0]["messages"][1]["content"].splitlines()[-1]
    keys = line.removeprefix("Field keys for cioms: ").split(", ")
    assert keys == sorted(keys) and len(keys) > 5


# ---------------------------------------------------------------- malformed answers


def _raw_answer(content: str) -> Any:
    msg = SimpleNamespace(content=content, refusal=None, tool_calls=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=msg)])
    resp.usage = None
    return lambda _request: resp


async def test_rejected_output_is_logged_as_length_and_code_only(
    recorded: FakeModelClient, caplog: pytest.LogCaptureFixture
) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    content = '{"text": "Zentrobal 01001-004"} {"text": "hospitalised"}'
    client = ScriptedModelClient([_raw_answer(content), final("No.", [])])
    with caplog.at_level(logging.DEBUG, logger="chatmed"):
        await ask(setup, client, "Anything?")
    (rejected,) = [r for r in caplog.records if r.getMessage() == "agent.answer_rejected"]
    assert rejected.__dict__["status"] == "answer_not_one_json_object"
    assert rejected.__dict__["output_chars"] == len(content)
    for r in caplog.records:
        dumped = repr({k: v for k, v in r.__dict__.items() if k != "exc_info"})
        assert not any(m in dumped for m in MARKERS)


async def test_malformed_answers_count_toward_the_iteration_limit(
    recorded: FakeModelClient,
) -> None:
    settings = make_settings(agent_max_iterations=1)
    setup = await setup_session(recorded, ["cioms_initial.pdf"], settings=settings)
    client = ScriptedModelClient([_raw_answer("{} {}"), final("No.", [])])
    answer = await ask(setup, client, "Anything?")
    assert len(client.requests) == 1
    assert (answer.verified, answer.reason) == (False, "max_iterations")


async def test_unmatched_subject_fact_is_not_added_to_the_unit(recorded: FakeModelClient) -> None:
    """Only the *joined* subject_id fact is part of the reconciliation unit."""
    setup = await setup_session(recorded, ["cioms_initial.pdf"], tenant="globex")
    s = setup.shas["cioms_initial.pdf"][:12]
    answer = await ask(setup, ScriptedModelClient(reconcile_script(s)), "Match?")
    assert answer.verified
    assert not any(i.startswith(("fact:", "adae:", "adsl:")) for i in ids(answer))


def test_prompt_tells_the_model_statuses_are_checked_by_name() -> None:
    from chatmed.agent.prompt import SYSTEM_PROMPT

    # agent-v4: says names are checked word for word; no longer quotes the paraphrase
    # (test_prompt_never_shows_a_status_paraphrase).
    assert "status name itself" in SYSTEM_PROMPT and "word\n  for word" in SYSTEM_PROMPT


# ---------------------------------------------------------------- AC2: question time limit


class _SlowClient:
    """Answers every call after `delay` seconds; counts the calls."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **_request: Any) -> Any:
        self.calls += 1
        await asyncio.sleep(self.delay)
        raise AssertionError("the time limit should have stopped this call")


async def test_question_time_limit_ends_with_the_fallback(recorded: FakeModelClient) -> None:
    settings = make_settings(agent_question_timeout_seconds=0.2)
    setup = await setup_session(recorded, ["cioms_initial.pdf"], settings=settings)
    client = _SlowClient(delay=30)
    started = time.perf_counter()
    answer = await ask(setup, client, "Arm?")  # type: ignore[arg-type]
    assert time.perf_counter() - started < 5
    assert (answer.outcome, answer.reason) == ("fallback", "deadline")
    assert answer.fingerprint is None
    assert setup.session.history[-1]["content"] == answer.text


async def test_time_limit_covers_retries_and_backoff(recorded: FakeModelClient) -> None:
    """429s with real backoff sleeps: the limit still ends the question on time."""
    settings = make_settings(agent_question_timeout_seconds=0.3, llm_max_attempts=10)
    setup = await setup_session(recorded, ["cioms_initial.pdf"], settings=settings)
    client = ScriptedModelClient([_status_error(429)] * 10)

    async def real_sleep(_seconds: float) -> None:
        await asyncio.sleep(1)

    started = time.perf_counter()
    answer = await run_agent(
        client,  # type: ignore[arg-type]
        setup.session,
        setup.ctx(),
        "Arm?",
        settings=setup.settings,
        sleep=real_sleep,
    )
    assert time.perf_counter() - started < 5
    assert answer.reason == "deadline"
