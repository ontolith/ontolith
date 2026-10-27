# ADR-0037: GraphQL Interface

**Status:** Accepted

**Date:** 2026-08-27

**Deciders:** Ontolith Core Team

**Related:** ADR-0021/ADR-0022 (REST interface), ADR-0014 (MCP bearer-token
authentication), ADR-0008 (MCP tool surface)

## Context

M3's scope column named two unstarted items: the RDF/OWL bridge (shipped in
ADR-0036) and a GraphQL interface. SPEC §14.3 defines GraphQL narrowly and
separately from REST's full resource list:

> GraphQL exposes `Entity`, `Assertion`, `Proposal`, `Contradiction`,
> `Principal` types with `query`, `propose`, and `review` operations
> mirroring the SDK.

This is a materially smaller surface than REST's, which — beyond SPEC parity
— also grew a direct-write route and principal/token admin as REST-specific
extensions (ADR-0022, KI-022). `pyproject.toml` already carried an unused
`graphql = ["strawberry-graphql>=0.219"]` optional-dependency group reserved
for exactly this, dating back to M0's repo skeleton.

## Decision

**1. Scope: query, propose, and review only — no direct write, no principal
admin.** `Query` exposes `schema`, `entity`, `query`, `provenance`,
`proposals`, `contradictions`, and `principals` (read-only — `Principal` is
one of SPEC's five named types, but SPEC's operation list doesn't include
principal creation). `Mutation` exposes `propose`, `acceptProposal`,
`rejectProposal`, `requestChanges`, `resubmitProposal`, `flagContradiction`,
and `resolveContradiction` — every SDK-level propose/review operation REST
already wraps, deliberately excluding REST's `POST /assertions` (direct
write) and `POST /principals`/token routes (admin). Widening this surface
later, if ever needed, is additive; narrowing an already-shipped GraphQL
schema is a breaking change for any client, so starting at SPEC's literal
scope was the safer default.

**2. Module shape mirrors `interfaces/rest.py`:** one file,
`src/ontolith/interfaces/graphql.py`, one factory,
`create_graphql_app(kb: Ontology, auth_provider: AuthProvider, name: str =
"ontolith", *, graphql_ide=...) -> FastAPI`, mounting a
`strawberry.fastapi.GraphQLRouter` at `/graphql`. Strawberry types are
defined in the same file, same as REST's Pydantic models.

**3. Auth: reuse ADR-0014 bearer tokens, required on every query and
mutation — but resolved differently than REST's `Depends`-based gate.**
REST's `_resolve_principal` dependency raises before a route body ever
runs. GraphQL has no equivalent single request-level gate that wouldn't
also block introspection (`{ __schema { ... } }`) — and introspection
needs to stay reachable unauthenticated, the same posture as REST's
`/docs`/`/openapi.json` (ADR-0021 §2: exposes the API's shape, not its
data). So `context_getter` resolves the bearer token once per request and
stores either the resolved `Principal` or the `AuthError` in
`info.context` — never raising there — and every resolver calls
`_require_principal(info)` first, which raises the stored error if
resolution failed. The exception then flows through GraphQL's normal
per-field error path into the response body, landing in the same place a
domain error from deeper in the call stack would.

**4. Error handling: one centralized hook, not per-resolver try/except —
the same "one mapping" principle ADR-0021 established for REST, adapted to
GraphQL's different error channel.** GraphQL has no HTTP-status channel for
domain errors: by convention every response is HTTP 200, with errors
surfacing in the response body's `errors[]` array. A custom
`_OntolithSchema(strawberry.Schema)` overrides `process_errors` (the
official hook `strawberry.Schema` exposes for exactly this) — for every
error whose `original_error` is an `OntolithError`, it sets `extensions =
{"code": ..., "detail": ...}`; `StorageError`/`PluginError` (the same two
REST maps to 5xx) additionally get their message redacted to a generic
string, with the real message logged server-side only, same rationale as
REST's redaction. Anything that isn't an `OntolithError` (a genuine
programming bug) falls through to the default `process_errors` behavior
(log-only) rather than being mistaken for a domain error — deliberately not
fabricating a `code` extension for it.

