# Technical Specification — **Ontolith**

*Engineering specification for the collaborative human + AI ontology framework. Defines the data model, semantics, storage layout, plugin contracts, and interface surfaces.*

| | |
|---|---|
| **Spec version** | 0.1 (draft) |
| **Implements** | PRD v0.2 |
| **Status** | For engineering review |
| **Last updated** | 2026-06-20 |
| **Language/runtime** | Python ≥ 3.11 |

> **Conventions.** The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are used per RFC 2119. Code and DDL are *normative for shape*, *indicative for exact syntax* — implementers pin syntax to the actual library version in use (e.g. `sqlite-vec`, which is pre-v1). This spec covers the **core**; plugin internals are specified by **contract only**.

---

## 1. Scope

This document specifies:
- The **core data model** and identifier scheme (§3–§5).
- The **schema definition system** (class DSL + LinkML-aligned YAML over one IR) (§6).
- **Assertion, provenance, and confidence** semantics (§7).
- The **principal / identity** model, including agents (§8).
- The **write path**: proposals, policy engine, review (§9).
- **Conflict semantics**: temporal supersession + contradictions (§10).
- **Query & retrieval**, including hybrid and temporal queries (§11).
- The **storage layer**: SQLite schema + the backend interface (§12).
- The **plugin system** contracts (§13).
- **Interfaces**: SDK, CLI, REST/GraphQL, MCP (§14).
- **Versioning/migration, errors, security, observability, conformance** (§15–§19).

Out of scope: the managed control plane, UI, and plugin marketplace (separate documents).

---

## 2. Architecture recap (normative layering)

```
SDK / CLI / REST / GraphQL / MCP        (§14)
Governance: proposals · policy · review · conflict   (§9, §10)
Identity: principals · owners · trust · delegation    (§8)
Query & retrieval: symbolic + vector · as_of          (§11)
Ontology core: concepts · relations · assertions      (§6, §7)
Persistence: SQLite default (+sqlite-vec); pluggable   (§12)
Plugin runtime & registry (cross-cutting)              (§13)
```

A conforming implementation **MUST** keep the storage layer behind the backend interface (§12.3); core logic **MUST NOT** issue backend-specific SQL.

---

## 3. Identifiers

| Object | ID scheme | Notes |
|---|---|---|
| Namespace | slug `[a-z0-9-]{1,64}` | Globally unique within a deployment. |
| Entity | ULID | Stable; **MAY** carry a `natural_key` unique within `(namespace, concept)`. |
| Assertion | ULID | Monotonic-ish, time-sortable — suits the append-only log. |
| Proposal | ULID | — |
| Contradiction | ULID | — |
| Principal (human) | email or stable URI | — |
| Principal (ai/service) | slug `[a-z0-9-]{1,64}` | — |
| Schema version | integer, per namespace, monotonic | — |

IDs **MUST** be treated as opaque by clients. Assertions are **append-only**: an assertion record is never mutated except for its `status`, `valid_to`, and `supersedes`/successor links (§7, §10).

---

## 4. Type system

Literal value types (the `value_type` of a property):

| Type | Python | Serialized form |
|---|---|---|
| `Text` | `str` | UTF-8 string |
| `Integer` | `int` | decimal string |
| `Float` | `float` | IEEE-754 string |
| `Boolean` | `bool` | `"true"`/`"false"` |
| `Date` | `datetime.date` | ISO-8601 date |
| `DateTime` | `datetime.datetime` | ISO-8601 UTC |
| `URI` | `str` | RFC 3986 |
| `JSON` | `dict`/`list` | canonical JSON |

Relations use `Ref[Concept]` and resolve to an **entity id**. Properties and relations declare:
- `cardinality`: `single` (default) | `many`.
- `temporality`: `static` (default) | `time_varying` (§10).
- optional `required`, `unique`, and `constraints` (validator-backed, §13).

---

## 5. Core data model

The meta-model is small and closed. All records are namespaced.

### 5.1 Concept / Property / Relation (schema objects)
Defined in code or YAML, compiled to the IR (§6), and stored as a `schema_version` row. Not instance data.

### 5.2 Entity
A concrete individual of a Concept.

| Field | Type | Req | Notes |
|---|---|---|---|
| `id` | ULID | ✓ | |
| `namespace` | slug | ✓ | |
| `concept` | name | ✓ | |
| `natural_key` | text | | unique within `(namespace, concept)` |
| `created_at` | datetime | ✓ | |
| `created_by` | principal id | ✓ | |

