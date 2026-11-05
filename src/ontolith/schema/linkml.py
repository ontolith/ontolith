"""LinkML-aligned YAML front-end for schema definitions (SPEC §6.1, ADR-0013).

`to_yaml`/`from_yaml` translate between `SchemaIR` and a deliberately-scoped
subset of the LinkML schema dialect — hand-rolled against PyYAML directly,
not the real `linkml`/`linkml-runtime` packages (see ADR-0013 for why).

Not re-exported from `schema/__init__.py`: `pyyaml` is gated behind the
optional `interop` extra, so eagerly importing this module from the package
root would make plain `import ontolith`/`import ontolith.schema` fail with
`ModuleNotFoundError` for anyone who hasn't installed `ontolith[interop]` —
the same reasoning `schema/rdf.py`'s own docstring already states for
`rdflib`. Import directly: `from ontolith.schema.linkml import from_yaml,
to_yaml`.

v1 dialect coverage (ADR-0013):
    - Only inline `attributes:` per class — no shared top-level `slots:` dict.
    - Scalar ranges map to Ontolith value types via a fixed table.
    - A range naming another class in the same schema becomes a relation.
    - `multivalued:` -> cardinality, native `inverse:` -> RelationDef.inverse.
    - Schema-level `default_range:` is honored as the fallback range for any
      slot that omits `range:` (must itself be a builtin scalar or a class
      defined in the schema).
    - Temporality has no LinkML equivalent: encoded as
      `annotations.ontolith_temporality: time_varying` on the slot.
    - Unsupported constructs (is_a/mixins, enums, slot_usage, abstract,
      patterns, any_of/exactly_one_of, identifier/key/alias/ifabsent/
      readonly/recommended, imports, multi-file schemas) raise SchemaError
      on import rather than silently dropping data. This is an enumerated
      blacklist, not an exhaustive one — a real LinkML key outside both the
      supported and blacklisted sets is still silently ignored; the
      blacklist only covers the constructs identified so far as plausible
      to encounter. `to_yaml` only ever emits constructs it can faithfully
      round-trip.
"""

from __future__ import annotations

from typing import Any, Literal, cast

import yaml

from ontolith.core.errors import SchemaError
from ontolith.schema.ir import ConceptDef, PropertyDef, RelationDef, SchemaIR

_Cardinality = Literal["single", "many"]
_Temporality = Literal["static", "time_varying"]
_ValueType = Literal["Text", "Integer", "Float", "Boolean", "Date", "DateTime", "URI", "JSON"]

_VALUE_TYPE_TO_LINKML_RANGE: dict[str, str] = {
    "Text": "string",
    "Integer": "integer",
    # "double" (64-bit), not "float" (32-bit) - Ontolith's Float is backed by
    # Python's float/IEEE-754 double throughout (identical to what schema/rdf.py's
    # RDF/OWL bridge already emits, XSD.double). Ontolith's own IR -> YAML -> IR
    # round-trip was never lossy either way (the reverse table below has always
    # accepted both spellings) - the defect this fixes is a mis-declared
    # precision that only matters to a downstream LinkML-consuming tool (codegen,
    # SHACL/JSON-Schema generation) resolving `float` to 32-bit xsd:float, not
    # anything Ontolith itself round-trips through. (KI-068; ADR-0013 Update -
    # ADR-0036's own Consequences section disclosed the mismatch and declined to
    # reconcile it as out of that ADR's scope, not "named it as future work".)
    # The reverse mapping already accepted "double" before this change, so this
    # makes the two tables consistent with each other, not just internally.
    "Float": "double",
    "Boolean": "boolean",
    "Date": "date",
    "DateTime": "datetime",
    "URI": "uriorcurie",
    "JSON": "string",
}

