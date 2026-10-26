"""Typed operations for multi-operation proposals (SPEC §9.5, ADR-0055).

`Ontology.propose_batch` takes these instead of the stored payload dicts, so
the storage shape is not a public contract and a local key can never be
confused with a real entity id.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LocalRef(BaseModel):
    """A reference to an entity a `CreateEntityOp` earlier in the same batch
    declares.

    Stands in for a real entity id as an assertion op's `subject` or `target`.
    The real id is allocated when the proposal is accepted (ADR-0055).
    """

    key: str = Field(min_length=1)

    model_config = ConfigDict(frozen=True, extra="forbid")

    def __init__(self, key: str) -> None:
        super().__init__(key=key)


class CreateEntityOp(BaseModel):
    """Create a new entity inside a batch.

    Attributes:
        key: Name for the new entity, unique within the batch; later
            operations refer to it with `LocalRef(key)`
        concept: Concept of the new entity
        natural_key: Optional unique key within `concept`
    """

    key: str = Field(min_length=1)
    concept: str
    natural_key: str | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


class AssertLiteralOp(BaseModel):
    """Assert a literal value inside a batch.

    Fields mirror `Ontology.propose`, except `model` and `acting_as`, which are
    batch-level arguments of `propose_batch`. Unknown fields are rejected.
    """

    subject: str | LocalRef
    predicate: str
    value: str
    value_type: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: str | None = None
    rationale: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes: str | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


class AssertRefOp(BaseModel):
    """Assert a reference (relation) inside a batch.

    Fields mirror `Ontology.propose_ref`, except `model` and `acting_as`, which
    are batch-level arguments of `propose_batch`. Unknown fields are rejected.
    """

    subject: str | LocalRef
    predicate: str
    target: str | LocalRef
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: str | None = None
    rationale: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    supersedes: str | None = None

    model_config = ConfigDict(frozen=True, extra="forbid")


BatchOp = CreateEntityOp | AssertLiteralOp | AssertRefOp
"""One operation of `Ontology.propose_batch`."""

MAX_BATCH_OPERATIONS = 100
"""Upper bound on operations in one batch, so a single review stays reviewable."""

__all__ = [
    "AssertLiteralOp",
    "AssertRefOp",
    "BatchOp",
    "CreateEntityOp",
    "LocalRef",
    "MAX_BATCH_OPERATIONS",
]
