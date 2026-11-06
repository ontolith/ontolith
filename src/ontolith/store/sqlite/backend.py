"""SQLite storage backend implementation.

Default storage adapter for Ontolith. Provides:
- Append-only entity and assertion storage
- Transaction management
- Query interface
- Vector search via sqlite-vec (ADR-0020)
"""

import json
import sqlite3
import struct
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar, cast

import sqlite_vec

from ontolith.core import Assertion, AssertionEvent, Clock, Entity, Namespace, SystemClock
from ontolith.core.errors import StorageError, ValidationError
from ontolith.govern.contradiction import Contradiction
from ontolith.govern.proposal import Proposal, ProposalEvent
from ontolith.identity import AdminEvent, ErasureMode, ErasureRecord, Principal, PrincipalCredential
from ontolith.schema import SchemaIR
from ontolith.store.base import DEFAULT_NAMESPACE, VECTOR_SCOPES
from ontolith.store.redaction import ErasureCounts, RedactionPlan
from ontolith.store.sqlite import migrations

_P = ParamSpec("_P")
_R = TypeVar("_R")

_RANGE_SQL_OPERATORS = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}
"""entities_where() operator name -> SQL comparison operator (KI-039)."""


def _pack_vector(vec: list[float]) -> bytes:
    """Serialize a vector for sqlite-vec's vec0 FLOAT[N] column format."""
    return struct.pack(f"{len(vec)}f", *vec)