**5. Filters use an explicit `[FilterInput!]` list, not a JSON scalar.**
REST's `QueryIn.filters` is a `dict[str, str]`; GraphQL has no native map
type. Rather than adding a `JSON` scalar (a broader escape hatch than this
query surface needs), `FilterInput { key: String!, value: String! }` keeps
filters typed and self-documenting in the schema, at the cost of a more
verbose call shape than a bare dict.

**6. `EntityType.assertions` is a nested, lazily-resolved field, not a
flat container the way REST's `EntityDetailOut{entity, assertions}` is.**
This is the one place this interface diverges from mechanically mirroring
REST's response shape — GraphQL's whole value proposition is
client-selected nested fetching, so `entity(id) { id assertions { ... } }`
only touches `kb.backend.assertions()` when a caller actually asks for
them (verified by a dedicated test that patches `assertions()` to track
calls and confirms it's never invoked when the field isn't requested).
Every other type is a flat projection of its REST counterpart.

**7. `pyproject.toml`'s `graphql` extra widened to
`strawberry-graphql[fastapi]>=0.219` plus `uvicorn>=0.27`.** The bare
`strawberry-graphql` dependency doesn't pull in `fastapi` —
`strawberry.fastapi.GraphQLRouter` needs it, and previously it was only
reachable by also installing `ontolith[rest]` or `[all]`. Mirrors REST's
own self-sufficient extra (`rest = ["fastapi>=0.110", "uvicorn>=0.27"]`) so
`pip install ontolith[graphql]` alone is enough to run this interface.

## Rationale

**Why SPEC's literal scope instead of REST parity:** SPEC names GraphQL's
operation set explicitly and separately from REST's ("query, propose, and
review... mirroring the SDK"), and REST's own extra routes are documented
in ADR-0022 as deliberate REST-specific extensions beyond SPEC, not a
baseline every interface should match. Matching REST's full surface here
would mean re-deciding, in GraphQL's schema, exactly the same
direct-write/admin exposure question ADR-0021/0022 already settled once —
with no new information to justify a different answer.

**Why `process_errors` instead of wrapping every resolver:** Strawberry's
`Schema.process_errors` is the one hook that sees every field error
regardless of which resolver raised it (graphql-core catches per-field
exceptions and appends them to the result before this hook runs), making
it the direct GraphQL analog of REST's single
`@app.exception_handler(OntolithError)` — a decorator applied to N
resolvers would have been N places to keep in sync instead of one.

**Why defer `context_getter`-time auth instead of matching REST's model
exactly:** the alternative (raising `AuthError` inside `context_getter`)
would return a transport-level failure for the entire request, including
introspection — the one behavior REST doesn't have a direct analog for,
since REST's docs endpoints are separate routes from its data routes.
GraphQL bundles both under one endpoint, so the auth gate has to live
inside field resolution, not in front of it.

## Alternatives Considered

**A `JSON` scalar for `Query.query`'s filters, rejected.** Would let a
caller pass an arbitrary nested JSON value where only flat string equality/
operator filters are ever interpreted (`QueryBuilder.where`'s existing
`__contains`/`__gt`/etc. suffix convention, KI-039) — a wider surface than
the underlying capability, for no expressiveness gain over an explicit
`[FilterInput!]` list.

**Matching REST's exact response shapes (flat `EntityDetailOut`-style
containers) throughout, rejected for `EntityType.assertions`.** Considered
for consistency with every other type in this module, but would give up
GraphQL's core advantage (client-selected, lazily-resolved nested fields)
for a surface whose entire reason for existing alongside REST is that
advantage.

**Widening scope to match REST's write/admin routes now, rejected.** See
Rationale — no new information since ADR-0021/0022 to justify revisiting
that boundary, and narrowing a shipped GraphQL schema later is harder than
widening one.

## Consequences

