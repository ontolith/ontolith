# Known Issues and Gaps

Tracked gaps between the current implementation and the full SPEC/PRD requirements.
Each entry states the issue, its severity, the milestone it's targeted for, and where to find the relevant code.

---

## KI-001 — QueryBuilder.where() N+1 query pattern ✓ RESOLVED (M2)

**Severity:** Performance — critical gap against budget  
**Milestone target:** M2 — resolved in `perf(query): fix N+1 in QueryBuilder.where() with SQL subquery push-down`  
**SPEC reference:** SPEC §11, Implementation Plan §9 (p95 < 150 ms symbolic query)

### Description

`QueryBuilder.where()` in `src/ontolith/query/builder.py` filters by predicate value using Python-side iteration: it fetches all entities of the concept, then issues a separate `assertions()` SQL query per entity to check the predicate value. On the 100k-assertion benchmark seed (1k entities × 100 assertions) this produces 1,000 serial SQL round-trips, yielding a mean latency of ~15 seconds against the p95 budget of 150 ms — roughly 100× over budget.

M1 baseline captured in `tests/benchmarks/test_traversal.py::test_bench_symbolic_query_concept_filter`.

### Root cause

`builder.py:all()` calls `backend.entities()` for the concept scan, then for each entity calls `backend.assertions(subject=..., predicate=...)` and checks `value == filter_value` in Python. No SQL JOIN is used.

### Fix

Rewrite `QueryBuilder.all()` to issue a single SQL query joining `entity` and `assertion` on `subject`, filtering by `concept`, `predicate`, and `value_lit` in the WHERE clause. This will collapse the 1,000 round-trips into one indexed lookup.

---

## KI-002 — No governed retraction API on Ontology ✓ RESOLVED (M2)

**Severity:** Architecture gap — missing write surface  
**Milestone target:** M2 — resolved in `feat(govern): implement proposal workflow, conflict routing, and contradiction handling`  
**SPEC reference:** SPEC §9 (all writes through proposal path)

### Description

There is no `kb.retract()` or proposal-based retraction method on `Ontology`. The only way to retract an assertion in M1 is to call `kb.backend.set_assertion_status(id, "retracted")` directly — bypassing the proposal/policy path that SPEC §9 mandates for all writes.

This surfaces in the conformance vector `conformance/test_append_only.py::test_retracted_assertion_excluded_from_default_query`, which calls the backend directly.

### Fix

In M2, add `Ontology.retract(assertion_id, author)` that creates a `Proposal` with a retraction operation, runs it through `ThresholdPolicy`, and — if auto-accepted — calls `backend.set_assertion_status()`. This aligns retraction with the full governance pipeline.

---

## KI-003 — Append-only conformance vector does not directly test frozen model ✓ RESOLVED (M2)

**Severity:** Test gap — conformance coverage  
**Milestone target:** M2 — resolved in `test(conformance): add Hypothesis property tests for conflict routing and append-only invariants`  
**SPEC reference:** SPEC §5 (append-only assertions)

### Description

`conformance/test_append_only.py::test_value_field_is_immutable` demonstrates that new values create new assertion records rather than overwriting existing ones — but it never directly asserts that attempting to mutate `assertion.value` in-place raises an error.

### Fix

Added `test_assertion_value_mutation_raises` and `test_assertion_status_cannot_be_mutated_directly` to `conformance/test_append_only_properties.py`, directly asserting `pytest.raises(ValidationError)` on in-place field assignment.

---

## KI-004 — Symbolic query filter covers only `retracted` status in append-only vector ✓ RESOLVED (M2)

**Severity:** Test gap — partial conformance coverage  
**Milestone target:** M2 — resolved in `feat(govern): implement proposal workflow, conflict routing, and contradiction handling`  
**SPEC reference:** SPEC §5, §10

### Description

`conformance/test_append_only.py` tests that retracted assertions are excluded from default queries and retained in history. It did not cover the `superseded` and `flagged` statuses, which also must be excluded from default retrieval (SPEC §5) and queryable for audit.

### Fix

`conformance/test_conflict.py` now covers both: `status="flagged"` queryability (contradiction vectors) and `status="superseded"` queryability (supersession vectors).

---

## KI-005 — `assert_literal` / `assert_ref` bypass the proposal/policy path ✓ RESOLVED

**Severity:** Architecture gap — not SPEC-compliant for untrusted principals  
**Milestone target:** M2 added `propose()` as a governed path; `assert_literal`/`assert_ref`
themselves remained an ungoverned direct-write bypass until fixed on `fix/govern-write-pipeline`.  
**SPEC reference:** SPEC §9.3 ("A principal with `write` capability MAY bypass proposals; such
writes still pass through the conflict pipeline (§10) and still record full provenance.")

### Description

M2's `feat(govern): implement proposal workflow, conflict routing, and contradiction handling`
added `Ontology.propose(...)` as a governed path, but `Ontology.assert_literal()` and
`Ontology.assert_ref()` continued to call `backend.put_assertion()` directly with no
capability check and no conflict routing — any principal, including `ai`-kind ones, could
write an assertion that silently coexisted with a conflicting `static` fact with no
contradiction raised. This entry was previously marked resolved based on the *intended* fix
description below, but the code was never changed to match — the bypass was live until now.

### Fix

`assert_literal`/`assert_ref` now require `write` (or `admin`) capability, explicitly reject
`ai`-kind principals regardless of a misconfigured capability, and route through
`_apply_with_conflict_routing` (the same SPEC §10 routing `propose()`'s auto-accept path uses)
instead of writing directly. Per SPEC §9.3 they intentionally do **not** create a `Proposal`
record or evaluate `ThresholdPolicy` — that's the defined difference between the write-capable
bypass and the governed `propose()` path, not a gap to close.

---

## KI-006 — `govern/policy.py` `Reject` decision is never produced ✓ RESOLVED (M2)

**Severity:** Informational — dead code  
**Milestone target:** M2 — resolved in `feat(govern): wire Reject decision for read-only principals`  
**SPEC reference:** SPEC §9.2

### Description

`ThresholdPolicy.evaluate()` can return `AutoAccept` or `RequireReview` but never `Reject`. The `Reject` decision class exists but has no production path. Line 45 in `govern/policy.py` (`return Reject(...)`) is unreachable from `ThresholdPolicy`, leaving it uncovered.

### Fix

`ThresholdPolicy` now rejects proposals from principals with `read` capability immediately (no review queue). `Ontology.propose()` and `Ontology.retract()` handle the `Reject` branch: persist the proposal as `state="rejected"` with `decided_at` set, return without writing any assertions. 8 conformance vectors added in `conformance/test_proposal_workflow.py::TestRejectDecision`.

---

## KI-007 — No API to accept or reject a `require_review` proposal ✓ RESOLVED (M2)

**Severity:** Architecture gap — pending proposals are unresolvable  
**Milestone target:** M2 — resolved in `feat(govern): implement accept_proposal() and reject_proposal() review-acceptance API`  
**SPEC reference:** SPEC §9 (proposal state machine)

### Description

`Ontology.propose()` and `Ontology.retract()` can produce proposals with state `require_review` (e.g. for AI principals), and those proposals are persisted. However there is no `accept_proposal()`, `reject_proposal()`, or review-approval method anywhere in `src/ontolith/`. A reviewer cannot advance the proposal through the state machine (`require_review → under_review → accepted/rejected`), so pending proposals accumulate with no resolution path.

`update_proposal_state()` exists on the `StorageBackend` port and `SQLiteBackend` but is currently dead code — it was added in anticipation of this workflow.

### Fix

Add `Ontology.accept_proposal(proposal_id, reviewer)` and `Ontology.reject_proposal(proposal_id, reviewer, reason)` that advance the proposal state and — on acceptance — replay the stored payload operations through `_apply_with_conflict_routing`. Wire `update_proposal_state` into that path.

---

## KI-008 — Multi-target supersession links only the first superseded assertion ✓ RESOLVED (M3)

**Severity:** Informational — intentional v1 limitation, now closed  
**Milestone target:** M3 — resolved via ADR-0023  
**SPEC reference:** SPEC §10.2 (supersession chain), §12.2 (normative SQLite schema)

### Description

When multiple overlapping `time_varying` assertions are superseded at once (e.g. two concurrent employers both active when a new one arrives), `_apply_with_conflict_routing` sets `supersedes = result.targets[0]` on the incoming assertion — linking it to only one predecessor. The other superseded assertions have no successor pointer.

`Assertion.supersedes` is a single `str | None` field by model design (one-to-one chain, not a list). This means time-travel via the `supersedes` chain cannot recover all concurrent predecessors — only the first.

In practice this is rare because `time_varying` with truly concurrent open windows indicates a data-entry race, but it is a known fidelity gap in the provenance trail.

### Fix

`Assertion.supersedes` stays a scalar `str | None`, unchanged — SPEC §12.2's normative schema models it as scalar `TEXT`, and widening it would break the public `Assertion` model (ADR-0019) to fix a rare-path fidelity gap. Instead, the full predecessor set is recovered through the existing `assertion_event` audit log, which already writes one row per superseded predecessor at exactly the right point in `_apply_with_conflict_routing`: `AssertionEvent` gained a `successor_id: str | None` field (populated only on `"superseded"` events), and `StorageBackend` gained `get_assertion_events_by_successor(successor_id) -> list[AssertionEvent]`. The full predecessor set for any successor is `{e.assertion_id for e in kb.backend.get_assertion_events_by_successor(successor_id)}`. Surfaced via `ProvenanceOut.superseded_ids` (REST) and the `superseded_ids` key on the MCP `ontolith.provenance` tool. See ADR-0023 for the full design, including why a new `supersession_link` table was rejected in favor of extending the audit log.

---

## KI-009 — Contradiction resolution API was missing ✓ RESOLVED (M2)

**Severity:** Major — SPEC §19 SHOULD-vector gap  
**Milestone target:** M2 — resolved in `feat(govern): implement resolve_contradiction() for contradiction resolution`  
**SPEC reference:** SPEC §10.3, §19 ("contradiction creation + resolution")

### Description

`Contradiction` carries `resolved_by`/`resolved_at` fields and `.claude/rules/conflict.md` documents a `resolve(c, winner_id, by)` function, but no code path ever called it. Contradictions could be created (flagged, routed to review) but never resolved — `state` stayed `"open"` forever and losing assertions stayed `"flagged"` indefinitely.

### Fix

Added `Ontology.resolve_contradiction(contradiction_id, winner_assertion_id, resolver)`: validates resolver has `review`/`admin` capability, contradiction is open, and the winner is an actual member; then retracts all other members, reactivates the winner, and marks the contradiction `resolved` — all inside one transaction. Added `StorageBackend.get_contradiction()` and `resolve_contradiction()` to the port and SQLite adapter. 12 conformance vectors in `conformance/test_contradiction_resolution.py`. Not exposed via MCP — resolution is a reviewer-only action, out of scope for the ADR-0008 propose-only surface.

---

## KI-010 — LinkML-aligned YAML schema dialect and reference plugins ✓ RESOLVED (M3)

**Severity:** Informational — scoped M2 deliverable, now complete  
**Milestone target:** M3 — LinkML dialect resolved via ADR-0013; reference plugins resolved via the reference plugin implementations below  
**SPEC reference:** Implementation Plan §2 (M2 exit list), ADR-0007 (interop priority), SPEC §13 (plugin protocols)

### Description

The Implementation Plan's M2 deliverable list includes "LinkML-aligned YAML, first 3 reference plugins" alongside review workflow, bitemporal time-travel, conflict handling, MCP server, and trust/delegation — all of which are now complete. The YAML dialect and plugin work were explicitly deferred: they require their own design pass (schema dialect coverage, bidirectional codegen, and the shape of the `Importer`/`Exporter`/`Reasoner`/`Validator` protocol implementations) rather than being folded into a conformance-gap sweep.

### Fix

**LinkML YAML dialect: resolved (ADR-0013).**

**Reference plugins: resolved.** Three first-party plugins, one per storage posture, implement the real `Importer`/`Exporter`/`Validator` protocols (`plugins/ports.py`) against the ADR-0015 capability-scoped views, and are registered as genuine `importlib.metadata` entry points in `pyproject.toml` (not test doubles) — proving plugin discovery, manifest negotiation, and view-scoped reads/writes end to end with working code:

- `CsvImporter` (`plugins/reference/csv_importer.py`, entry point `csv-importer`) — `importer` kind, `storage="write"`. Creates entities/assertions from CSV rows through `WriteView`, deduplicating entities by `natural_key` within a single import run.
- `JsonExporter` (`plugins/reference/json_exporter.py`, entry point `json-exporter`) — `exporter` kind, hard-capped `read`. Serializes active assertions to JSON through `ReadOnlyView`.
- `RequiredFieldsValidator` (`plugins/reference/required_fields_validator.py`, entry point `required-fields-validator`) — `validator` kind, hard-capped `read`. Demonstrates the kind of domain rule a `Validator` plugin exists for: a configurable required-predicate-per-concept business rule the core schema doesn't itself enforce.

`Reasoner`/`Connector` reference implementations were not built — the Implementation Plan's exit criterion is "first 3 reference plugins," met by the three above, one per storage posture (write/read/read+cross-check).

---

## KI-011 — Confidence-based auto-accept for AI proposals: deliberately not implemented

**Severity:** Informational — design decision, not a defect  
**Milestone target:** N/A — will not be implemented in v1  
**SPEC reference:** SPEC §19 (informative "SHOULD include vectors" list), Appendix A (informative example)

### Description

SPEC §19's SHOULD-vector list mentions "policy auto-accept vs. review by confidence threshold," and the informative Appendix A worked example shows an AI agent's proposal with confidence 0.82 going to `under_review` against an implied 0.9 auto-accept threshold — suggesting confidence ≥ 0.9 would auto-accept for an AI principal.

This was considered and explicitly rejected for Ontolith's `ThresholdPolicy`. Both SPEC §19's vector list and Appendix A are non-normative (informative/SHOULD, not MUST). Letting an AI principal's self-reported confidence score grant `AutoAccept` would mean an AI proposal can be committed with zero human in the loop — which conflicts with the project's AI-safety posture (SPEC §8.3: agents MUST default to `propose` and MUST NOT be granted `write` implicitly; SPEC §8.1: mandatory accountable owner; ADR-0008: no direct MCP write tool). A model's own confidence number is not a trust signal strong enough to bypass human review.

### Resolution

`ThresholdPolicy` always returns `RequireReview` for `kind == "ai"` principals, regardless of confidence or trust level. Confidence is still captured and visible to reviewers — it's stored on both `Proposal.payload["operations"][0]["confidence"]` and the resulting `Assertion.confidence` field — but it is a provenance/triage signal only, never a policy input. No code changes were needed for this decision since that storage already existed; this entry documents the choice so it isn't re-litigated as a "missing feature" in a future gap audit.

**Update (2026-09-04, KI-069/ADR-0045):** this Resolution's guarantee is specific to `ThresholdPolicy` (the default), not a codebase-wide invariant. KI-069 added `ConfidenceThreshold`, a `PolicyStrategy` that deliberately does *not* special-case AI authorship (matching `SourceQuorum`'s own precedent, ADR-0025 §5) — a deployment configured with `Ontology(policy=ConfidenceThreshold(0.9))` reproduces exactly the confidence-0.9-auto-accepts-for-an-AI-proposal scenario this entry rejected for the shipped default. This is not a regression of this KI's decision (`ThresholdPolicy` is unchanged, and remains the default), but it does mean the AI-safety guarantee described above is a property of which `PolicyStrategy` is installed, not something SPEC or Ontolith enforces structurally across every possible one — the same caveat ADR-0040 already documents for `Composite`/`SourceQuorum` not making the AI-review rule "structural." See ADR-0045's own Consequences section for the trade-off spelled out directly.

---

## KI-012 — Property tests (Hypothesis) only covered bitemporal reconstruction ✓ RESOLVED (M2)

**Severity:** Test-strategy gap — conflict routing and append-only invariants are correctness-critical and warrant property coverage, not just example-based vectors  
**Milestone target:** M2 — resolved in `test(conformance): add Hypothesis property tests for conflict routing and append-only invariants`  
**SPEC reference:** SPEC §19

### Description

Only bitemporal reconstruction (`conformance/test_bitemporal.py`) had a Hypothesis property test. Conflict routing (SPEC §10) and the append-only invariant (SPEC §5) — both correctness-critical, branch-heavy logic — had only example-based conformance vectors, which don't exhaustively probe edge cases the way generated inputs do.

### Fix

Added `conformance/test_conflict_routing_properties.py` — 8 property tests directly exercising the pure `govern/conflict.route()` function: empty-existing always activates, determinism, static routing contradicts iff any value differs (with exact member-set verification), time-varying supersedes iff window overlaps AND value differs, `_windows_overlap` symmetry, half-open boundary behavior, identical-window overlap, and fully-open-window overlap.

Added `conformance/test_append_only_properties.py` — 4 tests: direct frozen-field mutation checks (closing KI-003) plus two property tests driving a real `Ontology` through randomized `propose()`/`retract()` sequences, verifying no assertion's `value` ever changes after creation and the total record count never decreases.

---

## KI-013 — MCP provenance and flag_contradiction couldn't resolve non-active assertions ✓ RESOLVED (M2)

**Severity:** Correctness bug — audit trail unreachable for its primary use case  
**Milestone target:** M2 — resolved in `fix(interfaces): fix status filter bug in MCP provenance and flag_contradiction`  
**SPEC reference:** ADR-0008 (MCP surface)

### Description

`ontolith.provenance` and `ontolith.flag_contradiction` both called `kb.backend.assertions()` with no `status` argument. `StorageBackend.assertions()` defaults `status` to `"active"`, so both tools silently returned "not found" for any retracted, superseded, or flagged assertion. This defeated the purpose of `ontolith.provenance` (audit trail lookups are exactly the case where the assertion is often no longer active) and made `ontolith.flag_contradiction` unable to find an already-flagged member when extending an open contradiction with a third conflicting assertion.

Found while closing an MCP test-coverage gap (`interfaces/mcp.py` was at 78%) — writing a coverage test for the "extend existing contradiction" branch immediately surfaced the bug.

### Fix

Both call sites now use `assertions(status=None)` to search across all statuses. 5 new tests cover the fix: provenance on a retracted assertion, flag_contradiction assertion-not-found (both sides), and extending an existing contradiction with a third assertion.

---

## KI-014 — No plugin capability isolation (deny-by-default network/fs/write, manifest enforcement) — PARTIALLY RESOLVED (M3)

**Severity:** Architecture gap — security-relevant; originally filed as "not yet exploitable since no plugin loading mechanism exists to secure," which is now stale (a working registry and four reference plugins ship today — see the 2026-09-07 update below)
**Milestone target:** M3 — storage-capability isolation resolved via ADR-0015; M4 — process isolation resolved via ADR-0051 (network/filesystem enforcement now real on Linux; still open on macOS/Windows and with `isolate=False`, see the 2026-09-19 update below)
**SPEC reference:** SPEC §13 (plugin protocols and discovery), §17 (security model, plugin sandboxing); Implementation Plan `plugins/` module (registry, protocols, lifecycle, sandbox)

### Description

`src/ontolith/plugins/` contained only protocol stubs (`ports.py`, `Embedder`/`PolicyStrategy`) — no registry, no manifest schema, no entry-point discovery exercised end-to-end, and critically, no capability sandbox. SPEC §13.1 requires plugins to declare a `name`/`version`/`capabilities` manifest and SPEC §13.2 requires reasoner-derived assertions to enter through the proposal path (never bypass governance), but none of this was enforced anywhere because nothing loaded a plugin.

Surfaced during the 2026-07-08 post-remediation security re-audit: a hypothetical in-process plugin, once loading exists, would run fully trusted and could call `backend.put_assertion` directly (bypassing governance) or `Ontology.issue_token` (see the now-admin-gated fix, MED2 in the same remediation) to mint itself a high-capability MCP credential.

### Fix

**Storage-capability isolation: resolved (ADR-0015).** A plugin is registered as a `service`-kind `Principal` with a capped `default_capability`; the sandbox is a capability-scoped facade over `Ontology` (`ReadOnlyView`/`WriteView`, `src/ontolith/plugins/views.py`) rather than a capability-restricted `StorageBackend` wrapper — the original fix sketch here was superseded by that approach (see ADR-0015's Alternatives Considered for why: a `StorageBackend` wrapper would reimplement policy/conflict-routing/model-requirement checks a second time, and wouldn't naturally block `issue_token`/`apply_schema` since those aren't `StorageBackend` methods anyway). Admin-only methods and the direct-write bypass are structurally absent from the views, not runtime-checked. `PluginRegistry.register()` (`src/ontolith/plugins/registry.py`) requires `admin` capability and discovers plugins via `importlib.metadata.entry_points(group="ontolith.plugins")`.

**Still open:** network/filesystem enforcement — the manifest schema declares these fields, but nothing enforces them yet; closing this requires process/wasm isolation, explicitly phased to later work in the Implementation Plan. ADR-0015 also states explicitly that the storage-isolation fix is a governance-correctness boundary, not a security sandbox against a plugin author who deliberately writes code to defeat the convention (Python has no true encapsulation) — that gap closes only with process/wasm isolation too.

**Update (2026-08-30, M3 milestone-boundary security audit):** re-confirmed both "still open" items above are exactly as stated — no drift, nothing regressed — but escalated to HIGH severity given the context changed materially: this design now ships with four real reference plugins and a documented third-party entry-point contract, not the "no plugin loader exists yet" state this KI was originally filed against. Also newly confirmed: `ReadOnlyView.query()`/`.as_of()` return objects (`QueryBuilder`, `AsOfView`) that themselves hold a raw `StorageBackend` reference — `view.query(...)._backend.put_assertion(...)` reaches the unrestricted backend, one attribute hop past the intended API. Not a new gap; a specific instance of "Python has no true encapsulation," already named above.

**Partial mitigation added:** `PluginRegistry.register()` now logs a `WARNING`, on successful registration, when a plugin's manifest declares `network=True` or `filesystem=True`, naming the plugin and stating explicitly that these are not enforced — closes the gap between what the manifest API visually implies (a control) and what's actually true, at the moment (registration) that's an operator's only real lever (there's no separate "grant" step for network/filesystem the way there is for storage capability; the plugin author declares intent, the operator decides whether to register at all). This is a visibility fix, not an enforcement fix — SPEC §17's MUST itself remains unmet for network/filesystem, and real process/subprocess/wasm isolation remains the only actual fix — a materially larger, separate infrastructure project, tracked via the Implementation Plan's existing phasing, not newly deferred here.

Separately, and unrelated to the warning above: `QueryBuilder`'s reachable-backend gap (previous paragraph) was also re-examined and left unchanged. "Materializing" `query()`'s results eagerly to stop it carrying a backend reference was considered and rejected: it would break the fluent builder API (`.where(...).limit(n).all()`) that's the entire point of `QueryBuilder`, for a property Python's object model can't provide regardless. See ADR-0015's 2026-08-30 update for the full record.

**Update (2026-09-07, pre-M4 deep + security audit):** independently re-confirmed, no drift — network/filesystem enforcement is still unenforced (manifest declares intent, registration only warns via `_warn_if_unenforced_capabilities_requested`). The severity line above is updated to reflect that four reference plugins (`plugins/reference/`: csv_importer, json_exporter, rdf_exporter, required_fields_validator) now ship against this contract, which the original filing's "not yet exploitable" framing didn't anticipate. Flagging explicitly for M4 planning: this should be scoped as real M4 work (process/wasm isolation) rather than carried forward again as backlog, since "production" milestone exit criteria and "plugin sandbox is advisory only" are in direct tension.

**Update (2026-09-19, M4 workstream 3, ADR-0051): process isolation shipped — both named gaps closed structurally, network/filesystem closed for real on Linux.** `PluginRegistry.register()` now defaults to `isolate=True`: a plugin's protocol entrypoint (`import_`/`export`/`derive`/`validate`/`sync`) runs in a freshly spawned child process (`plugins/sandbox/`), with its `kb` view and any other live argument proxied back over one IPC pipe. This closes the "Python has no true encapsulation" gap named in the 2026-08-30 update *structurally, on every platform*: `view.query(...)._backend.put_assertion(...)` no longer reaches anything — there is no `_backend` attribute to reach past, since only picklable messages cross the process boundary at all, not a live object graph. On Linux, with a working `pyseccomp`/libseccomp install, `capabilities.network`/`.filesystem` are now genuinely enforced at the OS syscall level (seccomp, `ERRNO(EPERM)`), closing SPEC §17's "MUST deny undeclared access" for that platform. **Still open:** macOS and Windows get no OS-level enforcement — `sandbox-exec`/job objects are real but different mechanisms this pass doesn't build (unverifiable in this project's own development environment; see ADR-0051's Rationale for why only the Linux mechanism shipped) — a plugin's declared `network=False`/`filesystem=False` on those platforms, or with `isolate=False` anywhere, remains advisory only, same as before this update, now qualified to "on this platform/registration" rather than "at all." `QueryBuilder`'s reachable-backend gap from the 2026-08-30 update is moot for an isolated plugin (`.query()`/`.as_of()` raise a clear `PluginError` inside a sandboxed call rather than being reachable at all — filed as its own follow-up, **KI-101**, since no shipped reference plugin needs them yet) and unchanged for `isolate=False`. This KI stays "partially resolved," not fully resolved — the remaining gap is now precisely "non-Linux OS-level enforcement," not "any process isolation at all."

**Update (2026-09-20): the "no live object graph reaches the plugin" claim above was false when
written — two review rounds found the process boundary itself was bypassable, both fixed and
verified by direct reproduction before this note was written, see ADR-0051's own Update section for
the full record.** The dispatch loop unpickled bytes the child process sent with plain
`pickle.loads()` (a plugin's own return value, raised exception, or any proxied-call argument could
carry a hostile `__reduce__` — reproduced running arbitrary code in the parent) and invoked any
method name the child asked for on the real, unproxied view object with no allow-list (reproduced:
`__setattr__("_principal_id", "admin@example.com")` forged the acting principal on every subsequent
write through that view, from a read-only registration, using plain strings — no pickle trickery
needed). Both are closed now (a restricted unpickler with an explicit exception-class allowlist, and
an explicit method-name allow-list before any dispatch), and independently re-verified by direct
reproduction of both attacks against the fixed code. The syscall deny-lists also gained several
entries a security review found missing (`io_uring_*`, `ptrace`/`process_vm_*`, several filesystem
syscalls, non-native-architecture registration) — see ADR-0051 for the full list. This KI's status
is unchanged by this update (still "partially resolved" for the same non-Linux reason stated above)
— the update is about the *fix's own correctness*, not a new scope change.

---

## KI-015 — `propose()`/`propose_ref()`/`retract()` capability floor — RESOLVED (design clarification, no code change)

**Severity:** Informational — design clarification, not a defect
**Milestone target:** N/A — resolved by clarifying intended behavior, no implementation needed
**SPEC reference:** SPEC §8.3 (capability lattice: `read < propose < write < review < admin`), SPEC §9.1 (proposal state machine — `reject` as a terminal state reached via policy evaluation)

### Description

`Ontology.propose()`/`propose_ref()`/`retract()` resolve the author principal and check AI-model requirements and delegation authorization, but never check `principal.default_capability` directly before proceeding to `ThresholdPolicy.evaluate()` — unlike `create_entity` and `flag_contradiction`, both of which explicitly raise `CapabilityError` for `read`-capability authors before doing anything.

Surfaced during the code review for ADR-0015 (plugin capability isolation): `create_entity` gained a `>= propose` capability gate as part of that work, which made the asymmetry concrete — a `read`-only plugin principal is blocked from `create_entity` but reaches `ThresholdPolicy.evaluate()` via `propose()`/`propose_ref()`/`retract()`. This looked like a missing gate.

### Resolution

Not a gap — the two code paths are different by necessity, not inconsistent in effect. `create_entity`/`flag_contradiction` have no proposal/policy machinery at all, so a hard pre-check is their only mechanism for blocking `read`-capability authors. `propose`/`propose_ref`/`retract` already have that machinery, and route every author — including `read` capability — through `ThresholdPolicy.evaluate()`, which returns a `Reject` decision for `read`-capability principals (per KI-006's resolution): a persisted `Proposal` with `state="rejected"` and `decided_at` set, but no assertion ever written. This is SPEC §9.1's modeled behavior (`reject` is a first-class terminal state reached via policy, not a pre-check short-circuit), is covered by 8 existing conformance tests (`conformance/test_proposal_workflow.py::TestRejectDecision`), and is the contract documented in `interfaces/mcp.py`'s `propose_tool` docstring. Both mechanisms end in the same place — no assertion written, no elevated access granted — via the only path available to each. Adding a hard `CapabilityError` pre-check to `propose`/`propose_ref`/`retract` would not close a gap; it would regress all 8 conformance tests and contradict the SPEC-modeled state machine and the documented MCP contract.

**Update (2026-07-27, KI-017/ADR-0025):** this resolution's "routed through `ThresholdPolicy.evaluate()`" framing was accurate when written, but ADR-0018 (not yet merged at the time) later made `PolicyStrategy` genuinely swappable — the capability floor above is enforced by `ThresholdPolicy`'s own logic, not by anything in `propose`/`propose_ref`/`retract` itself, so it is only as strong as whichever strategy is actually installed. `SourceQuorum` (ADR-0025) was found in review to omit this floor entirely — a `read`-capability principal's sufficiently-corroborated proposal would otherwise auto-accept — and now replicates `ThresholdPolicy`'s read-rejection explicitly. Any future `PolicyStrategy` (`ConfidenceThreshold`, `TrustLevel`, etc.) needs to make the same deliberate choice; it is not inherited for free.

---

## KI-016 — No second StorageBackend implementation for conformance kit ✓ RESOLVED (M3)

**Severity:** Architecture gap
**Milestone target:** M3
**SPEC reference:** SPEC §12 (Storage layer), SPEC §19 (Conformance kit)

### Description

Only `SQLiteBackend` implemented `StorageBackend`; the conformance kit had never been exercised against a second backend, leaving the port abstraction (`core`/`schema`/`govern`/`query` never importing a concrete adapter) unproven in practice and the M3 exit criterion ("Backend conformance kit passes on a 2nd backend") unmet.

### Fix

Implemented `DuckDBBackend` (`src/ontolith/store/duckdb/`), recorded in ADR-0016. Added `conformance/conftest.py` with a `make_kb` fixture parametrizing every backend-touching conformance vector over both SQLite and DuckDB (357 tests total, up from ~180 single-backend vectors); `tests/unit/test_duckdb_backend.py` gives the new backend its own dedicated unit coverage (53 tests, exceeding `SQLiteBackend`'s own coverage percentage). Three real DuckDB-specific divergences were discovered and resolved along the way, each documented inline in `backend.py`: DuckDB's `REAL` type is 4-byte single-precision (unlike SQLite's always-8-byte `REAL`), requiring `DOUBLE` for the `confidence` column; `at` is a reserved word requiring quoting; and DuckDB (confirmed on 1.5.4) raises a false-positive constraint violation on `UPDATE ... RETURNING` against a row that's the target of an incoming foreign key from another table — worked around via a `SELECT` existence check plus a plain `UPDATE`, and by dropping the `assertion_event.assertion_id`/`proposal_event.proposal_id` FK declarations (referential integrity there is enforced at the application layer instead, matching `Ontology`'s own write discipline). DuckPGQ/graph-native traversal remains out of scope — no traversal method exists on `StorageBackend` yet for it to serve (tracked separately, see Implementation Plan §14 item 1).

---

## KI-017 — `PolicyStrategy.evaluate()` has no `kb: ReadOnlyView` parameter ✓ RESOLVED (M3)

**Severity:** Architecture gap — blocked one class of policy strategy; now closed
**Milestone target:** M3 — resolved via ADR-0025
**SPEC reference:** SPEC §9.2 (Policy engine contract — `evaluate` signature, purity requirement), §14 (Plugin protocols)

### Description

SPEC §9.2/§14 specify `PolicyStrategy.evaluate(self, proposal, principal, kb: ReadOnlyView) -> Decision`. The actual `PolicyStrategy` Protocol (`src/ontolith/govern/policy.py`) had `evaluate(self, proposal, principal, acting_as=None)` — no `kb` parameter, plus an `acting_as` parameter (needed for SPEC §8.4 delegation) the SPEC signature doesn't name. Without `kb`, a policy strategy could not inspect KB state — `SourceQuorum` (one of SPEC §9.2's SHOULD-have built-in strategies, which needs to count corroborating sources) could not be implemented.

Recorded during a whole-project audit (2026-07-14) alongside ADR-0018, which made `PolicyStrategy` actually injectable (`Ontology(backend, policy=...)`) — closing the "not pluggable at all" half of the gap while deliberately deferring this half.

### Why not fixed then

Adding `kb: ReadOnlyView` wasn't blocked by the SPEC's purity requirement ("no writes, deterministic given inputs" permits reads), but SPEC §9.2 also requires evaluation to be "testable and replayable" — a live `ReadOnlyView` over an open, potentially-concurrently-mutated connection has no fixed state to be reproducible against. Resolving this needed a snapshot/consistency contract that didn't exist yet, and design work against a real `SourceQuorum`-shaped consumer rather than speculatively. See ADR-0018's Rationale and Alternatives Considered for the original analysis.

### Fix

`PolicyStrategy.evaluate()` gained a required `kb: KbView` parameter (inserted between `principal` and `acting_as`, matching SPEC's ordering). `KbView` is a minimal structural Protocol declared locally in `govern/policy.py` (`assertions(subject=, predicate=) -> list[Assertion]`) rather than the SPEC-named `ReadOnlyView` — `ReadOnlyView` wraps a *live* `Ontology` and isn't the pinned type "testable and replayable" needs, and importing either concrete view class into `govern/policy.py` would also be circular. `Ontology.propose`/`propose_ref`/`retract` each pass `self.as_of(now)` (an `AsOfView` pinned at the proposal's own `created_at`) — since nothing is persisted until after the decision, this snapshot can't see the in-flight proposal's own operation *at evaluation time*. Replay is weaker than that: `asserted_at <= t` is inclusive, so re-running `as_of(proposal.created_at)` after anything else commits at that exact same instant *will* see it — replay reproduces the original read only absent a same-instant write afterward (real clocks: negligible; `FixedClock` in tests: not guaranteed). `ThresholdPolicy`'s own concrete `evaluate()` gives `kb` a default (`KbView | None = None`, unused) so none of the ~25 pre-existing pure `ThresholdPolicy` tests needed changes. `SourceQuorum` was implemented as the first KB-inspecting strategy — it also replicates `ThresholdPolicy`'s `read`-capability rejection, since nothing else gates it on the `propose`/`propose_ref`/`retract` paths (KI-015) — with conformance vectors in `conformance/test_source_quorum_policy.py` proving it reads real committed data through both backends. Full design in ADR-0025.

---

## KI-018 — Hybrid retrieval (vector search + `Embedder`) never implemented ✓ RESOLVED (M3)

**Severity:** Architecture gap — SPEC-normative surface entirely missing, not partially built
**Milestone target:** M3
**SPEC reference:** SPEC §11.3 (Hybrid retrieval), §12.2 (`StorageBackend.vector_upsert`/`vector_search`), §14 (`Embedder` Protocol)

### Description

SPEC §11.3 specifies semantic search over entity/assertion embeddings via an `Embedder` port and a vector store, defaulting to `sqlite-vec`; SPEC §12.2's normative `StorageBackend` shape includes `vector_upsert(scope, id, vec)` and `vector_search(scope, vec, k)`; SPEC §14 defines the `Embedder` Protocol itself. None of this existed: `StorageBackend` (`src/ontolith/store/base.py`) had no vector methods on either backend, `Embedder` had no concrete Protocol anywhere in the codebase (`plugins/ports.py` documented its own absence explicitly), and `QueryBuilder` had no `.semantic()` method. `sqlite-vec==0.1.1` was a pinned optional dependency in `pyproject.toml` but was never imported anywhere — it did not back any working code path.

Surfaced during a whole-project docs/known-issues refresh (2026-07-14); this was a pre-existing gap, not a regression — hybrid retrieval was never scheduled before M4 in the Implementation Plan, so its absence through M3 was expected. Recorded so it stopped being an implicit assumption and started being an explicit, trackable gap against the SPEC's normative surface.

### Fix

Implemented across two sequential PRs, per ADR-0020 (amended for the query-layer decisions below):

**Storage layer:** `Embedder` Protocol (`src/ontolith/core/embedder.py`) with `HashingEmbedder` (dependency-free sha256 feature-hashing default) and `LookupEmbedder` (deterministic test double). `StorageBackend.vector_upsert`/`vector_search` added to the port and both adapters — SQLite via `sqlite-vec`'s `vec0` virtual tables (with a rowid-indirection table working around `vec0`'s lack of TEXT-primary-key upsert/delete support), DuckDB via its native `list_distance()` scalar function. `sqlite-vec` moved from optional to a required dependency, matching SPEC §12.1's MUST.

**Query layer:** `QueryBuilder.semantic(text)` composes with `.where()` per SPEC §11.3's example — a vector-search-first, symbolic-intersect algorithm (overfetches from the vector index, then intersects with `.where()` matches preserving vector rank order; see ADR-0020 for why this diverges from the Implementation Plan's literal "symbolic prefilter + vector rerank" phrasing, and the resulting recall-cutoff limitation). `.min_confidence(threshold)`/`.trust_at_least(level)` add independent existential post-filters; `.limit(n)` caps results. `Ontology.reindex(concept=None)` is the explicit (non-auto) entry point that embeds entities' Text-typed assertion content into the vector index — no write path auto-embeds on write. A CLI `ontolith reindex [--concept]` command and an informational hybrid-query benchmark (`tests/benchmarks/test_hybrid_query.py`, p95 well under the 150ms budget at 1k entities) were added alongside.

---

## KI-019 — `as_of(t)` does not resolve the schema version effective at `t` ✓ RESOLVED (M3)

**Severity:** Correctness gap — bitemporal reconstruction was schema-version-blind, now closed
**Milestone target:** M3 — resolved via ADR-0024
**SPEC reference:** SPEC §11.4 ("Schema is resolved to the `schema_version` effective at `t`"), §19 (informative SHOULD-vector: "`as_of` reconstruction across a schema migration")

### Description

Bitemporal reconstruction is documented (and, per SPEC §19, expected to be tested) to resolve the schema that was active at time `t`, not the current schema — otherwise `as_of()` can misreport a property's `temporality`/`cardinality` for a point in time before a schema migration changed them. `StorageBackend.get_schema()` only ever returned the latest schema version, with no way to ask for the version effective at a given `t`.

Surfaced during a whole-project docs/known-issues refresh (2026-07-14).

### Fix

Investigation while resolving this (see ADR-0024's Context) found the KI's original description overstated where the gap actually lived: `Ontology._resolve_temporality`/`_resolve_cardinality`/`_require_known_predicate` are write-time-only helpers (always correctly resolving the current schema, since writes happen "now"), and `AsOfView`/`QueryBuilder` never consulted schema at all — so there was no misreported data from any existing read path, only a missing capability. Also found that `schema_version.applied_at` already existed in both backends' storage, populated deterministically via each backend's injected `Clock` on every `put_schema` call — just never read back.

Closed additively: new `StorageBackend.get_schema_at(namespace, at) -> SchemaIR | None` port method resolves the highest version whose `applied_at <= at`, and new `AsOfView.schema()` calls it with the view's own `as_of` time. `SchemaIR` and `put_schema` are both unchanged — no breaking change. Conformance vectors in `conformance/test_bitemporal.py::TestAsOfSchema` cover the SPEC §19 suggested case (a property's temporality reconstructed differently on either side of a schema migration) on both backends.

---

## KI-020 — No automated `griffe diff` CI gate for public API breaking changes ✓ RESOLVED (M3)

**Severity:** Test gap — breaking changes to the public API surface were caught by convention/review, not CI; now closed
**Milestone target:** M3 — resolved via ADR-0026
**SPEC reference:** Implementation Plan §5 (quality gates table — `griffe diff`, "warn pre-1.0, block post-1.0"), §7.1 (CI pipeline description)

### Description

ADR-0019 defines the public API surface and adds a pinned-`__all__` regression test (`tests/unit/test_public_api_surface.py`) as the M3-level starting point for "public API stability policy begins," but the Implementation Plan's quality-gates table separately names a `griffe diff` CI gate that would catch signature-level breakage (a parameter added/removed/retyped on an already-exported symbol) that the pinned-export-list test cannot. Investigated during ADR-0019: the installed `griffe` (via `griffelib` 2.1.0, pulled in transitively by `mkdocstrings[python]`) has its CLI split into a separate `griffecli` distribution not currently a project dependency — `python -m griffe` fails with `ModuleNotFoundError: griffecli`.

More fundamentally, this gate was one piece of a CI-hardening pass (alongside `bandit`, `gitleaks`, `pip-audit`, SBOM generation) described in Implementation Plan §7.1's `security.yml`/`nightly.yml`/`release.yml` workflows, none of which existed yet (only `ci.yml` did). Standing up `griffe` diffing in isolation, ahead of that surrounding pipeline, would have risked committing to tooling/config choices that should be made together.

### Fix

New `.github/workflows/security.yml` (ADR-0026): `pip-audit`, `bandit`, `gitleaks`, CycloneDX SBOM generation — triggered on PR + weekly, matching Implementation Plan §7.1 exactly. Building it surfaced two real, previously-undetected issues that had to be fixed before the gates could actually be blocking: `pip-audit` found `mcp==1.28.0`/`sqlite-vec==0.1.1` had disclosed CVEs (both bumped to fixed versions), and `bandit` found 7 medium `B608` false-positive findings across two call-site shapes — 5 in the vector-search code (table names/an IN-clause arity string built from a validated closed set, not caller-controlled) and 2 in `entities_where()` (a `flagged_clause` local always one of two hardcoded literals) — each closed with a targeted `# nosec B608` and an explanatory comment rather than a blanket suppression. `griffecli` added as a dev dependency; a new `griffe check` step in `ci.yml`'s `quality` job diffs the public API against the PR's base commit and reports via native GitHub Actions annotations (`-f github`). This step is informational only because of `continue-on-error: true` on the CI step itself — `griffe check` exits `1` on any detected change, including non-breaking ones, not `0` as an earlier draft of this text claimed (a verification bug: piping the command through `head` before checking the exit code captured `head`'s exit code, not `griffe`'s; caught in review). `nightly.yml` (needs a benchmark-trend hosting decision) and `release.yml` (needs PyPI Trusted Publishing registration and a docs site, neither of which exist yet) remain deliberately out of scope — see ADR-0026 for why.

**Update (2026-09-28, v1.0.0 tag):** `continue-on-error: true` was removed from this step, per this
entry's own "warn pre-1.0, block post-1.0" reference and ADR-0026's Decision #3. This entry's "exits
`1` on any detected change, including non-breaking ones" is imprecise: it's specifically
`find_breaking_changes()`'s own breakage set, scoped to whatever a module's own `is_public` considers
public (for a module with its own `__all__`, exactly the names listed there, unless re-exported via
another module's own `__all__` — no name in `src/ontolith` is today) — a pure addition exits 0,
removing a name from `__all__` alone (keeping the import) also exits 0, and a signature/attribute
change on a name absent from its own module's `__all__` exits 0 too (confirmed on
`interfaces/cli.py`'s `db_status`, not listed in `cli.py`'s own `__all__ = ["app"]`) — most everyday
edits don't trip this gate at all, not "almost always" as an earlier draft of this Update claimed.
See ADR-0026's own 2026-09-28 Update and ADR-0019's 2026-09-28 Update for the corrected,
empirically-reproduced account (two successive re-verification attempts each overcorrected in a
different direction — one claiming only genuine breaking changes trip it, the other claiming any
non-underscore module-level function does — both wrong, both fixed there).

---

## KI-021 — MCP read tools accept unauthenticated calls, contradicting SPEC §8.3 ✓ RESOLVED

**Severity:** Architecture gap — SPEC compliance gap in a shipped interface, not yet exploitable beyond information disclosure (read-only)
**Milestone target:** Backlog (resolved alongside the REST interface work, KI-022, so both interfaces converge on one auth posture instead of being fixed twice)
**SPEC reference:** SPEC §8.3 ("`read`/`query`: required for any retrieval"), §17 ("every operation MUST be capability-checked ... against the resolved principal")

### Description

`src/ontolith/interfaces/mcp.py`'s `schema_tool`, `get_tool`, `query_tool`, and `provenance_tool` took no `token` parameter and called straight into `kb.backend`/`kb.query()` with no principal resolution at all — any MCP client could call them with zero credentials. Only `propose_tool` and `flag_contradiction_tool` required a bearer `token` (ADR-0014). SPEC §8.3 states plainly that `read`/`query` "required for any retrieval," and §17 states every operation MUST be capability-checked against a resolved principal — the four read tools satisfied neither.

Surfaced while scoping the REST interface (KI-022): REST's read routes were designed to require auth from the start (matching SPEC §14.3's "auth required" language for REST resources), which would have left MCP and REST diverging on an identical class of operation for no principled reason — REST enforcing something SPEC already required of MCP too, and MCP simply never got it.

In practice this was a lower-severity gap than the CRITICAL/HIGH findings closed in the 2026-07-06/07 security remediation arc (see project memory) — it granted unauthenticated *reads*, not writes, capability escalation, or governance bypass. But it was a genuine, live SPEC violation, not a documentation gap.

### Fix

Added a `token: str` parameter to `schema_tool`/`get_tool`/`query_tool`/`provenance_tool` (`src/ontolith/interfaces/mcp.py`), resolved via the same `AuthProvider` already injected into `create_mcp_server`, returning the same `{"error": ..., "code": "auth_error"}` shape `propose_tool`/`flag_contradiction_tool` already used on failure. Since every capability level is `>= read` in the SPEC §8.3 ordering, this was purely "must resolve to *some* valid principal" — no new capability-tier logic needed, mirroring the read-auth design settled for REST in ADR-0021. Documented as an update to ADR-0014. All 6 MCP tools now require a bearer token, closing the divergence with REST's read routes.

---

## KI-022 — REST interface (SPEC §14.3) ✓ RESOLVED (M3)

**Severity:** Architecture gap — named M3 scope item with zero implementation; all originally-deferred pieces now closed
**Milestone target:** M3 — full SPEC §14.3 parity except `/query` offset pagination (tracked as a separate concern, not blocked on this KI); GraphQL resolved separately (ADR-0037)
**SPEC reference:** SPEC §14.3 (REST + GraphQL), §16 (error model), §17 (security model)

### Description

SPEC §14.3 defines a REST resource set — `/namespaces`, `/entities`, `/assertions`, `/proposals` (create + `/{id}/accept|reject|review`), `/contradictions/{id}/resolve`, `/principals`, `/query`, `/provenance/{assertion_id}` — spanning every capability tier (read, propose, write, review, admin). None of it exists: `src/ontolith/interfaces/` contains only `cli.py` and `mcp.py`. `pyproject.toml` already carries an unused `rest = ["fastapi>=0.110", "uvicorn>=0.27"]` optional-dependency group reserved for exactly this, dating back to M0's repo skeleton.

Scoped (design approved 2026-07-21) as two PRs rather than one, given the full resource set's span across capability tiers is a materially bigger surface than MCP's deliberately narrow read/propose-only tool set (ADR-0008):

- **First PR:** read + propose slice mirroring MCP's proven 6-tool surface — `GET /schema`, `GET /entities/{id}`, `POST /query`, `GET /provenance/{assertion_id}`, `POST /proposals`, `GET /proposals` — reusing ADR-0014's bearer-token `AuthProvider` unchanged, with auth required on every route including reads (see KI-021, since resolved, for why that was the deliberate choice and the MCP inconsistency it surfaced at the time). ADR-0021 records the auth-on-reads decision, the `OntolithError`→HTTP status mapping (SPEC §16), and the endpoint-to-MCP-tool mapping.
- **Deferred to a follow-up PR:** direct write (`/assertions` POST/PUT via `assert_literal`/`assert_ref`), `/proposals/{id}/accept|reject|review`, `/contradictions` (list + resolve) and `flag_contradiction`, `/principals` (create/list, token issue/revoke/list), `/namespaces`.
- **Deferred again, in the follow-up PR itself:** `/principals` list and `/namespaces` — no backing SDK method existed for either (audited, not an oversight); `/principals` list has since been closed (see Fix).
- **Deferred, separate concern:** offset/cursor-based pagination on `/query` — `QueryBuilder` itself only supports `.limit()` today, no `.offset()`; extending it is a `query/`+`store/` change, not an `interfaces/` one, and is out of scope for "expose the existing SDK over HTTP."

GraphQL (SPEC §14.3's other named half) is untouched by this KI and remains fully unscoped.

### Fix

**Read + propose slice: resolved.** `src/ontolith/interfaces/rest.py` (`create_rest_app`, ADR-0021) implements `GET /schema`, `GET /entities/{id}`, `POST /query`, `GET /provenance/{id}`, `POST /proposals`, `GET /proposals` — all requiring an ADR-0014 bearer token, all errors mapped through one `OntolithError -> HTTP status` handler per SPEC §16.

**Write, review, and admin slice: resolved (ADR-0022, same day).** `POST /assertions` (direct write), `POST /proposals/{id}/accept|reject`, `GET /contradictions`, `POST /contradictions/flag`, `POST /contradictions/{id}/resolve`, `POST /principals`, and `/principals/{id}/tokens` (POST issue / GET list / DELETE revoke) — all reusing ADR-0021's auth and error-mapping machinery, eight of the ten routes needing no new capability-check code since the wrapped `Ontology` methods already gate themselves (the ninth, `GET /contradictions`, needs none either, but only because it's a read — see ADR-0022 §2). `POST /principals` is the exception: `Ontology.create_principal` has no built-in gate (matches the CLI's own ungated `principal create`), so the route calls `Ontology.require_admin()` explicitly. Building this slice also surfaced and fixed a real bug: `create_principal(kind="ai", owner=None)` raised a raw pydantic `ValidationError` instead of the documented `ontolith.core.errors.ValidationError`, which REST's error mapping couldn't catch — fixed in `Ontology.create_principal` itself (benefits every caller, not just REST). Review before merge found and closed four further issues (untyped `CreatePrincipalIn` fields letting other invalid inputs hit the same unmapped-pydantic-error class, a credential-id ordering ambiguity, a missing credential-ownership check on token revocation, and a capability-denial test-coverage gap) — see ADR-0022's Update section for the full list; one residual gap from that pass is tracked separately as KI-024.

**`GET /principals` (list): resolved (2026-07-26, ADR-0022 update).** New `StorageBackend.list_principals()` port method (both backends), `Ontology.list_principals(author)` (gated via the same `require_admin` used by `issue_token`/`revoke_token`/`list_tokens`), `GET /principals` (REST, admin-only), and `ontolith principal list` (CLI). No pagination, matching `list_tokens`'s existing precedent — see ADR-0022's Update section for why.

**`/proposals/{id}/review` (request_changes): resolved (2026-07-28, ADR-0022 update).** New `Ontology.request_changes(proposal_id, reviewer, reason="")` — SPEC §9.1's third `under_review` outcome (`changes_requested`), alongside the two `accept_proposal`/`reject_proposal` already implement. Reviewer-eligibility/proposal-state checks (previously duplicated between `accept_proposal`/`reject_proposal`) factored into a shared `Ontology._require_reviewer` helper. `ProposalEvent.type` widened to admit `"request_changes"`; the redundant DB `CHECK` constraint on both backends' `proposal_event.type` column was removed entirely rather than widened again (SPEC's own DDL has none, and widening it silently breaks the feature on any pre-existing database file — see ADR-0022's Update section for the full reasoning, including why this specific action keeps the same strict no-AI/no-self-review gate as accept rather than a lighter one). `POST /proposals/{proposal_id}/review` (REST) mirrors `/reject`'s shape exactly. The `require_review`/`under_review` state-naming conflation and resubmission (`changes_requested` → `submitted`) remain deliberately unaddressed.

**`GET /namespaces` (namespace registry): resolved (2026-07-28, ADR-0022 update).** New `Namespace` model (`ontolith.core.namespace`), `StorageBackend.list_namespaces()` port method (both backends, modeled on SPEC §12.2's own normative `namespace` table — resolving ADR-0022's own open question of table-vs-`DISTINCT` in the table's favor), `Ontology.list_namespaces()` (ungated, like `proposals()`/`contradictions()`), `GET /namespaces` (REST), and `ontolith namespace list` (CLI). Both backends idempotently register `DEFAULT_NAMESPACE` (`"default"`) at schema-creation time, and `put_schema()` idempotently registers `schema.namespace` too — otherwise a namespace with only a schema applied, no entities, would be invisible to the registry, the exact blind spot the table-over-`DISTINCT` decision was meant to close. No explicit `put_namespace`/create-namespace API was added; this project remains single-namespace throughout (ADR-0015). See ADR-0022's Update section for the full list of what's deliberately still out of scope (namespace-creation API, `Ontology.connect(namespace=...)`, per-namespace `principal_trust`/plugin isolation/read scoping, namespace-existence validation on other routes).

**GraphQL (SPEC §14.3's other named half): resolved separately (2026-08-27, ADR-0037).** `src/ontolith/interfaces/graphql.py` (`create_graphql_app`) — deliberately scoped to SPEC's literal wording (query/propose/review only, no direct-write or principal-admin mutations), not REST's fuller surface. See ADR-0037 for the full design.

**Still open, tracked as a separate concern (not blocked on "no backing method"):** `/query` offset pagination — `QueryBuilder` only supports `.limit()`, no `.offset()`; extending it is a `query/`+`store/` change, out of scope for "expose the existing SDK over HTTP," and applies equally to both REST and GraphQL once addressed.

---

## KI-023 — SQLite backend's single connection is not safe under concurrent writes from an ASGI server ✓ RESOLVED

**Severity:** Architecture gap — data-integrity risk under real concurrent traffic, not yet triggered by any test (single-threaded today)
**Milestone target:** Backlog (resolved before REST, KI-022, was exposed to concurrent traffic)
**SPEC reference:** SPEC §12.1 (SQLite default backend, WAL mode), SPEC §9's "one transaction per proposal acceptance" invariant (ADR-0010)

### Description

`SQLiteBackend.__init__` (`src/ontolith/store/sqlite/backend.py`) opened its connection with `check_same_thread=False`, added when the REST interface (KI-022) was built: an ASGI server dispatches sync route handlers onto a worker threadpool, a different OS thread than the one that constructed the backend, which stock `sqlite3` blocks regardless of whether the cross-thread access is ever actually concurrent.

That flag only lifted the same-thread check — it did not serialize access. The connection's transaction state (`self._in_transaction`, `transaction()`'s `BEGIN`/`COMMIT` in `backend.py`) was shared, mutable, unguarded state. Two genuinely concurrent requests that both wrote (e.g. two overlapping `POST /proposals` that auto-accept) could interleave on the same connection: a second `BEGIN` while the first transaction was still open raised a raw `sqlite3.OperationalError` — `begin()` had no try/except around it, so this was not an `OntolithError` and was not caught by the REST error mapping (SPEC §16); it surfaced as FastAPI's generic, unmapped 500 — or worse, non-transactional autocommit statements from one request could interleave with another's open transaction, violating the "one transaction per proposal acceptance" invariant (SPEC §9, ADR-0010) without necessarily raising anything.

Found during `ontolith-reviewer`'s pass on the REST interface's first PR (KI-022) — flagged MEDIUM there ("would elevate to HIGH if this REST surface is intended to serve concurrent traffic").

### Fix

Implemented option (a) from the original fix note — the correct general fix: `SQLiteBackend` now holds a `threading.RLock` (`self._lock`). `begin()` acquires it, held for the full span of an explicit transaction; every other public method is wrapped with a `@_synchronized` decorator that acquires the same lock for its own call. RLock's reentrancy lets a method called from inside an already-locked `transaction()` block (the common case — proposal acceptance) re-acquire on the same thread without blocking, while a different thread calling any method blocks until the lock is free.

`commit()`/`rollback()` release the lock asymmetrically, not both unconditionally: an `ontolith-reviewer` pass on this fix caught that unconditional release in both would double-release the lock when `commit()` fails inside a `transaction()` block (its `except` calls `rollback()` next, which would then release an already-released lock) — masking the real `StorageError` behind a `RuntimeError` and leaving `_in_transaction` stuck `True`. `commit()` now releases only on success; `rollback()` keeps the unconditional release, since it is always the terminal step regardless of outcome. Full details, the tradeoff analysis (serialization vs. real write concurrency, which this single-connection design never actually had), and the asymmetric-release rationale are recorded as an update to ADR-0010.

Regression coverage: `tests/unit/test_sqlite_backend.py::TestConcurrency` uses a `ThreadPoolExecutor` + `threading.Barrier` to force genuine concurrent contention on `begin()`/standalone writes — confirmed to reproduce the exact `sqlite3.OperationalError` described above against the pre-fix code, and to pass cleanly against the fix. A dedicated `test_commit_failure_inside_transaction_raises_storage_error_and_frees_lock` test forces a `commit()` failure inside a `transaction()` block and asserts a clean `StorageError` (not `RuntimeError`), a freed lock, and `_in_transaction is False` — confirmed to fail with exactly the predicted `RuntimeError` against the intermediate unconditional-release version found during review.

---

## KI-024 — `POST /principals/{id}/tokens` can return a mismatched `credential_id` under concurrent issuance ✓ RESOLVED

**Severity:** Architecture gap — narrow race, requires two admins issuing tokens for the same principal within the same request gap; not yet observed, no test reproduces it (the deterministic same-timestamp case it's adjacent to is fixed, see below)
**Milestone target:** Resolved via an update to ADR-0014
**SPEC reference:** ADR-0014 (credential lifecycle — each issued credential must be individually attributable and revocable)

### Description

`interfaces/rest.py`'s `issue_token_route` calls `Ontology.issue_token(principal_id, author)` (returns only the raw token — by design, it's never persisted) and then separately calls `Ontology.list_tokens(principal_id, author)[0].id` to recover the new credential's id, relying on `StorageBackend.get_credentials_for_principal`'s "most recently issued first" ordering. These are two independent, non-transactional calls.

Found during review of ADR-0022's write/admin REST slice: if a second admin issues another token for the *same* `principal_id` in the gap between this route's `issue_token()` and `list_tokens()` calls, `list_tokens()[0]` can return that second, unrelated credential instead of the one whose raw token was just handed back — pairing the correct raw token with the wrong `credential_id`. A caller who stores `(token, credential_id)` together and later calls `DELETE /principals/{id}/tokens/{credential_id}` would then revoke the wrong credential, silently leaving the intended (possibly compromised) one active.

A narrower, more likely variant — two credentials for the same principal sharing an identical `created_at` (coarse or injected `Clock`) — was fixed in the same PR by adding `id DESC` as a secondary sort key to `get_credentials_for_principal` (both backends), since ids are monotonically assigned and the newest one is always correct among same-timestamp ties. That fix does not close the cross-request race described here, which requires genuine concurrent issuance, not just a coarse clock.

### Fix

`Ontology.issue_token(principal_id, author) -> tuple[str, str]` now returns `(token, credential_id)` directly — the credential's id is already known at the point it's persisted, so neither caller needs a second, racy `list_tokens()[0]` lookup. Both `interfaces/rest.py`'s `issue_token_route` and the CLI's `principal issue-token` command were updated to unpack the tuple. Breaking change to `Ontology`'s public API (ADR-0019); recorded as an update to ADR-0014.

---

## KI-025 — CI's `mypy --strict` never type-checks `DuckDBBackend` against `StorageBackend` ✓ RESOLVED (M3)

**Severity:** Test gap — a future `StorageBackend` port addition could have landed SQLite-only and still passed every CI gate; now closed
**Milestone target:** M3 — resolved in `ci(quality): type-check conformance/conftest.py, resolve KI-025`
**SPEC reference:** Implementation Plan §2/§7.1 (dependency rule, quality gates), ADR-0016 (DuckDB backend)

### Description

CI's type-checking step is `uv run mypy --strict src` (`.github/workflows/ci.yml`) — `src/` only. `SQLiteBackend` gets checked structurally against the `StorageBackend` Protocol as a side effect: `Ontology.connect()` (`src/ontolith/ontology.py`) constructs one and passes it into `Ontology(backend: StorageBackend, ...)`, so a missing/mistyped Protocol method on `SQLiteBackend` would fail `mypy --strict src`.

`DuckDBBackend` has no equivalent path. The only place it's ever assigned to a `StorageBackend`-annotated slot is `conformance/conftest.py`'s `_duckdb_factory(...) -> StorageBackend` — and `conformance/` is never passed to `mypy` in CI. `uv run mypy --strict conformance/conftest.py` does pass today in isolation (confirmed), so the two backends currently agree — but nothing *guarantees* that going forward.

Surfaced during review of KI-022's `list_principals()` slice (PR #42): this is the second port-method addition in a row where both backends happened to get the method, checked only by discipline, not by CI. A future addition landing on `SQLiteBackend` alone (e.g. missed in a rushed PR, or added to `StorageBackend` and only one backend updated before the diff was reviewed) would still pass `ruff`, `mypy --strict src`, and the full test suite, since nothing structurally checks `DuckDBBackend`'s conformance to the Protocol in CI.

Widening CI's mypy invocation to the *whole* `conformance/` tree is not the cheap fix it first looks like: `uv run mypy --strict src conformance` (confirmed, run directly) currently fails with 28 pre-existing errors across `test_conflict.py`, `test_bitemporal.py`, `test_basic_assertion.py`, and `test_append_only_properties.py` — untyped/loosely-typed test fixtures and a couple of stale `# type: ignore` comments, unrelated to this KI. Only `conftest.py` alone is currently strict-clean.

### Fix

`.github/workflows/ci.yml`'s Type check step now runs `uv run mypy --strict src conformance/conftest.py` (was `src` only) — option (a) from the two considered. `DuckDBBackend`'s assignment to `_duckdb_factory(...) -> StorageBackend` is now structurally checked on every CI run, the same way `SQLiteBackend` already was via `Ontology.connect()`. A future port method missed on one backend now fails CI instead of passing silently.

The other 28 pre-existing mypy errors elsewhere in `conformance/` (untyped test fixtures, stale `# type: ignore` comments) are unrelated to this KI and remain out of scope — `conftest.py` was the only file that actually needed checking here, since it's the sole place a backend is assigned to a `StorageBackend`-typed slot outside `src/`.

---

## KI-026 — `resolve_contradiction()` has no self-resolution guard ✓ RESOLVED (M3)

**Severity:** Architecture gap — governance-integrity hole; a reviewer could unilaterally win a dispute they were party to, with zero check; now closed
**Milestone target:** M3 — resolved in `fix(govern): close resolve_contradiction self-resolution guard (KI-026)`
**SPEC reference:** SPEC §10.3 (contradiction resolution), ADR-0003 (self-review guard precedent)

### Description

`Ontology.resolve_contradiction(contradiction_id, winner_assertion_id, resolver)` (`src/ontolith/ontology.py`) checks that `resolver` has `review`/`admin` capability and is not AI-kind, but never checks whether `resolver` is the author (or delegate) of any member assertion in the contradiction — including the one being chosen as winner. `accept_proposal`/`reject_proposal`/`request_changes` all block exactly this via the shared `_require_reviewer` helper (`reviewer in (proposal.author, proposal.acting_as)`, ADR-0003's self-review guard); `resolve_contradiction` has no equivalent call anywhere in its body.

Concretely: a human principal with `review` capability who authored one of two disputed static facts can call `resolve_contradiction(contradiction_id, their_own_assertion_id, their_own_id)` and win — the other party's assertion is retracted, theirs reactivated, with no guard preventing it. `conformance/test_contradiction_resolution.py`'s existing vectors contain no self-authored-winner case.

`docs/adr/ADR-0022-rest-write-review-admin.md`'s §2 audit currently states this guard already exists for `resolve_contradiction`, grouping it with `accept_proposal`/`reject_proposal` — that claim is false for this method specifically.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

`resolve_contradiction` now resolves each member assertion (`self.backend.get_assertion(member_id)`) and raises `CapabilityError` if `resolver` is the author or delegate of *any* member — not just the winner, since an interested party shouldn't get to pick against their own losing entry either. All of this validation (state, membership, self-resolution) moved inside the existing `with self.backend.transaction():` block — a review pass caught that reading `contradiction.member_ids` before the transaction opened a real TOCTOU window (another thread could extend the same open contradiction via a concurrent `propose()` between the check and the write, and the new member would escape both the self-resolution check and the retraction loop). A member assertion that can't be resolved (`get_assertion` returns `None`) now raises `NotFoundError` rather than silently skipping the self-resolution check for it — assertions are append-only and never deleted, so a missing member is corruption, not a benign gap. Three new conformance vectors in `conformance/test_contradiction_resolution.py::TestResolveContradictionGuards` cover self-authored-winner, self-authored-losing-member, and delegate-authored cases. ADR-0022's §2 false claim corrected (it had also been copied into an already-written CHANGELOG entry — fixed there too).

Two related, narrower gaps were found while fixing this and are tracked separately rather than expanding this fix's scope: KI-033 (`retract()` lets a party to an open contradiction unilaterally retract the *opposing* member, achieving a similar outcome through a different method) and KI-034 (extending an open contradiction can resurrect an already-`retracted` member back to `flagged`).

---

## KI-027 — `request_changes()` produces a permanently stuck proposal, invisible to the default review queue ✓ RESOLVED (M3)

**Severity:** Architecture gap — a shipped governance action strands data with no recovery path
**Milestone target:** M3 — resolved in `feat(govern): add Ontology.resubmit(), close request_changes dead end (KI-027)`
**SPEC reference:** SPEC §9.1 (proposal state machine — `changes_requested ──resubmit──▶ submitted` is a normative transition, not terminal)

### Description

Once `Ontology.request_changes()` (ADR-0022 update, ships alongside `POST /proposals/{id}/review`) moves a proposal to `changes_requested`, nothing in the codebase can move it anywhere else — `_require_reviewer` (`src/ontolith/ontology.py:1125-1163`) only accepts proposals in `("require_review", "under_review")`, so any further `accept_proposal`/`reject_proposal`/`request_changes` call raises `ValidationError`. No `resubmit` method exists anywhere (confirmed via grep for `changes_requested` across `src/`). `conformance/test_review_workflow.py`'s dead-end test (`test_changes_requested_is_a_dead_end_for_further_review`) pins this as expected behavior, not a bug to fix — the gap was known at ship time (ADR-0022's Update section names it as deliberately out of scope) but was never logged as its own tracked issue, only as ADR prose.

Compounding this: `Ontology.proposals()` defaults to `state="require_review"` (`src/ontolith/ontology.py`) — the call a reviewer would naturally make to see "what's pending" — which silently excludes `changes_requested` proposals. A reviewer must already know to pass `state="changes_requested"` or `state=None` to ever see a proposal again after requesting changes on it.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

Added `Ontology.resubmit(proposal_id, author)` implementing SPEC §9.1's `changes_requested → submitted → {policy}` transition: only the proposal's own author or delegate may call it (the inverse of `_require_reviewer`'s self-review guard); the existing payload is replayed unedited through a fresh policy evaluation, but — unlike `propose`/`propose_ref` — the `kb_view` pinned for that evaluation is the *resubmission* instant, not `proposal.created_at` (ADR-0025 update): the proposal already exists, so a KB-reading `PolicyStrategy` must see what's true now. `Ontology.accept_proposal`'s operation-replay loop was extracted into a shared `_replay_proposal_operations` helper so `resubmit`'s auto-accept branch doesn't duplicate it. `_finalize_non_accepted_decision` (shared with `propose`/`propose_ref`/`retract`) gained an `is_new` flag: `resubmit` re-decides an *existing* persisted row via `update_proposal_state`, where the original callers `INSERT` a brand-new one via `put_proposal` — reusing the insert path on an existing id raised a UNIQUE-constraint `StorageError`, caught by the conformance suite before this shipped. Unlike `propose`'s own auto-accept path (which records no event for a brand-new proposal), `resubmit` always records a `ProposalEvent(type="resubmit")` regardless of outcome — it re-decides an already-persisted row, and a `require_review` outcome with no event would leave no trace of when policy last ran. REST gained `POST /proposals/{id}/resubmit`, MCP gained `ontolith.resubmit` (the only MCP-exposed way for an AI author to act on its own `changes_requested` proposal, since AI proposals always route to require_review per ADR-0003 and carry no write fallback); CLI parity (an explicit `ontolith proposal resubmit` command, vs. the SDK method already reachable through `ontolith proposal list --state pending` for discovery) is deferred to KI-032 (CLI proposal-review commands).

`Ontology.proposals()` gained a `state="pending"` query-level alias merging `require_review` and `changes_requested` — both are still-open proposals needing someone's attention, and without it a `changes_requested` proposal was invisible to any single-state query even after `resubmit` existed to act on it. `"pending"` is deliberately **not** the default: the canonical reviewer loop (`for p in kb.proposals(): kb.accept_proposal(p.id, ...)`) assumes every returned proposal is reviewer-actionable, which is only true of `require_review` — `changes_requested` proposals raise `ValidationError` from `accept_proposal`/`reject_proposal`. The default stays `state="require_review"`; pass `state="pending"` explicitly (`GET /proposals?state=pending`, `ontolith proposal list --state pending`) to see both.

---

## KI-028 — `.min_confidence()`/`.trust_at_least()` reintroduce an N+1 backend-round-trip pattern ✓ RESOLVED (M3)

**Severity:** Performance — unbounded per-entity (and per-assertion) backend round trips, unbenchmarked
**Milestone target:** M3 — resolved in `perf(query): push min_confidence/trust_at_least down to bulk backend lookups (KI-028)`
**SPEC reference:** Implementation Plan §9 (performance budgets)

### Description

`QueryBuilder._apply_confidence_trust_filters` (`src/ontolith/query/builder.py:216-235`) runs after `_base_candidates()` resolves the full pre-filter entity list — unbounded when neither `.where()` nor `.semantic()` is chained (`self._backend.entities(namespace=, concept=)` returns every entity of the concept). For each candidate, `_passes_confidence_trust` issues a separate `backend.assertions(subject=entity.id, status="active")` call, and `.trust_at_least()` additionally calls `backend.get_principal(author_id)` once per assertion, uncached even across repeated authors within the same query. `kb.query("Person").trust_at_least(5)` with no other filter on a 100k-entity concept issues on the order of 100k+ separate backend calls before `.limit()` is ever applied.

This is the same defect class KI-001 was created and fixed for — reintroduced, apparently unnoticed, when `.min_confidence()`/`.trust_at_least()` shipped alongside `.semantic()` as part of KI-018's hybrid-retrieval resolution. `tests/benchmarks/test_hybrid_query.py` exercises `.semantic()` and `.semantic()+.where()` only; neither confidence nor trust filtering is benchmarked, so the regression is invisible to CI.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

Added `StorageBackend.entities_meeting_confidence(namespace, concept, threshold)` / `.entities_meeting_trust(namespace, concept, min_trust)` (both backends) — a single `SELECT DISTINCT a.subject FROM assertion a JOIN entity e ON e.id = a.subject WHERE e.namespace = ? AND e.concept = ? AND ...` per filter (an added `JOIN principal` for trust), returning every qualifying entity id in that `(namespace, concept)` in one query. `QueryBuilder._apply_confidence_trust_filters` intersects this against the already-narrowed candidate list (from `.where()`/`.semantic()`, if chained) — one round trip per active filter, regardless of concept size or candidate count, not one per candidate.

An id-list-bound design (`WHERE id IN (...)`, one placeholder per candidate) was tried first and reverted during review: it hits SQLite's bound-variable limit outright on large concepts (`sqlite3.OperationalError: too many SQL variables` — invisible on this project's dev-machine SQLite build, whose reported limit happens to exceed the 1k-entity benchmark scale, but real on the upstream default of 32766, and much lower on some builds), and costs DuckDB linear per-parameter bind overhead (measured ~83µs/id — a 10k-entity concept scan with both filters chained would cost ~1.7s against the 150ms hybrid-query budget). Scoping by `(namespace, concept)` instead keeps the parameter count constant regardless of data size.

This tradeoff is bidirectional, not a strict improvement: scoping by concept means `.min_confidence()`/`.trust_at_least()` can no longer exploit an already-narrow `.where()`/`.semantic()` candidate set the way the reverted id-list design would have (a `.where()` match narrowed to 1 entity out of 50k measured ~1150x slower under the concept-scan design than the old per-candidate loop would have been for that one candidate). Tracked separately as KI-037 rather than re-introducing the id-list design's own failure mode to chase this back — a hint parameter that a backend may selectively exploit (SQLite can; a naive DuckDB `IN (unnest(?))` measured slower than its own full scan) is the likely fix, and is additive rather than another breaking `StorageBackend` change if landed later.

The two new SQLite methods carry `@_synchronized` like every other public `SQLiteBackend` method (KI-023's invariant) — missing on the first pass, and confirmed via a reproduction that a concurrent call without it returned an uncommitted row from another thread's still-open transaction. `tests/unit/test_sqlite_backend.py::TestConcurrency::test_entities_meeting_confidence_serialized_with_open_transaction` pins this: a reader blocked on an in-flight writer's lock, observing the writer's forced rollback rather than a dirty read.

New benchmarks (`test_bench_min_confidence_full_concept_scan`, `test_bench_trust_at_least_full_concept_scan` in `tests/benchmarks/test_hybrid_query.py`) exercise both filters over a 1k-entity concept scan with no `.where()`/`.semantic()` narrowing — the scenario that made the original N+1 invisible — at ~4-5ms mean, comfortably inside the general symbolic-query budget; informational only, no CI gate, so this alone does not prevent a future reintroduction of the N+1 (a spy-backend unit test asserting call counts would be a stronger regression guard, not added here). New conformance vectors (`conformance/test_confidence_trust_filters.py`) prove both backends implement the push-down identically (the pre-existing unit tests only ever exercised SQLite), including that the two filters compose conjunctively (AND), not disjunctively.

Not addressed (pre-existing, out of scope for this performance fix — tracked as KI-036): `.min_confidence()`/`.trust_at_least()` never threaded `QueryBuilder._as_of_time` through to the confidence/trust check at all, before or after this fix — `kb.as_of(t).query(...).min_confidence(...)` silently evaluates against current-active assertions regardless of `t`. Both builder methods' docstrings now say so explicitly. The new backend methods' signatures deliberately don't reserve an unused `as_of_time` parameter for this — KI-036's fix needs a design decision on what "trust_level as of `t`" even means (principals aren't currently versioned), and a speculative parameter shaped before that decision is made risks being the wrong shape anyway.

---

## KI-029 — MCP `ontolith.schema` and REST `GET /schema` omit `relations` ✓ RESOLVED (M3)

**Severity:** Architecture gap — SPEC-required schema information is unreachable via two of the four primary interfaces
**Milestone target:** M3 — resolved in `feat(interfaces): add relations to MCP/REST schema output (KI-029)`
**SPEC reference:** SPEC §14.4 (`ontolith.schema` MUST "Return concepts/relations/temporality")

### Description

`schema_tool` (`src/ontolith/interfaces/mcp.py:64-103`) iterates `concept_def.properties` only; `ConceptDef.relations` (`src/ontolith/schema/ir.py:58-73`, populated for every schema that declares a `Relation`) is never read. `GET /schema`'s `ConceptOut`/`get_schema` route (`src/ontolith/interfaces/rest.py:98-103`, `:495-...`) has the identical gap — no relations field on the response model at all.

An agent or REST client inspecting the schema this way cannot see that a relation like `Person.employer` exists, or whether it's `time_varying` — exactly the information that predicts whether a subsequent proposal on that predicate will supersede or contradict (SPEC §10.1).

Surfaced during a whole-project milestone audit (2026-07-29); flagged in a prior 2026-07-14 audit and not yet closed.

### Fix

Added a `relations` list (name, target concept, cardinality, required, temporality, inverse) alongside `properties` in both `schema_tool`'s dict output and REST's new `RelationOut` model on `ConceptOut`/`SchemaOut` — mirroring `PropertyOut`'s existing shape. `ConceptOut.relations` is a required field (not defaulted), so any caller still constructing one without it now fails fast at construction rather than silently omitting the field again; the one in-repo call site (`get_schema` route) was updated accordingly. `PropertyOut`/the property dict output also gained `cardinality` in the same pass — an omission of the same class (SPEC §4/ADR-0017: cardinality, not just temporality, predicts contradiction-vs-coexistence for a subsequent proposal), left inconsistent with relations (which already had it) had this not been caught in review.

The CLI has no schema-inspection command at all — genuinely out of scope for this fix (a new CLI command, not an output-shape change), but this text previously claimed it was "tracked separately" under KI-032, which was wrong: KI-032 covers CLI proposal-review commands and never mentions schema. Corrected; the actual gap is now tracked as KI-038 (`ontolith schema show` closed by KI-038; `ontolith schema migrate` remains open, forward-tracked as KI-048).

---

## KI-030 — `QueryBuilder.where()` silently no-ops on relation-traversal filter keys ✓ RESOLVED (M3)

**Severity:** Test gap / DX — a documented example produces an empty result with no error
**Milestone target:** M3 — resolved in `fix(query-store): reject dunder relation-traversal keys, match relation filters on value_ref (KI-030)`
**SPEC reference:** N/A — internal DX/correctness gap, not a SPEC deviation

### Description

`QueryBuilder`'s class docstring (`src/ontolith/query/builder.py:28-31`) advertises `kb.as_of("2025-01-01").query(Person).where(employer__name="Acme Corp")` as a working example; `.where()`'s own docstring (`:63-67`) still says "M2 will add relation traversal." `_qualified_filters()` (`:166-168`) compiles any keyword into the literal predicate string `f"{concept}.{key}"` — `employer__name` becomes the literal predicate `"Person.employer__name"`, which matches no assertion. Even a correctly-named relation-target filter (e.g. `employer="org-id"`) can't match, since `entities_where()`'s SQL (`store/{sqlite,duckdb}/backend.py`) only compares `value_lit`, never `value_ref`. `.where()` accepts any of this silently and returns an empty list — no error, no warning — for a pattern its own docstring calls working, two milestones after the "M2 will add" comment was written.

Surfaced during a whole-project milestone audit (2026-07-29); flagged in a prior 2026-07-14 audit and not yet closed.

### Fix

`.where()` now rejects any dunder-containing key (e.g. `employer__name`) with a `ValidationError` at call time, naming the offending key and explaining that neither multi-hop traversal nor lookup operators are implemented (ADR-0027 records this as a deliberate deferral, not an oversight; see also KI-039) — instead of silently compiling it into `f"{concept}.{key}"`, a predicate string that could never match anything. MCP's `ontolith.query` tool and REST's `POST /query` both already mapped `ValidationError` correctly (400 / structured error) once `.where()` started raising it; only MCP's `query_tool` needed an explicit `try`/`except` added, since it previously called `.where()` outside its existing error-handling block.

Separately, the class docstring's own example used a relation-target filter that — dunder syntax aside — couldn't have worked anyway, since `entities_where()`'s SQL only ever compared `value_lit`. Rather than just deleting the example, `entities_where()` (`store/{sqlite,duckdb}/backend.py`) now matches a filter value against `value_lit` or `value_ref`, so a direct relation-target-id equality filter (`employer="org-123"`) actually returns matches — the class docstring's example was updated to this form, which now genuinely works. `.where()`'s docstring was rewritten to state the real contract: equality on literal properties and on a relation's target id, no traversal.

**Caught in review, fixed before merge:** the first version of the `value_ref` match used a single `value_lit = ? OR value_ref = ?` predicate. Measured against a 100k-assertion SQLite fixture, the `OR` defeated both `idx_assertion_pred_value` and a new `idx_assertion_pred_ref` companion index — the planner fell back to a full table scan on the bitemporal (`as_of`) branch (~9x slower measured; ~600x at 50k rows for a single predicate) — turning every `.where()` call, not just relation filters, into an unindexed scan on the backend the SPEC performance budgets target. Reworked into a `UNION ALL` of two single-column point lookups, one per index, which restored (slightly beat) the pre-fix latency. The SPEC §11.1 example, PRD walkthrough, and a `docs/Ontolith_UseCases_and_Interfaces.md` example (an unrelated but same-shaped `__contains` lookup-operator gap, KI-039) were also brought in line with the actual (equality-only) contract, and ADR-0027 records the traversal/lookup-operator deferral as a decision rather than leaving the SPEC and code in silent disagreement.

---

## KI-031 — Schema `value_type`/`required` are declared but never enforced at write time — PARTIALLY RESOLVED (M3)

**Severity:** Architecture gap — declared schema constraints are silently unenforced
**Milestone target:** M3 — `value_type` resolved; `required` remains open, tracked forward as KI-041/KI-042
**SPEC reference:** SPEC §4 (`value_type` is a core type; `required`/`unique`/`constraints` are validator-backed, §13); Implementation Plan §4.3 ("validate at edges")

### Description

`Ontology._require_known_predicate` (`src/ontolith/ontology.py:397-409`) rejects an unknown predicate at write time but never checks the caller-supplied `value_type` against the schema-declared `PropertyDef.value_type` (`src/ontolith/schema/ir.py:12-27`), and nothing checks `PropertyDef.required`/`RelationDef.required` at all. A predicate declared `value_type: Integer` in schema currently accepts `assert_literal(..., value_type="Text", ...)` without error; a `required: true` property is never checked as present on an entity.

A prior audit (2026-07-14) flagged this as partially open; cardinality is now enforced (`govern/conflict.py`, ADR-0017) and unknown predicates are now rejected, but the `value_type`/`required` remainder was never itself tracked as its own entry.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

**`value_type`: resolved.** `_require_known_predicate` (`src/ontolith/ontology.py`) now takes an optional `value_type` parameter; `assert_literal` and `propose` (the two literal write paths) pass their caller-supplied `value_type` through, and a mismatch against the schema's declared `PropertyDef.value_type` (via new `SchemaIR.value_type_of()`) raises `ValidationError` — same shape and same call sites as the existing unknown-predicate check. `assert_ref`/`propose_ref` are unaffected (relations have no `value_type`). No check fires when the predicate resolves to a relation (`value_type` is a required field on `PropertyDef`, so a property can never itself omit it), or when no schema is registered for the namespace. Enforcement is submission-time only — a proposal is not re-validated against a possibly-changed schema when later accepted/resubmitted, matching the existing, documented precedent that `_require_known_predicate` isn't re-run at replay either.

**`required`: still open, deliberately deferred, not silently dropped.** ADR-0028 records the decision not to add a core-layer required-field presence check — SPEC §4 itself assigns `required` to the validator layer (§13), and structurally a per-write core gate is the wrong shape for it anyway (an entity necessarily fails every required declaration for a moment right after `create_entity`, before its other properties are asserted). This ADR's first draft justified the deferral by pointing at `RequiredFieldsValidator` (KI-010) as an already-working answer; that turned out to be inaccurate on review and was corrected before merge: the plugin's required-predicate set is independently configured, not derived from `PropertyDef.required`/`RelationDef.required` (now tracked as **KI-041**), and more fundamentally, no code path anywhere invokes any registered `Validator.validate()` at all — the protocol exists but nothing calls it (now tracked as **KI-042**). Today `required` is schema metadata, surfaced read-only via `GET /schema`/`ontolith.schema`/codegen, enforced by nobody. See ADR-0028 for the full corrected rationale.

---

## KI-032 — CLI has no `proposal accept`/`reject`/`review` commands ✓ RESOLVED (M3)

**Severity:** Test gap / DX — one of SPEC's four primary interfaces cannot act on its own review queue
**Milestone target:** M3 — resolved in `feat(interfaces): add CLI proposal accept/reject/review/resubmit commands (KI-032)`
**SPEC reference:** SPEC §14.2 (CLI command surface, `proposal {list|review}`)

### Description

`src/ontolith/interfaces/cli.py`'s `proposal_app` has exactly one subcommand, `list` (`:347`). `Ontology.accept_proposal`/`reject_proposal`/`request_changes` all exist, and REST exposes all three (`POST /proposals/{id}/accept|reject|review`), but the CLI — one of SPEC's four primary interfaces alongside SDK/REST/MCP — has no way to act on any of them. An operator using only the CLI can discover what's pending review (`proposal list`) but cannot accept, reject, or request changes on any of it.

Surfaced during a whole-project milestone audit (2026-07-29).

### Fix

Added `ontolith proposal accept <id> --reviewer`, `proposal reject <id> --reviewer [--reason]`, and `proposal review <id> --reviewer [--reason]` (mapping to `request_changes`; the `review` name is SPEC §14.2's own literal command name, `proposal {list|review}`, not just a REST-route echo), each calling straight through to the corresponding `Ontology` method. `--reviewer` accepts `--author` as an alias — caught in review: `Proposal.author` is a real, distinct field `_require_reviewer` explicitly checks the acting principal is *not* (self-review is rejected), so naming the reviewer option `--author` reads as an assertion guaranteed false on every successful call; `--author` is kept working as an alias rather than a breaking rename. Also added `ontolith proposal resubmit <id> --author` (correctly `--author` here — the resubmitting principal genuinely is the proposal's own author or delegate) — REST/MCP both gained a `resubmit` action alongside accept/reject/review when KI-027 closed the `request_changes` dead end, with CLI parity explicitly deferred to this KI at the time (see `CHANGELOG.md`'s KI-027 entry); closing it here keeps all four proposal-lifecycle actions available from every primary interface, not three of four.

---

## KI-033 — `retract()` lets a party to an open contradiction unilaterally retract the opposing member ✓ RESOLVED (M3)

**Severity:** Architecture gap — same governance outcome as KI-026, reachable through a different method that has no contradiction awareness at all
**Milestone target:** M3 — resolved in `fix(govern): reject retract() of a contradiction member the retracting principal is party to (KI-033)`
**SPEC reference:** SPEC §10.3 (contradiction resolution)

### Description

`Ontology.retract()` routes through `self.policy` like any other governed write; a human principal with `review` (or higher) capability auto-accepts under `ThresholdPolicy`. `retract()` performs no check on whether the target assertion is a `flagged` member of an open contradiction, nor whether the caller is a party to that contradiction (author/delegate of any of its members). A principal who authored one side of a disputed static fact can therefore retract the *opposing* member directly — reproduced: after such a retraction, the contradiction is left `open` with only the retracting party's own value still `flagged`, so a later legitimate resolver effectively has no real choice left to make.

KI-026 closed the front door (`resolve_contradiction` itself); this is a side door reaching a similar outcome through `retract()`, which has no notion of contradictions at all today. Found while fixing KI-026, not introduced by it — pre-existing.

### Fix

New `Ontology._reject_retract_if_party_to_contradiction(assertion_id, parties)`: if the target assertion is currently `flagged` and a member of an open contradiction, raises `CapabilityError` when any of `parties` is the author or delegate of *any* member of that contradiction — mirroring KI-026's "any member, not just one side" reasoning, so retracting your own losing entry is blocked too, not just the opposing one. A missing member (`get_assertion` returns `None`) raises `NotFoundError` rather than silently skipping the check for it, matching `resolve_contradiction`'s own precedent (assertions are append-only and never deleted, so a missing member is corruption, not a benign gap). Runs inside the same transaction that performs the retraction, before any of that transaction's writes land, in both call sites that can execute a `retract`-kind operation — `retract()`'s own auto-accept branch (`parties = {author, acting_as}`), and `_replay_proposal_operations`'s `retract` branch (shared by `accept_proposal`/`resubmit`; `parties = {proposal.author, proposal.acting_as}`, plus the accepting reviewer when called from `accept_proposal` — a reviewer who is themselves a party to the same contradiction can reach the identical one-sided outcome by approving a *neutral* principal's retract proposal, not just by retracting directly, so `parties` must cover whoever's decision actually causes the retraction, not only the original proposer). Matches `resolve_contradiction`'s own reasoning for checking inside the transaction: a contradiction opened or extended concurrently can't slip past a check made only beforehand. A neutral third party (author/delegate of no member) is unaffected by this guard — whether that should itself require `resolve_contradiction`-grade `review`/`admin` capability, rather than plain `write`, is a separate, broader question tracked as KI-043, not expanded into this fix's scope. New conformance vectors in `conformance/test_contradiction_resolution.py::TestRetractContradictionGuard`.

---

## KI-034 — Extending an open contradiction can resurrect an already-`retracted` member back to `flagged` ✓ RESOLVED (M3)

**Severity:** Test gap — a documented lifecycle transition (`retracted` is meant to be terminal) doesn't hold under a specific sequence
**Milestone target:** M3 — resolved in `fix(govern): retracted stays terminal across contradiction extension (KI-034)`
**SPEC reference:** SPEC §5 (assertion lifecycle — `retracted` status)

### Description

Reproduced while investigating KI-033: if an assertion belonging to an open contradiction is retracted (e.g. via the KI-033 gap, or by any other means reaching `retract()`), a subsequent `propose()`/`assert_literal` on the same `(subject, predicate)` that extends the same open contradiction can flip that already-`retracted` assertion's status back to `flagged`. `retracted` is meant to be a terminal status (SPEC §5's append-only lifecycle) — this is the path reachable from the write/proposal pipeline where it wasn't. Review found `flag_contradiction()` has the identical bug via a separate call site (fixed alongside this, below); `resolve_contradiction()` could also resurrect an already-`retracted` member all the way to `active` if picked as the winner — a broader eligibility question, not the same "extend a contradiction" mechanism this fix closes, tracked and since resolved separately as KI-044/ADR-0031 (which also extended this fix's own "retracted is terminal" guard to cover `superseded`, found incomplete during KI-044's own review).

### Fix

The primary bug lives in `Ontology._apply_with_conflict_routing`'s own "extend an already-open contradiction" branch (not `govern/conflict.py`'s pure `route()` — that path is bypassed entirely once a contradiction is already open), which unconditionally re-flagged every id in `open_contradiction.member_ids` alongside the incoming assertion. The `Contradict`-branch flagging loop now looks up each existing member's *current* status and skips the `set_assertion_status(..., "flagged")` write (and its accompanying event) for any member already `retracted` (and, since KI-044/ADR-0031 widened this same branch, `superseded` too) — treating both as final regardless of the contradiction's own open/resolved state. A missing member now raises `NotFoundError`, matching `resolve_contradiction`'s own KI-026 precedent (found in review). `flag_contradiction()` (SPEC §14, MCP `ontolith.flag_contradiction` — reachable at only `propose` capability, including by an AI principal) had the identical resurrection bug via its own, separate flagging loop; it now skips `retracted`/`superseded` members the same way. The member's id is deliberately left in the `Contradiction`'s own `member_ids` — that list isn't audit-only, it's also `resolve_contradiction`'s winner-eligibility set (KI-044/ADR-0031 closed that interaction) and `_reject_retract_if_party_to_contradiction`'s scan set (which — per KI-051, found later — still doesn't recognize a `superseded` member as governed the way it does a `flagged` one); only the re-flagging write is skipped here. `resolve_contradiction` itself also no longer re-emits a duplicate, resolver-misattributed `retracted` event for a loser that's already `retracted` (found in review; later widened to `superseded` too, KI-044). New conformance vectors in `conformance/test_contradiction_resolution.py::TestRetractedIsTerminalAcrossExtension`, including one confirming the primary vector fails without the fix (reverted locally and re-run to verify).

---

## KI-035 — Proposal-transition methods validate state before opening the write transaction (TOCTOU) ✓ RESOLVED (M3)

**Severity:** Architecture gap — data-integrity risk under real concurrent traffic, not yet triggered by any test (single-threaded today)
**Milestone target:** M3 — resolved in `fix(govern): close proposal-transition TOCTOU window (KI-035)`
**SPEC reference:** SPEC §9.1 (proposal state machine)

### Description

`Ontology.accept_proposal`/`reject_proposal`/`request_changes`/`resubmit` (`src/ontolith/ontology.py`) each read the proposal, validate its current state (via `_require_reviewer` for the first three; inline in `resubmit`), and run policy evaluation — all *before* opening `with self.backend.transaction():`. Only the resulting writes are atomic; the read-validate window itself is not. Two concurrent calls that both observe the same pre-transition state (e.g. two `resubmit()` calls both reading `changes_requested`, or an `accept_proposal` racing a `reject_proposal` both reading `require_review`) can both pass validation and both reach the transactional write — under an auto-accepting policy this can replay the same proposal's operations twice.

`resolve_contradiction`'s equivalent gap (found and fixed for KI-026: "a review pass caught that reading `contradiction.member_ids` before the transaction opened a real TOCTOU window") shows the fix pattern: move validation inside the transaction, re-reading the row instead of trusting the pre-transaction snapshot. This is the same class of bug applied to the proposal state machine's four transition methods, none of which received that fix. Found while re-reviewing the KI-027 `resubmit()` fix — pre-existing in `accept_proposal`/`reject_proposal`/`request_changes` before this branch, not introduced by it.

### Fix

`_require_reviewer` split into `_require_reviewer_principal(reviewer)` (auth/capability/AI-kind checks — depend only on the reviewer's own identity, safe to run once before the transaction opens) and `_require_pending_proposal(proposal_id, reviewer)` (re-reads the proposal fresh and checks self-review + state — MUST run inside the transaction, immediately before the write). `accept_proposal`/`reject_proposal`/`request_changes` all now call `_require_pending_proposal` as the first thing inside their `with self.backend.transaction():` block, mirroring `resolve_contradiction`'s own KI-026 fix. `resubmit` keeps its original author/state checks before policy evaluation (needed to construct the object policy evaluates against) as an optimistic fast-fail, but adds a second, authoritative re-read-and-recheck as the first thing inside its own transaction, before `_finalize_non_accepted_decision`/the auto-accept write — so a proposal a concurrent call already moved out of `changes_requested` is detected and rejected rather than double-processed. New conformance vectors in `conformance/test_review_workflow.py::TestProposalTransitionTOCTOU`, one per method: a `_RacingClock` test double (subclasses `FixedClock`) fires a one-shot side effect the first time `.now()` is called — for `accept_proposal`/`reject_proposal`/`request_changes` that's the exact point right before the write transaction opens; `resubmit` calls it slightly earlier (before policy evaluation, itself before the transaction), a marginally wider window but still inside the same gap the fix closes — running a real, complete sibling transition as if it had just won the race, deterministically simulating the TOCTOU window without needing real threads. All four confirmed to fail without the fix (reverted `ontology.py` locally and re-ran). `resubmit`'s in-transaction re-check closes the race on the proposal's *state* only, not on the `kb_view` its policy decision was evaluated against outside the transaction — a pre-existing, deliberate tradeoff (per this KI's own Fix text) shared by `propose`/`propose_ref`/`retract`, not something newly closed here.

Review found the identical TOCTOU shape in `flag_contradiction()` (reads two assertions and any existing open contradiction before its transaction, filed separately as KI-045, since resolved) and that `DuckDBBackend` has no equivalent of `SQLiteBackend`'s KI-023 lock, so this fix's serialization guarantee is airtight only for SQLite today (filed as KI-046) — neither expanded into this fix's scope.

---

## KI-036 — `.min_confidence()`/`.trust_at_least()` ignore `.as_of()` ✓ RESOLVED (M3)

**Severity:** Architecture gap — bitemporal query results are inconsistent within a single query
**Milestone target:** M3 — resolved in `fix(query): thread as_of_time through confidence/trust filters (KI-036)`
**SPEC reference:** SPEC §12 (bitemporal query semantics — `as_of` reconstruction)

### Description

`QueryBuilder._apply_confidence_trust_filters`/`_passes_confidence_trust` (`src/ontolith/query/builder.py`) never reads `self._as_of_time`. `kb.as_of(t).query("Person").where(...)` correctly threads `t` through `_base_candidates()` (both `entities()` and `entities_where()` accept `as_of_time`), but `.min_confidence()`/`.trust_at_least()` chained onto the same query always check current-active assertions/principals regardless of `t` — an entity can pass the `.where()` half of the query as it existed at `t`, then get filtered by confidence/trust values that only became true after `t` (or that existed at `t` but were later superseded/retracted). The result is not a coherent point-in-time view.

Found while fixing KI-028 (the N+1 performance issue for the same two filters); pre-existing before that fix and unchanged by it — the new `entities_meeting_confidence`/`entities_meeting_trust` backend methods intentionally preserve the old (non-bitemporal) behavior rather than silently changing query semantics inside a performance-only fix.

### Fix

`StorageBackend.entities_meeting_confidence`/`entities_meeting_trust` (port + both backends) gained an `as_of_time: datetime | None = None` parameter, and `QueryBuilder._apply_confidence_trust_filters` now passes `self._as_of_time` through to both. When set, each method switches from `status = 'active'` to the same bitemporal window `entities_where()` already uses (`asserted_at <= as_of_time`, `valid_from`/`valid_to` bracketing it) — so the qualifying assertion is whichever one was actually active at `t`, not whichever is active now. One caveat, since `status` itself is not bitemporally versioned: a `status = 'flagged'` assertion (an open static contradiction) is excluded regardless of `t`, even at a `t` before it was flagged — matching `entities_where()`'s own default (`include_flagged=False`), the only mode `QueryBuilder` ever reaches.

The design question this KI flagged — what "trust_level as of `t`" means, since principals aren't versioned — resolved to: `trust_level` is always the principal's *current* value, never a historical one. This isn't an approximation: no code path anywhere updates a principal's `trust_level` after creation (confirmed by grep — the only `UPDATE principal*` statements touch `principal_credential`, never `principal` itself), so "trust_level as of any t at or after the principal's creation" and "trust_level now" are provably the same number. Only which *assertion* counts as qualifying is bitemporally scoped; the trust threshold it's compared against is not, because there is nothing to reconstruct. A new regression guard, `tests/unit/test_principal_trust_immutability_invariant.py`, greps both backends for any `UPDATE`/`DELETE FROM` targeting the `principal` table and fails if one is ever added — the prompt that this shortcut needs replacing with real principal versioning before `.trust_at_least()` + `.as_of()` can keep relying on it.

New conformance vectors in `conformance/test_confidence_trust_filters.py::TestAsOfConfidenceTrust` (22 cases across both backends): a retraction-boundary pair and a schema-declared `Person.employer` time_varying supersession-boundary pair per filter (as originally landed); plus, per review, boundary vectors isolating each of the four temporal clauses individually (backdated-but-not-yet-known, future `valid_from`, the `valid_to` half-open boundary, and flagged-exclusion) per filter, and vectors combining `.as_of()` with `.where()` and with both filters chained together. Mutation-testing each clause in the SQLite backend (blanking `status != 'flagged'`, `valid_from`, `valid_to`, and `asserted_at` one at a time and rerunning the suite) confirms every clause is now individually pinned by a failing test, not just exercised incidentally.

---

## KI-037 — `.min_confidence()`/`.trust_at_least()` can't exploit an already-narrowed `.where()`/`.semantic()` candidate set ✓ RESOLVED (M3)

**Severity:** Performance — no budget violated today, but a real, measured slowdown relative to the design it replaced for the selective-query case
**Milestone target:** M3 — resolved in `perf(query): let entities_meeting_confidence/trust exploit a narrowed candidate set (KI-037)`
**SPEC reference:** Implementation Plan §9 (performance budgets)

### Description

KI-028's fix scoped `entities_meeting_confidence`/`entities_meeting_trust` by `(namespace, concept)` rather than an explicit candidate id list, specifically to keep the SQL parameter count constant regardless of data size (an id-list-bound design was tried first and reverted — see KI-028's own Fix text). The tradeoff: when `.where()` or `.semantic()` has already narrowed the candidate set to a small fraction of the concept (the common case for `.semantic()`, which caps overfetch at `_MAX_OVERFETCH = 1000` regardless of concept size), `.min_confidence()`/`.trust_at_least()` still scan the *entire* `(namespace, concept)` rather than just the narrowed candidates — measured at ~1150x slower than the old per-candidate N+1 loop for a single-candidate `.where()` match against a 50k-entity concept (46ms vs ~0.04ms), and estimated to consume a meaningful fraction of the 150ms hybrid-query budget at 100k+ entities when chained after `.semantic()`.

This is not a regression relative to *shipped* behavior (the id-list design that would have preserved this property was never released — it was caught and reworked during KI-028's own review, before merge), but it is a real, measured cost of the design actually shipped, worth its own tracked follow-up rather than silently living only in KI-028's fix-note prose. `tests/benchmarks/test_hybrid_query.py::test_bench_min_confidence_narrowed_by_where` benchmarks this scenario (informational only, no CI gate) — at the 1k-entity scale used there the cost is small (SQLite's query planner still picks an index-backed scan), so the effect is real but not yet visible at benchmark scale; the 46ms figure above was measured separately at 50k entities.

### Fix

`StorageBackend.entities_meeting_confidence`/`entities_meeting_trust` (port + both backends) gained a **breaking** `candidate_ids: frozenset[str] | None = None` parameter — `QueryBuilder` now passes it as an explicit keyword on every call (including `None`), so any third-party `StorageBackend` must add it (an earlier draft of this fix called it "additive, not breaking"; that was wrong — see CHANGELOG). `QueryBuilder._apply_confidence_trust_filters` passes a non-`None` value only when `.where()`/`.semantic()` narrowed the base candidate set *and* that set is no larger than a new `_CANDIDATE_HINT_MAX = 1000` constant — never on a bare full-concept query (no benefit), and never on a large narrowed set (see below for why that matters).

SQLite binds the id set as a single JSON-encoded parameter (`AND e.id IN (SELECT value FROM json_each(?))`) rather than one placeholder per id; `DuckDBBackend` uses the equivalent `AND e.id IN (SELECT unnest(?))`. Both backends now genuinely narrow — an earlier draft of this fix left `DuckDBBackend` ignoring the hint entirely, reasoning that `unnest()`-based narrowing "measured slower than DuckDB's existing unscoped scan"; that reasoning didn't hold up under review at a wider range of scales: `unnest()` is 2-3x *faster* than the unscoped scan at 10k-50k entities for a small candidate set, and only slower at very small (1k) scale where everything is already fast in absolute terms — but it is a measured **>100x regression** for a candidate set of a few thousand against a 10k-entity concept, which is why `_CANDIDATE_HINT_MAX` exists: bounding the hint's size, not avoiding `unnest()`/`json_each` altogether, is what makes narrowing a reliable win on both backends. SQLite has an analogous (if less severe) crossover: narrowing is a clear win through several thousand candidates against a 50k-entity concept, and a measured regression past ~15-25k.

Measured like-for-like — same query, same fixture, same machine, code-only diff, 50k entities, single-candidate `.where()` match — this fix consistently measures several times faster (absolute latency varies by run/hardware, so no specific ratio is recorded here; see `tests/benchmarks/test_hybrid_query.py`'s `test_bench_min_confidence_narrowed_by_where_50k` vs `..._without_candidate_ids` for a reproducible comparison). An earlier draft of this benchmark claimed a much larger, ~17x win by comparing against a full-concept-scan benchmark returning 25,000 results instead of the same 1-result query with narrowing suppressed — an apples-to-oranges comparison dominated by result-set materialization cost, not by `candidate_ids`; the two benchmarks now exist side by side specifically so that mistake isn't repeated. Not O(1) on SQLite: `EXPLAIN QUERY PLAN` shows it still `SEARCH`es the full `(namespace, concept)` range of the `entity` index before bloom-filtering against `candidate_ids` — only the assertion-side join is pruned.

New conformance vectors (`TestCandidateIdsNarrowing` in `conformance/test_confidence_trust_filters.py`, cross-backend) pin the backend-agnostic contract that holds regardless of whether/how a given backend narrows: `(full_qualifying_set & candidate_ids) <= narrowed <= full_qualifying_set` — a narrowed call never returns a non-qualifying entity, and never drops a candidate that genuinely qualifies. Backend-specific "does narrowing actually narrow" vectors (`TestCandidateIdsNarrowing` in both `tests/unit/test_sqlite_backend.py` and `tests/unit/test_duckdb_backend.py`) pin each backend's own implementation with exact-equality assertions, including an empty-`candidate_ids` short-circuit — these, plus the conformance vectors above, are what actually regression-pin the fix (confirmed to fail reverted to pre-KI-037 `main`). A `.semantic()`-narrowed (not just `.where()`-narrowed) end-to-end vector was also added to `tests/unit/test_query.py`'s `TestSemanticSearch` class for branch coverage of the gating condition's `self._semantic_text is not None` disjunct — this one is *not* a regression pin (like its pre-existing `.where()`-based sibling, `_apply_confidence_trust_filters`'s local re-intersection makes the end-to-end outcome invariant to whether `candidate_ids` is threaded at all; the test still passes reverted to `main`), and its docstring says so.

Docstrings on both port methods were rewritten: `(namespace, concept)` is still always the mandatory scope (this is what bounds the parameter count — see KI-028's own reverted id-list design), and `candidate_ids` is an optional narrowing hint layered on top, not a replacement for it.

---

## KI-038 — CLI has no `schema` command — ✓ RESOLVED (M3, across two entries)

**Severity:** Architecture gap — SPEC-normative CLI surface is entirely unimplemented
**Milestone target:** M3 for `show` (resolved in `feat(cli): add schema show command (KI-038)`); `migrate` forward-tracked as KI-048, now also resolved
**SPEC reference:** SPEC §14.2 (`ontolith schema {show|migrate}`)

### Description

`src/ontolith/interfaces/cli.py` registers `principal`/`entity`/`proposal`/`contradiction`/`namespace` sub-apps plus top-level `assert`/`assertions`/`query`/`reindex` commands — no `schema` command at all, despite SPEC §14.2 normatively listing `ontolith schema {show|migrate}` as part of the CLI surface. Found while fixing KI-029 (MCP/REST schema output), which had initially described this gap as "tracked separately... KI-032 covers CLI proposal-review commands specifically" — that framing was wrong: KI-032 doesn't mention schema at all, so the gap had no actual tracked issue until now.

### Fix

Added `ontolith schema show [--namespace]` (new `schema_app` sub-app, matching every other CLI sub-command's `_kb()`/try-except-finally shape), printing `namespace=... version=...` followed by each concept's properties (`name: value_type  cardinality=...  temporality=...  required=...`) and relations (`name -> target_concept  cardinality=...  temporality=...  required=...  inverse=...`) — the same field set as MCP's `ontolith.schema`/REST's `GET /schema` output (both already fixed by KI-029 to include relations), though not their exact attribute order: REST's own `PropertyOut`/`RelationOut` don't even agree with each other on relation field order, so the CLI normalizes to one consistent order instead of copying either verbatim. A namespace with no registered schema prints a plain message rather than an empty/error output, matching other read-path commands' "nothing found" convention elsewhere in the CLI.

`ontolith schema migrate` was out of scope for this pass, as this KI's own original Fix text anticipated ("split into its own issue if `show` lands first"). Forward-tracked as **KI-048**, matching the pattern KI-031 set for its own leftover half, rather than leaving it implicit in this entry's own partially-resolved status — **KI-048 has since been resolved too (ADR-0034)**, so the full `{show|migrate}` surface SPEC §14.2 names is now implemented.

---

## KI-039 — `QueryBuilder.where()` has no lookup-operator syntax (e.g. `__contains`), despite a documented example using one ✓ RESOLVED (M3)

**Severity:** Documentation/DX gap — a documented use-case example used a filter shape the code never implemented
**Milestone target:** M3 — resolved in `feat(query): add __contains/__gt/__lt/__gte/__lte lookup operators to .where() (KI-039)`
**SPEC reference:** N/A — not covered by SPEC §11.1's normative shape; only appeared in a worked example

### Description

`docs/Ontolith_UseCases_and_Interfaces.md` §4.3 used `kb.query(Claim).where(text__contains="Compound X")` to illustrate a substring/lookup-style filter. `.where()` has never supported any lookup-operator syntax — only bare equality — so this example was exactly as broken as KI-030's `employer__name` traversal example: a double-underscore key that (pre-KI-030) silently compiled into an unreachable predicate and matched nothing, and (post-KI-030) now raises `ValidationError` instead, same as any other dunder key.

Surfaced while fixing KI-030 and drafting ADR-0027, which defers both traversal and lookup operators as a single scope decision; the use-case doc was corrected to an equivalent equality-based example in the same pass, but implementing `__contains` (or any other operator) itself was out of scope for that fix.

### Fix

`.where()` now recognizes a closed set of five lookup-operator suffixes: `__contains` (substring match) and `__gt`/`__lt`/`__gte`/`__lte` (numeric range). Multi-hop relation traversal (a hypothetical `employer__name=`, ADR-0027) and any dunder suffix outside this set are still rejected with `ValidationError`, unchanged from KI-030.

Design decisions this needed, per the Fix text's own open questions:

- **How operators compose with relations**: they don't. `__contains`/range operators only ever check `value_lit`, never `value_ref` — plain equality keeps its existing dual-branch (`value_lit` OR `value_ref`) behavior, matching KI-030. This falls out for free for range operators: `SchemaIR.value_type_of()` returns `None` for a `RelationDef` (relations have no `value_type`), and range operators require a schema-declared `Integer`/`Float` value_type (see next point), so a relation predicate is rejected the same way a `Text`-typed property is — no special-case code needed.
- **Numeric correctness**: `value_lit` is always stored as `TEXT` (SPEC §12.2), so `"9" > "10"` lexicographically but not numerically. Rather than silently produce wrong comparisons, `__gt`/`__lt`/`__gte`/`__lte` are restricted to predicates the active schema declares `Integer` or `Float` — `QueryBuilder` validates this eagerly (at `.where()` call time, via `SchemaIR.value_type_of()`) and raises `ValidationError` otherwise (no schema registered, predicate undeclared, non-numeric value_type, or a value that doesn't parse as a number). The backend then safely does `CAST(value_lit AS REAL)` (SQLite) / `CAST(value_lit AS DOUBLE)` (DuckDB — this module's own header comment already notes DuckDB's `REAL` is 4-byte single precision, unlike SQLite's always-8-byte `REAL`, which would silently round values).
- **Multiple operators on the same predicate** (e.g. an age range needs both `age__gte` and `age__lt`): `StorageBackend.entities_where`'s `predicate_filters` parameter changed from `dict[str, str]` (predicate → value) to `list[tuple[str, str, Any]]` (`(predicate, operator, value)` triples) — a dict keyed by predicate alone couldn't represent two different operators targeting the same predicate. **Breaking** change to the `StorageBackend` Protocol; any third-party implementation must update. `QueryBuilder._filters` is similarly now keyed by `(property, operator)` rather than bare property, so `.where(age__gte=18).where(age__lt=65)` composes as AND while `.where(name="Ada").where(name="Grace")` still overwrites (last value wins), matching equality's pre-existing behavior.

Both backends' `entities_where()` were refactored from duplicated as_of/non-as_of branches into a single loop building each filter's SQL fragment by operator — a side effect that also removed pre-existing code duplication, not just added new operator branches. `__contains` uses a parameterized `LIKE ... ESCAPE '\'` with `%`/`_`/`\` escaped in the search term, so a literal `%` or `_` in a search value can't act as an unintended wildcard.

New conformance vectors (`conformance/test_where_lookup_operators.py`, cross-backend) prove both backends implement every operator identically, including that numeric comparison is genuinely numeric (`"9" > "10"` lexicographically, `9 < 10` correctly under `__gt`) and that LIKE wildcards are escaped. Unit vectors (`tests/unit/test_query.py`) cover operator composition, the schema-validation error paths, and that the still-rejected cases (relation traversal, unrecognized suffixes) keep failing loudly. All new/updated vectors confirmed to fail without the fix (reverted the four touched `src/` files to pre-KI-039 `main` and reran).

`docs/Ontolith_UseCases_and_Interfaces.md` §4.3's example was restored to `where(text__contains="Compound X")`, since the caveat it carried since KI-030/ADR-0027 no longer applies.

**Review found and this fix now closes**: `__contains` was genuinely not identical across backends as first shipped — SQLite's `LIKE` is case-insensitive by default, DuckDB's is not, so the same `.where(name__contains="ada")` call matched different result sets per backend. `SQLiteBackend` now sets `PRAGMA case_sensitive_like = ON` at connection time to match DuckDB's default rather than the reverse. `.where(x__contains=<non-str>)` and `.where(x__gt=True)` (bool is a subclass of `int`, so `float(True) == 1.0` would otherwise silently accept it) now raise `ValidationError` eagerly instead of a bare `AttributeError` leaking from inside the storage adapter for the former. A leading-dunder key with an empty property name (e.g. `.where(__contains="x")`) is now rejected instead of silently compiling to an unmatchable `"Concept."` predicate — the exact KI-030 failure shape. `_coerce_range_value` now resolves the schema via `get_schema_at()` when `.as_of()` is pinned, not always today's schema (SPEC §11.4) — a predicate retyped since `t` is judged by what it was declared at `t`. ADR-0027 was amended (not superseded — multi-hop traversal remains deferred and its rationale still holds) to record that the lookup-operators half of its deferral is resolved; `docs/Ontolith_SPEC.md` §11.1 and the REST/MCP/CLI filter docs (which had drifted to claim equality-only, since all three splat caller filters straight into `.where()`) were corrected to match.

**Review found and filed separately, not fixed here**: nothing validates that a literal's stored *content* actually parses as its predicate's declared `value_type` (KI-031 only checks the type *token* matches) — `CAST(value_lit AS REAL)` on a non-numeric stored value therefore returns `0.0` on SQLite (wrong, silent) rather than excluding the row; DuckDB uses `TRY_CAST` instead of `CAST` specifically to avoid the alternative failure mode (a raw `duckdb.ConversionException` escaping through the port, violating SPEC §16), but that only prevents the crash, not the underlying cross-backend divergence in which rows match. Filed as **KI-049**.

---

## KI-040 — Nothing validates that a predicate's declared kind (property vs. relation) matches how it's written or filtered ✓ RESOLVED (M3)

**Severity:** Architecture gap — a narrow, currently-theoretical write-time/read-time consistency gap
**Milestone target:** M3 — resolved in `fix(ontology): reject predicate-kind mismatches at write time (KI-040)`
**SPEC reference:** SPEC §4 (Schema — properties and relations are distinct declaration kinds)

### Description

`SchemaIR.has_predicate`/`_resolve_field` (`src/ontolith/schema/ir.py`) resolve a predicate name against *either* `ConceptDef.properties` or `ConceptDef.relations` with no kind check propagated back to the caller. `Ontology._require_known_predicate` (used by both `assert_literal` and `assert_ref`) only checks that the predicate is declared *somewhere* in the schema — nothing stops `assert_literal(subj, "Person.employer", "Acme Corp", "Text", ...)` from writing a literal value under a predicate the schema declares as a relation, or the reverse (`assert_ref` writing a `value_ref` under a property-declared predicate).

This was surfaced during KI-030's review: `entities_where()`'s `value_lit`/`value_ref` union match can't distinguish a predicate's intended kind either — it matches both columns unconditionally for every filter, so if a predicate ever did carry mixed-kind assertions (via the gap above), `.where()` would match across both without the caller being able to tell which kind actually matched. Today this is narrow and mostly theoretical: nothing in the codebase currently writes mixed-kind assertions under one predicate, so the union match is safe in practice, not just in theory-free-today.

### Fix

New `SchemaIR.kind_of(predicate) -> Literal["property", "relation"] | None`, mirroring `value_type_of`'s existing shape (returns `None` for an unresolvable predicate — schema-less namespace or undeclared field). `Ontology._require_known_predicate` gained an `expected_kind` keyword-only parameter; when given, it rejects a kind mismatch with a clear `ValidationError` naming the declared kind and the kind the write actually is. `assert_literal`/`propose` pass `expected_kind="property"`; `assert_ref`/`propose_ref` pass `expected_kind="relation"`. `expected_kind` is passed explicitly by each call site rather than inferred from whether `value_type` is set (which would have worked today, since every literal caller happens to pass both together) — keeping the two checks independently reasoned about rather than coupling them through an incidental correlation. No-op for a schema-less namespace, matching `_require_known_predicate`'s existing precedent for the unknown-predicate and value_type checks. `_replay_proposal_operations` (the shared path behind `accept_proposal`/`resubmit`) deliberately does not re-run `_require_known_predicate` at all — pre-existing precedent this fix inherits unchanged, documented at `resubmit`'s own docstring.

A pre-existing conformance test (`TestValueTypeMismatchRejected::test_relation_declared_predicate_has_no_value_type_to_mismatch`) had itself pinned the gap as "deliberately out-of-scope (KI-040)" and asserted the old, now-incorrect behavior (a literal write under a relation-declared predicate silently succeeding) — updated to assert the new `ValidationError` instead, plus a new `TestPredicateKindMismatch` class (10 cases across both backends) covering all four write paths, the matching-kind regression guard, and the no-schema-registered permissive case. Fixing this also surfaced a **real, pre-existing bug** in two unrelated tests (`TestValidityWindowWriteAPI::test_assert_ref_explicit_window_persisted`/`test_propose_ref_reviewed_explicit_window_survives_replay`): both called `assert_ref`/`propose_ref` against `Person.employer`, a property-declared (not relation-declared) predicate in `test_conflict.py`'s shared `_kb()` schema — silently permitted before this fix, now correctly rejected. `_kb()` gained a genuine `Person.manager -> Person` relation for these tests to target instead. New unit vectors (`tests/unit/test_schema_ir.py::TestKindOf`) cover `kind_of()` directly. All new/changed vectors confirmed to fail without the fix.

`entities_where()`'s optional follow-up — selecting `value_lit` vs. `value_ref` per-filter based on the predicate's declared kind, rather than always matching both via `UNION ALL` — was evaluated and deliberately not done, but not because the union is now redundant: the write-time guarantee this fix adds is per-schema-version and forward-only, so it does NOT make the union provably safe to remove. Three cases keep it load-bearing: (1) schema-less namespaces, where kind can't be known and both writes and reads are explicitly permitted regardless of kind (see `test_permitted_without_a_registered_schema`); (2) schema drift — `apply_schema` enforces only monotonic versioning, not backward compatibility, so a predicate declared a property in v1 and a relation in v2 can genuinely hold both `value_lit` and `value_ref` rows, both real and both queryable via `.include_history()`/`.as_of()`; (3) data written before this fix shipped, which is grandfathered. Tightening the union to a kind-based branch would silently break historical relation/property filters in all three cases — left alone deliberately, not because there's nothing left to protect against.

---

## KI-041 — `RequiredFieldsValidator` doesn't read the schema's `PropertyDef.required`/`RelationDef.required` ✓ RESOLVED (M3)

**Severity:** Architecture gap — a schema author's `required=True` declaration has no observable effect anywhere, including in the one plugin whose job description matches it
**Milestone target:** M3 — resolved via ADR-0029, alongside KI-042
**SPEC reference:** SPEC §4 (`required` — validator-backed, §13), §13.2 (Validator protocol)

### Description

`RequiredFieldsValidator` (`src/ontolith/plugins/reference/required_fields_validator.py`) checks whether an entity has all of a *separately, independently configured* set of required predicates (`required_predicates`, a constructor argument defaulting to `{"Person": ("name",)}`) — it never reads `SchemaIR`/`PropertyDef.required`/`RelationDef.required` at all. A schema author who declares `PropertyDef(name="ssn", value_type="Text", required=True)` gets no enforcement from this plugin unless a deployment separately, manually, redundantly re-declares `"ssn"` in the plugin's own `required_predicates` mapping — the two `required` declarations (schema's and the plugin's) are unrelated in code, easy to let drift out of sync, and nothing points out that they should agree.

Surfaced during KI-031's review (2026-08-04): ADR-0028's first draft justified deferring core-layer `required` enforcement by claiming this plugin "already does this correctly" — it does a presence/absence check, but not *the schema's* presence/absence check.

### Fix

`RequiredFieldsValidator.from_schema(schema: SchemaIR)` — a new classmethod that scans every concept's `properties`/`relations` for `required=True` and collects their bare field names into a `required_predicates` mapping, so a schema's own declaration drives the check instead of a hand-maintained, independently drifting one. `RequiredFieldsValidator(...)`'s existing constructor and its small worked-example default (`{"Person": ("name",)}`) are unchanged — `from_schema()` is an alternative constructor, not a replacement.

This has an observable effect end to end only combined with KI-042's resolution (ADR-0029): a deployment wires `Ontology(completeness_validators=[RequiredFieldsValidator.from_schema(schema)])`, and `accept_proposal` runs it. New tests: `tests/unit/test_reference_required_fields_validator.py::TestFromSchema` covers the derivation itself (properties, relations, `required=False` fields excluded, concepts with no required fields absent from the resulting mapping); `tests/unit/test_ontology_validators.py` covers the end-to-end path through `accept_proposal`.

---

## KI-042 — No code path invokes registered `Validator` plugins; `Validator.validate()` is unreachable ✓ RESOLVED (M3)

**Severity:** Architecture gap — an entire plugin protocol category (`Validator`) is wired to nothing
**Milestone target:** M3 — resolved via ADR-0029
**SPEC reference:** SPEC §13.2 (Validator protocol)

### Description

`PluginRegistry` (`src/ontolith/plugins/registry.py`) registers plugins, resolves their capability-scoped views, and enforces the manifest/capability model (ADR-0015) — but nothing in `src/ontolith/` ever calls `.validate()` on a registered `Validator` plugin. `RequiredFieldsValidator` (KI-010) implements the `Validator` protocol correctly and is registrable, but is never actually run: it isn't consulted during `assert_literal`/`assert_ref`/`propose`/`propose_ref`, `accept_proposal`, or anywhere else. Grepping `src/` for `.validate(` (the protocol's one method) turns up zero call sites outside the protocol/plugin definitions themselves.

Surfaced during KI-031's review (2026-08-04) — ADR-0028's first draft described this plugin as "already wired into the `Validator` protocol," which conflated *implementing* the protocol with being *invoked* by anything.

### Fix

ADR-0029 records the decision and the structural conflict that shaped it: a single "run validators synchronously at write time" mechanism cannot serve both single-assertion validators and `RequiredFieldsValidator`'s whole-entity-completeness shape (see KI-041's own description — an entity built up one assertion at a time is incomplete by construction until its last write). Resolved with two separate, independently configured `Ontology` constructor parameters:

- **`validators: Sequence[Validator]`** — per-assertion, synchronous, blocking, run immediately before an assertion commits, at *every* point one actually does: `assert_literal`, `assert_ref`, `propose`/`propose_ref`'s auto-accept branch, and `_replay_proposal_operations` (shared by `accept_proposal` and `resubmit`'s auto-accept branch) — so a proposal that went through review isn't silently exempt just because it skipped the auto-accept branch. A failing validator raises `ValidationError`, aborting the write.
- **`completeness_validators: Sequence[Validator]`** — whole-entity, run once per distinct subject touched by an accepted proposal's operations, only from `accept_proposal`, after all of that proposal's writes have landed in the same transaction. This is where `RequiredFieldsValidator`/`RequiredFieldsValidator.from_schema()` (KI-041) is meant to be wired. Not run on direct writes or any auto-accept path (`propose`/`propose_ref`/`resubmit`) — those bypass human review, an explicit, accepted scope limitation (see ADR-0029's Consequences), not an oversight. A proposal's `retract` operations count as "touching" their target's subject too (review found the initial version silently excluded them, which would have let a retraction make an entity incomplete again with `accept_proposal` never noticing) — only that subject's `.subject` is contractually meaningful in the completeness path, documented directly on the `Validator` protocol.

`Validator.validate()`'s `kb` parameter type widened from the concrete `ReadOnlyView` to a new minimal structural `ValidatorKbView` Protocol (`plugins/ports.py`) — needed because `Ontology` passes itself (not a `ReadOnlyView`) as `kb` for validators registered this way (trusted the same way `PolicyStrategy` already is, ADR-0018 — not sandboxed, since these are deployment-configured, not `PluginRegistry`-loaded third-party plugins), and typing `Ontology`'s own constructor parameters against the concrete `Validator` protocol without a runtime circular import needed the same structural-Protocol technique `govern/policy.py`'s `KbView` already uses (KI-017, ADR-0025) — `ontology.py` imports `Validator`'s type under `TYPE_CHECKING` only. `PluginRegistry`-loaded validators are untouched by this fix and still have no automatic invocation point of their own (recorded as an explicit follow-up in ADR-0029, not a new KI). SPEC §13.2's literal `kb: ReadOnlyView` signature is deliberately left unmatched, mirroring ADR-0025's identical, already-accepted precedent for `PolicyStrategy`.

New tests: `tests/unit/test_ontology_validators.py` covers both lists across all five commit points (direct writes, auto-accept, reviewed accept_proposal, resubmit's auto-accept correctly running `validators` but never `completeness_validators` — the initial version of this test used a vacuously-passing double and wouldn't have caught a regression, fixed after review to use an always-objecting one), a validator rejecting a write correctly rolling back the transaction (including the proposal state reverting on `resubmit`'s auto-accept), multiple validators/multiple error messages aggregating into one `ValidationError`, a `retract` op correctly re-triggering a completeness failure, a multi-subject proposal (hand-built, since `propose`/`propose_ref` only ever create single-operation proposals) checking each subject independently, and the `assert_ref` branch of proposal replay exercised with a validator configured.

---

## KI-043 — `retract()` lets any `write`-capability principal unilaterally shrink an open contradiction, unlike `resolve_contradiction()` ✓ RESOLVED (M3)

**Severity:** Architecture gap — a review-grade governance action (disturbing a disputed static fact) is reachable at a lower capability floor through a different method
**Milestone target:** M3 — resolved via ADR-0030
**SPEC reference:** SPEC §10.3 (contradiction resolution — routed to review, not auto-resolved)

### Description

`resolve_contradiction()` requires `review`/`admin` capability and blocks AI-kind resolvers (SPEC §10.3: disputed static facts are adjudicated by review, not auto-resolved). `retract()` has no such floor — any human/service principal with plain `write` capability auto-accepts under `ThresholdPolicy` for *any* retraction, including one targeting a `flagged` member of an open contradiction. KI-033 closed the *self-dealing* half of this (a party to the contradiction can no longer retract a member they're a party to — see KI-033, and `_reject_retract_if_party_to_contradiction`), but a **neutral** `write`-capability principal — party to neither disputed value — can still retract one member outright, leaving the contradiction `open` with a `retracted` member and no path back to a normal resolution outcome (KI-034 separately ensures that retracted member stays terminal rather than being resurrected if the contradiction is later extended, but doesn't address the capability floor itself). `conformance/test_contradiction_resolution.py::TestRetractContradictionGuard::test_neutral_third_party_can_still_retract_flagged_member` pins this as current, intentional-for-now behavior.

Found during KI-033's review (2026-08-05): the party-to-contradiction check that review closed the loop on is a narrower guard than the capability floor `resolve_contradiction()` itself enforces, and nothing currently states that gap is deliberate.

### Fix

ADR-0030: retracting a `flagged` member of an open contradiction now requires the same `review`/`admin` + non-AI floor `resolve_contradiction()` enforces — but a principal below that floor is **routed to review, not rejected outright**. `retract()`/`resubmit()` substitute a `RequireReview` decision for whatever `self.policy` would otherwise decide, before ever opening a write transaction, whenever the target is a flagged contradiction member the principal can't clear the floor for. A `review`-capable, non-AI principal can then accept the resulting proposal via `accept_proposal()` — the same review queue every other under-capability proposal already uses.

This two-step design (route-to-review, not raise) replaced an initial version of the fix that raised `CapabilityError` unconditionally — review found that left a `write`-capability principal strictly *worse off* than a merely `propose`-capability one for the identical action: `ThresholdPolicy` auto-accepts `write` unconditionally, so the `write`-capability principal's retraction was flatly rejected with no way to get a proposal into the review queue at all, while a lower-capability principal's retraction sailed through the ordinary review queue untouched. New `Ontology._retract_op_review_override`/`_meets_retract_contradiction_floor`/`_open_contradiction_if_flagged_member` implement the routing; `_require_capability_to_retract_flagged_member` (the original raising check) still exists but only as an authoritative, in-transaction backstop for the narrow race where a contradiction opens concurrently between the optimistic routing check and the write transaction.

The party-to-contradiction guard (KI-033) must run *before* the capability-floor routing, and only within the branch where policy would otherwise auto-accept — getting this ordering right (found via two rounds of test failures during review) preserves KI-033's own established behavior: a party is blocked unconditionally regardless of capability (routing them to review instead would just defer an already-certain rejection), while a low-capability, non-party author's proposal that's already headed to review for unrelated reasons skips both checks at submission time, deferring to `accept_proposal()` — exactly as it always has.

Called from `retract()`'s own would-auto-accept branch and `resubmit()`'s own would-auto-accept branch; `_require_capability_to_retract_flagged_member`'s race-only backstop is threaded a `retracting_principal` pair from `resubmit()`'s already-resolved principal/delegate rather than re-deriving them (an earlier version's re-derivation failed *open* on a missing `acting_as`, found in review).

`conformance/test_contradiction_resolution.py::TestRetractContradictionGuard::test_neutral_third_party_can_still_retract_flagged_member` renamed to `test_neutral_write_capability_principal_is_routed_to_review` and rewritten to assert the `RequireReview` routing followed by a successful `accept_proposal()`; new sibling tests cover a neutral `review`-capability principal still auto-accepting directly, an AI-kind principal being routed to review even with `review` capability (via a test-double `PolicyStrategy`, since the default `ThresholdPolicy` already routes every AI-authored proposal to review before this check would ever run), and delegation attenuation. Five other pre-existing tests in this file that used a `write`-only neutral principal purely as retract() setup (not testing the capability floor itself) were updated to a `review`-capability principal so they keep exercising what they were actually about. `tests/unit/test_retract_contradiction_capability_floor.py` covers the `resubmit`-path routing and directly unit-tests the race-only backstop method (both raise branches, and its no-op for ordinary retraction), since the actual race isn't practical to construct end-to-end.

**Breaking:** a deployment automating retraction of flagged contradiction members with only `write`-capability principals will now get back a `require_review` proposal instead of an immediately-retracted assertion — the retraction doesn't take effect until a `review`-capable, non-AI principal accepts it. Ordinary (non-contradiction) retraction is unaffected. Flagged in CHANGELOG per ADR-0019's policy despite no public signature change, since it's a behavior-level break.

---

## KI-044 — `resolve_contradiction()` can pick an already-`retracted` member as the winner, reactivating it to `active` ✓ RESOLVED (M3)

**Severity:** Architecture gap — a governed lifecycle action produces a semantically odd result (an `active` assertion with a closed `valid_to` window) for a state combination that didn't used to persist long enough to reach it
**Milestone target:** M3 — resolved via ADR-0031
**SPEC reference:** SPEC §10.3 (contradiction resolution — winner reactivation)

### Description

`resolve_contradiction(contradiction_id, winner_assertion_id, resolver)` gates winner eligibility purely on `winner_assertion_id in contradiction.member_ids` (SPEC §10.3) — it does not check the candidate's current `status`. Since KI-034 now keeps a `retracted` member's id in `Contradiction.member_ids` deliberately (that list is also `_reject_retract_if_party_to_contradiction`'s scan set, not audit-only), a resolver can pick that already-`retracted` member as the winner: its status flips to `active` (`ontology.py`, the `self.backend.set_assertion_status(winner_assertion_id, "active")` line), the value reappears in default query results, but `valid_to` stays closed at the original retraction time. That's not an impossible field combination on its own (an author-declared `valid_to` produces `active` + a closed window too) — what's wrong is that it silently undoes a *governance action's* close of that window (the retraction) with no new write recording that it reopened; `valid_to` still points at whatever closed it, now contradicted by `status=active`. Pre-existing (not introduced by KI-034), but KI-034 is what makes a `retracted` member persist inside an *open* contradiction long enough for a resolver to plausibly reach this path — before that fix, an extension would have already resurrected it back to `flagged`, an equally wrong but different outcome.

Found during KI-034's review (2026-08-05).

### Fix

ADR-0031: `resolve_contradiction()`'s winner-eligibility check now excludes members whose current status is `retracted` **or `superseded`**, raising `ValidationError` — the same exception type as the existing "winner not a member" check. The check runs only after the full party-to-contradiction loop has passed for every member (deterministic error precedence — a resolver who is both a party and picks a terminal-status winner always sees `CapabilityError`, never `ValidationError`, regardless of member iteration order), reusing the already-fetched winner object rather than a second lookup.

Review of the first version of this fix found `retracted`-only wasn't sufficient: `flag_contradiction()` already accepts a `superseded` assertion as a contradiction member by design (its own inline comment: "a flagged/superseded assertion must still be resolvable here"), and `_apply_with_conflict_routing`'s KI-034 extension-branch fix had only ever skipped re-flagging a `retracted` member, not a `superseded` one — so extending an open contradiction could flip a `superseded` member's status back to `flagged`, un-terminalizing it before `resolve_contradiction()`'s check ever saw it. Both gaps are closed together: the extension branch now skips `retracted` **and** `superseded` members, matching `flag_contradiction()`'s own already-correct handling.

Retraction/supersession is now terminal everywhere `resolve_contradiction()` and the write paths that decide a contradiction member's status are concerned — the winner check, the extension branch (KI-034's `retracted`-only fix widened to also cover `superseded`), and `flag_contradiction()`'s own already-both-aware re-flag guard. `resolve_contradiction()`'s own loser loop was one more site needing the same widening (found in the same review pass): it now skips a `superseded` loser the same way it already skipped a `retracted` one, instead of overwriting it to `retracted` and recording a second, resolver-misattributed event. **Not closed by this fix**, found in a later review pass and tracked separately: `retract()` itself can still overwrite a `superseded` contradiction member to `retracted`, bypassing the KI-033 party guard and KI-043 capability floor — see KI-051. A resolver who wants a terminal winner-candidate value active again has no reactivation path via `resolve_contradiction()` — they must submit it as a new assertion, which flows back through ordinary SPEC §10 conflict routing.

New conformance vectors in `conformance/test_contradiction_resolution.py` pin: picking an already-retracted or already-superseded member as winner raises `ValidationError`; the rejection runs before any write; picking a still-`flagged` member remains unaffected by the new check; (deterministic precedence) a resolver who is both a party and picks a terminal-status winner sees the party `CapabilityError`, not the terminal-status `ValidationError`; extending an open contradiction no longer resurrects a `superseded` member back to `flagged` (mutation-tested — the code fix alone left the suite green, so this vector was added specifically to close that gap); and no duplicate event for an already-superseded loser.

A contradiction whose every *existing* member ends up `retracted`/`superseded` has no eligible winner until a fresh assertion joins it — not fixed here, and not a permanent dead end either (a fresh assertion always resolves this, see below). The escape hatch is **predicate-shape-dependent, corrected during review**: for a `static` predicate, asserting the intended value again is enough (it rejoins the same contradiction as a new `flagged` member automatically). For a `time_varying` predicate — the only kind a predicate can be at the moment an assertion is superseded (`_apply_with_conflict_routing`'s `Supersede` branch is the sole place `superseded` is ever written) — a bare re-assert does NOT rejoin the contradiction (it comes back `active` and untracked, since the extension shortcut is `static`-only); `flag_contradiction()` is the actual remedy there. Separately, `flag_contradiction()` can create a *brand-new* contradiction whose two founding members are already both terminal — not permanently unresolvable either (same fresh-assertion remedy applies), but starting with zero eligible winners is itself worth preventing at creation time — not addressed here, filed separately as KI-050. `retract()` itself bypassing the party/capability guards for a `superseded` member is a separate gap again, filed as KI-051.

**Breaking:** a resolver who was relying on picking an already-retracted or already-superseded member to reactivate it now gets `ValidationError` instead. Flagged in CHANGELOG per ADR-0019's policy despite no public signature change, matching KI-043's precedent for this class of behavior-level break.

---

## KI-045 — `flag_contradiction()` reads its target assertions and any existing open contradiction before opening its write transaction (TOCTOU) ✓ RESOLVED (M3)

**Severity:** Architecture gap — same bug shape as KI-035, on a method KI-035 didn't touch
**Milestone target:** M3 — resolved in `fix(ontology): close flag_contradiction()'s TOCTOU race (KI-045)`
**SPEC reference:** SPEC §9.1 (proposal state machine); SPEC §10.3 (contradiction resolution)

### Description

`Ontology.flag_contradiction(assertion_id_a, assertion_id_b, author, rationale=None)` reads both target assertions and looks up any existing open contradiction for their `(subject, predicate)` — all before opening `with self.backend.transaction():`. Two concrete consequences of deciding from that stale snapshot:

- The KI-034 terminal-status guard (skip re-flagging a `retracted`/`superseded` assertion) tests a `status` read before the transaction. A concurrent `retract()` or supersession landing in the gap means the guard can still see a stale `active` status and re-flag an assertion that's since become terminal — the exact resurrection KI-034 closed, reachable via this race at only `propose` capability (including via MCP `ontolith.flag_contradiction`, which carries no write capability at all).
- The pre-fetched `existing` (open) contradiction is equally stale: a concurrent `resolve_contradiction()` that closes it in the gap leaves this call calling `update_contradiction_members()` on a now-`resolved` contradiction (no state guard on that write) and re-flagging the just-reactivated winner back into dispute.

Found during KI-035's review (2026-08-05) — KI-035 itself scopes to the four proposal-transition methods (`accept_proposal`/`reject_proposal`/`request_changes`/`resubmit`) and deliberately didn't expand to cover this separate method.

### Fix

Mechanically identical to KI-035's fix: the assertion reads, the subject/predicate check, and the existing-open-contradiction lookup all moved to the first statements inside `flag_contradiction()`'s own `with self.backend.transaction():` block, re-read fresh rather than trusted from the pre-transaction snapshot. Only the principal/capability check stays outside the transaction — verified (review) no code path in either backend ever mutates a principal's `default_capability` after creation, so this is provably race-free today, not merely conventionally excused; if a principal-mutation port is ever added, this check needs to move inside too.

New conformance vectors in `conformance/test_contradiction_resolution.py::TestFlagContradictionTOCTOU`, using the same `_RacingClock` technique KI-035 introduced (`conformance/test_review_workflow.py`, redefined locally in this file matching its own self-contained style): a concurrent `retract()` landing in the gap is no longer resurrected to `flagged`; a concurrent `resolve_contradiction()` closing the only existing open contradiction in the gap is no longer extended (the call correctly starts a *new* contradiction instead, using two non-member assertions so the resulting `member_ids` change is actually discriminating — an earlier version of this vector reused the original contradiction's own members, making the assertion pass vacuously regardless of whether the fix worked, found in review); a concurrent `flag_contradiction()` opening a competing contradiction for the same `(subject, predicate)` in the gap is correctly found and extended rather than raced into a second, independently-open contradiction `get_open_contradiction()` could never fully see again (found missing in review). All three vectors confirmed to fail without the fix (reverted locally and re-run to verify, across both backends).

---

## KI-046 — `DuckDBBackend` has no equivalent of `SQLiteBackend`'s concurrency lock (KI-023) ✓ RESOLVED (M3)

**Severity:** Architecture gap — backend-specific correctness gap; concurrency-dependent fixes (e.g. KI-035) are only airtight on SQLite today
**Milestone target:** M3 — resolved via ADR-0032
**SPEC reference:** Implementation Plan (conformance kit: both backends must satisfy the same guarantees)

### Description

`SQLiteBackend.begin()` (`store/sqlite/backend.py`) acquires a `threading.RLock` and holds it across the full span of an explicit transaction (KI-023) — a losing thread in a race blocks in `begin()` until the winner commits or rolls back, then re-reads and provably sees the winner's committed state. `DuckDBBackend.transaction()`/`begin()` (`store/duckdb/backend.py`) has no equivalent lock and no `_in_transaction` guard at all — two concurrent transitions on the same connection don't serialize the way SQLite's do; a second `BEGIN TRANSACTION` on the shared connection while one is already open is unguarded from the Python side.

Concrete consequence: KI-035's proposal-transition TOCTOU fix (and any future fix relying on the same "the transaction serializes concurrent callers" reasoning) is airtight for `SQLiteBackend` but not proven for `DuckDBBackend` — KI-035's own conformance vectors are parametrized over both backends via mocked/single-threaded racing (`_RacingClock`), which doesn't exercise real concurrency and so can't surface this gap either way.

Found during KI-035's review (2026-08-05).

### Fix

ADR-0032: `DuckDBBackend` now holds a `threading.RLock` (`self._lock`), structurally identical to `SQLiteBackend`'s — `begin()` acquires it for the full span of an explicit transaction, and every other public method (the same 40 methods `SQLiteBackend` decorates, verified name-by-name) carries the same `@_synchronized` decorator. `commit()`/`rollback()` release the lock with the same asymmetric pattern KI-023 established (`commit()` releases only on success, to avoid double-releasing when `transaction()`'s `except` clause calls `rollback()` next after a failed `commit()`).

Verified before deciding, not assumed: DuckDB's Python driver reports `duckdb.threadsafety == 1` ("threads may share the module, but not connections") — the identical constraint driving `SQLiteBackend`'s own KI-023 fix, confirming the RLock mirror (rather than a hypothetical per-thread-cursor redesign) was the right call, not just the simplest one.

One deliberate divergence from `SQLiteBackend`: no `_in_transaction` flag. `SQLiteBackend` needs one because it tracks whether to auto-commit a standalone write in its own `isolation_level=None` autocommit mode; `DuckDBBackend` never had this pattern since DuckDB's own native autocommit already makes standalone writes durable without one. `begin()`/`rollback()` also gained the same `try`/`except duckdb.Error → StorageError` wrapping `SQLiteBackend`'s already had (a smaller, adjacent inconsistency — DuckDB's `begin()` previously let a raw `duckdb.Error` escape uncaught — fixed alongside since this exact code was already being rewritten).

New `tests/unit/test_duckdb_backend.py::TestConcurrency`, mirroring `test_sqlite_backend.py::TestConcurrency`'s shapes (real `ThreadPoolExecutor` + `threading.Barrier`/`threading.Event` contention, not mocked). Four of its five tests confirmed, by reverting the fix and rerunning, to fail against the pre-fix code. The one exception (concurrent standalone, non-transactional *writes*) does **not** independently prove pre-fix risk — confirmed by reverting and rerunning it five times, it passed every time even without the lock: `put_entity()` is a single `execute()` call with no follow-up fetch, and DuckDB's own connection object empirically guards a bare execute-with-no-fetch internally even without external locking. Kept for structural parity with `SQLiteBackend`'s equivalent test, with its docstring explicit about not being independent proof.

**The worst pre-fix consequence, found in review, was not what this entry's Description above leads with — it was silent data corruption on concurrent *reads*, with no exception at all**, not merely an unguarded multi-statement transaction span. Every read method here is an `execute()`-then-`fetch()` pair; `duckdb.DuckDBPyConnection.execute()` returns the connection object itself, so the pending result set is *connection* state a concurrent `execute()` from another thread can clobber mid-read. Reproduced pre-fix: 16 concurrent readers against 50 pre-seeded entities returned as few as 0-1 of the 50 rows from `entities()` (never raising), and `get_entity()` returned `None` for entities that exist — see the new `test_concurrent_standalone_reads_do_not_return_corrupted_results`. Wrong data with no signal anything went wrong is worse than the raised-exception failure modes the other tests demonstrate, since no caller or error-taxonomy mapping could ever detect it.

---

## KI-047 — `.trust_at_least()` ignores delegation attenuation (SPEC §8.4) ✓ RESOLVED (M3)

**Severity:** Architecture gap — a query-time trust check can disagree with the policy engine's own trust semantics for the same assertion
**Milestone target:** M3 — resolved via ADR-0033
**SPEC reference:** SPEC §8.4 (effective capability under delegation — the `min()` rule this fix extends by analogy to trust; SPEC itself states it for capability only)

### Description

`entities_meeting_trust` (`store/sqlite/backend.py`, `store/duckdb/backend.py`) joins `assertion.author = principal.id` and compares `principal.trust_level` directly. But an assertion made under delegation (`acting_as` set) has its *effective* trust attenuated — `govern/policy.py`'s `ThresholdPolicy`/`SourceQuorum` compute `min(principal.trust_level, acting_as.trust_level)` for exactly this reason, by analogy with SPEC §8.4's capability rule ("the effective capability for the operation is `min(capability(author), capability(acting_as))`" — the same rule this project already applies to *capability*, `ontology.py:_check_direct_write_capability`; SPEC itself does not state a `min()` rule for trust). `entities_meeting_trust` reads only `author`'s raw `trust_level` and never looks at `acting_as` at all, so a low-trust delegate acting as a high-trust principal is scored as fully trusted by `.trust_at_least()` even though the policy engine that decided whether to auto-accept that same assertion would have scored it lower (or vice versa: a high-trust delegate acting as a low-trust principal is scored as low-trust by the query filter, though `govern/policy.py`'s own `min()` rule agrees with that direction).

Pre-existing since `.trust_at_least()` first shipped; not introduced or worsened by KI-028 or KI-036, both of which touch how the underlying query is scoped/bitemporally filtered but neither of which reads `acting_as`. Found during KI-036's review while double-checking the "trust_level is exact, not an approximation" claim that fix's docstrings make — that claim is true for whether `trust_level` needs bitemporal reconstruction, but doesn't cover this separate gap in which column the query reads.

### Fix

ADR-0033: `entities_meeting_trust` (both `store/sqlite/backend.py` and `store/duckdb/backend.py`) now `LEFT JOIN`s a second `principal` alias (`delegate`) on `assertion.acting_as = delegate.id`, and filters on `min(p.trust_level, COALESCE(delegate.trust_level, p.trust_level)) >= min_trust` — matching `govern/policy.py`'s `min(principal.trust_level, acting_as.trust_level)` effective-trust formula exactly, by analogy with SPEC §8.4's capability rule (SPEC states the `min()` rule for capability only; the trust-min is this codebase's own conservative extension of it). No `acting_as IS NULL` special case is needed: `COALESCE` falls back to the author's own `trust_level` when there's no delegate, and `min(x, x) == x`, so a non-delegated assertion is scored identically to before.

One cross-backend divergence, verified empirically before implementing (same pattern as KI-039's `TRY_CAST`/`CAST` split): SQLite's `min(a, b)` is the scalar two-argument form, but DuckDB's `min(a, b)` is aggregate-only and returns a list when given two scalar arguments (`duckdb min(3,5)` → `[3]`, not `3`). DuckDB's scalar two-arg minimum is `least(a, b)` (`duckdb least(3,5)` → `3`). SQLite's query uses `min(...)`; DuckDB's uses `least(...)`, with a comment cross-referencing this entry and noting the two functions aren't NULL-equivalent (unreachable today since `trust_level` is `NOT NULL` on both backends and `COALESCE` already excludes the no-delegate case).

A dangling `acting_as` (no resolvable delegate — unreachable via `Ontology`'s own write paths, since `_resolve_delegation` always validates the delegate exists first, but reachable via a direct `put_assertion()` call) falls back to the author's own `trust_level` rather than excluding the row or raising — deliberately the opposite of `_resolve_delegation`'s own fail-closed behavior for the same input, since query-time is not the place to reject data that was already accepted at write time. Now a normative part of the `StorageBackend.entities_meeting_trust` port contract, pinned by a new conformance vector constructing the fixture directly (`test_dangling_acting_as_falls_back_to_author_trust_level`) — found during this fix's own review.

`QueryBuilder.trust_at_least()`'s and `StorageBackend.entities_meeting_trust`'s docstrings now describe "effective trust_level" and spell out the delegation formula, rather than implying the author's raw `trust_level`. New conformance vectors (`conformance/test_confidence_trust_filters.py::TestTrustAtLeastDelegationAttenuation`) cover both attenuation directions (low-trust delegate acting as a high-trust principal, and the reverse — the direction that actually discriminates the bug, confirmed by re-running against pre-fix code), the inclusive threshold boundary (oriented so it too discriminates against pre-fix code, not just the non-discriminating pairing), non-delegated assertions being unaffected, and the dangling-delegate fallback.

**Breaking (observable, not signature-level):** any caller relying on `.trust_at_least()` matching a delegated assertion by the author's raw `trust_level` alone will now see it filtered by the (possibly lower) effective value instead.

---

## KI-048 — CLI has no `schema migrate` command ✓ RESOLVED (M3)

**Severity:** Architecture gap — SPEC-normative CLI surface remains partially unimplemented
**Milestone target:** M3 — resolved via ADR-0034
**SPEC reference:** SPEC §14.2 (`ontolith schema {show|migrate}`)

### Description

KI-038 added `ontolith schema show` but explicitly left `ontolith schema migrate` unimplemented, forward-tracked here per that entry's own Fix text ("split into its own issue if `show` lands first"). Schema versioning/migration isn't implemented anywhere in the codebase yet — `Ontology.apply_schema` enforces strict monotonic version numbering (`current_latest + 1`) but has no concept of a migration plan, diffing between versions, or data backfill; there is no `StorageBackend` port method or SDK call for anything migration-shaped, on any interface (SDK, REST, MCP, CLI).

### Fix

ADR-0034: scoped to (a) only — `ontolith schema migrate <file> --author <admin>` is a thin CLI wrapper reading a LinkML-aligned YAML document (ADR-0013 dialect) from disk and applying it via the existing governed `Ontology.apply_schema` path. No new domain logic, no new `StorageBackend` port method; `apply_schema`'s existing strict-monotonic version check is unchanged (the file must already declare the correct next version — not auto-incremented). Errors (missing file, malformed YAML, wrong version, insufficient capability) all funnel through the CLI's existing generic error handler, matching every other command's convention.

Format is YAML-only, not class-DSL — there is no existing mechanism to load a `SchemaIR` from a class-DSL *file path* (`compile_schema()` takes already-imported Python classes, not a file to read), and building one would mean dynamically executing arbitrary user-supplied Python from a CLI argument, a materially riskier capability SPEC §14.2 didn't ask for. A class-DSL schema still reaches this command via the existing `compile_schema()` → `to_yaml()` round-trip.

Scope (b) — actual data migration/backfill against already-stored assertions when a schema changes — remains explicitly out of scope, tracked as its own future decision per ADR-0034's Consequences section, not folded into this command.

---

## KI-049 — A predicate's declared `value_type` token is checked at write time (KI-031), but the literal's actual string content is never validated to match it ✓ RESOLVED (M3)

**Severity:** Architecture gap — declared schema constraints can silently diverge from stored data
**Milestone target:** M3 — resolved via ADR-0028's amendment
**SPEC reference:** SPEC §4 (`value_type` is a core type); Implementation Plan §4.3 ("validate at edges")

### Description

KI-031 made `_require_known_predicate` (`src/ontolith/ontology.py`) reject a literal write whose caller-supplied `value_type` *token* doesn't match the schema's declared `value_type` for that predicate (e.g. writing `value_type="Text"` against a predicate declared `Integer`). It does not — and never has — validated that `value` itself is actually well-formed for that type. `kb.assert_literal(entity.id, "Person.age", "unknown", "Integer", author)` succeeds today: `value_type="Integer"` matches the schema's declaration, so the token check passes, even though `"unknown"` is not a valid integer. The same gap exists for `Date`/`DateTime`/`URI`/`JSON` — nothing parses `value` against its claimed format.

Found during KI-039's review: `.where()`'s new `__gt`/`__lt`/`__gte`/`__lte` range operators trust a predicate's declared `value_type` (via `SchemaIR.value_type_of()`) to decide whether `CAST(value_lit AS REAL/DOUBLE)` is safe, but "declared numeric" and "actually stored as parseable numeric text" are different guarantees — this gap is what lets them diverge. Also reachable via schema evolution: a property declared `Text` in schema v1, retyped to `Integer` in v2 (nothing re-validates or migrates existing rows written under v1 — see KI-048's own note that no migration/backfill mechanism exists at all).

### Fix

ADR-0028's 2026-08-26 amendment: `_require_known_predicate` gained an optional `value` parameter alongside its existing `value_type`, and a new `_validate_literal_value(value, value_type)` parses `value` against the schema-confirmed type — same two call sites as the token check (`assert_literal`, `propose`), same "not re-run at `_replay_proposal_operations`" precedent the token check and the KI-040 kind check already established. A dedicated regex for `Integer`/`Float` — deliberately **not** bare `int()`/`float()`, which accept underscore separators, surrounding whitespace, non-ASCII decimal digits, and (`float()`) `"inf"`/`"nan"`, none of which cast consistently across both backends' KI-039 SQL `CAST`/`TRY_CAST` paths (verified empirically, including a 20,000-case fuzz comparison across both backends during review, finding zero divergences under the tightened regex); `date.fromisoformat()`/`datetime.fromisoformat()` for `Date`/`DateTime` (Python's `fromisoformat` grammar, not exactly ISO 8601 in either direction; `Date` rejects a string carrying a time component — its accepted set is a strict subset of `DateTime`'s, not disjoint from it); `json.loads()` for `JSON`, additionally rejecting the non-standard `NaN`/`Infinity`/`-Infinity` constants Python's parser accepts by default (RFC 8259 has neither).

`Boolean` decision: case-insensitive `"true"`/`"false"` only — explicitly not `"1"`/`"0"`, to avoid blurring the line with `Integer` (asked and decided, not the only defensible choice).

`URI` decision: SPEC's `URI` maps to LinkML's `uriorcurie` (ADR-0013), so both a full URI (`scheme://...`) and a CURIE (`prefix:local-name`) must validate — the check requires only a non-empty segment on both sides of the first `:`, not a strict RFC 3986 parse.

Retroactivity: as anticipated, not retroactive — enforced at submission time only, same as the token check beside it (no migration mechanism exists — KI-048). `entities_where()`'s `TRY_CAST`/`CAST` defensive handling (KI-039) is **not removed** — it remains necessary for data written before this fix, even though new writes can no longer produce it.

Conformance vectors (`conformance/test_conflict.py::TestLiteralContentValidation`): accept/reject boundary for all eight `value_type`s, both literal-write entry points, the no-schema-registered pass-through, and the not-re-validated-at-replay guarantee.

---

## KI-050 — `flag_contradiction()` can create a contradiction whose two founding members are already both terminal, with no eligible winner among them ✓ RESOLVED (M3)

**Severity:** Architecture gap — a `propose`-capability action can open a `Contradiction` `resolve_contradiction()` can't immediately close, requiring a follow-up write before it's resolvable at all
**Milestone target:** M3 — resolved via ADR-0035
**SPEC reference:** SPEC §10.3 (contradiction resolution — winner reactivation), §14 (MCP `ontolith.flag_contradiction`)

### Description

`flag_contradiction(assertion_id_a, assertion_id_b, author)` validates that the two named assertions share a subject and predicate, and — since KI-034/KI-044 (ADR-0031) — deliberately accepts either (or both) already being `retracted`/`superseded`, so it can name a terminal assertion when extending an open contradiction. Nothing stops both of the two assertions passed at *creation* time from already being terminal: `flag_contradiction(retracted_id, superseded_id, author)` opens a brand-new `Contradiction` whose two founding `member_ids` are both ineligible winners under ADR-0031's check — `resolve_contradiction()` fails against every existing member immediately.

**Correction (found in this KI's own third review pass, 2026-08-21): the contradiction is not permanently unresolvable, and an earlier draft of this entry — and of ADR-0031's Consequences — said so incorrectly.** A third assertion for the same `(subject, predicate)` reaches it exactly as ADR-0031 describes for any all-terminal contradiction: for a `static` predicate, a plain `propose()`/`assert_literal` extends the same open contradiction automatically; for a `time_varying` predicate (reachable here too — `flag_contradiction()` itself is not restricted to `static` predicates the way auto-detected, conflict-routing-opened contradictions are, SPEC §10.1), a second `flag_contradiction()` call naming the fresh assertion alongside an existing member is the remedy, per ADR-0031. The real gap is narrower: `flag_contradiction()` lets a caller open a contradiction that starts with *zero* eligible winners, forcing that follow-up write before `resolve_contradiction()` can ever succeed — a state a *caller* has to know to work around, not a state nothing can recover from.

Reachable at only `propose` capability, including by an AI principal over MCP (`ontolith.flag_contradiction` carries no write capability at all) — a low-privilege or automated caller can create a contradiction with no immediately eligible winner, with no elevated action required.

Found during KI-044/ADR-0031's second review pass (2026-08-19), while confirming ADR-0031's escape-hatch claim; this entry's own overclaim (see Correction above) found during the third review pass (2026-08-21). Also reachable by race, not just by an explicit two-terminal-ids call (found during KI-045's review, 2026-08-21): if a concurrent `retract()`/supersession terminalizes *both* of the two assertions named in a `flag_contradiction()` call between its (KI-045-fixed) fresh read and the write, the terminal-status guard correctly declines to re-flag either — but the call still opens a brand-new contradiction with two now-terminal members and zero eligible winners, this KI's exact scenario. Not a regression from KI-045 (the pre-fix behavior was worse: resurrection), and doesn't change this KI's own Fix.

### Fix

ADR-0035: `flag_contradiction()` now requires *at least one* of the two named assertions to be non-terminal (`active` or `flagged`) when opening a **new** contradiction — both already `retracted`/`superseded` raises `ValidationError`, mirroring `resolve_contradiction()`'s own winner-eligibility check (KI-044, ADR-0031). Checked right after the fresh, in-transaction reads of both assertions (KI-045), so it also covers the race scenario noted above, not just an explicit two-terminal-ids call. Placed before `id_provider.next()` so a rejected call doesn't consume an ID for a contradiction that's never persisted.

Extending an *already-open* contradiction with an all-terminal pair remains permitted, unchanged — the new check only guards the `else` (create) branch, never the `if existing is not None` (extend) branch, per ADR-0031's own deliberate escape hatch.

Conformance vectors (`conformance/test_contradiction_resolution.py::TestFlagContradictionRequiresEligibleWinner`): both founding members retracted, one retracted + one superseded (matching this KI's own motivating pairing), a regression guard confirming the ordinary one-terminal-one-active case still creates successfully, and confirming extension of an already-open contradiction with an all-terminal pair still succeeds.

**Breaking (observable, not signature-level):** a caller relying on `flag_contradiction()` succeeding with two already-terminal assertion IDs to open a new contradiction now gets `ValidationError` instead.

---

## KI-051 — `retract()` can overwrite an already-`retracted`/`superseded` contradiction member, bypassing both the party guard and the capability floor ✓ RESOLVED (M3)

**Severity:** Architecture gap — a governance action (KI-033's self-dealing guard, KI-043's capability floor) can be bypassed for any terminal-status member, not just a `flagged` one
**Milestone target:** M3 — resolved via ADR-0030's amendment
**SPEC reference:** SPEC §10.3 (contradiction resolution), §5 (append-only lifecycle)

### Description

`_reject_retract_if_party_to_contradiction` (KI-033) and `_open_contradiction_if_flagged_member`/`_meets_retract_contradiction_floor` (KI-043, ADR-0030) both early-return — treating the retraction as ordinary, ungoverned — whenever the target assertion's status is anything other than `flagged`. **Correction (found in this KI's own fourth review pass, 2026-08-21): this was already an incomplete guard before ADR-0031, not a safe simplification as an earlier draft of this entry claimed.** `retract()` has no already-terminal idempotency check of its own — nothing stops re-retracting an already-`retracted` target, and doing so records a second, misattributed event exactly like the one `resolve_contradiction()`'s loser-loop fix (KI-044/ADR-0031) exists to prevent on the resolution side. So a party to the dispute could already bypass KI-033's guard against an already-`retracted` member, at only `write` capability, before ADR-0031 ever shipped. ADR-0031 adds a second, worse dimension on top: `superseded` is now a designed, documented status a contradiction member can hold while its contradiction is still `open` (via `flag_contradiction()`, which accepts a `superseded` assertion by design), and overwriting *that* to `retracted` doesn't just misattribute an event — it destroys the record of *how* the assertion actually became terminal (supersession vs. explicit retraction). Neither guard was ever updated to recognize either case — both still only check `status == "flagged"`/`status != "flagged"`, keyed off "is this the one true terminal-adjacent status" rather than "does this assertion belong to an open contradiction at all."

Verified: a principal who is a party to the contradiction (author of one of the disputed values), with only `write` capability, calls `retract()` on a `retracted` *or* `superseded` member of an *open* contradiction — it auto-accepts directly, with no party check and no review/admin floor ever applied. This does not reopen KI-044 itself (`retracted` and `superseded` are both already terminal, so `resolve_contradiction()`'s winner check still correctly rejects the result either way), and `_retraction_valid_to` correctly refuses to widen an already-closed validity window, so no temporal corruption results in either case.

Found during KI-044/ADR-0031's third review pass (2026-08-21); the `retracted`-target half of the gap, and the false "already excluded by idempotency" framing of an earlier draft, found during the fourth review pass (2026-08-21).

**Correction (found in this KI's own fix review, 2026-08-27): "destroys the record of how the assertion actually became terminal" overstates what the shipped fix's own idempotency scoping leaves possible.** The Fix below deliberately does *not* extend `retract()`'s no-op to an already-`superseded` target (only to an already-`retracted` one) — a non-party, floor-meeting principal explicitly calling `retract()` on a `superseded` contradiction member still transitions it to `retracted`, same as before this KI. What changes is that this transition is now *governed* (party guard + capability floor both apply), not that it's prevented outright. The event log is not actually destroyed either way: `_record_assertion_event` appends, it never overwrites, so a `superseded`-then-explicitly-`retracted` assertion's event trail still reads `["superseded", "retracted"]` with each event's own actor — the *summary* `status` column advances to `retracted`, but provenance for how it got there survives intact. See the Fix section for the actual scoping and why.

### Fix

ADR-0030 amendment: `_open_contradiction_if_member` (renamed from `_open_contradiction_if_flagged_member`) and the equivalent inline check in `_reject_retract_if_party_to_contradiction` now key off contradiction membership alone — any current or former member of a still-`open` contradiction, regardless of the member's own status — instead of gating on `status == "flagged"` before ever checking membership. `_require_capability_to_retract_contradiction_member` (renamed from `..._flagged_member`) inherits the fix automatically, since it's built on the same shared membership check. Both KI-033's party guard and KI-043's capability floor now apply to a `retracted`/`superseded` contradiction member exactly as they already did to a `flagged` one.

Separately, `retract()` and `_replay_proposal_operations`'s `retract` op branch now no-op (no `set_assertion_status`/event write) when re-retracting a target that's already exactly `retracted` — closing the narrower event-misattribution gap independent of contradiction membership. **Deliberately narrower than `resolve_contradiction()`'s own loser-loop no-op (KI-044, ADR-0031), which also skips an already-`superseded` loser**: that loop is an automatic side effect of picking a winner, not a call the user directly targeted at that assertion, whereas explicitly retracting a `superseded` assertion via `retract()` is a distinct, legitimate transition this codebase already treats as worth its own event (`test_events_ordered_oldest_first`, `test_retract_never_widens_an_already_closed_valid_to`) — verified this fix doesn't break either of those pre-existing tests.

New conformance vectors: `conformance/test_contradiction_resolution.py::TestRetractGuardsApplyToTerminalMembers` (party guard blocks retracting an already-`retracted` or already-`superseded` opposing member; a below-floor neutral principal retracting a terminal member is routed to review, not auto-accepted) and `::TestRetractAlreadyTerminalIdempotency` (re-retracting records no second event and doesn't widen `valid_to`; the same no-op applies when the retract op is replayed via `accept_proposal`).

**Breaking (observable, not signature-level):** a caller relying on `retract()` auto-accepting against a `retracted`/`superseded` contradiction member with no governance check (bypassing the party guard or capability floor) now gets the same `CapabilityError`/`RequireReview` routing a `flagged` member already had.

---

## KI-052 — GraphQL resolvers are synchronous and block the ASGI event loop under concurrent load ✓ RESOLVED (M3)

**Severity:** Performance — measured, real production impact under concurrency; not a correctness or security gap
**Milestone target:** Backlog — resolved as a follow-up fix, not blocking anything
**SPEC reference:** SPEC §14.3 (REST + GraphQL)

### Description

Every resolver in `src/ontolith/interfaces/graphql.py` (`interfaces/graphql.py`, ADR-0037) is a plain synchronous function. REST's routes (`interfaces/rest.py`, ADR-0021) are also synchronous, but Starlette dispatches ordinary `def` route handlers to a thread pool automatically; `strawberry.fastapi.GraphQLRouter` executes resolvers inline on the ASGI event loop instead. A slow resolver — a large query, a cold vector index, a lock wait — blocks the event loop for the full duration of every concurrent request, not just the ones touching the database, unlike REST's equivalent.

Measured directly during ADR-0037's second review round: three concurrent requests against a deliberately slowed (0.3s) resolver serialized to ~0.92s total under GraphQL, versus ~0.31s under REST's equivalent (the three requests overlap). Real-world impact is softened, though not eliminated, by `store/sqlite/backend.py`'s own process-wide write lock already serializing concurrent DB *writes* regardless of interface — the added harm here is event-loop starvation blocking *all* traffic (including fast, non-DB requests and socket servicing), not just DB-bound ones.

### Fix

Every `Query`/`Mutation` field resolver, plus `EntityType.assertions` and `create_graphql_app`'s `_get_context`, is now `async def`. `_require_principal`/`_kb` (cheap dict lookups) still run inline; every call that touches `kb`/`kb.backend` — the actual blocking SQLite/DuckDB I/O — was factored into a plain sync helper function (`_build_schema`, `_build_entity`, `_execute_query`, `_build_provenance`, `_list_proposals`, `_list_contradictions`, `_list_principals`, `_do_propose`, `_do_accept_proposal`, `_do_reject_proposal`, `_do_request_changes`, `_do_resubmit_proposal`, `_do_flag_contradiction`, `_do_resolve_contradiction`, `_build_assertions`) and dispatched via `starlette.concurrency.run_in_threadpool`. `_get_context`'s `auth_provider.resolve(token)` call is offloaded the same way, since token resolution is also a backend-backed lookup.

Re-measured after the fix with the same deliberately-slowed-resolver setup: three concurrent requests now complete in ~0.33s, matching REST's ~0.31s (overlapping) rather than the previous ~0.92s (serialized). New regression test
`tests/unit/test_graphql.py::TestResolverConcurrency::test_concurrent_requests_overlap_instead_of_serializing` pins this — it uses `httpx2.AsyncClient` + `ASGITransport` with real `asyncio.gather` concurrency (FastAPI's `TestClient` runs everything through a single background portal thread and doesn't exercise genuine concurrent event-loop scheduling), and fails if resolvers ever regress back to blocking (verified via mutation testing: reverting one resolver to sync/inline execution reliably fails the test).

**Not addressed by this fix, unchanged:** the backend's own process-wide lock (`store/sqlite/backend.py`) still serializes genuinely concurrent *writes* regardless of interface — expected, pre-existing, and orthogonal to the event-loop-blocking problem this KI was about.

---

## KI-053 — `require_admin` never checks principal `kind`, letting a misconfigured AI principal hold `admin` ✓ RESOLVED (M3)

**Severity:** Security — real (if misconfiguration-gated) privilege-escalation path
**Milestone target:** M3 — resolved via ADR-0038
**SPEC reference:** SPEC §17 (capability-checked operations), §8.3 (capability ordering)

### Description

Found during the M3 milestone-boundary security audit. `Ontology.require_admin` (`ontology.py`) — the shared gate for token issuance/revocation, principal listing, schema application, and plugin registration — checked `principal.default_capability == "admin"` but never `principal.kind`. Every other capability-tier gate in the codebase already excludes AI-kind principals regardless of configured capability (`_check_direct_write_capability`, `_require_reviewer_principal`, `resolve_contradiction`'s own inline check) — `admin` sits above all of them in SPEC §8.3's total order, and it was the one gate that didn't.

Concretely: a misconfigured AI principal with `default_capability="admin"` could call `issue_token(<a human write/review principal>)`, receive a raw bearer token for that human, and authenticate as them over REST or GraphQL — bypassing every AI-kind guard on direct write, review, and contradiction resolution, and destroying attribution on the resulting writes.

### Fix

`require_admin` now raises `CapabilityError` for `kind == "ai"`, mirroring `_require_reviewer_principal`'s existing pattern exactly. Closes the gap for all of `require_admin`'s callers at once — no call-site changes needed. See ADR-0038 for the full design record, including why the fix lives at the gate (matching the codebase's existing "defense at every gate" pattern) rather than at `Principal`/`create_principal` construction time.

---

## KI-054 — CLI's `principal create` is the one identity-mutating command with no `--author`/capability gate ✓ RESOLVED (M3)

**Severity:** Security — consistency gap (SPEC §17 MUST), not a new trust-boundary crossing (CLI access already implies file-level trust)
**Milestone target:** M3 — resolved via ADR-0038
**SPEC reference:** SPEC §17 (capability-checked operations)

### Description

Found during the same audit as KI-053. `Ontology.create_principal` has no capability check of its own — a deliberate ADR-0022 decision, matching the CLI's own historically ungated `principal create` command on an "implicitly-trusted local access" rationale. REST doesn't share that trust boundary (network-reachable), so `create_principal_route` compensates by calling `Ontology.require_admin(principal.id)` explicitly before calling `create_principal`. The CLI never adopted that same external-gate pattern: `principal create` was the only identity-mutating CLI command with no `--author` option at all, unlike `principal list`, `issue-token`, `revoke-token`, `list-tokens`, and every `proposal`/`schema` write command.

### Fix

`principal create` now requires `--author` (naming an existing admin, checked via `Ontology.require_admin`), mirroring REST's already-correct pattern — with one bootstrap exception: `--author` may be omitted only when the database has zero existing principals (checked via the raw `StorageBackend.list_principals()` port method, not the gated `Ontology.list_principals`, to avoid a circular "need an admin to check if an admin exists" dependency). `Ontology.create_principal` itself remains intentionally ungated for direct SDK callers, unchanged from ADR-0022 — see ADR-0038 for the full reasoning.

---

## KI-056 — GraphQL query amplification: unauthenticated introspection recursion, and alias amplification escalated to cross-interface DoS by KI-052 ✓ RESOLVED (M3)

**Severity:** Security — live, remotely-reachable amplification vector; the only finding from the M3 milestone-boundary security audit reachable without any credential
**Milestone target:** M3 — resolved via ADR-0037's 2026-08-30 update
**SPEC reference:** SPEC §17 (security model)

### Description

Found during the M3 milestone-boundary security audit, as a direct escalation of a limitation ADR-0037 had already named but not fixed. Two distinct vectors:

1. **Unauthenticated introspection amplification.** `create_graphql_app`'s auth model defers `_require_principal` to resolver-time so introspection queries stay reachable without a token (by design, matching REST's `docs_url` posture). Introspection queries are self-referentially recursive over the schema's own type graph (`{ __schema { types { fields { type { fields { ... } } } } } }`), so an anonymous caller could trigger an expensive, deeply-nested response with a short request — the classic GraphQL introspection DoS shape — with no depth or cost limit anywhere.
2. **Authenticated, now cross-interface, alias amplification.** ADR-0037's own 2026-08-28 measurement (recorded when resolvers were converted to async, KI-052) showed 200 aliased root fields at 0.5s each saturating the shared anyio worker-thread pool enough to delay an unrelated concurrent request by ~2.5s. Before KI-052, the same query would have blocked only the GraphQL event loop itself; after it, the harm reaches any REST app mounted in the same process, since both now share the same thread pool.

### Fix

`strawberry.extensions.MaxAliasesLimiter(max_alias_count=15)` and `QueryDepthLimiter(max_depth=10)` are now wired into every schema unconditionally (not tied to any flag) — **bounds** vector 2, doesn't eliminate it: measured 15 concurrent worker threads per max-alias request against the shared pool's default 40-thread capacity, so a handful of concurrent requests can still exhaust it; this reduces the amplification ratio (~200 aliased fields → 15) rather than closing the vector outright. `QueryDepthLimiter` is, today, pure defense-in-depth — no type in this schema is recursive, so no schema-valid query can approach depth 10 regardless. `QueryDepthLimiter` was verified *not* to bound introspection (the carve-out is in *strawberry's own* depth-counting logic, not graphql-core's — graphql-core has no depth validator at all — and a custom `should_ignore` callback can't override the hardcoded skip), so `introspection` now defaults to `False` — closes vector 1. `MaxTokensLimiter` was considered and rejected for the introspection vector: it bounds request token count, not response size, and introspection's amplification is exactly a short request producing a disproportionately large response. Also found in review: the alias limit applies to `Mutation` fields too, where the threat doesn't exist (GraphQL executes root mutations serially, so aliasing can't parallelize them) — accepted as a minor, documented tradeoff rather than building a mutation-aware exception. See ADR-0037's 2026-08-30 update for the full design record, including why the REST `docs_url` analogy that originally justified defaulting introspection on doesn't hold for this specific risk (REST's OpenAPI JSON has no recursive amplification potential; GraphQL's schema graph does), and the `strawberry-graphql` dependency-floor bump this fix's factory-callable pattern required.

---

## KI-057 — `Ontology.retract()` is unreachable from REST, GraphQL, MCP, or the CLI ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — the most heavily-governed write path in the codebase has zero production interface exposure
**Milestone target:** Backlog — resolved via ADR-0039
**SPEC reference:** SPEC §5.3 (append-only assertions, retraction), §10.3 (contradiction resolution), §14 (interfaces)

### Description

`Ontology.retract()` (`src/ontolith/ontology.py:1542`) is the only production entry point for governed retraction — introduced by KI-002 specifically so retraction would go through the same proposal/policy/conflict-routing pipeline as every other write, then hardened across five further KIs and two ADRs (KI-033's self-dealing guard, KI-043/ADR-0030's capability floor and review routing, KI-044/ADR-0031's terminal-status winner guard, KI-051's terminal-member guard extension). Despite that, it had no route on any of the four shipped interfaces:

- REST (`interfaces/rest.py`): no `/assertions/{id}/retract` or equivalent — the full route list (`/schema`, `/entities/{id}`, `/query`, `/provenance/{id}`, `/proposals`, `/proposals/{id}/accept|reject|review|resubmit`, `/assertions` POST, `/contradictions`, `/contradictions/flag`, `/contradictions/{id}/resolve`, `/principals`, `/principals/{id}/tokens`, `/namespaces`) had no retract path.
- GraphQL (`interfaces/graphql.py`): `Mutation` exposed `propose`, `acceptProposal`, `rejectProposal`, `requestChanges`, `resubmitProposal`, `flagContradiction`, `resolveContradiction` — no `retract`.
- MCP (`interfaces/mcp.py`): six tools registered, no retract tool.
- CLI (`interfaces/cli.py`): `assert`/`assertions`/`proposal {list,accept,reject,review,resubmit}`/`contradiction list` — no `assert retract` or equivalent.

It was reachable from `WriteView.retract()` (`plugins/views.py:144`) and the SDK directly, but a REST/GraphQL/MCP/CLI-only deployment — the normal production shape — had no way to retract a fact at all. Unlike `resolve_contradiction`'s deliberate MCP omission (explicitly recorded in this file as a reviewer-only action, out of ADR-0008's propose-only scope), this gap had never been named as a deliberate boundary in any ADR or KI.

### Fix

Added `POST /assertions/{id}/retract` (REST, `acting_as` as an optional query parameter), a `retract` mutation (GraphQL), a new top-level `ontolith retract <id> --author <id> [--acting-as <id>]` CLI command (not nested under `assert`, which is a plain command, not a Typer sub-app), and `ontolith.retract` (MCP) — all thin wrappers around the already-governed `Ontology.retract()`, no new domain logic. MCP inclusion was the one open design question the original Fix text named: decided in favor of exposing it, at the same `propose` tier as `ontolith.propose`/`ontolith.flag_contradiction`, since `retract()` is structurally policy-evaluated and proposal-producing like those two — unlike the reviewer-only, unconditionally-gated `resolve_contradiction()`, which stays MCP-excluded. Full rationale in ADR-0039.

---

## KI-058 — MCP's `ontolith.query` tool has none of the hybrid-retrieval or time-travel parameters REST/GraphQL expose ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — agents (MCP's own audience) cannot use semantic retrieval at all
**Milestone target:** Backlog — resolved via ADR-0043
**SPEC reference:** SPEC §14.4 (`ontolith.query | {namespace, concept, where?, semantic?, as_of?, min_confidence?, limit?}`), §11.3 (hybrid retrieval)

### Description

`query_tool(concept, token, filters=None, namespace="default")` (`interfaces/mcp.py:174-228`) accepted only `concept`, `token`, `filters`, `namespace`. It had no `semantic`, `min_confidence`, `trust_at_least`, `as_of`, or `limit` parameters — none of the hybrid-retrieval capability `QueryBuilder` gained in M3 (KI-018, KI-037/KI-047's confidence/trust filters) was reachable from MCP, and there was no way to cap result size via MCP at all. REST's `POST /query` and GraphQL's `Query.query` both wire `semantic`, `min_confidence`, `trust_at_least`, and `limit` straight into the same `QueryBuilder`; MCP did not. SPEC §14.4's own normative tool table names `semantic`, `as_of`, and `min_confidence` explicitly, and ADR-0008 itself describes the tool as "Symbolic + semantic retrieval."

Found at the M3 milestone boundary specifically because it requires comparing MCP against REST/GraphQL side by side — no single PR review (KI-018's hybrid-retrieval PR didn't touch MCP; the M2 MCP PR predates hybrid retrieval entirely) would surface it.

### Fix

Added `semantic`, `as_of`, `min_confidence`, `trust_at_least`, `limit` parameters to `query_tool`, mirroring REST/GraphQL's `QueryBuilder` wiring for the first, third, fourth, and fifth (ADR-0043) — `as_of` is new even to REST/GraphQL, added to MCP alone per SPEC §14.4's own normative tool table naming it for this tool specifically. Also removed the tool's pre-existing `namespace` parameter: `Ontology.query()` never accepted one (namespace is hardcoded, an M1 limitation REST/GraphQL already work around by not exposing the field at all), so it was silently doing nothing on every call — schema-visible, not runtime-breaking (FastMCP drops unrecognized arguments rather than rejecting the call). While adding `as_of` support, found and fixed a related bitemporal-correctness gap: `QueryBuilder.semantic()` combined with `.as_of()` but no `.where()` filter ignored `as_of_time` entirely, returning entities that didn't exist yet at that point in time — unreachable before this KI, since no prior interface ever exposed `as_of` at all.

---

## KI-059 — MCP's error `code` values diverge from the taxonomy REST and GraphQL share ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — breaks cross-interface client error-handling reuse
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §16 (stable, machine-readable error codes)

### Description

32 call sites in `interfaces/mcp.py` hand-wrote `{"error": str(exc), "code": "auth_error"}` / `"capability_error"` / `"validation_error"` / `"not_found"` rather than reading `exc.code` off the caught `OntolithError` (two further sites, `get_tool`/`provenance_tool`'s "not found" paths, had no `code` key at all — found while fixing this). `core/errors.py` defines the actual codes as `ONTOLITH_ERROR`, `SCHEMA_ERROR`, `VALIDATION_ERROR`, `AUTH_ERROR`, `CAPABILITY_ERROR`, `POLICY_DENIED`, `CONFLICT_ERROR`, `NOT_FOUND`, `STORAGE_ERROR`, `PLUGIN_ERROR`. REST (`rest.py:494`, `ErrorOut(code=exc.code, ...)`) and GraphQL (`graphql.py:873`, `error.extensions = {"code": original.code, ...}`) both pass `exc.code` through unmodified, so the taxonomy was genuinely identical between those two — MCP alone diverged, both in casing (`auth_error` vs `AUTH_ERROR`) and, for not-found, in the literal string used (`not_found` vs `NOT_FOUND`). The divergence was introduced piecemeal starting with KI-024 and never reconciled once REST/GraphQL's shared convention formed afterward — exactly the kind of drift only visible once all three interfaces exist and are compared directly.

### Fix

Replaced every hand-written `"code": "..."` literal in `mcp.py` with `exc.code` from the caught `OntolithError` instance, or the exception class's own `.code` attribute (e.g. `ValidationError.code`) for the handful of sites with no exception instance in scope (a synthesized validation error, and `_bearer_token()`'s own auth-shaped error strings) — matching REST's own precedent for its equivalent case (`RequestValidationError`, mapped via `ValidationError.code`). Also found and fixed along the way: `get_tool`/`provenance_tool`'s "not found" paths had no `code` key at all, not just the wrong casing. Added `tests/unit/test_cross_interface_error_codes.py`, a cross-interface regression test asserting REST, GraphQL, and MCP all return the same `code` string for the same underlying exception type (`AuthError`, `NotFoundError`), sharing one `Ontology`/`AuthProvider` across all three the way a real deployment would.

---

## KI-060 — No audit trail for admin actions: token issuance/revocation, principal creation, and schema application are unattributable ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — the highest-value actions in the system leave no forensic trace
**Milestone target:** Backlog — resolved via ADR-0042
**SPEC reference:** SPEC §17 ("all writes, proposal decisions, and resolutions are append-only and attributable")

### Description

`PrincipalCredential` (`identity/credential.py:25-29`) recorded `principal_id`, `token_hash`, `created_at`, `revoked_at` — never who issued or revoked it, even though `Ontology.issue_token(principal_id, author)` (`ontology.py:2907-2920`) and `revoke_token` (`ontology.py:2936-2941`) both received the acting admin's id and discarded it. There was no event-table row for credential lifecycle, principal creation (`create_principal`, `ontology.py:258-319`), schema application (`apply_schema`, `ontology.py:2825-2836`), or plugin registration. The only audit tables that existed at all were `assertion_event` and `proposal_event`.

Token issuance in particular converts local file access into a durable, network-reachable credential — the single highest-value action in the system — yet after an incident there was no way to answer "which admin minted this credential, and who revoked it and when." This also meant any investigation into a credential-compromise or privilege-escalation incident (see KI-053/KI-054) had no trace to work from.

### Fix

Added `issued_by`/`revoked_by` columns to `principal_credential` (both backends) — `issue_token`/`revoke_token` populate them from the already-required `author` parameter. Added a new `AdminEvent` (`identity/admin_event.py`) and `admin_event` table for `create_principal`, `apply_schema`, and `PluginRegistry.register` — reuses ADR-0041's exact SQLite-trigger immutability mechanism (DuckDB gets the same documented no-equivalent gap). `create_principal` gained an optional `author` parameter (attribution only, not a new capability gate — ADR-0022's "no built-in check" decision is unchanged) so REST/CLI can pass through the admin id they already validate via `require_admin`/`--author`. Found and fixed along the way: re-revoking an already-revoked credential was silently overwriting `revoked_by` on a second call — now a true no-op, preserving the first revocation's real attribution. ADR-0042.

---

## KI-061 — `SourceQuorum` policy strategy silently drops the "AI proposals always require review" invariant ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — the headline AI-safety guarantee becomes policy-dependent without any interface saying so
**Milestone target:** Backlog — resolved via ADR-0040
**SPEC reference:** SPEC §9.2 (policy strategies, `Composite`), ADR-0003 (AI/low-trust principals require review)

### Description

`SourceQuorum` (`govern/policy.py:206-288`) has no `kind`-based check anywhere in its evaluation — only `ThresholdPolicy` (the default) implements the "AI-kind principal always routes to review" rule (`policy.py:149-154`). `Ontology.propose`/`propose_ref` themselves have no AI check of their own (`ontology.py:1152-1156`) — the configured `PolicyStrategy` is the *only* thing standing between an AI-authored proposal and an immediate auto-accepted write. A deployment configured with `Ontology(backend, policy=SourceQuorum(2))` silently loses the guarantee, while MCP's tool docstrings (`mcp.py:320-323`, `:483-486`) continued telling agent callers their proposals are always queued for human review — which is only true under the default strategy. SPEC §9.2's `Composite` strategy, named in `SourceQuorum`'s own docstring as the intended resolution, wasn't implemented.

### Fix

`SourceQuorum`'s AI-auto-accept behavior is a deliberate ADR-0025 decision, pinned by its own conformance vector — this was decided not to be silently reversed. Instead, built SPEC §9.2's `Composite(all=…, any=…)` (`govern/policy.py`, ADR-0040): every strategy in `all` must independently `AutoAccept` (most-restrictive decision wins), at least one in `any` must (least-restrictive wins), same-severity decisions merge rather than one being discarded. `Composite(all=[<an AI-review strategy>, SourceQuorum(2)])` now actually expresses the combination ADR-0025 named but couldn't build. No new "AI-always-reviews" strategy shipped — `ThresholdPolicy` can't be reused for this (it would re-impose its own capability gate too), so the pattern is documented as a five-line inline example in `Composite`'s own docstring instead. MCP's `ontolith.propose`/`ontolith.resubmit` docstrings corrected to attribute the guarantee to the *default* `ThresholdPolicy`, not state it unconditionally.

---

## KI-062 — Two known-vulnerable dev/docs-only dependencies; `pip-audit` CI gate currently red ✓ RESOLVED (Backlog)

**Severity:** Security/supply-chain — dev-only exposure, but the unconditional gate is failing
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** Implementation Plan §7.1 (supply-chain gate)

### Description

`pip-audit` against the full locked dependency set reported two findings, both dev/docs-only (neither ships in the `ontolith` wheel or any runtime extra):
- `pip==26.1.2` — PYSEC-2026-3721 / CVE-2026-13346 (doubly-encoded package URLs → arbitrary write location), fixed in `26.2`. Transitive via `pip-api` ← `pip-audit` itself.
- `pymdown-extensions==11.0` — PYSEC-2026-3654 / GHSA-gm37-52c6-37mw / CVE-2026-67422 (ReDoS in four default-config inline processors), fixed in `11.0.1`. Transitive via `mkdocs-material`/`mkdocstrings`.

`.github/workflows/security.yml`'s `pip-audit` step is unconditional, so the `scan` job was failing on every PR — `security.yml`'s own comments document a prior incident where exactly this kind of unfiltered noise masked a genuine finding. Runtime dependencies (`rdflib==7.6.0`, `strawberry-graphql==0.319.0`, `graphql-core==3.2.11`, `fastapi==0.138.0`, `starlette==1.3.1`, `mcp==1.29.0`, `sqlite-vec==0.1.3`, `duckdb==1.5.4`, `pydantic==2.13.4`) have no advisories — the M3 additions (`rdflib`, `strawberry-graphql`) introduced no CVE debt.

### Fix

`uv lock --upgrade-package pip --upgrade-package pymdown-extensions` bumped `pip` to `26.2.1` and `pymdown-extensions` to `11.0.2` in `uv.lock` — both transitive, via `pip-audit` and `mkdocs-material`/`mkdocstrings` respectively, so no direct `pyproject.toml` dependency changed. `uv run pip-audit` now reports zero findings.

---

## KI-063 — CLI has no way to flag or resolve a contradiction ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — the only one of four interfaces with zero contradiction write surface
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §10.3 (contradiction resolution), §14.2 (CLI)

### Description

`contradiction_app` (`interfaces/cli.py`) registered only `contradiction list` — no `contradiction flag` or `contradiction resolve` command existed. REST has `POST /contradictions/flag` and `POST /contradictions/{id}/resolve`; GraphQL has `flagContradiction`/`resolveContradiction` mutations; MCP has `ontolith.flag_contradiction` (deliberately not `resolve`, per ADR-0008/KI-009's reviewer-only scoping). The CLI was the only one of the four with neither — and resolution in particular is explicitly a human-reviewer action per SPEC §10.3, i.e. exactly the actor the CLI serves, yet the CLI couldn't do it. KI-032 (M3) closed the equivalent gap for proposal review commands but never mentioned contradictions; this specific gap had never been named in any prior KI.

### Fix

Added `ontolith contradiction flag <id_a> <id_b> --author <id> [--rationale <text>]` (propose-tier, mirrors `ontolith assert`/`retract`'s `--author` convention, not a reviewer action) and `ontolith contradiction resolve <id> --winner <assertion_id> --reviewer <id>` (mirrors `proposal accept`'s `--reviewer`/`--author` alias) — both thin wrappers around the already-governed `Ontology.flag_contradiction()`/`resolve_contradiction()`, no new domain logic.

---

## KI-064 — `observe/` remains a fully empty package at the M3 milestone boundary ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — SPEC §18 (SHOULD, not MUST) unimplemented across the whole M0-M3 arc
**Milestone target:** Backlog — resolved by planning, not by implementation (see Fix)
**SPEC reference:** SPEC §18 (metrics, events, structured logs)

### Description

`src/ontolith/observe/__init__.py` is empty (0 lines) as of M3's close. With REST, GraphQL, MCP, and CLI all now shipped and deployable, there is no way to observe proposal-acceptance rate, review latency, open-contradiction count, or auto-accept rate for any live interface, and no structured logging correlating `namespace`/`principal`/`acting_as`/`proposal_id` across a request — exactly what SPEC §18 asks for. This was flagged as a lower-confidence, informational observation mid-M3 (§18 is SHOULD, not MUST, so it never blocks a conformance vector); at the M3 boundary, with a production-shaped deployment now genuinely possible, the absence is more consequential and worth a real decision rather than continuing to defer silently.

### Fix

Recorded **ADR-0044**, scoping SPEC §18 observability to M4 rather than declaring it out of scope through 1.0 — `docs/Ontolith_Implementation_Plan.md`'s M4 scope column previously never named observability at all (an oversight, not a decision, given M4 is literally "Production (1.0)"), now updated to reference ADR-0044 explicitly. The ADR decides the architecture ahead of time (a single `ObservabilitySink`-style port beside `Clock`/`IdProvider`, dependency-rule-compliant, `govern/policy` still emits nothing itself — instrumentation happens one layer up) and a priority order for M4 implementation (structured correlated logs first, replacing today's four ad hoc `logging.getLogger()` calls (five emission sites); the four named events next — call-site counts vary per event, not uniformly one each; the seven-metric surface last, deferred to its own follow-up ADR for a concrete backend choice). **No code lands with this fix** — `observe/` stays empty until M4 itself starts; implementing ahead of M4's own turn would be scope creep beyond this milestone's own boundary. This KI is resolved as "planned," not "built."

---

## KI-065 — `strawberry-graphql`/`rdflib` have no upper version bound, unlike `mcp` ✓ RESOLVED (Backlog)

**Severity:** Supply-chain — inconsistent pinning policy, not an active vulnerability
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** n/a (dependency management convention)

### Description

`mcp` is pinned `>=1.28.1,<2.0` with a documented rationale ("avoids an unreviewed major bump"). The two M3-era dependencies carried no upper bound at all: `strawberry-graphql[fastapi]>=0.316` (currently resolving `0.319.0`) and `rdflib>=7.0`. `uv.lock` protects this repo's own CI, but not a downstream `pip install ontolith[graphql]`, which resolves whatever is newest at install time.

### Fix

Applied the same `<N.0` convention already used for `mcp`: `strawberry-graphql[fastapi]>=0.316,<1.0` and `rdflib>=7.0,<8.0` (`pyproject.toml`). `uv lock` produced only a 2-line lockfile metadata diff — both packages were already resolving within the new bounds, no version actually changed. Noted explicitly in a `pyproject.toml` comment that `<1.0` is a coarser guarantee for `strawberry-graphql` specifically than for `mcp`/`rdflib`: the `>=0.316` floor itself exists because a *minor* pre-1.0 release already broke this integration once (KI-056's factory-callable pattern), so an upper bound at the next major version doesn't protect against the next 0.x break — `security.yml`'s weekly `pip-audit`/scheduled scan remains the real backstop for that, same as for any other dependency.

---

## KI-066 — Audit tables (`assertion_event`/`proposal_event`) are append-only by convention, not by DB constraint ✓ RESOLVED (Backlog, SQLite only)

**Severity:** Architecture gap — defense-in-depth gap, not a demonstrated exploit
**Milestone target:** Backlog — resolved for SQLite via ADR-0041; DuckDB has no fix available
**SPEC reference:** SPEC §17 ("the audit trail MUST NOT be mutable")

### Description

Immutability of `assertion_event`/`proposal_event` was enforced solely by `StorageBackend`'s port surface exposing only `put_assertion_event`/`put_proposal_event` — no update or delete method exists at the port level. There was no DB trigger, view, or revoked grant backing this. Any code holding the raw connection (including, per KI-014's own "still open" update, an in-process plugin that reaches `ReadOnlyView._backend` one attribute hop past the intended API) could `UPDATE`/`DELETE` the audit tables directly.

### Fix

Added `BEFORE UPDATE`/`BEFORE DELETE` triggers on `assertion_event` and `proposal_event` that `RAISE(ABORT, ...)` — `SQLiteBackend` only. Review (round 1) found the initial version incomplete: `INSERT OR REPLACE` performs an implicit conflict-row delete that a plain `BEFORE DELETE` trigger only catches when `PRAGMA recursive_triggers` is ON (SQLite defaults it OFF) — added the pragma to `SQLiteBackend.__init__`. Review (round 2) found *that* still incomplete: the pragma is per-*connection*, not persisted in the database file, so a second raw connection to the same file (reachable via `backend.path`, a public attribute) revived the bypass with no privilege escalation needed. Closed durably with a third trigger per table — `BEFORE INSERT ... WHEN EXISTS(SELECT 1 FROM <table> WHERE id = NEW.id)` — which persists in the schema itself and blocks `INSERT OR REPLACE` regardless of pragma state or which connection issues it; the pragma is kept as defense in depth but is no longer load-bearing for this case. `DuckDBBackend` gained no equivalent: verified DuckDB (1.5.4) has no `CREATE TRIGGER` support at all, and no connection-level access-restriction mechanism exists to work around that (DuckDB's embedded, single-user connection model has no role/grant system to revoke). Documented explicitly as a currently-unfixable backend asymmetry in `DuckDBBackend`'s own docstring, mirroring ADR-0032's precedent for the same kind of documented DuckDB limitation — not silently left unaddressed. `conformance/test_audit_table_immutability.py` pins the SQLite guarantee (`sqlite3.IntegrityError` on UPDATE/DELETE/`INSERT OR REPLACE` × both tables, including from a second raw connection that never sets the pragma) and the DuckDB gap (the same operations currently still succeed, re-verified by re-reading the row) as executable tests. ADR-0041.

**Update (2026-09-07, pre-M4 audit):** re-confirmed the DuckDB gap is unchanged and remains a genuine, currently-unfixable backend asymmetry (no upstream trigger/grant mechanism exists to close it). No dedicated deployment guide exists yet in this repo to carry the operational implication, so recording it directly here: a deployment choosing the DuckDB backend does not get a tamper-evident audit trail at the database layer — that guarantee is SQLite-only. Anyone selecting a storage backend for a production deployment should treat this as a real input to that decision, not an implementation detail; worth surfacing prominently once a deployment guide exists (tracked informally here until one does).

---

## KI-067 — MCP tools take bearer tokens as arguments, placing a live credential in model context ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — credential-exposure surface outside Ontolith's own logging
**Milestone target:** Backlog — resolved via an ADR-0014 update
**SPEC reference:** ADR-0014 (bearer-token authentication)

### Description

Every MCP tool (`interfaces/mcp.py`) took `token: str` as a required parameter rather than reading it from a transport-level header. Because the token was a tool *argument*, the calling model had to emit it in every tool call — landing it in the model's context window, conversation transcripts, and any MCP client's own tool-call logging, none of which Ontolith controls. ADR-0014 discussed token hashing and revocation but never named this exposure path. Tokens are also long-lived (no expiry — ADR-0014 states a leaked token grants access "until revoked"), making a transcript-embedded token a durable liability. The SSE/streamable-HTTP transports had a header channel available and unused.

### Fix

`token` became optional (`str | None = None`) on all 8 tools. A new `_bearer_token()` helper reads `mcp.get_context().request_context.request` — the raw Starlette request the mcp SDK's own transport wiring already threads through to every tool call under SSE/streamable-HTTP — and prefers its `Authorization: Bearer <token>` header over the `token` argument, falling back to the argument only when no header is present at all (including stdio, which has no HTTP request at all). A header that IS present but malformed (wrong scheme, or a blank value) fails the call closed rather than falling back — silently accepting the argument on a misconfigured header would reopen the exact exposure this fix removes. Not adopted: the mcp SDK's built-in OAuth-shaped bearer-auth stack (`FastMCP(auth=..., token_verifier=...)`) — it models a full OAuth 2.1 resource server (requires an `issuer_url`, advertises RFC 9728 metadata) with nothing real behind it in Ontolith's per-principal API-key model, so reading the header directly instead keeps `AuthProvider.resolve()` unchanged. stdio's exposure isn't closed by this fix (no header channel exists there) — documented in ADR-0014 as a residual, smaller-but-nonzero risk, with proactive rotation/revocation recommended for stdio-facing principals (corrected by security review, M4 Workstream 7: `PrincipalCredential` has no `expires_at`/automatic-expiry mechanism at all, so "short-lived tokens" — this entry's original wording — wasn't actually achievable; see KI-107). Also documented as a residual: an HTTP deployment cannot yet *require* the header — `token` remains an accepted argument, so the exposure is made avoidable, not eliminated.

---

## KI-068 — RDF/OWL bridge and LinkML bridge map `Float` to different XSD precisions, disclosed on only one side ✓ RESOLVED (Backlog)

**Severity:** Informational — export-only, non-normative for round-tripping
**Milestone target:** Backlog
**SPEC reference:** SPEC §4 (value types)

### Description

`schema/linkml.py` maps Ontolith's `Float` to LinkML `range: float` (which real LinkML tooling treats as 32-bit `xsd:float`); `schema/rdf.py` maps the same `Float` to `XSD.double` (64-bit) — matching what Ontolith actually stores (Python `float`/IEEE-754 double). ADR-0036 candidly discloses this divergence in its own Consequences section, but ADR-0013 (the LinkML bridge's ADR) has no corresponding note, so a reader of the LinkML bridge in isolation has no signal that a same-`value_type` inconsistency exists on the RDF/OWL side.

### Fix

Changed the LinkML bridge's `Float` mapping (`schema/linkml.py`) from `range: float` to `range: double` — the precision-accurate choice given the two bridges now agree exactly (`double`/`XSD.double`, both 64-bit). This never made Ontolith's own `to_yaml`/`from_yaml` round-trip lossy (the reverse mapping accepted both spellings even before this fix, and the golden round-trip tests check IR fidelity, not the emitted string) — the old mapping's actual defect was a mis-declared precision that only matters to a downstream LinkML-consuming tool resolving `float` to a real 32-bit type. Low-risk fix: the reverse mapping (`from_yaml`) already accepted `double` before this change, apparently anticipated but never matched by the emission side, so this made the two tables consistent with each other rather than requiring new parsing logic. `float` stays accepted on import for backward compatibility (IR-level, not YAML-text-level: a hand-authored `range: float` file still imports as `Float`, but now re-exports as `range: double`, not a byte-identical copy — no regression, the same asymmetry existed in the opposite direction before this fix) — hand-authored LinkML and any schema exported by a pre-KI-068 Ontolith version both still use it, and Python's `float` parsing doesn't distinguish 32-bit from 64-bit on read regardless. ADR-0013 updated with a dated Update section and its own type-mapping table corrected; ADR-0036's "not reconciled here" Consequences bullet updated to point at the fix rather than left claiming an open gap.

---

## KI-069 — SPEC §9.2 still names four unbuilt `PolicyStrategy` implementations ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — a SPEC SHOULD-list gap with no open tracking item once KI-061 closes
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §9.2 (policy strategies)

### Description

SPEC §9.2 names six built-in `PolicyStrategy` implementations a conforming implementation SHOULD provide: `ConfidenceThreshold`, `TrustLevel`, `SourceRequired`, `RequireReviewByRole`, `SourceQuorum`, and `Composite(all=…, any=…)`. `SourceQuorum` (ADR-0025) and `Composite` (KI-061, ADR-0040) are now built; `ThresholdPolicy` (the default) covers roughly what `TrustLevel` would. `ConfidenceThreshold`, `SourceRequired`, and `RequireReviewByRole` remain entirely unbuilt, with no strategy in the codebase covering their specific behavior (confidence-based routing, requiring a non-empty `source`, and role-based reviewer assignment, respectively). Found while resolving KI-061 (ADR-0040's own Consequences section names this gap) — closing KI-061 removed the last KI that named it, leaving it untracked.

### Fix

Implemented all three (`docs/adr/ADR-0045-confidence-source-role-policy-strategies.md`), none deferred, in `govern/policy.py` following `SourceQuorum`'s established conventions exactly (KI-015 capability floor enforced explicitly, only the proposal's first staged operation inspected, retractions/unrecognized op kinds always require review). `ConfidenceThreshold(threshold, reviewers=None)` auto-accepts once the proposal's own staged `confidence` meets `threshold` (inclusive) — a missing confidence always requires review, never assumed as 0 or 1. `SourceRequired(reviewers=None)` auto-accepts only when the operation carries a non-empty `source` — no `kb` read, no corroboration count, a narrower unconditional cousin of `SourceQuorum` (compose both via `Composite` for "sourced AND quorum'd"). `RequireReviewByRole(role_reviewers, *, default=None)` never auto-accepts — its entire purpose is choosing reviewers, not deciding whether review is needed — reading `principal.metadata.get("role")` (no dedicated `Principal.role` field exists; `metadata` is the codebase's own documented extension point for exactly this) with role looked up on the real author, never `acting_as`, mirroring `ThresholdPolicy`'s "AI's own kind never laundered via delegation" precedent (ADR-0003). Unit tests (`tests/unit/test_govern.py`) cover every decision path per strategy; conformance vectors (`conformance/test_confidence_threshold_policy.py`, `test_source_required_policy.py`, `test_require_review_by_role_policy.py`) pin the SPEC §9.2 purity/determinism contract. All three added to the public API surface (`ontolith.govern.__all__`, pinned in `tests/unit/test_public_api_surface.py`). Review found the returned `Decision.reviewers` list was aliased, not copied, in `RequireReview.__init__` (a caller mutating a returned decision's `.reviewers` silently rewrote the issuing strategy's own configuration for every future evaluation) — fixed by copying at construction, closing the same latent bug in every pre-existing strategy too, not just these three. Also found a `RequireReviewByRole`-specific gap this same review surfaced: nothing in the system actually consumes a `RequireReview`'s `reviewers` at all — filed separately as **KI-078**, since it's a pre-existing gap this KI's own `RequireReviewByRole` makes far more consequential rather than one this KI itself introduced.

---

## KI-070 — Most direct dependencies still have no upper version bound ✓ RESOLVED (Backlog)

**Severity:** Supply-chain — inconsistent pinning policy, not an active vulnerability
**Milestone target:** Backlog
**SPEC reference:** n/a (dependency management convention)

### Description

KI-065 extended the `<N.0` upper-bound convention (established for `mcp`, ADR-0026) to `strawberry-graphql`/`rdflib`, but left every other direct dependency unbounded: `pydantic>=2.0`, `typer>=0.9`, `python-ulid>=2.0`, `fastapi>=0.110`, `duckdb>=1.0`, `uvicorn>=0.27`, `pyyaml>=6.0` (`pyproject.toml`). Several of these have a *higher* blast radius than the two KI-065 covered — `pydantic` underlies every domain model, `fastapi` sits on the same auth-bearing request path the new `strawberry-graphql` bound's own rationale names. A downstream `pip install ontolith` (no extras) or any extra resolves whatever is newest for these at install time, unreviewed by the project, same gap KI-065 closed for two packages specifically.

### Fix

Applied the same `<N.0` convention to all seven: `pydantic>=2.0,<3.0`, `typer>=0.9,<1.0`, `python-ulid>=2.0,<4.0`, `fastapi>=0.110,<1.0`, `duckdb>=1.0,<2.0`, `uvicorn>=0.27,<1.0` (both places it's listed — `rest` and `graphql` extras each declare it independently), `pyyaml>=6.0,<7.0`. `fastapi`/`typer`/`uvicorn` are all long-lived pre-1.0 packages, like `strawberry-graphql` was before KI-065's own floor bump — the `<1.0` bound guards against an eventual major release, not 0.x churn; `security.yml`'s weekly `pip-audit`/scheduled scan remains the real backstop for that, same caveat KI-065 already documented. `python-ulid` is the one exception to "one major per bound": its floor stayed at `2.0` (the lock resolves `3.1.0`) rather than being bumped to match, so `<4.0` deliberately spans two majors instead of one — this KI's own scope was adding upper bounds, not auditing floors. `uv lock` produced only an 8-line lockfile metadata diff — no package's resolved version actually changed, all seven were already resolving within the new bounds (verified: `pydantic` 2.13.4, `typer` 0.26.7, `python-ulid` 3.1.0, `fastapi` 0.138.0, `duckdb` 1.5.4, `uvicorn` 0.49.0, `pyyaml` 6.0.3). The `dev` extra's ~20 tooling dependencies remain unbounded — out of this KI's own scope (its Description named only the seven runtime dependencies above) and lock-pinned in practice, since CI installs from the committed `uv.lock` rather than a fresh resolve. ADR-0026 updated to record the convention now covers every direct runtime dependency.

---

## KI-071 — `flag_contradiction()`'s `rationale` is silently dropped when extending an already-open contradiction ✓ RESOLVED (Backlog)

**Severity:** Data-loss gap — a caller-supplied explanation is silently discarded, not rejected or errored
**Milestone target:** Backlog
**SPEC reference:** SPEC §10.3 (contradiction resolution)

### Description

`Ontology.flag_contradiction(assertion_id_a, assertion_id_b, author, *, rationale=None)` (`ontology.py`) only writes `rationale` into the new `Contradiction`'s `metadata` on the "create a new contradiction" branch (`Contradiction(..., metadata={"rationale": rationale} if rationale else {})`). The "extend an already-open contradiction" branch (`self.backend.update_contradiction_members(existing.id, merged)`) never touches `metadata` at all — a caller who passes `rationale="..."` while extending gets no error, no warning, and the text is simply gone. Affects every interface that exposes `flag_contradiction`: REST (`POST /contradictions/flag`), GraphQL (`flagContradiction`), MCP (`ontolith.flag_contradiction`), and now the CLI (`ontolith contradiction flag --rationale`, KI-063) — none of their docstrings/help text mention this. Found in review of KI-063's CLI addition; pre-existing, not introduced by that PR.

### Fix

Both branches now write into the same unified `metadata["rationale_history"]` shape: a list of `{"rationale", "actor", "at"}` entries, one per call that supplied a truthy rationale (`None`/`""` are both still treated as "none given", matching this method's own long-standing check), whether that call created the contradiction or extended an already-open one. `create` seeds the list with one entry (or leaves `metadata` empty, as before, when no rationale is given); `extend` reads the existing list (defaulting to `[]` if the contradiction had none yet), appends, and writes the full dict back — a rationale-less extend appends nothing and leaves prior history untouched. This required extending the storage port: `StorageBackend.update_contradiction_members(contradiction_id, member_ids, metadata=None)` gained an optional `metadata` parameter that replaces the metadata blob wholesale when given (matching `member_ids`' own full-replacement convention) and leaves it alone when omitted — implemented identically in both SQLite and DuckDB, each folding the metadata column into the same single `UPDATE`. `Contradiction.metadata` was undocumented/unexposed by every interface before this, so no external consumer depended on the old single-`"rationale"`-key shape from the `create` path; that shape was unified into `rationale_history` too rather than kept as a special case for entry #1.

At the time this fix landed, the trail was readable only via `Ontology`/the SDK — no shipped interface (REST/GraphQL/MCP/CLI) serialized `Contradiction.metadata`, so a caller supplying `rationale` through any of them couldn't read it back. Filed as KI-075 rather than folded into this fix, matching the same data-captured/no-read-surface shape KI-072 closed for the admin-action audit trail — since resolved, see that entry. `rationale_history` is also stored as a plain, fully-overwritable `metadata` blob rather than an append-only, trigger-protected event table like `assertion_event`/`proposal_event`/`admin_event` — a deliberate scope choice (ADR-0041 update) given the three options considered, not an oversight.

---

## KI-072 — No interface exposes the admin-action audit trail recorded by ADR-0042 ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — the data is captured but unreachable through any shipped interface
**Milestone target:** Backlog — resolved via an ADR-0042 update
**SPEC reference:** SPEC §17 (append-only, attributable writes)

### Description

KI-060/ADR-0042 added `StorageBackend.get_admin_events()` and `PrincipalCredential.issued_by`/`revoked_by`, closing the *recording* half of "admin actions are unattributable." Nothing read any of it back through REST, GraphQL, the CLI, or MCP: `get_admin_events()` had no route/query/command/tool; `CredentialOut` (`interfaces/rest.py`) and the CLI's `principal list-tokens` output didn't include `issued_by`/`revoked_by` even though `PrincipalCredential` carried both fields. The only way to answer "who created this principal" or "who issued/revoked this token" was direct SDK/`kb.backend` access, which defeated KI-060's own stated motivation. ADR-0042 named this as "explicit future scope, not silently dropped," but no KI previously tracked it. Found in round-2 review of KI-060's PR.

### Fix

New `Ontology.get_admin_events(author, *, actor=None, target=None)`, admin-gated the same way `list_tokens`/`list_principals` already are. REST gets `GET /admin-events` with optional `actor`/`target` query parameters and a new `AdminEventOut` model; `CredentialOut` gains `issued_by`/`revoked_by`. CLI gets a new `ontolith admin-event list [--actor] [--target] --author <id>` command, and `principal list-tokens`'s output line now shows who issued/revoked each credential. GraphQL/MCP left for their own future scope, per this KI's own "extend as those surfaces need it" — MCP specifically because `get_admin_events()`'s admin-gating would make it the first MCP tool requiring `admin` capability rather than `read`/`propose`, a genuine new precedent not worth spinning up speculatively.

---

## KI-073 — MCP has no way to require the `Authorization` header, so a deployment can't close the `token`-argument exposure outright ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — the fix KI-067 shipped makes the exposure avoidable, not eliminated
**Milestone target:** Backlog
**SPEC reference:** ADR-0014 (bearer-token authentication)

### Description

KI-067/ADR-0014 made every MCP tool's `token` argument optional and prefer a transport-level `Authorization` header when one is present and well-formed, closing the default exposure of a live credential landing in the calling model's own context. But the `token` argument still works whenever no header is supplied at all — an HTTP (SSE/streamable-HTTP) deployment cannot currently *require* the header and reject the argument outright. A model that already has a token in its context (or a client that keeps emitting one out of habit) can keep authenticating via the argument indefinitely; nothing server-side forces the more private channel to actually be used.

### Fix

Added an opt-in `require_header_token: bool = False` keyword-only parameter to `create_mcp_server()`. When set, `_bearer_token()` returns "No bearer token provided" for a live request whose header is absent — even when the caller still supplies a valid `token` argument — disabling the fallback entirely; a *malformed* header (KI-067's existing fail-closed case) is unaffected either way. stdio (no header channel at all) simply becomes unusable under the flag, as anticipated when this was deferred (KI-067/ADR-0014's own Residual paragraph) — not a bug, the flag is only for HTTP (SSE/streamable-HTTP) deployments that want to require the private channel outright. All 9 tools' `token` parameter docstrings (the actual MCP tool-schema descriptions a calling model reads) updated to disclose the flag's effect, not just the module/constructor docs. Every code path independently mutation-tested: the argument-still-works, no-request-context-at-all, and real-transport cases were each reproduced against the pre-fix code and confirmed clean after. Review found two gaps the first test pass didn't cover — a valid header alongside a still-present `token` argument (the exact migration case the flag exists to support: a client that hasn't stopped sending `token` yet, behind a now header-injecting proxy), and the flag being captured per-server-instance rather than shared/global state — both closed with dedicated tests, each independently mutation-tested the same way.

---

## KI-074 — MCP still hand-catches errors per tool instead of one blanket mapping; 5 of 10 taxonomy codes are unreachable from MCP ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — MCP diverges from REST/GraphQL's error-handling architecture, not just the values KI-059 fixed
**Milestone target:** Backlog — resolved via an ADR-0014 update
**SPEC reference:** SPEC §16 (stable, machine-readable error codes)

### Description

KI-059 fixed MCP's error `code` *values* to match `exc.code`, but left the *mechanism* untouched: REST installs one `@app.exception_handler(OntolithError)` with a `_STATUS_BY_ERROR_TYPE` mapping covering the whole taxonomy (`SchemaError`, `PolicyDenied`, `ConflictError`, `StorageError`, `PluginError` included), and GraphQL has an equivalent blanket extension; `interfaces/mcp.py` still hand-caught only `AuthError`/`CapabilityError`/`NotFoundError`/`ValidationError` per call site. A `SchemaError`, `PolicyDenied`, `ConflictError`, `StorageError`, or `PluginError` escaping any MCP tool was not converted to `{"error", "code"}` at all — it propagated as an unstructured MCP protocol exception with no taxonomy code, and (for `StorageError`/`PluginError` specifically) without the message redaction both REST and GraphQL apply to avoid leaking internal exception text. The response *shape* also still diverged even where the code matched: MCP returned `{"error": str(exc), "code": ...}` while REST/GraphQL return `{"code", "message", "detail"}` — `exc.detail` was dropped entirely on the MCP side. Found in review of KI-059's own fix.

### Fix

New module-level `_error_response(exc: OntolithError) -> dict[str, Any]` — the MCP equivalent of REST's `_handle_ontolith_error`/GraphQL's `process_errors` override — redacts `StorageError`/`PluginError` messages via `isinstance` (matching GraphQL's own `_REDACT_MESSAGE_FOR` precedent, since MCP has no HTTP status to key a REST-style exact-type dict off) and returns `{"error", "code", "detail"}` uniformly. Every tool now wraps its entire body in one `try: ... except OntolithError as exc: return _error_response(exc)`, replacing every per-type `except` clause; the remaining non-exception error paths (a synthesized validation message, a manual `is None` not-found check, `_bearer_token`'s own auth-shaped strings) now construct a real exception instance and route it through the same helper, closing the shape divergence completely, not just the code values KI-059 closed. ADR-0014 update.

Round 1 review found the new blanket redaction turned a pre-existing mislabel in `Ontology.retract()` into an information-destroying one: an unknown `assertion_id` fell through to the backend's generic `set_assertion_status`, whose "no row updated" case raises `StorageError` — previously cosmetic (wrong code, right text reached the caller), now redacted into an opaque "An internal error occurred" everywhere `retract()` is exposed. First fix checked existence only inside the auto-accept transaction — round 2 review found that never ran on the review-routed path, exactly the one an AI/MCP caller takes (`ThresholdPolicy` always routes AI to review, ADR-0003): an unknown id from an AI caller produced no error at all, a phantom `require_review` proposal persisted silently, and accepting it later hit a bare `assert`, escaping as an uncaught `AssertionError`. Fixed properly: `retract()` checks existence unconditionally, before a proposal is even created or policy evaluated, raising `NotFoundError` regardless of routing — matching `flag_contradiction`'s existing precedent; the now-unreachable `assert` in `_replay_proposal_operations` was hardened into a real `NotFoundError` backstop too. Also added a cross-interface `StorageError` redaction-parity test (`test_cross_interface_error_codes.py`) and corrected an inverted "fails closed" comment on the `isinstance`-based redaction tuple (both MCP and GraphQL). ADR-0014 update.

---

## KI-075 — No interface can read `Contradiction.metadata`/`rationale_history` back ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — the data is captured but unreachable through any shipped interface
**Milestone target:** Backlog
**SPEC reference:** SPEC §10.3 (contradiction resolution)

### Description

KI-071 fixed `flag_contradiction()`'s `rationale` being silently dropped on the "extend" branch by accumulating it into `Contradiction.metadata["rationale_history"]`, readable via `Ontology`/the SDK. But `Contradiction.metadata` is not serialized by any of the four shipped interfaces: REST's `ContradictionOut` model, GraphQL's `_contradiction_type`, MCP's `ontolith.flag_contradiction` response dict (MCP has no contradiction query/list tool at all to serve it from either), and the CLI's `contradiction` sub-app's echo output all omit it entirely. A caller supplying `rationale` through any interface other than the raw SDK has no way to read back what was recorded — structurally the same gap KI-072 closed for the admin-action audit trail (data captured, no read surface), found during KI-071's own review.

### Fix

REST's `ContradictionOut` gained a `metadata: dict[str, Any]` field, populated identically by all three routes that return one (`GET /contradictions`, `POST /contradictions/flag`, `POST /contradictions/{id}/resolve`) — verified the trail survives `resolve` too, the one write among the three that touches `state`/`resolved_by`/`resolved_at` rather than `metadata` itself. GraphQL's `ContradictionType` gained `rationale_history: list[RationaleEntryType]` instead of a raw `metadata` field — GraphQL has no native map scalar (the same reason `FilterInput` already exists as an explicit key/value list), so the accumulated trail is projected out of `metadata["rationale_history"]` into a structured `{rationale, actor, at}` type rather than exposed as an opaque blob; `_contradiction_type()`, the one helper shared by the query and both mutations, is the single call site. MCP's `ontolith.flag_contradiction` response dict gained a `rationale_history` key carrying the *full* accumulated trail (not just the value passed to that call) — MCP still has no contradiction query/list tool at all (unchanged by this fix; the same gap the Description notes), so it's the one interface where reading the trail back still requires a propose-tier mutation call, not a pure read (filed as KI-076). CLI's `contradiction list` output gained a `rationale_entries=<n>` suffix per contradiction (omitted when there's no history, to keep the one-line-per-contradiction format) plus a `--show-rationale` flag that prints every entry's full text, matching `flag`'s own indented-entry format — added during review, since a bare count with no way to read the text on a pure read path defeated the point; `contradiction flag` echoes every accumulated entry the same way after its summary line, so a caller can confirm an "extend" call's rationale was appended rather than dropped without a separate `list` round-trip. `contradiction resolve` (CLI) has no equivalent flag — `resolve_contradiction()` takes no `rationale` input, and the accumulated trail on a resolved contradiction remains reachable via `contradiction list --state resolved --show-rationale`.

Two secondary items from round-2 review: `metadata`/`rationale_history` is an open blob (ADR-0041) with no schema enforcement. A first pass at defending GraphQL's/the CLI's entry-rendering used a per-field `.get(key, default)` instead of direct indexing, but that alone only covers a *missing* key — it still raised on a non-dict entry (`AttributeError`, e.g. `"rationale_history": ["a bare string"]`), a non-list `rationale_history`, or a present-but-`None` value (`.get` returns the key's actual value, `None`, not the default, which then fails GraphQL's non-nullable `RationaleEntryType` fields). Replaced with a shared `govern.contradiction.safe_rationale_history()` helper — used by GraphQL and the CLI at the time — that validates `rationale_history` is a list, skips non-dict entries, and coerces every field to a string, with dedicated unit tests (`tests/unit/test_contradiction.py`) pinning each malformed shape. REST alone doesn't need it: `ContradictionOut.metadata` returns the raw, unprojected blob (any shape is valid JSON) rather than a specifically-`rationale_history` view, so it never indexes into an individual entry. (MCP's own two tools were folded onto this same helper later, closing an identical gap — see KI-076.) Separately: REST's raw `metadata` pass-through means a database seeded before KI-071 (a `{"rationale": "..."}` single-key blob, the pre-KI-071 shape) is visible as-is through REST but renders as an empty `rationale_history` on GraphQL/MCP/CLI, all three of which look specifically for the `rationale_history` key — affects only pre-KI-071 dev databases, since no release tag exists yet and KI-071 deliberately didn't migrate old rows.

---

## KI-076 — MCP has no contradiction list/query tool at all, only `flag_contradiction` ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — the only way to read a contradiction back via MCP is a propose-tier write
**Milestone target:** Backlog
**SPEC reference:** SPEC §14.4 (MCP tool surface)

### Description

REST and GraphQL both expose a pure read path for contradictions (`GET /contradictions`, `Query.contradictions`) alongside their write operations. MCP has neither `ontolith.list_contradictions` nor any query-shaped tool covering contradictions — `ontolith.flag_contradiction` (propose-tier, mutates: extends membership, flips assertion statuses to `flagged`) is the *only* MCP surface that returns one. KI-075 added `rationale_history` to that tool's response, but an MCP-only caller who wants to inspect an existing contradiction (or its accumulated rationale) without triggering a write has no way to do so — noted as a pre-existing, out-of-scope gap during KI-075's own review, not introduced by it.

### Fix

New `ontolith.list_contradictions` tool (`state: str | None = "open"`, mirroring `Ontology.contradictions()`'s own default), `read`-tier like `ontolith.get`/`.query`/`.provenance` — no additional capability check beyond a resolved principal, since `read` is the floor of SPEC §8.3's total order. Returns the same `id`/`namespace`/`subject`/`predicate`/`state`/`member_ids`/`created_at`/`raised_by`/`resolved_by`/`resolved_at` fields as REST's `ContradictionOut`/GraphQL's `ContradictionType`, plus a `rationale_history` field shaped like GraphQL's own `rationaleHistory` projection (via the shared `govern.contradiction.safe_rationale_history()` helper KI-075 introduced) rather than REST's raw `metadata` blob — deliberate, matching `ontolith.flag_contradiction`'s own existing `rationale_history` precedent (also switched to the same helper in this fix, for identical behavior on a malformed blob across both MCP tools); the raw `metadata` field itself isn't exposed. Closes the last "propose-tier write is the only read path" gap left after KI-075.

`state` accepts `"open"`/`"resolved"` (filters), `"all"` or `None` (every state — both work identically; `"all"` is kept for cross-interface consistency with REST's `GET /contradictions`/GraphQL's `Query.contradictions`, which both need that exact string since an HTTP query string can't express "no filter" unambiguously the way MCP's JSON `null` can), or an error for anything else — round-1 review found the first version passed an unrecognized `state` value straight through to the backend's `WHERE state = ?`, silently matching zero rows (indistinguishable from "no contradictions exist") rather than rejecting it; fixed to validate against the four accepted values and return a `validation_error` otherwise (validated after auth resolution, not before, matching `ontolith.query`'s own `as_of`-validation placement). Now 9 MCP tools total (was 8 as of KI-067).

Round 2 found the same "malformed/unexpected shape silently corrupts or breaks" pattern one layer down, in `Ontology.flag_contradiction()`'s own "extend" branch (`ontology.py`, pre-existing since KI-071, not introduced by this fix): reading `existing.metadata.get("rationale_history", [])` before appending a new entry used the same unguarded pattern the read surfaces were just fixed for, so a malformed prior blob either raised (a non-iterable value) or, worse, got silently corrupted further on write (e.g. a bare string exploded into one list entry per character). Fixed by routing through the same `safe_rationale_history()` helper on this write path too, closing the one remaining place in the whole `rationale_history` life cycle without this defense. REST's `GET /contradictions` and GraphQL's `Query.contradictions` still pass their own unvalidated `state` argument straight to the same backend query, sharing the identical "unrecognized value silently matches zero rows" shape this fix closed for MCP specifically — filed as KI-077 rather than fixed here, since it's a pre-existing gap on two other interfaces, not part of this KI's own scope.

---

## KI-077 — REST's `GET /contradictions`/GraphQL's `Query.contradictions` pass an unvalidated `state` filter straight to the backend ✓ RESOLVED (Backlog)

**Severity:** Correctness gap — an unrecognized value silently returns an empty result rather than an error
**Milestone target:** Backlog
**SPEC reference:** SPEC §10.3 (contradiction resolution)

### Description

`GET /contradictions`'s `state: str | None = "open"` query parameter and GraphQL's `Query.contradictions(state: str | None = "open")` argument both flow straight into `Ontology.contradictions(state=...)` → `StorageBackend.contradictions(state=...)` → a `WHERE state = ?` clause. `Contradiction.state` is `Literal["open", "resolved"]`, so any other string (a typo, wrong case, a plausible-sounding synonym like `"unresolved"`) matches zero rows silently instead of erroring — indistinguishable from "no contradictions exist." REST's own docstring documents `state=all` as the sentinel for every state, so `state=All`/`state=ALL` (case mismatches of the interface's own documented value) hit exactly this. Found and fixed for MCP's newly-added `ontolith.list_contradictions` during its own review (KI-076); REST and GraphQL share the identical underlying gap, pre-existing before that fix and untouched by it.

### Fix

Both routes/resolvers now validate `state` before querying, raising `ValidationError` (400 on REST, `VALIDATION_ERROR` on GraphQL) for anything not in `{"open", "resolved", "all", None}` — the identical accepted set on both interfaces, since GraphQL's `Query.contradictions` already documents and accepts `"all"` too, the same as REST, not a narrower GraphQL-specific set limited to a JSON `null`. `GET /proposals`/`Query.proposals`'s own `state` parameter shared the identical shape (a larger accepted set: the 8 `Proposal.state` values plus `"pending"`/`"all"`/`None`) — fixed identically in the same pass, per this KI's own Fix text.

`Proposal.state`/`Contradiction.state` were extracted into named `ProposalState`/`ContradictionState` `Literal` type aliases (`govern/proposal.py`/`govern/contradiction.py`) rather than left inlined, so every interface's own accepted-value tuple can derive from `typing.get_args()` on the shared alias — the exact `plugins/registry.py` pattern already used for `PluginKind` — instead of a hand-duplicated tuple that could silently drift if either type ever gains or loses a state.

Review found the CLI (`ontolith proposal list --state`/`ontolith contradiction list --state`) had the identical bug, untouched by the initial REST/GraphQL fix and left with no open KI tracking it once this one closed — arguably the interface where a hand-typed typo is likeliest. Fixed in the same pass rather than filed separately: both commands now validate `--state` against the same derived tuple (skipped entirely when `--all` is passed, since the CLI has no `"all"` string sentinel for `--state` itself — that's its own separate boolean flag, unlike REST/GraphQL). MCP's `ontolith.list_contradictions` — hand-written with its own literal tuple when KI-076 shipped it, rather than derived — was also switched onto the same `get_args(ContradictionState)` derivation, closing the one remaining place a state Literal change could still silently desync one interface from the other three.

---

## KI-078 — A `RequireReview` decision's `reviewers` are never persisted or surfaced through any interface ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — a SPEC §9.4 MUST-level gap (`assign` is one of five named review actions), made newly consequential by KI-069
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §9.4 (review workflow — names an `assign` action), SPEC §9.2 (policy engine contract)

### Description

Every `PolicyStrategy` that returns `RequireReview(reviewers, reason)` computes a `reviewers` list, but nothing downstream ever stores or exposes it: `Proposal` has no `reviewers` field, `Ontology`'s non-auto-accept path (`_finalize_non_accepted_decision`) persists only `policy_reason`, and no `assign` review action exists anywhere in `src/` despite SPEC §9.4 naming one among five review actions it says MUST be recorded (`assign`, `comment`, `accept`, `reject`, `request_changes` — only the latter three actually exist today). This has been true since `ThresholdPolicy`'s own `reviewers=[]` default shipped in M1, but stayed low-consequence because every existing strategy's reviewer list was either always empty (`ThresholdPolicy`'s default case) or a secondary detail alongside an accept/review decision that mattered more on its own (`SourceQuorum`).

KI-069's `RequireReviewByRole` (ADR-0045) makes this gap materially worse: its *entire stated purpose* is choosing which reviewers a proposal routes to, not deciding whether review is needed at all (it never auto-accepts). Configuring it today produces a `Decision.reviewers` value that only a direct SDK caller inspecting the returned object ever sees — REST/MCP/CLI callers see only the free-text `policy_reason` (which does name the role, e.g. `"Requires review by role 'legal' (principal: a@x.com)"`, but that's a string a caller would have to parse, not a queryable assignment).

### Fix

Closed both halves in one PR (**ADR-0046**). New `Proposal.reviewers: list[str]` field, populated from `RequireReview.reviewers` at proposal-creation time and refreshed on `resubmit`'s policy re-evaluation (KI-027) — not cleared on accept/reject/request_changes, a historical record the same way `policy_reason` is. New `Ontology.assign_reviewers(proposal_id, reviewers, actor)` implements SPEC §9.4's `assign` action: replaces the reviewer list wholesale, records a `ProposalEvent(type="assign")`, and reuses the exact eligibility checks `accept_proposal`/`reject_proposal`/`request_changes` already share (capability, non-AI, no self-review — though review found this guard isn't currently load-bearing, since `reviewers` isn't itself enforced at accept time; kept for consistency, see ADR-0046). Exposed via REST only (`POST /proposals/{proposal_id}/assign`, `ProposalOut.reviewers` on every proposal route) — GraphQL/CLI/MCP deliberately deferred, filed as **KI-079**. **Breaking:** `StorageBackend` gains a required `update_proposal_reviewers()` method; both backends needed a schema migration for the new column on existing database files (DuckDB's `ALTER TABLE ADD COLUMN` rejects any constraint — `NOT NULL`, `UNIQUE`, `CHECK` all fail identically, verified directly during review — but a plain `DEFAULT '[]'` isn't itself a constraint, is accepted, and backfills existing rows in one statement; a fresh database's own `CREATE TABLE` still gets the stronger `NOT NULL DEFAULT '[]'`). Review also found a manual `assign_reviewers` call doesn't survive a later `resubmit` that lands back in `require_review` (that branch's policy re-evaluation overwrites `reviewers`) — a `resubmit` that instead auto-accepts or gets rejected leaves a manual assignment untouched — documented and pinned by tests covering all three resubmit outcomes, not fixed (deliberate).

---

## KI-079 — `Proposal.reviewers`/`assign_reviewers()` (KI-078) not exposed through GraphQL or CLI; MCP deliberately excluded ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — a deliberately scoped-down interface surface, not a defect
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §9.4 (review workflow — `assign` action), SPEC §9.2 (policy engine contract), SPEC §14.4/ADR-0008 (MCP surface — no direct-write tool)

### Description

KI-078 (ADR-0046) added `Proposal.reviewers` and `Ontology.assign_reviewers()`, exposing both through REST only (`ProposalOut.reviewers`, `POST /proposals/{proposal_id}/assign`). GraphQL's `ProposalType` still has no `reviewers` field (unlike REST's `ProposalOut`), and the CLI has no `proposal assign` command — a deployment using either interface can't read or change reviewer assignments through it, so `RequireReviewByRole` (ADR-0045) is actionable via REST only.

MCP is a different case, not just an unclosed gap: every MCP tool today is `read`- or `propose`-tier (`schema`/`get`/`query`/`provenance`/`list_contradictions` read; `propose`/`retract`/`resubmit`/`flag_contradiction` propose/author-tier) — none require `review`/`admin` capability. `assign_reviewers()` does, so an `ontolith.assign` MCP tool would be MCP's first review-capability write tool, a genuine new precedent against SPEC §14.4/ADR-0008's surface constraint, not a mechanical port of the REST route.

### Fix

Closed for GraphQL and CLI in one PR (**ADR-0046**'s own Update section). `ProposalType` gains `reviewers: list[str]`, projected by the same shared helper every other proposal-returning field already uses; new `Mutation.assignReviewers(proposalId, reviewers)`, structurally identical to `requestChanges`/`rejectProposal` — GraphQL's ninth mutation, still within ADR-0037 §1's query/propose/review scope. CLI gains `proposal assign <proposal_id> --actor <id> [--reviewer <id> ...] [--clear]` (`--reviewer` repeatable, replaces wholesale; `--reviewer`/`--clear` are mutually exclusive-by-requirement — passing neither is a usage error, not an implicit clear, since clearing is irreversible and the CLI has no other natural "the caller meant to clear everything" signal); `proposal list` gains a `reviewers=<comma-joined>` suffix when non-empty, mirroring KI-075's `rationale_entries=<n>` convention. No real consumer signal ever distinguished "GraphQL first" from "CLI first" for this one, and by the time it was picked up every other review action already had full three-interface parity, so both were closed together rather than picking one arbitrarily. MCP remains excluded, unchanged from this KI's own Description: `assign_reviewers()` would be MCP's first review-capability write tool, left for a real MCP consumer to motivate rather than added speculatively.

---

## KI-080 — `cardinality="many"` has no effect on `time_varying` properties — concurrent multi-valued facts silently collapse to one ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — `cardinality="many"` is unreachable for `time_varying` properties; current behavior is SPEC-conformant (§10.1/§10.2's pseudocode never mentions `cardinality`), so this is a design-scope gap in `cardinality`, not a SPEC violation
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §4 (`cardinality: single (default) | many`), SPEC §10.1/§10.2 (temporal supersession routing, no `cardinality` parameter in either); ADR-0017 (cardinality-aware routing, amended by ADR-0050)

### Description

`govern/conflict.route()` accepts a `cardinality` parameter but only threads it into `_route_static()` — `_route_time_varying()` takes no `cardinality` argument at all and supersedes *every* existing assertion whose window overlaps the incoming one and whose value differs, regardless of what the schema declares. Reproducible directly: declaring `Person.role` as `cardinality="many", temporality="time_varying"` and asserting two different, genuinely-concurrent values with the same `valid_from` causes the second write to immediately supersede the first, collapsing what the schema says should be an independently-tracked pair of concurrent facts (e.g. two concurrent job titles) down to one. SPEC §10.1/§10.2's own routing pseudocode never references `cardinality` at all, so today's behavior does not violate SPEC as written — it's `cardinality`'s own §4 contract that reads as unconditional ("single (default) | many") but is, in practice, only honored for `static` properties.

This is not an oversight — ADR-0017 (which introduced cardinality-aware routing for `static` properties) explicitly considered and declined to extend it to `time_varying`: "two overlapping-window, differing-value `time_varying` assertions are a genuine supersession regardless of cardinality... the newer one replaces the older." That reasoning holds for the *single*-concurrent-value case (an update to the one fact that's true right now), but it implicitly assumes there's only one "slot" to replace — which is exactly what `cardinality="many"` says isn't true. The ADR's own stated goal for `static` — "a schema author declaring `cardinality=many` has no way to express that intent" — applies identically to `time_varying`, and today it's just as unaddressed there as it was for `static` before ADR-0017.

### Fix

The fix direction was chosen over two alternatives (always-coexist with no new parameter; affirm ADR-0017's original decision and leave the gap open) — recorded in new **ADR-0050**: `govern/conflict._route_time_varying` now consults `cardinality`. A `many`-cardinality overlapping differing value coexists by default (`Activate()`, mirroring `_route_static`'s own "many" branch — the behavior change from before this fix, which superseded unconditionally). A new `supersedes: str | None = None` keyword parameter on `assert_literal()`/`assert_ref()`/`propose()`/`propose_ref()` lets a caller explicitly name the one specific existing assertion to replace instead (`Supersede(targets=[supersedes])`) — every other overlapping-differing concurrent value stays untouched, so a hint for one replacement never silently sweeps in unrelated values. `cardinality="single"` is completely unaffected — that branch never consults the hint at all, since routing is already unambiguous there.

Two-layer validation: `Ontology._require_valid_supersedes_hint` rejects a non-`None` hint outright (`ValidationError`) unless the predicate resolves to `cardinality="many"`/`temporality="time_varying"`, checked once at submission time (mirroring `_require_existing_subject`/`_require_existing_target`'s "fail loud before the write reaches routing" convention); `route()` itself validates that the hint names a real, active, same-`(subject, predicate)`, overlapping-and-differing assertion, against a fresh read of `existing` — a plain `ValueError` there, translated to `ValidationError` in `_apply_with_conflict_routing`, the one call site where `govern/conflict`'s pure-layer contract violations cross into the stable, interface-facing error taxonomy (SPEC §16). For `propose()`/`propose_ref()`, the hint rides along in the staged payload (`op["supersedes"]`) and `route()`'s own membership check re-runs fresh at accept/resubmit replay time — the same "checked once at submission, re-validated fresh at apply time" pattern `temporality` itself already has in `_replay_proposal_operations`; a schema change between submission and replay that moves the predicate out of scope leaves the hint silently unconsulted rather than re-validated late, matching that method's existing, documented temporality-drift precedent, not a new gap.

**Deliberately out of scope:** REST/GraphQL/MCP/CLI parity — `supersedes` was SDK-only when this KI shipped, matching this project's established pattern of shipping a capability SDK-first and filing interface parity separately (e.g. KI-081 → KI-094/KI-096). **Filed as its own follow-up: KI-099** (below — now resolved).

New tests: `conformance/test_conflict.py::TestManyCardinalityTimeVaryingSupersedes` — pure `route()`-level cases (coexist without a hint; supersede exactly the named target while a second concurrent value stays untouched; a hint not among `existing`, pointing at a same-value assertion, or pointing at a non-overlapping window all raise the identical `ValueError`; `cardinality="single"` ignores the hint entirely, including a bogus one, proven with two overlapping targets so a coincidental single-target match can't hide the difference) plus storage-backed, both-backends end-to-end cases through `Ontology` (default coexistence; explicit-hint replacement via `assert_literal`, with the closed window and the untouched second value both checked; the same via `propose()`'s auto-accept path; the relation equivalent via `assert_ref` *and*, separately, `propose_ref`'s own auto-accept path, on a new `Person.colleague` time_varying+many relation; `supersedes` rejected for a `static` property, for `cardinality="single"` `time_varying`, for an unknown hint id, and for a hint naming a real assertion on the wrong subject; a regression guard confirming `cardinality="single"` `time_varying` supersession is completely unchanged); a `TestManyCardinalityTimeVaryingSupersedes`-scoped block covering `accept_proposal` replay specifically — a hint honored through a `require_review` proposal's later acceptance, a stale hint (its target retracted by a neutral party before review) rejected at accept time rather than silently dropped or crashing, and a schema drift to `cardinality="single"` between submission and replay leaving the hint unconsulted while still correctly superseding every overlapping value under the drifted rules.

A review round found the fix above was itself incomplete in one genuine way: `_route_time_varying`'s "nothing overlaps" short-circuit ran *before* the hint was ever consulted, so a stale or typo'd `supersedes` hint silently vanished (routed to plain `Activate()`, no error) whenever no *other* existing assertion happened to overlap the incoming write too — including the common case of a fresh subject/predicate, or a client retrying a correction whose original target had already left the active set. Reproduced directly (a nonexistent hint id against an empty `existing` list accepted with no error) before fixing: the cardinality="many" hint-membership check in `_route_time_varying` now runs unconditionally, ahead of that short-circuit, so a hint is always either honored or rejected, never silently dropped, regardless of what else exists. The round also found two coverage gaps matching this: `propose_ref`'s own `supersedes` wiring was never actually exercised (a same-named test called only `assert_ref`) and the `accept_proposal`/`resubmit` replay path — the most intricate part of the change, per its own docstring — had zero tests despite the behavior being asserted in prose in four places; both closed with new tests (see above). Also fixed: a weak "single ignores the hint" test whose single-target setup couldn't distinguish "ignored" from "coincidentally matched"; the ADR-0050 Consequences section's odd placement of the real breaking-change (default coexistence) under a separate `**Breaking:**` heading about the `route()` signature instead of where the actual behavior change belongs; several stale "filed as its own follow-up KI" claims that had no KI number behind them, now naming **KI-099**; a couple of cross-reference/wording nits (ADR-0017's own `**Related**` field, this document's own Implementation Plan status date).

A second review round independently re-verified the fix above via mutation testing (each new/changed test confirmed to fail on exactly the mutation it was meant to catch, on both backends) and found the code itself correct throughout — every remaining finding was documentation: this document and the Implementation Plan had picked up a literal agent-tool name in describing how the fix direction was chosen (removed, with no attribution to a mechanism this project's docs don't otherwise name); one of four "filed as its own follow-up" cross-references still lacked the **KI-099** number the others had already gained; and KI-099's own interface-by-interface description named GraphQL mutations and a CLI subcommand that don't exist (`Mutation.assertLiteral`/`assertRef`/`proposeRef`, `ontolith propose`) — rewritten against the actual REST/GraphQL/CLI source (GraphQL's only mutation that asserts a value at all is `Mutation.propose`, fed by one discriminated `ProposeInput`; the CLI has `ontolith assert`, literal-only, and no `propose` subcommand). Also: the ADR-0050 `**Breaking:**` note moved from a standalone paragraph to a bullet inside Negative/follow-ups, matching ADR-0042/ADR-0046's own precedent exactly; ADR-0050's own claim about which private-function parameters were newly added corrected (`_route_time_varying` gained both `cardinality` and `supersedes_hint`, not just the latter); and the stale-target replay test now also pins that a failed accept leaves the proposal exactly `require_review`, retryable and failing the same way, not just that it raises once. A third round confirmed the code unchanged and found one more real gap: `route()`'s own docstring said the caller is responsible for validating the hint's existence/activeness before calling `route()` — the opposite of what `_require_valid_supersedes_hint`'s own docstring and ADR-0050 both say `route()` itself does — reworded to state the precondition `existing` actually carries (already scoped to active, same-(subject, predicate) assertions) rather than a precondition about the hint nothing ever enforced.

---

## KI-081 — `QueryBuilder` never exposes SPEC §11.2's `.include_flagged()`/`.include_history()` opt-ins ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — a SPEC MUST-adjacent requirement unmet at the primary query API
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §11.2 ("Flagged/superseded/retracted assertions are excluded by default; `.include_flagged()` / `.include_history()` opt in"), SPEC §10.3 ("A flagged assertion... MUST be excluded from default (unflagged) retrieval unless explicitly requested")

### Description

`StorageBackend.entities_where()` accepts an `include_flagged: bool` parameter (`store/base.py:403`), but `QueryBuilder._base_candidates()` never passes it through — the call defaults it to `False` unconditionally, and `QueryBuilder` has no `.include_flagged()`/`.include_history()` method at all (confirmed: neither name appears anywhere in `query/builder.py`). (`entities()`, the other port method `_base_candidates()` can call, has no `include_flagged` parameter at all — only `entities_where()` and `assertions()` support it.) `store/base.py`'s own docstring already discloses this in passing, and a passing mention in `docs/known-issues.md`'s own KI-040 discussion (`.include_history()`) implies this is live functionality when it isn't. The gap is real but partially mitigated by existing SDK-level access that doesn't go through `QueryBuilder`: `kb.assertions(status="flagged")` returns all flagged assertions unscoped, and `kb.contradictions()`/MCP's `ontolith.list_contradictions` (KI-076) already answer "which entities have an open contradiction" directly — so the missing piece is specifically entity-level filtering *through `kb.query(Concept)`*, not flagged-assertion visibility altogether.

SPEC §10.3's MUST ("A flagged assertion is retained and queryable but MUST be excluded from default (unflagged) retrieval unless explicitly requested") is satisfiable today via those SDK-level calls, but not through the primary, documented `QueryBuilder` fluent API (`kb.query(Person).where(...)`) — despite SPEC §11.2 naming `.include_flagged()`/`.include_history()` as `QueryBuilder`'s own opt-ins.

### Fix

Both opt-ins built, as **match-set wideners** — they change only which assertion statuses a `.where()` filter may match against; `.all()`/`.first()`/`.count()` keep returning `Entity`/`Entity | None`/`int` (**ADR-0048** records this over the "return per-entity timelines" alternative). Default match set is `active`; `.include_flagged()` adds `flagged`; `.include_history()` adds `superseded` + `retracted`; the two are independent (neither implies the other). No effect on a query with no `.where()` filter and (since KI-093) no `.min_confidence()`/`.trust_at_least()` floor either — nothing to widen. `.include_flagged()` still applies under `.as_of()`, as it has since before this KI (a flagged assertion's open validity window already makes it visible unless excluded); `.include_history()` is a no-op there for `superseded` — the `as_of` branch never restricts matches to `active` in the first place (it excludes only `flagged`, itself gated behind `.include_flagged()`), so a superseded assertion whose window covers the queried instant already matches without this opt-in. A `retracted` assertion is the exception, closed later by **KI-095/ADR-0049** (below): `as_of` additionally excludes it once its own retraction event's assertion-time has passed, and `.include_history()` is what opts back into seeing it.

`QueryBuilder` gained `.include_flagged()` / `.include_history()` chainable methods + `_include_flagged`/`_include_history` fields, threaded through to `entities_where()` from both `_base_candidates()` and `_semantic_candidates()`. `StorageBackend.entities_where()` (port + both adapters) gained `include_history: bool = False`; each adapter's current-state branch replaced its hard-coded `" AND status = 'active'"` with a parameter-bound `" AND status IN (?, …)"` built from the widened status list (the `as_of` branch's `include_flagged` handling was already correct and is unchanged). SPEC §11.2's wording tightened to name which statuses each opt-in adds.

New `tests/unit/test_query.py::TestIncludeFlaggedAndHistory` (15 cases: each status excluded by default and matched on the right opt-in; the two flags' independence; both-together; the widener invariant — an *active* value is still returned under each opt-in, so a mutation that drops `active` from the status set is caught, not just a whole-branch revert; filter-less no-op; chainable; `.count()`/`.first()` parity; `.as_of()` + `.include_flagged()`; a two-`.where()`-filter case; a `.semantic().where().include_flagged()` case exercising the `_semantic_candidates()` threading). Mutation-tested: dropping `active` under either flag fails the invariant case; removing the `_semantic_candidates()` threading fails the semantic case; reverting the whole SQLite `else` branch fails the 5 match-expecting cases. New `tests/unit/test_sqlite_backend.py::test_entities_where_status_widening_flags` at the port level. New `conformance/test_include_flagged_history.py` (6 cases × both backends = 12) since `Ontology.connect()` only ever builds SQLite and the widening lives in each adapter.

Found reviewing this KI, filed as follow-ups: `.include_*()` didn't compose with `.min_confidence()`/`.trust_at_least()` — those still filtered to `active` only — **closed by KI-093** (below); the three interface `query` surfaces don't forward the new opt-ins — **closed by KI-094** (below); and a retracted assertion with a future `valid_to` stays visible in `.as_of()` queries with no opt-out (pre-existing bitemporal gap) — **closed by KI-095/ADR-0049** (below), whose own Update section now records the fix ADR-0048's original body could only note as an open gap.

---

## KI-082 — No entity-creation capability on REST, GraphQL, or MCP — CLI-only ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — a real interface-parity/DX gap; note SPEC §14.1's normative SDK surface doesn't itself list `create_entity`, and §14.4's MCP tool table is an enumerated closed default set that excludes it, so this reads as an unaddressed gap rather than an unmet SPEC MUST
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §14.1 (normative SDK surface), §14.3 (REST resource list names `/entities`, but only `/proposals` carries a documented `POST`; GraphQL scoped to "query, propose, and review operations"), §14.4 (MCP default tool table)

### Description

`Ontology.create_entity()` is exposed on exactly one interface: the CLI (`entity create`, `interfaces/cli.py:251`) and the plugin sandbox facade (`plugins/views.py:97`). REST has no `POST /entities` route (only `GET /entities/{entity_id}`) despite SPEC §14.3 listing `/entities` among REST's resources — the spec text doesn't say what verbs it supports beyond naming `POST` explicitly for `/proposals`, so this reads as ambiguous rather than clearly unaddressed by REST's own contract. GraphQL's `Mutation` type has no `createEntity`, and MCP has no `ontolith.create_entity`-style tool (confirmed: zero hits for entity-creation across `rest.py`/`graphql.py`/`mcp.py`). None of the governed write paths those three interfaces *do* expose (`propose`/`propose_ref`/`assert_literal`/`assert_ref`) validate or create the target `subject` entity — they all assume it already exists.

This means an AI agent or application talking only to REST/GraphQL/MCP can assert facts about entities that already exist, but can never introduce a genuinely new individual into the KB — a real limitation on the "agent proposes new knowledge" story those interfaces exist to serve, and a first-run trap for a REST/GraphQL adopter who hasn't also reached for the CLI or raw SDK. Worth weighing against KI-079's precedent for *not* adding an MCP tool speculatively (there, `assign_reviewers` was deliberately left out pending a real consumer) — entity creation is a stronger case since REST/GraphQL/MCP callers otherwise have no path to it at all, not merely a narrower one.

### Fix

Exposed `create_entity` on all three interfaces, each a thin wrapper over `Ontology.create_entity()` with no new capability logic of its own (propose-tier, matching the SDK method's own existing gate — rejects only `read`-only principals, no AI-kind check, since an entity carries no fact/confidence/temporality for policy to evaluate):

- REST: `POST /entities` (`CreateEntityIn` body: `concept`, optional `natural_key`; returns `EntityOut`, `201`)
- GraphQL: `Mutation.createEntity(input: CreateEntityInput!): EntityType` — module docstring's "query, propose, review" scope description updated to name this as the one exception, with its own rationale
- MCP: `ontolith.create_entity` — module docstring/tool table updated with the analogous rationale (entity creation was never proposal/policy-gated at the SDK level either, so this isn't a "direct write" in ADR-0008's sense); `test_all_required_tools_registered` (KI-085's own exact-set test) deliberately updated to 10 tools, not left to silently drift

**Review found this genuinely moves two documented scope boundaries, not just adds a route within them** — ADR-0008's closed, numbered "Exposed Tools" list and ADR-0037 §1's explicit "query, propose, and review only" GraphQL scope statement (with its own 2026-09-05 update asserting the boundary "hasn't moved, only the roster" for the two additions before this one — true then, not true of `createEntity`). Both ADRs updated with a dated `## Update` section recording the addition and why it's still the right call despite moving the boundary (see each ADR directly). Also filed, not left implicit: two pre-existing SDK-level gaps this change makes agent-reachable for the first time — `concept` isn't validated against the schema (KI-090) and a duplicate `natural_key` surfaces as a redacted `StorageError` (KI-091).

Each interface's existing "every route/field/tool requires auth" exact-coverage tests (`TestAuthCoversEveryField`'s mutation-field-probe set on GraphQL, the tool-count test on MCP) needed conscious updates too, confirming those safety nets work as designed rather than silently passing around the new surface. New dedicated test classes on all three interfaces cover: creation success, round-trip retrieval (not just a response-shape check), `propose` capability sufficing (including for an AI principal — no AI-blocking check exists on this path), and `read` capability being rejected.

---

## KI-083 — Asserting against a nonexistent subject surfaces as an opaque, redacted `StorageError` instead of `NotFoundError` ✓ RESOLVED (Backlog)

**Severity:** Bug — wrong error taxonomy for caller-input error
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §16 (error taxonomy — `NotFoundError` for missing entity/assertion/namespace vs. `StorageError` for backend failure)

### Description

None of `propose()`/`assert_literal()`/`assert_ref()`/`propose_ref()` pre-check that the `subject` entity id actually exists before writing — they rely entirely on the DB's `FOREIGN KEY` constraint to fail late, inside the write, which raises a generic `sqlite3.IntegrityError` caught only as `StorageError(f"Assertion conflict (id=..., subject=...): FOREIGN KEY constraint failed")`. Reproduced directly: `kb.assert_literal("nonexistent-entity-id", ...)` raises exactly this. `StorageError` is one of exactly two error types (`interfaces/rest.py`, `interfaces/mcp.py`, `interfaces/graphql.py`) whose message is deliberately redacted before being returned to a caller, since it's meant to represent internal/unexpected backend failures, not caller input mistakes — so a REST/GraphQL/MCP caller who typos an entity id gets a generic, message-redacted 500-class error with no indication of what went wrong, and the message that *is* logged server-side ("conflict") actively misdescribes the actual problem (a missing row, not a conflicting one).

### Fix

Added `Ontology._require_existing_subject(subject)`, called from all four write paths (`assert_literal`/`assert_ref` right after the direct-write capability check; `propose`/`propose_ref` right after the AI-model check, before the proposal is even constructed — sufficient because entities are never deleted, so a subject validated at submission time can't later become invalid by accept time), raising `NotFoundError(f"Entity not found: {subject!r}")` (message later changed to `"Subject not found: ..."` when KI-089 added the analogous `target` check and split it into a separate method with its own distinct message). `NotFoundError` is not in any interface's message-redaction list, so the real message now reaches REST/GraphQL/MCP callers unchanged; each interface's existing generic `OntolithError` handler required no changes. Mutation-tested directly: reverting each of the four call sites one at a time reproduces the original `StorageError`/FOREIGN KEY failure and is caught by exactly its own new test, no cross-coverage. A new `tests/unit/test_cross_interface_error_codes.py::TestSubjectNotFoundMessageParity` test locks in the unredacted-message behavior specifically (not just the error code, which alone wouldn't catch `NotFoundError` silently joining a redaction list) — mutation-tested the same way, by adding `NotFoundError` to `graphql.py`/`mcp.py`'s own `_REDACT_MESSAGE_FOR` tuples and confirming the new test catches it.

**Behavior note:** on all four paths, this check now runs before `_require_known_predicate`, so a call with both an unknown subject and an unknown predicate raises `NotFoundError` where it previously raised `ValidationError`. No caller depended on the old ordering (full suite unaffected).

Found and filed separately while implementing this (KI-089, closed the following day): `assert_ref`/`propose_ref`'s `target` (stored in the `assertion` table's `value_ref` column) had no equivalent check — and unlike `subject`, had no `FOREIGN KEY` backing it either (confirmed on both SQLite and DuckDB), so a nonexistent target silently succeeded rather than erroring at all.

---

## KI-084 — No documented or enforced cross-process write-safety guarantee for the SQLite default backend ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — a real deployment-safety gap, but the actual failure mode is a hard, opaque error under contention, not silent data corruption
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §12.1 ("single SQLite database file... require no external services")

### Description

The entire concurrency story for `SQLiteBackend` (ADR-0010's KI-023 update) is an in-process `threading.RLock` around one shared connection; `begin()` issues a plain `"BEGIN"`, not `"BEGIN IMMEDIATE"` (confirmed: `store/sqlite/backend.py`). ADR-0001 already names "Single-writer limitation (fine for proposals/review workflow)" as a known consequence of the SQLite choice, but that's a one-line acknowledgment at the storage-selection level, not an elaborated deployment constraint — nothing describes what actually happens under two-process contention or what an operator running more than one server process should do about it.

Verified empirically (WAL mode is on — `PRAGMA journal_mode = WAL`, `backend.py:133` — with two separate connections to the same file: connection B `BEGIN`s and reads active assertions for conflict routing, connection A commits a write to the same row via `BEGIN IMMEDIATE`, then B attempts its own write): the second writer does **not** silently race to an inconsistent result — it fails hard, with `sqlite3.OperationalError: database is locked`, in well under a millisecond. That's *not* ordinary lock contention timing out: `sqlite3.connect()` (`backend.py:110`) takes no explicit `timeout=`, so Python's 5-second default `busy_timeout` is actually in effect (verified: `PRAGMA busy_timeout` reports `5000` on a real backend connection, and a plain write-write contention test on the same connect args does block for the full ~5s before raising, confirming the retry mechanism itself works). The immediate failure here is a *different*, non-retryable case: B already took a read snapshot under a deferred `BEGIN`, and upgrading that stale snapshot to a write is `SQLITE_BUSY_SNAPSHOT`, which SQLite deliberately does not route through the busy handler at all — no amount of `busy_timeout` would help, since retrying can't succeed until B's transaction rolls back and re-reads. Because `StorageError` is caught broadly around `sqlite3.Error` (`store/sqlite/backend.py:1115`) and redacted at every interface boundary (`rest.py`/`graphql.py`/`mcp.py`), a concurrent writer under real multi-process load sees an opaque, message-redacted 500-class error with nothing indicating it's a stale-snapshot conflict it could safely retry from scratch — a hard, confusing error under a workload SQLite itself is actually preventing from corrupting, not the silent inconsistency a read-then-route race might otherwise suggest.

### Fix

`SQLiteBackend.begin()` (`store/sqlite/backend.py`) now issues `BEGIN IMMEDIATE` instead of a plain deferred `BEGIN`, applying to all fourteen `with self.backend.transaction():` call sites in `ontology.py` (`create_principal`, `assert_literal`, `assert_ref`, `propose`, `propose_ref`, `retract`, `accept_proposal`, `reject_proposal`, `request_changes`, `assign_reviewers`, `resubmit`, `resolve_contradiction`, `flag_contradiction`, `apply_schema`) with no call sites touched individually — but the stale-snapshot race it closes only existed for the subset of those whose pre-write read genuinely runs *inside* that block: `assert_literal`/`assert_ref` (SPEC §10 conflict routing), `propose`/`propose_ref`'s auto-accept branch, `accept_proposal`, and `resubmit`'s auto-accept branch (via `_replay_proposal_operations`). A concurrent writer contending on any of the fourteen is now serialized behind SQLite's ordinary busy handler instead of hitting the non-retryable `SQLITE_BUSY_SNAPSHOT` path (for the ones with no in-block read, there was no such race to begin with — `BEGIN IMMEDIATE` still applies there, just with nothing to protect). `sqlite3.connect()` (`SQLiteBackend.__init__`) now passes `timeout=5.0` explicitly, pinning what was previously Python's implicit default — meaningful now that `begin()` actually reaches the busy handler. See ADR-0001's own Update section for the two write shapes this fix deliberately does not reach: KI-092 (writes with no `transaction()` at all — since resolved: `create_entity` was given its own `transaction()` wrapper, the others left as deliberate non-races) and the already-resolved KI-035 (policy evaluated against a pre-transaction read in `propose`/`propose_ref`/`retract`).

`begin()`'s exception handling distinguishes the two failure shapes: an `sqlite3.OperationalError` whose extended error code's low byte is `SQLITE_BUSY` (`code & 0xFF == sqlite3.SQLITE_BUSY` — masking needed because Python's `sqlite3` surfaces the *extended* code, e.g. `517` for `SQLITE_BUSY_SNAPSHOT`, confirmed directly rather than assumed) raises a `StorageError` reading "lock contention... safe to retry"; every other `begin()` failure keeps the prior generic message. Still redacted at every interface boundary like any other `StorageError` (KI-083's precedent), so this is a server-log-only improvement, not a wire-visible one — an operator reading logs can now tell transient contention from a genuine fault. This is a deliberately narrower reading of the KI's own Fix text than "distinguish... in the error taxonomy" literally suggests: a message-level distinction inside the existing `StorageError` type, not a new `code` value or a new exception class — reviewed and kept as-is rather than widened, since the message is redacted at every boundary regardless (no API caller can observe `code` either way) and a new taxonomy member for exactly one call site would be the kind of premature abstraction this project's own conventions single out. If a genuinely programmatic "is this retryable" signal is ever needed by a caller (not just an operator reading logs), that's a new decision warranting its own ADR, not an extension of this fix.

Added a dated `## Update (2026-09-09, closes KI-084)` section to `docs/adr/ADR-0001-storage-default.md`, elaborating the one-line "single-writer limitation" consequence already on record there into the actual deployment implication: one process should hold the write path per database file; a second process no longer risks the instant `SQLITE_BUSY_SNAPSHOT` failure for the write paths named above — not a claim that every `Ontology` write is now cross-process-safe, and still serializes rather than parallelizes even where it does apply (a throughput consideration, not a correctness one). Two narrower gaps remain, cross-referenced rather than fixed here: `create_entity`/`issue_token`/`revoke_token`/`reindex` write outside any `transaction()` block at all (filed as KI-092 — since resolved: `create_entity` was wrapped in a `transaction()`, the other three left as deliberate non-races; KI-092 also notes `propose`/`propose_ref`/`retract`'s reject/require-review outcome as the same shape); and `propose`/`propose_ref`/`retract` evaluate policy against a read taken before any transaction opens, a pre-existing, deliberately accepted tradeoff from the already-resolved KI-035, not reopened by this fix. Scaling writes across processes is out of scope for the SQLite default and belongs to a server-backed `StorageBackend` instead. The same Update section also documents a cost this fix introduces rather than merely inherits: `self._lock` is acquired before `BEGIN IMMEDIATE`, so a `begin()` genuinely contended by another process now blocks every other call on *this* process — reads included — for up to the full `busy_timeout`, measured directly at ~0.99s against a 1s timeout; previously a deferred `BEGIN` returned near-instantly regardless of contention, so this specific stall is new, not merely newly documented. A retry-loop alternative that would bound the stall was considered and recorded as rejected in that section (disproportionate complexity for a condition the single-writer-process topology recommendation already exists to avoid).

Regression-tested directly with two independent `SQLiteBackend` instances on the same file (standing in for two OS processes, since `self._lock`, KI-023's `threading.RLock`, is per-instance and doesn't itself serialize them) — `tests/unit/test_sqlite_backend.py::TestConcurrency::test_begin_blocks_on_cross_process_writer_then_reports_retryable` and `test_begin_retries_and_succeeds_once_cross_process_writer_releases` — plus two connection-stand-in tests exercising `begin()`'s exception handling directly: `test_begin_reports_generic_message_for_non_busy_operational_error` (a non-`SQLITE_BUSY` `OperationalError` must keep the generic message) and `test_begin_masks_extended_busy_variant_as_retryable` (a synthetic `SQLITE_BUSY_SNAPSHOT`, 517, must still be masked to the primary `SQLITE_BUSY` code, 5, and reported as retryable — added after a review round found the first three tests alone never exercised a case where `code & 0xFF == sqlite3.SQLITE_BUSY` and plain `code == sqlite3.SQLITE_BUSY` disagree, since the real contention site only ever produces literal 5 once `BEGIN IMMEDIATE` is in place). Mutation-tested: reverting `BEGIN IMMEDIATE` to `BEGIN` makes the first test fail (contention no longer even attempted at `begin()` time — `DID NOT RAISE`); removing the whole `except sqlite3.OperationalError` branch (falling through to the generic `except sqlite3.Error` handler) makes the cross-process and extended-variant tests fail on their "safe to retry" assertions, while the non-busy fallback test correctly keeps passing — its coverage is catching the opposite mistake, an *over-broad* mask, confirmed directly by mutating the condition to `if True:` and watching it fail; narrowing `code & 0xFF == sqlite3.SQLITE_BUSY` to plain `code == sqlite3.SQLITE_BUSY` is caught specifically by the extended-variant test (confirmed directly — the other three tests all still pass under that mutation). A review round also caught that `..._releases` originally passed identically whether or not `begin()` actually contended on anything (its `elapsed`-blocking assertion was missing) — confirmed by reproducing the false-pass directly, then fixed and reconfirmed the fix now fails correctly under the same reverted-`begin()` mutation. A manual timing check (500 `assert_literal` calls, uncontended, after warm-up) confirmed no regression against SPEC's `propose + policy eval + commit: p95 < 50ms` budget: p95 ≈ 0.2ms — `BEGIN IMMEDIATE` costs nothing extra absent actual contention, matching SQLite's documented behavior.

---

## KI-085 — MCP's "no write tool" test is a blocklist/subset check, not a closed-set check ✓ RESOLVED (Backlog)

**Severity:** Test gap — protects the single most safety-critical guarantee in the project, currently via a heuristic
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §14.4 ("MCP exposing no direct-write tool"), SPEC §19 conformance item 5

### Description

`tests/unit/test_mcp_server.py::test_no_write_tool_registered` asserts the registered tool-name set is disjoint from a 4-item blocklist (`{"ontolith.write", "ontolith.update", "ontolith.delete", "ontolith.assert"}`); `test_all_required_tools_registered` asserts a 9-item `required` set is a *subset* of the registered names, not equal to them. Neither test would fail if a future PR added a 10th tool with unrestricted write semantics under any name other than the four blocklisted strings (e.g. `ontolith.commit`, `ontolith.apply`) — the exact scenario KI-079 explicitly declined to do for `assign_reviewers` (documented as deliberately excluded, "would be MCP's first review-capability write tool"), but nothing structurally stops a less careful future change from doing it accidentally.

### Fix

Changed `test_all_required_tools_registered`'s assertion from `required.issubset(tool_names)` to `tool_names == required` (`tests/unit/test_mcp_server.py`) — `test_no_write_tool_registered`'s blocklist check is kept alongside it, since its failure message documents *why* those four names specifically are forbidden, but the exact-set check is now what actually closes the gap. Mutation-tested directly: registering a 10th tool under a non-blocklisted name (e.g. `ontolith.commit`) still passes both the old blocklist and the old subset check, but fails the new equality check — confirming this is exactly the scenario the fix closes.

---

## KI-086 — Provenance assembly is hand-duplicated across REST, GraphQL, and MCP instead of living once in the domain layer ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — a recurring source of the exact parity-gap class this session's KI history keeps finding
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §5.4 ("Provenance is a derived view... MUST be able to return it for any assertion in one call"), SPEC §14.1 (Python SDK sketch: `Entity.history()`, `provenance(predicate)`, `contradictions()` — non-normative, see ADR-0047)

### Description

Each of REST's `provenance_route`, MCP's `provenance_tool`, and GraphQL's `_build_provenance` independently reassembles the same `backend.get_assertion()` + `get_proposal_events()` + `get_assertion_events_by_successor()` logic — three separate implementations of one concept, not one shared implementation called three times. `core/entity.py` is a flat, frozen Pydantic value object with no methods at all, and `Ontology` has no `history`/`provenance` method under any name — so SPEC §14.1's own normative SDK-level shape (`Entity.history()`, `Entity.provenance(predicate)`, `Entity.contradictions()`) doesn't exist either; the capability is satisfied only at the interface layer, three times over, never at the domain layer once.

This project's own KI history (KI-058/059/075/076/077/079, among others) shows this exact pattern — logic implemented once per interface — repeatedly causing parity gaps that needed a dedicated KI each time to close after the fact. A shared domain-layer method removes that recurring risk class at the source rather than requiring another cross-interface parity sweep the next time provenance logic needs to change.

### Fix

Added `Ontology.provenance(assertion_id: str) -> Provenance` (`ontology.py`, next to `assertions()`) — the single domain-layer implementation of SPEC §5.4's one-call view. Returns a new frozen `Provenance` value object (`govern/provenance.py`, exported from `ontolith.govern`, added to `test_public_api_surface.py`'s pinned surface) carrying `assertion: Assertion`, `review_events: tuple[ProposalEvent, ...]`, and `superseded_ids: tuple[str, ...]`. REST's `provenance_route`, GraphQL's `_build_provenance`, and MCP's `provenance_tool` each dropped their copy of the `get_assertion` + None-check + `get_proposal_events` + `get_assertion_events_by_successor` assembly and now call `kb.provenance()`, shaping its result into their own `ProvenanceOut` / `ProvenanceType` / dict (that field-copying is genuinely per-interface — Pydantic vs Strawberry vs JSON — not duplicated *logic*). `NotFoundError` for an unknown id is now raised once by `provenance()`; each interface's existing error mapping handles it unchanged, so the wire behavior (`404` / GraphQL error / MCP `_error_response`) is identical.

The two SPEC §14.1 sketch-vs-shipped drifts KI-086 also named are settled in **ADR-0047** rather than by changing code: `Entity` deliberately stays a pure value object with no backend handle, so `Entity.history()`/`provenance()`/`contradictions()` are **not** added — their equivalents are `Ontology.assertions(status=None)` / `Ontology.provenance()` / `Ontology.contradictions()`; and the `create_principal()`/`get_principal()` split is intentional (mint vs look-up differ in capability gating and error contract), so no `kb.principal(...)` alias. SPEC §14.1 gained a note flagging these as ADR-recorded deviations from its stated shape (§1's Conventions still govern — the section isn't relabelled non-normative), rather than a piecemeal rewrite of a block that diverges in ~6 other places too.

New `tests/unit/test_ontology.py::TestProvenance` (4 cases): unknown id → `NotFoundError`; direct write → empty `review_events`/`superseded_ids`; AI-proposed + accepted → the `accept` event in order; KI-008 multi-predecessor supersession → full `superseded_ids` set. Mutation-tested (stubbing `get_proposal_events` to `()` fails exactly the reviewed-assertion case). REST's and MCP's pre-existing provenance suites (review-events-after-accept, empty-for-direct-write, 404, retracted-reachable, KI-008 superseded set) all still pass unchanged, now exercising the shared path; GraphQL's `TestProvenanceQuery` only selected `{ id subject predicate value proposalId }` on the known-assertion case, so `reviewEvents`/`supersededIds` shaping was untested there — this review round added a full-field selection plus review-events-after-accept, empty-for-direct-write, and KI-008 cases, mutation-tested (hardwiring both fields empty in `_build_provenance` now fails two of them).

---

## KI-087 — `mkdocs-material` has an unpatched CVE; `pip-audit` CI gate is currently red ✓ RESOLVED (Backlog)

**Severity:** Supply-chain — dev-only exposure, but `.github/workflows/security.yml`'s unconditional `pip-audit` step is failing on every PR right now, not just a missing upper bound
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** n/a (dependency management / supply-chain hygiene)

### Description

`uv run pip-audit` reports `mkdocs-material 9.7.6` (the version `uv.lock` currently resolves, matching `pyproject.toml`'s unbounded `mkdocs-material>=9.5`) is vulnerable to **CVE-2026-73295** (DOM-based XSS in the `search.suggest` feature, fixed in 9.7.7). `mkdocs-material` is a `dev`-extra, docs-build-only dependency — never runs in the deployed service — so practical exposure is low, but `security.yml:38-39` runs `uv run pip-audit` unconditionally and is currently failing because of this finding. KI-062 already resolved exactly this situation once before (an unfiltered `pip-audit` failure on a dev-only CVE, which that KI's own history notes once masked two genuine `bandit` findings by making the gate noisy) — this is new drift of the same class since KI-062 closed, not a fresh category of problem. It's also new drift since KI-070, whose scope was explicitly limited to the seven named runtime dependencies (KI-070 left the `dev` extra's ~20 tooling dependencies out of scope as lock-pinned in practice, since CI installs from the committed `uv.lock` rather than a fresh resolve — a different rationale than "pip-audit is sufficient coverage for that subgroup").

### Fix

Bumped the pin to `mkdocs-material>=9.7.7` (`pyproject.toml`), re-locked (`uv lock` resolves `9.7.7`) and synced the venv — `uv run pip-audit` now reports zero findings. Kept the fix minimal and scoped, matching KI-070's precedent for the `dev` extra: no upper bound added here either, only the floor moved past the vulnerable version.

Left open, deliberately out of scope for this fix: whether KI-062's resolution (an unconditional `pip-audit` CI gate) needs a documented triage path for dev-only findings so a future one doesn't block every PR until someone notices and bumps the pin. That's a process question about the gate itself, not a dependency fix — worth its own KI if it recurs.

---

## KI-088 — `RequireReviewForAI` remains a docstring-only sketch, not a shippable class — revisits KI-061/ADR-0040's chosen resolution ✓ RESOLVED (Backlog)

**Severity:** Informational — KI-061/ADR-0040 already reviewed this exact question and consciously chose the documentation-only pattern over shipping a new strategy; this KI only questions whether that choice should be revisited now that `Composite` has real users, not a newly-discovered gap
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §9.2 (`PolicyStrategy`, `Composite`), §8.3 ("Agents MUST default to `propose`...", unaffected by this)

### Description

Only the default `ThresholdPolicy` unconditionally routes AI-kind principals to `RequireReview`, checked first in its `evaluate()` before any capability math (`govern/policy.py:156-163`, comment attributes this to ADR-0003 — though ADR-0003 itself is actually scoped to accountable-owner/model-capture/delegation and doesn't discuss review requirements; this attribution in the code predates this KI and isn't corrected here). `ConfidenceThreshold`/`SourceRequired`/`SourceQuorum` (KI-069, ADR-0045; `SourceQuorum` itself is KI-017/ADR-0025) each carry an explicit "does not special-case AI-authored proposals" docstring — this is a deliberate, already-reviewed design (KI-061/ADR-0040): `SourceQuorum`'s AI-auto-accept behavior was explicitly *not* silently reversed, and `Composite(all=[…], any=[…])` was built precisely so a deployment can compose an AI-blocking rule back in. KI-061's own Fix already states plainly that no new "AI-always-reviews" strategy was shipped — the pattern lives only as an inline example in `Composite`'s own docstring (`govern/policy.py:687-700`; KI-061 calls it "five-line," but the actual sketch spans 14 lines — a pre-existing inaccuracy in KI-061's own text, not introduced here), and MCP's tool docstrings were corrected at the time to attribute the guarantee to the *default* `ThresholdPolicy` specifically, not state it unconditionally. Note also that KI-061's own **SPEC reference** line still cites "ADR-0003 (AI/low-trust principals require review)" — the same ADR-0003 misattribution named above; this KI doesn't correct KI-061's text, only avoids repeating the error in its own citations.

This does not violate SPEC: §9.2 doesn't mandate that any particular strategy special-case AI authorship, and §8.3's own MUST ("Agents MUST default to `propose` and MUST NOT be granted `write` implicitly") still holds regardless of which `PolicyStrategy` is installed — no direct write occurs under any of them. "AI proposals always require review" is not itself a named SPEC guarantee; it's `ThresholdPolicy`-specific behavior, and KI-061 already corrected the one place (MCP docstrings) that used to imply otherwise. What remains open is narrower than "the guarantee is silently lost": a deployer composing a non-default strategy still has to hand-write the `RequireReviewForAI` pattern correctly from a docstring rather than importing a tested class — worth a modest DX/safety improvement, not a re-opened safety hole.

### Fix

Promoted the docstring's sketch to a real class, `RequireReviewForAI` (`govern/policy.py`), exported from both `ontolith.govern.policy` and `ontolith.govern` (`__all__` updated in both, plus the pinned `tests/unit/test_public_api_surface.py` entry). `Composite(all=[RequireReviewForAI(), SourceQuorum(2)])` is now copy-pasteable exactly as the docstring always showed it — `Composite`'s own docstring was simplified to reference the real class instead of re-sketching it inline, removing the duplication-drift risk that let the sketch's own line-count claim (KI-061's "five-line," actually 14) go unnoticed for as long as it did.

The implementation adds one thing the inline sketch never had: its own KI-015 capability-floor enforcement (`read`-only principals rejected), matching every sibling strategy in this module (`SourceQuorum`, `ConfidenceThreshold`, `SourceRequired`, `RequireReviewByRole`) — the sketch, as written, would `AutoAccept` a read-only *non-AI* principal if used standalone rather than always composed with a floor-enforcing partner. Checked before the AI-kind test (unlike `ThresholdPolicy`'s AI-first ordering), matching the other four strategies' own convention; documented explicitly why this ordering choice doesn't change the practical outcome under `Composite(all=...)`'s most-restrictive-wins merge, only the standalone case. AI-kind is read from `principal` only, never `acting_as` — mirroring `ThresholdPolicy`'s own "never laundered via delegation" precedent (its own `evaluate()` comment, not ADR-0003, per this KI's own Description above), verified with a dedicated test.

This is a genuine reversal of ADR-0040's own explicit decision *not* to ship this class — its Alternatives Considered rejected exactly this as "better shown as a docstring example... than shipped as a new public symbol with its own maintenance surface." Recorded honestly in ADR-0040's own 2026-09-09 update rather than left implicit: the tradeoff ADR-0040 weighed hasn't changed, but the docstring-sketch pattern's real cost (every deployer re-deriving it from a comment, correctly, each time) outweighed the "new public symbol" concern once the pattern had been in production use since KI-061. `docs/adr/README.md`'s own index entry updated to point at the reversal too, so a reader skimming the index isn't misled by the unqualified original claim.

Added a full conformance vector (`conformance/test_require_review_for_ai_policy.py`, mirroring `RequireReviewByRole`'s own conformance file — its closest sibling, no `kb` read, always a fixed outcome for a given principal) covering: AI requires review with owner as sole reviewer, non-AI (human and service) auto-accepts, the KI-015 floor (including a read-capability AI principal, which is rejected outright rather than routed to review), AI-kind never laundered via `acting_as`, determinism/statelessness, and two end-to-end tests through a real `kb.propose()` call proving the documented `Composite(all=[RequireReviewForAI(), SourceQuorum(...)])` pattern actually works both ways (AI still requires review even once the quorum is met; a non-AI author auto-accepts once it is). Mutation-tested directly: disabling the capability-floor check and disabling the AI-kind check independently, each caught by exactly its own dedicated tests.

---

## KI-089 — `assert_ref`/`propose_ref` never validate that a relation's `target` entity exists — a dangling reference silently succeeds ✓ RESOLVED (Backlog)

**Severity:** Bug — a write silently produces a reference to nothing, more severe than KI-083 (which only closed the equivalent gap for `subject`)
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §16 (error taxonomy — `NotFoundError` for missing entity)

### Description

Found while fixing KI-083 (which checks that `subject` exists before a write). The `assertion` table's `FOREIGN KEY` constraint is declared only on `subject` — `FOREIGN KEY(subject) REFERENCES entity(id)` in both `store/sqlite/backend.py` and `store/duckdb/backend.py` — the `value_ref` column, which holds the target entity id for `ref`-kind assertions (`value_lit` holds literal values instead; `value_kind` picks between them, per KI-030), carries no such constraint in either backend. Reproduced directly on both: `kb.assert_ref(person.id, "Person.employer", "nonexistent-org-id", author=...)` returns a normal, persisted `Assertion` with `value="nonexistent-org-id"` — no error of any kind, and `kb.get_entity("nonexistent-org-id")` confirms the target genuinely doesn't exist. Unlike KI-083's gap (a real row rejected late, with a confusing error), this is worse: the write just succeeds, and the KB now contains a `Person.employer` reference to an entity that was never created and may never be — a query that traverses the relation would need to handle a target lookup silently returning nothing, and nothing today signals that this ever happened.

Two of KI-083's four call sites (`assert_ref`/`propose_ref` — the two literal-only paths, `assert_literal`/`propose`, have no `target` to check), but a distinct fix: `subject` got a `FOREIGN KEY` on both backends (rejects at the DB layer, KI-083 replaces its opaque surfacing with a clean pre-check) while `target` has no DB-level protection to lean on at all, on either backend.

### Fix

Added `Ontology._require_existing_target(target)`, mirroring KI-083's `_require_existing_subject` — called from `assert_ref`/`propose_ref` right after the existing subject check, raising `NotFoundError(f"Target not found: {target!r}")` when `get_entity(target) is None`. Kept as a separate method rather than a shared parametrized one so each error message names *which* endpoint is missing (`_require_existing_subject`'s own message was changed alongside this, from a shared "Entity not found" to "Subject not found", for the same reason) — matching the codebase's existing `"<kind> not found: <id>"` convention. Applied only the application-level check, not a real `FOREIGN KEY(value_ref) REFERENCES entity(id)` — verified directly that such a constraint would also work cleanly (`value_ref` is its own dedicated, nullable column: literal rows leave it `NULL`, which a `FOREIGN KEY` permits unconstrained, so no partial/conditional form is needed), but a DB constraint would need its own migration-path decision (KI-048) for existing databases, so the application-level check alone is the safer, sufficient fix to ship.

**Behavior note:** on both paths, this check now runs before `_require_known_predicate`, matching KI-083's own identical precedence change for `subject` — a call with both an unknown target and an unknown predicate now raises `NotFoundError` where it previously raised `ValidationError`. No caller depended on the old ordering (full suite unaffected).

Found and fixed along the way: three existing tests (`test_ontology_validators.py`) exercised `assert_ref`/`propose_ref`'s `Validator` invocation by passing a fictional string as `target`, relying on this exact gap (no target existence check) to get a rejectable value into the write path. Fixed by making the test double's rejection marker (`_RejectMarkerValue.reject`) a plain settable attribute, pointed at a real entity's own generated id instead of a fictional string, so the target is genuinely valid while the validator's own rejection logic (matching on that value) still exercises the intended code path.

Mutation-tested directly: reverting each of the two call sites individually reproduces the pre-fix silent-success behavior (confirmed via a dedicated `test_..._does_not_persist_a_dangling_reference` test for both `assert_ref` and `propose_ref`) and is caught only by its own new test(s), no cross-coverage between the two sites.

---

## KI-090 — `create_entity()` never validates `concept` against the active schema — now agent-reachable via REST/GraphQL/MCP (KI-082) ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — pre-existing SDK behavior, but KI-082 made it reachable by a `propose`-tier AI agent for the first time, not just a human CLI operator
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §4 (concepts are schema-declared); compare `Ontology._require_known_predicate`, which does this exact check for `predicate` on every assertion write

### Description

`Ontology.create_entity()` (`ontology.py`) writes `concept` as free text with no schema lookup — reproduced directly: `kb.create_entity("TotallyMadeUpConcept", author=...)` succeeds and persists an entity under a concept the active schema never declared. Every assertion write path validates the analogous case for `predicate` (`_require_known_predicate`, hardened specifically by KI-031/KI-040/KI-049 for exactly this class of gap — an unknown/mistyped/wrong-kind predicate), but `create_entity()` was never brought in line with that pattern.

This is pre-existing SDK behavior, not introduced by KI-082 — before KI-082, the reachable callers were the CLI (a human at a terminal, who'd typically notice a typo'd concept name immediately) and the plugin sandbox's `WriteView.create_entity` (`plugins/views.py`, used by the shipped `csv_importer` reference plugin, which passes an unvalidated `row["concept"]` straight through — arguably a stronger pre-existing instance of this exact gap than the CLI). KI-082 exposed `create_entity` on REST/GraphQL/MCP at `propose` tier with no policy evaluation in between (see ADR-0008's 2026-09-08 update for why entity creation has no policy step to catch this at), so an AI agent can now populate the KB with arbitrary undeclared concepts through any of the three governed interfaces directly, not just via a plugin or a human at a terminal.

### Fix

Added `Ontology._require_known_concept(concept)`, mirroring `_require_known_predicate` exactly (no-op when no schema is registered for the namespace, otherwise `ValidationError` naming the unknown concept), called from `create_entity()` right after the existing capability check. Backed by a new `SchemaIR.has_concept(concept)` method (mirrors `has_predicate`) rather than reaching into `schema.concepts` directly from `Ontology`. One check, one call site — no interface-level changes needed beyond the SDK method itself, so REST/GraphQL/MCP's KI-082 wrappers pick this up automatically.

Added conformance vectors (`conformance/test_conflict.py::TestUnknownConceptRejected`, runs against both backends) mirroring `TestUnknownPredicateRejected`'s exact shape: unknown concept rejected, known concept succeeds, and the schema-less-namespace no-op case. Also added direct `SchemaIR.has_concept()` unit tests (`tests/unit/test_schema_ir.py::TestHasConcept`), mirroring `TestHasPredicate`. Mutation-tested directly: reverting the call site reproduces the pre-fix behavior (both conformance-vector backends) and is caught by exactly the two tests that exercise the unknown-concept case, no cross-coverage with the schema-less/known-concept cases. Full suite confirmed no regressions — no existing test creates an entity under a concept its own schema fixture (where one is applied) doesn't already declare.

---

## KI-091 — Duplicate `(namespace, concept, natural_key)` on entity creation surfaces as a redacted `StorageError`, not a caller-actionable error — now agent-reachable via REST/GraphQL/MCP (KI-082) ✓ RESOLVED (Backlog)

**Severity:** Bug — wrong error taxonomy for a caller-input conflict, same class as KI-083/KI-089; pre-existing SDK behavior newly agent-reachable via KI-082
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §16 (error taxonomy — a caller-input conflict should be a named, actionable error, not a generic backend failure)

### Description

`create_entity()` does no pre-check against the `UNIQUE(namespace, concept, natural_key)` constraint (`store/sqlite/backend.py`) before writing — reproduced directly: creating two entities with the same `concept`/`natural_key` raises `StorageError: Entity conflict: UNIQUE constraint failed: entity.namespace, entity.concept, entity.natural_key` on the second call. `StorageError` is one of the two error types every interface redacts before returning it to a caller (same mechanism KI-083 closed for `assert_literal`/`assert_ref`'s FK failure) — so a REST/GraphQL/MCP caller who supplies a natural key that's already taken gets a generic, message-redacted 500-class error with no indication of what went wrong, even though the backend's own exception message is already a clear, actionable description of the actual problem.

Same root shape as KI-083/KI-089 (a caller-input condition surfacing through the redacted `StorageError` path instead of a named, unredacted domain error), but pre-existing rather than newly introduced — KI-082 is what made it reachable by a `propose`-tier AI agent rather than only a human CLI operator, who'd see the real message in a terminal traceback.

### Fix

Added a new `StorageBackend` port method, `get_entity_by_natural_key(namespace, concept, natural_key)` (implemented on both backends, mirroring `get_entity`'s exact query shape), and `Ontology._require_unique_natural_key(concept, natural_key)`, called from `create_entity()` right after the existing `_require_known_concept` check — no-op when `natural_key is None` (verified directly on both backends: `NULL` is exempt from the `UNIQUE` constraint, so any number of entities may already share it within a concept), otherwise `ValidationError` naming the conflicting concept/natural_key/existing entity id. Used `ValidationError`, not the existing `ConflictError` — that type's own docstring scopes it specifically to SPEC §10's unresolved-contradiction domain concept, and a duplicate natural key is a caller-input validation failure, not a contradiction between facts.

Added conformance vectors (`conformance/test_conflict.py::TestDuplicateNaturalKeyRejected`, both backends): duplicate raises, unique succeeds, same natural_key under a *different* concept doesn't conflict (uniqueness is scoped per-concept), and `natural_key=None` never conflicts. Added direct `get_entity_by_natural_key()` unit tests on both backends, mirroring `get_entity`'s own existing test pattern. Mutation-tested directly: reverting the call site reproduces the original `StorageError`/`UNIQUE` constraint failure on both conformance backends and is caught by exactly the two tests that exercise the duplicate case, no cross-coverage.

---

## KI-092 — Several `Ontology` write methods read-then-write outside any `transaction()` block, unreached by KI-084's `BEGIN IMMEDIATE` fix — found while fixing KI-084 ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — and only for `create_entity`, the one member whose read actually guards an invariant; a race there surfaced as an error (KI-091's `UNIQUE` constraint), not a silent duplicate, narrower than KI-084's own pre-fix shape. `issue_token`/`revoke_token`/`reindex` are named for completeness of the "outside `transaction()`" audit, not because they share the race — none of their reads guard anything the write could violate
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §10 (conflict routing), SPEC §12.1 (SQLite default backend)

### Description

KI-084 fixed the cross-process stale-snapshot race for the write paths whose pre-write read runs inside their `with self.backend.transaction():` block: `assert_literal`/`assert_ref`, `propose`/`propose_ref`'s auto-accept branch, `accept_proposal`, and `resubmit`'s auto-accept branch. Auditing every other `self.backend.<mutator>` call in `ontology.py` (prompted by a review round on KI-084 itself, which caught an earlier draft of this entry wrongly calling `create_entity` "the one exception") finds four more governed writes with a comparable read-then-write shape, none wrapped in `transaction()` at all, so none reached by `BEGIN IMMEDIATE`:

- **`create_entity`** — `_require_unique_natural_key` (read, KI-091) → `put_entity` (write). Has a backstop: the `entity` table's `UNIQUE(namespace, concept, natural_key)` constraint still catches a race as a `StorageError`, not a silent duplicate.
- **`issue_token`** — `get_principal` (read) → `put_credential` (write). No uniqueness constraint applies (a token doesn't conflict with another token), so this isn't the *same* race as `create_entity`'s — the read is only an existence check on `principal_id`, and the write cannot itself violate any invariant the read was protecting. Named here for completeness of the "writes outside `transaction()`" audit, not as a race.
- **`revoke_token`** — `get_credential` (read) → `revoke_credential` (write). Same shape as `issue_token`: an existence check, not a race-prone invariant check.
- **`reindex`** — `entities()` (read) → `vector_upsert` per entity (write), in a loop. Idempotent by design (re-embeds and upserts every call) — a concurrent second `reindex()` or write landing mid-loop produces at worst a slightly stale embedding, corrected on the next call, not a correctness violation.

A fifth, related instance of "writes outside `transaction()`" belongs to a different, already-resolved KI rather than this one: `propose`/`propose_ref`/`retract` persist their `Reject`/`RequireReview` outcome (`_finalize_non_accepted_decision`) via an autocommit write outside any transaction when the policy decision isn't auto-accept — and the `kb_view` that decision was evaluated against is itself read before any transaction opens. KI-035 already recorded this as a pre-existing, deliberately accepted tradeoff ("policy evaluation itself can likely stay outside the transaction... not something newly closed here") when it closed the narrower TOCTOU window on proposal *state*; KI-084 doesn't reopen it, and this KI doesn't either — named here only so the "outside `transaction()`" audit doesn't read as having missed it.

`record_admin_event` writes inside a `transaction()` at both its `ontology.py` call sites (`create_principal`, `apply_schema`) — it does *not* belong on this list; its one out-of-transaction caller, `PluginRegistry.register()`, lives in a different file and has no preceding read either, so the stale-read race this KI is about doesn't apply to it regardless.

Of the four bulleted above, only `create_entity` has the specific shape KI-084's own fix targets: a read whose result determines whether the write should be *rejected*, where a stale read lets an invalid write through. Concretely: two processes racing to create an entity with the same `(namespace, concept, natural_key)` can both pass `_require_unique_natural_key`'s pre-check (reading no conflict yet) before either commits its `put_entity`. Unlike KI-084's original failure mode, this doesn't produce a silent duplicate or an opaque instant failure — KI-091's constraint still catches the loser at the SQLite level, surfacing as the same redacted `StorageError` KI-091 was filed to eliminate for the *non-concurrent* case (a caller who simply supplies a taken key gets the friendly `ValidationError`; a caller who loses this specific race gets the generic redacted one instead, since the constraint fires after `_require_unique_natural_key`'s own check already passed).

### Fix

`create_entity` (`ontology.py`) now wraps its check-then-write in `with self.backend.transaction():` — `_require_unique_natural_key`, the `Entity(...)` construction (so a rejected duplicate consumes no `id_provider`/`clock` draw), and the `put_entity` it guards all run in one transaction. Two ways this closes the race, both verified:

- **Multi-threaded, single process** (the REST/GraphQL ASGI-threadpool topology `check_same_thread=False` + KI-023's `threading.RLock` exist for): `begin()` holds the RLock across the whole block, so a second thread's `create_entity` blocks on the lock until the first commits, then does its own in-transaction check and sees the row. Reproducible pre-fix — 20 threads racing one `natural_key` gave a `StorageError: UNIQUE constraint failed`; post-fix, 1 success + 19 `ValidationError`, one row. Holds on **both** backends (DuckDB has the same RLock).
- **Cross-process, SQLite only**: `begin()`'s `BEGIN IMMEDIATE` (KI-084) claims the SQLite write lock before the check runs, so a second OS process is serialized behind it. DuckDB takes an exclusive file lock, so no second writing process exists to serialize against there — moot rather than unfixed.

`_require_known_concept` stays *outside* the block as an optimistic fast-fail, matching `assert_literal`'s own precedent for schema validation (a concurrent schema change is a different, out-of-scope race). `_require_unique_natural_key`'s docstring updated to describe the now-transactional guarantee instead of the old check-then-write caveat. `create_entity` gained the standard `Note:` block (matching `create_principal`/`apply_schema`) recording that it can no longer be called from inside a caller's already-open `backend.transaction()`.

Chosen over the cheaper alternative (leave the race, just remap the `UNIQUE`-constraint `StorageError` to `ValidationError`) so `create_entity` is genuinely consistent with every other governed write path rather than leaving a permanent asymmetry a future audit would re-flag — and `BEGIN IMMEDIATE` was already verified (KI-084) to cost nothing extra when uncontended (re-measured here: 1000 uncontended `create_entity` calls, p95 within noise of `main`), so there's no perf downside to the wrapper on the common single-writer path.

Two new tests (`tests/unit/test_ontology.py::TestCreateEntityNaturalKeyTransaction`): a backend-agnostic spy recording the `begin` → check → `put_entity` → `commit` call order (primary mutation guard — reverting the wrapper drops `begin`/`commit` from the sequence), and a two-`Ontology`-instances-on-one-file test that interleaves a paused check in one "process" against a concurrent `create_entity` for the same key in another — with an explicit `assert t2.is_alive()` so the test fails if the contention it exists to exercise never happens — asserting the loser gets `ValidationError` ("Entity conflict"), not `StorageError`, and that exactly one row lands. Both confirmed to fail with the wrapper reverted (the concurrent test reproduces the exact pre-fix `StorageError: UNIQUE constraint failed`, and now also trips the `is_alive()` assertion since nothing blocks).

`issue_token`/`revoke_token`/`reindex` were left as-is — their reads are existence checks or (for `reindex`) idempotent re-computation, not an invariant a stale read could let a write violate, so there's nothing for a `transaction()` wrapper to protect there. `propose`/`propose_ref`/`retract`'s reject/require-review path was out of scope entirely — it's KI-035's already-recorded, deliberately-accepted tradeoff, not this KI's to revisit.

---

## KI-093 — `.include_flagged()`/`.include_history()` don't compose with `.min_confidence()`/`.trust_at_least()` — found reviewing KI-081 ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — a chained floor that should pass every scored assertion (`.min_confidence(0.0)`; `.trust_at_least(0)`, a true no-op since `trust_level` is `NOT NULL`) silently empties an otherwise-populated result
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §11.2 (`.include_flagged()` / `.include_history()` opt-ins), SPEC §11.3 (`confidence`/`trust` retrieval signals)

### Description

KI-081 (ADR-0048) added `QueryBuilder.include_flagged()`/`.include_history()` as match-set wideners: they widen `_base_candidates()`'s `entities_where()` call to also match `flagged`/`superseded`/`retracted` assertions. But `QueryBuilder._apply_confidence_trust_filters()` runs *after* `_base_candidates()` and calls `StorageBackend.entities_meeting_confidence()` / `entities_meeting_trust()`, whose current-state (non-`as_of`) branches still hard-code `AND a.status = 'active'` (`store/sqlite/backend.py`, `store/duckdb/backend.py`). So `kb.query(Person).where(name="Ada").include_history().min_confidence(0.0)` widens the candidate set to include an entity matched via a retracted assertion, then immediately re-narrows to active-only and drops it — a `min_confidence(0.0)` / `trust_at_least(0)` floor that should be a no-op empties the result.

Verified: an entity whose only `Person.name` assertion was retracted — `include_history()` alone returns it; `include_history().min_confidence(0.0)` and `include_history().trust_at_least(0)` both return `[]`.

### Fix

`entities_meeting_confidence()` / `entities_meeting_trust()` (port + both adapters, `store/base.py`/`store/sqlite/backend.py`/`store/duckdb/backend.py`) gained the identical `include_flagged`/`include_history` parameters `entities_where()` already had. Their current-state branch was widened the same way: `a.status = 'active'` became a parameter-bound `a.status IN (?, …)` built from the same status list; their `as_of` branch gained the identical `flagged_clause` treatment (`include_flagged` applies, `include_history` is a documented no-op for `superseded` — a bitemporal snapshot already matches whatever was valid then regardless of status now; later narrowed by ADR-0049/KI-095 for `retracted` specifically). `QueryBuilder._apply_confidence_trust_filters()` threads `self._include_flagged`/`self._include_history` into both calls. Semantics decided per the KI's own framing: "an entity that *historically* had a ≥X-confidence (or ≥X-trust) assertion" now qualifies under `.include_history()`, consistent with `.where()`'s own widened matching — not made to raise.

New tests: `tests/unit/test_query.py::TestIncludeFlaggedHistoryComposesWithConfidenceAndTrust` (all four `{include_flagged, include_history} x {min_confidence, trust_at_least}` combinations — a genuine no-op floor no longer re-narrows; a genuinely-failing floor still excludes even when widened, proving the widening isn't a bypass); `tests/unit/test_sqlite_backend.py`/`test_duckdb_backend.py::test_entities_meeting_{confidence,trust}_status_widening_flags` at the port level, on both backends; `conformance/test_include_flagged_history.py::TestComposesWithConfidenceAndTrust` (both backends). A review round found the `as_of` branch's `include_flagged` handling was untested on both new methods — a mutation reverting `flagged_clause` to unconditionally exclude flagged survived the whole suite — so also added `test_entities_meeting_{confidence,trust}_as_of_include_flagged` at the port level (both backends) and `conformance/test_confidence_trust_filters.py`'s `test_{trust_at_least,min_confidence}_as_of_include_flagged_opts_back_in` (both backends), all mutation-tested. Mutation-tested throughout: reverting either backend's widened current-state `IN` clause back to `status = 'active'` fails the no-op-floor cases on both methods; reverting either backend's `as_of` `flagged_clause` to unconditional fails the new `as_of` cases; hardcoding `include_flagged=False` in `QueryBuilder`'s trust call site fails the flagged+trust case. `QueryBuilder.include_flagged()`/`.include_history()`'s docstrings, ADR-0048, the ADR README index, and SPEC §11.2 updated to state the composition now holds (and corrected a related pre-existing inaccuracy the review round found: "no effect without `.where()`" was never quite right and is now actively wrong, since confidence/trust floors run regardless of `.where()`).

A second review round found: the pre-existing (KI-081) `.semantic()` path's `include_history` threading in `_semantic_candidates()` was untested — hardcoding it to `False` also survived the whole suite — closed with `test_semantic_where_honors_include_history`, mutation-tested, mirroring the `include_flagged` case KI-081 already had; a pinning test for `include_history`'s documented `as_of` no-op (`test_entities_meeting_confidence_as_of_include_history_is_a_documented_noop`, both backends); "by default" added to two pre-existing conformance docstrings that had gone stale by omission; and KI-081's own entry above corrected in the same three places the round found stale ("no effect on a filter-less query" narrowed to the precise condition; the "don't compose" follow-up note updated from present tense to "closed by KI-093"). The four new `# nosec B608` markers, and the one pre-existing dead marker on `entities_where()`'s own `as_of` `flagged_clause` line that the round also found (removing it doesn't change bandit's finding count either), were all removed — verified bandit-clean before and after on all three occurrences per backend.

---

## KI-094 — MCP/REST/GraphQL `query` surfaces don't expose `.include_flagged()`/`.include_history()` — found reviewing KI-081 ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — SPEC §10.3's "unless explicitly requested" opt-in is now reachable from the Python SDK only, not from the interface agents actually use
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §10.3 ("MUST be excluded from default retrieval unless explicitly requested"), SPEC §11.2, SPEC §14.3/§14.4 (REST/GraphQL/MCP query surfaces)

### Description

KI-081 added `.include_flagged()`/`.include_history()` to `QueryBuilder`, but the three interface `query` surfaces (`interfaces/mcp.py`'s `ontolith.query`, `interfaces/rest.py`'s `/query`, `interfaces/graphql.py`'s `Query.query`) — which already forward `semantic`, `min_confidence`, `trust_at_least`, `limit` — do not forward the two new opt-ins. So a REST/GraphQL/MCP caller can't opt in to flagged/history visibility through the primary query API, only a direct Python SDK caller can. This matters most for MCP, where the flagged-exclusion rule is a safety property for agents (SPEC §14.4).

Correction found while resolving: the claim above that these surfaces "already forward `as_of`" does not hold for REST or GraphQL — only `interfaces/mcp.py`'s `ontolith.query` accepts `as_of` at all. Not an undiscovered gap, though: per KI-058's own Fix (ADR-0043), `as_of` was deliberately added to MCP alone, "per SPEC §14.4's own normative tool table naming it for this tool specifically" — REST/GraphQL were never intended to gain it as part of that work. Unrelated to `include_flagged`/`include_history` either way, and out of scope here.

### Fix

Added `include_flagged`/`include_history` boolean parameters (default `False`) to all three interfaces' `query` operations, forwarding to the builder exactly as `min_confidence`/`trust_at_least`/`limit` already are: `interfaces/rest.py`'s `QueryIn` gained the two fields, wired in `query_route`; `interfaces/graphql.py`'s `_execute_query` and `Query.query` gained the two parameters (strawberry auto-camelCases them to `includeFlagged`/`includeHistory` on the wire), wired the same way (its `run_in_threadpool` call site passes all arguments by keyword, not position, since two adjacent `bool` and two adjacent `int | None` parameters are otherwise swappable with no mypy error); `interfaces/mcp.py`'s `ontolith.query` tool gained the two parameters with docstring coverage, including the pre-existing `include_history`+`as_of` no-op explanation, corrected in the same pass (see ADR-0048's Update). Same pattern as KI-079 (GraphQL/CLI parity for `assign_reviewers` after REST shipped first).

The CLI's `ontolith query` command was deliberately left out of scope: at the time, unlike REST and GraphQL, it didn't forward `semantic`, `min_confidence`, `trust_at_least`, or `limit` either — a materially larger, pre-existing gap that predates KI-081, filed separately as **KI-096** (now resolved — see its own entry below). `as_of` is a separate case per the correction above: only MCP has it today, so adding it to the CLI was never a parity question with REST/GraphQL either way.

New tests: `tests/unit/test_rest.py`/`test_graphql.py`, two each (`include_flagged` matching a flagged assertion, `include_history` matching a retracted one, each with a false-default baseline); `tests/unit/test_mcp_server.py` additionally gets a third, combining `include_flagged` with `as_of` — the one branch genuinely distinct from the other two, since MCP alone builds from `kb.as_of(...)`. Mutation-tested: reverting each interface's `if include_flagged: ... if include_history: ...` wiring back out fails exactly its own new tests on that interface, restored after confirming.

---

## KI-095 — A retracted assertion with a future `valid_to` stays visible in `.as_of()` queries with no way to exclude it — found reviewing KI-081 ✓ RESOLVED (Backlog)

**Severity:** Bug — SPEC §11.2's "retracted assertions are excluded by default" is unmet on the `as_of` path
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §11.2 ("Flagged/superseded/retracted assertions are excluded by default"), SPEC §11.4 (`as_of` semantics), SPEC §10 (retraction)

### Description

`Ontology._retraction_valid_to()` (`ontology.py`) deliberately does not narrow an already-open validity window when an assertion is retracted (it only refuses to *widen* a closed one). So an assertion asserted with an explicit far-future `valid_to` and then retracted keeps `status='retracted'` but `valid_to` unchanged. The `as_of` branch of `entities_where()` (and the other bitemporal query paths) matches purely on the validity window plus `asserted_at <= t` — it has no assertion-time dimension for the *retraction event* — so `kb.as_of(t).query(Person).where(name="Ada")` for `t` after the retraction still returns the entity, and KI-081's `.include_history()` being a deliberate no-op on the `as_of` path means there is no opt-out either.

Verified: assertion with `valid_to=2030`, retracted at `2025` → current-state query excludes it (correct), `as_of(2026)` query includes it (wrong), `as_of(2026).include_history()` also includes it (no opt-out).

Pre-existing — the `as_of` branch has always been window-based, not status-based; surfaced by KI-081's review because ADR-0048 initially claimed the `as_of` branch was "already correct" for history (corrected).

### Fix

**ADR-0049** decides direction (b) from the two candidates this KI originally floated: `as_of(t)` gains a second, independent assertion-time cutoff for retraction, on top of the existing valid-time window check — an assertion is excluded once its own `retracted` `assertion_event`'s `at` is `<= t`. No schema change: the timestamp already exists (KI-051 guarantees exactly one such event per assertion, ever) and was simply never consulted by any bitemporal query path. `valid_to` itself is never touched — rejected direction (a), closing `valid_to` to the retraction instant at write time, since `AssertionEvent` never snapshots field values and an assertion row is mutated in place, so an originally-asserted `valid_to=2030` would be permanently unrecoverable the moment a retraction narrowed it. See ADR-0049 for the full rationale and rejected alternatives.

`entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()`'s `as_of_time` branch (both backends, matching KI-093's established precedent that these three move together) gained a `retracted_clause` alongside the existing `flagged_clause`: `AND (status != 'retracted' OR EXISTS (SELECT 1 FROM assertion_event ae WHERE ae.assertion_id = <row> AND ae.action = 'retracted' AND ae.at > ?))`, bound to `as_of_time`. `.include_history()` becomes the opt-out — the first thing it has ever done on the `as_of` path (every prior KI-081/093/094/096 note calling it "a documented no-op under `.as_of()`" is now scoped to say "unless retraction is involved"; superseded is unaffected either way, since its `valid_to` closure already tells the truth about the real-world end point). Every stale "no-op under as_of" docstring this correction touches was updated: `QueryBuilder.include_history()`, `StorageBackend.entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()` (port + both adapters), and MCP's `ontolith.query` docstring.

New tests: conformance vectors in `conformance/test_include_flagged_history.py::TestAsOfRetraction` (`.where()` path: excluded once known, still visible before the retraction's own assertion-time, `.include_history()` opts back in) and `conformance/test_confidence_trust_filters.py::TestAsOfConfidenceTrust` (same three shapes for both `.min_confidence()`/`.trust_at_least()`), both backends; port-level unit tests in `tests/unit/test_sqlite_backend.py`/`test_duckdb_backend.py` constructing the `assertion_event` row directly (bypassing `Ontology`, isolating the SQL from the write-path plumbing); one `QueryBuilder`-level test in `tests/unit/test_query.py`. Mutation-tested on both backends: neutralizing all three `retracted_clause` definitions to unconditionally empty (matching pre-fix behavior) fails exactly the new tests built to catch it, with zero interference on the 51 pre-existing `as_of` tests in the same two conformance files — confirming the fix is additive, not a regression to the existing window/flagged logic.

**Found and filed separately while building this fix, not part of this KI's own scope:** **KI-097** — `entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()`'s `as_of` branch reconstructed `flagged` from *current* status rather than point-in-time, unlike `StorageBackend.assertions()`'s own already-correct event-log reconstruction for the same status. Same root cause class (current status trusted instead of point-in-time reconstruction) but a distinct transition pair (`flagged`/`reactivated`, a two-way cycle) needing its own, more careful fix — not folded into ADR-0049's simpler one-way `retracted` case. **Closed by KI-097** (below).

A review round found the fix above was itself incomplete: it covered only `entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()` (the `QueryBuilder`-facing trio), but `StorageBackend.assertions()` — the distinct, lower-level bitemporal query path this KI's own original text named alongside `entities_where()` ("and the other bitemporal query paths") — still leaked the retracted value, reproduced directly: `kb.as_of(t).query(Person).where(name="Ada")` correctly excluded it while `kb.as_of(t).assertions(subject=...)` on the identical data still returned it. This wasn't cosmetic: `govern/policy.py`'s `SourceQuorum` evaluates `kb_view.assertions(...)` on an `AsOfView` during policy decisions, so an un-narrowed retracted assertion could silently contribute a distinct `source` to quorum counting. Fixed: `assertions()` (both backends) gained the identical `EXISTS`-based clause, applied unconditionally at the time — this method had no `include_history`-style opt-out yet (its `status` parameter is already ignored entirely once `as_of_time` is set), so there was nothing for a flag to widen back into. ADR-0049 updated to describe both fixes and this asymmetry explicitly rather than the original, narrower claim. **Closed by KI-098**, which gave `assertions()` its own `include_history` parameter.

The same round found several docstrings still carried the pre-ADR-0049 "no-op under as_of" claim unconditionally rather than scoped to `superseded` only: `QueryBuilder.min_confidence()`/`.trust_at_least()` (never touched despite MCP's identical parameter doc having been corrected), ADR-0048's own body in four places (a bolded normative Decision-section claim, its Consequences section, its KI-093 Update section, and the paragraph that used to describe KI-095 as an unaddressed open gap — only the ADR's cross-reference index had been updated, not the body itself), `AsOfView`'s class docstring in `ontology.py`, and two historical narrative paragraphs in this document (KI-081's and KI-096's own entries, both of which had described the pre-fix behavior in a way that read as still-current) — all corrected. Also closed: test-coverage gaps the round found (a missing `entities_meeting_trust()` "still visible before retraction" test on both backends, paired with the confidence version that already had one; a missing DuckDB-level `entities_where()` retraction test, matching SQLite's; an exact-boundary test pinning `t == retraction event's at` as already-excluded; a combined `.include_flagged().include_history()` test under `as_of` retraction), plus new conformance vectors for `assertions()` itself in `conformance/test_bitemporal.py::TestAsOfRetractionAndFlagging` (the class's pre-existing retraction tests only ever used an open-ended `valid_to`, the one case that was never actually buggy). All new/changed assertions mutation-tested on both backends.

A second review round found the headline governance-impact claim justifying the `assertions()` extension — that `SourceQuorum` could silently count a retracted assertion toward quorum — had no test: the round's own repro confirmed it was real (a second proposal from a distinct source flips from `RequireReview` to `AutoAccept` with the fix reverted), but `conformance/test_source_quorum_policy.py`'s pre-existing `test_retracted_assertion_does_not_corroborate` seeds an assertion with no explicit `valid_to` — the open-ended-window case `_retraction_valid_to()` already narrowed correctly before this KI, not the case this fix actually addresses. Closed with `test_retracted_assertion_with_explicit_valid_to_does_not_corroborate` (mutation-tested — reverting the `assertions()` clause fails it, confirmed on both backends), and the pre-existing test's docstring corrected to describe both mechanisms rather than implying only the (correct, but not load-bearing here) window closure. The round also found: ADR-0049's own Decision section had a code-block cross-reference orphaned by a later edit (the `assertions()` paragraph landed between the sentence introducing the SQL block and the block itself) — reordered; a count error ("all four call sites" → three); two more stale cross-references in this document's own KI-081 entry — a still-unconditional `.include_history()` claim the first round's sweep had missed entirely, and a self-contradicting closing sentence — both fixed; and `docs/adr/README.md`'s ADR-0048 index entry, whose inline claim still read unconditionally despite the ADR body itself having been corrected. **Filed as its own follow-up, not fixed here:** KI-098 — `assertions()`'s new exclusion had no opt-out parameter, unlike the `QueryBuilder`-facing trio's `.include_history()`. **Closed by KI-098** (below).

A third review round confirmed the new SourceQuorum test's mutation-sensitivity but found its own failure mode didn't match its docstring's claim: the FixedIdProvider list was exactly one id short of what the auto-accept path the mutation opens up actually consumes, so under the mutation the test failed on a StorageError (id collision from provider wraparound) rather than the documented RequireReview-to-AutoAccept decision mismatch — still mutation-sensitive, just for a confounded reason that would produce a misleading message on a future regression. Fixed with one spare id. The round also found several more stale copies of the pre-ADR-0049 claim the first two rounds' sweeps had missed: the CHANGELOG's own KI-081 entry (unconditional, sitting in the same [Unreleased] section as this KI's own corrected Fixed entry — the release notes would have shipped self-contradictory); `conformance/test_bitemporal.py`'s module docstring and its `TestAsOfRetractionAndFlagging` class docstring (the latter directly contradicted by the two test docstrings added immediately beneath it); and SPEC §11.4 itself, whose literal `as_of` visibility rule this PR's own new conformance vectors now enforce beyond what the unqualified formula states — tightened with a cross-reference to §10.3's exclusion rule and the retraction-specific carve-out, mirroring KI-081's precedent of tightening SPEC wording when behavior changed. Also fixed: KI-098's Severity line misattributed a quoted phrase to §11.2 when it's §10.3's; `docs/adr/README.md`'s ADR-0048 entry had an ambiguously-ordered parenthetical from the second round's own fix, reworded.

---

## KI-096 — CLI's `ontolith query` command doesn't support `semantic`, `min_confidence`, `trust_at_least`, or `limit` — found resolving KI-094 ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — the CLI query surface is far behind REST/GraphQL/MCP, not just missing KI-081's two opt-ins
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §11.2/§11.3 (confidence/trust retrieval signals), SPEC §11.4 (`as_of`), SPEC §14 (interface parity)

### Description

While resolving KI-094 (adding `include_flagged`/`include_history` to REST/GraphQL/MCP's `query` operations), found that `interfaces/cli.py`'s `ontolith query` command only supports `concept` and `--where` filters. Unlike REST and GraphQL's `query` surfaces, it has never forwarded `semantic`, `min_confidence`, `trust_at_least`, or `limit` — a materially larger, pre-existing gap that predates KI-081 and is unrelated to the flagged/history opt-ins, so it was kept out of KI-094's scope rather than folded in. `as_of` is a separate case: per KI-094's own correction, only MCP supports bitemporal time-travel on `query` today, so adding `--as-of` to the CLI would make it the *second* interface to gain this, not bring it to parity with REST/GraphQL.

### Fix

`ontolith query` gained `--semantic`, `--min-confidence`, `--trust-at-least`, `--limit`, `--include-flagged`, and `--include-history`, each forwarding to `QueryBuilder` exactly as REST/GraphQL/MCP already do (same order: `--where` → `--semantic` → `--min-confidence` → `--trust-at-least` → `--include-flagged` → `--include-history` → `--limit`). `--as-of` deliberately not added: it would make the CLI the *second* interface with bitemporal time-travel, not parity with REST/GraphQL (which have none), and wasn't part of what this KI was scoped to fix. Flag naming decided directly (no ADR needed, precedent-implied by the three sibling interfaces' existing parameter names, kebab-cased the same way `--min-confidence`'s CLI sibling params already are) rather than via a design conversation: `--where KEY=VALUE` (existing, repeatable) coexists with `--semantic TEXT` as an independent, orthogonal option, matching how REST's `filters`/`semantic` JSON fields and MCP's `filters`/`semantic` tool arguments already coexist — no new interaction to design.

New tests in `tests/unit/test_cli.py::TestQuery` (one per flag): `min_confidence`/`trust_at_least` each assert a floor that should exclude and one that shouldn't; `limit` counts output lines with and without the flag; `include_flagged`/`include_history` mirror the existing REST/GraphQL/MCP tests for the same two opt-ins (KI-094); `semantic` queries two entities with distinct content, one direction at a time, asserting each `--semantic <text>` invocation returns its own matching entity and not the other's — order-independent, unlike an insertion-order discriminator (see the review-round note below for why that was changed). All six mutation-tested by reverting each `if <param>: q = q.<method>(...)` line in turn and confirming exactly its own test(s) fail (the `--limit` mutation also fails the `--semantic` test, since that test's own assertion depends on `--limit 1` too — expected, not a false signal, verified independently above).

A review round found the `if x is not None:` guards on `--min-confidence`/`--limit` were untested against the exact KI-093 bug class: every test passed a truthy value, so mutating either guard to plain truthiness (`if min_confidence:`) — silently skipping a genuine `0.0`/`0` floor entirely — survived the whole suite. Closed with `test_min_confidence_zero_is_still_applied` (a confidence-less assertion, per ADR-0004, never satisfies even `--min-confidence 0.0`, so the floor being genuinely applied still excludes it) and `test_limit_zero_is_still_applied`, both mutation-tested against the exact truthiness mutation. Also added `test_include_history_composes_with_a_no_op_confidence_floor`, a CLI-level pin for KI-093's composition fix (a genuine no-op `--min-confidence 0.0` floor, on an assertion that has a real confidence recorded, doesn't re-narrow a `--include-history` match back to active-only) — the underlying semantics were already covered at the builder/backend level, but nothing pinned the CLI's own threading of both flags into one chain.

The same round found several help-text/docstring issues, all fixed: `--semantic`'s help claimed "Requires the KnowledgeBase's Embedder to be configured" — false through the CLI, since `Ontology.connect()` always defaults to `HashingEmbedder()` (copied from MCP's own docstring, where it's equally unreachable, pre-existing there) — reworded to name the precondition that actually bites, `ontolith reindex` needing to have run since the last write; `--min-confidence`'s help was missing ADR-0004's "`None` confidence never satisfies a numeric threshold, even 0.0" rule (documented on MCP's sibling parameter, omitted here) and still said "active" despite KI-093 widening the floor under `--include-flagged`/`--include-history`; the command's own docstring read as changelog prose ("KI-094/096 parity ... this command previously supported only --where") rather than describing behavior, unlike every sibling command's docstring — reworded to describe what the command does; and "only MCP exposes bitemporal time-travel today" was unqualified when the SDK's own `kb.as_of(t)` has it too — reworded to mirror MCP's own precise phrasing, scoped to the `query` surface specifically. Out-of-range/degenerate numeric values (`--min-confidence 5.0`, `--limit -1`) are deliberately left unvalidated at the CLI boundary, matching REST/GraphQL/MCP's identical lack of range-checking on the same parameters — adding a check only here would be a new asymmetry, not a fix to one.

A second review round found this round's own fix was incomplete in three ways, all corrected: the "`--trust-at-least 0` has no equivalent test" reasoning was wrong — `trust_level` being `NOT NULL` only makes the truthiness mutation unobservable for an entity that *already has* a qualifying assertion (trivially satisfying floor 0); an entity with *no* assertions at all is still excluded when the floor is genuinely evaluated and included when it's skipped, closed with `test_trust_at_least_zero_is_still_applied` using exactly that shape, mutation-tested; `test_min_confidence_zero_is_still_applied` and `test_include_history_composes_with_a_no_op_confidence_floor` each asserted only a negative ("No entities found.") with no check that their own setup (`assert`/`retract`) actually succeeded — both discarded return values silently passed under a broken seed (verified: renaming the predicate, or retracting a bogus id, left each test green) — fixed with explicit `exit_code == 0` checks plus a positive control on the confidence test and a "default query is empty" + "a genuinely-failing floor still excludes" pair on the composition test (the latter closing a second gap the round found: as originally written, dropping `--min-confidence`'s own wiring didn't fail the composition test either, since it only ever exercised the no-op-floor direction — reverting that wiring now fails it, mutation-tested); and the `--semantic` test's insertion-order discriminator implicitly relied on `SQLiteBackend.entities()` having no `ORDER BY`, promoting that into an asserted (if unstated) contract — replaced with an order-independent form querying two distinctly-worded entities and asserting each direction returns its own match, not the other's. Also fixed: the reworded `--as-of` docstring note read as self-contradictory in `--help` output ("only MCP ... — the SDK has it too"); a stray line-break in the summary caused a rendered "`--include-flagged/ --include-history`" double-space; and `QueryBuilder.min_confidence()`/`.trust_at_least()`'s own docstrings plus MCP's identical `min_confidence` parameter doc still said "active" — the same staleness just fixed on the CLI's copy — updated all three to state the KI-093 widening.

A third review round found that second round's own docstring fix was itself wrong on the one path it hadn't been checked against: the new `builder.py`/`mcp.py` wording claimed `.include_history()` widens `.min_confidence()`/`.trust_at_least()` under `.as_of()` too — false at the time, and empirically verified false then (a retracted, confidence-bearing assertion still matched `.as_of(t).min_confidence(...)` with no `.include_history()` at all, since the `as_of` branch never restricted to `active` in the first place, only conditionally excluded `flagged`; the same "`include_flagged` still applies, `include_history` is a no-op" carve-out `.where()` itself already documented). This has since changed for `retracted` specifically — see KI-095/ADR-0049 below, which narrows this exact claim. Corrected to scope the widening claim to current-state only and describe the `as_of` carve-out precisely, re-verified this time by direct reproduction (both the "no `include_history` needed" and the "`include_flagged` still applies" halves) rather than by pattern-matching the CLI's own current-state-only text. The round also caught that the same sentence conflated which opt-in adds which status (`include_flagged` → `flagged`; `include_history` → `superseded`/`retracted`, not both under either) — fixed in all three copies including the CLI's own help text; and two now-stale prose paragraphs in this document (this entry's own description of the `--semantic` test, superseded by the second round's rewrite; KI-094's entry above, whose correction paragraph described the CLI's gap in the present tense after this KI closed it) — both updated.

---

## KI-097 — `entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()`'s `as_of` branch reconstructs `flagged` from current status, not point-in-time — unlike `assertions()` — found resolving KI-095 ✓ RESOLVED (Backlog)

**Severity:** Bug — a `.where()`/`.min_confidence()`/`.trust_at_least()` query pinned to a time when an assertion *was* flagged can wrongly include it, if the contradiction has since been resolved
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §11.4 (`as_of` semantics — "reconstructs what we knew at time `t` about what was true at time `t`"), SPEC §10.3 (static contradictions)

### Description

While building ADR-0049's retraction-aware `as_of` fix (KI-095), found that `entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()`'s `as_of_time` branch excludes `flagged` (unless `include_flagged` is set) by checking the assertion's **current** `status` column — not whether it *was* flagged at `as_of_time`. `StorageBackend.assertions()`'s own `as_of_time` branch does this correctly already, via a `COALESCE((SELECT ae.action FROM assertion_event ae WHERE ae.assertion_id = assertion.id AND ae.at <= ? AND ae.action IN ('flagged', 'reactivated') ORDER BY ae.at DESC, ae.id DESC LIMIT 1), 'reactivated') != 'flagged'` reconstruction (both backends) — the three query-builder-facing methods never picked up the equivalent logic.

Verified by reproduction: an assertion flagged by a contradiction at `t1`, later reactivated (the contradiction's winner, `resolve_contradiction()`) at `t2 > t1` — `kb.as_of(t1).query(Person).where(name=...)` (a `t` strictly *during* the disputed window) wrongly returns the entity, because current status is `active` again by the time the query runs; `kb.backend.assertions(..., as_of_time=t1)` on the same data correctly returns nothing. `entities_meeting_confidence()`/`entities_meeting_trust()` share the identical bug, since they copy `entities_where()`'s `flagged_clause` verbatim.

This is the same class of gap ADR-0049 just fixed for `retracted` (current status used instead of point-in-time reconstruction), but for the `flagged`/`reactivated` transition pair instead — `reactivated` only ever follows `flagged` (`resolve_contradiction()`'s winner path), so it's a two-state cycle unlike retraction's one-way terminal transition, closer in shape to `assertions()`'s own already-correct handling than to ADR-0049's simpler EXISTS-based fix.

### Fix

Ported `assertions()`'s event-log-based `flagged`-at-`t` reconstruction verbatim into `entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()`'s `as_of_time` branches, on both backends — matching KI-093's established precedent that these three methods move together. Same `COALESCE((SELECT ae.action FROM assertion_event ae WHERE ae.assertion_id = <row> AND ae.at <= ? AND ae.action IN ('flagged', 'reactivated') ORDER BY ae.at DESC, ae.id DESC LIMIT 1), 'reactivated') != 'flagged'` shape `assertions()` already used, correctly handling the two-way cycle direction (a flagged-then-reactivated assertion reads as "flagged during `[t1, t2)`, active from `t2` on") the same way `assertions()` already did — no new logic to design, this was purely a matter of the three `QueryBuilder`-facing methods never having picked up code that already existed one file over. No ADR needed, as anticipated.

`entities_where()`'s `include_flagged` docstring (port + both adapters) rewritten to say so explicitly, in both directions — excludes a since-resolved dispute, includes a predispute value; `entities_meeting_confidence()`/`entities_meeting_trust()` already deferred to `entities_where()`'s own docstring by cross-reference (KI-093 precedent), so the corrected text reaches them too without needing three more direct copies. Also corrected a stale docstring in `store/base.py`'s `entities_meeting_confidence()` that had described the *bug itself* as an accepted "One caveat" limitation ("a `status = 'flagged'` assertion... is excluded even at a `t` before it was flagged, unless `include_flagged` is set") — this KI is exactly what fixed that — plus two stale cross-references elsewhere in the same file still pointing at "the flagged-status caveat" a review round found.

New tests: `conformance/test_confidence_trust_filters.py::TestAsOfConfidenceTrust` gained `test_{min_confidence,trust_at_least}_as_of_before_dispute_still_qualifies` (the corrected half — a `t` before the dispute existed must still qualify) alongside the pre-existing `test_{min_confidence,trust_at_least}_as_of_excludes_flagged_assertion`, which were themselves restructured: they previously captured `t` *before* the disputing assertion was even written, which — the bug this KI fixes — is exactly the case that must NOT exclude; moved to capture `t` mid-dispute instead, the case that genuinely should exclude. `conformance/test_bitemporal.py::TestAsOfRetractionAndFlagging` gained `.query()`-level counterparts (`test_query_as_of_before_dispute_shows_predispute_value`, `test_query_as_of_mid_dispute_excludes_even_after_later_resolution`) to its pre-existing `assertions()`-level tests of the same two shapes — the class previously had zero `.query()`-path coverage for either, only the already-correct `assertions()` path. Two pre-existing port-level unit test fixtures (`_seed_flagged_with_window` in `test_sqlite_backend.py`/`test_duckdb_backend.py`) constructed a `status='flagged'` assertion via raw `put_assertion()` with no matching `assertion_event` row — broke once the fix started consulting the event log instead of the status column; fixed by adding the matching event, same shape the retraction fixtures already used. Mutation-tested on both backends: reverting each `flagged_clause` (all three methods) back to the current-status check fails exactly its own new "before dispute"/`.query()` tests, with zero interference elsewhere.

A review round found the ported reconstruction's `ORDER BY ae.at DESC, ae.id DESC` tiebreak — the piece that matters when a flag and its resolution land in the same clock tick, so `at` alone can't order them — was genuinely untested at all six new call sites (both backends × three methods): removing `, ae.id DESC` from all six survived the entire 2252-test suite. `assertions()` already had a same-instant regression test (`test_as_of_after_same_instant_flag_and_resolve_shows_winner`); its `.query()`-level counterpart, exercising all three `QueryBuilder`-facing surfaces (`.where()`/`.min_confidence()`/`.trust_at_least()`) in one test, was missing entirely. Closed with `test_query_as_of_after_same_instant_flag_and_resolve_shows_winner`, mutation-tested on both backends (fails cleanly, zero interference). The round also found the fix's fail-*open* default on a missing `flagged` event (COALESCE defaults to "not flagged," so a `status='flagged'` row with no matching `assertion_event` — reachable only via a direct `put_assertion()` bypassing `Ontology`, or a pre-ADR-0049-era row — is visible at every `t`) was undocumented, the opposite default from `retracted_clause`'s fail-*closed* one three lines below it; ADR-0049 itself calls out its own fail-closed choice explicitly, this one didn't. Documented in the code comments (both backends) and cross-referenced from ADR-0049's own KI-097 update. Also fixed: the `include_flagged` docstring updates stated only the "excludes a resolved dispute" direction, omitting "includes a predispute value" (the direction a caller is more likely to be surprised by); two stale "flagged-status caveat" cross-references elsewhere in `store/base.py` that survived the caveat itself being corrected into a statement of fact; ADR-0049's own "filed separately, not closed here" note, now updated; and a hard-wrapped identifier in this file's own module docstring. The round also measured the reconstruction's cost on a 40k-assertion SQLite database: ~+20% on `entities_meeting_confidence()`/`entities_meeting_trust()`'s `as_of` full-concept-scan path (58.9ms→72.2ms, 64.2ms→78.5ms), ~+15% on `entities_where()` (10.9ms→12.6ms) — comfortably inside the §9 performance budgets, so not treated as a defect; no new benchmark guard added against this specific regression, since none existed for the pre-existing `entities_where()`/confidence/trust `as_of` paths either.

A second review round found round 1's own cross-reference claim was itself wrong: the code comment said the fail-open default was "cross-referenced from ADR-0049's own KI-097 update," but that update never actually mentioned it — the same overstated-fix-text class round 1 itself was closing. Fixed by adding the fail-open default explicitly to ADR-0049's KI-097 Update section. Also found: `QueryBuilder.include_flagged()`'s own docstring (never touched by this KI, despite being the surface SDK users actually read) still described the pre-KI-097 current-status rationale — corrected, along with looser mentions on `.min_confidence()`/`.trust_at_least()` and `AsOfView`'s class docstring; round 1's own comment edit had accidentally deleted `entities_where()`'s original bandit-B608 explanation from `sqlite/backend.py` (replacing it with a forward reference to `retracted_clause`'s comment, which itself pointed back — circular, with the real explanation gone, and `entities_meeting_confidence()`/`entities_meeting_trust()`'s own "see entities_where()'s identical comment" cross-references left dangling) while leaving the same explanation duplicated alongside a now-redundant copy of the exact same sentence in `duckdb/backend.py` — restored in SQLite, de-duplicated in DuckDB; ADR-0049's own perf Consequences bullet still described only the pre-existing `EXISTS` subquery, not this KI's additional non-short-circuiting `COALESCE` one; and an "always" that should have said "already," an orphaned two-word docstring line, both cosmetic.

A third review round found round 2's own restored bandit-B608 comment now stated a false reason: it said the `flagged_clause` fragment "has none" of the SQL keywords bandit's heuristic looks for, which stopped being true the moment the fragment gained a `COALESCE(... SELECT ... FROM ... WHERE ... ORDER BY ... LIMIT ...)` subquery (KI-097's own change) — the conclusion ("no `#nosec` needed") still held, just for a different reason: bandit's rule only fires on a SQL keyword joined into a string via concatenation/`.format()`/f-string, never inside a bare literal constant, and `flagged_clause`/`retracted_clause` are always the latter (confirmed by AST on both backends). Corrected in both backends' comments, re-verified bandit-clean after. Also found: ADR-0049's Decision section still claimed `.include_flagged()`'s sibling "no-op under as_of" claim "remains exactly as before" its own KI-097 Update section — true that it still applies, false that *why* is unchanged; corrected. `known-issues.md`'s own KI-095 entry described KI-097's bug in the present tense with no closure marker — added one. ADR-0048 had two more instances of the same pre-KI-097 rationale this whole review chain kept finding and fixing elsewhere: a dangling cross-reference to `entities_meeting_confidence()`'s "One caveat" paragraph (renamed away from "caveat" by this KI) and a stale "as_of path has honored include_flagged... since before this ADR" framing in its own Decision section that never mentioned KI-097 changed *why*. MCP's `ontolith.query` tool docs were the one surface left out of the point-in-time sweep entirely — closed.

---

## KI-098 — `StorageBackend.assertions()` has no way to opt back into seeing a retracted assertion under `.as_of()` — found reviewing KI-095 ✓ RESOLVED (Backlog)

**Severity:** Architecture gap — SPEC §10.3's "excluded from default retrieval **unless explicitly requested**" has no request path on this one method, post-ADR-0049
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §10.3 ("MUST be excluded from default retrieval unless explicitly requested"), SPEC §11.2 ("Flagged/superseded/retracted assertions are excluded by default; `.include_flagged()`/`.include_history()` opt in"), SPEC §11.4 (`as_of` semantics)

### Description

ADR-0049 (KI-095) gave `StorageBackend.assertions()`'s `as_of_time` branch the same retraction-aware exclusion `entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()` have, but applied it *unconditionally* — this method has no `include_history`-shaped parameter to opt back into a retracted assertion under `as_of` (unlike the `QueryBuilder`-facing trio, where `.include_history()` is exactly that opt-out). This was a deliberate, reviewed choice at the time (the method's `status` parameter is already ignored entirely once `as_of_time` is set, so there was no pre-existing capability being removed, and no caller relied on seeing a retracted assertion this way), not an oversight — but it does leave a real, if narrow, asymmetry: `kb.as_of(t).query(Concept).include_history()` can surface an entity via a retracted assertion, while `kb.as_of(t).assertions(subject=entity.id)` on the exact same view cannot surface the assertion itself. "Retracted assertions as of `t`" is answerable through the `QueryBuilder` path but not through `assertions()` directly, even for explicit audit/history use — which is precisely the use case `include_flagged`'s own docstring already names ("pass True for explicit audit/history views").

`AsOfView.assertions()` already forwards `include_flagged` as a keyword argument, so the equivalent `include_history` (or a differently-named opt-out, to avoid implying it also widens `superseded` — which is already visible under `as_of` regardless, per `entities_where()`'s own semantics) would be a small, precedented addition.

### Fix

Added `include_history: bool = False` to `StorageBackend.assertions()` (port + both adapters) and `AsOfView.assertions()`, mirroring `include_flagged`'s existing shape exactly: when set, the retraction-exclusion clause is skipped entirely, the same mechanism `.include_history()` already uses to opt out on the `QueryBuilder`-facing trio. Mechanical, as anticipated — no new bitemporal semantics decision, since the underlying mechanism ADR-0049 already built is being exposed, not redesigned; no ADR needed. Named `include_history` rather than a bespoke name (the Description above flagged this as open): the mismatch with this method's actual behavior — no effect on `superseded` (already visible under `as_of` regardless) or on the current-state path at all — is no worse than the mismatch `include_history` already carries on the `QueryBuilder`-facing trio, and a shared name across every `as_of`-capable read path is worth more than a name precisely scoped to one method's semantics. `AsOfView`'s class docstring updated to drop the "no equivalent opt-out today" caveat; ADR-0049 gained an Update section recording the parameter was added after all, as its own follow-up rather than folded back into that ADR.

New tests: `conformance/test_bitemporal.py::TestAsOfRetractionAndFlagging::test_include_history_opts_back_into_a_retracted_assertion_via_assertions` (both backends) — the same explicit-far-future-`valid_to` scenario the sibling `entities_where()`-family tests use, confirming `kb.as_of(t).assertions(...)` can now surface the retracted assertion `kb.as_of(t).query(Concept).include_history()` could already surface indirectly via the entity. Mutation-tested on both backends (forcing the exclusion clause to always apply, ignoring `include_history`, fails exactly this test on the mutated backend and nothing else). No port-level unit tests added, matching the precedent `assertions()`'s own `as_of` coverage already set during KI-095 (conformance-only, since this method has no dedicated port-level `as_of` test suite the way `entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()` do).

---

## KI-099 — `supersedes` (ADR-0050) is SDK-only — REST/GraphQL/MCP/CLI have no way to pass it — found reviewing KI-080 ✓ RESOLVED (Backlog)

**Severity:** Interface-parity gap — every write surface except the Python SDK is missing a capability the SDK now has
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §9.3 (direct-write surface), SPEC §9 (proposal surface); ADR-0050 (`supersedes` hint)

### Description

ADR-0050/KI-080 added an optional `supersedes: str | None = None` keyword parameter to `Ontology.assert_literal()`, `assert_ref()`, `propose()`, and `propose_ref()` — deliberately scoped to the Python SDK only, matching this project's established pattern of shipping a capability SDK-first and filing interface parity as its own follow-up (e.g. KI-081 → KI-094/KI-096 for `.include_flagged()`/`.include_history()`). No other interface has a request/argument shape for it at all, so a caller on any of them writing to a `cardinality="many"`, `time_varying` property can only ever get the new coexistence default — explicit replacement of one specific concurrent value is unreachable except from the SDK directly. Concretely, per interface:

- **REST**: `POST /assertions`'s `WriteAssertionIn` body (direct write, `assert_literal`/`assert_ref`) and `POST /proposals`'s `ProposeIn` body (`propose`/`propose_ref`) — both already discriminate literal vs. ref via exactly-one-of `(value, value_type)` or `target`; `supersedes` would be one more optional field on each.
- **GraphQL**: `Mutation.propose` is the only mutation that asserts a value at all (ADR-0037: GraphQL deliberately has no direct-write mutation, unlike REST — `createEntity`/`retract`/`flagContradiction`/`resolveContradiction` are its other mutations, none of them a value assertion) — its `ProposeInput` would need a `supersedes` field, camelCase needing no renaming.
- **MCP**: the `propose` tool only (SPEC §14.4's no-direct-write-tool rule) — same shape as GraphQL's gap.
- **CLI**: `ontolith assert` (literal only — it never wires `assert_ref`, a separate pre-existing gap this KI doesn't extend) has no `--supersedes` flag; there is no `propose` subcommand at all today (its own docstring already points callers needing the governed propose→review→accept path at the SDK).

### Fix

`supersedes: str | None = None` added to every shape named in the Description above, each forwarding straight through to `Ontology` with no additional validation of its own (all real validation stays in `Ontology._require_valid_supersedes_hint`/`route()`, per ADR-0050): REST's `WriteAssertionIn`/`ProposeIn` (`POST /assertions`/`POST /proposals`) gain the field; GraphQL's `ProposeInput` (`Mutation.propose`) gains it, camelCase needing no renaming as anticipated; MCP's `ontolith.propose` tool gains the keyword argument; the CLI's `ontolith assert` gains a `--supersedes` flag. Mechanical, as anticipated — no new validation logic anywhere, since `Ontology` already rejects an out-of-scope or invalid hint identically regardless of which interface the call came through.

New tests in `tests/unit/test_rest.py::TestWriteAssertionRoute`/`TestCreateProposalRoute`, `tests/unit/test_graphql.py::TestProposeMutation`, `tests/unit/test_mcp_server.py::TestProposeTool`, `tests/unit/test_cli.py::TestAssertLiteral` — for each of the nine `supersedes=` call sites the change touches (REST's literal/ref branches on both `POST /assertions` and `POST /proposals` — four; GraphQL's and MCP's literal/ref branches — two each; the CLI's one literal-only command), a forwarding case applying a small `cardinality="many"`, `temporality="time_varying"` schema and confirming `supersedes=<id>` replaces exactly the named prior value (verified via `kb.assertions(status="active")` afterward). Mutation-tested throughout: the four ref-branch sites each fail exactly their own forwarding test and nothing else; the five literal-branch sites each fail their own forwarding test *and* that route's rejected-hint test (REST's `assert_literal`/`propose` sites each pair with their own route's case, not each other's), since both cases route through the same literal call. REST, GraphQL, and MCP all cover the `time_varying`+`many` relation shape (`assert_ref`/`propose_ref`), not just the literal-property one. One rejected-hint case per interface (REST covers both `POST /assertions` and `POST /proposals`) confirms `VALIDATION_ERROR`/exit code 1 through that interface's own error mapping, not a raw exception — these run against no schema at all (the `temporality != "time_varying"` branch of the scope check), since the SDK-level `cardinality != "many"` branch is already covered directly in `conformance/test_conflict.py::TestManyCardinalityTimeVaryingSupersedes::test_supersedes_rejected_for_single_cardinality_time_varying`.

---

## KI-100 — `_apply_with_conflict_routing`'s open-contradiction extension is O(members) per write, entirely unbenchmarked, and can blow the propose+commit budget — found benchmarking M4's performance budgets

**Severity:** Performance gap — a real, reproducible per-write cost with no budget coverage and no known ceiling
**Milestone target:** Backlog
**SPEC reference:** SPEC §9 (performance budgets — `propose` + policy eval + commit, p95 < 50 ms), SPEC §10.3 (contradiction extension)

### Description

`Ontology._apply_with_conflict_routing`'s "extend an already-open contradiction" fast path (the branch taken when a `static` predicate already has an open contradiction) does **two** backend round-trips per existing member of that contradiction, for every new write that lands in it: a `backend.get_assertion(mid)` read, unconditional for every member (the `continue` guard below it, for a member already `retracted`/`superseded` — KI-034/KI-044 — consumes the read's own result, it doesn't avoid the read), and a `backend.set_assertion_status(mid, "flagged")` write, skipped by that same `continue` for a terminal member but otherwise unconditional too — including for a member already flagged, where only the *event-recording* is skipped (`already_flagged`), never the status write itself, which is a genuine no-op there. Profiled directly, the write accounts for a larger share of the per-member cost than the read, roughly 45-50% vs. 35-40%. Reproduced directly while fixing `tests/benchmarks/test_traversal.py`'s `propose`+commit benchmark (M4, performance budgets workstream): repeatedly asserting differing static values on the same `(subject, predicate)` opens a contradiction on the 2nd write and keeps extending it. Per-member cost is flat (measured on the order of tens of microseconds per member across several independent runs at up to 3,000 members, both on a fresh KB and on the 100k-assertion seeded benchmark dataset) — **linear** growth, matching this entry's own title ("O(members) per write"), not superlinear as an earlier draft of this entry claimed. SPEC §9's 50 ms `propose`+policy-eval+commit budget is itself a **p95** target, and this path's own p95/median ratio runs roughly 1.2-4x from SQLite write-tail variance (median close to 1.5x) — so it crosses the budget meaningfully earlier by p95 than by median. The exact member count at which that happens varies run to run and machine to machine (measured anywhere from several hundred to just over a thousand members across independent reproductions) enough that no single number here would stay accurate, matching why §9 itself declines to quote precise multiples for its own budget margins — the qualitative point is what matters: a member count reachable by unremarkable, unprivileged repeated writes is enough, and nothing today caps contradiction size or escalates once one gets large (there is no cap of any kind).

This is real production behavior, not a benchmark artifact — the benchmark this KI was found while fixing (see `CHANGELOG.md`'s M4 section) was itself broken *because* it accidentally exercised this exact path across a growing number of rounds; the fix moved every write off this path entirely (a fresh `(subject, predicate)` per round, so nothing ever contradicts), which correctly stabilized the benchmark but also means nothing in the suite exercises this path's cost at all going forward. The SPEC §10.3 pseudocode itself doesn't set a bound on contradiction size, and no code path anywhere rejects or caps one — `flag_contradiction()`/`resolve_contradiction()` both operate on `member_ids` lists of arbitrary length already.

### Fix

(a) is done: `tests/benchmarks/test_contradiction_extension.py` times one write extending an open contradiction at 10, 100 and 500 existing members (informational, no budget ceiling yet); measured locally, the cost grows roughly linearly with the member count, reaching on the order of ten milliseconds at a few hundred members. (b) is not started. Two independent angles, not mutually exclusive: (a) a dedicated benchmark exercising this specific path at a realistic member count, so a future regression or improvement here is visible in the M4 performance-budgets workstream rather than silently unmeasured (mirrors how a benchmark for `propose()`'s own auto-accept path was missing until M4's benchmark-fixing pass found it missing); (b) investigate whether **both** per-member round-trips are avoidable, not just the read — the `set_assertion_status(mid, "flagged")` write is the larger of the two costs and is provably a no-op whenever the member is already flagged (the code already computes `already_flagged` to skip the event write; the status write itself isn't gated the same way), so it's the more promising target, not the `get_assertion` read a narrower reading of this KI might reach for first — and/or whether a size cap or review-routing escalation makes sense once a contradiction crosses some threshold, which would be a genuine SPEC §10.3 semantics question needing its own design decision, not a mechanical fix.

---

## KI-101 — No remote `QueryBuilder`/`AsOfView` proxy for an isolated plugin call

**Severity:** Architecture gap — a real, if currently unused, capability restriction
**Milestone target:** Backlog — filed while building M4's plugin process isolation (ADR-0051)
**SPEC reference:** SPEC §13.2 (plugin protocols); ADR-0051 (plugin process isolation)

### Description

`ReadOnlyView.query(concept)`/`.as_of(t)` both return a fluent builder object (`QueryBuilder`/`AsOfView`) holding a live `StorageBackend` reference, not a plain, picklable value — crossing ADR-0051's process boundary would need its own remote-proxy protocol (accumulate the fluent chain on the child side, replay it against the real object in the parent only on a terminal call like `.all()`/`.first()`/`.count()`), which this pass didn't build. `plugins/sandbox/remote_view.py`'s `RemoteReadOnlyView.query()`/`.as_of()` instead raise a clear `PluginError` naming this limitation.

No shipped reference plugin calls either method today — `CsvImporter`/`JsonExporter`/`RdfExporter`/`RequiredFieldsValidator` all use `assertions()`/`get_entity()`/`schema()`, which are already proxied — so this is not a regression against any real, exercised code path. A future `Reasoner`/`Connector` plugin wanting anything beyond `assertions()`'s flat filter (e.g. a `.where(...).limit(n)`-shaped query) would hit it, with `isolate=False` as the immediate workaround (opts out of sandboxing entirely, not just this one limitation).

### Fix

Not started. Build a remote fluent-builder proxy mirroring `RemoteWriteView`'s pattern: the child-side proxy object accumulates each chained call (`.where(...)`, `.limit(...)`, etc.) locally instead of sending it immediately, and only sends one IPC message describing the full accumulated chain when a terminal method (`.all()`/`.first()`/`.count()` for `QueryBuilder`; the equivalent for `AsOfView`) is actually called — matching how `QueryBuilder` is already lazy in-process. No design decision needed beyond this shape; deferred because nothing exercises it yet, not because the approach is unclear.

---

## KI-102 — No timeout on an isolated plugin call — a hung plugin blocks the caller indefinitely

**Severity:** Architecture gap — a real, currently-unbounded blocking risk with no test or mitigation
**Milestone target:** Backlog — found in round-2 review of M4's plugin process isolation (ADR-0051)
**SPEC reference:** ADR-0051 (plugin process isolation)

### Description

`plugins/sandbox/runner.py`'s `_dispatch_loop` reads every message from the sandboxed child via `protocol.recv_from_child`, which wraps `Connection.recv_bytes()` — a blocking call with no deadline. A plugin whose own code hangs (an infinite loop, a blocked network call the platform doesn't deny, a deadlock) never sends another message, so the parent's calling thread blocks indefinitely inside `recv_bytes()`. `run_isolated`'s own cleanup (`process.join(timeout=5)` then `process.terminate()`) is in a `finally` block that only runs once `_dispatch_loop` itself returns or raises — it provides no help while still blocked waiting on the child's next message, so it does not bound the caller's own wait time despite looking like it would.

This is a real, if narrower, escalation of a pre-existing gap: in-process plugin execution (pre-ADR-0051) also had no timeout, but a hang there was at least visible as the caller's own thread doing nothing productive, in the caller's own process. Under isolation, the caller's thread is blocked on IPC specifically, with a live but unresponsive child process consuming resources on the other end of the pipe — a materially different (and, for a "Production 1.0" milestone, more consequential) failure mode to leave unbounded.

### Fix

Not started. Bound `recv_bytes()` with a `poll(timeout)` check before each blocking read (`Connection.poll()` accepts a timeout and returns whether data is ready without blocking indefinitely), escalating to `process.terminate()`/`process.kill()` and a clear `PluginError` if a deadline is exceeded. No design decision needed for the mechanism; the open question is what the default deadline should be (and whether it should be configurable per `register()`/per-call) — SPEC §9 names no performance budget for plugin invocation to anchor a default against, so this needs its own, if small, design call before implementation.

---

## KI-103 — Three more known-vulnerable transitive dependencies (`anyio`, `httpx2`, `httpcore2`); `pip-audit` CI gate red a third time ✓ RESOLVED (Backlog)

**Severity:** Security/supply-chain — `anyio` is runtime-reachable this time, unlike KI-062/KI-087's dev-only exposure
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** Implementation Plan §7.1 (supply-chain gate)

### Description

`pip-audit` against the full locked dependency set reported nine findings, found while reviewing M4's plugin process isolation (ADR-0051) — unrelated to that branch's own changes (`git diff main...HEAD -- uv.lock` before this fix touched only the two `pyseccomp` lines it added):
- `anyio==4.14.1` — CVE-2026-63374 / CVE-2026-64847 / CVE-2026-63349, fixed in `4.14.2`. **Runtime-reachable**, unlike KI-062's and KI-087's dev/docs-only findings: `mcp` and `starlette` both depend on it, so it ships with `ontolith[mcp]`, `ontolith[rest]`, and `ontolith[graphql]`, on the authenticated request path. CVE-2026-64847 in particular concerns `anyio`'s own process-pool workers not draining a subprocess's `stderr`, worth a second look given this KI was found while building a `multiprocessing`-based sandbox — Ontolith's own sandbox doesn't use `anyio`'s process pool, so it isn't directly affected, but the failure shape (untrusted/faulty code writing too much to a pipe) is exactly this project's own current threat model.
- `httpcore2==2.7.0` — PYSEC-2026-3844, fixed in `2.10.0`.
- `httpx2==2.7.0` — PYSEC-2026-3845/3846/3847/3848/3849, fixed across `2.10.0`–`2.12.0`. Dev-only (test-suite dependency), same class as KI-062's original two findings.

`.github/workflows/security.yml`'s `pip-audit` step is unconditional, so the `scan` job fails on every PR regardless of that PR's own changes — the third recurrence of the identical pattern KI-062 and KI-087 already resolved once each; KI-087's own closing note anticipated this happening again.

### Fix

`uv lock --upgrade-package anyio --upgrade-package httpx2 --upgrade-package httpcore2` — same mechanical fix as KI-062/KI-087, all three transitive (no direct `pyproject.toml` dependency changed). `anyio` resolved to `4.14.2`; `httpcore2`/`httpx2` both resolved to `2.13.0`. `uv run pip-audit` now reports zero findings.

---

## KI-104 — `store/{sqlite,duckdb}/migrations.py`'s per-`up()` defensiveness should become a declarative, centrally-applied registry once a third migration is added

**Severity:** Architecture gap — not a bug in the current two-migration registry, a maintainability risk for the next one
**Milestone target:** Backlog — filed in round-5 review of M4's storage-format migrations (ADR-0052)
**SPEC reference:** SPEC §15 (Versioning & migration); ADR-0052 (on-disk storage-format migrations)

### Description

ADR-0052's migration framework took five review rounds to converge, and four of those rounds (2 through 5, the last coming back clean) each found the *same* root-cause class of bug: `_infer_format_version` collapses each table's own independent state into one global version scalar, then `migrate_file` dispatches every registered migration above that scalar regardless of which specific table each one targets — so a migration can be handed a target table in a state its own version number says nothing about (the column already present; the table absent entirely; the table shadowed by a view). Each round discovered one more such state the relevant `up()` hadn't been taught to tolerate, and fixed it by adding another existence/column check to that specific `up()` function, by hand, on both backends.

The current registry (`_MIGRATIONS`, two entries: `principal_credential.issued_by`/`.revoked_by`, `proposal.reviewers`) is now correct and exhaustively verified for every reachable combination (round 5's own 100-shape empirical sweep found zero failures), so there is no bug to fix today. But the defensiveness this took five rounds to build lives entirely inside each `up()` function's own hand-written guard clauses, duplicated per-migration and per-backend — a **third** migration (a hypothetical v4) would need the identical class of guard re-derived by hand, by whoever adds it, without the benefit of the four rounds of review that found this shape the first three times.

### Fix

Not started; no design decision needed beyond confirming the shape. When a third migration is actually added, consider replacing the callable-`up`/`down`-per-`_Migration` shape with a declarative one for the common (single-table, add-columns) case — e.g. `_Migration(version=4, table="...", add_columns={"col": "TYPE", ...})` — with one central applier that does, for any such entry: skip if the table isn't a base table (absent or a view), skip each column already present, else `ALTER TABLE ADD COLUMN`. That makes the whole bug class structurally unrepresentable instead of requiring per-migration vigilance, and lets `_infer_format_version` derive its own per-table checks from the same declarative data instead of the two being hand-maintained in parallel (a second source of the divergence this round's own findings kept surfacing). Keep an `up`/`down` callable escape hatch for a future migration that isn't column-shaped (a widened `CHECK`, a table rewrite, an index change) — not every migration will fit the declarative shape, and forcing one that doesn't would be worse than the status quo. Deliberately not done now: refactoring working, exhaustively-verified code with no reachable bug, purely to reduce future maintenance risk, isn't worth the churn on its own — the trigger is the next migration actually being added, not this filing.

---

## KI-105 — `format_version` freeze pins the version number, not the actual on-disk shape or the sqlite-vec extension's own format

**Severity:** Test gap / Architecture gap — no reachable bug today, a real coverage gap in what the freeze actually guards
**Milestone target:** Backlog — filed during M4 Workstream 6's `format_version` freeze declaration (ADR-0052)
**SPEC reference:** SPEC §15 (Versioning & migration); ADR-0052 (on-disk storage-format migrations)

### Description

Two related gaps found while declaring `format_version=3` frozen for 1.0 (M4 Workstream 6), both confirmed by direct reproduction, neither a bug in code shipped today:

**(a) No golden-schema test — an edited `CREATE TABLE` with no matching migration goes undetected.** `format_version` is a single tracked integer; nothing snapshots the actual DDL each backend's fresh-connect path creates. Reproduced: adding a new column to `proposal`'s `CREATE TABLE` in both `store/sqlite/backend.py` and `store/duckdb/backend.py`, with `CURRENT_FORMAT_VERSION` left unchanged at 3, leaves the entire unit + conformance suite (2423 tests, including the new `TestFormatVersionFrozen` class) entirely green. Every migration test creates a brand-new file matching whatever the current `CREATE TABLE` statements say, so none of them can notice that those statements changed underneath a fixed version number. In practice: an engineer edits a `CREATE TABLE` without registering a migration and bumping `format_version`; an old on-disk file still opens successfully (still reports `format_version=3`) and then fails at query time on the missing/extra column — the exact "side effect nobody noticed" `TestFormatVersionFrozen` exists to prevent for the version *number*, with no equivalent guard for the *shape* the number is supposed to track.

**(b) sqlite-vec's own on-disk format is outside `format_version`'s scope, undocumented.** `SQLiteBackend._ensure_vector_table` creates `vec0` virtual tables via the `sqlite-vec` extension, pinned pre-1.0 in `pyproject.toml` with its own "breaking changes possible" comment. Their on-disk layout is owned by that extension, not by anything `format_version` tracks — a future `sqlite-vec` version bump that changes `vec0`'s storage format would not be caught by `format_version`, `migrate_file`, or `TestFormatVersionFrozen` at all. ADR-0052 never names sqlite-vec or vec0.

### Fix

Not started. (a) Add a golden-schema regression test per backend alongside `TestFormatVersionFrozen` — a snapshot of every base table's definition for a freshly-created database (SQLite: `sqlite_master.sql`, which covers table DDL, indexes, and constraints in one text blob; DuckDB needs the equivalent breadth, not just `information_schema.columns` alone — column-only coverage would miss the non-column-shaped changes KI-104 itself already names as a future migration category, e.g. a widened `CHECK` or an index change), so an edited `CREATE TABLE` with no matching migration+version bump fails immediately instead of silently. (b) Add one explicit sentence to ADR-0052 scoping `format_version` to Ontolith-owned tables only, cross-referencing the `sqlite-vec` pin's own "breaking changes possible" comment as the (pinning, not migration) mechanism that currently guards `vec0`'s format. Neither started now — filed as a known gap in the freeze's own coverage, not a design decision that needs making today.

---

## KI-106 — `capabilities.filesystem=True` subsumes `granted_capability`'s own storage ceiling entirely

**Severity:** Architecture gap — a real governance bypass for any plugin with `filesystem=True`, not a hypothetical
**Milestone target:** Backlog — filed during M4 Workstream 7's security review (ADR-0051/ADR-0015)
**SPEC reference:** SPEC §17 (Security model — "MUST deny undeclared access (storage, network, filesystem)"); ADR-0015 (capability ceilings)

### Description

`PluginRegistry.register()`'s `granted_capability` parameter caps what storage capability (`read`/`propose`/`write`) a plugin's `view` argument can exercise, and read-only plugin kinds are further hard-capped to `read`. But this ceiling only restricts the *proxied view object* the plugin is handed — it does nothing to stop a plugin with `capabilities.filesystem=True` from bypassing the view entirely: `filesystem=True` means the OS-level filter (`sandbox/enforcement.py`) never denies `open`/`openat`/etc., so the plugin's own code can `sqlite3.connect(<the KB's own db path>)` directly and execute arbitrary DDL/DML against it — `UPDATE assertion SET value_lit=..., status='active'`, say — with no proposal, no policy evaluation, and no `assertion_event`/`admin_event` audit row. `granted_capability="read"` is not a real limit on such a plugin.

Three of the four shipped reference plugins (`csv-importer`, `json-exporter`, `rdf-owl-exporter`) declare `filesystem=True` today — only `required-fields-validator` doesn't (it declares no capabilities at all, i.e. every default, which is `filesystem=False`) — so this is the common case, not an edge case. The registration-time warning (`registry.py`'s `_warn_if_unenforced_capabilities_requested`) now explicitly discloses this (fixed alongside this filing, M4 Workstream 7) — an operator registering a `filesystem=True` plugin sees a warning naming the bypass, rather than the capability's declared storage ceiling reading as a real guarantee it isn't.

### Fix

Not started (the disclosure above is the only part of this filing that shipped). The structural fix: give an importer a parent-opened, capability-scoped readable proxy for its *source* argument, mirroring `RemoteWritable`'s existing treatment of an exporter's *target* — the plugin would then need `filesystem=True` only for genuinely-plugin-owned files (a temp scratch directory, say), never for the KB's own storage file, and the reference importers/exporters would only need `filesystem=True` when they actually take a filesystem path (as opposed to an open, already-provided handle). Longer term, path-scoped OS-level grants (Landlock on Linux ≥5.13) that always exclude the KB file and its `-wal`/`-shm` siblings would close this even for a plugin that insists on `filesystem=True` for an unrelated reason. Neither started now — this is real design/implementation work (a new `RemoteReadable` proxy shape, updating the reference importers to use it, deciding whether `filesystem=True` still means what it currently does for a plugin that only ever touches its own scratch files), not a small fix bundled into this review pass.

---

## KI-107 — `PrincipalCredential` has no expiry — a stdio MCP token authenticates indefinitely until manually revoked

**Severity:** Architecture gap — ADR-0014 recommended a mitigation the credential model can't actually provide
**Milestone target:** Backlog — filed during M4 Workstream 7's security review, correcting ADR-0014
**SPEC reference:** ADR-0014 (MCP authentication); SPEC §17 (Security model)

### Description

ADR-0014's stdio-residual-exposure paragraph recommended "short-lived tokens" for stdio-facing principals, since stdio has no header channel and a token must live in the client's own process environment. `PrincipalCredential` (`identity/credential.py`) has no `expires_at` field and `get_principal_by_token_hash` performs no expiry check at all — only `issued_by`/`revoked_by` for manual, admin-initiated revocation. A stdio token, once issued, authenticates indefinitely regardless of age; "short-lived" was never actually achievable, only immediate manual revocation was. Corrected in ADR-0014's own text and this entry (KI-067) to recommend proactive rotation/revocation instead, the mitigation the current model actually supports.

### Fix

Not started. Add an optional `expires_at: datetime | None` to `PrincipalCredential`, set at issuance (`issue_token(..., expires_at=...)` or a fixed default TTL), and check it in `get_principal_by_token_hash`/wherever a token is resolved to a principal, treating an expired credential the same as a revoked one (`AuthError`). Needs a design decision this filing doesn't make: whether expiry is opt-in per issuance (a caller-supplied `expires_at`) or has a project-wide default, and whether an expired-but-not-revoked credential should still show up distinctly in `list_credentials`-shaped output for audit purposes. Not urgent enough to block 1.0 — the existing `revoked_by` mechanism already lets an admin close the gap immediately once aware, and no exploit path was found beyond "the token keeps working for longer than 'short-lived' would imply."

---

## KI-108 — `ontolith db migrate` changes on-disk DDL with no actor and no audit event; a `reversible=True` migration has no way to actually be reversed

**Severity:** Test gap / Architecture gap — not a security bypass, an audit/observability gap
**Milestone target:** Backlog — filed during M4 Workstream 7's security review
**SPEC reference:** SPEC §18 (Observability — "Events: … schema migration"); SPEC §15 (reversibility)

### Description

`migrate_file`/`ontolith db migrate` operates directly on a file path, deliberately outside `Ontology.connect()` (which would refuse to open the very file this function exists to unblock) — so it has no `Principal`, no `author`, and never calls `record_admin_event`. A DDL-changing operation this consequential leaves no record of who ran it or when, unlike every other admin-shaped action in this project (`register_plugin`, credential issuance/revocation, etc.), which SPEC §18 names schema migration alongside as an event this project should emit. Separately: every migration's `down()` (`_MIGRATIONS`) is fully implemented and exercised directly by tests (`test_each_migration_reversal_restores_the_prior_shape`), but nothing in `src/` ever calls it — there is no CLI command or API to actually reverse a migration, despite SPEC §15's "reversible" designation implying one should exist.

### Fix

Not started. (a) Have `db migrate` optionally accept an `--author` (no existing env var to fall back to — `ONTOLITH_DB` is the only one the CLI currently supports, and it's unrelated) and, after a successful migration, write an admin-event-shaped record — this needs its own storage path since `migrate_file` runs before/outside a normal `Ontology`-backed connection, so it can't reuse `record_admin_event` as-is without some restructuring. (b) Add `ontolith db migrate --to N` (or a dedicated `db rollback`) that calls the appropriate migrations' `down()` in reverse order, with the same dry-run support `db migrate` already has going forward. Neither started now — both need a design decision (how to attribute an action that by definition runs before the normal principal/capability system is even reachable) this filing doesn't make.

---

## KI-109 — A sandboxed plugin's own module import/constructor can defeat enforcement for the rest of the call too, not just leave filesystem open during loading

**Severity:** Architecture gap — a real, disclosed residual; round 3 found it's worse than round 2's own framing, round 4 found round 3's own mitigation attempt doesn't help
**Milestone target:** Backlog — filed during M4 Workstream 7's security review, round 2; extended rounds 3 and 4
**SPEC reference:** SPEC §17 (Security model — "MUST deny undeclared access")

### Description

Round 1 of this review's own remediation attempted to close this by installing the *full* enforcement filter (network + filesystem + process-isolation floor) before `_load_plugin_instance` ran at all. That broke every `filesystem=False` plugin's own loading on Linux+libseccomp: `entry_points()`'s metadata scan and `EntryPoint.load()`'s module import both need real filesystem reads regardless of what the plugin's own manifest declares, and `filesystem=False` is this project's own default (`required-fields-validator`, the one shipped reference plugin with default capabilities, failed to load in CI with `PluginError: Plugin entry point not found`). Reverted and replaced with `enforcement.apply_preimport_enforcement`, a narrower pre-import filter that only denies network (if declared `False`) and the always-on process-isolation floor — deliberately not filesystem, at any point before the plugin's own protocol method is called.

**Round 2's own framing of the residual understated it.** Round 2 described this as: a plugin's own module-level code and `__init__` can still read and write the filesystem freely during loading, but filesystem enforcement for the *rest* of the call (the protocol method itself) remains exactly where it was, unaffected. **Round 3 reproduced directly that this second half is false**: because the plugin's own module-level code runs in the same interpreter, before the full filter is ever attempted, it can tamper with the Python-level state that `apply_capability_enforcement` itself depends on — reassigning `enforcement.apply_capability_enforcement` as a plain module attribute, or (not reproduced, but the same class of gap) poisoning `sys.modules["pyseccomp"]`, which that function's own body re-imports fresh on every call. A plugin doing this makes even the *post-load* enforcement attempt falsely report success, silently defeating both the ordinary advisory warning and `require_enforcement=True` for the entire rest of the call, not just the loading phase. Reproduced end to end: an installed entry point whose module-level code reassigns `apply_capability_enforcement` to always return `applied=True` ran to completion (including a real filesystem write) with `require_enforcement=True`, no exception, no warning.

**Attempted, round 3 — and round 4 found this attempt doesn't meaningfully help, correcting round
3's own overclaim.** `_child_main` captures a reference to the real `apply_capability_enforcement`
function *before* calling `_load_plugin_instance`, so reassigning the module's own
`apply_capability_enforcement` attribute afterward no longer affects the captured reference. Round 3
described this as closing "the specific, reproduced module-attribute variant." **Round 4 reproduced
directly that it doesn't**: the captured function's own body still resolves every name it depends on
(`_install_seccomp_filter`, `EnforcementResult`, `_FILESYSTEM_SYSCALLS`, `_NETWORK_SYSCALLS`,
`_PROCESS_ISOLATION_SYSCALLS`) fresh from the same shared, mutable `enforcement` module namespace on
every call — reassigning `enforcement._install_seccomp_filter` (verified:
`enforcement._install_seccomp_filter = lambda denied: enforcement.EnforcementResult(applied=True,
reason=None)`) defeats the *captured* reference exactly as completely as reassigning
`apply_capability_enforcement` itself would have. Capturing the outer function object protects
nothing about its inner dependencies, which live in the identical module `__dict__` regardless of
which name a caller used to reach the function. **In practice, this capture defends only against the
single most naive reproduction — kept because it's free and harmless, not because it materially
narrows the attack.** The `sys.modules["pyseccomp"]` variant this entry originally named is one
instance of the same underlying gap, not a separate, deeper one — any module-level name the function
touches is equally reachable. `require_enforcement` is documented everywhere as protecting against a
*non-adversarial* failure to enforce (a container's own outer seccomp blocking it, missing
libseccomp, an unresolvable syscall name) — not as a security boundary against a plugin actively
trying to defeat it.

### Fix

Not started (round 3's function-reference capture is kept as a harmless no-op-cost step, not counted
as progress on this issue — see above). The only real fix needs argument-filtered seccomp rules:
allow `open`/`openat` in read-only mode (masking the flags argument against `O_WRONLY`/`O_RDWR`/
`O_CREAT`) during the pre-import phase, while still denying every filesystem-mutation syscall
(`unlink`, `rename`, `mkdir`, `chmod`, etc.) and write-mode opens outright — then the existing full
filter, installed after loading, tightens further to deny reads too. Because the filter would then
be installed *before* any plugin code runs at all, no Python-level tampering of any kind — not the
function, not `pyseccomp`, not `sys.modules`, nothing this entry or its own fix could ever capture a
reference to — would have anything left to defeat: a real OS-level, kernel-enforced filter can't be
undone by userspace code once loaded, no matter what runs afterward in the same process. This needs
real Linux-specific seccomp argument-filter work this review deliberately didn't attempt, given the
risk of another CI-breaking regression (round 1's own experience) and the inability to test seccomp
behavior at all on a non-Linux development machine. `openat2`'s flags sit in a struct seccomp can't
inspect via a simple argument comparator, so it likely needs to stay fully denied in the pre-import
phase regardless of read/write intent. There is no smaller, partial hardening step worth doing
instead — round 4 confirmed that capturing more references (even the `pyseccomp` module itself, not
just `apply_capability_enforcement`) doesn't close this class of gap, only the full pre-load
kernel-level filter does.

---

## KI-110 — `require_enforcement`'s guarantee can be satisfied by a partially-installed filter; an unresolved syscall name or a failed TSYNC is silently treated as "still applied"

**Severity:** Architecture gap — narrows what `require_enforcement=True` actually guarantees
**Milestone target:** Backlog — filed during M4 Workstream 7's security review, round 2
**SPEC reference:** SPEC §17 (Security model — "MUST deny undeclared access")

### Description

`enforcement.py`'s filter-install loop deliberately skips any syscall name libseccomp/the running kernel doesn't recognize, and still returns `applied=True` as long as the overall `SyscallFilter.load()` call itself succeeds — this is a documented, deliberate "best-effort denial of everything resolvable beats aborting the whole filter over one unresolvable name" choice, reasonable for the *default* (`require_enforcement=False`) advisory posture. But it means `require_enforcement=True`'s "refuse the call rather than proceed unenforced" guarantee is weaker than it reads: if the installed libseccomp/kernel doesn't recognize one of the newer syscall names this floor denies (`pidfd_getfd`, `process_mrelease`, etc. — recent kernel additions an older libseccomp build may predate), that specific syscall is silently left allowed (`defaction=ALLOW` is the filter's own default action) while `EnforcementResult.applied` still reports `True`, satisfying `require_enforcement` without actually denying everything the caller believed was denied.

**Round 4 found the same shape in `CTL_TSYNC` (added that round)**: if `SyscallFilter.set_attr(Attr.CTL_TSYNC, 1)` itself raises (an older libseccomp build without TSYNC support), the filter still loads — on the calling thread only — and `applied=True` is still reported exactly as if every thread were covered. A thread a plugin started before the filter installed stays unfiltered regardless, invisible to `require_enforcement` the same way an unresolved syscall name is.

### Fix

Not started. Have `_install_seccomp_filter` collect which requested names it had to skip (not just swallow the exception) and surface them in `EnforcementResult` (a new field, e.g. `skipped: tuple[str, ...]`). Under `require_enforcement`, treat any skipped name from the *process-isolation floor* specifically (the always-on part, not the plugin-declared network/filesystem lists, where "the plugin explicitly asked for less than full denial" is a different, already-accepted risk) as `applied=False` — that floor's own denial list is not something the caller opted out of, so a silent partial application of it shouldn't count as success. The same treatment applies to a `set_attr(CTL_TSYNC, ...)` failure — surface it the same way, since an unsynced filter is exactly this kind of partial application. Longer term, moving from a default-`ALLOW` deny-list to a default-`ERRNO`/allowlist model would remove this class of gap for every future kernel addition at once, not just the ones this project remembers to add by name — a substantially larger redesign, not attempted here.

---

## KI-111 — Process-isolation floor still has real gaps: the kill syscall family, inherited-environment secrets, and several other cross-process-capable syscalls

**Severity:** Architecture gap — found reviewing round 2's own pidfd fix; pre-existing, not introduced by it
**Milestone target:** Backlog — filed during M4 Workstream 7's security review, round 2
**SPEC reference:** SPEC §17 (Security model — "MUST deny undeclared access")

### Description

Reviewing round 2's `pidfd_*`/`kcmp`/`process_madvise`/`process_mrelease` additions to `_PROCESS_ISOLATION_SYSCALLS` surfaced further, pre-existing gaps in the same "deny more than strictly necessary" floor, none closed by this pass:

- **The kill syscall family is entirely unrestricted.** `kill`/`tkill`/`tgkill`/`rt_sigqueueinfo` are not in any deny list — a sandboxed plugin can signal its own parent process (`os.kill(os.getppid(), signal.SIGKILL)` or `SIGSTOP`) or any other process owned by the same user, regardless of declared capabilities. Denying `pidfd_send_signal` alone (round 2's own addition) achieves nothing against this, since the plain, older signaling syscalls remain fully available.
- **The child inherits the parent's entire environment.** `multiprocessing.get_context("spawn")` passes `os.environ` through unmodified; any secret the parent process holds in an environment variable is readable by every sandboxed plugin regardless of `capabilities.network`/`.filesystem` — reproduced directly (a synthetic env var was readable from inside a `network=False`/`filesystem=False` plugin call). A plugin could return it via its own result value, or exfiltrate it over the network if `network=True`. Not mentioned anywhere in ADR-0051.
- **Other allowed calls with cross-process or kernel-level reach**: `keyctl`/`add_key`/`request_key` (kernel keyring), `bpf`, `userfaultfd`, `perf_event_open`, `unshare`/`setns` (namespace manipulation) — none currently denied, none currently used by any reference plugin, but all are exactly the kind of syscall a deny-more-than-necessary floor should probably cover.

### Fix

Not started. (a) Add the kill family to `_PROCESS_ISOLATION_SYSCALLS`, ideally argument-filtered to the plugin's own process group rather than denied outright (a plugin legitimately signaling its own child subprocess, if it spawns one, shouldn't need `filesystem`/`network` capabilities to do so) — the parent's own PID is knowable when the filter is built, so an argument comparator on the target PID is feasible. (b) Scrub the child's environment before `exec`, passing through only an explicit allowlist the sandbox itself needs (or nothing, if the plugin protocol doesn't require any inherited env var — needs checking). (c) Add the remaining named syscalls to the floor once confirmed unused by any reference plugin and confirmed not to break `pytest`/CI (same caution KI-109 names). None started now — each needs its own verification pass this review round didn't have time for, and (a)/(b) in particular touch behavior this project can't fully verify without a Linux host, the same limitation KI-109 names.

---

## KI-112 — `assert_ref`/`propose_ref` never check that a relation's `target` entity is of the declared target concept ✓ RESOLVED (Backlog)

**Severity:** Bug — a write silently produces a reference to an entity of the wrong type (e.g. an `Invoice` as a `Person.employer`)
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §6 (relations declare a target concept)

### Description

KI-089 added `_require_existing_target`, which only checks that the `target` id names a real entity. `RelationDef.target_concept` was validated when the schema was built but never read on the write path, so a relation declared `Ref["Organization"]` accepted any existing entity — an extraction step that resolves a name to the wrong entity went through without an error and produced a reference no query over the declared type would ever expect.

### Fix

Added `SchemaIR.target_concept_of(predicate)` and `Ontology._require_target_concept(predicate, target)`, called from `assert_ref` and `propose_ref` right after the predicate-kind check, and again in `_replay_proposal_operations`' `assert_ref` branch so a proposal staged under an older schema version (or before this check existed) can't land a mismatched reference at accept time. Raises `ValidationError` naming the relation, the declared concept and the target's actual concept. A no-op when the namespace has no schema, matching every other schema-driven write check. Plugin `WriteView.propose_ref` goes through `Ontology.propose_ref`, so it is covered too. Existing mismatched rows are left untouched (assertions are append-only); a report of them is a possible follow-up. Declaring a relation target that several concepts satisfy (interfaces) remains out of scope.

Known residuals, deliberately not closed here: (a) the concept is compared by name, and `get_entity` is not namespace-scoped (the same gap KI-089 has), so a target from another namespace with an identically named concept passes; (b) a proposal that is now doomed because the schema moved the relation's target stays in `require_review` — `accept_proposal` raises and rolls back cleanly, but the proposal sits pending until a reviewer rejects it, and `resubmit`'s non-auto-accept branch does not pre-check it; (c) if a schema change since submission removed the relation or redeclared it as a property, the replay check is a no-op, the same narrow replay gap KI-031/KI-040 already document; (d) `CsvImporter` runs outside a transaction, so a mismatch partway through an import leaves the earlier rows written, as an unknown predicate already does.

---

## KI-113 — `resolve_contradiction` retracted members that corroborated the winner ✓ RESOLVED (Backlog)

**Severity:** Bug — resolving a contradiction discarded a second source that agreed with the winning value
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §10.1 (corroboration: keep both), §10.4 (resolution); ADR-0053

### Description

Contradiction membership is not pairwise: a differing value flags every overlapping active assertion on `(subject, predicate)`, including ones that agree with each other. `resolve_contradiction` retracted every member except the chosen winner, so with values `100` (A), `100` (B) and `120` (C), resolving for A's `100` retracted B although it agreed with the winner. §10.4's pseudocode described exactly that behavior, so the defect was in the SPEC text as well as the code.

### Fix

Resolution is now by winning value (ADR-0053): non-terminal members equal to the winner's `(value_kind, value)` stay or become `active` and get a `reactivated` event; non-terminal members with a different value are retracted. Retracted and superseded members stay terminal (ADR-0031). Equality is conflict routing's exact value comparison, plus `value_kind`. The party guard (KI-026) is unchanged and still checks every member. No signature change and no storage migration. Behavior change: queries after resolution can now return more than one active assertion for the same `(subject, predicate)`, as corroboration already does outside a contradiction. Resolutions made before this change are left as recorded; a reviewer who wants only one assertion kept can retract the others with `retract()`.

---

## KI-114 — Re-asserting the same fact wrote a new active assertion every time ✓ RESOLVED (Backlog)

**Severity:** Bug — repeated writes looked like fresh corroboration and grew the assertion table without new information
**Milestone target:** Backlog — resolved without a milestone change
**SPEC reference:** SPEC §10.1 (corroboration); ADR-0054

### Description

The same principal asserting `Item.name = "Bolt"` three times from the same source left three active assertions, on `static` and `time_varying` predicates alike. A scheduled re-import of one file therefore looked like new agreement each run, and every caller had to check for an existing assertion before writing.

### Fix

`_apply_with_conflict_routing` now looks for an identical `active` or `flagged` assertion first (`_find_identical_assertion`) and returns it without writing. Identical means equal on subject, predicate, `value_kind`, value, author, `acting_as`, source, `valid_from` and `valid_to`; an unspecified `valid_from` matches an existing one that was also defaulted. Confidence, rationale, model and `value_type` are not part of the key, so a repeat that differs only there is dropped, not applied. A write naming a `supersedes` hint is never treated as a repeat. A different source, author or window is still written as corroboration or new history; `retracted` and `superseded` rows never absorb a repeat. It covers `assert_literal`, `assert_ref`, `propose`/`propose_ref` and proposal replay. A proposal for an identical assertion is still accepted and keeps its own events; no assertion is created or linked to it. Behavior change: a repeat returns the earlier assertion, with its original id and `asserted_at`. Existing duplicate rows are left as they are (append-only).

---

## Format

Each entry follows this structure:

```
## KI-NNN — Short title

**Severity:** [Blocking | Performance | Architecture gap | Test gap | Informational]
**Milestone target:** [M1 | M2 | M3 | M4 | Backlog]
**SPEC reference:** [section]

### Description
What the gap is and why it matters.

### Fix
What needs to be done.
```
