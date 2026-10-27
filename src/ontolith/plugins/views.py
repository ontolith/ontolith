"""Capability-scoped facades over Ontology for plugin code (ADR-0015).

ReadOnlyView and WriteView expose only a safe method subset of Ontology.
Admin-only methods (issue_token, revoke_token, apply_schema, create_principal,
record_admin_event, get_admin_events, accept_proposal, reject_proposal,
resolve_contradiction, flag_contradiction) and the direct-write bypass
methods (assert_literal, assert_ref) are
STRUCTURALLY ABSENT — not runtime-checked — so a plugin using the intended
API surface cannot reach them, and the omission cannot regress silently the
way a runtime check could if someone forgot to call it.

Every mutating method hardcodes `author` to the view's own bound principal
id — there is no `acting_as` parameter on any view method. Delegation is a
human/AI-owner concept (ADR-0003); letting a plugin act as another principal
would reopen the spoofing class of bug ADR-0014 closed.

This is a governance-correctness boundary, not a security sandbox: Python
has no true encapsulation, so code that deliberately reaches past a view's
private `_kb` reference is not stopped by this module. See ADR-0015
Consequences for the full statement of what this does and doesn't defend
against.
"""

from datetime import datetime

from ontolith.core import Assertion, Entity
from ontolith.govern.policy import Decision
from ontolith.govern.proposal import Proposal
from ontolith.ontology import AsOfView, Ontology
from ontolith.query import QueryBuilder
from ontolith.schema import SchemaIR


class ReadOnlyView:
    """Read-only facade over Ontology, scoped to a single principal id."""

    def __init__(self, kb: Ontology, principal_id: str) -> None:
        self._kb = kb
        self._principal_id = principal_id

    @property
    def principal_id(self) -> str:
        """The principal id this view is bound to."""
        return self._principal_id

    def get_entity(self, entity_id: str) -> Entity | None:
        """Retrieve an entity by ID."""
        return self._kb.get_entity(entity_id)

    def schema(self) -> SchemaIR | None:
        """The current schema for this view's namespace, or `None` if no
        schema has been applied yet.

        Added for the RDF/OWL exporter (SPEC §13.3), the first reference
        plugin needing schema access rather than just entity/assertion
        data — schema is namespace-scoped, non-sensitive metadata (no
        principal-specific governance concern the way write access is),
        so this is a plain passthrough with no capability narrowing.
        Delegates to `Ontology.schema()`, not `self._kb.backend` directly —
        every other method on this view delegates to an `Ontology` method
        too (this module's own docstring calls it "a safe method subset of
        Ontology"); reaching past that to the storage port would have been
        the only exception.
        """
        return self._kb.schema()

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        status: str | None = "active",
    ) -> list[Assertion]:
        """Query assertions."""
        return self._kb.assertions(subject=subject, predicate=predicate, status=status)

    def query(self, concept: str) -> QueryBuilder:
        """Create a query builder for a concept."""
        return self._kb.query(concept)

    def as_of(self, t: datetime | str) -> AsOfView:
        """Return a read-only bitemporal view at time t (SPEC §11.4)."""
        return self._kb.as_of(t)


class WriteView(ReadOnlyView):
    """Governed-write facade over Ontology, scoped to a single principal id.

    Adds create_entity/propose/propose_ref/retract — never the direct-write
    bypass (assert_literal/assert_ref). `author` is always the view's own
    principal id. propose/propose_ref/retract go through Ontology's SPEC §10
    conflict routing and policy evaluation; create_entity is capability-gated
    (`Ontology.create_entity` requires `>= propose`) but is NOT itself SPEC
    §10 governed — entities are structural records, not assertions. For an
    `ai`-kind principal `Ontology.create_entity` raises (ADR-0055); the view
    does not offer `propose_batch`, so such a plugin cannot create entities.
    """

    def create_entity(self, concept: str, natural_key: str | None = None) -> Entity:
        """Create a new entity, authored by this view's principal."""
        return self._kb.create_entity(concept, author=self._principal_id, natural_key=natural_key)

    def propose(
        self,
        subject: str,
        predicate: str,
        value: str,
        value_type: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a literal assertion through the proposal/policy path (SPEC §9)."""
        return self._kb.propose(
            subject,
            predicate,
            value,
            value_type,
            self._principal_id,
            confidence=confidence,
            source=source,
            rationale=rationale,
        )

    def propose_ref(
        self,
        subject: str,
        predicate: str,
        target: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a reference (relation) assertion through the proposal/policy path (SPEC §9)."""
        return self._kb.propose_ref(
            subject,
            predicate,
            target,
            self._principal_id,
            confidence=confidence,
            source=source,
            rationale=rationale,
        )

    def retract(self, assertion_id: str) -> tuple[Proposal, Decision]:
        """Propose retraction of an assertion through the policy path (SPEC §9)."""
        return self._kb.retract(assertion_id, self._principal_id)


__all__ = ["ReadOnlyView", "WriteView"]
