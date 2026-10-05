# Concepts

A handful of ideas everything else in Ontolith is built from. For the full
formal treatment, see the
[Technical Specification](https://github.com/ontolith/ontolith/blob/main/docs/Ontolith_SPEC.md) —
this page is the short version.

## Principals

A **principal** is an identified actor: `human`, `ai`, or `service` (the
latter for registered plugins). Every principal has a
**capability** — the ceiling on what it's allowed to do, ordered
`read < propose < write < review < admin` — and a **trust level**.

AI principals are structurally different, not just policy-different: they
**must** declare an accountable human/team **owner** — enforced both in
code and by a real database `CHECK` constraint (`CHECK (kind <> 'ai' OR
owner IS NOT NULL)`), not just convention — and every AI-authored
assertion captures the model family+version that made it (enforced in
code; no equivalent DB-level constraint exists for this second
requirement).

```mermaid
flowchart TB
    subgraph KINDS["Principals · every read and write is attributed to one"]
        direction LR
        AI["<b>ai</b> · id is a slug<br/>MUST name an accountable owner<br/>model + version stamped on each assertion<br/>defaults to propose<br/>can never direct-write, review, or resolve"]
        H["<b>human</b> · id is an email or URI"]
        S["<b>service</b> · id is a slug"]
        AI -- "owner (human or service)<br/>enforced in code and by a DB CHECK" --> H
    end

    KINDS -- "each holds one capability per namespace;<br/>acting_as delegation takes the lower of the two" --> CAPS

    subgraph CAPS["Capability ladder · each level includes the ones before it"]
        direction LR
        R["<b>read</b><br/>query · get · provenance"] --> P["<b>propose</b><br/>stage a change;<br/>policy decides"] --> W["<b>write</b><br/>direct write,<br/>still conflict-routed"] --> RV["<b>review</b><br/>accept or reject proposals;<br/>resolve contradictions"] --> AD["<b>admin</b><br/>schema · principals ·<br/>policy config"]
    end
```

## Assertions are append-only

An assertion is a single `(subject, predicate, value)` fact with full
provenance attached (author, source, confidence, timestamps). Once written,
an assertion's `value` is **never edited in place** — only its `status` and
temporal-validity fields (`valid_to`, `supersedes`) may change. "Updating" a
fact always means: create a new assertion, and — depending on the
predicate's declared temporality — either close the old one's validity
window (supersession) or flag a dispute (contradiction). See
[Conflict Handling](#conflict-handling) below.

This is what makes full audit history and bitemporal time-travel possible:
nothing is ever destroyed, so `as_of(t)` can always reconstruct what was
known.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> active: no conflict, or corroboration
    [*] --> flagged: static value disagrees<br/>with an active one
    active --> superseded: time_varying successor<br/>closes valid_to
    active --> flagged: a disagreeing static<br/>value arrives
    flagged --> active: chosen as winner
    flagged --> retracted: loses a resolution,<br/>or a direct retract
    active --> retracted: governed retract
    superseded --> retracted: an explicit retract<br/>of a superseded assertion

    note right of superseded
        Kept forever, so as_of(t)
        can still see it.
    end note
    note right of retracted
        Terminal. Never deleted.
    end note
```

## Confidence and provenance

**Confidence** is a single scalar (0.0–1.0) representing the asserting
principal's own stated belief. It is *never* auto-combined across
corroborating sources in v1 — if three sources each assert the same fact at
confidence 0.9, you get three separate, queryable assertions, not one
merged 0.97. Corroboration is surfaced, not synthesized.

**Provenance** — who, what source, when, which model+version, why, and the
full delegation chain — is captured automatically on every assertion and
retrievable in a single call (`kb.provenance(assertion_id)`). The "why" is
a dedicated `rationale` field — free text, optional, accepted by
`assert_literal()`/`propose()`/`propose_ref()` right alongside `source` and
`confidence`. It's deliberately separate from the scalar: `confidence` is
the cheap signal a policy can threshold on without reasoning about it,
`rationale` is where the actual justification or cited evidence lives for
a human (or another agent) auditing the claim later. Neither replaces the
other, and neither drives the routing decision itself — whether a
differing value opens a contradiction or supersedes the prior one depends
only on the predicate's declared temporality and cardinality (§10.1); a
human resolving a contradiction has both `confidence` and `rationale`
available as context, but nothing is auto-ranked or auto-resolved from
them.

## Temporality: static vs. time-varying

Every property/relation declares a **temporality**: `static` (the default)
or `time_varying`. This one setting decides how Ontolith reacts when a
differing value arrives for the same subject+predicate:

- **`static`** — the property doesn't change over time (a person's date of
  birth). A differing value is a genuine disagreement — the check is on
  the value alone, not on who asserted it — so it's flagged as a
  **contradiction**, not silently overwritten.
- **`time_varying`** — the property changes over time as a matter of course
  (a person's job title). A differing value **supersedes** the prior one,
  closing its validity window — no dispute, no review needed, because change
  over time is expected.

Multiple `time_varying` values can coexist if their validity windows don't
overlap (an employment history). See the
[Bitemporal Queries tutorial](tutorials/bitemporal-queries.md) for this in
action.

## Conflict handling

Routing is determined by the predicate's declared temporality first, then
its cardinality — a caller never chooses "supersede vs. contradict"
directly, and the check is purely on the *value*, not on who authored it
(the same principal contradicting themselves routes exactly like two
different principals disagreeing):

```
existing := active assertions on (subject, predicate) whose validity overlaps
t := schema.temporality(predicate)          # "static" (default) | "time_varying"
c := schema.cardinality(predicate)          # "single" (default) | "many"

if t == "static":
    if c == "many":
        activate(new_assertion)   # legitimately multi-valued (e.g. phone numbers); no dispute possible
    elif any existing.value != new_assertion.value:
        contradict(existing + [new_assertion])   # disputed, routed to review
elif t == "time_varying":
    if c == "many" and no explicit supersedes-hint was given:
        activate(new_assertion)   # a new concurrent value, not a replacement (e.g. concurrent job titles)
    else:
        supersede(existing, new_assertion)   # expected change, no review
```

```mermaid
flowchart TD
    A(["Accepted assertion A<br/>about subject S, predicate P, value V"]) --> OV{"Active assertions on S, P<br/>with an overlapping validity window?"}
    OV -- none --> ACT1["Activate A"]
    OV -- "one or more" --> T{"temporality of P"}

    T -- "static (default)" --> SCARD{"cardinality of P"}
    SCARD -- many --> ACT2["Activate A<br/>legitimately multi-valued"]
    SCARD -- single --> DIFF{"Does any existing value differ from V?"}
    DIFF -- no --> CORR["Activate A as corroboration<br/>both kept, confidence never merged"]
    DIFF -- yes --> CON["Open or extend a Contradiction<br/>non-terminal members become flagged"]
    CON --> REV["Human review<br/>resolve_contradiction picks a winner,<br/>the others become retracted"]

    T -- time_varying --> TCARD{"cardinality many<br/>and no supersedes hint?"}
    TCARD -- yes --> ACT3["Activate A<br/>concurrent values coexist"]
    TCARD -- no --> TDIFF{"Does the overlapping<br/>value differ from V?"}
    TDIFF -- no --> ACT5["Activate A<br/>same value coexists, no supersession"]
    TDIFF -- yes --> SUP["Supersede<br/>prior.valid_to = A.valid_from<br/>prior.status = superseded<br/>A.supersedes = prior.id"]
    SUP --> ACT4["Activate A · no review needed"]

    classDef dispute fill:#fdecd8,stroke:#b45309,color:#3b2106
    classDef change fill:#e3e8fb,stroke:#3b4cca,color:#141a3d
    class CON,REV dispute
    class SUP,ACT4 change
```

`cardinality="many"` (ADR-0017, extended to `time_varying` by ADR-0050)
changes what a differing, window-overlapping value means: for `static`, it
opts out of contradiction entirely (legitimately multi-valued, e.g. phone
numbers); for `time_varying`, it defaults to coexistence too (concurrent
values, e.g. two simultaneous job titles) unless the caller explicitly
names which specific existing assertion the new one replaces.

Flagged (contradicting) assertions are retained and queryable, but excluded
from default query results — you have to ask for them explicitly
(`status="flagged"`, or `.include_flagged()` on a `QueryBuilder`). A
`review`-or-above, non-AI principal resolves a contradiction explicitly with
`resolve_contradiction()`; nothing is ever auto-resolved.

## Bitemporality

Every assertion carries **two** independent time dimensions:

- **Valid time** (`valid_from`/`valid_to`) — when the fact was/is true in
  the real world.
- **Assertion time** (`asserted_at`) — when the knowledge base learned it.

`kb.as_of(t)` answers "what did we know at `t` about what was true at `t`" —
both dimensions must be satisfied. This is why a real historical fact can
still be invisible under `as_of()` at an early `t`, if the KB genuinely
hadn't recorded it yet: see the
[Bitemporal Queries tutorial](tutorials/bitemporal-queries.md) for a worked
example.

```mermaid
gantt
    title Dana's job title, recorded on two independent time axes
    dateFormat YYYY-MM-DD
    axisFormat %Y
    todayMarker off

    section Valid time
    Engineer · superseded, window closed        :done,   eng, 2020-01-01, 2023-06-01
    Senior Engineer · active, open window       :active, sen, 2023-06-01, 2024-12-31

    section Assertion time
    Engineer recorded 2023-01-01                :milestone, rec1, 2023-01-01, 0d
    Promotion recorded 2023-05-31               :milestone, rec2, 2023-05-31, 0d

    section as_of probes
    as_of 2021-06-01 returns nothing            :crit, milestone, q1, 2021-06-01, 0d
    as_of 2023-03-01 returns Engineer           :milestone, q2, 2023-03-01, 0d
    as_of 2023-07-01 returns Senior Engineer    :milestone, q3, 2023-07-01, 0d
```

This is the scenario in
[`examples/bitemporal_queries.py`](https://github.com/ontolith/ontolith/blob/main/examples/bitemporal_queries.py),
run verbatim — the `as_of(2021-06-01)` probe really does return nothing even
though Dana was genuinely an engineer then, because the KB hadn't recorded
it yet.

## Governance: proposals and policy

A **capability**-limited principal (typically an AI) doesn't write directly —
it **proposes**. A pluggable `PolicyStrategy` (the default,
`ThresholdPolicy`, ships with the SDK) evaluates every proposal and returns
one of three decisions: auto-accept, require human review, or reject. AI
proposals always route to review under the default policy, regardless of
trust level — see the
[Governance & Review tutorial](tutorials/governance-and-review.md).

## Plugins run governed and sandboxed

Importers, exporters, reasoners, connectors, and validators are discovered
via Python entry points and registered with a capped storage capability —
the *lower* of what the plugin's own manifest requests (`read` by default;
a plugin that writes must explicitly declare at least `propose`) and what
the registrar grants at registration time (`propose` by default). Neither
side alone decides the outcome; least privilege applies to both.
Since ADR-0051, a plugin's one protocol entrypoint runs in a **sandboxed
child process** by default, with `capabilities.network`/`.filesystem`
enforced at the OS syscall level on Linux (seccomp). A reasoner's derived
assertions must enter through the same proposal path as everything else —
no plugin bypasses governance. See the
[Writing a Plugin tutorial](tutorials/writing-a-plugin.md).

## The MCP surface has no write tool

The Model Context Protocol server Ontolith ships exposes ten tools
(`schema`, `get`, `create_entity`, `query`, `provenance`,
`list_contradictions`, `propose`, `retract`, `flag_contradiction`,
`resubmit`) — but **none of them is a direct-write tool**. Every
write-shaped one (`create_entity`, `propose`, `retract`, `resubmit`) goes
through the same governed proposal/policy path any other write does; there
is no MCP tool that commits a value assertion unconditionally. An AI agent
talking to a knowledge base through MCP can never bypass governance, full
stop.
