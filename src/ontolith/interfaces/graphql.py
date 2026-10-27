"""GraphQL interface for Ontolith (SPEC §14.3, ADR-0037).

Exposes the ``Entity``, ``Assertion``, ``Proposal``, ``Contradiction``, and
``Principal`` types SPEC §14.3 names, with ``query``, ``propose``,
``createEntity``, and ``review`` operations "mirroring the SDK" (SPEC's
own phrasing) — narrower than the REST interface (``interfaces/rest.py``,
ADR-0021/0022), which also exposes a direct-write route and
principal/token admin as REST-specific extensions beyond SPEC parity
(KI-022). This module deliberately does not add GraphQL equivalents of
those: no direct-write mutation, no principal creation or token
issuance/revocation. ``principals`` is exposed as a read-only query only,
since SPEC explicitly names ``Principal`` as one of the five exposed
types. ``createEntity`` (KI-082) is propose-tier, matching every other
governed write here — added because, without it, a GraphQL-only caller
could never introduce a genuinely new entity into the KB at all, not
merely reach a narrower surface than REST's.

Authentication (ADR-0014, reused unchanged, same posture as REST/ADR-0021):
every query and mutation requires an ``Authorization: Bearer <token>``
header, resolved server-side via the injected ``AuthProvider`` — never a
caller-asserted principal ID. Unlike REST's ``Depends``-based dependency
(which raises before the route body runs), GraphQL has no single
request-level auth gate: auth resolution happens in ``context_getter``
(storing either the resolved ``Principal`` or the ``AuthError`` in context,
never raising there) and each field resolver calls
``_require_principal(info)`` first, which raises the stored ``AuthError``
if resolution failed — the exception then flows through GraphQL's normal
per-field error path into the response body, same as any other domain
error (see below). This deferred-to-resolver-time design exists so
introspection queries (``__schema``) *can* stay reachable without a token
when a deployment opts into that (``introspection=True`` — see
``create_graphql_app``'s own docstring; ``False`` by default since KI-056,
so this isn't the out-of-the-box behavior).

Error handling: GraphQL has no HTTP-status channel for domain errors the
way REST does (SPEC §16's ``code``/``message``/``detail`` envelope doesn't
map onto HTTP status codes here — by GraphQL convention, every response is
HTTP 200 and errors surface in the response body's ``errors[]`` array).
``_OntolithSchema.process_errors`` is the single centralized hook (mirrors
REST's one ``@app.exception_handler(OntolithError)`` instead of per-route
try/except): for every error whose ``original_error`` is an
``OntolithError``, it sets ``extensions = {"code": ..., "detail": ...}``;
``StorageError``/``PluginError`` (the same two REST maps to 5xx) additionally
get their message redacted to a generic string and the real message logged
server-side only — the raw text can carry internal exception detail (e.g.
sqlite3 constraint/transaction-state messages) a caller has no use for. Any
other resolver exception (not an ``OntolithError`` at all — a genuine bug)
is redacted the same way, with ``extensions.code = "INTERNAL_ERROR"``: REST's
equivalent path gets FastAPI's generic, code-less 500, never the raw
message, and this must fail closed to match rather than leak by omission.

Concurrency (KI-052): every resolver is ``async def``. ``_require_principal``/
``_kb`` are cheap dict lookups and run inline, but everything that touches
``kb``/``kb.backend`` — the actual blocking SQLite/DuckDB I/O — is factored
into a plain sync helper function and run via
``starlette.concurrency.run_in_threadpool``. Unlike REST, whose plain ``def``
routes Starlette dispatches to a thread pool automatically, `strawberry`'s
`GraphQLRouter` executes resolvers inline on the ASGI event loop by default;
without this, one slow resolver would block every concurrent request (not
just database-bound ones) for its full duration — measured directly during
review, closed here.

Usage:
    ```python
    from ontolith.identity.token_auth import TokenAuthProvider
    from ontolith.interfaces.graphql import create_graphql_app
    app = create_graphql_app(kb, TokenAuthProvider(kb.backend))
    # uvicorn.run(app) to serve; GraphiQL served at /graphql by default, but
    # introspection is off by default (KI-056) so it can't load a schema —
    # pass introspection=True for a working interactive dev experience:
    # app = create_graphql_app(kb, TokenAuthProvider(kb.backend), introspection=True)
    ```
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast, get_args

import strawberry
from fastapi import FastAPI, Header
from graphql.error import GraphQLError
from starlette.concurrency import run_in_threadpool
from strawberry.extensions import (
    DisableIntrospection,
    MaxAliasesLimiter,
    QueryDepthLimiter,
    SchemaExtension,
)
from strawberry.fastapi import GraphQLRouter
from strawberry.scalars import JSON

from ontolith.core.errors import (
    AuthError,
    NotFoundError,
    OntolithError,
    PluginError,
    StorageError,
    ValidationError,
)
from ontolith.govern.contradiction import Contradiction, ContradictionState, safe_rationale_history
from ontolith.govern.operations import CreateEntityOp
from ontolith.govern.proposal import Proposal, ProposalState
from ontolith.identity import Principal
from ontolith.ontology import Ontology

if TYPE_CHECKING:
    from strawberry.types.execution import ExecutionContext

    from ontolith.identity.ports import AuthProvider

# Errors whose raw message carries internal exception detail (e.g. sqlite3
# constraint/transaction-state text) rather than caller-actionable
# information — same two types REST maps to a 500 (rest.py's
# _STATUS_BY_ERROR_TYPE). Redacted here rather than importing that dict
# directly: GraphQL doesn't use HTTP status at all, so keying off REST's
# status-code mapping would tie this module to a framing that doesn't apply
# to it. isinstance, not exact type: this automatically redacts any future
# subclass of StorageError/PluginError specifically, without needing this
# tuple updated - but a brand-new OntolithError direct subclass that ISN'T
# one of those two still fails open (unredacted) here, same as it would
# anywhere isinstance is used for this kind of check - a real divergence
# from REST, whose .get(type(exc), 500) defaults an unrecognized exact
# type to 500 and therefore redacts it: REST fails closed for exactly the
# case this isinstance check fails open on.
_REDACT_MESSAGE_FOR: tuple[type[OntolithError], ...] = (StorageError, PluginError)
_GENERIC_SERVER_ERROR_MESSAGE = "An internal error occurred"

# Derived from ProposalState's/ContradictionState's own named Literal alias
# (govern/proposal.py, govern/contradiction.py), not hand-duplicated, so
# neither can silently drift if either type ever gains/loses a state
# (KI-077; mirrors rest.py's/mcp.py's identical constants).
_PROPOSAL_STATES: tuple[str, ...] = get_args(ProposalState)
_CONTRADICTION_STATES: tuple[str, ...] = get_args(ContradictionState)

# Deliberately NOT an ontolith.core.errors.OntolithError code: SPEC §16's
# error taxonomy is for *domain* errors (schema, validation, auth,
# capability, policy, conflict, not-found, storage, plugin) - a bare
# resolver exception that isn't any of those isn't a domain error either,
# it's a bug. REST's equivalent path (an exception process_errors' fallback
# branch below handles) never gets a code at all - it escapes to FastAPI's
# generic, code-less 500. GraphQL's error envelope has no HTTP-status
# channel to fall back to (module docstring), so this exists purely so a
# client can distinguish "an unrecognized resolver failure" from a named
# domain error without over-widening core/errors.py's stable taxonomy for
# one interface's transport-level need.
_INTERNAL_ERROR_CODE = "INTERNAL_ERROR"

# Query-amplification limits (KI-056): always on, not configurable off.
# Found in review: a single authenticated request with many aliased
# `entity { assertions }` selections dispatches that many backend calls in
# one round trip, and — since KI-052 converted resolvers to async, sharing
# the process's anyio worker-thread pool with any co-mounted REST app —
# this became a cross-interface DoS vector, not just a self-inflicted one.
# MaxAliasesLimiter bounds that directly (verified: 15 concurrent worker
# threads per request against the pool's default 40-thread capacity — this
# reduces the amplification ratio, it does not eliminate the vector; a few
# concurrent max-alias requests can still exhaust the shared pool).
# QueryDepthLimiter never bounds `__schema`/`__type` introspection fields
# (a hardcoded, non-overridable carve-out in *strawberry's own*
# depth-limiting validator — this is a strawberry-graphql implementation
# detail, not a graphql-core one; introspection is disabled by default
# instead, see `introspection` below). Separately: no field in this schema
# is recursive (no type nests back into itself), so no schema-valid query
# can reach anywhere near max_depth=10 today — the depth limit is
# forward-looking defense-in-depth for a future recursive relation field,
# not a live mitigation. max_alias_count=15 is the limit doing real work
# right now; real query shapes in this schema never need more than a
# handful of aliases.
_MAX_QUERY_DEPTH = 10
_MAX_ALIAS_COUNT = 15


# ---------------------------------------------------------------------------
# GraphQL types
# ---------------------------------------------------------------------------


@strawberry.type
class PropertyType:
    """A single concept property, as returned by Query.schema."""

    name: str
    type: str
    cardinality: str
    temporality: str
    required: bool


@strawberry.type
class RelationType:
    """A single concept relation, as returned by Query.schema."""

    name: str
    target_concept: str
    cardinality: str
    required: bool
    temporality: str
    inverse: str | None


@strawberry.type
class ConceptType:
    """A single concept and its properties/relations, as returned by Query.schema."""

    name: str
    properties: list[PropertyType]
    relations: list[RelationType]


@strawberry.type
class SchemaType:
    """Response type for Query.schema."""

    namespace: str | None
    version: int | None
    concepts: list[ConceptType]


@strawberry.type
class AssertionType:
    """A single active assertion, as returned nested under an entity."""

    id: str
    predicate: str
    value: str
    value_type: str | None
    confidence: float | None
    author: str
    asserted_at: str


@strawberry.type
class EntityType:
    """An entity's own fields, plus its active assertions as a nested field."""

    id: str
    concept: str
    namespace: str
    natural_key: str | None
    created_at: str
    created_by: str

    @strawberry.field
    async def assertions(self, info: strawberry.Info) -> list[AssertionType]:
        """Currently active assertions for this entity — resolved lazily,
        so a query that only asks for entity fields never pays for it.

        Calls _require_principal itself rather than relying solely on
        Query.entity's own gate (the only current caller) — matches every
        other resolver's own-gate invariant, and stays correct if a future
        field ever returns EntityType through a different path."""
        _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_build_assertions, kb, self.id)


