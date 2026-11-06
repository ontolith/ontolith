"""The stored erasure mode and `enable_redaction` (ADR-0056)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock
from ontolith.core.errors import AuthError, CapabilityError, ValidationError
from ontolith.schema import SchemaIR
from ontolith.store.duckdb import DuckDBBackend

T0 = datetime(2025, 1, 1, tzinfo=UTC)
ADMIN = "admin@example.com"


def _connect(path: Path, **kwargs: object) -> Ontology:
    return Ontology.connect(path, clock=FixedClock(T0), **kwargs)  # type: ignore[arg-type]


def _admin(kb: Ontology) -> None:
    kb.create_principal(ADMIN, kind="human", default_capability="admin")


def test_default_is_strict(tmp_path: Path) -> None:
    assert _connect(tmp_path / "kb.db").erasure_mode == "strict"


def test_new_kb_can_be_created_in_redaction_mode_and_remembers_it(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    assert _connect(path, erasure="redaction").erasure_mode == "redaction"
    assert _connect(path).erasure_mode == "redaction"


def test_stored_mode_is_the_same_for_every_connection(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    _connect(path, erasure="redaction")
    assert _connect(path, erasure="redaction").erasure_mode == "redaction"


def test_asking_for_strict_on_a_redaction_kb_raises(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    _connect(path, erasure="redaction")
    with pytest.raises(ValidationError, match="redaction"):
        _connect(path, erasure="strict")


def test_asking_for_redaction_on_a_kb_that_has_data_raises(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    _admin(_connect(path))
    with pytest.raises(ValidationError, match="enable_redaction"):
        _connect(path, erasure="redaction")


def test_enable_redaction_moves_a_strict_kb_one_way(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    kb = _connect(path)
    _admin(kb)
    kb.enable_redaction(ADMIN)
    assert kb.erasure_mode == "redaction"
    assert _connect(path).erasure_mode == "redaction"


def test_enable_redaction_is_recorded_and_idempotent(tmp_path: Path) -> None:
    kb = _connect(tmp_path / "kb.db")
    _admin(kb)
    kb.enable_redaction(ADMIN)
    kb.enable_redaction(ADMIN)
    [record] = kb.backend.erasure_records()  # type: ignore[attr-defined]
    assert (record.kind, record.actor, record.at) == ("enabled", ADMIN, T0)


@pytest.mark.parametrize(
    ("kind", "capability", "error"),
    [
        ("human", "write", CapabilityError),
        ("ai", "admin", CapabilityError),
        ("service", "admin", CapabilityError),
    ],
)
def test_enable_redaction_needs_a_human_admin(
    tmp_path: Path, kind: str, capability: str, error: type[Exception]
) -> None:
    kb = _connect(tmp_path / "kb.db")
    _admin(kb)
    kwargs = {"owner": ADMIN} if kind == "ai" else {}
    kb.create_principal("other@example.com", kind=kind, default_capability=capability, **kwargs)
    with pytest.raises(error):
        kb.enable_redaction("other@example.com")
    assert kb.erasure_mode == "strict"


def test_enable_redaction_unknown_actor_is_an_auth_error(tmp_path: Path) -> None:
    with pytest.raises(AuthError):
        _connect(tmp_path / "kb.db").enable_redaction("nobody@example.com")


def test_the_store_refuses_to_edit_or_remove_audit_rows(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    kb = _connect(path)
    _admin(kb)
    kb.enable_redaction(ADMIN)
    raw = sqlite3.connect(path)
    for statement in (
        "UPDATE kb_setting SET value = 'strict'",
        "DELETE FROM kb_setting",
        "UPDATE erasure_record SET actor = 'x'",
        "DELETE FROM erasure_record",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            raw.execute(statement)


def test_the_store_refuses_to_switch_the_mode_back(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    kb = _connect(path)
    _admin(kb)
    kb.enable_redaction(ADMIN)
    raw = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError, match="one-way"):
        raw.execute(
            "INSERT INTO kb_setting (id, key, value, at, actor) "
            "VALUES ('x', 'erasure_mode', 'strict', '2025-01-02T00:00:00+00:00', 'a')"
        )


def test_the_store_refuses_to_replace_an_audit_row(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    kb = _connect(path)
    _admin(kb)
    kb.enable_redaction(ADMIN)
    raw = sqlite3.connect(path)
    [(record_id,)] = raw.execute("SELECT id FROM erasure_record").fetchall()
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute(
            "INSERT OR REPLACE INTO erasure_record (id, kind, actor, at) "
            "VALUES (?, 'enabled', 'x', '2025-01-02T00:00:00+00:00')",
            (record_id,),
        )


def test_a_backend_without_the_capability_cannot_be_put_in_redaction_mode(
    tmp_path: Path,
) -> None:
    backend = DuckDBBackend(tmp_path / "kb.duckdb")
    assert Ontology(backend, clock=FixedClock(T0)).erasure_mode == "strict"
    with pytest.raises(ValidationError, match="redaction"):
        Ontology(backend, clock=FixedClock(T0), erasure="redaction")
    kb = Ontology(backend, clock=FixedClock(T0))
    _admin(kb)
    with pytest.raises(ValidationError, match="redaction"):
        kb.enable_redaction(ADMIN)


def test_a_rowid_replace_cannot_remove_an_audit_row(tmp_path: Path) -> None:
    """The REPLACE guard keys on `id`; a REPLACE that collides on `rowid`
    instead would bypass it, so these tables have no `rowid` to collide on."""
    path = tmp_path / "kb.db"
    kb = _connect(path)
    _admin(kb)
    kb.enable_redaction(ADMIN)
    raw = sqlite3.connect(path)
    for statement in (
        "INSERT OR REPLACE INTO kb_setting(rowid, id, key, value, at, actor) "
        "VALUES (1, 'x', 'junk', 'v', 't', 'a')",
        "REPLACE INTO erasure_record(rowid, id, kind, actor, at) "
        "VALUES (1, 'y', 'enabled', 'a', 't')",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute(statement)
    assert _connect(path).erasure_mode == "redaction"
    assert len(_connect(path).backend.erasure_records()) == 1  # type: ignore[attr-defined]


def test_the_mode_cannot_be_turned_on_without_an_audit_record(tmp_path: Path) -> None:
    path = tmp_path / "kb.db"
    _connect(path)
    raw = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError, match="erasure_record"):
        raw.execute(
            "INSERT INTO kb_setting (id, key, value, at, actor) "
            "VALUES ('x', 'erasure_mode', 'redaction', '2025-01-01T00:00:00+00:00', 'a')"
        )
    assert _connect(path).erasure_mode == "strict"


def test_a_kb_with_a_schema_but_no_principals_is_not_empty(tmp_path: Path) -> None:
    """Data written straight to the backend leaves `list_principals()` empty."""
    path = tmp_path / "kb.db"
    kb = _connect(path)
    kb.backend.put_schema(SchemaIR(namespace="default", version=1))
    with pytest.raises(ValidationError, match="enable_redaction"):
        _connect(path, erasure="redaction")
    assert _connect(path).erasure_mode == "strict"


def test_a_failed_connect_closes_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kb.db"
    _connect(path, erasure="redaction")
    closed: list[bool] = []
    from ontolith.store.sqlite import SQLiteBackend

    real_close = SQLiteBackend.close

    def spy(self: SQLiteBackend) -> None:
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(SQLiteBackend, "close", spy)
    with pytest.raises(ValidationError):
        _connect(path, erasure="strict")
    assert closed == [True]
