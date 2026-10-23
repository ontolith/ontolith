# ADR-0054: Re-Asserting an Identical Fact Is a No-Op

**Status**: Proposed
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

1. **Match key.** `(namespace, subject, predicate, value_kind, value, author, acting_as, source, valid_from, valid_to)` all equal. Equality is exact, with no canonicalization, consistent with ADR-0053. A different source, author, delegate or validity window is a different fact and is written as today.
2. **Matched statuses.** The existing assertion must be `active` or `flagged`. A `retracted` or `superseded` assertion never absorbs a repeat: re-asserting after a retraction is a deliberate new claim, and it must be able to land.
3. **Where it runs.** Before conflict routing, inside the same transaction and covering every write path that reaches `_apply_with_conflict_routing` (`assert_literal`, `assert_ref`, proposal acceptance and replay). The match is a fresh read inside the transaction, so two concurrent identical writes cannot both pass it.
4. **What a repeat returns.** The existing `Assertion`, unchanged. No assertion row, no assertion event, no `valid_to`/`status` change and no routing. If the existing one is `flagged` it is returned as `flagged`, and the open contradiction is not extended.
5. **Differing confidence or rationale.** Not part of the key and not applied. The repeat is a no-op and the earlier values stand. Assertions are append-only, so a changed belief is a new, distinct assertion (for example with a different source or window), not an edit.
6. **Proposals.** A proposal whose assertion operation resolves to an existing identical assertion is still accepted and keeps its own proposal events, so the attempt stays in the review trail. It just links to the existing assertion instead of creating one.
7. **Default behavior, no new parameter.** Signatures and return types do not change.

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
- A repeat with different confidence or rationale is dropped silently. Documented in the method docstrings.
- One extra indexed read per write. KI-100's cost note applies; the implementation PR measures it with the existing benchmark.

**Tests required before implementation merges:** conformance vectors on both backends for the three-repeat scenario on a `static` and a `time_varying` predicate, a `ref`-valued repeat, a different-source repeat still written, a different-window repeat still written, a repeat after retraction and after supersession still written, a flagged repeat returned without extending the contradiction, a repeat with different confidence returned unchanged, the proposal path linking to the existing assertion; plus a Hypothesis property that applying any write sequence twice leaves the same assertion set as applying it once.

## Alternatives Considered

- **Opt-in `idempotent=True`.** Rejected: keeps the duplicating behavior as the default.
- **Dedup only inside importers.** Rejected: leaves the SDK and MCP paths duplicating.
- **Ignore the validity window in the key.** Rejected: would absorb distinct `time_varying` history.
- **Include confidence and rationale in the key.** Rejected: a re-import that tweaks either would still duplicate.
- **Error on a repeat with different metadata.** Rejected: breaks retries.