@strawberry.type
class EntitySummaryType:
    """A single entity's summary fields, as returned in Query.query's result list."""

    id: str
    concept: str
    natural_key: str | None
    created_at: str


@strawberry.type
class QueryResultType:
    """Response type for Query.query."""

    concept: str
    count: int
    entities: list[EntitySummaryType]


@strawberry.type
class ReviewEventType:
    """A single review-decision event in an assertion's provenance trail."""

    actor: str
    type: str
    detail: str | None
    at: str


@strawberry.type
class ProvenanceType:
    """Response type for Query.provenance."""

    id: str
    subject: str
    predicate: str
    value: str
    value_type: str | None
    status: str
    author: str
    confidence: float | None
    source: str | None
    rationale: str | None
    model: str | None
    asserted_at: str
    valid_from: str | None
    valid_to: str | None
    proposal_id: str | None
    supersedes: str | None
    superseded_ids: list[str]
    review_events: list[ReviewEventType]


@strawberry.type
class ProposalType:
    """A proposal's summary fields, returned by every proposal query/mutation."""

    id: str
    namespace: str
    author: str
    acting_as: str | None
    state: str
    created_at: str
    decided_at: str | None
    policy_reason: str | None
    reviewers: list[str]


@strawberry.type
class ProposeResultType:
    """Response type for Mutation.propose/resubmitProposal."""

    proposal: ProposalType
    decision: str
    created_entities: JSON | None = None