**Positive:** SPEC §14.3's GraphQL requirement is met exactly as specified.
Zero new auth infrastructure (ADR-0014's tokens, unchanged). Centralized
error mapping, same principle as REST. `EntityType.assertions`
demonstrates the interface's actual value over REST for at least one field.

**Negative / follow-ups:** No direct-write or principal-admin mutations —
a GraphQL client needs REST or the SDK/CLI for those, by design (Decision
§1). No subscriptions (SPEC doesn't ask for any, and `strawberry`'s
subscription support wasn't evaluated). No pagination on `Query.query`
beyond `limit` (REST doesn't have one either, KI-022, and this interface
doesn't reopen it — offset pagination is one `QueryBuilder`-level addition
either interface will inherit together, not decided per-interface).
`Query.principals`' admin gate is enforced entirely inside
`Ontology.list_principals`, same as REST — a regression there would affect
both interfaces identically, which is the intended shared-gate behavior,
not a gap specific to this ADR.

M3's scope column is now fully closed: REST (ADR-0021/0022), hybrid
retrieval (KI-018), the RDF/OWL bridge (ADR-0036), and GraphQL (this ADR)
are all shipped.

## Update (2026-08-27): review-driven fixes

Review before merge found two real issues and several smaller gaps, all
fixed in the same PR:

- **Every root `Query`/`Mutation` field's `_require_principal(info)` call
  was effectively untested.** Two tests hit only `{ schema }`; mutation
  testing (deleting the call from `entity`, `query`, `provenance`,
  `proposals`, or `contradictions`) left the full suite green — each
  deletion would have shipped unauthenticated read access to entity data,
  assertion values, and proposal `source`/`rationale` text with no test
  failure. Fixed with a parametrized sweep test that walks every field
  strawberry's own introspection reports on `Query`/`Mutation` and asserts
  `AUTH_ERROR` with no token — new fields are covered automatically, not
  just the ones present today. `EntityType.assertions` also gained its own
  `_require_principal` call (previously relied solely on `Query.entity`
  gating the only path that reaches it) for the same reason: correctness
  that depends on "nothing else ever calls this" is one refactor away from
  silently breaking.
- **`_OntolithSchema.process_errors`'s fallback branch returned a resolver
  exception's raw message unredacted** — the *opposite* of REST's posture
  for the same failure class (an unmapped exception there gets FastAPI's
  generic 500, never the real message). This was reachable with an
  in-bounds-looking input: `Mutation.propose` with `confidence: 5.0` hits
  `Assertion`'s own pydantic bound check before reaching any `OntolithError`
  path, and the resulting text (a raw pydantic validation message) reached
  the client with no `code` extension at all — missing the SPEC §16
  envelope entirely, not just verbose. Fixed: any resolver exception that
  isn't an `OntolithError` is now also redacted to the generic message,
  logged server-side with `exc_info`, and given a distinct
  `extensions.code = "INTERNAL_ERROR"` so a client can still distinguish
  "some resolver bug" from a named domain error. A `None`
  `original_error` (GraphQL's own parse/validation failures, which never
  reach a resolver — e.g. malformed query syntax) is deliberately left
  unredacted; that text describes the query's own shape, not server
  internals, the same class of thing REST's `RequestValidationError`
  handler exposes. `_REDACT_MESSAGE_FOR` also switched from an exact
  `type(x) in ...` check to `isinstance` — the previous form failed *open*
  (unredacted) for a hypothetical future `OntolithError` subclass of
  `StorageError`/`PluginError`, the opposite of the fail-closed default
  this module otherwise aims for.
- **`Query.query`'s `filters: [FilterInput!]` silently dropped duplicate
  keys** (last-wins via a dict comprehension) instead of erroring — a
  correctness gap `ProposeInput`'s list-shaped filter design introduced
  that REST's plain `dict` body never could (a JSON object can't carry a
  duplicate key past the parser). Fixed: `Query.query` now raises
  `ValidationError` naming the duplicate key(s) before building the filter
  dict. A related finding — a filter `key` of literal `"self"` raising a
  raw `TypeError` from the `**kwargs` unpacking colliding with the bound
  method's own `self` — is a pre-existing gap shared with REST's identical
  `.where(**body.filters)` call, not introduced here; the redaction fix
  above already prevents it from leaking past the generic message, so it
  isn't separately patched in this ADR's scope.
- **No way to disable GraphQL introspection independent of the IDE.**
  `graphql_ide=None` correctly 404s the IDE's own UI, but an anonymous
  `POST /graphql` running `{ __schema { ... } }` directly still returned
  the full schema regardless — including every mutation name. Added
  `introspection: bool = True` (backed by
  `strawberry.extensions.DisableIntrospection`), and forwarded
  `docs_url`/`redoc_url`/`openapi_url` to the wrapped `FastAPI(...)` call
  the same way `create_rest_app` already does — previously hardcoded on
  with no way to turn off, an incomplete parity claim against the
  docstring's own "unauthenticated, like REST's docs_url" comparison.
- **Scope boundary (Decision §1) had no test enforcing it.** Added an
  introspection-based test asserting `Mutation`'s field set is exactly the
  seven named operations (now ten — see the 2026-09-05 and 2026-09-08
  Updates below) —
  mirrors `test_mcp_server.py`'s existing `test_no_write_tool_registered`
  precedent — so a future PR that quietly adds a write-shaped mutation
  fails a test instead of silently widening this ADR's stated boundary.

**Deliberately not fixed, recorded here instead** (both were MEDIUM/LOW
findings, not correctness or security regressions against this module's
own stated goals):

- **Resolvers are synchronous and run inline in the ASGI event loop**,
  unlike REST's routes, which Starlette dispatches to a thread pool by
  default. A slow resolver (a large query, a cold cache) blocks the event
  loop for every concurrent request, not just database work — measured
  directly: a deliberately slowed resolver serialized three concurrent
  requests where REST's equivalent overlapped them. Fixing this properly
  means `async def` resolvers offloading blocking calls via
  `starlette.concurrency.run_in_threadpool`, a change to every resolver's
  signature, not a localized fix — deferred as a follow-up rather than
  rushed into this PR. Real-world impact is softened, not eliminated, by
  `store/sqlite/backend.py`'s own process-wide write lock already
  serializing concurrent DB writes regardless of interface. Tracked as
  KI-052 (also filed in `docs/known-issues.md`, not just here, so it's
  visible in the tracked backlog).
- **No `as_of` argument on `Query.query`.** Consistent with REST and MCP —
  neither exposes bitemporal time-travel either — but worth naming
  explicitly since it's one of this project's headline capabilities and
  is the kind of gap easy to miss precisely because it matches existing
  precedent rather than standing out as new.
- **No query cost/complexity limiting.** An authenticated caller can send
  one request with many aliased selections (e.g. 200 aliased `entity {
  assertions }` fields) and have it execute the full multiple of backend
  calls in one round trip — verified directly. REST has no equivalent
  single-request amplification vector (each request maps to one route),
  and neither interface rate-limits today, so this isn't a regression
  against REST's posture, but GraphQL is the first interface where one
  HTTP request can carry unbounded work. A query depth/complexity limit
  (e.g. `strawberry.extensions.QueryDepthLimiter` or a cost-based
  extension) is a reasonable follow-up, not implemented here.

## Update (2026-08-28): resolvers converted to async, KI-052 resolved

The "Resolvers are synchronous and run inline in the ASGI event loop"
limitation named above (KI-052) is now fixed, as a small dedicated
follow-up rather than left deferred indefinitely.

Every `Query`/`Mutation` field resolver, `EntityType.assertions`, and
`create_graphql_app`'s `_get_context` are now `async def`.
`_require_principal`/`_kb` (cheap dict lookups, no I/O) still run inline;
every call that touches `kb`/`kb.backend` was factored into a plain sync
helper function (e.g. `_build_schema`, `_execute_query`,
`_do_accept_proposal`) and dispatched via
`starlette.concurrency.run_in_threadpool` — one helper per resolver,
mirroring the resolver's prior body exactly, so the fix is a mechanical
"extract and offload," not a behavior change. `_get_context`'s
`auth_provider.resolve(token)` call is offloaded the same way, since token
resolution is also a backend-backed lookup, not just a dict access.

Re-measured with the same deliberately-slowed-resolver setup this ADR's
first measurement used: three concurrent requests now complete in ~0.33s,
matching REST's ~0.31s (overlapping) rather than the original ~0.92s
(serialized). Pinned by a new regression test,
`TestResolverConcurrency::test_concurrent_requests_overlap_instead_of_serializing`
— built on `httpx2.AsyncClient` + `ASGITransport` with real
`asyncio.gather` concurrency rather than FastAPI's `TestClient` (which runs
every request through a single background portal thread and doesn't
exercise genuine concurrent event-loop scheduling the way a live async
client does). Verified the test actually catches a regression: reverting
one resolver to synchronous/inline execution reliably fails it.