_LINKML_RANGE_TO_VALUE_TYPE: dict[str, str] = {
    "string": "Text",
    "integer": "Integer",
    # "float" stays accepted on import (not just "double") for backward
    # compatibility with hand-authored LinkML and any schema exported by an
    # older Ontolith version before the emission side switched to "double"
    # above (KI-068) - both losslessly represent Ontolith's own Float either
    # way, since Python float parsing doesn't distinguish the two on read.
    "float": "Float",
    "double": "Float",
    "boolean": "Boolean",
    "date": "Date",
    "datetime": "DateTime",
    "uriorcurie": "URI",
    "uri": "URI",
}

_UNSUPPORTED_SCHEMA_KEYS = ("slots", "imports", "types", "subsets", "enums")
_UNSUPPORTED_CLASS_KEYS = ("is_a", "mixins", "slot_usage", "tree_root", "abstract")
_UNSUPPORTED_SLOT_KEYS = (
    "pattern",
    "any_of",
    "all_of",
    "exactly_one_of",
    "none_of",
    "permissible_values",
    "identifier",
    "key",
    "alias",
    "ifabsent",
    "readonly",
    "recommended",
)

_SUPPORTED_METADATA_KEYS = ("prefixes", "default_prefix", "description")

_TEMPORALITY_ANNOTATION_KEY = "ontolith_temporality"
_PERSONAL_ANNOTATION_KEY = "ontolith_personal"
_PERSONAL_KEY_ANNOTATION_KEY = "ontolith_personal_key"
_VALUE_TYPE_ANNOTATION_KEY = "ontolith_value_type"

# value_types whose LinkML `range` mapping is ambiguous with another value_type
# (JSON and Text both map to range: string) and therefore need the
# ontolith_value_type annotation to disambiguate on import.
_AMBIGUOUS_RANGE_VALUE_TYPES = frozenset({"JSON"})


def to_yaml(schema: SchemaIR) -> str:
    """Serialize a SchemaIR to LinkML-aligned YAML (SPEC §6.1: IR -> YAML).

    Only ever emits constructs `from_yaml` can faithfully round-trip.

    Args:
        schema: SchemaIR to serialize

    Returns:
        YAML document text
    """
    document: dict[str, Any] = {
        "id": schema.namespace,
        "name": schema.namespace,
        "version": schema.version,
    }
    if schema.metadata:
        unsupported_metadata_keys = set(schema.metadata) - set(_SUPPORTED_METADATA_KEYS)
        if unsupported_metadata_keys:
            raise SchemaError(
                f"Unsupported schema metadata key(s) for the v1 LinkML dialect: "
                f"{sorted(unsupported_metadata_keys)!r} (only {_SUPPORTED_METADATA_KEYS} "
                "round-trip through YAML)"
            )
        for key in _SUPPORTED_METADATA_KEYS:
            if key in schema.metadata:
                document[key] = schema.metadata[key]

    classes: dict[str, Any] = {}
    for concept_name in schema.concepts:
        concept = schema.concepts[concept_name]
        class_doc: dict[str, Any] = {}
        if concept.description:
            class_doc["description"] = concept.description
        if concept.personal_key:
            class_doc["annotations"] = {_PERSONAL_KEY_ANNOTATION_KEY: "true"}

        attributes: dict[str, Any] = {}
        for prop_name in concept.properties:
            attributes[prop_name] = _property_to_slot(concept.properties[prop_name])
        for rel_name in concept.relations:
            attributes[rel_name] = _relation_to_slot(concept.relations[rel_name])
        class_doc["attributes"] = attributes
        classes[concept_name] = class_doc

    document["classes"] = classes
    return yaml.safe_dump(document, sort_keys=False)


def _property_to_slot(prop: PropertyDef) -> dict[str, Any]:
    """Render a PropertyDef as a LinkML slot mapping."""
    slot: dict[str, Any] = {"range": _VALUE_TYPE_TO_LINKML_RANGE[prop.value_type]}
    if prop.required:
        slot["required"] = True
    if prop.cardinality == "many":
        slot["multivalued"] = True
    if prop.description:
        slot["description"] = prop.description

    annotations: dict[str, str] = {}
    if prop.temporality != "static":
        annotations[_TEMPORALITY_ANNOTATION_KEY] = prop.temporality
    if prop.personal:
        annotations[_PERSONAL_ANNOTATION_KEY] = "true"
    if prop.value_type in _AMBIGUOUS_RANGE_VALUE_TYPES:
        # range: string is shared by Text and JSON — disambiguate explicitly.
        annotations[_VALUE_TYPE_ANNOTATION_KEY] = prop.value_type
    if annotations:
        slot["annotations"] = annotations
    return slot


