# ADR-0022: REST Interface — Write, Review, and Admin Slice

**Status:** Accepted

**Date:** 2026-07-22

**Deciders:** Ontolith Core Team

**Related:** ADR-0021 (REST read + propose slice), ADR-0014 (MCP bearer-token
authentication), ADR-0003 (agent identity, delegation)

## Context

ADR-0021 shipped REST's read + propose slice and deliberately deferred the
rest of SPEC §14.3's resource set — direct write, proposal review actions,
contradiction handling, and principal/token admin — to a follow-up PR
(KI-022's remaining-scope list). This ADR covers that follow-up.

Before implementing, the exact `Ontology`/`StorageBackend` surface backing
each deferred resource was audited method-by-method (signatures, exceptions,
implicit capability checks) rather than assumed. That audit found two gaps:

- **No `list_principals()` anywhere** — `Ontology.get_principal(id)` only
  fetches one principal by id; no `StorageBackend` method enumerates all
  principal rows either.
- **No namespace registry** — `Ontology.namespace` is hardcoded to
  `"default"` (an M1 limitation, `ontology.py`'s own comment); entity/
  assertion rows carry a free-text `namespace` column, but nothing tracks
  the set of namespaces that exist.

Both `GET /principals` (list) and `GET /namespaces` therefore have no
existing SDK method to wrap — building them means adding new
`StorageBackend` port methods first, real store-layer work, not just
"expose the existing SDK over HTTP" (the same standard ADR-0021 already
applied to defer `QueryBuilder.offset()`). Decided to defer both again
rather than grow this PR into store-layer design; tracked in KI-022's
updated remaining-scope list.

SPEC §14.3 also names `/proposals/{id}/review` as a third action alongside
`accept`/`reject`. No `Ontology` method backs a "review" action either — the
`under_review`/`changes_requested` proposal states exist in the schema, but
no method transitions into them (`assign`/`comment`/`request_changes` are
explicitly not implemented, per `ProposalEvent`'s own docstring). Deferred
alongside `/principals` and `/namespaces` for the same reason.

## Decision

**1. Ten new routes, all in the same `src/ontolith/interfaces/rest.py`,
using the exact `_resolve_principal`/error-mapping machinery ADR-0021
already built** — no new auth or error-handling infrastructure:

| Route | Method | Wraps |
|---|---|---|
| `/assertions` | POST | `Ontology.assert_literal`/`assert_ref` |
| `/proposals/{id}/accept` | POST | `Ontology.accept_proposal` |
| `/proposals/{id}/reject` | POST | `Ontology.reject_proposal` |
| `/contradictions` | GET | `Ontology.contradictions` |
| `/contradictions/flag` | POST | `Ontology.flag_contradiction` |
| `/contradictions/{id}/resolve` | POST | `Ontology.resolve_contradiction` |
| `/principals` | POST | `Ontology.create_principal` |
| `/principals/{id}/tokens` | POST | `Ontology.issue_token` |
| `/principals/{id}/tokens` | GET | `Ontology.list_tokens` |
| `/principals/{id}/tokens/{credential_id}` | DELETE | `Ontology.revoke_token` |

`POST /assertions` follows `POST /proposals`'s exact XOR shape: exactly one
of (`value` and `value_type`) or `target`. Unlike `POST /proposals`, a
`target` write accepts no `rationale` — `Ontology.assert_ref` doesn't take
one (`assert_literal` does) — an existing SDK-level asymmetry with
`assert_literal`, not something this ADR introduces or should paper over.
`POST /assertions` also accepts `valid_from`/`valid_to` (native `datetime`
fields, parsed by Pydantic), which `POST /proposals` doesn't expose today —
`assert_literal`/`assert_ref` take them, `propose`/`propose_ref` also take
them but ADR-0021's `ProposeIn` schema never added them; left as a pre-
existing, separate omission on the already-shipped route rather than
expanded here.

**2. No new REST-layer capability logic for eight of the ten routes** — the
wrapped method already enforces its own gate for eight of them (the ninth,
`GET /contradictions`, needs none either, but because it's a read covered by
ADR-0021's existing "authentication is the read-capability check" reasoning,
not because `Ontology.contradictions()` self-gates — it doesn't, matching
`Ontology.proposals()`). The eight: `assert_literal`/`assert_ref` require
`write`/`admin` and hard-block AI-kind principals (ADR-0003) regardless of
misconfigured capability; `accept_proposal`/`reject_proposal`/
`request_changes`/`resolve_contradiction` require `review`/`admin`, block
AI-kind reviewers, and reject a reviewer/resolver who is party to what
they're reviewing — the first three via the shared `_require_reviewer`
helper (author/delegate of the proposal), `resolve_contradiction` via its
own check (author/delegate of *any* member assertion, not just the one
being picked as winner — KI-026); `flag_contradiction` requires `propose`+;
`issue_token`/`revoke_token`/`list_tokens` each call
`Ontology.require_admin()` internally. The route handlers just resolve the
principal from the bearer token, pass `.id` through, and let the domain
exception surface through ADR-0021's existing `OntolithError` → HTTP
mapping — identical shape to every route ADR-0021 shipped.

**Correction (2026-07-29, KI-026):** the paragraph above originally claimed
`resolve_contradiction` already rejected a resolver who authored one of the
disputed members, grouping it with `accept_proposal`/`reject_proposal` as if
all three shared one guard. That was false — `resolve_contradiction` had no
such check at all until KI-026's fix added one. `accept_proposal`/
`reject_proposal`/`request_changes`'s own self-review guard (shared
`_require_reviewer`, ADR-0003) was and remains real; only the claim about
`resolve_contradiction` was wrong. The paragraph above now describes the
post-fix state accurately.

**3. `POST /principals` is the one exception: `Ontology.create_principal`
has no capability gate of its own.** Unlike every other write/admin method
above, `create_principal` takes no `author`/acting-principal argument at
all and performs no authorization check — by design, matching the CLI's
`principal create` command, which also has no `--author` flag (the "operator
provisions agents" trust model ADR-0014's Consequences section already
names: CLI has direct DB access and is implicitly trusted). REST has no
such implicit trust — it's network-reachable — so `create_principal_route`
calls `Ontology.require_admin(principal.id)` explicitly before calling
`create_principal`, reusing the exact gate `issue_token`/`revoke_token`/
`list_tokens` already call internally rather than hand-rolling a new check.

**4. A real bug found while wiring `POST /principals`, fixed in
`Ontology.create_principal` itself, not routed around at the REST layer.**
Calling `create_principal(kind="ai", owner=None)` — no owner at all, as
opposed to an owner string that doesn't resolve — raised a raw
**pydantic** `ValidationError` from `Principal`'s own `@model_validator`,
not `ontolith.core.errors.ValidationError`. `create_principal`'s docstring
already documented `ValidationError` as the contract; the CLI's blanket
`except Exception` masked the distinction, but REST's error mapping only
handles `OntolithError` subtypes — the untyped exception would have
escaped as an unhandled 500 with no SPEC §16 envelope at all. Fixed by
checking `kind == "ai" and owner is None` explicitly in
`create_principal`, before constructing the `Principal` model, raising the
documented `ontolith.core.errors.ValidationError` directly. The model's
own validator still runs as defense-in-depth for direct `Principal(...)`
construction elsewhere in the codebase; this fix only changes what
`Ontology.create_principal` itself raises. `conformance/
test_accountable_owner.py::test_ai_principal_without_owner_raises_at_application_layer`
previously accepted `(ValueError, StorageError)` — loose enough to pass
either way — tightened to require `ValidationError` specifically, now that
every backend gets one consistent domain exception here.

**5. Response/request schemas follow ADR-0021's existing per-route,
typed-Pydantic-class pattern** — `WriteAssertionIn`/`AssertionDetailOut`,
`RejectIn`, `ContradictionOut`/`ResolveContradictionIn`/
`FlagContradictionIn`/`FlagContradictionOut`, `CreatePrincipalIn`/
`PrincipalOut`, `TokenIssuedOut`/`CredentialOut` — no schema is reused
across routes with different field needs (e.g. `ContradictionOut` is its
own type, not a repurposed `ProvenanceOut`), matching ADR-0021's existing
style of small, purpose-fit models over speculative sharing.

**6. `POST /principals/{id}/tokens/DELETE` nests under `/principals/{id}/`
for REST resource discoverability even though `Ontology.revoke_token`
doesn't itself take or check `principal_id`** — it identifies the
credential solely by `credential_id`. The path segment is documented as
non-authoritative in the route's docstring rather than silently
implying a check that isn't performed.

## Rationale

**Why one PR for all ten routes, not split further:** KI-022's original
scoping already named this "a follow-up PR" (singular) for the full
deferred list; the ten routes are thin wrappers around already-gated (or,
for the one pure read, deliberately ungated per ADR-0021) SDK methods with
no new design surface between them, so splitting further would fragment
closely related, mechanically
similar work without a real risk-isolation benefit (unlike ADR-0021's
read/propose-vs-write/admin split, which *did* isolate the FastAPI/auth
wiring risk from higher-stakes write paths — that risk is already retired).

**Why fix the `create_principal` bug here instead of filing a separate
KI:** it was discovered directly by building `POST /principals`, blocks
that route from behaving correctly (an unmapped 500 instead of a clean
400), and the fix is small, targeted, and test-covered at both the
`Ontology` and conformance-vector level. Deferring it would ship a REST
route with a known crash on a documented, easily-reachable input.

**Why defer `/principals` (list) and `/namespaces` again instead of adding
the two missing methods now:** consistent with ADR-0021's own precedent
(`QueryBuilder.offset()`) — real `StorageBackend` port additions deserve
their own design pass (e.g., should `list_principals()` paginate? does a
namespace registry need its own table, or is `SELECT DISTINCT namespace`
sufficient despite missing namespaces with a schema but zero entities?)
rather than being decided as a side effect of an interfaces-layer PR.

## Alternatives Considered

**Building `list_principals()`/`namespaces()` now to complete the SPEC
§14.3 resource list in this PR, rejected.** Considered and explicitly
declined (see Context) — would mix real store-layer design decisions into
a PR whose stated purpose is wrapping existing SDK behavior.

**Leaving the `create_principal` pydantic-`ValidationError` gap unfixed and
having `create_principal_route` catch `Exception` broadly as a workaround,
rejected.** Would reintroduce exactly the kind of per-route error-handling
duplication ADR-0021's centralized `OntolithError` mapping was built to
eliminate, and would hide a real, fixable SDK bug behind a REST-only patch.

## Consequences

**Positive:** Full write/review/admin capability tiers now reachable over
HTTP with zero new auth or error-handling infrastructure. The
`create_principal` fix benefits every caller (CLI, SDK, MCP if ever
extended to principal management), not just REST.

**Negative / follow-ups:** `GET /principals`, `GET /namespaces`, and
`/proposals/{id}/review` remain unimplemented — no backing SDK method for
any of the three (KI-022, updated). `POST /proposals` still has no
`valid_from`/`valid_to` fields despite the underlying SDK method accepting
them (pre-existing, not introduced here).

## Update (2026-07-22): review-driven fixes

Review of this slice before merge found four real issues, all fixed in the
same PR:

- **`CreatePrincipalIn`'s `kind`/`auth_method`/`default_capability`/
  `trust_level` were plain `str`/`int`,** not typed to `Principal`'s own
  `Literal`/bounded constraints. An invalid value (e.g. `kind="wizard"`,
  `trust_level=99`) skipped Pydantic's own validation and reached
  `Ontology.create_principal`'s `Principal(...)` construction, raising the
  same *class* of raw, unmapped pydantic error the `owner=None` fix (Decision
  §4) was supposed to close for this route generally — closed for `owner`
  only, not for its sibling fields. Fixed by typing all four fields to match
  `Principal` exactly, so Pydantic itself rejects an invalid value into the
  existing `RequestValidationError` → SPEC §16 envelope path, no
  `Ontology`-layer change needed this time.
- **`issue_token_route` recovers the newly-issued credential's id via
  `list_tokens(...)[0]`, ordered by `created_at DESC` with no tiebreak** —
  two credentials issued in the same timestamp tick (coarse/injected
  `Clock`) could return the wrong `credential_id` alongside the right raw
  token, so a later `DELETE .../tokens/{credential_id}` would revoke the
  wrong credential. Fixed by adding `id DESC` as a secondary sort key to
  `StorageBackend.get_credentials_for_principal` (both SQLite and DuckDB) —
  closes the same-timestamp case deterministically. A second, narrower race
  (a *different* admin issuing another token for the same principal in the
  gap between this route's `issue_token()` and `list_tokens()` calls, which
  aren't wrapped in one transaction) is not closed by this fix and is
  tracked as KI-024, since properly closing it means changing
  `Ontology.issue_token`'s return contract to hand back the credential id
  directly — a larger API change than warranted as a review-response patch.
- **`DELETE /principals/{id}/tokens/{credential_id}` didn't verify the
  credential actually belonged to `principal_id`** (Decision §6 originally
  treated this as harmless-by-design). Reviewed again and judged worth
  closing: the route now calls `Ontology.require_admin()` explicitly, then
  `StorageBackend.get_credential(credential_id)` to check
  `credential.principal_id == principal_id`, raising `NotFoundError` on any
  mismatch (hiding existence rather than returning 403, consistent with
  other id-lookup routes in this file). Still admin-only and still not a
  privilege boundary — an admin retains full revoke authority via SDK/CLI
  regardless — but the URL now means what its shape implies.
- **Test coverage gap:** `rest.py` showed 100% line+branch coverage despite
  five routes (`reject`, `resolve`, `list tokens`, `revoke`, and `accept`'s
  non-self-review 403 path) never being exercised with a capability-denied
  principal — the gates live inside the wrapped `Ontology` methods, so
  100% coverage was reachable on happy paths alone. A regression that
  dropped one of these gates would not have been caught. Closed with one
  denial-path test per gap, plus two new tests for the `CreatePrincipalIn`
  typing fix and one for the credential-ownership fix.

## Update (2026-07-26): `list_principals()` closes the `/principals` gap

This ADR's Decision/Rationale (above) deliberately deferred `GET /principals`
because no `list_principals()` existed anywhere, and raised one open design
question before building it: **should it paginate?**

Resolved: no. `Ontology.list_tokens()` (credential listing, the closest
existing precedent — same admin-tier sensitivity class) doesn't paginate
either, and REST's own `/query` route already has a separate, tracked,
unimplemented pagination gap (KI-022's remaining-scope list) rather than
each list-shaped route inventing its own scheme. `list_principals()` returns
every principal, most recently created first — consistent with
`list_tokens`'s ordering — and pagination remains a single future addition
applied uniformly across all list routes, not decided per-route.

Added: `StorageBackend.list_principals() -> list[Principal]` (new required
Protocol method — breaking for third-party backends, see CHANGELOG),
`Ontology.list_principals(author)` (gated via the same `require_admin` used
by `issue_token`/`revoke_token`/`list_tokens`), `GET /principals` (REST,
admin-only), and `ontolith principal list` (CLI).

The namespace registry and `/proposals/{id}/review` state machine are
unaffected by this update — both remain deferred for the reasons already
stated in this ADR's Decision section (real design questions, not just a
missing accessor method).

## Update (2026-07-28): `request_changes()` closes the `/proposals/{id}/review` gap

The previous update's closing note called `/proposals/{id}/review` a "real
design question, not just a missing accessor method." Resolved narrowly:
SPEC §9.1's `under_review ──review──▶ { ACCEPTED | REJECTED |
changes_requested }` already names `changes_requested` as the third outcome
alongside the two `accept`/`reject` already implement — so the design
question was smaller than it looked. No new state, no new resource concept,
just the missing third leg of an existing triad.

New `Ontology.request_changes(proposal_id, reviewer, reason="")`: same
reviewer-eligibility and proposal-state preconditions as `accept_proposal`/
`reject_proposal` (existence, `review`/`admin` capability, not AI-kind, not
self-review, proposal in `require_review`/`under_review`), now factored into
one shared `Ontology._require_reviewer` helper (previously duplicated
verbatim across the two existing methods — a third near-identical copy was
the signal to extract it; pure refactor, no behavior change, existing tests
unchanged). No operations are applied; the proposal moves to
`changes_requested`, and a `ProposalEvent(type="request_changes")` is
recorded. `POST /proposals/{proposal_id}/review` (`ReviewIn{reason}` →
`ProposalOut`, mirroring `RejectIn`/`/reject` exactly) wraps it, reusing this
ADR's existing auth/error-mapping machinery unchanged.

**Why the same heavy reviewer gate (no AI, no self-review) as accept, not a
lighter one:** `changes_requested` applies no operations, so it might look
less consequential than accept — but it's arguably worse if under-gated.
Nothing resubmits a `changes_requested` proposal back to `submitted` yet
(see below), so it's currently a dead end. An AI principal with `review`
capability let loose on this action could drive every pending proposal into
that unrecoverable state — a governance denial-of-service that's harder to
notice and reverse than a bad accept (which is at least visible and
retractable via a subsequent proposal). `reject` is an honest terminal
state; `changes_requested` implies "fix and resubmit" while nothing can
resubmit — so if anything this action deserves the strict gate more than
accept does, not less.

`ProposalEvent.type`'s `Literal` widened to admit `"request_changes"`. This
is additive for *producers* (constructing a `ProposalEvent`) but narrows the
guarantee for *consumers* that exhaustively match on `.type` — existing
in-repo code doesn't do this, so nothing broke here, but a third-party
consumer performing an exhaustive match would need updating. Also required
widening — and, better, **removing** — the `type` `CHECK` constraint on
both backends' `proposal_event` tables (added for `accept`/`reject` only,
before this ADR; SPEC §12.2's own `proposal_event` DDL has no such
constraint). The `CHECK` was pure redundancy with the `Literal` validation
Pydantic already performs at construction time (this project's "validate at
edges, trust within" boundary), and it was a recurring migration hazard on
top of that: `CREATE TABLE IF NOT EXISTS` never widens an already-created
table's constraint, so this widening — like the next one `assign`/`comment`
would eventually force — silently breaks `request_changes()` on any
database file created before this change (`StorageError` → REST 500, with a
misleading "CHECK constraint failed" message). Removing the `CHECK`
entirely closes this class of hazard for good, but does **not** retroactively
fix already-created database files with the old 2-value constraint baked
in — this project has no DDL migration mechanism yet, and is still
pre-1.0/pre-alpha, so the accepted resolution is: recreate the database
file. A real migration mechanism is out of scope for this slice.

**Deliberately not addressed by this change** (all noted here so they stay
explicit, not silently assumed away):

- **The `require_review`/`under_review` conflation.** SPEC's diagram reads
  `require_review` as the policy *decision* label and `under_review` as the
  resulting persisted state; the codebase persists `require_review` itself
  and never produces `under_review` at all (`accept`/`reject`/
  `request_changes` all pragmatically accept either as a valid pre-state).
  Fixing this is a materially bigger change — the CLI's `proposal list`
  defaults to `--state require_review`, so changing what gets persisted
  would silently break that default too — and isn't required to close the
  `/review` route gap.
- **Resubmission** (`changes_requested ──resubmit──▶ submitted`, SPEC §9.1).
  `changes_requested` is currently a dead end — nothing transitions a
  proposal back out of it. A future, separate slice.
- **`assign`/`comment`** (the other two of SPEC §9.4's five named review
  actions) still have no backing method or `ProposalEvent` type — they need
  a new `Proposal` field (an assignee) or a comment thread, not just a state
  transition, so they're a bigger change than this slice's scope.

## Update (2026-07-28): namespace registry closes the `GET /namespaces` gap, KI-022 fully resolved

This ADR's original Context (§2 of the audit) raised an explicit open question: *"does a
namespace registry need its own table, or is `SELECT DISTINCT namespace` sufficient despite
missing namespaces with a schema but zero entities?"* Resolved in favor of a dedicated table,
modeled closely on SPEC §12.2's own normative DDL:
```sql
CREATE TABLE namespace (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, metadata TEXT
);
```
Both backends deviate from this in one respect, deliberately: `metadata TEXT NOT NULL DEFAULT
'{}'` rather than SPEC's literal nullable `metadata TEXT` — matches the existing `principal` table
convention and guarantees deserialization never has to handle a NULL `metadata` column. This is
the same class of pragmatic, documented deviation this project already makes elsewhere (e.g.
`KbView` vs. SPEC's literal `ReadOnlyView`, ADR-0025) — noted here because an earlier draft of this
section overstated it as "SPEC-literal."

New `Namespace` model (`ontolith.core.namespace`, alongside `Entity` — SPEC §5's meta-model lists
Namespace as a first-class concept). New `StorageBackend.list_namespaces() -> list[Namespace]`
port method, implemented identically on both backends. `Ontology.list_namespaces()` is ungated,
matching `proposals()`/`contradictions()` rather than `list_principals()`'s admin gate — namespace
metadata (id, creation time) carries nothing as sensitive as `list_principals()`'s `owner`/
`trust_level` fields. `GET /namespaces` (REST) and `ontolith namespace list` (CLI) follow the same
gating/shape precedent as their `GET /proposals`/`proposal list` counterparts.

**What actually gets registered, and when:** this project is still single-namespace throughout
(ADR-0015's own words), and this slice doesn't change that — `Ontology.namespace` is still
hardcoded, still not a constructor parameter, and there is still no explicit `put_namespace`/
create-namespace API. But two write paths *do* register a namespace as a side effect, both
idempotent: (1) both backends register `DEFAULT_NAMESPACE` at schema-creation time — the KB always
operates in exactly that namespace from the moment a backend exists; (2) `put_schema()` registers
`schema.namespace` before persisting the schema version, since `SchemaIR.namespace` is a
caller-chosen string independent of `Ontology.namespace` (`apply_schema()` doesn't cross-check it
against `self.namespace`) — without this, the registry would have the exact blind spot rejected
above, just relocated: a schema applied under a namespace nothing else ever touches would be
invisible to `list_namespaces()`, the same failure mode `SELECT DISTINCT namespace FROM entity`
was rejected for. Entity/assertion writes still do **not** register anything — a namespace that
only ever receives entities (no schema applied) stays invisible to the registry; extending
registration to every write path that carries a `namespace` field would start to look like the
namespace-creation API this update explicitly declines to build.

The idempotent-insert path in both cases is a read-then-maybe-write (`SELECT ... WHERE id = ?`,
insert only if absent), not an unconditional `INSERT OR IGNORE`/`ON CONFLICT DO NOTHING` on every
call. Found in review: an unconditional insert attempt takes SQLite's write lock even when the row
already exists, which turned every backend construction — including a purely read-only reconnect,
e.g. `ontolith namespace list` — into a blocking write. Against a database file another connection
held open for writing, that surfaced as a raw, unmapped `sqlite3.OperationalError` after SQLite's
default busy-timeout, bypassing the SPEC §16 error taxonomy entirely; a read-only database file
could no longer be opened at all. The read-then-maybe-write path keeps the common case (row
already present) genuinely read-only; `INSERT OR IGNORE`/`ON CONFLICT DO NOTHING` is kept on the
write path itself as the safety net against a genuine race between the check and the insert.

**Deliberately out of scope** (so this isn't mistaken for "Ontolith is now multi-tenant"):
namespace *creation* as a public API — no `put_namespace` on the port, no
`Ontology.connect(namespace=...)` parameter, despite SPEC §14.1 naming the latter as the normative
constructor signature (registration as a side effect of an existing write, per above, is not the
same thing); SPEC §12.2's `principal_trust` table (per-namespace trust/capability overrides) —
also normatively defined, also never implemented; per-namespace plugin enable/disable (SPEC §13.1
MUST, already flagged unimplemented in ADR-0015); per-principal/per-namespace REST or MCP read
scoping (`rest.py`'s module docstring already states outright there is none in this slice);
namespace-*existence* validation — `GET /schema`/the `ontolith.schema` MCP tool still return an
empty/`200` result for a namespace no one ever wrote anything to, rather than SPEC §16's `NotFound`
("entity/assertion/namespace missing"), even though the registry could now back that check. Each
is a materially larger
body of work than "the registry exists and is listable," and none is needed to close the literal
`GET /namespaces` gap this update resolves.

**KI-022 is now fully resolved.** All three pieces originally deferred for lack of a backing SDK
method — `list_principals`, `request_changes`, and now `list_namespaces` — are closed. The two
remaining named gaps (`/query` offset pagination, GraphQL) were always tracked as separate
concerns, not blocked on "no backing method."

## Update (2026-07-29): `resubmit()` closes the resubmission gap, KI-027

The `request_changes()` update above named resubmission (`changes_requested ──resubmit──▶
submitted`) as deliberately unaddressed and called `changes_requested` "currently a dead end."
That gap was logged as its own tracked issue (KI-027, found during a whole-project milestone
audit) and is now closed: new `Ontology.resubmit(proposal_id, author)` implements the transition,
re-running policy evaluation against the existing (unedited) payload. Only the proposal's own
author or delegate may call it — the inverse of `_require_reviewer`'s self-review guard used by
`accept_proposal`/`reject_proposal`/`request_changes`: this is an author action, not a reviewer
one, so the eligibility check requires the caller to *be* a party to the proposal rather than
forbidding it.

Unlike `propose`/`propose_ref` — which ADR-0025 established evaluate policy against a `kb_view`
pinned at `proposal.created_at`, on the grounds that there's no meaningfully different "evaluation
time" for a brand-new proposal — `resubmit`'s `kb_view` is pinned at the *resubmission* instant.
The proposal already exists; a KB-reading `PolicyStrategy` (e.g. one that checks corroborating
sources) must evaluate the resubmission against what's true now, not what was true when the
proposal was first drafted. This is a fourth call site with a genuinely different evaluation time,
not a mechanical reuse of the other three.

Implementation details worth recording because they weren't obvious going in:

- `accept_proposal`'s operation-replay loop (the `for op in proposal.payload.get("operations",
  [])` block dispatching on `assert_literal`/`assert_ref`/`retract`) was extracted into a shared
  `_replay_proposal_operations` helper so `resubmit`'s own auto-accept branch doesn't duplicate
  it. Temporality is still re-resolved dynamically per operation at replay time, not trusted from
  the payload snapshot — unchanged from `accept_proposal`'s existing behavior.
- `_finalize_non_accepted_decision` (shared by `propose`/`propose_ref`/`retract` since before this
  update) persists via `put_proposal`, an `INSERT` — correct for those three callers, which are
  always creating a brand-new row. `resubmit` re-decides an *already-persisted* row, so reusing
  `_finalize_non_accepted_decision` unmodified raised a `StorageError` (SQLite `UNIQUE constraint
  failed: proposal.id`) the moment the re-evaluated policy landed on `reject`/`require_review`
  again — caught by the conformance suite before this shipped, not in production. Fixed by adding
  an `is_new` flag that switches the persistence call to `update_proposal_state` (an `UPDATE`) when
  `False`; `resubmit` is the only caller that passes it. `update_proposal_state` clears
  `decided_at` unconditionally (unlike `policy_reason`, which is `COALESCE`'d) — load-bearing here,
  since a resubmission landing back in `require_review` must not carry forward the prior
  `request_changes` decision's `decided_at`; this asymmetry is now documented on the
  `StorageBackend` port itself, not just observable from the SQLite/DuckDB implementations.
- Unlike `propose`'s own auto-accept path (which records no `ProposalEvent` for a brand-new
  proposal — there's nothing to have a *prior* event about), `resubmit` re-decides an
  already-persisted row: a `ProposalEvent(type="resubmit")` is always recorded, regardless of
  outcome. Without it, a `require_review` outcome left no persisted trace that a resubmission
  happened at all — `decided_at` stays unset and `created_at` is unchanged, so the row alone is
  indistinguishable from one that was never resubmitted.

`POST /proposals/{proposal_id}/resubmit` (no request body; `principal.id` from auth supplies
`author`) wraps it, returning the same `ProposeOut{proposal, decision}` shape `POST /proposals`
does. MCP gained `ontolith.resubmit` — the only MCP-exposed way for an AI principal to act on its
own `changes_requested` proposal, since AI proposals always route to `require_review` (ADR-0003)
and AI principals have no write capability to fall back on; this does not widen the "no direct
write" MCP surface (ADR-0008), since `resubmit` re-runs policy exactly like `ontolith.propose`
does. CLI parity (a dedicated `ontolith proposal resubmit` command) is deferred to KI-032, which
already tracks the CLI's missing proposal-review commands as one item.

Separately, `Ontology.proposals()` gained a `state="pending"` query-level alias merging
`require_review` and `changes_requested` — KI-027's audit finding named this half of the bug too:
even with `resubmit()` now able to act on a `changes_requested` proposal, it stayed invisible to
any single-state query a reviewer would naturally run. `"pending"` was initially made the
*default* for `Ontology.proposals()`/`GET /proposals`/`ontolith proposal list`, but review caught
that this breaks the canonical reviewer loop (`for p in kb.proposals(): kb.accept_proposal(p.id,
...)`) the moment a `changes_requested` proposal appears in the result — `accept_proposal`/
`reject_proposal` both raise `ValidationError` on that state, and the merge doesn't even solve the
author-discoverability problem it was meant to (there's no `author=` filter, so an author still
can't isolate their own `changes_requested` proposals from everyone else's). The default was
reverted back to `state="require_review"`; `"pending"` remains available as an explicit,
documented opt-in (`GET /proposals?state=pending`, `ontolith proposal list --state pending`).

## Update (2026-10-26): `POST /entities` for AI callers, ADR-0055

For an `ai`-kind caller `POST /entities` stages a one-operation batch and returns 202 with the `POST /proposals` response shape (`ProposeOut`, including `created_entities` once applied) instead of 201 and an `EntityOut`. `model` is required for such a caller. The same route therefore returns one of two shapes by caller kind; the OpenAPI document declares both. A `read`-only caller still gets 403.

## References

- SPEC §14.3 (REST + GraphQL), §16 (error model), §8.3 (capabilities), §8.1
  (accountable owner), §9 (proposal workflow), §10.3 (contradictions), §5
  (meta-model — Namespace), §12.2 (normative `namespace`/`principal_trust`
  DDL)
- ADR-0021 (REST read + propose slice — this ADR's auth/error-handling
  foundation), ADR-0014 (MCP bearer-token authentication), ADR-0003 (agent
  identity, delegation, self-review guard), ADR-0015 (plugin capability
  isolation — states the project is still single-namespace throughout)
- `docs/known-issues.md` KI-022 (now fully resolved), KI-027 (resubmission gap, now resolved)
