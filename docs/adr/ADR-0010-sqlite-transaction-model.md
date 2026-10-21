# ADR-0010: SQLite Backend Transaction Model (Autocommit + Explicit Transaction Flag)

**Status**: Accepted  
**Date**: 2026-06-26  
**Deciders**: Ontolith Core Team  
**Related**: ADR-0001 (Storage Default), SPEC §9 (Proposal workflow)

---

## Context

The `SQLiteBackend` must support two distinct write patterns with conflicting requirements:

1. **Standalone writes** — individual operations (`put_entity`, `put_assertion`, `put_principal`, etc.) called directly from the SDK. These must be durable immediately: if the caller closes the connection after `put_entity()`, the entity must survive and be visible to a new connection.

2. **Batch transactions** — proposal acceptance (SPEC §9) requires multiple writes to be atomic. All writes land or none land, with `rollback()` undoing everything on failure. This is the "one transaction per proposal acceptance" invariant from the SPEC.

Python's `sqlite3` default mode ("legacy transaction mode") implicitly issues `BEGIN` before the first DML and holds the transaction open until an explicit `conn.commit()` is called. This means:

- Standalone writes are **not durable across connections** — data written in one connection is invisible to any other until `commit()` is called.
- The bug surfaces as "writes work within the same connection but disappear on `close()`," which is how all existing unit tests pass while the CLI (which opens a new connection per command) sees empty databases.

## Decision

Open the connection in **autocommit mode** (`isolation_level=None`) and track an explicit `_in_transaction: bool` flag on the backend instance:

```python
self.conn = sqlite3.connect(str(self.path), isolation_level=None)
self._in_transaction: bool = False
```

Each write method calls `conn.commit()` after successful DML **only when not inside an explicit transaction**:

```python
# After the INSERT/UPDATE succeeds:
if not self._in_transaction:
    self.conn.commit()
```

`begin()`, `commit()`, and `rollback()` manage the flag:

```python
def begin(self) -> None:
    self.conn.execute("BEGIN")
    self._in_transaction = True


def commit(self) -> None:
    self.conn.commit()
    self._in_transaction = False


def rollback(self) -> None:
    self.conn.rollback()
    self._in_transaction = False
```

## Rationale