@strawberry.type
class RationaleEntryType:
    """One entry in a contradiction's ``rationale_history`` (KI-071)."""

    rationale: str
    actor: str
    at: str


@strawberry.type
class ContradictionType:
    """A contradiction's fields, returned by every contradiction query/mutation."""

    id: str
    namespace: str
    subject: str
    predicate: str
    state: str
    member_ids: list[str]
    created_at: str
    raised_by: str | None
    resolved_by: str | None
    resolved_at: str | None
    # KI-075: projected out of `metadata["rationale_history"]` specifically,
    # rather than exposing the raw open metadata blob — GraphQL has no
    # native map scalar (see FilterInput's own comment elsewhere in this
    # module), so a structured list is the standard workaround here too.
    rationale_history: list[RationaleEntryType]


@strawberry.type
class FlagContradictionResultType:
    """Response type for Mutation.flagContradiction."""

    contradiction: ContradictionType
    action: str


@strawberry.type
class PrincipalType:
    """A principal's fields, returned by Query.principals."""

    id: str
    kind: str
    owner: str | None
    auth_method: str
    default_capability: str
    trust_level: int
    created_at: str


@strawberry.input
class FilterInput:
    """One equality/operator filter for ``Query.query`` (GraphQL has no
    native map scalar — an explicit key/value list is the standard
    workaround). ``key`` mirrors REST's ``QueryIn.filters`` dict keys,
    including the ``__contains``/``__gt``/etc. operator-suffix convention
    (KI-039)."""

    key: str
    value: str


@strawberry.input
class ProposeInput:
    """Input for ``Mutation.propose``. Exactly one of (``value`` and
    ``value_type``) or ``target`` must be set — a literal assertion or a
    relation, never both, never neither (mirrors REST's ``ProposeIn``).
    ``supersedes`` (ADR-0050/KI-080, KI-099) names a specific existing
    assertion this one explicitly replaces — only meaningful for a
    cardinality="many", temporality="time_varying" property or relation;
    ``Ontology.propose``/``propose_ref`` reject it otherwise."""

    subject: str
    predicate: str
    value: str | None = None
    value_type: str | None = None
    target: str | None = None
    confidence: float | None = None
    source: str | None = None
    rationale: str | None = None
    acting_as: str | None = None
    model: str | None = None
    supersedes: str | None = None


@strawberry.input
class CreateEntityInput:
    """Input for ``Mutation.createEntity`` (KI-082)."""

    concept: str
    natural_key: str | None = None


@strawberry.input
class ProposeEntityInput:
    """Input for ``Mutation.proposeEntity`` (ADR-0055)."""

    concept: str
    natural_key: str | None = None
    model: str | None = None


def _do_propose_entity(kb: Ontology, author: str, payload: ProposeEntityInput) -> ProposeResultType:
    """Blocking body of Mutation.proposeEntity."""
    proposal, decision = kb.propose_batch(
        [CreateEntityOp(key="entity", concept=payload.concept, natural_key=payload.natural_key)],
        author,
        model=payload.model,
    )
    return ProposeResultType(
        proposal=_proposal_type(proposal),
        decision=type(decision).__name__,
        created_entities=cast(JSON | None, kb.created_entities(proposal.id) or None),
    )


def _do_create_entity(kb: Ontology, author: str, payload: CreateEntityInput) -> EntityType:
    """Blocking body of Mutation.createEntity."""
    entity = kb.create_entity(
        concept=payload.concept,
        author=author,
        natural_key=payload.natural_key,
    )
    return EntityType(
        id=entity.id,
        concept=entity.concept,
        namespace=entity.namespace,
        natural_key=entity.natural_key,
        created_at=entity.created_at.isoformat(),
        created_by=entity.created_by,
    )


def _proposal_type(proposal: Proposal) -> ProposalType:
    """Project a domain Proposal onto its GraphQL type."""
    return ProposalType(
        id=proposal.id,
        namespace=proposal.namespace,
        author=proposal.author,
        acting_as=proposal.acting_as,
        state=proposal.state,
        created_at=proposal.created_at.isoformat(),
        decided_at=proposal.decided_at.isoformat() if proposal.decided_at else None,
        policy_reason=proposal.policy_reason,
        reviewers=proposal.reviewers,
    )


