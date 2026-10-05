"""Ontology - main entry point for the knowledge base.

The Ontology class is the primary API surface for users. It wraps the storage
backend and provides high-level methods for entities, assertions, and queries.
"""

import json
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from ontolith.core import (
    Assertion,
    AssertionEvent,
    Clock,
    Embedder,
    Entity,
    HashingEmbedder,
    IdProvider,
    Namespace,
    ObservabilitySink,
    StdlibLoggingSink,
    SystemClock,
    UlidProvider,
)
from ontolith.core.errors import (
    AuthError,
    CapabilityError,
    NotFoundError,
    SchemaError,
    ValidationError,
)
from ontolith.govern import AutoAccept, ThresholdPolicy
from ontolith.govern.conflict import ConflictResult, Contradict, Supersede, route
from ontolith.govern.contradiction import Contradiction, safe_rationale_history
from ontolith.govern.policy import Decision, PolicyStrategy, Reject, RequireReview
from ontolith.govern.proposal import Proposal, ProposalEvent
from ontolith.govern.provenance import Provenance
from ontolith.identity import AdminEvent, Principal, PrincipalCredential, min_capability
from ontolith.identity.admin_event import AdminAction
from ontolith.query import QueryBuilder
from ontolith.schema import SchemaIR
from ontolith.store.base import DEFAULT_NAMESPACE, StorageBackend

if TYPE_CHECKING:
    from ontolith.plugins.ports import Validator


# KI-049: ASCII-only, no whitespace/underscore-separator/unicode-digit
# leniency - int()/float() alone accept PEP-515 underscores, surrounding
# whitespace, and (for int()) non-ASCII decimal digits, none of which the
# KI-039 SQL CAST/TRY_CAST paths this validation exists to back up agree on
# across backends (verified empirically during review: e.g. SQLite casts
# "5_000" to 0, DuckDB to 5000). float() additionally accepts "inf"/"nan",
# neither a JSON- or SQL-numeric-cast-compatible value.
_INTEGER_RE = re.compile(r"[+-]?[0-9]+")
_FLOAT_RE = re.compile(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?")

# Cap how much of an oversized literal a ValidationError message repeats
# back - a rejected multi-MB blob shouldn't be echoed in full into an
# exception surfaced through REST/CLI.
_MAX_VALUE_IN_ERROR = 200


def _value_for_error(value: str) -> str:
    """`repr()` of `value`, truncated so an oversized literal doesn't blow
    up a `ValidationError` message (KI-049 review)."""
    if len(value) <= _MAX_VALUE_IN_ERROR:
        return repr(value)
    return f"{value[:_MAX_VALUE_IN_ERROR]!r}... ({len(value)} chars total)"


def _reject_json_constant(token: str) -> NoReturn:
    """`json.loads(..., parse_constant=)` hook rejecting Python's
    non-standard `NaN`/`Infinity`/`-Infinity` extensions (KI-049 review) -
    not valid RFC 8259 JSON, so not well-formed content for `value_type="JSON"`
    even though the stdlib parser accepts them by default."""
    raise ValueError(f"{token!r} is not valid JSON (RFC 8259 has no NaN/Infinity)")


class AsOfView:
    """Read-only bitemporal view at a specific point in time (SPEC §11.4).

    Reconstructs what was known and true at time `t`:
        valid_from <= t < (valid_to or ∞)  AND  asserted_at <= t

    Status is not used as a positive filter — the temporal dimensions
    determine visibility — but two statuses are still excluded by default,
    with the same opt-ins default (non-as_of) queries use, even though the
    underlying mechanism differs: 'flagged' assertions (disputed, not
    confirmed-valid; reconstructed point-in-time from the assertion_event
    log, not by current status, KI-097 — pass ``include_flagged=True`` for
    explicit audit/history views) and, since ADR-0049 (KI-095), a
    'retracted' assertion once its own retraction event's assertion-time
    has passed (opt back into seeing it via ``.query()``'s chained
    ``.include_history()`` builder method, or via ``.assertions()``'s own
    ``include_history=True`` keyword since KI-098 closed the asymmetry
    where only ``.query()`` had this opt-out).
    """

    def __init__(
        self,
        backend: "StorageBackend",
        as_of: datetime,
        namespace: str,
        embedder: Embedder | None = None,
    ) -> None:
        self._backend = backend
        self._as_of = as_of
        self._namespace = namespace
        self._embedder = embedder

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        *,
        include_flagged: bool = False,
        include_history: bool = False,
    ) -> list[Assertion]:
        """Assertions visible at the as_of timestamp."""
        return self._backend.assertions(
            subject=subject,
            predicate=predicate,
            status=None,
            as_of_time=self._as_of,
            include_flagged=include_flagged,
            include_history=include_history,
        )

    def query(self, concept: str) -> QueryBuilder:
        """Query entities as they existed at the as_of timestamp."""
        return QueryBuilder(
            backend=self._backend,
            namespace=self._namespace,
            concept=concept,
            as_of_time=self._as_of,
            embedder=self._embedder,
        )

    def schema(self) -> SchemaIR | None:
        """The schema version effective at the as_of timestamp (KI-019).

        Resolves via `StorageBackend.get_schema_at`, not `get_schema` (which
        always returns the latest version) — reconstructing what a
        property's temporality/cardinality meant at this point in time
        requires the schema that was actually in force then, not today's.

        Returns:
            Schema effective at this view's as_of time, or None if no
            version of this namespace's schema had been applied yet.
        """
        return self._backend.get_schema_at(self._namespace, self._as_of)