def _relation_to_slot(rel: RelationDef) -> dict[str, Any]:
    """Render a RelationDef as a LinkML slot mapping."""
    slot: dict[str, Any] = {"range": rel.target_concept}
    if rel.required:
        slot["required"] = True
    if rel.cardinality == "many":
        slot["multivalued"] = True
    if rel.inverse:
        slot["inverse"] = rel.inverse
    if rel.description:
        slot["description"] = rel.description
    if rel.temporality != "static":
        slot["annotations"] = {_TEMPORALITY_ANNOTATION_KEY: rel.temporality}
    return slot


def from_yaml(text: str) -> SchemaIR:
    """Parse LinkML-aligned YAML into a SchemaIR (SPEC §6.1: YAML -> IR).

    Raises SchemaError on any construct outside the v1 dialect (ADR-0013)
    rather than silently dropping data.

    Args:
        text: YAML document text

    Returns:
        Parsed SchemaIR

    Raises:
        SchemaError: If the document uses an unsupported LinkML construct,
            is missing required fields, or a relation references an unknown
            concept
    """
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        raise SchemaError("LinkML YAML document must be a mapping at the top level")

    for key in _UNSUPPORTED_SCHEMA_KEYS:
        if key in document:
            raise SchemaError(
                f"Unsupported LinkML construct at schema level: {key!r} "
                "(v1 dialect: inline attributes only, no shared slots/imports/types)"
            )

    namespace = document.get("id") or document.get("name")
    if not namespace:
        raise SchemaError("LinkML YAML document must set 'id' or 'name'")

    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        # bool is an int subclass in Python — isinstance(True, int) is True —
        # so it must be excluded explicitly or `version: true` would parse as 1.
        raise SchemaError(f"'version' must be an integer for the v1 dialect, got {version!r}")

    classes = document.get("classes", {})
    if classes is None:
        classes = {}
    if not isinstance(classes, dict):
        raise SchemaError("'classes' must be a mapping of class name -> class definition")

    default_range = document.get("default_range")
    if default_range is not None:
        if default_range not in _LINKML_RANGE_TO_VALUE_TYPE and default_range not in classes:
            raise SchemaError(
                f"'default_range' {default_range!r} is not a builtin scalar type "
                "or a class defined in this schema"
            )
    else:
        default_range = "string"

    concepts: dict[str, ConceptDef] = {}
    for class_name, class_doc in classes.items():
        concepts[class_name] = _parse_class(
            class_name, class_doc, list(classes.keys()), default_range
        )

    metadata: dict[str, Any] = {}
    for key in _SUPPORTED_METADATA_KEYS:
        if key in document:
            metadata[key] = document[key]

    return SchemaIR(namespace=namespace, version=version, concepts=concepts, metadata=metadata)


