# Writing a Plugin

Full source:
[`examples/writing_a_plugin.py`](https://github.com/ontolith/ontolith/blob/main/examples/writing_a_plugin.py)

This tutorial registers and calls a real reference plugin, then covers what
you'd change to write your own.

## Discovery and registration

Plugins are discovered via `importlib.metadata` entry points under the
`"ontolith.plugins"` group — the same mechanism most Python plugin
ecosystems use. `PluginRegistry.register()` requires `admin` capability on
the caller:

```python
from ontolith.plugins.registry import PluginRegistry

loaded = PluginRegistry(kb).register(
    "csv-importer",
    author=admin.id,
    granted_capability="write",  # a CEILING, not a grant of whatever the plugin asks for
)
```

`register()` creates a `service`-kind `Principal` for the plugin (least
privilege by default — a plugin's effective *storage* capability is
`min(granted_capability, the manifest's own requested capability)`,
further capped to `read` for read-only plugin kinds), and returns a
`LoadedPlugin` bundling that principal's id, a capability-scoped view
(`ReadOnlyView`/`WriteView`), and the plugin instance itself.

!!! warning "`granted_capability` is a ceiling on *storage*, not on filesystem/network"
    `filesystem=True` is an allow, never enforced as a ceiling by
    `granted_capability` — a plugin that declares it can touch the
    filesystem could open the KB's own on-disk file directly and write to
    it, bypassing `granted_capability`'s storage limit entirely (KI-106).
    `granted_capability` genuinely limits what the plugin can do through
    the `kb`/`WriteView` object it's handed; it says nothing about what a
    filesystem- or network-capable plugin can do outside that object.

## Calling it — and the sandbox underneath

Since ADR-0051, a plugin's protocol entrypoint runs in a **spawned child
process** by default (`isolate=True`), with `capabilities.network`/
`.filesystem` enforced at the OS syscall level on Linux (seccomp). You call
it exactly as you would the real object — the sandboxed round-trip is
transparent:

```python
rows = [
    {
        "concept": "Person",
        "natural_key": "grace",
        "predicate": "Person.name",
        "value": "Grace Hopper",
        "value_kind": "literal",
    }
]
report = loaded.instance.import_(rows, loaded.view)
# {"rows_processed": 1, "entities_created": 1, "assertions_proposed": 1}
```

A dataclass return value (like this plugin's own `ImportReport`) crosses an
isolated call as a plain `dict` — the sandbox can only safely round-trip a
restricted set of types, not arbitrary pickled objects (a defense against a
hostile plugin sending back a malicious `__reduce__` payload, found and
fixed in the M4 security review).

!!! warning "Registration-time AND call-time warnings are expected, not errors"
    Registering a plugin that declares `filesystem=True`, or whose
    `network=False`/`filesystem=False` can't actually be enforced on your
    platform (true for every plugin on macOS/Windows today, including one
    with the all-`False` default manifest), logs a real warning about what
    actually is/isn't enforced (this project deliberately surfaces gaps
    rather than staying silent about them — see
    [`docs/known-issues.md`](https://github.com/ontolith/ontolith/blob/main/docs/known-issues.md)
    KI-014/KI-106 for the full detail), and **every call** on a platform
    with no OS-level enforcement mechanism (macOS/Windows today) logs a
    second warning at call time ("OS-level capability enforcement did not
    apply for this call to ..."). Both are honestly disclosed residuals,
    not bugs in your setup.

## Writing your own

A plugin is one of five protocols
([`ontolith/plugins/ports.py`](https://github.com/ontolith/ontolith/blob/main/src/ontolith/plugins/ports.py)):

| Protocol | Method | Given |
|---|---|---|
| `Importer` | `import_(source, kb)` | a `WriteView` |
| `Exporter` | `export(kb, target)` | a `ReadOnlyView` |
| `Reasoner` | `derive(kb)` | a `WriteView` |
| `Validator` | `validate(assertion, kb)` | a `ValidatorKbView` |
| `Connector` | `sync(kb)` | a `WriteView` |

Every plugin class needs a `manifest` class attribute. `PluginCapabilities.storage`
defaults to `"read"` — an `Importer`/`Reasoner`/`Connector` that writes
needs at least `storage="propose"` declared explicitly, or registration
refuses it outright:

```python
from ontolith.plugins.manifest import PluginCapabilities, PluginManifest


class MyImporter:
    manifest = PluginManifest(
        name="my-importer",
        version="0.1.0",
        kind="importer",
        capabilities=PluginCapabilities(storage="propose"),
    )

    def import_(self, source, kb): ...
```

`"propose"` is the minimum that lets registration succeed at all for a
storage-writing kind; the shipped `csv-importer` reference plugin actually
declares `storage="write"` instead. `ThresholdPolicy` *can* auto-accept a
`propose`-capability principal too, but only above a trust-level floor —
and `PluginRegistry` always creates a plugin's service principal at
`trust_level=0`, below that floor — so in practice a `"propose"`-capability
plugin's every proposal sits in human review; `"write"` (or `"admin"`)
auto-accepts regardless of trust level, which is what a bulk importer
actually needs.

...and a registered entry point in your own package's `pyproject.toml`:

```toml
[project.entry-points."ontolith.plugins"]
my-importer = "my_package.importer:MyImporter"
```

`PluginRegistry` instantiates your plugin class with a **no-argument
constructor** — give it sensible defaults, or a `from_config(...)`
classmethod callers can use before registration if it needs configuration
(the shipped `RequiredFieldsValidator.from_schema()` is a real example of
this pattern).

A `Reasoner`'s derived assertions must still enter through the normal
proposal path — a plugin never bypasses governance, no matter how it
computed the fact.

See
[`src/ontolith/plugins/reference/`](https://github.com/ontolith/ontolith/tree/main/src/ontolith/plugins/reference)
in the repository for four complete, real reference plugins (a CSV
importer, a JSON exporter, an RDF/OWL exporter, and a required-fields
validator) to use as templates.