An Entity carries **no attribute values directly** — all attributes are Assertions about the entity. This keeps provenance/confidence/validity at the atomic level.

### 5.3 Assertion (atomic unit)

| Field | Type | Req | Notes |
|---|---|---|---|
| `id` | ULID | ✓ | |
| `namespace` | slug | ✓ | |
| `subject` | entity id | ✓ | |
| `predicate` | qualified name | ✓ | e.g. `Person.born` |
| `value_kind` | `literal`\|`ref` | ✓ | |
| `value_type` | type name | when literal | §4 |
| `value` | literal \| entity id | ✓ | per `value_kind` |
| `author` | principal id | ✓ | |
| `acting_as` | principal id | | delegation (§8.4) |
| `source` | string/URI | SHOULD | policy MAY require |
| `confidence` | float `0.0–1.0` | | asserting principal's **stated belief**; NULL allowed |
| `rationale` | text | | |
| `model` | string | for `ai` authors | captured model+version (§8.3) |
| `asserted_at` | datetime | ✓ | |
| `valid_from` | datetime | | defaults to `asserted_at` |
| `valid_to` | datetime | | NULL = open (currently valid) |
| `status` | enum | ✓ | `active`\|`superseded`\|`retracted`\|`flagged` |
| `proposal_id` | ULID | | introducing proposal |
| `supersedes` | assertion id | | set on supersession (§10.2) |
| `metadata` | JSON | | open blob; reserved for future confidence/aggregation evolution |

`confidence` is a single scalar and **MUST NOT** be auto-combined across corroborating assertions in v1 (PRD §16, decision 4). Implementations **MUST** preserve the `metadata` blob round-trip to keep that evolution path open.

### 5.4 Provenance (projection, not a table)
Provenance is a **derived view** assembled from an assertion plus its proposal trail:
`{author, acting_as, source, confidence, model, asserted_at, proposal_id, review_events[]}`.
Implementations **MUST** be able to return it for any assertion in one call (§14.1, §14.4).

### 5.5 Proposal
A staged set of write operations awaiting policy evaluation (§9).

### 5.6 Contradiction
Links ≥2 active assertions disagreeing about the same **static** `(subject, predicate)` (§10.3).

### 5.7 Principal
An identified actor (§8).

---

## 6. Schema definition system

### 6.1 One IR, two front-ends
There **MUST** be a single internal representation (IR) that is the source of truth. Two front-ends compile to it:

- **Class-based Python DSL** (primary DX; ships first). Built on Pydantic-style declarations.
- **LinkML-aligned YAML** (ships 0.2). The YAML dialect **MUST** be a strict subset/superset documented against LinkML so the LinkML bridge (§13, interop) is largely a projection.

Codegen **MUST** be bidirectional: `classes → IR → YAML` and `YAML → IR → class stubs`.

### 6.2 Class DSL (normative shape)
```python
from ontolith import Concept, Relation, Text, Date, Ref


class Organization(Concept):
    name: Text
    industry: Text | None = None


class Person(Concept):
    name: Text
    born: Date | None = None  # temporality defaults to "static"
    employer: Ref["Organization"] = Relation(
        inverse="employees",
        cardinality="single",
        temporality="time_varying",
    )
```

### 6.3 IR (normative content)
The IR for a namespace is a JSON document:
```json
{
  "namespace": "acme-research",
  "version": 3,
  "concepts": {
    "Person": {
      "properties": {
        "name":  {"type": "Text", "cardinality": "single", "required": true, "temporality": "static"},
        "born":  {"type": "Date", "cardinality": "single", "temporality": "static"}
      },
      "relations": {
        "employer": {"target": "Organization", "inverse": "employees",
                     "cardinality": "single", "temporality": "time_varying"}
      }
    }
  },
  "axioms": []
}
```

### 6.4 Migration rules
- **Additive** changes (new concept/property/relation, relaxing a constraint) **MUST** be applied without review gating and bump the schema version.
- **Breaking** changes (removing/renaming a field, tightening cardinality/uniqueness, changing a type, changing `temporality`) **MUST** be gated behind an explicit migration with a declared data-rewrite strategy, and **MUST NOT** silently invalidate existing assertions.
- Every applied schema **MUST** persist as a `schema_version` row to support time-travel (§11.4) and reproducibility.