def _contradiction_type(contradiction: Contradiction) -> ContradictionType:
    """Project a domain Contradiction onto its GraphQL type."""
    return ContradictionType(
        id=contradiction.id,
        namespace=contradiction.namespace,
        subject=contradiction.subject,
        predicate=contradiction.predicate,
        state=contradiction.state,
        member_ids=contradiction.member_ids,
        created_at=contradiction.created_at.isoformat(),
        raised_by=contradiction.raised_by,
        resolved_by=contradiction.resolved_by,
        resolved_at=contradiction.resolved_at.isoformat() if contradiction.resolved_at else None,
        # safe_rationale_history, not direct indexing: `metadata` is an
        # open blob (ADR-0041) with no schema enforcement, and a malformed
        # entry here must not take down every OTHER contradiction in the
        # same `Query.contradictions` list (KI-075 review, round 2 — a
        # plain `.get(key, default)` per field still raised on a non-dict
        # entry, or passed a present-but-`None` value straight through to
        # a non-nullable GraphQL field).
        rationale_history=[
            RationaleEntryType(rationale=e["rationale"], actor=e["actor"], at=e["at"])
            for e in safe_rationale_history(contradiction.metadata)
        ],
    )


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


def _kb(info: strawberry.Info) -> Ontology:
    """Fetch the Ontology instance stashed in context by create_graphql_app."""
    kb = info.context["kb"]
    assert isinstance(kb, Ontology)
    return kb


def _require_principal(info: strawberry.Info) -> Principal:
    """Resolve the calling Principal, raising the stored error (set by
    context_getter) if the bearer token was missing/malformed/invalid, or
    if resolving it failed for a different domain reason (e.g. a
    StorageError from the backend lookup — context_getter catches
    OntolithError broadly, not just AuthError).

    Deferred to resolver-time rather than raised in context_getter itself
    so unauthenticated introspection queries *can* stay reachable when a
    deployment opts into introspection=True — mirrors REST's
    _resolve_principal, just triggered per-field instead of per-request
    (module docstring).
    """
    principal = info.context.get("principal")
    if principal is None:
        error = info.context["auth_error"]
        assert isinstance(error, OntolithError)
        raise error
    assert isinstance(principal, Principal)
    return principal


# ---------------------------------------------------------------------------
# Resolver helpers (sync, blocking - always run via run_in_threadpool,
# never called directly from a resolver, module docstring's Concurrency
# section)
# ---------------------------------------------------------------------------


def _build_assertions(kb: Ontology, entity_id: str) -> list[AssertionType]:
    """Blocking body of EntityType.assertions."""
    active = kb.backend.assertions(subject=entity_id, status="active")
    return [
        AssertionType(
            id=a.id,
            predicate=a.predicate,
            value=a.value,
            value_type=a.value_type,
            confidence=a.confidence,
            author=a.author,
            asserted_at=a.asserted_at.isoformat(),
        )
        for a in active
    ]


def _build_schema(kb: Ontology, namespace: str) -> SchemaType:
    """Blocking body of Query.schema."""
    ir = kb.backend.get_schema(namespace)
    if ir is None:
        return SchemaType(namespace=None, version=None, concepts=[])
    concepts = [
        ConceptType(
            name=concept_name,
            properties=[
                PropertyType(
                    name=prop_name,
                    type=prop_def.value_type,
                    cardinality=prop_def.cardinality,
                    temporality=prop_def.temporality,
                    required=prop_def.required,
                )
                for prop_name, prop_def in concept_def.properties.items()
            ],
            relations=[
                RelationType(
                    name=rel_name,
                    target_concept=rel_def.target_concept,
                    cardinality=rel_def.cardinality,
                    required=rel_def.required,
                    temporality=rel_def.temporality,
                    inverse=rel_def.inverse,
                )
                for rel_name, rel_def in concept_def.relations.items()
            ],
        )
        for concept_name, concept_def in ir.concepts.items()
    ]
    return SchemaType(namespace=namespace, version=ir.version, concepts=concepts)


def _build_entity(kb: Ontology, entity_id: str) -> EntityType:
    """Blocking body of Query.entity."""
    entity = kb.backend.get_entity(entity_id)
    if entity is None:
        raise NotFoundError(f"Entity {entity_id!r} not found")
    return EntityType(
        id=entity.id,
        concept=entity.concept,
        namespace=entity.namespace,
        natural_key=entity.natural_key,
        created_at=entity.created_at.isoformat(),
        created_by=entity.created_by,
    )


def _execute_query(
    kb: Ontology,
    concept: str,
    filters: list[FilterInput] | None,
    semantic: str | None,
    min_confidence: float | None,
    trust_at_least: int | None,
    limit: int | None,
    include_flagged: bool,
    include_history: bool,
) -> QueryResultType:
    """Blocking body of Query.query."""
    builder = kb.query(concept)
    if filters:
        keys = [f.key for f in filters]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise ValidationError(f"Duplicate filter keys: {duplicates}")
        builder = builder.where(**{f.key: f.value for f in filters})
    if semantic is not None:
        builder = builder.semantic(semantic)
    if min_confidence is not None:
        builder = builder.min_confidence(min_confidence)
    if trust_at_least is not None:
        builder = builder.trust_at_least(trust_at_least)
    if include_flagged:
        builder = builder.include_flagged()
    if include_history:
        builder = builder.include_history()
    if limit is not None:
        builder = builder.limit(limit)
    entities = builder.all()
    return QueryResultType(
        concept=concept,
        count=len(entities),
        entities=[
            EntitySummaryType(
                id=e.id,
                concept=e.concept,
                natural_key=e.natural_key,
                created_at=e.created_at.isoformat(),
            )
            for e in entities
        ],
    )


