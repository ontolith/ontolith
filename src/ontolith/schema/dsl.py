"""Class-based DSL for schema definitions (SPEC §6.2).

Compiles Pydantic-style class declarations into the canonical `SchemaIR`,
and decompiles a `SchemaIR` back into class-stub source text — the two
halves of SPEC §6.1's bidirectional `classes ↔ IR` codegen requirement.

Example:
    >>> class Organization(Concept):
    ...     name: Text
    >>> class Person(Concept):
    ...     name: Text
    ...     born: Date | None = None
    ...     employer: Ref["Organization"] = Relation(
    ...         inverse="employees", temporality="time_varying"
    ...     )
    >>> schema = compile_schema("default", 1, Person, Organization)
"""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass
from typing import Annotated, Any, Literal, get_args, get_origin, get_type_hints

from ontolith.core.errors import SchemaError
from ontolith.schema.ir import ConceptDef, PropertyDef, RelationDef, SchemaIR

ValueType = Literal["Text", "Integer", "Float", "Boolean", "Date", "DateTime", "URI", "JSON"]
Cardinality = Literal["single", "many"]
Temporality = Literal["static", "time_varying"]


@dataclass(frozen=True)
class _ScalarMarker:
    """Metadata attached via Annotated[...] identifying a scalar value_type."""

    value_type: ValueType


@dataclass(frozen=True)
class _RefMarker:
    """Metadata attached via Annotated[...] capturing a relation's target concept name."""

    target_concept: str


@dataclass(frozen=True)
class _RelationSpec:
    """Sentinel returned by Relation(...), captured as a class-body default value.

    `required=None` means "not stated explicitly — infer from whether the
    annotation itself is `X | None`", matching plain-annotation behavior.
    """

    inverse: str | None
    cardinality: Cardinality
    required: bool | None
    temporality: Temporality
    description: str | None


@dataclass(frozen=True)
class _PropertySpec:
    """Sentinel returned by Property(...), captured as a class-body default value.

    `required=None` means "not stated explicitly — infer from whether the
    annotation itself is `X | None`", matching plain-annotation behavior.
    """

    cardinality: Cardinality
    required: bool | None
    temporality: Temporality
    description: str | None
    personal: bool = False


def Relation(
    *,
    inverse: str | None = None,
    cardinality: Cardinality = "single",
    required: bool | None = None,
    temporality: Temporality = "static",
    description: str | None = None,
) -> Any:
    """Field spec for a relation, used as a class-body default (SPEC §6.2).

    `required` defaults to `None`, meaning "infer from the annotation" — a
    bare `Ref["X"]` is required, `Ref["X"] | None` is optional — exactly like
    a relation with no `Relation(...)` spec at all. Pass `required=True`/`False`
    explicitly only to override what the annotation itself already states.

    Example:
        employer: Ref["Organization"] = Relation(inverse="employees")
    """
    return _RelationSpec(
        inverse=inverse,
        cardinality=cardinality,
        required=required,
        temporality=temporality,
        description=description,
    )


def Property(
    *,
    cardinality: Cardinality = "single",
    required: bool | None = None,
    temporality: Temporality = "static",
    description: str | None = None,
    personal: bool = False,
) -> Any:
    """Field spec for a property, used as a class-body default.

    Plain annotations (`name: Text`) are sufficient for static, single-valued,
    optional properties. Use `Property(...)` to declare `temporality="time_varying"`,
    non-default cardinality, or a description on a property.

    `required` defaults to `None`, meaning "infer from the annotation" — a bare
    `Integer` is required, `Integer | None` is optional — exactly as if no
    `Property(...)` spec were attached at all. Pass `required=True`/`False`
    explicitly only to override what the annotation itself already states.

    `personal=True` marks a value that can identify a person, so a knowledge
    base in redaction mode can erase it (ADR-0056). It changes nothing else.

    Example:
        salary: Integer = Property(temporality="time_varying")
        email: Text = Property(personal=True)
    """
    return _PropertySpec(
        cardinality=cardinality,
        required=required,
        temporality=temporality,
        description=description,
        personal=personal,
    )


class _RefAlias:
    """Implements Ref["TargetConcept"] via __class_getitem__.

    Captures the target concept name as a plain string rather than resolving it
    as a real forward reference — this is what lets mutually-referencing concepts
    (Person.employer -> Organization, Organization.employees -> Person) compile
    without needing either class to exist yet when the other's body is evaluated.
    """

    def __class_getitem__(cls, target_concept: str) -> Any:
        """Return an Annotated[str, _RefMarker(target_concept)] type for `Ref["X"]`."""
        if not isinstance(target_concept, str):
            raise SchemaError(
                f"Ref[...] target must be a string concept name, got {target_concept!r}"
            )
        return Annotated[str, _RefMarker(target_concept)]


Ref = _RefAlias
"""Relation target type: `Ref["ConceptName"]`, e.g. `employer: Ref["Organization"] = Relation()`."""

Text = Annotated[str, _ScalarMarker("Text")]
"""A scalar string-valued property/attribute type in the class DSL."""

