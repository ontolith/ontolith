"""Conformance vectors for the assertion status-mutation audit log.

Assertions themselves are append-only (only status/valid_to/supersedes
mutate in place, SPEC §5.3) — the assertion row alone doesn't retain who
caused a given transition or when it happened, only its current status.
AssertionEvent (core/assertion.py) makes every supersession, flagging,
retraction, and reactivation independently attributable and timestamped.
"""

from __future__ import annotations

from datetime import UTC, datetime

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, FixedIdProvider
from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)

AUTHOR = "alice@example.com"
REVIEWER = "bob@example.com"


def _kb(make_kb: KbFactory, *, time_varying: bool = False) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(60)])
    kb = make_kb(clock, ids)
    kb.create_principal(AUTHOR, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    if time_varying:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.apply_schema(
            SchemaIR(
                namespace="default",
                version=1,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={
                            "name": PropertyDef(
                                name="name", value_type="Text", temporality="time_varying"
                            )
                        },
                    )
                },
            ),
            author="admin@example.com",
        )
    return kb


class TestSupersessionEvent:
    def test_supersede_records_event_with_new_authors_id(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb, time_varying=True)
        entity = kb.create_entity("Person", author=AUTHOR)
        old = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        events = kb.backend.get_assertion_events(old.id)
        assert len(events) == 1
        assert events[0].action == "superseded"
        assert events[0].actor == AUTHOR


class TestContradictionEvents:
    def test_fresh_contradiction_records_flag_for_both_members(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        first = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        second = kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        # The first (pre-existing) member transitions active->flagged: 1 event.
        first_events = kb.backend.get_assertion_events(first.id)
        assert len(first_events) == 1
        assert first_events[0].action == "flagged"
        assert first_events[0].actor == AUTHOR

        # The second (incoming) assertion is created already-flagged - it
        # still gets its own "flagged" event (at its own asserted_at), so
        # as_of() can reconstruct that it was never active/undisputed.
        second_events = kb.backend.get_assertion_events(second.id)
        assert len(second_events) == 1
        assert second_events[0].action == "flagged"
        assert second_events[0].actor == AUTHOR

    def test_extending_open_contradiction_does_not_re_flag_existing_members(
        self, make_kb: KbFactory
    ) -> None:
        """Members already in an open contradiction are already flagged -
        adding a third conflicting value must not emit a duplicate 'flagged'
        event for members that didn't actually change status."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        first = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)
        third = kb.assert_literal(entity.id, "Person.name", "Eve", "Text", AUTHOR)

        # first was only ever flagged once, not once per subsequent conflict.
        assert len(kb.backend.get_assertion_events(first.id)) == 1
        # third is the new incoming assertion - it gets its own birth "flagged"
        # event (unlike existing members, it has no prior state to transition
        # from, but as_of() still needs a timestamp for when it became flagged).
        third_events = kb.backend.get_assertion_events(third.id)
        assert len(third_events) == 1
        assert third_events[0].action == "flagged"


class TestRetractionEvents:
    def test_auto_accepted_retract_records_authors_id(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)

        kb.retract(a.id, AUTHOR)

        events = kb.backend.get_assertion_events(a.id)
        assert len(events) == 1
        assert events[0].action == "retracted"
        assert events[0].actor == AUTHOR

    def test_review_accepted_retract_records_proposal_authors_id(self, make_kb: KbFactory) -> None:
        """The proposal's author is recorded (who's accountable for the
        change), not the reviewer who executed the accept - review
        accountability is tracked separately via proposal_event."""
        kb = _kb(make_kb)
        kb.create_principal(
            "bot@example.com", kind="ai", owner=AUTHOR, default_capability="propose"
        )
        entity = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)

        proposal, _ = kb.retract(a.id, "bot@example.com")
        assert proposal.state == "require_review"
        kb.accept_proposal(proposal.id, REVIEWER)

        events = kb.backend.get_assertion_events(a.id)
        assert len(events) == 1
        assert events[0].action == "retracted"
        assert events[0].actor == "bot@example.com"


class TestContradictionResolutionEvents:
    def test_resolution_records_retracted_for_losers_and_reactivated_for_winner(
        self, make_kb: KbFactory
    ) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        winner = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        loser = kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        kb.resolve_contradiction(contradiction.id, winner.id, REVIEWER)

        loser_events = kb.backend.get_assertion_events(loser.id)
        assert loser_events[-1].action == "retracted"
        assert loser_events[-1].actor == REVIEWER

        winner_events = kb.backend.get_assertion_events(winner.id)
        # winner was itself flagged when Ava (the conflicting value) came in,
        # then reactivated on resolution - both are real transitions.
        assert [e.action for e in winner_events] == ["flagged", "reactivated"]
        assert winner_events[-1].actor == REVIEWER


class TestExplicitFlagEvents:
    def test_flag_contradiction_records_flagging_principal(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        kb.assert_literal(entity.id, "Person.born", "1815", "Text", AUTHOR)
        # Distinct sources: an identical repeat would be a no-op (ADR-0054).
        a = kb.assert_ref(entity.id, "Person.employer", entity.id, AUTHOR, source="src-a")
        b = kb.assert_ref(entity.id, "Person.employer", entity.id, AUTHOR, source="src-b")

        kb.flag_contradiction(a.id, b.id, REVIEWER)

        for assertion_id in (a.id, b.id):
            events = kb.backend.get_assertion_events(assertion_id)
            assert len(events) == 1
            assert events[0].action == "flagged"
            assert events[0].actor == REVIEWER

    def test_flag_contradiction_does_not_re_flag_already_flagged_member(
        self, make_kb: KbFactory
    ) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        first = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        second = kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)
        third = kb.assert_literal(entity.id, "Person.name", "Eve", "Text", AUTHOR)
        # first and second are already flagged via auto-detected conflict routing.
        assert len(kb.backend.get_assertion_events(first.id)) == 1
        assert len(kb.backend.get_assertion_events(second.id)) == 1

        # Explicitly flagging an already-flagged assertion alongside another
        # existing member of the same open contradiction must not duplicate
        # either one's "flagged" event.
        kb.flag_contradiction(first.id, third.id, REVIEWER)

        assert len(kb.backend.get_assertion_events(first.id)) == 1


class TestAssertionEventOrderingAndScope:
    def test_events_ordered_oldest_first(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb, time_varying=True)
        entity = kb.create_entity("Person", author=AUTHOR)
        a1 = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        kb.clock.advance(days=1)  # type: ignore[attr-defined]
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)
        kb.clock.advance(days=1)  # type: ignore[attr-defined]
        kb.retract(a1.id, AUTHOR)

        events = kb.backend.get_assertion_events(a1.id)
        assert [e.action for e in events] == ["superseded", "retracted"]
        assert events[0].at <= events[1].at

    def test_events_scoped_to_their_own_assertion(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=AUTHOR)
        a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", AUTHOR)
        b = kb.assert_literal(entity.id, "Person.name", "Ava", "Text", AUTHOR)

        assert [e.assertion_id for e in kb.backend.get_assertion_events(a.id)] == [a.id]
        assert [e.assertion_id for e in kb.backend.get_assertion_events(b.id)] == [b.id]

    def test_no_events_for_unknown_assertion(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        assert kb.backend.get_assertion_events("nonexistent") == []
