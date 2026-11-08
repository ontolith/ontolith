"""A REPLACE that collides on `rowid` cannot remove an audit row (KI-117).

The older audit tables are append-only through triggers. The REPLACE guard
used to look only at `id`, so `INSERT OR REPLACE ... (rowid, id, ...)` with a
new id and an old rowid removed the old row without firing the delete trigger
(`recursive_triggers` is per connection, and a raw connection opens with it
off).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock

T0 = datetime(2025, 1, 1, tzinfo=UTC)
AT = "2025-01-01T00:00:00+00:00"

# table -> (columns, a row of values with the placeholder id first)
_ROWS: dict[str, tuple[str, tuple[str, ...]]] = {
    "assertion_event": (
        "id, assertion_id, actor, action, at",
        ("{id}", "a-1", "p-1", "retracted", AT),
    ),
    "proposal_event": (
        "id, proposal_id, actor, type, at",
        ("{id}", "pr-1", "p-1", "submitted", AT),
    ),
    "admin_event": (
        "id, actor, action, target, at",
        ("{id}", "p-1", "apply_schema", "ns", AT),
    ),
}


def _insert(raw: sqlite3.Connection, table: str, row_id: str, verb: str = "INSERT") -> None:
    columns, values = _ROWS[table]
    raw.execute(
        f"{verb} INTO {table} ({columns}) VALUES ({', '.join('?' * len(values))})",  # noqa: S608  # nosec B608
        tuple(row_id if v == "{id}" else v for v in values),
    )


def _kb(tmp_path: Path) -> Path:
    path = tmp_path / "kb.db"
    Ontology.connect(path, clock=FixedClock(T0))
    return path


@pytest.mark.parametrize("table", sorted(_ROWS))
def test_a_rowid_replace_cannot_remove_an_audit_row(tmp_path: Path, table: str) -> None:
    path = _kb(tmp_path)
    raw = sqlite3.connect(path)
    _insert(raw, table, "original")
    [(rowid,)] = raw.execute(f"SELECT rowid FROM {table}").fetchall()  # noqa: S608  # nosec B608
    raw.commit()

    columns, values = _ROWS[table]
    statement = (
        f"INSERT OR REPLACE INTO {table} (rowid, {columns}) "  # noqa: S608  # nosec B608
        f"VALUES (?, {', '.join('?' * len(values))})"
    )
    params = (rowid, *("other" if v == "{id}" else v for v in values))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute(statement, params)

    assert raw.execute(f"SELECT id FROM {table}").fetchall() == [("original",)]  # noqa: S608  # nosec B608


@pytest.mark.parametrize("table", sorted(_ROWS))
def test_ordinary_appends_still_work(tmp_path: Path, table: str) -> None:
    raw = sqlite3.connect(_kb(tmp_path))
    for row_id in ("one", "two", "three"):
        _insert(raw, table, row_id)
    raw.commit()
    count = raw.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608  # nosec B608
    assert count == (3,)


@pytest.mark.parametrize("table", sorted(_ROWS))
def test_a_file_made_before_the_guard_picks_it_up_on_open(tmp_path: Path, table: str) -> None:
    path = _kb(tmp_path)
    raw = sqlite3.connect(path)
    raw.execute(f"DROP TRIGGER trg_{table}_no_replace_rowid")
    raw.commit()
    raw.close()

    Ontology.connect(path, clock=FixedClock(T0))

    names = {
        name
        for (name,) in sqlite3.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
    }
    assert f"trg_{table}_no_replace_rowid" in names