---

## 7. Assertion & provenance semantics

1. Assertions are **append-only**. The only mutable fields are `status`, `valid_to`, and successor/`supersedes` links.
2. Creating an attribute value, updating it, or retracting it all produce **new** assertion records (or status changes), never in-place edits of the value.
3. `confidence` semantics: it is the **author's stated belief**, distinct from any future system-derived score. NULL means "unspecified," which policy **MAY** treat as below any threshold.
4. For `ai` authors, the resolving layer **MUST** capture `model` (family+version) at assertion time.
5. Validity: an assertion with `valid_to = NULL` and `status = active` is **currently asserted**. Time-travel (§11.4) reconstructs state by `valid_from/valid_to` and `asserted_at`.

---

## 8. Principal & identity model

### 8.1 Principal record

| Field | Type | Req | Notes |
|---|---|---|---|
| `id` | email/slug | ✓ | |
| `kind` | `human`\|`ai`\|`service` | ✓ | |
| `owner` | principal id | **required if `kind=ai`** | accountable human/team |
| `auth_method` | `oidc`\|`workload`\|`apikey` | ✓ | §8.2 |
| `default_capability` | capability | ✓ | default `read` for ai/service is `propose` per policy |
| `trust_level` | integer | ✓ | base level; per-namespace override allowed |
| `created_at` | datetime | ✓ | |
| `metadata` | JSON | | |

An `ai` principal **MUST** declare an `owner`. Implementations **MUST** reject creation of an `ai` principal without a resolvable owner.

### 8.2 Authentication
- **Humans**: OIDC/SSO via an `AuthProvider` plugin (§13).
- **Agents/services**: OIDC **client-credentials** or **workload identity**; API keys **MAY** be supported for local/dev only.
Auth resolves a request to a single `principal_id` (plus optional `acting_as`, §8.4).

### 8.3 Capabilities & trust
Capabilities (increasing): `read < propose < write < review < admin`. They are granted **per namespace**. Checks:
- `read`/`query`: required for any retrieval.
- `propose`: required to create a proposal.
- `write`: required for **direct** writes (bypassing review); granted only to high-trust principals with an explicit grant.
- `review`: required to accept/reject others' proposals.
- `admin`: schema changes, principal management, policy config.

Agents **MUST** default to `propose` and **MUST NOT** be granted `write` implicitly.

### 8.4 Delegation (acting-as)
When an agent acts for a human, the request carries an `acting_as` principal. The resolving layer **MUST** record both on every resulting assertion/proposal. The effective capability for the operation is `min(capability(author), capability(acting_as))` unless policy states otherwise.

---

## 9. Write path: proposals, policy, review

### 9.1 Proposal state machine
```
draft ──submit──▶ submitted ──policy──▶ ┌─ auto_accepted ─▶ ACCEPTED
                                        ├─ require_review ─▶ under_review
                                        └─ reject ─────────▶ REJECTED
under_review ──review──▶ { ACCEPTED | REJECTED | changes_requested }
changes_requested ──resubmit──▶ submitted
```
`ACCEPTED` and `REJECTED` are terminal. On `ACCEPTED`, staged assertions are applied through the conflict pipeline (§10).

### 9.2 Policy engine contract (policy as a pure function)
```python
class Decision: ...  # one of:


class AutoAccept(Decision): ...


class RequireReview(Decision):
    reviewers: list[str]
    reason: str


class Reject(Decision):
    reason: str


class PolicyStrategy(Protocol):
    def evaluate(self, proposal: Proposal, principal: Principal, kb: ReadOnlyView) -> Decision: ...
```
`evaluate` **MUST** be pure (no writes, deterministic given inputs) so it is testable and replayable. Built-in strategies a conforming impl **SHOULD** provide: `ConfidenceThreshold`, `TrustLevel`, `SourceRequired`, `RequireReviewByRole`, `SourceQuorum`, and `Composite(all=…, any=…)`.

### 9.3 Direct writes
A principal with `write` capability **MAY** bypass proposals; such writes still pass through the conflict pipeline (§10) and still record full provenance.

### 9.4 Review workflow
Review actions (`assign`, `comment`, `accept`, `reject`, `request_changes`) **MUST** be recorded as `proposal_event` rows and surface in provenance.

