"""`personal` / `personal_key` schema marks (ADR-0056) survive every front-end."""

from __future__ import annotations

import pytest

from ontolith import Concept, Property, Text
from ontolith.core.errors import SchemaError
from ontolith.schema import SchemaIR, compile_schema
from ontolith.schema.dsl import generate_class_stubs
from ontolith.schema.ir import ConceptDef, PropertyDef
from ontolith.schema.linkml import from_yaml, to_yaml


class Person(Concept):
    __personal_key__ = True
    name: Text = Property(personal=True)
    nickname: Text | None = None


class Org(Concept):
    title: Text


def _schema() -> SchemaIR:
    return compile_schema("people", 1, Person, Org)


def test_marks_default_to_false() -> None:
    assert PropertyDef(name="x", value_type="Text").personal is False
    assert ConceptDef(name="C").personal_key is False


def test_class_dsl_compiles_the_marks() -> None:
    schema = _schema()
    person = schema.concepts["Person"]
    assert person.personal_key is True
    assert person.properties["name"].personal is True
    assert person.properties["nickname"].personal is False
    assert schema.concepts["Org"].personal_key is False


def test_schema_without_the_fields_still_loads() -> None:
    """A stored IR written before these fields existed has neither key."""
    raw = {
        "namespace": "n",
        "version": 1,
        "concepts": {"C": {"name": "C", "properties": {"p": {"name": "p", "value_type": "Text"}}}},
    }
    schema = SchemaIR.model_validate(raw)
    assert schema.concepts["C"].properties["p"].personal is False
    assert schema.concepts["C"].personal_key is False


def test_stub_round_trip_keeps_the_marks() -> None:
    schema = _schema()
    namespace: dict[str, object] = {}
    # dont_inherit: this module's `from __future__ import annotations` would
    # otherwise turn the generated annotations into strings.
    source = generate_class_stubs(schema)
    exec(compile(source, "<generated>", "exec", dont_inherit=True), namespace)  # noqa: S102
    again = compile_schema("people", 1, namespace["Person"], namespace["Org"])  # type: ignore[arg-type]
    assert again == schema


def test_yaml_round_trip_keeps_the_marks() -> None:
    schema = _schema()
    assert from_yaml(to_yaml(schema)) == schema


def test_unmarked_schema_emits_no_annotation() -> None:
    """Nothing about an unmarked schema's serialized form changes."""
    schema = compile_schema("people", 1, Org)
    assert "personal" not in to_yaml(schema)
    assert "personal" not in generate_class_stubs(schema)


_YAML = """
id: https://example.org/s
name: s
version: 1
default_range: string
classes:
  C:
    attributes:
      {slot}:
        range: {range}
        annotations:
          ontolith_personal: {value}
"""


@pytest.mark.parametrize("value", ["ture", "1", "'maybe'"])
def test_an_unrecognised_personal_annotation_is_an_error_not_false(value: str) -> None:
    with pytest.raises(SchemaError, match="ontolith_personal"):
        from_yaml(_YAML.format(slot="p", range="string", value=value))


@pytest.mark.parametrize("value", ["true", "false", "'true'", "'false'"])
def test_true_and_false_are_accepted_as_string_or_boolean(value: str) -> None:
    schema = from_yaml(_YAML.format(slot="p", range="string", value=value))
    expected = value.strip("'") == "true"
    assert schema.concepts["C"].properties["p"].personal is expected


def test_a_personal_annotation_on_a_relation_is_an_error() -> None:
    doc = _YAML.format(slot="r", range="C", value="true")
    with pytest.raises(SchemaError, match="relation"):
        from_yaml(doc)
