"""Conformance vectors: relation target concept check (SPEC §6).

A relation declared `Ref["Organization"]` must only accept an entity of that
concept as its target. KI-089 added the existence check; these vectors cover
the concept check on every write path that takes a caller-supplied `target`:
`assert_ref`, `propose_ref` and proposal replay.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, SequentialIdProvider
from ontolith.core.errors import ValidationError
from ontolith.govern import Decision, RequireReview
from ontolith.schema import ConceptDef, RelationDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)

ADMIN = "admin@example.com"
WRITER = "alice@example.com"
REVIEWER = "bob@example.com"


class _AlwaysReview:
    """PolicyStrategy test double routing every proposal to review, so a
    proposal stays pending until the test accepts it explicitly."""

    def evaluate(
        self, proposal: object, principal: object, kb: object, acting_as: object = None
    ) -> Decision:
        return RequireReview([REVIEWER], "test double: always review")


def _schema(employer_target: str, version: int = 1) -> SchemaIR:
    return SchemaIR(
        namespace="default",
        version=version,
        concepts={
            "Person": ConceptDef(
                name="Person",
                relations={
                    "employer": RelationDef(name="employer", target_concept=employer_target)
                },
            ),
            "Organization": ConceptDef(name="Organization"),
            "Invoice": ConceptDef(name="Invoice"),
        },
    )


def _kb(make_kb: KbFactory, *, policy: object = None) -> Ontology:
    kb = make_kb(
        FixedClock(T0),
        SequentialIdProvider(prefix="e"),
        policy=policy,  # type: ignore[arg-type]
    )
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
    kb.create_principal(WRITER, kind="human", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", default_capability="review")
    kb.apply_schema(_schema("Organization"), author=ADMIN)
    return kb


def test_assert_ref_rejects_wrong_target_concept(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    person = kb.create_entity("Person", author=WRITER)
    invoice = kb.create_entity("Invoice", author=WRITER)

    with pytest.raises(ValidationError, match=r"Person\.employer.*Organization.*Invoice"):
        kb.assert_ref(person.id, "Person.employer", invoice.id, WRITER)

    assert kb.assertions(subject=person.id, predicate="Person.employer") == []


def test_assert_ref_accepts_declared_target_concept(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    person = kb.create_entity("Person", author=WRITER)
    org = kb.create_entity("Organization", author=WRITER)

    assertion = kb.assert_ref(person.id, "Person.employer", org.id, WRITER)

    assert assertion.status == "active"
    assert assertion.value == org.id


def test_propose_ref_rejects_wrong_target_concept(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    person = kb.create_entity("Person", author=WRITER)
    invoice = kb.create_entity("Invoice", author=WRITER)

    with pytest.raises(ValidationError, match=r"Person\.employer.*Organization.*Invoice"):
        kb.propose_ref(person.id, "Person.employer", invoice.id, WRITER)

    assert kb.assertions(subject=person.id, predicate="Person.employer") == []


def test_unknown_target_still_raises_not_found_first(make_kb: KbFactory) -> None:
    """KI-089's existence check keeps priority: a missing target is a
    NotFoundError, not a concept mismatch."""
    from ontolith.core.errors import NotFoundError

    kb = _kb(make_kb)
    person = kb.create_entity("Person", author=WRITER)

    with pytest.raises(NotFoundError, match="Target not found"):
        kb.assert_ref(person.id, "Person.employer", "missing", WRITER)


def test_schema_less_namespace_skips_concept_check(make_kb: KbFactory) -> None:
    """With no schema applied there is nothing to check against, matching
    every other schema-driven write check."""
    kb = make_kb(FixedClock(T0), SequentialIdProvider(prefix="e"))
    kb.create_principal(WRITER, kind="human", default_capability="write")
    person = kb.create_entity("Person", author=WRITER)
    invoice = kb.create_entity("Invoice", author=WRITER)

    assertion = kb.assert_ref(person.id, "Person.employer", invoice.id, WRITER)

    assert assertion.status == "active"


def test_replay_rechecks_target_concept(make_kb: KbFactory) -> None:
    """A proposal staged under one schema version is re-checked at accept
    time: if the schema has since moved the relation to a different target
    concept, replaying the stale ref is rejected instead of landing."""
    kb = _kb(make_kb, policy=_AlwaysReview())
    person = kb.create_entity("Person", author=WRITER)
    org = kb.create_entity("Organization", author=WRITER)
    proposal, _ = kb.propose_ref(person.id, "Person.employer", org.id, WRITER)
    assert proposal.state == "require_review"

    kb.apply_schema(_schema("Invoice", version=2), author=ADMIN)

    with pytest.raises(ValidationError, match=r"Person\.employer.*Invoice.*Organization"):
        kb.accept_proposal(proposal.id, REVIEWER)

    assert kb.assertions(subject=person.id, predicate="Person.employer") == []
