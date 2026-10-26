# ADR-0055: Multi-Operation Proposals, and AI Entity Creation Through Them

**Status**: Proposed
**Date**: 2026-10-26
**Deciders**: Ontolith Core Team
**Related**: SPEC §9.1-9.2 (proposals, policy), SPEC §14.4 (`ontolith.propose` takes `ops[]`), ADR-0003 (agent identity), ADR-0008 (MCP surface, 2026-09-08 update), ADR-0019 (SemVer policy), ADR-0050 (`supersedes` hint), ADR-0054 (idempotent re-assertion), KI-082, KI-090, KI-091

---

## Context

Two related gaps.

**One operation per proposal.** One sentence in a meeting can yield a decision, an action, an owner, a deadline and a meeting reference: new entities plus several facts about them. Today the public proposal methods (`propose`, `propose_ref`) each stage exactly one assertion, so that is five proposals, reviewed one at a time. The stored payload is already a list (`payload["operations"]`) and `_replay_proposal_operations` already applies a list, and SPEC §14.4 already shows `ontolith.propose` taking `ops[]`; only the public entry point is missing. A new entity cannot be part of a proposal at all, so a fact about a not-yet-existing entity cannot be proposed together with the entity.

**AI entity creation bypasses review.** `Ontology.create_entity` only rejects `read`-only principals, so an `ai` principal creates entities directly (ADR-0008's 2026-09-08 update records this as deliberate: an entity is "an identity anchor, not a fact", so there was no policy step). The cost is orphans: when a reviewer rejects the facts an agent proposed about an entity it just created, the empty entity stays behind with no review trail and nothing to remove it.

**A hazard to design around.** Every shipped `PolicyStrategy` reads `proposal.payload["operations"][0]` and ignores the rest. That is harmless while a proposal has one operation. If a proposal can carry several, a low-risk first operation would carry the rest past review. Multi-operation proposals are only safe if policy evaluates every operation.

## Decision

### 1. `Ontology.propose_batch`

A new public method stages several operations as **one proposal**, accepted or rejected atomically.

```python
kb.propose_batch(
    [
        CreateEntityOp(key="d1", concept="Decision"),
        AssertLiteralOp(
            subject=LocalRef("d1"),
            predicate="Decision.title",
            value="...",
            value_type="Text",
            source="...",
        ),
        AssertRefOp(
            subject=LocalRef("d1"), predicate="Decision.owner", target=person_id, source="..."
        ),
    ],
    author="...",
)  # -> tuple[Proposal, Decision]
```

- **Typed operations.** `CreateEntityOp`, `AssertLiteralOp` and `AssertRefOp` are frozen Pydantic models, exported from `ontolith`. The assertion ops carry the same fields as `propose`/`propose_ref` (confidence, source, rationale, model, validity window, `supersedes`). Retraction stays its own method; it is out of scope here.
- **Local keys.** `CreateEntityOp.key` names a new entity inside the batch. Elsewhere in the batch a `LocalRef(key)` can stand for it as a `subject` or `target`. A key must be declared by an earlier operation (no forward references) and be unique within the batch. Real entity ids are still plain strings. The ids of new entities are allocated from the injected `IdProvider` when the proposal is **accepted**, not when it is submitted, so a rejected batch never reserves one.
- **Validation at submit.** Each operation passes the checks its single-operation counterpart passes (capability, known concept and predicate, `value_type`, target existence and target concept, `supersedes` hint shape). A `LocalRef` is checked against the concept its `CreateEntityOp` declares. Natural-key collisions (KI-091) are checked too. Errors use the existing `ValidationError`/`NotFoundError` taxonomy.
- **Re-checks at accept.** Replay re-runs the same existence, concept and natural-key checks, as `accept_proposal` already does for target concept (KI-112).
- **Atomicity.** Accept applies every operation in one transaction (the existing one-transaction-per-acceptance rule); any failure rolls the whole proposal back. Reject rejects all of it. A batch is never partially applied, so no orphan entity can remain.
- **Size.** A batch is capped at 100 operations, so one review stays reviewable. An empty batch is a `ValidationError`.
- **Same pipeline.** Assertion operations pass through conflict routing (SPEC §10) and idempotent re-assertion (ADR-0054) exactly as in a single proposal. A repeat inside a batch is absorbed like any other.

### 2. Policy sees every operation: the strictest operation wins

For a proposal with more than one operation, `Ontology` evaluates the configured `PolicyStrategy` **once per operation**, each against a view of the proposal holding only that operation, and combines the results:

1. Any `Reject` rejects the proposal (the first reason is reported).
2. Otherwise any `RequireReview` requires review; the reviewer sets are unioned and the reasons joined.
3. Otherwise `AutoAccept`.

Policies keep their single-operation logic and the `PolicyStrategy` protocol does not change (ADR-0019). The fan-out lives in one place in `Ontology`, so a custom policy is covered without being rewritten. `evaluate` stays pure; it is simply called more than once, each time against the same pinned `kb_view` (ADR-0025).

A `create_entity` operation carries no confidence, source or temporality, like a `retract` operation. Policies already tolerate that shape for retraction, and the implementation PR verifies it for each shipped policy. A batch of only `create_entity` operations is judged on the author-level rules (capability, AI kind), which is what keeps AI-created entities in review.

Not chosen: forcing every multi-operation proposal to review (it removes the review-click saving for routine batches), and changing `PolicyStrategy` to take the whole payload (it breaks the plugin-facing contract and every custom policy).

### 3. AI principals create entities only inside a proposal

- `Ontology.create_entity` **raises `CapabilityError`** for an `ai`-kind author, pointing at `propose_batch`. Humans and services keep direct `create_entity`, unchanged.
- The MCP tool `ontolith.create_entity` (and the REST/GraphQL entry points when the caller is an AI principal) becomes a thin wrapper over a one-operation `propose_batch`. It returns the proposal and decision, not an entity. Under the default `ThresholdPolicy` an AI author's proposal always goes to review, so a rejected proposal leaves no entity behind.
- A new MCP tool `ontolith.propose_batch` is added. It is propose-tier, takes `ops[]`, stamps the author from the verified token, and exposes no direct write, as before. ADR-0008's tool list and its 2026-09-08 update are amended in the implementation PR.

## Rationale

**Why one proposal rather than five.** A reviewer decides on a unit of meaning ("this decision, its owner and its deadline"), not on five rows. It also removes the orphan problem structurally: an entity and its first facts live or die together.

**Why fan-out and not a new policy contract.** The risk is a policy that looks only at the first operation. Fixing it where the policy is invoked closes it for every strategy, including ones written by users, with no change to `PolicyStrategy`.

**Why block AI direct creation.** ADR-0008's reasoning ("an entity is not a fact") holds for the entity row itself. It does not hold for the pair "entity plus the facts that justify it": the orphan is a side effect of splitting them. Only AI principals are affected because they are the ones whose output is untrusted by default (ADR-0003); a human who creates an entity directly is accountable for it.

**Why typed operations with a local key, not dicts.** Public surfaces are fully typed. A typed `LocalRef` also makes "this string is a real id" and "this string is a key in this batch" impossible to confuse.

**Why ids are allocated at accept.** A rejected, withdrawn or never-reviewed batch should reserve nothing. It also keeps replay deterministic: ids come from the injected provider at the single point the entities are created.

## Consequences

**Positive**
- Fewer review decisions, and no orphan entities from rejected agent proposals.
- Closes the `operations[0]` blind spot before it can be exploited by a multi-operation proposal.
- No storage migration: the payload is already a list of operations; `create_entity` is a new `kind` value in it. No `format_version` bump.

**Negative / follow-ups**
- **Behavior change for AI callers.** `create_entity` by an `ai` principal now raises, and the MCP/REST/GraphQL entity-creation entry points return a proposal for them. Flag `**Breaking:**` in the CHANGELOG and ship in a minor release, per ADR-0019's behavior-level precedent.
- A new `kind` in stored payloads: an older reader replaying a proposal that holds `create_entity` would not understand it. Proposals are created and replayed by the same version, but the implementation PR makes unknown `kind` values fail loudly (`ValidationError`) rather than being skipped.
- Per-operation policy evaluation costs one `evaluate` call per operation. The implementation PR measures a 100-operation batch.
- Retraction in a batch, and a cross-batch dependency (a batch that refers to entities from another pending proposal), are deliberately not supported yet.

**Delivery.** This ADR is the design only. Two implementation PRs follow: (1) `propose_batch`, the typed operations, local keys, accept-time id allocation and policy fan-out, with conformance vectors on both backends and Hypothesis properties; (2) the AI `create_entity` change, the MCP/REST/GraphQL wiring and `ontolith.propose_batch`, with ADR-0008 amended.

**Tests required before the implementation merges** (conflict and policy logic is test-first): conformance vectors on both backends for atomic accept (one failing operation rolls back every operation and every new entity), atomic reject (no entity created), a batch with a local-key chain, a forward or duplicate key rejected, a target-concept mismatch against a `LocalRef`, natural-key collision, id allocation at accept (a rejected batch consumes no ids), the 100-operation cap, an idempotent repeat inside a batch; policy vectors proving each shipped policy plus a custom one cannot be bypassed through a later operation (the strictest operation wins, `Reject` beats `RequireReview` beats `AutoAccept`, reviewer sets unioned); and Hypothesis properties that the combined decision is the strictest of the single-operation decisions for any operation order, and that accepting a batch yields the same state as applying its operations in sequence.

## Alternatives Considered

- **Keep single-operation proposals and add a client-side helper.** Rejected: it cannot make several operations one review unit, and the orphan problem stays.
- **Dicts in the stored payload shape as the public input.** Rejected: untyped on a public API, and it exposes the storage shape as contract.
- **Policies receive all operations.** Rejected: breaks `PolicyStrategy` and every custom policy.
- **Always review multi-operation proposals.** Rejected: loses auto-accept for routine, trusted batches.
- **Remove direct `create_entity` for every principal.** Rejected: breaks the CLI, importers and plugins for no gain, since the orphan problem is specific to untrusted AI authors.
- **Leave AI creation direct and document the orphans.** Rejected: leaves the review bypass in place.
