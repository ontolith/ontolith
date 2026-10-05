"""Unit tests for Ontology entry point."""

import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import Assertion, FixedClock, SequentialIdProvider
from ontolith.core.errors import (
    AuthError,
    CapabilityError,
    NotFoundError,
    SchemaError,
    StorageError,
    ValidationError,
)
from ontolith.govern import Provenance
from ontolith.govern.proposal import Proposal
from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR


@pytest.fixture
def temp_db() -> Path:
    """Create a temporary database file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        return Path(f.name)


@pytest.fixture
def kb(temp_db: Path) -> Ontology:
    """Create an Ontology instance with deterministic behavior and a seeded principal."""
    clock = FixedClock("2025-01-01T00:00:00Z")
    ids = SequentialIdProvider(prefix="test")
    ontology = Ontology.connect(temp_db, clock=clock, id_provider=ids)
    # Pre-create the principal used across tests. Uses its own string ID,
    # not the SequentialIdProvider, so entity/assertion IDs are unaffected.
    # write capability: this fixture's tests exercise assert_literal/assert_ref
    # (SPEC §9.3 direct writes), which require >= write.
    ontology.create_principal("alice@example.com", kind="human", default_capability="write")
    yield ontology
    ontology.close()
    temp_db.unlink()


class TestOntology:
    """Tests for Ontology class."""

    def test_connect_creates_database(self, temp_db: Path) -> None:
        """Connecting creates the database and schema."""
        kb = Ontology.connect(temp_db)
        assert temp_db.exists()
        kb.close()

    def test_create_entity(self, kb: Ontology) -> None:
        """Entities can be created."""
        entity = kb.create_entity(
            concept="Person",
            author="alice@example.com",
            natural_key="ada",
        )

        assert entity.id == "test-001"  # Sequential ID
        assert entity.concept == "Person"
        assert entity.natural_key == "ada"
        assert entity.namespace == "default"
        assert entity.created_by == "alice@example.com"
        assert entity.created_at == datetime(2025, 1, 1, tzinfo=UTC)

    def test_create_entity_persists(self, kb: Ontology) -> None:
        """Created entities are persisted."""
        entity = kb.create_entity(
            concept="Person",
            author="alice@example.com",
        )

        retrieved = kb.get_entity(entity.id)
        assert retrieved is not None
        assert retrieved.id == entity.id

    def test_create_entity_unknown_author_raises_auth_error(self, kb: Ontology) -> None:
        with pytest.raises(AuthError, match="Principal not found"):
            kb.create_entity("Person", author="nobody@example.com")

    def test_create_entity_read_only_author_raises_capability_error(self, kb: Ontology) -> None:
        kb.create_principal("readonly@example.com", kind="human", default_capability="read")
        with pytest.raises(CapabilityError, match="lacks propose capability"):
            kb.create_entity("Person", author="readonly@example.com")

    def test_create_entity_propose_capability_succeeds(self, kb: Ontology) -> None:
        kb.create_principal("bob@example.com", kind="human", default_capability="propose")
        entity = kb.create_entity("Person", author="bob@example.com")
        assert entity.created_by == "bob@example.com"

    def test_assert_literal(self, kb: Ontology) -> None:
        """Literal assertions can be created."""
        entity = kb.create_entity("Person", author="alice@example.com")

        assertion = kb.assert_literal(
            subject=entity.id,
            predicate="Person.name",
            value="Ada Lovelace",
            value_type="Text",
            author="alice@example.com",
            confidence=0.95,
            source="test",
        )

        assert assertion.id == "test-002"  # Entity was test-001
        assert assertion.subject == entity.id
        assert assertion.predicate == "Person.name"
        assert assertion.value_kind == "literal"
        assert assertion.value_type == "Text"
        assert assertion.value == "Ada Lovelace"
        assert assertion.confidence == 0.95
        assert assertion.source == "test"

    def test_assert_ref(self, kb: Ontology) -> None:
        """Reference assertions can be created."""
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        assertion = kb.assert_ref(
            subject=person.id,
            predicate="Person.employer",
            target=org.id,
            author="alice@example.com",
            confidence=1.0,
            source="test",
            rationale="LinkedIn profile lists this employer",
        )

        assert assertion.value_kind == "ref"
        assert assertion.value_type is None  # No type for refs
        assert assertion.value == org.id
        assert assertion.source == "test"
        assert assertion.rationale == "LinkedIn profile lists this employer"

    def test_assert_literal_requires_write_capability(self, kb: Ontology) -> None:
        """SPEC §9.3: direct writes require >= write capability."""
        kb.create_principal("bob@example.com", kind="human", default_capability="propose")
        entity = kb.create_entity("Person", author="alice@example.com")

        with pytest.raises(CapabilityError, match="lacks write capability"):
            kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "bob@example.com")

    def test_assert_ref_requires_write_capability(self, kb: Ontology) -> None:
        """SPEC §9.3: direct writes require >= write capability."""
        kb.create_principal("bob@example.com", kind="human", default_capability="propose")
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        with pytest.raises(CapabilityError, match="lacks write capability"):
            kb.assert_ref(person.id, "Person.employer", org.id, "bob@example.com")

    def test_assert_literal_rejects_ai_principal_even_with_write_capability(
        self, kb: Ontology
    ) -> None:
        """AI principals never get the direct-write path, even if misconfigured
        with write capability (ADR-0003: AI proposals always require review)."""
        kb.create_principal(
            "bot@example.com",
            kind="ai",
            owner="alice@example.com",
            default_capability="write",
        )
        entity = kb.create_entity("Person", author="alice@example.com")

        with pytest.raises(CapabilityError, match="cannot make direct writes"):
            kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "bot@example.com")

    def test_assert_literal_routes_through_conflict_pipeline(self, kb: Ontology) -> None:
        """Direct writes still go through SPEC §10 conflict routing, not a raw insert."""
        entity = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", "alice@example.com")

        contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
        assert contradiction is not None
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert len(flagged) == 2

    def test_propose_ref_auto_accept_creates_relation(self, kb: Ontology) -> None:
        """propose_ref() mirrors propose() for relations (SPEC §9)."""
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        proposal, decision = kb.propose_ref(
            person.id, "Person.employer", org.id, "alice@example.com"
        )

        assert proposal.state == "auto_accepted"
        active = kb.assertions(subject=person.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        assert active[0].value_kind == "ref"
        assert active[0].value == org.id
        assert active[0].value_type is None

    def test_propose_ref_conflicting_relations_contradict(self, kb: Ontology) -> None:
        """Static relation predicate: conflicting targets flag a contradiction."""
        person = kb.create_entity("Person", author="alice@example.com")
        org1 = kb.create_entity("Organization", author="alice@example.com")
        org2 = kb.create_entity("Organization", author="alice@example.com")

        kb.propose_ref(person.id, "Person.employer", org1.id, "alice@example.com")
        kb.propose_ref(person.id, "Person.employer", org2.id, "alice@example.com")

        contradiction = kb.backend.get_open_contradiction("default", person.id, "Person.employer")
        assert contradiction is not None
        assert len(contradiction.member_ids) == 2

    def test_propose_ref_time_varying_supersedes(self, kb: Ontology) -> None:
        """Schema-declared time_varying relation predicate supersedes, not contradicts."""
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            temporality="time_varying",
                        ),
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )
        kb.apply_schema(schema, author="admin@example.com")

        person = kb.create_entity("Person", author="alice@example.com")
        org1 = kb.create_entity("Organization", author="alice@example.com")
        org2 = kb.create_entity("Organization", author="alice@example.com")

        kb.propose_ref(person.id, "Person.employer", org1.id, "alice@example.com")
        kb.propose_ref(person.id, "Person.employer", org2.id, "alice@example.com")

        contradiction = kb.backend.get_open_contradiction("default", person.id, "Person.employer")
        assert contradiction is None
        superseded = kb.assertions(
            subject=person.id, predicate="Person.employer", status="superseded"
        )
        assert len(superseded) == 1

    def test_propose_ref_ai_requires_review(self, kb: Ontology) -> None:
        """AI-authored relation proposals require review, same as literal proposals."""
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        proposal, decision = kb.propose_ref(
            person.id, "Person.employer", org.id, "bot@example.com", model="test-model-v1"
        )

        assert proposal.state == "require_review"
        active = kb.assertions(subject=person.id, predicate="Person.employer", status="active")
        assert active == []

    def test_accept_proposal_replays_relation(self, kb: Ontology) -> None:
        """Full propose -> require_review -> accept round trip for a relation.

        Also covers acting_as survives replay for the ref branch (regression
        for the bug where accept_proposal dropped delegation provenance) —
        the literal-branch equivalent is pinned in test_trust_delegation.py's
        test_delegation_survives_the_review_accept_path.
        """
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        proposal, decision = kb.propose_ref(
            person.id,
            "Person.employer",
            org.id,
            "bot@example.com",
            acting_as="alice@example.com",
            model="test-model-v1",
        )
        assert proposal.state == "require_review"

        accepted = kb.accept_proposal(proposal.id, "carol@example.com")
        assert accepted.state == "accepted"

        active = kb.assertions(subject=person.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        assert active[0].value_kind == "ref"
        assert active[0].value == org.id
        assert active[0].proposal_id == proposal.id
        assert active[0].acting_as == "alice@example.com"

    def test_query_assertions_by_subject(self, kb: Ontology) -> None:
        """Assertions can be queried by subject."""
        entity = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", author="alice@example.com")
        kb.assert_literal(
            entity.id, "Person.born", "1815-12-10", "Date", author="alice@example.com"
        )

        assertions = kb.assertions(subject=entity.id)
        assert len(assertions) == 2
        assert {a.predicate for a in assertions} == {"Person.name", "Person.born"}

    def test_query_assertions_by_predicate(self, kb: Ontology) -> None:
        """Assertions can be queried by predicate."""
        e1 = kb.create_entity("Person", author="alice@example.com")
        e2 = kb.create_entity("Person", author="alice@example.com")

        kb.assert_literal(e1.id, "Person.name", "Ada", "Text", author="alice@example.com")
        kb.assert_literal(e2.id, "Person.name", "Grace", "Text", author="alice@example.com")

        assertions = kb.assertions(predicate="Person.name")
        assert len(assertions) == 2
        assert {a.value for a in assertions} == {"Ada", "Grace"}

    def test_assertions_default_to_active_only(self, kb: Ontology) -> None:
        """Assertions query defaults to active status only."""
        entity = kb.create_entity("Person", author="alice@example.com")
        assertion = kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", author="alice@example.com"
        )

        # Retract it
        kb.backend.set_assertion_status(assertion.id, "retracted")

        # Default query doesn't return retracted
        active = kb.assertions(subject=entity.id)
        assert len(active) == 0

        # Explicit status=None returns all
        all_assertions = kb.assertions(subject=entity.id, status=None)
        assert len(all_assertions) == 1
        assert all_assertions[0].status == "retracted"

    def test_end_to_end_workflow(self, kb: Ontology) -> None:
        """Complete workflow: create entity, assert properties, query."""
        # Create entity
        person = kb.create_entity(
            concept="Person",
            author="alice@example.com",
            natural_key="ada",
        )

        # Make assertions
        kb.assert_literal(
            person.id,
            "Person.name",
            "Ada Lovelace",
            "Text",
            author="alice@example.com",
            source="Wikipedia",
            confidence=1.0,
        )

        kb.assert_literal(
            person.id,
            "Person.born",
            "1815-12-10",
            "Date",
            author="alice@example.com",
            confidence=1.0,
        )

        # Query by natural key (simulate)
        retrieved = kb.get_entity(person.id)
        assert retrieved is not None
        assert retrieved.natural_key == "ada"

        # Get all facts about the person
        facts = kb.assertions(subject=person.id)
        assert len(facts) == 2

        # Verify provenance
        name_assertion = next(a for a in facts if a.predicate == "Person.name")
        assert name_assertion.author == "alice@example.com"
        assert name_assertion.source == "Wikipedia"
        assert name_assertion.confidence == 1.0

    def test_create_principal(self, kb: Ontology) -> None:
        """Principals can be created."""
        principal = kb.create_principal(
            "bob@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=10,
        )

        assert principal.id == "bob@example.com"
        assert principal.kind == "human"
        assert principal.default_capability == "write"
        assert principal.trust_level == 10

    def test_create_principal_persists(self, kb: Ontology) -> None:
        """Created principals are persisted."""
        kb.create_principal("bob@example.com", kind="human")

        retrieved = kb.get_principal("bob@example.com")
        assert retrieved is not None
        assert retrieved.id == "bob@example.com"

    def test_create_ai_principal_with_owner(self, kb: Ontology) -> None:
        """AI principals require an owner."""
        principal = kb.create_principal(
            "research-bot",
            kind="ai",
            owner="alice@example.com",
            auth_method="workload",
            metadata={"model": "claude-sonnet-4"},
        )

        assert principal.kind == "ai"
        assert principal.owner == "alice@example.com"
        assert principal.metadata["model"] == "claude-sonnet-4"

    def test_create_ai_principal_without_owner_raises_validation_error(self, kb: Ontology) -> None:
        """An AI principal with no owner at all raises ontolith's own
        ValidationError, not a raw pydantic one — callers (REST's error
        mapping in particular) only handle OntolithError subtypes."""
        with pytest.raises(ValidationError, match="AI principals must have an owner"):
            kb.create_principal("research-bot", kind="ai", auth_method="workload")

    def test_create_ai_principal_with_unresolvable_owner_raises_validation_error(
        self, kb: Ontology
    ) -> None:
        with pytest.raises(ValidationError, match="owner not found"):
            kb.create_principal(
                "research-bot", kind="ai", owner="nobody@example.com", auth_method="workload"
            )

    def test_create_ai_principal_with_ai_owner_raises_validation_error(self, kb: Ontology) -> None:
        kb.create_principal(
            "other-bot", kind="ai", owner="alice@example.com", auth_method="workload"
        )
        with pytest.raises(ValidationError, match="must be human or service"):
            kb.create_principal(
                "research-bot", kind="ai", owner="other-bot", auth_method="workload"
            )


class TestSubjectExistenceCheck:
    """KI-083: writing against a nonexistent subject raises NotFoundError
    up front, not a redacted StorageError from a late FOREIGN KEY failure."""

    def test_assert_literal_unknown_subject_raises_not_found(self, kb: Ontology) -> None:
        with pytest.raises(NotFoundError, match="Subject not found"):
            kb.assert_literal("nonexistent-id", "Person.name", "Ada", "Text", "alice@example.com")

    def test_assert_ref_unknown_subject_raises_not_found(self, kb: Ontology) -> None:
        org = kb.create_entity("Organization", author="alice@example.com")
        with pytest.raises(NotFoundError, match="Subject not found"):
            kb.assert_ref("nonexistent-id", "Person.employer", org.id, "alice@example.com")

    def test_propose_unknown_subject_raises_not_found(self, kb: Ontology) -> None:
        with pytest.raises(NotFoundError, match="Subject not found"):
            kb.propose("nonexistent-id", "Person.name", "Ada", "Text", "alice@example.com")

    def test_propose_ref_unknown_subject_raises_not_found(self, kb: Ontology) -> None:
        org = kb.create_entity("Organization", author="alice@example.com")
        with pytest.raises(NotFoundError, match="Subject not found"):
            kb.propose_ref("nonexistent-id", "Person.employer", org.id, "alice@example.com")

    def test_assert_literal_existing_subject_unaffected(self, kb: Ontology) -> None:
        """The check doesn't false-positive on a real entity."""
        entity = kb.create_entity("Person", author="alice@example.com")
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")
        assert assertion.subject == entity.id


