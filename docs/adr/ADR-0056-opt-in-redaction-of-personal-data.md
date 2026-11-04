# ADR-0056: Opt-In Redaction of Personal Data

**Status**: Proposed
**Date**: 2026-11-03
**Deciders**: Ontolith Core Team
**Related**: SPEC §5 (assertions are append-only), SPEC §15 (versioning, time-travel), SPEC §17 (the audit trail MUST NOT be mutable), ADR-0003 (agent identity), ADR-0031 (a retracted member cannot win a contradiction), ADR-0052 (storage-format migrations, `format_version` frozen at 3)

---

## Context

Ontolith never deletes an assertion. That is a deliberate invariant: time-travel (`as_of`), supersession chains, contradiction membership and the audit trail all depend on it (SPEC §15, §17). It is also in conflict with data-protection law. LGPD Art. 18 and GDPR Art. 17 let a person ask for their personal data to be erased, and a knowledge base that holds facts about people will receive such requests. Two gaps make this impossible today, even with a willing operator:

- **No way to remove content.** A value, its source and its rationale stay in the `assertion` row for good. Copies of that text also sit in `proposal.payload`, in the `detail` column of `proposal_event`, and in a contradiction's rationale history.
- **No way to remove a vector.** `StorageBackend` has `vector_upsert` and `vector_search` but nothing to remove. An entity's vector is built from the concatenation of its Text assertion values (`_entity_text`), so the embedding of personal text outlives any attempt to erase the text.

Most deployments do not need erasure, and for them the strict guarantee is the product: nothing the system said is ever silently altered. A regulated deployment needs the opposite trade. The two cannot both be the default, so the behavior has to be a choice made when the knowledge base is set up, visible in the data, and hard to change by accident.

## Decision

### 1. Strict by default; redaction is an opt-in mode of one knowledge base

Each knowledge base has an **erasure mode**: `strict` (the default) or `redaction`. In `strict` mode nothing in this ADR changes behavior: the erasure operation raises, no tombstone can be written, and the append-only guarantees read exactly as they do today.

The mode is stored in the database, not passed on each connection, so the same file behaves the same for every process that opens it.

```python
kb = Ontology.connect("kb.db", erasure="redaction")  # creates a new KB in redaction mode
kb = Ontology.connect("kb.db")  # uses the stored mode
```

Rules for the argument:

- A new database takes the given mode (default `strict`) and records it.
- An existing database already has a mode. Passing the other mode raises instead of being ignored, so a typo cannot flip a regulated KB to strict or the reverse silently.
- `Ontology.enable_redaction(actor)` moves an existing `strict` KB to `redaction`. It needs `admin`, a human principal and no delegation, and it writes an audit record. It is **one-way**: there is no call that returns a KB to `strict`, because after the first erasure the strict guarantee no longer holds for that file and a switch back would claim otherwise.

### 2. What is marked as personal

A schema author marks the properties and relations whose values can identify a person: a `personal` flag on the property or relation definition in the IR, available in the class DSL and in the YAML front-end. It is an additive field with a default of `False`, so no existing schema changes meaning. An entity's `natural_key` is treated as personal when its concept declares it so.

Erasure only touches what is marked. It does not search free text for names. If a person's name appears in some other principal's `rationale`, the operation cannot find it, and the ADR does not claim it does (see Consequences).

### 3. The erasure operation

`Ontology.erase_subject(entity_id, *, actor, reason)` is available only in `redaction` mode and only to a human principal holding `admin`. An `ai` principal, a `service` principal and any `acting_as` delegation are refused. `reason` is required and recorded (a request reference, not personal data). For one entity, in a single transaction:

1. **Redact assertions.** For every assertion whose subject is the entity and whose predicate is marked personal, in any status, replace `value_lit`, `source`, `rationale` and `metadata` with a fixed tombstone and record in `metadata` that the row is redacted (`by`, `at`, the erasure id). Keep `id`, `subject`, `predicate`, `author`, `asserted_at`, validity window, `supersedes` and `proposal_id`. Assertions that point at the entity (as `value_ref`) are left alone: they hold an id, not content.
2. **Take the assertion out of current state.** A redacted assertion that is `active` or `flagged` becomes `retracted`, using the same status mutation and `assertion_event` as an ordinary retraction. Current-state queries and conflict routing therefore never compare against a tombstone, and `as_of(t)` still shows that a fact existed, with the content gone.
3. **Redact the copies.** Remove the same content from the stored proposal payloads and `proposal_event.detail` that carried it, and from contradiction rationale history.
4. **Redact the natural key** when the concept marks it personal.
5. **Remove the vectors.** Delete the entity's vector and any vector derived from a redacted assertion. An entity that keeps other non-personal Text values is re-indexed from them afterwards.
6. **Write the record.** Append an `erasure_record` (actor, entity, reason, time, counts of what was redacted and removed). It holds no personal data.

