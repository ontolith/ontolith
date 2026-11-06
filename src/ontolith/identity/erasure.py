"""ErasureRecord - append-only audit record of the erasure mode (ADR-0056).

A knowledge base is `strict` (the default: nothing is ever altered) or
`redaction` (a human admin may erase a subject's personal data). Moving to
`redaction` is recorded here; so is every erasure once it exists. A record
holds no personal data.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ErasureMode = Literal["strict", "redaction"]
"""The two erasure modes of a knowledge base (ADR-0056)."""


class ErasureRecord(BaseModel):
    """One append-only record of redaction being enabled, or of an erasure.

    Attributes:
        id: Unique record ID (ULID)
        kind: `enabled` when the KB moved to (or was created in) redaction
            mode; `erased` for one erasure of a subject
        actor: Principal who did it, or `kb-setup` when the mode was chosen
            at creation, before any principal existed
        at: When it happened
        subject: Entity ID erased (`erased` records only)
        reason: Request reference for an erasure (never personal data)
        detail: Optional machine-readable counts of what was erased
    """

    id: str
    kind: Literal["enabled", "erased"]
    actor: str
    at: datetime
    subject: str | None = None
    reason: str | None = None
    detail: str | None = None

    model_config = ConfigDict(frozen=True)


class ErasureResult(BaseModel):
    """What one `Ontology.erase_subject` call did.

    Attributes:
        erasure_id: ID of the `erased` ErasureRecord
        entity_id: The erased entity
        assertions_redacted: Assertions whose personal value was removed
        proposals_rejected: Pending proposals rejected as "erased"
        proposals_redacted: Proposals whose payload was blanked (includes the
            rejected ones)
        contradictions_closed: Open contradictions closed with no winner
        compaction_error: Set if the old bytes could not all be removed from
            the file (for example another connection is reading). The data
            change is committed; call `erase_subject` again once readers have
            finished, which compacts again.
        reindex_error: Set if re-embedding the entity's remaining text failed
            afterwards. The erasure itself is complete; run `reindex()` later.
    """

    erasure_id: str
    entity_id: str
    assertions_redacted: int
    proposals_rejected: int
    proposals_redacted: int
    contradictions_closed: int
    compaction_error: str | None = None
    reindex_error: str | None = None

    model_config = ConfigDict(frozen=True)


__all__ = ["ErasureMode", "ErasureRecord", "ErasureResult"]