def _parse_class(
    class_name: str, class_doc: Any, known_class_names: list[str], default_range: str
) -> ConceptDef:
    """Parse one LinkML class mapping into a ConceptDef, raising SchemaError on v1-dialect violations."""
    if not isinstance(class_doc, dict):
        raise SchemaError(f"Class {class_name!r} must be a mapping")

    for key in _UNSUPPORTED_CLASS_KEYS:
        if key in class_doc:
            raise SchemaError(
                f"Unsupported LinkML construct on class {class_name!r}: {key!r} "
                "(v1 dialect: no inheritance/mixins/slot_usage/abstract classes)"
            )

    description = class_doc.get("description")
    attributes = class_doc.get("attributes", {})
    if attributes is None:
        attributes = {}
    if not isinstance(attributes, dict):
        raise SchemaError(f"Class {class_name!r}.attributes must be a mapping")

    properties: dict[str, PropertyDef] = {}
    relations: dict[str, RelationDef] = {}
    for slot_name, slot_doc in attributes.items():
        if not isinstance(slot_doc, dict):
            raise SchemaError(f"Attribute {class_name}.{slot_name} must be a mapping")

        for key in _UNSUPPORTED_SLOT_KEYS:
            if key in slot_doc:
                raise SchemaError(
                    f"Unsupported LinkML construct on {class_name}.{slot_name}: {key!r} "
                    "(v1 dialect: no patterns/enums/boolean-combinators/identifier-key-"
                    "alias-ifabsent-readonly-recommended)"
                )

        range_value = slot_doc.get("range", default_range)
        required = bool(slot_doc.get("required", False))
        cardinality: _Cardinality = "many" if slot_doc.get("multivalued") else "single"
        slot_description = slot_doc.get("description")
        annotations = slot_doc.get("annotations")
        raw_temporality = (
            annotations.get(_TEMPORALITY_ANNOTATION_KEY, "static")
            if isinstance(annotations, dict)
            else "static"
        )
        if raw_temporality not in ("static", "time_varying"):
            raise SchemaError(
                f"{class_name}.{slot_name}: invalid {_TEMPORALITY_ANNOTATION_KEY} "
                f"annotation value {raw_temporality!r}"
            )
        temporality = cast(_Temporality, raw_temporality)

        if range_value in known_class_names:
            if isinstance(annotations, dict) and _PERSONAL_ANNOTATION_KEY in annotations:
                raise SchemaError(
                    f"{class_name}.{slot_name}: {_PERSONAL_ANNOTATION_KEY} is not supported on "
                    "a relation (only properties can be marked personal)"
                )
            relations[slot_name] = RelationDef(
                name=slot_name,
                target_concept=range_value,
                cardinality=cardinality,
                required=required,
                temporality=temporality,
                inverse=slot_doc.get("inverse"),
                description=slot_description,
            )
        elif range_value in _LINKML_RANGE_TO_VALUE_TYPE:
            annotated_value_type = (
                annotations.get(_VALUE_TYPE_ANNOTATION_KEY)
                if isinstance(annotations, dict)
                else None
            )
            if (
                annotated_value_type is not None
                and annotated_value_type not in _VALUE_TYPE_TO_LINKML_RANGE
            ):
                raise SchemaError(
                    f"{class_name}.{slot_name}: invalid {_VALUE_TYPE_ANNOTATION_KEY} "
                    f"annotation value {annotated_value_type!r}"
                )
            value_type = cast(
                _ValueType, annotated_value_type or _LINKML_RANGE_TO_VALUE_TYPE[range_value]
            )
            properties[slot_name] = PropertyDef(
                name=slot_name,
                value_type=value_type,
                cardinality=cardinality,
                required=required,
                temporality=temporality,
                personal=_annotation_flag(
                    annotations, _PERSONAL_ANNOTATION_KEY, f"{class_name}.{slot_name}"
                ),
                description=slot_description,
            )
        else:
            raise SchemaError(
                f"{class_name}.{slot_name}: unknown range {range_value!r} "
                "(not a builtin scalar type or a class defined in this schema)"
            )

    return ConceptDef(
        name=class_name,
        properties=properties,
        relations=relations,
        personal_key=_annotation_flag(
            class_doc.get("annotations"), _PERSONAL_KEY_ANNOTATION_KEY, f"Class {class_name!r}"
        ),
        description=description,
    )


def _annotation_flag(annotations: Any, key: str, where: str) -> bool:
    """Read a boolean LinkML annotation, written as the string "true"/"false"
    (what this module emits) or a YAML boolean. Anything else raises: for a
    privacy mark, a typo that silently reads as "not personal" is the
    dangerous direction."""
    if not isinstance(annotations, dict) or key not in annotations:
        return False
    value = annotations[key]
    text = str(value).lower() if isinstance(value, (str, bool)) else None
    if text not in ("true", "false"):
        raise SchemaError(f"{where}: invalid {key} annotation value {value!r} (use true or false)")
    return text == "true"
