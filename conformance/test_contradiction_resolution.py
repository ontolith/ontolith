"""Conformance vectors for contradiction resolution (SPEC §10.3, §19).

Ontology.resolve_contradiction() lets a reviewer pick a winning assertion
among an open contradiction's flagged members. Members with a different
value are retracted, members with the winner's value stay active (ADR-0053), and the contradiction transitions to 'resolved'.
Resolution is append-only: only status/state fields mutate, nothing is deleted.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import Assertion, FixedClock, FixedIdProvider
from ontolith.core.errors import (
    AuthError,
    CapabilityError,
    NotFoundError,
    ValidationError,
)
from ontolith.govern import AutoAccept, Decision, PolicyStrategy, RequireReview
from ontolith.schema import ConceptDef, PropertyDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)
T1 = datetime(2025, 6, 1, tzinfo=UTC)

HUMAN_WRITE = "alice@example.com"
REVIEWER = "bob@example.com"
NON_REVIEWER = "carol@example.com"


class _AlwaysAutoAccept:
    """PolicyStrategy test double that auto-accepts unconditionally,
    including for AI-kind authors ThresholdPolicy would always send to
    review — used to reach KI-043's AI-kind check in
    `_require_capability_to_retract_contradiction_member`, which is otherwise
    unreachable under the default ThresholdPolicy (an AI author's retract
    never auto-accepts in the first place, so that defensive check never
    runs). Deployments are free to plug in a PolicyStrategy this permissive
    (ADR-0006 names PolicyStrategy as an open-core extension point); this
    double exists to prove Ontology itself still enforces the floor even
    then, not to imply ThresholdPolicy behaves this way."""

    def evaluate(
        self, proposal: object, principal: object, kb: object, acting_as: object = None
    ) -> Decision:
        return AutoAccept("test double: always auto-accepts")


class _RacingClock(FixedClock):
    """Test clock double simulating the KI-045 TOCTOU race deterministically,
    without real threads - same technique
    conformance/test_review_workflow.py's `_RacingClock` uses for KI-035.

    `.now()` fires a one-shot side effect (`racer`) directly against the
    backend the *first* time it's called, then behaves like a normal
    `FixedClock`. `flag_contradiction()` calls `self.clock.now()` exactly
    once, as the last statement before opening its write transaction -
    right between the (fixed, KI-045) fresh reads that now happen inside
    that transaction and the pre-transaction identity/capability check
    that still happens before it. Swapping `kb.clock` for one of these
    right before calling `flag_contradiction()` makes `racer` fire at
    that point, simulating "another transition won the race and
    committed first" without any actual concurrency.
    """

    def __init__(self, fixed_time: datetime, racer: Callable[[], None]) -> None:
        super().__init__(fixed_time)
        self._racer = racer
        self._fired = False

    def now(self) -> datetime:
        if not self._fired:
            self._fired = True
            self._racer()
        return super().now()


def _kb(make_kb: KbFactory, *, policy: PolicyStrategy | None = None) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(30)])
    kb = make_kb(clock, ids, policy=policy)
    kb.create_principal(HUMAN_WRITE, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    kb.create_principal(
        NON_REVIEWER, kind="human", auth_method="oidc", default_capability="propose"
    )
    return kb


def _open_contradiction(kb: Ontology, entity_id: str) -> tuple[str, str, str]:
    """Create a two-member contradiction; returns (contradiction_id, ada_id, ava_id)."""
    kb.propose(entity_id, "Person.name", "Ada", "Text", HUMAN_WRITE)
    kb.propose(entity_id, "Person.name", "Ava", "Text", HUMAN_WRITE)
    flagged = kb.assertions(subject=entity_id, predicate="Person.name", status="flagged")
    ada_id = next(a.id for a in flagged if a.value == "Ada")
    ava_id = next(a.id for a in flagged if a.value == "Ava")
    contradiction = kb.backend.get_open_contradiction("default", entity_id, "Person.name")
    assert contradiction is not None
    return contradiction.id, ada_id, ava_id


# ===========================================================================
# raised_by attribution
# ===========================================================================


class TestContradictionRaisedBy:
    """Contradictions record who raised them, whether auto-detected during
    conflict routing or explicitly flagged via flag_contradiction()."""

    def test_auto_detected_contradiction_records_raiser(self, make_kb: KbFactory) -> None:
        """The author of the assertion whose write triggered conflict-routing
        detection is recorded as raised_by."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, _, _ = _open_contradiction(kb, entity.id)

        contradiction = kb.backend.get_contradiction(contradiction_id)
        assert contradiction is not None
        assert contradiction.raised_by == HUMAN_WRITE

    def test_explicit_flag_records_flagging_principal(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.assert_literal(entity.id, "Person.born", "1815", "Text", HUMAN_WRITE)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="propose"
        )
        a = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        b = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)

        contradiction, _ = kb.flag_contradiction(a.id, b.id, "dave@example.com")

        fetched = kb.backend.get_contradiction(contradiction.id)
        assert fetched is not None
        assert fetched.raised_by == "dave@example.com"


# ===========================================================================
# Successful resolution
# ===========================================================================