class TestTargetExistenceCheck:
    """KI-089: a ref assertion against a nonexistent target raises
    NotFoundError up front, instead of silently persisting a dangling
    reference (assert_ref/propose_ref's target has no FOREIGN KEY on
    either backend, unlike subject — KI-083's check doesn't cover it)."""

    def test_assert_ref_unknown_target_raises_not_found(self, kb: Ontology) -> None:
        person = kb.create_entity("Person", author="alice@example.com")
        with pytest.raises(NotFoundError, match="Target not found"):
            kb.assert_ref(person.id, "Person.employer", "nonexistent-id", "alice@example.com")

    def test_propose_ref_unknown_target_raises_not_found(self, kb: Ontology) -> None:
        person = kb.create_entity("Person", author="alice@example.com")
        with pytest.raises(NotFoundError, match="Target not found"):
            kb.propose_ref(person.id, "Person.employer", "nonexistent-id", "alice@example.com")

    def test_assert_ref_unknown_target_does_not_persist_a_dangling_reference(
        self, kb: Ontology
    ) -> None:
        """The exact regression KI-089 closes: before this fix, this call
        silently succeeded and left a reference to nothing in the KB."""
        person = kb.create_entity("Person", author="alice@example.com")
        with pytest.raises(NotFoundError):
            kb.assert_ref(person.id, "Person.employer", "nonexistent-id", "alice@example.com")
        assert kb.assertions(subject=person.id) == []

    def test_propose_ref_unknown_target_does_not_persist_a_dangling_reference(
        self, kb: Ontology
    ) -> None:
        """Same regression as the assert_ref test above, via the governed
        propose path — alice has write capability, so this would otherwise
        auto-accept and persist just as directly."""
        person = kb.create_entity("Person", author="alice@example.com")
        with pytest.raises(NotFoundError):
            kb.propose_ref(person.id, "Person.employer", "nonexistent-id", "alice@example.com")
        assert kb.assertions(subject=person.id) == []

    def test_assert_ref_existing_target_unaffected(self, kb: Ontology) -> None:
        """The check doesn't false-positive on a real target entity."""
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")
        assertion = kb.assert_ref(person.id, "Person.employer", org.id, "alice@example.com")
        assert assertion.value == org.id


