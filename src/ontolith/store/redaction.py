"""Optional storage capability: redaction mode (ADR-0056).

A separate protocol, not part of `StorageBackend`, so a backend written
against the core port keeps working unchanged and simply lacks the
capability. A backend should implement it only if it can meet the
physical-removal bar ADR-0056 sets for erasure.
"""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from ontolith.core.assertion import AssertionEvent
from ontolith.govern.proposal import ProposalEvent
from ontolith.identity.erasure import ErasureMode, ErasureRecord


class ProposalRedaction(BaseModel):
    """One proposal to blank, and whether to reject it as "erased".

    Attributes:
        proposal_id: Proposal to change
        payload: Its payload with the personal values already blanked
        reject_event: For a proposal still awaiting a decision: the `reject`
            event to append. The proposal moves to `rejected` with the policy
            reason `erased`. None for a proposal already decided.
    """

    proposal_id: str
    payload: dict[str, Any]
    reject_event: ProposalEvent | None = None

    model_config = ConfigDict(frozen=True)


class RedactionPlan(BaseModel):
    """Everything one erasure changes, computed by the domain (ADR-0056 §3).

    The backend applies it in a single transaction and does not decide
    anything itself.

    Attributes:
        record: The `erased` record to append (its `detail` is filled with the
            counts by the backend)
        entity_id: The subject being erased
        redact_natural_key: Set the entity's natural key to NULL
        assertion_ids: Assertions whose value, source and rationale are removed
        retractions: A `retracted` event for each of those that was still
            `active` or `flagged`; the assertion is retracted with it
        proposals: Proposals to blank, some also rejected
        contradiction_ids: Contradictions to close with no winner
    """

    record: ErasureRecord
    entity_id: str
    redact_natural_key: bool = False
    assertion_ids: tuple[str, ...] = ()
    retractions: tuple[AssertionEvent, ...] = ()
    proposals: tuple[ProposalRedaction, ...] = ()
    contradiction_ids: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class ErasureCounts(BaseModel):
    """What one applied plan changed."""

    assertions: int = 0
    proposals_rejected: int = 0
    proposals_redacted: int = 0
    contradictions_closed: int = 0

    model_config = ConfigDict(frozen=True)


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

    def apply_redaction(self, plan: RedactionPlan) -> ErasureCounts:
        """Apply `plan` inside the caller's open transaction (ADR-0056 §3).

        Blanks the assertions, natural key, proposals and contradictions the
        plan names, removes the entity's vectors and appends `plan.record`.
        The caller opens the transaction, builds the plan from reads made
        inside it (so nothing changes between planning and applying) and
        commits. Raises `ValidationError` unless the knowledge base is in
        redaction mode and a transaction is open. All or nothing.
        """
        ...

    def compact(self) -> str | None:
        """After the commit, remove the old bytes from the file (ADR-0056 §4).

        Returns None when the storage is clean, or a description of what could
        not be cleaned (for example a reader that prevents the log from being
        reset). Calling it again later is safe.
        """
        ...


__all__ = ["ErasureCounts", "ProposalRedaction", "RedactionBackend", "RedactionPlan"]
