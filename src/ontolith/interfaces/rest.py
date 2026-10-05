"""REST interface for Ontolith (SPEC §14.3, ADR-0021).

Exposes read, propose, direct write, retraction (governed, KI-057), proposal
review (accept/reject/request_changes/resubmit/assign, KI-078), contradiction
listing/flagging/resolution, namespace listing, and principal/token admin
over HTTP.
Full SPEC §14.3
resource parity (``/query`` offset pagination, GraphQL) is out of scope
(KI-022).

Authentication (ADR-0014, reused unchanged): every route requires an
``Authorization: Bearer <token>`` header, resolved server-side via the
injected AuthProvider — never a caller-asserted principal ID. This includes
read routes, a deliberate divergence from the *shipped* MCP server's
unauthenticated read tools (KI-021 tracks closing that gap on the MCP
side). Per ADR-0021 §2, authentication *is* the read-capability check —
any successfully-authenticated principal can read any entity, assertion,
provenance record, or proposal (including other principals' `source`/
`rationale` via ``GET /proposals``) in this namespace; there is no
per-principal or per-namespace read scoping in this slice.

FastAPI serves interactive docs (``/docs``, ``/redoc``, ``/openapi.json``)
unauthenticated by default, exposing the API's shape (not its data) to
anonymous callers. Pass ``docs_url=None, redoc_url=None,
openapi_url=None`` to disable them for a given deployment — this factory
forwards those three straight to ``FastAPI(...)``, defaulting to
FastAPI's own (docs-enabled) behavior when omitted.

Usage:
    ```python
    from ontolith.identity.token_auth import TokenAuthProvider
    from ontolith.interfaces.rest import create_rest_app
    app = create_rest_app(kb, TokenAuthProvider(kb.backend))
    # uvicorn.run(app) to serve
    ```
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, get_args

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ontolith.core.errors import (
    AuthError,
    CapabilityError,
    ConflictError,
    NotFoundError,
    OntolithError,
    PluginError,
    PolicyDenied,
    SchemaError,
    StorageError,
    ValidationError,
)
from ontolith.govern.contradiction import ContradictionState
from ontolith.govern.proposal import ProposalState
from ontolith.identity import Principal

if TYPE_CHECKING:
    from ontolith.identity.ports import AuthProvider
    from ontolith.ontology import Ontology

# Derived from ProposalState's/ContradictionState's own named Literal alias
# (govern/proposal.py, govern/contradiction.py), not hand-duplicated, so
# neither can silently drift if either type ever gains/loses a state
# (KI-077) — every interface that validates a state filter (MCP's own
# ontolith.list_contradictions included) derives from the same alias.
_PROPOSAL_STATES: tuple[str, ...] = get_args(ProposalState)
_CONTRADICTION_STATES: tuple[str, ...] = get_args(ContradictionState)

_STATUS_BY_ERROR_TYPE: dict[type[OntolithError], int] = {
    ValidationError: 400,
    SchemaError: 400,
    AuthError: 401,
    CapabilityError: 403,
    PolicyDenied: 403,
    ConflictError: 409,
    NotFoundError: 404,
    StorageError: 500,
    PluginError: 500,
}

# 5xx error messages (StorageError/PluginError) interpolate raw internal
# exception text (e.g. sqlite3 constraint/transaction-state messages) — not
# secrets, but more internal detail than a caller needs. Redacted in the
# response; the real message is logged server-side instead (SPEC §16 still
# gets a stable `code`, just not the raw text).
_GENERIC_SERVER_ERROR_MESSAGE = "An internal error occurred"


# ---------------------------------------------------------------------------
# Response/request schemas
# ---------------------------------------------------------------------------


class PropertyOut(BaseModel):
    """A single concept property as returned by GET /schema."""

    name: str
    type: str
    cardinality: str
    temporality: str
    required: bool


class RelationOut(BaseModel):
    """A single concept relation as returned by GET /schema (KI-029)."""

    name: str
    target_concept: str
    cardinality: str
    required: bool
    temporality: str
    inverse: str | None = None


class ConceptOut(BaseModel):
    """A single concept and its properties/relations, as returned by GET /schema."""

    name: str
    properties: list[PropertyOut]
    relations: list[RelationOut]


class SchemaOut(BaseModel):
    """Response body for GET /schema."""

    namespace: str | None = None
    version: int | None = None
    concepts: list[ConceptOut]


class ErrorOut(BaseModel):
    """Response body for every OntolithError (SPEC §16)."""

    code: str
    message: str
    detail: dict[str, Any]


class AssertionOut(BaseModel):
    """A single active assertion, as returned nested under an entity."""

    id: str
    predicate: str
    value: str
    value_type: str | None
    confidence: float | None
    author: str
    asserted_at: str


class CreateEntityIn(BaseModel):
    """Request body for POST /entities (KI-082).

    No `author` field — the entity's `created_by` provenance is the
    acting principal, resolved from the bearer token (ADR-0014), never
    taken from the request body.
    """

    concept: str
    natural_key: str | None = None


class EntityOut(BaseModel):
    """An entity's own fields, without its assertions."""

    id: str
    concept: str
    namespace: str
    natural_key: str | None
    created_at: str
    created_by: str


