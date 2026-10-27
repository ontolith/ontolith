# ADR-0008: MCP Surface (Read + Propose, No Write)

**Status:** Accepted

**Date:** 2026-06-20

**Deciders:** Ontolith Core Team

## Context

The MCP (Model Context Protocol) server exposes Ontolith to AI agents as a tool. We need to decide:
- Which operations agents can perform directly
- Whether to expose direct write capability
- How to enforce the governance model

## Decision

**Read/query + propose + flag_contradiction + provenance. No direct write.**

**Exposed Tools:**

1. **`ontolith.schema`** — Read concepts/relations/temporality (read)
2. **`ontolith.query`** — Symbolic + semantic retrieval (read)
3. **`ontolith.get`** — Fetch entity + current assertions (read)
4. **`ontolith.provenance`** — Provenance projection for any assertion (read)
5. **`ontolith.propose`** — Create a proposal (propose, NOT write)
6. **`ontolith.flag_contradiction`** — Open/extend contradiction (propose, NOT write)
7. **`ontolith.resubmit`** — Resubmit a `changes_requested` proposal (propose, NOT write; added
   2026-07-29, see Update below, KI-027)
8. **`ontolith.retract`** — Propose retraction of an assertion (propose, NOT write; added
   2026-08-31, see Update below, KI-057/ADR-0039)
9. **`ontolith.list_contradictions`** — Read-only contradiction listing (read; added 2026-09-03,
   see Update below, KI-076)
10. **`ontolith.create_entity`** — Create a new entity (propose, not a direct write in this ADR's
    sense; added 2026-09-08, see Update below, KI-082). For an `ai`-kind caller it stages a
    one-operation batch and returns a proposal (amended 2026-10-26, ADR-0055)
11. **`ontolith.propose_batch`** — Propose several entities and facts as one proposal (propose,
    NOT write; added 2026-10-26, ADR-0055)

**Forbidden Tools:**
- ❌ `ontolith.write` — No direct write
- ❌ `ontolith.update` — No direct update
- ❌ `ontolith.delete` — No direct delete
- ❌ Any tool that bypasses proposal/policy pipeline

**Provenance Capture:**
- `ontolith.propose` stamps calling agent as `author` — resolved server-side from a verified
  bearer token, never a caller-supplied ID (ADR-0014)
- Captures `model` from agent context
- Records `acting_as` if delegation present

**Policy Decides:**
- Agent capability defaults to `propose`
- Policy engine evaluates every proposal
- Review gate enforced per policy (confidence, trust, source)

## Rationale

**Why no direct write:**
- Agents default to `propose` (governance model, ADR-0003)
- Write bypasses review (only for high-trust principals with explicit grant)
- MCP agents are untrusted by default (external to system)
- Prevents accidental/malicious silent overwrites

**Why propose instead:**
- Governance model is preserved (proposal → policy → review)
- Agent writes are auditable (provenance trail)
- Policy can auto-accept high-confidence proposals
- Review gate stops hallucinated facts

**Why read/query:**
- Grounding requires retrieval
- Read is safe (no side effects)
- Provenance lookup aids agent reasoning