Not addressed by this fix, and out of scope for it: `store/sqlite/backend.py`'s
own process-wide lock still serializes genuinely concurrent *writes*
regardless of interface — pre-existing, orthogonal to the event-loop
problem this fix closes, not something a resolver-level change can or
should touch.

This fix also has a second-order effect worth naming: converting resolvers
to `async def` means graphql-core now executes *sibling root fields within
one request* concurrently too, not just separate requests — a query
aliasing many fields now dispatches that many `run_in_threadpool` calls at
once, sharing the process's single anyio worker-thread pool (default
capacity 40, also shared with any co-mounted REST app in the same
process). Measured: 200 aliased fields at 0.5s each saturated the pool
enough to delay an unrelated concurrent request by ~2.5s. This is strictly
better than pre-fix behavior (the same query would have blocked the event
loop directly, for the full ~100s), not a regression — but it makes the
already-named, still-unimplemented query cost/complexity limiter
(`strawberry.extensions.QueryDepthLimiter` or similar) more load-bearing
than before this fix, not just a nice-to-have. Still not implemented here.

## Update (2026-08-30): query-amplification limiting, KI-056 resolved

A milestone-boundary security audit escalated the "not just a nice-to-have"
follow-up named above to a HIGH-severity finding (KI-056), for two reasons:
the 200-aliased-field measurement above is now a cross-interface DoS (the
shared anyio thread pool also serves any co-mounted REST app), and a second,
separate vector — unauthenticated recursive `__schema` introspection — was
also live by default.

