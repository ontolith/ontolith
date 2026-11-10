#!/usr/bin/env python3
"""Erasing personal data (opt-in redaction mode) example.

This example demonstrates (ADR-0056):
- A knowledge base created in `redaction` mode (the default is `strict`:
  nothing is ever altered or removed)
- Marking what identifies a person in the schema: `Property(personal=True)`
  for a value, `__personal_key__ = True` for a concept's natural key
- Who may erase: a human principal holding `admin`, and nobody else
- What an erasure does: the personal values and the natural key are removed,
  their assertions are retracted, and the bytes leave the database file; the
  entity, its id and its non-personal facts stay
- How to tell a redacted value from one that was empty: `Assertion.redacted`
- Why you may need to run an erasure twice: if another process holds the
  file open, the byte removal can be incomplete, and that is reported
"""

import tempfile
from pathlib import Path

from ontolith import Ontology
from ontolith.core.errors import CapabilityError, ValidationError
from ontolith.schema import Concept, Property, Text, compile_schema

SECRET = "Ada Quuxplugh Lovelace"  # a distinctive string, so we can look for it in the file


def _on_disk(db_path: Path) -> bool:
    """Whether the secret is anywhere in the database file or its write-ahead log."""
    wal = db_path.with_name(db_path.name + "-wal")
    data = db_path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
    return SECRET.encode() in data


def main() -> None:
    """Run the erasing-personal-data example."""
    directory = tempfile.mkdtemp()
    db_path = Path(directory) / "kb.db"
    print("🧹 Ontolith Erasing Personal Data Example\n")

    # 1. A knowledge base is strict unless it is created in redaction mode
    #    (or moved there once with `kb.enable_redaction(admin)`, which is
    #    one-way). The mode is stored in the file, so every later connection
    #    sees it.
    kb = Ontology.connect(db_path, erasure="redaction")
    print(f"1. erasure mode: {kb.erasure_mode}")

    admin = kb.create_principal("dpo@example.com", kind="human", default_capability="admin")
    bot = kb.create_principal(
        "support-bot", kind="ai", owner=admin.id, default_capability="propose"
    )

    # 2. The schema says what identifies a person. Everything not marked is
    #    left alone by an erasure.
    class Customer(Concept):
        __personal_key__ = True  # the natural key is a name, so it is personal too
        name: Text = Property(personal=True)
        plan: Text | None = None

    kb.apply_schema(compile_schema("default", 1, Customer), author=admin.id)

    ada = kb.create_entity(concept="Customer", author=admin.id, natural_key=SECRET)
    name = kb.assert_literal(
        subject=ada.id, predicate="Customer.name", value=SECRET, value_type="Text", author=admin.id
    )
    kb.assert_literal(
        subject=ada.id, predicate="Customer.plan", value="pro", value_type="Text", author=admin.id
    )
    print(f"2. recorded {SECRET!r} and a plan for the customer")
    print(f"   the value is on disk now: {_on_disk(db_path)}")

    # 3. Only a human admin may erase. An AI principal is refused, whatever
    #    capability it holds.
    try:
        kb.erase_subject(ada.id, actor=bot.id, reason="REQ-1")
    except CapabilityError as exc:
        print(f"3. the bot cannot erase: {exc}")

    # 4. The erasure itself. `reason` is a request reference that goes into the
    #    erasure record; it must not be personal data.
    result = kb.erase_subject(ada.id, actor=admin.id, reason="REQ-2026-014")
    print(
        f"4. erased: {result.assertions_redacted} assertion(s) redacted, "
        f"{result.proposals_rejected} pending proposal(s) rejected"
    )
    if result.compaction_error:
        # Another process had the file open, so the cleanup is incomplete. The
        # data change is committed; running the erasure again finishes it.
        print(f"   warning: {result.compaction_error} -- run the erasure again")

    # 5. What is left. The personal value reads as an empty string, and
    #    `redacted` tells it apart from a value that really was empty. The
    #    non-personal fact and the entity stay.
    redacted = kb.provenance(name.id).assertion
    print(f"5. the name now reads {redacted.value!r}, redacted={redacted.redacted}")
    remaining = kb.assertions(subject=ada.id, status="active")
    print(f"   still active: {[(a.predicate, a.value) for a in remaining]}")
    entity = kb.get_entity(ada.id)
    print(f"   entity kept, natural key now {entity.natural_key if entity else None!r}")

    # 6. The bytes are gone from the file and its write-ahead log, not only
    #    from the rows.
    print(f"6. the value is on disk now: {_on_disk(db_path)}")
    kb.close()

    # A strict knowledge base has no erasure at all.
    strict = Ontology.connect(Path(directory) / "strict.db")
    strict.create_principal("dpo@example.com", kind="human", default_capability="admin")
    try:
        strict.erase_subject("anything", actor="dpo@example.com", reason="REQ-1")
    except ValidationError as exc:
        print(f"\nA strict knowledge base refuses: {exc}")
    strict.close()

    print("\n✅ Done.")


if __name__ == "__main__":
    main()