class Ontology:
    """Main knowledge base interface.

    This is the primary entry point for interacting with an Ontolith knowledge base.
    It provides methods for creating entities, making assertions, and querying.

    Example:
        >>> kb = Ontology.connect("my-kb.db")
        >>> alice = kb.create_principal(
        ...     "alice@example.com", kind="human", default_capability="write"
        ... )
        >>> entity = kb.create_entity("Person", author=alice.id)
        >>> assertion = kb.assert_literal(
        ...     entity.id, "Person.name", "Ada Lovelace", "Text", author=alice.id
        ... )
    """

    def __init__(
        self,
        backend: StorageBackend,
        clock: Clock | None = None,
        id_provider: IdProvider | None = None,
        policy: PolicyStrategy | None = None,
        embedder: Embedder | None = None,
        validators: Sequence["Validator"] | None = None,
        completeness_validators: Sequence["Validator"] | None = None,
        observability: ObservabilitySink | None = None,
    ) -> None:
        """Initialize Ontology with a storage backend.

        Args:
            backend: Storage backend implementation
            clock: Clock for deterministic timestamps (defaults to SystemClock)
            id_provider: ID provider for deterministic IDs (defaults to UlidProvider)
            policy: Policy strategy for proposal evaluation (defaults to
                ThresholdPolicy — ADR-0018). ADR-0006 names PolicyStrategy
                as an open-core extension point for proprietary strategies.
            embedder: Embedder for .semantic() queries and reindex() (defaults
                to HashingEmbedder — ADR-0020).
            observability: Sink for structured logs/events/metrics (SPEC §18,
                ADR-0044; defaults to `StdlibLoggingSink`, itself a
                production-safe default the same way `clock`/`id_provider`
                default to concrete, always-safe implementations — pass
                `NullObservabilitySink()` for explicit silence, or a
                `RecordingObservabilitySink()` in tests that assert on what
                was emitted).
            validators: Per-assertion Validators (SPEC §13.2, KI-042,
                ADR-0029), run synchronously and blocking at every point an
                assertion actually commits — assert_literal, assert_ref,
                propose/propose_ref's auto-accept path, and
                accept_proposal/resubmit's replay of a reviewed proposal's
                operations. A failing Validator raises ValidationError and
                aborts the write. Each Validator receives the `Ontology`
                itself as `kb` (trusted the same way `policy` is — see
                `ValidatorKbView`), not a capability-scoped view. Not
                suitable for whole-entity-completeness checks (e.g.
                `RequiredFieldsValidator`) — use `completeness_validators`
                for those (ADR-0029's Rationale explains why).
            completeness_validators: Validators run once per distinct
                subject touched by an accepted proposal's operations, after
                all of that proposal's writes have landed inside the same
                transaction (`accept_proposal` only — never direct writes
                or propose/propose_ref's auto-accept path, ADR-0029). This
                is the shape whole-entity-completeness checks like
                `RequiredFieldsValidator` need: an entity is incomplete by
                construction after every write but its last, so it cannot
                be gated per-assertion.
        """
        self.backend = backend
        self.clock = clock or SystemClock()
        self.id_provider = id_provider or UlidProvider()
        self.policy = policy or ThresholdPolicy()
        self.embedder = embedder or HashingEmbedder()
        self.observability = observability or StdlibLoggingSink()
        self.namespace = DEFAULT_NAMESPACE  # For M1, single namespace
        self.validators: Sequence[Validator] = list(validators) if validators else []
        self.completeness_validators: Sequence[Validator] = (
            list(completeness_validators) if completeness_validators else []
        )

    @classmethod
    def connect(
        cls,
        path: str | Path,
        *,
        clock: Clock | None = None,
        id_provider: IdProvider | None = None,
        policy: PolicyStrategy | None = None,
        embedder: Embedder | None = None,
        validators: Sequence["Validator"] | None = None,
        completeness_validators: Sequence["Validator"] | None = None,
        observability: ObservabilitySink | None = None,
    ) -> "Ontology":
        """Connect to a knowledge base.

        Args:
            path: Path to SQLite database file
            clock: Optional clock for deterministic behavior
            id_provider: Optional ID provider for deterministic behavior
            policy: Optional policy strategy (defaults to ThresholdPolicy —
                ADR-0018)
            embedder: Optional Embedder (defaults to HashingEmbedder)
            validators: Per-assertion Validators — see `__init__` (KI-042,
                ADR-0029)
            completeness_validators: Whole-entity-completeness Validators —
                see `__init__` (KI-041, ADR-0029)
            observability: Optional ObservabilitySink — see `__init__`
                (SPEC §18, ADR-0044)

        Returns:
            Ontology instance connected to the database
        """
        from ontolith.store.sqlite import SQLiteBackend

        effective_clock = clock or SystemClock()
        backend = SQLiteBackend(path, clock=effective_clock)
        return cls(
            backend,
            clock=effective_clock,
            id_provider=id_provider,
            policy=policy,
            embedder=embedder,
            validators=validators,
            completeness_validators=completeness_validators,
            observability=observability,
        )

    def create_principal(
        self,
        principal_id: str,
        kind: str,
        auth_method: str = "oidc",
        *,
        owner: str | None = None,
        default_capability: str = "propose",
        trust_level: int = 0,
        metadata: dict[str, Any] | None = None,
        author: str | None = None,
    ) -> Principal:
        """Create a new principal.

        Args:
            principal_id: Email (human) or slug (ai/service)
            kind: Type of principal (human, ai, service)
            auth_method: Authentication method (oidc, workload, apikey)
            owner: Required for AI principals - accountable human/team
            default_capability: Default permission level
            trust_level: Base trust score
            metadata: Optional metadata
            author: Principal ID of the admin creating this principal
                (KI-060) — purely for `AdminEvent` attribution, NOT a
                capability gate. This method still has no built-in
                capability check by design (ADR-0022) — external callers
                (REST, CLI) gate it themselves via `require_admin` before
                calling, and are expected to pass that same admin id here
                too. `None` records no event, e.g. the bootstrap case
                where no admin exists yet to attribute to.

        Returns:
            Created principal

        Note:
            Opens its own `self.backend.transaction()` (to keep the
            principal write and its `AdminEvent` atomic) — cannot be
            called from inside an already-open transaction (e.g. a caller
            wrapping this in its own `with kb.backend.transaction():`
            block), same constraint every other transaction-wrapped
            `Ontology` write method already has.

        Raises:
            ValidationError: kind is "ai" and owner is missing, or doesn't
                name an existing human/service principal (SPEC §8.1: AI
                principals must declare a *resolvable* accountable owner,
                not just a non-null string)
        """
        if kind == "ai" and owner is None:
            # Principal's own model_validator also enforces this, but
            # raises pydantic's ValidationError, not this method's
            # documented ontolith ValidationError - checked explicitly
            # here so every caller (including REST, which maps error
            # *types* to HTTP statuses) sees one consistent exception.
            raise ValidationError("AI principals must have an owner (SPEC §8.1)")

        principal = Principal(
            id=principal_id,
            kind=kind,  # type: ignore
            owner=owner,
            auth_method=auth_method,  # type: ignore
            default_capability=default_capability,  # type: ignore
            trust_level=trust_level,
            created_at=self.clock.now(),
            metadata=metadata or {},
        )

        if principal.kind == "ai":
            assert principal.owner is not None  # enforced by Principal's model_validator
            owner_principal = self.backend.get_principal(principal.owner)
            if owner_principal is None:
                raise ValidationError(f"AI principal owner not found: {principal.owner!r}")
            if owner_principal.kind == "ai":
                raise ValidationError(
                    f"AI principal owner must be human or service, not ai: {principal.owner!r}"
                )

        with self.backend.transaction():
            self.backend.put_principal(principal)
            if author is not None:
                self.record_admin_event(author, "create_principal", principal_id)
        return principal

    def get_principal(self, principal_id: str) -> Principal | None:
        """Retrieve a principal by ID.

        Args:
            principal_id: Principal ID

        Returns:
            Principal if found, None otherwise
        """
        return self.backend.get_principal(principal_id)

    def create_entity(
        self,
        concept: str,
        author: str,
        natural_key: str | None = None,
    ) -> Entity:
        """Create a new entity.

        Args:
            concept: Concept name (e.g., "Person", "Organization")
            author: Principal ID creating this entity
            natural_key: Optional unique key within concept

        Returns:
            Created entity

        Raises:
            AuthError: author is not a known principal
            CapabilityError: author's capability is 'read'
            ValidationError: concept is not declared in the active schema
                (KI-090), or natural_key is already taken within concept
                (KI-091)

        Note:
            Opens its own `self.backend.transaction()` (KI-092 — to keep
            the natural-key uniqueness check and the write it guards in one
            transaction, so `begin()`'s `BEGIN IMMEDIATE` serializes a
            concurrent writer between them) — cannot be called from inside
            an already-open transaction, same constraint every other
            transaction-wrapped `Ontology` write method already has.
        """
        principal = self.backend.get_principal(author)
        if principal is None:
            raise AuthError(f"Principal not found: {author}")
        if principal.default_capability == "read":
            raise CapabilityError(f"Principal {author!r} lacks propose capability")
        self._require_known_concept(concept)

        # KI-092: the natural-key uniqueness check and the write it guards
        # must share one transaction, so `begin()`'s `BEGIN IMMEDIATE`
        # (KI-084) serializes a concurrent writer between them — otherwise
        # two writers could both pass the check and both reach
        # `put_entity`, and the loser hits the `UNIQUE` constraint as a
        # redacted `StorageError` (the exact error KI-091 was filed to
        # eliminate for the non-concurrent case). `_require_known_concept`
        # above stays outside as an optimistic fast-fail, matching
        # `assert_literal`'s own precedent for schema validation — a
        # concurrent schema change is a different, out-of-scope race. The
        # entity (and its `id_provider`/`clock` draws) is built inside the
        # block so a rejected duplicate consumes neither.
        with self.backend.transaction():
            self._require_unique_natural_key(concept, natural_key)
            entity = Entity(
                id=self.id_provider.next(),
                namespace=self.namespace,
                concept=concept,
                natural_key=natural_key,
                created_at=self.clock.now(),
                created_by=author,
            )
            self.backend.put_entity(entity)
        return entity

    def _get_principal_or_raise(self, principal_id: str) -> Principal:
        """Look up `principal_id`, raising AuthError if unknown."""
        principal = self.backend.get_principal(principal_id)
        if principal is None:
            raise AuthError(f"Principal not found: {principal_id}")
        return principal

    def _resolve_delegation(
        self, principal: Principal, author: str, acting_as: str | None
    ) -> Principal | None:
        """Resolve and authorize the delegating principal for `acting_as` (ADR-0003).

        Returns None when `acting_as` is absent or equals `author` (no
        delegation). `author` must be owned by `acting_as` to delegate.

        Raises:
            AuthError: acting_as is not a known principal
            CapabilityError: author is not owned by acting_as
        """
        if acting_as is None or acting_as == author:
            return None
        delegating = self.backend.get_principal(acting_as)
        if delegating is None:
            raise AuthError(f"Delegating principal not found: {acting_as}")
        if principal.owner != acting_as:
            raise CapabilityError(f"Principal {author!r} is not authorized to act as {acting_as!r}")
        return delegating

    def _finalize_non_accepted_decision(
        self, proposal: Proposal, decision: Decision, now: datetime, *, is_new: bool = True
    ) -> tuple[Proposal, Decision] | None:
        """Persist and return the Reject/require-review outcome shared by
        propose/propose_ref/retract/resubmit. Returns None for AutoAccept,
        signaling the caller must still apply the operation-specific side
        effects.

        `is_new` selects how `proposal` is persisted: INSERT
        (`put_proposal`) for a proposal not yet written (propose/
        propose_ref/retract), or UPDATE (`update_proposal_state`) for an
        existing row being re-decided (`resubmit`, KI-027) — `put_proposal`
        would raise on the row's already-existing id. `update_proposal_state`
        clears the row's `decided_at` unconditionally (it is not
        COALESCE'd like `policy_reason`), so persistence is correct either
        way; callers passing `is_new=False` must still reset `proposal.
        decided_at` to None on their own in-memory copy before calling, or
        the object this method *returns* will disagree with the row it just
        wrote (a resubmitted proposal that lands back in require_review is,
        again, not yet decided).
        """
        if isinstance(decision, Reject):
            rejected = proposal.model_copy(
                update={"state": "rejected", "decided_at": now, "policy_reason": decision.reason}
            )
            if is_new:
                self.backend.put_proposal(rejected)
            else:
                self.backend.update_proposal_state(
                    proposal.id, "rejected", now.isoformat(), decision.reason
                )
            return rejected, decision

        if not isinstance(decision, AutoAccept):
            # KI-078: RequireReview.reviewers was computed by every
            # PolicyStrategy but never persisted anywhere before this -
            # getattr defensively, matching the same "decision might not
            # literally be RequireReview" caution `reason` already uses
            # here (a custom third-party Decision subclass isn't ruled out
            # structurally, only by the Reject/AutoAccept checks above).
            # isinstance-checked, not `or []`: a truthy non-list (e.g. a
            # custom subclass whose `.reviewers` is a bare string) would
            # otherwise sail through `list(...)` and silently explode into
            # one "reviewer" per character instead of failing safe. Non-str
            # *elements* are filtered the same way, not just rejected
            # wholesale: `model_copy(update=...)` below bypasses pydantic
            # validation, so a non-str element would otherwise reach
            # `put_proposal` unnoticed and only fail later, on *read back*
            # (`_row_to_proposal`'s `Proposal(...)` does validate) - by then
            # corrupting every subsequent read of the row, and any
            # `proposals()` listing that includes it, not just this one
            # decision's own result (found in review, worse than the
            # bare-string case this guard was first added for).
            raw_reviewers = getattr(decision, "reviewers", None)
            reviewers = (
                [r for r in raw_reviewers if isinstance(r, str)]
                if isinstance(raw_reviewers, list)
                else []
            )
            pending = proposal.model_copy(
                update={
                    "state": "require_review",
                    "policy_reason": getattr(decision, "reason", None),
                    "reviewers": reviewers,
                }
            )
            if is_new:
                self.backend.put_proposal(pending)
            else:
                self.backend.update_proposal_state(
                    proposal.id, "require_review", policy_reason=getattr(decision, "reason", None)
                )
                # Resubmission re-evaluates policy against a live kb - the
                # freshly-computed reviewers may differ from whatever was
                # assigned before, so they're refreshed here too rather than
                # left stale from the proposal's original creation.
                self.backend.update_proposal_reviewers(proposal.id, reviewers)
            return pending, decision

        return None

    def _check_direct_write_capability(
        self, author: str, acting_as: str | None
    ) -> tuple[Principal, Principal | None]:
        """Shared auth/capability gate for assert_literal/assert_ref (SPEC §9.3).

        A principal with `write` (or `admin`) capability MAY bypass proposals,
        but direct writes still pass through conflict routing (§10) and
        provenance is still recorded. AI-kind principals are never permitted
        this path, even if misconfigured with elevated capability — under the
        default `ThresholdPolicy`, AI proposals always require review
        (ADR-0003); direct writes skip review entirely, so this structural
        block holds regardless of which `PolicyStrategy` a deployment
        installs (KI-061, ADR-0040).

        Returns:
            (author principal, delegating principal or None)

        Raises:
            AuthError: author or acting_as is not a known principal
            CapabilityError: author is AI-kind, delegation is unauthorized, or
                effective capability is below `write`
        """
        principal = self._get_principal_or_raise(author)
        if principal.kind == "ai":
            raise CapabilityError(f"AI principal {author!r} cannot make direct writes")

        delegating = self._resolve_delegation(principal, author, acting_as)

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution.
        capability: str = principal.default_capability
        if delegating is not None:
            capability = min_capability(capability, delegating.default_capability)
        if capability not in ("write", "admin"):
            raise CapabilityError(f"Principal {author!r} lacks write capability")

        return principal, delegating

    def _resolve_temporality(self, predicate: str) -> Literal["static", "time_varying"]:
        """Look up a predicate's temporality, defaulting to static (SPEC §10.1)."""
        schema = self.backend.get_schema(self.namespace)
        return schema.temporality_of(predicate) if schema is not None else "static"

    def _resolve_cardinality(self, predicate: str) -> Literal["single", "many"]:
        """Look up a predicate's cardinality, defaulting to single (ADR-0017)."""
        schema = self.backend.get_schema(self.namespace)
        return schema.cardinality_of(predicate) if schema is not None else "single"

    def _require_known_predicate(
        self,
        predicate: str,
        value_type: str | None = None,
        value: str | None = None,
        *,
        expected_kind: Literal["property", "relation"],
    ) -> None:
        """Reject an unknown predicate at write time (SPEC §4) rather than
        silently defaulting its temporality/cardinality to static/single.

        When `value_type` is given (literal write paths only), also reject a
        mismatch against the schema-declared `PropertyDef.value_type`
        (KI-031) — e.g. writing `value_type="Text"` against a predicate
        declared `Integer`. When `value` is *also* given, further reject
        content that doesn't actually parse as that declared type (KI-049)
        — e.g. `value_type="Integer"` matching the schema, but
        `value="unknown"` not being a valid integer — via
        `_validate_literal_value`. `value` is accepted separately from
        `value_type` (rather than inferring "validate content" from
        `value_type` alone) so a caller can still run the token-mismatch
        check without the content check when it has no `value` to check
        against (there are none today, but this keeps the two concerns
        independently triggerable rather than accidentally coupled).

        `expected_kind` (required, KI-040) rejects a predicate-kind
        mismatch — a literal write (`assert_literal`/`propose`,
        `expected_kind="property"`) against a schema-declared relation
        predicate, or a ref write (`assert_ref`/`propose_ref`,
        `expected_kind="relation"`) against a schema-declared property
        predicate. Required rather than defaulted to `None`, and kept
        independent of `value_type` rather than inferred from whether it's
        set, so a future fifth write path can't silently skip the kind
        check by omitting the keyword — `value_type` is only ever set by
        the two literal-write call sites and never by the two ref-write
        ones, so the two parameters happen to correlate today, but they
        answer different questions (what shape is the value vs. what kind
        is the predicate) and a required, explicit `expected_kind` keeps
        that true by construction, not by accident.

        No-op when no schema is registered for the namespace yet — a
        schema-less namespace has nothing to validate a predicate against.
        """
        schema = self.backend.get_schema(self.namespace)
        if schema is None:
            return
        if not schema.has_predicate(predicate):
            raise ValidationError(
                f"Unknown predicate {predicate!r}: not declared in schema "
                f"{schema.namespace!r} version {schema.version}"
            )
        if value_type is not None:
            declared = schema.value_type_of(predicate)
            if declared is not None and declared != value_type:
                raise ValidationError(
                    f"Predicate {predicate!r} is declared value_type={declared!r} in "
                    f"schema {schema.namespace!r} version {schema.version}, but this "
                    f"write supplies value_type={value_type!r}"
                )
            if declared is not None and value is not None:
                self._validate_literal_value(value, declared)
        actual_kind = schema.kind_of(predicate)
        if actual_kind is not None and actual_kind != expected_kind:
            wrong_call = (
                "assert_literal/propose"
                if expected_kind == "property"
                else "assert_ref/propose_ref"
            )
            right_call = (
                "assert_ref/propose_ref"
                if expected_kind == "property"
                else "assert_literal/propose"
            )
            raise ValidationError(
                f"Predicate {predicate!r} is declared a {actual_kind} in schema "
                f"{schema.namespace!r} version {schema.version}, but {wrong_call} "
                f"asserts a {expected_kind}. Use {right_call} instead."
            )

    def _require_known_concept(self, concept: str) -> None:
        """Reject an unknown `concept` at entity-creation time (SPEC §4,
        KI-090), mirroring `_require_known_predicate`'s identical precedent
        for `predicate` — rather than silently persisting an entity under a
        concept the active schema never declared.

        No-op when no schema is registered for the namespace yet — same
        rationale as `_require_known_predicate`: a schema-less namespace has
        nothing to validate a concept against.
        """
        schema = self.backend.get_schema(self.namespace)
        if schema is None:
            return
        if not schema.has_concept(concept):
            raise ValidationError(
                f"Unknown concept {concept!r}: not declared in schema "
                f"{schema.namespace!r} version {schema.version}"
            )

    def _require_unique_natural_key(self, concept: str, natural_key: str | None) -> None:
        """Reject a `natural_key` already taken within `concept` before an
        entity is created (KI-091), rather than relying on the `entity`
        table's `UNIQUE(namespace, concept, natural_key)` constraint to fail
        late into a generic, redacted `StorageError` that discards the
        backend's own already-clear conflict message.

        No-op when `natural_key` is `None` — `NULL` is exempt from the
        `UNIQUE` constraint on both backends (any number of entities may
        share `natural_key=None` within a concept), so there's nothing to
        check.

        `create_entity` calls this *inside* its `with
        self.backend.transaction():` block (KI-092), so a concurrent writer
        is serialized behind it rather than able to commit a duplicate
        between this check and the `put_entity` that follows: the RLock
        (KI-023) is held across the whole block on either backend, and on
        SQLite `begin()`'s `BEGIN IMMEDIATE` (KI-084) additionally claims
        the write lock against a second OS process. The DB's `UNIQUE`
        constraint is still there as a backstop, but the transaction means
        a caller now reliably gets this named `ValidationError`, not the
        redacted `StorageError` the bare constraint failure produces.
        """
        if natural_key is None:
            return
        existing = self.backend.get_entity_by_natural_key(self.namespace, concept, natural_key)
        if existing is not None:
            raise ValidationError(
                f"Entity conflict: {concept!r} with natural_key={natural_key!r} "
                f"already exists in namespace {self.namespace!r} (id={existing.id!r})"
            )

    def _require_existing_subject(self, subject: str) -> None:
        """Reject an unknown `subject` entity id before a write reaches the
        backend (KI-083), rather than relying on the DB's `FOREIGN KEY`
        constraint to fail late with a generic, redacted `StorageError` that
        misdescribes a missing row as a "conflict".

        Called from all four write paths that take a caller-supplied
        `subject` (`assert_literal`/`assert_ref`/`propose`/`propose_ref`).
        For `propose`/`propose_ref` this runs before the proposal is even
        constructed. Checking here, once, at submission time is enough:
        no `StorageBackend` method or interface exposes entity deletion
        today, so a subject validated now can't later become invalid by
        the time a `require_review` proposal is eventually accepted. That
        permanence is an emergent property of the current implementation,
        not a SPEC guarantee — SPEC §5's append-only invariant is scoped to
        assertions, not entities (unlike `retract()`'s own analogous
        existence check on `assertion_id`, which correctly cites it).

        Only `subject` is checked here — `assert_ref`/`propose_ref` also
        call `_require_existing_target` for a ref assertion's `target`
        (KI-089). Kept as a separate method, not a second call to this one
        with a renamed parameter, so each raises a message naming *which*
        endpoint is missing ("Subject not found" vs "Target not found") —
        matching the codebase's own `"<kind> not found: <id>"` convention
        ("Assertion not found: ...", "Proposal not found: ...") rather than
        a single ambiguous "Entity not found" a caller couldn't attribute
        to either id. The two also, historically, landed in separate KIs
        (KI-083 then KI-089): `subject` at least had a `FOREIGN KEY` to
        fail on before this check existed; `target` (the `assertion`
        table's `value_ref` column — `value_lit` holds literal values
        instead, `value_kind` picks between them) has none on either
        backend, so an unknown target didn't fail at all before KI-089,
        not even with a confusing error.
        """
        if self.get_entity(subject) is None:
            raise NotFoundError(f"Subject not found: {subject!r}")

    def _require_existing_target(self, target: str) -> None:
        """Reject an unknown `target` entity id before a ref assertion
        write reaches the backend (KI-089) — mirrors
        `_require_existing_subject` (KI-083), but for a relation's *other*
        endpoint. Unlike `subject`, `target` (the `assertion` table's
        `value_ref` column) has no `FOREIGN KEY` on either backend, so
        without this check an unknown target doesn't fail at all: the
        write silently succeeds and the KB ends up with a dangling
        reference to an entity that was never created.

        Called only from `assert_ref`/`propose_ref` — the two literal-only
        write paths (`assert_literal`/`propose`) have no `target`.
        """
        if self.get_entity(target) is None:
            raise NotFoundError(f"Target not found: {target!r}")

    def _require_valid_supersedes_hint(
        self,
        supersedes: str | None,
        predicate: str,
        temporality: Literal["static", "time_varying"],
    ) -> None:
        """Reject a caller-supplied `supersedes` hint (ADR-0050) up front,
        at submission time, when it plainly can't apply — mirrors
        `_require_existing_subject`/`_require_existing_target`'s "fail
        loud before the write reaches routing" convention.

        `supersedes` is only meaningful for a cardinality="many"
        time_varying property (ADR-0050): every other combination already
        routes unambiguously without a hint (single: the one differing
        overlapping value always supersedes; static: `cardinality="many"`
        always coexists, `cardinality="single"` always contradicts), so a
        caller supplying one elsewhere almost certainly misunderstands
        what it does — reject rather than silently ignore it.

        Deliberately does NOT check that `supersedes` names a real,
        active, overlapping-and-differing assertion — `route()` does that
        itself (ValueError, translated to ValidationError in
        `_apply_with_conflict_routing`) against a fresh read of `existing`,
        since for `propose()`/`propose_ref()` this method only runs once,
        at submission time, while `route()` also runs again at
        proposal-replay time against whatever is active then. Called from
        `assert_literal`/`assert_ref`/`propose`/`propose_ref`, all four
        submission-time-only — NOT re-run at proposal-replay time, same
        precedent `_require_known_predicate`'s checks already set
        (`_replay_proposal_operations`'s own docstring): a schema change
        between submission and replay that moves a predicate out of
        cardinality="many"/temporality="time_varying" leaves a
        replay-time `supersedes` hint silently unconsulted by `route()`
        rather than raising late, a narrow, accepted gap matching the
        existing temporality-drift precedent, not a new one.
        """
        if supersedes is None:
            return
        if temporality != "time_varying" or self._resolve_cardinality(predicate) != "many":
            raise ValidationError(
                'supersedes is only meaningful for a cardinality="many", '
                f'temporality="time_varying" property or relation (ADR-0050) — '
                f"{predicate!r} is not one"
            )

    def _validate_literal_value(self, value: str, value_type: str) -> None:
        """Parse `value` against its schema-declared `value_type` and raise
        `ValidationError` if it isn't well-formed content for that type
        (KI-049, SPEC §4). Only called from `_require_known_predicate` once
        `value_type` is already confirmed to match the schema's own
        declaration for the predicate — `value_type` here is always one of
        SPEC §4's closed eight (`PropertyDef.value_type` is a `Literal`),
        never an arbitrary caller-supplied string.

        `Text` has no format to validate — any string is well-formed Text,
        so it falls through every branch below as a no-op.

        Enforcement is submission-time only, exactly like the `value_type`
        token check it sits beside — not retroactive against already-stored
        data (no migration mechanism exists, KI-048) and not re-run at
        proposal replay time (`_replay_proposal_operations`), matching
        `_require_known_predicate`'s own established, documented precedent
        for its other checks.
        """
        if value_type == "Integer":
            if not _INTEGER_RE.fullmatch(value):
                raise ValidationError(f"Value {_value_for_error(value)} is not a valid Integer")
        elif value_type == "Float":
            if not _FLOAT_RE.fullmatch(value):
                raise ValidationError(f"Value {_value_for_error(value)} is not a valid Float")
        elif value_type == "Boolean":
            # Case-insensitive "true"/"false" only - not "1"/"0", which
            # would blur the line with Integer (decided explicitly, not
            # the only defensible choice - see KI-049's Fix text). No
            # surrounding-whitespace leniency either (unlike a bare
            # .strip() would give), matching Integer/Float's exact-match
            # regexes - a value with whitespace would round-trip as stored
            # (with the whitespace) but silently fail to match a
            # .where(predicate="true")-style equality filter later
            # (review finding: an internal inconsistency worth avoiding,
            # not a cross-backend divergence like Integer/Float's).
            if value.lower() not in ("true", "false"):
                raise ValidationError(
                    f"Value {_value_for_error(value)} is not a valid Boolean "
                    "(expected 'true' or 'false', case-insensitive)"
                )
        elif value_type == "Date":
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ValidationError(
                    f"Value {_value_for_error(value)} is not a valid Date "
                    "(expected Python's date.fromisoformat grammar, e.g. '2026-01-01')"
                ) from None
        elif value_type == "DateTime":
            try:
                datetime.fromisoformat(value)
            except ValueError:
                raise ValidationError(
                    f"Value {_value_for_error(value)} is not a valid DateTime "
                    "(expected Python's datetime.fromisoformat grammar)"
                ) from None
        elif value_type == "URI":
            # SPEC's URI maps to LinkML's `uriorcurie` (ADR-0013) - both a
            # full URI (scheme:...) and a CURIE (prefix:local-name) are
            # valid, so this only requires a non-empty prefix and a
            # non-empty remainder either side of the first ':', not a
            # strict RFC 3986 scheme.
            prefix, sep, rest = value.partition(":")
            if not sep or not prefix or not rest:
                raise ValidationError(
                    f"Value {_value_for_error(value)} is not a valid URI or CURIE "
                    "(expected 'scheme:...' or 'prefix:local-name')"
                )
        elif value_type == "JSON":
            try:
                json.loads(value, parse_constant=_reject_json_constant)
            except ValueError as exc:
                raise ValidationError(
                    f"Value {_value_for_error(value)} is not valid JSON: {exc}"
                ) from None

    def _run_validators(self, assertion: Assertion) -> None:
        """Run `self.validators` against a single about-to-commit assertion
        (SPEC §13.2, KI-042, ADR-0029) and raise if any reject it.

        Called at every point an assertion actually commits: assert_literal,
        assert_ref, propose/propose_ref's auto-accept path, and
        accept_proposal/resubmit's replay of a reviewed proposal's
        operations — so a registered Validator sees every write regardless
        of which path it took, not just the direct-write ones. Each
        Validator receives `self` as `kb` (see `ValidatorKbView`), so it can
        read current KB state but not write. `assertion` here is always the
        actual about-to-commit assertion (pre-conflict-routing) — contrast
        `_run_completeness_validators`, whose `assertion` argument is only
        a subject stand-in.
        """
        if not self.validators:
            return
        errors = [
            msg for validator in self.validators for msg in validator.validate(assertion, self)
        ]
        if errors:
            raise ValidationError(f"Validator rejected assertion: {'; '.join(errors)}")

    def _run_completeness_validators(self, applied: list[Assertion]) -> None:
        """Run `self.completeness_validators` once per distinct subject
        touched by `applied` (KI-041, ADR-0029), after all of the writes
        that produced them have landed in the same transaction.

        Only called from `accept_proposal` — not `resubmit`'s own
        auto-accept path, which (like `propose`/`propose_ref`'s auto-accept)
        bypasses human review, and not direct writes, which have no
        multi-operation batch boundary to check completeness against.

        Each validator receives one representative `Assertion` per distinct
        subject (the first entry in `applied` for that subject) — only its
        `.subject` is contractually meaningful here; the rest of that
        assertion's fields describe whichever operation happened to be
        first for that subject in this proposal; not the entity's current
        state (query `kb` for that). It may also be a retracted assertion's
        pre-retraction snapshot (see `_replay_proposal_operations`'s
        `retract` branch) — `.status`/`.value` on it reflect neither "what
        just committed" nor "what's now active".
        """
        if not self.completeness_validators:
            return
        by_subject: dict[str, Assertion] = {}
        for assertion in applied:
            by_subject.setdefault(assertion.subject, assertion)
        errors = [
            msg
            for representative in by_subject.values()
            for validator in self.completeness_validators
            for msg in validator.validate(representative, self)
        ]
        if errors:
            raise ValidationError(f"Completeness validator rejected entity: {'; '.join(errors)}")

    def _retraction_valid_to(self, assertion_id: str, now: datetime) -> str | None:
        """Compute valid_to for a retraction.

        Closes an open window at `now`, but never widens a window already
        closed by a prior supersession/retraction — retraction must only
        ever narrow validity, never rewrite history (bitemporal.md #1-2).
        """
        current = self.backend.get_assertion(assertion_id)
        if current is not None and current.valid_to is not None:
            return None
        return now.isoformat()

    @staticmethod
    def _parse_window(op: dict[str, Any], key: str) -> datetime | None:
        """Parse a valid_from/valid_to ISO string back out of a proposal
        operation payload (propose()/propose_ref() serialize datetimes to
        strings since Proposal.payload is a JSON-compatible dict)."""
        raw = op.get(key)
        return datetime.fromisoformat(raw) if raw else None

    def _record_assertion_event(
        self,
        assertion_id: str,
        actor: str,
        action: Literal["superseded", "flagged", "retracted", "reactivated"],
        at: datetime,
        successor_id: str | None = None,
    ) -> None:
        """Append an audit event for an assertion status mutation.

        Must run inside the same transaction as the corresponding
        set_assertion_status call (append-only invariant: the event log and
        the status it describes must never diverge).

        Args:
            successor_id: For action="superseded", the id of the assertion
                that caused it — recovers the full predecessor set when one
                incoming assertion supersedes several at once (KI-008),
                since Assertion.supersedes only records the first.
        """
        self.backend.put_assertion_event(
            AssertionEvent(
                id=self.id_provider.next(),
                assertion_id=assertion_id,
                actor=actor,
                action=action,
                at=at,
                successor_id=successor_id,
            )
        )

    @staticmethod
    def _require_model_for_ai(principal: Principal, model: str | None) -> None:
        """SPEC §7.4/§14.4 MUST: AI-authored assertions carry model provenance."""
        if principal.kind == "ai" and model is None:
            raise ValidationError(
                f"model is required for ai-kind authors (principal: {principal.id})"
            )

    def assert_literal(
        self,
        subject: str,
        predicate: str,
        value: str,
        value_type: str,
        author: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
        acting_as: str | None = None,
        model: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        supersedes: str | None = None,
    ) -> Assertion:
        """Make a literal assertion about an entity, bypassing the proposal queue.

        SPEC §9.3: requires `write` capability (or `admin`); still passes
        through SPEC §10 conflict routing and records full provenance.

        Args:
            subject: Entity ID
            predicate: Property name (e.g., "Person.name")
            value: Literal value
            value_type: Type of literal (Text, Integer, etc.)
            author: Principal ID making this assertion
            confidence: Optional confidence (0.0-1.0)
            source: Optional source of information
            rationale: Optional why this assertion was made
            acting_as: Optional principal ID being acted on behalf of (delegation)
            model: Model family+version (AI principals cannot reach this
                direct-write path — see _check_direct_write_capability — so
                this is accepted but never required here)
            valid_from: When the fact became/becomes true (defaults to now —
                SPEC §5.3). Set explicitly to backfill historical windows,
                e.g. time_varying employment history.
            valid_to: When the fact stopped being true (defaults to open/None)
            supersedes: Id of a specific existing assertion this one
                explicitly replaces (ADR-0050/KI-080). Only meaningful for
                a cardinality="many", temporality="time_varying" property —
                there, an overlapping differing value no longer
                auto-supersedes (it coexists, since window overlap alone
                can't tell "replace my current value" from "a new,
                additional concurrent value" apart); pass this to force
                the specific replacement instead. Every other property
                shape rejects a non-None value outright, since routing is
                already unambiguous there without a hint.

        Returns:
            Assertion as persisted (status/supersedes reflect conflict routing)

        Raises:
            NotFoundError: subject is not a known entity id (KI-083)
            ValidationError: predicate is not declared in the active schema,
                value_type does not match the schema-declared value_type
                for predicate (KI-031), value does not parse as that
                declared value_type (KI-049), predicate is declared a
                relation rather than a property (KI-040), a registered
                `Validator` rejects the assertion (KI-042), supersedes is
                set but predicate isn't cardinality="many"/
                temporality="time_varying" (ADR-0050), or supersedes
                doesn't name a real, active, overlapping-and-differing
                assertion on this (subject, predicate) (ADR-0050)
        """
        self._check_direct_write_capability(author, acting_as)
        self._require_existing_subject(subject)
        self._require_known_predicate(predicate, value_type, value, expected_kind="property")
        temporality = self._resolve_temporality(predicate)
        self._require_valid_supersedes_hint(supersedes, predicate, temporality)

        assertion = Assertion(
            id=self.id_provider.next(),
            namespace=self.namespace,
            subject=subject,
            predicate=predicate,
            value_kind="literal",
            value_type=value_type,
            value=value,
            author=author,
            acting_as=acting_as,
            confidence=confidence,
            source=source,
            rationale=rationale,
            model=model,
            asserted_at=self.clock.now(),
            valid_from=valid_from,
            valid_to=valid_to,
        )

        self._run_validators(assertion)
        with self.backend.transaction():
            return self._apply_with_conflict_routing(assertion, temporality, supersedes)

    def assert_ref(
        self,
        subject: str,
        predicate: str,
        target: str,
        author: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
        acting_as: str | None = None,
        model: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        supersedes: str | None = None,
    ) -> Assertion:
        """Make a reference assertion (relation) between entities, bypassing the
        proposal queue.

        SPEC §9.3: requires `write` capability (or `admin`); still passes
        through SPEC §10 conflict routing and records full provenance.

        Args:
            subject: Source entity ID
            predicate: Relation name (e.g., "Person.employer")
            target: Target entity ID
            author: Principal ID making this assertion
            confidence: Optional confidence (0.0-1.0)
            source: Optional source of information
            rationale: Optional why this assertion was made
            acting_as: Optional principal ID being acted on behalf of (delegation)
            model: Model family+version (AI principals cannot reach this
                direct-write path — see _check_direct_write_capability — so
                this is accepted but never required here)
            valid_from: When the fact became/becomes true (defaults to now —
                SPEC §5.3). Set explicitly to backfill historical windows,
                e.g. time_varying employment history.
            valid_to: When the fact stopped being true (defaults to open/None)
            supersedes: Id of a specific existing assertion this one
                explicitly replaces (ADR-0050/KI-080) — see
                `assert_literal`'s docstring for the full rationale; only
                meaningful for a cardinality="many",
                temporality="time_varying" relation.

        Returns:
            Assertion as persisted (status/supersedes reflect conflict routing)

        Raises:
            NotFoundError: subject or target is not a known entity id
                (KI-083, KI-089)
            ValidationError: predicate is not declared in the active
                schema, predicate is declared a property rather than a
                relation (KI-040), a registered `Validator` rejects the
                assertion (KI-042), supersedes is set but predicate isn't
                cardinality="many"/temporality="time_varying" (ADR-0050),
                or supersedes doesn't name a real, active,
                overlapping-and-differing assertion on this (subject,
                predicate) (ADR-0050)
        """
        self._check_direct_write_capability(author, acting_as)
        self._require_existing_subject(subject)
        self._require_existing_target(target)
        self._require_known_predicate(predicate, expected_kind="relation")
        temporality = self._resolve_temporality(predicate)
        self._require_valid_supersedes_hint(supersedes, predicate, temporality)

        assertion = Assertion(
            id=self.id_provider.next(),
            namespace=self.namespace,
            subject=subject,
            predicate=predicate,
            value_kind="ref",
            value=target,
            author=author,
            acting_as=acting_as,
            confidence=confidence,
            source=source,
            rationale=rationale,
            model=model,
            asserted_at=self.clock.now(),
            valid_from=valid_from,
            valid_to=valid_to,
        )

        self._run_validators(assertion)
        with self.backend.transaction():
            return self._apply_with_conflict_routing(assertion, temporality, supersedes)

    def get_entity(self, entity_id: str) -> Entity | None:
        """Retrieve an entity by ID.

        Args:
            entity_id: Entity ID

        Returns:
            Entity if found, None otherwise
        """
        return self.backend.get_entity(entity_id)

    def schema(self) -> SchemaIR | None:
        """The current schema for this KB's namespace, or `None` if no
        schema has been applied yet.

        Mirrors `AsOfView.schema()` (`as_of()`'s bitemporal read view) but
        for the current, unversioned view — added so `ReadOnlyView.schema()`
        (`plugins/views.py`, ADR-0036) can delegate here instead of reaching
        into `self.backend` directly, keeping it consistent with every
        other `ReadOnlyView` method's own "safe method subset of `Ontology`"
        shape (found in review — it was previously the only view method
        bypassing `Ontology` to reach the storage port).
        """
        return self.backend.get_schema(self.namespace)

    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        status: str | None = "active",
    ) -> list[Assertion]:
        """Query assertions.

        Args:
            subject: Filter by subject entity ID
            predicate: Filter by predicate
            status: Filter by status (default: active only)

        Returns:
            List of matching assertions
        """
        return self.backend.assertions(subject=subject, predicate=predicate, status=status)

    def provenance(self, assertion_id: str) -> Provenance:
        """The full provenance record for a single assertion (SPEC §5.4).

        SPEC §5.4 mandates provenance be retrievable "for any assertion in
        one call" — this is that call. Returns the assertion together with
        every review action on its originating proposal and the full
        predecessor set it superseded (KI-008). REST
        (`GET /provenance/{id}`), GraphQL (`Query.provenance`), and MCP
        (`ontolith.provenance`) all shape this one result into their own
        response type rather than re-deriving the assembly (KI-086, ADR-0047).

        Args:
            assertion_id: Assertion ID to inspect.

        Returns:
            A `Provenance` value object (`assertion`, `review_events`,
            `superseded_ids`).

        Raises:
            NotFoundError: no assertion with that id exists.
        """
        match = self.backend.get_assertion(assertion_id)
        if match is None:
            raise NotFoundError(f"Assertion {assertion_id!r} not found")
        review_events = (
            tuple(self.backend.get_proposal_events(match.proposal_id)) if match.proposal_id else ()
        )
        superseded_ids = tuple(
            e.assertion_id for e in self.backend.get_assertion_events_by_successor(match.id)
        )
        return Provenance(
            assertion=match, review_events=review_events, superseded_ids=superseded_ids
        )

    def query(self, concept: str) -> QueryBuilder:
        """Create a query builder for a concept.

        Args:
            concept: Concept name to query

        Returns:
            QueryBuilder for fluent filtering

        Example:
            >>> kb.query("Person").where(name="Ada Lovelace").all()
        """
        return QueryBuilder(
            backend=self.backend,
            namespace=self.namespace,
            concept=concept,
            embedder=self.embedder,
        )

    def as_of(self, t: datetime | str) -> AsOfView:
        """Return a read-only bitemporal view at time t (SPEC §11.4).

        Reconstructs what was known and true at t:
            valid_from <= t < (valid_to or ∞)  AND  asserted_at <= t

        Args:
            t: Point in time — datetime or ISO-format string. Naive values
                (no tzinfo) are treated as UTC, matching Clock's contract
                that all stored timestamps are UTC — otherwise a naive `t`
                would be compared against UTC-aware stored timestamps as
                a plain ISO string, silently misordering results instead
                of erroring.

        Returns:
            AsOfView for querying the knowledge base as it stood at t
        """
        if isinstance(t, str):
            t = datetime.fromisoformat(t)
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        return AsOfView(self.backend, t, self.namespace, embedder=self.embedder)

    def reindex(self, concept: str | None = None) -> int:
        """Re-embed entities' Text content into the vector index (SPEC §11.3).

        No write path (propose/accept_proposal) auto-embeds on write — this
        is the only way vectors enter the index (ADR-0020 amendment). Safe
        to call repeatedly: each call re-embeds and upserts, so it is
        idempotent and picks up any Text assertions added since the last
        call.

        For each entity, concatenates its Text-typed active-assertion values
        (sorted by predicate then asserted_at) into one string and embeds
        it. Entities with no Text-typed active assertions are skipped, not
        zero-vector-upserted — a zero vector would spuriously rank as
        "close" to other empty entities in `.semantic()` results.

        Args:
            concept: If set, only re-index entities of this concept.
                Otherwise all entities in this namespace.

        Returns:
            Number of entities actually embedded (excludes skipped ones).
        """
        entities = self.backend.entities(namespace=self.namespace, concept=concept)

        indexed_entities = []
        texts = []
        for entity in entities:
            text = self._entity_text(entity)
            if text is None:
                continue
            indexed_entities.append(entity)
            texts.append(text)

        if not texts:
            return 0

        vectors = self.embedder.embed(texts)
        for entity, vector in zip(indexed_entities, vectors, strict=True):
            self.backend.vector_upsert("entity", entity.id, vector)

        return len(indexed_entities)

    def _entity_text(self, entity: Entity) -> str | None:
        """Concatenate an entity's Text-typed active assertion values.

        Returns None if the entity has no Text-typed active assertions.
        """
        text_assertions = [
            a
            for a in self.backend.assertions(subject=entity.id, status="active")
            if a.value_type == "Text"
        ]
        if not text_assertions:
            return None
        text_assertions.sort(key=lambda a: (a.predicate, a.asserted_at))
        return " ".join(a.value for a in text_assertions)

    def propose(
        self,
        subject: str,
        predicate: str,
        value: str,
        value_type: str,
        author: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
        acting_as: str | None = None,
        model: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        supersedes: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a literal assertion through the proposal/policy path (SPEC §9).

        Evaluates ``self.policy`` (``ThresholdPolicy`` by default — ADR-0018).
        Auto-accepted proposals are committed immediately with SPEC §10
        conflict routing; others are stored for review.

        Conflict-routing temporality is resolved from the active schema's
        declared temporality for ``predicate`` (SPEC §10.1: ``t :=
        schema.temporality(P)``) — it is never caller-supplied. Falls back to
        "static" if no schema is registered for this namespace.

        ``model`` (the AI model family+version) is REQUIRED when ``author``
        is an ``ai``-kind principal (SPEC §7.4/§14.4).

        When ``acting_as`` is set the proposal is made on behalf of another
        principal (delegation, ADR-0003). Policy is evaluated using the
        delegating principal's capability and trust level.

        Args:
            valid_from: When the fact became/becomes true (defaults to now —
                SPEC §5.3). Set explicitly to backfill historical windows.
            valid_to: When the fact stopped being true (defaults to open/None)
            supersedes: Id of a specific existing assertion this one
                explicitly replaces (ADR-0050/KI-080) — see
                `assert_literal`'s docstring for the full rationale; only
                meaningful for a cardinality="many",
                temporality="time_varying" property. Checked once at
                submission time, not re-checked at proposal-replay time if
                a `require_review` proposal is later accepted — matching
                this method's own existing schema-drift precedent for
                `temporality` itself (see `_replay_proposal_operations`).

        Returns:
            (Proposal, Decision) tuple

        Raises:
            AuthError: author or acting_as is not a known principal
            CapabilityError: delegation is unauthorized
            NotFoundError: subject is not a known entity id (KI-083)
            ValidationError: author is ai-kind and model is not provided,
                predicate is not declared in the active schema, value_type
                does not match the schema-declared value_type for predicate
                (KI-031), value does not parse as that declared value_type
                (KI-049), predicate is declared a relation rather than a
                property (KI-040), supersedes is set but predicate isn't
                cardinality="many"/temporality="time_varying" (ADR-0050),
                or (on auto-accept) a registered `Validator` rejects the
                assertion (KI-042) or supersedes doesn't name a real,
                active, overlapping-and-differing assertion (ADR-0050)
        """
        principal = self._get_principal_or_raise(author)
        self._require_model_for_ai(principal, model)
        self._require_existing_subject(subject)
        self._require_known_predicate(predicate, value_type, value, expected_kind="property")
        delegating = self._resolve_delegation(principal, author, acting_as)
        temporality = self._resolve_temporality(predicate)
        self._require_valid_supersedes_hint(supersedes, predicate, temporality)

        now = self.clock.now()
        proposal_id = self.id_provider.next()
        proposal = Proposal(
            id=proposal_id,
            namespace=self.namespace,
            author=author,
            acting_as=acting_as,
            state="submitted",
            created_at=now,
            payload={
                "operations": [
                    {
                        "kind": "assert_literal",
                        "subject": subject,
                        "predicate": predicate,
                        "value": value,
                        "value_type": value_type,
                        "temporality": temporality,
                        "confidence": confidence,
                        "source": source,
                        "rationale": rationale,
                        "model": model,
                        "valid_from": valid_from.isoformat() if valid_from else None,
                        "valid_to": valid_to.isoformat() if valid_to else None,
                        "supersedes": supersedes,
                    }
                ]
            },
        )

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution (ADR-0003).
        # kb pinned at `now` (== proposal.created_at): nothing from this
        # proposal is persisted yet, so at evaluation time it can't see its
        # own operation (KI-017, ADR-0025). Replaying as_of(proposal.
        # created_at) later reproduces this same read only if nothing else
        # was committed at exactly that timestamp afterward — asserted_at <=
        # t is inclusive of t, so a same-tick write IS visible on replay.
        kb_view = self.as_of(now)
        decision = self.policy.evaluate(proposal, principal, kb_view, acting_as=delegating)
        finalized = self._finalize_non_accepted_decision(proposal, decision, now)
        if finalized is not None:
            return finalized
        assert isinstance(decision, AutoAccept)

        assertion = Assertion(
            id=self.id_provider.next(),
            namespace=self.namespace,
            subject=subject,
            predicate=predicate,
            value_kind="literal",
            value_type=value_type,
            value=value,
            author=author,
            acting_as=acting_as,
            confidence=confidence,
            source=source,
            rationale=rationale,
            model=model,
            asserted_at=now,
            proposal_id=proposal_id,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self._run_validators(assertion)
        accepted = proposal.model_copy(
            update={"state": "auto_accepted", "decided_at": now, "policy_reason": decision.reason}
        )
        with self.backend.transaction():
            self.backend.put_proposal(accepted)
            self._apply_with_conflict_routing(assertion, temporality, supersedes)
        return accepted, decision

    def propose_ref(
        self,
        subject: str,
        predicate: str,
        target: str,
        author: str,
        *,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
        acting_as: str | None = None,
        model: str | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        supersedes: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Submit a reference (relation) assertion through the proposal/policy path (SPEC §9).

        Mirrors ``propose()`` for relations — the only difference is the
        operation kind and that ``target`` (an entity ID) replaces
        ``value``/``value_type``. Evaluates ``self.policy`` (``ThresholdPolicy``
        by default — ADR-0018); auto-accepted proposals are committed
        immediately with SPEC §10 conflict routing. Conflict-routing
        temporality is resolved from the active schema's declared
        temporality for ``predicate`` (SPEC §10.1), never caller-supplied.

        ``model`` (the AI model family+version) is REQUIRED when ``author``
        is an ``ai``-kind principal (SPEC §7.4/§14.4).

        When ``acting_as`` is set the proposal is made on behalf of another
        principal (delegation, ADR-0003). Policy is evaluated using the
        delegating principal's capability and trust level.

        Args:
            valid_from: When the fact became/becomes true (defaults to now —
                SPEC §5.3). Set explicitly to backfill historical windows.
            valid_to: When the fact stopped being true (defaults to open/None)
            supersedes: Id of a specific existing assertion this one
                explicitly replaces (ADR-0050/KI-080) — see
                `assert_literal`'s docstring for the full rationale; only
                meaningful for a cardinality="many",
                temporality="time_varying" relation. Same submission-time-
                only checking caveat as `propose()`'s own `supersedes`.

        Returns:
            (Proposal, Decision) tuple

        Raises:
            AuthError: author or acting_as is not a known principal
            CapabilityError: delegation is unauthorized
            NotFoundError: subject or target is not a known entity id
                (KI-083, KI-089)
            ValidationError: author is ai-kind and model is not provided,
                predicate is not declared in the active schema, predicate
                is declared a property rather than a relation (KI-040),
                supersedes is set but predicate isn't cardinality="many"/
                temporality="time_varying" (ADR-0050), or (on auto-accept)
                a registered `Validator` rejects the assertion (KI-042) or
                supersedes doesn't name a real, active,
                overlapping-and-differing assertion (ADR-0050)
        """
        principal = self._get_principal_or_raise(author)
        self._require_model_for_ai(principal, model)
        self._require_existing_subject(subject)
        self._require_existing_target(target)
        self._require_known_predicate(predicate, expected_kind="relation")
        delegating = self._resolve_delegation(principal, author, acting_as)
        temporality = self._resolve_temporality(predicate)
        self._require_valid_supersedes_hint(supersedes, predicate, temporality)

        now = self.clock.now()
        proposal_id = self.id_provider.next()
        proposal = Proposal(
            id=proposal_id,
            namespace=self.namespace,
            author=author,
            acting_as=acting_as,
            state="submitted",
            created_at=now,
            payload={
                "operations": [
                    {
                        "kind": "assert_ref",
                        "subject": subject,
                        "predicate": predicate,
                        "target": target,
                        "temporality": temporality,
                        "confidence": confidence,
                        "source": source,
                        "rationale": rationale,
                        "model": model,
                        "valid_from": valid_from.isoformat() if valid_from else None,
                        "valid_to": valid_to.isoformat() if valid_to else None,
                        "supersedes": supersedes,
                    }
                ]
            },
        )

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution (ADR-0003).
        # kb pinned at `now` (== proposal.created_at) — see propose()'s
        # comment for the replay caveat (KI-017, ADR-0025).
        kb_view = self.as_of(now)
        decision = self.policy.evaluate(proposal, principal, kb_view, acting_as=delegating)
        finalized = self._finalize_non_accepted_decision(proposal, decision, now)
        if finalized is not None:
            return finalized
        assert isinstance(decision, AutoAccept)

        assertion = Assertion(
            id=self.id_provider.next(),
            namespace=self.namespace,
            subject=subject,
            predicate=predicate,
            value_kind="ref",
            value=target,
            author=author,
            acting_as=acting_as,
            confidence=confidence,
            source=source,
            rationale=rationale,
            model=model,
            asserted_at=now,
            proposal_id=proposal_id,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self._run_validators(assertion)
        accepted = proposal.model_copy(
            update={"state": "auto_accepted", "decided_at": now, "policy_reason": decision.reason}
        )
        with self.backend.transaction():
            self.backend.put_proposal(accepted)
            self._apply_with_conflict_routing(assertion, temporality, supersedes)
        return accepted, decision

    def _reject_retract_if_party_to_contradiction(
        self, assertion_id: str, parties: set[str]
    ) -> None:
        """Block retracting a member of an *open* contradiction any of
        ``parties`` is a party to (KI-033) — mirrors resolve_contradiction's
        self-resolution guard (KI-026): checks every member, not just the
        target, since an interested party shouldn't get to unilaterally
        retract the *opposing* member either. Without this, a principal who
        authored one side of a disputed static fact could reach the same
        outcome as adjudicating the dispute in their own favor, just via
        retract() instead of resolve_contradiction() — leaving the
        contradiction stuck open with only their own value still flagged.
        ``parties`` covers every principal whose decision actually causes
        the retraction to take effect — the retracting caller (and its
        delegate) for retract()'s own auto-accept path, plus the accepting
        reviewer for a retract proposal that instead went through review
        (accept_proposal): a reviewer who is themselves a party to the same
        contradiction can reach the identical one-sided outcome by approving
        a neutral principal's retract proposal, not just by retracting
        directly.

        Applies to a member in ANY status — not just `flagged` (KI-051).
        Membership in a still-`open` contradiction, not the target's own
        current status, is what makes this guard applicable: a member the
        conflict-routing/`flag_contradiction()` machinery already
        terminalized to `retracted`/`superseded` while its contradiction
        stayed open (ADR-0031's own deliberate design — a terminal member
        can still belong to an open contradiction) was previously exempt
        from this guard entirely, since the pre-KI-051 check gated on
        `status == "flagged"` before ever looking up membership. A party
        could bypass KI-033 outright against exactly that member, at only
        `write` capability.

        Must run inside the same transaction that performs the retraction
        (like resolve_contradiction's own check) and before any of that
        transaction's writes land, so raising here rolls back a no-op —
        a contradiction opened or extended concurrently in another
        transaction can't be missed by a check made only beforehand, and a
        raise here can't leave a partial write in place either. A proposal
        this rejects has no further path to `accepted` — `reject_proposal`
        is the only way to close it out.

        Raises:
            NotFoundError: a contradiction member assertion could not be
                found — assertions are append-only and never deleted (SPEC
                §5), so this is data corruption, not a benign gap to skip
                past (matches resolve_contradiction's own precedent, KI-026)
        """
        target = self.backend.get_assertion(assertion_id)
        if target is None:
            return
        contradiction = self.backend.get_open_contradiction(
            self.namespace, target.subject, target.predicate
        )
        if contradiction is None or assertion_id not in contradiction.member_ids:
            return
        for member_id in contradiction.member_ids:
            member = self.backend.get_assertion(member_id)
            if member is None:
                raise NotFoundError(
                    f"Assertion {member_id!r}, a member of contradiction "
                    f"{contradiction.id!r}, could not be found"
                )
            if parties & ({member.author, member.acting_as} - {None}):
                raise CapabilityError(
                    f"Cannot retract assertion {assertion_id!r}: it is a member of "
                    f"open contradiction {contradiction.id!r} that {sorted(parties)!r} are "
                    f"party to (author or delegate of member assertion {member_id!r}) — use "
                    "resolve_contradiction() instead"
                )

    def _open_contradiction_if_member(self, assertion_id: str) -> Contradiction | None:
        """Return the open Contradiction `assertion_id` is a member of, or
        None otherwise (KI-043) — an ordinary retraction (target not a
        contradiction member, or its contradiction has since resolved)
        gets None.

        Applies regardless of the member's own current status — not just
        `flagged` (KI-051) — for the same reason
        `_reject_retract_if_party_to_contradiction`'s docstring explains:
        an already-`retracted`/`superseded` member can still belong to a
        still-`open` contradiction (ADR-0031), and this method's job is to
        answer "is `assertion_id` governed by that open contradiction",
        not "is it currently flagged".

        Shared by `retract()`/`resubmit()`'s optimistic pre-check (decides
        whether to route to review instead of evaluating `self.policy`) and
        `_require_capability_to_retract_contradiction_member`'s authoritative,
        in-transaction recheck — the same read, used twice for the same
        TOCTOU-safety reason `_reject_retract_if_party_to_contradiction`'s
        own docstring explains.
        """
        target = self.backend.get_assertion(assertion_id)
        if target is None:
            return None
        contradiction = self.backend.get_open_contradiction(
            self.namespace, target.subject, target.predicate
        )
        if contradiction is None or assertion_id not in contradiction.member_ids:
            return None
        return contradiction

    def _meets_retract_contradiction_floor(
        self, principal: Principal, delegating: Principal | None
    ) -> bool:
        """Whether `principal` (+`delegating`, if any) meets the review/admin
        + non-AI floor `resolve_contradiction()` requires (KI-043).

        Effective capability mirrors `_check_direct_write_capability`'s
        delegation-attenuation: min(principal, delegating) when delegating
        (SPEC §8.4) — a `write`-capability principal can't launder this
        floor by delegating to/from a `review`-capability one either way.
        AI-kind is checked on `principal` only (the acting party), matching
        `_check_direct_write_capability`'s own precedent — ADR-0003 already
        treats an AI's accountable owner, not the AI itself, as the
        eligible actor once delegation is involved.
        """
        if principal.kind == "ai":
            return False
        capability: str = principal.default_capability
        if delegating is not None:
            capability = min_capability(capability, delegating.default_capability)
        return capability in ("review", "admin")

    def _retract_op_review_override(
        self, proposal: Proposal, principal: Principal, delegating: Principal | None
    ) -> RequireReview | None:
        """If `proposal` stages any `retract` op targeting an open
        contradiction's member (any status, KI-051) `principal`
        (+`delegating`) doesn't meet `_meets_retract_contradiction_floor`
        for, return a `RequireReview`
        decision to use INSTEAD of evaluating `self.policy` — otherwise
        None, meaning the caller should evaluate policy normally
        (KI-043, ADR-0030). Checks every retract op in the payload, not
        just the first — today every proposal this codebase constructs has
        exactly one operation, so this only matters if that ever changes.

        This is the fix for an inversion an earlier version of this check
        had: raising `CapabilityError` after an auto-accept-eligible
        `write`-capability principal's decision was already computed left
        that principal with *no* path forward (their own retraction is
        blocked, and they have no way to get a proposal into the review
        queue either — `ThresholdPolicy` auto-accepts `write` and above
        unconditionally). Meanwhile a lower, merely-`propose`-capability
        principal sailed through via ordinary review. Routing to review
        instead — mirroring how an AI-authored proposal already always
        requires review under `ThresholdPolicy`, rather than being
        rejected outright — gives every principal the same two outcomes
        regardless of capability: auto-accept if they meet the floor,
        queue for review (where any `review`-capable, non-AI principal
        can accept it) if they don't. Ordinary (non-contradiction-member)
        retraction is unaffected — this only intercepts the specific
        payload shape SPEC §10.3's contradiction-adjudication floor cares
        about.

        Called from `retract()` and `resubmit()`, both *before* evaluating
        `self.policy` and before opening a transaction — optimistic, like
        the read `_reject_retract_if_party_to_contradiction`/
        `_require_capability_to_retract_contradiction_member` perform again,
        authoritatively, once inside the transaction. A contradiction that
        opens concurrently between this call and the transaction is not
        caught here — that narrow race is what the in-transaction
        `CapabilityError` raise remains for (an accepted, pre-existing
        class of race this project already tolerates elsewhere, e.g.
        `resubmit`'s own docstring on policy evaluation staying outside
        the transaction).
        """
        retract_ops = (
            op for op in proposal.payload.get("operations", []) if op["kind"] == "retract"
        )
        for op in retract_ops:
            if self._open_contradiction_if_member(op["assertion_id"]) is None:
                continue
            if self._meets_retract_contradiction_floor(principal, delegating):
                continue
            return RequireReview(
                reviewers=[],
                reason="Retracting an open contradiction's member requires review/admin "
                "capability, same floor as resolve_contradiction() (SPEC §10.3, KI-043)",
            )
        return None

    def _require_capability_to_retract_contradiction_member(
        self, assertion_id: str, principal: Principal, delegating: Principal | None
    ) -> None:
        """Authoritative, in-transaction backstop for
        `_retract_op_review_override`'s optimistic pre-check (KI-043):
        raises if `assertion_id` is (as of right now, inside the write
        transaction) a member (any status, KI-051) of an open contradiction
        and `principal` doesn't meet `_meets_retract_contradiction_floor`.

        In the common case this never fires — the pre-check in
        `retract()`/`resubmit()` already routed a below-floor principal to
        review before a transaction was ever opened for them. It only
        fires for the narrow race the pre-check's docstring describes: a
        contradiction that opened concurrently after the pre-check ran but
        before this transaction started. There is no way to "route to
        review" once inside an already-auto-accepting transaction, so this
        raises `CapabilityError` and rolls back rather than silently
        letting the write land.

        No-op when the target isn't currently a member of an open
        contradiction — an ordinary retraction is unaffected.

        Not called for `accept_proposal`'s replay of a retract op — the
        accepting reviewer there already satisfies this exact floor via
        `_require_reviewer_principal`, so re-checking would be redundant.
        Called for `retract()`'s own auto-accept path and for `resubmit`'s
        auto-accept path (see their respective call sites).

        Raises:
            CapabilityError: `principal` is AI-kind, or effective capability
                (after delegation attenuation) is below `review`
        """
        contradiction = self._open_contradiction_if_member(assertion_id)
        if contradiction is None:
            return
        if self._meets_retract_contradiction_floor(principal, delegating):
            return
        if principal.kind == "ai":
            raise CapabilityError(
                f"Cannot retract assertion {assertion_id!r}: it is a member of open "
                f"contradiction {contradiction.id!r} — AI principal {principal.id!r} cannot "
                "retract a disputed static fact, same floor as resolve_contradiction() "
                "(SPEC §10.3, KI-043)"
            )
        raise CapabilityError(
            f"Cannot retract assertion {assertion_id!r}: it is a member of open "
            f"contradiction {contradiction.id!r} — retracting a disputed static fact "
            f"requires review/admin capability, same floor as resolve_contradiction() "
            f"(SPEC §10.3, KI-043), not just write. This is an unusual race (a contradiction "
            "opened concurrently after this write was already decided) — retry, or have a "
            "review-capable principal accept a retract proposal instead."
        )

    def retract(
        self,
        assertion_id: str,
        author: str,
        *,
        acting_as: str | None = None,
    ) -> tuple[Proposal, Decision]:
        """Propose retraction of an assertion through the policy path (SPEC §9).

        When ``acting_as`` is set the retraction is made on behalf of another
        principal (delegation, ADR-0003).

        If ``self.policy`` would auto-accept and ``assertion_id`` is
        currently a member (any status — flagged, retracted, or
        superseded, KI-051) of an open contradiction, two further checks
        apply before the write actually lands: the retracting principal
        must not be a party to the contradiction (KI-033), and must meet
        ``resolve_contradiction()``'s own review/admin + non-AI floor
        (KI-043, ADR-0030) — if the latter fails, this routes to review
        instead of raising, so even a ``write``-capability principal below
        that floor has a real path forward (a ``review``-capable, non-AI
        principal can accept the resulting proposal via
        ``accept_proposal()``). A proposal ``self.policy`` was already
        going to send to review for its own reasons skips both checks here
        entirely — matching how the party guard has always worked, they're
        deferred to ``accept_proposal()``, which re-checks both (the party
        check unconditionally; the capability floor only for the narrow,
        no-reviewer-involved paths that need it — see
        ``_replay_proposal_operations``'s docstring).

        If ``assertion_id`` is already ``retracted``, this is a no-op on
        the assertion itself (KI-051) — the proposal still records
        `auto_accepted`, but no `set_assertion_status`/status event write
        happens, since re-retracting an already-retracted target is a pure
        status no-op that would otherwise record a second, misattributed
        `retracted` event. Deliberately narrower than
        `resolve_contradiction()`'s own loser-loop precedent (KI-044,
        ADR-0031), which also skips an already-``superseded`` loser —
        explicitly retracting a `superseded` assertion via this method is
        a distinct, legitimate transition this codebase already treats as
        worth its own event, unlike the loser loop's automatic side effect
        of picking a winner.

        Raises:
            NotFoundError: ``assertion_id`` does not exist — checked
                unconditionally up front, before policy is even evaluated,
                not only on the auto-accept path (KI-074 review, round 2):
                a proposal ``self.policy`` would route to review must never
                get to exist for an assertion that isn't real in the first
                place, since ``ThresholdPolicy`` always routes AI principals
                to review (ADR-0003) and the MCP/AI caller is exactly who
                KI-074 was about
            CapabilityError: ``self.policy`` would auto-accept and the
                assertion is a member of an open contradiction the
                retracting principal is a party to (author or delegate of
                any member, KI-033) — use resolve_contradiction() instead;
                or, in the narrow race where a contradiction opens
                concurrently between this call's checks and the write
                transaction, the same review/admin + non-AI floor (KI-043)

        Returns:
            (Proposal, Decision) tuple
        """
        principal = self._get_principal_or_raise(author)
        delegating = self._resolve_delegation(principal, author, acting_as)

        # Checked here, before any proposal is created or policy evaluated
        # at all - not deferred to the auto-accept transaction below, which
        # only the auto-accept path ever reaches. Safe to check once,
        # outside any transaction: unlike a contradiction's membership or
        # an assertion's status, existence is permanent once true
        # (assertions are append-only and never deleted, SPEC §5), so no
        # concurrent write can turn a real id back into a nonexistent one
        # between this check and a later replay. This is the only
        # constructor of a `retract` proposal payload, so a review-routed
        # proposal created past this point is guaranteed, for the rest of
        # its lifetime (accept_proposal, resubmit), to target a real
        # assertion - closing the gap round 1's fix left open, where an
        # unknown id auto-accepted fine but a review-routed one persisted
        # a phantom proposal that later hit accept_proposal's own bare
        # `assert retracted is not None` as an uncatchable AssertionError.
        if self.backend.get_assertion(assertion_id) is None:
            raise NotFoundError(f"Assertion not found: {assertion_id}")

        now = self.clock.now()
        proposal_id = self.id_provider.next()
        proposal = Proposal(
            id=proposal_id,
            namespace=self.namespace,
            author=author,
            acting_as=acting_as,
            state="submitted",
            created_at=now,
            payload={"operations": [{"kind": "retract", "assertion_id": assertion_id}]},
        )

        # SPEC §8.4: effective capability is min(author, acting_as) when
        # delegating, not a wholesale substitution (ADR-0003).
        # kb pinned at `now` (== proposal.created_at) — see propose()'s
        # comment for the replay caveat (KI-017, ADR-0025).
        kb_view = self.as_of(now)
        decision = self.policy.evaluate(proposal, principal, kb_view, acting_as=delegating)
        retracting_parties = {author} | ({acting_as} if acting_as is not None else set())
        if isinstance(decision, AutoAccept):
            # Only a proposal self.policy would otherwise auto-accept
            # needs the party/capability-floor checks at submission time
            # at all - one already headed to review for unrelated policy
            # reasons defers both to accept_proposal (KI-033's original
            # behavior, preserved: see
            # test_party_via_accept_proposal_is_also_blocked).
            self._reject_retract_if_party_to_contradiction(assertion_id, retracting_parties)
            review_override = self._retract_op_review_override(proposal, principal, delegating)
            if review_override is not None:
                decision = review_override
        finalized = self._finalize_non_accepted_decision(proposal, decision, now)
        if finalized is not None:
            return finalized
        assert isinstance(decision, AutoAccept)

        accepted = proposal.model_copy(
            update={"state": "auto_accepted", "decided_at": now, "policy_reason": decision.reason}
        )
        with self.backend.transaction():
            self._reject_retract_if_party_to_contradiction(assertion_id, retracting_parties)
            self._require_capability_to_retract_contradiction_member(
                assertion_id, principal, delegating
            )
            self.backend.put_proposal(accepted)
            # KI-051: re-retracting an already-`retracted` target is a pure
            # status no-op - writing it anyway would record a second,
            # misattributed `retracted` event for a transition that already
            # happened. Deliberately narrower than resolve_contradiction()'s
            # own loser-loop precedent (KI-044, ADR-0031), which also skips
            # an already-`superseded` loser: that loop is an automatic
            # side effect of picking a winner, not a call the user directly
            # targeted at that specific assertion. Explicitly retracting a
            # `superseded` assertion via retract() is a distinct, legitimate
            # action this codebase already treats as a real transition
            # worth its own event (test_events_ordered_oldest_first) - only
            # an exact `retracted` -> `retracted` re-call is the no-op.
            current = self.backend.get_assertion(assertion_id)
            assert current is not None  # existence already checked above (KI-074 review)
            if current.status != "retracted":
                self.backend.set_assertion_status(
                    assertion_id, "retracted", valid_to=self._retraction_valid_to(assertion_id, now)
                )
                self._record_assertion_event(assertion_id, author, "retracted", now)
        return accepted, decision

    def _apply_with_conflict_routing(
        self,
        assertion: Assertion,
        temporality: Literal["static", "time_varying"],
        supersedes: str | None = None,
    ) -> Assertion:
        """Apply an assertion with SPEC §10 conflict routing. Must run inside a transaction.

        Returns the assertion as actually persisted (its ``status``/``supersedes``
        may differ from the input, e.g. when routing flags or supersedes it).

        ``supersedes`` (ADR-0050): caller-supplied hint naming a specific
        existing assertion this one explicitly replaces, meaningful only
        for cardinality="many" time_varying properties. Passed through to
        ``route()`` as ``supersedes_hint`` — its own ``ValueError`` (the
        hint doesn't name a real, active, overlapping-and-differing
        assertion against a fresh read of ``existing``) is translated to
        the stable ``ValidationError`` taxonomy here, at the one call site
        where the pure ``govern.conflict`` layer's contract violations
        cross into caller-facing error codes.
        """
        open_contradiction = self.backend.get_open_contradiction(
            self.namespace, assertion.subject, assertion.predicate
        )

        # This shortcut only ever applies to a `static` incoming write —
        # for a `time_varying` one, an open contradiction here doesn't
        # short-circuit into Contradict at all, and the else branch below
        # can produce an `active` result even while this (subject,
        # predicate)'s contradiction stays open (ADR-0031 found this:
        # every existing member terminal means no `active` assertion to
        # conflict/supersede against, so a fresh `time_varying` write
        # routes to Activate — see `flag_contradiction()` for the actual
        # way to bring it into the still-open contradiction). For a
        # `static` write, though, every existing member of an open
        # contradiction is normally `flagged` — `retracted` and
        # `superseded` are the two exceptions (KI-034, ADR-0031) — and any
        # new incoming assertion must be added to the same contradiction.
        # See the Contradict branch below.
        if open_contradiction is not None and temporality == "static":
            all_member_ids = list(dict.fromkeys(open_contradiction.member_ids + [assertion.id]))
            result: ConflictResult = Contradict(
                member_ids=all_member_ids,
                existing_contradiction_id=open_contradiction.id,
            )
        else:
            existing = self.backend.assertions(
                subject=assertion.subject,
                predicate=assertion.predicate,
                status="active",
            )
            try:
                result = route(
                    incoming=assertion,
                    existing=existing,
                    temporality=temporality,
                    existing_contradiction_id=(
                        open_contradiction.id if open_contradiction else None
                    ),
                    cardinality=self._resolve_cardinality(assertion.predicate),
                    supersedes_hint=supersedes,
                )
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc

        if isinstance(result, Supersede):
            # Close prior window at the incoming assertion's valid_from (SPEC §10.2).
            # valid_from is guaranteed non-None after Assertion validation.
            close_at = (assertion.valid_from or assertion.asserted_at).isoformat()
            supersedes_id = result.targets[0] if result.targets else None
            final = assertion.model_copy(update={"supersedes": supersedes_id})
            # Persisted before the loop below: each superseded-event row's
            # successor_id FK (SQLite) references this row, so it must exist
            # first. Safe — both inserts share this method's transaction.
            self.backend.put_assertion(final)
            for target_id in result.targets:
                self.backend.set_assertion_status(target_id, "superseded", valid_to=close_at)
                self._record_assertion_event(
                    target_id,
                    assertion.author,
                    "superseded",
                    assertion.asserted_at,
                    successor_id=final.id,
                )
            return final

        elif isinstance(result, Contradict):
            for mid in result.member_ids:
                if mid != assertion.id:
                    # KI-034/KI-044: `retracted` and `superseded` are both
                    # terminal statuses (SPEC §5, ADR-0031) — extending an
                    # already-open contradiction must never flip a member
                    # that's since been legitimately retracted (e.g. by a
                    # neutral party via retract(), KI-033) or superseded
                    # (e.g. a schema change made its predicate time_varying
                    # after it was named in flag_contradiction(), which
                    # accepts a superseded assertion by design) back to
                    # `flagged`. Its id is deliberately left in the
                    # contradiction's member_ids — that list isn't audit-only,
                    # it's also the eligibility set resolve_contradiction()
                    # and _reject_retract_if_party_to_contradiction() scan
                    # (ADR-0031 closed resolve_contradiction()'s own gap —
                    # it can no longer pick a retracted/superseded member as
                    # winner) — only this method's own re-flagging write is
                    # skipped.
                    existing_member = self.backend.get_assertion(mid)
                    if existing_member is None:
                        # Assertions are append-only and never deleted (SPEC
                        # §5) — a contradiction member that can't be found is
                        # data corruption, not a benign gap to skip past
                        # (matches resolve_contradiction's own precedent,
                        # KI-026). `result.existing_contradiction_id`, not
                        # `open_contradiction.id`: this branch is also
                        # reached for a brand-new contradiction, where
                        # `open_contradiction` is still `None`.
                        raise NotFoundError(
                            f"Assertion {mid!r}, a member of contradiction "
                            f"{result.existing_contradiction_id!r}, could not be found"
                        )
                    if existing_member.status in ("retracted", "superseded"):
                        continue
                    # Extending an already-open contradiction re-flags members
                    # that are already flagged (idempotent status write) — only
                    # emit an event for an actual transition, not a no-op.
                    already_flagged = mid in (
                        open_contradiction.member_ids if open_contradiction else []
                    )
                    self.backend.set_assertion_status(mid, "flagged")
                    if not already_flagged:
                        self._record_assertion_event(
                            mid, assertion.author, "flagged", assertion.asserted_at
                        )
            flagged = assertion.model_copy(update={"status": "flagged"})
            self.backend.put_assertion(flagged)
            # The incoming assertion is created already-flagged, but it still
            # needs its own event: as_of() reconstructs flagged-status-at-t
            # from assertion_event, and an assertion with zero events looks
            # like it was never flagged, silently hiding pre-dispute history.
            self._record_assertion_event(
                flagged.id, assertion.author, "flagged", assertion.asserted_at
            )
            if result.existing_contradiction_id and open_contradiction:
                merged = list(dict.fromkeys(open_contradiction.member_ids + result.member_ids))
                self.backend.update_contradiction_members(result.existing_contradiction_id, merged)
            else:
                contradiction = Contradiction(
                    id=self.id_provider.next(),
                    namespace=self.namespace,
                    subject=assertion.subject,
                    predicate=assertion.predicate,
                    state="open",
                    member_ids=result.member_ids,
                    created_at=assertion.asserted_at,
                    raised_by=assertion.author,
                )
                self.backend.put_contradiction(contradiction)
            return flagged

        else:
            self.backend.put_assertion(assertion)
            return assertion

    def _require_reviewer_principal(self, reviewer: str) -> Principal:
        """Reviewer-eligibility checks that depend only on the reviewer's
        own identity, never on any proposal's mutable state — safe to run
        once, before the write transaction opens (KI-035). The reviewer
        must have `review` or `admin` capability, must not be an AI
        principal (ThresholdPolicy always routes AI proposals to
        require_review — an AI reviewer would defeat that guarantee).

        Shared by accept_proposal/reject_proposal/request_changes
        (SPEC §9.4). See `_require_pending_proposal` for the proposal-level
        checks (self-review, state) that must instead run *inside* the
        transaction, immediately before the write.

        This method's "safe to run once, before the transaction" claim
        depends on there being no code path that updates an existing
        principal's `default_capability`/`kind` after creation — true
        today (no such update path exists anywhere in the codebase). A
        future "update principal capability" feature would need to move
        these checks inside the transaction too, the same way the
        proposal-level ones already were for KI-035.

        Args:
            reviewer: Principal ID of the reviewer

        Returns:
            The reviewer's Principal

        Raises:
            AuthError: reviewer is not a known principal
            CapabilityError: reviewer lacks review/admin capability, or is AI-kind
        """
        reviewer_principal = self.backend.get_principal(reviewer)
        if reviewer_principal is None:
            raise AuthError(f"Principal not found: {reviewer}")
        if reviewer_principal.default_capability not in ("review", "admin"):
            raise CapabilityError(f"Principal {reviewer} lacks review capability")
        if reviewer_principal.kind == "ai":
            raise CapabilityError(f"Principal {reviewer!r} is an AI principal and cannot review")
        return reviewer_principal

    def _require_pending_proposal(self, proposal_id: str, reviewer: str) -> Proposal:
        """Re-read the proposal fresh and validate it's still reviewable by
        `reviewer` right now (SPEC §9.4). MUST be called inside the same
        `backend.transaction()` block that performs the transition's write,
        immediately before that write — not from a proposal object fetched
        before the transaction opened (KI-035, a TOCTOU gap: two concurrent
        transition calls that both read the same pre-transition state
        outside any transaction could both pass validation and both reach
        their writes). Mirrors `resolve_contradiction`'s own KI-026 fix:
        move validation inside the transaction and re-read, rather than
        trust a pre-transaction snapshot.

        The proposal's own author or delegating principal must not be
        `reviewer` (self-review would let a misconfigured AI principal with
        `review` capability, or a delegate reviewing their own delegated
        proposal, approve its own work) — `author`/`acting_as` never change
        after a proposal is created, so checking them here (rather than
        before the transaction, alongside `_require_reviewer_principal`)
        is only about co-locating the read with the state check below, not
        about a race on those fields specifically. The proposal must exist
        and be in `require_review` or `under_review` state.

        Args:
            proposal_id: ID of the proposal being reviewed
            reviewer: Principal ID of the reviewer

        Returns:
            The freshly-read proposal being reviewed

        Raises:
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: reviewer is the proposal's own author/delegate
            ValidationError: proposal is not pending review (including when
                a concurrent transition already moved it out of that state)
        """
        proposal = self.backend.get_proposal(proposal_id)
        if proposal is None:
            raise NotFoundError(f"Proposal not found: {proposal_id}")
        if reviewer in (proposal.author, proposal.acting_as):
            raise CapabilityError(f"Principal {reviewer!r} cannot review their own proposal")
        if proposal.state not in ("require_review", "under_review"):
            raise ValidationError(
                f"Proposal {proposal_id} is not pending review (state: {proposal.state})"
            )
        return proposal

    def _replay_proposal_operations(
        self,
        proposal: Proposal,
        now: datetime,
        *,
        extra_retracting_party: str | None = None,
        retracting_principal: tuple[Principal, Principal | None] | None = None,
    ) -> list[Assertion]:
        """Apply a proposal's staged operations through SPEC §10 conflict
        routing. Shared by `accept_proposal` and `resubmit` (KI-027) — MUST
        be called inside an open `backend.transaction()`.

        Temporality is re-resolved dynamically against the current schema
        for each operation's predicate, never trusted from the payload's
        stored snapshot (`TestAcceptProposalReResolvesTemporality`) — the
        schema may have changed between proposal creation and replay.
        `_require_known_predicate`'s validations (unknown-predicate,
        `value_type` mismatch KI-031, literal content vs. declared
        value_type KI-049, predicate-kind mismatch KI-040) are deliberately
        NOT re-run here, unlike temporality — the original
        `propose`/`propose_ref` call already ran them once; re-running them
        at replay time is `resubmit`'s own documented precedent to skip
        (see its docstring), not something this method decides on its own.
        A schema change between submission and replay (e.g. a property
        redeclared a relation) can therefore let a now-mismatched write
        through unchecked — a narrow, pre-existing gap shared with KI-031,
        not new to KI-040 or KI-049. `self.validators` (KI-042, ADR-0029), by
        contrast, IS re-run here for each assert_literal/assert_ref op —
        every commit point runs the same per-assertion Validators, so a
        proposal that went through review isn't exempt from them just
        because it skipped `propose`/`propose_ref`'s own auto-accept check.
        Each op's stored `supersedes` hint (ADR-0050/KI-080), if any, is
        read straight from the payload with no re-validation of its own —
        the same "checked once at submission, not re-run here" precedent
        as the predicate/value_type checks above, not a new one. It still
        can't silently misfire: `route()` itself re-validates it against a
        fresh read of `existing` every time it runs (translated to
        `ValidationError` in `_apply_with_conflict_routing`), and a schema
        change that moves the predicate out of cardinality="many"/
        temporality="time_varying" by replay time simply leaves the hint
        unconsulted (`_route_time_varying`'s "single" branch and
        `_route_static` both ignore it), not silently wrong.

        ``extra_retracting_party``: the accepting reviewer, when called from
        `accept_proposal` (KI-033) — a `retract` operation's contradiction
        guard (`_reject_retract_if_party_to_contradiction`) must also cover
        the reviewer, not just the proposal's own author/delegate, since a
        reviewer who is themselves a party to the same contradiction can
        reach the identical one-sided outcome by approving a neutral
        principal's retract proposal. `resubmit` passes nothing extra —
        its caller is already required to be the proposal's own
        author/delegate, already covered by `proposal.author`/`acting_as`.
        Whether ``extra_retracting_party`` is set also decides whether a
        `retract` op runs `_require_capability_to_retract_contradiction_member`
        (KI-043): only when it's unset (`resubmit`'s auto-accept branch),
        since `accept_proposal`'s reviewer already satisfies that floor via
        `_require_reviewer_principal` and re-checking would be redundant.
        In the non-race case `resubmit` will already have routed a
        below-floor author to review before ever reaching this replay (see
        `_retract_op_review_override`) — this method's own check is the
        race-only backstop. ``retracting_principal``, required in that
        case, is `resubmit`'s already-resolved ``(principal, delegating)``
        pair — passed through rather than re-derived here so a `None`
        delegating-principal lookup can't silently be read as "no
        delegation" instead of the fail-loud `_resolve_delegation` already
        ran at submission time (a fail-open gap review found in an earlier
        version of this fix).

        Returns:
            The assertions touched by this proposal's operations, in
            payload order — for assert_literal/assert_ref ops, the assertion
            as persisted; for retract ops, the (now-retracted) target
            assertion, included because retraction is the one operation
            that can *reduce* an entity's completeness (an assert can only
            ever improve it). Used by `accept_proposal` to run
            `self.completeness_validators` once per distinct subject after
            all of this proposal's writes have landed (KI-041) — only
            `.subject` is contractually meaningful for that use, not the
            other fields (a retract entry's `status`/`value` reflect the
            assertion as it was before this op retracted it).
        """
        applied: list[Assertion] = []
        for op in proposal.payload.get("operations", []):
            if op["kind"] == "assert_literal":
                assertion = Assertion(
                    id=self.id_provider.next(),
                    namespace=self.namespace,
                    subject=op["subject"],
                    predicate=op["predicate"],
                    value_kind="literal",
                    value_type=op["value_type"],
                    value=op["value"],
                    author=proposal.author,
                    acting_as=proposal.acting_as,
                    confidence=op.get("confidence"),
                    source=op.get("source"),
                    rationale=op.get("rationale"),
                    model=op.get("model"),
                    asserted_at=now,
                    proposal_id=proposal.id,
                    valid_from=self._parse_window(op, "valid_from"),
                    valid_to=self._parse_window(op, "valid_to"),
                )
                self._run_validators(assertion)
                applied.append(
                    self._apply_with_conflict_routing(
                        assertion,
                        self._resolve_temporality(op["predicate"]),
                        op.get("supersedes"),
                    )
                )
            elif op["kind"] == "assert_ref":
                ref_assertion = Assertion(
                    id=self.id_provider.next(),
                    namespace=self.namespace,
                    subject=op["subject"],
                    predicate=op["predicate"],
                    value_kind="ref",
                    value=op["target"],
                    author=proposal.author,
                    acting_as=proposal.acting_as,
                    confidence=op.get("confidence"),
                    source=op.get("source"),
                    rationale=op.get("rationale"),
                    model=op.get("model"),
                    asserted_at=now,
                    proposal_id=proposal.id,
                    valid_from=self._parse_window(op, "valid_from"),
                    valid_to=self._parse_window(op, "valid_to"),
                )
                self._run_validators(ref_assertion)
                applied.append(
                    self._apply_with_conflict_routing(
                        ref_assertion,
                        self._resolve_temporality(op["predicate"]),
                        op.get("supersedes"),
                    )
                )
            elif op["kind"] == "retract":
                retracting_parties = {proposal.author} | (
                    {proposal.acting_as} if proposal.acting_as is not None else set()
                )
                if extra_retracting_party is not None:
                    retracting_parties.add(extra_retracting_party)
                self._reject_retract_if_party_to_contradiction(
                    op["assertion_id"], retracting_parties
                )
                if extra_retracting_party is None:
                    # Called from resubmit's auto-accept branch, not
                    # accept_proposal (which passes extra_retracting_party
                    # =reviewer) - a reviewer there already satisfies this
                    # exact floor via _require_reviewer_principal, so only
                    # the no-reviewer-involved auto-accept case needs this
                    # check (KI-043) - race-only backstop, see this
                    # method's own docstring.
                    assert retracting_principal is not None
                    author_principal, delegating_principal = retracting_principal
                    self._require_capability_to_retract_contradiction_member(
                        op["assertion_id"], author_principal, delegating_principal
                    )
                retracted = self.backend.get_assertion(op["assertion_id"])
                if retracted is None:
                    # Should be unreachable: retract() (the only constructor
                    # of a `retract` op payload) now checks existence itself
                    # before ever creating the proposal this replays
                    # (KI-074 review, round 2) - append-only means that
                    # can't stop being true later either. A real
                    # NotFoundError rather than a bare assert regardless,
                    # so any future path that manages to reach this with a
                    # stale/corrupt payload fails as a caught OntolithError
                    # (REST 404 / MCP structured error), not an uncaught,
                    # blank-message AssertionError.
                    raise NotFoundError(f"Assertion not found: {op['assertion_id']}")
                # KI-051: same already-`retracted` no-op retract() itself
                # applies (not `superseded` too - see retract()'s own
                # comment for why the two aren't treated the same here).
                if retracted.status != "retracted":
                    self.backend.set_assertion_status(
                        op["assertion_id"],
                        "retracted",
                        valid_to=self._retraction_valid_to(op["assertion_id"], now),
                    )
                    self._record_assertion_event(
                        op["assertion_id"], proposal.author, "retracted", now
                    )
                # Retraction is the one operation that can *reduce*
                # completeness (an assert can only ever improve it) - its
                # subject must be checked too, or accept_proposal could
                # retract an entity's only assertion for a required
                # predicate without completeness_validators ever noticing
                # (found in review). Only `.subject` is used downstream
                # (accept_proposal dedups `applied` by subject before
                # running completeness_validators) - the retracted
                # assertion's other fields (status, value, ...) are not
                # contractually meaningful here.
                applied.append(retracted)
            else:
                raise ValidationError(f"Unknown operation kind in proposal payload: {op['kind']}")
        return applied

    def accept_proposal(self, proposal_id: str, reviewer: str) -> Proposal:
        """Accept a pending proposal, replaying its operations (SPEC §9).

        See `_require_reviewer_principal` for the reviewer-eligibility
        checks and `_require_pending_proposal` for the proposal-state
        check (KI-035: run inside the transaction, immediately before the
        write, not before it — a TOCTOU gap otherwise), both shared with
        `reject_proposal`/`request_changes`. Operations are replayed
        through SPEC §10 conflict routing inside the same transaction.
        `self.completeness_validators` (KI-041, ADR-0029) then run once per
        distinct subject the replayed operations touched — the one point
        in the write path where a whole-entity-completeness check like
        `RequiredFieldsValidator` is structurally meaningful, since this is
        after every operation in the proposal (which may span several
        assertions on the same entity) has landed, not mid-construction.

        Args:
            proposal_id: ID of the proposal to accept
            reviewer: Principal ID of the reviewer

        Returns:
            Updated Proposal with state `accepted`

        Raises:
            AuthError: reviewer is not a known principal
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: reviewer lacks review/admin capability, is
                AI-kind, or is the proposal's own author/delegate; or a
                staged retract operation targets an open contradiction's
                member (any status, KI-051) the proposal's own
                author/delegate *or the accepting reviewer* is party to
                (KI-033)
            ValidationError: proposal is not pending review (including when
                a concurrent transition already moved it out of that state,
                KI-035); or a registered `Validator`/`completeness_validator`
                rejects one of the replayed assertions (KI-042/KI-041)
        """
        self._require_reviewer_principal(reviewer)

        now = self.clock.now()

        with self.backend.transaction():
            proposal = self._require_pending_proposal(proposal_id, reviewer)
            applied = self._replay_proposal_operations(
                proposal, now, extra_retracting_party=reviewer
            )
            self._run_completeness_validators(applied)
            self.backend.update_proposal_state(proposal_id, "accepted", now.isoformat())
            self.backend.put_proposal_event(
                ProposalEvent(
                    id=self.id_provider.next(),
                    proposal_id=proposal_id,
                    actor=reviewer,
                    type="accept",
                    at=now,
                )
            )

        accepted = self.backend.get_proposal(proposal_id)
        assert accepted is not None
        return accepted

    def reject_proposal(self, proposal_id: str, reviewer: str, reason: str = "") -> Proposal:
        """Reject a pending proposal (SPEC §9).

        See `_require_reviewer_principal`/`_require_pending_proposal` for
        the reviewer-eligibility and proposal-state checks shared with
        `accept_proposal`/`request_changes` — the latter runs inside the
        transaction, immediately before the write (KI-035). No operations
        are applied; the proposal is marked rejected.

        Args:
            proposal_id: ID of the proposal to reject
            reviewer: Principal ID of the reviewer
            reason: Optional rejection reason

        Returns:
            Updated Proposal with state `rejected`

        Raises:
            AuthError: reviewer is not a known principal
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: reviewer lacks review/admin capability, is
                AI-kind, or is the proposal's own author/delegate
            ValidationError: proposal is not pending review (including when
                a concurrent transition already moved it out of that state,
                KI-035)
        """
        self._require_reviewer_principal(reviewer)

        now = self.clock.now()
        with self.backend.transaction():
            self._require_pending_proposal(proposal_id, reviewer)
            self.backend.update_proposal_state(proposal_id, "rejected", now.isoformat())
            self.backend.put_proposal_event(
                ProposalEvent(
                    id=self.id_provider.next(),
                    proposal_id=proposal_id,
                    actor=reviewer,
                    type="reject",
                    detail=reason or None,
                    at=now,
                )
            )

        rejected = self.backend.get_proposal(proposal_id)
        assert rejected is not None
        return rejected

    def request_changes(self, proposal_id: str, reviewer: str, reason: str = "") -> Proposal:
        """Request changes on a pending proposal (SPEC §9.1/§9.4).

        See `_require_reviewer_principal`/`_require_pending_proposal` for
        the reviewer-eligibility and proposal-state checks shared with
        `accept_proposal`/`reject_proposal` — the latter runs inside the
        transaction, immediately before the write (KI-035). No operations
        are applied; the proposal moves to `changes_requested` — SPEC
        §9.1's third `under_review` outcome, alongside `accepted`/
        `rejected`. The proposal's own author or delegate can move it back
        to `submitted` via `resubmit` (KI-027).

        Args:
            proposal_id: ID of the proposal
            reviewer: Principal ID of the reviewer
            reason: Optional explanation of what needs to change

        Returns:
            Updated Proposal with state `changes_requested`

        Raises:
            AuthError: reviewer is not a known principal
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: reviewer lacks review/admin capability, is
                AI-kind, or is the proposal's own author/delegate
            ValidationError: proposal is not pending review (including when
                a concurrent transition already moved it out of that state,
                KI-035)
        """
        self._require_reviewer_principal(reviewer)

        now = self.clock.now()
        with self.backend.transaction():
            self._require_pending_proposal(proposal_id, reviewer)
            self.backend.update_proposal_state(proposal_id, "changes_requested", now.isoformat())
            self.backend.put_proposal_event(
                ProposalEvent(
                    id=self.id_provider.next(),
                    proposal_id=proposal_id,
                    actor=reviewer,
                    type="request_changes",
                    detail=reason or None,
                    at=now,
                )
            )

        updated = self.backend.get_proposal(proposal_id)
        assert updated is not None
        return updated

    def assign_reviewers(self, proposal_id: str, reviewers: list[str], actor: str) -> Proposal:
        """Reassign a pending proposal's reviewers (SPEC §9.4's `assign` action, KI-078).

        A `PolicyStrategy` (e.g. `RequireReviewByRole`, ADR-0045) already
        chooses reviewers when a proposal is first created — this lets that
        initial assignment be corrected or supplemented later, the same way
        `request_changes` lets a decision be revisited rather than only
        ever set once. `reviewers` is replaced wholesale, not merged: pass
        the full desired list, including any names from the current
        assignment that should be kept.

        Uses the same reviewer-eligibility and proposal-state checks as
        `accept_proposal`/`reject_proposal`/`request_changes`
        (`_require_reviewer_principal`/`_require_pending_proposal`) —
        `actor` must hold `review`/`admin` capability, be non-AI, and not
        be the proposal's own author or delegate. Reusing the self-review
        guard here (not just for accept/reject/request_changes) is a
        deliberate consistency choice, not currently a load-bearing
        security control: `reviewers` is advisory metadata today, not
        checked by `accept_proposal`, so self-assignment can't presently
        steer who actually approves a proposal. See ADR-0046 for the full
        reasoning, including the real cost this guard has today (blocking
        a review-capable author from legitimately routing their own
        proposal to a specific reviewer).

        A manual assignment made here does NOT survive a later
        `request_changes` + `resubmit` round trip *if* the resubmission's
        fresh policy evaluation lands back in `require_review`: that branch
        of `_finalize_non_accepted_decision` overwrites `reviewers` with
        whatever the new decision computes, discarding this call's own
        list with no event and no trace. If resubmission instead
        auto-accepts or gets rejected, `reviewers` is untouched (those
        branches never call `update_proposal_reviewers`) and this call's
        assignment survives as the historical record. The require_review
        overwrite is a deliberate choice (policy is treated as
        authoritative on re-evaluation there, the same way `resubmit`
        already overwrites `policy_reason`), not an oversight — but it
        means `assign_reviewers` is durable only until the next
        `require_review`-bound `resubmit`, not unconditionally.

        Args:
            proposal_id: ID of the proposal to reassign
            reviewers: New reviewer list, replacing whatever was assigned
                before (an empty list clears every assignment)
            actor: Principal ID performing the reassignment

        Returns:
            Updated Proposal with the new `reviewers` list

        Raises:
            AuthError: actor is not a known principal
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: actor lacks review/admin capability, is
                AI-kind, or is the proposal's own author/delegate
            ValidationError: proposal is not pending review (including when
                a concurrent transition already moved it out of that state,
                KI-035)
        """
        self._require_reviewer_principal(actor)

        now = self.clock.now()
        with self.backend.transaction():
            self._require_pending_proposal(proposal_id, actor)
            self.backend.update_proposal_reviewers(proposal_id, reviewers)
            self.backend.put_proposal_event(
                ProposalEvent(
                    id=self.id_provider.next(),
                    proposal_id=proposal_id,
                    actor=actor,
                    type="assign",
                    detail=", ".join(reviewers) if reviewers else None,
                    at=now,
                )
            )

        updated = self.backend.get_proposal(proposal_id)
        assert updated is not None
        return updated

    def resubmit(self, proposal_id: str, author: str) -> tuple[Proposal, Decision]:
        """Resubmit a proposal after changes were requested (SPEC §9.1:
        `changes_requested -> submitted -> {policy}`), closing the dead end
        `request_changes` previously left (KI-027).

        Only the proposal's own author or delegating principal may call
        this — the inverse of `_require_pending_proposal`'s self-review
        guard: this is an author action, not a reviewer one. The payload is
        replayed unedited (in-place payload editing before resubmission is
        not yet supported); policy is re-evaluated against a fresh
        `kb_view` pinned at the resubmission instant — unlike `propose`/
        `propose_ref`, that pin is deliberately NOT `proposal.created_at`
        (ADR-0025): the proposal already exists, so a KB-reading
        `PolicyStrategy` (e.g. `SourceQuorum`) must evaluate it against
        what's true now, not what was true when it was first drafted.
        `_require_model_for_ai`/`_require_known_predicate` are
        deliberately not re-run — the original submission's shape is
        trusted, matching `accept_proposal`'s existing precedent — but
        temporality is re-resolved dynamically at replay time on
        auto-accept (see `_replay_proposal_operations`), which also runs
        `self.validators` per-assertion (KI-042). `self.completeness_validators`
        deliberately do NOT run here even on auto-accept — ADR-0029 confines
        that check to `accept_proposal`'s human-reviewed path.

        The initial `author`/state validation below runs before policy
        evaluation (needed to construct the `resubmitted` object policy
        evaluates against) and is optimistic — a fast, clear failure for
        the common, non-racing case. It is NOT the authoritative check: the
        state is re-read and re-validated a second time, fresh, as the
        first thing inside the write transaction, immediately before any
        write (KI-035) — two concurrent `resubmit()` calls (or a
        `resubmit()` racing an `accept_proposal`/`reject_proposal`) that
        both pass the optimistic check outside the transaction must not
        both reach a write; only the one that wins the transaction lock
        and still sees `changes_requested` on its fresh, authoritative
        re-read may proceed.

        A `ProposalEvent(type="resubmit")` is always recorded, regardless
        of outcome — unlike `propose`'s own auto-accept path (which
        records none for a brand-new proposal), `resubmit` re-decides an
        already-persisted row, and without an event a `require_review`
        outcome would otherwise leave no trace of when policy last ran
        (`decided_at` stays `None`, `created_at` stays the original
        submission time).

        If the staged operation is a `retract` targeting an open
        contradiction's member (any status, KI-051) and the author/delegate
        doesn't meet `resolve_contradiction()`'s own review/admin + non-AI
        floor, this routes back to review instead of evaluating
        `self.policy` at all — same as `retract()`'s own submission-time
        check (KI-043, ADR-0030).

        Args:
            proposal_id: ID of the proposal to resubmit
            author: Principal ID resubmitting (must be the proposal's own
                author or delegating principal)

        Returns:
            (Proposal, Decision) tuple, matching propose()/propose_ref()

        Raises:
            AuthError: author, or the proposal's original author/delegate
                (if since removed), is not a known principal
            NotFoundError: proposal_id does not name an existing proposal
            CapabilityError: author is not the proposal's own author/delegate;
                or, on auto-accept, a staged retract operation targets an
                open contradiction's member (any status, KI-051) the
                proposal's own author/delegate is party to — shares the
                same `_reject_retract_if_party_to_contradiction` guard as
                `accept_proposal` (KI-033), though only reachable here if
                the author's effective capability/trust has risen since the
                original submission, since that same guard already blocks a
                party at `retract()`'s own auto-accept time otherwise; or,
                in the narrow race where a contradiction opens concurrently
                between this call's review-routing check and the write
                transaction, the same review/admin + non-AI floor a staged
                retract op needs (KI-043) — the non-race case routes to
                review instead of raising (see above)
            ValidationError: proposal is not awaiting resubmission (including
                when a concurrent transition already moved it out of that
                state between the optimistic check and the write, KI-035);
                or, on auto-accept, a registered `Validator` rejects one of
                the replayed assertions (KI-042)
        """
        caller = self._get_principal_or_raise(author)
        proposal = self.backend.get_proposal(proposal_id)
        if proposal is None:
            raise NotFoundError(f"Proposal not found: {proposal_id}")
        if author not in (proposal.author, proposal.acting_as):
            raise CapabilityError(
                f"Principal {author!r} did not author proposal {proposal_id} and cannot resubmit it"
            )
        if proposal.state != "changes_requested":
            raise ValidationError(
                f"Proposal {proposal_id} is not awaiting resubmission (state: {proposal.state})"
            )

        principal = (
            caller if author == proposal.author else self._get_principal_or_raise(proposal.author)
        )
        delegating = self._resolve_delegation(principal, proposal.author, proposal.acting_as)

        now = self.clock.now()
        # decided_at is reset to None: the changes_requested -> submitted
        # transition re-opens the decision, it doesn't carry the prior
        # request_changes decision forward.
        resubmitted = proposal.model_copy(update={"state": "submitted", "decided_at": None})
        kb_view = self.as_of(now)
        decision = self.policy.evaluate(resubmitted, principal, kb_view, acting_as=delegating)
        if isinstance(decision, AutoAccept):
            # KI-033/KI-043/ADR-0030: same two checks retract() runs, in
            # the same order, and for the same reason only within the
            # would-auto-accept branch — a staged retract op that's
            # already headed to review for unrelated policy reasons
            # doesn't need either check at all, matching how the party
            # guard (checked inside _replay_proposal_operations, only
            # reached on this same branch) has always worked. The party
            # guard MUST run first: a party is blocked unconditionally
            # regardless of capability, and routing them to review instead
            # would only defer an already-certain rejection (the party
            # guard also runs, unconditionally, inside accept_proposal's
            # replay) into a silently-doomed pending proposal — exactly
            # what review found missing here relative to retract() itself.
            retracting_parties = {proposal.author} | (
                {proposal.acting_as} if proposal.acting_as is not None else set()
            )
            for op in resubmitted.payload.get("operations", []):
                if op["kind"] == "retract":
                    self._reject_retract_if_party_to_contradiction(
                        op["assertion_id"], retracting_parties
                    )
            review_override = self._retract_op_review_override(resubmitted, principal, delegating)
            if review_override is not None:
                decision = review_override

        with self.backend.transaction():
            # KI-035: authoritative re-check, fresh, first thing inside the
            # transaction — the checks above (author/state) ran before
            # policy evaluation and are only optimistic. Raising here rolls
            # back a no-op; nothing has been written yet. This closes the
            # race on the proposal's *state* only — `decision` was computed
            # against `kb_view` taken outside the transaction, so a
            # concurrent write landing between policy evaluation and here
            # could still mean a KB-reading PolicyStrategy (e.g.
            # SourceQuorum) auto-accepts against KB state it never actually
            # saw. That's a pre-existing, deliberate tradeoff shared by
            # propose/propose_ref/retract (KI-035's own text: "policy
            # evaluation itself can likely stay outside the transaction"),
            # not something this fix claims to close.
            current = self.backend.get_proposal(proposal_id)
            assert current is not None  # append-only; existed moments ago
            if current.state != "changes_requested":
                raise ValidationError(
                    f"Proposal {proposal_id} is not awaiting resubmission (state: {current.state})"
                )
            finalized = self._finalize_non_accepted_decision(
                resubmitted, decision, now, is_new=False
            )
            if finalized is not None:
                result, decision = finalized
            else:
                assert isinstance(decision, AutoAccept)
                result = resubmitted.model_copy(
                    update={
                        "state": "auto_accepted",
                        "decided_at": now,
                        "policy_reason": decision.reason,
                    }
                )
                self.backend.update_proposal_state(
                    proposal_id, "auto_accepted", now.isoformat(), decision.reason
                )
                # self.validators still run per-assertion inside the replay
                # (KI-042); self.completeness_validators deliberately do
                # not run here — this branch, like propose/propose_ref's
                # auto-accept, bypasses human review, and ADR-0029 confines
                # completeness checks to accept_proposal only.
                # retracting_principal: the race-only KI-043 backstop (see
                # _replay_proposal_operations' docstring) needs principal/
                # delegating already resolved above, not re-derived.
                self._replay_proposal_operations(
                    result, now, retracting_principal=(principal, delegating)
                )

            self.backend.put_proposal_event(
                ProposalEvent(
                    id=self.id_provider.next(),
                    proposal_id=proposal_id,
                    actor=author,
                    type="resubmit",
                    detail=getattr(decision, "reason", None),
                    at=now,
                )
            )

        return result, decision

    def proposals(self, state: str | None = "require_review") -> list[Proposal]:
        """List proposals, defaulting to those pending review (SPEC §14.1).

        Without this, `route_to_review` (SPEC §10.3) has no way to surface
        what it routed — a reviewer would need direct backend access to
        discover pending proposals.

        Args:
            state: Filter by proposal state. `"pending"` is a query-level
                alias for `require_review` OR `changes_requested` combined —
                both are still-open proposals needing someone's attention (a
                reviewer for the former, the author for the latter). It is
                NOT the default: a canonical reviewer loop
                (`for p in kb.proposals(): kb.accept_proposal(p.id, ...)`)
                assumes every returned proposal is actionable by a reviewer,
                which is only true of `require_review` —
                `changes_requested` proposals raise `ValidationError` from
                `accept_proposal`/`reject_proposal` (KI-027). Pass
                `state="changes_requested"` or `state="pending"` explicitly
                to include them. `None` returns every state.

        Returns:
            Matching proposals, most recently created first
        """
        if state == "pending":
            merged = self.backend.proposals(state="require_review")
            merged += self.backend.proposals(state="changes_requested")
            merged.sort(key=lambda p: (p.created_at, p.id), reverse=True)
            return merged
        return self.backend.proposals(state=state)

    def contradictions(self, state: str | None = "open") -> list[Contradiction]:
        """List contradictions, defaulting to open (unresolved) ones (SPEC §14.1).

        Args:
            state: Filter by contradiction state ("open" or "resolved");
                None returns every state

        Returns:
            Matching contradictions, most recently created first
        """
        return self.backend.contradictions(state=state)

    def list_namespaces(self) -> list[Namespace]:
        """List all registered namespaces (SPEC §12.2, KI-022).

        Ungated, like `proposals()`/`contradictions()` — namespace metadata
        (id, creation time) carries no sensitive content comparable to
        `list_principals()`'s `owner`/`trust_level` fields. This project is
        still single-namespace throughout (ADR-0015): today this always
        returns exactly one entry, `DEFAULT_NAMESPACE`, seeded by every
        backend at schema-creation time.

        Returns:
            All namespaces, most recently created first
        """
        return self.backend.list_namespaces()

    def resolve_contradiction(
        self,
        contradiction_id: str,
        winner_assertion_id: str,
        resolver: str,
    ) -> Contradiction:
        """Resolve an open contradiction by selecting a winning assertion (SPEC §10.3).

        All other member assertions are retracted; the winning assertion is
        reactivated. The resolver must have `review` or `admin` capability
        and must not be an AI principal (ThresholdPolicy always routes AI
        proposals to require_review; an AI resolver would let it approve
        its own or another AI's disputed value unsupervised). The resolver
        also must not be the author or delegate of *any* member assertion
        (KI-026) — not just the winner, since an interested party shouldn't
        get to pick against their own losing entry either. Without this, a
        principal who authored one side of a disputed static fact could
        adjudicate the dispute in their own favor unilaterally. This check
        is deliberately narrower than "any AI member's owner" — an AI
        principal's accountable owner is still eligible to resolve a
        contradiction that AI is party to, consistent with `owner` already
        being who `ThresholdPolicy` routes that AI's own proposals to for
        review (SPEC §7.4/ADR-0003); only actual authorship/delegation
        disqualifies a resolver, not the owner relationship.
        Resolution is recorded on the contradiction and appears in provenance.

        Args:
            contradiction_id: ID of the contradiction to resolve
            winner_assertion_id: ID of the member assertion to keep active
            resolver: Principal ID resolving the contradiction

        Returns:
            Updated Contradiction with state `resolved`

        Raises:
            AuthError: resolver is not a known principal
            NotFoundError: contradiction_id does not name an existing
                contradiction, or a member assertion could not be found
                (assertions are append-only and never deleted, so this
                indicates data corruption, not a benign gap)
            CapabilityError: resolver lacks review/admin capability, is
                AI-kind, or is the author/delegate of any member assertion
            ValidationError: contradiction is not open, winner_assertion_id
                is not one of its members, or winner_assertion_id is
                already `retracted`/`superseded` — both are terminal for
                winner selection (KI-044, ADR-0031), same as they already
                are for conflict-routing extension (KI-034) and
                party-to-contradiction retraction (KI-033). Checked only
                after every member has cleared the party-to-contradiction
                check above, so a resolver who is both a party AND picks a
                terminal-status winner always sees the `CapabilityError`,
                never this one — order-independent, not member-order-
                dependent.
        """
        resolver_principal = self.backend.get_principal(resolver)
        if resolver_principal is None:
            raise AuthError(f"Principal not found: {resolver}")
        if resolver_principal.default_capability not in ("review", "admin"):
            raise CapabilityError(f"Principal {resolver} lacks review capability")
        if resolver_principal.kind == "ai":
            raise CapabilityError(f"Principal {resolver!r} is an AI principal and cannot review")

        # Contradiction/membership/self-resolution validation runs inside the
        # transaction, not before it: reading contradiction.member_ids
        # outside the transaction would let another thread extend the same
        # open contradiction (e.g. via a concurrent propose()) between the
        # validation and the write below — a new member would then escape
        # both the self-resolution check and the retraction loop entirely.
        # Raising here rolls back a no-op (nothing has been written yet).
        now = self.clock.now()
        with self.backend.transaction():
            contradiction = self.backend.get_contradiction(contradiction_id)
            if contradiction is None:
                raise NotFoundError(f"Contradiction not found: {contradiction_id}")
            if contradiction.state != "open":
                raise ValidationError(
                    f"Contradiction {contradiction_id} is not open (state: {contradiction.state})"
                )
            if winner_assertion_id not in contradiction.member_ids:
                raise ValidationError(
                    f"Assertion {winner_assertion_id} is not a member of "
                    f"contradiction {contradiction_id}"
                )
            winner: Assertion | None = None
            for member_id in contradiction.member_ids:
                member = self.backend.get_assertion(member_id)
                if member is None:
                    # Assertions are append-only and never deleted (SPEC
                    # §5) — a contradiction member that can't be found is
                    # data corruption, not a benign gap to skip past.
                    raise NotFoundError(
                        f"Assertion {member_id!r}, a member of contradiction "
                        f"{contradiction_id!r}, could not be found"
                    )
                if resolver in (member.author, member.acting_as):
                    raise CapabilityError(
                        f"Principal {resolver!r} cannot resolve a contradiction they are "
                        f"party to (author or delegate of member assertion {member_id!r})"
                    )
                if member_id == winner_assertion_id:
                    winner = member
            assert winner is not None  # membership already checked above

            # KI-044/ADR-0031: `retracted` AND `superseded` are both
            # terminal for winner selection, matching the same pair
            # `flag_contradiction()`'s own re-flag guard and (below,
            # `_apply_with_conflict_routing`'s extension branch) already
            # treat as terminal — reactivating either would leave the
            # winner `active` with a `valid_to` a governance action (not
            # the author) closed, silently undoing that close rather than
            # reflecting a fact whose validity the author ever declared
            # ended (see ADR-0031's Rationale for why that distinction
            # matters). Checked only after the full party loop above, so
            # this can never fire ahead of a `CapabilityError` for a
            # resolver who is also a party — precedence is deterministic,
            # not dependent on `contradiction.member_ids`' iteration order.
            if winner.status in ("retracted", "superseded"):
                raise ValidationError(
                    f"Assertion {winner_assertion_id!r} is already {winner.status} and cannot "
                    "be selected as the winner of contradiction "
                    f"{contradiction_id!r} — retraction/supersession is terminal"
                )

            for member_id in contradiction.member_ids:
                if member_id != winner_assertion_id:
                    # A loser already `retracted` (KI-034 — e.g. a neutral
                    # third party's own earlier retract()) or `superseded`
                    # (ADR-0031 — e.g. named via flag_contradiction(), which
                    # accepts a superseded assertion by design) needs no
                    # further write: re-retracting a retracted loser is a
                    # no-op status-wise, and overwriting a superseded loser
                    # to `retracted` here would misattribute this specific
                    # transition to the resolver, who did nothing to cause
                    # it — this loop's skip is about *attribution* for an
                    # automatic side effect of picking a winner, not (as an
                    # earlier version of this comment overstated) permanently
                    # erasing supersession from the record: the event log
                    # only ever appends, so a *user-targeted* superseded ->
                    # retracted transition elsewhere (e.g. an explicit
                    # retract() call, KI-051 — deliberately NOT given this
                    # same skip, see that method's own comment) still leaves
                    # both events in the trail with their own actors.
                    loser = self.backend.get_assertion(member_id)
                    assert loser is not None  # already resolved via the loop above
                    if loser.status in ("retracted", "superseded"):
                        continue
                    self.backend.set_assertion_status(
                        member_id, "retracted", valid_to=self._retraction_valid_to(member_id, now)
                    )
                    self._record_assertion_event(member_id, resolver, "retracted", now)
            self.backend.set_assertion_status(winner_assertion_id, "active")
            self._record_assertion_event(winner_assertion_id, resolver, "reactivated", now)
            self.backend.resolve_contradiction(contradiction_id, resolver, now)

        resolved = self.backend.get_contradiction(contradiction_id)
        assert resolved is not None
        return resolved

    def flag_contradiction(
        self,
        assertion_id_a: str,
        assertion_id_b: str,
        author: str,
        *,
        rationale: str | None = None,
    ) -> tuple[Contradiction, str]:
        """Flag two assertions as contradictory, opening or extending a contradiction.

        Propose-level action (ADR-0008): author must hold >= propose
        capability. Both assertions are marked "flagged" and excluded from
        default queries until the contradiction is resolved. This does NOT
        resolve the contradiction — see resolve_contradiction().

        The assertion/contradiction reads this decides from are taken
        fresh inside the write transaction (KI-045) — mechanically the
        same TOCTOU fix KI-035 applied to the four proposal-transition
        methods — so a concurrent retract()/supersession or
        resolve_contradiction() landing between an earlier, stale read
        and this call's write can't be missed or acted on against
        already-superseded state.

        Args:
            assertion_id_a: First conflicting assertion ID
            assertion_id_b: Second conflicting assertion ID
            author: Principal ID raising the flag
            rationale: Optional explanation of the contradiction. Recorded
                in ``Contradiction.metadata["rationale_history"]`` — a list
                of ``{"rationale", "actor", "at"}`` entries, one per call
                that supplied a truthy rationale (matching every other
                optional-string field on this method's payload — ``None``
                and ``""`` are both treated as "none given"), whether that
                call created the contradiction or extended an already-open
                one (KI-071: previously silently dropped on the "extend"
                branch). A call with no rationale never appends —
                extending membership without an explanation adds no entry,
                it doesn't overwrite the history with a blank one.

        Returns:
            (Contradiction, action) where action is "created" or "extended"

        Raises:
            AuthError: author is not a known principal
            CapabilityError: author's capability is 'read'
            NotFoundError: either assertion id does not exist
            ValidationError: assertions do not share subject and predicate,
                or (KI-050) both are already retracted/superseded and no
                open contradiction already exists for their
                (subject, predicate) to extend — a brand-new contradiction
                must start with at least one eligible winner
        """
        principal = self.backend.get_principal(author)
        if principal is None:
            raise AuthError(f"Principal not found: {author}")
        if principal.default_capability == "read":
            raise CapabilityError(f"Principal {author!r} lacks propose capability")

        # Only the principal/capability check above stays outside the
        # transaction — identity doesn't change concurrently the way a
        # contradiction's or assertion's state can (verified: no code path
        # in either backend ever mutates a principal's default_capability
        # after creation; if one is ever added, this check needs to move
        # inside too). `now` also stays here, matching every sibling write
        # method's own convention (`resolve_contradiction` has the
        # identical now-outside/reads-inside shape already).
        now = self.clock.now()
        with self.backend.transaction():
            # KI-045: the assertion reads, the subject/predicate check, and
            # the existing-open-contradiction lookup below all read mutable
            # state that a concurrent write can change — moved inside the
            # transaction and re-read fresh here (not before it),
            # mechanically identical to KI-035's fix for accept_proposal/
            # reject_proposal/request_changes/resubmit. A stale
            # pre-transaction read of either could otherwise let this call
            # re-flag an assertion that's since become terminal (KI-034's
            # own resurrection bug, reachable again via this race) or
            # extend a contradiction a concurrent resolve_contradiction()
            # has already closed in the gap (update_contradiction_members()
            # has no state guard of its own).
            #
            # get_assertion is status-agnostic: a flagged/superseded assertion
            # must still be resolvable here, e.g. when extending an open contradiction
            a = self.backend.get_assertion(assertion_id_a)
            b = self.backend.get_assertion(assertion_id_b)
            if a is None:
                raise NotFoundError(f"Assertion not found: {assertion_id_a}")
            if b is None:
                raise NotFoundError(f"Assertion not found: {assertion_id_b}")
            if a.subject != b.subject or a.predicate != b.predicate:
                raise ValidationError(
                    "Assertions must share the same subject and predicate to contradict"
                )

            existing = self.backend.get_open_contradiction(
                namespace=self.namespace,
                subject=a.subject,
                predicate=a.predicate,
            )

            if existing is not None:
                merged = list(dict.fromkeys(existing.member_ids + [assertion_id_a, assertion_id_b]))
                # KI-071: rationale was previously silently dropped on this
                # branch - `existing.metadata` was never touched at all.
                # Appended, not overwritten: a rationale-less extend
                # (falsy rationale - None or "", matching the "create"
                # branch's own long-standing truthy check just below)
                # leaves prior history exactly as it was, so `metadata`
                # stays None and this UPDATE doesn't even touch that
                # column (matches update_contradiction_members' own
                # "None means untouched" contract).
                metadata = None
                if rationale:
                    # safe_rationale_history, not a raw .get() (KI-076
                    # review): `existing.metadata` is an open, schema-less
                    # blob (ADR-0041) that anything holding a raw backend
                    # connection could have written in some other shape -
                    # a non-list/non-dict-entries value here previously
                    # raised (a bare `.get()` return isn't guaranteed
                    # iterable) or, worse, silently exploded a string into
                    # one "entry" per character on append. Every read
                    # surface projecting this same field already defends
                    # against exactly this; the one write surface that
                    # reads prior history before appending needs the same
                    # defense, not just the surfaces reading it back.
                    # Unlike a read surface's per-request projection, the
                    # coerced result here IS what gets persisted below -
                    # unparseable prior content is dropped for good, not
                    # just hidden from one response (see the helper's own
                    # docstring for why that's the accepted trade-off).
                    history: list[dict[str, str]] = safe_rationale_history(existing.metadata)
                    history.append({"rationale": rationale, "actor": author, "at": now.isoformat()})
                    metadata = {**existing.metadata, "rationale_history": history}
                self.backend.update_contradiction_members(existing.id, merged, metadata=metadata)
                contradiction_id = existing.id
                action = "extended"
            else:
                # KI-050: a brand-new contradiction must start with at
                # least one eligible winner. resolve_contradiction()
                # already rejects a winner whose status is
                # retracted/superseded (KI-044, ADR-0031) — so a
                # contradiction whose two *founding* members are BOTH
                # already terminal would open unresolvable until some
                # later write extends it with a fresh member, forcing a
                # caller who didn't ask for that outcome to know to work
                # around it. This check only guards this "create" branch;
                # extending an *already-open* contradiction with an
                # all-terminal pair remains permitted below (ADR-0031's own
                # deliberate escape hatch — naming a terminal assertion for
                # audit/context when extending).
                if a.status in ("retracted", "superseded") and b.status in (
                    "retracted",
                    "superseded",
                ):
                    raise ValidationError(
                        f"Cannot open a new contradiction between {assertion_id_a!r} "
                        f"(status={a.status!r}) and {assertion_id_b!r} (status={b.status!r}): "
                        "both are already terminal, leaving no eligible winner for "
                        "resolve_contradiction() to select"
                    )
                contradiction_id = self.id_provider.next()
                self.backend.put_contradiction(
                    Contradiction(
                        id=contradiction_id,
                        namespace=self.namespace,
                        subject=a.subject,
                        predicate=a.predicate,
                        member_ids=[assertion_id_a, assertion_id_b],
                        state="open",
                        created_at=now,
                        raised_by=author,
                        # KI-071: same rationale_history shape the "extend"
                        # branch above appends to, so a reader never has to
                        # special-case "the first entry lives under a
                        # different key than the rest".
                        metadata=(
                            {
                                "rationale_history": [
                                    {"rationale": rationale, "actor": author, "at": now.isoformat()}
                                ]
                            }
                            if rationale
                            else {}
                        ),
                    )
                )
                action = "created"

            for aid, assertion in ((assertion_id_a, a), (assertion_id_b, b)):
                # KI-034: `retracted`/`superseded` are terminal — flagging
                # an assertion explicitly named here must not resurrect one
                # any more than conflict-routing's own contradiction
                # extension may (_apply_with_conflict_routing). Without
                # this, any >= propose-capability principal (including an
                # AI principal over MCP's ontolith.flag_contradiction, which
                # carries no write capability at all) could re-flag a
                # deliberately terminalized assertion back into dispute.
                if assertion.status in ("retracted", "superseded"):
                    continue
                if assertion.status != "flagged":
                    self.backend.set_assertion_status(aid, "flagged")
                    self._record_assertion_event(aid, author, "flagged", now)

        # This re-read happens after the transaction commits (matching
        # resolve_contradiction's identical shape) - KI-045 closes the race
        # on what this call *decides and writes*, not on what the returned
        # object reflects; a write landing between commit and this read
        # could mean the returned Contradiction is already stale by the
        # time the caller sees it. Not new here and not addressed by this
        # fix - same as every other write method's return-then-re-fetch.
        result = self.backend.get_contradiction(contradiction_id)
        assert result is not None
        return result, action

    def record_admin_event(
        self, actor: str, action: AdminAction, target: str, *, detail: str | None = None
    ) -> AdminEvent:
        """Record an append-only admin-action event (KI-060, SPEC §17).

        Does NOT itself check `actor`'s capability — callers are expected
        to have already gated the action this event is documenting (e.g.
        via `require_admin`) before calling this, the same "record, don't
        re-gate" contract `_record_assertion_event`/`ProposalEvent`
        recording already follow elsewhere in this class. Public (not a
        leading-underscore helper) because `PluginRegistry.register()`
        (a different module) needs to call it directly after a successful
        registration, the same way it already calls the public
        `require_admin`/`create_principal`.

        Args:
            actor: Principal ID who performed the action
            action: Which admin action this event records
            target: Free-text identifier of what was acted on
            detail: Optional free-text detail

        Returns:
            The persisted AdminEvent
        """
        event = AdminEvent(
            id=self.id_provider.next(),
            actor=actor,
            action=action,
            target=target,
            at=self.clock.now(),
            detail=detail,
        )
        self.backend.put_admin_event(event)
        return event

    def get_admin_events(
        self, author: str, *, actor: str | None = None, target: str | None = None
    ) -> list[AdminEvent]:
        """List recorded admin events, optionally filtered (KI-072, SPEC §17).

        The only way to answer "who created this principal" or "who
        applied this schema" without direct SDK/`kb.backend` access — the
        gap KI-072 closes, `record_admin_event` having closed the
        recording half back in KI-060. Admin-gated the same way
        `list_tokens` is: an admin action's audit trail is itself
        sensitive (it records who did what, to what), so reading it back
        is scoped the same as issuing/listing tokens, not open to any
        authenticated principal the way ordinary read tools are.

        Args:
            author: Principal ID performing the lookup — must hold `admin`
                capability
            actor: Filter to events performed by this principal ID
            target: Filter to events against this target

        Returns:
            Matching events, oldest first

        Raises:
            AuthError: `author` does not name an existing principal
            CapabilityError: `author` lacks admin capability
        """
        self.require_admin(author)
        return self.backend.get_admin_events(actor=actor, target=target)

    def apply_schema(self, schema: SchemaIR, author: str) -> SchemaIR:
        """Persist a new schema version, capability-checked (SPEC §6).

        The author must have `admin` capability. Versions must be applied in
        strict monotonic order: 1 if no schema exists yet for the namespace,
        otherwise exactly `current_latest + 1`.

        This is the only governed path that reaches `StorageBackend.put_schema()` —
        without it, schema versions could be persisted with no capability check
        by anything holding a `backend` reference directly. Records an
        `AdminEvent` (KI-060) after successful application.

        Args:
            schema: SchemaIR to persist
            author: Principal ID applying the schema

        Returns:
            The persisted SchemaIR

        Raises:
            AuthError: If the author principal is not found
            CapabilityError: If the author lacks `admin` capability
            SchemaError: If `schema.version` is not the next monotonic version

        Note:
            Opens its own `self.backend.transaction()` (to keep the
            schema write and its `AdminEvent` atomic, KI-060) — cannot be
            called from inside an already-open transaction, same
            constraint every other transaction-wrapped `Ontology` write
            method already has.
        """
        self.require_admin(author)

        current = self.backend.get_schema(schema.namespace)
        expected_version = (current.version + 1) if current is not None else 1
        if schema.version != expected_version:
            raise SchemaError(
                f"Schema version {schema.version} is not the next monotonic version "
                f"for namespace {schema.namespace!r} (expected {expected_version})"
            )

        with self.backend.transaction():
            self.backend.put_schema(schema)
            self.record_admin_event(author, "apply_schema", f"{schema.namespace}:v{schema.version}")
        return schema

    def require_admin(self, author: str) -> Principal:
        """Shared gate for admin-level actions (credential issuance/revocation,
        plugin registration): these convert local access into a remote,
        network-reachable capability or a standing in-process actor, so they
        require `admin`, not just whatever capability the target has.

        Rejects AI-kind principals regardless of their configured capability
        (KI-053) — every other capability-tier gate in this module already
        excludes AI-kind principals (`_check_direct_write_capability`,
        `_require_reviewer_principal`, `resolve_contradiction`'s own check),
        `admin` sits above all of them in SPEC §8.3's total order, and it was
        the one gate that didn't. Without this, a misconfigured AI principal
        with `default_capability="admin"` could call `issue_token()` for a
        human principal and then authenticate as that human over REST/GraphQL
        — bypassing every one of those other AI-kind checks and destroying
        attribution on the resulting writes.
        """
        principal = self.backend.get_principal(author)
        if principal is None:
            raise AuthError(f"Principal not found: {author}")
        if principal.default_capability != "admin":
            raise CapabilityError(f"Principal {author} lacks admin capability")
        if principal.kind == "ai":
            raise CapabilityError(f"Principal {author!r} is an AI principal and cannot hold admin")
        return principal

    def list_principals(self, author: str) -> list[Principal]:
        """List all principals (KI-022).

        Args:
            author: Principal ID performing the lookup — must hold `admin`
                capability (principal metadata, including `owner` and
                `trust_level`, is admin-tier information, same sensitivity
                class as credential listing)

        Returns:
            All principals, most recently created first

        Raises:
            AuthError: author does not name an existing principal
            CapabilityError: author lacks admin capability
        """
        self.require_admin(author)
        return self.backend.list_principals()

    def issue_token(self, principal_id: str, author: str) -> tuple[str, str]:
        """Issue a new API-key token for a principal (ADR-0014).

        Returns the raw secret ONCE — only its SHA-256 hash is persisted, and
        the raw value cannot be recovered afterward. Callers must save it
        immediately.

        Token generation uses ``secrets.token_urlsafe`` directly rather than
        the injected ``IdProvider``: this is a deliberate, documented
        exception to the "no non-determinism in domain logic" rule —
        cryptographic unpredictability is the entire point of a secret token,
        unlike ULIDs/timestamps, which are banned from domain logic for
        *reproducibility* reasons that don't apply here. The credential row's
        own id/created_at still go through id_provider/clock for that reason.

        Args:
            principal_id: Principal to issue a token for
            author: Principal ID performing the issuance — must hold `admin`
                capability (issuing a token mints a remote credential, a
                higher-stakes action than the target principal's own
                capability level)

        Returns:
            A ``(token, credential_id)`` tuple. ``token`` is the raw secret
            (not persisted anywhere — save it now). ``credential_id`` is
            returned directly rather than needing to be re-derived via
            ``list_tokens(...)[0]`` (KI-024) — that second, non-transactional
            lookup could race a concurrent issuance for the same principal
            and return a different credential's id.

        Raises:
            AuthError: author or principal_id does not name an existing principal
            CapabilityError: author lacks admin capability
        """
        import secrets

        from ontolith.identity.token_auth import hash_token

        self.require_admin(author)
        principal = self.backend.get_principal(principal_id)
        if principal is None:
            raise AuthError(f"Principal not found: {principal_id}")

        raw_token = secrets.token_urlsafe(32)
        credential = PrincipalCredential(
            id=self.id_provider.next(),
            principal_id=principal_id,
            token_hash=hash_token(raw_token),
            created_at=self.clock.now(),
            issued_by=author,
        )
        self.backend.put_credential(credential)
        return raw_token, credential.id

    def revoke_token(self, credential_id: str, author: str) -> None:
        """Revoke a previously issued token by its credential ID (ADR-0014).

        Args:
            credential_id: Credential to revoke (returned alongside the raw
                token by a token-issuance CLI/tool, not the token itself)
            author: Principal ID performing the revocation — must hold
                `admin` capability

        Raises:
            AuthError: author does not name an existing principal
            CapabilityError: author lacks admin capability
            NotFoundError: No credential with that ID exists
        """
        self.require_admin(author)
        credential = self.backend.get_credential(credential_id)
        if credential is None:
            raise NotFoundError(f"Token credential not found: {credential_id}")
        self.backend.revoke_credential(credential_id, self.clock.now(), author)

    def list_tokens(self, principal_id: str, author: str) -> list[PrincipalCredential]:
        """List all credentials (active and revoked) issued to a principal.

        Never returns the raw token or its hash — only id/created_at/revoked_at,
        enough to identify which credential to pass to ``revoke_token``.

        Args:
            principal_id: Principal to list credentials for
            author: Principal ID performing the lookup — must hold `admin`
                capability

        Returns:
            Credentials for this principal, most recently issued first

        Raises:
            AuthError: author does not name an existing principal
            CapabilityError: author lacks admin capability
        """
        self.require_admin(author)
        return self.backend.get_credentials_for_principal(principal_id)

    def close(self) -> None:
        """Close the knowledge base connection."""
        self.backend.close()


__all__ = ["AsOfView", "Ontology"]
