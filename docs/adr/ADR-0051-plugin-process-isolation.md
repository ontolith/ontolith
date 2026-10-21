# ADR-0051: Plugin Process Isolation

**Status:** Accepted

**Date:** 2026-09-19

**Deciders:** Ontolith Core Team

## Context

ADR-0015 gave plugins a *governance-correctness* boundary: `PluginRegistry.register()` loads a
plugin behind a capability-scoped view (`ReadOnlyView`/`WriteView`), so admin-only methods and the
direct-write bypass are structurally absent from what a plugin can reach. It explicitly did **not**
close `docs/known-issues.md` KI-014's other half: `capabilities.network`/`.filesystem` are declared
in the manifest but never enforced. A plugin still runs fully in-process, sharing the same Python
interpreter, memory space, and OS process as the host application — nothing stops it from
`import requests`-ing out to the network or `open()`-ing an arbitrary file, regardless of what its
manifest declares. ADR-0015 named this explicitly: "Python has no true encapsulation... this design
closes the *structural/accidental* bypass... it does **not** close a plugin author who deliberately
writes code to defeat the convention. That gap closes only with process/wasm isolation."

The Implementation Plan (§8) phases this as "process isolation in v1, wasm/subprocess hardening +
signed registry plugins by 1.0." M4 is that 1.0 milestone. Four real reference plugins
(`CsvImporter`, `JsonExporter`, `RdfExporter`, `RequiredFieldsValidator`) now ship against the
`Importer`/`Exporter`/`Validator` protocols, so — unlike when ADR-0015 rejected building this
"before any real plugin exists to run in it" — there is now real, exercised surface to isolate.

The user chose to build real process isolation now (not an ADR-only scoping pass, not a lighter
in-process-only mitigation), so this ADR records the design decided as part of building it, per the
project's own "record a decision before implementing it" convention — this is a single PR that
ships both.

## Decision