def _build_provenance(kb: Ontology, assertion_id: str) -> ProvenanceType:
    """Blocking body of Query.provenance."""
    prov = kb.provenance(assertion_id)  # raises NotFoundError, mapped by the error handler
    a = prov.assertion
    return ProvenanceType(
        id=a.id,
        subject=a.subject,
        predicate=a.predicate,
        value=a.value,
        value_type=a.value_type,
        status=a.status,
        author=a.author,
        confidence=a.confidence,
        source=a.source,
        rationale=a.rationale,
        model=a.model,
        asserted_at=a.asserted_at.isoformat(),
        valid_from=a.valid_from.isoformat() if a.valid_from else None,
        valid_to=a.valid_to.isoformat() if a.valid_to else None,
        proposal_id=a.proposal_id,
        supersedes=a.supersedes,
        superseded_ids=list(prov.superseded_ids),
        review_events=[
            ReviewEventType(actor=e.actor, type=e.type, detail=e.detail, at=e.at.isoformat())
            for e in prov.review_events
        ],
    )


def _list_proposals(kb: Ontology, state: str | None) -> list[ProposalType]:
    """Blocking body of Query.proposals.

    Raises:
        ValidationError: ``state`` is none of the accepted values — an
            unrecognized value previously reached ``kb.proposals()``'s own
            ``WHERE state = ?`` unfiltered and silently matched zero rows,
            indistinguishable from "no proposals in that state" (KI-077).
    """
    if state not in (None, *_PROPOSAL_STATES, "pending", "all"):
        raise ValidationError(f"Invalid state: {state!r}")
    effective_state = None if state == "all" else state
    return [_proposal_type(p) for p in kb.proposals(state=effective_state)]


def _list_contradictions(kb: Ontology, state: str | None) -> list[ContradictionType]:
    """Blocking body of Query.contradictions.

    Raises:
        ValidationError: ``state`` is none of "open"/"resolved"/"all" — an
            unrecognized value previously reached ``kb.contradictions()``'s
            own ``WHERE state = ?`` unfiltered and silently matched zero
            rows, indistinguishable from "no contradictions in that state"
            (KI-077; same class of bug KI-076 fixed for MCP's
            ``ontolith.list_contradictions``).
    """
    if state not in (None, *_CONTRADICTION_STATES, "all"):
        raise ValidationError(f"Invalid state: {state!r}")
    effective_state = None if state == "all" else state
    return [_contradiction_type(c) for c in kb.contradictions(state=effective_state)]


def _list_principals(kb: Ontology, author: str) -> list[PrincipalType]:
    """Blocking body of Query.principals."""
    return [
        PrincipalType(
            id=p.id,
            kind=p.kind,
            owner=p.owner,
            auth_method=p.auth_method,
            default_capability=p.default_capability,
            trust_level=p.trust_level,
            created_at=p.created_at.isoformat(),
        )
        for p in kb.list_principals(author=author)
    ]


def _do_propose(kb: Ontology, author: str, payload: ProposeInput) -> ProposeResultType:
    """Blocking body of Mutation.propose."""
    has_literal = payload.value is not None and payload.value_type is not None
    has_ref = payload.target is not None
    if has_literal == has_ref:
        raise ValidationError("Provide exactly one of (value and value_type) or target")

    if has_ref:
        assert payload.target is not None
        proposal, decision = kb.propose_ref(
            subject=payload.subject,
            predicate=payload.predicate,
            target=payload.target,
            author=author,
            confidence=payload.confidence,
            source=payload.source,
            rationale=payload.rationale,
            acting_as=payload.acting_as,
            model=payload.model,
            supersedes=payload.supersedes,
        )
    else:
        assert payload.value is not None and payload.value_type is not None
        proposal, decision = kb.propose(
            subject=payload.subject,
            predicate=payload.predicate,
            value=payload.value,
            value_type=payload.value_type,
            author=author,
            confidence=payload.confidence,
            source=payload.source,
            rationale=payload.rationale,
            acting_as=payload.acting_as,
            model=payload.model,
            supersedes=payload.supersedes,
        )
    return ProposeResultType(proposal=_proposal_type(proposal), decision=type(decision).__name__)


def _do_accept_proposal(kb: Ontology, proposal_id: str, reviewer: str) -> ProposalType:
    """Blocking body of Mutation.acceptProposal."""
    return _proposal_type(kb.accept_proposal(proposal_id, reviewer))


def _do_reject_proposal(kb: Ontology, proposal_id: str, reviewer: str, reason: str) -> ProposalType:
    """Blocking body of Mutation.rejectProposal."""
    return _proposal_type(kb.reject_proposal(proposal_id, reviewer, reason=reason))


def _do_request_changes(kb: Ontology, proposal_id: str, reviewer: str, reason: str) -> ProposalType:
    """Blocking body of Mutation.requestChanges."""
    return _proposal_type(kb.request_changes(proposal_id, reviewer, reason=reason))


def _do_assign_reviewers(
    kb: Ontology, proposal_id: str, reviewers: list[str], actor: str
) -> ProposalType:
    """Blocking body of Mutation.assignReviewers (SPEC §9.4, KI-078/KI-079)."""
    return _proposal_type(kb.assign_reviewers(proposal_id, reviewers, actor))