class EntityDetailOut(BaseModel):
    """Response body for GET /entities/{entity_id}."""

    entity: EntityOut
    assertions: list[AssertionOut]


class EntitySummaryOut(BaseModel):
    """A single entity's summary fields, as returned in a query result list."""

    id: str
    concept: str
    natural_key: str | None
    created_at: str


class QueryIn(BaseModel):
    """Request body for POST /query.

    No ``namespace`` field: ``Ontology.namespace`` is hardcoded to
    ``"default"`` (M1 limitation), so ``kb.query()`` has no namespace
    argument to forward one to.
    """

    concept: str
    # Property/relation name -> value (equality), or property__op -> value using a
    # closed set of lookup-operator suffixes (KI-039): __contains (substring),
    # __gt/__lt/__gte/__lte (numeric range, schema-declared Integer/Float properties
    # only). A relation filter (bare key, no suffix) matches the relation's target
    # entity id. Multi-hop double-underscore keys (e.g. "employer__name") and any
    # suffix outside the closed operator set are still not supported and return a
    # 400 validation_error (ADR-0027, KI-030).
    filters: dict[str, str] | None = None
    semantic: str | None = None
    min_confidence: float | None = None
    trust_at_least: int | None = None
    limit: int | None = None
    # KI-094: match-set wideners (SPEC §11.2/§10.3, ADR-0048) — widen which
    # assertion statuses `filters`/`min_confidence`/`trust_at_least` may
    # match against. False by default, same as QueryBuilder itself.
    include_flagged: bool = False
    include_history: bool = False


class QueryOut(BaseModel):
    """Response body for POST /query."""

    concept: str
    count: int
    entities: list[EntitySummaryOut]


class ReviewEventOut(BaseModel):
    """A single review-decision event in an assertion's provenance trail."""

    actor: str
    type: str
    detail: str | None
    at: str


class ProvenanceOut(BaseModel):
    """Response body for GET /provenance/{assertion_id}."""

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
    review_events: list[ReviewEventOut]


class ProposeIn(BaseModel):
    """Request body for POST /proposals.

    Exactly one of (``value`` and ``value_type``) or ``target`` must be
    set — a literal assertion or a relation, never both, never neither.
    ``supersedes`` (ADR-0050/KI-080, KI-099) names a specific existing
    assertion this one explicitly replaces — only meaningful for a
    cardinality="many", temporality="time_varying" property or
    relation; ``Ontology.propose``/``propose_ref`` reject it otherwise.
    """

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


class ProposalOut(BaseModel):
    """A proposal's summary fields, returned by both proposal routes."""

    id: str
    namespace: str
    author: str
    acting_as: str | None
    state: str
    created_at: str
    decided_at: str | None
    policy_reason: str | None
    reviewers: list[str]


class ProposeOut(BaseModel):
    """Response body for POST /proposals."""

    proposal: ProposalOut
    decision: str


class WriteAssertionIn(BaseModel):
    """Request body for POST /assertions (direct write, bypassing the
    proposal/policy pipeline).

    Exactly one of (``value`` and ``value_type``) or ``target`` must be
    set. Requires write or admin capability and a non-AI principal —
    enforced by ``Ontology.assert_literal``/``assert_ref``, not this
    schema. ``supersedes`` (ADR-0050/KI-080, KI-099) is the same opt-in
    ``ProposeIn`` has — see its own docstring.
    """

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
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes: str | None = None


class AssertionDetailOut(BaseModel):
    """Response body for POST /assertions."""

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
    supersedes: str | None


class RejectIn(BaseModel):
    """Request body for POST /proposals/{proposal_id}/reject."""

    reason: str = ""


class ReviewIn(BaseModel):
    """Request body for POST /proposals/{proposal_id}/review."""

    reason: str = ""


class AssignIn(BaseModel):
    """Request body for POST /proposals/{proposal_id}/assign (SPEC §9.4,
    KI-078). Replaces the proposal's reviewer list wholesale — pass the
    full desired set, including any names from the current assignment
    that should be kept."""

    reviewers: list[str]


class ContradictionOut(BaseModel):
    """A contradiction's fields, returned by every contradiction route."""

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
    # Open JSON blob (KI-071/KI-075) — in practice, {} or
    # {"rationale_history": [{"rationale", "actor", "at"}, ...]}.
    metadata: dict[str, Any]


class ResolveContradictionIn(BaseModel):
    """Request body for POST /contradictions/{contradiction_id}/resolve."""

    winner_assertion_id: str


class FlagContradictionIn(BaseModel):
    """Request body for POST /contradictions/flag."""

    assertion_id_a: str
    assertion_id_b: str
    rationale: str | None = None


class FlagContradictionOut(BaseModel):
    """Response body for POST /contradictions/flag."""

    contradiction: ContradictionOut
    action: str


