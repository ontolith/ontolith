"""Unit tests for the MCP server (ADR-0008, ADR-0014).

Verifies that all 9 tools return the correct structure, that the no-write
invariant holds, and that the acting principal is always resolved from a
verified bearer token (ADR-0014), never a caller-supplied ID. Tests run
against a real in-memory SQLite KB.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from starlette.datastructures import Headers

from ontolith import Ontology
from ontolith.core import Assertion, FixedClock, FixedIdProvider
from ontolith.core.observability import RecordingObservabilitySink
from ontolith.identity.token_auth import TokenAuthProvider
from ontolith.interfaces.mcp import create_mcp_server
from ontolith.schema.ir import ConceptDef, PropertyDef, RelationDef, SchemaIR

T0 = datetime(2025, 1, 1, tzinfo=UTC)
HUMAN = "alice@example.com"
AI = "scout-agent"
AI_OWNER = HUMAN
REVIEWER = "bob@example.com"
ADMIN = "admin@example.com"


def _kb(tmp_path: Path) -> Ontology:
    clock = FixedClock(T0)
    ids = FixedIdProvider([f"id-{i}" for i in range(50)])
    kb = Ontology.connect(tmp_path / "test.db", clock=clock, id_provider=ids)
    kb.create_principal(HUMAN, kind="human", auth_method="oidc", default_capability="write")
    kb.create_principal(REVIEWER, kind="human", auth_method="oidc", default_capability="review")
    kb.create_principal(
        AI, kind="ai", auth_method="apikey", owner=AI_OWNER, default_capability="propose"
    )
    kb.create_principal(ADMIN, kind="human", auth_method="oidc", default_capability="admin")
    return kb


def _server(kb: Ontology, *, require_header_token: bool = False) -> tuple:
    auth_provider = TokenAuthProvider(kb.backend)
    return (
        create_mcp_server(kb, auth_provider, require_header_token=require_header_token),
        auth_provider,
    )


@contextlib.contextmanager
def _http_request(authorization: str | None) -> Iterator[None]:
    """Simulate a tool call arriving over the SSE/streamable-HTTP transport
    with the given raw `Authorization` header value (KI-067) — mirrors the
    mcp SDK's own `ServerMessageMetadata(request_context=<starlette Request>)`
    wiring, which `create_mcp_server`'s `_bearer_token` helper reads via
    `mcp.get_context().request_context.request`. `authorization=None` means
    a live HTTP request with no such header at all — distinct from no HTTP
    request/context existing (the plain `.fn(...)` call outside this context
    manager, which every pre-KI-067 test in this file already exercises)."""
    headers = Headers({"Authorization": authorization} if authorization is not None else {})
    fake_request = SimpleNamespace(headers=headers)
    ctx = RequestContext(
        request_id="test-request",
        meta=None,
        session=None,
        lifespan_context=None,
        request=fake_request,
    )
    reset_token = request_ctx.set(ctx)
    try:
        yield
    finally:
        request_ctx.reset(reset_token)


async def _call_tool_over_real_transport(
    mcp: object, tool_name: str, arguments: dict[str, object], authorization: str | None
) -> object:
    """Drive one tool call through the real streamable-HTTP ASGI transport —
    a genuine JSON-RPC initialize handshake, real session management, and an
    actual `Authorization` header on an in-process HTTP request — instead of
    `_http_request`'s faked `RequestContext`. Closes the gap where the fake
    could silently drift from the SDK's real wiring while every faked-context
    test kept passing (KI-067 review). Uses `httpx` (not this repo's `httpx2`
    used elsewhere in this file) because `mcp.client.streamable_http` is
    itself built on plain `httpx`, not `httpx2`.
    """
    import httpx
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    app = mcp.streamable_http_app()  # type: ignore[attr-defined]

    headers = {"Authorization": authorization} if authorization is not None else {}
    # DNS-rebinding protection (mcp SDK default) rejects a bare "localhost"
    # Host header with no port — base_url must include one.
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000", headers=headers
    )

    async with mcp.session_manager.run():  # type: ignore[attr-defined]
        async with http_client:
            async with streamable_http_client(
                "http://localhost:8000/mcp", http_client=http_client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return result.structuredContent


# ---------------------------------------------------------------------------
# ontolith.schema
# ---------------------------------------------------------------------------


class TestSchemaTool:
    def test_schema_returns_empty_when_no_schema_stored(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.schema").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result == {"concepts": []}

    def test_schema_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.schema").fn(token="not-a-real-token")
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_schema_returns_concepts_and_properties(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text", required=True),
                        "employer": PropertyDef(
                            name="employer", value_type="Text", temporality="time_varying"
                        ),
                        "nicknames": PropertyDef(
                            name="nicknames", value_type="Text", cardinality="many"
                        ),
                    },
                ),
            },
        )
        kb.backend.put_schema(schema)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.schema").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["namespace"] == "default"
        assert result["version"] == 1
        assert len(result["concepts"]) == 1
        person = result["concepts"][0]
        assert person["name"] == "Person"
        props_by_name = {p["name"]: p for p in person["properties"]}
        assert props_by_name["name"]["type"] == "Text"
        assert props_by_name["name"]["required"] is True
        assert props_by_name["name"]["cardinality"] == "single"
        assert props_by_name["employer"]["temporality"] == "time_varying"
        assert props_by_name["nicknames"]["cardinality"] == "many"
        assert person["relations"] == []

    def test_schema_returns_relations(self, tmp_path: Path) -> None:
        """KI-029: relations were previously omitted entirely - an agent
        inspecting the schema this way couldn't see that Person.employer
        exists, or whether it's time_varying (predicts supersession vs.
        contradiction on a subsequent proposal, SPEC §10.1)."""
        kb = _kb(tmp_path)
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
                            cardinality="single",
                            required=True,
                            temporality="time_varying",
                            inverse="employees",
                        ),
                    },
                ),
                "Organization": ConceptDef(
                    name="Organization",
                    relations={
                        "employees": RelationDef(
                            name="employees",
                            target_concept="Person",
                            cardinality="many",
                        ),
                    },
                ),
            },
        )
        kb.backend.put_schema(schema)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.schema").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        concepts_by_name = {c["name"]: c for c in result["concepts"]}
        employer = concepts_by_name["Person"]["relations"][0]
        assert employer["name"] == "employer"
        assert employer["target_concept"] == "Organization"
        assert employer["cardinality"] == "single"
        assert employer["required"] is True
        assert employer["temporality"] == "time_varying"
        assert employer["inverse"] == "employees"

        employees = concepts_by_name["Organization"]["relations"][0]
        assert employees["cardinality"] == "many"
        assert employees["required"] is False
        assert employees["inverse"] is None

    def test_schema_respects_namespace_argument(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.schema").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0], namespace="other"
        )
        assert result == {"concepts": []}


# ---------------------------------------------------------------------------
# ontolith.create_entity (KI-082)
# ---------------------------------------------------------------------------


class TestCreateEntityTool:
    def test_create(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.create_entity").fn(
            concept="Person",
            natural_key="ada",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )
        assert result["concept"] == "Person"
        assert result["natural_key"] == "ada"
        assert result["created_by"] == HUMAN

    def test_created_entity_is_retrievable(self, tmp_path: Path) -> None:
        """Round-trips through ontolith.get — not just a shape check on the
        create response, but confirms the entity is actually persisted
        where the rest of the server expects to find it."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]
        created = mcp._tool_manager.get_tool("ontolith.create_entity").fn(
            concept="Person", token=token
        )
        result = mcp._tool_manager.get_tool("ontolith.get").fn(entity_id=created["id"], token=token)
        assert result["entity"]["id"] == created["id"]

    def test_ai_caller_gets_a_proposal_and_no_entity(self, tmp_path: Path) -> None:
        """ADR-0055: an AI principal's create_entity stages a one-operation
        batch. Under ThresholdPolicy it goes to review; nothing is created
        until a reviewer accepts."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.create_entity").fn(
            concept="Person", model="m-1", token=kb.issue_token(AI, author=ADMIN)[0]
        )
        assert result["decision"] == "RequireReview"
        assert result["proposal"]["state"] == "require_review"
        assert "id" not in result
        assert kb.backend.entities(concept="Person") == []

        kb.accept_proposal(result["proposal"]["id"], REVIEWER)
        [entity] = kb.backend.entities(concept="Person")
        assert entity.created_by == AI

    def test_ai_caller_without_model_is_a_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.create_entity").fn(
            concept="Person", token=kb.issue_token(AI, author=ADMIN)[0]
        )
        assert result["code"] == "VALIDATION_ERROR"
        assert kb.backend.proposals() == []

    def test_read_only_ai_caller_is_refused_not_proposed(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "ro-agent", kind="ai", owner=HUMAN, auth_method="workload", default_capability="read"
        )
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.create_entity").fn(
            concept="Person", model="m-1", token=kb.issue_token("ro-agent", author=ADMIN)[0]
        )
        assert result["code"] == "CAPABILITY_ERROR"
        assert kb.backend.proposals() == []

    def test_human_model_argument_is_ignored(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.create_entity").fn(
            concept="Person", model="ignored", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["created_by"] == HUMAN

    def test_rejected_ai_proposal_leaves_no_entity(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.create_entity").fn(
            concept="Person", model="m-1", token=kb.issue_token(AI, author=ADMIN)[0]
        )
        kb.reject_proposal(result["proposal"]["id"], REVIEWER, "no")
        assert kb.backend.entities(concept="Person") == []

    def test_read_capability_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "readonly@example.com", kind="human", auth_method="oidc", default_capability="read"
        )
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.create_entity").fn(
            concept="Person",
            token=kb.issue_token("readonly@example.com", author=ADMIN)[0],
        )
        assert "error" in result
        assert result["code"] == "CAPABILITY_ERROR"

    def test_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.create_entity").fn(
            concept="Person", token="not-a-real-token"
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# ontolith.get
# ---------------------------------------------------------------------------


class TestGetTool:
    def test_get_known_entity(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(
            entity_id=entity.id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["entity"]["id"] == entity.id
        assert result["entity"]["concept"] == "Person"
        assert len(result["assertions"]) == 1
        assert result["assertions"][0]["value"] == "Ada"

    def test_get_unknown_entity_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(
            entity_id="does-not-exist", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_get_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(
            entity_id=entity.id, token="not-a-real-token"
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_get_excludes_non_active_assertions(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        kb.retract(active[0].id, HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.get").fn(
            entity_id=entity.id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["assertions"] == []


# ---------------------------------------------------------------------------
# ontolith.query
# ---------------------------------------------------------------------------


class TestQueryTool:
    def test_query_all_of_concept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Organization", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["count"] == 2
        assert len(result["entities"]) == 2

    def test_query_with_filter(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e1 = kb.create_entity("Person", author=HUMAN)
        e2 = kb.create_entity("Person", author=HUMAN)
        kb.propose(e1.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(e2.id, "Person.name", "Grace", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            filters={"name": "Ada"},
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == e1.id

    def test_query_empty_concept_returns_empty(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Organization", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["count"] == 0
        assert result["entities"] == []

    def test_query_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", token="not-a-real-token"
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_query_dunder_filter_key_returns_validation_error(self, tmp_path: Path) -> None:
        """A relation-traversal-shaped key fails loudly instead of returning an empty result (KI-030)."""
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            filters={"employer__name": "Acme Corp"},
        )
        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_query_semantic_restricts_to_indexed_entities(self, tmp_path: Path) -> None:
        """KI-058: `semantic` was previously unreachable from MCP at all.

        Pins that .semantic() is actually invoked, not ranking quality
        (that's QueryBuilder's own concern) - the second entity is created
        (and left unindexed) AFTER reindex(), so a real semantic search
        must exclude it (only the vector index's own entities are
        candidates); a no-op `semantic` that silently fell through to a
        plain, unranked entities() scan would include it (count=2), which
        is what makes this discriminating rather than vacuously passing on
        a single-entity KB."""
        kb = _kb(tmp_path)
        indexed = kb.create_entity("Person", author=HUMAN)
        kb.propose(indexed.id, "Person.name", "Ada Lovelace", "Text", HUMAN)
        kb.reindex()
        kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            semantic="Ada Lovelace",
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == indexed.id

    def test_query_unknown_namespace_argument_is_silently_dropped(self, tmp_path: Path) -> None:
        """KI-058 removed the tool's dead `namespace` parameter. Through the
        real schema-validated dispatch path (unlike `.fn()`, which every
        other test in this class uses and which bypasses the tool's JSON
        schema entirely), FastMCP drops arguments the schema doesn't
        declare rather than rejecting the call - so a caller still passing
        `namespace=` keeps succeeding, identically to before this KI, when
        it was already a silent no-op. Pins that this is genuinely
        non-breaking at the protocol level, not just at the Python level."""
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        async def _call() -> object:
            _, structured = await mcp.call_tool(
                "ontolith.query", {"concept": "Person", "token": token, "namespace": "other"}
            )
            return structured

        result = asyncio.run(_call())
        assert result == {
            "concept": "Person",
            "count": 1,
            "entities": [
                {
                    "id": "id-0",
                    "concept": "Person",
                    "natural_key": None,
                    "created_at": "2025-01-01T00:00:00+00:00",
                }
            ],
        }

    def test_query_as_of_excludes_entity_not_yet_existing_with_semantic(
        self, tmp_path: Path
    ) -> None:
        """Found while adding as_of/semantic coverage for KI-058: with no
        .where() filter, `_semantic_candidates()` previously ignored
        as_of_time entirely, so a semantic-only as_of query returned
        entities that didn't exist yet at that point in time. Fixed in the
        same PR (query/builder.py) since KI-058 is what first exposes this
        combination through any interface at all - REST/GraphQL don't have
        as_of wired in, so nothing could trigger it before now."""
        kb = _kb(tmp_path)
        existing = kb.create_entity("Person", author=HUMAN)
        kb.propose(existing.id, "Person.name", "Ada Lovelace", "Text", HUMAN)
        kb.reindex()

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=1)
        # Created AND reindexed only after T0 - a real as_of(T0) query must
        # exclude it even though it's in the vector index and matches
        # semantically.
        later = kb.create_entity("Person", author=HUMAN)
        kb.propose(later.id, "Person.name", "Ada Lovelace Jr", "Text", HUMAN)
        kb.reindex()

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            semantic="Ada Lovelace",
            as_of=T0.isoformat(),
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == existing.id

    def test_query_semantic_without_embedder_returns_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.embedder = None
        kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            semantic="anything",
        )
        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_query_min_confidence_filters_entities(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        confident = kb.create_entity("Person", author=HUMAN)
        unsure = kb.create_entity("Person", author=HUMAN)
        kb.propose(confident.id, "Person.name", "Ada", "Text", HUMAN, confidence=0.9)
        kb.propose(unsure.id, "Person.name", "Bob", "Text", HUMAN, confidence=0.1)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            min_confidence=0.5,
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == confident.id

    def test_query_trust_at_least_filters_entities(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "trusted@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            trust_level=8,
        )
        trusted_entity = kb.create_entity("Person", author=HUMAN)
        untrusted_entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(trusted_entity.id, "Person.name", "Ada", "Text", "trusted@example.com")
        kb.propose(untrusted_entity.id, "Person.name", "Bob", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            trust_at_least=5,
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == trusted_entity.id

    def test_query_limit_caps_result_count(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            limit=2,
        )
        assert result["count"] == 2
        assert len(result["entities"]) == 2

    def test_query_as_of_reconstructs_past_state(self, tmp_path: Path) -> None:
        """KI-058: MCP is the first of the four shipped interfaces to expose
        bitemporal time-travel at all — REST/GraphQL/CLI still don't."""
        kb = _kb(tmp_path)
        kb.backend.put_schema(
            SchemaIR(
                namespace="default",
                version=1,
                concepts={
                    "Person": ConceptDef(
                        name="Person",
                        properties={
                            "title": PropertyDef(
                                name="title", value_type="Text", temporality="time_varying"
                            ),
                        },
                    ),
                },
            )
        )
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.title", "Engineer", "Text", HUMAN)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)
        kb.propose(entity.id, "Person.title", "Manager", "Text", HUMAN)

        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        past = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=token,
            filters={"title": "Engineer"},
            as_of=T0.isoformat(),
        )
        assert past["count"] == 1
        assert past["entities"][0]["id"] == entity.id

        now = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", token=token, filters={"title": "Engineer"}
        )
        assert now["count"] == 0

    def test_query_malformed_as_of_returns_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            as_of="not-a-timestamp",
        )
        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_query_include_flagged_matches_a_flagged_assertion(self, tmp_path: Path) -> None:
        """KI-094: the opt-in is forwarded to QueryBuilder, not silently dropped."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", HUMAN)  # -> flagged
        assert kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")

        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]
        default = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", token=token, filters={"name": "Ada"}
        )
        assert default["count"] == 0

        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", token=token, filters={"name": "Ada"}, include_flagged=True
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == entity.id

    def test_query_include_history_matches_a_retracted_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.retract(assertion.id, HUMAN)

        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]
        default = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", token=token, filters={"name": "Ada"}
        )
        assert default["count"] == 0

        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=token,
            filters={"name": "Ada"},
            include_history=True,
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == entity.id

    def test_query_as_of_include_flagged_opts_into_a_flagged_assertion(
        self, tmp_path: Path
    ) -> None:
        """KI-094: `include_flagged` is forwarded on the `as_of` branch too,
        not only current-state — distinct code path from the other two new
        tests above, which never set `as_of`."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", HUMAN)  # -> flagged
        assert kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")

        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]
        default = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person", token=token, filters={"name": "Ada"}, as_of=T0.isoformat()
        )
        assert default["count"] == 0

        result = mcp._tool_manager.get_tool("ontolith.query").fn(
            concept="Person",
            token=token,
            filters={"name": "Ada"},
            as_of=T0.isoformat(),
            include_flagged=True,
        )
        assert result["count"] == 1
        assert result["entities"][0]["id"] == entity.id


