"""Unit tests for Schema IR."""

import pytest

from ontolith.core.errors import SchemaError
from ontolith.schema import ConceptDef, PropertyDef, RelationDef, SchemaIR


class TestSchemaIR:
    """Tests for Schema Internal Representation."""

    def test_create_simple_schema(self) -> None:
        """Schema can be created with concepts."""
        person = ConceptDef(
            name="Person",
            properties={
                "name": PropertyDef(
                    name="name",
                    value_type="Text",
                    required=True,
                ),
                "born": PropertyDef(
                    name="born",
                    value_type="Date",
                    required=False,
                ),
            },
        )

        schema = SchemaIR(
            namespace="test-ns",
            version=1,
            concepts={"Person": person},
        )

        assert schema.namespace == "test-ns"
        assert schema.version == 1
        assert "Person" in schema.concepts
        assert schema.concepts["Person"].name == "Person"

    def test_schema_roundtrip_json(self) -> None:
        """Schema can serialize to/from JSON."""
        schema = SchemaIR(
            namespace="test-ns",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                    },
                ),
            },
        )

        # Serialize
        json_data = schema.to_json()
        assert json_data["namespace"] == "test-ns"
        assert json_data["version"] == 1

        # Deserialize
        restored = SchemaIR.from_json(json_data)
        assert restored.namespace == schema.namespace
        assert restored.version == schema.version
        assert "Person" in restored.concepts

    def test_property_defaults(self) -> None:
        """Properties have correct defaults."""
        prop = PropertyDef(name="age", value_type="Integer")

        assert prop.required is False
        assert prop.temporality == "static"
        assert prop.description is None

    def test_relation_with_inverse(self) -> None:
        """Relations can have inverse."""
        relation = RelationDef(
            name="employer",
            target_concept="Organization",
            inverse="employees",
            temporality="time_varying",
        )

        assert relation.name == "employer"
        assert relation.target_concept == "Organization"
        assert relation.inverse == "employees"
        assert relation.temporality == "time_varying"

    def test_schema_immutable(self) -> None:
        """Schema IR objects are immutable."""
        from pydantic import ValidationError

        schema = SchemaIR(namespace="test", version=1)

        with pytest.raises(ValidationError):
            schema.version = 2  # type: ignore

    def test_relation_to_unknown_concept_raises(self) -> None:
        """Relations to nonexistent concepts raise SchemaError."""
        from ontolith.core.errors import SchemaError

        person = ConceptDef(
            name="Person",
            relations={
                "employer": RelationDef(
                    name="employer",
                    target_concept="Organization",  # Doesn't exist!
                ),
            },
        )

        with pytest.raises(SchemaError, match="unknown concept"):
            SchemaIR(
                namespace="test",
                version=1,
                concepts={"Person": person},
            )

    def test_cardinality_defaults(self) -> None:
        """Cardinality defaults to single."""
        prop = PropertyDef(name="name", value_type="Text")
        assert prop.cardinality == "single"

        rel = RelationDef(name="employer", target_concept="Organization")
        assert rel.cardinality == "single"


class TestTemporalityOf:
    """SchemaIR.temporality_of() — schema-derived conflict routing (SPEC §10.1)."""

    def _schema(self) -> SchemaIR:
        return SchemaIR(
            namespace="test",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                        "employer_name": PropertyDef(
                            name="employer_name", value_type="Text", temporality="time_varying"
                        ),
                    },
                    relations={
                        "employer": RelationDef(
                            name="employer",
                            target_concept="Organization",
                            temporality="time_varying",
                        ),
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )

    def test_resolves_static_property(self) -> None:
        assert self._schema().temporality_of("Person.name") == "static"

    def test_resolves_time_varying_property(self) -> None:
        assert self._schema().temporality_of("Person.employer_name") == "time_varying"

    def test_resolves_time_varying_relation(self) -> None:
        assert self._schema().temporality_of("Person.employer") == "time_varying"

    def test_malformed_predicate_falls_back_to_static(self) -> None:
        """No '.' separator — falls back to static rather than raising."""
        assert self._schema().temporality_of("NoDotHere") == "static"

    def test_unknown_concept_falls_back_to_static(self) -> None:
        assert self._schema().temporality_of("Vehicle.make") == "static"

    def test_unknown_field_on_known_concept_falls_back_to_static(self) -> None:
        assert self._schema().temporality_of("Person.unknown_field") == "static"


class TestCardinalityOf:
    """SchemaIR.cardinality_of() — cardinality-aware conflict routing (ADR-0017)."""

    def _schema(self) -> SchemaIR:
        return SchemaIR(
            namespace="test",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                        "phone": PropertyDef(name="phone", value_type="Text", cardinality="many"),
                    },
                    relations={
                        "friend": RelationDef(
                            name="friend", target_concept="Person", cardinality="many"
                        ),
                    },
                ),
            },
        )

    def test_resolves_single_cardinality_default(self) -> None:
        assert self._schema().cardinality_of("Person.name") == "single"

    def test_resolves_many_cardinality_property(self) -> None:
        assert self._schema().cardinality_of("Person.phone") == "many"

    def test_resolves_many_cardinality_relation(self) -> None:
        assert self._schema().cardinality_of("Person.friend") == "many"

    def test_unknown_predicate_falls_back_to_single(self) -> None:
        assert self._schema().cardinality_of("Person.unknown_field") == "single"