---

## 10. Conflict semantics

On applying an assertion `A` about `(subject S, predicate P, value V)`:

### 10.1 Routing
```
existing := active assertions on (S, P) whose validity overlaps A
t := schema.temporality(P)                       # static (default) | time_varying

if existing is empty:
    activate(A); return

if t == "time_varying":
    supersede(existing, A)                        # §10.2
elif t == "static":
    conflicting := [e in existing if e.value != V]
    if conflicting:
        contradict(conflicting + [A])             # §10.3
    else:
        activate(A)                                # corroboration: keep both, do NOT merge confidence
```

> **Implementation deviation, recorded in ADR-0017 and ADR-0050.** The routing above is silent on
> `PropertyDef.cardinality` (`"single"` | `"many"`, schema-declared, default `"single"`). The
> shipped implementation (`govern/conflict.route()`) adds it as a second axis: for `static`,
> `cardinality="many"` activates a differing value instead of contradicting — a third routing
> outcome distinct from both `static`+`single` (contradiction) and `time_varying` (supersession)
> (ADR-0017). For `time_varying`, `cardinality="many"` only supersedes a specific prior assertion
> when the caller names it via an explicit `supersedes=` hint; with no hint, a differing overlapping
> value coexists rather than superseding (ADR-0050) — otherwise two genuinely concurrent
> `time_varying` values (e.g. two simultaneous job titles) would collapse into one. Both deviations
> default to today's `cardinality="single"` behavior, so untouched schemas are unaffected.

### 10.2 Temporal supersession (`time_varying`)
```
def supersede(existing, A):
    for e in existing:
        if windows_overlap(e, A) and e.value != A.value:
            e.valid_to = A.valid_from
            e.status   = "superseded"
            A.supersedes = e.id           # link successor chain
    activate(A)
```
Multiple values **MAY** coexist if their validity windows do not overlap (e.g. employment history). No review is triggered — change over time is expected.

### 10.3 Contradiction (`static`)
```
def contradict(members):
    c := find_open_contradiction(S, P) or new Contradiction(S, P, state="open")
    for m in members:
        m.status = "flagged"
        c.add_member(m)
    persist(c)
    route_to_review(c)                    # per policy; rank members by confidence × source-trust
```
A flagged assertion is **retained and queryable** but **MUST** be excluded from default (unflagged) retrieval unless explicitly requested. Static facts are **never silently overwritten**.

### 10.4 Resolution
A `review`-capable principal resolves a contradiction by selecting a winner (or asserting a new value). Resolution is by the winner's **value**: members that corroborate it (§10.1) are kept, and only members that disagree are retracted (ADR-0053):
```
def resolve(c, winner_id, by):
    for m in c.members where m.status not in ("retracted", "superseded"):
        if (m.value_kind, m.value) == (winner.value_kind, winner.value):
            m.status = "active"           # the winner and any corroborating member
        else:
            m.status = "retracted"
    c.state = "resolved"; c.resolved_by = by; c.resolved_at = now()
```
Members already `retracted` or `superseded` are terminal and are left unchanged. Resolution events, including which members were kept and which were retracted, **MUST** appear in provenance.

---

## 11. Query & retrieval

### 11.1 Query builder (normative shape)
```python
kb.query(Person) \
  .where(employer="org-123") \                        # symbolic filter: property or
                                                        #   relation-target-id equality
  .semantic("computing pioneers") \                   # vector search (optional)
  .as_of("2025-01-01") \                              # temporal (optional)
  .min_confidence(0.5) \                              # provenance-aware (optional)
  .trust_at_least(2) \                                # filter by author trust (optional)
  .limit(20)
```
Multi-hop traversal through a related entity's own properties (e.g. a hypothetical
`employer__name=` filter) is **deferred** (ADR-0027). `.where()` supports equality on the
queried concept's own literal properties and relation-target ids, plus a closed set of
lookup-operator suffixes against literal properties only — `__contains` (substring) and
`__gt`/`__lt`/`__gte`/`__lte` (numeric range, restricted to schema-declared `Integer`/
`Float` predicates) — e.g. `where(age__gte=18, name__contains="Ada")` (KI-039, ADR-0027
amendment).