# ---------------------------------------------------------------------------
# ontolith.provenance
# ---------------------------------------------------------------------------


class TestProvenanceTool:
    def test_provenance_known_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN, confidence=0.95, source="wiki")
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=assertions[0].id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["id"] == assertions[0].id
        assert result["author"] == HUMAN
        assert result["confidence"] == 0.95
        assert result["source"] == "wiki"
        assert result["subject"] == entity.id

    def test_provenance_surfaces_model_for_ai_authored_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "carol@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI, model="claude-sonnet-4"
        )
        kb.accept_proposal(proposal.id, "carol@example.com")
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=assertions[0].id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["model"] == "claude-sonnet-4"

    def test_provenance_surfaces_review_events_after_accept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "carol@example.com", kind="human", auth_method="oidc", default_capability="review"
        )
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI, model="claude-sonnet-4"
        )
        kb.accept_proposal(proposal.id, "carol@example.com")
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=assertions[0].id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert len(result["review_events"]) == 1
        assert result["review_events"][0]["type"] == "accept"
        assert result["review_events"][0]["actor"] == "carol@example.com"

    def test_provenance_review_events_empty_for_direct_write(self, tmp_path: Path) -> None:
        """A direct write (no proposal_id) has no review events - not an
        error, just an empty list."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=assertion.id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["proposal_id"] is None
        assert result["review_events"] == []

    def test_provenance_unknown_assertion_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id="nonexistent", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_provenance_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id="nonexistent", token="not-a-real-token"
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_provenance_reachable_for_retracted_assertion(self, tmp_path: Path) -> None:
        """Provenance must be resolvable for non-active assertions too — that's
        the audit-trail use case (regression: backend.assertions() defaults to
        status='active' and previously hid retracted/flagged/superseded records).
        """
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        kb.retract(active[0].id, HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=active[0].id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["id"] == active[0].id
        assert result["status"] == "retracted"

    def test_provenance_superseded_ids_include_all_concurrent_predecessors(
        self, tmp_path: Path
    ) -> None:
        """KI-008: supersedes alone only records the first predecessor when one
        incoming assertion supersedes several concurrently-overlapping ones."""
        kb = _kb(tmp_path)
        kb.backend.put_schema(
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
            )
        )
        entity = kb.create_entity("Person", author=HUMAN)

        a1 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.employer",
            value_kind="literal",
            value_type="Text",
            value="Acme Corp",
            author=HUMAN,
            asserted_at=T0,
            valid_from=T0,
        )
        kb.backend.put_assertion(a1)
        a2 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.employer",
            value_kind="literal",
            value_type="Text",
            value="Beta Inc",
            author=HUMAN,
            asserted_at=T0,
            valid_from=T0,
        )
        kb.backend.put_assertion(a2)

        clock = kb.clock
        assert isinstance(clock, FixedClock)
        clock.advance(days=180)
        kb.propose(entity.id, "Person.employer", "Gamma Ltd", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.employer", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.provenance").fn(
            assertion_id=active[0].id, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["supersedes"] in {a1.id, a2.id}
        assert set(result["superseded_ids"]) == {a1.id, a2.id}


# ---------------------------------------------------------------------------
# ontolith.propose
# ---------------------------------------------------------------------------


class TestProposeTool:
    def test_propose_human_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert result["proposal"]["state"] == "auto_accepted"
        assert result["decision"] == "AutoAccept"

    def test_propose_ai_without_model_returns_validation_error(self, tmp_path: Path) -> None:
        """model is required for ai-kind authors (SPEC §7.4) — enforced even
        via the MCP tool wrapper, not just the SDK method directly."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=kb.issue_token(AI, author=ADMIN)[0],
        )
        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_propose_ai_requires_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=kb.issue_token(AI, author=ADMIN)[0],
            model="test-model-v1",
        )

        assert result["proposal"]["state"] == "require_review"
        assert result["decision"] == "RequireReview"

    def test_propose_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        """A garbage/unknown token is rejected — this is the core fix: the
        acting principal can no longer be spoofed by naming any principal ID."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token="not-a-real-token",
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_propose_revoked_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        token, credential_id = kb.issue_token(HUMAN, author=ADMIN)
        kb.revoke_token(credential_id, author=ADMIN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=token,
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_propose_unauthorized_delegation_returns_capability_error(self, tmp_path: Path) -> None:
        """acting_as a principal that isn't the author's owner is rejected (ADR-0003)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.create_principal(
            "carol@example.com", kind="human", auth_method="oidc", default_capability="write"
        )

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=kb.issue_token(AI, author=ADMIN)[0],  # owner=alice
            acting_as="carol@example.com",  # not AI's owner
            model="test-model-v1",
        )
        assert "error" in result
        assert result["code"] == "CAPABILITY_ERROR"

    def test_propose_does_not_expose_direct_write(self, tmp_path: Path) -> None:
        """MCP propose tool must not bypass policy — AI assertions need review."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ada",
            value_type="Text",
            token=kb.issue_token(AI, author=ADMIN)[0],
            model="test-model-v1",
        )

        # No assertion written yet — still pending review
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active == []

    def test_propose_with_target_creates_relation(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        person = kb.create_entity("Person", author=HUMAN)
        org = kb.create_entity("Organization", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=person.id,
            predicate="Person.employer",
            target=org.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert result["proposal"]["state"] == "auto_accepted"
        active = kb.assertions(subject=person.id, predicate="Person.employer", status="active")
        assert len(active) == 1
        assert active[0].value_kind == "ref"
        assert active[0].value == org.id

    def test_propose_neither_value_nor_target_returns_validation_error(
        self, tmp_path: Path
    ) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_propose_both_value_and_target_returns_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        person = kb.create_entity("Person", author=HUMAN)
        org = kb.create_entity("Organization", author=HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=person.id,
            predicate="Person.employer",
            value="Acme",
            value_type="Text",
            target=org.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_supersedes_forwarded_to_propose(self, tmp_path: Path) -> None:
        """KI-099: `supersedes` (ADR-0050/KI-080) reaches `Ontology.propose`
        through `ontolith.propose`."""
        kb = _kb(tmp_path)
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "title": PropertyDef(
                            name="title",
                            value_type="Text",
                            temporality="time_varying",
                            cardinality="many",
                        ),
                    },
                ),
            },
        )
        kb.apply_schema(schema, author=ADMIN)
        entity = kb.create_entity("Person", author=HUMAN)
        sales = kb.assert_literal(entity.id, "Person.title", "VP Sales", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.title",
            value="SVP Sales",
            value_type="Text",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            supersedes=sales.id,
        )

        assert result["proposal"]["state"] == "auto_accepted"
        active = kb.assertions(subject=entity.id, predicate="Person.title", status="active")
        assert {a.value for a in active} == {"SVP Sales"}

    def test_supersedes_forwarded_to_propose_ref(self, tmp_path: Path) -> None:
        """KI-099: `supersedes` reaches `Ontology.propose_ref` through
        `ontolith.propose`'s `target`-shaped branch, not just its
        literal-value one."""
        kb = _kb(tmp_path)
        schema = SchemaIR(
            namespace="default",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    relations={
                        "colleague": RelationDef(
                            name="colleague",
                            target_concept="Person",
                            temporality="time_varying",
                            cardinality="many",
                        ),
                    },
                ),
            },
        )
        kb.apply_schema(schema, author=ADMIN)
        entity = kb.create_entity("Person", author=HUMAN)
        colleague_a = kb.create_entity("Person", author=HUMAN)
        colleague_b = kb.create_entity("Person", author=HUMAN)
        rel_a = kb.assert_ref(entity.id, "Person.colleague", colleague_a.id, HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.colleague",
            target=colleague_b.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            supersedes=rel_a.id,
        )

        assert result["proposal"]["state"] == "auto_accepted"
        active = kb.assertions(subject=entity.id, predicate="Person.colleague", status="active")
        assert {a.value for a in active} == {colleague_b.id}

    def test_supersedes_rejected_outside_many_time_varying(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        existing = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.propose").fn(
            subject=entity.id,
            predicate="Person.name",
            value="Ava",
            value_type="Text",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            supersedes=existing.id,
        )
        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_no_write_tool_registered(self, tmp_path: Path) -> None:
        """ADR-0008: no direct write, update, or delete tool must be registered."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        forbidden = {"ontolith.write", "ontolith.update", "ontolith.delete", "ontolith.assert"}
        assert tool_names.isdisjoint(forbidden)

    def test_all_required_tools_registered(self, tmp_path: Path) -> None:
        """ADR-0008 plus resubmit (KI-027), retract (KI-057, ADR-0039),
        list_contradictions (KI-076), create_entity (KI-082), and propose_batch
        (ADR-0055): the
        registered tool set must be exactly these 11 — no more, no fewer
        (KI-085). A subset check here would pass even if a future PR added
        a 12th tool under a name test_no_write_tool_registered's blocklist
        doesn't happen to cover; adding a tool on purpose means updating
        this set deliberately."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        tool_names = {t.name for t in mcp._tool_manager.list_tools()}
        required = {
            "ontolith.schema",
            "ontolith.get",
            "ontolith.create_entity",
            "ontolith.propose_batch",
            "ontolith.query",
            "ontolith.provenance",
            "ontolith.list_contradictions",
            "ontolith.propose",
            "ontolith.flag_contradiction",
            "ontolith.resubmit",
            "ontolith.retract",
        }
        assert tool_names == required


# ---------------------------------------------------------------------------
# ontolith.propose_batch (ADR-0055)
# ---------------------------------------------------------------------------


def _decision_ops(owner: str) -> list[dict]:
    return [
        {"op": "create_entity", "key": "d1", "concept": "Decision"},
        {
            "op": "assert_literal",
            "subject": {"local": "d1"},
            "predicate": "Decision.title",
            "value": "Ship it",
            "value_type": "Text",
        },
        {
            "op": "assert_ref",
            "subject": {"local": "d1"},
            "predicate": "Decision.owner",
            "target": owner,
        },
    ]


class TestProposeBatchTool:
    def _tool(self, kb: Ontology):
        mcp, _ = _server(kb)
        return mcp._tool_manager.get_tool("ontolith.propose_batch").fn

    def test_ai_batch_goes_to_review_then_creates_everything_on_accept(
        self, tmp_path: Path
    ) -> None:
        kb = _kb(tmp_path)
        owner = kb.create_entity("Person", author=HUMAN).id
        result = self._tool(kb)(
            ops=_decision_ops(owner), model="m-1", token=kb.issue_token(AI, author=ADMIN)[0]
        )
        assert result["decision"] == "RequireReview"
        assert kb.backend.entities(concept="Decision") == []

        kb.accept_proposal(result["proposal"]["id"], REVIEWER)

        [decision] = kb.backend.entities(concept="Decision")
        assert {a.predicate for a in kb.backend.assertions(subject=decision.id)} == {
            "Decision.title",
            "Decision.owner",
        }

    def test_human_batch_auto_accepts_and_reports_created_ids(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        owner = kb.create_entity("Person", author=HUMAN).id
        result = self._tool(kb)(
            ops=_decision_ops(owner), token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        [decision] = kb.backend.entities(concept="Decision")
        assert result["decision"] == "AutoAccept"
        assert result["proposal"]["created_entities"] == {"d1": decision.id}

    @pytest.mark.parametrize(
        "ops",
        [
            [{"op": "teleport"}],
            [{"concept": "Person", "key": "k"}],
            [{"op": "create_entity", "key": "k", "concept": "Person", "bogus": 1}],
            [
                {
                    "op": "assert_literal",
                    "subject": {"local": "k", "extra": 1},
                    "predicate": "Person.name",
                    "value": "x",
                    "value_type": "Text",
                }
            ],
            [
                {
                    "op": "assert_literal",
                    "subject": {"local": ""},
                    "predicate": "Person.name",
                    "value": "x",
                    "value_type": "Text",
                }
            ],
            [{"op": "create_entity", "key": f"k{i}", "concept": "Person"} for i in range(101)],
            [],
        ],
    )
    def test_malformed_ops_are_validation_errors(self, tmp_path: Path, ops: list[dict]) -> None:
        kb = _kb(tmp_path)
        result = self._tool(kb)(ops=ops, token=kb.issue_token(HUMAN, author=ADMIN)[0])
        assert result["code"] == "VALIDATION_ERROR"
        assert kb.backend.proposals() == []

    def test_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        result = self._tool(kb)(
            ops=[{"op": "create_entity", "key": "k", "concept": "Person"}], token="nope"
        )
        assert result["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# ontolith.list_contradictions
# ---------------------------------------------------------------------------


class TestListContradictionsTool:
    def _flag(self, kb: Ontology, entity_id: str, *, rationale: str | None = None) -> None:
        kb.assert_literal(entity_id, "Person.name", "Ada", "Text", HUMAN)
        second = kb.assert_literal(entity_id, "Person.name", "Ava", "Text", HUMAN)
        first = kb.assertions(subject=entity_id, predicate="Person.name", status="flagged")[0]
        kb.flag_contradiction(first.id, second.id, HUMAN, rationale=rationale)

    def _one_resolved_and_one_open(self, kb: Ontology) -> None:
        """Seeds two contradictions in different states, so an "every
        state" filter test can assert `count == 2` and not just "whichever
        one state happened to come back" (KI-076 review) — a mapping bug
        that coincidentally returned the resolved one alone (e.g. treating
        `"all"` as a literal state value equal to `"resolved"`) would pass
        a single-contradiction test just as easily as the real behavior."""
        resolved_entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, resolved_entity.id)
        [contradiction] = kb.contradictions()
        flagged = kb.assertions(
            subject=resolved_entity.id, predicate="Person.name", status="flagged"
        )
        kb.resolve_contradiction(contradiction.id, flagged[0].id, REVIEWER)

        open_entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, open_entity.id)

    def test_lists_open_contradictions_by_default(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, entity.id)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["count"] == 1
        assert result["contradictions"][0]["state"] == "open"
        assert result["contradictions"][0]["subject"] == entity.id

    def test_no_contradictions_returns_empty(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result == {"count": 0, "contradictions": []}

    def test_resolved_excluded_by_default(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, entity.id)
        [contradiction] = kb.contradictions()
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        kb.resolve_contradiction(contradiction.id, flagged[0].id, REVIEWER)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result == {"count": 0, "contradictions": []}

    def test_state_resolved_filters_correctly(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, entity.id)
        [contradiction] = kb.contradictions()
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        kb.resolve_contradiction(contradiction.id, flagged[0].id, REVIEWER)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            state="resolved", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["count"] == 1
        assert result["contradictions"][0]["state"] == "resolved"

    def test_state_none_lists_every_state(self, tmp_path: Path) -> None:
        """`state=None`, passed explicitly, is one of two ways to request
        every state (the other, `state="all"`, is tested separately) —
        MCP's JSON arguments could distinguish an explicit `null` from an
        omitted one unambiguously on their own (unlike REST's HTTP query
        string, which can't and so needs the `"all"` string sentinel), but
        `None` is accepted here purely as a convenience alongside it."""
        kb = _kb(tmp_path)
        self._one_resolved_and_one_open(kb)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            state=None, token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["count"] == 2
        assert {c["state"] for c in result["contradictions"]} == {"open", "resolved"}

    def test_state_all_lists_every_state(self, tmp_path: Path) -> None:
        """`state="all"` — the same sentinel REST's `GET /contradictions`
        and GraphQL's `Query.contradictions` already document and accept —
        works identically to `state=None` here, so a caller who knows one
        sibling interface's convention isn't punished for using it on MCP
        (KI-076 review: this used to silently match zero rows instead)."""
        kb = _kb(tmp_path)
        self._one_resolved_and_one_open(kb)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            state="all", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["count"] == 2
        assert {c["state"] for c in result["contradictions"]} == {"open", "resolved"}

    def test_invalid_state_returns_validation_error(self, tmp_path: Path) -> None:
        """A near-miss value (wrong case, a plausible-sounding synonym, or
        the literal string "None" a model might emit for a null) must
        raise loudly rather than silently matching zero contradictions,
        which an agent could mistake for "no contradictions exist"
        (KI-076 review)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, entity.id)

        mcp, _ = _server(kb)
        for bad_state in ("Open", "unresolved", "None", ""):
            result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
                state=bad_state, token=kb.issue_token(HUMAN, author=ADMIN)[0]
            )
            assert "error" in result, bad_state
            assert result["code"] == "VALIDATION_ERROR", bad_state

    def test_bad_token_reports_auth_error_over_bad_state(self, tmp_path: Path) -> None:
        """`state` is validated after token resolution (matches
        ontolith.query's own as_of-validation placement) - an
        unauthenticated caller should learn "no valid token" before "bad
        argument," not get a free, pre-auth probe of which state values
        this tool accepts (KI-076 review)."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            state="not-a-real-state", token="not-a-real-token"
        )
        assert result["code"] == "AUTH_ERROR"

    def test_state_none_over_real_transport_lists_every_state(self, tmp_path: Path) -> None:
        """Every other test in this class drives the tool via a direct
        `.fn()` call, bypassing FastMCP's own pydantic argument model
        entirely. This one instead runs the real streamable-HTTP ASGI app
        end to end, confirming a JSON `null` sent over the wire really
        does reach this function as `None` — distinct from the omitted-
        argument case, which falls back to the "open" default — rather
        than being coerced to the default by pydantic somewhere in
        between (KI-076 review; mirrors
        test_header_authenticates_over_real_streamable_http_transport's
        own rationale for testing the real transport, not just the fake
        one every other test in this file uses)."""
        kb = _kb(tmp_path)
        self._one_resolved_and_one_open(kb)

        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        result = asyncio.run(
            _call_tool_over_real_transport(
                mcp, "ontolith.list_contradictions", {"state": None, "token": token}, None
            )
        )
        assert result["count"] == 2
        assert {c["state"] for c in result["contradictions"]} == {"open", "resolved"}

    def test_rationale_history_is_included(self, tmp_path: Path) -> None:
        """KI-076's whole point: reading rationale_history (KI-071/KI-075)
        without also having to perform a flag_contradiction write."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, entity.id, rationale="Sources disagree")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["contradictions"][0]["rationale_history"] == [
            {"rationale": "Sources disagree", "actor": HUMAN, "at": T0.isoformat()}
        ]

    def test_no_rationale_gives_empty_rationale_history(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, entity.id)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["contradictions"][0]["rationale_history"] == []

    def test_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            token="not-a-real-token"
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_read_only_principal_can_list(self, tmp_path: Path) -> None:
        """Ungated beyond a resolved principal - matches ontolith.query/
        .provenance's own precedent (read is the capability floor)."""
        kb = _kb(tmp_path)
        kb.create_principal("readonly@example.com", kind="human", default_capability="read")
        entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, entity.id)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            token=kb.issue_token("readonly@example.com", author=ADMIN)[0]
        )
        assert result["count"] == 1

    def test_does_not_mutate_anything(self, tmp_path: Path) -> None:
        """The whole point of KI-076: unlike flag_contradiction, listing
        must not extend membership or touch any assertion's status."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, entity.id)
        [before] = kb.contradictions()
        statuses_before = {
            a.id: a.status
            for a in kb.assertions(subject=entity.id, predicate="Person.name", status=None)
        }

        mcp, _ = _server(kb)
        mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        [after] = kb.contradictions()
        assert after.member_ids == before.member_ids
        assert after.state == before.state
        statuses_after = {
            a.id: a.status
            for a in kb.assertions(subject=entity.id, predicate="Person.name", status=None)
        }
        assert statuses_after == statuses_before

    def test_malformed_metadata_degrades_gracefully_not_a_crash(self, tmp_path: Path) -> None:
        """`metadata` is an open, schema-less blob (ADR-0041) - one
        contradiction with a malformed rationale_history entry must not
        take down the whole listing, the same failure mode this tool's
        safe_rationale_history() usage was written to prevent (KI-076
        review)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        self._flag(kb, entity.id)
        [contradiction] = kb.contradictions()
        kb.backend.update_contradiction_members(
            contradiction.id,
            contradiction.member_ids,
            metadata={"rationale_history": ["a bare string, not a dict"]},
        )

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.list_contradictions").fn(
            token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )
        assert result["count"] == 1
        assert result["contradictions"][0]["rationale_history"] == []


