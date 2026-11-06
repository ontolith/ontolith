"""Pure planning helpers for erasure (ADR-0056 §3)."""

from __future__ import annotations

from ontolith.govern.erasure import is_pending, redact_operations

PERSONAL = frozenset({"Person.name"})


def test_non_dict_operations_pass_through() -> None:
    payload, touched = redact_operations(
        {"operations": ["odd"]},
        entity_id="e",
        local_keys=frozenset(),
        personal=PERSONAL,
        redact_key=False,
    )
    assert payload == {"operations": ["odd"]} and not touched


def test_other_entities_and_non_personal_predicates_are_untouched() -> None:
    ops = [
        {"kind": "assert_literal", "subject": "other", "predicate": "Person.name", "value": "x"},
        {"kind": "assert_literal", "subject": "e", "predicate": "Person.city", "value": "y"},
        {"kind": "create_entity", "key": "k", "natural_key": "n"},
    ]
    payload, touched = redact_operations(
        {"operations": ops},
        entity_id="e",
        local_keys=frozenset({"k"}),
        personal=PERSONAL,
        redact_key=False,
    )
    assert payload["operations"] == ops and not touched


def test_personal_value_and_key_are_blanked() -> None:
    ops = [
        {"kind": "create_entity", "key": "k", "natural_key": "n"},
        {
            "kind": "assert_literal",
            "subject": None,
            "subject_key": "k",
            "predicate": "Person.name",
            "value": "v",
            "source": "s",
            "rationale": "r",
        },
    ]
    payload, touched = redact_operations(
        {"operations": ops},
        entity_id="e",
        local_keys=frozenset({"k"}),
        personal=PERSONAL,
        redact_key=True,
    )
    assert touched
    assert payload["operations"][0]["natural_key"] is None
    assert payload["operations"][1]["value"] is None
    assert payload["operations"][1]["source"] is None


def test_pending_states() -> None:
    assert is_pending("submitted") and is_pending("changes_requested")
    assert not is_pending("accepted") and not is_pending("rejected")