class TestCreateEntityNaturalKeyTransaction:
    """KI-092: `create_entity`'s natural-key uniqueness check and the
    `put_entity` it guards run inside one `transaction()` block, so on
    SQLite `begin()`'s `BEGIN IMMEDIATE` (KI-084) serializes a concurrent
    writer between them rather than letting a duplicate slip in and hit
    the raw `UNIQUE` constraint (a redacted `StorageError`, the exact
    error KI-091 was filed to eliminate for the non-concurrent case)."""

    def test_uniqueness_check_and_write_share_one_transaction(self, kb: Ontology) -> None:
        """Primary mutation guard, backend-agnostic: `begin()` must run
        before the uniqueness check and `commit()` after `put_entity`.
        Reverting the `with self.backend.transaction():` wrapper drops
        `begin`/`commit` from the sequence entirely."""
        calls: list[str] = []

        def rec(name: str, fn: object) -> object:
            def wrapper(*args: object, **kwargs: object) -> object:
                calls.append(name)
                return fn(*args, **kwargs)  # type: ignore[operator]

            return wrapper

        kb.backend.begin = rec("begin", kb.backend.begin)  # type: ignore[method-assign]
        kb.backend.commit = rec("commit", kb.backend.commit)  # type: ignore[method-assign]
        kb.backend.put_entity = rec("put_entity", kb.backend.put_entity)  # type: ignore[method-assign]
        kb._require_unique_natural_key = rec(  # type: ignore[method-assign]
            "check", kb._require_unique_natural_key
        )

        kb.create_entity("Person", author="alice@example.com", natural_key="ada")
        assert calls == ["begin", "check", "put_entity", "commit"]

    def test_concurrent_duplicate_is_rejected_as_validation_error_not_storage_error(
        self, temp_db: Path
    ) -> None:
        """Two `Ontology` instances on the same file (two OS processes).
        `first` opens its transaction and pauses mid-check while holding
        `BEGIN IMMEDIATE`'s write lock; `second`'s `create_entity` for the
        same key blocks on its own `begin()` until `first` commits, then
        its own in-transaction check sees the row and raises
        `ValidationError` — nobody reaches the raw `UNIQUE` constraint."""
        clock = FixedClock("2025-01-01T00:00:00Z")
        first = Ontology.connect(temp_db, clock=clock, id_provider=SequentialIdProvider(prefix="a"))
        second = Ontology.connect(
            temp_db, clock=clock, id_provider=SequentialIdProvider(prefix="b")
        )
        try:
            first.create_principal("alice@example.com", kind="human", default_capability="write")
            # Short busy_timeout on `second` so the test doesn't wait out the
            # real 5s if the fix regresses and the lock is never contended.
            second.backend.conn.execute("PRAGMA busy_timeout = 2000")  # type: ignore[attr-defined]

            checked = threading.Event()
            release = threading.Event()
            errors: list[BaseException] = []
            real_check = first._require_unique_natural_key

            def paused_check(concept: str, natural_key: str | None) -> None:
                real_check(concept, natural_key)
                checked.set()
                release.wait(timeout=5)

            first._require_unique_natural_key = paused_check  # type: ignore[method-assign]

            def create_first() -> None:
                try:
                    first.create_entity("Person", author="alice@example.com", natural_key="ada")
                except BaseException as e:  # noqa: BLE001 - surfaced via the assertion below
                    errors.append(e)

            t = threading.Thread(target=create_first)
            t.start()
            assert checked.wait(timeout=5)  # first has begun + checked, holds the write lock

            second_error: list[BaseException] = []

            def create_second() -> None:
                try:
                    second.create_entity("Person", author="alice@example.com", natural_key="ada")
                except BaseException as e:  # noqa: BLE001
                    second_error.append(e)

            t2 = threading.Thread(target=create_second)
            t2.start()
            time.sleep(0.2)  # let second's begin() start blocking on the write lock
            # The contention this test exists to exercise: `second` must be
            # parked in its own `begin()` right now, behind `first`'s held
            # write lock. Without it, `second` runs entirely after `first`
            # commits, sees the row, and raises ValidationError anyway —
            # passing identically even with the wrapper reverted.
            assert t2.is_alive(), "second did not block on begin() — no contention exercised"
            release.set()
            t.join(timeout=5)
            t2.join(timeout=5)
            assert not t.is_alive() and not t2.is_alive()

            assert errors == []  # first succeeded
            assert len(second_error) == 1
            assert isinstance(second_error[0], ValidationError)
            assert not isinstance(second_error[0], StorageError)
            assert "Entity conflict" in str(second_error[0])
            # Exactly one "ada" Person landed.
            people = first.backend.entities(namespace=first.namespace, concept="Person")
            assert [e.natural_key for e in people] == ["ada"]
        finally:
            first.close()
            second.close()


