"""Unit tests for the REST interface (SPEC §14.3, ADR-0021).

Mirrors test_mcp_server.py's fixtures and structure: verifies each route's
response shape and that every route — reads included — requires a bearer
token resolved via AuthProvider (never a caller-asserted principal ID).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ontolith import Ontology
from ontolith.core import Assertion, FixedClock, FixedIdProvider
from ontolith.core.errors import StorageError
from ontolith.core.observability import RecordingObservabilitySink
from ontolith.identity.token_auth import TokenAuthProvider
from ontolith.interfaces.rest import create_rest_app
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


def _client(kb: Ontology) -> tuple[TestClient, TokenAuthProvider]:
    auth_provider = TokenAuthProvider(kb.backend)
    return TestClient(create_rest_app(kb, auth_provider)), auth_provider


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# create_rest_app(docs_url=..., redoc_url=..., openapi_url=...)
# ---------------------------------------------------------------------------


class TestDocsUrls:
    def test_docs_enabled_by_default(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200

    def test_docs_can_be_disabled(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        auth_provider = TokenAuthProvider(kb.backend)
        app = create_rest_app(kb, auth_provider, docs_url=None, redoc_url=None, openapi_url=None)
        client = TestClient(app)
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------------------
# GET /schema
# ---------------------------------------------------------------------------


class TestSchemaRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/schema")
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_ERROR"

    def test_rejects_invalid_token(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/schema", headers=_auth("not-a-real-token"))
        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_ERROR"

    def test_returns_empty_when_no_schema_stored(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/schema", headers=_auth(token))
        assert response.status_code == 200
        assert response.json() == {"namespace": None, "version": None, "concepts": []}

    def test_returns_concepts_and_properties(self, tmp_path: Path) -> None:
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

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/schema", headers=_auth(token))

        body = response.json()
        assert body["namespace"] == "default"
        assert body["version"] == 1
        assert len(body["concepts"]) == 1
        person = body["concepts"][0]
        assert person["name"] == "Person"
        props_by_name = {p["name"]: p for p in person["properties"]}
        assert props_by_name["name"]["type"] == "Text"
        assert props_by_name["name"]["required"] is True
        assert props_by_name["name"]["cardinality"] == "single"
        assert props_by_name["employer"]["temporality"] == "time_varying"
        assert props_by_name["nicknames"]["cardinality"] == "many"
        assert person["relations"] == []

    def test_returns_relations(self, tmp_path: Path) -> None:
        """KI-029: relations were previously omitted from the response
        model entirely - a REST client couldn't see that Person.employer
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

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/schema", headers=_auth(token))

        body = response.json()
        concepts_by_name = {c["name"]: c for c in body["concepts"]}
        person = concepts_by_name["Person"]
        assert len(person["relations"]) == 1
        employer = person["relations"][0]
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
        assert employer["inverse"] == "employees"

    def test_respects_namespace_argument(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/schema", params={"namespace": "other"}, headers=_auth(token))
        assert response.status_code == 200
        assert response.json() == {"namespace": None, "version": None, "concepts": []}


# ---------------------------------------------------------------------------
# POST /entities (KI-082)
# ---------------------------------------------------------------------------


class TestCreateEntityRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.post("/entities", json={"concept": "Person"})
        assert response.status_code == 401

    def test_create(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/entities",
            json={"concept": "Person", "natural_key": "ada"},
            headers=_auth(token),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["concept"] == "Person"
        assert body["natural_key"] == "ada"
        assert body["created_by"] == HUMAN

    def test_created_entity_is_retrievable(self, tmp_path: Path) -> None:
        """Round-trips through GET /entities/{id} — not just a shape check
        on the POST response, but confirms the entity is actually
        persisted where the rest of the API expects to find it."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        created = client.post("/entities", json={"concept": "Person"}, headers=_auth(token)).json()

        response = client.get(f"/entities/{created['id']}", headers=_auth(token))
        assert response.status_code == 200
        assert response.json()["entity"]["id"] == created["id"]

    def test_propose_capability_succeeds(self, tmp_path: Path) -> None:
        """Propose-tier, not write-tier — AI's own default capability suffices."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(AI, author=ADMIN)
        response = client.post("/entities", json={"concept": "Person"}, headers=_auth(token))
        assert response.status_code == 201

    def test_read_capability_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "readonly@example.com", kind="human", auth_method="oidc", default_capability="read"
        )
        client, _ = _client(kb)
        token, _ = kb.issue_token("readonly@example.com", author=ADMIN)
        response = client.post("/entities", json={"concept": "Person"}, headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# GET /entities/{entity_id}
# ---------------------------------------------------------------------------


class TestEntityRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        response = client.get(f"/entities/{entity.id}")
        assert response.status_code == 401

    def test_known_entity(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/entities/{entity.id}", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["entity"]["id"] == entity.id
        assert body["entity"]["concept"] == "Person"
        assert len(body["assertions"]) == 1
        assert body["assertions"][0]["value"] == "Ada"

    def test_unknown_entity_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/entities/does-not-exist", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_excludes_non_active_assertions(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        kb.retract(active[0].id, HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/entities/{entity.id}", headers=_auth(token))
        assert response.json()["assertions"] == []


# ---------------------------------------------------------------------------
# POST /query
# ---------------------------------------------------------------------------


class TestQueryRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.post("/query", json={"concept": "Person"})
        assert response.status_code == 401

    def test_all_of_concept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Person", author=HUMAN)
        kb.create_entity("Organization", author=HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post("/query", json={"concept": "Person"}, headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 2
        assert len(body["entities"]) == 2

    def test_with_filter(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        e1 = kb.create_entity("Person", author=HUMAN)
        e2 = kb.create_entity("Person", author=HUMAN)
        kb.propose(e1.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(e2.id, "Person.name", "Grace", "Text", HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/query",
            json={"concept": "Person", "filters": {"name": "Ada"}},
            headers=_auth(token),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["entities"][0]["id"] == e1.id

    def test_limit_is_applied(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        for _ in range(3):
            kb.create_entity("Person", author=HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/query", json={"concept": "Person", "limit": 2}, headers=_auth(token)
        )

        assert response.status_code == 200
        assert response.json()["count"] == 2

    def test_empty_concept_returns_empty(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post("/query", json={"concept": "Organization"}, headers=_auth(token))
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 0
        assert body["entities"] == []

    def test_dunder_filter_key_returns_400(self, tmp_path: Path) -> None:
        """A relation-traversal-shaped key fails loudly instead of returning 200 + empty (KI-030)."""
        kb = _kb(tmp_path)
        kb.create_entity("Person", author=HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/query",
            json={"concept": "Person", "filters": {"employer__name": "Acme Corp"}},
            headers=_auth(token),
        )

        assert response.status_code == 400

    def test_semantic_ranks_by_similarity(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada Lovelace", "Text", HUMAN)
        kb.reindex()

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/query",
            json={"concept": "Person", "semantic": "Ada Lovelace"},
            headers=_auth(token),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["entities"][0]["id"] == entity.id

    def test_min_confidence_filters_entities(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        confident = kb.create_entity("Person", author=HUMAN)
        unsure = kb.create_entity("Person", author=HUMAN)
        kb.propose(confident.id, "Person.name", "Ada", "Text", HUMAN, confidence=0.9)
        kb.propose(unsure.id, "Person.name", "Bob", "Text", HUMAN, confidence=0.1)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/query",
            json={"concept": "Person", "min_confidence": 0.5},
            headers=_auth(token),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["entities"][0]["id"] == confident.id

    def test_trust_at_least_filters_entities(self, tmp_path: Path) -> None:
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

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/query",
            json={"concept": "Person", "trust_at_least": 5},
            headers=_auth(token),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["entities"][0]["id"] == trusted_entity.id

    def test_include_flagged_matches_a_flagged_assertion(self, tmp_path: Path) -> None:
        """KI-094: the opt-in is forwarded to QueryBuilder, not silently dropped."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.assert_literal(entity.id, "Person.name", "Ava", "Text", HUMAN)  # -> flagged
        assert kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        default = client.post(
            "/query", json={"concept": "Person", "filters": {"name": "Ada"}}, headers=_auth(token)
        )
        assert default.json()["count"] == 0

        response = client.post(
            "/query",
            json={"concept": "Person", "filters": {"name": "Ada"}, "include_flagged": True},
            headers=_auth(token),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["entities"][0]["id"] == entity.id

    def test_include_history_matches_a_retracted_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.retract(assertion.id, HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        default = client.post(
            "/query",
            json={"concept": "Person", "filters": {"name": "Ada"}},
            headers=_auth(token),
        )
        assert default.json()["count"] == 0

        response = client.post(
            "/query",
            json={"concept": "Person", "filters": {"name": "Ada"}, "include_history": True},
            headers=_auth(token),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["entities"][0]["id"] == entity.id


# ---------------------------------------------------------------------------
# GET /provenance/{assertion_id}
# ---------------------------------------------------------------------------


class TestProvenanceRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        client, _ = _client(kb)
        response = client.get(f"/provenance/{assertion.id}")
        assert response.status_code == 401

    def test_known_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN, confidence=0.95, source="wiki")
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/provenance/{assertions[0].id}", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == assertions[0].id
        assert body["author"] == HUMAN
        assert body["confidence"] == 0.95
        assert body["source"] == "wiki"
        assert body["subject"] == entity.id

    def test_surfaces_review_events_after_accept(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", AI, model="claude-sonnet-4"
        )
        kb.accept_proposal(proposal.id, REVIEWER)
        assertions = kb.assertions(subject=entity.id, predicate="Person.name", status="active")

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/provenance/{assertions[0].id}", headers=_auth(token))

        body = response.json()
        assert len(body["review_events"]) == 1
        assert body["review_events"][0]["type"] == "accept"
        assert body["review_events"][0]["actor"] == REVIEWER

    def test_review_events_empty_for_direct_write(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/provenance/{assertion.id}", headers=_auth(token))

        body = response.json()
        assert body["proposal_id"] is None
        assert body["review_events"] == []

    def test_unknown_assertion_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/provenance/nonexistent", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_reachable_for_retracted_assertion(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        kb.retract(active[0].id, HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/provenance/{active[0].id}", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == active[0].id
        assert body["status"] == "retracted"

    def test_superseded_ids_include_all_concurrent_predecessors(self, tmp_path: Path) -> None:
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

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get(f"/provenance/{active[0].id}", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["supersedes"] in {a1.id, a2.id}
        assert set(body["superseded_ids"]) == {a1.id, a2.id}


# ---------------------------------------------------------------------------
# POST /proposals
# ---------------------------------------------------------------------------


class TestCreateProposalRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        response = client.post(
            "/proposals",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
            },
        )
        assert response.status_code == 401

    def test_human_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/proposals",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
            },
            headers=_auth(token),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["proposal"]["state"] == "auto_accepted"
        assert body["decision"] == "AutoAccept"

    def test_ai_without_model_returns_400(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(AI, author=ADMIN)
        response = client.post(
            "/proposals",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
            },
            headers=_auth(token),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"

    def test_ai_requires_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(AI, author=ADMIN)
        response = client.post(
            "/proposals",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
                "model": "test-model-v1",
            },
            headers=_auth(token),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["proposal"]["state"] == "require_review"
        assert body["decision"] == "RequireReview"

    def test_both_value_and_target_returns_400(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        other = kb.create_entity("Person", author=HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/proposals",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
                "target": other.id,
            },
            headers=_auth(token),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"

    def test_neither_value_nor_target_returns_400(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/proposals",
            json={"subject": entity.id, "predicate": "Person.name"},
            headers=_auth(token),
        )

        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"

    def test_ref_target_auto_accepted(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        person = kb.create_entity("Person", author=HUMAN)
        org = kb.create_entity("Organization", author=HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/proposals",
            json={"subject": person.id, "predicate": "Person.employer", "target": org.id},
            headers=_auth(token),
        )

        assert response.status_code == 201
        assert response.json()["proposal"]["state"] == "auto_accepted"

    def test_supersedes_forwarded_to_propose(self, tmp_path: Path) -> None:
        """KI-099: `supersedes` (ADR-0050/KI-080) reaches `Ontology.propose`
        through `POST /proposals`."""
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

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/proposals",
            json={
                "subject": entity.id,
                "predicate": "Person.title",
                "value": "SVP Sales",
                "value_type": "Text",
                "supersedes": sales.id,
            },
            headers=_auth(token),
        )

        assert response.status_code == 201
        assert response.json()["proposal"]["state"] == "auto_accepted"
        active = kb.assertions(subject=entity.id, predicate="Person.title", status="active")
        assert {a.value for a in active} == {"SVP Sales"}

    def test_supersedes_forwarded_to_propose_ref(self, tmp_path: Path) -> None:
        """KI-099: `supersedes` reaches `Ontology.propose_ref` through
        `POST /proposals`'s `target`-shaped branch, not just its
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

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/proposals",
            json={
                "subject": entity.id,
                "predicate": "Person.colleague",
                "target": colleague_b.id,
                "supersedes": rel_a.id,
            },
            headers=_auth(token),
        )

        assert response.status_code == 201
        assert response.json()["proposal"]["state"] == "auto_accepted"
        active = kb.assertions(subject=entity.id, predicate="Person.colleague", status="active")
        assert {a.value for a in active} == {colleague_b.id}

    def test_supersedes_rejected_outside_many_time_varying(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        existing = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/proposals",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ava",
                "value_type": "Text",
                "supersedes": existing.id,
            },
            headers=_auth(token),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# GET /proposals
# ---------------------------------------------------------------------------


class TestListProposalsRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/proposals")
        assert response.status_code == 401

    def test_defaults_to_require_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)  # auto-accepted
        kb.propose(
            entity.id, "Person.name", "Grace", "Text", AI, model="test-model"
        )  # require_review

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/proposals", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["state"] == "require_review"
        assert body[0]["author"] == AI

    def test_pending_merges_changes_requested(self, tmp_path: Path) -> None:
        """KI-027: request_changes() moves a proposal out of require_review
        with no query-level way to see it alongside other still-open
        proposals. state=pending is an explicit, documented alias that
        merges both (deliberately not the default - see
        Ontology.proposals's docstring)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Grace", "Text", AI, model="test-model")
        kb.request_changes(proposal.id, REVIEWER)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/proposals", params={"state": "pending"}, headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["state"] == "changes_requested"

    def test_state_filter(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/proposals", params={"state": "auto_accepted"}, headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["state"] == "auto_accepted"

    def test_all_states_via_sentinel(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
        kb.propose(entity.id, "Person.name", "Grace", "Text", AI, model="test-model")

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/proposals", params={"state": "all"}, headers=_auth(token))

        assert response.status_code == 200
        assert len(response.json()) == 2

    def test_no_matches_returns_empty_list(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/proposals", headers=_auth(token))
        assert response.status_code == 200
        assert response.json() == []

    def test_unrecognized_state_returns_validation_error(self, tmp_path: Path) -> None:
        """KI-077: an unrecognized `state` value previously reached
        kb.proposals()'s own WHERE state = ? unfiltered and silently
        matched zero rows - indistinguishable from "no proposals in that
        state." Must raise instead."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        for bad_state in ("Auto_accepted", "pendng", "All", "", "None", "open"):
            response = client.get("/proposals", params={"state": bad_state}, headers=_auth(token))
            assert response.status_code == 400, bad_state
            assert response.json()["code"] == "VALIDATION_ERROR", bad_state


# ---------------------------------------------------------------------------
# OntolithError -> HTTP status mapping (SPEC §16)
# ---------------------------------------------------------------------------


class TestErrorMapping:
    """One assertion per error type actually reachable through this slice's
    routes — guards against the status-code table in ADR-0021 silently
    drifting as routes are added or changed."""

    def test_auth_error_maps_to_401(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/schema", headers=_auth("garbage"))
        assert response.status_code == 401
        body = response.json()
        assert body["code"] == "AUTH_ERROR"
        assert "message" in body
        assert "detail" in body

    def test_not_found_error_maps_to_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/entities/nope", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_validation_error_maps_to_400(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/proposals",
            json={"subject": entity.id, "predicate": "Person.name"},
            headers=_auth(token),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"

    def test_capability_error_maps_to_403(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "readonly@example.com",
            kind="human",
            auth_method="oidc",
            default_capability="read",
        )
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token("readonly@example.com", author=ADMIN)
        response = client.post(
            "/proposals",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
            },
            headers=_auth(token),
        )
        # ThresholdPolicy resolves a read-capability author to a persisted
        # Reject decision (SPEC §9.1's modeled state, not an exception) —
        # this asserts the response is a normal 201 with state="rejected",
        # not that CapabilityError fires here. See KI-015/known-issues.md
        # for why this is deliberate, not a gap.
        assert response.status_code == 201
        assert response.json()["proposal"]["state"] == "rejected"

    def test_request_validation_error_maps_to_400_with_ontolith_envelope(
        self, tmp_path: Path
    ) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        # Missing required "concept" field — a FastAPI/Pydantic-level
        # RequestValidationError, not an OntolithError raised by a route
        # body. Must still come back in the SPEC §16 envelope.
        response = client.post("/query", json={}, headers=_auth(token))
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert "message" in body
        assert "detail" in body

    def test_storage_error_redacts_internal_detail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kb = _kb(tmp_path)
        sink = RecordingObservabilitySink()
        kb.observability = sink
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        def _raise_storage_error(*args: object, **kwargs: object) -> None:
            raise StorageError("sqlite3.OperationalError: table assertion has no column baz")

        monkeypatch.setattr(kb.backend, "get_schema", _raise_storage_error)

        response = client.get("/schema", headers=_auth(token))

        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "STORAGE_ERROR"
        assert body["message"] == "An internal error occurred"
        assert "sqlite3" not in body["message"]
        assert "baz" not in str(body)

        # SPEC §18/ADR-0044: the real (unredacted) message still reaches
        # kb's observability sink server-side, same as it always reached
        # this module's own logger before M4's observability rewiring.
        [(level, message, fields)] = sink.logs
        assert level == logging.ERROR
        assert "sqlite3" in message
        assert "baz" in message
        assert fields["code"] == "STORAGE_ERROR"


# ---------------------------------------------------------------------------
# POST /assertions
# ---------------------------------------------------------------------------


class TestWriteAssertionRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        response = client.post(
            "/assertions",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
            },
        )
        assert response.status_code == 401

    def test_literal_write(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN has write capability
        response = client.post(
            "/assertions",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
                "confidence": 0.9,
            },
            headers=_auth(token),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["subject"] == entity.id
        assert body["value"] == "Ada"
        assert body["status"] == "active"
        assert body["author"] == HUMAN
        assert body["confidence"] == 0.9

    def test_ref_write(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        person = kb.create_entity("Person", author=HUMAN)
        org = kb.create_entity("Organization", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/assertions",
            json={
                "subject": person.id,
                "predicate": "Person.employer",
                "target": org.id,
                "rationale": "LinkedIn profile lists this employer",
            },
            headers=_auth(token),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["value"] == org.id
        assert body["rationale"] == "LinkedIn profile lists this employer"

    def test_both_value_and_target_returns_400(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        person = kb.create_entity("Person", author=HUMAN)
        org = kb.create_entity("Organization", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/assertions",
            json={
                "subject": person.id,
                "predicate": "Person.employer",
                "value": "Acme",
                "value_type": "Text",
                "target": org.id,
            },
            headers=_auth(token),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"

    def test_supersedes_forwarded_to_assert_literal(self, tmp_path: Path) -> None:
        """KI-099: `supersedes` (ADR-0050/KI-080) reaches `Ontology.assert_literal`
        through `POST /assertions`, replacing exactly the named prior
        assertion on a cardinality="many" time_varying property, leaving a
        second concurrent value untouched."""
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
        kb.assert_literal(entity.id, "Person.title", "VP Marketing", "Text", HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/assertions",
            json={
                "subject": entity.id,
                "predicate": "Person.title",
                "value": "SVP Sales",
                "value_type": "Text",
                "supersedes": sales.id,
            },
            headers=_auth(token),
        )

        assert response.status_code == 201
        assert response.json()["supersedes"] == sales.id
        active = kb.assertions(subject=entity.id, predicate="Person.title", status="active")
        assert {a.value for a in active} == {"SVP Sales", "VP Marketing"}

    def test_supersedes_forwarded_to_assert_ref(self, tmp_path: Path) -> None:
        """KI-099: `supersedes` reaches `Ontology.assert_ref` through
        `POST /assertions`'s `target`-shaped branch, not just its
        literal-value one, leaving a second concurrent target untouched."""
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
        colleague_c = kb.create_entity("Person", author=HUMAN)
        rel_a = kb.assert_ref(entity.id, "Person.colleague", colleague_a.id, HUMAN)
        kb.assert_ref(entity.id, "Person.colleague", colleague_b.id, HUMAN)

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/assertions",
            json={
                "subject": entity.id,
                "predicate": "Person.colleague",
                "target": colleague_c.id,
                "supersedes": rel_a.id,
            },
            headers=_auth(token),
        )

        assert response.status_code == 201
        assert response.json()["supersedes"] == rel_a.id
        active = kb.assertions(subject=entity.id, predicate="Person.colleague", status="active")
        assert {a.value for a in active} == {colleague_b.id, colleague_c.id}

    def test_supersedes_rejected_outside_many_time_varying(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        existing = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/assertions",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ava",
                "value_type": "Text",
                "supersedes": existing.id,
            },
            headers=_auth(token),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"

    def test_neither_value_nor_target_returns_400(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/assertions",
            json={"subject": entity.id, "predicate": "Person.name"},
            headers=_auth(token),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"

    def test_ai_principal_forbidden(self, tmp_path: Path) -> None:
        """AI principals are categorically barred from direct writes (ADR-0003)."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(AI, author=ADMIN)
        response = client.post(
            "/assertions",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
            },
            headers=_auth(token),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"

    def test_read_capability_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "readonly@example.com", kind="human", auth_method="oidc", default_capability="read"
        )
        entity = kb.create_entity("Person", author=HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token("readonly@example.com", author=ADMIN)
        response = client.post(
            "/assertions",
            json={
                "subject": entity.id,
                "predicate": "Person.name",
                "value": "Ada",
                "value_type": "Text",
            },
            headers=_auth(token),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# POST /assertions/{assertion_id}/retract
# ---------------------------------------------------------------------------


class TestRetractAssertionRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        client, _ = _client(kb)
        response = client.post(f"/assertions/{assertion.id}/retract")
        assert response.status_code == 401

    def test_write_capability_auto_accepts(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        response = client.post(f"/assertions/{assertion.id}/retract", headers=_auth(token))

        assert response.status_code == 201
        body = response.json()
        assert body["proposal"]["state"] == "auto_accepted"
        assert body["decision"] == "AutoAccept"
        retracted = kb.assertions(subject=entity.id, status=None)[0]
        assert retracted.status == "retracted"

    def test_ai_propose_requires_review(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(AI, author=ADMIN)

        response = client.post(f"/assertions/{assertion.id}/retract", headers=_auth(token))

        assert response.status_code == 201
        body = response.json()
        assert body["proposal"]["state"] == "require_review"
        assert body["decision"] == "RequireReview"

    def test_read_capability_rejected(self, tmp_path: Path) -> None:
        # ThresholdPolicy resolves a read-capability author to a persisted
        # Reject decision (SPEC §9.1's modeled state, not an exception) —
        # same as POST /proposals's own read-capability case above.
        kb = _kb(tmp_path)
        kb.create_principal(
            "readonly@example.com", kind="human", auth_method="oidc", default_capability="read"
        )
        entity = kb.create_entity("Person", author=HUMAN)
        assertion = kb.assert_literal(entity.id, "Person.name", "Ada", "Text", HUMAN)
        client, _ = _client(kb)
        token, _ = kb.issue_token("readonly@example.com", author=ADMIN)

        response = client.post(f"/assertions/{assertion.id}/retract", headers=_auth(token))

        assert response.status_code == 201
        assert response.json()["proposal"]["state"] == "rejected"

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
        client, _ = _client(kb)
        token, _ = kb.issue_token("delegate@example.com", author=ADMIN)

        response = client.post(
            f"/assertions/{assertion.id}/retract?acting_as={HUMAN}", headers=_auth(token)
        )

        assert response.status_code == 201
        assert response.json()["proposal"]["acting_as"] == HUMAN


# ---------------------------------------------------------------------------
# POST /proposals/{proposal_id}/accept
# ---------------------------------------------------------------------------


class TestAcceptProposalRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        response = client.post(f"/proposals/{proposal.id}/accept")
        assert response.status_code == 401

    def test_reviewer_accepts(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post(f"/proposals/{proposal.id}/accept", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "accepted"
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert len(active) == 1

    def test_not_found_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post("/proposals/nonexistent/accept", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_self_review_forbidden(self, tmp_path: Path) -> None:
        """A review-capable delegate can't accept a proposal delegated to
        them (ADR-0003 self-review guard). Uses an AI author + acting_as
        rather than a review-capable human author directly, since
        ThresholdPolicy auto-accepts any human/service with review
        capability — an AI author is the only way to deterministically
        land in require_review while proposal.acting_as == the reviewer.
        """
        kb = _kb(tmp_path)
        kb.create_principal(
            "helper-bot",
            kind="ai",
            owner=REVIEWER,
            auth_method="workload",
            default_capability="propose",
        )
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(
            entity.id, "Person.name", "Ada", "Text", "helper-bot", acting_as=REVIEWER, model="m1"
        )
        assert proposal.state == "require_review"

        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post(f"/proposals/{proposal.id}/accept", headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"

    def test_reviewer_lacking_capability_forbidden(self, tmp_path: Path) -> None:
        """A principal without review/admin capability cannot accept, even
        with write capability (HUMAN here) — the accept-side capability
        gate, distinct from the self-review guard above."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN has write, not review
        response = client.post(f"/proposals/{proposal.id}/accept", headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# POST /proposals/{proposal_id}/reject
# ---------------------------------------------------------------------------


class TestRejectProposalRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        response = client.post(f"/proposals/{proposal.id}/reject", json={})
        assert response.status_code == 401

    def test_reviewer_rejects(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post(
            f"/proposals/{proposal.id}/reject",
            json={"reason": "insufficient source"},
            headers=_auth(token),
        )

        assert response.status_code == 200
        assert response.json()["state"] == "rejected"
        active = kb.assertions(subject=entity.id, predicate="Person.name", status="active")
        assert active == []

    def test_reject_without_body_defaults_to_empty_reason(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post(f"/proposals/{proposal.id}/reject", json={}, headers=_auth(token))
        assert response.status_code == 200
        assert response.json()["state"] == "rejected"

    def test_not_found_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post("/proposals/nonexistent/reject", json={}, headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_reviewer_lacking_capability_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN has write, not review
        response = client.post(f"/proposals/{proposal.id}/reject", json={}, headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# POST /proposals/{proposal_id}/review
# ---------------------------------------------------------------------------


class TestReviewProposalRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        response = client.post(f"/proposals/{proposal.id}/review", json={})
        assert response.status_code == 401

    def test_reviewer_requests_changes(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post(
            f"/proposals/{proposal.id}/review",
            json={"reason": "needs a source"},
            headers=_auth(token),
        )

        assert response.status_code == 200
        assert response.json()["state"] == "changes_requested"
        active = kb.assertions(subject=entity.id, predicate="Person.name")
        assert active == []

    def test_review_without_body_defaults_to_empty_reason(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post(f"/proposals/{proposal.id}/review", json={}, headers=_auth(token))
        assert response.status_code == 200
        assert response.json()["state"] == "changes_requested"

    def test_not_found_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post("/proposals/nonexistent/review", json={}, headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_reviewer_lacking_capability_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN has write, not review
        response = client.post(f"/proposals/{proposal.id}/review", json={}, headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# POST /proposals/{proposal_id}/assign (SPEC §9.4's `assign` action, KI-078)
# ---------------------------------------------------------------------------


class TestAssignReviewersRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        response = client.post(f"/proposals/{proposal.id}/assign", json={"reviewers": []})
        assert response.status_code == 401

    def test_reviewer_reassigns(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, decision = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        assert decision.__class__.__name__ == "RequireReview"
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)

        response = client.post(
            f"/proposals/{proposal.id}/assign",
            json={"reviewers": ["carol@example.com"]},
            headers=_auth(token),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["reviewers"] == ["carol@example.com"]
        assert body["state"] == "require_review"

    def test_reviewers_can_be_cleared(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)

        response = client.post(
            f"/proposals/{proposal.id}/assign", json={"reviewers": []}, headers=_auth(token)
        )

        assert response.status_code == 200
        assert response.json()["reviewers"] == []

    def test_missing_reviewers_field_rejected(self, tmp_path: Path) -> None:
        """`reviewers` has no default - a caller must explicitly say what
        the new list is, including an explicit empty list to clear it."""
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)

        response = client.post(f"/proposals/{proposal.id}/assign", json={}, headers=_auth(token))

        assert response.status_code == 400

    def test_not_found_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post(
            "/proposals/nonexistent/assign", json={"reviewers": []}, headers=_auth(token)
        )
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_reviewer_lacking_capability_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN has write, not review
        response = client.post(
            f"/proposals/{proposal.id}/assign", json={"reviewers": []}, headers=_auth(token)
        )
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# POST /proposals/{proposal_id}/resubmit
# ---------------------------------------------------------------------------


class TestResubmitProposalRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        kb.request_changes(proposal.id, REVIEWER)
        client, _ = _client(kb)
        response = client.post(f"/proposals/{proposal.id}/resubmit")
        assert response.status_code == 401

    def test_author_resubmits(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        kb.request_changes(proposal.id, REVIEWER)
        client, _ = _client(kb)
        token, _ = kb.issue_token(AI, author=ADMIN)

        response = client.post(f"/proposals/{proposal.id}/resubmit", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert body["proposal"]["state"] == "require_review"
        assert body["decision"] == "RequireReview"

    def test_not_found_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(AI, author=ADMIN)
        response = client.post("/proposals/nonexistent/resubmit", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_unrelated_principal_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        kb.request_changes(proposal.id, REVIEWER)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        response = client.post(f"/proposals/{proposal.id}/resubmit", headers=_auth(token))

        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"

    def test_wrong_state_rejected(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity = kb.create_entity("Person", author=HUMAN)
        proposal, _ = kb.propose(entity.id, "Person.name", "Ada", "Text", AI, model="m1")
        client, _ = _client(kb)
        token, _ = kb.issue_token(AI, author=ADMIN)

        response = client.post(f"/proposals/{proposal.id}/resubmit", headers=_auth(token))

        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# GET /contradictions
# ---------------------------------------------------------------------------


def _make_contradiction(kb: Ontology) -> tuple:
    """Create an entity with two conflicting static assertions, returning
    (entity, contradiction_id)."""
    entity = kb.create_entity("Person", author=HUMAN)
    kb.propose(entity.id, "Person.name", "Ada", "Text", HUMAN)
    kb.propose(entity.id, "Person.name", "Ava", "Text", HUMAN)
    contradiction = kb.backend.get_open_contradiction("default", entity.id, "Person.name")
    assert contradiction is not None
    return entity, contradiction.id


class TestListContradictionsRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/contradictions")
        assert response.status_code == 401

    def test_defaults_to_open(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        _make_contradiction(kb)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/contradictions", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["state"] == "open"

    def test_state_filter(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        _make_contradiction(kb)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/contradictions", params={"state": "resolved"}, headers=_auth(token))
        assert response.status_code == 200
        assert response.json() == []

    def test_all_states_via_sentinel(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        _make_contradiction(kb)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.get("/contradictions", params={"state": "all"}, headers=_auth(token))
        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_unrecognized_state_returns_validation_error(self, tmp_path: Path) -> None:
        """KI-077: an unrecognized `state` value previously reached
        kb.contradictions()'s own WHERE state = ? unfiltered and silently
        matched zero rows - indistinguishable from "no contradictions in
        that state." Must raise instead (same class of bug KI-076 fixed
        for MCP's ontolith.list_contradictions)."""
        kb = _kb(tmp_path)
        _make_contradiction(kb)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        for bad_state in ("Open", "unresolved", "All", "", "None", "accepted"):
            response = client.get(
                "/contradictions", params={"state": bad_state}, headers=_auth(token)
            )
            assert response.status_code == 400, bad_state
            assert response.json()["code"] == "VALIDATION_ERROR", bad_state


# ---------------------------------------------------------------------------
# POST /contradictions/flag
# ---------------------------------------------------------------------------


class TestFlagContradictionRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.post(
            "/contradictions/flag", json={"assertion_id_a": "a1", "assertion_id_b": "a2"}
        )
        assert response.status_code == 401

    def test_flag_creates_contradiction(self, tmp_path: Path) -> None:
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

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/contradictions/flag",
            json={"assertion_id_a": assertions[0].id, "assertion_id_b": a2.id},
            headers=_auth(token),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["action"] == "created"
        assert set(body["contradiction"]["member_ids"]) == {assertions[0].id, a2.id}

    def test_read_capability_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "readonly@example.com", kind="human", auth_method="oidc", default_capability="read"
        )
        client, _ = _client(kb)
        token, _ = kb.issue_token("readonly@example.com", author=ADMIN)
        response = client.post(
            "/contradictions/flag",
            json={"assertion_id_a": "a1", "assertion_id_b": "a2"},
            headers=_auth(token),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"

    def test_assertion_not_found_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/contradictions/flag",
            json={"assertion_id_a": "nonexistent", "assertion_id_b": "also-nonexistent"},
            headers=_auth(token),
        )
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_rationale_is_readable_back_in_metadata(self, tmp_path: Path) -> None:
        """KI-075: `rationale` (KI-071) must round-trip through this route,
        not just be recorded server-side and unreachable."""
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

        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(
            "/contradictions/flag",
            json={
                "assertion_id_a": assertions[0].id,
                "assertion_id_b": a2.id,
                "rationale": "Sources disagree",
            },
            headers=_auth(token),
        )

        assert response.status_code == 201
        contradiction_id = response.json()["contradiction"]["id"]
        history = response.json()["contradiction"]["metadata"]["rationale_history"]
        assert history == [{"rationale": "Sources disagree", "actor": HUMAN, "at": T0.isoformat()}]

        # And GET /contradictions surfaces the same history for the same
        # contradiction, not just the flag route's own response.
        list_response = client.get("/contradictions", headers=_auth(token))
        (listed,) = [c for c in list_response.json() if c["id"] == contradiction_id]
        assert listed["metadata"]["rationale_history"] == history

        # KI-075 review: extend the same contradiction with a second
        # rationale, pinning that this route's response is read back from
        # the persisted, re-fetched Contradiction (post-write) rather than
        # synthesized from just this call's own `rationale` field — a
        # single-call test can't distinguish the two.
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
        extend_response = client.post(
            "/contradictions/flag",
            json={
                "assertion_id_a": assertions[0].id,
                "assertion_id_b": a3.id,
                "rationale": "A third source also disagrees",
            },
            headers=_auth(token),
        )
        assert extend_response.status_code == 201
        assert extend_response.json()["action"] == "extended"
        extended_history = extend_response.json()["contradiction"]["metadata"]["rationale_history"]
        assert extended_history == [
            {"rationale": "Sources disagree", "actor": HUMAN, "at": T0.isoformat()},
            {
                "rationale": "A third source also disagrees",
                "actor": HUMAN,
                "at": "2025-01-02T00:00:00+00:00",
            },
        ]

        # And the accumulated trail survives POST .../resolve — the one
        # backend write (state/resolved_by/resolved_at only) that could
        # silently wipe `metadata` if it ever touched the wrong column.
        # REVIEWER, not HUMAN: the resolver must not be a party (author) to
        # any member assertion (KI-026), and HUMAN authored all three here.
        reviewer_token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        resolve_response = client.post(
            f"/contradictions/{contradiction_id}/resolve",
            json={"winner_assertion_id": assertions[0].id},
            headers=_auth(reviewer_token),
        )
        assert resolve_response.status_code == 200
        assert resolve_response.json()["metadata"]["rationale_history"] == extended_history


# ---------------------------------------------------------------------------
# POST /contradictions/{contradiction_id}/resolve
# ---------------------------------------------------------------------------


class TestResolveContradictionRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.post(
            "/contradictions/nonexistent/resolve", json={"winner_assertion_id": "a1"}
        )
        assert response.status_code == 401

    def test_reviewer_resolves(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity, contradiction_id = _make_contradiction(kb)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        assert len(flagged) == 2

        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post(
            f"/contradictions/{contradiction_id}/resolve",
            json={"winner_assertion_id": flagged[0].id},
            headers=_auth(token),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "resolved"
        assert body["resolved_by"] == REVIEWER

    def test_not_found_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(REVIEWER, author=ADMIN)
        response = client.post(
            "/contradictions/nonexistent/resolve",
            json={"winner_assertion_id": "a1"},
            headers=_auth(token),
        )
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_resolver_lacking_capability_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        entity, contradiction_id = _make_contradiction(kb)
        flagged = kb.assertions(subject=entity.id, predicate="Person.name", status="flagged")
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN has write, not review
        response = client.post(
            f"/contradictions/{contradiction_id}/resolve",
            json={"winner_assertion_id": flagged[0].id},
            headers=_auth(token),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# POST /principals
# ---------------------------------------------------------------------------


class TestCreatePrincipalRoute:
    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.post(
            "/principals",
            json={"principal_id": "dave@example.com", "kind": "human"},
        )
        assert response.status_code == 401

    def test_admin_creates_principal(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        response = client.post(
            "/principals",
            json={
                "principal_id": "dave@example.com",
                "kind": "human",
                "default_capability": "write",
                "trust_level": 3,
            },
            headers=_auth(token),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["id"] == "dave@example.com"
        assert body["default_capability"] == "write"
        assert body["trust_level"] == 3
        assert kb.get_principal("dave@example.com") is not None

    def test_non_admin_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN has write, not admin
        response = client.post(
            "/principals",
            json={"principal_id": "dave@example.com", "kind": "human"},
            headers=_auth(token),
        )
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"

    def test_ai_without_owner_returns_400(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        response = client.post(
            "/principals",
            json={"principal_id": "new-agent", "kind": "ai", "auth_method": "apikey"},
            headers=_auth(token),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"

    def test_invalid_kind_returns_400_not_500(self, tmp_path: Path) -> None:
        """kind/auth_method/default_capability/trust_level are typed to
        match Principal's own Literal/bounded constraints (not plain
        str/int) so Pydantic rejects an invalid value into the SPEC §16
        envelope, instead of it reaching Ontology.create_principal and
        raising a raw, unmapped pydantic error from constructing Principal
        internally."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        response = client.post(
            "/principals",
            json={"principal_id": "dave@example.com", "kind": "wizard"},
            headers=_auth(token),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"

    def test_trust_level_out_of_range_returns_400_not_500(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        response = client.post(
            "/principals",
            json={"principal_id": "dave@example.com", "kind": "human", "trust_level": 99},
            headers=_auth(token),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"


class TestListPrincipalsRoute:
    """KI-022: GET /principals."""

    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/principals")
        assert response.status_code == 401

    def test_admin_lists_principals(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        response = client.get("/principals", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        ids = {p["id"] for p in body}
        assert ids == {HUMAN, REVIEWER, AI, ADMIN}

        # Full field-by-field check on the AI principal (kind/owner/auth_method
        # all differ from the human principals) guards against a transposed
        # PrincipalOut(...) field mapping in the route.
        ai_out = next(p for p in body if p["id"] == AI)
        assert ai_out["kind"] == "ai"
        assert ai_out["owner"] == AI_OWNER
        assert ai_out["auth_method"] == "apikey"
        assert ai_out["default_capability"] == "propose"

    def test_non_admin_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN has write, not admin
        response = client.get("/principals", headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# /principals/{principal_id}/tokens
# ---------------------------------------------------------------------------


class TestTokenRoutes:
    def test_issue_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.post(f"/principals/{HUMAN}/tokens")
        assert response.status_code == 401

    def test_admin_issues_token(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        response = client.post(f"/principals/{HUMAN}/tokens", headers=_auth(token))

        assert response.status_code == 201
        body = response.json()
        assert "token" in body
        assert "credential_id" in body

    def test_non_admin_forbidden_issue(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.post(f"/principals/{HUMAN}/tokens", headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"

    def test_list_tokens(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.issue_token(HUMAN, author=ADMIN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        response = client.get(f"/principals/{HUMAN}/tokens", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert "token" not in body[0]
        assert "id" in body[0]

    def test_list_tokens_includes_issued_by_and_revoked_by(self, tmp_path: Path) -> None:
        """KI-072: the audit trail's read half - who issued/revoked this
        credential must be visible through the same route that lists it."""
        kb = _kb(tmp_path)
        _, credential_id = kb.issue_token(HUMAN, author=ADMIN)
        kb.revoke_token(credential_id, author=ADMIN)
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)

        response = client.get(f"/principals/{HUMAN}/tokens", headers=_auth(token))

        assert response.status_code == 200
        [credential] = [c for c in response.json() if c["id"] == credential_id]
        assert credential["issued_by"] == ADMIN
        assert credential["revoked_by"] == ADMIN

    def test_list_tokens_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get(f"/principals/{HUMAN}/tokens")
        assert response.status_code == 401

    def test_list_tokens_non_admin_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN itself: write, not admin
        response = client.get(f"/principals/{HUMAN}/tokens", headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"

    def test_revoke_token(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        admin_token, _ = kb.issue_token(ADMIN, author=ADMIN)
        issue_response = client.post(f"/principals/{HUMAN}/tokens", headers=_auth(admin_token))
        credential_id = issue_response.json()["credential_id"]
        issued_token = issue_response.json()["token"]

        response = client.delete(
            f"/principals/{HUMAN}/tokens/{credential_id}", headers=_auth(admin_token)
        )
        assert response.status_code == 204

        # The revoked token no longer authenticates.
        follow_up = client.get("/schema", headers=_auth(issued_token))
        assert follow_up.status_code == 401

    def test_revoke_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.delete(f"/principals/{HUMAN}/tokens/some-credential-id")
        assert response.status_code == 401

    def test_revoke_nonexistent_credential_returns_404(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)
        response = client.delete(f"/principals/{HUMAN}/tokens/nonexistent", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

    def test_revoke_non_admin_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        admin_token, _ = kb.issue_token(ADMIN, author=ADMIN)
        issue_response = client.post(f"/principals/{HUMAN}/tokens", headers=_auth(admin_token))
        credential_id = issue_response.json()["credential_id"]

        non_admin_token, _ = kb.issue_token(HUMAN, author=ADMIN)
        response = client.delete(
            f"/principals/{HUMAN}/tokens/{credential_id}", headers=_auth(non_admin_token)
        )
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"

    def test_revoke_credential_belonging_to_different_principal_returns_404(
        self, tmp_path: Path
    ) -> None:
        """The credential_id path segment must actually belong to
        principal_id — regression test for a REST-layer footgun (an admin
        could otherwise revoke a different principal's token while the URL
        implied it was scoped to principal_id)."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        admin_token, _ = kb.issue_token(ADMIN, author=ADMIN)
        issue_response = client.post(f"/principals/{HUMAN}/tokens", headers=_auth(admin_token))
        human_credential_id = issue_response.json()["credential_id"]

        # Path says REVIEWER, but the credential actually belongs to HUMAN.
        response = client.delete(
            f"/principals/{REVIEWER}/tokens/{human_credential_id}", headers=_auth(admin_token)
        )
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"

        # The credential is untouched - still resolves.
        assert kb.list_tokens(HUMAN, author=ADMIN)[0].revoked_at is None


# ---------------------------------------------------------------------------
# GET /admin-events
# ---------------------------------------------------------------------------


class TestAdminEventsRoute:
    def test_lists_recorded_events(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "erin@example.com", kind="human", default_capability="write", author=ADMIN
        )
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)

        response = client.get("/admin-events", headers=_auth(token))

        assert response.status_code == 200
        [event] = [e for e in response.json() if e["target"] == "erin@example.com"]
        assert event["actor"] == ADMIN
        assert event["action"] == "create_principal"

    def test_includes_detail_when_recorded(self, tmp_path: Path) -> None:
        """No production call site currently passes `detail` (KI-060/072
        review) - seed one directly via record_admin_event to pin that
        AdminEventOut actually surfaces it, not just id/actor/action/target/at."""
        kb = _kb(tmp_path)
        kb.record_admin_event(ADMIN, "apply_schema", "default:v1", detail="seeded for test")
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)

        response = client.get("/admin-events", headers=_auth(token))

        [event] = [e for e in response.json() if e["target"] == "default:v1"]
        assert event["detail"] == "seeded for test"

    def test_filters_by_actor(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "carol@example.com", kind="human", default_capability="admin", author=ADMIN
        )
        kb.create_principal(
            "dave@example.com", kind="human", default_capability="write", author="carol@example.com"
        )
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)

        response = client.get(
            "/admin-events", params={"actor": "carol@example.com"}, headers=_auth(token)
        )

        assert response.status_code == 200
        assert [e["target"] for e in response.json()] == ["dave@example.com"]

    def test_filters_by_target(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        kb.create_principal(
            "carol@example.com", kind="human", default_capability="write", author=ADMIN
        )
        client, _ = _client(kb)
        token, _ = kb.issue_token(ADMIN, author=ADMIN)

        response = client.get(
            "/admin-events", params={"target": "carol@example.com"}, headers=_auth(token)
        )

        assert response.status_code == 200
        assert [e["actor"] for e in response.json()] == [ADMIN]

    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/admin-events")
        assert response.status_code == 401

    def test_non_admin_forbidden(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN itself: write, not admin
        response = client.get("/admin-events", headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["code"] == "CAPABILITY_ERROR"


# ---------------------------------------------------------------------------
# GET /namespaces
# ---------------------------------------------------------------------------


class TestListNamespacesRoute:
    """KI-022: GET /namespaces."""

    def test_requires_auth(self, tmp_path: Path) -> None:
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        response = client.get("/namespaces")
        assert response.status_code == 401

    def test_non_admin_lists_namespaces(self, tmp_path: Path) -> None:
        """Ungated beyond authentication, unlike GET /principals - any
        authenticated principal can list namespaces, not just admin."""
        kb = _kb(tmp_path)
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)  # HUMAN has write, not admin
        response = client.get("/namespaces", headers=_auth(token))

        assert response.status_code == 200
        body = response.json()
        assert [n["id"] for n in body] == ["default"]
        assert "created_at" in body[0]
        assert body[0]["metadata"] == {}

    def test_namespace_metadata_round_trips(self, tmp_path: Path) -> None:
        """A non-empty metadata blob round-trips through the REST route -
        inserted directly since there's no public write path for it yet."""
        kb = _kb(tmp_path)
        kb.backend.conn.execute(
            "INSERT INTO namespace (id, created_at, metadata) VALUES (?, ?, ?)",
            ("acme-research", "2030-01-01T00:00:00+00:00", '{"team": "research"}'),
        )
        kb.backend.conn.commit()
        client, _ = _client(kb)
        token, _ = kb.issue_token(HUMAN, author=ADMIN)

        response = client.get("/namespaces", headers=_auth(token))

        body = response.json()
        acme = next(n for n in body if n["id"] == "acme-research")
        assert acme["metadata"] == {"team": "research"}