class TestResolveContradiction:
    def test_third_party_reviewer_can_resolve(self, make_kb: KbFactory) -> None:
        """REVIEWER, party to neither disputed value, can resolve normally -
        pins that KI-026's self-resolution guard doesn't block legitimate
        resolutions, rather than relying implicitly on every other test in
        this class using a reviewer unrelated to both members."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        resolved = kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        assert resolved.state == "resolved"

    def test_resolve_sets_contradiction_state(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        resolved = kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        assert resolved.state == "resolved"

    def test_resolve_sets_resolved_by_and_resolved_at(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        resolved = kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        assert resolved.resolved_by == REVIEWER
        assert resolved.resolved_at is not None

    def test_winner_reactivated(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].id == ada_id
        assert active[0].value == "Ada"

    def test_losers_retracted(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        retracted = kb.assertions(subject=entity.id, predicate="Person.name", status="retracted")
        assert len(retracted) == 1
        assert retracted[0].id == ava_id

    def test_losers_no_longer_flagged(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert flagged == []

    def test_resolution_is_append_only(self, make_kb: KbFactory) -> None:
        """Both original assertion records must still exist with their original values."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        all_assertions = kb.assertions(subject=entity.id, predicate="Person.name", status=None)
        by_id = {a.id: a for a in all_assertions}
        assert by_id[ada_id].value == "Ada"
        assert by_id[ava_id].value == "Ava"

    def test_no_duplicate_retracted_event_for_an_already_retracted_loser(
        self, make_kb: KbFactory
    ) -> None:
        """A loser that's already `retracted` (e.g. a neutral third party's
        own earlier retract(), KI-033/KI-034) needs no further write -
        re-retracting it is a status no-op and must not record a second,
        resolver-misattributed `retracted` event as if the resolver had
        just done it (found in KI-034's review). dave needs review
        capability, not just write, to retract a flagged member (KI-043)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)
        kb.retract(ava_id, "dave@example.com")
        events_before = kb.backend.get_assertion_events(ava_id)

        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)

        events_after = kb.backend.get_assertion_events(ava_id)
        assert events_after == events_before
        assert kb.backend.get_assertion(ava_id).status == "retracted"  # type: ignore[union-attr]

    def test_no_duplicate_event_for_an_already_superseded_loser(self, make_kb: KbFactory) -> None:
        """A loser that's already `superseded` (e.g. named via
        flag_contradiction(), which accepts a superseded assertion by
        design) needs no further write either - overwriting it to
        `retracted` would misrepresent how it actually became terminal,
        and record a second, resolver-misattributed event (ADR-0031,
        found in review alongside the winner-eligibility fix)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        kb.create_principal(
            "grace@example.com", kind="human", auth_method="oidc", default_capability="review"
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
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        acme = kb.assert_literal(entity.id, "Person.employer", "Acme", "Text", HUMAN_WRITE)
        globex = kb.assert_literal(entity.id, "Person.employer", "Globex", "Text", HUMAN_WRITE)
        assert kb.backend.get_assertion(acme.id).status == "superseded"  # type: ignore[union-attr]

        contradiction, _action = kb.flag_contradiction(acme.id, globex.id, "grace@example.com")
        events_before = kb.backend.get_assertion_events(acme.id)

        kb.resolve_contradiction(contradiction.id, globex.id, "grace@example.com")

        events_after = kb.backend.get_assertion_events(acme.id)
        assert events_after == events_before
        assert kb.backend.get_assertion(acme.id).status == "superseded"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(globex.id).status == "active"  # type: ignore[union-attr]

    def test_three_member_contradiction_resolves_all_losers(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Eve", "Text", HUMAN_WRITE)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        winner = next(a for a in flagged if a.value == "Ada")
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        kb.resolve_contradiction(contradiction.id, winner.id, REVIEWER)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        retracted = kb.assertions(subject=entity.id, predicate="Person.name", status="retracted")
        assert len(active) == 1
        assert len(retracted) == 2


# ===========================================================================
# Guard rails
# ===========================================================================


class TestResolveContradictionGuards:
    def test_unknown_resolver_raises_auth_error(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(AuthError, match="Principal not found"):
            kb.resolve_contradiction(contradiction_id, ada_id, "nobody@example.com")

    def test_non_reviewer_raises_capability_error(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(CapabilityError, match="lacks review capability"):
            kb.resolve_contradiction(contradiction_id, ada_id, NON_REVIEWER)

    def test_unknown_contradiction_raises_not_found(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        with pytest.raises(NotFoundError, match="Contradiction not found"):
            kb.resolve_contradiction("nonexistent-id", "some-assertion", REVIEWER)

    def test_winner_not_a_member_raises_validation_error(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, _, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(ValidationError, match="is not a member"):
            kb.resolve_contradiction(contradiction_id, "not-a-real-assertion", REVIEWER)

    def test_already_retracted_winner_raises_validation_error(self, make_kb: KbFactory) -> None:
        """Retraction is terminal for winner selection too (KI-044,
        ADR-0031, extending KI-033/KI-034's same principle) - picking an
        already-retracted member as winner must not reactivate it to
        `active` with a closed `valid_to`, silently undoing the
        retraction's close of that window with no new write recording it
        reopened."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)
        # REVIEWER is neutral (party to neither Ada nor Ava) and already
        # review-capable, so this retraction auto-accepts directly.
        kb.retract(ava_id, REVIEWER)
        assert kb.backend.get_assertion(ava_id).status == "retracted"  # type: ignore[union-attr]

        with pytest.raises(ValidationError, match="already retracted"):
            kb.resolve_contradiction(contradiction_id, ava_id, REVIEWER)

        # Rejected before any write - nothing about the contradiction or
        # its members changed.
        assert kb.backend.get_assertion(ava_id).status == "retracted"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(ada_id).status == "flagged"  # type: ignore[union-attr]
        contradiction = kb.backend.get_contradiction(contradiction_id)
        assert contradiction is not None
        assert contradiction.state == "open"

    def test_still_flagged_winner_unaffected_by_retracted_winner_check(
        self, make_kb: KbFactory
    ) -> None:
        """A contradiction with one already-retracted member (e.g. via a
        neutral third party's earlier retract(), KI-033) can still be
        resolved normally by picking the still-`flagged` member as
        winner - KI-044's check only rejects the winner candidate itself
        being retracted, not a contradiction merely containing a retracted
        loser (that shape is already covered by
        TestResolveContradiction::test_no_duplicate_retracted_event_for_an_already_retracted_loser)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)
        kb.retract(ava_id, REVIEWER)
        assert kb.backend.get_assertion(ava_id).status == "retracted"  # type: ignore[union-attr]

        resolved = kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)

        assert resolved.state == "resolved"
        assert kb.backend.get_assertion(ada_id).status == "active"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(ava_id).status == "retracted"  # type: ignore[union-attr]

    def test_already_superseded_winner_raises_validation_error(self, make_kb: KbFactory) -> None:
        """Supersession is terminal for winner selection too (ADR-0031,
        found in review to be needed alongside `retracted` - not covered
        by the first version of this fix). flag_contradiction() accepts a
        superseded assertion as a member by design (its own inline
        comment: "a flagged/superseded assertion must still be resolvable
        here"),
        so resolve_contradiction() must reject it as winner the same way
        it rejects a retracted one."""
        kb = _kb(make_kb)
        kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        schema = SchemaIR(
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
        )
        kb.apply_schema(schema, author="admin@example.com")
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        old = kb.assert_literal(entity.id, "Person.employer", "Acme", "Text", HUMAN_WRITE)
        new = kb.assert_literal(entity.id, "Person.employer", "Globex", "Text", HUMAN_WRITE)
        assert kb.backend.get_assertion(old.id).status == "superseded"  # type: ignore[union-attr]

        contradiction, _action = kb.flag_contradiction(old.id, new.id, REVIEWER)
        # flag_contradiction() accepts a superseded assertion by design but
        # never re-flags it - its status stays `superseded`.
        assert kb.backend.get_assertion(old.id).status == "superseded"  # type: ignore[union-attr]

        with pytest.raises(ValidationError, match="already superseded"):
            kb.resolve_contradiction(contradiction.id, old.id, REVIEWER)

    def test_party_guard_takes_precedence_over_terminal_winner_check(
        self, make_kb: KbFactory
    ) -> None:
        """A resolver who is both a party to the contradiction AND picks a
        terminal-status (retracted/superseded) winner always sees the
        party CapabilityError, never the terminal-status ValidationError -
        deterministic regardless of contradiction.member_ids' iteration
        order (ADR-0031's fix for an earlier, order-dependent version of
        this check, found in review)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "frank@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        kb.create_principal(
            "grace@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        # Ava (the terminal-status winner candidate) is proposed FIRST and
        # Ada (frank's own, making frank a party) SECOND, so the loop
        # reaches the winner before the party violation - proving the
        # party check still wins even then (the loop only *captures* the
        # winner as it passes; it doesn't check its status until the
        # whole loop has passed with no party violation).
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        kb.propose(entity.id, "Person.name", "Ada", "Text", "frank@example.com")
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        ava_id = next(a.id for a in flagged if a.author == REVIEWER)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        assert contradiction.member_ids[0] == ava_id  # winner listed first

        kb.retract(ava_id, "grace@example.com")
        assert kb.backend.get_assertion(ava_id).status == "retracted"  # type: ignore[union-attr]

        with pytest.raises(CapabilityError, match="party to"):
            kb.resolve_contradiction(contradiction.id, ava_id, "frank@example.com")

    def test_all_members_terminal_stays_open_with_fresh_assertion_as_escape_hatch(
        self, make_kb: KbFactory
    ) -> None:
        """A contradiction whose every existing member ends up retracted
        has no eligible winner until a fresh assertion lands - an
        accepted, documented consequence (ADR-0031), not a true dead end
        for a `static` predicate (contradictions *auto-detected by
        conflict routing* can only arise on `static` predicates in the
        first place, SPEC §10.1 - though flag_contradiction() itself can
        still open one on any predicate, see the next test): a fresh
        assertion for the intended value joins the same open contradiction
        as a new `flagged` member and resolves normally. See
        test_time_varying_predicate_has_no_bare_reassert_escape_hatch for
        the different, `flag_contradiction()`-only story a `superseded`
        member's own `time_varying` predicate leaves behind (found in
        review - this test's claim doesn't generalize)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "grace@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)
        kb.retract(ada_id, "grace@example.com")
        kb.retract(ava_id, "grace@example.com")
        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(ava_id).status == "retracted"  # type: ignore[union-attr]

        with pytest.raises(ValidationError, match="already retracted"):
            kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)
        contradiction = kb.backend.get_contradiction(contradiction_id)
        assert contradiction is not None
        assert contradiction.state == "open"

        kb.propose(entity.id, "Person.name", "Aida", "Text", HUMAN_WRITE)
        aida_id = next(
            a.id
            for a in kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
            if a.value == "Aida"
        )
        contradiction = kb.backend.get_contradiction(contradiction_id)
        assert contradiction is not None
        assert aida_id in contradiction.member_ids

        resolved = kb.resolve_contradiction(contradiction_id, aida_id, REVIEWER)

        assert resolved.state == "resolved"
        assert kb.backend.get_assertion(aida_id).status == "active"  # type: ignore[union-attr]

    def test_time_varying_predicate_has_no_bare_reassert_escape_hatch(
        self, make_kb: KbFactory
    ) -> None:
        """Found in review: the previous test's "just assert it again"
        escape hatch does NOT generalize to a `time_varying` predicate
        (the only kind a `superseded` member's predicate can be).
        `_apply_with_conflict_routing`'s open-contradiction-extension
        shortcut is gated on `temporality == "static"`, so a fresh
        assertion on a `time_varying` predicate never takes that path even
        when every existing member of the open contradiction is terminal
        - it goes through ordinary routing instead, finds no `active`
        assertion to conflict with, and comes back `active` on its own,
        never joining the contradiction at all. `flag_contradiction()` -
        not a bare re-assert - is the actual remedy (ADR-0031)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        kb.create_principal(
            "grace@example.com", kind="human", auth_method="oidc", default_capability="review"
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
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        acme = kb.assert_literal(entity.id, "Person.employer", "Acme", "Text", HUMAN_WRITE)
        globex = kb.assert_literal(entity.id, "Person.employer", "Globex", "Text", HUMAN_WRITE)
        assert kb.backend.get_assertion(acme.id).status == "superseded"  # type: ignore[union-attr]

        contradiction, _action = kb.flag_contradiction(acme.id, globex.id, "grace@example.com")
        kb.retract(globex.id, "grace@example.com")
        assert kb.backend.get_assertion(globex.id).status == "retracted"  # type: ignore[union-attr]
        # Both members are now terminal (superseded, retracted) - neither
        # is an eligible winner.
        with pytest.raises(ValidationError, match="already superseded"):
            kb.resolve_contradiction(contradiction.id, acme.id, "grace@example.com")
        with pytest.raises(ValidationError, match="already retracted"):
            kb.resolve_contradiction(contradiction.id, globex.id, "grace@example.com")

        # A bare re-assert does NOT rejoin the contradiction - it comes
        # back active and untracked, not flagged.
        initech = kb.assert_literal(entity.id, "Person.employer", "Initech", "Text", HUMAN_WRITE)
        assert kb.backend.get_assertion(initech.id).status == "active"  # type: ignore[union-attr]
        contradiction = kb.backend.get_contradiction(contradiction.id)
        assert contradiction is not None
        assert initech.id not in contradiction.member_ids
        assert contradiction.state == "open"

        # The actual remedy: flag_contradiction() extends the same
        # contradiction and flips the fresh assertion to flagged, which
        # can then be picked as winner normally.
        kb.flag_contradiction(initech.id, acme.id, "grace@example.com")
        assert kb.backend.get_assertion(initech.id).status == "flagged"  # type: ignore[union-attr]

        resolved = kb.resolve_contradiction(contradiction.id, initech.id, "grace@example.com")

        assert resolved.state == "resolved"
        assert kb.backend.get_assertion(initech.id).status == "active"  # type: ignore[union-attr]

    def test_already_resolved_contradiction_raises(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)
        kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)

        with pytest.raises(ValidationError, match="is not open"):
            kb.resolve_contradiction(contradiction_id, ada_id, REVIEWER)

    def test_ai_resolver_raises_capability_error(self, make_kb: KbFactory) -> None:
        """A misconfigured AI principal with review capability must still be
        blocked from resolving contradictions (ADR-0003)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "misconfigured-ai-reviewer",
            kind="ai",
            auth_method="apikey",
            owner=HUMAN_WRITE,
            default_capability="review",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)

        with pytest.raises(CapabilityError, match="AI principal and cannot review"):
            kb.resolve_contradiction(contradiction_id, ada_id, "misconfigured-ai-reviewer")

    def test_resolver_cannot_pick_own_authored_assertion_as_winner(
        self, make_kb: KbFactory
    ) -> None:
        """REVIEWER authored one of the two disputed facts - can't
        unilaterally pick it as the winner (KI-026)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        reviewer_authored_id = next(a.id for a in flagged if a.author == REVIEWER)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        with pytest.raises(CapabilityError, match="cannot resolve a contradiction"):
            kb.resolve_contradiction(contradiction.id, reviewer_authored_id, REVIEWER)

    def test_resolver_cannot_resolve_when_they_authored_a_losing_member(
        self, make_kb: KbFactory
    ) -> None:
        """REVIEWER authored one of the disputed facts, even the one NOT
        picked as winner - still blocked, since resolving still means
        adjudicating a dispute they're a party to (KI-026)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        other_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        with pytest.raises(CapabilityError, match="cannot resolve a contradiction"):
            kb.resolve_contradiction(contradiction.id, other_id, REVIEWER)

    def test_resolver_cannot_resolve_via_delegate_authored_assertion(
        self, make_kb: KbFactory
    ) -> None:
        """A disputed fact asserted by REVIEWER's delegate (acting_as)
        counts the same as one REVIEWER authored directly - the delegation
        chain doesn't launder around the self-resolution guard."""
        kb = _kb(make_kb)
        kb.create_principal(
            "erin@example.com",
            kind="human",
            auth_method="oidc",
            owner=REVIEWER,
            default_capability="write",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", "erin@example.com", acting_as=REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        delegated_id = next(a.id for a in flagged if a.author == "erin@example.com")
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        with pytest.raises(CapabilityError, match="cannot resolve a contradiction"):
            kb.resolve_contradiction(contradiction.id, delegated_id, REVIEWER)


# ===========================================================================
# retract() contradiction-member guard (KI-033)
# ===========================================================================


class TestRetractContradictionGuard:
    """retract() is a separate path from resolve_contradiction() that can
    reach a similar outcome — a party to a disputed static fact ending the
    dispute in their own favor unilaterally, just by retracting the
    *opposing* flagged member instead of picking a winner (KI-033)."""

    def test_party_cannot_retract_opposing_contradiction_member(self, make_kb: KbFactory) -> None:
        """HUMAN_WRITE authored "Ada", REVIEWER authored "Ava" - HUMAN_WRITE
        can't unilaterally retract REVIEWER's opposing entry."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        opposing_id = next(a.id for a in flagged if a.author == REVIEWER)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None

        with pytest.raises(CapabilityError, match="party to"):
            kb.retract(opposing_id, HUMAN_WRITE)

        # Rejected before any write — assertion and contradiction untouched.
        assert kb.backend.get_assertion(opposing_id).status == "flagged"  # type: ignore[union-attr]
        assert kb.backend.get_contradiction(contradiction.id).state == "open"  # type: ignore[union-attr]

    def test_party_cannot_retract_own_contradiction_member(self, make_kb: KbFactory) -> None:
        """Retracting your *own* losing entry ends the dispute in your favor
        just as much as picking it as the winner would - blocked too, same
        as resolve_contradiction's "any member" guard (KI-026)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        own_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        with pytest.raises(CapabilityError, match="party to"):
            kb.retract(own_id, HUMAN_WRITE)

    def test_delegate_authored_member_blocks_retraction(self, make_kb: KbFactory) -> None:
        """A disputed fact asserted by REVIEWER's delegate (acting_as) counts
        the same as one REVIEWER authored directly."""
        kb = _kb(make_kb)
        kb.create_principal(
            "erin@example.com",
            kind="human",
            auth_method="oidc",
            owner=REVIEWER,
            default_capability="write",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", "erin@example.com", acting_as=REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        ada_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        with pytest.raises(CapabilityError, match="party to"):
            kb.retract(ada_id, REVIEWER)

    def test_neutral_write_capability_principal_is_routed_to_review(
        self, make_kb: KbFactory
    ) -> None:
        """A principal with no stake in either disputed value (not an
        author/delegate of any member) clears the party guard above, but
        `write` capability alone is no longer enough to auto-accept a
        retraction of a flagged contradiction member (KI-043) —
        retracting one side of a dispute is a smaller-grained way of
        adjudicating it, the same reason resolve_contradiction() itself
        requires review/admin. Rather than a hard error with no path
        forward, this routes to review — a review-capable, non-AI
        principal can then accept it (ADR-0030's fix for an inversion an
        earlier version of this check had: a write-capability principal
        was left worse off than a merely propose-capability one, whose
        retraction already went through review normally). Renamed from
        `test_neutral_third_party_can_still_retract_flagged_member`, which
        pinned the pre-KI-043 behavior this test now supersedes."""
        kb = _kb(make_kb)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        kb.create_principal(
            "erin@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        ada_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        proposal, decision = kb.retract(ada_id, "dave@example.com")

        assert isinstance(decision, RequireReview)
        assert kb.backend.get_assertion(ada_id).status == "flagged"  # type: ignore[union-attr]

        # A neutral, review-capable, non-AI principal can accept it -
        # KI-043 raises the floor, it doesn't remove retract() as a path.
        kb.accept_proposal(proposal.id, "erin@example.com")
        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]

    def test_neutral_review_capability_principal_can_still_retract(
        self, make_kb: KbFactory
    ) -> None:
        """A neutral principal (no stake in either disputed value) who also
        meets resolve_contradiction()'s own review/admin floor can still
        retract a flagged member directly — KI-043 raises the floor, it
        doesn't remove retract() as a path."""
        kb = _kb(make_kb)
        kb.create_principal(
            "erin@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        ada_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        kb.retract(ada_id, "erin@example.com")

        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]

    def test_ai_principal_is_routed_to_review_even_with_review_capability(
        self, make_kb: KbFactory
    ) -> None:
        """An AI principal is routed to review regardless of its configured
        capability, mirroring resolve_contradiction()'s own AI block
        (SPEC §10.3, ADR-0003) — capability alone isn't the whole floor.

        Uses a permissive test-double policy (_AlwaysAutoAccept) because
        the default ThresholdPolicy already sends every AI-authored
        proposal to review (never auto-accept) — this test exists to prove
        Ontology's own KI-043 check is a real backstop, not just something
        ThresholdPolicy happens to make redundant."""
        kb = _kb(make_kb, policy=_AlwaysAutoAccept())
        kb.create_principal(
            "frank-ai",
            kind="ai",
            auth_method="workload",
            owner=REVIEWER,
            default_capability="review",
        )
        kb.create_principal(
            "erin@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        ada_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        proposal, decision = kb.retract(ada_id, "frank-ai")

        assert isinstance(decision, RequireReview)
        assert kb.backend.get_assertion(ada_id).status == "flagged"  # type: ignore[union-attr]

        kb.accept_proposal(proposal.id, "erin@example.com")
        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]

    def test_delegation_attenuates_capability_floor_for_retract(self, make_kb: KbFactory) -> None:
        """A review-capable principal delegating through a write-only
        principal gets the *lower* of the two, mirroring
        `_check_direct_write_capability`'s existing delegation-attenuation
        (SPEC §8.4) — the floor can't be laundered by picking whichever of
        the pair happens to qualify. Both grace and henry are neutral
        (authors of neither disputed value) so this isolates the capability
        check from the separate party-to-contradiction guard above."""
        kb = _kb(make_kb)
        kb.create_principal(
            "henry@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        kb.create_principal(
            "grace@example.com",
            kind="human",
            auth_method="oidc",
            owner="henry@example.com",
            default_capability="write",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        ada_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        # grace (write) acting on behalf of henry (review): effective
        # capability is min(write, review) = write, still below the floor
        # -> routed to review rather than auto-accepted.
        proposal, decision = kb.retract(ada_id, "grace@example.com", acting_as="henry@example.com")

        assert isinstance(decision, RequireReview)
        assert kb.backend.get_assertion(ada_id).status == "flagged"  # type: ignore[union-attr]

    def test_flagged_status_without_open_contradiction_is_unaffected(
        self, make_kb: KbFactory
    ) -> None:
        """Defensive branch: an assertion whose status happens to be
        `flagged` with no matching open contradiction record (there is no
        code path that produces this today - every `flagged` status is
        always paired with a Contradiction row) does not trip this guard;
        retract() proceeds normally rather than treating a `None` lookup as
        a reason to block."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a = kb.assert_literal(entity.id, "Person.born", "1815", "Text", HUMAN_WRITE)
        kb.backend.set_assertion_status(a.id, "flagged")

        kb.retract(a.id, HUMAN_WRITE)

        assert kb.backend.get_assertion(a.id).status == "retracted"  # type: ignore[union-attr]

    def test_missing_contradiction_member_raises_not_found(self, make_kb: KbFactory) -> None:
        """A contradiction member that can't be found is data corruption
        (assertions are append-only and never deleted, SPEC §5), not a
        benign gap to skip past - matches resolve_contradiction's own
        precedent (KI-026)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "dave@example.com", kind="human", auth_method="oidc", default_capability="write"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        _, ada_id, _ = _open_contradiction(kb, entity.id)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        kb.backend.update_contradiction_members(
            contradiction.id, [*contradiction.member_ids, "nonexistent-assertion-id"]
        )

        with pytest.raises(NotFoundError, match="could not be found"):
            kb.retract(ada_id, "dave@example.com")

    def test_party_via_accept_proposal_is_also_blocked(self, make_kb: KbFactory) -> None:
        """The guard also applies when the retraction reaches its effect via
        accept_proposal() (e.g. a lower-capability delegate's retract
        proposal that required review), not just retract()'s own
        auto-accept path - closing the same gap through the review queue,
        not only the direct call."""
        kb = _kb(make_kb)
        kb.create_principal(
            "erin@example.com",
            kind="human",
            auth_method="oidc",
            owner=REVIEWER,
            default_capability="propose",
        )
        kb.create_principal(
            "frank@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        opposing_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        # erin's own capability (propose, trust 0) requires review even when
        # delegating through REVIEWER (effective capability is min() of the
        # two, ADR-0003) - so this retract lands in the review queue rather
        # than auto-accepting.
        proposal, decision = kb.retract(opposing_id, "erin@example.com", acting_as=REVIEWER)
        assert proposal.state == "require_review"

        with pytest.raises(CapabilityError, match="party to"):
            kb.accept_proposal(proposal.id, "frank@example.com")

    def test_party_reviewer_cannot_accept_a_neutral_proposers_retract_proposal(
        self, make_kb: KbFactory
    ) -> None:
        """The party doesn't have to be the proposer at all - a reviewer who
        is themselves a party to the contradiction can reach the exact same
        one-sided outcome by *approving* an entirely neutral principal's
        retract proposal, so the guard must check the accepting reviewer
        too, not just the proposal's own author/delegate (found in review)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "grace@example.com", kind="human", auth_method="oidc", default_capability="propose"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        opposing_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)

        # grace has no stake in either disputed value and no delegation -
        # her own low capability (propose, trust 0) is why this lands in
        # the review queue, not any party-to-contradiction reasoning.
        proposal, decision = kb.retract(opposing_id, "grace@example.com")
        assert proposal.state == "require_review"

        # REVIEWER authored the *other* disputed value ("Ava") - approving
        # grace's proposal to retract "Ada" would let REVIEWER settle the
        # dispute in their own favor just by reviewing someone else's
        # proposal instead of retracting directly.
        with pytest.raises(CapabilityError, match="party to"):
            kb.accept_proposal(proposal.id, REVIEWER)

        assert kb.backend.get_assertion(opposing_id).status == "flagged"  # type: ignore[union-attr]
        assert kb.backend.get_proposal(proposal.id).state == "require_review"  # type: ignore[union-attr]


# ===========================================================================
# KI-033/KI-043 guards apply to a `retracted`/`superseded` member of an
# open contradiction too, not just a `flagged` one (KI-051)
# ===========================================================================


class TestRetractGuardsApplyToTerminalMembers:
    """Before KI-051, `_reject_retract_if_party_to_contradiction` and the
    KI-043 capability floor both gated on `status == "flagged"` before ever
    checking contradiction membership - so a member ADR-0031's own design
    had already terminalized (`retracted` via an earlier retract(), or
    `superseded` via flag_contradiction() naming one) while its
    contradiction stayed `open` was completely exempt from both guards. A
    party could retract it - or a below-floor neutral principal could
    auto-accept retracting it - with no governance applied at all, even
    though the contradiction it belongs to was still open."""

    def _open_contradiction_with_retracted_member(
        self, make_kb: KbFactory
    ) -> tuple[Ontology, str, str, str]:
        """An open contradiction between HUMAN_WRITE's "Ada" and
        REVIEWER's "Ava" - two different authors, so REVIEWER is a genuine
        party (unlike the shared _open_contradiction() helper, whose two
        members are both authored by HUMAN_WRITE and so have no party to
        test against). "Ada" has already been retracted by a third,
        neutral review-capability principal; the contradiction stays open
        (only resolve_contradiction() closes it).
        Returns (kb, entity_id, retracted_ada_id, still_flagged_ava_id)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "erin@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN_WRITE)
        kb.propose(entity.id, "Person.name", "Ava", "Text", REVIEWER)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        ada_id = next(a.id for a in flagged if a.author == HUMAN_WRITE)
        ava_id = next(a.id for a in flagged if a.author == REVIEWER)
        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        kb.retract(ada_id, "erin@example.com")
        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]
        assert kb.backend.get_contradiction(contradiction.id).state == "open"  # type: ignore[union-attr]
        return kb, entity.id, ada_id, ava_id

    def test_party_cannot_retract_already_retracted_opposing_member(
        self, make_kb: KbFactory
    ) -> None:
        """REVIEWER (author of the still-flagged "Ava") is a party to the
        contradiction - blocked from retracting the *already-retracted*
        "Ada", exactly as they'd be blocked from retracting it while still
        flagged. Pre-KI-051 this call bypassed the party guard entirely,
        since the guard's own status check gated on == "flagged"."""
        kb, _entity_id, ada_id, _ava_id = self._open_contradiction_with_retracted_member(make_kb)

        with pytest.raises(CapabilityError, match="party to"):
            kb.retract(ada_id, REVIEWER)

    def test_party_cannot_retract_already_superseded_member(self, make_kb: KbFactory) -> None:
        """Same guard, for a `superseded` member named via
        flag_contradiction() (ADR-0031's own escape hatch) rather than a
        `retracted` one."""
        kb = _kb(make_kb)
        admin = "admin@example.com"
        kb.create_principal(admin, kind="human", auth_method="oidc", default_capability="admin")
        schema = SchemaIR(
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
        )
        kb.apply_schema(schema, author=admin)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        acme = kb.assert_literal(entity.id, "Person.employer", "Acme", "Text", HUMAN_WRITE)
        # REVIEWER only has `review` capability - assert_literal requires
        # write/admin, so REVIEWER's "Globex" goes through propose()
        # (review-capability auto-accepts under ThresholdPolicy).
        kb.propose(entity.id, "Person.employer", "Globex", "Text", REVIEWER)
        globex = next(
            a
            for a in kb.assertions(subject=entity.id, predicate="Person.employer", status="active")
            if a.value == "Globex"
        )
        assert kb.backend.get_assertion(acme.id).status == "superseded"  # type: ignore[union-attr]
        contradiction, _action = kb.flag_contradiction(acme.id, globex.id, "admin@example.com")
        assert contradiction.state == "open"

        # REVIEWER authored "Globex" - a party to the contradiction,
        # blocked from retracting the opposing (superseded) "Acme".
        with pytest.raises(CapabilityError, match="party to"):
            kb.retract(acme.id, REVIEWER)

    def test_below_floor_neutral_principal_retracting_terminal_member_is_routed_to_review(
        self, make_kb: KbFactory
    ) -> None:
        """A neutral (non-party), merely-`write`-capability principal
        retracting an already-`retracted` contradiction member is routed
        to review (KI-043's floor), not auto-accepted - the same as
        retracting a still-`flagged` member would be. Pre-KI-051 this
        auto-accepted outright, since _open_contradiction_if_member (then
        `..._if_flagged_member`) returned None for a non-flagged target,
        making the capability floor a no-op for exactly this case."""
        kb, _entity_id, ada_id, _ava_id = self._open_contradiction_with_retracted_member(make_kb)
        kb.create_principal(
            "frank@example.com", kind="human", auth_method="oidc", default_capability="write"
        )

        proposal, decision = kb.retract(ada_id, "frank@example.com")

        assert isinstance(decision, RequireReview)
        # No further write happened - re-retracting is asserted separately
        # below (TestRetractAlreadyTerminalIdempotency), not this test's
        # concern; here only the routing decision matters.
        assert proposal.state == "require_review"


# ===========================================================================
# retract() no-ops on an already-`retracted` target instead of recording a
# second, misattributed event (KI-051)
# ===========================================================================


class TestRetractAlreadyTerminalIdempotency:
    """Deliberately narrower than resolve_contradiction()'s own loser-loop
    precedent (KI-044, ADR-0031), which treats `retracted` AND `superseded`
    losers identically: that loop is an automatic side effect of picking a
    winner, not a call the user directly targeted at that specific
    assertion. retract() itself only no-ops on an exact `retracted` ->
    `retracted` re-call - explicitly retracting a `superseded` assertion
    remains a real, event-recording transition
    (test_events_ordered_oldest_first,
    test_retract_never_widens_an_already_closed_valid_to already pin this
    half in conformance/test_bitemporal.py and
    conformance/test_assertion_audit_log.py)."""

    def test_re_retracting_records_no_second_event(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a = kb.assert_literal(entity.id, "Person.born", "1815", "Text", HUMAN_WRITE)
        kb.retract(a.id, HUMAN_WRITE)
        events_after_first_retract = kb.backend.get_assertion_events(a.id)
        assert [e.action for e in events_after_first_retract] == ["retracted"]

        kb.retract(a.id, HUMAN_WRITE)

        assert kb.backend.get_assertion_events(a.id) == events_after_first_retract
        assert kb.backend.get_assertion(a.id).status == "retracted"  # type: ignore[union-attr]

    def test_retracting_unknown_assertion_id_raises_not_found(self, make_kb: KbFactory) -> None:
        """The idempotency no-op's `current is None` branch does NOT fall
        through to attempt the write - unlike before the KI-074 review
        fix, an unknown id is now checked explicitly and raises
        NotFoundError, rather than silently treating an unknown id the
        same as an already-terminal one and letting it surface as an
        opaque StorageError from set_assertion_status. A caller-supplied
        bad id must stay distinguishable from a genuine storage fault
        (SPEC §16) - see docs/known-issues.md KI-074."""
        kb = _kb(make_kb)

        with pytest.raises(NotFoundError, match="nonexistent-assertion-id"):
            kb.retract("nonexistent-assertion-id", HUMAN_WRITE)

    def test_re_retracting_does_not_widen_valid_to(self, make_kb: KbFactory) -> None:
        """A second retract() call must not push valid_to forward either.
        Already guaranteed independently by _retraction_valid_to (it
        returns None, leaving valid_to untouched, once valid_to is already
        set) rather than by this KI's own idempotency no-op specifically -
        this is a regression guard for that existing behavior in the
        re-retraction shape, not a test that discriminates the no-op
        itself (see test_re_retracting_records_no_second_event for that)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a = kb.assert_literal(entity.id, "Person.born", "1815", "Text", HUMAN_WRITE)
        kb.retract(a.id, HUMAN_WRITE)
        first_valid_to = kb.backend.get_assertion(a.id).valid_to  # type: ignore[union-attr]

        kb.clock.advance(days=1)  # type: ignore[attr-defined]
        kb.retract(a.id, HUMAN_WRITE)

        assert kb.backend.get_assertion(a.id).valid_to == first_valid_to  # type: ignore[union-attr]

    def test_accept_proposal_replay_of_retract_also_no_ops(self, make_kb: KbFactory) -> None:
        """The same idempotency applies to a retract op replayed via
        accept_proposal() (_replay_proposal_operations), not just
        retract()'s own direct auto-accept path."""
        kb = _kb(make_kb)
        kb.create_principal(
            "grace@example.com", kind="human", auth_method="oidc", default_capability="propose"
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a = kb.assert_literal(entity.id, "Person.born", "1815", "Text", HUMAN_WRITE)
        kb.retract(a.id, HUMAN_WRITE)
        events_before = kb.backend.get_assertion_events(a.id)

        # grace's own low capability (propose, trust 0) - not the target's
        # already-retracted status - is what routes this to review.
        proposal, decision = kb.retract(a.id, "grace@example.com")
        assert isinstance(decision, RequireReview)

        accepted = kb.accept_proposal(proposal.id, REVIEWER)

        assert accepted.state == "accepted"
        assert kb.backend.get_assertion_events(a.id) == events_before
        assert kb.backend.get_assertion(a.id).status == "retracted"  # type: ignore[union-attr]


# ===========================================================================
# `retracted` is terminal across contradiction extension (KI-034)
# ===========================================================================


class TestRetractedIsTerminalAcrossExtension:
    """`retracted` (KI-034) and `superseded` (KI-044/ADR-0031) are both
    meant to be terminal statuses everywhere in the codebase (SPEC §5's
    append-only lifecycle). Extending an already-open contradiction with a
    fresh disputed value previously re-flagged *every* existing member
    unconditionally, including one that had since been legitimately
    retracted (e.g. by a neutral party via retract(), KI-033) or superseded
    (a time_varying predicate later redeclared static, with the old value
    named via flag_contradiction() - which accepts a superseded assertion
    by design) - resurrecting it back to `flagged`. Only the first and the
    superseded-specific test below actually exercise their respective
    regressions (confirmed by reverting each fix locally and re-running -
    both fail without their fix, and mutation-testing found the superseded
    one had no coverage at all until this test was added); the other two
    pin adjacent, already correct-pre-fix invariants (no duplicate event,
    membership retained for audit) that this change deliberately preserves
    rather than disturbs."""

    def test_extending_open_contradiction_does_not_resurrect_retracted_member(
        self, make_kb: KbFactory
    ) -> None:
        kb = _kb(make_kb)
        kb.create_principal(
            # review capability, not just write - retracting a flagged
            # contradiction member requires it now (KI-043); dave remains
            # neutral (party to neither disputed value).
            "dave@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="review",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)

        # A neutral third party retracts one member outright (allowed -
        # dave is party to neither disputed value, KI-033).
        kb.retract(ada_id, "dave@example.com")
        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]

        # A third disputed value extends the still-open contradiction.
        kb.propose(entity.id, "Person.name", "Aida", "Text", REVIEWER)

        assert kb.backend.get_assertion(ada_id).status == "retracted"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(ava_id).status == "flagged"  # type: ignore[union-attr]
        flagged_values = {
            a.value
            for a in kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        }
        assert flagged_values == {"Ava", "Aida"}

    def test_extending_open_contradiction_does_not_resurrect_superseded_member(
        self, make_kb: KbFactory
    ) -> None:
        """The same resurrection bug test_extending_open_contradiction_
        does_not_resurrect_retracted_member pins for `retracted`, but for
        `superseded` (ADR-0031) - found in review to need its own fix
        (`_apply_with_conflict_routing`'s extension branch only ever
        skipped `retracted`) and its own vector (mutation-testing found
        the code fix alone left the suite green)."""
        kb = _kb(make_kb)
        kb.create_principal(
            "admin@example.com", kind="human", auth_method="oidc", default_capability="admin"
        )
        # v1: employer is time_varying, so Acme is *superseded* (not
        # flagged) when Globex is asserted.
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
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        acme = kb.assert_literal(entity.id, "Person.employer", "Acme", "Text", HUMAN_WRITE)
        globex = kb.assert_literal(entity.id, "Person.employer", "Globex", "Text", HUMAN_WRITE)
        assert kb.backend.get_assertion(acme.id).status == "superseded"  # type: ignore[union-attr]

        # flag_contradiction() accepts a superseded assertion as a member
        # by design - opens a contradiction without re-flagging Acme.
        contradiction, _action = kb.flag_contradiction(acme.id, globex.id, REVIEWER)
        assert kb.backend.get_assertion(acme.id).status == "superseded"  # type: ignore[union-attr]

        # v2: employer is redeclared static, so a third value now extends
        # the still-open contradiction via conflict-routing, not
        # supersession.
        kb.apply_schema(
            SchemaIR(
                namespace="default",
                version=2,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={
                            "employer": PropertyDef(name="employer", value_type="Text"),
                        },
                    ),
                },
            ),
            author="admin@example.com",
        )
        kb.propose(entity.id, "Person.employer", "Initech", "Text", REVIEWER)

        assert kb.backend.get_assertion(acme.id).status == "superseded"  # type: ignore[union-attr]
        flagged_values = {
            a.value
            for a in kb.assertions(subject=entity.id, predicate="Person.employer", status="flagged")
        }
        assert flagged_values == {"Globex", "Initech"}
        contradiction = kb.backend.get_contradiction(contradiction.id)
        assert contradiction is not None
        assert acme.id in contradiction.member_ids

    def test_extending_open_contradiction_records_no_flagged_event_for_retracted_member(
        self, make_kb: KbFactory
    ) -> None:
        """The retracted member's event trail is untouched - no spurious
        'flagged' event is recorded for it just because the contradiction it
        once belonged to was extended."""
        kb = _kb(make_kb)
        kb.create_principal(
            # review capability (KI-043) - see the previous test's comment.
            "dave@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="review",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        _, ada_id, _ = _open_contradiction(kb, entity.id)
        kb.retract(ada_id, "dave@example.com")
        events_before = kb.backend.get_assertion_events(ada_id)

        kb.propose(entity.id, "Person.name", "Aida", "Text", REVIEWER)

        events_after = kb.backend.get_assertion_events(ada_id)
        assert events_after == events_before

    def test_retracted_member_stays_in_contradiction_membership_for_audit(
        self, make_kb: KbFactory
    ) -> None:
        """The retracted assertion's id is not dropped from the
        contradiction's own member_ids - only its status stops changing."""
        kb = _kb(make_kb)
        kb.create_principal(
            # review capability (KI-043) - see the earlier comment above.
            "dave@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="review",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, _ = _open_contradiction(kb, entity.id)
        kb.retract(ada_id, "dave@example.com")

        kb.propose(entity.id, "Person.name", "Aida", "Text", REVIEWER)

        contradiction = kb.backend.get_contradiction(contradiction_id)
        assert contradiction is not None
        assert ada_id in contradiction.member_ids

    def test_flag_contradiction_does_not_resurrect_a_retracted_assertion(
        self, make_kb: KbFactory
    ) -> None:
        """flag_contradiction() has its own, separate flagging loop from
        conflict-routing's extend-branch above - found in review to have
        the identical resurrection bug, and it's reachable at only
        `propose` capability (including by an AI principal via MCP's
        ontolith.flag_contradiction, which carries no write capability at
        all)."""
        kb = _kb(make_kb)
        kb.create_principal(
            # review capability (KI-043) - see the earlier comment above.
            "dave@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="review",
        )
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a = kb.assert_literal(entity.id, "Person.born", "1815", "Text", HUMAN_WRITE)
        b = kb.assert_literal(entity.id, "Person.born", "1816", "Text", HUMAN_WRITE)
        kb.retract(a.id, "dave@example.com")
        assert kb.backend.get_assertion(a.id).status == "retracted"  # type: ignore[union-attr]

        kb.flag_contradiction(a.id, b.id, NON_REVIEWER)

        assert kb.backend.get_assertion(a.id).status == "retracted"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(b.id).status == "flagged"  # type: ignore[union-attr]


# ===========================================================================
# flag_contradiction() requires an eligible winner among a NEW
# contradiction's founding members (KI-050)
# ===========================================================================


class TestFlagContradictionRequiresEligibleWinner:
    """A brand-new contradiction must start with at least one member
    resolve_contradiction() could actually select as winner
    (retracted/superseded are terminal, KI-044/ADR-0031) - otherwise it
    opens unresolvable until some later write extends it. Extending an
    *already-open* contradiction with an all-terminal pair remains
    permitted (ADR-0031's own deliberate escape hatch)."""

    def _kb_with_non_overlapping_windows(
        self, make_kb: KbFactory
    ) -> tuple[Ontology, str, str, str]:
        """Two assertions on the same predicate with non-overlapping
        validity windows - route() coexists them via Activate (SPEC
        §10.1/§10.2), not Contradict/Supersede, so neither is auto-flagged
        into an open contradiction the way two *overlapping-window* values
        for the same predicate would be. `_kb()` applies no schema, so
        `Person.employer` resolves to the `static` default (not
        `time_varying`) - but `_route_static` applies the identical
        `_windows_overlap` filter `_route_time_varying` does
        (`govern/conflict.py`), so this holds regardless of temporality;
        the non-overlapping windows are what matters, not the predicate's
        declared temporality. Needed so flag_contradiction() below
        genuinely reaches its "create a NEW contradiction" branch instead
        of extending one conflict-routing already opened.
        Returns (kb, entity_id, first_id, second_id)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        first = kb.assert_literal(
            entity.id, "Person.employer", "Acme", "Text", HUMAN_WRITE, valid_from=T0, valid_to=T1
        )
        second = kb.assert_literal(
            entity.id, "Person.employer", "Globex", "Text", HUMAN_WRITE, valid_from=T1
        )
        assert kb.backend.get_assertion(first.id).status == "active"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(second.id).status == "active"  # type: ignore[union-attr]
        assert kb.backend.get_open_contradiction("default", entity.id, "Person.employer") is None
        return kb, entity.id, first.id, second.id

    def test_both_founding_members_retracted_raises(self, make_kb: KbFactory) -> None:
        kb, entity_id, a_id, b_id = self._kb_with_non_overlapping_windows(make_kb)
        kb.retract(a_id, REVIEWER)
        kb.retract(b_id, REVIEWER)

        with pytest.raises(ValidationError, match="already terminal"):
            kb.flag_contradiction(a_id, b_id, NON_REVIEWER)

        assert kb.backend.get_open_contradiction("default", entity_id, "Person.employer") is None

    def test_rejected_creation_does_not_consume_an_id(self, make_kb: KbFactory) -> None:
        """The check is placed before id_provider.next() specifically so a
        rejected call doesn't consume an ID for a contradiction that's
        never persisted (ADR-0035) - IdProvider isn't transactional, so a
        consumed ID would not roll back with the rest of the write. `_kb()`
        seeds a FixedIdProvider cycling "id-0", "id-1", ... in strict
        order (shared across every id_provider.next() call in the
        session) - immediately-adjacent indices before/after the rejected
        call prove nothing was consumed in between."""
        kb, _entity_id, a_id, b_id = self._kb_with_non_overlapping_windows(make_kb)
        kb.retract(a_id, REVIEWER)
        kb.retract(b_id, REVIEWER)
        before = int(kb.id_provider.next().removeprefix("id-"))

        with pytest.raises(ValidationError, match="already terminal"):
            kb.flag_contradiction(a_id, b_id, NON_REVIEWER)

        after = int(kb.id_provider.next().removeprefix("id-"))
        assert after == before + 1

    def test_one_retracted_one_superseded_raises(self, make_kb: KbFactory) -> None:
        """Both terminal kinds count, not just retracted - KI-050's own
        motivating example is exactly this pairing. Both assertions target
        the same time_varying predicate so this exercises the eligible-
        winner check itself, not the separate shared-subject-and-predicate
        check."""
        kb = _kb(make_kb)
        admin = "admin@example.com"
        kb.create_principal(admin, kind="human", auth_method="oidc", default_capability="admin")
        schema = SchemaIR(
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
        )
        kb.apply_schema(schema, author=admin)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        acme = kb.assert_literal(entity.id, "Person.employer", "Acme", "Text", HUMAN_WRITE)
        globex = kb.assert_literal(entity.id, "Person.employer", "Globex", "Text", HUMAN_WRITE)
        kb.retract(globex.id, REVIEWER)
        assert kb.backend.get_assertion(acme.id).status == "superseded"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(globex.id).status == "retracted"  # type: ignore[union-attr]

        with pytest.raises(ValidationError, match="already terminal"):
            kb.flag_contradiction(acme.id, globex.id, NON_REVIEWER)

    def test_one_terminal_one_active_still_succeeds(self, make_kb: KbFactory) -> None:
        """Regression guard: the new check must not reject the ordinary
        one-terminal case flag_contradiction() has always supported
        (test_flag_contradiction_does_not_resurrect_a_retracted_assertion
        above already covers the resurrection half of this; this pins the
        "still creates successfully" half explicitly)."""
        kb, _entity_id, a_id, b_id = self._kb_with_non_overlapping_windows(make_kb)
        kb.retract(a_id, REVIEWER)

        contradiction, action = kb.flag_contradiction(a_id, b_id, NON_REVIEWER)

        assert action == "created"
        assert contradiction.state == "open"

    def test_extending_open_contradiction_with_all_terminal_pair_still_succeeds(
        self, make_kb: KbFactory
    ) -> None:
        """The eligible-winner check only guards creating a NEW
        contradiction - extending an already-open one with two terminal
        ids remains permitted (ADR-0031's escape hatch for naming a
        terminal assertion for audit/context). Re-naming the open
        contradiction's own two (now-terminal) members is enough to reach
        the "extend" branch - no third assertion needed."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        contradiction_id, ada_id, ava_id = _open_contradiction(kb, entity.id)
        kb.retract(ada_id, REVIEWER)
        kb.retract(ava_id, REVIEWER)

        contradiction, action = kb.flag_contradiction(ada_id, ava_id, NON_REVIEWER)

        assert action == "extended"
        assert contradiction.id == contradiction_id
        assert set(contradiction.member_ids) == {ada_id, ava_id}


# ===========================================================================
# flag_contradiction() TOCTOU (KI-045)
# ===========================================================================


class TestFlagContradictionTOCTOU:
    """flag_contradiction() reads both target assertions and any existing
    open contradiction for their (subject, predicate) - all before opening
    its write transaction. `_RacingClock` simulates a genuine concurrent
    actor deterministically (same technique as KI-035's
    TestProposalTransitionTOCTOU in conformance/test_review_workflow.py):
    it fires a one-shot side effect the moment `.now()` is called, right
    where the fresh, in-transaction reads (KI-045's fix) now happen instead
    of before them. The method under test must act on what its own fresh
    read sees, not a stale pre-transaction snapshot."""

    def test_flag_contradiction_loses_race_to_concurrent_retract(self, make_kb: KbFactory) -> None:
        """A concurrent retract() landing in the gap must not be resurrected
        back to `flagged` by this call's own terminal-status guard (KI-034)
        - that guard only works if it's checking a fresh read."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        # Same target value on both refs -> corroboration, not conflict, so
        # both start out `active` and independent of each other (matches
        # test_explicit_flag_records_flagging_principal's own setup above).
        a = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE, source="src-a")
        b = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE, source="src-b")

        def racer() -> None:
            kb.retract(a.id, HUMAN_WRITE)

        kb.clock = _RacingClock(T0, racer)
        contradiction, action = kb.flag_contradiction(a.id, b.id, REVIEWER)

        assert action == "created"
        assert a.id in contradiction.member_ids
        assert b.id in contradiction.member_ids
        # a was retracted by the racer before this call's own read of it -
        # must not have been resurrected to flagged.
        assert kb.backend.get_assertion(a.id).status == "retracted"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(b.id).status == "flagged"  # type: ignore[union-attr]

    def test_flag_contradiction_loses_race_to_concurrent_resolve(self, make_kb: KbFactory) -> None:
        """A concurrent resolve_contradiction() closing the only existing
        open contradiction for this (subject, predicate) in the gap must
        not be extended by this call - its own fresh read must see it's no
        longer open and start a new one instead.

        Uses two non-member assertions (c, d) rather than reusing the
        original contradiction's own members - found in review to matter:
        reusing existing members makes `merged = dedup(existing.member_ids
        + [a, b])` a no-op regardless of whether the fix works, so the
        member_ids assertion alone can't tell buggy from fixed. c/d
        genuinely joining (or not) member_ids is what's discriminating."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        # Same target on every ref -> corroboration, not conflict (matches
        # test_explicit_flag_records_flagging_principal's own pattern) -
        # a/b/c/d all start out independently `active`.
        a = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        b = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        c = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        d = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        original, _action = kb.flag_contradiction(a.id, b.id, REVIEWER)

        def racer() -> None:
            kb.resolve_contradiction(original.id, a.id, REVIEWER)

        kb.clock = _RacingClock(T0, racer)
        contradiction, action = kb.flag_contradiction(c.id, d.id, REVIEWER)

        # The racer's resolution stands, genuinely untouched by this call -
        # still resolved, member_ids still just [a, b].
        resolved = kb.backend.get_contradiction(original.id)
        assert resolved is not None
        assert resolved.state == "resolved"
        assert resolved.member_ids == [a.id, b.id]

        # c/d must not have joined the now-resolved contradiction - this
        # call's own fresh read of "is there an open contradiction here"
        # correctly saw none, so it started a new one instead.
        assert action == "created"
        assert contradiction.id != original.id
        assert contradiction.state == "open"
        assert contradiction.member_ids == [c.id, d.id]
        assert kb.backend.get_assertion(c.id).status == "flagged"  # type: ignore[union-attr]
        assert kb.backend.get_assertion(d.id).status == "flagged"  # type: ignore[union-attr]

    def test_flag_contradiction_loses_race_to_concurrent_flag(self, make_kb: KbFactory) -> None:
        """A concurrent flag_contradiction() opening a *new* contradiction
        for the same (subject, predicate) in the gap must not be missed -
        this call's own fresh existing-contradiction read must see it and
        extend it, not open a second, independently-open contradiction for
        the same (subject, predicate) (found in review: the fix already
        handles this correctly, it just wasn't pinned by a vector)."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        b = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        c = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        d = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)

        def racer() -> None:
            kb.flag_contradiction(c.id, d.id, REVIEWER)

        kb.clock = _RacingClock(T0, racer)
        contradiction, action = kb.flag_contradiction(a.id, b.id, REVIEWER)

        # Exactly one open contradiction for (entity.id, "Person.employer")
        # must exist - this call's fresh read must have found the racer's
        # and extended it, not raced it into a second, independently-open
        # one that get_open_contradiction() can never see both halves of.
        assert action == "extended"
        open_contradiction = kb.backend.get_open_contradiction(
            "default", entity.id, "Person.employer"
        )
        assert open_contradiction is not None
        assert open_contradiction.id == contradiction.id
        assert set(contradiction.member_ids) == {a.id, b.id, c.id, d.id}

    def test_flag_contradiction_loses_race_to_concurrent_double_retract(
        self, make_kb: KbFactory
    ) -> None:
        """KI-050: a concurrent retract() of BOTH named assertions in the
        gap must be caught by the eligible-winner check reading this
        call's own fresh, in-transaction status - not a stale
        pre-transaction snapshot that would have seen them still active
        and let a brand-new, zero-eligible-winner contradiction through."""
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)
        b = kb.assert_ref(entity.id, "Person.employer", entity.id, HUMAN_WRITE)

        def racer() -> None:
            kb.retract(a.id, HUMAN_WRITE)
            kb.retract(b.id, HUMAN_WRITE)

        kb.clock = _RacingClock(T0, racer)
        with pytest.raises(ValidationError, match="already terminal"):
            kb.flag_contradiction(a.id, b.id, REVIEWER)

        assert kb.backend.get_open_contradiction("default", entity.id, "Person.employer") is None


class TestFlagContradictionRationaleAccumulation:
    """KI-071: `rationale` must accumulate identically on both backends -
    the SDK-level unit tests (test_ontology.py) only exercise this
    against SQLite (the unit-test suite's default backend). Both
    backends' own `update_contradiction_members` metadata branch already
    has direct unit coverage (test_sqlite_backend.py/test_duckdb_
    backend.py) - this conformance vector's own job is proving the
    *composed* `Ontology.flag_contradiction()` -> backend path, not just
    the backend method in isolation, is identical end to end, and
    guarding against future divergence between the two."""

    def _conflicting_assertions(self, kb: Ontology, entity_id: str) -> tuple[str, str]:
        """Two active assertions on the same (subject, predicate) with
        different values, inserted directly (bypassing conflict routing)
        so they don't already form a contradiction before
        flag_contradiction() is called - mirrors
        test_ontology.py::TestFlagContradiction's own identical helper."""
        a = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity_id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author=HUMAN_WRITE,
            asserted_at=kb.clock.now(),
            status="active",
        )
        b = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity_id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ava",
            author=HUMAN_WRITE,
            asserted_at=kb.clock.now(),
            status="active",
        )
        kb.backend.put_assertion(a)
        kb.backend.put_assertion(b)
        return a.id, b.id

    def test_rationale_accumulates_across_create_and_extend(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a_id, b_id = self._conflicting_assertions(kb, entity.id)

        created, action = kb.flag_contradiction(
            a_id, b_id, HUMAN_WRITE, rationale="Sources disagree"
        )
        assert action == "created"

        # `_apply_with_conflict_routing`'s own static-fast-path auto-sweeps
        # `c` into the now-open contradiction right here (any subsequent
        # same-predicate write does, once one is open) - so by the time
        # flag_contradiction() below runs, `c` is already a member and
        # membership itself doesn't change. What this test actually
        # verifies is that the *explicit* call still takes the "extend"
        # branch and appends its own rationale entry, on top of whatever
        # routing's own 2-arg update_contradiction_members call already
        # did (which must not have blanked entry #1 - asserted below too).
        c = kb.assert_literal(entity.id, "Person.name", "Eve", "Text", HUMAN_WRITE)
        assert kb.backend.get_contradiction(created.id).metadata["rationale_history"] == [  # type: ignore[union-attr]
            {"rationale": "Sources disagree", "actor": HUMAN_WRITE, "at": T0.isoformat()}
        ]

        extended, action = kb.flag_contradiction(
            a_id, c.id, REVIEWER, rationale="A third source also disagrees"
        )

        assert action == "extended"
        assert extended.id == created.id
        assert extended.metadata["rationale_history"] == [
            {"rationale": "Sources disagree", "actor": HUMAN_WRITE, "at": T0.isoformat()},
            {"rationale": "A third source also disagrees", "actor": REVIEWER, "at": T0.isoformat()},
        ]

    def test_extend_without_rationale_leaves_history_untouched(self, make_kb: KbFactory) -> None:
        kb = _kb(make_kb)
        entity = kb.create_entity("Person", author=HUMAN_WRITE)
        a_id, b_id = self._conflicting_assertions(kb, entity.id)
        kb.flag_contradiction(a_id, b_id, HUMAN_WRITE, rationale="Sources disagree")

        c = kb.assert_literal(entity.id, "Person.name", "Eve", "Text", HUMAN_WRITE)
        extended, _ = kb.flag_contradiction(a_id, c.id, REVIEWER)

        assert extended.metadata["rationale_history"] == [
            {"rationale": "Sources disagree", "actor": HUMAN_WRITE, "at": T0.isoformat()}
        ]
