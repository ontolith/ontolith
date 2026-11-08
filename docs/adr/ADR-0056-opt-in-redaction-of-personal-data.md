# ADR-0056: Opt-In Redaction of Personal Data

**Status**: Accepted
**Date**: 2026-11-03
**Deciders**: Ontolith Core Team
**Related**: SPEC §3 and §7.1-7.2 (assertions are append-only; "never in-place edits of the value"), SPEC §5.3 (the `metadata` blob round-trips), SPEC §15 (versioning, time-travel), SPEC §17 (the audit trail MUST NOT be mutable), ADR-0003 (agent identity), ADR-0019 (SemVer policy), ADR-0031 (a retracted member cannot win a contradiction), ADR-0052 (storage-format migrations), ADR-0053 (resolution by winning value), KI-066 (store-level audit immutability)

---

## Context

Ontolith never deletes an assertion. That is a deliberate invariant: time-travel (`as_of`), supersession chains, contradiction membership and the audit trail all depend on it. It conflicts with data-protection law. LGPD Art. 18 and GDPR Art. 17 let a person ask for their personal data to be erased, and a knowledge base that holds facts about people will receive such requests. Three things make erasure impossible today, even for a willing operator:

- **No way to remove content.** A value, its source and its rationale stay in the `assertion` row. Copies sit in `proposal.payload` (including proposals that were never accepted) and in a contradiction's rationale history.
- **No way to remove a vector.** `StorageBackend` has `vector_upsert` and `vector_search` and nothing to remove. An entity's vector is built from the concatenation of its Text assertion values (`_entity_text`), and every re-index inserts a new row and drops the old one, so earlier embeddings of the same text also exist.
- **Deleting a row does not remove its bytes.** On SQLite the default is `secure_delete=OFF`, and the backend runs in WAL mode, so an overwritten or deleted value is still readable in the database file after the transaction commits and after a checkpoint. The `sqlite-vec` virtual table behaves the same way: deleting a vector clears a validity bit and leaves the float bytes in its shadow chunk table. This was reproduced, not assumed. A legal-erasure feature that stops at "the row no longer says it" does not erase anything.

Most deployments do not need erasure, and for them the strict guarantee is the product: nothing the system said is ever silently altered. A regulated deployment needs the opposite trade. The two cannot both be the default, so the behavior has to be a choice made when the knowledge base is set up, visible in the data, and hard to change by accident.

## Decision

### 1. Strict by default; redaction is an opt-in mode of one knowledge base

Each knowledge base has an **erasure mode**: `strict` (the default) or `redaction`. In `strict` mode nothing in this ADR changes behavior: the erasure operation raises, and the append-only guarantees read exactly as they do today.

The mode is stored in the database, not passed on each connection, so the same file behaves the same for every process that opens it.

```python
kb = Ontology.connect("kb.db", erasure="redaction")  # a new KB in redaction mode
kb = Ontology.connect("kb.db")  # uses the stored mode
```

- A new database takes the given mode (default `strict`) and records it. The argument is also accepted by the `Ontology` constructor, so a backend other than SQLite chooses the mode the same way.
- An existing database already has a mode. Passing the other mode raises instead of being ignored, so a typo cannot flip a regulated KB to strict or the reverse silently.
- `Ontology.enable_redaction(actor)` moves an existing `strict` KB to `redaction`. It needs `admin`, a human principal and no delegation, and it writes an audit record. It is **one-way**: there is no call that returns a KB to `strict`, because after the first erasure the strict guarantee no longer holds for that file.
- The mode is a row in a new `kb_setting` table. The table carries the same three audit triggers as the other audit tables (no update, no delete, no replace, KI-066), so the store itself refuses to switch the mode off.
- A KB can only be put in `redaction` mode on a backend that implements the redaction capability in section 4. Asking for it on one that does not raises.

### 2. What is marked as personal

A schema author marks the properties whose values can identify a person: a `personal` flag on the property definition in the IR, available in the class DSL and in the YAML front-end. It is an additive field defaulting to `False`, so no existing schema changes meaning. A concept can also declare its `natural_key` personal (`personal_key`, also additive).

