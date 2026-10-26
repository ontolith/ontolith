# ADR-0054: Re-Asserting an Identical Fact Is a No-Op

**Status**: Accepted
**Date**: 2026-10-23
**Deciders**: Ontolith Core Team
**Related**: SPEC §5 (assertions are append-only), SPEC §10.1 (corroboration), ADR-0019 (SemVer policy), ADR-0053 (resolution keeps corroborating members)

---

## Context

SPEC §10.1 treats a second assertion with the same value as *corroboration*: keep both, do not merge confidence. That is meant for a different source agreeing. Today it also applies to the same principal repeating the same claim from the same source, because nothing compares the incoming assertion to the ones it matches. Writing `Item.name = "Bolt"` three times as one service principal from `sheet.xlsx#row2` leaves three active assertions, on `static` and `time_varying` predicates alike.

The consequences are practical:

- A weekly re-import of the same file looks like fresh corroboration, though it adds no information. Corroboration is only worth surfacing if it counts distinct sources.
- The assertion table grows without new information.
- Every caller that imports on a schedule has to check for an existing assertion before each write.

## Decision

An incoming assertion that is **identical to an existing one is not written**. The existing assertion is returned.

1. **Match key.** `(namespace, subject, predicate, value_kind, value, author, acting_as, source, valid_from, valid_to)` all equal. Equality is exact, with no canonicalization, consistent with ADR-0053. `valid_from` defaults to `asserted_at` (SPEC §5.3), so a repeat that leaves it unspecified is compared as "defaulted": it matches an existing assertion whose `valid_from` was also defaulted, and an explicit `valid_from` must match exactly. Without this a re-import could never match, since its default is a later timestamp. A different source, author, delegate or validity window is a different fact and is written as today.
2. **Matched statuses.** The existing assertion must be `active` or `flagged`. A `retracted` or `superseded` assertion never absorbs a repeat: re-asserting after a retraction is a deliberate new claim, and it must be able to land.
3. **Where it runs.** Before conflict routing, inside the same transaction and covering every write path that reaches `_apply_with_conflict_routing` (`assert_literal`, `assert_ref`, proposal acceptance and replay). The match is a fresh read inside the transaction, so two concurrent identical writes cannot both pass it.
4. **What a repeat returns.** The existing `Assertion`, unchanged. No assertion row, no assertion event, no `valid_to`/`status` change and no routing. If the existing one is `flagged` it is returned as `flagged`, and the open contradiction is not extended.
5. **Differing confidence, rationale or model.** Not part of the key and not applied. The repeat is a no-op and the earlier values stand. That includes `model`: a repeat from a newer model version leaves the earlier assertion's model on record, which is deliberate, since a changed belief or a new model's independent claim should come in as a distinct assertion (for example with its own source). `value_type` is likewise not in the key; `value_kind` plus `value` identify the claim, and a schema that changes a predicate's `value_type` does not make a repeat new.
6. **Proposals.** A proposal whose assertion operation matches an existing identical assertion is still accepted and keeps its own proposal events, so the attempt stays in the review trail. No assertion is created and none is linked to that proposal; the earlier assertion keeps pointing at the proposal that introduced it.
7. **Explicit `supersedes`.** A write that names a `supersedes` hint (ADR-0050) is never treated as a repeat. It asked for a specific replacement, and an invalid hint still raises `ValidationError`.
8. **Default behavior, no new parameter.** Signatures and return types do not change.

Corroboration by a **different** source or author is unchanged: it is still written, kept alongside the original and surfaced, with confidence never merged.

## Rationale

**Why default, not opt-in.** The duplicates carry no information, so no caller wants them. An opt-in flag would put the correct behavior behind an argument every importer must remember, and adds surface area (ADR-0019).

**Why this key.** `author` and `source` are what make an assertion a distinct piece of evidence; the window is part of what the fact claims, so folding it in would hide real `time_varying` history. Confidence and rationale describe the belief, not the claim, and varying them would keep the weekly re-import problem alive.

**Why return the old row rather than error.** Idempotent callers (importers, retries after a timeout) need a repeat to succeed. An error would force every one of them to catch it.

**Why not update the old row.** Updating confidence or rationale in place breaks the append-only rule. Silently dropping new values is the cost of keeping that rule; the alternative of raising an error was considered and rejected because retries with slightly different metadata are common.

## Consequences

**Positive**
- Re-imports and retries are safe, and the corroboration count reflects distinct evidence.
- Importers no longer need a pre-write existence check. This pairs with a later source-scoped sync, which depends on it.

**Negative / follow-ups**
- **Behavior change.** A repeat used to return a new assertion with a new id; it now returns the earlier one with its original `asserted_at`. Flag `**Breaking:**` in the CHANGELOG and ship in a minor release.
- Callers that relied on one `asserted_at` per call, or on counting writes by row count, will see differences.
- A repeat with different confidence, rationale or model is dropped silently. Documented in the method docstrings.
- Two extra reads of the `(subject, predicate)` assertions per write (active, then flagged). The existing write benchmarks stay within their §9 budgets with it; KI-100's cost note applies.

**Tests required before implementation merges:** conformance vectors on both backends for the three-repeat scenario on a `static` and a `time_varying` predicate, a `ref`-valued repeat, a different-source repeat still written, a different-window repeat still written, a repeat after retraction and after supersession still written, a flagged repeat returned without extending the contradiction, a repeat with different confidence returned unchanged, the proposal path creating no assertion, a write with a `supersedes` hint not being absorbed; plus a Hypothesis property that applying a `static`-predicate write sequence twice leaves the same assertion set as applying it once. The property is deliberately not claimed for `time_varying` predicates: once a later value supersedes an earlier one, replaying the sequence legitimately writes again.

## Alternatives Considered

- **Opt-in `idempotent=True`.** Rejected: keeps the duplicating behavior as the default.
- **Dedup only inside importers.** Rejected: leaves the SDK and MCP paths duplicating.
- **Ignore the validity window in the key.** Rejected: would absorb distinct `time_varying` history.
- **Include confidence and rationale in the key.** Rejected: a re-import that tweaks either would still duplicate.
- **Error on a repeat with different metadata.** Rejected: breaks retries.
