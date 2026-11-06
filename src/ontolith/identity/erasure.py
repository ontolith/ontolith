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


__all__ = ["ErasureMode", "ErasureRecord"]
