"""Conformance vectors: resolution keeps members that corroborate the winner
(SPEC §10.1, §10.4, ADR-0053).

Contradiction membership is not pairwise: when a differing value arrives,
every overlapping active assertion on (subject, predicate) is flagged,
including ones that agree with each other. Resolution is by winning *value*,
so members equal to the winner's value stay active and only differing ones
are retracted. Retracted/superseded members stay terminal (ADR-0031).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock
from ontolith.core.errors import CapabilityError
from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)

SRC_A = "alice@example.com"
SRC_B = "beth@example.com"
SRC_C = "carl@example.com"
REVIEWER = "bob@example.com"
NEUTRAL_REVIEWER = "erin@example.com"

PREDICATE = "Person.salary"


def _kb(make_kb: KbFactory) -> Ontology:
    kb = make_kb(FixedClock(T0))
    for source in (SRC_A, SRC_B, SRC_C):
        kb.create_principal(source, kind="human", auth_method="oidc", default_capability="write")
    for reviewer in (REVIEWER, NEUTRAL_REVIEWER):
        kb.create_principal(reviewer, kind="human", auth_method="oidc", default_capability="review")
    return kb


def _status(kb: Ontology, assertion_id: str) -> str:
    assertion = kb.backend.get_assertion(assertion_id)
    assert assertion is not None
    return assertion.status


def _actions(kb: Ontology, assertion_id: str) -> list[str]:
    return [e.action for e in kb.backend.get_assertion_events(assertion_id)]


def _abc(kb: Ontology) -> tuple[str, str, str, str, str]:
    """100 (A), 100 (B), 120 (C): returns (entity, a, b, c, contradiction)."""
    entity = kb.create_entity("Person", author=SRC_A).id
    a = kb.assert_literal(entity, PREDICATE, "100", "Text", SRC_A)
    b = kb.assert_literal(entity, PREDICATE, "100", "Text", SRC_B)
    c = kb.assert_literal(entity, PREDICATE, "120", "Text", SRC_C)
    contradiction = kb.backend.get_open_contradiction("default", entity, PREDICATE)
    assert contradiction is not None
    assert set(contradiction.member_ids) == {a.id, b.id, c.id}
    return entity, a.id, b.id, c.id, contradiction.id


def test_corroborating_member_survives_resolution(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    _, a, b, c, cid = _abc(kb)

    resolved = kb.resolve_contradiction(cid, a, REVIEWER)

    assert resolved.state == "resolved"
    assert _status(kb, a) == "active"
    assert _status(kb, b) == "active"
    assert _status(kb, c) == "retracted"


def test_outcome_is_identical_whichever_agreeing_member_is_winner(make_kb: KbFactory) -> None:
    outcomes = []
    for pick in (1, 2):
        kb = _kb(make_kb)
        _, a, b, c, cid = _abc(kb)
        kb.resolve_contradiction(cid, (a, b)[pick - 1], REVIEWER)
        outcomes.append((_status(kb, a), _status(kb, b), _status(kb, c)))
    assert outcomes[0] == outcomes[1] == ("active", "active", "retracted")


def test_kept_and_retracted_members_are_recorded_as_events(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    _, a, b, c, cid = _abc(kb)

    kb.resolve_contradiction(cid, a, REVIEWER)

    for kept in (a, b):
        events = kb.backend.get_assertion_events(kept)
        assert events[-1].action == "reactivated"
        assert events[-1].actor == REVIEWER
    assert kb.backend.get_assertion_events(c)[-1].action == "retracted"


def test_terminal_corroborator_stays_terminal(make_kb: KbFactory) -> None:
    """B agrees with the winner but was already retracted by a neutral
    reviewer: it is not reactivated and gains no event (ADR-0031)."""
    kb = _kb(make_kb)
    _, a, b, c, cid = _abc(kb)
    kb.retract(b, NEUTRAL_REVIEWER)
    assert _status(kb, b) == "retracted"
    events_before = kb.backend.get_assertion_events(b)

    kb.resolve_contradiction(cid, a, REVIEWER)

    assert _status(kb, a) == "active"
    assert _status(kb, b) == "retracted"
    assert _status(kb, c) == "retracted"
    assert kb.backend.get_assertion_events(b) == events_before


def test_ref_valued_corroboration_is_kept(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    entity = kb.create_entity("Person", author=SRC_A).id
    org_x = kb.create_entity("Organization", author=SRC_A).id
    org_y = kb.create_entity("Organization", author=SRC_A).id
    a = kb.assert_ref(entity, "Person.employer", org_x, SRC_A)
    b = kb.assert_ref(entity, "Person.employer", org_x, SRC_B)
    c = kb.assert_ref(entity, "Person.employer", org_y, SRC_C)
    contradiction = kb.backend.get_open_contradiction("default", entity, "Person.employer")
    assert contradiction is not None

    kb.resolve_contradiction(contradiction.id, a.id, REVIEWER)

    assert _status(kb, a.id) == "active"
    assert _status(kb, b.id) == "active"
    assert _status(kb, c.id) == "retracted"


def test_party_guard_covers_kept_members(make_kb: KbFactory) -> None:
    """A resolver who authored a corroborating (kept) member is still a
    party: the guard checks every member, not just retracted ones."""
    kb = _kb(make_kb)
    kb.create_principal(
        "dana@example.com", kind="human", auth_method="oidc", default_capability="review"
    )
    entity = kb.create_entity("Person", author=SRC_A).id
    a = kb.assert_literal(entity, PREDICATE, "100", "Text", SRC_A)
    # dana corroborates via propose (review capability auto-accepts).
    kb.propose(entity, PREDICATE, "100", "Text", "dana@example.com")
    kb.assert_literal(entity, PREDICATE, "120", "Text", SRC_C)
    contradiction = kb.backend.get_open_contradiction("default", entity, PREDICATE)
    assert contradiction is not None

    with pytest.raises(CapabilityError, match="party to"):
        kb.resolve_contradiction(contradiction.id, a.id, "dana@example.com")


def test_superseded_corroborator_stays_superseded(make_kb: KbFactory) -> None:
    """g1 agrees with the winner g2 but was superseded (ADR-0031): it is
    not reactivated and gains no event."""
    kb = _kb(make_kb)
    kb.create_principal(
        "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
    )
    kb.apply_schema(
        SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "employer": PropertyDef(
                            name="employer", value_type="Text", temporality="time_varying"
                        ),
                    },
                ),
            },
        ),
        author="admin@example.com",
    )
    entity = kb.create_entity("Person", author=SRC_A).id
    g1 = kb.assert_literal(entity, "Person.employer", "Globex", "Text", SRC_A)
    kb.assert_literal(entity, "Person.employer", "Acme", "Text", SRC_B)
    g2 = kb.assert_literal(entity, "Person.employer", "Globex", "Text", SRC_C)
    assert _status(kb, g1.id) == "superseded"
    contradiction, _ = kb.flag_contradiction(g1.id, g2.id, NEUTRAL_REVIEWER)
    events_before = kb.backend.get_assertion_events(g1.id)

    kb.resolve_contradiction(contradiction.id, g2.id, REVIEWER)

    assert _status(kb, g1.id) == "superseded"
    assert _status(kb, g2.id) == "active"
    assert kb.backend.get_assertion_events(g1.id) == events_before


def test_all_members_agreeing_retracts_nothing(make_kb: KbFactory) -> None:
    """flag_contradiction() does not require the values to differ; resolving
    an all-agreeing contradiction keeps every member."""
    kb = _kb(make_kb)
    entity = kb.create_entity("Person", author=SRC_A).id
    a = kb.assert_literal(entity, PREDICATE, "100", "Text", SRC_A)
    b = kb.assert_literal(entity, PREDICATE, "100", "Text", SRC_B)
    contradiction, _ = kb.flag_contradiction(a.id, b.id, NEUTRAL_REVIEWER)

    resolved = kb.resolve_contradiction(contradiction.id, a.id, REVIEWER)

    assert resolved.state == "resolved"
    assert _status(kb, a.id) == "active"
    assert _status(kb, b.id) == "active"


def test_kept_corroborator_is_visible_as_of_resolution(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    _, a, b, _, cid = _abc(kb)
    kb.resolve_contradiction(cid, a, REVIEWER)

    visible = {x.id for x in kb.as_of(T0).assertions(predicate=PREDICATE)}

    assert {a, b} <= visible


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    values=st.lists(st.sampled_from(["100", "120", "130"]), min_size=2, max_size=6),
    winner_index=st.integers(min_value=0, max_value=5),
    retract_mask=st.lists(st.booleans(), min_size=6, max_size=6),
)
def test_resolution_is_by_winning_value(
    make_kb: KbFactory, values: list[str], winner_index: int, retract_mask: list[bool]
) -> None:
    """After any resolve, every non-terminal member equal to the winner's
    value is active, every non-terminal member with another value is
    retracted, terminal members are untouched, and the outcome does not
    depend on which equal-valued member was named winner."""
    assume(len(set(values)) >= 2)  # no contradiction without a differing value
    kb = _kb(make_kb)
    entity = kb.create_entity("Person", author=SRC_A).id
    sources = (SRC_A, SRC_B, SRC_C)
    ids = [
        kb.assert_literal(entity, PREDICATE, v, "Text", sources[i % 3], source=f"s{i}").id
        for i, v in enumerate(values)
    ]
    contradiction = kb.backend.get_open_contradiction("default", entity, PREDICATE)
    assert contradiction is not None

    winner_index %= len(values)
    # Terminalize some non-winner members first, via a neutral reviewer.
    terminal: set[str] = set()
    for i, assertion_id in enumerate(ids):
        if i != winner_index and retract_mask[i]:
            kb.retract(assertion_id, NEUTRAL_REVIEWER)
            terminal.add(assertion_id)
    before_events = {i: kb.backend.get_assertion_events(i) for i in terminal}

    winning_value = values[winner_index]
    kb.resolve_contradiction(contradiction.id, ids[winner_index], REVIEWER)

    for assertion_id, value in zip(ids, values, strict=True):
        if assertion_id in terminal:
            assert _status(kb, assertion_id) == "retracted"
            assert kb.backend.get_assertion_events(assertion_id) == before_events[assertion_id]
        elif value == winning_value:
            assert _status(kb, assertion_id) == "active"
        else:
            assert _status(kb, assertion_id) == "retracted"
