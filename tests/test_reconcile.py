"""M3 AC1/AC8, D14: the tiered `adae` record selection and the per-item comparison, in code."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from chatmed.agent.reconcile import (
    AE_MATCH_WINDOW_DAYS,
    Reason,
    reconcile,
    select_record,
)
from chatmed.extraction import Fact, JoinStatus, Status
from tests.fakes import ADAE_BY_TENANT, _adae

SHA = "a" * 64
ACME_AES = [r for r in ADAE_BY_TENANT["acme"] if r["usubjid"].endswith("01001-004")]


def fact(key: str, status: str = Status.OK, **kw: Any) -> Fact:
    base: dict[str, Any] = dict(
        doc_sha=SHA,
        doc_type="cioms",
        field_key=key,
        value_text=None,
        value_num=None,
        value_date=None,
        value_bool=None,
        unit=None,
        page=1,
        quote="q",
        status=status,
    )
    return Fact(**(base | kw))


def subject(join: str = JoinStatus.JOINED) -> Fact:
    return fact(
        "subject_id",
        value_text="101-01001-004",
        join_status=join,
        join_value="01001-004" if join == JoinStatus.JOINED else None,
    )


def onset(day: str, status: str = Status.OK) -> Fact:
    return fact("reaction_onset_date", status, value_text=day, value_date=date.fromisoformat(day))


def box(key: str, value: bool | None, status: str = Status.OK) -> Fact:
    return fact(key, status, value_bool=value, value_text=None if value is None else str(value))


INITIAL = [
    subject(),
    onset("2026-06-12"),
    box("hospitalisation", True),
    box("patient_died", None, Status.UNDETERMINABLE),
    box("disability", None, Status.UNDETERMINABLE),
    box("life_threatening", None, Status.UNDETERMINABLE),
]


def items(result: Any) -> dict[str, tuple[str, str | None]]:
    return {i.item: (i.status, i.reason) for i in result.items}


def test_exact_onset_match_selects_the_record() -> None:
    result = reconcile(INITIAL, ACME_AES)
    assert result.tier == "exact" and result.record is not None
    assert result.record["aeseq"] == 2
    assert items(result) == {
        "onset_date": ("match", None),
        "seriousness": ("match", None),
        "action_taken": ("not_comparable", Reason.NO_DOCUMENT_FIELD),
        "outcome": ("not_comparable", Reason.NO_DOCUMENT_FIELD),
    }


def test_two_days_off_selects_nearest_serious_event_as_mismatch() -> None:
    facts = [f for f in INITIAL if f.field_key != "reaction_onset_date"] + [onset("2026-06-10")]
    result = reconcile(facts, ACME_AES)
    assert result.tier == "nearest" and result.record is not None
    assert result.record["aeseq"] == 2
    onset_item = result.items[0]
    assert (onset_item.status, onset_item.day_difference) == ("mismatch", 2)
    assert (onset_item.document_value, onset_item.record_value) == ("2026-06-10", "2026-06-12")


def test_nearest_ignores_non_serious_events() -> None:
    # 2026-04-05 is 2 days from the non-serious rash and 68 days from the serious event.
    record, tier, _ = select_record([date(2026, 4, 5)], ACME_AES)
    assert (record, tier) == (None, None)


def test_outside_the_window_is_not_comparable() -> None:
    late = date(2026, 6, 12).toordinal() + AE_MATCH_WINDOW_DAYS + 1
    facts = [subject(), onset(date.fromordinal(late).isoformat())]
    result = reconcile(facts, ACME_AES)
    assert result.record is None
    assert {s for s, _ in items(result).values()} == {"not_comparable"}
    assert {r for _, r in items(result).values()} == {Reason.NO_UNIQUE_AE_RECORD}


def test_tie_between_two_serious_events_is_not_comparable() -> None:
    rows = ACME_AES + [
        _adae("01001-004", 3, "jaundice", "2026-06-16", "Y", "DRUG WITHDRAWN", "RECOVERING")
    ]
    facts = [subject(), onset("2026-06-14")]  # 2 days from both serious events
    result = reconcile(facts, rows)
    assert result.record is None
    assert items(result)["onset_date"] == ("not_comparable", Reason.NO_UNIQUE_AE_RECORD)


def test_two_exact_matches_fall_through_to_the_nearest_rule() -> None:
    rows = ACME_AES + [
        _adae("01001-004", 3, "nausea", "2026-06-12", "N", "DOSE NOT CHANGED", "RECOVERED")
    ]
    record, tier, diff = select_record([date(2026, 6, 12)], rows)
    assert record is not None and (record["aeseq"], tier, diff) == (2, "nearest", 0)


def test_followup_conflict_selects_the_record_but_onset_is_not_comparable() -> None:
    facts = [f for f in INITIAL if f.field_key != "reaction_onset_date"] + [
        onset("2026-06-12", Status.CONFLICT),
        onset("2026-06-11", Status.CONFLICT),
    ]
    result = reconcile(facts, ACME_AES)
    assert result.record is not None and result.record["aeseq"] == 2
    assert items(result)["onset_date"] == ("not_comparable", Reason.CONFLICT)
    assert result.items[0].document_value == "2026-06-11; 2026-06-12"
    assert items(result)["seriousness"] == ("match", None)


@pytest.mark.parametrize(
    "join", [JoinStatus.UNMATCHED, JoinStatus.AMBIGUOUS, JoinStatus.NOT_ATTEMPTED]
)
def test_unjoined_subject_compares_nothing(join: str) -> None:
    facts = [subject(join)] + INITIAL[1:]
    result = reconcile(facts, ACME_AES)
    assert result.record is None
    assert {r for _, r in items(result).values()} == {Reason.SUBJECT_NOT_JOINED}


def test_missing_onset_has_no_record() -> None:
    facts = [subject(), fact("reaction_onset_date", Status.MISSING, page=None, quote=None)]
    assert reconcile(facts, ACME_AES).record is None


def test_seriousness_undeterminable_when_no_box_is_true() -> None:
    facts = [subject(), onset("2026-06-12")] + [
        box(k, None, Status.UNDETERMINABLE)
        for k in ("hospitalisation", "patient_died", "disability", "life_threatening")
    ]
    assert items(reconcile(facts, ACME_AES))["seriousness"] == ("not_comparable", "undeterminable")


def test_seriousness_all_false_is_n_and_mismatches_a_serious_record() -> None:
    facts = [subject(), onset("2026-06-12")] + [
        box(k, False) for k in ("hospitalisation", "patient_died", "disability", "life_threatening")
    ]
    assert items(reconcile(facts, ACME_AES))["seriousness"] == ("mismatch", None)


def test_globex_style_serious_field_is_used() -> None:
    facts = [subject(), onset("2026-06-12"), box("serious", None, Status.UNDETERMINABLE)]
    assert items(reconcile(facts, ACME_AES))["seriousness"] == ("not_comparable", "undeterminable")


def test_action_and_outcome_compare_when_the_tenant_defines_them() -> None:
    facts = INITIAL + [
        fact("action_taken", value_text="drug withdrawn"),
        fact("outcome", value_text="recovering"),
    ]
    result = items(reconcile(facts, ACME_AES))
    assert result["action_taken"] == ("match", None)
    assert result["outcome"] == ("mismatch", None)
