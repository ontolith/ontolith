# ADR-0053: Contradiction Resolution Keeps Members That Corroborate the Winner

**Status**: Proposed
**Date**: 2026-10-22
**Deciders**: Ontolith Core Team
**Related**: SPEC §10.1 (corroboration: keep both), SPEC §10.3, SPEC §10.4 (resolution), ADR-0019 (SemVer policy), ADR-0031 (retracted/superseded are terminal for winner selection), KI-034

---

## Context

SPEC §10.1 says that when a new static assertion has the same value as an existing active one, it is *corroboration*: keep both, and do not merge confidence. Sources that agree are information, and Ontolith surfaces them rather than collapsing them.

SPEC §10.4's resolution pseudocode does not honor that. It retracts every member except the chosen winner:

```
for m in c.members where m.id != winner_id:
    m.status = "retracted"
```

Contradiction membership is not pairwise. When a differing value arrives, `_route_static` flags *every* overlapping active assertion on `(subject, predicate)` and adds all of them to the contradiction, including ones that agree with each other. So a contradiction with values `100` (source A), `100` (source B) and `120` (source C) has three members, and resolving in favor of A's `100` leaves:

```
[('100', active), ('100', retracted), ('120', retracted)]
```

B is retracted although it agrees with the winner. The agreement the SPEC says to preserve is lost at exactly the moment a reviewer confirms the value. This was reproduced on `main`; the behavior matches §10.4 as written, so this is a defect in the SPEC text, not drift in the implementation.

## Decision

Resolution is by **winning value**, not by winning assertion.

1. Every member whose `(value_kind, value)` equals the winner's is kept: a `flagged` one becomes `active`, and each gets a `reactivated` assertion event attributed to the resolver. The chosen winner is simply one of these.
2. Every member with a different `(value_kind, value)` is retracted, as today.
3. Members already `retracted` or `superseded` stay as they are, whichever group they fall in. ADR-0031 still applies: terminal means terminal, and they are never reactivated or overwritten.
4. Equality is the exact comparison conflict routing already uses (`e.value != V`). It is deliberately *not* canonicalized here: routing and resolution must agree on what "same value" means, so the members the router treated as corroborating are exactly the ones resolution keeps. Canonicalization, if wanted, is a separate decision that has to change both.
5. This is the **default behavior**, with no new parameter. `resolve_contradiction()`'s signature and return type are unchanged.
6. The resolution record needs no new column. Which members were kept and which were retracted is already in provenance as the per-member `reactivated` / `retracted` events written in the same transaction at the same timestamp. The `Contradiction` row keeps `resolved_by` / `resolved_at`. No storage migration and no `format_version` bump.

The self-resolution guard (KI-026) is unchanged: it still checks every member's author and delegate, kept or not.

One consequence worth stating: the outcome depends only on the winner's *value*, so resolving in favor of A's `100` or B's `100` gives identical results.

## Rationale

**Why default, not opt-in.** The current behavior contradicts §10.1, so leaving it as the default would leave a known correctness bug in place and require every caller to know to ask for the correct behavior. The change is a SPEC text correction first and a code change second.

**Why not a new keyword argument.** A reviewer who truly wants only one assertion kept can retract the unwanted corroborators explicitly afterwards, using the existing review-gated `retract()`. A parameter would be surface area (ADR-0019) for a rarely wanted case.

**Why by-value rather than "winner plus explicit corroborator ids".** The reviewer's decision in a contradiction is about which *value* is true. Asking them to also list which agreeing sources to keep adds a step that can only be answered one way.

**Why no resolution-record field.** The information already exists as append-only events, with the correct actor and time. Duplicating it onto the contradiction row would require a storage migration (ADR-0052) and a second source of truth that could drift.

## Consequences

**Positive**
- Corroboration survives resolution, consistent with §10.1. The `corroborating assertions are surfaced` guarantee holds end to end.
- Resolution becomes symmetric across agreeing members.
- No storage-format change.

**Negative / follow-ups**
- **Behavior change.** A resolution that previously retracted an agreeing member now leaves it active. Flag `**Breaking:**` in the CHANGELOG per ADR-0019's behavior-level precedent (ADR-0031), and ship in a minor release, not a patch.
- Queries after resolution can return more than one active assertion for the same `(subject, predicate)` where they previously returned one. That is the same shape corroboration already produces outside any contradiction, so consumers should already handle it.
- A contradiction whose only members agreeing with the winner are terminal gains nothing; behavior there is unchanged.

**Tests required before implementation merges** (per the project's TDD rule for conflict logic): conformance vectors on both backends for the A/B/C scenario, a terminal-corroborator case, a `ref`-valued case, and the unchanged party-guard behavior; plus a Hypothesis property that after any resolve, every non-terminal member equal to the winner's value is `active`, every non-terminal member with a different value is `retracted`, and the outcome is identical for any equal-valued choice of winner.

## Alternatives Considered

- **Keep §10.4 as is.** Rejected: it discards corroboration, which §10.1 and the product's provenance model exist to preserve.
- **Opt-in `keep_corroborating=True`.** Rejected: keeps the incorrect behavior as the default for no benefit.
- **Canonicalize values before comparing.** Deferred: it must change routing and resolution together, and is a separate decision.
- **Store kept/retracted lists on the `Contradiction`.** Rejected: duplicates the assertion event log and forces a storage migration.