class CreatePrincipalIn(BaseModel):
    """Request body for POST /principals. Requires admin capability.

    ``kind``/``auth_method``/``default_capability``/``trust_level`` mirror
    ``Principal``'s own ``Literal``/bounded types exactly (not plain
    ``str``/``int``) so an invalid value is rejected by Pydantic — mapped
    to the SPEC §16 envelope by the existing ``RequestValidationError``
    handler — instead of reaching ``Ontology.create_principal`` and
    raising a raw, unmapped pydantic error from constructing ``Principal``
    internally (the exact failure mode the ai/no-owner fix in this same
    slice closed for one field; every field needs the same protection,
    not just ``owner``).
    """

    principal_id: str
    kind: Literal["human", "ai", "service"]
    auth_method: Literal["oidc", "workload", "apikey"] = "oidc"
    owner: str | None = None
    default_capability: Literal["read", "propose", "write", "review", "admin"] = "propose"
    trust_level: int = Field(default=0, ge=0, le=10)
    metadata: dict[str, Any] | None = None


class PrincipalOut(BaseModel):
    """A principal's fields, returned by principal routes."""

    id: str
    kind: str
    owner: str | None
    auth_method: str
    default_capability: str
    trust_level: int
    created_at: str


class TokenIssuedOut(BaseModel):
    """Response body for POST /principals/{principal_id}/tokens.

    The raw token is returned exactly once, here, and cannot be recovered
    afterward — only its SHA-256 hash is persisted.
    """

    token: str
    credential_id: str


class CredentialOut(BaseModel):
    """A single issued credential's metadata. Never the raw token or hash."""

    id: str
    principal_id: str
    created_at: str
    revoked_at: str | None
    issued_by: str | None
    revoked_by: str | None


class AdminEventOut(BaseModel):
    """A single recorded admin-action event (KI-060/KI-072, ADR-0042)."""

    id: str
    actor: str
    action: str
    target: str
    at: str
    detail: str | None