class TestValueTypeOf:
    """SchemaIR.value_type_of() — write-time value_type validation (KI-031, ADR-0028)."""

    def _schema(self) -> SchemaIR:
        return SchemaIR(
            namespace="test",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                        "age": PropertyDef(name="age", value_type="Integer"),
                    },
                    relations={
                        "employer": RelationDef(name="employer", target_concept="Organization"),
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )

    def test_resolves_declared_value_type(self) -> None:
        assert self._schema().value_type_of("Person.name") == "Text"
        assert self._schema().value_type_of("Person.age") == "Integer"

    def test_relation_has_no_value_type(self) -> None:
        """Relations have no value_type — that's a predicate-kind mismatch (KI-040), not this."""
        assert self._schema().value_type_of("Person.employer") is None

    def test_unknown_predicate_returns_none(self) -> None:
        assert self._schema().value_type_of("Person.unknown_field") is None

    def test_malformed_predicate_returns_none(self) -> None:
        assert self._schema().value_type_of("NoDotHere") is None


class TestKindOf:
    """SchemaIR.kind_of() — write-time predicate-kind validation (KI-040)."""

    def _schema(self) -> SchemaIR:
        return SchemaIR(
            namespace="test",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={
                        "name": PropertyDef(name="name", value_type="Text"),
                    },
                    relations={
                        "employer": RelationDef(name="employer", target_concept="Organization"),
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )

    def test_property_resolves_to_property(self) -> None:
        assert self._schema().kind_of("Person.name") == "property"

    def test_relation_resolves_to_relation(self) -> None:
        assert self._schema().kind_of("Person.employer") == "relation"

    def test_unknown_predicate_returns_none(self) -> None:
        assert self._schema().kind_of("Person.unknown_field") is None

    def test_malformed_predicate_returns_none(self) -> None:
        assert self._schema().kind_of("NoDotHere") is None


class TestPropertyRelationNameCollision:
    """ConceptDef rejects a field name declared as both a property and a
    relation (KI-040) - _resolve_field checks properties first, so an
    unrejected collision would make the relation half permanently
    unreachable (every ref write to that name would fail the kind
    check it could never satisfy)."""

    def test_collision_raises(self) -> None:
        with pytest.raises(SchemaError, match="employer"):
            ConceptDef(
                name="Person",
                properties={"employer": PropertyDef(name="employer", value_type="Text")},
                relations={"employer": RelationDef(name="employer", target_concept="Organization")},
            )

    def test_no_collision_is_fine(self) -> None:
        concept = ConceptDef(
            name="Person",
            properties={"name": PropertyDef(name="name", value_type="Text")},
            relations={"employer": RelationDef(name="employer", target_concept="Organization")},
        )
        assert concept.properties.keys().isdisjoint(concept.relations.keys())


class TestHasPredicate:
    """SchemaIR.has_predicate() — write-time unknown-predicate validation."""

    def _schema(self) -> SchemaIR:
        return SchemaIR(
            namespace="test",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={"name": PropertyDef(name="name", value_type="Text")},
                    relations={
                        "employer": RelationDef(name="employer", target_concept="Organization"),
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )

    def test_known_property_is_true(self) -> None:
        assert self._schema().has_predicate("Person.name") is True

    def test_known_relation_is_true(self) -> None:
        assert self._schema().has_predicate("Person.employer") is True

    def test_unknown_field_on_known_concept_is_false(self) -> None:
        assert self._schema().has_predicate("Person.nmae") is False

    def test_unknown_concept_is_false(self) -> None:
        assert self._schema().has_predicate("Vehicle.make") is False

    def test_malformed_predicate_is_false(self) -> None:
        assert self._schema().has_predicate("NoDotHere") is False


class TestHasConcept:
    """SchemaIR.has_concept() — write-time unknown-concept validation (KI-090)."""

    def _schema(self) -> SchemaIR:
        return SchemaIR(
            namespace="test",
            version=1,
            concepts={
                "Person": ConceptDef(name="Person"),
            },
        )

    def test_known_concept_is_true(self) -> None:
        assert self._schema().has_concept("Person") is True

    def test_unknown_concept_is_false(self) -> None:
        assert self._schema().has_concept("Vehicle") is False


class TestTargetConceptOf:
    """SchemaIR.target_concept_of() — relation target concept lookup (KI-112)."""

    def _schema(self) -> SchemaIR:
        return SchemaIR(
            namespace="test",
            version=1,
            concepts={
                "Person": ConceptDef(
                    name="Person",
                    properties={"name": PropertyDef(name="name", value_type="Text")},
                    relations={
                        "employer": RelationDef(name="employer", target_concept="Organization")
                    },
                ),
                "Organization": ConceptDef(name="Organization"),
            },
        )

    def test_relation_returns_declared_target(self) -> None:
        assert self._schema().target_concept_of("Person.employer") == "Organization"

    def test_property_returns_none(self) -> None:
        assert self._schema().target_concept_of("Person.name") is None

    def test_unknown_predicate_returns_none(self) -> None:
        assert self._schema().target_concept_of("Person.missing") is None
        assert self._schema().target_concept_of("Vehicle.owner") is None


class TestRelationDefTargetAlias:
    """RelationDef.target — read-only alias for target_concept, matching the
    DSL's `Ref["X"]` wording (O-19)."""

    def test_target_aliases_target_concept(self) -> None:
        rel = RelationDef(name="employer", target_concept="Organization")
        assert rel.target == "Organization"

    def test_alias_is_not_a_serialized_field(self) -> None:
        rel = RelationDef(name="employer", target_concept="Organization")
        assert "target" not in rel.model_dump()
        assert "target" not in RelationDef.model_fields

    def test_alias_is_read_only(self) -> None:
        rel = RelationDef(name="employer", target_concept="Organization")
        with pytest.raises((AttributeError, ValueError)):
            rel.target = "Other"  # type: ignore[misc]