class TestProvenance:
    """KI-086 / ADR-0047: `Ontology.provenance(assertion_id)` is the single
    domain-layer implementation of SPEC §5.4's one-call provenance view;
    REST/GraphQL/MCP now shape its result rather than re-deriving it."""

    def test_unknown_assertion_raises_not_found(self, kb: Ontology) -> None:
        with pytest.raises(NotFoundError, match="Assertion 'nope' not found"):
            kb.provenance("nope")

    def test_direct_write_has_no_review_events_and_no_superseded(self, kb: Ontology) -> None:
        entity = kb.create_entity("Person", author="alice@example.com")
        a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")

        prov = kb.provenance(a.id)
        assert isinstance(prov, Provenance)
        assert prov.assertion.id == a.id
        assert prov.assertion.proposal_id is None
        assert prov.review_events == ()
        assert prov.superseded_ids == ()

    def test_reviewed_assertion_carries_its_proposal_events_in_order(self, kb: Ontology) -> None:
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        entity = kb.create_entity("Person", author="alice@example.com")
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", "bot@example.com", model="test-model-v1"
        )
        kb.accept_proposal(proposal.id, "carol@example.com")
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        prov = kb.provenance(active[0].id)
        assert prov.assertion.proposal_id == proposal.id
        assert [e.type for e in prov.review_events] == ["accept"]
        assert prov.review_events[0].actor == "carol@example.com"

    def test_superseded_ids_recover_the_full_predecessor_set(self, kb: Ontology) -> None:
        """KI-008: `assertion.supersedes` records only the first predecessor
        when one write supersedes several concurrently-overlapping ones —
        `superseded_ids` recovers all of them. Mirrors the REST test's setup:
        two overlapping predecessors written straight through the backend
        (bypassing conflict routing so both stay active), then a governed
        write that supersedes both."""
        kb.backend.put_schema(
            SchemaIR(
                namespace="default",
                version=1,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={
                            "role": PropertyDef(
                                name="role", value_type="Text", temporality="time_varying"
                            ),
                        },
                    ),
                },
            )
        )
        entity = kb.create_entity("Person", author="alice@example.com")
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        pred_ids = []
        for value in ("Engineer", "Manager"):
            pred = Assertion(
                id=kb.id_provider.next(),
                namespace="default",
                subject=entity.id,
                predicate="Person.role",
                value_kind="literal",
                value_type="Text",
                value=value,
                author="alice@example.com",
                asserted_at=t0,
                valid_from=t0,
            )
            kb.backend.put_assertion(pred)
            pred_ids.append(pred.id)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=90)
        winner = kb.assert_literal(
            entity.id, "Person.role", "Director", "Text", "alice@example.com"
        )

        prov = kb.provenance(winner.id)
        assert set(prov.superseded_ids) == set(pred_ids)
        assert prov.assertion.supersedes in set(pred_ids)