### 11.2 Symbolic semantics
- Filters compile to predicate lookups over `active` assertions by default (or the `as_of` snapshot); see the opt-ins below.
- A relation filter (`employer="org-123"`) matches the queried concept's own relation assertions by target id. Multi-hop traversal across subject→ref→subject (e.g. `employer__name`) is deferred (§11.1, ADR-0027).
- Flagged/superseded/retracted assertions are **excluded by default**; `.include_flagged()` (adds `flagged`) / `.include_history()` (adds `superseded`/`retracted`) opt in. Both widen which assertion statuses `.where()`, `.min_confidence()`, and `.trust_at_least()` match against; the result stays `list[Entity]` (ADR-0048).

### 11.3 Hybrid retrieval
- Semantic search runs over entity- and/or assertion-level embeddings via the active `Embedder` and the vector store (sqlite-vec by default).
- Results from symbolic and semantic paths **MUST** be combinable; ranking strategy is implementation-defined but **MUST** expose `confidence`, `recency`, and `trust` as signals.

### 11.4 Temporal queries (`as_of`)
`kb.as_of(t)` returns a read-only view where an assertion is visible iff `valid_from ≤ t < (valid_to or ∞)` and it was asserted by `t` — the full rule for an assertion that never changed status. §11.2's status exclusions still apply under `as_of`: a `flagged` assertion is excluded as of whenever the dispute existed (point-in-time, not current status), opt-in via `.include_flagged()`; a `retracted` assertion is additionally excluded once its own retraction has itself become known (i.e. `t` is at or after the retraction's own assertion-time) even if its validity window still nominally covers `t`, opt-in via `.include_history()` (ADR-0049). A `superseded` assertion needs no such exclusion — its `valid_to` closure already encodes the real-world end point the window check above handles directly. Schema is resolved to the `schema_version` effective at `t`.

---

## 12. Storage layer

### 12.1 Default backend
The default backend **MUST** be a single SQLite database file with WAL mode, plus `sqlite-vec` (`vec0`) for embeddings. It **MUST** require no external services.

### 12.2 SQLite schema (normative shape)
```sql
CREATE TABLE namespace (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, metadata TEXT
);

CREATE TABLE schema_version (
  namespace TEXT NOT NULL, version INTEGER NOT NULL,
  definition TEXT NOT NULL,                         -- IR JSON (§6.3)
  applied_at TEXT NOT NULL,
  PRIMARY KEY (namespace, version)
);

CREATE TABLE principal (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  owner TEXT, auth_method TEXT,
  default_capability TEXT NOT NULL DEFAULT 'read',
  trust_level INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, metadata TEXT,
  CHECK (kind <> 'ai' OR owner IS NOT NULL)
);
CREATE TABLE principal_trust (                      -- per-namespace overrides
  principal TEXT NOT NULL, namespace TEXT NOT NULL,
  trust_level INTEGER NOT NULL, capability TEXT,
  PRIMARY KEY (principal, namespace)
);

CREATE TABLE entity (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL, concept TEXT NOT NULL,
  natural_key TEXT, created_at TEXT NOT NULL, created_by TEXT NOT NULL,
  UNIQUE (namespace, concept, natural_key)
);

CREATE TABLE assertion (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
  subject TEXT NOT NULL, predicate TEXT NOT NULL,
  value_kind TEXT NOT NULL,                         -- literal|ref
  value_type TEXT, value_lit TEXT, value_ref TEXT,
  author TEXT NOT NULL, acting_as TEXT, source TEXT,
  confidence REAL, rationale TEXT, model TEXT,
  asserted_at TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
  status TEXT NOT NULL DEFAULT 'active',            -- active|superseded|retracted|flagged
  proposal_id TEXT, supersedes TEXT, metadata TEXT,
  CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0))
);
CREATE INDEX idx_assertion_spo  ON assertion(namespace, subject, predicate, status);
CREATE INDEX idx_assertion_subj ON assertion(namespace, subject);
CREATE INDEX idx_assertion_time ON assertion(asserted_at);
CREATE INDEX idx_assertion_valid ON assertion(valid_from, valid_to);

CREATE TABLE proposal (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
  author TEXT NOT NULL, acting_as TEXT,
  state TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT,
  policy_reason TEXT, payload TEXT NOT NULL          -- JSON: staged ops
);
CREATE TABLE proposal_event (
  id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,
  actor TEXT NOT NULL, type TEXT NOT NULL, detail TEXT, at TEXT NOT NULL
);

CREATE TABLE contradiction (
  id TEXT PRIMARY KEY, namespace TEXT NOT NULL,
  subject TEXT NOT NULL, predicate TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open',
  opened_at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT, resolution TEXT
);
CREATE TABLE contradiction_member (
  contradiction_id TEXT NOT NULL, assertion_id TEXT NOT NULL,
  PRIMARY KEY (contradiction_id, assertion_id)
);

-- embeddings (dim per active embedder; one table per dim or per scope)
CREATE VIRTUAL TABLE assertion_vec USING vec0(
  assertion_id TEXT PRIMARY KEY, embedding float[768]
);
```