**Why `isolation_level=None`:**
- Removes the implicit-transaction footgun from Python's `sqlite3` legacy mode
- SQLite's explicit `BEGIN`/`COMMIT`/`ROLLBACK` still work correctly in autocommit mode
- Standard approach for systems that need both per-operation durability and multi-statement transactions (matches SQLAlchemy's DBAPI layer behavior)

**Why a tracking flag instead of unconditional `conn.commit()` in every write method:**
- Unconditional auto-commit would break proposal acceptance: a `put_entity()` call inside `begin()`/`commit()` would commit prematurely, making `rollback()` unable to undo writes that already landed
- The flag makes the two modes explicit and testable

**Why not require callers to call `commit()` after every standalone write:**
- Leaks transaction management concerns into the application layer (Ontology, future MCP server, CLI)
- Error-prone: any caller that forgets would silently lose data

## Consequences

**Positive:**
- ✅ Standalone writes are immediately durable — cross-connection reads (CLI, second SDK instance) work correctly
- ✅ Batch transactions remain fully atomic — `rollback()` is safe and effective
- ✅ Future `StorageBackend` implementations have a clear protocol to follow: the same begin/commit/rollback contract applies to any backend

**Negative:**
- ⚠️ One extra `COMMIT` round-trip per standalone write — negligible for M1 write volumes
- ⚠️ `_in_transaction` is mutable state on the backend; callers must pair `begin()` / `commit()` correctly

**Implementation notes (M1):**
- A `transaction()` context manager (`with backend.transaction(): ...`) is provided on `StorageBackend` and `SQLiteBackend` to guarantee rollback on any exception and avoid wedged connections.
- The atomic multi-write path (proposal acceptance) is **not yet wired** in M1 — each `put_*` call at the `Ontology` layer commits independently. Wiring `begin()`/`commit()` around proposal acceptance is tracked for M2.

## Alternatives Considered

**Unconditional `conn.commit()` in every write method (no flag):**
- Rejected: Breaks atomic proposal acceptance — mid-transaction auto-commits make `rollback()` ineffective for writes already landed.

**Keep legacy transaction mode, require all callers to commit explicitly:**
- Rejected: Leaks transaction concerns upward; every SDK method, CLI command, and MCP handler would need to call `commit()` or risk data loss.

**Use SQLAlchemy for connection management:**
- Rejected for M1: Unnecessary dependency; adds complexity the `StorageBackend` port abstraction already handles.

## Update (2026-07-22, closes KI-023): thread-safety for `_in_transaction`

This ADR's own Consequences flagged `_in_transaction` as "mutable state on the
backend; callers must pair `begin()`/`commit()` correctly" — written when the
connection was only ever touched from one thread. That stopped being true
once the REST interface (ADR-0021) added `check_same_thread=False`: an ASGI
server dispatches sync route handlers onto a worker threadpool, so two
genuinely concurrent requests could call `begin()` back-to-back before the
first `commit()`/`rollback()` ran, corrupting `_in_transaction` and, in the
worst case, raising a raw `sqlite3.OperationalError` ("cannot start a
transaction within a transaction") that bypassed the REST error mapping
entirely (SPEC §16). Tracked as KI-023; confirmed reproducible with a
`ThreadPoolExecutor`-based regression test before this fix landed.

**Fix:** `SQLiteBackend` now holds a `threading.RLock` (`self._lock`).
`begin()` acquires it, held for the full span of an explicit transaction —
not just one method call. Every other public method is wrapped with a
`@_synchronized` decorator that acquires the same lock for its own duration.
RLock, not a plain `Lock`, because a method called from inside an
already-`begin()`-locked transaction (e.g. `put_entity` inside `with
backend.transaction():`) re-acquires on the same thread without blocking;
a different thread calling any method — standalone or transactional —
blocks until the lock is free. Net effect: the single shared connection is
never touched by two threads at once, whether or not either is inside an
explicit transaction.

`commit()`/`rollback()` release the lock asymmetrically, not both
unconditionally via `finally` — an `ontolith-reviewer` pass on this fix
caught the reason why that matters: `transaction()` calls `rollback()`
after catching *any* exception raised inside its `try` block, including one
raised by `commit()` itself failing. If `commit()` also released the lock
unconditionally on failure, `rollback()`'s own release would be a *second*
release of an already-free lock, which `RLock.release()` rejects with
`RuntimeError: cannot release un-acquired lock` — masking the original
`StorageError` and, since that exception fires before `_in_transaction =
False` is reached, leaving the flag stuck `True` (lock free, flag says
"still in a transaction," forever) after every future call. So `commit()`
releases the lock (and resets `_in_transaction`) only on success; on
failure it leaves both alone, so `rollback()` — the only thing that runs
next — is unambiguously the sole method that resolves and releases the
transaction. `rollback()` itself keeps the unconditional `finally` release,
since it is the terminal cleanup step regardless of outcome: leaving the
lock held after a failed rollback would deadlock every future caller, a
strictly worse failure mode than a stale flag. Regression-tested directly:
`tests/unit/test_sqlite_backend.py::TestConcurrency::test_commit_failure_inside_transaction_raises_storage_error_and_frees_lock`
forces `conn.commit()` to fail inside a `transaction()` block and asserts a
clean `StorageError` (not `RuntimeError`), a freed lock, and
`_in_transaction is False` afterward — confirmed to fail with exactly the
predicted `RuntimeError` against the unconditional-`finally` version before
this asymmetric-release design was adopted.

This trades true write concurrency for correctness — SQLite's own WAL mode
concurrent-reader support is unaffected (readers still don't block behind
writers at the SQLite level), but two threads can no longer make
*application-level* progress on this connection simultaneously. Deemed the
right tradeoff for now: this is a single shared connection object, not a
connection pool, so there was never real write parallelism to lose. A
connection-pool design (one connection per request, relying on SQLite's own
file-level locking instead of an in-process lock) remains a valid future
alternative if this serialization point becomes a measured bottleneck —
not pursued now since no throughput data suggests it is one yet.

## References

- ADR-0001: Storage Default (SQLite + sqlite-vec)
- ADR-0021: REST interface (introduced `check_same_thread=False`, surfaced this gap)
- SPEC §9: Proposal workflow and acceptance
- SPEC §12.1: SQLite default backend, WAL mode
- Python `sqlite3` docs: `isolation_level` parameter
- `docs/known-issues.md` KI-023