class TestModelCapture:
    """model is required for ai-kind authors on the governed proposal path
    (SPEC §7.4/§14.4); optional and never required for human/service authors.
    """

    def test_propose_ai_without_model_raises_validation_error(self, kb: Ontology) -> None:
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        entity = kb.create_entity("Person", author="alice@example.com")

        with pytest.raises(ValidationError, match="model is required for ai-kind authors"):
            kb.propose(entity.id, "Person.name", "Ada", "Text", "bot@example.com")

    def test_propose_ref_ai_without_model_raises_validation_error(self, kb: Ontology) -> None:
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        person = kb.create_entity("Person", author="alice@example.com")
        org = kb.create_entity("Organization", author="alice@example.com")

        with pytest.raises(ValidationError, match="model is required for ai-kind authors"):
            kb.propose_ref(person.id, "Person.employer", org.id, "bot@example.com")

    def test_propose_ai_with_model_succeeds_and_round_trips(self, kb: Ontology) -> None:
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )
        entity = kb.create_entity("Person", author="alice@example.com")

        proposal, decision = kb.propose(
            entity.id, "Person.name", "Ada", "Text", "bot@example.com", model="claude-sonnet-4"
        )
        assert proposal.state == "require_review"

        # Round-trips through accept_proposal's replay and SQLite persistence
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.accept_proposal(proposal.id, "carol@example.com")
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1
        assert active[0].model == "claude-sonnet-4"

    def test_propose_human_author_model_optional(self, kb: Ontology) -> None:
        """Human/service authors are never required to pass model."""
        entity = kb.create_entity("Person", author="alice@example.com")

        proposal, decision = kb.propose(
            entity.id, "Person.name", "Ada", "Text", "alice@example.com"
        )
        assert proposal.state == "auto_accepted"
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active[0].model is None

    def test_assert_literal_model_optional_for_write_capable_human(self, kb: Ontology) -> None:
        """Direct writes are human/service-only (AI hard-blocked), so model is
        accepted but never required here — still round-trips when provided."""
        entity = kb.create_entity("Person", author="alice@example.com")

        assertion = kb.assert_literal(
            entity.id, "Person.name", "Ada", "Text", "alice@example.com", model="unused-model"
        )
        assert assertion.model == "unused-model"