If the subject is a member of an open contradiction, the contradiction is closed as resolved by erasure inside the same transaction, because a retracted member cannot win (ADR-0031). The exact closing rule is settled in the implementation PR.

### 4. Vector removal on the storage port

`StorageBackend` gains `vector_delete(scope, id)`, which is a no-op for an unknown id. The SQLite backend already maps each caller id to an internal rowid (`vector_id_map`) because `vec0` cannot delete by a TEXT key, so removal goes through that map. The DuckDB backend implements the same method. Backends implemented outside this repository must add it; the conformance kit gets a vector for it.

### 5. Storage

- **New tables, additive:** a `kb_setting` table (key, value, set_at, set_by) holding the erasure mode, and an append-only `erasure_record` table with the same no-update, no-delete triggers as the other audit tables. `admin_event`'s `action` list has a CHECK constraint that SQLite cannot widen in place, so erasure is recorded in `erasure_record` instead of widening it. `enable_redaction` writes a `kb_setting` row and an `erasure_record` row of kind `enabled`.
- **`format_version` 3 to 4,** through a registered, reversible migration on both backends, marked **Breaking** in the CHANGELOG as ADR-0052 requires for any bump. Existing databases migrate in `strict` mode and behave as before.
- **Immutable event text.** `proposal_event` has triggers that forbid `UPDATE` and `DELETE`. In `redaction` mode the redaction step must be able to blank `detail`. The design is a trigger variant that permits an update of that one column only while an `erasure_session` row exists, and the erasure transaction creates and removes that row. DuckDB has no triggers, so there the rule is enforced in code. The trigger variant is created only in `redaction` mode, so a strict KB keeps the unconditional triggers. This detail needs a short spike before the implementation PR and can change; the decision here is the guarantee, not the mechanism.

### 6. Documentation

SPEC §5, §15 and §17 gain a note that the append-only guarantee is unconditional in `strict` mode and, in `redaction` mode, has one named, audited exception. The README and `site_docs` state the trade plainly.

## Rationale

- **An opt-in mode keeps the strong default.** Most users get exactly the guarantee Ontolith is known for. The exception exists only in a file that chose it, can be seen by reading its `kb_setting`, and cannot be switched off.
- **Redaction rather than deletion keeps the structure.** Ids, links, timestamps, supersession chains and audit events survive, so `as_of` and provenance still work; only the content is gone. Hard deletion would break all of them.
- **Retracting the redacted assertion avoids a special case in conflict routing.** The existing retraction path already excludes a row from current state and from contradiction comparison.
- **Marking personal properties in the schema keeps the operation predictable.** It erases what the schema author declared, and it does not guess.
- **Human-only, admin-only, with a required reason** keeps the one destructive operation out of any agent's reach, in line with how the MCP surface exposes no direct write.

## Consequences

- **Backups and copies are out of scope.** Redaction changes the live database. A backup, export or replica taken before erasure still holds the data, and this ADR makes no claim about them. Crypto-shredding (rejected below) would have covered backups; an operator who needs that must handle backup retention separately.
- **Free-text mentions are not found.** A name quoted in someone else's rationale or in an unmarked property remains. Schema authors have to mark properties, and reviewers have to avoid putting personal data into unmarked free text.
- **The invariant gets an exception.** In `redaction` mode, `value` is rewritten in place, once, to a tombstone. That conflicts with the append-only rule as written, which is why the exception is a named mode with its own audit record and not a general capability.
- **A `format_version` bump.** It needs a migration on both backends and a Breaking marker. The release that ships it should say that existing databases are unchanged.
- **Part of the API is new:** the `erasure` argument, `enable_redaction`, `erase_subject`, the `personal` schema flag and `vector_delete` on the port. The port addition is breaking for third-party backends.
- **Contradiction closure.** Erasing a member of an open contradiction changes that contradiction's state; the rule needs its own tests.

## Alternatives Considered

- **Crypto-shredding per subject.** Encrypt each personal value under a per-subject key and destroy the key on erasure. It keeps append-only fully intact and covers backups. Rejected for now: it needs a key store and key management that Ontolith does not have, every read and query path would have to decrypt, and filtering, search and embeddings over encrypted text do not work. It can be added later on top of the `personal` flag.
- **Hard delete.** Remove the rows. Simplest to explain. Rejected: it breaks `as_of`, supersession chains, contradiction membership and the audit trail, which is the reason the invariant exists.
- **Redaction always on.** Rejected: it removes the strict default for every user to serve some of them.
- **A per-connection flag instead of a stored mode.** Rejected: two processes could open one file with different guarantees, and the file would not say which one it was run under.
- **Reversible mode switch.** Rejected: after an erasure, a return to `strict` would restore the claim without restoring the data.
