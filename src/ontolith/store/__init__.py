"""Storage layer and backend abstractions."""

from ontolith.store.base import DEFAULT_NAMESPACE, VECTOR_SCOPES, StorageBackend
from ontolith.store.redaction import (
    ErasureCounts,
    ProposalRedaction,
    RedactionBackend,
    RedactionPlan,
)

__all__ = [
    "StorageBackend",
    "RedactionBackend",
    "RedactionPlan",
    "ProposalRedaction",
    "ErasureCounts",
    "VECTOR_SCOPES",
    "DEFAULT_NAMESPACE",
]
