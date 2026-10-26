"""Conformance vectors: re-asserting an identical fact is a no-op (ADR-0054).

An incoming assertion equal on (subject, predicate, value_kind, value, author,
acting_as, source, valid_from, valid_to) to an `active` or `flagged` one is not
written; the existing assertion is returned. A different source, author or
window is still new evidence, and a `retracted`/`superseded` row never absorbs
a repeat.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock
from ontolith.core.errors import ValidationError
from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)

SVC = "svc@example.com"
OTHER = "other@example.com"
REVIEWER = "bob@example.com"
ADMIN = "admin@example.com"
SOURCE = "sheet.xlsx#row2"


def _kb(make_kb: KbFactory) -> Ontology:
    kb = make_kb(FixedClock(T0))
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
    kb.create_principal(SVC, kind="service", auth_method="workload", default_capability="write")
    kb.create_principal(OTHER, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    kb.apply_schema(
        SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Item": ConceptDef(
                    name="Item",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                        "employer": PropertyDef(
                            name="employer", value_type="Text", temporality="time_varying"
                        ),
                    },
                ),
            },
        ),
        author=ADMIN,
    )
    return kb


def _all(kb: Ontology, subject: str, predicate: str) -> list:
    return kb.assertions(subject=subject, predicate=predicate, status=None)


def test_repeat_returns_existing_and_writes_nothing(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    item = kb.create_entity("Item", author=SVC).id

    returned = [
        kb.assert_literal(item, "Item.name", "Bolt", "Text", SVC, source=SOURCE) for _ in range(3)
    ]

    assert len({a.id for a in returned}) == 1
    stored = _all(kb, item, "Item.name")
    assert [(a.value, a.status) for a in stored] == [("Bolt", "active")]


def test_repeat_is_deduplicated_when_the_clock_has_moved(make_kb: KbFactory) -> None:
    """valid_from defaults to asserted_at, so an unspecified window must match
    an earlier assertion whose window was defaulted too."""
    kb = _kb(make_kb)
    clock = kb.clock
    assert isinstance(clock, FixedClock)
    item = kb.create_entity("Item", author=SVC).id
    first = kb.assert_literal(item, "Item.name", "Bolt", "Text", SVC, source=SOURCE)
    clock.advance(days=7)

    second = kb.assert_literal(item, "Item.name", "Bolt", "Text", SVC, source=SOURCE)

    assert second.id == first.id
    assert len(_all(kb, item, "Item.name")) == 1


def test_time_varying_repeat_is_deduplicated(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    item = kb.create_entity("Item", author=SVC).id

    for _ in range(3):
        kb.assert_literal(item, "Item.employer", "Acme", "Text", SVC, source=SOURCE)

    assert [(a.value, a.status) for a in _all(kb, item, "Item.employer")] == [("Acme", "active")]


def test_ref_repeat_is_deduplicated(make_kb: KbFactory) -> None:
    kb = make_kb(FixedClock(T0))
    kb.create_principal(SVC, kind="service", auth_method="workload", default_capability="write")
    person = kb.create_entity("Person", author=SVC).id
    org = kb.create_entity("Organization", author=SVC).id

    first = kb.assert_ref(person, "Person.employer", org, SVC, source=SOURCE)
    second = kb.assert_ref(person, "Person.employer", org, SVC, source=SOURCE)

    assert second.id == first.id
    assert len(_all(kb, person, "Person.employer")) == 1


def test_different_source_is_still_corroboration(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    item = kb.create_entity("Item", author=SVC).id
    kb.assert_literal(item, "Item.name", "Bolt", "Text", SVC, source="sheet.xlsx#row2")
    kb.assert_literal(item, "Item.name", "Bolt", "Text", SVC, source="catalog.pdf#p4")

    assert [a.status for a in _all(kb, item, "Item.name")] == ["active", "active"]


def test_different_author_is_still_corroboration(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    item = kb.create_entity("Item", author=SVC).id
    kb.assert_literal(item, "Item.name", "Bolt", "Text", SVC, source=SOURCE)
    kb.assert_literal(item, "Item.name", "Bolt", "Text", OTHER, source=SOURCE)

    assert len(_all(kb, item, "Item.name")) == 2


def test_explicit_window_must_match(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    item = kb.create_entity("Item", author=SVC).id
    start = T0 - timedelta(days=30)
    first = kb.assert_literal(
        item, "Item.name", "Bolt", "Text", SVC, source=SOURCE, valid_from=start
    )
    same = kb.assert_literal(
        item, "Item.name", "Bolt", "Text", SVC, source=SOURCE, valid_from=start
    )
    other = kb.assert_literal(
        item,
        "Item.name",
        "Bolt",
        "Text",
        SVC,
        source=SOURCE,
        valid_from=start,
        valid_to=T0 + timedelta(days=30),
    )

    assert same.id == first.id
    assert other.id != first.id
    assert len(_all(kb, item, "Item.name")) == 2


def test_repeat_after_retraction_is_written(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    item = kb.create_entity("Item", author=SVC).id
    first = kb.assert_literal(item, "Item.name", "Bolt", "Text", SVC, source=SOURCE)
    kb.retract(first.id, REVIEWER)
    assert kb.backend.get_assertion(first.id).status == "retracted"  # type: ignore[union-attr]

    again = kb.assert_literal(item, "Item.name", "Bolt", "Text", SVC, source=SOURCE)

    assert again.id != first.id
    assert again.status == "active"


def test_repeat_after_supersession_is_written(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    item = kb.create_entity("Item", author=SVC).id
    acme = kb.assert_literal(item, "Item.employer", "Acme", "Text", SVC, source=SOURCE)
    kb.assert_literal(item, "Item.employer", "Globex", "Text", SVC, source=SOURCE)
    assert kb.backend.get_assertion(acme.id).status == "superseded"  # type: ignore[union-attr]

    again = kb.assert_literal(item, "Item.employer", "Acme", "Text", SVC, source=SOURCE)

    assert again.id != acme.id
    assert again.status == "active"


def test_flagged_repeat_is_returned_without_extending_the_contradiction(
    make_kb: KbFactory,
) -> None:
    kb = _kb(make_kb)
    item = kb.create_entity("Item", author=SVC).id
    first = kb.assert_literal(item, "Item.name", "Bolt", "Text", SVC, source=SOURCE)
    kb.assert_literal(item, "Item.name", "Nut", "Text", OTHER, source="other.csv#1")
    contradiction = kb.backend.get_open_contradiction("default", item, "Item.name")
    assert contradiction is not None
    members_before = list(contradiction.member_ids)

    repeat = kb.assert_literal(item, "Item.name", "Bolt", "Text", SVC, source=SOURCE)

    assert repeat.id == first.id
    assert repeat.status == "flagged"
    after = kb.backend.get_contradiction(contradiction.id)
    assert after is not None
    assert after.member_ids == members_before


def test_repeat_with_different_confidence_is_returned_unchanged(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    item = kb.create_entity("Item", author=SVC).id
    first = kb.assert_literal(
        item, "Item.name", "Bolt", "Text", SVC, source=SOURCE, confidence=0.9, rationale="first"
    )

    repeat = kb.assert_literal(
        item, "Item.name", "Bolt", "Text", SVC, source=SOURCE, confidence=0.2, rationale="second"
    )

    assert repeat.id == first.id
    assert repeat.confidence == 0.9
    assert repeat.rationale == "first"
    assert len(_all(kb, item, "Item.name")) == 1


def test_supersedes_hint_is_never_absorbed_as_a_repeat(make_kb: KbFactory) -> None:
    kb = make_kb(FixedClock(T0))
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
    kb.apply_schema(
        SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Item": ConceptDef(
                    name="Item",
                    properties={
                        "title": PropertyDef(
                            name="title",
                            value_type="Text",
                            cardinality="many",
                            temporality="time_varying",
                        ),
                    },
                ),
            },
        ),
        author=ADMIN,
    )
    item = kb.create_entity("Item", author=ADMIN).id
    kb.assert_literal(item, "Item.title", "Engineer", "Text", ADMIN, source=SOURCE)
    manager = kb.assert_literal(item, "Item.title", "Manager", "Text", ADMIN, source=SOURCE)

    kb.assert_literal(
        item, "Item.title", "Engineer", "Text", ADMIN, source=SOURCE, supersedes=manager.id
    )

    assert kb.backend.get_assertion(manager.id).status == "superseded"  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        kb.assert_literal(
            item, "Item.title", "Engineer", "Text", ADMIN, source=SOURCE, supersedes="nope"
        )


def test_proposal_path_deduplicates_and_still_records_the_proposal(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    item = kb.create_entity("Item", author=SVC).id
    p1, _ = kb.propose(item, "Item.name", "Bolt", "Text", OTHER, source=SOURCE)
    p2, _ = kb.propose(item, "Item.name", "Bolt", "Text", OTHER, source=SOURCE)

    assert p1.id != p2.id
    assert kb.backend.get_proposal(p2.id) is not None
    assert len(_all(kb, item, "Item.name")) == 1


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    writes=st.lists(
        st.tuples(
            st.sampled_from(["Bolt", "Nut", "Screw"]),
            st.sampled_from(["s1", "s2"]),
            st.sampled_from([SVC, OTHER]),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_applying_a_static_write_sequence_twice_equals_applying_it_once(
    make_kb: KbFactory, writes: list[tuple[str, str, str]]
) -> None:
    kb = _kb(make_kb)
    once = kb.create_entity("Item", author=SVC).id
    twice = kb.create_entity("Item", author=SVC).id

    for subject, passes in ((once, 1), (twice, 2)):
        for _ in range(passes):
            for value, source, author in writes:
                kb.assert_literal(subject, "Item.name", value, "Text", author, source=source)

    def shape(subject: str) -> Counter[tuple[str, str | None, str, str]]:
        return Counter(
            (a.value, a.source, a.author, a.status) for a in _all(kb, subject, "Item.name")
        )

    assert shape(twice) == shape(once)
