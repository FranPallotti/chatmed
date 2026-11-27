"""D14 (agent-v7, rules-11): reconciliation answers are rendered by code, not written by the model.

The model only routes the question to `reconcile_case`; `answer.render_reconciliation` writes
one line per item with its status name and reason code, and the answer cites the whole
reconciliation unit. No second model call, so no model wording for the checker to reject.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from chatmed.agent import Answer, run_agent
from chatmed.agent.answer import (
    RECONCILIATION_HEADER,
    RECONCILIATION_SCOPE,
    AnswerRejected,
    render_reconciliation,
    verify,
)
from chatmed.agent.tools import EvidenceItem, ToolContext, dispatch
from tests.agent_support import AgentSetup, setup_session
from tests.fakes import FakeModelClient, ScriptedModelClient, final, tool_call
from tests.test_extraction_m2 import no_sleep

RUNS = 40


@pytest.fixture(scope="module")
def recorded() -> FakeModelClient:
    return FakeModelClient.from_recordings()


class _AgentEvents(logging.Handler):
    """Collects agent event names (metadata only)."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.events.append(record.getMessage())


@pytest.fixture
def agent_events() -> Iterator[_AgentEvents]:
    handler = _AgentEvents()
    logger = logging.getLogger("chatmed.agent")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(level)


async def ask(setup: AgentSetup, client: ScriptedModelClient, question: str) -> Answer:
    return await run_agent(
        client,  # type: ignore[arg-type]
        setup.session,
        setup.ctx(),
        question,
        settings=setup.settings,
        sleep=no_sleep,
    )


def _recon(doc: str, item: str, status: str, reason: str | None = None) -> EvidenceItem:
    return EvidenceItem(
        f"recon:{doc}:{item}",
        "reconciliation",
        {"item": item, "status": status, "reason": reason},
        {"doc": doc, "table": "adae", "key": None},
    )


# ---------------------------------------------------------------- the three statuses


async def test_each_status_renders_as_its_name_with_the_reason(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, [])
    ctx = setup.ctx()
    doc = "abcdefabcdef"
    for item in (
        _recon(doc, "outcome", "not_comparable", "no_document_field"),  # out of ITEMS order
        _recon(doc, "onset_date", "mismatch"),
        _recon(doc, "seriousness", "match"),
        _recon(doc, "action_taken", "not_comparable", "conflict"),
    ):
        ctx.evidence[item.id] = item
    answer = render_reconciliation(ctx, doc)
    assert answer.verified
    assert answer.text.splitlines() == [
        RECONCILIATION_HEADER,
        "Onset date: mismatch.",
        "Seriousness: match.",
        "Action taken: not comparable (conflict).",
        "Outcome: not comparable (no document field).",
        RECONCILIATION_SCOPE,
    ]
    assert [f["id"] for f in answer.facts_used] == sorted(ctx.evidence)


async def test_a_unit_row_with_an_unstated_status_gives_the_fallback(
    recorded: FakeModelClient,
) -> None:
    """The rendered text still goes through the checker: never an unchecked answer."""
    setup = await setup_session(recorded, [])
    ctx = setup.ctx()
    doc = "abcdefabcdef"
    ctx.evidence[f"recon:{doc}:onset_date"] = _recon(doc, "onset_date", "match")
    subject = EvidenceItem(
        f"fact:{doc}:subject_id:1",
        "document",
        {"status": "unverified", "join_status": "joined"},
        {},
    )
    ctx.evidence[subject.id] = subject
    answer = render_reconciliation(ctx, doc)
    assert (answer.verified, answer.reason) == (False, "status_not_stated")


# ---------------------------------------------------------------- the demo documents


async def test_initial_report_is_rendered_without_a_second_model_call(
    recorded: FakeModelClient, agent_events: _AgentEvents
) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    s = setup.shas["cioms_initial.pdf"][:12]
    client = ScriptedModelClient([tool_call("reconcile_case", {"doc_sha": s})])
    answer = await ask(setup, client, "Does this CIOMS match what ADAE has for this subject?")
    assert len(client.requests) == 1
    assert answer.verified and answer.fingerprint
    assert answer.text.splitlines()[1:-1] == [
        "Onset date: match.",
        "Seriousness: match.",
        "Action taken: not comparable (no document field).",
        "Outcome: not comparable (no document field).",
    ]
    assert [f["id"] for f in answer.facts_used] == [
        "adae:ACM-101-01001-004:2",
        f"fact:{s}:subject_id:1",
        f"recon:{s}:action_taken",
        f"recon:{s}:onset_date",
        f"recon:{s}:outcome",
        f"recon:{s}:seriousness",
    ]
    assert "agent.reconciliation_rendered" in agent_events.events
    assert "agent.answer_rejected" not in agent_events.events
    assert setup.session.history[-1] == {"role": "assistant", "content": answer.text}