class TestApplySchema:
    """Tests for Ontology.apply_schema (SPEC §6, capability-checked schema persistence)."""

    def test_admin_can_apply_first_schema_version(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        schema = SchemaIR(namespace="default", version=1, concepts={})

        applied = kb.apply_schema(schema, author="admin@example.com")

        assert applied.version == 1
        assert kb.backend.get_schema("default") is not None

    def test_non_admin_raises_capability_error(self, kb: Ontology) -> None:
        """alice@example.com defaults to 'propose' capability, not 'admin'."""
        schema = SchemaIR(namespace="default", version=1, concepts={})

        with pytest.raises(CapabilityError, match="lacks admin capability"):
            kb.apply_schema(schema, author="alice@example.com")

    def test_unknown_author_raises_auth_error(self, kb: Ontology) -> None:
        schema = SchemaIR(namespace="default", version=1, concepts={})

        with pytest.raises(AuthError, match="Principal not found"):
            kb.apply_schema(schema, author="nobody@example.com")

    def test_second_version_must_be_monotonic(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.apply_schema(
            SchemaIR(namespace="default", version=1, concepts={}), author="admin@example.com"
        )

        applied = kb.apply_schema(
            SchemaIR(namespace="default", version=2, concepts={}), author="admin@example.com"
        )
        assert applied.version == 2

    def test_skipping_a_version_raises_schema_error(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.apply_schema(
            SchemaIR(namespace="default", version=1, concepts={}), author="admin@example.com"
        )

        with pytest.raises(SchemaError, match="expected 2"):
            kb.apply_schema(
                SchemaIR(namespace="default", version=3, concepts={}), author="admin@example.com"
            )

    def test_first_version_must_be_one(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")

        with pytest.raises(SchemaError, match="expected 1"):
            kb.apply_schema(
                SchemaIR(namespace="default", version=2, concepts={}), author="admin@example.com"
            )


class TestRequireAdmin:
    """KI-053: require_admin must reject AI-kind principals regardless of
    their configured capability, matching every other capability-tier gate
    (_check_direct_write_capability, _require_reviewer_principal,
    resolve_contradiction) - admin was the one gate that didn't."""

    def test_human_admin_passes(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        principal = kb.require_admin("admin@example.com")
        assert principal.id == "admin@example.com"

    def test_ai_principal_with_admin_capability_is_rejected(self, kb: Ontology) -> None:
        """A misconfigured AI principal with default_capability="admin" must
        not pass require_admin - without this, it could call issue_token()
        for a human principal and authenticate as that human, bypassing
        every AI-kind guard on write/review/resolve."""
        kb.create_principal(
            "bot-admin",
            kind="ai",
            owner="alice@example.com",
            default_capability="admin",
        )
        with pytest.raises(CapabilityError, match="AI principal"):
            kb.require_admin("bot-admin")

    def test_non_admin_capability_is_rejected(self, kb: Ontology) -> None:
        with pytest.raises(CapabilityError, match="lacks admin capability"):
            kb.require_admin("alice@example.com")

    def test_unknown_principal_raises_auth_error(self, kb: Ontology) -> None:
        with pytest.raises(AuthError, match="Principal not found"):
            kb.require_admin("nobody@example.com")


class TestSchema:
    """Tests for Ontology.schema() (ADR-0036 - added so ReadOnlyView.schema()
    could delegate here instead of reaching into the storage port directly)."""

    def test_returns_none_when_no_schema_applied(self, kb: Ontology) -> None:
        assert kb.schema() is None

    def test_returns_current_schema(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        schema = SchemaIR(
            namespace="default", version=1, concepts={"Person": ConceptDef(name="Person")}
        )
        kb.apply_schema(schema, author="admin@example.com")

        result = kb.schema()
        assert result is not None
        assert result.version == 1
        assert "Person" in result.concepts

    def test_returns_latest_version(self, kb: Ontology) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.apply_schema(
            SchemaIR(namespace="default", version=1, concepts={}), author="admin@example.com"
        )
        kb.apply_schema(
            SchemaIR(namespace="default", version=2, concepts={}), author="admin@example.com"
        )

        result = kb.schema()
        assert result is not None
        assert result.version == 2


class TestAcceptProposalReResolvesTemporality:
    """accept_proposal must re-resolve temporality from the current schema at
    apply time, not trust the snapshot stored at propose time — otherwise a
    schema migration between propose and accept silently misroutes SPEC §10
    conflict handling (a static predicate could be superseded instead of
    flagged as a contradiction)."""

    def test_schema_change_between_propose_and_accept_uses_apply_time_temporality(
        self, kb: Ontology
    ) -> None:
        kb.create_principal("admin@example.com", kind="human", default_capability="admin")
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        kb.create_principal(
            "bot@example.com", kind="ai", owner="alice@example.com", default_capability="propose"
        )

        kb.apply_schema(
            SchemaIR(
                namespace="default",
                version=1,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={
                            "name": PropertyDef(
                                name="name", value_type="Text", temporality="static"
                            )
                        },
                    )
                },
            ),
            author="admin@example.com",
        )

        entity = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")

        proposal, decision = kb.propose(
            entity.id, "Person.name", "Ava", "Text", "bot@example.com", model="test-model-v1"
        )
        assert proposal.state == "require_review"

        # Schema migrates to time_varying before the proposal is reviewed.
        kb.apply_schema(
            SchemaIR(
                namespace="default",
                version=2,
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

        kb.accept_proposal(proposal.id, "carol@example.com")

        # Routed as time_varying (current schema at accept time): Ada
        # superseded, Ava active — NOT both flagged as a static contradiction.
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        superseded = kb.assertions(subject=entity.id, predicate="Person.name", status="superseded")
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert [a.value for a in active] == ["Ava"]
        assert [a.value for a in superseded] == ["Ada"]
        assert flagged == []


class TestRetract:
    """Ontology.retract() — propose-level capability gate (ADR-0008)."""

    def test_unknown_assertion_raises_not_found(self, kb: Ontology) -> None:
        """Regression (KI-074 review): an unknown assertion_id used to
        surface as an opaque StorageError off the backend's generic
        set_assertion_status write, indistinguishable from a genuine
        storage fault and — once KI-074's blanket MCP handler started
        redacting StorageError — hidden behind "An internal error
        occurred" everywhere retract() is exposed."""
        with pytest.raises(NotFoundError, match="Assertion not found: nonexistent"):
            kb.retract("nonexistent", author="alice@example.com")

    def test_unknown_assertion_raises_not_found_even_when_policy_routes_to_review(
        self, kb: Ontology
    ) -> None:
        """Regression (KI-074 review, round 2): the existence check must
        run before policy is evaluated at all, not only inside the
        auto-accept transaction - an AI principal (always routed to
        review by ThresholdPolicy, ADR-0003) retracting an unknown
        assertion_id previously got no error at all: a phantom
        require_review proposal was created and silently persisted."""
        kb.create_principal("bot@example.com", kind="ai", owner="alice@example.com")

        with pytest.raises(NotFoundError, match="Assertion not found: nonexistent"):
            kb.retract("nonexistent", author="bot@example.com")

        assert [p for p in kb.proposals(state=None) if p.author == "bot@example.com"] == []

    def test_replay_backstop_raises_not_found_for_a_hand_crafted_phantom_proposal(
        self, kb: Ontology
    ) -> None:
        """retract() itself can no longer create a proposal targeting an
        unknown assertion_id (checked up front now, previous test) - this
        pins _replay_proposal_operations's own backstop for a proposal
        not built via retract() at all, hand-crafted here the same way
        test_ontology_validators.py reaches its own otherwise-unreachable
        shapes. Without the backstop this hits a bare `assert`, escaping
        accept_proposal as an uncaught, blank-message AssertionError
        instead of a structured NotFoundError."""
        kb.create_principal("carol@example.com", kind="human", default_capability="review")
        proposal = Proposal(
            id=kb.id_provider.next(),
            namespace=kb.namespace,
            author="alice@example.com",
            state="require_review",
            created_at=kb.clock.now(),
            payload={"operations": [{"kind": "retract", "assertion_id": "nonexistent"}]},
        )
        kb.backend.put_proposal(proposal)

        with pytest.raises(NotFoundError, match="Assertion not found: nonexistent"):
            kb.accept_proposal(proposal.id, reviewer="carol@example.com")


class TestFlagContradiction:
    """Ontology.flag_contradiction() — propose-level capability gate (ADR-0008)."""

    def _conflicting_assertions(self, kb: Ontology) -> tuple[str, str, str]:
        """Two active assertions on the same (subject, predicate) with different
        values, inserted directly (bypassing conflict routing) so they don't
        already form a contradiction before flag_contradiction() is called."""
        from ontolith.core import Assertion

        entity = kb.create_entity("Person", author="alice@example.com")
        a = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada",
            author="alice@example.com",
            asserted_at=kb.clock.now(),
            status="active",
        )
        b = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ava",
            author="alice@example.com",
            asserted_at=kb.clock.now(),
            status="active",
        )
        kb.backend.put_assertion(a)
        kb.backend.put_assertion(b)
        return entity.id, a.id, b.id

    def test_read_capability_raises_capability_error(self, kb: Ontology) -> None:
        """Previously there was no capability check at all — the HIGH finding."""
        kb.create_principal("readonly@example.com", kind="human", default_capability="read")
        _, a_id, b_id = self._conflicting_assertions(kb)

        with pytest.raises(CapabilityError, match="lacks propose capability"):
            kb.flag_contradiction(a_id, b_id, "readonly@example.com")

    def test_propose_capability_succeeds(self, kb: Ontology) -> None:
        kb.create_principal("proposer@example.com", kind="human", default_capability="propose")
        _, a_id, b_id = self._conflicting_assertions(kb)

        contradiction, action = kb.flag_contradiction(a_id, b_id, "proposer@example.com")
        assert action == "created"
        assert set(contradiction.member_ids) >= {a_id, b_id}

    def test_unknown_author_raises_auth_error(self, kb: Ontology) -> None:
        _, a_id, b_id = self._conflicting_assertions(kb)
        with pytest.raises(AuthError, match="Principal not found"):
            kb.flag_contradiction(a_id, b_id, "nobody@example.com")

    def test_unknown_assertion_raises_not_found(self, kb: Ontology) -> None:
        _, a_id, _ = self._conflicting_assertions(kb)
        with pytest.raises(NotFoundError, match="Assertion not found"):
            kb.flag_contradiction(a_id, "nonexistent", "alice@example.com")

    def test_mismatched_subject_predicate_raises_validation_error(self, kb: Ontology) -> None:
        entity = kb.create_entity("Person", author="alice@example.com")
        a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")
        b = kb.assert_literal(entity.id, "Person.born", "1815", "Text", "alice@example.com")

        with pytest.raises(ValidationError, match="same subject and predicate"):
            kb.flag_contradiction(a.id, b.id, "alice@example.com")

    def test_rationale_persisted_in_contradiction_metadata(self, kb: Ontology) -> None:
        _, a_id, b_id = self._conflicting_assertions(kb)
        contradiction, _ = kb.flag_contradiction(
            a_id, b_id, "alice@example.com", rationale="Sources disagree"
        )
        assert contradiction.metadata["rationale_history"] == [
            {
                "rationale": "Sources disagree",
                "actor": "alice@example.com",
                "at": "2025-01-01T00:00:00+00:00",
            }
        ]

    def test_third_conflicting_assertion_extends_existing_contradiction(self, kb: Ontology) -> None:
        entity_id, a_id, b_id = self._conflicting_assertions(kb)
        first, _ = kb.flag_contradiction(a_id, b_id, "alice@example.com")

        c_assertion = kb.assert_literal(
            entity_id, "Person.name", "Eve", "Text", "alice@example.com"
        )
        second, action = kb.flag_contradiction(a_id, c_assertion.id, "alice@example.com")

        assert action == "extended"
        assert second.id == first.id
        assert set(second.member_ids) == {a_id, b_id, c_assertion.id}

    def test_rationale_on_extend_is_appended_not_dropped(self, kb: Ontology) -> None:
        """Regression (KI-071): extending an already-open contradiction
        used to silently discard `rationale` entirely - `existing.metadata`
        was never touched on this branch. It must now accumulate alongside
        whatever rationale (if any) was given when the contradiction was
        first created, not replace it."""
        kb.create_principal("bob@example.com", kind="human", default_capability="propose")
        entity_id, a_id, b_id = self._conflicting_assertions(kb)
        kb.flag_contradiction(a_id, b_id, "alice@example.com", rationale="Sources disagree")

        c_assertion = kb.assert_literal(
            entity_id, "Person.name", "Eve", "Text", "alice@example.com"
        )
        kb.clock.advance(days=1)  # type: ignore[attr-defined]  # distinct `at`, not just distinct content
        extended, action = kb.flag_contradiction(
            a_id, c_assertion.id, "bob@example.com", rationale="A third source also disagrees"
        )

        assert action == "extended"
        assert extended.metadata["rationale_history"] == [
            {
                "rationale": "Sources disagree",
                "actor": "alice@example.com",
                "at": "2025-01-01T00:00:00+00:00",
            },
            {
                "rationale": "A third source also disagrees",
                "actor": "bob@example.com",
                "at": "2025-01-02T00:00:00+00:00",
            },
        ]

    def test_extend_without_rationale_leaves_existing_history_untouched(self, kb: Ontology) -> None:
        """A caller extending membership without explaining why must not
        blank out a rationale an earlier call already recorded."""
        entity_id, a_id, b_id = self._conflicting_assertions(kb)
        kb.flag_contradiction(a_id, b_id, "alice@example.com", rationale="Sources disagree")

        c_assertion = kb.assert_literal(
            entity_id, "Person.name", "Eve", "Text", "alice@example.com"
        )
        extended, _ = kb.flag_contradiction(a_id, c_assertion.id, "alice@example.com")

        assert extended.metadata["rationale_history"] == [
            {
                "rationale": "Sources disagree",
                "actor": "alice@example.com",
                "at": "2025-01-01T00:00:00+00:00",
            }
        ]

    def test_empty_string_rationale_is_treated_the_same_as_none(self, kb: Ontology) -> None:
        """`rationale=""` must not record a real history entry - matches
        this method's own long-standing truthy check (an empty string was
        never distinguished from "no rationale given" on the "create"
        branch, and the "extend" branch added by this fix must not
        introduce that distinction either)."""
        entity_id, a_id, b_id = self._conflicting_assertions(kb)
        created, _ = kb.flag_contradiction(a_id, b_id, "alice@example.com", rationale="")
        assert created.metadata == {}

        c_assertion = kb.assert_literal(
            entity_id, "Person.name", "Eve", "Text", "alice@example.com"
        )
        extended, _ = kb.flag_contradiction(a_id, c_assertion.id, "alice@example.com", rationale="")
        assert extended.metadata == {}

    def test_extend_adds_rationale_to_a_contradiction_created_without_one(
        self, kb: Ontology
    ) -> None:
        """The created-without-rationale case is the other half of the same
        gap: metadata starts as {} (no rationale_history key at all), so
        the extend branch must build the list from scratch, not assume it
        already exists."""
        entity_id, a_id, b_id = self._conflicting_assertions(kb)
        first, _ = kb.flag_contradiction(a_id, b_id, "alice@example.com")
        assert first.metadata == {}

        c_assertion = kb.assert_literal(
            entity_id, "Person.name", "Eve", "Text", "alice@example.com"
        )
        extended, _ = kb.flag_contradiction(
            a_id, c_assertion.id, "alice@example.com", rationale="Now we have an explanation"
        )

        assert extended.metadata["rationale_history"] == [
            {
                "rationale": "Now we have an explanation",
                "actor": "alice@example.com",
                "at": "2025-01-01T00:00:00+00:00",
            }
        ]

    def test_extend_with_malformed_prior_history_degrades_gracefully(self, kb: Ontology) -> None:
        """`metadata` is an open, schema-less blob (ADR-0041) - some other
        writer holding a raw backend connection could have left
        `rationale_history` in any shape at all. Before this fix, reading
        it back here (`existing.metadata.get("rationale_history", [])`,
        not routed through `safe_rationale_history()`) either raised
        (a non-iterable value) or silently corrupted the trail further
        (e.g. a bare string exploded into one "entry" per character on
        append) - the same class of bug KI-076's review found on the MCP
        read side, but on this write path instead."""
        entity_id, a_id, b_id = self._conflicting_assertions(kb)
        kb.flag_contradiction(a_id, b_id, "alice@example.com")
        created = kb.contradictions()[0]
        kb.backend.update_contradiction_members(
            created.id,
            created.member_ids,
            metadata={"rationale_history": "not a list at all"},
        )

        c_assertion = kb.assert_literal(
            entity_id, "Person.name", "Eve", "Text", "alice@example.com"
        )
        extended, action = kb.flag_contradiction(
            a_id, c_assertion.id, "alice@example.com", rationale="A well-formed rationale"
        )

        assert action == "extended"
        assert extended.metadata["rationale_history"] == [
            {
                "rationale": "A well-formed rationale",
                "actor": "alice@example.com",
                "at": "2025-01-01T00:00:00+00:00",
            }
        ]


class TestReindex:
    """Tests for Ontology.reindex() (SPEC §11.3, ADR-0020)."""

    def test_reindex_embeds_text_properties(self, kb: Ontology) -> None:
        """A Text-typed assertion's entity is embedded and becomes findable via .semantic()."""
        entity = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ada Lovelace", "Text", "alice@example.com")

        count = kb.reindex()

        assert count == 1
        results = kb.query("Person").semantic("Ada Lovelace").all()
        assert results[0].id == entity.id

    def test_reindex_skips_entities_without_text_assertions(self, kb: Ontology) -> None:
        """An entity with only non-Text assertions is not upserted into the vector index."""
        with_text = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(with_text.id, "Person.name", "Ada", "Text", "alice@example.com")

        without_text = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(without_text.id, "Person.born", "1815", "Integer", "alice@example.com")

        count = kb.reindex()

        assert count == 1
        vec = kb.embedder.embed(["Ada"])[0]
        results = kb.backend.vector_search("entity", vec, k=10)
        assert [entity_id for entity_id, _ in results] == [with_text.id]

    def test_reindex_no_entities_returns_zero(self, kb: Ontology) -> None:
        assert kb.reindex() == 0

    def test_reindex_returns_zero_when_no_text_assertions(self, kb: Ontology) -> None:
        kb.create_entity("Person", author="alice@example.com")
        assert kb.reindex() == 0

    def test_reindex_concept_filter(self, kb: Ontology) -> None:
        """reindex(concept=...) only embeds entities of that concept."""
        person = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(person.id, "Person.name", "Ada", "Text", "alice@example.com")

        org = kb.create_entity("Organization", author="alice@example.com")
        kb.assert_literal(org.id, "Organization.name", "Acme", "Text", "alice@example.com")

        count = kb.reindex(concept="Person")

        assert count == 1
        vec = kb.embedder.embed(["Acme"])[0]
        results = kb.backend.vector_search("entity", vec, k=10)
        assert org.id not in {entity_id for entity_id, _ in results}

    def test_reindex_is_idempotent(self, kb: Ontology) -> None:
        """Calling reindex() repeatedly upserts (replaces), not duplicates."""
        entity = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")

        first = kb.reindex()
        second = kb.reindex()

        assert first == 1
        assert second == 1
        vec = kb.embedder.embed(["Ada"])[0]
        results = kb.backend.vector_search("entity", vec, k=10)
        assert len(results) == 1
        assert results[0][0] == entity.id

    def test_reindex_concatenates_multiple_text_properties(self, kb: Ontology) -> None:
        """Multiple Text-typed assertions on one entity are concatenated, sorted by predicate."""
        entity = kb.create_entity("Person", author="alice@example.com")
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", "alice@example.com")
        kb.assert_literal(entity.id, "Person.bio", "Mathematician", "Text", "alice@example.com")

        assert kb.reindex() == 1

        # "Person.bio" < "Person.name" alphabetically.
        vec = kb.embedder.embed(["Mathematician Ada"])[0]
        results = kb.backend.vector_search("entity", vec, k=10)
        assert results[0][0] == entity.id
        assert results[0][1] == pytest.approx(0.0, abs=1e-6)


class TestListNamespaces:
    """Ontology.list_namespaces() (SPEC §12.2, KI-022)."""

    def test_returns_seeded_default_namespace(self, kb: Ontology) -> None:
        """Delegates to backend.list_namespaces() - proves the wrapper is
        wired correctly, not just that the backend method works in
        isolation (already covered per-backend in test_sqlite_backend.py/
        test_duckdb_backend.py)."""
        namespaces = kb.list_namespaces()
        assert [n.id for n in namespaces] == ["default"]
