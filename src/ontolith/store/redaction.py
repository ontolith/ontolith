"""Optional storage capability: redaction mode (ADR-0056).

A separate protocol, not part of `StorageBackend`, so a backend written
against the core port keeps working unchanged and simply lacks the
capability. A backend should implement it only if it can meet the
physical-removal bar ADR-0056 sets for erasure.
"""

from typing import Protocol, runtime_checkable

from ontolith.identity.erasure import ErasureMode, ErasureRecord


@runtime_checkable
class RedactionBackend(Protocol):
    """A backend that can hold a knowledge base in redaction mode."""

    def erasure_mode(self) -> ErasureMode:
        """The stored erasure mode; `strict` when none was ever recorded."""
        ...

    def enable_redaction(self, record: ErasureRecord, *, require_empty: bool = False) -> None:
        """Atomically store the mode `redaction` and append `record`.

        Idempotent: already in redaction mode writes nothing. The change is
        one-way: the store itself refuses to record `strict` again, and
        refuses to record `redaction` without a matching `record`.

        Args:
            record: The `enabled` record to append (`kind` must be `enabled`)
            require_empty: Raise `ValidationError`, writing nothing, unless the
                knowledge base holds no principals, entities, assertions,
                proposals or schemas. The check and the write are one
                transaction, so nothing can be written in between.
        """
        ...

    def erasure_records(self) -> list[ErasureRecord]:
        """Every erasure record, oldest first."""
        ...


__all__ = ["RedactionBackend"]