**Each plugin's protocol entrypoint (`import_`/`export`/`derive`/`validate`/`sync`) runs in a
freshly spawned child process, with its `kb` view (and any other live object it's handed)
proxied back to the parent over IPC. Network/filesystem denial is enforced at the OS syscall level
on Linux (via seccomp); on platforms without that primitive, the process/IPC boundary is the only
enforcement, honestly documented as such.**

### Process and IPC model (`src/ontolith/plugins/sandbox/`)

- **One child process per call, not per plugin lifetime.** `multiprocessing.get_context("spawn")`
  is used unconditionally (not `fork`) — `spawn` behaves identically on Linux/macOS/Windows (all
  three are in this project's CI matrix), unlike `fork`, which is POSIX-only and unsafe to mix with
  the threads/locks `Ontology`'s own concurrency story (KI-023/KI-084) already relies on. A fresh
  process per call means no plugin state persists between calls (each entrypoint call reconstructs
  the plugin instance from scratch, same as a fresh `plugin_class()` per invocation) and no partially
  -compromised process lingers to serve a second call — the more conservative, defense-in-depth
  choice over a long-lived worker process, and the reference plugins are already effectively
  stateless per-call (the one plugin with constructor state, `RequiredFieldsValidator`, is only ever
  constructed no-argument by the registry, per its own docstring).
- **The plugin is reconstructed inside the child from the same `entry_point_name` string the
  registry already resolved once in the parent** (for the existing manifest/construction
  validation `PluginRegistry.register()` already performs, unchanged by this ADR) — not by pickling
  the parent's already-constructed instance across the boundary. This means the untrusted plugin's
  own `__init__` runs a second time, once in each process; the parent's copy exists only to validate
  early (preserving `register()`'s existing error-timing contract) and is discarded, never invoked
  again.
- **Exactly one duplex `multiprocessing.Pipe()` per call**, not one channel per proxied object.
  Every message is tagged (`"kb"` for the view argument, `"arg_<position>"` for any other live
  object needing a callback) so a single pipe can carry interleaved requests for more than one
  remote object — in practice at most a `kb` view and one writable target, per the Protocol shapes
  in `plugins/ports.py`. The exchange is fully synchronous: the child blocks on each proxied call
  until it gets a reply, so there is never more than one in-flight request, and no reason for a more
  elaborate multiplexed transport.
- **Object detection is by structural type, not argument position or keyword name.** Any argument
  (positional or keyword) that is a `ReadOnlyView`/`WriteView` instance is always proxied, never
  pickled — this works uniformly across `Importer.import_(source, kb)`, `Exporter.export(kb,
  target)`, `Reasoner.derive(kb)`, `Validator.validate(assertion, kb)`, and `Connector.sync(kb)`
  without hardcoding each protocol's argument order. Any other argument is pickled directly if
  possible (covers the paths/strings/dicts/lists every reference plugin actually passes); if pickling
  fails **and** the value exposes a callable `.write` attribute, it is wrapped in a remote-writable
  proxy instead (covers `JsonExporter`/`RdfExporter`'s documented `io.StringIO`-as-`target` support);
  otherwise the caller gets a clear `UnwirableArgumentError` (a `TypeError` subclass) at call time,
  before a child process is even spawned, naming the argument type that can't cross the boundary.
- **`RemoteReadOnlyView`/`RemoteWriteView`** (`sandbox/remote_view.py`) mirror `ReadOnlyView`/
  `WriteView`'s public method set (including `principal_id`, a plain string carried on the wire
  marker so no round trip is needed for it) and the *relationship between the two proxy classes*
  (`RemoteWriteView` subclasses `RemoteReadOnlyView`, mirroring `WriteView`/`ReadOnlyView`'s own
  shape) — they do **not** subclass the real `ReadOnlyView`/`WriteView` themselves, so
  `isinstance(kb, WriteView)` against the real class is `False` inside a sandboxed call, even for a
  writable registration (`isinstance(kb, RemoteWriteView)` is the isolated-call equivalent). Every
  method body sends one request over the pipe and blocks for the reply instead of calling `Ontology`
  directly — nothing else. `.query()`/
  `.as_of()` are **not supported in an isolated call** in this pass: both return fluent builder
  objects (`QueryBuilder`/`AsOfView`) that hold a live backend reference and support chained calls,
  which would need their own remote-proxy protocol; no shipped reference plugin calls either method,
  so building that proxy now would be speculative. Calling `kb.query(...)`/`kb.as_of(...)` from
  inside an isolated plugin raises a clear `PluginError` naming this limitation, tracked as this
  ADR's own follow-up (see Consequences), not silently broken.
- **Exceptions cross the boundary pickled directly wherever possible.** `BaseException`'s own
  `__reduce__` includes `__dict__` as reconstruction state, not just `self.args` — so an
  `OntolithError` subclass (`core/errors.py`) round-trips through a plain pickle correctly with no
  special-casing needed: `message` and `detail` both come back exactly as they went in, and so does
  the concrete exception type (verified directly, not assumed — see `sandbox/wire.py`'s tests). A
  non-`OntolithError` exception a plugin's own code raises is pickled the same way (covers ordinary
  `ValueError`/`TypeError`/etc.); if a third-party exception type isn't picklable at all, it
  degrades to a plain `RuntimeError` carrying the original type name and message —
  documented as a known, narrow fidelity loss for that one case, not a silent one (the message
  always survives; only the exact exception *type* doesn't, when it can't).

### Enforcement (`sandbox/enforcement.py`)

- **Linux: real, OS-level denial via seccomp** (`pyseccomp`, an optional dependency gated by the
  `sys_platform == "linux"` environment marker in `pyproject.toml`, so it never installs on
  macOS/Windows). Immediately before the child calls into the plugin's actual entrypoint method —
  after every import the runner infrastructure itself needs, since Python's own import machinery
  does filesystem reads that must not be blocked — a `seccomp.SyscallFilter(defaction=ALLOW)` is
  built and loaded denying (via `ERRNO(EPERM)`, not `KILL_PROCESS`) the network syscall group
  (`socket`, `connect`, `bind`, `listen`, `accept`/`accept4`, `send*`, `recv*`, `getsockopt`,
  `setsockopt`, `shutdown`) when `capabilities.network` is `False`, and the filesystem-mutation
  syscall group (`open`, `openat`, `openat2`, `creat`, `unlink*`, `rename*`, `mkdir*`, `rmdir`,
  `chmod*`, `chown*`, `truncate*`, `link*`, `symlink*`) when `capabilities.filesystem` is `False`.
  `ERRNO(EPERM)` rather than killing the process means a denied syscall surfaces as an ordinary
  Python `OSError`/`PermissionError` at the plugin's own call site — caught and propagated through
  the same exception-wire path as any other plugin-raised exception, not a special "process died"
  case the parent has to detect via a closed pipe. Already-open file descriptors (stdio, the IPC
  pipe itself) are unaffected — the filter blocks *new* filesystem access, not continued use of
  what's already open, so the RPC protocol itself keeps working under a `filesystem=False` filter.
- **macOS/Windows: no OS-level enforcement in this pass.** `sandbox-exec` (macOS) and Windows job
  objects/AppContainers are real primitives that could eventually close this the same way, but each
  is a materially different, platform-specific mechanism from seccomp, unverifiable in this
  project's own development environment, and every one of the three OSes in this project's CI
  matrix (`ubuntu-latest`/`macos-latest`/`windows-latest`) would need its own tested path before
  this ADR could honestly claim it — building two more platform-specific sandboxes speculatively,
  without a way to verify either actually restricts anything, would be worse than shipping the one
  that's real and saying so. On these platforms, a plugin's declared `network=False`/
  `filesystem=False` is still not enforced — the process/IPC boundary (no direct object reachability,
  crash isolation) is the only real improvement there, same class of gap ADR-0015 already
  documented, just now qualified to "on this platform" instead of "at all." `PluginRegistry`'s
  existing registration-time `WARNING` (ADR-0015's 2026-08-30 update) is reworded to say exactly
  this, rather than imply nothing has changed.
- **Enforcement failing to install degrades to the same "not enforced, loudly said so" state, never
  silently.** `pyseccomp`'s import can fail two ways: the package isn't installed at all
  (`ImportError`, expected on macOS/Windows since the dependency is platform-gated) or it's
  installed but `libseccomp.so` isn't present on the host (`RuntimeError`, raised by `pyseccomp`
  itself at import time) — CI's `ubuntu-latest` job installs `libseccomp2` via `apt-get` specifically
  so this path is exercised for real there, not merely imported successfully. Either failure is
  caught once, produces the same "no OS-level enforcement available" result as running on a
  non-Linux platform, and is logged through `kb.observability` (ADR-0044) at `WARNING`, not raised —
  a plugin that declared `network=False` should still run, just without the stronger guarantee, the
  same posture ADR-0015 already established for "declared but not enforced."

### `PluginRegistry` surface

- **`register()` gains `isolate: bool = True`.** Deny-by-default extends here the same way it
  already governs default temporality, MCP's read/propose/flag-only default, and capability floors
  defaulting to `propose`: a plugin registered with no explicit opt-out runs isolated. `isolate=False`
  is the documented escape hatch for a plugin whose `source`/`target` genuinely can't cross a process
  boundary at all (an unpicklable, non-`.write`-shaped live object) or for a fully trusted first-party
  plugin where the overhead of a subprocess per call isn't worth paying — it restores exactly today's
  pre-ADR-0051 behavior, unchanged.
- **`LoadedPlugin.instance` becomes an `IsolatedPluginProxy`** when `isolate=True` — a thin object
  exposing only the one protocol method `manifest.kind` implies (`import_`/`export`/`derive`/
  `validate`/`sync`), matching `plugins/ports.py`'s Protocols exactly. Calling it runs the full
  spawn-proxy-collect-result cycle above and returns/raises exactly what the real, unsandboxed call
  would have. `isinstance(loaded.instance, CsvImporter)`-style checks (a few existed in tests before
  this ADR) no longer hold for an isolated registration — expected, since `loaded.instance` is no
  longer the real object at all; `IsolatedPluginProxy.plugin_class` exposes the class being wrapped
  for exactly this kind of identity check.
- **Everything before that point in `register()` — entry-point resolution, manifest validation,
  capability negotiation, principal creation, the existing unenforced-capability warning, admin-event
  recording — is unchanged.** This ADR only replaces what `LoadedPlugin.instance` *is* and how calling
  its one method behaves; it does not touch how a plugin gets discovered, validated, or granted a
  capability.

## Rationale

**Why a fresh subprocess per call instead of a long-lived worker:** the reference plugins carry no
meaningful state between calls (each entrypoint call is a complete, self-contained unit of work —
import this file, export this snapshot, validate this assertion), so there's no efficiency loss
worth trading away the security benefit of never letting a second call reuse a process a first call
already ran untrusted code in.

**Why type-based object detection instead of one proxy-construction path per plugin kind:** the five
Protocols in `plugins/ports.py` don't agree on where `kb` sits in the argument list (`Exporter`
takes it first, `Importer` takes it second) — hardcoding a position per kind would work today but
silently break the moment a future Protocol's `kb` isn't in the position assumed, with no type
error to catch it (both positions type as `object` from the wire's perspective). Detecting `kb` by
`isinstance(value, ReadOnlyView)` instead makes the marshalling code Protocol-shape-agnostic: it
would keep working correctly even for a new plugin kind this ADR didn't anticipate, as long as that
kind still hands a `ReadOnlyView`/`WriteView` to the plugin somewhere in its call.

**Why seccomp specifically, and why only on Linux:** seccomp-bpf is a mature, well-audited Linux
kernel primitive purpose-built for exactly this ("restrict which syscalls a process may make"),
usable by an unprivileged process (`PR_SET_NO_NEW_PRIVS`, which `pyseccomp`/libseccomp already
handle internally), and testable in this project's own `ubuntu-latest` CI job. macOS's nearest
equivalent, `sandbox-exec`, is an Apple-private, undocumented, and repeatedly-flagged-for-eventual-
removal mechanism with no first-party Python binding this project could adopt with the same
confidence; Windows has no directly comparable per-syscall primitive at all (job objects and
AppContainers restrict different things — processes, tokens, resource limits — not arbitrary syscall
denial). Shipping a real mechanism on the one platform where one exists, rather than a best-effort
approximation on all three that this project has no way to verify actually restricts anything, is
the more honest choice — matching this ADR's own "say so, don't imply a guarantee that isn't there"
stance, the same one ADR-0015 already took for the gap this ADR partially closes.

**Why `ERRNO(EPERM)` instead of `KILL_PROCESS` as the seccomp deny action:** a killed process turns
every denied syscall into an abrupt pipe closure the parent has to detect (`EOFError` on `recv()`)
and translate into some generic "the plugin's process died" error, losing the specific syscall/
capability that triggered it. `ERRNO(EPERM)` instead surfaces as an ordinary Python exception
(`PermissionError`/`OSError`) at the exact call the plugin's own code made, which the sandbox's
already-built exception-wire path (needed regardless, for a plugin's own `ValidationError`/
`ValueError`/etc.) propagates back to the parent with no separate handling required — one mechanism
covers both "the plugin raised on purpose" and "the sandbox denied a syscall," rather than two.

## Consequences

**Positive:**
- ✅ Closes the "Python has no true encapsulation" gap ADR-0015 named as unclosed **for the isolated
  call itself**: a plugin's own protocol-method code, running in a separate OS process with only a
  picklable-message IPC channel to the parent, cannot reach `view._kb` or any other live object
  graph at all during that call — there is no longer an object to reach past, on any platform,
  regardless of seccomp availability. This is real, structural value independent of the
  network/filesystem enforcement question. (Security review, M4 Workstream 7: scoped this bullet's
  claim explicitly to the isolated call, not the plugin's whole lifecycle — the registration-time
  module import/constructor, covered by this section's own Negative bullet below, runs unsandboxed
  in the parent process and is not protected by anything this bullet describes; an earlier version
  of this bullet's unqualified "at all… regardless" wording read as covering that phase too, which
  was never true.)
- ✅ On Linux, `capabilities.network=False`/`capabilities.filesystem=False` are now genuinely
  enforced at the OS syscall level, closing SPEC §17's "MUST deny undeclared access" for the
  platform this project can actually verify it on.
- ✅ A plugin crash (segfault, unhandled fatal error, resource exhaustion) can no longer take down
  the host process — it's a different OS process now.

**Negative:**
- ⚠️ **macOS/Windows get no OS-level network/filesystem enforcement in this pass** — the same
  "declared but not enforced" gap ADR-0015 left open, now qualified by platform rather than closed
  everywhere. Tracked as this ADR's own follow-up (see below), not re-opening KI-014 from scratch —
  the Linux half is genuinely resolved.
- ⚠️ **`.query()`/`.as_of()` are unavailable from inside an isolated plugin call** — a real, if
  currently unused, capability regression for a hypothetical future plugin wanting the fluent query
  builder rather than `assertions()`'s flat filter. Filed as a follow-up (see below).
- ⚠️ **A subprocess-per-call has real, if modest, latency and resource cost** (process spawn,
  pickling overhead) compared to a direct in-process call — not measured against this project's SPEC
  §9 performance budgets, since none of those budgets name plugin invocation; `isolate=False` is the
  documented opt-out for a caller who has already decided this overhead isn't worth paying for a
  specific, trusted registration.
- ⚠️ **A plugin's module import and constructor still run once, unsandboxed, in the parent process**
  (unchanged from ADR-0015/`register()`'s existing behavior — `entry_point.load()` imports the
  plugin's module, then `plugin_class()` constructs it) purely to validate the manifest and catch a
  broken `__init__` early, before the security-relevant boundary this ADR adds even applies —
  arbitrary module-level code, not just the constructor, runs with full parent privileges at this
  point (round-2 review finding: naming only "constructor" here understated the exposure by one
  step). An operator registering a plugin at all is already an explicit trust decision (`register()`
  requires `admin` capability, unchanged), so this is the same trust boundary ADR-0015 already relied
  on, not a new one this ADR introduces — but it is, honestly, the single largest remaining
  unsandboxed surface in this design.
- ⚠️ **A third-party exception type that isn't picklable loses its exact type crossing the
  boundary** (degrades to a plain `RuntimeError` carrying the original name and message) — documented
  above, a narrow fidelity loss versus `isolate=False`'s exact-type-preserving behavior.

## Alternatives Considered

**A long-lived worker process per registered plugin, reused across calls:** rejected — no shipped
plugin needs cross-call state, and a persistent worker means a single compromised or buggy call
taints every subsequent call to the same plugin for the lifetime of the registration, not just the
one call that went wrong.

**WASM-based plugin execution** (compiling plugins to WASM, run under a WASM runtime like
`wasmtime`): rejected for this pass, matching the Implementation Plan's own phasing ("process
isolation... wasm/subprocess hardening... by 1.0" as two distinct, sequenced items, not one). WASM
would require every plugin author to compile to WASM instead of writing ordinary Python against
`plugins/ports.py`'s Protocols — a fundamentally different plugin-authoring model than the one all
four shipped reference plugins and this project's entry-point-based discovery already assume.
Building it now would mean redesigning plugin authorship from scratch, not hardening the existing
one; tracked as a distinct, later follow-up per the Implementation Plan's own sequencing, not
folded into "process isolation."

**A capability-restricted `StorageBackend`/socket-module monkeypatch instead of OS-level
enforcement:** rejected for the same reason ADR-0015 rejected a `StorageBackend` wrapper for the
storage half — an in-process Python-level restriction (replacing `socket.socket`, `builtins.open`,
etc. inside the child) can be trivially defeated by a deliberately malicious plugin (`ctypes`,
re-importing the original via `importlib.reload` on a saved reference, a compiled extension module
that doesn't go through Python's `socket`/`open` at all) — it would raise the bar for a careless
plugin without closing the gap ADR-0015 already named as the more consequential one: a plugin author
who deliberately writes code to defeat the convention. Only a kernel-enforced boundary (seccomp) or
a genuine process/VM boundary actually closes that.

**Signed registry plugins:** out of scope for this ADR, per the Implementation Plan's own phasing
("wasm/subprocess hardening **+ signed registry plugins**" as one bundled 1.0 item, both still
follow-ups here) — this ADR's isolation model is orthogonal to whether a plugin's source is verified
before it's ever loaded; signing addresses a different threat (a tampered or spoofed plugin
distribution), not what a legitimately-installed plugin's own code can do once it runs.

## Update (2026-09-20): review found two CRITICAL bypasses of the process boundary itself — fixed

Two independent review rounds (architecture + a dedicated security review) both found, independently,
that the first version of this ADR's own IPC design gave a malicious plugin a **better** path to the
parent than the in-process design it replaced — directly contradicting the "no object graph left to
reach past" claim this ADR originally made. Both are fixed; the claim is accurate now, verified by
direct reproduction of both attacks against the fixed code (not merely re-reading the fix).

**CRITICAL-1 — the dispatch loop unpickled attacker-controlled bytes, which is arbitrary code
execution.** `multiprocessing.Connection.recv()` is `pickle.loads()`; the child process is untrusted
by this ADR's own threat model; therefore a plugin's own `DONE`/`FAILED`/`CALL` message — including
its own return value, its own raised exception, or any argument it passes through a proxied view
call — could carry an object whose `__reduce__` calls an arbitrary importable callable (e.g.
`os.system`) the instant `.loads()` runs, before any of this project's own code executes. Reproduced:
a plugin returning an object with a hostile `__reduce__` ran a shell command in the **parent**
process. Fixed two ways, together: (1) `sandbox/wire.py`'s `RestrictedUnpickler`/`restricted_loads`
— the parent now reads every message from the child via `recv_bytes()` and a custom `Unpickler`
whose `find_class` refuses to construct any class instance not on an explicit allowlist (every
`OntolithError` subclass plus a short list of ordinary builtin exceptions); every other value must
arrive as one of `None`/`bool`/`int`/`float`/`str`/`bytes`/`list`/`dict`/`tuple`, which pickle's own
opcodes handle without ever calling `find_class` at all. (2) `wire.to_wire_result` — a plugin's own
return value is reduced to that same JSON-safe shape before it's ever sent (a `@dataclass` becomes a
plain `dict`, each field value recursively reduced the same way — deliberately not
`dataclasses.asdict()`, which would `deepcopy` a non-dataclass field value unrestricted and could
defeat this same guarantee), so the common case (a reference plugin's `ImportReport`-shaped return
value) never needs the restricted unpickler to reconstruct an arbitrary class at all.
**This means a plugin's dataclass-shaped return value now crosses an isolated call as a `dict`, not
its original type — `isolate=False` still returns the real object unchanged.**

**CRITICAL-2 — the dispatch loop invoked any method name the child asked for, on the real,
unproxied view object.** `getattr(real_view, call_method_name)(*call_args, **call_kwargs)` had no
allow-list; `call_method_name` came directly from the child's own `CALL` message. Reproduced: a
plugin registered with `ReadOnlyView`-only, `write`-incapable access called
`kb._call("__setattr__", "_principal_id", "admin@example.com")` on its own proxy, which forwarded
verbatim to `__setattr__` on the parent's real, live view object — successfully forging the acting
principal on every subsequent write through that view (the view object is long-lived on
`LoadedPlugin.view`, so the poisoning outlives the one call). No pickle trickery needed — plain
strings. Fixed: `runner.py`'s `_allowed_methods_for` is now an explicit, closed allow-list
(`get_entity`/`schema`/`assertions`/`create_entity`/`propose`/`propose_ref`/`retract`/`write` —
never a dunder, never `query()`/`as_of()`, which would hand back a live `QueryBuilder`/`AsOfView`
holding a backend reference), consulted before `getattr` ever runs. `RestrictedUnpickler` alone does
**not** close this: `__setattr__` and its arguments are all safe, ordinary strings.

**Other fixes from the same two review rounds, smaller but real:**
- **Call-time enforcement failures were silently discarded.** The registration-time warning
  (`registry.py`) predicts whether Linux+seccomp *should* work; it can't guarantee the per-call
  attempt actually succeeds (a container's own outer seccomp profile, say). The child now reports
  its `EnforcementResult` to the parent unconditionally (a new `ENFORCEMENT` protocol message,
  before the plugin's own method runs), and the parent logs a `WARNING` through `kb.observability`
  whenever it didn't apply — `IsolatedPluginProxy`/`run_isolated` now take an `observability`
  parameter for this.
- **The registration-time warning logic was checking the wrong condition.** It only fired when a
  capability was declared `True` (an *allow*, never restricted by seccomp on any platform — the
  original, correct ADR-0015 concern) — but suppressed itself whenever Linux enforcement was
  predicted available, which has no bearing on the `True` case at all. Worse, it never warned about
  a capability declared `False` (the default) when *that* declaration wouldn't actually be enforced
  either (macOS/Windows, or `isolate=False`) — exactly the silent-false-sense-of-enforcement case
  this warning exists to prevent. `registry.py`'s `_warn_if_unenforced_capabilities_requested` now
  checks both conditions independently and can log up to two distinct warnings per registration.
- **`_child_main`'s independent entry-point resolution had drifted from `PluginRegistry`'s.** It
  silently picked the first match on an ambiguous entry-point name instead of refusing, unlike
  `register()`'s own check. Now mirrors `_load_entry_point`'s error handling exactly.
- **`RemoteReadOnlyView`/`RemoteWriteView` were missing `principal_id`** (a real `ReadOnlyView`
  public property) and did not actually subclass the real `ReadOnlyView`/`WriteView` despite this
  ADR's original text claiming `isinstance(kb, WriteView)` "still holds" inside a sandboxed call —
  it doesn't; only `isinstance(kb, RemoteWriteView)` does. `principal_id` now crosses via the wire
  marker (a plain string, no round trip needed); the `isinstance` claim above is corrected.
- **The seccomp syscall deny-lists had real gaps**, per the security review: `io_uring_setup`/
  `_enter`/`_register` (the standard bypass — submits socket/openat/send/recv-shaped operations via
  kernel worker threads, never issuing the syscalls seccomp filters individually), `ptrace`/
  `process_vm_readv`/`process_vm_writev` (a direct route into the parent's memory on a host that
  hasn't hardened `ptrace_scope`, unrelated to network/filesystem but squarely inside "reach past the
  process boundary"), several filesystem syscalls (`fchmod`/`fchown`/`lchown`/`mknod`/`mknodat`/
  `fallocate`/`utimensat`/the `*xattr` family/`open_by_handle_at`/the newer mount API), and
  non-native-architecture syscalls (a 32-bit compat or x32 syscall on x86_64 matches no rule under a
  filter that only registered the native architecture, falling through to `ALLOW`). All added;
  `ptrace`/`process_vm_*` are denied unconditionally whenever any restriction is installed, not
  gated on which capability was declared `False`.
- **`pyseccomp`'s version range was loosely pinned** (`>=0.1,<1.0`) for a thin, infrequently-updated
  dependency that is the entire security boundary for network/filesystem denial — exact-pinned now
  (`==0.1.2`), matching `sqlite-vec`'s own precedent and stated reasoning in `pyproject.toml`.
- **A validator loaded via `Ontology`'s own `validators`/`completeness_validators` constructor
  parameters cannot be an `IsolatedPluginProxy`** — that path passes the trusted `Ontology` itself as
  `kb` (per ADR-0029, always unsandboxed, unrelated to `PluginRegistry`), which isn't a
  `ReadOnlyView` and isn't picklable, so it can't cross the sandbox boundary at all. This was never a
  supported combination and remains none — `plugins/ports.py`'s `ValidatorKbView` docstring now says
  so explicitly. Calling a `PluginRegistry`-loaded validator the same way every other plugin kind is
  called (`loaded.instance.validate(assertion, loaded.view)`) works correctly and is covered by a
  test — verified directly, not assumed.
- `UnwirableArgumentError` (a `TypeError` subclass) is what a caller actually gets for an argument
  that can't cross the boundary, raised at call time — this ADR's Decision section previously named
  a different exception type at a different time, both wrong; corrected.

**Round-2 review — both CRITICAL fixes independently re-verified via 12+16 reproduction variants
(hostile-pickle payloads and dispatch-escalation attempts, respectively, plus a full end-to-end run
with a real malicious plugin under a real entry point) — both hold, no third bypass of the process
boundary found. Six smaller issues found and fixed in the same round:**
- `wire_exception` checked the exception's full **MRO** against the allowlist, not its concrete
  class — since every exception's MRO includes `("builtins", "Exception")` (itself allowlisted), the
  check always passed regardless of the concrete type, so the documented `RuntimeError` fallback
  never fired for a picklable-but-not-allowlisted exception (e.g. a plugin-defined `MyValueError`).
  The parent's `restricted_loads` then refused the concrete class outright, destroying the
  exception's message entirely instead of degrading gracefully. Fixed: checks
  `(type(exc).__module__, type(exc).__name__)` — the concrete class — matching
  `RestrictedUnpickler.find_class`'s own check exactly.
- A malformed message's own *shape* (wrong arity, an unhashable tag or method name) previously
  escaped `_dispatch_loop` as a raw `ValueError`/`TypeError`/`IndexError`, contradicting
  `run_isolated`'s own documented `PluginError` contract for "a malformed or disallowed message" —
  `restricted_loads` already kept every value's *content* safe, this closes the same hole for the
  message's own shape. Fixed: message handling is wrapped so any such error is converted to
  `PluginError` and the child process is terminated, the same severity as an `UnpicklingError`.
- `to_wire_result` reduced a dict's *values* but not its *keys* — a hostile key still failed closed
  (the parent's `restricted_loads` refuses it regardless), but the function's own "JSON-safe" promise
  didn't hold for keys. Fixed: keys are reduced too.
- The new `ENFORCEMENT` message had no cap on how many a child could send — a child sending
  thousands produced one `WARNING` per message through `kb.observability`, a log-amplification
  vector. Fixed: only the first `ENFORCEMENT` message per call is honored.
- Added `execve`/`execveat` to the filesystem deny-list: the kernel opens the target program image
  internally, bypassing `open`/`openat`, so `filesystem=False` previously denied *reading* a file's
  contents but not *running* it as a new program. Bounded even before this fix — an installed filter
  is inherited across `execve` (`NO_NEW_PRIVS`), so the new image runs under the same restrictions,
  not a wider set — but worth closing directly rather than relying on that as the only mitigation.
- **The `ENFORCEMENT` message is self-reported by the untrusted child, so a malicious plugin can
  forge it** (report `applied=True` when it isn't, or send nothing at all) — not fixed, and
  deliberately so: the message is a diagnostic courtesy for a *cooperating* plugin/operator
  debugging a misconfiguration, not a security control. Forging it doesn't weaken what's actually
  enforced (the real seccomp filter state is entirely independent of what the child claims about
  it) — it can only make the operator-facing warning wrong, which a plugin author with no reason to
  lie (accepting the manifest's own declared capabilities) never triggers. Documented here so the
  distinction is explicit, not implied.
- **A `filesystem=True` registration (already drawing its own loud "declared, not enforced" warning)
  can still reach the parent's memory via `/proc/<ppid>/mem`**, the file-based equivalent of the
  `process_vm_readv`/`process_vm_writev` syscalls this ADR's `_PROCESS_ISOLATION_SYSCALLS` denies —
  since those syscalls are denied but the file that does the same thing through `open`/`pread` isn't,
  when filesystem access is itself allowed. Not a regression (the same `filesystem=True` declaration
  already accepts unenforced filesystem access broadly) and host-dependent (blocked by
  `ptrace_scope=1`, the common Linux default, when the target is an ancestor process) — documented
  as a residual gap in what "denied unconditionally whenever any restriction is installed" (above)
  can actually deliver once `filesystem=True` is declared.
- Nine known CVEs in transitive dev/runtime dependencies (`anyio`, `httpx2`, `httpcore2`) were
  failing `pip-audit` — not introduced by this branch (`git diff main...HEAD -- uv.lock` before this
  fix touched only the two `pyseccomp` lines) but blocking this PR's own CI regardless, and `anyio`
  is runtime-reachable (`mcp`/`starlette` depend on it), unlike KI-062's/KI-087's dev-only exposure —
  filed and resolved as its own entry, **KI-103**, rather than folded silently into this ADR, since
  it's unrelated to plugin isolation itself.

**Deliberately not fixed in this pass, tracked as known limitations rather than silently left
inaccurate:**
- **No timeout anywhere on an isolated call.** A plugin that hangs blocks the calling host thread
  indefinitely — `_dispatch_loop`'s `recv_bytes()` call has no deadline (`run_isolated`'s own
  cleanup `finally` block does bound `process.join(timeout=5)`, but only runs after `_dispatch_loop`
  itself returns or raises, so it doesn't help while still blocked in `recv_bytes()`). Filed as
  **KI-102**. Pre-existing risk in a different shape (in-process execution also had no timeout), but
  the blocking-`recv_bytes()` shape is new.
- **A `RemoteWritable.write()` call is one full IPC round trip per call**, not batched — a plugin
  that writes in small increments (e.g. `json.dump`'s per-token writes) pays a real, measured
  latency cost proportional to call count, not just to spawn overhead. `isolate=False` is the
  immediate workaround for a write-heavy plugin; buffering `RemoteWritable`'s writes and flushing on
  a size threshold or at the end of the call would remove most of this without changing the
  boundary's safety properties, left as a future optimization.
- **The `filesystem=False` seccomp deny-list has no dedicated test confirming a full `run_isolated()`
  round trip still works under a live filter on Linux** — only the `network=False`/`network=True`
  cases are covered by `TestRealSeccompEnforcementOnLinux` (`tests/unit/test_plugin_sandbox.py`,
  Linux-only, skipped elsewhere). Since `filesystem=False` is the manifest default, this is the more
  consequential of the two untested directions; adding it is a natural next increment, not deferred
  for a design reason.

**Round-3 review — both CRITICAL fixes re-verified a third time (28 reproduction variants across
both, plus a real malicious plugin installed under a real entry point and invoked via
`run_isolated()`) — both still hold, no third bypass of the process boundary found. Round 2's own
fixes, however, were not the convergent pass the trend suggested: two of its six fixes each
introduced a new regression, one of them reachable through two shipped reference plugins. All fixed
here, four for real, two by an explicit design decision:**
- **HIGH — round-2 fix #2 (malformed-message handling) also caught a plugin's own legitimate
  exception when it happened to be one of the four wrapped types.** `_handle_one_message`'s FAILED
  branch raised the plugin's unwired exception *from inside* the same function round 2 wrapped in
  `except (ValueError, TypeError, IndexError, KeyError)` — and every `OntolithError` subclass aside,
  a plugin's own `ValueError`/`TypeError`/`KeyError`/`IndexError` all cross the boundary as
  themselves (they're on `RestrictedUnpickler`'s builtin-exception allowlist). Reproduced through two
  shipped reference plugins: `RdfExporter.export()` with no schema registered raises `ValueError`
  unsandboxed, but `PluginError: ...sent a malformed message... (ValueError: ...)` under
  `isolate=True` (the default); `JsonExporter.export(target=12345)` raises `TypeError` unsandboxed,
  same mislabelling under isolation. Both plugins document these exceptions in their own `Raises:`
  sections, so a caller's `except ValueError:`/`except TypeError:` silently stopped working the
  moment the plugin was sandboxed. Fixed: `_handle_one_message`'s FAILED branch now returns a
  `_Failed` wrapper instead of raising directly, and `_dispatch_loop` raises the wrapped exception
  *outside* the try/except that catches a malformed message's own shape — the two concerns (a
  plugin's own exception vs. a protocol violation) are now structurally distinct, not
  distinguished by exception type alone.
- **MEDIUM — the EOF handler's plain `process.join()` (no timeout) assumed EOF always means the
  process already exited.** `restricted_loads(b"")` raises `EOFError` too (pickle's own decoder, not
  the pipe closing) — a still-alive child that deliberately sends an empty message and then keeps
  running would hang the parent's `process.join()` forever, since `_dispatch_loop`'s own EOF branch
  never actually terminates the process, only waits for it. Fixed: every exceptional path in
  `_dispatch_loop` now goes through a new `_terminate_and_join` helper (`terminate()` then a bounded
  `join(timeout=5)`, `kill()` as a last resort) instead of an unbounded `join()` — reproduced with a
  child that sends `b""` then sleeps 30s: the parent now returns in well under 10s instead of hanging.
- **MEDIUM — round-2 fix #2's decode-error handling only caught `pickle.UnpicklingError`, not every
  way `restricted_loads` can fail on corrupt bytes.** A bogus pickle protocol byte raises a plain
  `ValueError`; a malformed string opcode raises `UnicodeDecodeError` (itself a `ValueError`
  subclass) — neither was caught by the narrower clause, so both escaped as raw exceptions, and
  (unlike the `UnpicklingError` branch) neither terminated the child. Reproduced with hand-crafted
  corrupt byte sequences for both. Fixed: the decode step's error handling is now `except EOFError`
  (specific, see above) followed by `except Exception` (broad — any other decode failure is treated
  as a protocol violation, same severity as an explicit `RestrictedUnpickler` refusal, and now
  actively terminates the child too).
- **MEDIUM — round-2 fix #3 (dict-key reduction in `to_wire_result`) broke every tuple-keyed dict.**
  Reducing a key through `to_wire_result` itself ran it through the same list/tuple branch a value
  would use, which always returns a `list` (JSON has no tuple type) — an unhashable result, so
  `to_wire_result({(1, 2): "v"})` raised a raw, undocumented `TypeError: unhashable type: 'list'`
  where it had previously (pre-round-2) worked, since a tuple of scalars is exactly as safe as a list
  of them for `restricted_loads`'s purposes. Fixed: keys go through a new, narrower
  `_reduce_dict_key` — scalars pass through, tuples recurse *as tuples*, anything else raises the
  documented `UnwirableArgumentError` instead of a raw `TypeError`.
- **LOW — `_PROCESS_ISOLATION_SYSCALLS` (the `ptrace`/`process_vm_*` denial) was skipped entirely
  for the single most-capable registration** (`network=True, filesystem=True`): the early-return "no
  restriction needed" branch ran before those syscalls were ever added to the deny list, so no filter
  was installed at all in that case. Fixed: the process-isolation syscalls are now unconditional,
  included even when neither `network` nor `filesystem` needs denying — they're a floor on what a
  plugin may reach *around* the boundary, not a consequence of what it's denied *through* it.
- **LOW (design decision, not a code fix) — the `ptrace_scope` rationale comment had the attack
  direction backwards.** It claimed `ptrace_scope=1` (a distro default on Ubuntu/Debian, not — as
  originally stated — a kernel-wide default) *permits* the child-to-parent attack this denial closes;
  YAMA's `ptrace_scope=1` actually confines a *tracer* to its own *descendants*, so it already
  *blocks* exactly this direction on hosts that have it — the denial defends the `ptrace_scope=0`
  hosts instead, where nothing else stops it. Corrected in place (`enforcement.py`'s own comment),
  not treated as an ADR self-Update, since it was never a design decision, only a wrong justification
  for a decision that itself was already correct.

Two round-2 items (the self-reported `ENFORCEMENT` message, `/proc/<ppid>/mem` under
`filesystem=True`) were re-examined and their reasoning confirmed sound; round 3 additionally found
the `ENFORCEMENT` fix's own "only the first message" cap could let a malicious child's forged
`applied=True` suppress a later *genuine* `applied=False` — narrowed to "only the first *genuine
`applied=False`* is honored," which closes that without reopening the log-flood round 2's own fix
was for (verified: a flood of 50 genuine `applied=False` messages still logs exactly once).

Round 3 also added a length cap to `recv_bytes()` (`protocol.py`'s `_MAX_MESSAGE_BYTES`, 256 MiB) —
generous for any legitimate isolated-call payload this project ships today, small enough that one
oversized message from a malicious child can't grow the parent's memory unbounded before a single
byte of content is even inspected. This shipped in the same commit as the rest of round 3's fixes
but was missing from this record until round 4 pointed it out.

**Round-4 review — both CRITICALs re-reproduced a fourth time (21+ dispatch-escalation variants,
several hostile-`__reduce__` placements) after round 3's `_dispatch_loop` control-flow refactor —
both still hold, no new self-inflicted regression from that refactor. One MEDIUM found and fixed,
closing the last uncovered direction of the message-handling hardening rounds 2-3 built:**
- **MEDIUM — the *send* side of `_dispatch_loop` was never wrapped, only the *receive* side.**
  Rounds 2-3 hardened every path where the parent *reads* from the child (a malformed message, a
  decode failure, an EOF that might not mean the process exited) to surface as `PluginError` and
  actively reap the child. The three `protocol.send_*` calls in the same loop (the CALL allow-list
  refusal, the real method's success reply, the real method's own exception forwarded back) were
  never covered — if the child had already crashed or exited by the time the parent tried to reply,
  `send_result`/`send_error` raised a raw `BrokenPipeError` that propagated to the caller verbatim,
  contradicting `run_isolated`'s own documented contract ("`PluginError`: the child process ended
  without completing the call") and never actively reaping the child (`run_isolated`'s own `finally`
  still did, just up to 5s slower). Reproduced deterministically, including the fully non-adversarial
  case: a plugin makes an ordinary `kb.assertions()` call and then dies (segfault, `os._exit`,
  OOM-kill) before reading the reply. Fixed: `_dispatch_loop`'s wrapping `try`/`except` around
  `_handle_one_message` now also catches `OSError` (covers `BrokenPipeError`/
  `ConnectionResetError`) and `PluginError` (covers the "unknown protocol kind" branch
  `_handle_one_message` itself raises directly, a second, smaller gap round 4 found in the same
  sweep — it also skipped `_terminate_and_join`) — both now terminate the child and surface as
  `PluginError`, the same as every other protocol-level failure in this loop.
- Two doc-accuracy fixes from the same round: `_terminate_and_join`'s own docstring claimed it ran
  on "every exceptional path" when two didn't yet (the "unknown kind" branch and the send-side
  failures above, both now fixed to match); `registry.py`'s warning docstring claimed a declared-
  `True` capability is "never restricted, on any platform" — true for the *specific* capability
  declared `True`, but round 3's own `_PROCESS_ISOLATION_SYSCALLS` fix means a `network=True,
  filesystem=True` registration now gets a `ptrace`/`process_vm_*` denial regardless, an unrelated,
  always-on floor the original wording didn't anticipate — reworded to say so explicitly.

**Round-5 review — both CRITICALs re-reproduced a fifth time, every round 2-4 fix independently
re-verified (including the full gate suite, `--cov-fail-under=90` showing 95.35%) — no CRITICAL, no
HIGH, no MEDIUM, and no further code change requested. Ready to merge.** Only three LOW
documentation-staleness items found, all in paragraphs that summarize this ADR's own review history
elsewhere (`CHANGELOG.md`'s M4 entry and `docs/Ontolith_Implementation_Plan.md`'s status paragraph
and §8, all still saying "three review rounds" and, in one spot, an inconsistent shipped-date) —
fixed in the same commit as this record, no code touched.

## Follow-ups filed

- **macOS/Windows OS-level network/filesystem enforcement** — no KI filed as a *new* gap; this
  remains KI-014's own still-open half, now scoped precisely to non-Linux platforms rather than
  every platform.
- **Remote `QueryBuilder`/`AsOfView` proxy for isolated plugins** — filed as **KI-101**: an isolated
  plugin cannot call `kb.query(...)`/`kb.as_of(...)` today; no shipped reference plugin needs it yet,
  but a future `Reasoner`/`Connector` plugin doing anything beyond `assertions()`'s flat filter would.
- **No timeout on an isolated call** — filed as **KI-102** (round-2 review finding): a hung plugin
  blocks the calling host thread indefinitely; see this Update section's own "Deliberately not fixed"
  list above for the precise blocking call.

## Update (2026-09-27): security review found a real bypass of the Linux enforcement itself (M4
Workstream 7) — fixed across three rounds (round 1's own fix broke CI; round 2's own fix could
itself be defeated by the plugin it guards against); further gaps disclosed/filed, not fixed

**Round 1 found, and attempted to fix, two HIGH issues — round 1's own fix for the first one broke
Linux CI and had to be redesigned in round 2 (below); described here in its final, corrected form.**

**HIGH — the child installed its seccomp filter too late, after the plugin's own module import and
constructor already ran unsandboxed inside the child process.** `_child_main` called
`_load_plugin_instance` (imports the plugin's module, constructs it) *before*
`enforcement.apply_capability_enforcement`. A `network=False`/`filesystem=False` plugin could open a
socket or file at import time or in `__init__` — before any filter existed to deny it — then use
already-open-descriptor syscalls (`read`/`write`, which the filter must always allow for the
sandbox's own IPC pipe; see `enforcement.py`'s own `_FILESYSTEM_SYSCALLS` comment) on that
already-opened handle afterward, bypassing the declared capabilities entirely on the one platform
this ADR's own headline claim is about.

Round 1's own fix — installing the *full* filter (network + filesystem + floor) before
`_load_plugin_instance` — broke every `filesystem=False` plugin's own loading on Linux+libseccomp:
`entry_points()`'s metadata scan and `EntryPoint.load()`'s module import both need real filesystem
reads regardless of what the plugin itself declares, and `filesystem=False` is this project's own
default. Reproduced as a genuine CI failure (`required-fields-validator`, the one shipped reference
plugin with default capabilities, raised `PluginError: Plugin entry point not found` on every Linux
job) — round 2 caught this and reverted the ordering.

**Fixed, round 2's corrected design**: a new, narrower `enforcement.apply_preimport_enforcement`
runs before `_load_plugin_instance` — denying only network (if declared `False`) and the always-on
process-isolation floor, never filesystem. Neither has any legitimate reason to fire during plugin
loading, so denying them this early costs nothing and closes the network-exfiltration-during-import
vector fully. The existing `apply_capability_enforcement` (full filter, including filesystem) stays
exactly where it always was: installed immediately before the plugin's own protocol method is
invoked, after loading — unchanged timing, so no regression risk there. Multiple seccomp filters
installed in the same process stack (each `load()` call only ever adds restriction, standard Linux
seccomp-BPF semantics), so having both filters active is safe. **Residual, disclosed, filed as
KI-109**: a plugin's own module import/constructor still runs with filesystem fully unenforced, even
when `capabilities.filesystem=False` — closing that needs argument-filtered seccomp rules (allow
read-only opens, deny writes, during the pre-import phase) this round deliberately didn't attempt,
given the risk of another CI-breaking regression and the inability to test seccomp behavior on this
project's own (non-Linux) development machines.

**HIGH — no operator lever existed to demand real enforcement rather than silently degrade to
advisory.** The registration-time warning (`_warn_if_unenforced_capabilities_requested`) was, and
remains by default, the only signal when OS-level enforcement can't be attempted or a specific
attempt fails — advisory, not a guarantee. Added `require_enforcement: bool = False` on
`PluginRegistry.register()` (default preserves today's behavior — flipping the default would refuse
every registration on macOS/Windows outright): `True` refuses registration outright when enforcement
can't even be attempted (`isolate=False`, or `enforcement.enforcement_available()` is `False` for
this host/platform).

Round 1's own call-time version of this fix was itself broken, caught in round 2: the refusal lived
only in the parent's own message handler, which can make the *caller's* `run_isolated()` raise
promptly but cannot stop the already-running, separate-OS-process child from continuing on its own to
load and call the plugin regardless — reproduced directly, over 3 seconds of fully unenforced plugin
execution (import, `__init__`, and the protocol method itself) before `run_isolated`'s own
`process.join(timeout=5)`/`terminate()` eventually caught up. **Fixed, round 2**: the child itself now
checks `require_enforcement` right after its own real, call-time enforcement attempt, and refuses to
invoke the plugin's protocol method at all if it was required but didn't apply — the parent's own
copy of the check remains too, as a second, faster-to-observe layer, but the child-side check is what
actually stops the untrusted code from running. This guarantee is still narrower than "the plugin
never runs at all": the plugin's module import/constructor still ran first, under only the
pre-import filter's network/floor-only protection — same KI-109 residual named above, not a new one.

**MEDIUM — the always-on process-isolation floor missed the `pidfd_*` syscall family.**
`_PROCESS_ISOLATION_SYSCALLS` denied `ptrace`/`process_vm_readv`/`process_vm_writev` but not
`pidfd_getfd` (Linux 5.6+, requiring the same `PTRACE_MODE_ATTACH` permission `ptrace(PTRACE_ATTACH,
...)` itself requires) — on exactly the `ptrace_scope=0` hosts this floor exists to defend, a child
could `pidfd_open(getppid())` (no permission check of its own) then `pidfd_getfd` to duplicate one of
the parent's own open file descriptors (the SQLite connection, a live REST/MCP client socket) into
itself, then read/write it directly, reaching past the process boundary without ever calling
`ptrace`/`process_vm_*` at all. Added `pidfd_open`, `pidfd_getfd`, `pidfd_send_signal`, `kcmp`,
`process_madvise`, `process_mrelease` to the floor as the same family of cross-process
introspection/tampering primitive — not all gated by an identical permission check (`kcmp`/
`process_madvise` use the weaker `PTRACE_MODE_READ`, not restricted by YAMA's `ptrace_scope` the way
`ATTACH` is; `process_mrelease`'s exact model wasn't independently verified), but all denied on the
same "cross-process, no legitimate use inside a plugin call" basis. Round 2 found and fixed an
overstated claim in round 1's own comment here (that all of these share `pidfd_getfd`'s exact
permission check) and added a platform-independent regression test pinning the floor's contents
(`TestProcessIsolationFloorContents` — round 1 shipped this addition with no test pinning it at all).
Further, pre-existing gaps in this same floor (the kill syscall family entirely unrestricted;
secrets in the parent's environment fully inherited by every sandboxed child regardless of declared
capabilities; several other cross-process-capable syscalls not yet considered) found while reviewing
this fix, not introduced by it — filed as **KI-111**, not fixed this pass.

**Also filed, round 2: KI-110** — `require_enforcement`'s guarantee can be satisfied by a partially-
installed filter, since an unresolved syscall name is silently skipped while `EnforcementResult.
applied` still reports `True`. Not fixed this pass.

**Round 3 found round 2's own `require_enforcement` fix could itself be defeated by the exact
plugin it exists to guard against — reproduced end to end, partially fixed.**

**HIGH — a plugin's own module-level code can reassign `enforcement.apply_capability_enforcement`
itself, silently defeating `require_enforcement=True` with no exception and no warning.** Round 2's
fix checked `apply_capability_enforcement`'s real, call-time result — genuinely correct reasoning
for a *non-adversarial* failure (a container's outer seccomp blocking the install). But that result
comes from a plain module-level function, looked up fresh (`enforcement.apply_capability_
enforcement(capabilities)`) *after* `_load_plugin_instance` has already run the plugin's own
untrusted module import and `__init__` — necessarily, since attempting the full filter any earlier
is exactly what broke loading in round 1. A plugin's own code, once running, can do:
```python
import ontolith.plugins.sandbox.enforcement as enf

enf.apply_capability_enforcement = lambda caps: enf.EnforcementResult(applied=True, reason=None)
```
and the subsequent lookup picks up the forged function instead of the real one. Reproduced directly
with a real installed entry point: `run_isolated(..., require_enforcement=True)` returned
successfully with no exception and no warning, and the plugin's own `export()` wrote a real file to
disk despite `capabilities.filesystem=False` (the default). The stale trust comment in
`_handle_one_message`'s own ENFORCEMENT-message handling — which claimed the first ENFORCEMENT
message "cannot have tampered with or fabricated," true under round 1's ordering — was also
corrected, since round 2's own reordering made that claim false without anyone noticing at the
time.

**Fixed, round 3, the single most literal reproduced variant** — **and round 4 found this fix
doesn't meaningfully narrow the attack at all, correcting round 3's own overclaim that it did**:
`_child_main` captures a reference to the real `apply_capability_enforcement` function into a local
variable *before* calling `_load_plugin_instance` — reassigning the module's own
`apply_capability_enforcement` attribute afterward no longer affects a name this function already
bound to the original object. Round 3 described this as closing "the specific module-attribute
version" of the attack. Round 4 reproduced directly that it doesn't: the captured function's own
body still resolves every name it depends on (`_install_seccomp_filter`, `EnforcementResult`,
`_FILESYSTEM_SYSCALLS`, `_NETWORK_SYSCALLS`, `_PROCESS_ISOLATION_SYSCALLS`) fresh from the same
shared, mutable `enforcement` module namespace on every call — a plugin reassigning
`enforcement._install_seccomp_filter` (verified: `enforcement._install_seccomp_filter = lambda
denied: enforcement.EnforcementResult(applied=True, reason=None)`) defeats the *captured* reference
exactly as completely as reassigning `apply_capability_enforcement` itself would have. Capturing the
outer function object protects nothing about its inner dependencies, which live in the identical
module `__dict__` regardless of which name a caller used to reach the function. **In practice, this
capture defends only against the single most naive reproduction, not the underlying class of
attack** — kept because it's free and harmless, not because it materially changes the threat model.
Verified by a new regression test that reproduces the originally-found attack (a monkeypatched
`_load_plugin_instance` standing in for a plugin's own malicious import, using safe fakes throughout
— see this Update's own CI-corruption paragraph below) and confirms the captured reference still
reports the intended result for *that one* reproduction; mutation-tested by reverting to the
unprotected module-attribute lookup and confirming exactly that test fails. The test does not (and,
given the finding above, cannot meaningfully) prove anything broader.

**Not fixed — the real gap, unchanged by round 3's fix, folded into KI-109's own extended text**: no
Python-level capture of any kind — the function itself, the `pyseccomp` module, or anything else
resolvable via `sys.modules`/module attributes — can protect against code that already shares the
same mutable process, module, and interpreter state. `require_enforcement` is now documented
everywhere it's mentioned — `registry.py`, `runner.py`, this ADR — as protection against a
non-adversarial enforcement failure, not a security boundary against a plugin actively trying to
defeat it. The mitigating context, unchanged from round 3's own framing: a plugin capable of this
already has full, unsandboxed access during its own registration-time `plugin_class()` call in the
parent process (this ADR's own pre-existing Negative bullet) — a malicious plugin author already
has more to work with there than this bypass adds; nothing here makes the actual security posture
worse, only round 3's own documentation of what had changed was inaccurate, corrected here.

**MEDIUM (round 3) — `apply_preimport_enforcement` used to return `None`, so its own outcome was
never checked at all.** The narrower pre-import filter (network + floor, installed before loading)
could itself silently fail to install — exactly the container-profile-blocked scenario
`require_enforcement` exists for — and nothing would notice, since there was nothing to check.
Fixed: it now returns its own `EnforcementResult`, checked in `_child_main` before `_load_plugin_
instance` ever runs (the earliest point at which no plugin code has executed and the result is
fully trustworthy) — `require_enforcement=True` now refuses before loading if even this narrower
filter didn't apply, not only after the full filter's own later attempt.

**MEDIUM (round 3) — the AST-based "structural proof" test from round 2 didn't actually prove
anything about control flow.** It compared line numbers only: the `require_enforcement` check's
line number came before the protocol-method call's line number in source, but nothing checked that
the check's own branch actually returned. Reproduced: removing just the `return` statement from
that branch (falling through to call the plugin anyway after sending FAILED) left the AST test
passing unchanged. Replaced with a behavioral test that runs `_child_main` for real with every
dependency monkeypatched to a controlled double, and asserts the plugin's own method was never
actually invoked — confirmed this one fails against the exact no-return mutant that fooled the AST
test.

**Also added, round 3: `CTL_TSYNC`** on every installed filter. libseccomp's own TSYNC default is
off, meaning a filter with no TSYNC only binds the thread that calls `load()` — a plugin's own code
starting a thread before the filter installs could keep that thread running under whatever (weaker
or absent) filter existed when it started, unaffected by anything installed on the main thread
afterward. Verified the exact API (`pyseccomp.Attr.CTL_TSYNC`, `SyscallFilter.set_attr`) against
the pinned `pyseccomp==0.1.2`'s own source before adding it, since this can't be tested on a
non-Linux development machine either. Not independently reproduced as an attack this round (would
need a real Linux host) — added as a genuine, verified-correct hardening step regardless.

**Round 4 found round 3's own PR had itself shipped a CI-breaking regression, unrelated to the
security substance, in its own new test.** `test_capturing_the_enforcement_function_before_load_
defeats_the_attribute_attack` called the REAL, captured `apply_capability_enforcement` directly
in-process (not through a spawned child) to prove the captured reference survives a later
reassignment. On real Linux+libseccomp CI, calling the real function genuinely installs a
restrictive seccomp filter — and a loaded filter only ever gets more restrictive and can never be
removed (`TestRealSeccompEnforcementOnLinux`, a few classes above in the same test file, already
documents exactly this hazard and runs each of its own probes in a dedicated subprocess for exactly
this reason). This one bypassed that discipline, permanently sandboxing the shared pytest worker
process for the rest of the test session — every test after it in the same worker started failing
with `PermissionError: Operation not permitted` from pytest's own tmp-dir/capture machinery.
**Fixed**: the module attribute is now monkeypatched to a safe, side-effect-free fake standing in
for "the real function" before `_child_main` ever runs, so the test never touches actual OS-level
enforcement on any platform. Every other `_child_main`-calling test in the file was re-checked by
hand for the same class of risk and confirmed already safe (fully mocked, or a capture-without-
calling that has no side effect regardless of what it captures).

**Round 4 also found one of those now-safe tests is safe only by a coincidence of control flow, not
by its own mocking — LOW.** `test_require_enforcement_refuses_before_loading_when_the_preimport_
filter_fails` never mocks `apply_capability_enforcement` at all; it's safe today only because the
function under test returns before ever reaching that call. If that early `return` ever regresses,
this test would silently start installing a real filter in the pytest worker again — the exact
failure mode above, recurring. Fixed by adding an explicit mock there too, so a future regression
fails this one test cleanly instead of corrupting the whole worker.

**Round 4 also found a real gap in `CTL_TSYNC`'s own reporting — folded into KI-110, not filed
separately.** If `set_attr(CTL_TSYNC, ...)` itself raises (an older libseccomp build without TSYNC
support), the filter still loads on the calling thread only, and `apply_capability_enforcement`
still reports `applied=True` — a thread the plugin started before the filter installed stays
unfiltered, invisible to `require_enforcement`. Same shape as KI-110's own existing scope (a
partially-installed filter that still reports full success); not a new KI.

**Disclosed, not fixed — filed as KI-106: `capabilities.filesystem=True` subsumes
`granted_capability`'s own storage ceiling entirely.** A plugin with `filesystem=True` (three of the
four shipped reference plugins declare it) can open the KB's own on-disk file directly and write to it,
bypassing proposal/policy/audit regardless of how low its granted storage capability is —
`granted_capability="read"` was never a real limit on such a plugin. The registration-time warning
now discloses this explicitly. The structural fix (a parent-opened, capability-scoped readable proxy
for an importer's source, mirroring `RemoteWritable`'s existing treatment of an exporter's target) is
real design/implementation work, not bundled into this pass — see KI-106's own Fix section.

**Wording fix, not a functional change:** the Consequences section's Positive #1 bullet's "cannot
reach `view._kb`... at all... regardless" claim read as covering the plugin's whole lifecycle;
scoped it explicitly to the isolated call, cross-referencing the adjacent Negative bullet that
already, correctly, documents the registration-time module-import/constructor phase as unsandboxed.

Both CRITICALs from the 2026-09-20 Update were re-verified still closed at the start of this
security review, before any of the three remediation rounds above (reproduced both attacks directly
against the fixed code — `restricted_loads` refuses a `__reduce__`-based RCE payload, and `_handle_one_
message`'s allow-list refuses every non-allowlisted CALL on a real view, including `__setattr__`).

## References

- `docs/known-issues.md` KI-014 (network/filesystem half; Linux now resolved, macOS/Windows remain
  open per this ADR's own scoping), **KI-101** (new, the `.query()`/`.as_of()` follow-up above),
  **KI-102** (new, the no-timeout follow-up above), **KI-103** (new, unrelated to plugin
  isolation itself — the `anyio`/`httpx2`/`httpcore2` `pip-audit` fix found while reviewing this ADR),
  **KI-106** (new, M4 Workstream 7's security review round 1 — `filesystem=True` subsumes the storage
  capability ceiling), **KI-109** (new, round 2, extended round 3 — a plugin's own import/construction
  phase can defeat enforcement for the rest of the call too, not just leave filesystem open during
  loading), **KI-110** (new, round 2 — `require_enforcement` can be satisfied by a
  partially-installed filter), and **KI-111** (new, round 2 — further pre-existing process-isolation
  floor gaps: the kill syscall family, inherited environment secrets, other unconsidered syscalls) —
  see this ADR's own 2026-09-27 Update for all of the above
- ADR-0015 (Plugin Capability Isolation — the storage-capability half this ADR doesn't change, and
  the "Python has no true encapsulation" gap this ADR closes structurally)
- ADR-0044 (Observability — `kb.observability` is how this ADR's enforcement-degradation warning is
  emitted, not a fixed module logger)
- SPEC §13 (Plugin protocols and discovery), §17 (Security model: "runtime SHOULD isolate execution
  and MUST deny undeclared access (storage, network, filesystem)" — storage already enforced via
  ADR-0015, network/filesystem now enforced on Linux via this ADR)
- `docs/Ontolith_Implementation_Plan.md` §8 (Security & supply chain — the "process isolation...
  wasm/subprocess hardening + signed registry plugins by 1.0" phasing this ADR implements the first
  half of)