async def test_followup_report_names_the_conflict(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_followup.pdf"])
    s = setup.shas["cioms_followup.pdf"][:12]
    client = ScriptedModelClient([tool_call("reconcile_case", {"doc_sha": s})])
    answer = await ask(setup, client, "Does this CIOMS match ADAE?")
    assert answer.verified
    assert "Onset date: not comparable (conflict)." in answer.text.splitlines()


async def test_unjoined_subject_is_rendered_as_not_comparable(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"], tenant="globex")
    s = setup.shas["cioms_initial.pdf"][:12]
    client = ScriptedModelClient([tool_call("reconcile_case", {"doc_sha": s})])
    answer = await ask(setup, client, "Match?")
    assert answer.verified
    assert answer.text.splitlines()[1:-1] == [
        f"{name}: not comparable (subject not joined)."
        for name in ("Onset date", "Seriousness", "Action taken", "Outcome")
    ]
    assert not any(f["id"].startswith(("fact:", "adae:")) for f in answer.facts_used)


async def test_combined_question_gets_the_reconciliation_and_says_its_scope(
    recorded: FakeModelClient,
) -> None:
    """D14: the rest of a combined question is not answered; the last line says so."""
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    s = setup.shas["cioms_initial.pdf"][:12]
    client = ScriptedModelClient([tool_call("reconcile_case", {"doc_sha": s})])
    answer = await ask(
        setup, client, "Does this CIOMS match ADAE, and what treatment arm is the subject in?"
    )
    assert answer.verified and len(client.requests) == 1
    lines = answer.text.splitlines()
    assert lines[0] == RECONCILIATION_HEADER and lines[-1] == RECONCILIATION_SCOPE
    assert "Zentrobal" not in answer.text  # the arm is not answered
    assert not any(f["id"].startswith("adsl:") for f in answer.facts_used)


async def test_a_failed_reconcile_call_goes_back_to_the_model(recorded: FakeModelClient) -> None:
    """Only a successful reconcile_case is rendered; an error code is a normal tool result."""
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    client = ScriptedModelClient(
        [tool_call("reconcile_case", {"doc_sha": "nope"}), final("No such document.", [])]
    )
    answer = await ask(setup, client, "Match?")
    assert len(client.requests) == 2 and answer.text == "No such document."
    tool_results = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    assert json.loads(tool_results[0]["content"]) == {"error": "unknown_document"}


@pytest.mark.parametrize("fixture", ["cioms_initial.pdf", "cioms_followup.pdf"])
async def test_reconciliation_verifies_on_the_first_attempt_every_run(
    recorded: FakeModelClient, agent_events: _AgentEvents, fixture: str
) -> None:
    """40 runs, each a fresh session: verified, one model call, no rejection, same answer."""
    answers = []
    for _ in range(RUNS):
        setup = await setup_session(recorded, [fixture])
        s = setup.shas[fixture][:12]
        client = ScriptedModelClient([tool_call("reconcile_case", {"doc_sha": s})])
        answers.append(await ask(setup, client, "Does this CIOMS match ADAE?"))
        assert len(client.requests) == 1
    assert [a.outcome for a in answers] == ["verified"] * RUNS
    assert "agent.answer_rejected" not in agent_events.events
    assert agent_events.events.count("agent.reconciliation_rendered") == RUNS
    assert len({(a.text, json.dumps(a.facts_used), a.fingerprint) for a in answers}) == 1


# ---------------------------------------------------------------- the checker itself (unchanged)


async def _reconciled_ctx(recorded: FakeModelClient) -> tuple[ToolContext, str]:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    s = setup.shas["cioms_initial.pdf"][:12]
    ctx = setup.ctx()
    assert "rows" in await dispatch(ctx, "reconcile_case", json.dumps({"doc_sha": s}))
    return ctx, s


async def test_checker_still_rejects_a_reworded_status(recorded: FakeModelClient) -> None:
    """Seen with the real model (agent-v1..v6): "cannot be compared" for not_comparable."""
    ctx, s = await _reconciled_ctx(recorded)
    output: dict[str, Any] = {
        "text": "Onset and seriousness match. Action taken and outcome cannot be compared.",
        "cited_ids": [f"recon:{s}:onset_date"],
        "quotes": [],
    }
    with pytest.raises(AnswerRejected, match="status_not_stated"):
        verify(ctx, output)
    output["text"] = "The onset date matches."  # uncited not_comparable items still count
    with pytest.raises(AnswerRejected, match="status_not_stated"):
        verify(ctx, output)
    output["text"] = "Onset and seriousness match. Action taken and outcome are not comparable."
    assert verify(ctx, output).verified


async def test_citing_one_reconciliation_row_cites_the_whole_unit(
    recorded: FakeModelClient,
) -> None:
    ctx, s = await _reconciled_ctx(recorded)
    rendered = render_reconciliation(ctx, s)
    partial = verify(
        ctx,
        {
            "text": "Onset matches. Action taken and outcome are not comparable.",
            "cited_ids": [f"recon:{s}:onset_date"],
            "quotes": [],
        },
    )
    assert partial.facts_used == rendered.facts_used