Integer = Annotated[int, _ScalarMarker("Integer")]
"""A scalar integer-valued property/attribute type in the class DSL."""

Float = Annotated[float, _ScalarMarker("Float")]
"""A scalar float-valued property/attribute type in the class DSL."""

Boolean = Annotated[bool, _ScalarMarker("Boolean")]
"""A scalar boolean-valued property/attribute type in the class DSL."""

Date = Annotated[str, _ScalarMarker("Date")]
"""A scalar ISO-8601 date-valued property/attribute type in the class DSL."""

DateTime = Annotated[str, _ScalarMarker("DateTime")]
"""A scalar ISO-8601 datetime-valued property/attribute type in the class DSL."""

URI = Annotated[str, _ScalarMarker("URI")]
"""A scalar URI-or-CURIE-valued property/attribute type in the class DSL
(LinkML's `uriorcurie`, ADR-0013 — a full `scheme:...` URI or a
`prefix:local-name` CURIE, either is accepted)."""

JSON = Annotated[dict[str, Any], _ScalarMarker("JSON")]
"""A scalar JSON-valued property/attribute type in the class DSL — any
value `json.loads` accepts (object, array, string, number, bool, or
null), not only a JSON object despite the DSL annotation's `dict[str,
Any]` typing (a caller-facing convenience, not what storage actually
validates against)."""


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Strip `X | None` (or `typing.Optional[X]`), returning (X, was_optional)."""
    origin = get_origin(annotation)
    if origin is types.UnionType or origin is typing.Union:
        all_args = get_args(annotation)
        non_none = [a for a in all_args if a is not type(None)]
        if len(non_none) == 1 and len(all_args) == 2:
            return non_none[0], True
    return annotation, False


def _find_marker(annotation: Any) -> _ScalarMarker | _RefMarker | None:
    """Extract the Annotated[...] marker (scalar type or ref target), if present."""
    if get_origin(annotation) is Annotated:
        for meta in annotation.__metadata__:
            if isinstance(meta, (_ScalarMarker, _RefMarker)):
                return meta
    return None


class ConceptMeta(type):
    """Metaclass compiling class-body annotations into a ConceptDef (SPEC §6.2).

    No concept inheritance in v1: only the concept's own class body is walked,
    matching the current flat (non-inheriting) ConceptDef shape in the IR.

    An implementation detail of how `Concept` subclasses get compiled at
    class-definition time, not something a caller ever names directly — the
    natural annotation need (a class object that's a `Concept` subclass) is
    already covered by `type[Concept]`. Not in this module's own `__all__`
    (found in review during the M4 API-surface-freeze audit, ADR-0019's
    Update: it had been listed there with no external consumer anywhere in
    the codebase, the only such case that audit's sweep found across two
    rounds — removed rather than left ambiguous between "public" and
    "internal").
    """

    def __new__(mcs, name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> ConceptMeta:
        """Compile the class body's annotations into a ConceptDef and attach it to the class."""
        cls = super().__new__(mcs, name, bases, namespace)
        if bases == ():
            # This is the `Concept` base class itself — nothing to compile.
            return cls

        hints = get_type_hints(cls, include_extras=True)
        properties: dict[str, PropertyDef] = {}
        relations: dict[str, RelationDef] = {}

        for field_name, annotation in hints.items():
            if field_name.startswith("_"):
                continue
            unwrapped, is_optional = _unwrap_optional(annotation)
            marker = _find_marker(unwrapped)
            default = namespace.get(field_name, None)

            if isinstance(marker, _RefMarker):
                if isinstance(default, _RelationSpec):
                    required = default.required if default.required is not None else not is_optional
                    relations[field_name] = RelationDef(
                        name=field_name,
                        target_concept=marker.target_concept,
                        cardinality=default.cardinality,
                        required=required,
                        temporality=default.temporality,
                        inverse=default.inverse,
                        description=default.description,
                    )
                else:
                    relations[field_name] = RelationDef(
                        name=field_name,
                        target_concept=marker.target_concept,
                        required=not is_optional,
                    )
            elif isinstance(marker, _ScalarMarker):
                if isinstance(default, _PropertySpec):
                    required = default.required if default.required is not None else not is_optional
                    properties[field_name] = PropertyDef(
                        name=field_name,
                        value_type=marker.value_type,
                        cardinality=default.cardinality,
                        required=required,
                        temporality=default.temporality,
                        personal=default.personal,
                        description=default.description,
                    )
                else:
                    properties[field_name] = PropertyDef(
                        name=field_name,
                        value_type=marker.value_type,
                        required=not is_optional,
                    )
            else:
                raise SchemaError(
                    f"{name}.{field_name}: annotation must be one of the ontolith "
                    f"scalar types (Text, Integer, ...) or Ref[...], got {annotation!r}"
                )

        cls.__ontolith_concept_def__ = ConceptDef(  # type: ignore[attr-defined]
            name=name,
            properties=properties,
            relations=relations,
            personal_key=bool(namespace.get("__personal_key__", False)),
            description=(namespace.get("__doc__") or None),
        )
        return cls


class Concept(metaclass=ConceptMeta):
    """Base class for schema concepts (SPEC §6.2).

    Example:
        class Person(Concept):
            name: Text
            born: Date | None = None
    """


def compile_schema(
    namespace: str,
    version: int,
    *concepts: type[Concept],
    metadata: dict[str, Any] | None = None,
) -> SchemaIR:
    """Compile Concept subclasses into a SchemaIR (SPEC §6.1: classes -> IR).

    Explicit — takes the concepts to include rather than tracking every
    `Concept` subclass ever defined in a hidden global registry.

    Args:
        namespace: Namespace this schema belongs to
        version: Schema version (monotonically increasing)
        concepts: Concept subclasses to compile
        metadata: Optional schema-level metadata

    Returns:
        SchemaIR containing the compiled concepts

    Raises:
        SchemaError: If a relation references a concept not among `concepts`
    """
    concept_defs: dict[str, ConceptDef] = {}
    for concept_cls in concepts:
        concept_def = getattr(concept_cls, "__ontolith_concept_def__", None)
        if concept_def is None:
            raise SchemaError(f"{concept_cls!r} is not a compiled Concept subclass")
        concept_defs[concept_def.name] = concept_def

    return SchemaIR(
        namespace=namespace,
        version=version,
        concepts=concept_defs,
        metadata=metadata or {},
    )


_VALUE_TYPE_TO_MARKER_NAME = {
    "Text": "Text",
    "Integer": "Integer",
    "Float": "Float",
    "Boolean": "Boolean",
    "Date": "Date",
    "DateTime": "DateTime",
    "URI": "URI",
    "JSON": "JSON",
}


def _property_annotation(prop: PropertyDef) -> str:
    """Render a property field. `required` is encoded via the annotation's
    optionality (bare vs. `X | None`), never via an explicit `required=` arg,
    so `not prop.required` round-trips correctly through `_unwrap_optional`'s
    infer-from-annotation fallback regardless of whether other overrides
    (cardinality/temporality/description) are also present.
    """
    type_name = _VALUE_TYPE_TO_MARKER_NAME[prop.value_type]
    annotation = type_name if prop.required else f"{type_name} | None"

    non_default_args = []
    if prop.cardinality != "single":
        non_default_args.append(f"cardinality={prop.cardinality!r}")
    if prop.temporality != "static":
        non_default_args.append(f"temporality={prop.temporality!r}")
    if prop.personal:
        non_default_args.append("personal=True")
    if prop.description:
        non_default_args.append(f"description={prop.description!r}")

    if non_default_args:
        return f"{annotation} = Property({', '.join(non_default_args)})"
    return annotation if prop.required else f"{annotation} = None"


def _relation_field(rel: RelationDef) -> str:
    """Render a relation field. See `_property_annotation` for why `required`
    is encoded via the annotation's optionality rather than a `required=` arg.
    """
    ref_expr = f'Ref["{rel.target_concept}"]'
    annotation = ref_expr if rel.required else f"{ref_expr} | None"

    args = [f"inverse={rel.inverse!r}"] if rel.inverse else []
    if rel.cardinality != "single":
        args.append(f"cardinality={rel.cardinality!r}")
    if rel.temporality != "static":
        args.append(f"temporality={rel.temporality!r}")
    if rel.description:
        args.append(f"description={rel.description!r}")
    spec = f"Relation({', '.join(args)})" if args else "Relation()"
    return f"{annotation} = {spec}"


def generate_class_stubs(schema: SchemaIR) -> str:
    """Decompile a SchemaIR into Concept subclass source text (SPEC §6.1: IR -> classes).

    Output is deterministic (concepts sorted by name, fields in declaration order
    as stored in the IR dicts) so repeated calls on the same IR are byte-identical.

    Args:
        schema: SchemaIR to decompile

    Returns:
        Formatted Python source defining one Concept subclass per concept
    """
    lines = [
        '"""Generated by ontolith.schema.dsl.generate_class_stubs. Do not edit by hand."""',
        "",
        "from ontolith import Concept, Property, Ref, Relation",
        "from ontolith import Boolean, Date, DateTime, Float, Integer, JSON, Text, URI",
        "",
    ]
    for concept_name in sorted(schema.concepts):
        concept = schema.concepts[concept_name]
        lines.append("")
        lines.append(f"class {concept_name}(Concept):")
        body_lines: list[str] = []
        if concept.description:
            # repr(), not a raw triple-quoted string: a description containing
            # quotes or backslashes would otherwise produce invalid Python.
            body_lines.append(f"    {concept.description!r}")
        if concept.personal_key:
            body_lines.append("    __personal_key__ = True")
        for prop_name in concept.properties:
            prop = concept.properties[prop_name]
            body_lines.append(f"    {prop_name}: {_property_annotation(prop)}")
        for rel_name in concept.relations:
            rel = concept.relations[rel_name]
            body_lines.append(f"    {rel_name}: {_relation_field(rel)}")
        if not body_lines:
            body_lines.append("    pass")
        lines.extend(body_lines)
    lines.append("")
    return "\n".join(lines)


__all__ = [
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
    "compile_schema",
    "generate_class_stubs",
]