- **Scope of this version:** literal property values and the entity's natural key. A relation's `value_ref` is an id, not content, and links to an erased entity are left in place pointing at an entity that no longer carries any personal content. Marking relations personal is deliberately not offered yet.
- **Schema versions:** a predicate counts as personal if *any* schema version of the namespace marked it personal. That errs on the side of erasing too much. A predicate that was renamed is not followed across the rename; the schema author has to mark both names.
- Erasure only touches what is marked. It does not search free text for names.

### 3. The erasure operation

`Ontology.erase_subject(entity_id, *, actor, reason)` is available only in `redaction` mode and only to a human principal holding `admin`. An `ai` principal, a `service` principal and any `acting_as` delegation are refused. `reason` is required and recorded (a request reference, not personal data).

The domain layer computes a **redaction plan** (which rows and fields), and the backend applies it in one transaction. Erasure writes through the backend directly. It does **not** go through `retract()` or any proposal, so it does not run policy, does not hit the contradiction-party guard (`_reject_retract_if_party_to_contradiction`), and is not subject to the KI-043 capability floor: it has its own, stricter check. For one entity:

1. **Redact assertions.** For every assertion whose subject is the entity and whose predicate is personal, in any status: set `value_lit`, `source` and `rationale` to `NULL`, and replace `metadata` with a marker (`redacted`: by, at, erasure id). Keep `id`, `subject`, `predicate`, `author`, `asserted_at`, the validity window, `supersedes` and `proposal_id`. A `NULL` value, not a fixed string, so that typed Integer and Float comparisons see no value instead of `0.0`. Read paths treat a redacted row as having no value and exclude it from typed filters. Replacing `metadata` wholesale is a second named exception, next to the value, to SPEC §5.3's round-trip rule.
2. **Take it out of current state.** A redacted assertion that is `active` or `flagged` becomes `retracted` through the backend's status mutation, with the usual `assertion_event`. Current-state queries and conflict routing never compare against a redacted row. `as_of(t)` for a `t` before the erasure still shows that a fact existed, with the content gone.
3. **Handle every proposal that carries the subject's personal values,** in any state, found through the operations' subject id or, for a batch with local keys, through the `created_entities` map recorded when it was accepted:
   - A proposal that is still `submitted`, under review or awaiting changes is **rejected with reason "erased"** and its payload redacted. It must not be replayable: accepting a redacted payload would write tombstone assertions, and not redacting it would reintroduce the data.
   - An accepted or rejected proposal has the personal values in its payload blanked.
   - A `create_entity` operation's `natural_key` is blanked when the concept's key is personal.
4. **Redact the entity's natural key** when the concept declares it personal. Set it to `NULL`, which the `UNIQUE(namespace, concept, natural_key)` constraint does not collide on. A fixed tombstone would make a second erasure in the same concept fail.
5. **Close affected contradictions.** Contradictions are keyed by (subject, predicate), so every member of one on a personal predicate of the erased entity is redacted and retracted. Such a contradiction is closed as resolved by the erasure actor with **no winner**, a close `resolve_contradiction` cannot express, and its rationale history is blanked. Contradictions on the same entity's non-personal predicates stay open. No open contradiction is left whose members share a redacted value, which would otherwise read as corroboration under ADR-0053.
6. **Remove the vectors and the physical bytes** (section 4).
7. **Write the record.** Append an `erasure_record` (actor, entity, reason, time, counts). It holds no personal data and carries the three audit triggers.

`proposal_event.detail` is **not** redacted and keeps its unconditional triggers. It holds reviewer reasons, reviewer ids, the policy reason and the `{"created_entities": ...}` map; it does not hold assertion values. Weakening a KI-066 guarantee on an audit table for no systematic gain is not worth it. The one personal item that can land there is a caller-chosen `LocalRef` key, which section "Out of scope" lists. Re-indexing the entity's remaining text afterwards calls the embedder, which can be a remote plugin, so it runs after the transaction commits and a failure is reported, not rolled back.

### 4. Physical removal is part of the guarantee

Erasure is not complete until the bytes are gone from the database file, so the backend capability is defined by that bar.