def _like_escape(value: str) -> str:
    """Escape SQL LIKE wildcards so a `__contains` filter matches `value`
    literally, not as a LIKE pattern (KI-039). Paired with `ESCAPE '\\'` in
    the SQL and the value wrapped in `%...%` by the caller."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _synchronized(
    method: Callable[Concatenate["SQLiteBackend", _P], _R],
) -> Callable[Concatenate["SQLiteBackend", _P], _R]:
    """Serialize a method's connection access across threads (KI-023).

    Reentrant on the calling thread: a method called from inside an active
    ``transaction()`` block (which already holds the lock via ``begin()``)
    re-acquires without blocking. A different thread blocks until the lock
    is free, so the single shared connection is never touched concurrently.
    """

    @wraps(method)
    def wrapper(self: "SQLiteBackend", *args: _P.args, **kwargs: _P.kwargs) -> _R:
        """Acquire self._lock, call the wrapped method, then release it."""
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(Callable[Concatenate["SQLiteBackend", _P], _R], wrapper)


class SQLiteBackend:
    """SQLite implementation of StorageBackend.

    Schema follows SPEC §12.2:
    - entity table with ULID primary key
    - assertion table with ULID primary key
    - Bitemporal columns (asserted_at, valid_from, valid_to)
    - Status tracking for append-only invariant

    KI-066: `assertion_event`/`proposal_event`'s append-only invariant
    (SPEC §17) is backed here by three triggers per table — `BEFORE
    UPDATE`, `BEFORE DELETE`, and `BEFORE INSERT ... WHEN EXISTS(...)` —
    raising `sqlite3.IntegrityError` on any raw mutation attempt, not just
    the port surface exposing no update/delete method. The `BEFORE INSERT`
    trigger is what actually closes `INSERT OR REPLACE`: it is schema
    state (persisted in the file, enforced on every connection), unlike
    `PRAGMA recursive_triggers = ON` (also set below, as defense in depth)
    which is per-connection and does not by itself stop a second raw
    connection to the same file from reviving the REPLACE bypass. `DuckDBBackend`
    has no equivalent — see its own docstring.
    """

    def __init__(self, path: str | Path, *, clock: Clock | None = None) -> None:
        """Initialize SQLite backend.

        Args:
            path: Path to SQLite database file (created if doesn't exist)
            clock: Clock for timestamps (defaults to SystemClock)
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit mode (ADR-0010).
        # Each write auto-commits unless _in_transaction is True.
        # check_same_thread=False: an ASGI server (REST, interfaces/rest.py)
        # dispatches sync route handlers onto a worker threadpool, which is
        # a different OS thread than the one that constructed this backend
        # — stock sqlite3 blocks that regardless of whether the access is
        # ever actually concurrent. This flag only lifts that same-thread
        # check; it does not by itself serialize concurrent access — that is
        # what self._lock (below) does (KI-023).
        # timeout=5.0 (KI-084): 5.0 is already Python's own sqlite3 default
        # when this kwarg is omitted (verified directly), so this line is a
        # behavioral no-op by itself — pinning it explicitly rather than
        # inheriting a stdlib default is what makes it a deliberate,
        # documented value instead of an unexamined implicit one. It only
        # becomes load-bearing given the change below: begin() now issues
        # BEGIN IMMEDIATE, so SQLITE_BUSY (and its extended variants) is
        # reachable at begin() time and genuinely retries against this
        # timeout for up to 5s before raising — under the old deferred
        # BEGIN, a stale-snapshot-upgrade failure (SQLITE_BUSY_SNAPSHOT)
        # bypassed the busy handler entirely, so no busy_timeout value,
        # explicit or not, would have helped against the specific failure
        # this KI closes — see begin()'s own docstring for the full
        # explanation.
        self.conn = sqlite3.connect(
            str(self.path), timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        # ADR-0052: refuses (SchemaError) an existing file below
        # migrations.CURRENT_FORMAT_VERSION rather than silently applying
        # pending migrations — see require_current_format's own docstring.
        # Runs before every PRAGMA/DDL below, not just _create_schema() —
        # round-1 review found `PRAGMA journal_mode = WAL` (further down)
        # still ran ahead of this check, switching a stale file's on-disk
        # journal mode before refusing to open it, which the neighboring
        # "a stale file must not have any DDL touch it on this connect at
        # all" claim didn't actually hold for a session-level PRAGMA like
        # this one. Closes the connection on *any* failure here, not just
        # SchemaError (a corrupt/non-database file raises sqlite3.Error
        # instead) — a refused open shouldn't leak a live handle either way.
        try:
            migrations.require_current_format(self.conn.cursor(), path=self.path)
        except BaseException:
            self.conn.close()
            raise
        self.conn.execute("PRAGMA foreign_keys = ON")
        # KI-066 review: recursive_triggers defaults OFF, and SQLite only
        # fires a BEFORE DELETE trigger for an `INSERT OR REPLACE`
        # conflict-row removal when this is ON — without it, `INSERT OR
        # REPLACE INTO assertion_event ...` with an existing id silently
        # rewrites the row (including `actor`, laundering attribution)
        # instead of tripping trg_assertion_event_no_delete/
        # trg_proposal_event_no_delete below. Verified empirically: the
        # bypass reproduces with this OFF and is blocked with it ON.
        self.conn.execute("PRAGMA recursive_triggers = ON")
        # entities_where()'s __contains filter (KI-039) uses LIKE; SQLite's
        # default LIKE is ASCII-case-insensitive, DuckDB's is case-sensitive
        # — without this, the same .where(x__contains=...) call would
        # silently return different result sets per backend. Case-sensitive
        # matches DuckDB's default and is the less surprising choice for a
        # substring filter (matches Python's own `in` semantics).
        self.conn.execute("PRAGMA case_sensitive_like = ON")
        # SPEC §12.1 MUST: default backend uses WAL mode — readers don't
        # block behind writers, which matters for a long-lived MCP server
        # process handling concurrent tool calls. No-op (falls back to a
        # different mode) for in-memory/`:memory:` databases.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self._in_transaction: bool = False
        # Serializes all access to self.conn across threads (KI-023): the
        # connection and _in_transaction are shared mutable state that stock
        # sqlite3 does not protect once check_same_thread=False lifts the
        # same-thread check. RLock (not Lock): begin() holds it across
        # multiple public-method calls inside a transaction() block, each of
        # which re-acquires it via the @_synchronized decorator.
        self._lock = threading.RLock()
        self._clock: Clock = clock or SystemClock()
        self._create_schema()
        if self.erasure_mode() == "redaction":
            # Freed pages are overwritten, not just unlinked (ADR-0056 §4).
            self.conn.execute("PRAGMA secure_delete = ON")

    def _create_schema(self) -> None:
        """Create database schema if not exists."""
        cursor = self.conn.cursor()

        # Principal table (SPEC §8, ADR-0003, ADR-0009)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS principal (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('human', 'ai', 'service')),
                owner TEXT,
                auth_method TEXT NOT NULL CHECK(auth_method IN ('oidc', 'workload', 'apikey')),
                default_capability TEXT NOT NULL DEFAULT 'propose' CHECK(default_capability IN ('read', 'propose', 'write', 'review', 'admin')),
                trust_level INTEGER NOT NULL DEFAULT 0 CHECK(trust_level BETWEEN 0 AND 10),
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                CHECK (kind <> 'ai' OR owner IS NOT NULL),
                FOREIGN KEY(owner) REFERENCES principal(id)
            )
        """)

        # Principal credential table (ADR-0014) — hashed API-key tokens.
        # The raw token is never persisted, only its SHA-256 hash. A principal
        # may hold multiple concurrent active credentials (rotation = issue
        # new + revoke old, both explicit); revoked rows are kept, not deleted.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS principal_credential (
                id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                issued_by TEXT,
                revoked_by TEXT,
                FOREIGN KEY(principal_id) REFERENCES principal(id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_principal_credential_principal
            ON principal_credential(principal_id)
        """)

        # KI-060's `issued_by`/`revoked_by` columns are declared directly in
        # the CREATE TABLE above (current format shape) — a file that
        # predates them is refused before reaching this method at all
        # (require_current_format in __init__, ADR-0052) rather than
        # idempotently ALTER TABLE'd here on every connect. The historical
        # migration itself now lives in sqlite/migrations.py, applied only
        # by an explicit migrate_file() call.

        # Namespace registry table (SPEC §12.2, KI-022) — tracks namespaces
        # that have a schema applied or are the seeded default; NOT a
        # complete registry of every namespace string ever written to an
        # entity/assertion row (those remain free-text, unvalidated against
        # this table — see ADR-0022's Update section for the deliberate
        # scope boundary). `metadata TEXT NOT NULL DEFAULT '{}'` deviates
        # from SPEC §12.2's literal nullable `metadata TEXT` — matches this
        # project's `principal` table convention and guarantees
        # `_row_to_namespace`'s `json.loads` never sees NULL.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS namespace (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)
        self._ensure_namespace_registered(cursor, DEFAULT_NAMESPACE)

        # Schema version table (SPEC §12.2, §6.4)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                namespace TEXT NOT NULL,
                version INTEGER NOT NULL,
                definition TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                PRIMARY KEY (namespace, version)
            )
        """)

        # Entity table (SPEC §12.2)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entity (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                concept TEXT NOT NULL,
                natural_key TEXT,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                UNIQUE(namespace, concept, natural_key),
                FOREIGN KEY(created_by) REFERENCES principal(id)
            )
        """)

        # Assertion table (SPEC §12.2)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS assertion (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                value_kind TEXT NOT NULL CHECK(value_kind IN ('literal', 'ref')),
                value_type TEXT,
                value_lit TEXT,
                value_ref TEXT,
                author TEXT NOT NULL,
                acting_as TEXT,
                source TEXT,
                confidence REAL CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
                rationale TEXT,
                model TEXT,
                asserted_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'retracted', 'flagged')),
                proposal_id TEXT,
                supersedes TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(subject) REFERENCES entity(id),
                FOREIGN KEY(author) REFERENCES principal(id)
            )
        """)

        # Assertion event table — append-only audit log for status
        # mutations (supersession, flagging, retraction, reactivation).
        # The assertion row itself only carries current status; this table
        # makes each transition independently attributable and timestamped.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS assertion_event (
                id TEXT PRIMARY KEY,
                assertion_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('superseded', 'flagged', 'retracted', 'reactivated')),
                at TEXT NOT NULL,
                successor_id TEXT,
                FOREIGN KEY(assertion_id) REFERENCES assertion(id),
                FOREIGN KEY(actor) REFERENCES principal(id),
                FOREIGN KEY(successor_id) REFERENCES assertion(id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_event_assertion
            ON assertion_event(assertion_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_event_successor
            ON assertion_event(successor_id)
        """)

        # KI-066: immutability was previously enforced only by
        # StorageBackend's port surface exposing no update/delete method —
        # a convention any code holding this raw connection could bypass.
        # These triggers make it a store-level guarantee instead (SPEC
        # §17: "the audit trail MUST NOT be mutable"). No DuckDB
        # equivalent exists — DuckDB has no CREATE TRIGGER support at all
        # (verified against 1.5.4; see DuckDBBackend's own docstring).
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_assertion_event_no_update
            BEFORE UPDATE ON assertion_event
            BEGIN
                SELECT RAISE(ABORT, 'assertion_event is append-only: UPDATE is not permitted');
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_assertion_event_no_delete
            BEFORE DELETE ON assertion_event
            BEGIN
                SELECT RAISE(ABORT, 'assertion_event is append-only: DELETE is not permitted');
            END
        """)

        # Round-2 review finding: `PRAGMA recursive_triggers` (below, in
        # __init__) is per-*connection* state, not persisted in the
        # database file — a second raw connection to the same file opens
        # with it OFF regardless, silently reviving the `INSERT OR
        # REPLACE` bypass the pragma was meant to close, with no
        # privilege escalation needed (just `backend.path`, a public
        # attribute). This trigger closes it durably at the schema level:
        # any `INSERT` whose id already exists is exactly what a REPLACE
        # conflict-resolution does, regardless of pragma state or which
        # connection issues it. The pragma is kept anyway (defense in
        # depth, and it produces a clearer error for a plain `UPDATE`-
        # shaped conflict-row removal specifically) but is no longer
        # load-bearing for REPLACE.
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_assertion_event_no_replace
            BEFORE INSERT ON assertion_event
            WHEN EXISTS(SELECT 1 FROM assertion_event WHERE id = NEW.id)
            BEGIN
                SELECT RAISE(ABORT, 'assertion_event is append-only: REPLACE is not permitted');
            END
        """)

        # ADR-0056: the erasure mode and the record of redaction being
        # enabled (and, later, of each erasure). Both are append-only at the
        # store level, like the other audit tables (KI-066), so no raw
        # connection can edit them. The mode is the newest `erasure_mode`
        # row; no row means `strict`. A `strict` row is refused, which makes
        # the switch one-way, and a `redaction` row needs the `enabled`
        # erasure_record with the same id. Both tables are WITHOUT ROWID: the
        # REPLACE trigger keys on `id`, and a REPLACE that collided on
        # `rowid` instead would delete a row without firing the delete
        # trigger (the connection-level `recursive_triggers` pragma that would
        # otherwise stop it is not stored in the file).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS kb_setting (
                id TEXT PRIMARY KEY,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                at TEXT NOT NULL,
                actor TEXT NOT NULL
            ) WITHOUT ROWID
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS erasure_record (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('enabled', 'erased')),
                actor TEXT NOT NULL,
                at TEXT NOT NULL,
                subject TEXT,
                reason TEXT,
                detail TEXT
            ) WITHOUT ROWID
        """)
        for table in ("kb_setting", "erasure_record"):
            cursor.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only: UPDATE is not permitted');
                END
            """)
            cursor.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only: DELETE is not permitted');
                END
            """)
            # `table` iterates the two fixed names above, never caller input.
            cursor.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trg_{table}_no_replace
                BEFORE INSERT ON {table}
                WHEN EXISTS(SELECT 1 FROM {table} WHERE id = NEW.id)
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only: REPLACE is not permitted');
                END
            """)  # nosec B608
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_kb_setting_erasure_mode_needs_record
            BEFORE INSERT ON kb_setting
            WHEN NEW.key = 'erasure_mode' AND NEW.value = 'redaction'
                AND NOT EXISTS(
                    SELECT 1 FROM erasure_record WHERE id = NEW.id AND kind = 'enabled'
                )
            BEGIN
                SELECT RAISE(ABORT, 'erasure_mode needs a matching enabled erasure_record');
            END
        """)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_kb_setting_erasure_mode_one_way
            BEFORE INSERT ON kb_setting
            WHEN NEW.key = 'erasure_mode' AND NEW.value <> 'redaction'
            BEGIN
                SELECT RAISE(ABORT, 'erasure_mode is one-way: only redaction can be recorded');
            END
        """)

        # Proposal table (SPEC §9.1)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS proposal (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                author TEXT NOT NULL,
                acting_as TEXT,
                state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN (
                    'draft', 'submitted', 'auto_accepted',
                    'require_review', 'under_review',
                    'accepted', 'rejected', 'changes_requested'
                )),
                created_at TEXT NOT NULL,
                decided_at TEXT,
                policy_reason TEXT,
                reviewers TEXT NOT NULL DEFAULT '[]',
                payload TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(author) REFERENCES principal(id)
            )
        """)

        # KI-078's `reviewers` column is declared directly in the CREATE
        # TABLE above, same reasoning as issued_by/revoked_by just above —
        # see that comment and ADR-0052.

        # Proposal event table (SPEC §9.4) — structured review actions.
        # Scoped to accept/reject/request_changes/assign, the four review
        # actions that exist as Ontology methods; comment is not implemented
        # yet (see ProposalEvent docstring).
        #
        # No CHECK on `type` — matches SPEC §12.2's own DDL (`type TEXT NOT
        # NULL`, no constraint) and this project's "validate at edges, trust
        # within" boundary (Pydantic's `ProposalEvent.type` Literal already
        # enforces the vocabulary at construction time). A CHECK here would
        # also be a recurring migration hazard: `CREATE TABLE IF NOT EXISTS`
        # never widens an already-created table's constraint, so adding one
        # more accepted value (as `request_changes` needed to) would have
        # silently broken every pre-existing database file rather than the
        # new database this comment is protecting.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS proposal_event (
                id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                type TEXT NOT NULL,
                detail TEXT,
                at TEXT NOT NULL,
                FOREIGN KEY(proposal_id) REFERENCES proposal(id),
                FOREIGN KEY(actor) REFERENCES principal(id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_proposal_event_proposal
            ON proposal_event(proposal_id)
        """)

        # KI-066: same store-level immutability guarantee as
        # assertion_event above (SPEC §17).
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_proposal_event_no_update
            BEFORE UPDATE ON proposal_event
            BEGIN
                SELECT RAISE(ABORT, 'proposal_event is append-only: UPDATE is not permitted');
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_proposal_event_no_delete
            BEFORE DELETE ON proposal_event
            BEGIN
                SELECT RAISE(ABORT, 'proposal_event is append-only: DELETE is not permitted');
            END
        """)

        # Same durable REPLACE-bypass close as trg_assertion_event_no_replace
        # above — see its comment for why the pragma alone isn't enough.
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_proposal_event_no_replace
            BEFORE INSERT ON proposal_event
            WHEN EXISTS(SELECT 1 FROM proposal_event WHERE id = NEW.id)
            BEGIN
                SELECT RAISE(ABORT, 'proposal_event is append-only: REPLACE is not permitted');
            END
        """)

        # Admin event table (KI-060, SPEC §17) — append-only audit log for
        # the highest-stakes actions in the system: principal creation,
        # schema application, plugin registration. `target` is free text,
        # not a foreign key (see AdminEvent's own docstring for why one
        # column can't reference three different row types). `actor` is
        # also not a foreign key, unlike assertion_event/proposal_event's
        # — apply_schema/register_plugin always validate `actor` via
        # require_admin before this event is ever recorded, but
        # create_principal's own `author` is optional and deliberately
        # unvalidated (ADR-0022: create_principal itself has no built-in
        # capability check), so a FOREIGN KEY here would reject a
        # create_principal event whose caller supplied a bogus author
        # string rather than just recording it, unlike every other write
        # path in this file. Immutability enforced the same way as
        # assertion_event/proposal_event (KI-066) — three triggers per
        # table, `recursive_triggers` already ON from __init__ above.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_event (
                id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN (
                    'create_principal', 'apply_schema', 'register_plugin'
                )),
                target TEXT NOT NULL,
                at TEXT NOT NULL,
                detail TEXT
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_admin_event_actor
            ON admin_event(actor)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_admin_event_target
            ON admin_event(target)
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_admin_event_no_update
            BEFORE UPDATE ON admin_event
            BEGIN
                SELECT RAISE(ABORT, 'admin_event is append-only: UPDATE is not permitted');
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_admin_event_no_delete
            BEFORE DELETE ON admin_event
            BEGIN
                SELECT RAISE(ABORT, 'admin_event is append-only: DELETE is not permitted');
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_admin_event_no_replace
            BEFORE INSERT ON admin_event
            WHEN EXISTS(SELECT 1 FROM admin_event WHERE id = NEW.id)
            BEGIN
                SELECT RAISE(ABORT, 'admin_event is append-only: REPLACE is not permitted');
            END
        """)

        # Contradiction table (SPEC §10.3)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contradiction (
                id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open', 'resolved')),
                member_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                raised_by TEXT,
                resolved_by TEXT,
                resolved_at TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_contradiction_open
            ON contradiction(namespace, subject, predicate, state)
        """)

        # Indexes (SPEC §12.2)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_spo
            ON assertion(namespace, subject, predicate, status)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_subj
            ON assertion(namespace, subject)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_time
            ON assertion(asserted_at)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_valid
            ON assertion(valid_from, valid_to)
        """)

        # idx_assertion_spo above leads with namespace, which every query
        # leaves unconstrained (single-namespace today), making it unusable
        # for the actual filter shapes in assertions()/entities_where() —
        # confirmed via EXPLAIN QUERY PLAN (full table SCAN, not SEARCH).
        # These two match the real WHERE clauses without requiring namespace.
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_subj_pred_status
            ON assertion(subject, predicate, status)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_pred_value
            ON assertion(predicate, value_lit, status)
        """)

        # Companion to idx_assertion_pred_value for relation (value_ref)
        # filters in entities_where() (KI-030). Kept as a separate index
        # rather than adding value_ref to idx_assertion_pred_value: SQLite's
        # planner won't reliably pick a single (predicate, value_lit OR
        # value_ref, status) index for an OR-shaped predicate, so
        # entities_where() issues two seekable point queries (UNION ALL)
        # instead — one per index — confirmed via EXPLAIN QUERY PLAN.
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_assertion_pred_ref
            ON assertion(predicate, value_ref, status)
        """)

        # Tracks the embedding dimension established per scope (ADR-0020).
        # vec0 virtual tables (vector_{scope}) are created lazily, on first
        # vector_upsert for that scope, once the dimension is known — this
        # table lets vector_upsert/vector_search validate dimension without
        # introspecting vec0's own DDL.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vector_scope (
                scope TEXT PRIMARY KEY,
                dim INTEGER NOT NULL
            )
        """)

        # Maps our caller-facing TEXT (scope, id) to a vec0 table's internal
        # rowid. vec0 (this sqlite-vec version) doesn't support DELETE/UPDATE
        # by a TEXT primary key column (confirmed empirically — only rowid-
        # keyed DELETE works), so vector_{scope} tables are rowid-only and
        # this table is the id<->rowid index that makes upsert/lookup by id
        # possible.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vector_id_map (
                scope TEXT NOT NULL,
                id TEXT NOT NULL,
                vec_rowid INTEGER NOT NULL,
                PRIMARY KEY (scope, id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_vector_id_map_rowid
            ON vector_id_map(scope, vec_rowid)
        """)

        self.conn.commit()

    def begin(self) -> None:
        """Begin an explicit transaction (ADR-0010, KI-084).

        Acquires self._lock (KI-023) — held across every subsequent
        @_synchronized call until commit()/rollback() releases it, so no
        other thread's operation can interleave with this transaction.
        Only serializes *this process*'s own threads — see KI-084's
        `docs/adr/ADR-0001-storage-default.md` update for the
        cross-process constraint this alone doesn't cover.

        ``BEGIN IMMEDIATE``, not a plain deferred ``BEGIN`` (KI-084): a
        deferred transaction takes its read snapshot lazily, on first
        statement. `assert_literal`/`assert_ref`'s own conflict-routing
        read (SPEC §10) and `propose`/`propose_ref`'s auto-accept branch,
        `accept_proposal`, and `resubmit`'s auto-accept branch (via
        `_replay_proposal_operations`) all do that read *inside* the
        `transaction()` block this method opens — so a deferred `BEGIN`
        let a concurrent writer (a second OS process; `self._lock` above
        only protects this process's own threads) commit between that read
        and this connection's own later write. The resulting write then
        hit `SQLITE_BUSY_SNAPSHOT` — a stale-snapshot-upgrade failure
        SQLite deliberately never routes through the busy handler, so it
        failed immediately no matter how long `busy_timeout` (set in
        `__init__`) allowed. `BEGIN IMMEDIATE` claims the write lock right
        here, before any read this transaction goes on to do, so a
        concurrent writer is instead serialized behind it — blocked and
        retried by the busy handler, same as any other reachable
        `SQLITE_BUSY`, for up to `busy_timeout` before genuinely failing.

        This is not a blanket claim that every `Ontology` write is now
        cross-process-safe — see KI-084's `docs/known-issues.md` entry and
        its own KI-092 follow-up for exactly which write paths this does
        and doesn't reach (several either write outside any `transaction()`
        block at all, or, per the already-resolved KI-035, evaluate policy
        against a read taken before the transaction opens).

        Trade-off worth knowing (KI-084 review): `self._lock` is acquired
        *before* the `BEGIN IMMEDIATE` call below, so while this call is
        parked in SQLite's busy handler waiting out a cross-process writer,
        every other `@_synchronized` call on this process — reads included
        — blocks behind it too, for up to the full `busy_timeout`. This
        can't be avoided by acquiring the lock later: only one Python
        thread may safely touch the single shared `self.conn` at a time
        regardless of which statement is running, so narrowing the lock's
        span here would just reopen KI-023 (two threads issuing statements
        on one connection concurrently) instead. The WAL claim elsewhere in
        this file ("readers don't block behind writers") holds at the
        SQLite level; it does not hold at this process's own read
        availability once a `begin()` here is genuinely contended by
        another process. See KI-084's `docs/adr/ADR-0001-storage-default.md`
        update for the deployment-facing version of this same trade-off.
        """
        self._lock.acquire()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            self._lock.release()
            # SQLITE_BUSY and its extended variants (SQLITE_BUSY_SNAPSHOT,
            # SQLITE_BUSY_RECOVERY, SQLITE_BUSY_TIMEOUT) all share primary
            # code 5 in their low byte (extended code & 0xFF == primary
            # code) — Python's sqlite3 does surface the extended code on
            # `.sqlite_errorcode` (verified directly: a snapshot-upgrade
            # failure reports 517, not just 5). `BEGIN IMMEDIATE` above
            # means `SQLITE_BUSY_SNAPSHOT` specifically can no longer occur
            # at *this* call site (the write lock is claimed before any
            # read, so there is no stale snapshot left to upgrade here) —
            # but the masking itself stays as defense-in-depth: it's still
            # what's needed to catch `SQLITE_BUSY_RECOVERY`/`_TIMEOUT`
            # should either become reachable here, and to not silently stop
            # matching if a future SQLite/Python change alters which
            # variant this specific contention surfaces as. Distinguishable
            # from other begin() failures (a lock this connection's own
            # busy_timeout couldn't clear within its window) rather than
            # folded into the same generic message a non-transient failure
            # below would get — still a StorageError (redacted at every
            # interface boundary, KI-083's own precedent for why 5xx
            # messages carry real detail only in server-side logs, never in
            # the response), but a caller reading logs can now tell "this
            # was contention, safe to retry the whole operation" from
            # "something is actually broken" without guessing from the raw
            # sqlite3 message.
            code = getattr(e, "sqlite_errorcode", None)
            if code is not None and code & 0xFF == sqlite3.SQLITE_BUSY:
                raise StorageError(
                    f"Transaction start failed due to lock contention (safe to retry): {e}"
                ) from e
            raise StorageError(f"Failed to begin transaction: {e}") from e
        except sqlite3.Error as e:
            self._lock.release()
            raise StorageError(f"Failed to begin transaction: {e}") from e
        self._in_transaction = True

    def commit(self) -> None:
        """Commit the current explicit transaction.

        Releases self._lock only on success. A failed commit leaves the
        transaction (and the lock) open: transaction()'s except block calls
        rollback() next, which is then the sole path that releases the
        lock — releasing here too on failure would double-release it (the
        lock is not reentrant-safe against being released twice), raising a
        RuntimeError that masks the real StorageError and leaves
        _in_transaction stuck True.
        """
        try:
            self.conn.commit()
        except sqlite3.Error as e:
            raise StorageError(f"Failed to commit transaction: {e}") from e
        self._in_transaction = False
        self._lock.release()

    def rollback(self) -> None:
        """Rollback the current explicit transaction. Always releases self._lock.

        Unlike commit(), this always resolves the transaction (successful
        or not) — it's the terminal cleanup path, including when called
        after a failed commit() (which deliberately did not release the
        lock itself, see commit()'s docstring). _in_transaction is reset
        unconditionally too, even if the underlying rollback itself fails:
        leaving it True after the lock is released would let a future
        caller believe it must skip autocommit for a transaction no one
        will ever commit or roll back again.
        """
        try:
            self.conn.rollback()
        except sqlite3.Error as e:
            raise StorageError(f"Failed to rollback transaction: {e}") from e
        finally:
            self._in_transaction = False
            self._lock.release()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Context manager for atomic multi-write transactions (ADR-0010).

        Usage:
            with backend.transaction():
                backend.put_entity(entity)
                backend.put_assertion(assertion)
        """
        self.begin()
        try:
            yield
            self.commit()
        except Exception:
            self.rollback()
            raise

    @_synchronized
    def put_principal(self, principal: Principal) -> None:
        """Persist a principal.

        Args:
            principal: Principal to persist

        Raises:
            StorageError: If persistence fails
        """
        import json

        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO principal (id, kind, owner, auth_method, default_capability, trust_level, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    principal.id,
                    principal.kind,
                    principal.owner,
                    principal.auth_method,
                    principal.default_capability,
                    principal.trust_level,
                    principal.created_at.isoformat(),
                    json.dumps(principal.metadata),
                ),
            )
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise StorageError(f"Principal conflict (id={principal.id}): {e}") from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist principal (id={principal.id}): {e}") from e

    @_synchronized
    def get_principal(self, principal_id: str) -> Principal | None:
        """Retrieve a principal by ID.

        Args:
            principal_id: Principal ID to retrieve

        Returns:
            Principal if found, None otherwise
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM principal WHERE id = ?",
            (principal_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_principal(row)

    @_synchronized
    def list_principals(self) -> list[Principal]:
        """List all principals (KI-022).

        Returns:
            All principals, most recently created first
        """
        cursor = self.conn.cursor()
        # created_at DESC for most-recent-first, same as
        # get_credentials_for_principal. The id DESC tiebreak means
        # something different here, though: credential ids are ULIDs
        # (id DESC ~= recency), but principal ids are user-supplied
        # emails/slugs — id DESC is just a deterministic lexical tiebreak
        # for same-timestamp rows, not a recency proxy.
        cursor.execute("SELECT * FROM principal ORDER BY created_at DESC, id DESC")
        return [self._row_to_principal(row) for row in cursor.fetchall()]

    @_synchronized
    def list_namespaces(self) -> list[Namespace]:
        """List all registered namespaces (SPEC §12.2, KI-022).

        Returns:
            All namespaces, most recently created first
        """
        cursor = self.conn.cursor()
        # id DESC tiebreak is purely lexical (namespace ids are slugs, not
        # ULIDs) — same convention as list_principals, not a recency proxy.
        cursor.execute("SELECT * FROM namespace ORDER BY created_at DESC, id DESC")
        return [self._row_to_namespace(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_namespace(row: sqlite3.Row) -> Namespace:
        """Deserialize a `namespace` table row into a Namespace."""
        import json

        return Namespace(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            metadata=json.loads(row["metadata"]),
        )

    def _ensure_namespace_registered(self, cursor: sqlite3.Cursor, namespace: str) -> None:
        """Idempotently register a namespace in the registry, if not already present.

        Read-then-maybe-write rather than an unconditional `INSERT OR
        IGNORE`: an unconditional insert attempt takes SQLite's write lock
        even when the row already exists, which would turn every read-only
        backend construction (e.g. reconnecting just to list namespaces)
        into a blocking write — able to raise a raw, unmapped
        `sqlite3.OperationalError` against a database another connection
        is mid-write on, instead of needing no write at all. `INSERT OR
        IGNORE` is kept for the write path itself, to stay safe against a
        genuine race between the SELECT and the INSERT.

        Does not commit — caller controls transaction/commit timing.
        """
        cursor.execute("SELECT 1 FROM namespace WHERE id = ?", (namespace,))
        if cursor.fetchone() is not None:
            return
        cursor.execute(
            "INSERT OR IGNORE INTO namespace (id, created_at, metadata) VALUES (?, ?, ?)",
            (namespace, self._clock.now().isoformat(), "{}"),
        )

    @staticmethod
    def _row_to_principal(row: sqlite3.Row) -> Principal:
        """Deserialize a `principal` table row into a Principal."""
        import json

        return Principal(
            id=row["id"],
            kind=row["kind"],
            owner=row["owner"],
            auth_method=row["auth_method"],
            default_capability=row["default_capability"],
            trust_level=row["trust_level"],
            created_at=datetime.fromisoformat(row["created_at"]),
            metadata=json.loads(row["metadata"]),
        )

    @_synchronized
    def put_credential(self, credential: PrincipalCredential) -> None:
        """Persist a principal credential (hashed API-key token).

        Args:
            credential: PrincipalCredential to persist (token_hash, never the
                raw token)

        Raises:
            StorageError: If persistence fails
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO principal_credential
                    (id, principal_id, token_hash, created_at, revoked_at, issued_by, revoked_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential.id,
                    credential.principal_id,
                    credential.token_hash,
                    credential.created_at.isoformat(),
                    credential.revoked_at.isoformat() if credential.revoked_at else None,
                    credential.issued_by,
                    credential.revoked_by,
                ),
            )
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise StorageError(f"Credential conflict (id={credential.id}): {e}") from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist credential (id={credential.id}): {e}") from e

    @_synchronized
    def get_principal_by_token_hash(self, token_hash: str) -> Principal | None:
        """Resolve a principal via a credential's token hash.

        Only unrevoked credentials resolve. This is the sole read path used
        for MCP authentication — it never trusts a caller-supplied principal
        ID directly.

        Args:
            token_hash: SHA-256 hash of the raw bearer token

        Returns:
            Principal if the hash matches an active (unrevoked) credential,
            None otherwise
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT p.* FROM principal p
            JOIN principal_credential c ON c.principal_id = p.id
            WHERE c.token_hash = ? AND c.revoked_at IS NULL
            """,
            (token_hash,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_principal(row)

    @_synchronized
    def get_credential(self, credential_id: str) -> PrincipalCredential | None:
        """Retrieve a credential by ID (never exposes the raw token or hash to callers).

        Args:
            credential_id: Credential ID to retrieve

        Returns:
            PrincipalCredential if found, None otherwise
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM principal_credential WHERE id = ?",
            (credential_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_credential(row)

    @_synchronized
    def get_credentials_for_principal(self, principal_id: str) -> list[PrincipalCredential]:
        """List all credentials (active and revoked) issued to a principal.

        Args:
            principal_id: Principal to list credentials for

        Returns:
            Credentials for this principal, most recently issued first
        """
        cursor = self.conn.cursor()
        # id DESC tiebreaks two credentials issued at the same timestamp
        # (coarse/injected Clock) deterministically, so this listing has a
        # stable, reproducible order (ids are monotonically assigned).
        # issue_token() itself returns its new credential's id directly
        # (KI-024) rather than relying on this ordering to recover it.
        cursor.execute(
            "SELECT * FROM principal_credential WHERE principal_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (principal_id,),
        )
        return [self._row_to_credential(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_credential(row: sqlite3.Row) -> PrincipalCredential:
        """Deserialize a `principal_credential` table row into a PrincipalCredential."""
        return PrincipalCredential(
            id=row["id"],
            principal_id=row["principal_id"],
            token_hash=row["token_hash"],
            created_at=datetime.fromisoformat(row["created_at"]),
            revoked_at=datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None,
            issued_by=row["issued_by"],
            revoked_by=row["revoked_by"],
        )

    @_synchronized
    def revoke_credential(self, credential_id: str, revoked_at: datetime, revoked_by: str) -> None:
        """Mark a credential as revoked. Idempotent-safe: re-revoking an
        already-revoked credential is a true no-op, not a silent
        re-stamp — it doesn't overwrite `revoked_by`/`revoked_at` with a
        second caller's values (KI-060: that would launder the first
        revocation's real attribution).

        Args:
            credential_id: Credential to revoke
            revoked_at: Timestamp of revocation
            revoked_by: Principal ID of the admin performing the revocation

        Raises:
            StorageError: If the credential is not found
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE principal_credential SET revoked_at = ?, revoked_by = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (revoked_at.isoformat(), revoked_by, credential_id),
        )
        if cursor.rowcount == 0:
            exists = cursor.execute(
                "SELECT 1 FROM principal_credential WHERE id = ?", (credential_id,)
            ).fetchone()
            if exists is None:
                raise StorageError(f"Credential not found: {credential_id}")
            # Already revoked - no-op, first revocation's attribution stands.
        if not self._in_transaction:
            self.conn.commit()

    @_synchronized
    def erasure_mode(self) -> ErasureMode:
        """The stored erasure mode (ADR-0056); `strict` when never recorded."""
        try:
            row = self.conn.execute(
                "SELECT 1 FROM kb_setting WHERE key = 'erasure_mode' AND value = 'redaction'"
            ).fetchone()
        except sqlite3.Error as e:
            raise StorageError(f"Failed to read the erasure mode: {e}") from e
        return "redaction" if row is not None else "strict"

    @_synchronized
    def enable_redaction(self, record: ErasureRecord, *, require_empty: bool = False) -> None:
        """Store the mode `redaction` and append `record`, atomically."""
        if record.kind != "enabled":
            raise ValidationError(
                f"enable_redaction needs an 'enabled' record, got {record.kind!r}"
            )
        try:
            with self.transaction():
                if self.erasure_mode() == "redaction":
                    return
                if require_empty and not self._is_empty():
                    raise ValidationError(
                        "This knowledge base already has data and is in strict mode; "
                        "use enable_redaction() to move it to redaction mode"
                    )
                self._insert_erasure_record(record)
                self.conn.execute(
                    "INSERT INTO kb_setting (id, key, value, at, actor) "
                    "VALUES (?, 'erasure_mode', 'redaction', ?, ?)",
                    (record.id, record.at.isoformat(), record.actor),
                )
            self.conn.execute("PRAGMA secure_delete = ON")
        except sqlite3.Error as e:
            raise StorageError(f"Failed to enable redaction: {e}") from e

    def _is_empty(self) -> bool:
        """True when no principal, entity, assertion, proposal or schema exists."""
        return all(
            self.conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None  # noqa: S608  # nosec B608
            for table in ("principal", "entity", "assertion", "proposal", "schema_version")
        )

    def _insert_erasure_record(self, record: ErasureRecord) -> None:
        self.conn.execute(
            "INSERT INTO erasure_record (id, kind, actor, at, subject, reason, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.kind,
                record.actor,
                record.at.isoformat(),
                record.subject,
                record.reason,
                record.detail,
            ),
        )

    @_synchronized
    def apply_redaction(self, plan: RedactionPlan) -> ErasureCounts:
        """Apply an erasure plan inside the caller's open transaction (ADR-0056 §3).

        The caller opens the transaction, builds the plan from reads made
        inside it, and commits, so nothing can change between planning and
        applying. Call `compact()` after the commit to remove the bytes.
        """
        if self.erasure_mode() != "redaction":
            raise ValidationError(
                "This knowledge base is in strict mode; enable redaction before erasing"
            )
        if not self._in_transaction:
            raise ValidationError("apply_redaction must run inside an open transaction")
        record = plan.record
        marker = json.dumps(
            {
                "redacted": {
                    "erasure_id": record.id,
                    "by": record.actor,
                    "at": record.at.isoformat(),
                }
            }
        )
        at = record.at.isoformat()
        rejected = 0
        try:
            cur = self.conn.cursor()
            for assertion_id in plan.assertion_ids:
                cur.execute(
                    "UPDATE assertion SET value_lit = NULL, source = NULL, "
                    "rationale = NULL, metadata = ? "
                    "WHERE id = ? AND subject = ? AND value_kind = 'literal'",
                    (marker, assertion_id, plan.entity_id),
                )
                if cur.rowcount != 1:
                    raise StorageError(
                        f"Assertion {assertion_id} is not a literal of {plan.entity_id}"
                    )
            for event in plan.retractions:
                cur.execute(
                    "UPDATE assertion SET status = 'retracted', "
                    "valid_to = COALESCE(valid_to, ?) WHERE id = ?",
                    (event.at.isoformat(), event.assertion_id),
                )
                self.put_assertion_event(event)
            if plan.redact_natural_key:
                cur.execute("UPDATE entity SET natural_key = NULL WHERE id = ?", (plan.entity_id,))
            for item in plan.proposals:
                cur.execute(
                    "UPDATE proposal SET payload = ? WHERE id = ?",
                    (json.dumps(item.payload), item.proposal_id),
                )
                if item.reject_event is not None:
                    cur.execute(
                        "UPDATE proposal SET state = 'rejected', decided_at = ?, "
                        "policy_reason = 'erased' WHERE id = ?",
                        (item.reject_event.at.isoformat(), item.proposal_id),
                    )
                    self.put_proposal_event(item.reject_event)
                    rejected += 1
            contradiction_marker = json.dumps({"erased": json.loads(marker)["redacted"]})
            closed = 0
            for contradiction_id in plan.contradiction_ids:
                was_open = cur.execute(
                    "SELECT 1 FROM contradiction WHERE id = ? AND state = 'open'",
                    (contradiction_id,),
                ).fetchone()
                closed += 1 if was_open else 0
                cur.execute(
                    "UPDATE contradiction SET "
                    "resolved_by = CASE WHEN state = 'open' THEN ? ELSE resolved_by END, "
                    "resolved_at = CASE WHEN state = 'open' THEN ? ELSE resolved_at END, "
                    "state = 'resolved', metadata = ? WHERE id = ?",
                    (record.actor, at, contradiction_marker, contradiction_id),
                )
            if plan.assertion_ids:
                self._remove_vectors({plan.entity_id, *plan.assertion_ids})
            counts = ErasureCounts(
                assertions=len(plan.assertion_ids),
                proposals_rejected=rejected,
                proposals_redacted=len(plan.proposals),
                contradictions_closed=closed,
            )
            self._insert_erasure_record(
                record.model_copy(update={"detail": counts.model_dump_json()})
            )
        except sqlite3.Error as e:
            raise StorageError(f"Failed to apply the erasure of {plan.entity_id}: {e}") from e
        return counts

    def _remove_vectors(self, ids: set[str]) -> None:
        """Drop `ids` from every vector scope by rebuilding the scope.

        vec0 keeps deleted vectors in its chunk tables, and a re-embedded
        text leaves its old embedding behind, so deleting by id is not enough:
        the virtual table is recreated from the survivors (ADR-0056 §4).
        """
        cur = self.conn.cursor()
        for scope_row in cur.execute("SELECT scope, dim FROM vector_scope").fetchall():
            scope, dim = scope_row["scope"], scope_row["dim"]
            self._validate_scope(scope)
            table = f"vector_{scope}"
            survivors: list[tuple[str, bytes]] = []
            for m in cur.execute(
                "SELECT id, vec_rowid FROM vector_id_map WHERE scope = ? ORDER BY vec_rowid",
                (scope,),
            ).fetchall():
                if m["id"] in ids:
                    continue
                blob = cur.execute(
                    f"SELECT embedding FROM {table} WHERE rowid = ?",  # nosec B608
                    (m["vec_rowid"],),
                ).fetchone()
                if blob is not None:
                    survivors.append((m["id"], blob[0]))
            cur.execute(f"DROP TABLE {table}")  # nosec B608
            cur.execute(f"CREATE VIRTUAL TABLE {table} USING vec0(embedding FLOAT[{int(dim)}])")  # nosec B608
            cur.execute("DELETE FROM vector_id_map WHERE scope = ?", (scope,))
            for vec_id, embedding in survivors:
                cur.execute(f"INSERT INTO {table}(embedding) VALUES (?)", (embedding,))  # nosec B608
                cur.execute(
                    "INSERT INTO vector_id_map (scope, id, vec_rowid) VALUES (?, ?, ?)",
                    (scope, vec_id, cur.lastrowid),
                )

    @_synchronized
    def compact(self) -> str | None:
        """Remove freed bytes from the database file and its WAL (ADR-0056 §4).

        Returns None when the file is clean, or a description of what is left:
        a WAL checkpoint cannot finish while another connection holds a read
        snapshot, and `VACUUM` needs free disk space and no open transaction.
        Safe to call again later; an erasure with nothing to change still
        compacts.
        """
        if self._in_transaction:
            raise ValidationError("compact cannot run inside an open transaction")
        try:
            self.conn.execute("PRAGMA secure_delete = ON")
            self.conn.execute("VACUUM")
            busy, log, done = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error as e:
            return f"compaction failed: {e}"
        if busy or log != done:
            return (
                "the write-ahead log could not be fully checkpointed because another "
                "connection is reading; erased bytes may remain in the database or -wal file until "
                "readers finish and erase_subject (or compact) is run again"
            )
        return None

    @_synchronized
    def erasure_records(self) -> list[ErasureRecord]:
        """Every erasure record, oldest first."""
        try:
            rows = self.conn.execute(
                "SELECT id, kind, actor, at, subject, reason, detail "
                "FROM erasure_record ORDER BY at, id"
            ).fetchall()
        except sqlite3.Error as e:
            raise StorageError(f"Failed to read erasure records: {e}") from e
        return [
            ErasureRecord(
                id=r[0],
                kind=r[1],
                actor=r[2],
                at=datetime.fromisoformat(r[3]),
                subject=r[4],
                reason=r[5],
                detail=r[6],
            )
            for r in rows
        ]

    @_synchronized
    def put_admin_event(self, event: AdminEvent) -> None:
        """Persist an append-only admin-action event (KI-060)."""
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO admin_event (id, actor, action, target, at, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.actor,
                    event.action,
                    event.target,
                    event.at.isoformat(),
                    event.detail,
                ),
            )
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise StorageError(f"Admin event conflict (id={event.id}): {e}") from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist admin event (id={event.id}): {e}") from e

    @_synchronized
    def get_admin_events(
        self, actor: str | None = None, target: str | None = None
    ) -> list[AdminEvent]:
        """Retrieve admin events, optionally filtered by actor or target, oldest first."""
        cursor = self.conn.cursor()
        query = "SELECT * FROM admin_event WHERE 1=1"
        params: list[str] = []
        if actor is not None:
            query += " AND actor = ?"
            params.append(actor)
        if target is not None:
            query += " AND target = ?"
            params.append(target)
        query += " ORDER BY at ASC, id ASC"
        cursor.execute(query, params)
        return [
            AdminEvent(
                id=row["id"],
                actor=row["actor"],
                action=row["action"],
                target=row["target"],
                at=datetime.fromisoformat(row["at"]),
                detail=row["detail"],
            )
            for row in cursor.fetchall()
        ]

    @_synchronized
    def put_entity(self, entity: Entity) -> None:
        """Persist an entity.

        Args:
            entity: Entity to persist

        Raises:
            StorageError: If persistence fails
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO entity (id, namespace, concept, natural_key, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entity.id,
                    entity.namespace,
                    entity.concept,
                    entity.natural_key,
                    entity.created_at.isoformat(),
                    entity.created_by,
                ),
            )
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise StorageError(f"Entity conflict: {e}") from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist entity: {e}") from e

    @_synchronized
    def put_assertion(self, assertion: Assertion) -> None:
        """Persist an assertion.

        Args:
            assertion: Assertion to persist

        Raises:
            StorageError: If persistence fails
        """
        import json

        # Map unified value field to value_lit/value_ref based on kind
        value_lit = assertion.value if assertion.value_kind == "literal" else None
        value_ref = assertion.value if assertion.value_kind == "ref" else None

        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO assertion (
                    id, namespace, subject, predicate,
                    value_kind, value_type, value_lit, value_ref,
                    author, acting_as, source, confidence, rationale, model,
                    asserted_at, valid_from, valid_to,
                    status, proposal_id, supersedes, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assertion.id,
                    assertion.namespace,
                    assertion.subject,
                    assertion.predicate,
                    assertion.value_kind,
                    assertion.value_type,
                    value_lit,
                    value_ref,
                    assertion.author,
                    assertion.acting_as,
                    assertion.source,
                    assertion.confidence,
                    assertion.rationale,
                    assertion.model,
                    assertion.asserted_at.isoformat(),
                    assertion.valid_from.isoformat() if assertion.valid_from else None,
                    assertion.valid_to.isoformat() if assertion.valid_to else None,
                    assertion.status,
                    assertion.proposal_id,
                    assertion.supersedes,
                    json.dumps(assertion.metadata),
                ),
            )
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise StorageError(
                f"Assertion conflict (id={assertion.id}, subject={assertion.subject}): {e}"
            ) from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist assertion (id={assertion.id}): {e}") from e

    @_synchronized
    def get_entity(self, entity_id: str) -> Entity | None:
        """Retrieve an entity by ID.

        Args:
            entity_id: Entity ID to retrieve

        Returns:
            Entity if found, None otherwise
        """
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM entity WHERE id = ?",
            (entity_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return Entity(
            id=row["id"],
            namespace=row["namespace"],
            concept=row["concept"],
            natural_key=row["natural_key"],
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
        )

    @_synchronized
    def get_entity_by_natural_key(
        self, namespace: str, concept: str, natural_key: str
    ) -> Entity | None:
        """Retrieve an entity by its unique (namespace, concept, natural_key) triple."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM entity WHERE namespace = ? AND concept = ? AND natural_key = ?",
            (namespace, concept, natural_key),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        return Entity(
            id=row["id"],
            namespace=row["namespace"],
            concept=row["concept"],
            natural_key=row["natural_key"],
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
        )

    @_synchronized
    def assertions(
        self,
        subject: str | None = None,
        predicate: str | None = None,
        status: str | None = "active",
        as_of_time: datetime | None = None,
        include_flagged: bool = False,
        include_history: bool = False,
    ) -> list[Assertion]:
        """Query assertions with optional filters.

        Args:
            subject: Filter by subject entity ID
            predicate: Filter by predicate
            status: Filter by current status (ignored when as_of_time is
                set). Defaults to "active"; pass status=None for every
                status.
            as_of_time: If set, applies bitemporal filter:
                asserted_at <= t AND valid_from <= t AND (valid_to IS NULL OR valid_to > t)
            include_flagged: When as_of_time is set, whether to include
                'flagged' assertions (excluded by default — a flagged
                assertion is disputed, not confirmed-valid; pass True for
                explicit audit/history views); ignored when as_of_time is
                None.
            include_history: When as_of_time is set, whether to opt back
                into seeing a 'retracted' assertion once its own retraction
                event's timestamp is <= as_of_time (excluded by default,
                ADR-0049/KI-095) — mirrors include_flagged's shape (KI-098);
                ignored when as_of_time is None.

        Returns:
            List of matching assertions
        """
        query = "SELECT * FROM assertion WHERE 1=1"
        params: list[str] = []

        if subject is not None:
            query += " AND subject = ?"
            params.append(subject)

        if predicate is not None:
            query += " AND predicate = ?"
            params.append(predicate)

        if as_of_time is not None:
            t_iso = as_of_time.isoformat()
            query += " AND asserted_at <= ?"
            params.append(t_iso)
            query += " AND (valid_from IS NULL OR valid_from <= ?)"
            params.append(t_iso)
            query += " AND (valid_to IS NULL OR valid_to > ?)"
            params.append(t_iso)
            if not include_flagged:
                # Flagged-at-t, not current status: a static conflict flags an
                # assertion permanently (no valid_to change), so using current
                # status here would hide it from as_of() queries for times
                # before the dispute existed. Reconstruct from the event log
                # instead — every flagged transition (including an assertion
                # born already-flagged) has a 'flagged' event, see
                # Ontology._apply_with_conflict_routing. Tiebreak on ae.id:
                # two events can share the same `at` under a clock that
                # hasn't advanced (e.g. flag-then-resolve in the same tick),
                # and `at` alone would make "last recorded wins"
                # nondeterministic. Under SequentialIdProvider/FixedIdProvider
                # (used in tests) id order matches recording order exactly;
                # under the production UlidProvider, id is monotonic across
                # milliseconds but not guaranteed within one, so same-`at`
                # AND same-millisecond ties are a residual (low-probability,
                # not exploitable) nondeterminism.
                query += """ AND COALESCE(
                    (SELECT ae.action FROM assertion_event ae
                     WHERE ae.assertion_id = assertion.id AND ae.at <= ?
                       AND ae.action IN ('flagged', 'reactivated')
                     ORDER BY ae.at DESC, ae.id DESC LIMIT 1),
                    'reactivated'
                ) != 'flagged'"""
                params.append(t_iso)
            # ADR-0049 (KI-095): same retraction-aware exclusion the
            # entities_where()/entities_meeting_confidence()/
            # entities_meeting_trust() family has — see entities_where()'s
            # comment for the full reasoning (KI-051 guarantees at most one
            # 'retracted' event per assertion, so unlike flagged/reactivated
            # above this needs no ORDER BY tiebreak: retraction is a
            # one-way terminal transition, never followed by another event).
            # include_history opts back out of this check, the same
            # mechanism the QueryBuilder-facing trio already uses (KI-098 —
            # this method had no such opt-out when ADR-0049 first shipped).
            if not include_history:
                query += (
                    " AND (status != 'retracted' OR EXISTS ("
                    "SELECT 1 FROM assertion_event ae"
                    " WHERE ae.assertion_id = assertion.id"
                    " AND ae.action = 'retracted' AND ae.at > ?"
                    "))"
                )
                params.append(t_iso)
        elif status is not None:
            query += " AND status = ?"
            params.append(status)

        cursor = self.conn.cursor()
        cursor.execute(query, params)

        return [self._row_to_assertion(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_assertion(row: sqlite3.Row) -> Assertion:
        """Deserialize an `assertion` table row into an Assertion."""
        import json

        # Reconstruct unified value from value_lit/value_ref
        value = row["value_lit"] if row["value_kind"] == "literal" else row["value_ref"]
        if value is None:
            # A redacted value (ADR-0056) is NULL in storage; the model keeps
            # `value: str` and reads it as empty. `Assertion.redacted` tells
            # the two apart.
            value = ""

        return Assertion(
            id=row["id"],
            namespace=row["namespace"],
            subject=row["subject"],
            predicate=row["predicate"],
            value_kind=row["value_kind"],
            value_type=row["value_type"],
            value=value,
            author=row["author"],
            acting_as=row["acting_as"],
            source=row["source"],
            confidence=row["confidence"],
            rationale=row["rationale"],
            model=row["model"],
            asserted_at=datetime.fromisoformat(row["asserted_at"]),
            valid_from=(datetime.fromisoformat(row["valid_from"]) if row["valid_from"] else None),
            valid_to=(datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None),
            status=row["status"],
            proposal_id=row["proposal_id"],
            supersedes=row["supersedes"],
            metadata=json.loads(row["metadata"]),
        )

    @_synchronized
    def get_assertion(self, assertion_id: str) -> Assertion | None:
        """Retrieve a single assertion by ID, regardless of status.

        Args:
            assertion_id: Assertion ID to retrieve

        Returns:
            Assertion if found, None otherwise
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM assertion WHERE id = ?", (assertion_id,))
        row = cursor.fetchone()
        return self._row_to_assertion(row) if row else None

    @_synchronized
    def set_assertion_status(
        self,
        assertion_id: str,
        status: str,
        valid_to: str | None = None,
    ) -> None:
        """Update assertion status and optionally close validity window.

        This is the ONLY allowed mutation on assertions (append-only invariant).

        Args:
            assertion_id: Assertion ID to update
            status: New status (superseded, retracted, flagged)
            valid_to: Optional validity end time (ISO format)

        Raises:
            StorageError: If update fails or assertion not found
        """
        try:
            cursor = self.conn.cursor()
            if valid_to is not None:
                cursor.execute(
                    "UPDATE assertion SET status = ?, valid_to = ? WHERE id = ?",
                    (status, valid_to, assertion_id),
                )
            else:
                cursor.execute(
                    "UPDATE assertion SET status = ? WHERE id = ?",
                    (status, assertion_id),
                )

            if cursor.rowcount == 0:
                raise StorageError(f"Assertion not found: {assertion_id}")
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.Error as e:
            raise StorageError(f"Failed to update assertion status (id={assertion_id}): {e}") from e

    @_synchronized
    def put_schema(self, schema: SchemaIR) -> None:
        """Persist a schema version.

        Also registers ``schema.namespace`` in the namespace registry if
        not already present (KI-022) — a namespace that only ever has a
        schema applied, never an entity, is still discoverable via
        `list_namespaces()`.

        Args:
            schema: Schema to persist

        Raises:
            StorageError: If persistence fails
        """
        import json

        try:
            cursor = self.conn.cursor()
            self._ensure_namespace_registered(cursor, schema.namespace)
            cursor.execute(
                """
                INSERT INTO schema_version (namespace, version, definition, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    schema.namespace,
                    schema.version,
                    json.dumps(schema.to_json()),
                    self._clock.now().isoformat(),
                ),
            )
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise StorageError(
                f"Schema conflict (namespace={schema.namespace}, version={schema.version}): {e}"
            ) from e
        except sqlite3.Error as e:
            raise StorageError(
                f"Failed to persist schema (namespace={schema.namespace}): {e}"
            ) from e

    @_synchronized
    def get_schema(self, namespace: str, version: int | None = None) -> SchemaIR | None:
        """Retrieve a schema version.

        Args:
            namespace: Namespace to query
            version: Specific version, or None for latest

        Returns:
            Schema if found, None otherwise
        """
        import json

        cursor = self.conn.cursor()

        if version is None:
            # Get latest version
            cursor.execute(
                """
                SELECT definition FROM schema_version
                WHERE namespace = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (namespace,),
            )
        else:
            # Get specific version
            cursor.execute(
                """
                SELECT definition FROM schema_version
                WHERE namespace = ? AND version = ?
                """,
                (namespace, version),
            )

        row = cursor.fetchone()
        if row is None:
            return None

        definition = json.loads(row["definition"])
        return SchemaIR.from_json(definition)

    @_synchronized
    def get_schema_at(self, namespace: str, at: datetime) -> SchemaIR | None:
        """Retrieve the schema version effective at a point in time (KI-019).

        Orders by applied_at (the actual "effective at" moment), with
        version as a tiebreak for same-timestamp rows under a coarse or
        injected Clock — not by version alone, so this stays correct even
        if a future write path ever persisted schema rows out of temporal
        order relative to their version numbers.
        """
        import json

        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT definition FROM schema_version
            WHERE namespace = ? AND applied_at <= ?
            ORDER BY applied_at DESC, version DESC
            LIMIT 1
            """,
            (namespace, at.isoformat()),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        definition = json.loads(row["definition"])
        return SchemaIR.from_json(definition)

    @_synchronized
    def entities(
        self,
        namespace: str | None = None,
        concept: str | None = None,
        as_of_time: datetime | None = None,
    ) -> list[Entity]:
        """Query entities with optional filters.

        Args:
            namespace: Filter by namespace
            concept: Filter by concept
            as_of_time: If set, exclude entities created after this time

        Returns:
            List of matching entities
        """
        query = "SELECT * FROM entity WHERE 1=1"
        params: list[str] = []

        if namespace is not None:
            query += " AND namespace = ?"
            params.append(namespace)

        if concept is not None:
            query += " AND concept = ?"
            params.append(concept)

        if as_of_time is not None:
            query += " AND created_at <= ?"
            params.append(as_of_time.isoformat())

        cursor = self.conn.cursor()
        cursor.execute(query, params)

        results = []
        for row in cursor.fetchall():
            results.append(
                Entity(
                    id=row["id"],
                    namespace=row["namespace"],
                    concept=row["concept"],
                    natural_key=row["natural_key"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    created_by=row["created_by"],
                )
            )

        return results

    @_synchronized
    def put_proposal(self, proposal: Proposal) -> None:
        """Persist a proposal."""
        import json

        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO proposal (id, namespace, author, acting_as, state,
                    created_at, decided_at, policy_reason, reviewers, payload, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.id,
                    proposal.namespace,
                    proposal.author,
                    proposal.acting_as,
                    proposal.state,
                    proposal.created_at.isoformat(),
                    proposal.decided_at.isoformat() if proposal.decided_at else None,
                    proposal.policy_reason,
                    json.dumps(proposal.reviewers),
                    json.dumps(proposal.payload),
                    json.dumps(proposal.metadata),
                ),
            )
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise StorageError(f"Proposal conflict (id={proposal.id}): {e}") from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist proposal (id={proposal.id}): {e}") from e

    @_synchronized
    def get_proposal(self, proposal_id: str) -> Proposal | None:
        """Retrieve a proposal by ID."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM proposal WHERE id = ?", (proposal_id,))
        row = cursor.fetchone()
        return self._row_to_proposal(row) if row else None

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> Proposal:
        import json

        return Proposal(
            id=row["id"],
            namespace=row["namespace"],
            author=row["author"],
            acting_as=row["acting_as"],
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
            decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
            policy_reason=row["policy_reason"],
            reviewers=json.loads(row["reviewers"]),
            payload=json.loads(row["payload"]),
            metadata=json.loads(row["metadata"]),
        )

    @_synchronized
    def proposals(self, state: str | None = None) -> list[Proposal]:
        """Query proposals, optionally filtered by state (SPEC §14.1)."""
        cursor = self.conn.cursor()
        if state is not None:
            cursor.execute(
                "SELECT * FROM proposal WHERE state = ? ORDER BY created_at DESC, id DESC",
                (state,),
            )
        else:
            cursor.execute("SELECT * FROM proposal ORDER BY created_at DESC, id DESC")
        return [self._row_to_proposal(row) for row in cursor.fetchall()]

    @_synchronized
    def update_proposal_state(
        self,
        proposal_id: str,
        state: str,
        decided_at: str | None = None,
        policy_reason: str | None = None,
    ) -> None:
        """Update proposal state after policy decision.

        policy_reason=None leaves the stored value unchanged (COALESCE), it
        does not clear it — see the port docstring for why.
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE proposal SET state = ?, decided_at = ?, "
                "policy_reason = COALESCE(?, policy_reason) WHERE id = ?",
                (state, decided_at, policy_reason, proposal_id),
            )
            if cursor.rowcount == 0:
                raise StorageError(f"Proposal not found: {proposal_id}")
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.Error as e:
            raise StorageError(f"Failed to update proposal (id={proposal_id}): {e}") from e

    @_synchronized
    def update_proposal_reviewers(self, proposal_id: str, reviewers: list[str]) -> None:
        """Replace a proposal's assigned reviewers (SPEC §9.4's `assign` action)."""
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE proposal SET reviewers = ? WHERE id = ?",
                (json.dumps(reviewers), proposal_id),
            )
            if cursor.rowcount == 0:
                raise StorageError(f"Proposal not found: {proposal_id}")
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.Error as e:
            raise StorageError(
                f"Failed to update proposal reviewers (id={proposal_id}): {e}"
            ) from e

    @_synchronized
    def put_proposal_event(self, event: ProposalEvent) -> None:
        """Persist a structured review-action event (SPEC §9.4)."""
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO proposal_event (id, proposal_id, actor, type, detail, at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.proposal_id,
                    event.actor,
                    event.type,
                    event.detail,
                    event.at.isoformat(),
                ),
            )
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise StorageError(f"Proposal event conflict (id={event.id}): {e}") from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist proposal event (id={event.id}): {e}") from e

    @_synchronized
    def get_proposal_events(self, proposal_id: str) -> list[ProposalEvent]:
        """Retrieve all review events for a proposal, oldest first."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM proposal_event WHERE proposal_id = ? ORDER BY at ASC",
            (proposal_id,),
        )
        return [
            ProposalEvent(
                id=row["id"],
                proposal_id=row["proposal_id"],
                actor=row["actor"],
                type=row["type"],
                detail=row["detail"],
                at=datetime.fromisoformat(row["at"]),
            )
            for row in cursor.fetchall()
        ]

    @_synchronized
    def put_assertion_event(self, event: AssertionEvent) -> None:
        """Persist an append-only assertion status-mutation event."""
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO assertion_event (id, assertion_id, actor, action, at, successor_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.assertion_id,
                    event.actor,
                    event.action,
                    event.at.isoformat(),
                    event.successor_id,
                ),
            )
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise StorageError(f"Assertion event conflict (id={event.id}): {e}") from e
        except sqlite3.Error as e:
            raise StorageError(f"Failed to persist assertion event (id={event.id}): {e}") from e

    @_synchronized
    def get_assertion_events(self, assertion_id: str) -> list[AssertionEvent]:
        """Retrieve all status-mutation events for an assertion, oldest first."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM assertion_event WHERE assertion_id = ? ORDER BY at ASC",
            (assertion_id,),
        )
        return [self._row_to_assertion_event(row) for row in cursor.fetchall()]

    @_synchronized
    def get_assertion_events_by_successor(self, successor_id: str) -> list[AssertionEvent]:
        """Retrieve all 'superseded' events caused by a given successor assertion."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM assertion_event WHERE successor_id = ? ORDER BY at ASC, id ASC",
            (successor_id,),
        )
        return [self._row_to_assertion_event(row) for row in cursor.fetchall()]

    @staticmethod
    def _row_to_assertion_event(row: sqlite3.Row) -> AssertionEvent:
        return AssertionEvent(
            id=row["id"],
            assertion_id=row["assertion_id"],
            actor=row["actor"],
            action=row["action"],
            at=datetime.fromisoformat(row["at"]),
            successor_id=row["successor_id"],
        )

    @_synchronized
    def put_contradiction(self, contradiction: Contradiction) -> None:
        """Persist a new contradiction."""
        import json

        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO contradiction (id, namespace, subject, predicate, state,
                    member_ids, created_at, raised_by, resolved_by, resolved_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contradiction.id,
                    contradiction.namespace,
                    contradiction.subject,
                    contradiction.predicate,
                    contradiction.state,
                    json.dumps(contradiction.member_ids),
                    contradiction.created_at.isoformat(),
                    contradiction.raised_by,
                    contradiction.resolved_by,
                    contradiction.resolved_at.isoformat() if contradiction.resolved_at else None,
                    json.dumps(contradiction.metadata),
                ),
            )
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.IntegrityError as e:
            raise StorageError(f"Contradiction conflict (id={contradiction.id}): {e}") from e
        except sqlite3.Error as e:
            raise StorageError(
                f"Failed to persist contradiction (id={contradiction.id}): {e}"
            ) from e

    @staticmethod
    def _row_to_contradiction(row: sqlite3.Row) -> Contradiction:
        """Deserialize a `contradiction` table row into a Contradiction."""
        import json

        return Contradiction(
            id=row["id"],
            namespace=row["namespace"],
            subject=row["subject"],
            predicate=row["predicate"],
            state=row["state"],
            member_ids=json.loads(row["member_ids"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            raised_by=row["raised_by"],
            resolved_by=row["resolved_by"],
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            metadata=json.loads(row["metadata"]),
        )

    @_synchronized
    def get_open_contradiction(
        self, namespace: str, subject: str, predicate: str
    ) -> Contradiction | None:
        """Return the open contradiction for (namespace, subject, predicate), if any."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM contradiction
            WHERE namespace = ? AND subject = ? AND predicate = ? AND state = 'open'
            LIMIT 1
            """,
            (namespace, subject, predicate),
        )
        row = cursor.fetchone()
        return self._row_to_contradiction(row) if row else None

    @_synchronized
    def update_contradiction_members(
        self,
        contradiction_id: str,
        member_ids: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add member IDs to an existing open contradiction (KI-071:
        optionally replace metadata too, in the same UPDATE)."""
        import json

        try:
            cursor = self.conn.cursor()
            if metadata is not None:
                cursor.execute(
                    "UPDATE contradiction SET member_ids = ?, metadata = ? WHERE id = ?",
                    (json.dumps(member_ids), json.dumps(metadata), contradiction_id),
                )
            else:
                cursor.execute(
                    "UPDATE contradiction SET member_ids = ? WHERE id = ?",
                    (json.dumps(member_ids), contradiction_id),
                )
            if cursor.rowcount == 0:
                raise StorageError(f"Contradiction not found: {contradiction_id}")
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.Error as e:
            raise StorageError(
                f"Failed to update contradiction (id={contradiction_id}): {e}"
            ) from e

    @_synchronized
    def get_contradiction(self, contradiction_id: str) -> Contradiction | None:
        """Retrieve a contradiction by ID, regardless of state."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM contradiction WHERE id = ?", (contradiction_id,))
        row = cursor.fetchone()
        return self._row_to_contradiction(row) if row else None

    @_synchronized
    def contradictions(self, state: str | None = None) -> list[Contradiction]:
        """Query contradictions, optionally filtered by state (SPEC §14.1)."""
        cursor = self.conn.cursor()
        if state is not None:
            cursor.execute(
                "SELECT * FROM contradiction WHERE state = ? ORDER BY created_at DESC, id DESC",
                (state,),
            )
        else:
            cursor.execute("SELECT * FROM contradiction ORDER BY created_at DESC, id DESC")
        return [self._row_to_contradiction(row) for row in cursor.fetchall()]

    @_synchronized
    def resolve_contradiction(
        self,
        contradiction_id: str,
        resolved_by: str,
        resolved_at: datetime,
    ) -> None:
        """Mark a contradiction as resolved (SPEC §10.3)."""
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                UPDATE contradiction
                SET state = 'resolved', resolved_by = ?, resolved_at = ?
                WHERE id = ?
                """,
                (resolved_by, resolved_at.isoformat(), contradiction_id),
            )
            if cursor.rowcount == 0:
                raise StorageError(f"Contradiction not found: {contradiction_id}")
            if not self._in_transaction:
                self.conn.commit()
        except sqlite3.Error as e:
            raise StorageError(
                f"Failed to resolve contradiction (id={contradiction_id}): {e}"
            ) from e

    @_synchronized
    def entities_where(
        self,
        namespace: str,
        concept: str,
        predicate_filters: list[tuple[str, str, Any]],
        as_of_time: datetime | None = None,
        include_flagged: bool = False,
        include_history: bool = False,
    ) -> list[Entity]:
        """Query entities matching all predicate filters in one SQL query.

        Uses correlated subqueries so each filter hits the
        idx_assertion_pred_value/idx_assertion_pred_ref indexes instead of
        doing one round-trip per entity. `"eq"` matches either a literal
        property (`value_lit`) or a relation's target entity id
        (`value_ref`) — KI-030: relation filters like `employer="org-123"`
        are equality checks against `value_ref`, not traversal into the
        target entity's own properties. The two are checked via a UNION ALL
        of two single-column point lookups rather than one `value_lit = ?
        OR value_ref = ?` predicate — SQLite's planner doesn't reliably pick
        a seekable plan for the latter (falls back to a full table SCAN on
        the as_of branch; confirmed via EXPLAIN QUERY PLAN), which turned
        every `.where()` call — not just relation filters — into an
        unindexed scan. `"contains"`/`"gt"`/`"lt"`/`"gte"`/`"lte"` (KI-039)
        check `value_lit` only — see this port method's own docstring for
        why relations don't get a UNION ALL branch for those.

        Args:
            namespace: Namespace to query
            concept: Concept to filter by
            predicate_filters: List of `(full_predicate, operator, value)`
                triples (AND semantics) — see the port method's docstring
                for the operator set
            as_of_time: If set, applies bitemporal filter on assertions and entity creation
            include_flagged: Also match 'flagged' assertions — honored on
                both the current-state and as_of_time paths (KI-081;
                excluded by default on both, see assertions()). On the
                as_of_time path this is point-in-time, not current status
                (KI-097): reconstructed from the assertion_event log the
                same way assertions() already does, so a query pinned to a
                time when an assertion *was* disputed correctly excludes
                it even after the dispute has since been resolved — and,
                the other direction, a time strictly before any dispute
                existed still includes an otherwise-undisputed value, even
                though the same assertion is flagged now.
            include_history: Also match 'superseded'/'retracted' assertions
                (KI-081). On the current-state path this widens the status
                set beyond 'active'. On the as_of_time path, 'superseded' is
                unaffected either way (its window already never restricts to
                'active') — but 'retracted' does something under this flag
                now (ADR-0049, KI-095): as_of_time excludes a retracted
                assertion once its own retraction event's `at` is <=
                as_of_time (a stale, un-narrowed window otherwise keeps
                matching indefinitely); include_history opts back out of
                that exclusion.

        Returns:
            List of entities where all filters match at the given time
        """
        query = "SELECT * FROM entity WHERE namespace = ? AND concept = ?"
        params: list[Any] = [namespace, concept]

        if as_of_time is not None:
            t_iso = as_of_time.isoformat()
            query += " AND created_at <= ?"
            params.append(t_iso)
            # flagged_clause is always one of exactly two hardcoded literals,
            # never caller-controlled. No # nosec needed here (unlike the
            # match_clause consumers below, e.g. `AND id IN (...SELECT...`):
            # bandit's B608 rule only flags a SQL keyword joined into a
            # string via BinOp/.format()/f-string - never a bare literal
            # Constant, which is all flagged_clause/retracted_clause ever
            # are (confirmed by AST: both branches of the ternary are plain
            # folded string constants). The COALESCE subquery below *does*
            # now contain SELECT/FROM/WHERE/ORDER BY/LIMIT (KI-097 - it
            # didn't when this comment was first written), but since that
            # text lives entirely inside the constant rather than being
            # concatenated onto one, bandit's detector still never sees it -
            # confirmed directly, not assumed (a #nosec placed here was
            # previously dead: removing it left bandit's finding count
            # unchanged).
            # KI-097: flagged-at-t, not current status — same event-log
            # reconstruction assertions() already uses (see its own
            # comment for the full reasoning: a static conflict flags an
            # assertion permanently, so using current status would hide it
            # from as_of() queries for times before the dispute existed,
            # and — the bug this KI fixes — would wrongly *include* it for
            # times during a dispute that has since been resolved, since
            # `reactivated` flips current status back to `active`).
            # COALESCE/tiebreak reasoning identical to assertions()'s own.
            # Fail-open on a missing event (COALESCE defaults to
            # 'reactivated', i.e. "not flagged"): a status='flagged' row
            # with no matching event — reachable only via a direct
            # put_assertion() bypassing Ontology, or a pre-existing row
            # from before assertion_event tracked this — is visible at
            # every t. The opposite default from retracted_clause below
            # (fail-closed, ADR-0049), but the correct one here: matches
            # assertions()'s own identical COALESCE, and status='flagged'
            # rows always carry a real 'flagged' event through every
            # write path this codebase has (_apply_with_conflict_routing).
            flagged_clause = (
                ""
                if include_flagged
                else (
                    " AND COALESCE("
                    "(SELECT ae.action FROM assertion_event ae"
                    " WHERE ae.assertion_id = assertion.id AND ae.at <= ?"
                    " AND ae.action IN ('flagged', 'reactivated')"
                    " ORDER BY ae.at DESC, ae.id DESC LIMIT 1),"
                    " 'reactivated'"
                    ") != 'flagged'"
                )
            )
            # retracted_clause: same two-hardcoded-literals shape as
            # flagged_clause above, same reasoning for why no #nosec is
            # needed. ADR-0049 (KI-095): a `retracted` assertion's window is
            # not reliably narrowed at retraction time, so `as_of(t)` also
            # excludes it once its own retraction event's `at` is <= t —
            # KI-051 guarantees at most one such event per assertion.
            # `.include_history()` opts back out of this check, the first
            # thing it has ever done on the `as_of` path.
            retracted_clause = (
                ""
                if include_history
                else (
                    " AND (status != 'retracted' OR EXISTS ("
                    "SELECT 1 FROM assertion_event ae"
                    " WHERE ae.assertion_id = assertion.id"
                    " AND ae.action = 'retracted' AND ae.at > ?"
                    "))"
                )
            )
            match_clause = (
                " AND asserted_at <= ?"
                " AND (valid_from IS NULL OR valid_from <= ?)"
                " AND (valid_to IS NULL OR valid_to > ?)"
                f"{flagged_clause}"
                f"{retracted_clause}"
            )
            match_params = [t_iso, t_iso, t_iso]
            if not include_flagged:
                match_params.append(t_iso)
            if not include_history:
                match_params.append(t_iso)
        else:
            # Current-state: 'active' only by default;
            # .include_flagged()/.include_history() widen the set (KI-081).
            # `status IN (?, …)` is fully parameter-bound — the interpolated
            # piece is only the placeholder string (`?, ?`), built from
            # `len(match_params)`, never caller input.
            match_params = ["active"]
            if include_flagged:
                match_params.append("flagged")
            if include_history:
                match_params += ["superseded", "retracted"]
            match_clause = f" AND status IN ({', '.join(['?'] * len(match_params))})"

        # predicate/value are always bound via `?` below, never
        # interpolated; the two interpolated pieces are match_clause (built
        # from hardcoded literals, see the flagged_clause justification
        # above) and, for range operators, sql_op — a lookup into the
        # closed, module-level _RANGE_SQL_OPERATORS dict, never the
        # caller's raw operator string. Same already-justified pattern, not
        # a new SQL injection surface.
        for predicate, operator, value in predicate_filters:
            if operator == "eq":
                query += (
                    " AND id IN ("  # nosec B608
                    "SELECT subject FROM assertion"
                    f" WHERE predicate = ? AND value_lit = ?{match_clause}"
                    " UNION ALL "
                    "SELECT subject FROM assertion"
                    f" WHERE predicate = ? AND value_ref = ?{match_clause}"
                    ")"
                )
                params.extend([predicate, value, *match_params, predicate, value, *match_params])
            elif operator == "contains":
                query += (
                    " AND id IN ("  # nosec B608
                    "SELECT subject FROM assertion"
                    f" WHERE predicate = ? AND value_lit LIKE ? ESCAPE '\\'{match_clause}"
                    ")"
                )
                params.extend([predicate, f"%{_like_escape(value)}%", *match_params])
            else:
                # QueryBuilder only validates the predicate's *declared*
                # value_type is Integer/Float, never that already-stored
                # value_lit content actually parses as one (KI-049).
                # SQLite has no TRY_CAST (unlike DuckDB's equivalent
                # branch): CAST('unknown' AS REAL) silently returns 0.0
                # rather than erroring or excluding the row — a known,
                # tracked gap, not something this fix can close without
                # write-time content validation (KI-049), which is a
                # materially different, larger scope than this operator.
                sql_op = _RANGE_SQL_OPERATORS[operator]
                query += (
                    " AND id IN ("  # nosec B608
                    "SELECT subject FROM assertion"
                    f" WHERE predicate = ? AND CAST(value_lit AS REAL) {sql_op} ?{match_clause}"
                    ")"
                )
                params.extend([predicate, value, *match_params])

        cursor = self.conn.cursor()
        cursor.execute(query, params)

        return [
            Entity(
                id=row["id"],
                namespace=row["namespace"],
                concept=row["concept"],
                natural_key=row["natural_key"],
                created_at=datetime.fromisoformat(row["created_at"]),
                created_by=row["created_by"],
            )
            for row in cursor.fetchall()
        ]

    @_synchronized
    def entities_meeting_confidence(
        self,
        namespace: str,
        concept: str,
        threshold: float,
        as_of_time: datetime | None = None,
        candidate_ids: frozenset[str] | None = None,
        include_flagged: bool = False,
        include_history: bool = False,
    ) -> set[str]:
        """IDs of entities in `(namespace, concept)` with >=1 assertion at or
        above `threshold` confidence, active at `as_of_time` (KI-036) or
        currently active if `as_of_time` is None. `candidate_ids`, if given,
        narrows the scan below `(namespace, concept)` (KI-037) via a single
        JSON-encoded bound parameter rather than one placeholder per id.
        `include_flagged`/`include_history` widen "active" the same way
        `entities_where()` does (KI-093) — see its docstring."""
        if candidate_ids is not None and not candidate_ids:
            return set()

        query = (
            "SELECT DISTINCT a.subject FROM assertion a"
            " JOIN entity e ON e.id = a.subject"
            " WHERE e.namespace = ? AND e.concept = ? AND a.confidence >= ?"
        )
        params: list[Any] = [namespace, concept, threshold]

        if as_of_time is not None:
            t_iso = as_of_time.isoformat()
            # One of exactly two hardcoded literals, never caller-controlled
            # - no injection surface, and no #nosec needed (see
            # entities_where()'s identical comment for why bandit's B608
            # heuristic doesn't fire on this shape at all).
            # KI-097: flagged-at-t, not current status — same event-log
            # reconstruction entities_where()/assertions() already use;
            # see entities_where()'s comment for the full reasoning.
            flagged_clause = (
                ""
                if include_flagged
                else (
                    " AND COALESCE("
                    "(SELECT ae.action FROM assertion_event ae"
                    " WHERE ae.assertion_id = a.id AND ae.at <= ?"
                    " AND ae.action IN ('flagged', 'reactivated')"
                    " ORDER BY ae.at DESC, ae.id DESC LIMIT 1),"
                    " 'reactivated'"
                    ") != 'flagged'"
                )
            )
            # ADR-0049 (KI-095): same retraction-aware exclusion
            # entities_where() has — see its comment for the full
            # reasoning. Two-hardcoded-literals shape, no #nosec needed.
            retracted_clause = (
                ""
                if include_history
                else (
                    " AND (a.status != 'retracted' OR EXISTS ("
                    "SELECT 1 FROM assertion_event ae"
                    " WHERE ae.assertion_id = a.id"
                    " AND ae.action = 'retracted' AND ae.at > ?"
                    "))"
                )
            )
            query += (
                " AND a.asserted_at <= ?"
                " AND (a.valid_from IS NULL OR a.valid_from <= ?)"
                " AND (a.valid_to IS NULL OR a.valid_to > ?)" + flagged_clause + retracted_clause
            )
            params.extend([t_iso, t_iso, t_iso])
            if not include_flagged:
                params.append(t_iso)
            if not include_history:
                params.append(t_iso)
        else:
            status_params = ["active"]
            if include_flagged:
                status_params.append("flagged")
            if include_history:
                status_params += ["superseded", "retracted"]
            query += f" AND a.status IN ({', '.join(['?'] * len(status_params))})"
            params.extend(status_params)

        if candidate_ids is not None:
            query += " AND e.id IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(list(candidate_ids)))

        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return {row["subject"] for row in cursor.fetchall()}

    @_synchronized
    def entities_meeting_trust(
        self,
        namespace: str,
        concept: str,
        min_trust: int,
        as_of_time: datetime | None = None,
        candidate_ids: frozenset[str] | None = None,
        include_flagged: bool = False,
        include_history: bool = False,
    ) -> set[str]:
        """IDs of entities in `(namespace, concept)` with >=1 assertion,
        active at `as_of_time` (KI-036) or currently active if `as_of_time`
        is None, whose *effective* trust_level >= `min_trust` (KI-047) —
        `min(author.trust_level, acting_as.trust_level)` when the assertion
        was made under delegation, matching `govern/policy.py`'s identical
        formula for effective trust (by analogy with SPEC §8.4's capability
        rule), or just `author.trust_level` when it wasn't. A dangling
        `acting_as` (no resolvable delegate) falls back to `author.trust_level`
        via `coalesce` — see `StorageBackend.entities_meeting_trust`'s
        docstring for why. `candidate_ids` narrows the scan the same way as
        `entities_meeting_confidence` (KI-037) — see its docstring.
        `include_flagged`/`include_history` widen "active" the same way
        `entities_where()` does (KI-093) — see its docstring."""
        if candidate_ids is not None and not candidate_ids:
            return set()

        query = (
            "SELECT DISTINCT a.subject FROM assertion a"
            " JOIN entity e ON e.id = a.subject"
            " JOIN principal p ON p.id = a.author"
            " LEFT JOIN principal delegate ON delegate.id = a.acting_as"
            " WHERE e.namespace = ? AND e.concept = ?"
            " AND min(p.trust_level, coalesce(delegate.trust_level, p.trust_level)) >= ?"
        )
        params: list[Any] = [namespace, concept, min_trust]

        if as_of_time is not None:
            t_iso = as_of_time.isoformat()
            # One of exactly two hardcoded literals, never caller-controlled
            # - no injection surface, and no #nosec needed (see
            # entities_where()'s identical comment for why bandit's B608
            # heuristic doesn't fire on this shape at all).
            # KI-097: flagged-at-t, not current status — same event-log
            # reconstruction entities_where()/assertions() already use;
            # see entities_where()'s comment for the full reasoning.
            flagged_clause = (
                ""
                if include_flagged
                else (
                    " AND COALESCE("
                    "(SELECT ae.action FROM assertion_event ae"
                    " WHERE ae.assertion_id = a.id AND ae.at <= ?"
                    " AND ae.action IN ('flagged', 'reactivated')"
                    " ORDER BY ae.at DESC, ae.id DESC LIMIT 1),"
                    " 'reactivated'"
                    ") != 'flagged'"
                )
            )
            # ADR-0049 (KI-095): same retraction-aware exclusion
            # entities_where() has — see its comment for the full
            # reasoning. Two-hardcoded-literals shape, no #nosec needed.
            retracted_clause = (
                ""
                if include_history
                else (
                    " AND (a.status != 'retracted' OR EXISTS ("
                    "SELECT 1 FROM assertion_event ae"
                    " WHERE ae.assertion_id = a.id"
                    " AND ae.action = 'retracted' AND ae.at > ?"
                    "))"
                )
            )
            query += (
                " AND a.asserted_at <= ?"
                " AND (a.valid_from IS NULL OR a.valid_from <= ?)"
                " AND (a.valid_to IS NULL OR a.valid_to > ?)" + flagged_clause + retracted_clause
            )
            params.extend([t_iso, t_iso, t_iso])
            if not include_flagged:
                params.append(t_iso)
            if not include_history:
                params.append(t_iso)
        else:
            status_params = ["active"]
            if include_flagged:
                status_params.append("flagged")
            if include_history:
                status_params += ["superseded", "retracted"]
            query += f" AND a.status IN ({', '.join(['?'] * len(status_params))})"
            params.extend(status_params)

        if candidate_ids is not None:
            query += " AND e.id IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(list(candidate_ids)))

        cursor = self.conn.cursor()
        cursor.execute(query, params)
        return {row["subject"] for row in cursor.fetchall()}

    def _validate_scope(self, scope: str) -> None:
        if scope not in VECTOR_SCOPES:
            raise ValidationError(
                f"Unknown vector scope: {scope!r} (must be one of {sorted(VECTOR_SCOPES)})"
            )

    def _ensure_vector_table(self, scope: str, vec_len: int) -> None:
        """Establish (or validate against) the dimension for `scope`.

        The first vector ever upserted into a scope fixes its dimension: the
        vec0 virtual table is created lazily here, at that dimension. Later
        calls validate `vec_len` matches.

        Raises:
            ValidationError: `vec_len` doesn't match the established dim.
            StorageError: If lazy table creation fails.
        """
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT dim FROM vector_scope WHERE scope = ?", (scope,)).fetchone()
        if row is None:
            try:
                cursor.execute(
                    f"CREATE VIRTUAL TABLE vector_{scope} USING vec0(embedding FLOAT[{vec_len}])"
                )
                cursor.execute(
                    "INSERT INTO vector_scope (scope, dim) VALUES (?, ?)", (scope, vec_len)
                )
                if not self._in_transaction:
                    self.conn.commit()
            except sqlite3.Error as e:
                if not self._in_transaction:
                    self.conn.rollback()
                raise StorageError(f"Failed to create vector table for scope {scope!r}: {e}") from e
            return
        if row["dim"] != vec_len:
            raise ValidationError(
                f"Vector for scope {scope!r} has dimension {vec_len}, "
                f"but this scope is established at dimension {row['dim']}"
            )

    @_synchronized
    def vector_upsert(self, scope: str, id: str, vec: list[float]) -> None:
        """Insert or replace the embedding vector for (scope, id).

        Args:
            scope: Embedding scope. Must be one of VECTOR_SCOPES.
            id: Entity or assertion ID the vector represents.
            vec: Embedding vector.

        Raises:
            ValidationError: scope is not in VECTOR_SCOPES, or vec's length
                does not match the scope's already-established dimension.
            StorageError: If persistence fails.
        """
        self._validate_scope(scope)
        self._ensure_vector_table(scope, len(vec))
        table = f"vector_{scope}"

        try:
            was_in_transaction = self._in_transaction
            if not was_in_transaction:
                self.begin()
            cursor = self.conn.cursor()
            existing = cursor.execute(
                "SELECT vec_rowid FROM vector_id_map WHERE scope = ? AND id = ?", (scope, id)
            ).fetchone()
            # table is built from `scope`, which _validate_scope() above
            # already checked against the closed VECTOR_SCOPES set — not
            # caller-controlled free text.
            cursor.execute(f"INSERT INTO {table}(embedding) VALUES (?)", (_pack_vector(vec),))  # nosec B608
            new_rowid = cursor.lastrowid
            if existing is not None:
                cursor.execute(f"DELETE FROM {table} WHERE rowid = ?", (existing["vec_rowid"],))  # nosec B608
            cursor.execute(
                "INSERT OR REPLACE INTO vector_id_map (scope, id, vec_rowid) VALUES (?, ?, ?)",
                (scope, id, new_rowid),
            )
            if not was_in_transaction:
                self.commit()
        except sqlite3.Error as e:
            if not was_in_transaction:
                self.rollback()
            raise StorageError(f"Failed to upsert vector (scope={scope}, id={id}): {e}") from e

    @_synchronized
    def vector_search(self, scope: str, vec: list[float], k: int) -> list[tuple[str, float]]:
        """Return the k nearest ids to vec within scope, ascending distance.

        Args:
            scope: Embedding scope. Must be one of VECTOR_SCOPES.
            vec: Query vector.
            k: Maximum number of results.

        Returns:
            (id, distance) tuples, nearest first. Empty list if the scope
            has never been populated.

        Raises:
            ValidationError: scope is not in VECTOR_SCOPES, or vec's length
                does not match the scope's already-established dimension.
        """
        self._validate_scope(scope)
        cursor = self.conn.cursor()
        row = cursor.execute("SELECT dim FROM vector_scope WHERE scope = ?", (scope,)).fetchone()
        if row is None:
            return []
        if row["dim"] != len(vec):
            raise ValidationError(
                f"Query vector for scope {scope!r} has dimension {len(vec)}, "
                f"but this scope is established at dimension {row['dim']}"
            )

        # table is built from `scope`, already validated above (same
        # reasoning as vector_upsert) — not caller-controlled free text.
        table = f"vector_{scope}"
        rows = cursor.execute(
            f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? ORDER BY distance LIMIT ?",  # nosec B608
            (_pack_vector(vec), k),
        ).fetchall()
        if not rows:
            return []

        # placeholders is just N repetitions of the literal "?" (N = len(rowids),
        # itself derived from `rows` above, never from external input) — an
        # IN-clause arity string, not a value; the actual rowids are still
        # bound as parameters below, not interpolated.
        rowids = [r["rowid"] for r in rows]
        placeholders = ",".join("?" for _ in rowids)
        id_rows = cursor.execute(
            f"SELECT vec_rowid, id FROM vector_id_map WHERE scope = ? AND vec_rowid IN ({placeholders})",  # nosec B608
            (scope, *rowids),
        ).fetchall()
        id_by_rowid = {r["vec_rowid"]: r["id"] for r in id_rows}

        return [(id_by_rowid[r["rowid"]], r["distance"]) for r in rows]

    @_synchronized
    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()


__all__ = ["SQLiteBackend"]