**Alias/depth limiting, always on.** `create_graphql_app` now wires
`strawberry.extensions.MaxAliasesLimiter(max_alias_count=15)` and
`QueryDepthLimiter(max_depth=10)` into every schema unconditionally — not
tied to the `introspection` flag (a regression a review round actually
mutation-tested for and caught: nesting the limiter list inside `if not
introspection:` still passed every other test in the suite). `MaxAliasesLimiter`
is the one doing real work: it reduces the amplification ratio from
~200 aliased fields per request to 15, converting the vector from
"amplification" into an ordinary connection-count-proportional cost — it
**does not eliminate** the shared-pool exposure named above (measured: 15
concurrent worker threads per request against the pool's default 40-thread
capacity, so a handful of concurrent max-alias requests can still exhaust
it). `QueryDepthLimiter(max_depth=10)` is, today, pure defense-in-depth: no
type in this schema is recursive (nothing nests back into itself), so no
schema-valid query can reach anywhere near depth 10 — the limiter currently
rejects nothing a query-validity check wouldn't already reject on its own.
It earns its keep the day a recursive relation field is added.

**The alias limit also applies to `Mutation`, where the amplification threat
it's meant to address doesn't exist** — GraphQL requires root mutation
fields to execute serially, so N aliased mutations occupy one worker thread
at a time regardless of N. This is a real, if minor, behavior change: with
no bulk-propose mutation and no batch-query field in this schema, aliasing
was the only way to submit more than one proposal or fetch more than one
entity's assertions in a single round trip, and 15 is now a hard ceiling on
that too. Accepted as a reasonable tradeoff — one shared limiter is simpler
than a mutation-aware exception, and 15 aliased operations per request is
not a meaningfully tight ceiling for real client usage.

