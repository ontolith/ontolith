"""Internal Representation (IR) for schema definitions.

The IR is a JSON-serializable format that serves as the single source of truth
for schema definitions. Multiple front-ends (class DSL, YAML) compile to IR.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PropertyDef(BaseModel):
    """Property definition in the IR.

    Attributes:
        name: Property name
        value_type: Type of value (Text, Integer, Date, etc.)
        cardinality: Single value or many (SPEC §6)
        required: Whether the property is required
        temporality: How conflicts are handled (static vs time_varying)
        description: Optional description
    """

    name: str
    value_type: Literal["Text", "Integer", "Float", "Boolean", "Date", "DateTime", "URI", "JSON"]
    cardinality: Literal["single", "many"] = "single"
    required: bool = False
    temporality: Literal["static", "time_varying"] = "static"
    description: str | None = None

    model_config = {"frozen": True}


class RelationDef(BaseModel):
    """Relation definition in the IR.

    Attributes:
        name: Relation name
        target_concept: Target concept name
        cardinality: Single reference or many (SPEC §6)
        required: Whether the relation is required
        temporality: How conflicts are handled
        inverse: Inverse relation name (optional)
        description: Optional description
    """

    name: str
    target_concept: str
    cardinality: Literal["single", "many"] = "single"
    required: bool = False
    temporality: Literal["static", "time_varying"] = "static"
    inverse: str | None = None
    description: str | None = None

    model_config = {"frozen": True}

    @property
    def target(self) -> str:
        """Alias for `target_concept`, matching the class DSL's `Ref["X"]`
        wording. Read-only and not a serialized field."""
        return self.target_concept


class ConceptDef(BaseModel):
    """Concept definition in the IR.

    Attributes:
        name: Concept name (e.g., "Person", "Organization")
        properties: Property definitions
        relations: Relation definitions
        description: Optional description
    """

    name: str
    properties: dict[str, PropertyDef] = Field(default_factory=dict)
    relations: dict[str, RelationDef] = Field(default_factory=dict)
    description: str | None = None

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def validate_no_property_relation_name_collision(self) -> "ConceptDef":
        """Reject a field name declared in both `properties` and
        `relations` on the same concept (KI-040).

        `SchemaIR._resolve_field` checks `properties` before `relations`,
        so an unrejected collision would silently make `kind_of()`
        (and `value_type_of`/`temporality_of`/`cardinality_of`) always
        resolve to the property, making the relation half permanently
        unreachable — every ref write to that name would be rejected as a
        kind mismatch it could never satisfy. Not reachable via the class
        DSL or the LinkML front-end today (both keep properties/relations
        in one namespace), only via a hand-built `SchemaIR`.
        """
        collisions = self.properties.keys() & self.relations.keys()
        if collisions:
            from ontolith.core.errors import SchemaError

            raise SchemaError(
                f"Concept {self.name!r} declares {sorted(collisions)} as both a "
                "property and a relation — a field name must be one or the other"
            )
        return self


class SchemaIR(BaseModel):
    """Complete schema definition in Internal Representation.

    This is the canonical format for schema. All DSLs compile to this.

    Attributes:
        namespace: Namespace this schema belongs to
        version: Schema version (monotonically increasing)
        concepts: Concept definitions by name
        metadata: Optional metadata
    """

    namespace: str
    version: int
    concepts: dict[str, ConceptDef] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_relation_targets(self) -> "SchemaIR":
        """Validate that all relation targets reference existing concepts."""
        for concept in self.concepts.values():
            for relation in concept.relations.values():
                if relation.target_concept not in self.concepts:
                    from ontolith.core.errors import SchemaError

                    raise SchemaError(
                        f"Relation {concept.name}.{relation.name} references "
                        f"unknown concept: {relation.target_concept}"
                    )
        return self

    model_config = {"frozen": True}

    def _resolve_field(self, predicate: str) -> PropertyDef | RelationDef | None:
        """Resolve a dotted "Concept.field" predicate to its declaration.

        Returns None if the concept or field isn't declared in this schema —
        e.g. a schema-less namespace, or a field not yet added to the
        active schema version.
        """
        concept_name, _, field_name = predicate.partition(".")
        if not field_name:
            return None
        concept = self.concepts.get(concept_name)
        if concept is None:
            return None
        prop = concept.properties.get(field_name)
        if prop is not None:
            return prop
        return concept.relations.get(field_name)

    def has_predicate(self, predicate: str) -> bool:
        """Return whether a dotted "Concept.field" predicate is declared in this schema."""
        return self._resolve_field(predicate) is not None

    def has_concept(self, concept: str) -> bool:
        """Return whether `concept` (e.g. "Person") is declared in this schema (KI-090)."""
        return concept in self.concepts

    def temporality_of(self, predicate: str) -> Literal["static", "time_varying"]:
        """Resolve the declared temporality of a predicate (SPEC §10.1).

        Args:
            predicate: Dotted predicate, e.g. "Person.name" or "Person.employer"

        Returns:
            The property's or relation's declared temporality, or "static"
            (SPEC's stated default) if unresolvable.
        """
        field = self._resolve_field(predicate)
        return field.temporality if field is not None else "static"

    def cardinality_of(self, predicate: str) -> Literal["single", "many"]:
        """Resolve the declared cardinality of a predicate (SPEC §4, ADR-0017).

        Args:
            predicate: Dotted predicate, e.g. "Person.name" or "Person.phone"

        Returns:
            The property's or relation's declared cardinality, or "single"
            (the schema default) if unresolvable.
        """
        field = self._resolve_field(predicate)
        return field.cardinality if field is not None else "single"

    def target_concept_of(self, predicate: str) -> str | None:
        """Resolve the declared target concept of a relation predicate.

        Args:
            predicate: Dotted predicate, e.g. "Person.employer"

        Returns:
            The relation's `target_concept`, or None if the predicate is
            unresolvable or resolves to a PropertyDef — properties have no
            target concept.
        """
        field = self._resolve_field(predicate)
        return field.target_concept if isinstance(field, RelationDef) else None

    def value_type_of(self, predicate: str) -> str | None:
        """Resolve the declared value_type of a literal property predicate
        (SPEC §4, KI-031).

        Args:
            predicate: Dotted predicate, e.g. "Person.name" or "Person.age"

        Returns:
            The declared value_type, or None if the predicate is
            unresolvable or resolves to a RelationDef — relations have no
            value_type. Use `kind_of()` to check a predicate's kind
            directly (KI-040) rather than inferring it from a `None` here.
        """
        field = self._resolve_field(predicate)
        return field.value_type if isinstance(field, PropertyDef) else None

    def kind_of(self, predicate: str) -> Literal["property", "relation"] | None:
        """Resolve whether a predicate is declared a property or a relation
        (SPEC §4, KI-040).

        Args:
            predicate: Dotted predicate, e.g. "Person.name" or "Person.employer"

        Returns:
            `"property"` or `"relation"`, or `None` if the predicate is
            unresolvable (schema-less namespace, or not declared in this
            schema version).
        """
        field = self._resolve_field(predicate)
        if field is None:
            return None
        return "property" if isinstance(field, PropertyDef) else "relation"

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return self.model_dump()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SchemaIR":
        """Deserialize from JSON-compatible dict."""
        return cls.model_validate(data)


__all__ = ["PropertyDef", "RelationDef", "ConceptDef", "SchemaIR"]
