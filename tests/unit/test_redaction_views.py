"""The `personal` mark and the `redacted` flag in every interface (ADR-0056)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ontolith import Ontology
from ontolith.identity.token_auth import TokenAuthProvider
from ontolith.interfaces.graphql import create_graphql_app
from ontolith.interfaces.mcp import create_mcp_server
from ontolith.interfaces.rest import create_rest_app
from ontolith.schema import Concept, Property, Text, compile_schema

ADMIN = "admin@example.com"


class Person(Concept):
    name: Text = Property(personal=True)  # type: ignore[assignment]
    city: Text | None = None


def _setup(tmp_path: Path) -> tuple[Ontology, str, str, str]:
    kb = Ontology.connect(tmp_path / "kb.db", erasure="redaction")
    kb.create_principal(ADMIN, kind="human", default_capability="admin")
    kb.apply_schema(compile_schema("default", 1, Person), ADMIN)
    eid = kb.create_entity("Person", ADMIN).id
    aid = kb.assert_literal(eid, "Person.name", "Ada Lovelace", "Text", ADMIN).id
    kb.erase_subject(eid, actor=ADMIN, reason="r")
    return kb, kb.issue_token(ADMIN, author=ADMIN)[0], eid, aid


def test_rest_shows_personal_and_redacted(tmp_path: Path) -> None:
    kb, token, _, aid = _setup(tmp_path)
    client = TestClient(create_rest_app(kb, TokenAuthProvider(kb.backend)))
    headers = {"Authorization": f"Bearer {token}"}
    props = {
        p["name"]: p
        for p in client.get("/schema", headers=headers).json()["concepts"][0]["properties"]
    }
    assert props["name"]["personal"] is True and props["city"]["personal"] is False
    body = client.get(f"/provenance/{aid}", headers=headers).json()
    assert body["redacted"] is True and body["value"] == ""


def test_graphql_shows_personal_and_redacted(tmp_path: Path) -> None:
    kb, token, _, aid = _setup(tmp_path)
    client = TestClient(create_graphql_app(kb, TokenAuthProvider(kb.backend)))
    headers = {"Authorization": f"Bearer {token}"}

    def run(query: str) -> dict:  # type: ignore[type-arg]
        return client.post("/graphql", json={"query": query}, headers=headers).json()["data"]

    schema = run("{ schema { concepts { properties { name personal } } } }")
    flags = {p["name"]: p["personal"] for p in schema["schema"]["concepts"][0]["properties"]}
    assert flags == {"name": True, "city": False}
    prov = run(f'{{ provenance(assertionId: "{aid}") {{ redacted value }} }}')["provenance"]
    assert prov == {"redacted": True, "value": ""}


def test_mcp_shows_personal_and_redacted(tmp_path: Path) -> None:
    kb, token, _, aid = _setup(tmp_path)
    mcp = create_mcp_server(kb, TokenAuthProvider(kb.backend))
    schema = mcp._tool_manager.get_tool("ontolith.schema").fn(token=token)
    props = {p["name"]: p["personal"] for p in schema["concepts"][0]["properties"]}
    assert props == {"name": True, "city": False}
    prov = mcp._tool_manager.get_tool("ontolith.provenance").fn(token=token, assertion_id=aid)
    assert prov["redacted"] is True and prov["value"] == ""
