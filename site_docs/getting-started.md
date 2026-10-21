# Getting Started

This walks through building a small knowledge base from scratch: connecting,
defining a schema, creating principals and entities, making assertions, and
querying them back with provenance. It mirrors
[`examples/quickstart.py`](https://github.com/ontolith/ontolith/blob/main/examples/quickstart.py)
in the repository — run that file directly if you'd rather see it all at
once.

## Install

Ontolith isn't on PyPI yet. Install from source with
[uv](https://github.com/astral-sh/uv):

```bash
git clone https://github.com/ontolith/ontolith.git
cd ontolith
uv sync
```

## Connect to a knowledge base

```python
from ontolith import Ontology

kb = Ontology.connect("my_kb.db")  # SQLite by default
```

`Ontology.connect()` creates the file if it doesn't exist. Everything below
happens inside one namespace (`"default"` unless you say otherwise).

## Create principals

Every assertion in Ontolith is made *by* someone — a human or an AI, each a
[`Principal`](reference/identity.md). AI principals must declare an
accountable owner (SPEC §8.1) and have their capability capped at `propose`
by default: they can suggest facts, never write them directly.

`auth_method` below is descriptive, not enforcing — only `"apikey"` is
backed by a real shipped `AuthProvider` (`TokenAuthProvider`) today.
`"oidc"` and `"workload"` are specified (SPEC §8.2) but not built yet.

```python
alice = kb.create_principal(
    "alice@example.com",
    kind="human",
    auth_method="oidc",
    default_capability="admin",
    trust_level=8,
)

research_bot = kb.create_principal(
    "research-bot",
    kind="ai",
    owner=alice.id,
    auth_method="workload",
    default_capability="propose",
    trust_level=5,
)
```

## Define a schema

Ontolith ships a class-based DSL as its primary schema front-end (a
LinkML-aligned YAML front-end also compiles to the same internal
representation):

```python
from ontolith.schema import Concept, Date, Ref, Relation, Text, compile_schema


class Person(Concept):
    name: Text
    born: Date | None = None
    collaboratedWith: Ref["Person"] = Relation()


schema = compile_schema("default", 1, Person)
kb.apply_schema(schema, author=alice.id)
```

## Create entities and make assertions

```python
ada = kb.create_entity(concept="Person", author=alice.id, natural_key="ada-lovelace")

kb.assert_literal(
    subject=ada.id,
    predicate="Person.name",
    value="Ada Lovelace",
    value_type="Text",
    author=alice.id,
    source="Wikipedia",
    confidence=1.0,
)
```

`assert_literal`/`assert_ref` are the direct-write path — available to
`write` or `admin` capability principals (not `review` — that capability is
for accepting/rejecting others' proposals, not for direct writes of your
own). An AI principal (`propose`
capability) uses `propose`/`propose_ref` instead, which routes through the
governed review workflow covered in the
[Governance & Review tutorial](tutorials/governance-and-review.md):

```python
proposal, decision = kb.propose_ref(
    subject=ada.id,
    predicate="Person.collaboratedWith",
    target=some_other_entity.id,
    author=research_bot.id,
    source="ACM Digital Library",
    confidence=0.95,
    model="example-model-2026-01",  # required provenance for AI authors
)
```

## Query and check provenance

```python
facts = kb.assertions(subject=ada.id)
for fact in facts:
    print(fact.predicate, fact.value, fact.author)

name_assertion = next(a for a in facts if a.predicate == "Person.name")
print(name_assertion.source, name_assertion.confidence, name_assertion.asserted_at)
```

Every assertion carries its own provenance — no separate lookup needed.

## Next steps

- [Concepts](concepts.md) — the ideas behind what you just did
- [Governance & Review](tutorials/governance-and-review.md) — the full
  propose → review → accept/reject lifecycle
- [Bitemporal Queries](tutorials/bitemporal-queries.md) — time-travel with
  `as_of()`
- [Examples](examples.md) — every runnable script in the repository
