"""Conformance vector: erasure in redaction mode (ADR-0056).

Runs against every registered backend that implements the optional
`RedactionBackend` capability, and skips the rest. A backend must not
implement the capability unless it passes the byte-absence vector below:
redaction is never offered with a weaker promise.
"""

from __future__ import annotations

import pytest

from conformance.conftest import KbFactory
from ontolith import FixedClock, SequentialIdProvider
from ontolith.schema import Concept, Property, Text, compile_schema
from ontolith.store.redaction import RedactionBackend

ADMIN = "admin@example.com"
SECRET = "Ada Quuxplugh Lovelace"


class Person(Concept):
    name: Text = Property(personal=True)  # type: ignore[assignment]
    city: Text | None = None


def _redaction_kb(make_kb: KbFactory):  # type: ignore[no-untyped-def]
    kb = make_kb(FixedClock("2025-01-01T00:00:00Z"), SequentialIdProvider())
    if not isinstance(kb.backend, RedactionBackend):
        pytest.skip("backend does not implement the redaction capability")
    kb.create_principal(ADMIN, kind="human", default_capability="admin")
    kb.apply_schema(compile_schema("default", 1, Person), ADMIN)
    kb.enable_redaction(ADMIN)
    return kb


def test_erasure_removes_the_value_and_keeps_the_rest(make_kb: KbFactory) -> None:
    kb = _redaction_kb(make_kb)
    eid = kb.create_entity("Person", ADMIN).id
    named = kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN)
    city = kb.assert_literal(eid, "Person.city", "Lisbon", "Text", ADMIN)

    kb.erase_subject(eid, actor=ADMIN, reason="REQ-1")

    gone = kb.backend.get_assertion(named.id)
    assert gone is not None and gone.status == "retracted" and gone.redacted
    assert SECRET not in gone.value
    kept = kb.backend.get_assertion(city.id)
    assert kept is not None and kept.status == "active" and kept.value == "Lisbon"
    assert [r.kind for r in kb.backend.erasure_records()] == ["enabled", "erased"]


def test_erased_bytes_are_not_in_the_database_file(make_kb: KbFactory) -> None:
    kb = _redaction_kb(make_kb)
    path = make_kb.last_path
    eid = kb.create_entity("Person", ADMIN).id
    kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN, rationale=SECRET)
    kb.reindex()
    kb.erase_subject(eid, actor=ADMIN, reason="REQ-1")
    kb.backend.close()
    for candidate in path.parent.glob(path.name + "*"):
        assert SECRET.encode() not in candidate.read_bytes(), candidate.name
