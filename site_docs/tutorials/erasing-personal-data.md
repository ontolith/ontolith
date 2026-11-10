# Erasing Personal Data

Full source:
[`examples/erasing_personal_data.py`](https://github.com/ontolith/ontolith/blob/main/examples/erasing_personal_data.py)

This tutorial covers the opt-in `redaction` mode: how to set it up, how to
mark what identifies a person, how a human admin erases it, and how to
check that the data is gone from the file as well as from the rows.

By default a knowledge base is **strict**: assertions are append-only and
nothing is ever altered or removed. That is the right default for most uses,
and nothing here changes it. Redaction mode exists for the case where the
law requires you to erase a person's data on request.

## Choosing the mode

Create a knowledge base in redaction mode, or move an existing one over once:

```python
from ontolith import Ontology

kb = Ontology.connect("kb.db", erasure="redaction")  # a new knowledge base

kb = Ontology.connect("kb.db")  # an existing, strict one
kb.enable_redaction("dpo@example.com")  # a human admin; one-way, and recorded
```

The mode is stored in the file, so every later connection sees it. You cannot
switch back to `strict`, and asking for `erasure="strict"` on a redaction file
raises. `Ontology.erasure_mode` tells you which one you have.

The SQLite backend supports the mode. The DuckDB backend does not, because it
has no way to remove old bytes from its file in place, so asking for redaction
on it raises rather than promising something it cannot do.

## Marking what identifies a person

An erasure only touches what the schema marks. Mark a property with
`personal=True`, and a concept whose natural key is itself personal with
`__personal_key__ = True`:

```python
from ontolith.schema import Concept, Property, Text, compile_schema


class Customer(Concept):
    __personal_key__ = True  # the natural key is a name
    name: Text = Property(personal=True)
    plan: Text | None = None  # not personal: an erasure leaves it alone
```

A predicate counts as personal if **any** version of the schema marked it, so
marking a property later still covers values written before. A renamed property
is not followed across the rename: mark both names.

## Who may erase

Only a human principal holding `admin`. An AI or service principal is refused
whatever capability it holds, and so is any delegation:

```python
try:
    kb.erase_subject(entity_id, actor="support-bot", reason="REQ-1")
except CapabilityError as exc:
    print(exc)  # Principal support-bot lacks admin capability
```

There is no erase tool in the MCP server, as for every write: erasure is a
human admin action through the SDK or the CLI.

## Erasing a subject

```python
result = kb.erase_subject(entity_id, actor="dpo@example.com", reason="REQ-2026-014")
```

`reason` is a request reference and goes into an append-only erasure record. It
must not be personal data. The call, in one transaction:

- sets the value of every marked property of the entity to NULL (and clears the
  assertion's `source`, `rationale` and `metadata`) and retracts the assertion,
- blanks the natural key if the concept marks it personal,
- rejects pending proposals that carry the values, as "erased", and blanks the
  payload of every proposal that carried them,
- closes affected contradictions with no winner,
- removes the entity's vectors from the search index,

and then removes the bytes from the database file. The entity, its id and its
non-personal assertions stay. The returned `ErasureResult` holds the counts.

The same thing from the command line, with a confirmation prompt unless you
pass `--yes`:

```bash
ontolith --db kb.db redaction status
ontolith --db kb.db redaction enable --author dpo@example.com
ontolith --db kb.db entity erase <entity-id> --author dpo@example.com --reason REQ-2026-014
```

## Seeing what was erased

An erased value reads as an empty string. `Assertion.redacted` tells it apart
from a value that really was empty:

```python
assertion = kb.provenance(assertion_id).assertion
print(repr(assertion.value), assertion.redacted)  # '' True
```

The schema views show what is marked: `ontolith schema show`, `GET /schema`,
the GraphQL `schema` query and the MCP `ontolith.schema` tool report `personal`
on each property and `personal_key` on each concept. The provenance views
(REST, GraphQL, MCP) report `redacted`, and so does the CLI `ontolith
assertions` listing.

## Checking the bytes are gone

The example looks for a distinctive string in the file and its write-ahead log
before and after the erasure:

```python
wal = db_path.with_name(db_path.name + "-wal")
data = db_path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
print(PERSON_NAME.encode() in data)  # True before the erasure, False after
```

Look in the write-ahead log as well as the main file: a fresh write sits in the
log until it is checkpointed, so checking the main file alone can say
"absent" before the erasure has done anything.

## When the cleanup cannot finish

Removing the bytes needs a checkpoint of the write-ahead log. If another
process holds the file open for reading, the checkpoint can be blocked. The data
change is already committed, so the values are gone from the rows, but the bytes
may remain until the checkpoint runs. This is reported, not hidden:

```python
result = kb.erase_subject(entity_id, actor="dpo@example.com", reason="REQ-2026-014")
if result.compaction_error:
    # close the other reader, then run the erasure again to finish the cleanup
    ...
```

On the command line the same case exits with status 1 and a warning. Running the
erasure again finishes the cleanup. `reindex_error` is reported the same way if
the entity's remaining text could not be re-embedded.

## What it does not reach

- **Backups and exports** you made before the erasure.
- **Free text** the schema did not mark, principal ids and logs.
- A file moved to redaction mode **must not be opened with a release older than
  1.2.0**: it would not know about the mode and would treat the file as strict.

The reasoning and the alternatives that were rejected are in
[ADR-0056](https://github.com/ontolith/ontolith/blob/main/docs/adr/ADR-0056-opt-in-redaction-of-personal-data.md).
