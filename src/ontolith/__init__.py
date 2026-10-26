"""Ontolith: A Python framework for collaborative knowledge bases.

Where humans and AI agents are co-equal authors of a shared, governed ontology.
"""

from ontolith.core import Assertion, Entity, FixedClock, Namespace, SequentialIdProvider
from ontolith.govern import AssertLiteralOp, AssertRefOp, CreateEntityOp, LocalRef
from ontolith.identity import Principal
from ontolith.ontology import AsOfView, Ontology
from ontolith.schema import (
    JSON,
    URI,
    Boolean,
    Concept,
    Date,
    DateTime,
    Float,
    Integer,
    Property,
    Ref,
    Relation,
    Text,
)

__version__ = "1.0.0"

__all__ = [
    "Ontology",
    "AsOfView",
    "Entity",
    "Assertion",
    "Namespace",
    "Principal",
    "CreateEntityOp",
    "AssertLiteralOp",
    "AssertRefOp",
    "LocalRef",
    "FixedClock",
    "SequentialIdProvider",
    "Concept",
    "Relation",
    "Property",
    "Ref",
    "Text",
    "Integer",
    "Float",
    "Boolean",
    "Date",
    "DateTime",
    "URI",
    "JSON",
]