- **SQLite:** a KB in `redaction` mode runs with `PRAGMA secure_delete=ON` for every connection. After the erasure transaction commits, the backend runs a WAL checkpoint (truncate) and `VACUUM`.
- **Vectors:** deleting a vector through `vector_id_map` is not enough, because `vec0` leaves the bytes in its chunk tables and earlier embeddings of the same text are already there. The backend **rebuilds the affected scope's virtual table** from the surviving vectors. That removes every earlier embedding, then the file is vacuumed.
- **A backend implements the capability only if it can meet this bar.** The DuckDB backend has the same concern (old row versions persist until a checkpoint or vacuum), and whether it can meet the bar is settled in the implementation spike. If it cannot, it simply does not implement the capability, and `erasure="redaction"` on it raises. Redaction is never offered with a weaker promise.
- A conformance vector writes a distinctive value, erases it, and asserts the bytes are absent from the database file and its journal.

The redaction work is a **separate optional backend protocol**, not new methods on `StorageBackend`. It covers applying a redaction plan, compacting storage, and reading and writing `kb_setting` and `erasure_record`. Existing third-party backends keep working unchanged and only lack the capability, so this is an additive minor-version change, not a breaking port change under ADR-0019.

### 5. Storage and versioning

- **New tables, `kb_setting` and `erasure_record`,** created with `CREATE TABLE IF NOT EXISTS`. The storage-format policy says a brand-new table needs no `format_version` bump (`migrations.py`), so there is **no** bump, no migration, and no `ontolith db migrate` step for any user. A migration would also have had to be irreversible or to refuse on a file with erasure history, because dropping `erasure_record` would erase the audit of what was erased.
- The cost of that choice is that an older build opening a `redaction`-mode file ignores the mode. It runs as a strict KB: it cannot erase, and it cannot undo anything. The release notes say not to open such a file with a build older than the one that introduces this.
- Because `_create_schema()` re-runs on every open, the audit triggers stay unconditional and nothing about this ADR makes them mode-aware.
- Third-party code that implements `StorageBackend` is unaffected.

### 6. Documentation

SPEC §3, §7.2 and §17 gain a note that the append-only guarantee is unconditional in `strict` mode and, in `redaction` mode, has named, audited exceptions: the redacted value, the replaced `metadata`, and the blanked payloads. The README and `site_docs` state the trade plainly.

## Rationale

- **An opt-in mode keeps the strong default.** Most users get exactly the guarantee Ontolith is known for. The exception exists only in a file that chose it, can be seen by reading its `kb_setting`, and the store itself refuses to switch it off.
- **Redaction rather than deletion keeps the structure.** Ids, links, timestamps, supersession chains and audit events survive, so `as_of` and provenance still work; only the content is gone.
- **Retracting the redacted assertion avoids a special case in conflict routing,** and closing contradictions with no winner avoids a tombstone being compared against.
- **Removing bytes, not just rows, is what makes it erasure.** The guarantee is defined by what is left in the file.
- **An optional protocol keeps this a minor release** and lets a backend that cannot meet the physical-removal bar opt out cleanly.
- **Human-only, admin-only, with a required reason** keeps the one destructive operation out of any agent's reach.

## Consequences

- **Out of scope, stated plainly:**
  - Backups, exports and replicas made before erasure.
  - Personal data in unmarked free text, including a name quoted in another principal's rationale or in a reviewer's reason.
  - Principal identifiers. A human is identified by email, and that id appears in `author`, `acting_as`, `actor`, `reviewers`, `resolved_by`, `policy_reason` and similar fields. Erasing a *principal* is a different operation from erasing a *subject* and is not designed here.
  - Application logs. The REST, GraphQL and MCP error handlers can log values and natural keys, so log retention has to be handled by the operator.
  - A caller-chosen `LocalRef` key stored in `proposal_event.detail`.
  - Re-ingestion. Nothing stops the same value being written again after erasure; a suppression list is a possible follow-up.
