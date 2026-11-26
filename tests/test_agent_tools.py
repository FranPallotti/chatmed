"""M3 AC1 (tools, tenant from the session), AC4 (no study data without a join), AC5 (page text)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from chatmed.agent.prompt import TOOL_SCHEMAS
from chatmed.agent.retrieval import bm25, relevant_pages
from chatmed.agent.tools import dispatch
from chatmed.models import ParsedDocument, ParsedPage
from tests.agent_support import AgentSetup, setup_session
from tests.fakes import FakeModelClient
from tests.test_extraction_m2 import make_settings


@pytest.fixture(scope="module")
def recorded() -> FakeModelClient:
    return FakeModelClient.from_recordings()


async def call(setup: AgentSetup, name: str, ctx: Any = None, **args: Any) -> dict[str, Any]:
    return await dispatch(ctx or setup.ctx(), name, json.dumps(args))


# ---------------------------------------------------------------- schemas


def test_no_tool_takes_a_tenant() -> None:
    """Rule 5: the model never chooses the tenant."""
    for tool in TOOL_SCHEMAS:
        props = tool["function"]["parameters"]["properties"]
        assert not any("tenant" in p for p in props)


def test_tools_are_strict() -> None:
    for tool in TOOL_SCHEMAS:
        fn = tool["function"]
        assert fn["strict"] is True
        assert fn["parameters"]["additionalProperties"] is False
        assert sorted(fn["parameters"]["required"]) == sorted(fn["parameters"]["properties"])


# ---------------------------------------------------------------- query_document_facts


async def test_query_document_facts_returns_cited_rows(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    sha = setup.shas["cioms_initial.pdf"]
    ctx = setup.ctx()
    result = await call(setup, "query_document_facts", ctx, doc_sha=None, field_keys=["subject_id"])
    (row,) = result["rows"]
    assert row["id"] == f"fact:{sha[:12]}:subject_id:1"
    assert (row["join_status"], row["join_value"]) == ("joined", "01001-004")
    assert ctx.evidence[row["id"]].citation["page"] == 1


async def test_query_document_facts_is_stable(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf", "cioms_followup.pdf"])
    first = await call(setup, "query_document_facts", doc_sha=None, field_keys=None)
    second = await call(setup, "query_document_facts", doc_sha=None, field_keys=None)
    assert first == second
    assert len({r["id"] for r in first["rows"]}) == len(first["rows"])


async def test_doc_prefix_and_unknown_document(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf", "cioms_followup.pdf"])
    sha = setup.shas["cioms_followup.pdf"]
    rows = (await call(setup, "query_document_facts", doc_sha=sha[:12], field_keys=None))["rows"]
    assert {r["doc"] for r in rows} == {sha[:12]}
    assert await call(setup, "query_document_facts", doc_sha="0000000000", field_keys=None) == {
        "error": "unknown_document"
    }
    assert (await call(setup, "reconcile_case", doc_sha="abc"))["error"] == "unknown_document"


# ---------------------------------------------------------------- tenant DB tools


async def test_field_metadata_from_the_sessions_tenant(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"], tenant="globex")
    rows = (await call(setup, "get_field_metadata", doc_type="cioms"))["rows"]
    assert {r["key"] for r in rows} == {
        "subject_id", "reaction_onset_date", "reaction_description", "serious",
        "suspect_drug", "report_type",
    }  # fmt: skip
    assert setup.repo.tenant_id == "globex"


async def test_unknown_document_type_is_a_code(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])

    async def unknown(_doc_type: str) -> Any:
        from chatmed.tenant_db import UnknownDocumentType

        raise UnknownDocumentType("x")

    setup.repo.field_schema = unknown  # type: ignore[method-assign]
    assert await call(setup, "get_field_metadata", doc_type="lab") == {
        "error": "unknown_document_type"
    }


async def test_get_subject_for_the_joined_subject(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    (row,) = (await call(setup, "get_subject", subjid="01001-004"))["rows"]
    assert row["id"] == "adsl:01001-004"
    assert (row["arm"], row["trtsdt"]) == ("Zentrobal 20 mg", "2026-03-10")


async def test_get_subject_refuses_a_subject_that_is_not_joined(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    setup.repo.calls.clear()
    # another subject in the DB, a partial id, another prefix, empty
    for subjid in ("01001-001", "01001", "101-01001-001", "", "004"):
        assert await call(setup, "get_subject", subjid=subjid) == {"error": "subject_not_joined"}
        result = await call(setup, "list_adverse_events", subjid=subjid, serious_only=False)
        assert result == {"error": "subject_not_joined"}
    assert setup.repo.calls == []  # refused before touching the tenant DB


async def test_printed_subject_id_of_a_joined_fact_maps_to_its_join_value(
    recorded: FakeModelClient,
) -> None:
    """Seen with the real model: it passes the id as printed on the form, not the join_value."""
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    (row,) = (await call(setup, "get_subject", subjid="101-01001-004"))["rows"]
    assert row["id"] == "adsl:01001-004"


async def test_printed_id_maps_only_to_its_own_documents_join(recorded: FakeModelClient) -> None:
    """Two documents, two subjects: each printed id maps to its own document's join_value."""
    setup = await setup_session(recorded, ["cioms_initial.pdf", "cioms_followup.pdf"])
    setup.store.facts_db(setup.session.id).execute(
        "UPDATE facts SET value_text = '101-01001-001', join_value = '01001-001' "
        "WHERE doc_sha = ? AND field_key = 'subject_id'",
        [setup.shas["cioms_followup.pdf"]],
    )
    ids = {
        printed: [r["id"] for r in (await call(setup, "get_subject", subjid=printed))["rows"]]
        for printed in ("101-01001-004", "101-01001-001")
    }
    assert ids == {"101-01001-004": ["adsl:01001-004"], "101-01001-001": ["adsl:01001-001"]}
    events = await call(setup, "list_adverse_events", subjid="101-01001-001", serious_only=False)
    assert {r["usubjid"] for r in events["rows"]} == {"ACM-101-01001-001"}
    # a printed id no document carries still maps to nothing
    assert await call(setup, "get_subject", subjid="101-01001-002") == {
        "error": "subject_not_joined"
    }


