"""SAE reconciliation: CIOMS facts vs the subject's `adae` record, decided in code (rule 4, D14).

Which `adae` record is "the" record (tiered, deterministic):
1. exactly one row has `astdt` equal to one of the document's onset values → onset `match`;
2. else the unique serious row whose `astdt` is nearest to an onset value, within
   `AE_MATCH_WINDOW_DAYS` → onset `mismatch`, with the day difference;
3. else every item is `not_comparable`, reason `no_unique_ae_record`.

Onset values are taken from `ok` and `conflict` facts, so a corrected date can still find the
record, but a `conflict` onset is never compared (`not_comparable`, reason `conflict`).

This module is a rule module: it is hashed for `RULES_VERSION` (D6), so any code change here
needs a version bump (tests/guardrails/test_rules_version.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from chatmed.extraction import Fact, JoinStatus, Status

AE_MATCH_WINDOW_DAYS = 7

ONSET_FIELD = "reaction_onset_date"
SERIOUS_FIELD = "serious"  # globex-style single field
SERIOUSNESS_BOXES = ("patient_died", "hospitalisation", "disability", "life_threatening")
ACTION_FIELD = "action_taken"
OUTCOME_FIELD = "outcome"


class ItemStatus(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_COMPARABLE = "not_comparable"


class Reason(StrEnum):
    NO_UNIQUE_AE_RECORD = "no_unique_ae_record"
    NO_DOCUMENT_FIELD = "no_document_field"
    CONFLICT = "conflict"
    SUBJECT_NOT_JOINED = "subject_not_joined"


@dataclass(frozen=True)
class Item:
    """One compared item. Values are strings so they can be cited and checked as text."""

    item: str  # onset_date | seriousness | action_taken | outcome
    status: str  # an ItemStatus value
    reason: str | None = None
    document_value: str | None = None
    record_value: str | None = None
    day_difference: int | None = None  # record astdt minus document onset, tier 2 only


@dataclass(frozen=True)
class Reconciliation:
    record: dict[str, Any] | None  # the selected adae row
    tier: str | None  # "exact" | "nearest" | None
    items: tuple[Item, ...]


ITEMS = ("onset_date", "seriousness", "action_taken", "outcome")


def _by_key(facts: list[Fact], key: str) -> list[Fact]:
    return [f for f in facts if f.field_key == key]


def onset_values(facts: list[Fact]) -> list[date]:
    """Onset dates usable to select the record: from `ok` and `conflict` facts, sorted."""
    ok = {Status.OK, Status.CONFLICT}
    return sorted(
        {f.value_date for f in _by_key(facts, ONSET_FIELD) if f.status in ok and f.value_date}
    )


def select_record(
    onsets: list[date], rows: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None, int | None]:
    """(record, tier, day difference) by the tiered rule in the module docstring."""
    if not onsets:
        return None, None, None
    exact = [r for r in rows if r["astdt"] in onsets]
    if len(exact) == 1:
        return exact[0], "exact", 0
    nearest: list[tuple[int, int, dict[str, Any]]] = []  # (|diff|, signed diff, row)
    for row in rows:
        if row["aeser"] != "Y":
            continue
        diff = min(((row["astdt"] - d).days for d in onsets), key=lambda x: (abs(x), x))
        if abs(diff) <= AE_MATCH_WINDOW_DAYS:
            nearest.append((abs(diff), diff, row))
    if not nearest:
        return None, None, None
    best = min(n[0] for n in nearest)
    winners = [n for n in nearest if n[0] == best]
    if len(winners) != 1:
        return None, None, None  # a tie is not a unique record
    return winners[0][2], "nearest", winners[0][1]


def _onset_item(facts: list[Fact], record: dict[str, Any], tier: str, diff: int | None) -> Item:
    onset = _by_key(facts, ONSET_FIELD)
    statuses = {f.status for f in onset}
    record_value = record["astdt"].isoformat()
    if Status.CONFLICT in statuses:
        values = "; ".join(sorted({f.value_text for f in onset if f.value_text}))
        return Item("onset_date", ItemStatus.NOT_COMPARABLE, Reason.CONFLICT, values, record_value)
    doc_value = onset[0].value_text if onset else None
    if tier == "exact":
        return Item("onset_date", ItemStatus.MATCH, None, doc_value, record_value, 0)
    return Item("onset_date", ItemStatus.MISMATCH, None, doc_value, record_value, diff)


def _document_seriousness(facts: list[Fact]) -> tuple[str | None, str | None]:
    """('Y'|'N'|None, reason when None) from `serious` or the four CIOMS boxes (D14)."""
    serious = _by_key(facts, SERIOUS_FIELD)
    if serious:
        fact = serious[0]
        if fact.status == Status.OK and fact.value_bool is not None:
            return ("Y" if fact.value_bool else "N"), None
        return None, ",".join(sorted({f.status for f in serious}))
    boxes = [f for key in SERIOUSNESS_BOXES for f in _by_key(facts, key)]
    if not boxes:
        return None, Reason.NO_DOCUMENT_FIELD
    if any(f.status == Status.OK and f.value_bool for f in boxes):
        return "Y", None
    if all(f.status == Status.OK and f.value_bool is False for f in boxes):
        return "N", None
    return None, ",".join(sorted({f.status for f in boxes if f.status != Status.OK}))


def _seriousness_item(facts: list[Fact], record: dict[str, Any]) -> Item:
    value, reason = _document_seriousness(facts)
    if value is None:
        return Item("seriousness", ItemStatus.NOT_COMPARABLE, reason, None, record["aeser"])
    status = ItemStatus.MATCH if value == record["aeser"] else ItemStatus.MISMATCH
    return Item("seriousness", status, None, value, record["aeser"])


def _text_item(facts: list[Fact], item: str, key: str, record_value: str) -> Item:
    """Case-insensitive exact comparison of a text field with an `adae` column."""
    found = _by_key(facts, key)
    if not found:
        return Item(item, ItemStatus.NOT_COMPARABLE, Reason.NO_DOCUMENT_FIELD, None, record_value)
    fact = found[0]
    if fact.status != Status.OK or fact.value_text is None:
        return Item(item, ItemStatus.NOT_COMPARABLE, fact.status, None, record_value)
    same = fact.value_text.strip().casefold() == record_value.strip().casefold()
    status = ItemStatus.MATCH if same else ItemStatus.MISMATCH
    return Item(item, status, None, fact.value_text, record_value)


def _all_not_comparable(reason: str) -> tuple[Item, ...]:
    return tuple(Item(name, ItemStatus.NOT_COMPARABLE, reason) for name in ITEMS)


def subject_is_joined(facts: list[Fact]) -> str | None:
    """The joined `adsl.subjid` of the document, or None (D13a: no join, no study data)."""
    joined = {f.join_value for f in facts if f.join_status == JoinStatus.JOINED and f.join_value}
    return joined.pop() if len(joined) == 1 else None


def reconcile(facts: list[Fact], ae_rows: list[dict[str, Any]]) -> Reconciliation:
    """Compare one document's facts with the subject's `adae` rows (already fetched)."""
    if subject_is_joined(facts) is None:
        return Reconciliation(None, None, _all_not_comparable(Reason.SUBJECT_NOT_JOINED))
    record, tier, diff = select_record(onset_values(facts), ae_rows)
    if record is None or tier is None:
        return Reconciliation(None, None, _all_not_comparable(Reason.NO_UNIQUE_AE_RECORD))
    items = (
        _onset_item(facts, record, tier, diff),
        _seriousness_item(facts, record),
        _text_item(facts, "action_taken", ACTION_FIELD, record["aeacn"]),
        _text_item(facts, "outcome", OUTCOME_FIELD, record["aeout"]),
    )
    return Reconciliation(record, tier, items)