### 12.3 Backend interface (normative)
A storage backend **MUST** implement:
```python
class StorageBackend(Protocol):
    # transactions
    def begin(self) -> Tx: ...
    def commit(self, tx: Tx) -> None: ...
    def rollback(self, tx: Tx) -> None: ...
    # writes
    def put_entity(self, e: Entity, tx: Tx) -> None: ...
    def put_assertion(self, a: Assertion, tx: Tx) -> None: ...
    def set_assertion_status(self, id: str, status: str, valid_to: str | None, tx: Tx) -> None: ...
    # reads
    def assertions(
        self,
        namespace: str,
        subject: str | None,
        predicate: str | None,
        status: set[str],
        as_of: str | None,
    ) -> Iterable[Assertion]: ...
    def get_entity(self, id: str) -> Entity | None: ...
    # vectors
    def vector_upsert(self, scope: str, id: str, vec: list[float]) -> None: ...
    def vector_search(self, scope: str, vec: list[float], k: int) -> list[tuple[str, float]]: ...
    # governance objects
    def put_proposal(self, p: Proposal, tx: Tx) -> None: ...
    def put_contradiction(self, c: Contradiction, tx: Tx) -> None: ...
```
All writes within a proposal acceptance **MUST** occur in a single transaction.

---

## 13. Plugin system

### 13.1 Discovery & lifecycle
Plugins are discovered via Python entry points under the `ontolith.plugins` group. Each plugin **MUST** declare a `name`, `version`, and a `capabilities` manifest. The runtime **MUST** load plugins with least privilege (§17) and **MUST** allow disabling any plugin per namespace.

### 13.2 Contracts (normative protocols)
```python
class Importer(Protocol):
    def import_(self, source, kb: WriteView) -> ImportReport: ...


class Exporter(Protocol):
    def export(self, kb: ReadOnlyView, target) -> ExportReport: ...


class Reasoner(Protocol):
    def derive(self, kb: ReadOnlyView) -> list[Assertion]: ...  # proposed, never auto-written


class Validator(Protocol):
    def validate(self, a: Assertion, kb: ReadOnlyView) -> list[Violation]: ...


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class Connector(Protocol):
    def sync(self, kb: WriteView) -> SyncReport: ...


class AuthProvider(Protocol):
    def authenticate(self, request) -> Identity: ...  # -> principal_id (+ acting_as)


class PolicyStrategy(Protocol):  # see §9.2
    def evaluate(self, proposal, principal, kb) -> Decision: ...


class StorageBackend(Protocol): ...  # see §12.3
```
Reasoner-derived assertions **MUST** enter through the proposal path (§9); plugins **MUST NOT** bypass governance.

### 13.3 Interop sequence
Bridges ship in order **LinkML → RDF/OWL → agent-memory** (PRD §16, decision 7). The LinkML exporter/importer **MUST** round-trip the IR; the RDF/OWL bridge **MAY** initially delegate to LinkML's RDF emission.

---

## 14. Interfaces

### 14.1 Python SDK (primary surface)
```python
Ontology.connect(namespace, schema=[...], backend=None, policy=None) -> KnowledgeBase

KnowledgeBase:
    principal(id, *, kind, owner=None, auth=None, default_capability=None) -> Principal
    get(Concept, **natural_key) -> Entity | None
    query(Concept) -> Query
    as_of(t) -> ReadOnlyView
    propose(ops, *, by, source=None, confidence=None, rationale=None) -> Proposal
    contradictions(state="open") -> list[Contradiction]
    migrate(new_schema, strategy=...) -> SchemaVersion

Principal:
    propose(entity_or_ops, *, source, confidence=None, rationale=None) -> Proposal
    can(capability, namespace) -> bool

Proposal:
    request_review(assignee=None) ; accept() ; reject(reason) ; request_changes(note)
    state ; confidence ; events

Entity:
    history() ; provenance(predicate) ; contradictions()
```