def _do_resubmit_proposal(kb: Ontology, proposal_id: str, author: str) -> ProposeResultType:
    """Blocking body of Mutation.resubmitProposal."""
    proposal, decision = kb.resubmit(proposal_id, author)
    return ProposeResultType(proposal=_proposal_type(proposal), decision=type(decision).__name__)


def _do_retract(
    kb: Ontology, assertion_id: str, author: str, acting_as: str | None
) -> ProposeResultType:
    """Blocking body of Mutation.retract."""
    proposal, decision = kb.retract(assertion_id, author=author, acting_as=acting_as)
    return ProposeResultType(proposal=_proposal_type(proposal), decision=type(decision).__name__)


def _do_flag_contradiction(
    kb: Ontology,
    assertion_id_a: str,
    assertion_id_b: str,
    author: str,
    rationale: str | None,
) -> FlagContradictionResultType:
    """Blocking body of Mutation.flagContradiction."""
    contradiction, action = kb.flag_contradiction(
        assertion_id_a, assertion_id_b, author, rationale=rationale
    )
    return FlagContradictionResultType(
        contradiction=_contradiction_type(contradiction), action=action
    )


def _do_resolve_contradiction(
    kb: Ontology, contradiction_id: str, winner_assertion_id: str, resolver: str
) -> ContradictionType:
    """Blocking body of Mutation.resolveContradiction."""
    return _contradiction_type(
        kb.resolve_contradiction(contradiction_id, winner_assertion_id, resolver)
    )


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