- **The invariant gets an exception.** In `redaction` mode a value is rewritten in place, once, to `NULL`, and `metadata` is replaced. That conflicts with SPEC §7.2 as written, which is why the exception is a named mode with its own audit record and not a general capability.
- **Cost.** `VACUUM` rewrites the database file, so an erasure on a large KB is a heavy, blocking operation, and the vector scope rebuild grows with the scope's size. Erasure is for rare requests, not routine use.
- **New surface:** the `erasure` argument, `enable_redaction`, `erase_subject`, the `personal` and `personal_key` schema fields, a `NULL`-valued redacted assertion that read paths must handle, and the optional backend protocol. All additive.
- **Redacted rows reach every read path.** REST, GraphQL, MCP and the CLI must render a redacted assertion as redacted, not as an error. The implementation PR lists these and tests each.

## Alternatives Considered

- **Crypto-shredding per subject.** Encrypt each personal value under a per-subject key and destroy the key on erasure. It keeps append-only fully intact, also covers backups, and sidesteps the physical-removal problem. Rejected for now: it needs a key store and key management that Ontolith does not have, every read and query path would have to decrypt, and filtering, search and embeddings over encrypted text do not work. It can be added later on top of the `personal` flag.
- **Hard delete.** Remove the rows. Rejected: it breaks `as_of`, supersession chains, contradiction membership and the audit trail, which is the reason the invariant exists.
- **Redaction always on.** Rejected: it removes the strict default for every user to serve some of them.
- **A per-connection flag instead of a stored mode.** Rejected: two processes could open one file with different guarantees, and the file would not say which one it was run under.
- **A reversible mode switch.** Rejected: after an erasure, a return to `strict` would restore the claim without restoring the data.
- **A `format_version` bump and a new method on `StorageBackend`.** Rejected: the bump forces `ontolith db migrate` on every user and, with the port change, makes this a major release under ADR-0019, for no benefit that the stored mode row does not already give.
- **Making `proposal_event` updatable during erasure.** Rejected: it weakens a store-level guarantee for a column that does not hold the subject's values, and a gate that any raw connection can open does not protect the guarantee.

## Update (2026-11-05): implementation, part 1

Landed: the `personal` and `personal_key` schema fields (IR, class DSL, generated stubs, LinkML YAML annotations `ontolith_personal` and `ontolith_personal_key`); the `kb_setting` and `erasure_record` tables with the three append-only triggers each plus the one-way trigger on `erasure_mode`; the optional `RedactionBackend` protocol (SQLite implements it, DuckDB does not yet); `Ontology(erasure=...)`, `Ontology.connect(erasure=...)`, `Ontology.erasure_mode` and `Ontology.enable_redaction`. No `format_version` change.

Decisions made while building it:

- **`strict` is the absence of a row.** The store refuses any `erasure_mode` row other than `redaction`, so there is no `strict` row to maintain and nothing to flip back.
- **"New knowledge base" means no principals.** `connect(erasure="redaction")` takes effect only on a knowledge base with no principals; any other strict knowledge base must use `enable_redaction`. The record's actor is `kb-setup` when the mode is chosen at creation.
- **Redaction needs the optional protocol.** Asking for it on a backend without `RedactionBackend` raises `ValidationError`.
- **"New" is checked in the same transaction as the write.** `enable_redaction(record, require_empty=True)` on the backend refuses unless there are no principals, entities, assertions, proposals or schemas, so nothing can be written between the check and the mode change. The rule is wider than "no principals" because data can be written straight to a backend.
- **The new tables are `WITHOUT ROWID`.** A `REPLACE` that collides on `rowid` instead of `id` would delete an audit row without firing the delete trigger (the `recursive_triggers` pragma is per-connection). The older audit tables have this hole; it is recorded as KI-117.
- **A `redaction` row needs its record.** The store refuses an `erasure_mode = redaction` row unless an `enabled` `erasure_record` with the same id exists, so no raw insert can turn the mode on without leaving an audit record.
- **A schema mark that cannot be read is an error.** The LinkML `ontolith_personal` annotation accepts only `true` or `false`; anything else, or the annotation on a relation, raises `SchemaError`, because a typo that reads as "not personal" is the unsafe direction.

