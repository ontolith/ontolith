"""Conformance vectors: multi-operation proposals (SPEC §9.5, ADR-0055).

`propose_batch` stages several operations as ONE proposal that is accepted
or rejected atomically. New entities are named by local keys and get real
ids at accept time. The configured policy is evaluated once per operation
and the strictest outcome wins, so a later operation can never ride a
first operation's decision past review.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from conformance.conftest import KbFactory
from ontolith import Ontology
from ontolith.core import FixedClock, SequentialIdProvider
from ontolith.core.errors import NotFoundError, ValidationError
from ontolith.govern import (
    AssertLiteralOp,
    AssertRefOp,
    AutoAccept,
    CreateEntityOp,
    LocalRef,
    RequireReview,
)
from ontolith.govern.policy import (
    Composite,
    ConfidenceThreshold,
    Decision,
    PolicyStrategy,
    Reject,
    RequireReviewByRole,
    RequireReviewForAI,
    SourceQuorum,
    SourceRequired,
    ThresholdPolicy,
)
from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal
from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)

HUMAN = "alice@example.com"
OTHER_HUMAN = "carl@example.com"
AI = "agent"
REVIEWER = "bob@example.com"
ADMIN = "admin@example.com"


def _schema() -> SchemaIR:
    return SchemaIR(
        namespace="default",
        version=1,
        concepts={
            "Person": ConceptDef(
                name="Person",
                properties={"name": PropertyDef(name="name", value_type="Text")},
            ),
            "Organization": ConceptDef(
                name="Organization",
                properties={"name": PropertyDef(name="name", value_type="Text")},
            ),
            "Decision": ConceptDef(
                name="Decision",
                properties={
                    "title": PropertyDef(name="title", value_type="Text"),
                    "deadline": PropertyDef(name="deadline", value_type="Text"),
                },
                relations={
                    "owner": RelationDef(name="owner", target_concept="Person"),
                    "org": RelationDef(name="org", target_concept="Organization"),
                },
            ),
        },
    )


def _kb(
    make_kb: KbFactory,
    *,
    policy: PolicyStrategy | None = None,
    ids: SequentialIdProvider | None = None,
) -> Ontology:
    kb = make_kb(FixedClock(T0), ids, policy=policy)
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
    kb.apply_schema(_schema(), author=ADMIN)
    kb.create_principal(HUMAN, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(OTHER_HUMAN, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    kb.create_principal(
        AI, kind="ai", auth_method="workload", owner=HUMAN, default_capability="propose"
    )
    return kb


def _decision_batch(*, owner: str, title: str = "Ship it", source: str | None = "mtg") -> list[Any]:
    return [
        CreateEntityOp(key="d1", concept="Decision"),
        AssertLiteralOp(
            subject=LocalRef("d1"),
            predicate="Decision.title",
            value=title,
            value_type="Text",
            source=source,
        ),
        AssertRefOp(
            subject=LocalRef("d1"), predicate="Decision.owner", target=owner, source=source
        ),
    ]


def _entities(kb: Ontology, concept: str) -> list[str]:
    return [e.id for e in kb.backend.entities(concept=concept)]


# --- submit + accept ---------------------------------------------------------


def test_human_batch_auto_accepts_and_applies_every_operation(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    owner = kb.create_entity("Person", author=HUMAN).id

    proposal, decision = kb.propose_batch(_decision_batch(owner=owner), HUMAN)

    assert isinstance(decision, AutoAccept)
    assert proposal.state == "auto_accepted"
    [decision_id] = _entities(kb, "Decision")
    assert proposal.metadata["created_entities"] == {"d1": decision_id}
    facts = {
        (a.predicate, a.value) for a in kb.backend.assertions(subject=decision_id, status=None)
    }
    assert facts == {("Decision.title", "Ship it"), ("Decision.owner", owner)}
    for a in kb.backend.assertions(subject=decision_id, status=None):
        assert a.proposal_id == proposal.id


def test_ai_batch_goes_to_review_and_creates_nothing_until_accepted(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    owner = kb.create_entity("Person", author=HUMAN).id

    proposal, decision = kb.propose_batch(_decision_batch(owner=owner), AI, model="m-1")

    assert isinstance(decision, RequireReview)
    assert proposal.state == "require_review"
    assert _entities(kb, "Decision") == []

    kb.accept_proposal(proposal.id, REVIEWER)

    [decision_id] = _entities(kb, "Decision")
    stored = kb.backend.get_proposal(proposal.id)
    assert stored is not None and stored.state == "accepted"
    assert {a.predicate for a in kb.backend.assertions(subject=decision_id, status=None)} == {
        "Decision.title",
        "Decision.owner",
    }
    events = kb.backend.get_proposal_events(proposal.id)
    assert '"d1"' in (events[-1].detail or "")
    assert decision_id in (events[-1].detail or "")


def test_rejecting_a_batch_leaves_no_entity_and_allocates_no_entity_id(
    make_kb: KbFactory,
) -> None:
    ids = SequentialIdProvider(prefix="x")
    kb = _kb(make_kb, ids=ids)
    owner = kb.create_entity("Person", author=HUMAN).id
    proposal, _ = kb.propose_batch(_decision_batch(owner=owner), AI, model="m-1")
    mark = int(ids.next().split("-")[-1])

    kb.reject_proposal(proposal.id, REVIEWER, "no")

    assert _entities(kb, "Decision") == []
    # Only the reject event drew an id (mark, then the event, then this one).
    assert int(ids.next().split("-")[-1]) == mark + 2


def test_submission_allocates_no_entity_ids(make_kb: KbFactory) -> None:
    """Entity ids come from the IdProvider at accept time only: a batch
    sitting in review has reserved none."""
    ids = SequentialIdProvider(prefix="x")
    kb = _kb(make_kb, ids=ids)
    owner = kb.create_entity("Person", author=HUMAN).id
    mark = int(ids.next().split("-")[-1])

    kb.propose_batch(_decision_batch(owner=owner), AI, model="m-1")

    # Exactly one id (the proposal's own) was drawn by submission.
    assert int(ids.next().split("-")[-1]) == mark + 2


def test_ref_to_a_local_key_chains_between_new_entities(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    ops: list[Any] = [
        CreateEntityOp(key="p", concept="Person"),
        CreateEntityOp(key="d", concept="Decision"),
        AssertRefOp(subject=LocalRef("d"), predicate="Decision.owner", target=LocalRef("p")),
    ]

    proposal, _ = kb.propose_batch(ops, HUMAN)

    created = proposal.metadata["created_entities"]
    [ref] = kb.backend.assertions(subject=created["d"], status=None)
    assert ref.value_kind == "ref" and ref.value == created["p"]


def test_natural_key_is_applied_to_the_created_entity(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)

    proposal, _ = kb.propose_batch(
        [CreateEntityOp(key="p", concept="Person", natural_key="ada")], HUMAN
    )

    entity = kb.backend.get_entity_by_natural_key("default", "Person", "ada")
    assert entity is not None
    assert entity.id == proposal.metadata["created_entities"]["p"]
    assert entity.created_by == HUMAN


# --- validation --------------------------------------------------------------


def test_empty_batch_is_rejected(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    with pytest.raises(ValidationError, match="at least one"):
        kb.propose_batch([], HUMAN)


def test_batch_is_capped_at_one_hundred_operations(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    ops = [CreateEntityOp(key=f"k{i}", concept="Person") for i in range(101)]
    with pytest.raises(ValidationError, match="100"):
        kb.propose_batch(ops, HUMAN)
    ok = [CreateEntityOp(key=f"k{i}", concept="Person") for i in range(100)]
    proposal, decision = kb.propose_batch(ok, HUMAN)
    assert isinstance(decision, AutoAccept)
    assert len(proposal.metadata["created_entities"]) == 100


def test_forward_reference_is_rejected(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    ops: list[Any] = [
        AssertLiteralOp(
            subject=LocalRef("d1"), predicate="Decision.title", value="t", value_type="Text"
        ),
        CreateEntityOp(key="d1", concept="Decision"),
    ]
    with pytest.raises(ValidationError, match="d1"):
        kb.propose_batch(ops, HUMAN)


def test_unknown_local_key_is_rejected(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    ops: list[Any] = [
        AssertLiteralOp(
            subject=LocalRef("nope"), predicate="Decision.title", value="t", value_type="Text"
        )
    ]
    with pytest.raises(ValidationError, match="nope"):
        kb.propose_batch(ops, HUMAN)


def test_duplicate_key_is_rejected(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    ops = [CreateEntityOp(key="k", concept="Person"), CreateEntityOp(key="k", concept="Person")]
    with pytest.raises(ValidationError, match="Duplicate"):
        kb.propose_batch(ops, HUMAN)


def test_local_ref_target_concept_mismatch_is_rejected(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    ops: list[Any] = [
        CreateEntityOp(key="o", concept="Organization"),
        CreateEntityOp(key="d", concept="Decision"),
        AssertRefOp(subject=LocalRef("d"), predicate="Decision.owner", target=LocalRef("o")),
    ]
    with pytest.raises(ValidationError, match="Person"):
        kb.propose_batch(ops, HUMAN)


def test_real_target_concept_mismatch_is_rejected(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    org = kb.create_entity("Organization", author=HUMAN).id
    ops: list[Any] = [
        CreateEntityOp(key="d", concept="Decision"),
        AssertRefOp(subject=LocalRef("d"), predicate="Decision.owner", target=org),
    ]
    with pytest.raises(ValidationError, match="Person"):
        kb.propose_batch(ops, HUMAN)


def test_unknown_concept_and_predicate_are_rejected(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    with pytest.raises(ValidationError, match="Nope"):
        kb.propose_batch([CreateEntityOp(key="k", concept="Nope")], HUMAN)
    ops: list[Any] = [
        CreateEntityOp(key="d", concept="Decision"),
        AssertLiteralOp(
            subject=LocalRef("d"), predicate="Decision.zzz", value="t", value_type="Text"
        ),
    ]
    with pytest.raises(ValidationError, match="Decision.zzz"):
        kb.propose_batch(ops, HUMAN)


def test_unknown_real_subject_is_rejected(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    ops = [AssertLiteralOp(subject="ghost", predicate="Person.name", value="x", value_type="Text")]
    with pytest.raises(NotFoundError):
        kb.propose_batch(ops, HUMAN)


def test_natural_key_collisions_are_rejected(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    kb.create_entity("Person", author=HUMAN, natural_key="ada")
    with pytest.raises(ValidationError, match="ada"):
        kb.propose_batch([CreateEntityOp(key="p", concept="Person", natural_key="ada")], HUMAN)
    twice = [
        CreateEntityOp(key="a", concept="Person", natural_key="bob"),
        CreateEntityOp(key="b", concept="Person", natural_key="bob"),
    ]
    with pytest.raises(ValidationError, match="bob"):
        kb.propose_batch(twice, HUMAN)


def test_natural_key_taken_between_submit_and_accept_fails_the_whole_accept(
    make_kb: KbFactory,
) -> None:
    kb = _kb(make_kb)
    owner = kb.create_entity("Person", author=HUMAN).id
    ops: list[Any] = [
        CreateEntityOp(key="d", concept="Decision", natural_key="dec-1"),
        AssertRefOp(subject=LocalRef("d"), predicate="Decision.owner", target=owner),
    ]
    proposal, _ = kb.propose_batch(ops, AI, model="m-1")
    kb.create_entity("Decision", author=HUMAN, natural_key="dec-1")

    with pytest.raises(ValidationError, match="dec-1"):
        kb.accept_proposal(proposal.id, REVIEWER)

    assert len(_entities(kb, "Decision")) == 1  # only the directly-created one
    stored = kb.backend.get_proposal(proposal.id)
    assert stored is not None and stored.state == "require_review"


def test_ai_batch_requires_a_model(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    with pytest.raises(ValidationError, match="model"):
        kb.propose_batch([CreateEntityOp(key="k", concept="Person")], AI)


def test_read_only_author_is_rejected_by_policy(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    kb.create_principal("viewer", kind="human", auth_method="oidc", default_capability="read")
    proposal, decision = kb.propose_batch([CreateEntityOp(key="k", concept="Person")], "viewer")
    assert isinstance(decision, Reject)
    assert proposal.state == "rejected"
    assert _entities(kb, "Person") == []


# --- atomicity ---------------------------------------------------------------


def test_a_failing_operation_rolls_back_every_operation_and_entity(make_kb: KbFactory) -> None:
    """A registered Validator rejecting the LAST operation must undo the
    entity and the earlier assertions (one transaction per acceptance)."""
    from ontolith.core import Assertion

    class RejectOwner:
        def validate(self, assertion: Assertion, kb: Ontology) -> list[str]:
            return ["no owners today"] if assertion.predicate == "Decision.owner" else []

    kb = _kb(make_kb)
    kb.validators = [RejectOwner()]  # type: ignore[list-item]
    owner = kb.create_entity("Person", author=HUMAN).id
    before = {a.id for a in kb.backend.assertions(subject=owner, status=None)}

    with pytest.raises(ValidationError, match="no owners today"):
        kb.propose_batch(_decision_batch(owner=owner), HUMAN)

    assert _entities(kb, "Decision") == []
    assert {a.id for a in kb.backend.assertions(subject=owner, status=None)} == before
    assert kb.backend.proposals() == []


# --- pipeline ----------------------------------------------------------------


def test_repeat_inside_a_batch_is_absorbed_like_any_other(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    subject = kb.create_entity("Person", author=HUMAN).id
    op = AssertLiteralOp(
        subject=subject, predicate="Person.name", value="Ada", value_type="Text", source="s"
    )

    kb.propose_batch([op, op], HUMAN)

    assert len(kb.backend.assertions(subject=subject, status=None)) == 1


def test_conflict_routing_applies_to_batched_assertions(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    subject = kb.create_entity("Person", author=HUMAN).id
    kb.assert_literal(subject, "Person.name", "Ada", "Text", HUMAN)

    kb.propose_batch(
        [
            AssertLiteralOp(
                subject=subject,
                predicate="Person.name",
                value="Grace",
                value_type="Text",
                source="x",
            )
        ],
        OTHER_HUMAN,
    )

    assert kb.backend.get_open_contradiction("default", subject, "Person.name") is not None


# --- policy fan-out ----------------------------------------------------------


class _ByKind:
    """Test policy: outcome depends only on the single op it is shown."""

    def __init__(self, table: dict[str, Decision]) -> None:
        self.table = table
        self.seen: list[int] = []

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: Any,
        acting_as: Principal | None = None,
    ) -> Decision:
        ops = proposal.payload["operations"]
        self.seen.append(len(ops))
        return self.table[ops[0]["kind"]]


def _mixed_ops(subject: str) -> list[Any]:
    return [
        AssertLiteralOp(subject=subject, predicate="Person.name", value="A", value_type="Text"),
        AssertRefOp(subject=LocalRef("d"), predicate="Decision.owner", target=subject),
    ]


def _with_decision(subject: str) -> list[Any]:
    return [CreateEntityOp(key="d", concept="Decision"), *_mixed_ops(subject)]


def test_policy_is_evaluated_once_per_operation_against_a_single_op_view(
    make_kb: KbFactory,
) -> None:
    policy = _ByKind(
        {
            "create_entity": AutoAccept("c"),
            "assert_literal": AutoAccept("l"),
            "assert_ref": AutoAccept("r"),
        }
    )
    kb = _kb(make_kb, policy=policy)
    subject = kb.create_entity("Person", author=HUMAN).id

    kb.propose_batch(_with_decision(subject), HUMAN)

    assert policy.seen == [1, 1, 1]


def test_a_later_review_operation_cannot_ride_an_earlier_auto_accept(
    make_kb: KbFactory,
) -> None:
    policy = _ByKind(
        {
            "create_entity": AutoAccept("c"),
            "assert_literal": AutoAccept("l"),
            "assert_ref": RequireReview(["r1"], "ref needs eyes"),
        }
    )
    kb = _kb(make_kb, policy=policy)
    subject = kb.create_entity("Person", author=HUMAN).id

    proposal, decision = kb.propose_batch(_with_decision(subject), HUMAN)

    assert isinstance(decision, RequireReview)
    assert proposal.state == "require_review"
    assert proposal.reviewers == ["r1"]
    assert _entities(kb, "Decision") == []


def test_reject_beats_review_beats_accept_and_reviewers_are_unioned(
    make_kb: KbFactory,
) -> None:
    policy = _ByKind(
        {
            "create_entity": RequireReview(["r1", "r2"], "c"),
            "assert_literal": RequireReview(["r2", "r3"], "l"),
            "assert_ref": Reject("nope"),
        }
    )
    kb = _kb(make_kb, policy=policy)
    subject = kb.create_entity("Person", author=HUMAN).id

    proposal, decision = kb.propose_batch(_with_decision(subject), HUMAN)
    assert isinstance(decision, Reject) and proposal.state == "rejected"
    assert decision.reason == "nope"

    policy.table["assert_ref"] = AutoAccept("r")
    proposal, decision = kb.propose_batch(_with_decision(subject), HUMAN)
    assert isinstance(decision, RequireReview)
    assert decision.reviewers == ["r1", "r2", "r3"]


@pytest.mark.parametrize(
    "policy",
    [
        SourceRequired(reviewers=["r"]),
        SourceQuorum(1, reviewers=["r"]),
        ConfidenceThreshold(0.5, reviewers=["r"]),
        Composite(all=[SourceRequired(reviewers=["r"]), ThresholdPolicy()]),
    ],
)
def test_content_reading_policies_cannot_be_bypassed_by_a_later_operation(
    make_kb: KbFactory, policy: PolicyStrategy
) -> None:
    """Op 1 is the one every shipped content policy used to read: it is fully
    sourced and confident. Op 2 has no source and no confidence."""
    kb = _kb(make_kb, policy=policy)
    subject = kb.create_entity("Person", author=HUMAN).id
    good = AssertLiteralOp(
        subject=subject,
        predicate="Person.name",
        value="A",
        value_type="Text",
        source="s",
        confidence=0.9,
    )
    bad = AssertLiteralOp(  # no source, no confidence
        subject=subject, predicate="Person.name", value="B", value_type="Text"
    )

    _, decision = kb.propose_batch([good, bad], HUMAN)

    assert isinstance(decision, RequireReview)


def test_every_shipped_policy_tolerates_a_create_entity_operation(make_kb: KbFactory) -> None:
    """A create_entity op carries no confidence or source (like retract). No
    shipped content policy auto-accepts it; none raises on it."""
    for policy in (
        SourceRequired(reviewers=["r"]),
        SourceQuorum(1, reviewers=["r"]),
        ConfidenceThreshold(0.5, reviewers=["r"]),
    ):
        kb = _kb(make_kb, policy=policy)
        _, decision = kb.propose_batch([CreateEntityOp(key="k", concept="Person")], HUMAN)
        assert isinstance(decision, RequireReview)
    for policy in (ThresholdPolicy(), RequireReviewForAI()):
        kb = _kb(make_kb, policy=policy)
        _, decision = kb.propose_batch([CreateEntityOp(key="k", concept="Person")], HUMAN)
        assert isinstance(decision, AutoAccept)
    kb = _kb(make_kb, policy=RequireReviewByRole({}, default=["r"]))
    _, decision = kb.propose_batch([CreateEntityOp(key="k", concept="Person")], HUMAN)
    assert isinstance(decision, RequireReview)
    kb = _kb(make_kb, policy=Composite(all=[RequireReviewForAI(), SourceRequired(reviewers=["r"])]))
    _, decision = kb.propose_batch([CreateEntityOp(key="k", concept="Person")], HUMAN)
    assert isinstance(decision, RequireReview)


class _AcceptCreates:
    """Accepts create_entity ops itself and defers every other op to `inner`,
    so the create op does not force a batch to review."""

    def __init__(self, inner: PolicyStrategy) -> None:
        self.inner = inner

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: Any,
        acting_as: Principal | None = None,
    ) -> Decision:
        if proposal.payload["operations"][0]["kind"] == "create_entity":
            return AutoAccept("create ok")
        return self.inner.evaluate(proposal, principal, kb, acting_as)


def test_source_quorum_does_not_count_other_subjects_for_a_local_key(make_kb: KbFactory) -> None:
    """A fact about a not-yet-created entity must not be corroborated by
    other entities' facts with the same predicate and value. The create op
    auto-accepts here, so the quorum decision is what is under test."""
    kb = _kb(make_kb, policy=_AcceptCreates(SourceQuorum(2, reviewers=["r"])))
    for source in ("s1", "s2"):
        other = kb.create_entity("Decision", author=HUMAN).id
        kb.assert_literal(other, "Decision.title", "T", "Text", HUMAN, source=source)
    ops: list[Any] = [
        CreateEntityOp(key="d", concept="Decision"),
        AssertLiteralOp(
            subject=LocalRef("d"),
            predicate="Decision.title",
            value="T",
            value_type="Text",
            source="s3",
        ),
    ]

    _, decision = kb.propose_batch(ops, HUMAN)

    assert isinstance(decision, RequireReview)
    assert "1/2" in decision.reason


def test_resubmit_evaluates_every_operation(make_kb: KbFactory) -> None:
    policy = _ByKind(
        {
            "create_entity": AutoAccept("c"),
            "assert_literal": RequireReview(["r1"], "l"),
            "assert_ref": AutoAccept("r"),
        }
    )
    kb = _kb(make_kb, policy=policy)
    subject = kb.create_entity("Person", author=HUMAN).id
    proposal, _ = kb.propose_batch(_with_decision(subject), HUMAN)
    kb.request_changes(proposal.id, REVIEWER, "again")

    _, decision = kb.resubmit(proposal.id, HUMAN)
    assert isinstance(decision, RequireReview)
    assert _entities(kb, "Decision") == []

    policy.table["assert_literal"] = AutoAccept("l")
    kb.request_changes(proposal.id, REVIEWER, "once more")
    result, decision = kb.resubmit(proposal.id, HUMAN)
    assert isinstance(decision, AutoAccept) and result.state == "auto_accepted"
    assert len(_entities(kb, "Decision")) == 1


def test_unknown_operation_kind_in_a_stored_payload_fails_loudly(make_kb: KbFactory) -> None:
    kb = _kb(make_kb)
    proposal = Proposal(
        id="p-bad",
        namespace="default",
        author=AI,
        state="require_review",
        created_at=T0,
        payload={"operations": [{"kind": "teleport"}]},
    )
    kb.backend.put_proposal(proposal)
    with pytest.raises(ValidationError, match="teleport"):
        kb.accept_proposal("p-bad", REVIEWER)


# --- review findings ---------------------------------------------------------


class _Escalate(Decision):
    """A custom Decision subclass: neither accept, review nor reject."""


class _EscalatingPolicy:
    def evaluate(
        self, proposal: Proposal, principal: Principal, kb: Any, acting_as: Principal | None = None
    ) -> Decision:
        return _Escalate()


def test_an_unrecognized_decision_never_auto_accepts_a_batch(make_kb: KbFactory) -> None:
    for policy in (_EscalatingPolicy(), Composite(all=[_EscalatingPolicy()])):
        kb = _kb(make_kb, policy=policy)
        subject = kb.create_entity("Person", author=HUMAN).id
        ops = [
            AssertLiteralOp(
                subject=subject, predicate="Person.name", value=v, value_type="Text", source=v
            )
            for v in ("a", "b")
        ]
        proposal, decision = kb.propose_batch(ops, HUMAN)
        assert not isinstance(decision, AutoAccept)
        assert proposal.state == "require_review"


def test_a_bare_string_reviewer_list_is_not_split_into_letters(make_kb: KbFactory) -> None:
    class Weird(Decision):
        reviewers = "alice"  # type: ignore[assignment]
        reason = "r"

    class Pol:
        def evaluate(
            self,
            proposal: Proposal,
            principal: Principal,
            kb: Any,
            acting_as: Principal | None = None,
        ) -> Decision:
            return Weird()

    kb = _kb(make_kb, policy=Pol())
    subject = kb.create_entity("Person", author=HUMAN).id
    ops = [
        AssertLiteralOp(subject=subject, predicate="Person.name", value=v, value_type="Text")
        for v in ("a", "b")
    ]
    proposal, _ = kb.propose_batch(ops, HUMAN)
    assert proposal.reviewers == []


def test_operations_reject_unknown_fields() -> None:
    with pytest.raises(ValueError):
        AssertLiteralOp(  # type: ignore[call-arg]
            subject="s", predicate="Person.name", value="x", value_type="Text", model="m"
        )
    with pytest.raises(ValueError):
        CreateEntityOp(key="k", concept="Person", natural_keys="x")  # type: ignore[call-arg]


def test_reviewer_accept_failing_the_last_operation_rolls_back_everything(
    make_kb: KbFactory,
) -> None:
    from ontolith.core import Assertion

    class RejectOwner:
        def validate(self, assertion: Assertion, kb: Ontology) -> list[str]:
            return ["no owners today"] if assertion.predicate == "Decision.owner" else []

    kb = _kb(make_kb)
    owner = kb.create_entity("Person", author=HUMAN).id
    proposal, _ = kb.propose_batch(_decision_batch(owner=owner), AI, model="m-1")
    kb.validators = [RejectOwner()]  # type: ignore[list-item]

    with pytest.raises(ValidationError, match="no owners today"):
        kb.accept_proposal(proposal.id, REVIEWER)

    assert _entities(kb, "Decision") == []
    assert kb.backend.assertions(subject=owner, status=None) == []
    stored = kb.backend.get_proposal(proposal.id)
    assert stored is not None and stored.state == "require_review"
    assert kb.backend.get_proposal_events(proposal.id) == []


def test_created_entity_ids_are_machine_readable_on_every_path(make_kb: KbFactory) -> None:
    import json

    policy = _ByKind(
        {
            "create_entity": AutoAccept("c"),
            "assert_literal": RequireReview(["r1"], "l"),
            "assert_ref": AutoAccept("r"),
        }
    )
    kb = _kb(make_kb, policy=policy)
    subject = kb.create_entity("Person", author=HUMAN).id
    proposal, _ = kb.propose_batch(_with_decision(subject), HUMAN)
    kb.request_changes(proposal.id, REVIEWER, "again")
    policy.table["assert_literal"] = AutoAccept("l")

    kb.resubmit(proposal.id, HUMAN)

    detail = json.loads(kb.backend.get_proposal_events(proposal.id)[-1].detail or "")
    [decision_id] = _entities(kb, "Decision")
    assert detail["created_entities"] == {"d": decision_id}


# --- properties --------------------------------------------------------------

_DECISIONS = st.sampled_from(["accept", "review_a", "review_b", "reject"])


def _decision(tag: str) -> Decision:
    return {
        "accept": AutoAccept("ok"),
        "review_a": RequireReview(["ra"], "a"),
        "review_b": RequireReview(["rb"], "b"),
        "reject": Reject("no"),
    }[tag]


def _expected(tags: list[str]) -> str:
    if "reject" in tags:
        return "reject"
    if any(t.startswith("review") for t in tags):
        return "review"
    return "accept"


class _Scripted:
    """Returns a scripted decision per evaluation, in call order."""

    def __init__(self, tags: list[str]) -> None:
        self.tags = list(tags)
        self.calls = 0

    def evaluate(
        self, proposal: Proposal, principal: Principal, kb: Any, acting_as: Principal | None = None
    ) -> Decision:
        tag = self.tags[self.calls]
        self.calls += 1
        return _decision(tag)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(tags=st.lists(_DECISIONS, min_size=2, max_size=8), data=st.data())
def test_combined_decision_is_the_strictest_for_any_operation_order(
    make_kb: KbFactory, tags: list[str], data: st.DataObject
) -> None:
    shuffled = data.draw(st.permutations(tags))
    outcomes = []
    for order in (tags, list(shuffled)):
        kb = _kb(make_kb, policy=_Scripted(order))
        subject = kb.create_entity("Person", author=HUMAN).id
        ops = [
            AssertLiteralOp(
                subject=subject,
                predicate="Person.name",
                value=f"v{i}",
                value_type="Text",
                source=f"s{i}",
            )
            for i in range(len(order))
        ]
        _, decision = kb.propose_batch(ops, HUMAN)
        outcomes.append(
            "reject"
            if isinstance(decision, Reject)
            else "review"
            if isinstance(decision, RequireReview)
            else "accept"
        )
    assert outcomes[0] == outcomes[1] == _expected(tags)


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    values=st.lists(st.sampled_from(["a", "b", "c"]), min_size=1, max_size=6),
)
def test_accepting_a_batch_equals_applying_its_operations_in_sequence(
    make_kb: KbFactory, values: list[str]
) -> None:
    def build(use_batch: bool) -> list[tuple[str, str, str, str]]:
        kb = _kb(make_kb)
        subject = kb.create_entity("Person", author=HUMAN).id
        if use_batch:
            kb.propose_batch(
                [
                    AssertLiteralOp(
                        subject=subject,
                        predicate="Person.name",
                        value=v,
                        value_type="Text",
                        source=f"s{i}",
                    )
                    for i, v in enumerate(values)
                ],
                HUMAN,
            )
        else:
            for i, v in enumerate(values):
                kb.assert_literal(subject, "Person.name", v, "Text", HUMAN, source=f"s{i}")
        return sorted(
            (a.predicate, a.value, a.source or "", a.status)
            for a in kb.backend.assertions(subject=subject, status=None)
        )

    assert build(True) == build(False)
