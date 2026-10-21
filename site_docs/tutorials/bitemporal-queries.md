# Bitemporal Queries

Full source:
[`examples/bitemporal_queries.py`](https://github.com/ontolith/ontolith/blob/main/examples/bitemporal_queries.py)

This tutorial covers `time_varying` supersession and `as_of(t)`
time-travel — the two features that make Ontolith bitemporal rather than
just "versioned."

## Declaring a time-varying property

A job title changes over time as a matter of course — that's not a
disagreement between sources, it's just the world changing. Declaring the
property `time_varying` tells Ontolith to route a differing value to
**supersession** instead of **contradiction**:

```python
from ontolith.schema import Concept, Property, Text, compile_schema


class Person(Concept):
    name: Text
    title: Text = Property(temporality="time_varying")
```

## Using an injected clock

Ontolith never calls `datetime.now()` directly in domain code — `asserted_at`
is always stamped from an injected `Clock`. This tutorial uses `FixedClock`
so the assertion-time dimension is fully under your control (and the whole
example is deterministic — no wall-clock dependence):

```python
from ontolith.core import FixedClock

clock = FixedClock("2023-01-01T00:00:00Z")
kb = Ontology.connect(db_path, clock=clock)
```

## Supersession in action

```python
# Recorded on 2023-01-01 (the clock's current time = asserted_at):
# "Engineer" since 2020, no end date yet -- an open-ended window.
kb.assert_literal(
    subject=dana.id,
    predicate="Person.title",
    value="Engineer",
    value_type="Text",
    author=hr.id,
    valid_from=datetime(2020, 1, 1, tzinfo=UTC),
)

clock.advance(days=150)  # now 2023-05-31

# A promotion, effective 2023-06-01. This window OVERLAPS the still-open
# "Engineer" window and has a different value -- supersession closes the
# prior window automatically (valid_to = this assertion's valid_from).
kb.assert_literal(
    subject=dana.id,
    predicate="Person.title",
    value="Senior Engineer",
    value_type="Text",
    author=hr.id,
    valid_from=datetime(2023, 6, 1, tzinfo=UTC),
)
```

Only the current value is active; the superseded one is kept forever
(append-only), just excluded from the default `status="active"` view:

```python
kb.assertions(subject=dana.id, predicate="Person.title")
# -> [<"Senior Engineer", active>]

kb.assertions(subject=dana.id, predicate="Person.title", status=None)
# -> [<"Senior Engineer", active, valid 2023-06-01 to open>,
#     <"Engineer", superseded, valid 2020-01-01 to 2023-06-01>]
```

!!! note "Windows must actually overlap"
    Supersession only fires when the new assertion's validity window
    **overlaps** an existing active one with a differing value. Two
    adjacent-but-non-overlapping windows (say, one ending exactly where the
    next begins, both with an explicit `valid_to`) don't overlap by this
    definition — they simply **coexist**, which is intentional: SPEC §10.2
    explicitly allows multiple `time_varying` values to coexist when their
    validity windows don't overlap (an employment history is the canonical
    example).

## Time travel with `as_of()`

`as_of(t)` reconstructs what was known **and** true at `t` — both
`valid_from <= t < valid_to` and `asserted_at <= t` must hold:

```python
kb.as_of(datetime(2023, 3, 1, tzinfo=UTC)).assertions(subject=dana.id, predicate="Person.title")
# -> [<"Engineer">]   -- after the first assertion, before the promotion

kb.as_of(datetime(2023, 7, 1, tzinfo=UTC)).assertions(subject=dana.id, predicate="Person.title")
# -> [<"Senior Engineer">]   -- after the promotion
```

## The subtle part: knowledge time vs. real-world time

Dana's title was *also* "Engineer" back in 2021 in the real world — but the
knowledge base didn't learn that fact until 2023-01-01 (the clock's start).
`as_of()` answers "what did we **know**", not "what was **true**":

```python
kb.as_of(datetime(2021, 6, 1, tzinfo=UTC)).assertions(subject=dana.id, predicate="Person.title")
# -> []   -- a real historical date, but BEFORE we had recorded anything
```

This is the whole point of tracking two time dimensions independently:
`as_of()` is a genuine reconstruction of the knowledge base's own historical
state of belief, not a query over `valid_from`/`valid_to` alone.
