# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Full documentation site (`mkdocs.yml` + `site_docs/`), deployed to GitHub
  Pages: Getting Started, Concepts, four tutorials (Governance & Review,
  Bitemporal Queries, Writing a Plugin, Hybrid Search), an Examples index,
  and an mkdocstrings-generated API reference covering the ADR-0019-pinned
  public surface.
- Four new runnable example scripts under `examples/`
  (`governance_and_review.py`, `bitemporal_queries.py`,
  `writing_a_plugin.py`, `hybrid_search.py`), one per tutorial. All five
  example scripts (including the pre-existing `quickstart.py`) now run in
  `ci.yml`'s "Tests" job on every push/PR to `main`.
- Docstrings on 13 previously-undocumented type aliases
  (`ontolith.schema.Text`/`Ref`/`Integer`/`Float`/`Boolean`/`Date`/
  `DateTime`/`URI`/`JSON`, `ontolith.identity.AdminAction`,
  `ontolith.govern.ProposalState`/`ContradictionState`/`ConflictResult`) so
  they render on the new API reference — found missing while building it,
  since mkdocstrings drops an undocumented `Literal`/`Annotated` alias
  silently.

### Fixed
- `pyproject.toml`'s `Documentation` URL pointed at an unreachable
  readthedocs.io page that was never deployed; now points at the new
  GitHub Pages site.
- `Ontology.assert_ref()` now accepts `rationale`, matching
  `assert_literal()`/`propose()`/`propose_ref()` — a real asymmetry
  where a direct reference assertion couldn't carry a rationale even
  though a direct literal assertion could. `POST /assertions`'
  `rationale` field is now actually forwarded on `target` (ref) writes
  too — it was already accepted in the request body but silently
  dropped for that branch.

## [1.0.0] - 2026-09-28

### M4 - Production (1.0)

All 8 of M4's named workstreams (performance budgets, SPEC §18 observability tier (a), plugin
process isolation, migration tooling, SemVer 1.0 API-surface freeze, `format_version` freeze,
security review, complete docs) plus a final exit-criteria review are complete — see
`docs/Ontolith_Implementation_Plan.md` §2's milestone table and its long-form status paragraph for
the full per-workstream record. The public API surface (ADR-0019, 13 packages), the on-disk
`format_version` (ADR-0052, frozen at `3`), and the SPEC §9 performance budgets (now CI-blocking) are
the three concrete things this tag freezes; `ci.yml`'s `griffe check` gate is blocking from this tag
onward (previously warn-only pre-1.0).

