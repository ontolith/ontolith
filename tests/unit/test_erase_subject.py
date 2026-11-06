"""`Ontology.erase_subject` on SQLite (ADR-0056 part 2)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock
from ontolith.core.errors import AuthError, CapabilityError, NotFoundError, ValidationError
from ontolith.govern.operations import AssertLiteralOp, CreateEntityOp, LocalRef
from ontolith.schema import Concept, Integer, Property, Text, compile_schema

T0 = datetime(2025, 1, 1, tzinfo=UTC)
ADMIN = "admin@example.com"
SECRET = "Zyxwvut-Quuxplugh-1971"
SECRET_KEY = "key-Qwertyuiop-4242"


class Person(Concept):
    __personal_key__ = True
    name: Text = Property(personal=True)  # type: ignore[assignment]
    city: Text | None = None


def _kb(tmp_path: Path, mode: str = "redaction") -> Ontology:
    kb = Ontology.connect(tmp_path / "kb.db", clock=FixedClock(T0), erasure=mode)  # type: ignore[arg-type]
    kb.create_principal(ADMIN, kind="human", default_capability="admin")
    kb.apply_schema(compile_schema("default", 1, Person), ADMIN)
    return kb


def _person(kb: Ontology, key: str = SECRET_KEY) -> str:
    return kb.create_entity("Person", ADMIN, natural_key=key).id


def test_erase_redacts_the_value_and_keeps_the_rest(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    named = kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN, source="passport")
    city = kb.assert_literal(eid, "Person.city", "Lisbon", "Text", ADMIN)

    result = kb.erase_subject(eid, actor=ADMIN, reason="REQ-1")

    assert result.assertions_redacted == 1
    gone = kb.backend.get_assertion(named.id)
    assert gone is not None
    assert gone.status == "retracted"
    assert gone.source is None and gone.rationale is None
    assert gone.metadata["redacted"]["erasure_id"] == result.erasure_id
    assert SECRET not in gone.value
    kept = kb.backend.get_assertion(city.id)
    assert kept is not None and kept.value == "Lisbon" and kept.status == "active"
    entity = kb.backend.get_entity(eid)
    assert entity is not None and entity.natural_key is None


def test_bytes_are_gone_from_the_database_files(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN, rationale=SECRET)
    kb.reindex()
    kb.erase_subject(eid, actor=ADMIN, reason="REQ-1")
    kb.backend.close()
    for suffix in ("", "-wal", "-journal"):
        f = Path(str(tmp_path / "kb.db") + suffix)
        if f.exists():
            data = f.read_bytes()
            assert SECRET.encode() not in data, suffix
            assert SECRET_KEY.encode() not in data, suffix


def test_an_old_embedding_does_not_survive(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    other = _person(kb, "other")
    kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN)
    kb.assert_literal(other, "Person.name", "Someone Else", "Text", ADMIN)
    kb.reindex()
    before = kb.backend.vector_search("entity", kb.embedder.embed([SECRET])[0], 5)
    assert eid in {i for i, _ in before}
    kb.erase_subject(eid, actor=ADMIN, reason="REQ-1")
    after = kb.backend.vector_search("entity", kb.embedder.embed([SECRET])[0], 5)
    assert eid not in {i for i, _ in after}
    assert other in {i for i, _ in after}


def test_only_a_human_admin_may_erase(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    kb.create_principal("writer@example.com", kind="human", default_capability="write")
    kb.create_principal("bot", kind="service", default_capability="admin", owner=ADMIN)
    with pytest.raises(CapabilityError):
        kb.erase_subject(eid, actor="writer@example.com", reason="r")
    with pytest.raises(CapabilityError):
        kb.erase_subject(eid, actor="bot", reason="r")
    with pytest.raises(AuthError):
        kb.erase_subject(eid, actor="nobody", reason="r")


def test_strict_mode_refuses(tmp_path: Path) -> None:
    kb = _kb(tmp_path, mode="strict")
    eid = _person(kb)
    with pytest.raises(ValidationError):
        kb.erase_subject(eid, actor=ADMIN, reason="r")


def test_blank_reason_and_unknown_entity(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    with pytest.raises(ValidationError):
        kb.erase_subject(eid, actor=ADMIN, reason="  ")
    with pytest.raises(NotFoundError):
        kb.erase_subject("missing", actor=ADMIN, reason="r")


def test_erasure_is_recorded_and_repeatable(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN)
    first = kb.erase_subject(eid, actor=ADMIN, reason="REQ-1")
    again = kb.erase_subject(eid, actor=ADMIN, reason="REQ-2")
    assert again.assertions_redacted == 0
    erased = [r for r in kb.backend.erasure_records() if r.kind == "erased"]  # type: ignore[attr-defined]
    assert [r.id for r in erased] == [first.erasure_id, again.erasure_id]
    assert all(r.subject == eid for r in erased)
    assert SECRET not in (erased[0].detail or "") + (erased[0].reason or "")


def test_a_second_person_with_a_redacted_key_does_not_collide(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    a, b = _person(kb, "a"), _person(kb, "b")
    kb.erase_subject(a, actor=ADMIN, reason="r")
    kb.erase_subject(b, actor=ADMIN, reason="r")


def test_pending_proposal_is_rejected_and_blanked(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    kb.create_principal("ai", kind="ai", owner=ADMIN, default_capability="propose")
    proposal, _ = kb.propose(
        eid, "Person.name", SECRET, "Text", "ai", rationale=SECRET, model="m-1"
    )
    assert proposal.state in ("submitted", "require_review", "under_review")
    result = kb.erase_subject(eid, actor=ADMIN, reason="r")
    assert result.proposals_rejected == 1
    stored = kb.backend.get_proposal(proposal.id)
    assert stored is not None and stored.state == "rejected"
    assert SECRET not in str(stored.payload)
    with pytest.raises(ValidationError):
        kb.accept_proposal(proposal.id, ADMIN)


def test_accepted_batch_proposal_payload_is_blanked(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    kb.create_principal("ai", kind="ai", owner=ADMIN, default_capability="propose")
    proposal, _ = kb.propose_batch(
        [
            CreateEntityOp(key="p", concept="Person", natural_key=SECRET_KEY),
            AssertLiteralOp(
                subject=LocalRef("p"), predicate="Person.name", value=SECRET, value_type="Text"
            ),
        ],
        "ai",
        model="m-1",
    )
    kb.accept_proposal(proposal.id, ADMIN)
    eid = kb.created_entities(proposal.id)["p"]
    result = kb.erase_subject(eid, actor=ADMIN, reason="r")
    assert result.proposals_redacted == 1 and result.proposals_rejected == 0
    stored = kb.backend.get_proposal(proposal.id)
    assert stored is not None and stored.state == "accepted"
    assert SECRET not in str(stored.payload) and SECRET_KEY not in str(stored.payload)


def test_contradiction_is_closed_with_no_winner(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    a = kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN)
    b = kb.assert_literal(eid, "Person.name", SECRET + "x", "Text", ADMIN)
    contradiction = kb.backend.get_open_contradiction("default", eid, "Person.name")
    assert contradiction is not None
    kb.flag_contradiction(a.id, b.id, ADMIN, rationale=SECRET)
    result = kb.erase_subject(eid, actor=ADMIN, reason="r")
    assert result.contradictions_closed == 1
    closed = kb.backend.get_contradiction(contradiction.id)
    assert closed is not None and closed.state == "resolved" and closed.resolved_by == ADMIN
    assert SECRET not in str(closed.metadata)
    for aid in (a.id, b.id):
        row = kb.backend.get_assertion(aid)
        assert row is not None and row.status == "retracted"


class _Clock:
    def __init__(self) -> None:
        self.t = T0

    def now(self) -> datetime:
        return self.t


def test_as_of_before_erasure_shows_a_fact_without_content(tmp_path: Path) -> None:
    clock = _Clock()
    kb = Ontology.connect(tmp_path / "kb.db", clock=clock, erasure="redaction")  # type: ignore[arg-type]
    kb.create_principal(ADMIN, kind="human", default_capability="admin")
    kb.apply_schema(compile_schema("default", 1, Person), ADMIN)
    eid = kb.create_entity("Person", ADMIN, natural_key="k").id
    kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN)
    clock.t = datetime(2025, 2, 1, tzinfo=UTC)
    kb.erase_subject(eid, actor=ADMIN, reason="r")
    before = kb.backend.assertions(subject=eid, as_of_time=datetime(2025, 1, 15, tzinfo=UTC))
    assert len(before) == 1 and before[0].redacted and before[0].value == ""
    after = kb.backend.assertions(subject=eid, as_of_time=datetime(2025, 3, 1, tzinfo=UTC))
    assert after == []


def test_cannot_run_inside_an_open_transaction(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    with kb.backend.transaction(), pytest.raises(ValidationError):
        kb.backend.compact()  # type: ignore[attr-defined]
    kb.erase_subject(eid, actor=ADMIN, reason="r")


def test_a_reading_connection_is_reported_not_hidden(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN)
    reader = sqlite3.connect(tmp_path / "kb.db", isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM assertion").fetchone()
    kb.assert_literal(eid, "Person.city", "x", "Text", ADMIN)  # WAL moves past the snapshot
    result = kb.erase_subject(eid, actor=ADMIN, reason="r")
    assert result.compaction_error is not None
    reader.execute("COMMIT")
    reader.close()
    assert kb.erase_subject(eid, actor=ADMIN, reason="r").compaction_error is None
    kb.backend.close()
    assert SECRET.encode() not in _file_bytes(tmp_path / "kb.db")


def test_superseded_and_resolved_rows_are_redacted_too(tmp_path: Path) -> None:
    class Worker(Concept):
        phone: Text = Property(personal=True, temporality="time_varying")  # type: ignore[assignment]

    kb = Ontology.connect(tmp_path / "kb.db", clock=FixedClock(T0), erasure="redaction")
    kb.create_principal(ADMIN, kind="human", default_capability="admin")
    kb.apply_schema(compile_schema("default", 1, Worker), ADMIN)
    eid = kb.create_entity("Worker", ADMIN).id
    old = kb.assert_literal(eid, "Worker.phone", SECRET, "Text", ADMIN)
    kb.assert_literal(eid, "Worker.phone", SECRET + "2", "Text", ADMIN)
    assert kb.backend.get_assertion(old.id).status == "superseded"  # type: ignore[union-attr]
    kb.erase_subject(eid, actor=ADMIN, reason="r")
    row = kb.backend.get_assertion(old.id)
    assert row is not None and row.status == "superseded" and row.redacted
    kb.backend.close()
    assert SECRET.encode() not in _file_bytes(tmp_path / "kb.db")


def test_already_resolved_contradiction_keeps_its_resolver_and_loses_its_text(
    tmp_path: Path,
) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    a = kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN)
    b = kb.assert_literal(eid, "Person.name", SECRET + "x", "Text", ADMIN)
    cid = kb.backend.get_open_contradiction("default", eid, "Person.name").id  # type: ignore[union-attr]
    kb.flag_contradiction(a.id, b.id, ADMIN, rationale=SECRET)
    kb.create_principal("second@example.com", kind="human", default_capability="admin")
    kb.resolve_contradiction(cid, a.id, "second@example.com")
    result = kb.erase_subject(eid, actor=ADMIN, reason="r")
    assert result.contradictions_closed == 0
    row = kb.backend.get_contradiction(cid)
    assert row is not None and SECRET not in str(row.metadata)


def test_auto_accepted_batch_is_found_through_its_metadata(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    kb.create_principal("svc", kind="human", default_capability="write")
    proposal, _ = kb.propose_batch(
        [
            CreateEntityOp(key="p", concept="Person", natural_key=SECRET_KEY),
            AssertLiteralOp(
                subject=LocalRef("p"), predicate="Person.name", value=SECRET, value_type="Text"
            ),
        ],
        "svc",
    )
    eid = kb.created_entities(proposal.id).get("p")
    assert eid is not None, proposal.state
    result = kb.erase_subject(eid, actor=ADMIN, reason="r")
    assert result.proposals_redacted == 1
    stored = kb.backend.get_proposal(proposal.id)
    assert stored is not None and SECRET not in str(stored.payload)


def test_raw_sql_cannot_undo_an_erasure_record(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    eid = _person(kb)
    kb.erase_subject(eid, actor=ADMIN, reason="r")
    kb.backend.close()
    con = sqlite3.connect(tmp_path / "kb.db")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM erasure_record")


def _file_bytes(path: Path) -> bytes:
    return b"".join(
        Path(str(path) + s).read_bytes() for s in ("", "-wal") if Path(str(path) + s).exists()
    )


def test_the_value_is_in_the_file_before_and_gone_after_a_late_switch(tmp_path: Path) -> None:
    """A KB that began strict and switched later still ends up clean."""
    kb = _kb(tmp_path, mode="strict")
    eid = _person(kb)
    kb.assert_literal(eid, "Person.name", SECRET, "Text", ADMIN, rationale=SECRET)
    kb.reindex()
    assert SECRET.encode() in _file_bytes(tmp_path / "kb.db")  # the check can fail
    kb.enable_redaction(ADMIN)
    kb.erase_subject(eid, actor=ADMIN, reason="r")
    kb.backend.close()
    assert SECRET.encode() not in _file_bytes(tmp_path / "kb.db")


class Patient(Concept):
    age: Integer = Property(personal=True)  # type: ignore[assignment]


def test_a_redacted_number_matches_no_typed_filter(tmp_path: Path) -> None:
    kb = Ontology.connect(tmp_path / "kb.db", clock=FixedClock(T0), erasure="redaction")
    kb.create_principal(ADMIN, kind="human", default_capability="admin")
    kb.apply_schema(compile_schema("default", 1, Patient), ADMIN)
    eid = kb.create_entity("Patient", ADMIN).id
    kb.assert_literal(eid, "Patient.age", "42", "Integer", ADMIN)
    kb.erase_subject(eid, actor=ADMIN, reason="r")
    for op in ("lt", "lte", "gt", "gte", "eq"):
        found = kb.backend.entities_where(
            "default", "Patient", [("Patient.age", op, 0 if op in ("gt", "gte") else 100)]
        )
        assert found == [], op