@strawberry.type
class Query:
    """Root Query type — read operations (module docstring)."""

    @strawberry.field
    async def schema(self, info: strawberry.Info, namespace: str = "default") -> SchemaType:
        """Return the schema (concepts, properties, relations) for a namespace."""
        _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_build_schema, kb, namespace)

    @strawberry.field
    async def entity(self, info: strawberry.Info, id: str) -> EntityType:
        """Fetch a single entity by id. Its assertions are a nested field
        (EntityType.assertions), resolved lazily on request."""
        _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_build_entity, kb, id)

    @strawberry.field
    async def query(
        self,
        info: strawberry.Info,
        concept: str,
        filters: list[FilterInput] | None = None,
        semantic: str | None = None,
        min_confidence: float | None = None,
        trust_at_least: int | None = None,
        limit: int | None = None,
        include_flagged: bool = False,
        include_history: bool = False,
    ) -> QueryResultType:
        """Query entities of a concept, optionally filtered/ranked (mirrors
        REST's POST /query). `include_flagged`/`include_history` (KI-094)
        widen which assertion statuses `filters`/`min_confidence`/
        `trust_at_least` may match against — SPEC §11.2/§10.3, ADR-0048."""
        _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(
            _execute_query,
            kb,
            concept,
            filters,
            semantic=semantic,
            min_confidence=min_confidence,
            trust_at_least=trust_at_least,
            limit=limit,
            include_flagged=include_flagged,
            include_history=include_history,
        )

    @strawberry.field
    async def provenance(self, info: strawberry.Info, assertion_id: str) -> ProvenanceType:
        """Return the full provenance record for a single assertion (mirrors
        REST's GET /provenance/{id})."""
        _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_build_provenance, kb, assertion_id)

    @strawberry.field
    async def proposals(
        self, info: strawberry.Info, state: str | None = "require_review"
    ) -> list[ProposalType]:
        """List proposals, defaulting to those pending review. Pass
        state="all" to list every state (mirrors REST's GET /proposals,
        including its "all" sentinel — see that route for why one is
        needed)."""
        # _require_principal runs, and can raise AuthError, before
        # _list_proposals (which validates state, KI-077) is even
        # scheduled - an unauthenticated caller never gets a free pre-auth
        # probe of the accepted-value set (matches MCP's own
        # ontolith.list_contradictions, KI-076 review; pinned by
        # test_bad_token_reports_auth_error_over_bad_state, round 2). This
        # is also why `state` stays a plain `String`, not a GraphQL enum:
        # an enum argument is checked during document validation, before
        # any resolver runs at all - switching to one would silently move
        # this check ahead of auth, the opposite of what this ordering
        # exists for.
        _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_list_proposals, kb, state)

    @strawberry.field
    async def contradictions(
        self, info: strawberry.Info, state: str | None = "open"
    ) -> list[ContradictionType]:
        """List contradictions, defaulting to open ones. Pass state="all"
        for every state (mirrors REST's GET /contradictions)."""
        # Structurally after auth, not just textually — see
        # Query.proposals's identical comment above.
        _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_list_contradictions, kb, state)

    @strawberry.field
    async def principals(self, info: strawberry.Info) -> list[PrincipalType]:
        """List all principals. Requires admin capability (gated inside
        Ontology.list_principals, same as REST's GET /principals)."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_list_principals, kb, principal.id)


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------


@strawberry.type
class Mutation:
    """Root Mutation type — entity creation, propose, and review operations
    (module docstring)."""

    @strawberry.mutation
    async def create_entity(self, info: strawberry.Info, input: CreateEntityInput) -> EntityType:
        """Create a new entity (KI-082). Propose-tier — same capability
        gate as every other governed write (``Ontology.create_entity``
        rejects a ``read``-only principal). An ``ai``-kind principal is
        rejected too and uses ``proposeEntity`` instead (ADR-0055). The acting principal is
        resolved from the bearer token (ADR-0014), never taken from the
        input (mirrors REST's POST /entities)."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_do_create_entity, kb, principal.id, input)

    @strawberry.mutation
    async def propose_entity(
        self, info: strawberry.Info, input: ProposeEntityInput
    ) -> ProposeResultType:
        """Propose a new entity as a one-operation batch (ADR-0055). The
        route for an ``ai``-kind principal, whose ``createEntity`` is
        rejected: under the default policy the proposal goes to review and a
        rejected one leaves no entity behind. ``model`` is required for an AI
        principal. The acting principal is resolved from the bearer token
        (ADR-0014)."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_do_propose_entity, kb, principal.id, input)

    @strawberry.mutation
    async def propose(self, info: strawberry.Info, input: ProposeInput) -> ProposeResultType:
        """Create a proposal to assert a fact or relation. Does NOT write
        directly. The acting principal is resolved from the bearer token
        (ADR-0014), never taken from the input (mirrors REST's POST
        /proposals)."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_do_propose, kb, principal.id, input)

    @strawberry.mutation
    async def accept_proposal(self, info: strawberry.Info, proposal_id: str) -> ProposalType:
        """Accept a pending proposal, replaying its operations. Requires
        review or admin capability; the reviewer must not be the
        proposal's own author or delegate (self-review is blocked)."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_do_accept_proposal, kb, proposal_id, principal.id)

    @strawberry.mutation
    async def reject_proposal(
        self, info: strawberry.Info, proposal_id: str, reason: str = ""
    ) -> ProposalType:
        """Reject a pending proposal. Requires review or admin capability;
        same self-review guard as accept."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_do_reject_proposal, kb, proposal_id, principal.id, reason)

    @strawberry.mutation
    async def request_changes(
        self, info: strawberry.Info, proposal_id: str, reason: str = ""
    ) -> ProposalType:
        """Request changes on a pending proposal (SPEC §9.1/§9.4's third
        under_review outcome, alongside accept/reject). Requires review or
        admin capability; same self-review guard as accept/reject."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_do_request_changes, kb, proposal_id, principal.id, reason)

    @strawberry.mutation
    async def assign_reviewers(
        self, info: strawberry.Info, proposal_id: str, reviewers: list[str]
    ) -> ProposalType:
        """Reassign a pending proposal's reviewers (SPEC §9.4's `assign`
        action, KI-078/KI-079). Requires review or admin capability; same
        self-review guard as accept/reject/requestChanges — replaces the
        reviewer list wholesale, including clearing it via an empty list."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(
            _do_assign_reviewers, kb, proposal_id, reviewers, principal.id
        )

    @strawberry.mutation
    async def resubmit_proposal(self, info: strawberry.Info, proposal_id: str) -> ProposeResultType:
        """Resubmit a proposal after changes were requested (KI-027),
        re-running policy evaluation against the unedited payload. Only the
        proposal's own author or delegate may call this."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_do_resubmit_proposal, kb, proposal_id, principal.id)

    @strawberry.mutation
    async def retract(
        self, info: strawberry.Info, assertion_id: str, acting_as: str | None = None
    ) -> ProposeResultType:
        """Propose retraction of an assertion through the governed
        proposal/policy pipeline (SPEC §9) — does NOT delete or write
        directly, mirrors REST's POST /assertions/{id}/retract. When
        acting_as is set the retraction is made on behalf of another
        principal (delegation, ADR-0003)."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(_do_retract, kb, assertion_id, principal.id, acting_as)

    @strawberry.mutation
    async def flag_contradiction(
        self,
        info: strawberry.Info,
        assertion_id_a: str,
        assertion_id_b: str,
        rationale: str | None = None,
    ) -> FlagContradictionResultType:
        """Flag two assertions as contradictory, opening or extending a
        contradiction. Requires propose capability or higher (ADR-0008)."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(
            _do_flag_contradiction, kb, assertion_id_a, assertion_id_b, principal.id, rationale
        )

    @strawberry.mutation
    async def resolve_contradiction(
        self, info: strawberry.Info, contradiction_id: str, winner_assertion_id: str
    ) -> ContradictionType:
        """Resolve an open contradiction by selecting a winning assertion.
        Requires review or admin capability."""
        principal = _require_principal(info)
        kb = _kb(info)
        return await run_in_threadpool(
            _do_resolve_contradiction, kb, contradiction_id, winner_assertion_id, principal.id
        )


# ---------------------------------------------------------------------------
# Schema / app factory
# ---------------------------------------------------------------------------


class _OntolithSchema(strawberry.Schema):
    """Centralizes OntolithError -> GraphQL error-extensions mapping, one
    place instead of per-resolver try/except (mirrors REST's single
    @app.exception_handler(OntolithError), module docstring).

    Takes the bound `Ontology` itself at construction time, not just its
    `observability` sink (SPEC §18/ADR-0044) — `process_errors` has no other
    way to reach it, since strawberry constructs/calls this class outside
    any per-request closure over `kb`. Reading `kb.observability` live on
    each call, the same as REST/MCP/the plugin registry do, rather than
    snapshotting the sink at construction time, means reassigning
    `kb.observability` after the app is built still takes effect here too.
    """

    def __init__(self, *args: Any, kb: Ontology, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._kb = kb

    def process_errors(
        self,
        errors: list[GraphQLError],
        execution_context: ExecutionContext | None = None,
    ) -> None:
        """Map each OntolithError to extensions={code, detail}. Any other
        resolver-raised exception (a genuine bug, not a domain error) is
        redacted the same way — REST's default (unhandled-exception ->
        generic 500, FastAPI/Starlette's own behavior) never returns a raw
        exception message either, and this must fail closed to match, not
        open. A None original_error (e.g. a GraphQL parse/validation
        failure, never reaching a resolver) is not a resolver exception at
        all and is left to the default (unredacted) behavior — that text
        describes the query's own shape, not internal server state."""
        for error in errors:
            original = error.original_error
            if isinstance(original, OntolithError):
                if isinstance(original, _REDACT_MESSAGE_FOR):
                    self._kb.observability.log(logging.ERROR, original.message, code=original.code)
                    error.message = _GENERIC_SERVER_ERROR_MESSAGE
                error.extensions = {"code": original.code, "detail": original.detail}
            elif original is not None:
                self._kb.observability.log(
                    logging.ERROR,
                    "Unhandled exception in GraphQL resolver",
                    exc_info=original,
                )
                error.message = _GENERIC_SERVER_ERROR_MESSAGE
                error.extensions = {"code": _INTERNAL_ERROR_CODE, "detail": {}}
            else:
                super().process_errors([error], execution_context)