#### Fixed
- Plugin sandbox (ADR-0051): a `network=False`/`filesystem=False` plugin's own module import and
  `__init__` ran with no seccomp filter installed at all — found by M4 Workstream 7's security
  review, fixed and corrected across four review rounds. Round 1's own fix (install the full
  filter before loading) broke every `filesystem=False` plugin's own loading on Linux
  (`entry_points()`/module import need real filesystem reads regardless of what the plugin
  declares — reproduced as a genuine CI failure on `required-fields-validator`, the one shipped
  reference plugin with default, i.e. `filesystem=False`, capabilities); round 2 corrected it with
  a narrower pre-import filter (`apply_preimport_enforcement`) denying only network and the
  process-isolation floor before loading, leaving filesystem enforcement exactly where it always
  was (immediately before the plugin's own protocol method call) — closes the
  network-exfiltration-during-import vector against a *non-adversarial* failure to enforce.
  **Round 3 found and reproduced end to end that a plugin's own module-level code can defeat this
  adversarially**: since that code necessarily runs before the full filter is even attempted, it
  can reassign `enforcement.apply_capability_enforcement` itself (a plain module attribute) to
  fake a successful install, completely silencing `require_enforcement=True` with no exception and
  no warning — reproduced with a real installed entry point, no exception raised, a real file
  written despite `filesystem=False`. Round 3 shipped a fix — `_child_main` captures the real
  function as a local reference *before* calling `_load_plugin_instance`, so reassigning the
  module's own attribute afterward doesn't affect it — describing it as closing "the specific
  module-attribute variant" of the attack. **Round 4 reproduced directly that this doesn't
  meaningfully narrow the attack at all**: the captured function's own body still resolves every
  name it depends on (`_install_seccomp_filter`, `EnforcementResult`, the syscall lists) fresh from
  the same shared, mutable module namespace — reassigning `_install_seccomp_filter` defeats the
  captured reference exactly as completely as reassigning the function itself would have.
  Capturing an outer function object protects nothing about its inner dependencies. The capture is
  kept (free, harmless, closes the single most naive reproduction) but corrected everywhere it's
  documented (`runner.py`, `registry.py`, ADR-0051, KI-109) to say so honestly.
  `require_enforcement` remains documented as protection against a non-adversarial enforcement
  failure only, not a security boundary against a hostile plugin's own code — unchanged in
  substance by any of this, only the claim about what round 3 had narrowed was wrong. Also added
  `CTL_TSYNC` to every installed filter (a filter with no TSYNC only binds the thread that calls
  `load()`; a plugin starting its own thread before the filter installs could otherwise keep
  running unfiltered on it — round 4 found a TSYNC-install failure itself still silently reports
  `applied=True`, folded into **KI-110**) and moved `apply_preimport_enforcement`'s own outcome
  check to before `_load_plugin_instance` runs (it used to return `None` and go unchecked, meaning
  `require_enforcement` didn't catch a failure of even the narrower pre-import filter). Extended
  **KI-109** with the full escalation and round 4's correction.

  **Separately, round 3's own new regression test broke Linux CI a second time**: it called the
  real, captured `apply_capability_enforcement` directly inside the pytest worker process to prove
  the capture survives a later reassignment — on real Linux+libseccomp, that genuinely installs a
  restrictive seccomp filter, which can never be removed once loaded, permanently sandboxing the
  shared worker for the rest of the test session and corrupting every test that ran after it
  (`PermissionError: Operation not permitted` from pytest's own tmp-dir/capture machinery).
  `TestRealSeccompEnforcementOnLinux`, a few classes above in the same file, already documents this
  exact hazard and runs each of its own probes in a dedicated subprocess for exactly this reason —
  this new test bypassed that discipline. Fixed: the module attribute is now monkeypatched to a
  safe, side-effect-free fake before the test ever runs, so no real OS-level call happens on any
  platform; every other test calling `_child_main` directly was re-checked by hand for the same
  risk (round 4 found one more that was safe only by coincidence of control flow, not its own
  mocking, and added an explicit mock there too as a defense against the same regression).

  Separately, added `PluginRegistry.register(..., require_enforcement: bool = False)`: `True`
  refuses registration when OS-level enforcement can't even be attempted, and makes the isolated
  child itself refuse to invoke the plugin's protocol method if a specific call-time attempt to
  install the filter fails — round 1's own version of this check lived only in the parent process,
  which couldn't stop an already-spawned child from running the plugin regardless (reproduced: over
  3 seconds of fully unenforced execution before the parent's timeout-based cleanup caught up);
  round 2 moved the check into the child itself, verified this round by a behavioral test after a
  purely structural (AST line-order) test was shown to pass even with the fix's own `return`
  statement removed.

  The always-on process-isolation floor also gained `pidfd_open`/`pidfd_getfd`/`pidfd_send_signal`/
  `kcmp`/`process_madvise`/`process_mrelease` (`pidfd_getfd`'s own duplicate-fd attack requires the
  same permission check `ptrace` itself does, but wasn't denied), pinned by a new
  platform-independent regression test.

  A plugin's `manifest.name` is self-declared, not a unique identifier this project controls — a
  second, distinct entry point declaring the same name (and the same computed
  `effective_capability`) silently rebound to the existing principal, inheriting its `trust_level`
  and misattributing writes in the audit trail. Now records the entry point on the principal's own
  metadata and refuses a rebind when it differs — a bug fix closing a silent-rebind footgun, not a
  documented contract change; a principal created before this fix is grandfathered.

  `ci.yml` had no top-level `permissions:` block — added `contents: read` (least privilege; no job
  needs more, matching `security.yml`'s own existing scoping).

  Filed **KI-106** (`filesystem=True` subsumes the storage capability ceiling entirely), **KI-107**
  (no credential expiry), **KI-108** (migration audit trail / reversal), **KI-110**
  (`require_enforcement` can be satisfied by a partially-installed filter that silently skips an
  unresolved syscall name), **KI-111** (further pre-existing floor gaps: the kill syscall family
  entirely unrestricted, the child inheriting the parent's full environment including secrets,
  other unconsidered syscalls) — none fixed this pass.
- `tests/benchmarks/test_traversal.py`'s `propose` + policy eval + commit budget row (SPEC §9,
  Implementation Plan §9, p95 < 50 ms) had no valid benchmark. `test_bench_write_assert_literal`
  stood in for it, but repeated `assert_literal` calls on the *same* `(subject, predicate)` with a
  different value each round — no schema registered, so the predicate defaulted to static/single
  cardinality — meant the 2nd round already contradicted the 1st, and every round after extended
  the same growing open contradiction (`_apply_with_conflict_routing`'s "extend an already-open
  contradiction" fast path scans every existing member). Reproduced directly: the open
  contradiction's `member_ids` genuinely grew one entry per round, so the benchmark's reported p95
  was an artifact of however many rounds pytest-benchmark happened to calibrate for a given run
  (observed swinging from p95=2.9 ms at 84 rounds to a 56 ms *max* at 1061 rounds of the identical
  code), not a stable per-write cost. Fixed by giving every round its own `(subject, predicate)` —
  a dedicated `WriteBenchTarget`-concept entity pool the fixture now seeds specifically for write
  benchmarks, never the `Person` pool other benchmarks in the same module-scoped fixture assert
  exact counts against, so this fix can't corrupt e.g. `test_bench_assertions_by_subject`'s
  `== 100` regardless of pytest-benchmark's own round count.

#### Added
- Plugin process isolation (ADR-0051, closes KI-014's still-open half on Linux; **Breaking**) —
  `PluginRegistry.register()` gains `isolate: bool = True`: a plugin's one protocol entrypoint
  (`import_`/`export`/`derive`/`validate`/`sync`) now runs in a freshly spawned child process
  (`plugins/sandbox/`), with its `kb` view and any other live argument (e.g. an `io.StringIO`
  export target) proxied back to the parent over one IPC pipe — `LoadedPlugin.instance` is an
  `IsolatedPluginProxy`, not the real plugin object, so `isinstance(instance, SomePluginClass)` no
  longer holds (`instance.plugin_class` is the replacement check), and a `@dataclass`-shaped return
  value (e.g. a reference plugin's `ImportReport`) crosses as a plain `dict`, not its original type;
  `isolate=False` restores exactly the pre-ADR-0051 behavior for both. Closes ADR-0015's "Python has
  no true encapsulation" gap structurally, on every platform — a plugin's own code can no longer
  reach `view._kb` or any other live object graph, since only picklable messages cross the boundary
  at all, decoded by a restricted unpickler that only ever reconstructs this project's own exception
  hierarchy plus a short list of ordinary builtin exceptions as a class instance (everything else
  must arrive as `None`/`bool`/`int`/`float`/`str`/
  `bytes`/`list`/`dict`/`tuple`), and every proxied method call is checked against an explicit
  allow-list before dispatch. Two review rounds found the first version of this design didn't
  actually have those two properties — a plugin's own message to the parent was unpickled with plain
  `pickle.loads()` (reproduced: arbitrary code execution in the parent via a hostile `__reduce__`)
  and the dispatch loop invoked any method name the child asked for on the real, unproxied view with
  no allow-list (reproduced: `__setattr__("_principal_id", ...)` forged the acting principal on
  every later write through that view, from a read-only registration, no pickle trickery needed) —
  both fixed and independently re-verified by reproducing both attacks against the fixed code before
  merge. Five review rounds total; the second, third, and fourth each found a real regression in the
  prior round's own fix (most notably: a plugin's own documented `ValueError`/`TypeError` was briefly
  mislabelled as a protocol violation instead of propagating normally, round 3; round 4 then closed
  an unguarded send-side `BrokenPipeError` path round 3's own refactor had left open) — all fixed,
  all re-verified; a fifth round found no further code-level issues. See ADR-0051's own Update
  section for the full record. On Linux, with a working
  `pyseccomp`/libseccomp install (new Linux-only dependency, `sys_platform` marker, exact-pinned
  like `sqlite-vec`), a plugin's declared `capabilities.network=False`/`.filesystem=False` are now
  genuinely enforced at the OS syscall level (seccomp, `ERRNO(EPERM)` — a denied syscall surfaces as
  an ordinary caught exception, not a killed process; the deny-lists cover `io_uring`/`ptrace`/
  `process_vm_*` and non-native-architecture syscalls too, per the same review); macOS/Windows get no
  OS-level enforcement in this pass, honestly documented rather than approximated, and the
  registration-time warning now covers that case too (previously it only warned when a capability
  was declared `True`). `.query()`/`.as_of()` raise a clear `PluginError` from inside an isolated
  call — no shipped reference plugin needs either; filed as **KI-101**.
- SPEC §18 observability port, tier (a) — structured, correlated logs (ADR-0044) — new
  `core/observability.py`: `ObservabilitySink` (the ABC — `log`/`record_event`/`record_metric`,
  exactly the shape ADR-0044 already decided), `StdlibLoggingSink` (the production-safe default,
  mirrors `Clock`'s `SystemClock` — writes through Python's own `logging` module under an
  `ontolith.observability` logger, not a no-op, so nothing regresses to silence for a deployment
  that hasn't wired up an `observe/`-resident adapter sink, since none exist yet),
  `NullObservabilitySink` (explicit
  silence), and `RecordingObservabilitySink` (test double, mirrors `FixedClock`/`FixedIdProvider`).
  `Ontology` gains an `observability: ObservabilitySink | None = None` parameter (`__init__` and
  `connect()`), defaulting the same way `clock`/`id_provider` already do. `pyproject.toml`'s
  import-linter contract gains the `ontolith.observe` entry ADR-0044 named as still-needed — the
  contract now fences off that (currently empty) package the same way it already does
  `store.sqlite`/`store.duckdb`, ahead of any concrete adapter sink actually landing there. All
  four of ADR-0044's named ad hoc `logging.getLogger()` call sites (five emission sites) now log
  through `kb.observability` instead: `interfaces/rest.py`'s `_handle_ontolith_error`,
  `interfaces/graphql.py`'s `_OntolithSchema.process_errors` (both call sites — its `__init__` now
  takes the bound `Ontology` itself, since `process_errors` has no other way to reach it, and reads
  `kb.observability` live on each call, the same as the other three sites below),
  `interfaces/mcp.py`'s `_error_response` (moved from a module-level function to a closure inside
  `create_mcp_server`, the only scope with `kb` in it — its own 24 call sites are textually
  unchanged), and `plugins/registry.py`'s `_warn_if_unenforced_capabilities_requested`. Tiers (b)
  (the four named lifecycle events) and (c) (the seven-metric surface, its own follow-up ADR)
  remain not started — this closes only tier (a), ADR-0044's own stated first M4 priority.
- New `test_bench_propose_auto_accept` benchmarks the budget row's own actually-named operation —
  `propose()` + `ThresholdPolicy.evaluate()` + commit — a materially *different* path than
  `assert_literal`'s direct write (proposal construction/persistence and policy evaluation on top
  of the same commit), which had no benchmark of its own before this; measured cost is comparable
  to `assert_literal`'s, not dramatically higher — the extra overhead is real but modest. All five
  SPEC §9 budget rows now have a valid, stable benchmark and pass comfortably on their own dataset
  (see the Implementation Plan's §9 for the per-row breakdown and a caveat: the hybrid-query row's
  dataset is two orders of magnitude smaller than the other four's, not the same one throughout) —
  informational only, not yet a CI-blocking gate (per the Implementation Plan's own "informational →
  blocking by M4" note). Found while fixing this: the open-contradiction-extension code path the old
  `test_bench_write_assert_literal` used to accidentally exercise (see `#### Fixed` above) has a
  real, measured, unbounded-with-size per-write cost and no benchmark of its own at all now that
  this fix moved every write off it — filed as **KI-100**.
- On-disk storage-format migrations (SPEC §15, ADR-0052; **Breaking**) — new `format_version`
  single-row table, tracked independently per backend (`store/sqlite/migrations.py`,
  `store/duckdb/migrations.py`; both currently at `CURRENT_FORMAT_VERSION = 3`). Formalizes the two
  historical, previously ad hoc `PRAGMA table_info` + `ALTER TABLE` fixups
  (`principal_credential.issued_by`/`.revoked_by`, KI-060; `proposal.reviewers`, KI-078) as
  registered migrations (v2, v3), each declaring `reversible: bool` and a `down()`.
  `SQLiteBackend`/`DuckDBBackend` now **refuse** (`SchemaError`) to open an existing file below
  `CURRENT_FORMAT_VERSION` instead of silently upgrading it on connect — a fresh, empty file is
  unaffected (created directly at the current format). `migrate_file(path, *, dry_run=False)` is
  the explicit, standalone action that applies pending migrations (or, dry-run, only reports them,
  writing nothing at all) — new CLI commands `ontolith db status`/`ontolith db migrate [--dry-run]`
  (SQLite only, matching `Ontology.connect()`'s own scope; DuckDB's `migrate_file` is reached
  programmatically). Any existing database file below the current format_version now needs an
  explicit `ontolith db migrate` before it opens again. Data migration/backfill for a renamed or
  retyped *domain* predicate against already-stored assertion data (ADR-0034's own "scope (b)")
  remains a distinct, still-open problem this does not solve. Five review rounds — rounds 2 through 4
  each found a real bug in the same narrow area (a registered migration's `up()` needing to tolerate
  one more state of its own target table/column than the previous round anticipated: already applied,
  absent entirely, shadowed by a view); round 5 found none, backed by an exhaustive empirical sweep of
  every reachable table/column-state combination. See ADR-0052's own Update section for the full
  record. Filed **KI-104** as a maintainability follow-up (not a bug) for centralizing that
  defensiveness into a declarative registry once a third migration is added.
- Public API surface audit and 1.0 freeze declaration (Implementation Plan §14 open question #6,
  ADR-0019's Update section) — nine real exports added across four packages (found across two
  review rounds), closing gaps where a symbol was already public in spirit (declared in a module's
  own `__all__`, or directly needed to use a pinned method/attribute) but never reachable from the
  package it belongs to: `AsOfView` (from `ontolith`, alongside `Ontology` — the return type of
  `Ontology.as_of()`/`ReadOnlyView.as_of()`, previously importable only via `ontolith.ontology`),
  `AuthProvider`/`TokenAuthProvider`/`hash_token` (from `ontolith.identity` — the abstract port every
  `create_rest_app`/`create_graphql_app`/`create_mcp_server` factory takes, plus its one concrete
  implementation and the token-hashing helper it uses; `TokenAuthProvider`'s own re-export needs a
  specific import order in `identity/__init__.py` — round-1 review found the original "hard circular
  import, can't export it" justification was false, only order-dependent, and already protected by
  `ruff`'s own CI-blocking isort rule — see that file's own docstring), `VECTOR_SCOPES`/
  `DEFAULT_NAMESPACE` (from `ontolith.store` **and now also `store.base`'s own `__all__`**, alongside
  `StorageBackend` — both relevant to a third-party backend implementer), `ProposalState`/
  `ContradictionState`/`safe_rationale_history` (from `ontolith.govern`, round-1 review's own find —
  the first two are the `Literal` types annotating `Proposal.state`/`Contradiction.state`, both
  pinned attributes of pinned classes; the third is the defensive coercion every reader of
  `Contradiction.metadata`'s open `rationale_history` blob needs). `ontolith.interfaces.rest`/
  `ontolith.interfaces.graphql` are now pinned too (round-1 review found ADR-0019's own exclusion list
  had never addressed them) — same treatment as `ontolith.store.duckdb`: part of the tested surface,
  exempt from the "importable with no optional extra" guarantee, since both need their own extra.
  Declares the resulting, now-audited thirteen-package surface the 1.0 API-freeze candidate. New
  regression test (`test_pinned_packages_import_cleanly_without_any_optional_extra`, subprocess-
  isolated, its import list derived from the pinned-module set itself after round 1 found the first
  version hardcoded a separate, driftable list) catches a package eagerly importing an optional-extra
  dependency (`pyyaml`/`rdflib`/`duckdb`/etc.) — added after the audit's own first pass briefly
  reintroduced exactly that mistake (re-exporting
  `schema.linkml`'s `from_yaml`/`to_yaml`, verified broken, reverted before merge). See ADR-0019's
  own Update section for the full record.
- On-disk `format_version` declared frozen at `3` for the 1.0 baseline (M4 Workstream 6, ADR-0052's
  own Update section) — closes the Implementation Plan's separate "`format_version` frozen" M4 exit
  criterion (distinct from Workstream 4's own migration-tooling *mechanism*, the same relationship
  Workstream 5 above had to ADR-0019's earlier-shipped SemVer mechanism). Audited both backends'
  `CURRENT_FORMAT_VERSION` (still agree, still 3) and confirmed both registered migrations are
  genuinely `reversible=True` with a working `down()` before declaring; a future bump, pre- or
  post-1.0, now unconditionally requires a registered migration and a `**Breaking:**` CHANGELOG
  entry, mirroring ADR-0019's own discipline. New `tests/unit/test_storage_migrations.py::
  TestFormatVersionFrozen` pins the literal value on both backends (review found a real bump was
  already caught two other ways — an import-time dense/contiguous assert, and 18 pre-existing tests'
  own hardcoded version literals, 9 per backend — so this test's value is a dedicated assertion
  message pointing at the freeze, not a newly closed gap) plus the reversible-implies-has-a-`down()`
  invariant SPEC §15 requires, mutation-tested. Filed **KI-105**: the freeze pins the version number, not a snapshot of the
  actual on-disk shape (an edited `CREATE TABLE` with no matching migration goes undetected) or the
  `sqlite-vec` extension's own on-disk vector format (outside `format_version`'s scope entirely).

#### Documented
- M4 Workstream 8 (complete docs, PR #143, 2 review rounds, doc-only): fixed stale README.md content
  (dev-status banner still said "M3 in progress"; missing links to `Ontolith_UseCases_and_Interfaces.md`/
  `known-issues.md`; the Quick Start block never actually ran `examples/quickstart.py`) and CONTRIBUTING.md
  (3× literal `yourusername` URL placeholder; a false `structlog` dependency claim; `gitleaks`/CodeQL
  missing from the quality-gate table; a missing `uv run` prefix on the full-gate command; the Scopes
  list missing `conformance`/`adr`/`ci`, already used in the file's own Examples; the `m<N>/<description>`
  branch-naming convention missing entirely). Review round 1 found 2 more real issues the first pass
  missed: a dead GitHub Discussions link (Discussions is disabled on this repo — verified via the GitHub
  API) and SECURITY.md recommending "Use OIDC for authentication" and "Enable audit logging in
  production," neither a shippable option today (the only implemented `AuthProvider` is per-principal API
  keys, ADR-0014; observability tiers (b)/(c) were never built, only tier (a) structured logs).

#### Changed
- M4 exit-criteria review (2026-09-28, PR #144): the perf-regression Quality Gate's own
  "informational → blocking by M4" commitment, deferred since Workstream 1 and never picked up by a
  later workstream, is now fulfilled. Each of the 5 SPEC §9 budget rows carries a hard per-test p95
  ceiling (`tests/benchmarks/conftest.py::assert_within_budget`, computed from pytest-benchmark's raw
  per-round data via `statistics.quantiles`); the CI `benchmarks` job now runs on every PR instead of
  only on pushes to `main`. Deliberately an absolute per-row ceiling, not the Quality Gate's
  originally-sketched relative "no > 15% regression vs a stored baseline" — an honest trade-off, not
  a strictly superior choice: at today's margins a relative 15% check would catch a real regression
  far earlier than this loose an absolute ceiling ever would, deliberately left for later scope
  rather than adding a second new mechanism in the same review.
  **The PR's own review found a real flaw in the first version of this gate**: it checked
  `benchmark.stats.stats.max` (the single slowest observed round), reasoning `max < budget` is a
  stricter implication of "p95 < budget" and that pytest-benchmark "calibrates to a handful of
  rounds" for the slower rows — both claims checked against real CI data and found wrong. Round
  counts on the 5 gated rows range from roughly 270 to 8,000 on real CI runs, easily enough for a
  genuine percentile; and `max` proved *more* sensitive to CI-runner noise than p95, not less — a
  real CI run of `test_bench_propose_auto_accept` (budget 50 ms) recorded a `max` of 53.6 ms from one
  stalled SQLite-commit round, while that same run's real p95 was 0.62 ms. Fixed before merge by
  computing a genuine p95, re-verified against 6 downloaded real CI benchmark artifacts (including
  the one that had exceeded the `max`-based budget) — all 5 rows pass comfortably on every one.
  Also fixed: the CI `benchmarks` job's result-upload step now runs with `if: always()` (previously
  skipped on a failing run, losing exactly the data needed to diagnose it) and `overwrite: true`;
  guarded `assert_within_budget` against `benchmark.stats is None` (`--benchmark-disable` mode,
  unused in CI but reachable locally); and corrected leftover pre-existing mislabelings in
  `test_traversal.py`/`test_hybrid_query.py` — `test_bench_symbolic_query_concept_filter`,
  `test_bench_write_assert_literal`, and `test_bench_hybrid_semantic_query_intersected_with_where`
  each previously carried a "p95 target" docstring number implying it was a SPEC §9 budget row; none
  of the three is gated (the real rows are `test_bench_propose_auto_accept` and
  `test_bench_hybrid_semantic_query_k10`).

### M3 - Extensible (0.3)

#### Added
- REST, GraphQL, MCP, and CLI gain `supersedes` (closes KI-099, found reviewing/closing KI-080) —
  ADR-0050's hint was SDK-only when it shipped; REST's `WriteAssertionIn`/`ProposeIn` (`POST
  /assertions`/`POST /proposals`), GraphQL's `ProposeInput` (`Mutation.propose`), MCP's `propose`
  tool, and the CLI's `ontolith assert` (`--supersedes`) all now forward the parameter straight
  through to `Ontology`, which does all real validation — no new logic on any interface. GraphQL's
  camelCase needs no renaming (`supersedes` has no underscore); the CLI's `ontolith assert` never
  wired `assert_ref` and there is no `propose` subcommand at all, so this KI's scope stops at the
  one CLI command that exists — both pre-existing, separate gaps, not extended here.
- **Breaking:** `time_varying` properties/relations honor `cardinality="many"` (closes KI-080,
  ADR-0050) — ADR-0017 gave `cardinality="many"` `static` properties coexistence on a differing
  value but deliberately left `time_varying` unextended, reasoning that an overlapping differing
  value is always a genuine supersession there; true for `cardinality="single"`, but not for
  `"many"`, where window overlap plus a differing value can't tell "replace my current value" from
  "a new, additional concurrent value" apart (e.g. two concurrent job titles silently collapsing to
  one). `govern.conflict.route()`/`_route_time_varying()` now consult `cardinality`: a
  `many`-cardinality overlapping differing value coexists by default, mirroring `_route_static`'s
  own "many" branch — the behavior change from before this fix, which superseded unconditionally
  regardless of cardinality. `Ontology.assert_literal()`/`assert_ref()`/`propose()`/`propose_ref()`
  gain a new keyword-only `supersedes: str | None = None` parameter naming a specific existing
  assertion to replace instead, for the correction case — every other overlapping-differing
  concurrent value stays untouched. `supersedes` is rejected (`ValidationError`) outside
  `cardinality="many"`/`temporality="time_varying"`, and must name a real, active,
  overlapping-and-differing assertion on the same `(subject, predicate)` or the write is rejected —
  never silently ignored either way — including when no *other* existing assertion happens to
  overlap the incoming one, so a stale or typo'd hint can't vanish with no error. `cardinality="single"`
  `time_varying` properties are completely unaffected. REST/GraphQL/MCP/CLI parity deliberately out
  of scope, filed as its own follow-up (KI-099), matching KI-081's precedent of shipping an
  SDK-first capability separately from interface parity.
- CLI's `ontolith query` command gains `--semantic`, `--min-confidence`, `--trust-at-least`,
  `--limit`, `--include-flagged`, and `--include-history` (closes KI-096, found resolving KI-094)
  — previously supported only `--where`, a materially larger gap than the REST/GraphQL/MCP
  parity issue KI-094 closed. Each forwards to `QueryBuilder` exactly as the other three
  interfaces already do. `--as-of` deliberately not added: only MCP has bitemporal time-travel on
  `query` today, so adding it to the CLI would make it the *second* interface with time-travel,
  not parity with REST/GraphQL — out of this KI's scope.
- REST, GraphQL, and MCP `query` operations gain `include_flagged`/`include_history` parameters
  (closes KI-094, found reviewing KI-081) — the two `QueryBuilder` opt-ins KI-081 shipped were
  reachable only from the Python SDK; a REST/GraphQL/MCP caller had no way to opt in to
  flagged/history visibility, which matters most for MCP where the flagged-exclusion default is a
  safety property for agents (SPEC §14.4). `interfaces/rest.py`'s `QueryIn`, `interfaces/graphql.py`'s
  `Query.query` (camelCased to `includeFlagged`/`includeHistory` on the wire), and
  `interfaces/mcp.py`'s `ontolith.query` each forward the two booleans (default `False`) to the
  builder exactly as `min_confidence`/`trust_at_least`/`limit` already are. Only MCP's `query`
  supports `as_of` at all (REST/GraphQL never have) — corrected in KI-094's own entry, which
  previously claimed otherwise. The CLI's `ontolith query` command was left out — it doesn't
  forward `semantic`, `min_confidence`, `trust_at_least`, or `limit` either, a materially larger
  pre-existing gap filed separately as KI-096.
- **Breaking:** `entities_meeting_confidence()` / `entities_meeting_trust()` now honor
  `.include_flagged()` / `.include_history()` too (closes KI-093, ADR-0048 Update — found reviewing
  KI-081) — previously their current-state branch stayed hard-coded to `status = 'active'`, so a
  floor that should pass every scored assertion (`.min_confidence(0.0)` for anything with a
  confidence value; `.trust_at_least(0)` unconditionally) chained after `.include_history()`
  silently re-narrowed the result back to active-only and could empty it. Both port methods (+ both
  adapters) gained the identical `include_flagged`/`include_history` parameters
  `entities_where()` already had, widened the same way on both their current-state and `as_of`
  branches; `QueryBuilder` always passes both flags through, so a third-party `StorageBackend` still
  on the old signature raises `TypeError` on every `.min_confidence()`/`.trust_at_least()` call.
- **Breaking:** `QueryBuilder.include_flagged()` / `.include_history()` (closes KI-081, ADR-0048) —
  SPEC §11.2's two opt-ins, now on the fluent API. They widen which assertion statuses `.where()`
  (and, since KI-093, `.min_confidence()`/`.trust_at_least()`) match against: default `active`;
  `.include_flagged()` adds `flagged`; `.include_history()` adds `superseded` + `retracted`; the two
  are independent. Match-set wideners only — `.all()` still returns `list[Entity]`, no per-entity
  timeline (that stays `kb.assertions(status=None)` / `kb.provenance()`). No effect on a query with
  neither a `.where()` filter nor a confidence/trust floor; `.include_history()` is additionally a
  no-op under `.as_of()` for `superseded` (since ADR-0049/KI-095, not for `retracted` — see that
  entry below) (`.include_flagged()` is not a no-op under `.as_of()` at all). `StorageBackend.entities_where()` gains
  `include_history: bool`; each adapter's current-state branch's hard-coded `status = 'active'`
  becomes a parameter-bound `status IN (…)`. `_base_candidates()` always passes both flags through,
  so a third-party `StorageBackend` still on the old signature raises `TypeError` on any `.where()`
  call.
- `Ontology.provenance(assertion_id) -> Provenance` (closes KI-086, ADR-0047) — the single
  domain-layer implementation of SPEC §5.4's one-call provenance view. REST (`GET
  /provenance/{id}`), GraphQL (`Query.provenance`), and MCP (`ontolith.provenance`) each stopped
  re-deriving the `get_assertion` + `get_proposal_events` + `get_assertion_events_by_successor`
  assembly (three copies of one concept — the parity-gap pattern KI-058/059/075/076/077/079 kept
  re-finding) and now shape one `kb.provenance()` result into their own response DTO; wire
  behavior is unchanged. New frozen `Provenance` value object (`ontolith.govern`) carrying
  `assertion` / `review_events` / `superseded_ids`. ADR-0047 also settles two SPEC §14.1
  sketch-vs-shipped drifts KI-086 named: `Entity` stays a pure value object (no `Entity.history()`
  etc. — the equivalents are `Ontology.assertions(status=None)` / `provenance()` /
  `contradictions()`), and the `create_principal()`/`get_principal()` split is intentional (no
  `kb.principal(...)` alias). SPEC §14.1 gained a note flagging these as ADR-recorded deviations from its stated shape.
- `RequireReviewForAI` (closes KI-088, found in the pre-M4 deep + security audit) — the
  `Composite(all=[RequireReviewForAI(), SourceQuorum(2)])` pattern `Composite`'s own docstring has
  sketched inline since ADR-0040 is now a real, exported, tested `PolicyStrategy`, not something
  every deployer re-derives from a comment. Routes AI-kind principals to review (owner as sole
  reviewer, read from `principal` only — never `acting_as`, mirroring `ThresholdPolicy`'s own
  "never laundered via delegation" precedent) and defers everyone else. Enforces the KI-015
  capability floor itself (checked before the AI-kind test, unlike `ThresholdPolicy`'s AI-first
  ordering), matching every sibling strategy in this module — the inline sketch never had this, so
  it would previously `AutoAccept` a read-only non-AI principal if used standalone rather than
  always composed with a floor-enforcing partner. **This is a genuine reversal** of ADR-0040's own
  explicit decision not to ship this class (its Alternatives Considered rejected exactly this) —
  recorded honestly in ADR-0040's own new update rather than left to silently drift, not framed as
  something narrower than it is. `Composite`'s docstring simplified to reference the real class
  instead of re-sketching it.
- Entity creation on REST, GraphQL, and MCP (closes KI-082, found in the pre-M4 deep + security
  audit) — previously CLI/SDK-only, so an agent or application talking only to REST/GraphQL/MCP
  could assert facts about existing entities but never introduce a genuinely new one. `POST
  /entities` (REST), `Mutation.createEntity` (GraphQL), `ontolith.create_entity` (MCP) — all three
  thin wrappers over `Ontology.create_entity()`, propose-tier (rejects only `read`-only
  principals; no AI-kind block, since an entity carries no fact/confidence/temporality for policy
  to evaluate, unlike `assert_literal`/`assert_ref`'s direct-write path). Each interface's own
  exact-coverage tests (GraphQL's mutation-field-probe set, MCP's KI-085 tool-count test) required
  a conscious update, confirming those safety nets work as designed. Genuinely widens two
  documented scope boundaries rather than adding a route within them — ADR-0008 (MCP's closed
  tool list) and ADR-0037 §1 (GraphQL's "query, propose, review only" scope) both updated to
  record it and why. Surfaced two pre-existing SDK gaps now agent-reachable for the first time,
  filed rather than fixed here: `create_entity()` doesn't validate `concept` against the schema
  (KI-090), and a duplicate `natural_key` surfaces as a redacted `StorageError` (KI-091).
- GraphQL and CLI parity for `Proposal.reviewers`/`assign_reviewers()` (closes KI-079, ADR-0046
  Update — see the KI-078 entry below for the feature this closes the interface gap on):
  `ProposalType` gains `reviewers: list[str]`; new `Mutation.assignReviewers(proposalId,
  reviewers)` (GraphQL's ninth mutation, structurally identical to `requestChanges`/
  `rejectProposal`). CLI gains `proposal assign <proposal_id> --actor <id> [--reviewer <id> ...]
  [--clear]` — `--reviewer`/`--clear` are mutually exclusive-by-requirement, since clearing is
  irreversible and the CLI has no other natural "the caller meant to clear everything" signal the
  way REST/GraphQL's explicit empty-list argument does; `proposal list` gains a
  `reviewers=<comma-joined>` suffix when non-empty (mirrors KI-075's `rationale_entries=<n>`
  convention). MCP remains deliberately excluded — `assign_reviewers()` would be its first
  review-capability write tool, left for a real consumer to motivate.
- `Proposal.reviewers: list[str]` and `Ontology.assign_reviewers(proposal_id, reviewers, actor)`,
  implementing SPEC §9.4's `assign` review action (closes KI-078, ADR-0046 — GraphQL/CLI parity
  closed separately above, KI-079). A `PolicyStrategy`'s `RequireReview.reviewers` was computed by
  every strategy but never persisted or surfaced anywhere — now populated on `Proposal` at creation
  time and refreshed on `resubmit`'s re-evaluation (KI-027), not cleared on accept/reject/
  request_changes. `assign_reviewers()` replaces the reviewer list wholesale, records a
  `ProposalEvent(type="assign")`, and reuses the exact eligibility checks accept/reject/
  request_changes already share (review/admin capability, non-AI, no self-review — kept for
  consistency with the sibling actions, though not currently load-bearing since `reviewers` itself
  isn't enforced at accept time; see ADR-0046). Exposed via REST only at first: `POST
  /proposals/{proposal_id}/assign`, and `reviewers` added to `ProposalOut` on every proposal
  route. **Breaking:** `StorageBackend` gains a required `update_proposal_reviewers()` method; both
  backends migrate existing database files to add the new column — DuckDB's `ALTER TABLE ADD
  COLUMN` rejects any constraint (`NOT NULL`, `UNIQUE`, `CHECK` all fail identically), but a plain
  `DEFAULT` isn't itself a constraint, so its migrated column uses `DEFAULT '[]'` instead, which
  DuckDB backfills into existing rows automatically, unlike a fresh database's stronger `NOT NULL
  DEFAULT '[]'`. A manual `assign_reviewers` call also doesn't survive a later `resubmit` that
  lands back in `require_review` (that branch's policy re-evaluation overwrites `reviewers`) — a
  `resubmit` that instead auto-accepts or gets rejected leaves a manual assignment untouched.
  Documented and pinned by tests covering all three resubmit outcomes.
- Three new `PolicyStrategy` implementations closing out SPEC §9.2's built-in strategy list (closes
  KI-069, ADR-0045): `ConfidenceThreshold(threshold, reviewers=None)` auto-accepts once a
  proposal's own staged confidence meets `threshold` (a missing confidence always requires review,
  never assumed as 0 or 1); `SourceRequired(reviewers=None)` auto-accepts only when the operation
  carries a non-empty `source` (no `kb` read — a narrower, unconditional cousin of `SourceQuorum`,
  composable with it via `Composite` for "sourced AND quorum'd"); `RequireReviewByRole(
  role_reviewers, *, default=None)` never auto-accepts — it always routes to review, choosing
  reviewers by `principal.metadata.get("role")` (no dedicated `Principal.role` field exists;
  `metadata` is the existing documented extension point), read from the real author, never
  `acting_as`, so delegation can't be used to dodge a role's reviewers. All three enforce the
  KI-015 capability floor themselves, mirroring `SourceQuorum`, and are exported from
  `ontolith.govern`. SPEC §9.2's six-strategy SHOULD-list is now fully built (`ThresholdPolicy`
  continues to cover roughly what `TrustLevel` would; no class of that name exists separately).
  Also fixed along the way: `RequireReview.__init__` now copies its `reviewers` argument instead
  of aliasing it — a caller mutating a returned decision's `.reviewers` previously rewrote the
  issuing strategy's own configuration for good, a latent bug in every pre-existing strategy too.
  Filed KI-078 during review: nothing in the system persists or surfaces a `RequireReview`'s
  `reviewers` anywhere yet — pre-existing, but `RequireReviewByRole`'s entire purpose being
  reviewer routing makes it far more consequential now.
- New MCP tool `ontolith.list_contradictions` (closes KI-076): `read`-tier, mirroring REST's
  `GET /contradictions`/GraphQL's `Query.contradictions` — `ontolith.flag_contradiction`
  (propose-tier, mutates) was previously the only MCP surface that returned a contradiction at
  all, so reading one back (including its `rationale_history`, KI-075) required a write. `state:
  str | None = "open"` accepts `"open"`/`"resolved"`, `"all"` or `None` for every state (both work
  identically — `"all"` kept for consistency with REST/GraphQL's own sentinel, even though MCP's
  JSON `null` doesn't share the HTTP-query-string ambiguity that sentinel exists to work around),
  or a `validation_error` for anything else (review finding: an unrecognized value previously
  matched zero rows silently, indistinguishable from "no contradictions exist"). Returns
  `rationale_history` via the same `govern.contradiction.safe_rationale_history()` helper
  `ontolith.flag_contradiction`'s response was also switched to in this fix (review finding: the
  two tools previously guaranteed different shapes for the same field on the same contradiction —
  a malformed `metadata` blob degraded to `[]` on one and passed through raw, unprojected garbage
  on the other). MCP now has 9 tools (was 8 as of KI-067).
- Read surface for a contradiction's accumulated `rationale_history` (closes KI-075): REST's
  `ContradictionOut` gains a `metadata: dict[str, Any]` field (all three routes that return one —
  `GET /contradictions`, `POST /contradictions/flag`, `POST /contradictions/{id}/resolve`).
  GraphQL's `ContradictionType` gains `rationaleHistory: [RationaleEntryType!]!` instead — GraphQL
  has no native map scalar, so the trail is projected into a structured `{rationale, actor, at}`
  type rather than exposed as an opaque blob (same reason `FilterInput` already exists as an
  explicit key/value list). MCP's `ontolith.flag_contradiction` response gains a
  `rationale_history` key carrying the *full* trail, not just the value passed to that call —
  still the only MCP surface that returns a contradiction at all (KI-076, filed during review: no
  read-only `list_contradictions`-shaped tool exists). CLI's `contradiction list` output gains a
  `rationale_entries=<n>` suffix (omitted when a contradiction has no history) plus a
  `--show-rationale` flag that prints every entry's full text (mirroring `flag`'s own format —
  added during review, since a bare count with no way to read the text on a pure read path
  defeated the point; named distinctly from `flag`'s own `--rationale <text>` option to avoid a
  same-flag-different-meaning trap between the two commands), and `contradiction flag` echoes
  every accumulated entry on its own line after the summary. Mirrors KI-072's own shape: the data
  was captured (KI-071) but unreachable through any interface but the raw SDK. `contradiction
  resolve` (CLI) has no equivalent flag — `resolve_contradiction()` takes no `rationale` input,
  and a resolved contradiction's trail is reachable via `contradiction list --state resolved
  --show-rationale`. New `govern.contradiction.safe_rationale_history()` — used by GraphQL's and
  the CLI's entry-rendering instead of per-field `.get(key, default)` (review finding: `.get()`
  alone still raised on a non-dict entry or a non-list `rationale_history`, and silently passed a
  present-but-`None` field value through instead of defaulting it, since the key check `.get()`
  performs doesn't cover either case) — `metadata`/`rationale_history` is an open, schema-less
  blob (ADR-0041), and a malformed or legacy-shape entry previously raised an uncaught exception —
  on GraphQL, one that failed the entire `contradictions` query, not just the one bad
  contradiction.
- Read surface for the admin-action audit trail (closes KI-072, ADR-0042 update): new
  `Ontology.get_admin_events(author, *, actor=None, target=None)`, admin-gated the same way
  `list_tokens`/`list_principals` already are. REST gets `GET /admin-events` (`actor`/`target`
  query filters, new `AdminEventOut` model) and `CredentialOut` gains `issued_by`/`revoked_by`. CLI
  gets `ontolith admin-event list [--actor] [--target] --author <id>`, and `principal
  list-tokens`'s output now shows who issued/revoked each credential. GraphQL/MCP left for their
  own future scope — MCP specifically, since admin-gating this would make it the first MCP tool
  requiring `admin` capability rather than `read`/`propose`.
- MCP's `ontolith.query` tool gains `semantic`/`as_of`/`min_confidence`/`trust_at_least`/`limit`
  parameters (closes KI-058, ADR-0043), mirroring REST's `POST /query`/GraphQL's `Query.query`
  wiring into `QueryBuilder` for the first three and `limit`; `as_of` is new even to REST/GraphQL,
  making MCP the first of the four shipped interfaces to expose bitemporal time-travel (SPEC
  §11.4) through any route, per SPEC §14.4's own normative tool table naming it for this tool
  specifically. Also removed the tool's pre-existing `namespace` parameter, a silent no-op
  (`Ontology.query()` never accepted a namespace argument; namespace is hardcoded, the same M1
  limitation REST/GraphQL already work around by never exposing the field) — schema-visible, not
  runtime-breaking: FastMCP drops unrecognized tool arguments rather than rejecting the call, so a
  caller still passing `namespace=` keeps succeeding exactly as it silently did before.
- Admin-action audit trail (closes KI-060, ADR-0042): `PrincipalCredential` gains `issued_by`/
  `revoked_by` columns (both backends, migrated in place for existing database files), populated
  from `issue_token`/`revoke_token`'s already-required `author` parameter. New `AdminEvent`
  (`identity/admin_event.py`) and `admin_event` table record `create_principal`/`apply_schema`/
  `PluginRegistry.register` — reuses the exact SQLite-trigger immutability mechanism KI-066/
  ADR-0041 built for `assertion_event`/`proposal_event` (DuckDB has the same documented gap).
  `Ontology.create_principal` gained an optional `author` parameter, used only to attribute the
  resulting event — not a new capability gate; `ADR-0022`'s "no built-in check" decision is
  unchanged. REST's `POST /principals` and the CLI's `principal create` both pass through the
  admin id they already validate. **Breaking:** `StorageBackend.revoke_credential()` gained a
  required `revoked_by: str` parameter — any external `StorageBackend` implementation must update
  its signature. Also fixed along the way: re-revoking an already-revoked credential was silently
  overwriting `revoked_by` on a second call (attribution laundering) — now a true no-op.
  **Behavior change:** `apply_schema`/`create_principal` now open their own transaction (to keep
  the governed write and its `AdminEvent` atomic), so calling either from inside a caller's own
  `with kb.backend.transaction():` block now raises `StorageError` instead of composing — matches
  the constraint 11 other `Ontology` write methods already had.
- CLI `ontolith contradiction flag <id_a> <id_b> --author <id> [--rationale <text>]` and
  `ontolith contradiction resolve <id> --winner <assertion_id> --reviewer <id>` (closes KI-063) —
  the CLI was the only one of the four shipped interfaces with no contradiction write surface at
  all (REST/GraphQL/MCP all had at least `flag`; REST/GraphQL also had `resolve` — MCP
  deliberately doesn't, per ADR-0008/KI-009's reviewer-only scoping). Both are thin
  wrappers around `Ontology.flag_contradiction()`/`resolve_contradiction()`, mirroring existing
  CLI conventions (`flag`'s `--author` matches `assert`/`retract`; `resolve`'s `--reviewer`/
  `--author` alias matches `proposal accept`).
- `Composite(all=…, any=…)` policy strategy (`govern/policy.py`, SPEC §9.2, closes KI-061,
  ADR-0040) — the last of SPEC §9.2's six named strategies still missing. Combines multiple
  `PolicyStrategy` instances by decision severity (`Reject` > `RequireReview` > `AutoAccept`):
  every strategy in `all` must independently `AutoAccept` for the group to approve (the most
  restrictive decision wins); at least one strategy in `any` must (the least restrictive wins);
  same-severity decisions at the winning level are merged — `RequireReview.reviewers` as a
  dedup'd union, every `Decision.reason` concatenated — rather than one being silently discarded.
  Closes a real configuration gap: `SourceQuorum` deliberately does not special-case AI-authored
  proposals the way the default `ThresholdPolicy` does (ADR-0025 §5, unchanged by this), so a
  deployment on `SourceQuorum` alone silently drops ADR-0003's "AI principals always require
  review" guarantee — `Composite` is SPEC §9.2's sanctioned way to layer that rule back on top,
  and until now it couldn't actually be built. No new "AI-always-reviews" strategy shipped
  alongside it at the time (`ThresholdPolicy` can't be reused for this without re-imposing its own
  capability gate); the pattern was documented as an inline example in `Composite`'s own docstring
  — since promoted to a real, shipped class, `RequireReviewForAI` (KI-088, below).
  MCP's `ontolith.propose`/`ontolith.resubmit` tool docstrings, which previously stated the
  AI-review guarantee unconditionally, now correctly attribute it to the *default*
  `ThresholdPolicy` specifically.
- RDF/OWL bridge, export only (SPEC §13.3, ADR-0036): `schema.rdf.to_owl(schema)`
  translates a `SchemaIR` into an OWL ontology (`rdflib.Graph`) — concepts become
  `owl:Class`, properties become `owl:DatatypeProperty` (XSD-typed range), relations
  become `owl:ObjectProperty` (`owl:inverseOf` if declared). `cardinality="single"` is
  additionally typed `owl:FunctionalProperty` only when `temporality="static"` too — a
  `time_varying` predicate can hold multiple simultaneously-active assertions with
  non-overlapping validity windows (SPEC §10.2) even at `single` cardinality, and
  declaring it functional unconditionally would assert a real OWL inconsistency for
  that valid state. New `RdfExporter` reference plugin
  (`plugins.reference.rdf_exporter`, entry point `rdf-owl-exporter`) adds active
  assertions as RDF instance data on top — one `rdf:type` triple per distinct entity
  seen, one property triple per active assertion — and serializes the combined graph
  (Turtle by default; any `rdflib` format). Every property/relation IRI either module
  references is declared its own `owl:DatatypeProperty`/`owl:ObjectProperty` type,
  including an `owl:inverseOf` target the schema doesn't otherwise mention and a
  predicate a later schema version removed but an already-active assertion still uses
  (OWL 2 DL requires a declaration for every property IRI in use). New `rdflib`
  dependency in the `interop` extra, not the real `linkml`/`linkml-runtime` packages
  ADR-0013 already rejected for the adjacent YAML bridge. Deterministic,
  percent-encoded `urn:ontolith:{namespace}:...` IRI scheme (percent-encoding closes a
  real serialization crash on realistic LinkML-imported schema names — spaces, URL
  namespaces, non-ASCII — found in review), no dependency on a schema's LinkML-sourced
  `default_prefix`/`prefixes` metadata. `from_owl` (import direction) is explicitly out
  of scope for v1, and so is any representation of valid time, confidence, or
  provenance — every currently-active assertion becomes exactly one triple with none of
  that context, unlike `JsonExporter`. `Ontology` and `ReadOnlyView` both gain a new
  `schema()` method — the first reference plugin needing schema access, not just
  entity/assertion data.
- GraphQL interface, closing M3's last unstarted scope item (SPEC §14.3,
  ADR-0037): `create_graphql_app(kb, auth_provider)` (`interfaces/graphql.py`)
  serves a `strawberry`-backed schema at `/graphql` exposing `Entity`, `Assertion`,
  `Proposal`, `Contradiction`, and `Principal` types with `query`, `propose`, and
  `review` operations — SPEC's literal wording, deliberately narrower than REST's
  own extended write/admin surface (ADR-0022): no direct-write mutation, no
  principal creation or token issuance. `Query`: `schema`, `entity` (with a
  lazily-resolved nested `assertions` field), `query`, `provenance`, `proposals`,
  `contradictions`, `principals` (admin-gated). `Mutation`: `propose`,
  `acceptProposal`, `rejectProposal`, `requestChanges`, `resubmitProposal`,
  `flagContradiction`, `resolveContradiction`. Reuses ADR-0014 bearer-token auth,
  resolved once per request into GraphQL context (never raised there, so
  introspection stays reachable unauthenticated like REST's `/docs`) and checked
  per-resolver. A custom `strawberry.Schema.process_errors` override centralizes
  `OntolithError` → `extensions={code, detail}` mapping — the GraphQL analog of
  REST's single `OntolithError` exception handler — redacting `StorageError`/
  `PluginError` messages the same way REST does; any other resolver exception
  (not a domain error) is redacted identically with a new `extensions.code =
  "INTERNAL_ERROR"`, matching REST's generic, code-less 500 for the same
  failure class rather than leaking the raw message. `create_graphql_app`
  also gained `introspection` (default `True`; set `False` to disable
  `__schema`/`__type` independent of the `graphql_ide` toggle) and
  `docs_url`/`redoc_url`/`openapi_url` passthrough, matching
  `create_rest_app`'s existing parameters. `graphql` extra widened to
  `strawberry-graphql[fastapi]` plus `uvicorn` so it's installable standalone,
  without also needing `[rest]`.
- Class-based schema DSL compiler and `Ontology.apply_schema` (SPEC §6.2)
- LinkML-aligned YAML schema front-end: `to_yaml`/`from_yaml`, a deliberately-scoped
  dialect subset documented in ADR-0013, with schema-level `default_range` support and
  a fail-loud policy on unsupported LinkML constructs (`abstract`, `identifier`, `key`,
  `alias`, `ifabsent`, `readonly`, `recommended`, and others) rather than silently
  dropping them
- `Ontology.proposals(state=)`/`contradictions(state=)` — reviewer-queue listing, backed
  by new `StorageBackend.proposals()`/`contradictions()` port methods; CLI commands
  `ontolith proposal list`/`ontolith contradiction list` (`--state`, `--all`)
- Hybrid retrieval (SPEC §11.3/§12.3/§14, ADR-0020, closes KI-018): `Embedder` port
  (`ontolith.core.embedder`) with a dependency-free default (`HashingEmbedder`) and a
  deterministic test double (`LookupEmbedder`); `StorageBackend.vector_upsert`/
  `vector_search` on both SQLite (via `sqlite-vec`, now a required dependency) and DuckDB
  (via native `list_distance`); `QueryBuilder.semantic(text)`, `.min_confidence(t)`,
  `.trust_at_least(l)`, `.limit(n)`; `Ontology.reindex(concept=)` to explicitly (re-)embed
  entities into the vector index; CLI `ontolith reindex [--concept]`
- REST interface, read + propose slice (SPEC §14.3, ADR-0021, partially closes KI-022):
  `create_rest_app(kb, auth_provider)` (`interfaces/rest.py`) exposing `GET /schema`,
  `GET /entities/{id}`, `POST /query`, `GET /provenance/{id}`, `POST /proposals`,
  `GET /proposals` — every route, reads included, requires an ADR-0014 bearer token (at
  the time, a deliberate divergence from MCP's then-unauthenticated read tools, tracked
  as KI-021 and since resolved — see below). One `OntolithError` → HTTP status handler
  (SPEC §16) replaces per-route error handling.
- REST interface, write/review/admin slice (SPEC §14.3, ADR-0022, further closes
  KI-022): `POST /assertions` (direct write via `assert_literal`/`assert_ref`),
  `POST /proposals/{id}/accept|reject`, `GET /contradictions`,
  `POST /contradictions/flag`, `POST /contradictions/{id}/resolve`, `POST /principals`,
  and `/principals/{id}/tokens` (`POST` issue, `GET` list, `DELETE` revoke) — reusing
  ADR-0021's auth and error-mapping unchanged. Eight of ten routes need no new
  capability-check code (the wrapped `Ontology` methods already gate themselves); the
  ninth, `GET /contradictions`, needs none either but only because it's a read;
  `POST /principals` calls `Ontology.require_admin()` explicitly, since
  `create_principal` has no built-in gate of its own. `GET /principals` (list),
  `GET /namespaces`, and `/proposals/{id}/review` remain deferred — no backing SDK
  method exists for any of the three (confirmed by an explicit audit, not an oversight).
  `DELETE /principals/{id}/tokens/{credential_id}` verifies the credential actually
  belongs to `principal_id` before revoking, raising `NotFoundError` on mismatch.
- `GET /principals` (SPEC §14.3, ADR-0022 update, further closes KI-022): new
  `Ontology.list_principals(author)`, gated the same way as `issue_token`/
  `revoke_token`/`list_tokens` (`require_admin`); REST route requires admin
  capability; CLI `ontolith principal list`. No pagination, matching `list_tokens`'s
  existing precedent.
- **Breaking:** `StorageBackend` gained a new required Protocol method,
  `list_principals() -> list[Principal]` (ADR-0022 update, closes KI-022's
  `GET /principals` gap) — any third-party `StorageBackend` implementation must add
  it.
- Full predecessor recovery for multi-target supersession (SPEC §10.2, ADR-0023, closes
  KI-008): `AssertionEvent.successor_id: str | None` records which successor caused a
  `"superseded"` event. `Assertion.supersedes` itself is unchanged (still scalar, still
  names only the first predecessor per SPEC §12.2's normative schema) — the full set of
  predecessors superseded by one incoming assertion is now recoverable via
  `{e.assertion_id for e in kb.backend.get_assertion_events_by_successor(successor_id)}`,
  surfaced as `ProvenanceOut.superseded_ids` (REST `GET /provenance/{id}`) and a matching
  `superseded_ids` key on the MCP `ontolith.provenance` tool.
- **Breaking:** `StorageBackend` gained a new required Protocol method,
  `get_assertion_events_by_successor(successor_id) -> list[AssertionEvent]` (ADR-0023,
  closes KI-008) — any third-party `StorageBackend` implementation must add it.
- **Breaking:** `StorageBackend` gained a new required Protocol method,
  `get_schema_at(namespace, at) -> SchemaIR | None` (SPEC §11.4, ADR-0024, closes
  KI-019) — any third-party `StorageBackend` implementation must add it. New
  `AsOfView.schema()` resolves the schema version effective at that view's `as_of`
  time (via `applied_at`, already recorded deterministically by every `put_schema`
  call) rather than always the latest version — `kb.as_of(t).schema()` now correctly
  differs across a schema migration boundary. `SchemaIR` and `put_schema` are
  unchanged.
- `SourceQuorum` policy strategy (SPEC §9.2, ADR-0025, closes KI-017):
  `PolicyStrategy.evaluate()` now sees KB state via a new `kb` parameter, pinned to a
  bitemporal `AsOfView` snapshot at the proposal's creation time. `SourceQuorum(threshold,
  reviewers=)` auto-accepts once `threshold` distinct sources corroborate the same
  `(subject, predicate, value)`, counting the proposal's own source together with
  matching, sourced, `kb`-visible assertions; retractions and sourceless proposals
  always require review; principals below `propose` capability are rejected (KI-015
  update). Does **not** reimplement `ThresholdPolicy`'s "AI proposals always require
  review" rule (ADR-0003) — an AI-authored proposal auto-accepts under `SourceQuorum`
  once quorum is reached; combining that guarantee with source-quorum is `Composite`'s
  job (still unbuilt).
- **Breaking:** `PolicyStrategy.evaluate()` gained a required `kb: KbView` parameter
  (SPEC §9.2, ADR-0025, closes KI-017), inserted between `principal` and `acting_as` —
  any third-party `PolicyStrategy` implementation must add it. `KbView` (new,
  `ontolith.govern.policy`) is a minimal structural Protocol (`assertions(subject=,
  predicate=) -> list[Assertion]`), not SPEC's literal `ReadOnlyView` — see ADR-0025 for
  why. `ThresholdPolicy` is unaffected at call sites: its own concrete signature keeps
  `kb` optional (unused), so existing callers that don't pass one are unchanged.
- `/proposals/{id}/review` (SPEC §9.1/§9.4, ADR-0022 update, further closes KI-022):
  new `Ontology.request_changes(proposal_id, reviewer, reason="")` — the third
  `under_review` outcome (`changes_requested`) alongside `accept_proposal`/
  `reject_proposal`, gated identically (review/admin capability, no AI reviewer, no
  self-review — see ADR-0022 for why this one keeps the strict gate too). Same
  reviewer-eligibility/proposal-state checks as its siblings, now factored into a
  shared `Ontology._require_reviewer` helper (pure refactor, no behavior change).
  `ProposalEvent.type` widened to admit `"request_changes"` — third-party
  `StorageBackend`/consumer code that exhaustively matches on `.type` needs updating.
  `POST /proposals/{proposal_id}/review` (REST) mirrors `/reject`'s shape exactly.
- **Note:** both backends' `proposal_event.type` `CHECK` constraint was removed
  (previously `CHECK(type IN ('accept', 'reject'))`) rather than widened again —
  `CREATE TABLE IF NOT EXISTS` never updates an existing table's constraint, so
  widening it a second time would silently break `request_changes()` on any database
  file created before this release. Databases created before this change need to be
  recreated; there is no DDL migration mechanism yet (pre-1.0/pre-alpha).
- `GET /namespaces` (SPEC §5/§12.2, ADR-0022 update, closes KI-022): new `Namespace`
  model (`ontolith.core.namespace`), `Ontology.list_namespaces()` (ungated, like
  `proposals()`/`contradictions()`), `GET /namespaces` (REST), and
  `ontolith namespace list` (CLI). Backed by SPEC §12.2's own normative `namespace`
  registry table (`id`, `created_at`, `metadata`) on both backends, seeded
  idempotently with the one namespace this project operates in today
  (`DEFAULT_NAMESPACE = "default"`) — this project remains single-namespace
  throughout (ADR-0015); no namespace-creation path was added. KI-022 is now fully
  resolved.
- **Breaking:** `StorageBackend` gained a new required Protocol method,
  `list_namespaces() -> list[Namespace]` (SPEC §12.2, ADR-0022 update, closes
  KI-022) — any third-party `StorageBackend` implementation must add it.
- `Ontology.resubmit(proposal_id, author)` (SPEC §9.1, ADR-0022 update, closes KI-027):
  the missing `changes_requested → submitted → {policy}` transition —
  `request_changes()` previously left a proposal permanently stuck once changed,
  with no way back into the review pipeline. Only the proposal's own author or
  delegate may call it; the existing payload is replayed unedited through a fresh
  policy evaluation, evaluated against the resubmission instant rather than the
  proposal's original `created_at`. A `ProposalEvent(type="resubmit")` is always
  recorded, regardless of outcome. `POST /proposals/{proposal_id}/resubmit` (REST)
  and `ontolith.resubmit` (MCP) both wrap it, returning the same `{proposal,
  decision}` shape as `POST /proposals`; CLI parity landed separately — see the
  KI-032 entry below.
- `Ontology.proposals()` (and `GET /proposals`, `ontolith proposal list --state`)
  gained a `state="pending"` query-level alias merging `require_review` and
  `changes_requested` — the other half of KI-027: even with `resubmit()` able to act
  on a `changes_requested` proposal, it was previously invisible to any single-state
  query a reviewer would naturally run. `"pending"` is an explicit opt-in, not the
  default (which remains `state="require_review"`) — a canonical reviewer loop
  (`for p in kb.proposals(): kb.accept_proposal(p.id, ...)`) assumes every returned
  proposal is reviewer-actionable, which `changes_requested` proposals are not.
- CLI `ontolith proposal accept|reject|review <id> --reviewer [--reason]` and
  `ontolith proposal resubmit <id> --author` (closes KI-032): previously
  `proposal list` was the CLI's only proposal command, so an operator using only
  the CLI could see what was pending review but had no way to act on it — every
  other primary interface (SDK, REST, MCP-for-`resubmit`) already could. `review`
  maps to `Ontology.request_changes` — SPEC §14.2 literally names the CLI command
  `proposal {list|review}`, and REST's `/review` route agrees. `accept`/
  `reject`/`review` take `--reviewer` (with `--author` accepted as an alias);
  `resubmit` takes `--author`, since there the acting principal genuinely must be
  the proposal's own author or delegate — the two options are deliberately not the
  same name for the same reason across all four commands. `resubmit` closes the
  CLI gap `Ontology.resubmit`/REST/MCP explicitly deferred to this KI when it
  shipped (see the KI-027 entry above).
- CLI `ontolith schema show [--namespace]` (partially closes KI-038): SPEC §14.2
  normatively lists `ontolith schema {show|migrate}` as CLI surface, but the CLI had
  no `schema` command at all — every other primary interface (SDK, REST, MCP) could
  already inspect a registered schema. Prints concepts, properties, and relations —
  the same field set as MCP's `ontolith.schema`/REST's `GET /schema` output (both
  already extended by KI-029 to include relations), normalized to one consistent
  attribute order rather than copying either verbatim (REST's own `PropertyOut`/
  `RelationOut` don't agree with each other on relation field order). `ontolith
  schema migrate` remains unimplemented — schema versioning/migration isn't built
  anywhere yet, only monotonic version numbering via `apply_schema` — forward-tracked
  as KI-048 rather than left implicit in KI-038's now-partial-resolved status.
- `.where()` lookup operators `__contains`/`__gt`/`__lt`/`__gte`/`__lte` (closes
  KI-039): a documented use-case example used `where(text__contains=...)` before
  `.where()` supported any lookup-operator syntax at all — every dunder-suffixed key
  raised `ValidationError` (KI-030). `__contains` does a substring match (`LIKE`,
  wildcards escaped); `__gt`/`__lt`/`__gte`/`__lte` do a numeric range comparison,
  restricted to predicates the active schema declares `Integer`/`Float` (`value_lit`
  is always stored as `TEXT`, so an unrestricted ordering comparison would silently
  compare `"9" > "10"` lexicographically) — a relation predicate is rejected the same
  way, since `SchemaIR.value_type_of()` returns `None` for one. Two different
  operators can target the same predicate (`.where(age__gte=18).where(age__lt=65)`),
  which required changing how filters are represented internally and passed to the
  backend.
- **Breaking:** `StorageBackend.entities_where`'s `predicate_filters` parameter
  changed from `dict[str, str]` to `list[tuple[str, str, Any]]` (`(predicate,
  operator, value)` triples, KI-039) — a predicate-keyed dict couldn't represent two
  different operators on the same predicate. Any third-party `StorageBackend`
  implementation must update.
- Review found `__contains` genuinely wasn't identical across backends as first
  shipped: SQLite's `LIKE` is case-insensitive by default, DuckDB's is not, so the
  same filter matched different result sets per backend. `SQLiteBackend` now sets
  `PRAGMA case_sensitive_like = ON` at connection time to agree with DuckDB's
  default. `.where(x__contains=<non-str>)`/`.where(x__gt=True)` now raise
  `ValidationError` eagerly instead of a bare `AttributeError` (the former) or
  silently accepting a bool as numeric (the latter, since `bool` is an `int`
  subclass). A leading-dunder key with an empty property name (`.where(__contains=
  "x")`) is now rejected instead of silently compiling to an unmatchable predicate —
  the exact KI-030 failure shape. Range-operator schema validation now resolves via
  `get_schema_at()` under `.as_of()` (SPEC §11.4) instead of always today's schema.
  ADR-0027 was amended (traversal remains deferred) and `docs/Ontolith_SPEC.md`
  §11.1 plus the REST/MCP/CLI filter docs, which had drifted to claim equality-only,
  were corrected. Filed separately, not fixed here: nothing validates that a
  literal's stored content actually parses as its declared `value_type` (KI-031 only
  checks the type token) — SQLite's `CAST` silently returns `0.0` for non-numeric
  stored text under a range filter where DuckDB's `TRY_CAST` excludes the row
  instead, a real, tracked cross-backend divergence (KI-049).
- Registered `Validator` plugins (SPEC §13.2) are now actually invoked — previously
  `.validate()` had zero call sites anywhere outside the protocol/plugin definitions
  themselves (closes KI-042). `Ontology`/`Ontology.connect` gain two new constructor
  parameters: `validators` (per-assertion, synchronous, blocking — runs at every point
  an assertion actually commits: `assert_literal`, `assert_ref`, `propose`/
  `propose_ref`'s auto-accept path, and `accept_proposal`/`resubmit`'s replay of a
  proposal's operations) and `completeness_validators` (whole-entity, run once per
  distinct subject touched by an accepted proposal's operations, `accept_proposal`
  only — never direct writes or any auto-accept path). The two-list split exists
  because a single per-assertion invocation point cannot serve whole-entity-completeness
  checks: an entity built up one assertion at a time is incomplete by construction until
  its last write (see ADR-0029). `RequiredFieldsValidator` gains a
  `from_schema(schema: SchemaIR)` classmethod (closes KI-041) that derives its
  `required_predicates` from the schema's own `PropertyDef.required`/
  `RelationDef.required` declarations instead of a hand-maintained, independently
  drifting mapping — wire it via `completeness_validators=[RequiredFieldsValidator.
  from_schema(schema)]` for schema-declared `required` fields to actually be enforced.
  `Validator.validate()`'s `kb` parameter type widened from the concrete `ReadOnlyView`
  to a new minimal structural `ValidatorKbView` Protocol (`plugins/ports.py`, exported
  from `ontolith.plugins`), since `Ontology`-registered validators receive the live
  `Ontology` instance itself as `kb` (trusted the same way `PolicyStrategy` already is,
  not sandboxed) rather than a capability-scoped view; `PluginRegistry`-loaded
  validators are unaffected and still have no automatic invocation point of their own —
  recorded as an explicit follow-up in ADR-0029, not a new KI.
- CLI `ontolith schema migrate <file> --author <admin>` (closes KI-048, ADR-0034):
  completes the SPEC §14.2 `ontolith schema {show|migrate}` surface KI-038 left half
  implemented. A thin wrapper reading a LinkML-aligned YAML document (ADR-0013 dialect)
  from disk and applying it as a new schema version via the existing governed
  `Ontology.apply_schema` — no new domain logic or port method. YAML-only, not
  class-DSL (no existing mechanism loads a `SchemaIR` from a class-DSL *file path*
  without dynamically executing arbitrary Python; a class-DSL schema still reaches this
  command via the existing `compile_schema()` → `to_yaml()` round-trip). Does not
  migrate/backfill existing assertion data against a changed schema — that remains
  explicitly out of scope, tracked as its own future decision.

#### Fixed
- **Breaking:** `StorageBackend.assertions()` gains an `include_history: bool = False` parameter
  (closes KI-098, found reviewing KI-095) — the asymmetry ADR-0049 left where
  `.include_history()` opted a retracted assertion back into `.query()`/`.min_confidence()`/
  `.trust_at_least()` under `.as_of()`, but the lower-level `assertions()` read path had no
  equivalent opt-out at all, closing the exact same window unconditionally. Both backends'
  `assertions()` now wrap the ADR-0049 retraction-exclusion check in `if not include_history:`,
  mirroring `include_flagged`'s existing shape on the same method. `AsOfView.assertions()` now
  passes `include_history` as an explicit keyword on every call, so a third-party `StorageBackend`
  still on the old signature raises `TypeError` on every `kb.as_of(t).assertions(...)` call, not
  only ones touching a retracted assertion. No new bitemporal semantics, no ADR needed — purely
  closing a parameter-shape gap the retraction fix left behind.
- `entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()`'s `as_of` branch
  now excludes/includes `flagged` assertions point-in-time, not by current status (closes KI-097,
  found building ADR-0049's KI-095 fix) — a `.query()`/`.min_confidence()`/`.trust_at_least()` call
  pinned to a `t` when an assertion *was* disputed could wrongly include it once the dispute was
  later resolved (`reactivated` flips status back to `active`), and, in the opposite direction, a
  `t` strictly before a dispute ever existed could wrongly exclude an undisputed value just because
  the same assertion is `flagged` *now*. `StorageBackend.assertions()` already reconstructed this
  correctly from the `assertion_event` log; the three `QueryBuilder`-facing methods never picked up
  the identical logic. Ported verbatim, on both backends — no new bitemporal semantics decision, no
  ADR needed.
- `.as_of(t)` no longer wrongly includes a retracted assertion once its own retraction has become
  known (closes KI-095, ADR-0049, found reviewing KI-081) — SPEC §11.2's "excluded by default" was
  unmet on the `as_of` path: `_retraction_valid_to()` deliberately leaves an already-set `valid_to`
  untouched at retraction, so an assertion asserted with an explicit far-future end date kept
  matching `.as_of(t)` for any `t` its stale window still covered, with `.include_history()`'s
  documented `as_of` no-op leaving no opt-out either. Fix is read-side only, no schema change:
  `entities_where()`/`entities_meeting_confidence()`/`entities_meeting_trust()`'s `as_of_time`
  branch (both backends) now also excludes a `retracted` assertion once its own retraction event's
  timestamp is `<=` the queried instant — reusing the timestamp `_record_assertion_event()` already
  writes exactly once per assertion (KI-051), never previously consulted by any bitemporal query
  path. `StorageBackend.assertions()` (both backends) gets the identical exclusion, unconditional
  as originally shipped here — a distinct bitemporal query path with its own governance-visible
  impact, since `SourceQuorum`
  evaluates `kb_view.assertions(...)` during policy decisions. `.include_history()` becomes the
  opt-out on the `QueryBuilder`-facing trio, the first thing it has ever done on the `as_of` path
  (`assertions()` gained the same opt-out separately as KI-098) — see ADR-0049 for the full
  rationale and the rejected alternative (closing `valid_to` to the retraction instant at write
  time, which would have permanently destroyed the originally asserted end date). **Existing
  `as_of` callers may see smaller result sets** for any query that previously (incorrectly)
  surfaced a retracted value.
  **Filed separately, found while building this fix:** KI-097 — the same current-status-vs-
  point-in-time gap affects `flagged`/`reactivated` reconstruction on the `QueryBuilder`-facing
  trio specifically (`assertions()` already reconstructs that pair correctly).
- `Ontology.create_entity()` now wraps its natural-key uniqueness check and the `put_entity` it
  guards in one `transaction()` block (closes KI-092, filed while fixing KI-084) — closing the last
  read-then-write path that could hit the raw `UNIQUE` constraint (a redacted `StorageError`, the
  exact error KI-091 was filed to eliminate for the non-concurrent case) instead of the friendly
  `ValidationError`, both under a multi-threaded ASGI server (the RLock is held across the block, on
  either backend) and cross-process (SQLite's `BEGIN IMMEDIATE`). `create_entity()` **can no longer
  be called from inside a caller's own open `backend.transaction()`** — same constraint
  `create_principal`/`apply_schema` already carry; no in-repo caller nests. Chosen over merely
  remapping the constraint error so `create_entity` is consistent with every other governed write
  path; `BEGIN IMMEDIATE` costs nothing extra uncontended, so no perf downside. `issue_token`,
  `revoke_token`, and `reindex` (also named in KI-092) were left as-is — their reads don't guard an
  invariant a stale read could let a write violate.
- `SQLiteBackend.begin()` now issues `BEGIN IMMEDIATE` instead of a plain deferred `BEGIN` (closes
  KI-084, found in the pre-M4 deep + security audit) — closes a cross-process write-safety gap:
  every write path's SPEC §10 conflict-routing read runs inside the transaction `begin()` opens,
  so a deferred `BEGIN`'s lazily-taken read snapshot let a concurrent writer (a second OS process)
  commit in between, and the resulting write then hit `SQLITE_BUSY_SNAPSHOT` — a stale-snapshot
  failure SQLite deliberately never routes through the busy handler, so it failed instantly no
  matter how long `busy_timeout` allowed. `BEGIN IMMEDIATE` claims the write lock up front instead,
  so contention is now serialized behind the ordinary busy handler and only fails after genuinely
  waiting out `busy_timeout` (now pinned explicitly to 5.0s in `SQLiteBackend.__init__`, rather
  than left as Python's implicit default) — with a `StorageError` message that now distinguishes
  transient lock contention ("safe to retry") from a genuine storage fault, still redacted at every
  interface boundary like any other `StorageError` (KI-083's precedent), so this is a server-log-only
  improvement. `docs/adr/ADR-0001-storage-default.md` gained a dated Update section elaborating its
  existing one-line "single-writer limitation" consequence into the actual deployment implication,
  including a genuinely new trade-off this fix introduces (a contended `begin()` now blocks this
  process's own reads for up to `busy_timeout`, not just cross-process writers) rather than only
  documenting a pre-existing one. Not a blanket fix for every `Ontology` write: `create_entity`,
  `issue_token`, `revoke_token`, and `reindex` did not open a `transaction()` at all (filed
  separately as KI-092; `create_entity` since given one — see below), and
  `propose`/`propose_ref`/`retract`'s reject/require-review outcome persists outside one too — the
  latter a pre-existing, already-accepted tradeoff from KI-035, not reopened here.
- `create_entity()` now raises `ValidationError` naming the conflict when `natural_key` is already
  taken within `concept`, instead of relying on the `entity` table's `UNIQUE(namespace, concept,
  natural_key)` constraint to fail late into a generic, redacted `StorageError` that discarded the
  backend's own already-clear conflict message (closes KI-091, found while fixing KI-082) — same
  class of fix as KI-083/KI-089. New `StorageBackend.get_entity_by_natural_key()` port method
  (implemented on both backends); no-op when `natural_key` is `None` (`NULL` is exempt from the
  `UNIQUE` constraint on both backends, verified directly, so there's nothing to check).
  Pre-existing SDK behavior; KI-082 made it reachable by a `propose`-tier AI agent for the first
  time. Conformance vectors added (`TestDuplicateNaturalKeyRejected`, both backends) plus direct
  backend-level tests for the new port method.
- `create_entity()` now raises `ValidationError` naming the concept when `concept` isn't declared
  in the active schema, instead of silently persisting an entity under an undeclared concept
  (closes KI-090, found while fixing KI-082) — mirrors `_require_known_predicate`'s identical,
  long-standing precedent for `predicate` on every assertion write. Pre-existing SDK behavior;
  KI-082 made it reachable by a `propose`-tier AI agent via REST/GraphQL/MCP for the first time,
  not just a human CLI operator or the plugin sandbox's `csv_importer` reference plugin (both of
  which reached this exact gap before KI-082 too). No-op when no schema is registered for the
  namespace, matching every other schema-declared-thing check in this codebase. New
  `SchemaIR.has_concept()`; conformance vectors added (`TestUnknownConceptRejected`, both
  backends).
- `assert_literal`/`assert_ref`/`propose`/`propose_ref` now raise `NotFoundError` naming the
  entity id when `subject` doesn't exist, instead of relying on the `assertion` table's
  `FOREIGN KEY` constraint to fail late with a generic, redacted `StorageError` ("Assertion
  conflict ... FOREIGN KEY constraint failed") that misdescribed a missing entity as a conflict
  (closes KI-083, found in the pre-M4 deep + security audit). `NotFoundError` isn't
  message-redacted at any interface boundary, so REST/GraphQL/MCP callers who typo an entity id
  now see the real error instead of an opaque 500. Found along the way, filed separately: `assert_ref`/
  `propose_ref`'s `target` has no equivalent check and, unlike `subject`, no `FOREIGN KEY` backing
  it either — a nonexistent target silently succeeded (KI-089, closed below).
- `assert_ref`/`propose_ref` now raise `NotFoundError` naming the entity id when `target` (a
  relation's other endpoint) doesn't exist, instead of silently persisting a dangling reference
  (closes KI-089, found while fixing KI-083). Unlike `subject`, `target` (the `assertion` table's
  `value_ref` column) had no `FOREIGN KEY` to fail on either — before this fix, the write just
  succeeded and the KB ended up with a reference to an entity that was never created. Adds
  `Ontology._require_existing_target()`, called from both write paths right after the existing
  subject check.
- `schema/linkml.py`'s LinkML bridge now emits `range: double` for `value_type="Float"`, not
  `range: float` (closes KI-068): real LinkML tooling treats `float` as 32-bit `xsd:float`, but
  Ontolith's `Float` is backed by Python's `float` (IEEE-754 double) throughout, so the old mapping
  understated the actual precision and diverged from `schema/rdf.py`'s own RDF/OWL bridge, which
  already mapped the identical `value_type` to `XSD.double` — a same-`value_type` inconsistency
  ADR-0036 disclosed on its own side but ADR-0013 didn't (both now updated). `from_yaml` already
  accepted `double` on import before this change; `float` stays accepted too, for backward
  compatibility with hand-authored LinkML and schemas exported by a pre-KI-068 Ontolith version.
- REST's `GET /contradictions`/GraphQL's `Query.contradictions`, `GET /proposals`/
  `Query.proposals`, and the CLI's `contradiction list --state`/`proposal list --state` all passed
  an unvalidated `state` filter straight to the backend's `WHERE state = ?` (closes KI-077, found
  during KI-076's review): an unrecognized value (a typo, wrong case, or a plausible-sounding
  synonym) silently matched zero rows instead of erroring — indistinguishable from "no results in
  that state." Every one now raises/exits on anything outside its accepted set (`{"open",
  "resolved", "all", None}` for contradictions; the 8 `Proposal.state` values plus
  `"pending"`/`"all"`/`None` for proposals; the CLI has no `"all"` value for `--state` itself,
  since `--all` is its own separate flag). `Proposal.state`/`Contradiction.state` are now named
  `ProposalState`/`ContradictionState` `Literal` aliases (`govern/proposal.py`/
  `govern/contradiction.py`) rather than inlined, so every interface — MCP's own
  `ontolith.list_contradictions` included, previously a hand-written duplicate of the same tuple —
  derives its accepted-value set from the same `get_args()` call on the shared alias instead of
  four independent copies that could silently drift from each other.
- `Ontology.flag_contradiction()`'s "extend" branch reading a malformed prior `rationale_history`
  blob (pre-existing since KI-071, found during KI-076's review): `existing.metadata.get(
  "rationale_history", [])` either raised (a non-iterable value) or, worse, silently corrupted the
  trail further on write (e.g. a bare string exploded into one entry per character). Now routes
  through the same `safe_rationale_history()` helper the read surfaces already use. Filed KI-077
  for the same unvalidated-`state`-parameter shape on REST's `GET /contradictions`/GraphQL's
  `Query.contradictions`, pre-existing and out of this fix's own scope.
- `flag_contradiction()`'s `rationale` is no longer silently dropped when extending an
  already-open contradiction (closes KI-071). Previously only the "create a new contradiction"
  branch wrote `rationale` into `Contradiction.metadata`; the "extend" branch never touched
  `metadata` at all. Both branches now write into a unified `metadata["rationale_history"]` shape
  — a list of `{"rationale", "actor", "at"}` entries, one per call that supplied a truthy
  rationale (`None`/`""` are both still treated as "none given", matching the method's
  long-standing behavior) — so a rationale given while extending is preserved alongside whatever
  was recorded at creation, and a rationale-less extend leaves prior history untouched rather than
  blanking it. `Contradiction.metadata` was never exposed by any interface, so no consumer
  depended on the old single-key `{"rationale": ...}` shape from the `create` path — it's unified
  into `rationale_history` too. No shipped interface reads `rationale_history` back yet — tracked
  as KI-075.
- **Breaking:** `StorageBackend.update_contradiction_members` gained an optional `metadata`
  parameter (implemented identically in SQLite and DuckDB), backing the `flag_contradiction` fix
  above. `Ontology` now always passes `metadata=` (`None` when no rationale was given) on the
  "extend" branch, so a third-party backend still on the old 2-argument signature raises an
  unmapped `TypeError` (not a SPEC §16 `OntolithError`) on *every* `flag_contradiction` call that
  extends an existing contradiction, rationale or not — accept a `metadata` keyword argument if
  your backend needs to keep working.
- **Breaking:** MCP now has one blanket error-handling path instead of hand-catching a handful of
  exception types per tool (closes KI-074, ADR-0014 update): a new module-level
  `_error_response(exc)` — the MCP equivalent of REST's `_handle_ontolith_error`/GraphQL's
  `process_errors` override — every tool now wraps its whole body in
  `try: ... except OntolithError as exc: return _error_response(exc)`. Closes the remaining 5 of
  10 taxonomy codes (`SchemaError`, `PolicyDenied`, `ConflictError`, `StorageError`, `PluginError`)
  that were previously unreachable from MCP entirely (escaping as an unstructured protocol
  exception with no code), and redacts `StorageError`/`PluginError` messages the same way
  REST/GraphQL already do (they interpolate raw internal exception text). Every tool's response
  shape also gains a `detail` key, matching REST/GraphQL's `{"code", "message", "detail"}` — this
  is the breaking part: any client relying on the exact 2-key `{"error", "code"}` shape gets a
  third key now, though `error`/`code` themselves are unchanged for the codes MCP already returned.
  Review found the new redaction turned a pre-existing mislabel in `Ontology.retract()` into an
  information-destroying one: an unknown `assertion_id` used to surface an actionable (if
  misclassified) `StorageError` message; blanket redaction hid it behind "An internal error
  occurred" — and, on the review-routed path an AI/MCP caller actually takes, no error surfaced
  at all, letting a phantom proposal persist. Fixed at the source — `retract()` now raises
  `NotFoundError` for an unknown `assertion_id` unconditionally, before policy is even evaluated
  — which also improves REST (`404` instead of `500`) and GraphQL, not just MCP.
- **Breaking:** MCP's error `code` values now match REST/GraphQL's shared taxonomy (closes KI-059,
  SPEC §16): 32 hand-written lowercase literals (`"auth_error"`, `"not_found"`, etc.) in
  `interfaces/mcp.py` replaced with `exc.code` from the caught `OntolithError` (or the exception
  class's own `.code` attribute where no instance is in scope), matching what REST/GraphQL already
  pass through unmodified. Any existing MCP client string-matching the old lowercase codes breaks
  on upgrade — MCP tool-schema/response stability has no formal ADR-0019-style policy yet (that ADR
  explicitly excludes `interfaces/mcp` from its scope), but this is called out regardless, the same
  as prior MCP wire-contract changes. Also found and fixed along the way:
  `ontolith.get`/`ontolith.provenance`'s "not found" responses had no `code` key at all, not just
  the wrong casing. New `tests/unit/test_cross_interface_error_codes.py` asserts REST, GraphQL, and
  MCP all report the same code for the same underlying exception type, sharing one backend across
  all three. The 5 taxonomy codes still unreachable from MCP at all (`SchemaError`, `PolicyDenied`,
  `ConflictError`, `StorageError`, `PluginError` — MCP still hand-catches per call site rather than
  one blanket mapping like REST/GraphQL) are tracked as KI-074, not fixed here.
- Bitemporal-correctness gap in `QueryBuilder.semantic()`, found while adding MCP's `as_of`
  support (KI-058): combined with `.as_of()` but no `.where()` filter, semantic search previously
  ignored `as_of_time` entirely, returning entities that didn't exist yet at that point in time. No
  prior interface could trigger this (REST/GraphQL never exposed `as_of`), so it was unreachable
  until MCP's own `as_of` addition made it reachable. `.semantic()` combined with `.as_of()` still
  only excludes entities that didn't exist by that time — it does not make the vector search itself
  bitemporal, since the vector index holds one embedding per entity with no historical versions;
  documented explicitly on `QueryBuilder.semantic()` and `query_tool`'s own `as_of` docstring.
- `Ontology.retract()` — the codebase's most heavily-governed write path — is now
  reachable from every shipped interface, not just the SDK (closes KI-057, ADR-0039):
  `POST /assertions/{id}/retract` (REST, `acting_as` as an optional query parameter),
  a `retract` mutation (GraphQL), a new top-level `ontolith retract <id> --author <id>
  [--acting-as <id>]` CLI command, and `ontolith.retract` (MCP, `propose` capability
  tier — the same tier as `ontolith.propose`/`ontolith.flag_contradiction`, unlike the
  reviewer-only `resolve_contradiction`, which stays MCP-excluded). All four are thin
  wrappers with no new domain logic.
- **Security, Breaking:** `create_graphql_app`'s `introspection` parameter now
  defaults to `False` (closes KI-056, amends ADR-0037). Introspection queries are
  self-referentially recursive over the schema's own type graph, and neither
  `QueryDepthLimiter` nor `MaxTokensLimiter` can bound that recursion (verified
  directly — `QueryDepthLimiter` hardcodes an introspection carve-out in
  *strawberry-graphql's own* depth-limiting validator, not graphql-core, which
  has no depth validator at all); an anonymous caller previously had an
  unauthenticated, unbounded-recursion amplification vector with no mitigation.
  Pass `introspection=True` to opt back in — note `graphql_ide` still defaults
  to serving GraphiQL, so pass both together for a working interactive dev
  experience. Also new: `MaxAliasesLimiter`/`QueryDepthLimiter` are wired into
  every schema unconditionally, capping alias count and query depth — *reduces*
  the cross-interface DoS vector KI-052's async resolver conversion introduced
  (measured: 15 concurrent worker threads per max-alias request against the
  shared anyio pool's default 40-thread capacity, so this doesn't eliminate the
  vector, only shrinks the amplification ratio from ~200 aliased fields to 15).
  Requires `strawberry-graphql>=0.316` (bumped from `>=0.219` — the
  factory-callable extension pattern this fix uses raises `TypeError` at
  request time on older releases).
- **Security, Breaking:** `require_admin` (`ontology.py`) now rejects AI-kind
  principals regardless of their configured capability (closes KI-053, ADR-0038),
  matching every other capability-tier gate in the codebase. Previously a
  misconfigured AI principal with `default_capability="admin"` could call
  `issue_token()` for a human principal and authenticate as them over
  REST/GraphQL, bypassing every AI-kind guard on direct write, review, and
  contradiction resolution — any deployment relying on that (misconfigured)
  behavior now gets `CapabilityError` instead. CLI's `principal create` now
  requires `--author` (naming an existing admin) once a database has any
  principal at all, mirroring REST's already-correct external-gate pattern, with
  a bootstrap exception only for a database's first-ever principal (closes
  KI-054, same ADR) — **any script or runbook calling `ontolith principal
  create` without `--author` against a non-empty database now exits 1** instead
  of succeeding.
- **Security:** `PluginRegistry.register()` (`plugins/registry.py`) now logs a warning, on
  successful registration, when a plugin's manifest declares `capabilities.network=True` or
  `capabilities.filesystem=True` (amends KI-014's still-open half, ADR-0015). Only
  `capabilities.storage` is actually enforced — plugins run in-process with no process/wasm
  isolation — so declaring these was previously silent, leaving an operator deciding whether to
  register the plugin with no signal, at the moment that matters, that the declaration does
  nothing. Three of the four shipped reference plugins (`CsvImporter`, `JsonExporter`,
  `RdfExporter`) declare `filesystem=True` and now log this on every registration — expected, not
  a regression. Visibility only, not enforcement: SPEC §17's MUST for network/filesystem
  isolation remains unmet. Real process/wasm isolation is unchanged, tracked separately per the
  Implementation Plan's existing phasing.
- GraphQL resolvers (`interfaces/graphql.py`) no longer block the ASGI event loop
  (closes KI-052, amends ADR-0037). Every `Query`/`Mutation` field, plus
  `EntityType.assertions` and `create_graphql_app`'s `_get_context`, is now
  `async def`; the actual blocking `kb`/`kb.backend` calls are factored into sync
  helper functions and dispatched via `starlette.concurrency.run_in_threadpool`.
  Previously every resolver ran inline on the event loop (unlike REST, whose
  routes Starlette dispatches to a thread pool automatically), so a slow resolver
  blocked *all* concurrent traffic, not just database-bound requests — measured
  directly: three concurrent requests against a deliberately slowed resolver went
  from ~0.92s (serialized) to ~0.33s (overlapping, matching REST).
- **Breaking:** KI-033's party guard and KI-043's capability floor now apply to a
  `retracted`/`superseded` contradiction member exactly as they already did to a
  `flagged` one (closes KI-051, amends ADR-0030). Both guards previously gated on
  `status == "flagged"` before ever checking contradiction membership, so a member
  ADR-0031 had already terminalized while its contradiction stayed `open` was
  completely exempt — a party could retract it, or a below-floor neutral principal
  could auto-accept retracting it, with no governance applied. `_open_contradiction_if_member`/
  `_require_capability_to_retract_contradiction_member` (renamed from
  `..._flagged_member`/`..._if_flagged_member` for accuracy) now key off contradiction
  membership alone, independent of the target's own status. Separately, `retract()`
  now no-ops (no status/event write) when re-retracting an already-`retracted` target,
  closing a narrower, contradiction-independent event-misattribution gap — deliberately
  **not** extended to an already-`superseded` target, unlike `resolve_contradiction()`'s
  own loser-loop no-op (KI-044): explicitly retracting a `superseded` assertion via
  `retract()` remains a real, event-recording transition this codebase already relies
  on (`test_events_ordered_oldest_first`).
- **Breaking:** `flag_contradiction()` now rejects opening a **new** contradiction whose
  two founding members are both already `retracted`/`superseded` (closes KI-050,
  ADR-0035) — `resolve_contradiction()` already rejects a terminal-status winner
  candidate (KI-044, ADR-0031), so an all-terminal pair at creation time opened a
  contradiction with zero eligible winners, forcing a follow-up write before it could
  ever be resolved. Mirrors `resolve_contradiction()`'s own check in mechanism
  (`ValidationError`, not a capability gate — this is a structural validity issue, not a
  capability shortfall) and in the terminal-status set checked. Only guards contradiction
  *creation*; extending an *already-open* contradiction with an all-terminal pair remains
  permitted (ADR-0031's own deliberate escape hatch for naming a terminal assertion for
  audit/context, unchanged). Checked against the fresh, in-transaction reads KI-045
  already established, so it also covers the race variant (a concurrent
  retract()/supersession terminalizing both named assertions between read and write),
  not just an explicit two-terminal-ids call.
- **Breaking:** `assert_literal`/`propose` now reject a literal whose `value` doesn't
  actually parse as its (already token-matched, KI-031) declared `value_type` (closes
  KI-049, amends ADR-0028) — e.g. `assert_literal(..., "unknown", "Integer", ...)`
  previously succeeded since `value_type="Integer"` matched the schema even though
  `"unknown"` isn't a valid integer; now raises `ValidationError`. A dedicated regex for
  `Integer`/`Float` (not bare `int()`/`float()`, which accept underscore separators,
  whitespace, and — for `float()` — `"inf"`/`"nan"`, none of which cast consistently
  across both backends' KI-039 SQL paths); Python's `fromisoformat` grammar for
  `Date`/`DateTime` (`Date` rejects a string carrying a time component);
  case-insensitive `"true"`/`"false"` only for `Boolean` (not `"1"`/`"0"`, a deliberate
  choice); `json.loads()` for `JSON` (rejecting the non-standard `NaN`/`Infinity`
  constants too); `URI` accepts LinkML's `uriorcurie` shape (ADR-0013) — a full URI or a
  CURIE, not a strict RFC 3986 parse. `Text` has no format to validate. Enforced at
  submission time only, same as the token check beside it — not retroactive against
  already-stored data (no migration mechanism, KI-048) and not re-run at proposal
  replay, matching that check's own established precedent.
  `entities_where()`'s `TRY_CAST`/`CAST` defensive handling (KI-039) is unchanged and
  still necessary for pre-existing data.
- **Breaking:** `.trust_at_least()`/`entities_meeting_trust` now compare a delegated
  assertion's *effective* trust — `min(author.trust_level, acting_as.trust_level)` —
  instead of the author's raw `trust_level` alone (closes KI-047). Matches
  `govern/policy.py`'s existing effective-trust formula for the same assertion, by
  analogy with SPEC §8.4's capability rule; a low-trust delegate acting as a high-trust
  principal (or vice versa) is now scored consistently between policy evaluation and
  query-time filtering, which it previously wasn't. Cross-backend divergence, verified
  before implementing: SQLite's `min(a, b)` is the scalar two-argument form; DuckDB's
  `min(a, b)` is aggregate-only and returns a list for two scalar args, so DuckDB's
  query uses `least(a, b)` instead. A dangling `acting_as` (no resolvable delegate)
  fails open, falling back to the author's own `trust_level`, deliberately unlike
  `_resolve_delegation`'s fail-closed behavior for the same input at write time.
  Non-delegated assertions are unaffected. New conformance vectors
  (`TestTrustAtLeastDelegationAttenuation`) cover both attenuation directions, the
  inclusive threshold boundary, and the dangling-delegate fallback. See ADR-0033.
- **`DuckDBBackend` now serializes connection access across threads with a
  `threading.RLock`, mirroring `SQLiteBackend`'s KI-023 fix (closes KI-046).**
  DuckDB's own DB-API `threadsafety` level is 1 ("threads may share the module, but not
  connections") — the identical constraint that drove SQLite's fix — but `DuckDBBackend`
  had no lock and no guard of any kind, so two concurrent transitions on the same
  connection didn't serialize. All 40 public methods now carry the same `@_synchronized`
  decorator `SQLiteBackend` uses; `begin()`/`commit()`/`rollback()` acquire/release the
  lock with the identical asymmetric-release pattern. No `_in_transaction` flag needed
  (unlike SQLite) — DuckDB's own native autocommit already makes standalone writes
  durable without one. New threaded regression tests
  (`tests/unit/test_duckdb_backend.py::TestConcurrency`) mirror SQLite's own KI-023
  coverage; four of five confirmed to fail against the pre-fix code, including a new
  test proving the worst pre-fix consequence: silent data corruption on concurrent
  reads (wrong/missing rows, no exception raised at all), not just an unguarded
  transaction span. See ADR-0032.
- **`flag_contradiction()` no longer has a TOCTOU window between reading its target
  assertions/existing open contradiction and writing its decision (closes KI-045).**
  Both target assertions and any existing open contradiction for their `(subject,
  predicate)` were read before opening the write transaction — a concurrent
  `retract()`/supersession landing in the gap meant the KI-034 terminal-status guard
  could still see a stale `active` status and resurrect an assertion that had since
  become terminal, and a concurrent `resolve_contradiction()` closing the open
  contradiction in the gap meant this call could extend an already-`resolved`
  contradiction (`update_contradiction_members()` has no state guard of its own).
  Mechanically identical to KI-035's fix for the four proposal-transition methods: the
  reads and the decisions built on them now happen as the first statements inside the
  transaction, re-read fresh; only the principal/capability check stays outside (pure
  identity, not state that races — verified no code path mutates a principal's
  capability after creation). New conformance vectors (`TestFlagContradictionTOCTOU`)
  simulate three races deterministically via the same `_RacingClock` test double KI-035
  introduced, without real threads: a concurrent `retract()`, a concurrent
  `resolve_contradiction()` closing the contradiction being extended, and a concurrent
  `flag_contradiction()` opening a competing contradiction for the same
  `(subject, predicate)` — all three confirmed to fail without the fix.
- **Breaking:** `resolve_contradiction()` now rejects a winner candidate whose status is
  already `retracted` **or `superseded`** with `ValidationError` instead of reactivating
  it to `active` with a closed `valid_to` (closes KI-044) — that combination silently
  undoes a governance action's close of the assertion's validity window with no new
  write recording it reopened. Mirrors the existing "winner not a member" check exactly
  (same exception type, same validation loop, before any write, checked only after the
  party-to-contradiction guard has cleared for every member so error precedence is
  deterministic). Also closes a related gap: extending an open contradiction previously
  un-terminalized a `superseded` member back to `flagged` (the KI-034 fix only ever
  covered `retracted`) — `_apply_with_conflict_routing`'s extension branch now skips
  both, and `resolve_contradiction()`'s own loser loop does the same instead of
  overwriting a `superseded` loser to `retracted` and misattributing a second event to
  the resolver. Retraction/supersession is now terminal everywhere `resolve_contradiction()`
  and its feeder write paths are concerned (party-to-contradiction guard KI-033,
  conflict-routing extension KI-034, `flag_contradiction()`'s own re-flag guard, and now
  the winner/loser checks here) — except `retract()` itself, which still doesn't
  recognize a `superseded` member as governed the way it does a `flagged` one, filed
  separately as KI-051. A resolver who wants a terminal value active again must submit
  it as a new assertion instead — for a `time_varying` predicate this means
  `flag_contradiction()` specifically, not a bare re-assert (which comes back `active`
  and never rejoins the contradiction; the bare-reassert shortcut only ever applies to
  `static` predicates). A contradiction whose every *existing* member ends up terminal
  has no eligible winner until one more assertion lands (not a permanent dead end — see
  ADR-0031 for the two-mechanism escape hatch); `flag_contradiction()` can also open a
  brand-new contradiction with no eligible winner among its two founding members from
  the start, filed separately as KI-050. See ADR-0031.
- **Breaking:** `retract()`/`resubmit()` now route to review, instead of auto-accepting,
  when the target is a `flagged` member of an open contradiction and the retracting
  principal doesn't meet the same `review`/`admin` capability + non-AI floor
  `resolve_contradiction()` already enforces (closes KI-043) — previously any
  `write`-capability principal (party to neither disputed value) could retract one side
  of a dispute outright, reaching close to the same effective outcome as
  `resolve_contradiction()` at a materially lower floor. A `write`-capability
  principal's retraction of such a member now returns a `require_review` proposal
  instead of taking effect immediately; a `review`-capable, non-AI principal can accept
  it via `accept_proposal()`. Ordinary retraction (target not a flagged contradiction
  member) is unaffected — still just `write`. Delegation attenuates the effective
  capability (`min(principal, delegating)`, SPEC §8.4) same as direct writes. See
  ADR-0030, which also documents why routing to review (rather than raising
  `CapabilityError` outright, an earlier version of this fix) was necessary to avoid
  leaving a `write`-capability principal worse off than a lower-capability one for the
  same action.
- **`assert_literal`/`assert_ref`/`propose`/`propose_ref` now reject a predicate-kind
  mismatch (closes KI-040):** nothing previously stopped a literal write against a
  schema-declared relation predicate, or a ref write against a schema-declared
  property predicate — `assert_literal(subj, "Person.employer", "Acme Corp", "Text",
  ...)` silently succeeded even when `Person.employer` was declared a relation. New
  `SchemaIR.kind_of(predicate)` resolves the declared kind; `_require_known_predicate`
  gained a required `expected_kind` keyword (not inferred from `value_type`'s presence,
  so a future write path can't silently skip the check by omitting it) and raises
  `ValidationError` on mismatch. No-op for a schema-less namespace, matching this
  method's existing precedent. Fixing this surfaced a real pre-existing bug in two
  unrelated conformance tests that had been writing `assert_ref` against a
  property-declared predicate, silently permitted before this fix.
- **Breaking:** `StorageBackend.entities_meeting_confidence`/`entities_meeting_trust` (port
  + both backends) gained a required `candidate_ids: frozenset[str] | None = None`
  parameter (KI-037) — any third-party `StorageBackend` implementation must add it, since
  `QueryBuilder` now passes it as an explicit keyword on every call (including `None`).
- **`.min_confidence()`/`.trust_at_least()` now exploit an already-narrowed `.where()`/
  `.semantic()` candidate set instead of always scanning the full concept (closes
  KI-037).** `QueryBuilder` passes the new `candidate_ids` hint only when `.where()`/
  `.semantic()` narrowed the base candidate set to at most `_CANDIDATE_HINT_MAX` (1000)
  entities — unbounded, a low-selectivity `.where()` predicate matching thousands of
  entities would make encoding the hint cost more than the scan it saves. Both backends
  use the hint: SQLite binds the id set as a single JSON-encoded parameter (`json_each`)
  rather than one placeholder per id; `DuckDBBackend` uses the equivalent `unnest()`
  construct. Measured on the same machine, same query, same fixture, code-only diff (50k
  entities, single-candidate `.where()` match): several times faster (absolute latency
  is hardware-dependent; see `tests/benchmarks/test_hybrid_query.py`'s like-for-like
  pair for a reproducible comparison rather than a point-in-time number here).
  Bounding the hint's size, not just adding it, is what makes it a reliable win — an
  earlier, unbounded version of the DuckDB hint measured over 100x *slower* for a
  candidate set of a few thousand against a 10k-entity concept. New conformance vectors
  pin the backend-agnostic contract (`(full ∩ candidate_ids) <= narrowed <= full`, which
  holds whether or not a given backend actually narrows); backend-specific unit vectors
  (SQLite and DuckDB) pin that both current backends' own implementations genuinely
  narrow, including an empty-candidate-set short-circuit — all confirmed to fail without
  the fix.
- **`.min_confidence()`/`.trust_at_least()` now respect `.as_of()` (closes KI-036).**
  `QueryBuilder._apply_confidence_trust_filters` never read `self._as_of_time`, so
  `kb.as_of(t).query(...).min_confidence(...)`/`.trust_at_least(...)` always checked
  current-active assertions regardless of `t` — an entity could pass the `.where()` half
  of a bitemporal query as it existed at `t`, then get filtered by confidence/trust values
  that only became true later (or that existed at `t` but were since superseded/retracted).
  `StorageBackend.entities_meeting_confidence`/`entities_meeting_trust` (port + both
  backends) gained an `as_of_time` parameter, mirroring `entities_where()`'s existing
  bitemporal-window branch; `QueryBuilder` now threads `self._as_of_time` through both.
  `trust_level` itself is always the principal's current value, not a historical one — no
  code path updates a principal's `trust_level` after creation, so there is no historical
  value to reconstruct; only which assertion counts as qualifying is bitemporally scoped.
  A new regression guard (`tests/unit/test_principal_trust_immutability_invariant.py`) fails
  if a `principal` table mutation path is ever added, since that would break this shortcut.
  `status` itself is not bitemporally versioned, so a flagged assertion is excluded
  regardless of `t`, even before it was flagged — matching `entities_where()`'s own default.
  Conformance vectors (`TestAsOfConfidenceTrust`, 22 cases across both backends) cover a
  retraction boundary and a schema-declared time_varying supersession boundary per filter,
  plus — per review — vectors isolating each of the four temporal clauses individually
  (backdated-but-not-yet-known, future `valid_from`, the `valid_to` half-open boundary,
  flagged-exclusion) and vectors combining `.as_of()` with `.where()` and with both filters
  chained together; every clause confirmed individually load-bearing via mutation testing.
  Review also found that `.trust_at_least()` ignores delegation attenuation (SPEC §8.4) —
  pre-existing, not introduced here, filed separately as KI-047.
- **`accept_proposal`/`reject_proposal`/`request_changes`/`resubmit` no longer have a TOCTOU
  window between validating a proposal's state and writing its transition (closes KI-035).**
  All four read the proposal, validated its current state, and ran policy evaluation before
  ever opening the write transaction — only the writes themselves were atomic. Two concurrent
  calls that both observed the same pre-transition state (e.g. an `accept_proposal` racing a
  `reject_proposal`, both reading `require_review`) could both pass validation and both reach
  their write, replaying the same proposal's operations twice under an auto-accepting policy.
  `_require_reviewer` split into `_require_reviewer_principal` (reviewer-identity checks, safe
  before the transaction) and `_require_pending_proposal` (re-reads the proposal fresh and
  checks self-review/state — now called as the first thing inside the transaction, in all
  three reviewer-side methods); `resubmit` keeps its original pre-transaction checks as an
  optimistic fast-fail but adds an authoritative re-check inside its own transaction before
  any write. Mirrors `resolve_contradiction`'s own KI-026 fix for the identical bug shape.
  New conformance vectors (`TestProposalTransitionTOCTOU`) deterministically simulate the race
  per method via a `_RacingClock` test double, without real threads — confirmed to fail
  without the fix. Pre-existing, not yet triggered by any test or reported incident
  (single-threaded usage today); found while re-reviewing the KI-027 `resubmit()` fix. Review
  found the identical TOCTOU shape in `flag_contradiction()` (filed separately as KI-045, not
  fixed here — KI-035 itself scopes to the four proposal-transition methods) and that
  `DuckDBBackend` has no equivalent of `SQLiteBackend`'s KI-023 concurrency lock, so this
  fix's serialization guarantee is proven airtight only for SQLite today (filed as KI-046).
- **`retracted` now stays terminal when an open contradiction is extended by a new disputed
  value (closes KI-034).** `retracted` is meant to be a terminal status everywhere in the
  codebase (SPEC §5's append-only lifecycle) — this was the path reachable from the
  write/proposal pipeline where it wasn't. `Ontology._apply_with_conflict_routing`'s "extend
  an already-open contradiction" branch (not `govern/conflict.py`'s pure `route()`, which is
  bypassed entirely once a contradiction is already open) unconditionally re-flagged every
  existing member alongside the incoming assertion, including one that had since been
  legitimately retracted (e.g. by a neutral third party via `retract()`, KI-033) —
  resurrecting it back to `flagged`. The flagging loop now skips the status write (and its
  event) for any member whose current status is already `retracted`, and now raises
  `NotFoundError` for a missing member instead of silently falling through into an unguarded
  write, matching `resolve_contradiction`'s own KI-026 precedent (found in review). The
  member's id is deliberately left in the `Contradiction`'s own `member_ids` — that list
  isn't audit-only, it's also `resolve_contradiction`'s winner-eligibility set and
  `_reject_retract_if_party_to_contradiction`'s scan set — only the re-flagging write is
  skipped. Review found the identical resurrection bug in `flag_contradiction()`'s own,
  separate flagging loop (SPEC §14, MCP `ontolith.flag_contradiction`, reachable at only
  `propose` capability including by an AI principal) — fixed the same way here, now also
  guarding `superseded`. `resolve_contradiction` itself no longer re-emits a duplicate,
  resolver-misattributed `retracted` event for a loser that's already `retracted`. Filed
  **KI-044** (backlog, not fixed here): `resolve_contradiction()` can still pick an
  already-`retracted` member as the *winner*, reactivating it to `active` with a closed
  `valid_to` window — a broader winner-eligibility question this fix doesn't expand into.
  Found while investigating KI-033, not introduced by it — pre-existing.
- **`Ontology.retract()` now rejects retracting a flagged member of an open contradiction
  when the retracting principal (author or delegate) is a party to that contradiction —
  author or delegate of *any* member, not just the target being retracted (closes
  KI-033).** `retract()` routes through the normal policy path like any other write; a
  human principal with `write`/`review`/`admin` capability auto-accepts under
  `ThresholdPolicy` with no contradiction awareness at all, so a principal who authored
  one side of a disputed static fact could retract the *opposing* member directly —
  reaching the same one-sided outcome `resolve_contradiction`'s KI-026 self-resolution
  guard already blocks, just through a side door with no notion of contradictions.
  Retracting your own losing member is blocked too, for the same "any member" reasoning
  KI-026 established: giving up your own side unilaterally ends the dispute in the other
  party's favor just as much as picking your own side as the winner would. The new
  `_reject_retract_if_party_to_contradiction` check runs inside the same transaction that
  performs the retraction, before any of that transaction's writes land (both in
  `retract()`'s own auto-accept branch and in `_replay_proposal_operations`'s `retract`
  branch, shared by `accept_proposal`/`resubmit`) so a contradiction opened or extended
  concurrently can't slip past it — mirroring `resolve_contradiction`'s own race-safety
  reasoning. The checked party set also covers the *accepting reviewer*, not just the
  proposal's original author/delegate: found in review, a reviewer who is themselves a
  party to the same contradiction could otherwise reach the identical one-sided outcome by
  approving a neutral principal's retract proposal instead of retracting directly. A
  missing contradiction member now raises `NotFoundError` rather than silently skipping
  the check for it, matching `resolve_contradiction`'s own precedent (also found in
  review). A neutral third party (author/delegate of no member) is unaffected;
  `resolve_contradiction` remains the correct way to actually close out a disputed fact —
  whether a neutral `write`-capability principal retracting a disputed member should
  itself require `resolve_contradiction`-grade capability is a separate question, tracked
  as KI-043.
- **Breaking:** `QueryBuilder.where()` no longer silently no-ops on relation-traversal
  filter keys (closes KI-030) — `.where(employer__name="Acme Corp")`, an example the class
  docstring itself advertised as working, compiled into an unreachable predicate string
  and always returned an empty result with no error. Dunder-containing keys (`__`) now
  raise `ValidationError` at `.where()` call time instead, naming the offending key and
  explaining that neither multi-hop traversal nor lookup operators are implemented
  (ADR-0027, KI-039) — a caller that previously got `[]` back for such a key now gets an
  exception (MCP: `{"error": ..., "code": "validation_error"}`; REST `POST /query`: `400`
  instead of `200` with an empty list). Separately, `StorageBackend.entities_where()`
  (both backends) now matches a filter value against either `value_lit` or `value_ref` via
  a `UNION ALL` of two indexed point lookups (a new `idx_assertion_pred_ref` index backs
  the `value_ref` arm), so direct relation-target-id equality (`.where(employer="org-123")`)
  actually returns matches — previously it silently matched nothing, since only
  `value_lit` was ever compared, and an initial `value_lit = ? OR value_ref = ?` version of
  this fix was reworked before merge after it was measured to fall back to a full table
  scan on SQLite. Docstrings on `QueryBuilder`/`.where()`/`StorageBackend.entities_where()`
  now state the real contract; SPEC §11.1, the PRD walkthrough, and a use-case doc example
  were corrected to match (see ADR-0027).
- MCP `ontolith.schema` and `GET /schema` now include each concept's `relations`, and
  each property's `cardinality` (closes KI-029) — both were previously omitted entirely,
  so an agent or REST client had no way to see that a relation like `Person.employer`
  exists, whether it's `time_varying`, or its cardinality — the information that
  predicts supersession vs. contradiction on a subsequent proposal (SPEC §10.1,
  ADR-0017). REST's `ConceptOut` gained a new required `relations: list[RelationOut]`
  field (name, target concept, cardinality, required, temporality, inverse) alongside
  the existing `properties`, mirroring `PropertyOut`'s shape (which itself gained
  `cardinality`) — code constructing `ConceptOut`/`PropertyOut` directly (not part of
  the public API surface per ADR-0019 — neither is exported from `interfaces.rest`)
  must now supply the new fields.
- **Breaking:** `assert_literal`/`propose` now raise `ValidationError` when the caller's
  `value_type` doesn't match the schema-declared `PropertyDef.value_type` for `predicate`
  (closes the `value_type` half of KI-031) — previously a predicate declared
  `value_type: Integer` silently accepted a literal written with `value_type="Text"` (or
  any other mismatched, case-sensitive-mismatched type), with no error anywhere.
  `SchemaIR` gained `value_type_of(predicate)`; `assert_ref`/`propose_ref` are unaffected
  (relations have no `value_type`), and no check fires for a namespace with no registered
  schema. This can break a schema-driven CSV import (`plugins/reference/csv_importer.py`)
  that previously relied on its `value_type` column defaulting to `"Text"` for every row —
  under a registered schema whose properties aren't all `Text`, that default may now raise
  mid-import. `required` remains unenforced anywhere in this codebase — ADR-0028 records
  the decision to keep it out of core (SPEC §4 assigns it to the validator layer; a
  per-write core gate is structurally the wrong shape for a check that's necessarily
  cross-assertion), and corrects an inaccurate first-draft claim that the existing
  `RequiredFieldsValidator` plugin already covered it — it doesn't read the schema's
  `required` field (KI-041), and no code path invokes any `Validator` plugin at all
  (KI-042).
- **Breaking:** `StorageBackend` gained two new required Protocol methods,
  `entities_meeting_confidence(namespace, concept, threshold) -> set[str]` and
  `entities_meeting_trust(namespace, concept, min_trust) -> set[str]` (KI-028) — any
  third-party `StorageBackend` implementation must add them.
- **`QueryBuilder.min_confidence()`/`.trust_at_least()` no longer issue one backend round
  trip per candidate entity (closes KI-028)** — reintroduced the same N+1 pattern KI-001
  fixed for `.where()`, apparently unnoticed when the two filters shipped alongside
  `.semantic()` as part of KI-018's hybrid retrieval. `entities_meeting_confidence`/
  `.entities_meeting_trust` (both backends) push each filter down to a single
  `(namespace, concept)`-scoped query, replacing the Python-side per-entity
  `assertions()`/`get_principal()` loop — a query bound to the candidate id list instead
  was tried and reverted, since its parameter count scales with data size (hits SQLite's
  bound-variable limit outright on large concepts; costs DuckDB linear per-parameter bind
  overhead). New benchmarks (`tests/benchmarks/test_hybrid_query.py`) and conformance
  vectors (`conformance/test_confidence_trust_filters.py`, covering both backends — the
  pre-existing unit tests only ever exercised SQLite) close the gap that let the original
  regression ship unbenchmarked.
- **Breaking:** `Ontology.issue_token(principal_id, author)` now returns
  `tuple[str, str]` (`(token, credential_id)`) instead of a bare `str` (closes KI-024,
  update to ADR-0014). `issue_token_route` (REST) and `principal issue-token` (CLI)
  previously recovered the newly-issued credential's id via a second,
  non-transactional `list_tokens(...)[0]` call — a concurrent token issuance for the
  same principal in that gap could return a mismatched `credential_id` alongside the
  correct raw token. The credential's id is already known when `issue_token` persists
  it, so both callers now get it directly with no second lookup.
- **`Ontology.create_principal(kind="ai", owner=None)` now raises the documented
  `ontolith.core.errors.ValidationError`** instead of a raw pydantic `ValidationError`
  leaking out of `Principal`'s own model validator. Found while wiring `POST /principals`
  (ADR-0022): REST's error mapping only handles `OntolithError` subtypes, so this would
  have surfaced as an unhandled 500 with no SPEC §16 envelope. The CLI's blanket
  `except Exception` had masked the same gap. `conformance/test_accountable_owner.py`'s
  matching vector tightened from `(ValueError, StorageError)` to `ValidationError`
  specifically, now that every backend gets one consistent exception type here.
- **`POST /principals`'s `kind`/`auth_method`/`default_capability`/`trust_level` fields
  are now typed to match `Principal`'s own `Literal`/bounded constraints** instead of
  plain `str`/`int` — an invalid value (e.g. `kind="wizard"`, `trust_level=99`) previously
  skipped Pydantic's own validation and hit the same unmapped-pydantic-error class the
  `create_principal` fix above closed for `owner`, just via a sibling field instead.
  Found in review; closed without any `Ontology`-layer change.
- **`StorageBackend.get_credentials_for_principal` (SQLite + DuckDB) now tiebreaks on
  `id DESC` in addition to `created_at DESC`** — two credentials issued in the same
  timestamp tick previously had no deterministic order, so `POST /principals/{id}/tokens`
  recovering the just-issued credential's id via `list_tokens(...)[0]` could return the
  wrong one. A narrower residual race under genuinely concurrent issuance (not just a
  coarse timestamp) is tracked as KI-024.
- **MCP read tools now require authentication (closes KI-021):** `ontolith.schema`,
  `ontolith.get`, `ontolith.query`, and `ontolith.provenance` previously took no `token`
  parameter and resolved no principal at all, contradicting SPEC §8.3 ("`read`/`query`:
  required for any retrieval") — any MCP client could call them with zero credentials.
  All four now take `token: str`, resolved via the same `AuthProvider` `propose`/
  `flag_contradiction` already use, returning the same `auth_error` shape on failure.
  Read-only, information-disclosure severity; no write/capability-escalation impact.
  Documented as an update to ADR-0014.
- SQLite backend now opens its connection with `check_same_thread=False` — an ASGI
  server (the new REST interface) dispatches requests on a different OS thread than the
  one that constructs the backend, which stock `sqlite3` blocks regardless of whether
  the access is ever actually concurrent. This flag only lifts that check; concurrent
  access is now serialized separately (see KI-023 below), not by this flag
- **SQLite backend is now thread-safe under genuinely concurrent access (closes KI-023):**
  a `threading.RLock` now guards every `SQLiteBackend` method — `begin()` holds it for
  the full span of an explicit transaction; every other public method acquires it for
  its own call, reentrant on the same thread so calls made from inside a
  `transaction()` block don't self-deadlock. Previously, two genuinely concurrent
  requests (the exact shape an ASGI worker threadpool produces) could interleave
  `BEGIN` calls, raising a raw, unmapped `sqlite3.OperationalError` instead of the
  SPEC §16 error envelope. `commit()`/`rollback()` release the lock asymmetrically
  (commit only on success, rollback always) — an `ontolith-reviewer` pass on the first
  version of this fix caught that releasing unconditionally in both double-released the
  lock on a commit failure, masking the real `StorageError` behind a `RuntimeError` and
  leaving `_in_transaction` stuck; both the fix and a dedicated regression test for that
  failure mode are documented as an update to ADR-0010. New `ThreadPoolExecutor`-based
  regression coverage in `test_sqlite_backend.py` confirmed reproducing both failures
  against the respective pre-fix code before verifying each fix
- **HIGH:** `as_of(t)` excluded flagged assertions by current status instead of
  status-at-t; since flagging never sets `valid_to`, once any contradiction had ever
  touched a `(subject, predicate)`, `as_of(t)` returned nothing for it at any t, including
  times before the dispute existed. Fixed by recording a `flagged` event for the newly
  incoming assertion in a fresh contradiction (previously only the pre-existing member
  got one) and reconstructing flagged-status-at-t from `assertion_event` instead of
  trusting current status
- **HIGH:** `accept_proposal`/`reject_proposal` now reject a reviewer who is the
  proposal's own author or delegate (`acting_as`) — self-review, including via
  delegation chain, was previously possible for a misconfigured principal with review
  capability. (`resolve_contradiction` was incorrectly believed to share this guard at
  the time — it didn't, and wasn't fixed until KI-026, below.)
- `ontolith.provenance` (MCP) and `flag_contradiction` fetched and deserialized every
  assertion in the KB to find one or two rows by ID; both now use the indexed
  `get_assertion(id)` lookup
- `assertions()`'s composite index led with `namespace`, which no query filters on
  (single-namespace today), making it unusable — confirmed via `EXPLAIN QUERY PLAN` (full
  `SCAN`, not `SEARCH`). Added indexes matching the actual filter shapes in both backends
- **HIGH:** `resolve_contradiction()` had no self-resolution guard (KI-026, found in a
  whole-project audit) — a reviewer who authored one of a contradiction's disputed member
  assertions could pick their own value as the winner, unilaterally settling a dispute they
  were a party to. `accept_proposal`/`reject_proposal`/`request_changes` already blocked this
  via their shared self-review check; `resolve_contradiction` now does too, and — unlike the
  other three, which only check the specific action being taken — checks every member of the
  contradiction, not just the winner, since an interested party shouldn't get to pick against
  their own losing entry either. `docs/adr/ADR-0022-rest-write-review-admin.md` incorrectly
  claimed this guard already existed; corrected.

#### Documented
- SPEC §18 observability (metrics/events/structured logs) is scoped to M4, not built
  opportunistically ahead of it and not declared out of scope through 1.0 (closes KI-064,
  ADR-0044): `observe/` stays an empty package for now, but the Implementation Plan's M4 scope
  column — which never named observability at all — now does, and the architecture (a single
  `Clock`/`IdProvider`-style port, `govern/policy` still emits nothing itself) and a priority
  order (structured correlated logs, then the four named lifecycle events, then the seven-metric
  surface) are decided ahead of M4 so the milestone doesn't have to re-litigate them. No code
  changes — a scoping decision, not a feature.

#### Security

- `mkdocs-material`'s floor bumped `>=9.5` → `>=9.7.7` (closes KI-087, found in the pre-M4 deep +
  security audit) to move past CVE-2026-73295 (a DOM-based XSS in the optional `search.suggest`
  feature) — `pip-audit`'s unconditional CI step had been failing on every PR since this advisory
  landed. Dev-only, docs-build dependency; never ships in the `ontolith` wheel or any runtime
  extra. `uv lock` resolved `9.7.7` (one patch release past the floor); no upper bound added,
  matching KI-070's precedent of leaving the `dev` extra's tooling dependencies otherwise
  unbounded.
- MCP's `create_mcp_server()` gains an opt-in `require_header_token: bool = False` keyword-only
  parameter (closes KI-073, ADR-0014 update) — when set, an HTTP (SSE/streamable-HTTP) deployment
  can require the `Authorization` header outright instead of merely preferring it (KI-067): an
  absent header now fails the call the same way no credential at all would, even when the caller
  still supplies a valid `token` argument. `False` by default — the argument fallback KI-067 added
  is what keeps stdio transports (no header channel exists there) usable at all, so this is a
  strictly opt-in hardening for HTTP deployments, not a fix for a live vulnerability. A malformed
  header still fails closed either way, unchanged from KI-067.
- `pydantic`, `typer`, `python-ulid`, `fastapi`, `duckdb`, `uvicorn`, and `pyyaml` now all carry an
  upper version bound (closes KI-070) — `pydantic>=2.0,<3.0`, `typer>=0.9,<1.0`,
  `python-ulid>=2.0,<4.0`, `fastapi>=0.110,<1.0`, `duckdb>=1.0,<2.0`, `uvicorn>=0.27,<1.0` (both
  places it's declared — the `rest` and `graphql` extras each list it independently),
  `pyyaml>=6.0,<7.0`, extending KI-065's `<N.0` convention past the two packages that KI itself
  covered — a downstream `pip install ontolith`/any extra previously resolved whatever was newest
  for these seven at install time, unreviewed by this project. `pydantic`/`fastapi` in particular
  have a higher blast radius than either `strawberry-graphql`/`rdflib` (KI-065): `pydantic`
  underlies every domain model, `fastapi` sits on the same auth-bearing request path
  `strawberry-graphql`'s own `<1.0` bound was justified by. `fastapi`/`typer`/`uvicorn` are all
  long-lived pre-1.0 packages, same shape `strawberry-graphql` was in before KI-065's own floor
  bump — the `<1.0` bound guards against an eventual major release, not 0.x churn;
  `security.yml`'s weekly `pip-audit` remains the real backstop for that. `python-ulid`'s floor
  stayed at `2.0` (the lock resolves `3.1.0`) rather than being bumped to match, so its `<4.0`
  deliberately spans two majors instead of one — out of scope for this KI, which added upper
  bounds, not audited floors. The `dev` extra's ~20 tooling dependencies remain unbounded, also out
  of scope and lock-pinned in practice via the committed `uv.lock`. `uv lock` produced only an
  8-line lockfile metadata diff — no package's resolved version actually changed. ADR-0026 updated.
- MCP tools prefer an `Authorization: Bearer <token>` HTTP header over the `token` tool argument
  under the SSE/streamable-HTTP transports (closes KI-067, ADR-0014 update) — keeps a live
  credential out of the calling model's own context window and any MCP client's tool-call logging.
  `token` is now optional (`str | None = None`) on all 8 tools and remains the only channel on
  stdio, which has no HTTP request to carry a header on. A header that IS present but malformed
  (wrong scheme, blank value) fails the call closed rather than silently falling back to the
  argument.
- CI now runs `pip-audit`/`bandit`/`gitleaks`/SBOM generation (new `security.yml`, ADR-0026,
  closes KI-020) and a `griffe check` public-API breaking-change diff (informational pre-1.0, in
  `ci.yml`). Building the pass surfaced two real vulnerabilities, both fixed: `mcp` bumped to
  `>=1.28.1,<2.0` (PYSEC-2026-3483) and `sqlite-vec`'s pin bumped to `0.1.3` (PYSEC-2026-1938,
  `vec0` DELETE+INSERT workarounds from ADR-0020 re-verified against the new version).
- `cryptography` (a transitive dependency of `mcp` via `pyjwt[crypto]`, not directly declared)
  bumped 49.0.0 → 50.0.0 (`uv lock --upgrade-package cryptography`) to fix PYSEC-2026-3552 —
  disclosed after `security.yml`'s previous scheduled run, first caught failing `pip-audit` on
  `main` post-merge rather than on any feature PR's own diff. No `pyproject.toml` change (the
  version floor lives in the lockfile only). Also newly caught while `pip-audit` was blocking
  the same CI job from ever reaching its later steps: one genuine `bandit` B608 finding per
  backend on the `UNION ALL` relation-filter query `entities_where()` gained for KI-030 — same
  already-justified false-positive shape as the pre-existing vector-search `nosec`s (predicate/
  value are always parameter-bound; only a hardcoded-literal clause is interpolated), just never
  actually run locally against `bandit` until this pass. `security.yml`'s `bandit`/SBOM steps
  now run with `if: always()` so a `pip-audit` failure can no longer mask them again — the same
  masking is exactly how the two bandit findings went unseen across a full PR.
- `pip` (a transitive dependency of `pip-audit` itself, via `pip-api`) bumped 26.1.2 → 26.2.1
  (PYSEC-2026-3721/CVE-2026-13346) and `pymdown-extensions` (transitive via `mkdocs-material`/
  `mkdocstrings`) bumped 11.0 → 11.0.2 (PYSEC-2026-3654/CVE-2026-67422), both via `uv lock
  --upgrade-package` (closes KI-062, found in the M3 milestone-boundary security audit) — same
  lockfile-only shape as the `cryptography` bump above. Neither ships in the `ontolith` wheel or
  any runtime extra. No `pyproject.toml` change.
- `strawberry-graphql[fastapi]` and `rdflib` now carry an upper version bound (`<1.0`, `<8.0`
  respectively), matching `mcp`'s existing `<2.0` convention (closes KI-065) — previously
  unbounded, so a downstream `pip install ontolith[graphql]`/`ontolith[interop]` resolved
  whatever was newest at install time, unreviewed by this project. No version actually changed;
  both were already resolving within the new bounds. `<1.0` is a weaker guarantee for
  `strawberry-graphql` than `<2.0` is for `mcp`: it's still pre-1.0, and a *minor* release already
  broke this integration once (the `>=0.316` floor bump above), so the bound guards against the
  next major only, not the next 0.x break.
- **SQLite:** `assertion_event`/`proposal_event` now reject raw `UPDATE`/`DELETE`/`INSERT OR
  REPLACE` at the database layer via six triggers (closes KI-066, ADR-0041), making SPEC §17's
  "the audit trail MUST NOT be mutable" a store-level guarantee rather than a port-surface
  convention alone (previously, only `StorageBackend` exposing no update/delete method stood
  between the audit tables and any code holding the raw connection). **DuckDB gets no equivalent
  fix** — verified DuckDB (1.5.4) has no `CREATE TRIGGER` support and no connection-level access
  restriction to work around that; documented as a currently-unfixable backend asymmetry rather
  than left unaddressed. Two real bypasses found in two review rounds and closed before merge:
  (1) `INSERT OR REPLACE`'s implicit conflict-row delete doesn't fire a `BEFORE DELETE` trigger
  unless `PRAGMA recursive_triggers` is ON (SQLite defaults it OFF) — could otherwise silently
  rewrite an existing audit row, including its `actor` field; (2) that pragma is per-*connection*,
  not persisted in the database file, so a second raw connection to the same file revived the
  bypass regardless — closed durably with a third, schema-persisted `BEFORE INSERT ... WHEN
  EXISTS(...)` trigger per table, which needs no pragma at all.

### Security & Correctness Remediation (2026-07-06 – 2026-07-09)

A project audit (`deep-reviewer` + `security-reviewer`) found a chained CRITICAL
governance bypass — an untrusted AI agent could spoof a trusted owner via delegation,
force a `static` fact to be silently superseded instead of contradicted, and auto-accept
the resulting write — plus several HIGH/MEDIUM findings. All were fixed across five PRs;
a follow-up re-audit against the merged fixes then found two of the fixes had introduced
new HIGH regressions, closed in a sixth PR.

#### Fixed
- **CRITICAL:** conflict-routing temporality is now resolved from the active schema
  (`SchemaIR.temporality_of`), not accepted as a caller-supplied `propose()` argument —
  closes the bypass letting a `static` fact be silently superseded instead of raising a
  contradiction (SPEC §10)
- **CRITICAL:** MCP callers are now authenticated via per-principal API-key tokens
  (ADR-0014) — the acting principal is resolved from a verified bearer token, never a
  caller-asserted `author` string
- **HIGH:** delegation now uses `min(capability(author), capability(acting_as))` per
  SPEC §8.4 instead of substituting the delegating principal's full capability; an AI
  principal's own `kind` is checked before any capability math, so it can never reach
  `AutoAccept` by naming a trusted owner
- **HIGH:** direct writes (`assert_literal`/`assert_ref`) now route through SPEC §10
  conflict routing and require `write`/`admin` capability; AI-kind principals are
  hard-blocked from this path regardless of misconfigured capability (ADR-0003)
- **HIGH:** relations now have a governed proposal path (`propose_ref`), mirroring
  `propose()` for literals
- **HIGH:** `flag_contradiction` now enforces a capability gate (`>= propose`) — it was
  previously reachable by any principal, including read-only, with no check at all
- **HIGH:** `as_of()` now closes `valid_to` on retraction and excludes `flagged`
  assertions by default (`include_flagged=True` to opt in)
- **HIGH:** `retract()` no longer widens an already-closed `valid_to` — retracting an
  already-superseded assertion previously reopened its validity window, corrupting
  bitemporal reconstruction
- **HIGH:** `accept_proposal` now preserves `acting_as` (delegation provenance) when
  replaying a proposal's operations — it was previously dropped on the review-accept
  path, the primary path for AI-delegated proposals since AI proposals always require
  review
- **MEDIUM:** AI-authored assertions now require and capture `model` provenance
  (SPEC §7.4/§14.4); `propose()`/`propose_ref()` raise `ValidationError` for an AI
  author with no `model`
- **MEDIUM:** `accept_proposal` now re-resolves temporality from the current schema at
  apply time instead of trusting a snapshot taken at propose time, closing a narrow
  schema-migration side door back into the original temporality-spoofing issue
- **MEDIUM:** `issue_token`/`revoke_token`/`list_tokens` now require the calling
  principal to hold `admin` capability — previously ungated, so anything with backend
  access could mint a bearer credential for any principal
- **MEDIUM:** SQLite backend now enables WAL journal mode (SPEC §12.1 MUST)
- **MEDIUM:** an AI principal's `owner` must now resolve to an existing human/service
  principal (FOREIGN KEY constraint + application-layer check) rather than merely being
  a non-null string

#### Added
- Structured `proposal_event` log for accept/reject review actions (SPEC §9.4), so
  `policy_reason` (set by the policy engine at proposal-creation time) is no longer
  overwritten by the reviewer's free-text reason
- Structured, append-only `assertion_event` log covering every status mutation
  (supersession, flagging, retraction, contradiction-resolution reactivation), each
  independently attributable and timestamped
- `Contradiction.raised_by` — records the principal who raised each contradiction,
  whether auto-detected during conflict routing or explicitly flagged
- `StorageBackend.get_assertion(id)` — single-assertion lookup by ID, regardless of
  status
- ADR-0014: MCP authentication model (per-principal API-key tokens)
- `docs/known-issues.md` KI-014: plugin capability isolation is tracked as a required
  gate before plugin discovery/loading is ever enabled — no plugin loader exists yet to
  secure, so a sandbox was deliberately not built speculatively ahead of that need

#### Changed
- **Breaking:** `propose()` no longer accepts a `temporality` parameter — it is always
  resolved from the schema
- **Breaking:** `assert_literal`/`assert_ref` now require `write` or `admin` capability
  and reject AI-kind principals outright, even if misconfigured with elevated capability
- **Breaking:** MCP's `propose`/`flag_contradiction` tools take a bearer `token`
  parameter instead of a caller-supplied `author` ID

### M2 - Collaboration (0.2) (Complete)

#### Added
- Review workflow: `accept_proposal`/`reject_proposal` for `require_review` proposals
- Bitemporal time-travel via `Ontology.as_of(t)`
- SPEC §10 conflict routing: temporal supersession for `time_varying` properties,
  contradiction flagging for `static` properties
- `resolve_contradiction()` for reviewer-driven contradiction resolution (SPEC §10.3)
- MCP server exposing `schema`/`get`/`query`/`provenance`/`propose`/`flag_contradiction`
  tools, with no direct-write tool (ADR-0008), test-enforced
- Trust levels and `acting_as` delegation (ADR-0003)
- Hypothesis property tests for conflict routing and the append-only invariant

#### Fixed
- `Reject` decision is now actually produced by `ThresholdPolicy` for insufficient
  capability instead of silently falling through
- MCP `provenance`/`flag_contradiction` tools could not resolve non-active
  (retracted/superseded/flagged) assertions, defeating their primary audit-trail use case

#### Documented
- Confidence-based auto-accept for AI proposals is a deliberate design decision, not a
  gap: AI principals always require review regardless of confidence or trust level

### M1 - Substrate (0.1 MVP) (Complete)

#### Added
- Meta-model + IR and the class-based schema DSL (first front-end; LinkML YAML followed
  in M3)
- SQLite storage backend (default adapter) with append-only entity/assertion tables
- Append-only `Assertion` model with full provenance (author, source, confidence,
  rationale, model, bitemporal fields)
- Identity basics: principals (`human`/`ai`/`service`), capability levels
  (`read < propose < write < review < admin`), AI accountable-owner requirement
- `propose()` → `ThresholdPolicy` evaluation → auto-accept/require-review/reject
- Basic query builder, Python SDK, CLI

### M0 - Foundations (Complete)

#### Added
- Repository structure and build configuration
- Core ports: Clock and IdProvider for deterministic behavior
- Error taxonomy with stable error codes
- Architecture Decision Records (ADR-0001 through ADR-0008)
- GitHub Actions CI workflow
- Contribution guidelines and community health files
- import-linter configuration for dependency rule enforcement

[Unreleased]: https://github.com/ontolith/ontolith/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ontolith/ontolith/releases/tag/v1.0.0