# ---------------------------------------------------------------------------
# ontolith.flag_contradiction
# ---------------------------------------------------------------------------


class TestFlagContradictionTool:
    def test_flag_creates_contradiction(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        # Need a second assertion — insert directly via backend
        from ontolith.core import Assertion

        a2 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada Lovelace",
            author=HUMAN,
            asserted_at=T0,
            status="active",
        )
        kb.backend.put_assertion(a2)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertions[0].id,
            assertion_id_b=a2.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert "contradiction_id" in result
        assert result["action"] == "created"
        assert set(result["member_ids"]) == {assertions[0].id, a2.id}
        assert result["raised_by"] == HUMAN

    def test_rationale_is_readable_back_in_rationale_history(self, tmp_path: Path) -> None:
        """KI-075: `rationale` (KI-071) must round-trip through this tool's
        own response, not just be recorded server-side and unreachable."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        from ontolith.core import Assertion

        a2 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada Lovelace",
            author=HUMAN,
            asserted_at=T0,
            status="active",
        )
        kb.backend.put_assertion(a2)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertions[0].id,
            assertion_id_b=a2.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            rationale="Sources disagree",
        )

        assert result["rationale_history"] == [
            {"rationale": "Sources disagree", "actor": HUMAN, "at": T0.isoformat()}
        ]

        # KI-075 review: pin that this response is read back from the
        # persisted, re-fetched Contradiction (post-write) rather than
        # synthesized from just this call's own `rationale` argument — a
        # single-call test can't distinguish the two, since both would
        # produce the same one-entry list.
        a3 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada L.",
            author=HUMAN,
            asserted_at=T0,
            status="active",
        )
        kb.backend.put_assertion(a3)
        kb.clock.advance(days=1)  # type: ignore[attr-defined]
        extended = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertions[0].id,
            assertion_id_b=a3.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            rationale="A third source also disagrees",
        )
        assert extended["action"] == "extended"
        assert extended["rationale_history"] == [
            {"rationale": "Sources disagree", "actor": HUMAN, "at": T0.isoformat()},
            {
                "rationale": "A third source also disagrees",
                "actor": HUMAN,
                "at": "2025-01-02T00:00:00+00:00",
            },
        ]

    def test_malformed_prior_metadata_degrades_gracefully_on_extend(self, tmp_path: Path) -> None:
        """`metadata` is an open, schema-less blob (ADR-0041) - a malformed
        entry left by some other writer must not crash a later extend call
        that itself supplies a perfectly good rationale (KI-076 review:
        this tool now routes through the same safe_rationale_history()
        helper as ontolith.list_contradictions, not a raw .get(), so both
        MCP tools must handle this identically)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        second = kb.assert_literal(entity.id, "Person.name", "Ava", "Text", HUMAN)
        first = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")[0]
        kb.flag_contradiction(first.id, second.id, HUMAN)
        [contradiction] = kb.contradictions()
        kb.backend.update_contradiction_members(
            contradiction.id,
            contradiction.member_ids,
            metadata={"rationale_history": ["a bare string, not a dict"]},
        )

        third = kb.assert_literal(entity.id, "Person.name", "Ada L.", "Text", HUMAN)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=first.id,
            assertion_id_b=third.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            rationale="A new, well-formed rationale",
        )

        assert result["action"] == "extended"
        assert result["rationale_history"] == [
            {"rationale": "A new, well-formed rationale", "actor": HUMAN, "at": T0.isoformat()}
        ]

    def test_flag_different_predicates_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(entity.id, "Person.born", "1815-12-10", "Date", HUMAN)

        all_a = kb.assertions(subject=entity.id, status="active")
        name_a = next(a for a in all_a if a.predicate == "Person.name")
        born_a = next(a for a in all_a if a.predicate == "Person.born")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=name_a.id,
            assertion_id_b=born_a.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )
        assert "error" in result

    def test_flag_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a="a1",
            assertion_id_b="a2",
            token="not-a-real-token",
        )
        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_flag_read_only_principal_returns_capability_error(self, tmp_path: Path) -> None:
        """Previously flag_contradiction had NO capability check at all — any
        principal, even read-only, could flag assertions out of default query
        results. This is the regression test for that HIGH finding."""
        kb = _kb(tmp_path)
        kb.create_principal(
            "readonly@example.com", kind="human", auth_method="oidc", default_capability="read"
        )
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        from ontolith.core import Assertion

        a2 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Ada Lovelace",
            author=HUMAN,
            asserted_at=T0,
            status="active",
        )
        kb.backend.put_assertion(a2)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertions[0].id,
            assertion_id_b=a2.id,
            token=kb.issue_token("readonly@example.com", author=ADMIN)[0],
        )
        assert "error" in result
        assert result["code"] == "CAPABILITY_ERROR"

    def test_flag_assertion_a_not_found_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a="nonexistent",
            assertion_id_b=assertions[0].id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )
        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_flag_assertion_b_not_found_returns_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertions[0].id,
            assertion_id_b="nonexistent",
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )
        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_flag_extends_existing_contradiction(self, tmp_path: Path) -> None:
        """A third conflicting assertion extends the open contradiction rather than
        creating a new one, and already-flagged members are not re-flagged.
        """
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        # First two proposals conflict via normal conflict routing -> open contradiction
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(entity.id, "Person.name", "Ava", "Text", HUMAN)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert len(flagged) == 2

        from ontolith.core import Assertion

        a3 = Assertion(
            id=kb.id_provider.next(),
            namespace="default",
            subject=entity.id,
            predicate="Person.name",
            value_kind="literal",
            value_type="Text",
            value="Eve",
            author=HUMAN,
            asserted_at=T0,
            status="active",
        )
        kb.backend.put_assertion(a3)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=flagged[0].id,
            assertion_id_b=a3.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert result["action"] == "extended"
        still_flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert {a.id for a in still_flagged} == {flagged[0].id, flagged[1].id, a3.id}