**`QueryDepthLimiter` does not bound introspection — verified empirically,
not assumed.** The carve-out is in *strawberry's own* depth-limiting
validator (`strawberry.extensions.query_depth_limiter`), not graphql-core —
graphql-core itself has no depth-limiting validator at all; strawberry's
hardcodes a skip for any field beginning with `__` ("by default, ignore the
introspection fields," in its own source comment) — confirmed a deeply
recursive `{ __schema { types { fields { type { ... } } } } }` query passes
depth validation untouched regardless of `max_depth`, and that a custom
`should_ignore` callback can't override the hardcoded carve-out either (it's
OR'd after the built-in check, never instead of it). `MaxTokensLimiter` was
also considered and rejected: it bounds the *request's* token count, not the
*response's* size, and introspection's amplification ratio is exactly a
short query producing a disproportionately large response by walking the
schema's own (cyclic) type graph — bounding input tokens doesn't touch that.

**Introspection now defaults to `False`.** Since no combination of the
built-in limiters can bound introspection's recursion, and REST's `docs_url`
analogy (module docstring, previously used to justify defaulting
introspection on) doesn't actually hold for this specific risk — REST's
OpenAPI JSON is a fixed, non-recursive document with no amplification
potential, unlike GraphQL's self-referential schema graph — introspection is
now off unless a deployment opts in via `introspection=True`. This is a
default-behavior change from this ADR's original decision, made deliberately
rather than silently: local development, trusted-network staging, or any
deployment that wants self-documenting tooling and doesn't face untrusted
traffic can still opt back in. One consequence worth naming: `graphql_ide`
still defaults to serving GraphiQL at `GET /graphql`, so the out-of-the-box
experience is a 200 IDE page that can't actually browse the schema unless
`introspection=True` is also passed — the module's `Usage:` block and the
`introspection` parameter's own docstring both now say so explicitly.

**Rejected: a custom depth-limiting validator that also bounds introspection.**
Considered instead of flipping the default (would have kept introspection
on by default with real depth protection), but requires hand-rolling a
graphql-core `ValidationRule` bypassing strawberry's hardcoded carve-out —
materially more engineering and testing burden than the fix shipped, for a
capability (introspection-with-bounded-recursion) most deployments don't
need by default anyway.

**Dependency floor bump, found in review.** `pyproject.toml`'s `graphql`
extra required `strawberry-graphql>=0.219`, a floor set before this fix
existed. The `extensions=[lambda: QueryDepthLimiter(...), ...]`
factory-callable pattern this fix introduces raises `TypeError` at
*request* time (not import time — `create_graphql_app` itself builds fine)
on strawberry-graphql<=0.313; verified against real released versions that
it works starting at 0.316. Bumped the floor to `>=0.316` accordingly — the
locked version (0.319) was already compatible, so this only changes what a
fresh `pip install ontolith[graphql]` resolves to, not this repo's own CI.
**Update (2026-08-31, KI-065):** the extra also gained an upper bound,
`strawberry-graphql[fastapi]>=0.316,<1.0`, mirroring `mcp`'s existing
`<2.0` convention (ADR-0026) — this floor bump is itself the evidence that
`<1.0` is a coarser guarantee here than `mcp`'s `<2.0`: a *minor* pre-1.0
release already broke this exact integration once, so the upper bound
only guards against the next major, not the next 0.x break.

## Update (2026-09-05, KI-057/KI-079): the enumerated mutation list has grown to nine

Decision §1's mutation list above (`propose`, `acceptProposal`, `rejectProposal`,
`requestChanges`, `resubmitProposal`, `flagContradiction`, `resolveContradiction` — seven) predates
two additions that were never folded back into it: `retract` (KI-057, ADR-0039, "governed
retraction through the same propose/policy/conflict-routing pipeline as `POST /proposals`") and
`assignReviewers` (KI-079, ADR-0046, SPEC §9.4's `assign` action). The schema's actual `Mutation`
type has nine fields today. This note exists so a reader comparing the enumerated list against the
live schema isn't misled into thinking either addition was an oversight — both are within this
ADR's own §1 scope boundary ("every SDK-level propose/review operation REST already wraps") and
neither is a direct-write or principal-admin mutation, so the boundary itself hasn't moved, only
the roster of what's already inside it. `retract`, `flagContradiction`, and `resolveContradiction`
already stretched "propose/review" to cover conflict-routing and contradiction-resolution
operations too, so `assignReviewers` (a review-workflow action, SPEC §9.4) fits the same pattern
rather than widening it further.

## Update (2026-09-08, KI-082): `createEntity` added — this one genuinely widens §1's boundary

Unlike the 2026-09-05 update above, this addition is not "within this ADR's own §1 scope boundary
worded differently" — `createEntity` is not a propose or review operation on an existing fact, it's
entity creation, and §1's own decision text names the mutation set as covering exactly "every
SDK-level propose/review operation REST already wraps." `createEntity` is the first mutation here
that doesn't fit that description under any reading, so the honest record is that the boundary
moved, not just the roster inside it.

The reason it's still the right call: `Ontology.create_entity()` was previously reachable on
exactly one interface (the CLI), so a GraphQL-only caller could assert facts about entities that
already exist but could never introduce a genuinely new one into the KB at all — SPEC §14.3's own
"mirroring the SDK" phrasing this ADR's Context quotes is unmet by omission, not honored by a
narrower-but-complete slice. Kept at the same `propose`-tier capability floor
`Ontology.create_entity()` already enforced (no new capability logic added); unlike REST's `POST
/assertions` (direct write, ADR-0022) it does not bypass SPEC §10 conflict routing or policy
evaluation, because entity creation was never routed through that pipeline at the SDK level either
— an entity carries no fact/confidence/temporality for a policy to evaluate. See ADR-0008's own
2026-09-08 update (`ontolith.create_entity`, KI-082) for the identical reasoning applied to MCP's
closed tool list, and REST's `POST /entities` (KI-082, same commit) for the third interface.

Two pre-existing SDK-level gaps this change makes agent-reachable for the first time, deliberately
left unfixed here and tracked separately: `create_entity()` didn't validate `concept` against the
active schema (**KI-090 since resolved**, via `_require_known_concept()`), and a duplicate
`(namespace, concept, natural_key)` surfaced as a redacted `StorageError` rather than a
caller-actionable validation error (**KI-091 since resolved** too, via
`_require_unique_natural_key()`).

## Update (2026-10-26): `proposeEntity`, ADR-0055

`Mutation.createEntity` rejects an `ai`-kind caller (`CAPABILITY_ERROR`); `Mutation.proposeEntity(input: {concept, naturalKey, model})` stages a one-operation batch and returns `ProposeResultType` (now with a `createdEntities` JSON field). The 2026-09-08 reasoning above that entity creation has no proposal path no longer holds for AI principals. `createEntity` keeps its `EntityType` return, since turning it into a union would break every existing client.

## References

- SPEC §14.3 (REST + GraphQL), §16 (error model), §8.3 (capabilities), §17
  (security model)
- ADR-0021 (REST read + propose slice), ADR-0022 (REST write/review/admin
  slice — names GraphQL as the one remaining M3 gap this ADR closes),
  ADR-0014 (MCP bearer-token authentication), ADR-0008 (MCP tool surface —
  the same read/propose-first precedent this ADR's scope follows, including
  its own identical `createEntity` boundary-widening update, KI-082)
- `docs/known-issues.md` KI-022 (REST's own deferred-scope list, referenced
  for what this ADR deliberately does not add), KI-052 (event-loop-blocking
  resolvers, resolved by the 2026-08-28 update above), KI-082 (entity
  creation added to REST/GraphQL/MCP), KI-090/KI-091 (gaps surfaced by
  KI-082, both since resolved)