> **Implementation deviations, recorded in ADR-0047.** The block above shows the primary surface
> at design time; the shipped SDK deliberately deviates in a few places and **ADR-0047 is the
> authoritative record where they differ**. Notably: provenance is
> `Ontology.provenance(assertion_id) -> Provenance` (SPEC §5.4's one-call view, per assertion);
> `Entity` is a pure value object per §5.2 with no methods — its sketched `history()` /
> `provenance()` / `contradictions()` are `Ontology.assertions(status=None)` /
> `Ontology.provenance()` / `Ontology.contradictions()`; and principal creation vs look-up are
> separate `create_principal()` / `get_principal()` methods, not one `principal(...)` factory.
> (Unrelated to ADR-0047, `Ontology.connect(...)`'s signature and several `Principal`/`Proposal`
> members also differ from this block — the SDK's own docstrings are current there.)

### 14.2 CLI
`ontolith init` · `ontolith schema {show|migrate}` · `ontolith import|export` · `ontolith principal {add|list}` · `ontolith proposal {list|review}` · `ontolith history <entity>` · `ontolith query`.

> **Implementation deviations.** Abbreviated and drifted from the shipped command set in places not
> yet reconciled row-by-row, the same as §14.4's own note below — `interfaces/cli.py` is current.
> Notably: `ontolith init` doesn't exist as its own command (a database file is created implicitly on
> first connect, via `--db`/`ONTOLITH_DB`); `principal {add|list}` shipped as `principal
> {create|list}`; `entity`/`contradiction`/`namespace`/`admin-event` command groups exist with no
> mention above; `import|export` and `history` as named here were never implemented (plugin
> import/export is reached via `Ontology`/the plugin registry directly, not a CLI verb; `history`'s
> equivalent is `ontolith assertions --subject`). `ontolith db {status|migrate}` (SPEC §15,
> ADR-0052) was added 2026-09-20 for on-disk format-version migrations — a distinct thing from
> `schema migrate`'s domain-schema versioning (ADR-0034) despite the similar name.

### 14.3 REST + GraphQL
REST resources (auth required; capability-checked):
`/namespaces` · `/entities` · `/assertions` · `/proposals` (`POST` to create, `/{id}/accept|reject|review`) · `/contradictions/{id}/resolve` · `/principals` · `/query` · `/provenance/{assertion_id}`.
GraphQL exposes `Entity`, `Assertion`, `Proposal`, `Contradiction`, `Principal` types with `query`, `propose`, and `review` operations mirroring the SDK.

### 14.4 MCP server (agent-native)
Default tool set — **read and propose only**, no direct write (PRD §16, decision 8):

| Tool | Input (JSON Schema, abbreviated) | Effect |
|---|---|---|
| `ontolith.schema` | `{namespace}` | Return concepts/relations/temporality. |
| `ontolith.query` | `{concept, filters?, semantic?, as_of?, min_confidence?, trust_at_least?, limit?, include_flagged?, include_history?}` | Symbolic + semantic retrieval. |
| `ontolith.get` | `{namespace, concept, natural_key}` | Fetch an entity + current assertions. |
| `ontolith.provenance` | `{assertion_id}` | Return the provenance projection (§5.4). |
| `ontolith.propose` | `{namespace, ops[], source, confidence?, rationale?}` | Create a proposal (policy decides). |
| `ontolith.flag_contradiction` | `{namespace, subject, predicate, note?}` | Open/extend a contradiction. |

The server **MUST NOT** expose a direct-write tool. `ontolith.propose` **MUST** stamp the calling agent as `author`, capture `model`, and record `acting_as` when delegation is present.

> **Implementation deviations.** The table above is abbreviated and has drifted from the shipped
> tool set in places not yet reconciled row-by-row; `interfaces/mcp.py`'s own tool docstrings are
> current. Notably: every tool also takes `token` (ADR-0014, omitted above for brevity); the
> shipped set additionally includes `ontolith.create_entity` (KI-082), `ontolith.retract`
> (KI-057), `ontolith.list_contradictions` (KI-076), and `ontolith.resubmit` (KI-027), none listed
> here; and `namespace` was removed from every tool's inputs (ADR-0043 — `Ontology` hardcodes one
> namespace, so there was never anything to forward). The `ontolith.query` row above is current
> as of KI-094; other rows have not been re-verified to the same depth.