async def test_list_adverse_events_serious_only(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    rows = (await call(setup, "list_adverse_events", subjid="01001-004", serious_only=False))[
        "rows"
    ]
    assert [r["id"] for r in rows] == ["adae:ACM-101-01001-004:1", "adae:ACM-101-01001-004:2"]
    serious = await call(setup, "list_adverse_events", subjid="01001-004", serious_only=True)
    assert [r["aeseq"] for r in serious["rows"]] == [2]


# ---------------------------------------------------------------- reconcile_case (AC8)


def _recon(rows: list[dict[str, Any]]) -> dict[str, tuple[str, str | None]]:
    return {r["item"]: (r["status"], r["reason"]) for r in rows if r["id"].startswith("recon:")}


async def test_reconcile_initial_form(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    rows = (await call(setup, "reconcile_case", doc_sha=setup.shas["cioms_initial.pdf"]))["rows"]
    assert _recon(rows) == {
        "onset_date": ("match", None),
        "seriousness": ("match", None),
        "action_taken": ("not_comparable", "no_document_field"),
        "outcome": ("not_comparable", "no_document_field"),
    }
    assert rows[-1]["id"] == "adae:ACM-101-01001-004:2"


async def test_reconcile_followup_onset_conflict(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_followup.pdf"])
    rows = (await call(setup, "reconcile_case", doc_sha=setup.shas["cioms_followup.pdf"]))["rows"]
    assert _recon(rows)["onset_date"] == ("not_comparable", "conflict")
    assert _recon(rows)["seriousness"] == ("match", None)


async def test_unmatched_subject_returns_no_study_data(recorded: FakeModelClient) -> None:
    """AC4: globex has no subject 01001-004, so the join is `unmatched`: no study data at all."""
    setup = await setup_session(recorded, ["cioms_initial.pdf"], tenant="globex")
    ctx = setup.ctx()
    facts = await call(setup, "query_document_facts", ctx, doc_sha=None, field_keys=["subject_id"])
    assert facts["rows"][0]["join_status"] == "unmatched"
    setup.repo.calls.clear()
    assert await call(setup, "get_subject", ctx, subjid="01001-004") == {
        "error": "subject_not_joined"
    }
    rows = (await call(setup, "reconcile_case", ctx, doc_sha=setup.shas["cioms_initial.pdf"]))[
        "rows"
    ]
    assert {v for v in _recon(rows).values()} == {("not_comparable", "subject_not_joined")}
    assert not any(r["id"].startswith(("adae:", "adsl:")) for r in rows)
    assert setup.repo.calls == []
    assert not any(k.startswith(("adae:", "adsl:")) for k in ctx.evidence)


# ---------------------------------------------------------------- dispatch errors


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("query_document_facts", "{not json", "bad_arguments"),
        ("get_subject", "{}", "bad_arguments"),
        ("get_subject", "[]", "bad_arguments"),
        ("drop_table", "{}", "unknown_tool"),
    ],
)
async def test_bad_calls_become_codes(
    recorded: FakeModelClient, name: str, arguments: str, code: str
) -> None:
    setup = await setup_session(recorded, ["cioms_initial.pdf"])
    assert await dispatch(setup.ctx(), name, arguments) == {"error": code}


# ---------------------------------------------------------------- AC5: page text fallback


async def test_small_documents_return_every_page(recorded: FakeModelClient) -> None:
    setup = await setup_session(recorded, ["cioms_followup.pdf"])
    ctx = setup.ctx()
    pages = (await call(setup, "search_document_text", ctx, query="anything", doc_sha=None))[
        "pages"
    ]
    sha = setup.shas["cioms_followup.pdf"][:12]
    assert [p["page_id"] for p in pages] == [f"page:{sha}:1", f"page:{sha}:2"]
    assert set(ctx.pages) == {f"page:{sha}:1", f"page:{sha}:2"}


async def test_large_documents_use_bm25(recorded: FakeModelClient) -> None:
    settings = make_settings(agent_fulltext_max_chars=100, agent_bm25_top_k=1)
    setup = await setup_session(recorded, ["cioms_followup.pdf"], settings=settings)
    result = await call(setup, "search_document_text", query="corrected onset", doc_sha=None)
    sha = setup.shas["cioms_followup.pdf"][:12]
    assert [p["page_id"] for p in result["pages"]] == [f"page:{sha}:2"]


def _doc(sha: str, *texts: str) -> ParsedDocument:
    pages = tuple(ParsedPage(n, t) for n, t in enumerate(texts, start=1))
    return ParsedDocument(sha, pages, "test")


def test_bm25_ties_break_by_document_then_page() -> None:
    docs = [_doc("b" * 64, "liver injury", "other"), _doc("a" * 64, "other", "liver injury")]
    pages = relevant_pages("liver", docs, fulltext_max_chars=1, top_k=3)
    assert [(p.doc_sha[0], p.page) for p in pages] == [("a", 2), ("b", 1)]


def test_bm25_prefers_pages_with_more_query_terms_and_skips_misses() -> None:
    docs = [_doc("a" * 64, "liver", "liver injury onset", "nothing here")]
    pages = relevant_pages("liver injury", docs, fulltext_max_chars=1, top_k=5)
    assert [p.page for p in pages] == [2, 1]


def test_bm25_is_deterministic() -> None:
    docs = [_doc("a" * 64, *(f"page {i} liver alt {i % 3}" for i in range(30)))]
    pages = relevant_pages("liver alt 2", docs, fulltext_max_chars=1, top_k=5)
    assert pages == relevant_pages("liver alt 2", docs, fulltext_max_chars=1, top_k=5)
    assert bm25("", pages, 3) == []