**Why flag_contradiction:**
- Agents can surface disagreements (valuable signal)
- Contradiction creation is proposal-like (doesn't bypass governance)
- Helps maintain KB quality

## Consequences

**Positive:**
- ✅ Governance model enforced (no write bypass)
- ✅ Agent writes are reviewable
- ✅ Safe default (propose, not write)
- ✅ Provenance captures agent + model

**Negative:**
- ⚠️ High-latency for auto-accepted proposals (could optimize)
- ⚠️ Agents can't "just write" (requires proposal/accept)

**Mitigations:**
- Policy auto-accepts high-confidence proposals from trusted agents
- Proposal → accept is single transaction (latency minimized)
- Elevated agents can get `write` capability (explicit grant)

## Alternatives Considered

**Expose write tool:**
- Rejected: Bypasses governance, silently overwrites facts

**Expose write with policy check:**
- Rejected: Policy checks belong in proposal path, not tool layer

**No propose (read-only):**
- Rejected: Agents can't contribute knowledge

## Update (2026-07-29): `ontolith.resubmit` added, KI-027

A 7th tool, **`ontolith.resubmit`**, was added (propose, NOT write) — full rationale and
implementation notes are recorded in `ADR-0022`'s own 2026-07-29 update, not duplicated here. In
short: AI proposals always route to `require_review` (ADR-0003), so an AI principal whose proposal
lands in `changes_requested` has no write capability to fall back on — without this tool, MCP was
the one interface where that state was a genuine dead end for its own author. `ontolith.resubmit`
follows the exact same shape as `ontolith.propose`: it re-runs policy evaluation and cannot write
or edit an assertion directly, so it does not widen the "no direct write" boundary this ADR
establishes — it accepts only a `proposal_id` and a token-resolved principal, the same footprint as
the tool it mirrors.

## Update (2026-08-31): `ontolith.retract` added, KI-057/ADR-0039

An 8th tool, **`ontolith.retract`**, was added (propose, NOT write) — full rationale and
implementation notes are recorded in `ADR-0039`, not duplicated here. In short: `Ontology.retract()`
had no route on any of the four shipped interfaces, so a REST/GraphQL/MCP/CLI-only deployment had
no way to retract a fact at all (KI-057). `ontolith.retract` follows the same shape as
`ontolith.propose`/`ontolith.resubmit`: it is policy-evaluated and proposal-producing, cannot write
or edit an assertion directly, and accepts only an `assertion_id`, a token-resolved principal, and
an optional `acting_as` — the same footprint as the tools it mirrors. This is a different posture
than `resolve_contradiction()`, which has no policy evaluation at all and remains deliberately
excluded from MCP as a reviewer-only action (unchanged by this update).

## Update (2026-09-03): `ontolith.list_contradictions` added, KI-076

A 9th tool, **`ontolith.list_contradictions`** (read, not propose) — mirroring REST's
`GET /contradictions`/GraphQL's `Query.contradictions`. Before this, `ontolith.flag_contradiction`
(a propose-tier write: it extends membership and flips assertion statuses to `flagged`) was the
*only* MCP surface that returned a contradiction at all, so an agent that only wanted to inspect
one — including its accumulated `rationale_history` (KI-071/KI-075) — had no way to do so without
also performing a write, an out-of-scope gap found and filed during KI-075's own review. This tool
is `read`-tier, unlike the other tools added in the two Updates above: it requires no capability
beyond a resolved principal, the same as `ontolith.schema`/`.query`/`.get`/`.provenance` — `read`
is the floor of SPEC §8.3's capability order, so it does not widen this ADR's "no direct write"
boundary any more than those four already-listed read tools do.

## Update (2026-09-08): `ontolith.create_entity` added, KI-082

A 10th tool, **`ontolith.create_entity`** (propose tier), was added alongside REST's `POST
/entities` and GraphQL's `Mutation.createEntity` (KI-082) — `Ontology.create_entity()` was
previously exposed on exactly one interface, the CLI, so a REST/GraphQL/MCP-only caller could
assert facts about entities that already exist but could never introduce a genuinely new one into
the KB. This is the one tool added to this ADR's list that is not itself a proposal-producing
operation, so it's worth being explicit about why it doesn't violate the Forbidden list's "❌ Any
tool that bypasses proposal/policy pipeline": that clause is about *assertion* writes — SPEC §10
conflict routing and `PolicyStrategy` evaluation apply to facts, which carry a value, a
confidence, and a temporality for policy to reason about. An `Entity` carries none of that; it's
an identity anchor a fact is later asserted about, not itself a fact. `Ontology.create_entity()`
was never routed through the proposal/policy pipeline at the SDK level either — it always wrote
directly, gated only by a capability floor (rejects `read`-only) — so there was no existing
pipeline for this tool to bypass, unlike a hypothetical `ontolith.write`/`ontolith.update` that
would let an agent write an *assertion* directly. `ontolith.create_entity` keeps the identical
capability floor `Ontology.create_entity()` already enforced (propose-tier, no `kind == "ai"`
block, unlike `assert_literal`/`assert_ref`'s direct-write path) — no new capability logic was
added anywhere to accommodate this.

Two gaps surfaced during review, deliberately left unfixed here as pre-existing SDK behavior this
change makes newly agent-reachable rather than new regressions of its own — tracked separately
(KI-090, KI-091) rather than expanding this change's scope: `create_entity()` did not pre-validate
`concept` against the active schema (an agent could create entities under an undeclared concept
name, where `assert_literal`/`assert_ref` do validate `predicate` this way) — **KI-090 since
resolved**, via `_require_known_concept()` mirroring `_require_known_predicate()` exactly — and a
duplicate `(namespace, concept, natural_key)` triggered the DB's `UNIQUE` constraint late,
surfacing as a redacted, generic `StorageError` (500-class) rather than a caller-actionable
`ValidationError` naming the conflict — **KI-091 since resolved** too, via
`_require_unique_natural_key()` and a new `StorageBackend.get_entity_by_natural_key()` port method.

## Update (2026-10-26): AI entity creation goes through a proposal, `ontolith.propose_batch` added, ADR-0055

The 2026-09-08 update above reasoned that an entity is "an identity anchor, not a fact", so
`ontolith.create_entity` could create one directly. That holds for the entity row by itself but
not for an agent's entity together with the facts that justify it: when a reviewer rejects those
facts, the entity the agent already created stays behind with no review trail (ADR-0055).

- `Ontology.create_entity` now raises `CapabilityError` for an `ai`-kind author. Human and service
  principals are unchanged.
- **`ontolith.create_entity`**, called by an `ai`-kind principal, stages a one-operation batch
  (`CreateEntityOp`) and returns the proposal and decision (the `ontolith.propose_batch` shape)
  instead of an entity. Other callers still get the entity. It takes an optional `model`, required
  for AI callers.
- **`ontolith.propose_batch`** (11th tool, propose tier) takes `ops[]` (`create_entity`,
  `assert_literal`, `assert_ref`; `{"local": "<key>"}` names an entity created earlier in the
  batch), stamps the author from the verified token and returns the proposal. It exposes no
  direct write: the server's `PolicyStrategy` is evaluated per operation and the strictest outcome
  wins. The "no direct write" boundary above is unchanged.
- REST `POST /entities` returns 202 with the proposal for an AI caller. GraphQL
  `Mutation.createEntity` rejects an AI caller and `Mutation.proposeEntity` is the route for it,
  because a union return type would have broken every existing GraphQL client.

## References

- PRD §8 P6 (Interfaces: MCP server)
- PRD §11 (Trust & safety: AI-specific)
- PRD §16 Decision 8
- SPEC §14.4 (MCP server)
- ADR-0003 (Agent identity)