---

## 15. Versioning & migration
- **Spec/SemVer**: the SDK follows SemVer; the on-disk format carries a `format_version`.
- **Schema**: per §6.4; every change persists a `schema_version` row.
- **Migrations**: breaking changes ship with a declared rewrite strategy and a dry-run mode; they **MUST** be reversible or explicitly marked irreversible.
- **Time-travel** relies on never deleting superseded/retracted assertions or prior schema versions.

---

## 16. Error model
```
OntolithError
├─ SchemaError          # invalid/incompatible schema or migration
├─ ValidationError      # validator/constraint failure (carries Violations)
├─ AuthError            # authentication failed / no identity
├─ CapabilityError      # principal lacks required capability
├─ PolicyDenied         # proposal rejected by policy (carries reason)
├─ ConflictError        # unresolved contradiction blocks an operation
├─ NotFound             # entity/assertion/namespace missing
├─ StorageError         # backend failure
└─ PluginError          # plugin load/execution failure
```
Errors **MUST** carry a stable `code`, a human message, and structured `detail`.

---

## 17. Security model
- **Authz**: every operation **MUST** be capability-checked (§8.3) against the resolved principal (and `acting_as`).
- **Agent safety**: agents reach the KB only via `read`/`propose`/`flag_contradiction`; `write` requires an explicit grant.
- **Plugin sandboxing**: third-party plugins run with a declared capability manifest; the runtime **SHOULD** isolate execution and **MUST** deny undeclared access (storage, network, filesystem).
- **Audit**: all writes, proposal decisions, and resolutions are append-only and attributable; the audit trail **MUST NOT** be mutable.
- **Data ownership**: the default deployment keeps all data in a single local file with no external dependency.

---

## 18. Observability
Implementations **SHOULD** emit:
- **Metrics**: proposals created/accepted/rejected, auto-accept rate, review latency, open contradictions, human-vs-AI authored ratio, query latency (symbolic/semantic), vector index size.
- **Events**: proposal lifecycle, contradiction open/resolve, schema migration, plugin load.
- **Logs**: structured, with `namespace`, `principal`, `acting_as`, and `proposal_id` where applicable.

---

## 19. Conformance
A conforming implementation **MUST**:
1. Implement the data model (§5) with append-only assertions and the projection-based provenance (§5.4).
2. Enforce the capability and accountable-owner rules (§8).
3. Route writes through the proposal/policy path, with policy as a pure function (§9).
4. Implement conflict routing by temporality exactly as specified (§10), defaulting properties to `static`.
5. Provide the SDK and MCP surfaces (§14.1, §14.4), with MCP exposing no direct-write tool.
6. Keep storage behind the backend interface (§12.3) and ship the SQLite default.
7. Preserve the `metadata` blob and superseded/retracted history to support future confidence evolution and time-travel.

A test suite **SHOULD** include vectors for: supersession of a `time_varying` relation across overlapping windows; contradiction creation + resolution on a `static` property; policy auto-accept vs. review by confidence threshold; delegation provenance capture; and `as_of` reconstruction across a schema migration.

---

## Appendix A — End-to-end example (informative)
1. Architect defines `Person`/`Organization` (DSL → IR → `schema_version` 1).
2. Agent `scout` (owner `alice`) calls `ontolith.propose` to assert `Person.born`. Policy: confidence 0.82 < auto-accept 0.9 → `under_review`, assigned to `alice`.
3. `alice` accepts → assertion `active`, provenance records model+version, source, acting-as (none).
4. Later, `scout` proposes a different `born` value → `static` → **contradiction** opened, both flagged, routed to review.
5. `alice` resolves, selecting the sourced value → loser `retracted`, contradiction `resolved`.
6. `scout` proposes `employer` change → `time_varying` → prior window closed (`valid_to` set, `superseded`), new assertion `active`; `kb.as_of(last_year)` still shows the old employer.

## Appendix B — Open implementation questions (carried from PRD §16)
Traversal benchmark thresholds for the SQLite default; LinkML dialect coverage; confidence calibration across principal kinds; default policy thresholds per deployment profile; `sqlite-vec` (pre-v1) pinning vs. alternatives.