def create_graphql_app(
    kb: Ontology,
    auth_provider: AuthProvider,
    name: str = "ontolith",
    *,
    graphql_ide: Literal["graphiql", "apollo-sandbox", "pathfinder"] | None = "graphiql",
    introspection: bool = False,
    docs_url: str | None = "/docs",
    redoc_url: str | None = "/redoc",
    openapi_url: str | None = "/openapi.json",
) -> FastAPI:
    """Build and return a FastAPI app serving GraphQL at ``/graphql``.

    Args:
        kb: Ontology instance (knowledge base) to expose.
        auth_provider: Resolves caller-supplied bearer tokens to Principals
            (ADR-0014) — e.g. ``TokenAuthProvider(kb.backend)``.
        name: API title.
        graphql_ide: Which IDE to serve at GET /graphql ("graphiql",
            "apollo-sandbox", "pathfinder"), or None to disable it.
            Unauthenticated, like REST's docs_url — it exposes the API's
            shape via the IDE's own introspection call, not its data.
        introspection: Whether ``__schema``/``__type`` introspection
            queries are answered at all. False by default (KI-056):
            introspection queries are self-referentially recursive over the
            schema's own type graph, and depth/alias limiting can't bound
            them (`QueryDepthLimiter` hardcodes an introspection carve-out
            in *strawberry's own* depth-limiting validator, not
            graphql-core's — see the module-level comment above
            `_MAX_QUERY_DEPTH`) — so unlike REST's `docs_url` (a fixed,
            bounded JSON document with no recursive amplification
            potential), leaving introspection on by default would leave an
            *unauthenticated* recursive-response amplification vector open
            with no other mitigation in this factory. Set this True for a
            deployment that accepts that tradeoff (local development,
            trusted-network staging, or any deployment that wants
            self-documenting tooling and doesn't face untrusted traffic).
            Leaving ``graphql_ide`` at its default while this is False still
            serves the IDE page itself (GET /graphql returns 200), but the
            IDE won't be able to load a schema through it either — pass
            ``introspection=True`` alongside the default ``graphql_ide`` for
            a working interactive dev experience (see ``Usage`` above).
        docs_url: FastAPI Swagger UI path, or None to disable it. Kept for
            parity with ``create_rest_app`` — this app has no REST routes
            of its own, but mounting under a shared FastAPI app is a
            documented usage pattern, and the FastAPI docs surface exists
            regardless.
        redoc_url: ReDoc path, or None to disable it.
        openapi_url: OpenAPI schema JSON path, or None to disable it (also
            disables docs_url/redoc_url, which depend on it).

    Returns:
        Configured FastAPI app with the query/propose/review surface
        (ADR-0037).
    """
    app = FastAPI(title=name, docs_url=docs_url, redoc_url=redoc_url, openapi_url=openapi_url)

    # Factory callables, not instances: passing an already-constructed
    # extension instance to `extensions=[...]` is deprecated in strawberry
    # (a fresh instance must be built per request).
    extensions: list[Callable[[], SchemaExtension]] = [
        lambda: QueryDepthLimiter(max_depth=_MAX_QUERY_DEPTH),
        lambda: MaxAliasesLimiter(max_alias_count=_MAX_ALIAS_COUNT),
    ]
    if not introspection:
        extensions.append(DisableIntrospection)
    schema = _OntolithSchema(query=Query, mutation=Mutation, extensions=extensions, kb=kb)

    async def _get_context(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        """Resolve the bearer token into context, never raising here (module docstring).

        Catches OntolithError broadly, not just AuthError: auth_provider.resolve()
        is a StorageBackend-backed lookup and can raise StorageError too — that
        must flow through the same deferred-to-resolver-time path (and so
        through process_errors' redaction) rather than escaping this
        dependency as a raw, unmapped 500 with no SPEC §16 envelope at all.
        """
        context: dict[str, object] = {"kb": kb}
        if authorization is None or not authorization.startswith("Bearer "):
            context["auth_error"] = AuthError("Missing or malformed Authorization header")
            return context
        token = authorization.removeprefix("Bearer ")
        try:
            context["principal"] = await run_in_threadpool(auth_provider.resolve, token)
        except OntolithError as exc:
            context["auth_error"] = exc
        return context

    graphql_router = GraphQLRouter[dict[str, object], None](
        schema, context_getter=_get_context, graphql_ide=graphql_ide
    )
    app.include_router(graphql_router, prefix="/graphql")
    return app


__all__ = ["create_graphql_app"]
