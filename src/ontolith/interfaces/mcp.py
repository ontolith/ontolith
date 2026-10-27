"""MCP server for Ontolith (ADR-0008, ADR-0014).

Exposes read/propose/flag tools to AI agents. No direct write tool is exposed;
every *assertion* mutation flows through the proposal/policy pipeline.
``ontolith.create_entity`` (KI-082) creates an entity directly only for a
non-AI principal (an entity is an identity anchor, not a fact). For an
``ai``-kind principal it stages a one-operation batch instead and returns
the proposal, so a rejected agent proposal leaves no entity behind
(ADR-0055). ``ontolith.propose_batch`` stages several entities and facts as
one proposal.

Tools (ADR-0008, plus resubmit added for KI-027, retract added for KI-057/ADR-0039,
list_contradictions added for KI-076, create_entity added for KI-082,
propose_batch added for ADR-0055):
  ontolith.schema           — read concept/relation definitions
  ontolith.get              — fetch entity + current assertions
  ontolith.create_entity    — create a new entity (AI callers get a proposal, ADR-0055)
  ontolith.propose_batch    — propose several entities/facts as one proposal (NOT write)
  ontolith.query            — symbolic entity retrieval
  ontolith.provenance       — full provenance trail for an assertion
  ontolith.list_contradictions — read-only contradiction listing
  ontolith.propose          — create a proposal (NOT write)
  ontolith.flag_contradiction — open/extend a contradiction for review
  ontolith.resubmit         — resubmit a changes_requested proposal (NOT write)
  ontolith.retract          — propose retraction of an assertion (NOT write)

Authentication (ADR-0014): every tool, reads included, resolves a bearer
token to a Principal via the injected AuthProvider — the acting principal is
always server-derived from a verified credential, never client-asserted. One
server (one AuthProvider/backend) can serve many principals, each with their
own issued token (``kb.issue_token(principal_id, author=admin_id)`` via
SDK/CLI — requires the issuing author to hold `admin` capability). Read
tools require no further capability check beyond a resolved principal:
capability is a total order (SPEC §8.3, ``read < propose < write < review <
admin``), so any successfully authenticated principal already clears the
"read" floor.

Token transport (KI-067): every tool still accepts an optional ``token``
argument, but under the SSE/streamable-HTTP transports the server prefers
an ``Authorization: Bearer <token>`` HTTP header when the transport supplies
one, falling back to the argument only when no header is present at all.
Prefer the header: the argument must be emitted by the calling model as
part of the tool call itself, landing it in the model's own context window
and any MCP client's tool-call logging; the header travels on the transport
connection instead, out of the model's context entirely. A header that IS
present but malformed (wrong scheme, or a blank value) fails the call
closed rather than silently falling back to the argument — see
``_bearer_token``'s docstring for why. stdio has no HTTP request at all, so
``token`` remains the only channel there — ADR-0014 recommends short-lived
tokens for stdio-facing principals for that reason.

That fallback is opt-in to disable (KI-073): ``create_mcp_server(...,
require_header_token=True)`` makes an absent header fail the call the same
as no credential at all, even when a caller still supplies ``token`` —
letting an HTTP deployment require the more private channel outright
instead of merely preferring it. Off by default, since it makes stdio
transports (which have no header channel to require) unusable outright;
a deployment reaches for it once the header path is available, not
because the argument fallback is itself a live vulnerability.

Error handling (KI-059, KI-074, SPEC §16): every tool wraps its body in one
``except OntolithError as exc: return _error_response(exc)``, the MCP
equivalent of REST's single ``@app.exception_handler(OntolithError)`` and
GraphQL's single ``process_errors`` override — not a per-tool, per-exception-
type catch, which previously left some taxonomy members unreachable and let
the ``code`` values drift from REST/GraphQL's own. ``_error_response()``
returns ``{"error", "code", "detail"}`` for every taxonomy member, redacting
``StorageError``/``PluginError`` messages the same way those two interfaces
already do (they interpolate raw internal exception text).

Usage:
    from ontolith.identity.token_auth import TokenAuthProvider
    from ontolith.interfaces.mcp import create_mcp_server
    mcp = create_mcp_server(kb, TokenAuthProvider(kb.backend))
    mcp.run()          # stdio (default for MCP)
    mcp.run("sse")     # SSE transport
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, get_args

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError as PydanticValidationError

from ontolith.core.errors import OntolithError, PluginError, StorageError, ValidationError
from ontolith.govern.contradiction import ContradictionState
from ontolith.govern.operations import (
    MAX_BATCH_OPERATIONS,
    AssertLiteralOp,
    AssertRefOp,
    BatchOp,
    CreateEntityOp,
    LocalRef,
)

if TYPE_CHECKING:
    from ontolith.govern.policy import Decision
    from ontolith.govern.proposal import Proposal
    from ontolith.identity.ports import AuthProvider
    from ontolith.ontology import Ontology

# Derived from ContradictionState's own named Literal alias
# (govern/contradiction.py), not hand-duplicated, so this can't silently
# drift if that type ever gains/loses a state (KI-077 review — this tuple
# used to be hand-written here, independently of the identical constant
# rest.py/graphql.py derive the same way).
_CONTRADICTION_STATES: tuple[str, ...] = get_args(ContradictionState)

# StorageError/PluginError messages interpolate raw internal exception text
# (e.g. sqlite3 constraint/transaction-state details) - redacted in the
# response the same way REST's _handle_ontolith_error/GraphQL's
# process_errors override already do; the real message is logged
# server-side instead (SPEC §16 still gets a stable `code`, just not the
# raw text). isinstance, not exact type (unlike REST's _STATUS_BY_ERROR_TYPE
# dict, which is keyed by exact type because it also has to pick an HTTP
# status - MCP has no status channel to key off): this automatically
# redacts any future subclass of StorageError/PluginError specifically,
# without needing this tuple updated - but a brand-new OntolithError
# direct subclass that ISN'T one of those two still fails open
# (unredacted) here, same as it would anywhere isinstance is used for
# this kind of check. Matches GraphQL's own _REDACT_MESSAGE_FOR precedent
# exactly (including that same fails-open case for a genuinely new type)
# - a real divergence from REST, whose .get(type(exc), 500) defaults an
# unrecognized exact type to 500 and therefore redacts it: REST fails
# closed for exactly the case this isinstance check fails open on.
_REDACT_MESSAGE_FOR: tuple[type[OntolithError], ...] = (StorageError, PluginError)
_GENERIC_SERVER_ERROR_MESSAGE = "An internal error occurred"


_BATCH_OP_MODELS: dict[str, type[CreateEntityOp | AssertLiteralOp | AssertRefOp]] = {
    "create_entity": CreateEntityOp,
    "assert_literal": AssertLiteralOp,
    "assert_ref": AssertRefOp,
}


def _parse_batch_ops(raw: list[dict[str, Any]]) -> list[BatchOp]:
    """Turn `ontolith.propose_batch`'s JSON ops into typed operations.

    ``{"local": "key"}`` in ``subject``/``target`` becomes a `LocalRef`.
    Unknown ``op`` values, unknown fields and malformed fields are a
    `ValidationError` (the wire boundary; the SDK models validate the rest).
    """
    if len(raw) > MAX_BATCH_OPERATIONS:
        raise ValidationError(
            f"A batch holds at most {MAX_BATCH_OPERATIONS} operations; got {len(raw)}"
        )
    ops: list[BatchOp] = []
    for index, entry in enumerate(raw):
        fields = dict(entry)
        kind = fields.pop("op", None)
        model = _BATCH_OP_MODELS.get(kind) if isinstance(kind, str) else None
        if model is None:
            raise ValidationError(f"ops[{index}]: 'op' must be one of {sorted(_BATCH_OP_MODELS)}")
        try:
            for field in ("subject", "target"):
                value = fields.get(field)
                if isinstance(value, dict):
                    if set(value) != {"local"} or not isinstance(value["local"], str):
                        raise ValidationError(
                            f'ops[{index}].{field}: expected an entity id or {{"local": "<key>"}}'
                        )
                    fields[field] = LocalRef(value["local"])
            ops.append(model.model_validate(fields))
        except PydanticValidationError as exc:
            first = exc.errors()[0]
            raise ValidationError(f"ops[{index}]: {first['msg']} ({first['loc']})") from None
    return ops


def _batch_response(proposal: Proposal, decision: Decision) -> dict[str, Any]:
    """The `{"proposal", "decision"}` shape `ontolith.propose` returns,
    plus `created_entities` once a batch has been applied (ADR-0055)."""
    out: dict[str, Any] = {
        "id": proposal.id,
        "state": proposal.state,
        "policy_reason": proposal.policy_reason,
        "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
        "acting_as": proposal.acting_as,
    }
    created = proposal.metadata.get("created_entities")
    if created:
        out["created_entities"] = created
    return {"proposal": out, "decision": type(decision).__name__}


def create_mcp_server(
    kb: Ontology,
    auth_provider: AuthProvider,
    name: str = "ontolith",
    *,
    require_header_token: bool = False,
) -> FastMCP:
    """Build and return a FastMCP server bound to the given knowledge base.

    The returned server is not yet running — call ``mcp.run()`` to start it.

    Args:
        kb: Ontology instance (knowledge base) to expose
        auth_provider: Resolves caller-supplied bearer tokens to Principals
            (ADR-0014) — e.g. ``TokenAuthProvider(kb.backend)``
        name: Server name advertised to MCP clients
        require_header_token: When ``True`` (KI-073), disables the ``token``
            tool-argument fallback entirely — a call reaching a tool with no
            (or a malformed) ``Authorization`` header fails the same way a
            call with no credential at all would, even if ``token`` was
            supplied. ``False`` by default: the argument remains a valid
            fallback (KI-067), which is what keeps stdio transports (no
            header channel exists there) usable at all. Set this only for
            an HTTP (SSE/streamable-HTTP) deployment that wants to require
            the more private channel outright, not merely prefer it.

    Returns:
        Configured FastMCP server with all ADR-0008 tools registered
    """
    mcp: FastMCP = FastMCP(name)

    def _error_response(exc: OntolithError) -> dict[str, Any]:
        """Map any OntolithError to this module's ``{"error", "code",
        "detail"}`` shape (SPEC §16, KI-074) — the single place every
        tool's error dict is built, closing both the "5 of 10 taxonomy
        codes unreachable from MCP" gap and the "detail dropped entirely"
        shape divergence REST/GraphQL don't have. Every tool wraps its
        whole body (after token resolution, which isn't itself an
        ``OntolithError`` — see ``_bearer_token``) in one
        ``except OntolithError as exc: return _error_response(exc)``, the
        MCP equivalent of REST's single
        ``@app.exception_handler(OntolithError)`` and GraphQL's single
        ``process_errors`` override — not a per-call-site try/except per
        exception type, which is what let KI-059's drift and this gap both
        happen unnoticed for as long as they did.

        A closure over ``kb``, not a module-level function (SPEC §18/
        ADR-0044) — this is the one call site here that logs, and reaching
        ``kb.observability`` is the only reason it moved inside this
        factory; every one of its 24 call sites elsewhere in this
        function is unchanged, since ``_error_response`` still resolves
        the same way from their own enclosing scope.
        """
        if isinstance(exc, _REDACT_MESSAGE_FOR):
            kb.observability.log(logging.ERROR, exc.message, code=exc.code)
            message = _GENERIC_SERVER_ERROR_MESSAGE
        else:
            message = exc.message
        return {"error": message, "code": exc.code, "detail": exc.detail}

    def _bearer_token(token: str | None) -> tuple[str | None, str | None]:
        """Resolve the caller's bearer token for the current tool call,
        preferring the ``Authorization`` HTTP header over the ``token``
        argument (KI-067, ADR-0014 — see module docstring).

        Returns ``(resolved_token, error_message)`` — exactly one is
        non-``None``. A *present but malformed* header (wrong scheme, or a
        blank/whitespace-only value) fails closed with an error message and
        does NOT fall back to the ``token`` argument: silently accepting the
        argument whenever a proxy's header injection happens to misconfigure
        would reopen the exact exposure this fix removes, with no signal
        that it happened. An *absent* header (or no HTTP request context at
        all — see below) is not malformed, just missing, and does fall back
        to the argument as before — unless ``require_header_token`` is set
        (KI-073), in which case an absent header is treated the same as no
        credential supplied at all, regardless of ``token``.

        ``mcp.get_context().request_context.request`` is the raw Starlette
        request under the SSE/streamable-HTTP transports (the mcp SDK's own
        ``ServerMessageMetadata`` wiring); it's ``None`` under stdio (no HTTP
        request exists) and accessing ``.request_context`` itself raises
        ``ValueError`` when called outside any live request at all (e.g. a
        test invoking a tool's ``.fn`` directly) — both cases fall back to
        the ``token`` argument, same as a live request with no header (or,
        under ``require_header_token``, fail the same way a live request
        with no header would — stdio simply has no way to satisfy the
        requirement).
        """
        try:
            request = mcp.get_context().request_context.request
        except ValueError:
            request = None
        if request is not None:
            auth_header = request.headers.get("authorization")
            if auth_header is not None:
                scheme, _, value = auth_header.partition(" ")
                value = value.strip()
                if scheme.lower() != "bearer" or not value:
                    return None, "Malformed Authorization header"
                return value, None
        if require_header_token:
            return None, "No bearer token provided"
        if token is None:
            return None, "No bearer token provided"
        return token, None

    # ------------------------------------------------------------------
    # ontolith.schema — list concepts and their properties/relations
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.schema")
    def schema_tool(token: str | None = None, namespace: str = "default") -> dict[str, Any]:
        """Return the schema (concepts, properties, and relations) for a namespace.

        Args:
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.
            namespace: Namespace to inspect (default: "default")

        Returns:
            Dict with "concepts" key listing concept names, their property
            definitions, and their relation definitions from the active
            schema version (SPEC §14.4), or "error" if no token was
            resolvable or it does not resolve to a valid principal.
        """
        from ontolith.core.errors import AuthError

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            auth_provider.resolve(token)
            ir = kb.backend.get_schema(namespace)
        except OntolithError as exc:
            return _error_response(exc)
        if ir is None:
            return {"concepts": []}
        concepts = []
        for concept_name, concept_def in ir.concepts.items():
            concepts.append(
                {
                    "name": concept_name,
                    "properties": [
                        {
                            "name": prop_name,
                            "type": prop_def.value_type,
                            "cardinality": prop_def.cardinality,
                            "temporality": prop_def.temporality,
                            "required": prop_def.required,
                        }
                        for prop_name, prop_def in concept_def.properties.items()
                    ],
                    "relations": [
                        {
                            "name": rel_name,
                            "target_concept": rel_def.target_concept,
                            "cardinality": rel_def.cardinality,
                            "required": rel_def.required,
                            "temporality": rel_def.temporality,
                            "inverse": rel_def.inverse,
                        }
                        for rel_name, rel_def in concept_def.relations.items()
                    ],
                }
            )
        return {"namespace": namespace, "version": ir.version, "concepts": concepts}

    # ------------------------------------------------------------------
    # ontolith.get — fetch entity + active assertions
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.get")
    def get_tool(entity_id: str, token: str | None = None) -> dict[str, Any]:
        """Fetch an entity and its currently active assertions.

        Args:
            entity_id: Entity ID to retrieve
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.

        Returns:
            Dict with "entity" and "assertions" keys, or "error" if not found,
            no token was resolvable, or it does not resolve to a valid
            principal.
        """
        from ontolith.core.errors import AuthError, NotFoundError

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            auth_provider.resolve(token)
            entity = kb.backend.get_entity(entity_id)
            if entity is None:
                return _error_response(NotFoundError(f"Entity {entity_id!r} not found"))
            assertions = kb.backend.assertions(subject=entity_id, status="active")
        except OntolithError as exc:
            return _error_response(exc)
        return {
            "entity": {
                "id": entity.id,
                "concept": entity.concept,
                "namespace": entity.namespace,
                "natural_key": entity.natural_key,
                "created_at": entity.created_at.isoformat(),
                "created_by": entity.created_by,
            },
            "assertions": [
                {
                    "id": a.id,
                    "predicate": a.predicate,
                    "value": a.value,
                    "value_type": a.value_type,
                    "confidence": a.confidence,
                    "author": a.author,
                    "asserted_at": a.asserted_at.isoformat(),
                }
                for a in assertions
            ],
        }

    # ------------------------------------------------------------------
    # ontolith.create_entity (KI-082)
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.create_entity")
    def create_entity_tool(
        concept: str,
        natural_key: str | None = None,
        token: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Create a new entity. Propose-tier: a ``read``-only principal is
        refused (``CAPABILITY_ERROR``) for every caller kind.

        An ``ai``-kind principal does not create the entity directly: this
        stages a one-operation batch (ADR-0055) and returns the proposal and
        decision, in the same shape as ``ontolith.propose_batch``. Under the
        default ``ThresholdPolicy`` it goes to review, and a rejected
        proposal leaves no entity behind.

        Args:
            concept: Concept name (e.g. "Person", "Organization")
            natural_key: Optional unique key within concept
            model: Model family+version; required when the calling principal
                is AI-kind, ignored otherwise
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.

        Returns:
            Dict with the created entity's fields (or, for an AI principal,
            a "proposal" and "decision" like ``ontolith.propose_batch``), or
            "error" if no token was resolvable, it does not resolve to a
            valid principal, the
            principal lacks propose capability, `concept` isn't declared in
            the active schema (KI-090), or `natural_key` is already taken
            within `concept` (KI-091).
        """
        from ontolith.core.errors import AuthError

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            caller = auth_provider.resolve(token)
            if caller.kind == "ai":
                proposal, decision = kb.propose_batch(
                    [CreateEntityOp(key="entity", concept=concept, natural_key=natural_key)],
                    caller.id,
                    model=model,
                )
                return _batch_response(proposal, decision)
            entity = kb.create_entity(concept=concept, author=caller.id, natural_key=natural_key)
        except OntolithError as exc:
            return _error_response(exc)
        return {
            "id": entity.id,
            "concept": entity.concept,
            "namespace": entity.namespace,
            "natural_key": entity.natural_key,
            "created_at": entity.created_at.isoformat(),
            "created_by": entity.created_by,
        }

    # ------------------------------------------------------------------
    # ontolith.query — symbolic entity retrieval
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.query")
    def query_tool(
        concept: str,
        token: str | None = None,
        filters: dict[str, str] | None = None,
        semantic: str | None = None,
        as_of: str | None = None,
        min_confidence: float | None = None,
        trust_at_least: int | None = None,
        limit: int | None = None,
        include_flagged: bool = False,
        include_history: bool = False,
    ) -> dict[str, Any]:
        """Query entities of a concept, optionally filtered/ranked/pinned in time.

        Mirrors REST's ``POST /query`` and GraphQL's ``Query.query`` wiring
        of the same ``QueryBuilder`` (SPEC §11.3, §14.4) — this tool
        previously exposed only ``filters`` (KI-058), leaving agents, MCP's
        own audience, with no way to use hybrid/semantic retrieval or cap
        result size at all.

        Args:
            concept: Concept name to query (e.g. "Person")
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.
            filters: Optional dict of property/relation name → value
                (equality) — a relation filter matches the relation's
                target entity id (e.g. {"employer": "org-123"}) — or
                property__op → value using a closed set of lookup-operator
                suffixes (KI-039): __contains (substring), __gt/__lt/__gte/
                __lte (numeric range, schema-declared Integer/Float
                properties only), e.g. {"age__gte": 18}. Multi-hop
                double-underscore keys (e.g. "employer__name") and any
                suffix outside this closed operator set are not supported
                and raise a validation_error (ADR-0027, KI-030).
            semantic: Optional query text — ranks results by vector
                similarity (SPEC §11.3) instead of/in addition to
                ``filters``. Requires the server's ``Ontology`` to have an
                ``Embedder`` configured; otherwise returns a
                validation_error.
            as_of: Optional ISO-8601 timestamp (SPEC §11.4) — reconstructs
                the knowledge base as it stood at that point in time
                instead of querying current state. The first of the four
                shipped interfaces to expose bitemporal time-travel at all
                (REST/GraphQL/CLI don't yet — KI-058's own scope is MCP
                only). A malformed value returns a validation_error.
                Combined with ``semantic``: only entity *existence* as of
                this time is reconstructed (an entity that didn't exist yet
                is excluded) — the ranking itself is not bitemporal, since
                the vector index holds one embedding per entity as of its
                last reindex, not a historical version. Results are always
                ranked by an entity's *current* embedded content.
            min_confidence: Optional 0.0-1.0 floor — keep only entities
                with at least one qualifying assertion at or above this
                confidence (ADR-0004: an assertion with confidence=None
                never satisfies a numeric threshold). Without ``as_of``:
                ``active`` by default, also ``flagged`` when
                ``include_flagged`` is set and/or ``superseded``/
                ``retracted`` when ``include_history`` is set (KI-093).
                With ``as_of``: the qualifying set is never restricted to
                ``active`` in the first place, so ``include_history`` has
                nothing to add for a ``superseded`` assertion there;
                ``include_flagged`` still applies (excludes ``flagged``
                point-in-time, KI-097 — not by current status, so a `t`
                during a since-resolved dispute still excludes and a `t`
                before any dispute still includes). A ``retracted``
                assertion is the exception (ADR-0049, KI-095): ``as_of``
                additionally excludes it once its own retraction has
                happened (assertion-time), and ``include_history`` opts
                back out of that exclusion.
            trust_at_least: Optional floor on a qualifying assertion's
                *effective* trust_level (min(author, acting_as) under
                delegation, KI-047) — independent of ``min_confidence``;
                the qualifying assertion need not be the same for both.
            limit: Optional cap on the number of entities returned.
            include_flagged: Also match ``filters``/``min_confidence``/
                ``trust_at_least`` against ``flagged`` (contradicted)
                assertions — excluded by default (SPEC §10.3, KI-081/093).
                Under ``as_of``, point-in-time not current status (KI-097):
                see ``min_confidence``'s own note below for what that means.
            include_history: Also match against ``superseded``/
                ``retracted`` assertions — excluded by default (KI-081/093).
                Mostly a no-op combined with ``as_of``: that branch never
                restricts matches to ``active`` in the first place — it
                excludes only ``flagged`` (itself gated behind
                ``include_flagged``) — so a superseded assertion whose
                validity window covers the queried instant already matches
                without this opt-in. A retracted assertion is the
                exception (ADR-0049, KI-095): ``as_of`` additionally
                excludes it once its own retraction event's assertion-time
                has passed, and this opt-in is what lets a caller see it
                anyway.

        Returns:
            Dict with "entities" list and "count", or "error" if no token
            was resolvable, it does not resolve to a valid principal, a
            filter key is invalid, ``as_of`` doesn't parse, or ``semantic``
            was given with no Embedder configured.
        """
        from ontolith.core.errors import AuthError, ValidationError

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            auth_provider.resolve(token)
            if as_of is not None:
                try:
                    builder = kb.as_of(as_of).query(concept)
                except ValueError as exc:
                    return _error_response(ValidationError(f"Invalid as_of value: {exc}"))
            else:
                builder = kb.query(concept)
            if filters:
                builder = builder.where(**filters)
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
        except OntolithError as exc:
            return _error_response(exc)
        return {
            "concept": concept,
            "count": len(entities),
            "entities": [
                {
                    "id": e.id,
                    "concept": e.concept,
                    "natural_key": e.natural_key,
                    "created_at": e.created_at.isoformat(),
                }
                for e in entities
            ],
        }

    # ------------------------------------------------------------------
    # ontolith.provenance — full trail for an assertion
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.provenance")
    def provenance_tool(assertion_id: str, token: str | None = None) -> dict[str, Any]:
        """Return the full provenance record for a single assertion.

        Args:
            assertion_id: Assertion ID to inspect
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.

        Returns:
            Dict with assertion details including author, confidence, source,
            rationale, proposal link, temporal fields, and superseded_ids
            (the full predecessor set this assertion superseded — supersedes
            alone only records the first, see KI-008), or "error" if not
            found, no token was resolvable, or it does not resolve to a
            valid principal.
        """
        from ontolith.core.errors import AuthError

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            auth_provider.resolve(token)
            prov = kb.provenance(assertion_id)  # raises NotFoundError, caught below
        except OntolithError as exc:
            return _error_response(exc)

        a = prov.assertion
        return {
            "id": a.id,
            "subject": a.subject,
            "predicate": a.predicate,
            "value": a.value,
            "value_type": a.value_type,
            "status": a.status,
            "author": a.author,
            "confidence": a.confidence,
            "source": a.source,
            "rationale": a.rationale,
            "model": a.model,
            "asserted_at": a.asserted_at.isoformat(),
            "valid_from": a.valid_from.isoformat() if a.valid_from else None,
            "valid_to": a.valid_to.isoformat() if a.valid_to else None,
            "proposal_id": a.proposal_id,
            "supersedes": a.supersedes,
            "superseded_ids": list(prov.superseded_ids),
            "review_events": [
                {"actor": e.actor, "type": e.type, "detail": e.detail, "at": e.at.isoformat()}
                for e in prov.review_events
            ],
        }

    # ------------------------------------------------------------------
    # ontolith.list_contradictions — read-only contradiction listing
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.list_contradictions")
    def list_contradictions_tool(
        state: str | None = "open", token: str | None = None
    ) -> dict[str, Any]:
        """List contradictions, defaulting to open (unresolved) ones.

        Mirrors REST's ``GET /contradictions`` and GraphQL's
        ``Query.contradictions`` (SPEC §14.1) — before this tool,
        ``ontolith.flag_contradiction`` (propose-tier: extends membership,
        flips assertion statuses to ``flagged``) was the only MCP surface
        that returned a contradiction at all, so an agent that only wanted
        to *read* one — including its accumulated ``rationale_history``,
        KI-071/KI-075 — had no way to do so without also performing a write
        (KI-076).

        Args:
            state: Filter by contradiction state ("open" or "resolved").
                Pass ``"all"`` for every state — same string REST's
                ``GET /contradictions``/GraphQL's ``Query.contradictions``
                both already document and accept, kept here too for
                cross-interface consistency even though MCP's JSON
                arguments could instead use an explicit ``null``
                unambiguously (unlike REST's HTTP query string, which
                can't express "no filter" any other way — see that route's
                own docstring). ``None``, passed explicitly, works
                identically to ``"all"``: both are accepted so a caller
                who already knows one sibling interface's convention isn't
                punished for using it. Anything else — including a
                near-miss like ``"Open"``, ``"unresolved"``, or the
                literal string ``"None"`` a model might emit for a null —
                raises a validation_error rather than silently matching
                zero contradictions, which an agent could otherwise
                mistake for "no contradictions exist" (KI-076 review).
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.

        Returns:
            Dict with "contradictions" list and "count", or "error" if no
            token was resolvable, it does not resolve to a valid principal,
            or ``state`` is none of "open"/"resolved"/"all"/``None``.
            Ungated beyond that — matches ``ontolith.query``/``.provenance``:
            `read` is the floor of SPEC §8.3's capability order, so any
            resolved principal already clears it, the same as
            ``proposals()``/``list_namespaces()`` at the SDK level.
        """
        from ontolith.core.errors import AuthError, ValidationError
        from ontolith.govern.contradiction import safe_rationale_history

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            auth_provider.resolve(token)
            # Validated after auth, not before (matches ontolith.query's
            # own as_of validation, KI-076 review): an unauthenticated
            # caller should learn "no token" before "bad argument," not
            # get a free, pre-auth probe of which state values this tool
            # accepts.
            if state not in (None, *_CONTRADICTION_STATES, "all"):
                return _error_response(ValidationError(f"Invalid state: {state!r}"))
            effective_state = None if state == "all" else state
            results = kb.contradictions(state=effective_state)
        except OntolithError as exc:
            return _error_response(exc)

        return {
            "count": len(results),
            "contradictions": [
                {
                    "id": c.id,
                    "namespace": c.namespace,
                    "subject": c.subject,
                    "predicate": c.predicate,
                    "state": c.state,
                    "member_ids": c.member_ids,
                    "created_at": c.created_at.isoformat(),
                    "raised_by": c.raised_by,
                    "resolved_by": c.resolved_by,
                    "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
                    "rationale_history": safe_rationale_history(c.metadata),
                }
                for c in results
            ],
        }

    # ------------------------------------------------------------------
    # ontolith.propose — create a proposal (NOT a direct write)
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.propose")
    def propose_tool(
        subject: str,
        predicate: str,
        token: str | None = None,
        value: str | None = None,
        value_type: str | None = None,
        target: str | None = None,
        confidence: float | None = None,
        source: str | None = None,
        rationale: str | None = None,
        acting_as: str | None = None,
        model: str | None = None,
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        """Create a proposal to assert a fact or relation. Does NOT write directly.

        The proposal is evaluated by the server's configured `PolicyStrategy`
        (SPEC §9.2). Under the default `ThresholdPolicy`:
        - Trusted principals → auto_accepted (assertion written immediately)
        - AI/low-trust principals → require_review (queued for human review)
        - Read-only principals → rejected

        A deployment MAY configure a different strategy (e.g. `SourceQuorum`,
        `Composite`) with different rules — `ThresholdPolicy`'s "AI principals
        always require review" guarantee (ADR-0003) is that strategy's own
        design choice, not enforced unconditionally by this tool. Check the
        server's actual policy configuration before relying on it (KI-061,
        ADR-0040).

        Conflict-routing temporality is resolved server-side from the active
        schema's declaration for ``predicate`` (SPEC §10.1) — it is not a
        caller-supplied argument.

        The acting principal is resolved from ``token`` (ADR-0014), never
        taken as a caller-supplied ID. When ``acting_as`` is set (delegation),
        policy is evaluated using min(capability(author), capability(acting_as))
        (SPEC §8.4, ADR-0003). ``model`` (the AI model family+version) is
        REQUIRED when the resolved principal is ``ai``-kind (SPEC §7.4/§14.4).

        Args:
            subject: Entity ID to assert about
            predicate: Predicate name (e.g. "Person.name" or "Person.employer")
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.
            value: Literal value to assert (mutually exclusive with target)
            value_type: Type of value (e.g. "Text", "Integer", "Date"); required with value
            target: Target entity ID for a relation (mutually exclusive with value)
            confidence: Optional confidence score (0.0–1.0)
            source: Optional source URL or reference
            rationale: Optional explanation for the assertion
            acting_as: Optional principal ID being acted on behalf of (delegation)
            model: Model family+version; required when the calling principal is AI-kind
            supersedes: Id of a specific existing assertion this one explicitly
                replaces (ADR-0050/KI-080, KI-099) — only meaningful for a
                cardinality="many", temporality="time_varying" property or
                relation; rejected otherwise

        Returns:
            Dict with "proposal" (id, state, policy_reason, acting_as) and "decision" type.
        """
        from ontolith.core.errors import AuthError, ValidationError

        has_literal = value is not None and value_type is not None
        has_ref = target is not None
        if has_literal == has_ref:
            return _error_response(
                ValidationError("Provide exactly one of (value and value_type) or target")
            )

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            author = auth_provider.resolve(token).id
            if has_ref:
                assert target is not None
                proposal, decision = kb.propose_ref(
                    subject=subject,
                    predicate=predicate,
                    target=target,
                    author=author,
                    confidence=confidence,
                    source=source,
                    rationale=rationale,
                    acting_as=acting_as,
                    model=model,
                    supersedes=supersedes,
                )
            else:
                assert value is not None and value_type is not None
                proposal, decision = kb.propose(
                    subject=subject,
                    predicate=predicate,
                    value=value,
                    value_type=value_type,
                    author=author,
                    confidence=confidence,
                    source=source,
                    rationale=rationale,
                    acting_as=acting_as,
                    model=model,
                    supersedes=supersedes,
                )
        except OntolithError as exc:
            return _error_response(exc)

        return {
            "proposal": {
                "id": proposal.id,
                "state": proposal.state,
                "policy_reason": proposal.policy_reason,
                "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
                "acting_as": proposal.acting_as,
            },
            "decision": type(decision).__name__,
        }

    # ------------------------------------------------------------------
    # ontolith.propose_batch — several entities/facts as one proposal (ADR-0055)
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.propose_batch")
    def propose_batch_tool(
        ops: list[dict[str, Any]],
        token: str | None = None,
        acting_as: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Propose several operations as ONE proposal, accepted or rejected
        atomically (ADR-0055). Does NOT write directly.

        Each entry of ``ops`` has an ``"op"`` of ``"create_entity"``,
        ``"assert_literal"`` or ``"assert_ref"``:

        - ``{"op": "create_entity", "key": "d1", "concept": "Decision",
          "natural_key": null}`` declares a new entity named ``key``.
        - ``{"op": "assert_literal", "subject": ..., "predicate": ...,
          "value": ..., "value_type": ...}`` plus optional ``confidence``,
          ``source``, ``rationale``, ``valid_from``, ``valid_to``,
          ``supersedes``.
        - ``{"op": "assert_ref", "subject": ..., "predicate": ...,
          "target": ...}`` plus the same optional fields.

        ``subject`` and ``target`` are a real entity id, or
        ``{"local": "d1"}`` to refer to a ``create_entity`` declared earlier
        in the same batch (no forward references, keys unique). New entity
        ids are allocated when the proposal is accepted. 1 to 100 ops.

        The server's ``PolicyStrategy`` is evaluated once per operation and
        the strictest outcome wins. The acting principal is resolved from
        ``token`` (ADR-0014). ``model`` is REQUIRED for an ``ai``-kind
        principal and is recorded on every assertion of the batch.

        Args:
            ops: The operations, in application order
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.
            acting_as: Optional principal ID being acted on behalf of (delegation)
            model: Model family+version; required when the calling principal is AI-kind

        Returns:
            Dict with "proposal" (id, state, policy_reason, decided_at,
            acting_as, and "created_entities" — local key to entity id — once
            the batch is applied) and "decision" type; or "error".
        """
        from ontolith.core.errors import AuthError

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            author = auth_provider.resolve(token).id
            proposal, decision = kb.propose_batch(
                _parse_batch_ops(ops), author, acting_as=acting_as, model=model
            )
        except OntolithError as exc:
            return _error_response(exc)
        return _batch_response(proposal, decision)

    # ------------------------------------------------------------------
    # ontolith.retract — propose retraction of an assertion (NOT a direct write)
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.retract")
    def retract_tool(
        assertion_id: str,
        token: str | None = None,
        acting_as: str | None = None,
    ) -> dict[str, Any]:
        """Propose retraction of an assertion through the governed
        proposal/policy pipeline (SPEC §9). Does NOT delete or write
        directly — same propose/policy/conflict-routing pipeline as
        ``ontolith.propose``, exposed at the same propose capability tier
        (ADR-0008/ADR-0039), unlike ``resolve_contradiction`` which stays
        reviewer-only and is deliberately not exposed via MCP at all.

        The acting principal is resolved from ``token`` (ADR-0014), never
        taken as a caller-supplied ID. When ``acting_as`` is set
        (delegation), the retraction is made on behalf of another
        principal (SPEC §8.4, ADR-0003).

        Args:
            assertion_id: ID of the assertion to retract
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.
            acting_as: Optional principal ID being acted on behalf of (delegation)

        Returns:
            Dict with "proposal" (id, state, policy_reason, decided_at) and
            "decision" type, or "error".

        Note:
            ``AuthError`` (bad token, or an unknown ``acting_as``),
            ``CapabilityError`` (delegation not owned, or the
            KI-033/KI-043 contradiction party/floor guards), and
            ``NotFoundError`` (unknown ``assertion_id`` — ``retract()``
            checks explicitly rather than letting it surface as an opaque
            ``StorageError`` off the backend write, KI-074 review) are
            reachable from ``kb.retract()`` — all handled the same way,
            alongside every other tool, by the module-level
            ``_error_response()`` blanket handler (KI-074).
        """
        from ontolith.core.errors import AuthError

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            author = auth_provider.resolve(token).id
            proposal, decision = kb.retract(assertion_id, author=author, acting_as=acting_as)
        except OntolithError as exc:
            return _error_response(exc)

        return {
            "proposal": {
                "id": proposal.id,
                "state": proposal.state,
                "policy_reason": proposal.policy_reason,
                "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
                "acting_as": proposal.acting_as,
            },
            "decision": type(decision).__name__,
        }

    # ------------------------------------------------------------------
    # ontolith.flag_contradiction — open/extend a contradiction
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.flag_contradiction")
    def flag_contradiction_tool(
        assertion_id_a: str,
        assertion_id_b: str,
        token: str | None = None,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """Flag two assertions as contradictory and route them to review.

        Opens a new contradiction (or extends an existing open one) for the
        subject+predicate pair. Both assertions are marked "flagged" and
        excluded from default queries until the contradiction is resolved.

        This is a propose-level action (requires >= propose capability) — it
        does NOT resolve the contradiction. The acting principal is resolved
        from ``token`` (ADR-0014), never taken as a caller-supplied ID.

        Args:
            assertion_id_a: First conflicting assertion ID
            assertion_id_b: Second conflicting assertion ID
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.
            rationale: Optional explanation of the contradiction

        Returns:
            Dict describing the contradiction created/extended, or "error".
            ``rationale_history`` (KI-075) carries every rationale ever
            recorded for this contradiction, not just the one from this
            call — one ``{"rationale", "actor", "at"}`` entry per prior
            call that supplied a non-empty rationale (KI-071).
        """
        from ontolith.core.errors import AuthError
        from ontolith.govern.contradiction import safe_rationale_history

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            author = auth_provider.resolve(token).id
            contradiction, action = kb.flag_contradiction(
                assertion_id_a, assertion_id_b, author, rationale=rationale
            )
        except OntolithError as exc:
            return _error_response(exc)

        return {
            "contradiction_id": contradiction.id,
            "subject": contradiction.subject,
            "predicate": contradiction.predicate,
            "member_ids": [assertion_id_a, assertion_id_b],
            "action": action,
            "raised_by": contradiction.raised_by,
            # KI-075: the accumulated rationale trail (KI-071) — [] if no
            # call in this contradiction's history has ever supplied one.
            # safe_rationale_history, not a raw .get() (KI-076 review): a
            # malformed metadata blob must produce the same coerced shape
            # here as it does from ontolith.list_contradictions, not a
            # differently-shaped response for the same field on a
            # different MCP tool.
            "rationale_history": safe_rationale_history(contradiction.metadata),
        }

    # ------------------------------------------------------------------
    # ontolith.resubmit — resubmit a changes_requested proposal (NOT write)
    # ------------------------------------------------------------------

    @mcp.tool(name="ontolith.resubmit")
    def resubmit_tool(proposal_id: str, token: str | None = None) -> dict[str, Any]:
        """Resubmit a proposal after changes were requested (KI-027).

        Re-evaluates policy against a fresh view of the knowledge base,
        same as ``ontolith.propose`` — this is not a direct write. Only the
        proposal's own author (or delegating principal) may call this; the
        acting principal is resolved from ``token`` (ADR-0014), never taken
        as a caller-supplied ID. This is the only MCP-exposed way for an AI
        principal to act on a ``changes_requested`` proposal it authored —
        under the default ``ThresholdPolicy``, AI proposals always route to
        require_review (ADR-0003), so an AI author reaching
        changes_requested has no write capability to fall back on outside
        this tool. A deployment on a different `PolicyStrategy` (KI-061,
        ADR-0040) may route AI proposals differently.

        Args:
            proposal_id: ID of the proposal to resubmit
            token: Bearer token identifying the calling principal (ADR-0014).
                Optional: an ``Authorization`` header takes priority when the
                transport supplies one (KI-067, see module docstring); this
                argument is the fallback, and the only channel on stdio —
                unless the server was created with ``require_header_token=True``
                (KI-073), in which case this argument is never accepted and
                stdio becomes unusable.

        Returns:
            Dict with "proposal" (id, state, policy_reason, decided_at) and
            "decision" type, or "error".
        """
        from ontolith.core.errors import AuthError

        token, token_error = _bearer_token(token)
        if token_error is not None:
            return _error_response(AuthError(token_error))
        assert token is not None  # _bearer_token: exactly one of (token, error) is set
        try:
            author = auth_provider.resolve(token).id
            proposal, decision = kb.resubmit(proposal_id, author)
        except OntolithError as exc:
            return _error_response(exc)

        return {
            "proposal": {
                "id": proposal.id,
                "state": proposal.state,
                "policy_reason": proposal.policy_reason,
                "decided_at": proposal.decided_at.isoformat() if proposal.decided_at else None,
            },
            "decision": type(decision).__name__,
        }

    return mcp


__all__ = ["create_mcp_server"]
