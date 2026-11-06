"""Storage layer and backend abstractions."""

from ontolith.store.base import DEFAULT_NAMESPACE, VECTOR_SCOPES, StorageBackend
from ontolith.store.redaction import RedactionBackend

__all__ = ["StorageBackend", "RedactionBackend", "VECTOR_SCOPES", "DEFAULT_NAMESPACE"]
