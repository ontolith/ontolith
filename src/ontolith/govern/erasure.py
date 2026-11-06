"""Planning an erasure (ADR-0056 §3): pure functions, no I/O.

Given what is stored about one entity, work out what an erasure changes. The
backend applies the result; nothing here reads or writes storage.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from ontolith.schema.ir import SchemaIR

# A proposal in one of these states has been decided and applied (or refused).
# Any other state is still awaiting a decision and must not be replayable.
_DECIDED_STATES = frozenset({"accepted", "auto_accepted", "rejected"})


def personal_predicates(schemas: Iterable[SchemaIR]) -> frozenset[str]:
    """Predicates (`Concept.field`) marked personal in *any* schema version.

    Erring on the side of erasing too much is deliberate (ADR-0056 §2).
    """
    return frozenset(
        f"{concept.name}.{name}"
        for schema in schemas
        for concept in schema.concepts.values()
        for name, prop in concept.properties.items()
        if prop.personal
    )


def personal_key_concepts(schemas: Iterable[SchemaIR]) -> frozenset[str]:
    """Concepts whose natural key is marked personal in any schema version."""
    return frozenset(
        concept.name
        for schema in schemas
        for concept in schema.concepts.values()
        if concept.personal_key
    )


def is_pending(state: str) -> bool:
    """Whether a proposal in `state` is still awaiting a decision."""
    return state not in _DECIDED_STATES


def redact_operations(
    payload: Mapping[str, Any],
    *,
    entity_id: str,
    local_keys: frozenset[str],
    personal: frozenset[str],
    redact_key: bool,
) -> tuple[dict[str, Any], bool]:
    """Blank the erased entity's personal values in a proposal payload.

    Args:
        payload: The stored proposal payload
        entity_id: The entity being erased
        local_keys: Batch-local keys that the proposal's accepted result
            mapped to `entity_id`
        personal: Personal predicates (`personal_predicates`)
        redact_key: Whether the entity's concept has a personal natural key

    Returns:
        The payload with those values set to None, and whether any operation
        was touched. Operations about other entities, and non-personal
        operations, are returned unchanged.
    """
    touched = False
    operations: list[Any] = []
    for op in payload.get("operations") or []:
        if not isinstance(op, dict):
            operations.append(op)
            continue
        about_entity = op.get("subject") == entity_id or (
            op.get("subject") is None and op.get("subject_key") in local_keys
        )
        if op.get("kind") == "assert_literal" and about_entity and op.get("predicate") in personal:
            op = {**op, "value": None, "source": None, "rationale": None}
            touched = True
        elif (
            op.get("kind") == "create_entity"
            and redact_key
            and op.get("key") in local_keys
            and op.get("natural_key") is not None
        ):
            op = {**op, "natural_key": None}
            touched = True
        operations.append(op)
    return {**payload, "operations": operations}, touched


__all__ = [
    "is_pending",
    "personal_key_concepts",
    "personal_predicates",
    "redact_operations",
]