## Update (2026-11-08): display follow-up

The schema views show which concepts declare a personal natural key (`personal_key` on the concept in the REST, GraphQL and MCP schema views, and in the CLI `schema show` heading), and the CLI `assertions` listing marks a redacted assertion. Both are additive.

## Update (2026-11-06): implementation, part 3

Landed: the DuckDB decision, `ontolith redaction status|enable` and `ontolith entity erase` (a confirmation prompt unless `--yes`; exit 1 with a warning when compaction is incomplete), the `personal` mark in the REST, GraphQL, MCP and CLI schema views, the `redacted` flag in the REST, GraphQL and MCP provenance views, the SPEC §5.3, §7 and §17 notes, and the README and concepts page.

- **DuckDB does not implement the capability.** The spike wrote a distinctive value, nulled it and then tried `CHECKPOINT`, `VACUUM`, closing and reopening, and rebuilding the table (`CREATE TABLE ... AS`, drop, rename). In every case the bytes stayed in the database file. Only copying the whole database into a fresh file (`COPY FROM DATABASE`) left them out, and swapping the file under a live connection is not something this backend can do safely. Redaction is never offered with a weaker promise, so DuckDB stays strict and `erasure="redaction"` on it raises.
- **MCP has no erase tool,** as for every write: erasure is a human admin action through the SDK or the CLI.

## Update (2026-11-06): implementation, part 2

Landed: `Ontology.erase_subject(entity_id, *, actor, reason)` on SQLite, as `RedactionBackend.apply_redaction(plan)` and `compact()`. The domain builds a `RedactionPlan` (pure functions in `govern/erasure.py`) from reads made inside the same write transaction that applies it, so nothing can land between planning and applying; after the commit the backend runs `VACUUM` and a WAL checkpoint. It covers assertions, the natural key, proposals, contradictions, vectors and the `erased` record, and a conformance vector asserts that the erased bytes are absent from the database file and its WAL.

Decisions made while building it:

- **A redacted value reads as an empty string, not `None`.** The column is `NULL`, which typed filters (`CAST(NULL AS REAL)`) never match, but `Assertion.value` stays `str`: widening it would be a breaking annotation change under the 1.x API freeze. `Assertion.redacted` (true when the metadata carries the `redacted` marker) tells a redacted row from an empty value.
- **Only literal assertions of the entity on personal predicates are redacted.** Rows already redacted are skipped, so a repeated erasure is a no-op that still writes its own record.
- **The plan names every change.** The backend decides nothing: a plan row that does not match (an id that is not a literal of that entity) aborts the whole transaction.
- **Closing a contradiction keeps the original resolver.** An already resolved contradiction on a personal predicate keeps its `resolved_by` and `resolved_at`; only its rationale history is replaced. An open one is closed by the erasing actor with no winner.
- **Pending proposals are rejected as `erased`;** decided ones keep their state and have the values blanked. A batch proposal is matched through its recorded `created_entities` map.
- **Vectors are rebuilt, not deleted.** Every scope's `vec0` table is recreated from the surviving vectors inside the transaction, and the entity is re-embedded from its remaining text afterwards. A failure there is reported on the result, not rolled back.
- **Compaction is checked and reported.** A WAL checkpoint cannot finish while another connection holds a read snapshot, and `VACUUM` needs disk space. The result then carries `compaction_error`; the data change is already committed, and calling `erase_subject` again (which finds nothing more to change) compacts again. Erasure therefore wants a moment without long-lived readers on the file.
- **A pending batch is rejected whole** if any of its operations carries this entity's personal value, including operations about other people; resubmitting the rest is the author's job.
- **Left out, in addition to section "Out of scope":** a pending batch that would re-create the same person has no entity yet to match, and a custom `PolicyStrategy` reason stored as `policy_reason` is not blanked (the built-in reasons hold no values).
- **`secure_delete` is on for every connection to a redaction-mode file,** set on open and when redaction is enabled.

Not done here, and still open: showing the `personal` marks and the `redacted` flag in the REST, GraphQL, MCP and CLI views (they serialise the empty value and the metadata marker as they stand).