# ---------------------------------------------------------------------------
# ontolith.resubmit
# ---------------------------------------------------------------------------


class TestResubmitTool:
    def test_resubmit_moves_through_policy_again(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="test-model-v1")
        kb.request_changes(proposal.id, REVIEWER)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.resubmit").fn(
            proposal_id=proposal.id,
            token=kb.issue_token(AI, author=ADMIN)[0],
        )

        # AI proposals always route to require_review (ADR-0003).
        assert result["proposal"]["state"] == "require_review"
        assert result["decision"] == "RequireReview"

    def test_resubmit_unrelated_principal_returns_capability_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="test-model-v1")
        kb.request_changes(proposal.id, REVIEWER)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.resubmit").fn(
            proposal_id=proposal.id,
            token=kb.issue_token(REVIEWER, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "CAPABILITY_ERROR"

    def test_resubmit_unknown_proposal_returns_not_found(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.resubmit").fn(
            proposal_id="nonexistent-id",
            token=kb.issue_token(AI, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "NOT_FOUND"

    def test_resubmit_wrong_state_returns_validation_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="test-model-v1")

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.resubmit").fn(
            proposal_id=proposal.id,
            token=kb.issue_token(AI, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "VALIDATION_ERROR"

    def test_resubmit_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="test-model-v1")
        kb.request_changes(proposal.id, REVIEWER)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.resubmit").fn(
            proposal_id=proposal.id,
            token="not-a-real-token",
        )

        assert "error" in result
        assert result["code"] == "AUTH_ERROR"


# ---------------------------------------------------------------------------
# ontolith.retract (KI-057, ADR-0039)
# ---------------------------------------------------------------------------


class TestRetractTool:
    def test_write_capability_auto_accepts(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=assertion.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert result["proposal"]["state"] == "auto_accepted"
        assert result["decision"] == "AutoAccept"
        retracted = kb.assertions(subject=entity.id, status=None)[0]
        assert retracted.status == "retracted"

    def test_ai_requires_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=assertion.id,
            token=kb.issue_token(AI, author=ADMIN)[0],
        )

        assert result["proposal"]["state"] == "require_review"
        assert result["decision"] == "RequireReview"

    def test_acting_as_delegation(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "delegate@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="write",
            owner=HUMAN,
        )
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=assertion.id,
            token=kb.issue_token("delegate@example.com", author=ADMIN)[0],
            acting_as=HUMAN,
        )

        assert result["proposal"]["acting_as"] == HUMAN

    def test_unknown_acting_as_returns_auth_error(self, tmp_path: Path) -> None:
        """Distinct from an invalid token: the token resolves fine, but the
        requested acting_as principal doesn't exist - _resolve_delegation
        raises AuthError for this, not CapabilityError."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=assertion.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
            acting_as="nonexistent-principal",
        )

        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_retract_invalid_token_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=assertion.id,
            token="not-a-real-token",
        )

        assert "error" in result
        assert result["code"] == "AUTH_ERROR"

    def test_party_to_contradiction_returns_capability_error(self, tmp_path: Path) -> None:
        """KI-033: a party to an open contradiction can't retract the
        opposing member, even at write capability (auto-accept would
        otherwise let them unilaterally settle their own dispute)."""
        kb = _kb(tmp_path)
        kb.create_principal("wendy@example.com", kind="human", default_capability="write")
        entity = kb.create_entity("Person", author=HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", "wendy@example.com")
        opposing = next(
            a
            for a in kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
            if a.author == "wendy@example.com"
        )

        mcp, _ = _server(kb)
        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id=opposing.id,
            token=kb.issue_token(HUMAN, author=ADMIN)[0],
        )

        assert "error" in result
        assert result["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# Bearer-token transport (KI-067): Authorization header vs. `token` argument
# ---------------------------------------------------------------------------


class TestBearerTokenTransport:
    """The `token` argument still works everywhere (every test above uses
    it, unchanged) — these pin the new SSE/streamable-HTTP behavior: an
    `Authorization` header, when the transport supplies one, is preferred
    over the argument, keeping a live credential out of the calling model's
    own tool-call arguments."""

    def test_header_only_authenticates_without_token_argument(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"Bearer {token}"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"concepts": []}

    def test_header_takes_priority_over_token_argument(self, tmp_path: Path) -> None:
        """A valid header authenticates even when the `token` argument is
        garbage — the header wins, not the argument."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"Bearer {token}"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token="not-a-real-token")

        assert result == {"concepts": []}

    def test_unresolvable_header_token_does_not_fall_back_to_valid_argument(
        self, tmp_path: Path
    ) -> None:
        """The strongest fail-closed case: a well-formed header carrying a
        token that doesn't resolve to any principal must fail — even
        alongside a `token` argument that IS valid. The header winning only
        when it happens to work would defeat the point (a caller could
        smuggle a bad header past authentication by also supplying a good
        argument); the header must win, full stop, once it's well-formed."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        valid_token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request("Bearer not-a-real-token"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=valid_token)

        assert result == {"error": "Invalid or revoked token", "code": "AUTH_ERROR", "detail": {}}

    def test_no_header_falls_back_to_token_argument(self, tmp_path: Path) -> None:
        """A live HTTP request with no Authorization header at all (as
        opposed to no HTTP request/context existing) still falls back to
        the argument, same as stdio."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(None):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert result == {"concepts": []}

    def test_malformed_header_fails_closed_ignoring_token_argument(self, tmp_path: Path) -> None:
        """A header present but not a well-formed `Bearer <token>` value
        (wrong scheme here) does NOT fall back to the argument, even though
        the argument is valid — silently falling back on a malformed header
        would reopen the exposure KI-067 removes. Fails closed instead."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request("Basic dXNlcjpwYXNz"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert result == {
            "error": "Malformed Authorization header",
            "code": "AUTH_ERROR",
            "detail": {},
        }

    def test_empty_bearer_value_fails_closed(self, tmp_path: Path) -> None:
        """`Authorization: Bearer` with no value at all (not just a wrong
        scheme) also fails closed rather than falling back."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request("Bearer"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert result == {
            "error": "Malformed Authorization header",
            "code": "AUTH_ERROR",
            "detail": {},
        }

    def test_uppercase_bearer_scheme_authenticates(self, tmp_path: Path) -> None:
        """The scheme match is case-insensitive (RFC 7235) — pins the
        `.lower()` explicitly, not just implicitly via the lowercase-scheme
        tests above."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"BEARER {token}"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"concepts": []}

    def test_extra_whitespace_before_token_is_stripped(self, tmp_path: Path) -> None:
        """`Bearer  <token>` (double space, RFC 7235 permits BWS) still
        authenticates instead of silently failing closed on a value with
        leading whitespace."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"Bearer  {token}"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"concepts": []}

    def test_no_header_and_no_token_argument_returns_auth_error(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)

        with _http_request(None):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"error": "No bearer token provided", "code": "AUTH_ERROR", "detail": {}}

    def test_no_context_and_no_token_argument_returns_auth_error(self, tmp_path: Path) -> None:
        """Outside any request context at all (e.g. stdio, or a direct
        `.fn()` call as every other test in this file makes) — distinct
        from test_no_header_and_no_token_argument_returns_auth_error's live
        HTTP request with an absent header."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)

        result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"error": "No bearer token provided", "code": "AUTH_ERROR", "detail": {}}

    def test_header_authenticates_a_write_tool(self, tmp_path: Path) -> None:
        """Not just the read tools — the `author = auth_provider.resolve
        (token).id` shape (propose/retract/flag_contradiction/resubmit) is
        wired the same way."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"Bearer {token}"):
            result = mcp._tool_manager.get_tool("ontolith.propose").fn(
                subject=entity.id, predicate="Person.name", value="Ada", value_type="Text"
            )

        assert result["proposal"]["state"] == "auto_accepted"

    def test_no_token_returns_auth_error_for_every_tool(self, tmp_path: Path) -> None:
        """Each of the 9 tools gained its own `if token is None` branch
        (KI-067) — not just ontolith.schema's, exercised above."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        proposal, _ = kb.propose(
            entity.id, "Person.born", "1815-12-10", "Date", AI, model="test-model-v1"
        )
        kb.request_changes(proposal.id, REVIEWER)

        mcp, _ = _server(kb)
        calls = {
            "ontolith.get": {"entity_id": entity.id},
            "ontolith.query": {"concept": "Person"},
            "ontolith.provenance": {"assertion_id": assertion.id},
            "ontolith.list_contradictions": {},
            "ontolith.propose": {
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
            },
            "ontolith.retract": {"assertion_id": assertion.id},
            "ontolith.flag_contradiction": {
                "assertion_id_a": assertion.id,
                "assertion_id_b": assertion.id,
            },
            "ontolith.resubmit": {"proposal_id": proposal.id},
        }
        for tool_name, kwargs in calls.items():
            result = mcp._tool_manager.get_tool(tool_name).fn(**kwargs)
            assert result == {
                "error": "No bearer token provided",
                "code": "AUTH_ERROR",
                "detail": {},
            }, tool_name

    def test_header_authenticates_over_real_streamable_http_transport(self, tmp_path: Path) -> None:
        """Every test above drives `_bearer_token` through `_http_request`'s
        faked `RequestContext` — fast and deterministic, but only as good as
        the fake staying an accurate stand-in for the SDK's real wiring. This
        one instead runs the real streamable-HTTP ASGI app end to end."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        result = asyncio.run(
            _call_tool_over_real_transport(mcp, "ontolith.schema", {}, f"Bearer {token}")
        )

        assert result == {"concepts": []}

    def test_no_header_falls_back_to_argument_over_real_transport(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        result = asyncio.run(
            _call_tool_over_real_transport(mcp, "ontolith.schema", {"token": token}, None)
        )

        assert result == {"concepts": []}

    def test_malformed_header_fails_closed_over_real_transport(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        result = asyncio.run(
            _call_tool_over_real_transport(
                mcp, "ontolith.schema", {"token": token}, "Basic dXNlcjpwYXNz"
            )
        )

        assert result == {
            "error": "Malformed Authorization header",
            "code": "AUTH_ERROR",
            "detail": {},
        }


# ---------------------------------------------------------------------------
# require_header_token (KI-073)
# ---------------------------------------------------------------------------


class TestRequireHeaderToken:
    """`create_mcp_server(..., require_header_token=True)`: an opt-in that
    disables the `token` argument fallback entirely, so an HTTP deployment
    can require the header channel outright instead of merely preferring
    it (KI-067). Off by default — every test above in TestBearerTokenTransport
    exercises the default (`require_header_token=False`) behavior unchanged."""

    def test_default_is_false_existing_behavior_unaffected(self, tmp_path: Path) -> None:
        """create_mcp_server's own default, not passing the kwarg at all -
        pins that the flag is genuinely opt-in, not just False by
        convention in this file's own _server() helper."""
        kb = _kb(tmp_path)
        auth_provider = TokenAuthProvider(kb.backend)
        mcp = create_mcp_server(kb, auth_provider)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(None):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert result == {"concepts": []}

    def test_valid_header_still_authenticates(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb, require_header_token=True)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"Bearer {token}"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"concepts": []}

    def test_valid_header_still_authenticates_alongside_a_token_argument(
        self, tmp_path: Path
    ) -> None:
        """The migration case the flag exists to support: a client that
        hasn't been updated yet keeps sending `token` out of habit while a
        header-injecting proxy now also supplies the header. `token` being
        present must not itself trigger the "argument fallback disabled"
        rejection - only an *absent* header does that. Mirrors
        TestBearerTokenTransport.test_header_takes_priority_over_token_argument's
        own flag-off case (review finding: this flag-on analogue was the one
        gap two independent mutations both slipped through)."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb, require_header_token=True)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(f"Bearer {token}"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token="not-a-real-token")

        assert result == {"concepts": []}

    def test_no_header_rejects_even_a_valid_token_argument(self, tmp_path: Path) -> None:
        """The whole point of the flag: a live HTTP request with no header
        must fail, even though the token argument alone would have
        authenticated fine under the default (False) behavior - proven by
        TestBearerTokenTransport.test_no_header_falls_back_to_token_argument
        using the identical setup with the flag left off."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb, require_header_token=True)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(None):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert result == {"error": "No bearer token provided", "code": "AUTH_ERROR", "detail": {}}

    def test_no_request_context_at_all_also_rejects_token_argument(self, tmp_path: Path) -> None:
        """stdio (or any call outside a live HTTP request/context) has no
        header channel to satisfy the requirement at all - it becomes
        unusable under this flag, the documented tradeoff, not a bug. A
        direct .fn() call with no _http_request context simulates this."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb, require_header_token=True)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert result == {"error": "No bearer token provided", "code": "AUTH_ERROR", "detail": {}}

    def test_malformed_header_still_fails_closed(self, tmp_path: Path) -> None:
        """Unchanged from the default behavior - a present-but-malformed
        header never falls back to the argument regardless of this flag."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb, require_header_token=True)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request("Basic dXNlcjpwYXNz"):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert result == {
            "error": "Malformed Authorization header",
            "code": "AUTH_ERROR",
            "detail": {},
        }

    def test_no_header_and_no_token_argument_still_rejects(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb, require_header_token=True)

        with _http_request(None):
            result = mcp._tool_manager.get_tool("ontolith.schema").fn()

        assert result == {"error": "No bearer token provided", "code": "AUTH_ERROR", "detail": {}}

    def test_blocks_argument_fallback_over_real_transport(self, tmp_path: Path) -> None:
        """Every test above drives `_bearer_token` through `_http_request`'s
        faked `RequestContext` - this one instead runs the real
        streamable-HTTP ASGI app end to end, mirroring
        test_header_authenticates_over_real_streamable_http_transport's own
        rationale for not trusting the fake context alone (KI-067 review
        found the fake had drifted from the SDK's real wiring once)."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb, require_header_token=True)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        result = asyncio.run(
            _call_tool_over_real_transport(mcp, "ontolith.schema", {"token": token}, None)
        )
        assert result == {"error": "No bearer token provided", "code": "AUTH_ERROR", "detail": {}}

    def test_valid_header_authenticates_over_real_transport(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb, require_header_token=True)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        result = asyncio.run(
            _call_tool_over_real_transport(mcp, "ontolith.schema", {}, f"Bearer {token}")
        )
        assert result == {"concepts": []}

    def test_flag_is_per_server_instance_not_shared_process_wide(self, tmp_path: Path) -> None:
        """require_header_token is a closure-captured constructor argument,
        not module or process state - two servers built from the same kb
        must each honor their own setting independently. Review finding:
        a refactor hoisting the flag onto shared state (e.g. a module-level
        default) would have shipped green against every other test in this
        class, since none of them build more than one server."""
        kb = _kb(tmp_path)
        hardened, _ = _server(kb, require_header_token=True)
        lenient, _ = _server(kb, require_header_token=False)
        token = kb.issue_token(HUMAN, author=ADMIN)[0]

        with _http_request(None):
            hardened_result = hardened._tool_manager.get_tool("ontolith.schema").fn(token=token)
            lenient_result = lenient._tool_manager.get_tool("ontolith.schema").fn(token=token)

        assert hardened_result == {
            "error": "No bearer token provided",
            "code": "AUTH_ERROR",
            "detail": {},
        }
        assert lenient_result == {"concepts": []}


# ---------------------------------------------------------------------------
# Blanket OntolithError handling (KI-074)
# ---------------------------------------------------------------------------


class _FailingSchemaBackend:
    """Wraps a real StorageBackend, making get_schema raise a StorageError
    whose message interpolates fake internal detail - injects a failure
    from a taxonomy member no tool previously caught by name (StorageError
    was never in any per-tool except clause pre-KI-074), to prove the new
    blanket handler actually reaches it. Forwards every other call to the
    wrapped backend via __getattr__."""

    def __init__(self, real: object) -> None:
        self._real = real

    def get_schema(self, namespace: str) -> object:
        from ontolith.core.errors import StorageError

        raise StorageError("sqlite3.OperationalError: database is locked (fd=7, pid=12345)")

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class TestBlanketErrorHandling:
    """KI-074: every tool now catches the full OntolithError taxonomy via
    one shared _error_response() helper, not just the 4 types each tool
    used to hand-catch — and StorageError/PluginError messages are
    redacted the same way REST/GraphQL's own blanket handlers already do."""

    def test_storage_error_reaches_the_blanket_handler_redacted(self, tmp_path: Path) -> None:
        from ontolith.store.sqlite import SQLiteBackend

        real = SQLiteBackend(tmp_path / "test.db")
        try:
            sink = RecordingObservabilitySink()
            kb = Ontology(
                _FailingSchemaBackend(real),  # type: ignore[arg-type]
                clock=FixedClock(T0),
                id_provider=FixedIdProvider([f"id-{i}" for i in range(10)]),
                observability=sink,
            )
            kb.create_principal(HUMAN, kind="human", default_capability="admin")
            mcp, _ = _server(kb)

            result = mcp._tool_manager.get_tool("ontolith.schema").fn(
                token=kb.issue_token(HUMAN, author=HUMAN)[0]
            )

            assert result["code"] == "STORAGE_ERROR"
            assert result["error"] == "An internal error occurred"
            assert "database is locked" not in result["error"]
            assert result["detail"] == {}

            # SPEC §18/ADR-0044: the real message still reaches kb's
            # observability sink server-side — _error_response is now a
            # closure over kb inside create_mcp_server, not a module-level
            # function logging through a fixed module logger.
            [(level, message, fields)] = sink.logs
            assert level == logging.ERROR
            assert "database is locked" in message
            assert fields["code"] == "STORAGE_ERROR"
        finally:
            real.close()

    def test_capability_error_still_reaches_response_via_blanket_handler(
        self, tmp_path: Path
    ) -> None:
        """CapabilityError was one of the 4 types each tool already caught
        by name pre-KI-074 - pins that consolidating every except clause
        into one `except OntolithError` didn't silently drop coverage for
        the types that already worked."""
        kb = _kb(tmp_path)
        kb.create_principal("readonly@example.com", kind="human", default_capability="read")
        entity = kb.create_entity("Person", author=HUMAN)
        assertion_a = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        assertion_b = kb.assert_literal(entity.id, "Person.name", "Ava", "Text", HUMAN)
        mcp, _ = _server(kb)

        result = mcp._tool_manager.get_tool("ontolith.flag_contradiction").fn(
            assertion_id_a=assertion_a.id,
            assertion_id_b=assertion_b.id,
            token=kb.issue_token("readonly@example.com", author=ADMIN)[0],
        )

        assert result["code"] == "CAPABILITY_ERROR"
        assert "detail" in result

    def test_non_redacted_error_message_passes_through_unchanged(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)

        result = mcp._tool_manager.get_tool("ontolith.get").fn(
            entity_id="does-not-exist", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["code"] == "NOT_FOUND"
        assert result["error"] == "Entity 'does-not-exist' not found"
        assert result["detail"] == {}

    def test_retract_unknown_assertion_is_not_found_not_redacted(self, tmp_path: Path) -> None:
        """Regression (KI-074 review): before the Ontology.retract() fix,
        an unknown assertion_id surfaced as a StorageError, which
        _REDACT_MESSAGE_FOR then hid behind "An internal error occurred"
        — a client-supplied bad ID must stay actionable, not become
        indistinguishable from a genuine storage fault."""
        kb = _kb(tmp_path)
        mcp, _ = _server(kb)

        result = mcp._tool_manager.get_tool("ontolith.retract").fn(
            assertion_id="does-not-exist", token=kb.issue_token(HUMAN, author=ADMIN)[0]
        )

        assert result["code"] == "NOT_FOUND"
        assert result["error"] == "Assertion not found: does-not-exist"
        assert result["detail"] == {}