class NamespaceOut(BaseModel):
    """A registered namespace's fields, returned by GET /namespaces."""

    id: str
    created_at: str
    metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_rest_app(
    kb: Ontology,
    auth_provider: AuthProvider,
    name: str = "ontolith",
    *,
    docs_url: str | None = "/docs",
    redoc_url: str | None = "/redoc",
    openapi_url: str | None = "/openapi.json",
) -> FastAPI:
    """Build and return a FastAPI app bound to the given knowledge base.

    Args:
        kb: Ontology instance (knowledge base) to expose
        auth_provider: Resolves caller-supplied bearer tokens to Principals
            (ADR-0014) — e.g. ``TokenAuthProvider(kb.backend)``
        name: API title advertised in the OpenAPI schema
        docs_url: Swagger UI path, or None to disable it. Unauthenticated
            like the rest of FastAPI's docs surface (module docstring).
        redoc_url: ReDoc path, or None to disable it.
        openapi_url: OpenAPI schema JSON path, or None to disable it (also
            disables docs_url/redoc_url, which depend on it).

    Returns:
        Configured FastAPI app with the read + propose route slice
        (ADR-0021)
    """
    app = FastAPI(title=name, docs_url=docs_url, redoc_url=redoc_url, openapi_url=openapi_url)

    def _resolve_principal(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Principal:
        """Resolve the Authorization header to a Principal (ADR-0014).

        Raises:
            AuthError: header missing/malformed, or token invalid/revoked
        """
        if authorization is None or not authorization.startswith("Bearer "):
            raise AuthError("Missing or malformed Authorization header")
        token = authorization.removeprefix("Bearer ")
        return auth_provider.resolve(token)

    @app.exception_handler(OntolithError)
    def _handle_ontolith_error(_request: Request, exc: OntolithError) -> JSONResponse:
        """Map every OntolithError subtype to its HTTP status (SPEC §16).

        5xx errors (StorageError/PluginError) log the real message
        server-side but never return it — those messages interpolate raw
        internal exception text (e.g. sqlite3 constraint/transaction-state
        details) that a caller has no use for and shouldn't see.
        """
        status = _STATUS_BY_ERROR_TYPE.get(type(exc), 500)
        if status >= 500:
            # SPEC §18/ADR-0044: structured, correlated log via kb's sink —
            # replaces the ad hoc module logger this call site used to use.
            kb.observability.log(logging.ERROR, exc.message, code=exc.code)
            message = _GENERIC_SERVER_ERROR_MESSAGE
        else:
            message = exc.message
        body = ErrorOut(code=exc.code, message=message, detail=exc.detail)
        return JSONResponse(status_code=status, content=body.model_dump())

    @app.exception_handler(RequestValidationError)
    def _handle_request_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Map FastAPI's own request-body/query validation failures onto the
        same envelope as ValidationError (SPEC §16) — malformed input is a
        validation failure regardless of whether Pydantic or domain code
        caught it first."""
        body = ErrorOut(
            code=ValidationError.code,
            message="Request validation failed",
            detail={"errors": exc.errors()},
        )
        return JSONResponse(status_code=400, content=body.model_dump())

    # ------------------------------------------------------------------
    # GET /schema
    # ------------------------------------------------------------------

    @app.get("/schema")
    def get_schema(
        namespace: str = "default",
        _principal: Principal = Depends(_resolve_principal),
    ) -> SchemaOut:
        """Return the schema (concepts, properties, and relations) for a namespace."""
        ir = kb.backend.get_schema(namespace)
        if ir is None:
            return SchemaOut(concepts=[])
        concepts = [
            ConceptOut(
                name=concept_name,
                properties=[
                    PropertyOut(
                        name=prop_name,
                        type=prop_def.value_type,
                        cardinality=prop_def.cardinality,
                        temporality=prop_def.temporality,
                        required=prop_def.required,
                    )
                    for prop_name, prop_def in concept_def.properties.items()
                ],
                relations=[
                    RelationOut(
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
        return SchemaOut(namespace=namespace, version=ir.version, concepts=concepts)

    # ------------------------------------------------------------------
    # POST /entities
    # ------------------------------------------------------------------

    @app.post("/entities", status_code=201)
    def create_entity_route(
        body: CreateEntityIn,
        principal: Principal = Depends(_resolve_principal),
    ) -> EntityOut:
        """Create a new entity (KI-082). Propose-tier — same capability
        gate as every other governed write (`Ontology.create_entity`
        rejects `read`-only principals)."""
        entity = kb.create_entity(
            concept=body.concept,
            author=principal.id,
            natural_key=body.natural_key,
        )
        return EntityOut(
            id=entity.id,
            concept=entity.concept,
            namespace=entity.namespace,
            natural_key=entity.natural_key,
            created_at=entity.created_at.isoformat(),
            created_by=entity.created_by,
        )

    # ------------------------------------------------------------------
    # GET /entities/{entity_id}
    # ------------------------------------------------------------------

    @app.get("/entities/{entity_id}")
    def get_entity_route(
        entity_id: str,
        _principal: Principal = Depends(_resolve_principal),
    ) -> EntityDetailOut:
        """Fetch an entity and its currently active assertions."""
        entity = kb.backend.get_entity(entity_id)
        if entity is None:
            raise NotFoundError(f"Entity {entity_id!r} not found")
        assertions = kb.backend.assertions(subject=entity_id, status="active")
        return EntityDetailOut(
            entity=EntityOut(
                id=entity.id,
                concept=entity.concept,
                namespace=entity.namespace,
                natural_key=entity.natural_key,
                created_at=entity.created_at.isoformat(),
                created_by=entity.created_by,
            ),
            assertions=[
                AssertionOut(
                    id=a.id,
                    predicate=a.predicate,
                    value=a.value,
                    value_type=a.value_type,
                    confidence=a.confidence,
                    author=a.author,
                    asserted_at=a.asserted_at.isoformat(),
                )
                for a in assertions
            ],
        )

    # ------------------------------------------------------------------
    # POST /query
    # ------------------------------------------------------------------

    @app.post("/query")
    def query_route(
        body: QueryIn,
        _principal: Principal = Depends(_resolve_principal),
    ) -> QueryOut:
        """Query entities of a concept, optionally filtered/ranked."""
        builder = kb.query(body.concept)
        if body.filters:
            builder = builder.where(**body.filters)
        if body.semantic is not None:
            builder = builder.semantic(body.semantic)
        if body.min_confidence is not None:
            builder = builder.min_confidence(body.min_confidence)
        if body.trust_at_least is not None:
            builder = builder.trust_at_least(body.trust_at_least)
        if body.include_flagged:
            builder = builder.include_flagged()
        if body.include_history:
            builder = builder.include_history()
        if body.limit is not None:
            builder = builder.limit(body.limit)
        entities = builder.all()
        return QueryOut(
            concept=body.concept,
            count=len(entities),
            entities=[
                EntitySummaryOut(
                    id=e.id,
                    concept=e.concept,
                    natural_key=e.natural_key,
                    created_at=e.created_at.isoformat(),
                )
                for e in entities
            ],
        )

    # ------------------------------------------------------------------
    # GET /provenance/{assertion_id}
    # ------------------------------------------------------------------

    @app.get("/provenance/{assertion_id}")
    def provenance_route(
        assertion_id: str,
        _principal: Principal = Depends(_resolve_principal),
    ) -> ProvenanceOut:
        """Return the full provenance record for a single assertion.

        ``superseded_ids`` carries the full predecessor set this assertion
        superseded — ``supersedes`` alone only records the first predecessor
        when one incoming assertion supersedes several concurrently-
        overlapping ones (KI-008).
        """
        prov = kb.provenance(assertion_id)  # raises NotFoundError -> 404 via the error handler
        a = prov.assertion
        return ProvenanceOut(
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
                ReviewEventOut(actor=e.actor, type=e.type, detail=e.detail, at=e.at.isoformat())
                for e in prov.review_events
            ],
        )

    # ------------------------------------------------------------------
    # POST /proposals
    # ------------------------------------------------------------------

    @app.post("/proposals", status_code=201)
    def create_proposal_route(
        body: ProposeIn,
        principal: Principal = Depends(_resolve_principal),
    ) -> ProposeOut:
        """Create a proposal to assert a fact or relation. Does NOT write directly.

        Conflict-routing temporality is resolved server-side from the
        active schema (SPEC §10.1). The acting principal is resolved from
        the bearer token (ADR-0014), never taken from the request body.
        """
        has_literal = body.value is not None and body.value_type is not None
        has_ref = body.target is not None
        if has_literal == has_ref:
            raise ValidationError("Provide exactly one of (value and value_type) or target")

        if has_ref:
            assert body.target is not None
            proposal, decision = kb.propose_ref(
                subject=body.subject,
                predicate=body.predicate,
                target=body.target,
                author=principal.id,
                confidence=body.confidence,
                source=body.source,
                rationale=body.rationale,
                acting_as=body.acting_as,
                model=body.model,
                supersedes=body.supersedes,
            )
        else:
            assert body.value is not None and body.value_type is not None
            proposal, decision = kb.propose(
                subject=body.subject,
                predicate=body.predicate,
                value=body.value,
                value_type=body.value_type,
                author=principal.id,
                confidence=body.confidence,
                source=body.source,
                rationale=body.rationale,
                acting_as=body.acting_as,
                model=body.model,
                supersedes=body.supersedes,
            )

        return ProposeOut(
            proposal=ProposalOut(
                id=proposal.id,
                namespace=proposal.namespace,
                author=proposal.author,
                acting_as=proposal.acting_as,
                state=proposal.state,
                created_at=proposal.created_at.isoformat(),
                decided_at=proposal.decided_at.isoformat() if proposal.decided_at else None,
                policy_reason=proposal.policy_reason,
                reviewers=proposal.reviewers,
            ),
            decision=type(decision).__name__,
        )

    # ------------------------------------------------------------------
    # GET /proposals
    # ------------------------------------------------------------------

    @app.get("/proposals")
    def list_proposals_route(
        state: str | None = "require_review",
        _principal: Principal = Depends(_resolve_principal),
    ) -> list[ProposalOut]:
        """List proposals, defaulting to those pending review.

        Pass ``state=pending`` to merge ``require_review`` and
        ``changes_requested`` — see ``Ontology.proposals`` (KI-027) for why
        that isn't the default: a caller iterating the default result and
        calling accept/reject on each entry would break the moment a
        ``changes_requested`` proposal showed up in it.

        Pass ``state=all`` to list proposals in every state — a plain
        empty query string value can't express "no filter" unambiguously,
        so ``"all"`` is used as an explicit sentinel instead (mirrors the
        CLI's ``proposal list --all`` flag, expressed as a query value
        here since REST has no separate boolean-flag convention).

        Raises:
            ValidationError: ``state`` is none of the values above — an
                unrecognized value (a typo, wrong case, or a plausible-
                sounding non-existent state) previously reached
                ``kb.proposals()``'s own ``WHERE state = ?`` unfiltered and
                silently matched zero rows, indistinguishable from "no
                proposals in that state" (KI-077).
        """
        # Structurally after auth, not just textually: `_principal` is a
        # FastAPI `Depends`, resolved before this body ever runs - an
        # unauthenticated caller gets 401 regardless of `state`, never a
        # free pre-auth probe of the accepted-value set (matches MCP's own
        # ontolith.list_contradictions, KI-076 review).
        if state not in (None, *_PROPOSAL_STATES, "pending", "all"):
            raise ValidationError(f"Invalid state: {state!r}")
        effective_state = None if state == "all" else state
        results = kb.proposals(state=effective_state)
        return [
            ProposalOut(
                id=p.id,
                namespace=p.namespace,
                author=p.author,
                acting_as=p.acting_as,
                state=p.state,
                created_at=p.created_at.isoformat(),
                decided_at=p.decided_at.isoformat() if p.decided_at else None,
                policy_reason=p.policy_reason,
                reviewers=p.reviewers,
            )
            for p in results
        ]

    # ------------------------------------------------------------------
    # POST /assertions
    # ------------------------------------------------------------------

    @app.post("/assertions", status_code=201)
    def write_assertion_route(
        body: WriteAssertionIn,
        principal: Principal = Depends(_resolve_principal),
    ) -> AssertionDetailOut:
        """Directly write an assertion, bypassing the proposal/policy pipeline.

        Requires write or admin capability and a non-AI principal
        (Ontology.assert_literal/assert_ref enforce both) — SPEC §10
        conflict routing still applies; this is a governed direct-write
        path, not a way around review for AI principals.
        """
        has_literal = body.value is not None and body.value_type is not None
        has_ref = body.target is not None
        if has_literal == has_ref:
            raise ValidationError("Provide exactly one of (value and value_type) or target")

        if has_ref:
            assert body.target is not None
            assertion = kb.assert_ref(
                subject=body.subject,
                predicate=body.predicate,
                target=body.target,
                author=principal.id,
                confidence=body.confidence,
                source=body.source,
                rationale=body.rationale,
                acting_as=body.acting_as,
                model=body.model,
                valid_from=body.valid_from,
                valid_to=body.valid_to,
                supersedes=body.supersedes,
            )
        else:
            assert body.value is not None and body.value_type is not None
            assertion = kb.assert_literal(
                subject=body.subject,
                predicate=body.predicate,
                value=body.value,
                value_type=body.value_type,
                author=principal.id,
                confidence=body.confidence,
                source=body.source,
                rationale=body.rationale,
                acting_as=body.acting_as,
                model=body.model,
                valid_from=body.valid_from,
                valid_to=body.valid_to,
                supersedes=body.supersedes,
            )

        return AssertionDetailOut(
            id=assertion.id,
            subject=assertion.subject,
            predicate=assertion.predicate,
            value=assertion.value,
            value_type=assertion.value_type,
            status=assertion.status,
            author=assertion.author,
            confidence=assertion.confidence,
            source=assertion.source,
            rationale=assertion.rationale,
            model=assertion.model,
            asserted_at=assertion.asserted_at.isoformat(),
            valid_from=assertion.valid_from.isoformat() if assertion.valid_from else None,
            valid_to=assertion.valid_to.isoformat() if assertion.valid_to else None,
            supersedes=assertion.supersedes,
        )

    # ------------------------------------------------------------------
    # POST /assertions/{assertion_id}/retract
    # ------------------------------------------------------------------

    @app.post("/assertions/{assertion_id}/retract", status_code=201)
    def retract_assertion_route(
        assertion_id: str,
        acting_as: str | None = None,
        principal: Principal = Depends(_resolve_principal),
    ) -> ProposeOut:
        """Propose retraction of an assertion through the governed
        proposal/policy pipeline (SPEC §9). Does NOT delete or write
        directly — same propose/policy/conflict-routing pipeline as POST
        /proposals. When acting_as is set the retraction is made on
        behalf of another principal (delegation, ADR-0003)."""
        proposal, decision = kb.retract(assertion_id, author=principal.id, acting_as=acting_as)
        return ProposeOut(
            proposal=ProposalOut(
                id=proposal.id,
                namespace=proposal.namespace,
                author=proposal.author,
                acting_as=proposal.acting_as,
                state=proposal.state,
                created_at=proposal.created_at.isoformat(),
                decided_at=proposal.decided_at.isoformat() if proposal.decided_at else None,
                policy_reason=proposal.policy_reason,
                reviewers=proposal.reviewers,
            ),
            decision=type(decision).__name__,
        )

    # ------------------------------------------------------------------
    # POST /proposals/{proposal_id}/accept
    # ------------------------------------------------------------------

    @app.post("/proposals/{proposal_id}/accept")
    def accept_proposal_route(
        proposal_id: str,
        principal: Principal = Depends(_resolve_principal),
    ) -> ProposalOut:
        """Accept a pending proposal, replaying its operations. Requires
        review or admin capability; the reviewer must not be the
        proposal's own author or delegate (self-review is blocked)."""
        proposal = kb.accept_proposal(proposal_id, principal.id)
        return ProposalOut(
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

    # ------------------------------------------------------------------
    # POST /proposals/{proposal_id}/reject
    # ------------------------------------------------------------------

    @app.post("/proposals/{proposal_id}/reject")
    def reject_proposal_route(
        proposal_id: str,
        body: RejectIn,
        principal: Principal = Depends(_resolve_principal),
    ) -> ProposalOut:
        """Reject a pending proposal. Requires review or admin capability;
        same self-review guard as accept."""
        proposal = kb.reject_proposal(proposal_id, principal.id, reason=body.reason)
        return ProposalOut(
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

    # ------------------------------------------------------------------
    # POST /proposals/{proposal_id}/review
    # ------------------------------------------------------------------

    @app.post("/proposals/{proposal_id}/review")
    def review_proposal_route(
        proposal_id: str,
        body: ReviewIn,
        principal: Principal = Depends(_resolve_principal),
    ) -> ProposalOut:
        """Request changes on a pending proposal (SPEC §9.1/§9.4's third
        under_review outcome, alongside accept/reject). Requires review or
        admin capability; same self-review guard as accept/reject."""
        proposal = kb.request_changes(proposal_id, principal.id, reason=body.reason)
        return ProposalOut(
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

    # ------------------------------------------------------------------
    # POST /proposals/{proposal_id}/assign
    # ------------------------------------------------------------------

    @app.post("/proposals/{proposal_id}/assign")
    def assign_reviewers_route(
        proposal_id: str,
        body: AssignIn,
        principal: Principal = Depends(_resolve_principal),
    ) -> ProposalOut:
        """Reassign a pending proposal's reviewers (SPEC §9.4's `assign`
        action, KI-078). Requires review or admin capability; same
        self-review guard as accept/reject/review — replaces the reviewer
        list wholesale, including clearing it via an empty list."""
        proposal = kb.assign_reviewers(proposal_id, body.reviewers, principal.id)
        return ProposalOut(
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

    # ------------------------------------------------------------------
    # POST /proposals/{proposal_id}/resubmit
    # ------------------------------------------------------------------

    @app.post("/proposals/{proposal_id}/resubmit")
    def resubmit_proposal_route(
        proposal_id: str,
        principal: Principal = Depends(_resolve_principal),
    ) -> ProposeOut:
        """Resubmit a proposal after changes were requested (KI-027),
        re-running policy evaluation against the unedited payload. Only the
        proposal's own author or delegate may call this."""
        proposal, decision = kb.resubmit(proposal_id, principal.id)
        return ProposeOut(
            proposal=ProposalOut(
                id=proposal.id,
                namespace=proposal.namespace,
                author=proposal.author,
                acting_as=proposal.acting_as,
                state=proposal.state,
                created_at=proposal.created_at.isoformat(),
                decided_at=proposal.decided_at.isoformat() if proposal.decided_at else None,
                policy_reason=proposal.policy_reason,
                reviewers=proposal.reviewers,
            ),
            decision=type(decision).__name__,
        )

    # ------------------------------------------------------------------
    # GET /contradictions
    # ------------------------------------------------------------------

    @app.get("/contradictions")
    def list_contradictions_route(
        state: str | None = "open",
        _principal: Principal = Depends(_resolve_principal),
    ) -> list[ContradictionOut]:
        """List contradictions, defaulting to open ones.

        Pass ``state=all`` to list contradictions in every state (mirrors
        GET /proposals's ``all`` sentinel — see that route for why).

        Raises:
            ValidationError: ``state`` is none of "open"/"resolved"/"all" —
                an unrecognized value previously reached
                ``kb.contradictions()``'s own ``WHERE state = ?`` unfiltered
                and silently matched zero rows, indistinguishable from "no
                contradictions in that state" (KI-077; same class of bug
                KI-076 fixed for MCP's ``ontolith.list_contradictions``).
        """
        # Structurally after auth, not just textually — see
        # list_proposals_route's identical comment above.
        if state not in (None, *_CONTRADICTION_STATES, "all"):
            raise ValidationError(f"Invalid state: {state!r}")
        effective_state = None if state == "all" else state
        results = kb.contradictions(state=effective_state)
        return [
            ContradictionOut(
                id=c.id,
                namespace=c.namespace,
                subject=c.subject,
                predicate=c.predicate,
                state=c.state,
                member_ids=c.member_ids,
                created_at=c.created_at.isoformat(),
                raised_by=c.raised_by,
                resolved_by=c.resolved_by,
                resolved_at=c.resolved_at.isoformat() if c.resolved_at else None,
                metadata=c.metadata,
            )
            for c in results
        ]

    # ------------------------------------------------------------------
    # POST /contradictions/flag
    # ------------------------------------------------------------------

    @app.post("/contradictions/flag", status_code=201)
    def flag_contradiction_route(
        body: FlagContradictionIn,
        principal: Principal = Depends(_resolve_principal),
    ) -> FlagContradictionOut:
        """Flag two assertions as contradictory, opening or extending a
        contradiction. Requires propose capability or higher (ADR-0008)."""
        contradiction, action = kb.flag_contradiction(
            body.assertion_id_a,
            body.assertion_id_b,
            principal.id,
            rationale=body.rationale,
        )
        return FlagContradictionOut(
            contradiction=ContradictionOut(
                id=contradiction.id,
                namespace=contradiction.namespace,
                subject=contradiction.subject,
                predicate=contradiction.predicate,
                state=contradiction.state,
                member_ids=contradiction.member_ids,
                created_at=contradiction.created_at.isoformat(),
                raised_by=contradiction.raised_by,
                resolved_by=contradiction.resolved_by,
                resolved_at=(
                    contradiction.resolved_at.isoformat() if contradiction.resolved_at else None
                ),
                metadata=contradiction.metadata,
            ),
            action=action,
        )

    # ------------------------------------------------------------------
    # POST /contradictions/{contradiction_id}/resolve
    # ------------------------------------------------------------------

    @app.post("/contradictions/{contradiction_id}/resolve")
    def resolve_contradiction_route(
        contradiction_id: str,
        body: ResolveContradictionIn,
        principal: Principal = Depends(_resolve_principal),
    ) -> ContradictionOut:
        """Resolve an open contradiction by selecting a winning assertion.
        Requires review or admin capability."""
        contradiction = kb.resolve_contradiction(
            contradiction_id, body.winner_assertion_id, principal.id
        )
        return ContradictionOut(
            id=contradiction.id,
            namespace=contradiction.namespace,
            subject=contradiction.subject,
            predicate=contradiction.predicate,
            state=contradiction.state,
            member_ids=contradiction.member_ids,
            created_at=contradiction.created_at.isoformat(),
            raised_by=contradiction.raised_by,
            resolved_by=contradiction.resolved_by,
            resolved_at=(
                contradiction.resolved_at.isoformat() if contradiction.resolved_at else None
            ),
            metadata=contradiction.metadata,
        )

    # ------------------------------------------------------------------
    # POST /principals
    # ------------------------------------------------------------------

    @app.post("/principals", status_code=201)
    def create_principal_route(
        body: CreatePrincipalIn,
        principal: Principal = Depends(_resolve_principal),
    ) -> PrincipalOut:
        """Create a new principal. Requires admin capability.

        Unlike every other write route here, Ontology.create_principal()
        has no built-in capability gate of its own — this route calls
        Ontology.require_admin() explicitly first, the same gate
        issue_token/revoke_token/list_tokens already use internally.
        """
        kb.require_admin(principal.id)
        created = kb.create_principal(
            body.principal_id,
            kind=body.kind,
            auth_method=body.auth_method,
            owner=body.owner,
            default_capability=body.default_capability,
            trust_level=body.trust_level,
            metadata=body.metadata,
            author=principal.id,
        )
        return PrincipalOut(
            id=created.id,
            kind=created.kind,
            owner=created.owner,
            auth_method=created.auth_method,
            default_capability=created.default_capability,
            trust_level=created.trust_level,
            created_at=created.created_at.isoformat(),
        )

    # ------------------------------------------------------------------
    # GET /principals
    # ------------------------------------------------------------------

    @app.get("/principals")
    def list_principals_route(
        principal: Principal = Depends(_resolve_principal),
    ) -> list[PrincipalOut]:
        """List all principals. Requires admin capability (KI-022)."""
        principals = kb.list_principals(author=principal.id)
        return [
            PrincipalOut(
                id=p.id,
                kind=p.kind,
                owner=p.owner,
                auth_method=p.auth_method,
                default_capability=p.default_capability,
                trust_level=p.trust_level,
                created_at=p.created_at.isoformat(),
            )
            for p in principals
        ]

    # ------------------------------------------------------------------
    # POST /principals/{principal_id}/tokens
    # ------------------------------------------------------------------

    @app.post("/principals/{principal_id}/tokens", status_code=201)
    def issue_token_route(
        principal_id: str,
        principal: Principal = Depends(_resolve_principal),
    ) -> TokenIssuedOut:
        """Issue a new bearer token for a principal. Requires admin capability.

        The raw token is returned exactly once, here, and cannot be
        recovered afterward — store it immediately.
        """
        token, credential_id = kb.issue_token(principal_id, author=principal.id)
        return TokenIssuedOut(token=token, credential_id=credential_id)

    # ------------------------------------------------------------------
    # GET /principals/{principal_id}/tokens
    # ------------------------------------------------------------------

    @app.get("/principals/{principal_id}/tokens")
    def list_tokens_route(
        principal_id: str,
        principal: Principal = Depends(_resolve_principal),
    ) -> list[CredentialOut]:
        """List credentials issued to a principal. Requires admin
        capability. Never returns the raw token or its hash."""
        credentials = kb.list_tokens(principal_id, author=principal.id)
        return [
            CredentialOut(
                id=c.id,
                principal_id=c.principal_id,
                created_at=c.created_at.isoformat(),
                revoked_at=c.revoked_at.isoformat() if c.revoked_at else None,
                issued_by=c.issued_by,
                revoked_by=c.revoked_by,
            )
            for c in credentials
        ]

    # ------------------------------------------------------------------
    # DELETE /principals/{principal_id}/tokens/{credential_id}
    # ------------------------------------------------------------------

    @app.delete("/principals/{principal_id}/tokens/{credential_id}", status_code=204)
    def revoke_token_route(
        principal_id: str,
        credential_id: str,
        principal: Principal = Depends(_resolve_principal),
    ) -> None:
        """Revoke a previously issued token by its credential ID. Requires
        admin capability.

        Unlike Ontology.revoke_token() itself (which identifies the
        credential solely by credential_id), this route verifies the
        credential actually belongs to ``principal_id`` before revoking —
        otherwise a caller could revoke a different principal's token
        while believing, from the URL alone, that they'd scoped the
        action to ``principal_id``.
        """
        kb.require_admin(principal.id)
        credential = kb.backend.get_credential(credential_id)
        if credential is None or credential.principal_id != principal_id:
            raise NotFoundError(f"Credential {credential_id!r} not found for {principal_id!r}")
        kb.revoke_token(credential_id, author=principal.id)

    # ------------------------------------------------------------------
    # GET /admin-events
    # ------------------------------------------------------------------

    @app.get("/admin-events")
    def list_admin_events_route(
        actor: str | None = None,
        target: str | None = None,
        principal: Principal = Depends(_resolve_principal),
    ) -> list[AdminEventOut]:
        """List recorded admin-action events (create_principal/apply_schema/
        register_plugin), optionally filtered by actor or target (KI-072,
        ADR-0042). Requires admin capability — closes the read half of
        KI-060's audit trail: `issued_by`/`revoked_by` on GET
        /principals/{id}/tokens covers token issuance/revocation, this
        route covers the other three admin actions.
        """
        events = kb.get_admin_events(author=principal.id, actor=actor, target=target)
        return [
            AdminEventOut(
                id=e.id,
                actor=e.actor,
                action=e.action,
                target=e.target,
                at=e.at.isoformat(),
                detail=e.detail,
            )
            for e in events
        ]

    # ------------------------------------------------------------------
    # GET /namespaces
    # ------------------------------------------------------------------

    @app.get("/namespaces")
    def list_namespaces_route(
        _principal: Principal = Depends(_resolve_principal),
    ) -> list[NamespaceOut]:
        """List all registered namespaces (SPEC §12.2, KI-022).

        Ungated beyond authentication, same as GET /proposals — namespace
        metadata carries nothing as sensitive as GET /principals's
        owner/trust_level fields.
        """
        return [
            NamespaceOut(id=n.id, created_at=n.created_at.isoformat(), metadata=n.metadata)
            for n in kb.list_namespaces()
        ]

    return app


__all__ = ["create_rest_app"]
