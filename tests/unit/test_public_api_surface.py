"""Pins the public API surface defined in ADR-0019.

Fails if any package's `__all__` changes without a conscious update here —
the M3-level starting point for "public API stability policy begins"
(Implementation Plan §2). This catches export additions/removals/renames;
it does NOT catch signature-level changes to an already-exported symbol
(e.g. a new required parameter on `Ontology.propose`) — that gap is
tracked in KI-020, deferred pending a `griffe`-based CI diff gate.

If this test fails because of an intentional, reviewed change: update the
relevant entry in `_EXPECTED` below, and if anything was removed or
renamed, record it in CHANGELOG.md under a `**Breaking:**` marker per
ADR-0019's SemVer commitment.

`ontolith.interfaces.cli`/`ontolith.interfaces.mcp` are deliberately not
pinned here — ADR-0019 scopes this policy to the Python SDK surface, not
CLI flag or MCP tool-schema stability. `ontolith.interfaces.rest`/
`ontolith.interfaces.graphql` ARE pinned (`create_rest_app`/
`create_graphql_app` are genuine Python call-signature surfaces, not a
CLI-flag or MCP-tool-schema kind of contract) — same treatment as
`ontolith.store.duckdb`: part of the pinned/tested surface, but excluded
from `test_pinned_packages_import_cleanly_without_any_optional_extra`
below, since both require their own optional extra (`rest`/`graphql`,
`store-duckdb`) to import at all.
"""

import subprocess
import sys

import ontolith
import ontolith.core
import ontolith.govern
import ontolith.identity
import ontolith.interfaces.graphql
import ontolith.interfaces.rest
import ontolith.plugins
import ontolith.query
import ontolith.schema
import ontolith.store
import ontolith.store.duckdb
import ontolith.store.migrations
import ontolith.store.sqlite

_EXPECTED: dict[str, frozenset[str]] = {
    "ontolith": frozenset(
        {
            "Ontology",
            "AsOfView",
            "Entity",
            "Assertion",
            "Namespace",
            "Principal",
            "CreateEntityOp",
            "AssertLiteralOp",
            "AssertRefOp",
            "LocalRef",
            "FixedClock",
            "SequentialIdProvider",
            "Concept",
            "Relation",
            "Property",
            "Ref",
            "Text",
            "Integer",
            "Float",
            "Boolean",
            "Date",
            "DateTime",
            "URI",
            "JSON",
        }
    ),
    "ontolith.core": frozenset(
        {
            "Clock",
            "SystemClock",
            "FixedClock",
            "IdProvider",
            "UlidProvider",
            "SequentialIdProvider",
            "FixedIdProvider",
            "Entity",
            "Assertion",
            "AssertionEvent",
            "Namespace",
            "OntolithError",
            "SchemaError",
            "ValidationError",
            "AuthError",
            "CapabilityError",
            "PolicyDenied",
            "ConflictError",
            "NotFoundError",
            "StorageError",
            "PluginError",
            "Embedder",
            "HashingEmbedder",
            "LookupEmbedder",
            "ObservabilitySink",
            "StdlibLoggingSink",
            "NullObservabilitySink",
            "RecordingObservabilitySink",
        }
    ),
    "ontolith.identity": frozenset(
        {
            "AdminAction",
            "AdminEvent",
            "AuthProvider",
            "Principal",
            "PrincipalCredential",
            "TokenAuthProvider",
            "hash_token",
            "min_capability",
        }
    ),
    "ontolith.store": frozenset({"StorageBackend", "VECTOR_SCOPES", "DEFAULT_NAMESPACE"}),
    "ontolith.store.migrations": frozenset({"MigrationReport", "MigrationStep"}),
    "ontolith.store.sqlite": frozenset({"SQLiteBackend", "migrations"}),
    "ontolith.store.duckdb": frozenset({"DuckDBBackend", "migrations"}),
    "ontolith.govern": frozenset(
        {
            "Proposal",
            "ProposalEvent",
            "ProposalState",
            "Provenance",
            "Contradiction",
            "ContradictionState",
            "safe_rationale_history",
            "Decision",
            "AutoAccept",
            "RequireReview",
            "Reject",
            "KbView",
            "PolicyStrategy",
            "ThresholdPolicy",
            "SourceQuorum",
            "ConfidenceThreshold",
            "SourceRequired",
            "RequireReviewByRole",
            "RequireReviewForAI",
            "Composite",
            "Activate",
            "Supersede",
            "Contradict",
            "ConflictResult",
            "route",
            "BatchOp",
            "CreateEntityOp",
            "AssertLiteralOp",
            "AssertRefOp",
            "LocalRef",
            "MAX_BATCH_OPERATIONS",
        }
    ),
    "ontolith.query": frozenset({"QueryBuilder"}),
    "ontolith.schema": frozenset(
        {
            "SchemaIR",
            "ConceptDef",
            "PropertyDef",
            "RelationDef",
            "Concept",
            "Relation",
            "Property",
            "Ref",
            "Text",
            "Integer",
            "Float",
            "Boolean",
            "Date",
            "DateTime",
            "URI",
            "JSON",
            "compile_schema",
            "generate_class_stubs",
        }
    ),
    "ontolith.plugins": frozenset(
        {
            "PluginKind",
            "PluginCapabilities",
            "PluginManifest",
            "ReadOnlyView",
            "WriteView",
            "PluginRegistry",
            "LoadedPlugin",
            "Importer",
            "Exporter",
            "Reasoner",
            "Validator",
            "ValidatorKbView",
            "Connector",
        }
    ),
    "ontolith.interfaces.rest": frozenset({"create_rest_app"}),
    "ontolith.interfaces.graphql": frozenset({"create_graphql_app"}),
}

_MODULES = {
    "ontolith": ontolith,
    "ontolith.core": ontolith.core,
    "ontolith.identity": ontolith.identity,
    "ontolith.store": ontolith.store,
    "ontolith.store.migrations": ontolith.store.migrations,
    "ontolith.store.sqlite": ontolith.store.sqlite,
    "ontolith.store.duckdb": ontolith.store.duckdb,
    "ontolith.govern": ontolith.govern,
    "ontolith.query": ontolith.query,
    "ontolith.schema": ontolith.schema,
    "ontolith.plugins": ontolith.plugins,
    "ontolith.interfaces.rest": ontolith.interfaces.rest,
    "ontolith.interfaces.graphql": ontolith.interfaces.graphql,
}


def test_pinned_packages_match_expected_snapshot() -> None:
    assert set(_MODULES) == set(_EXPECTED), (
        "A package was added to or removed from the pinned public API surface "
        "without updating both _MODULES and _EXPECTED (ADR-0019)."
    )


def test_public_api_surface_matches_pinned_snapshot() -> None:
    actual = {name: frozenset(mod.__all__) for name, mod in _MODULES.items()}
    assert actual == _EXPECTED, (
        "Public API surface changed (ADR-0019). If this is an intentional, "
        "reviewed change: update _EXPECTED above, and if anything was removed "
        "or renamed, record it in CHANGELOG.md under a '**Breaking:**' marker."
    )


_OPTIONAL_EXTRA_PACKAGES = frozenset(
    {
        # Each of these is itself gated behind its own
        # `[project.optional-dependencies]` extra and cannot import at all
        # without it (`duckdb`/`fastapi`/`strawberry-graphql` at module
        # level) — genuinely excluded from the "no optional extra needed"
        # guarantee below, not a gap in it. Still part of the pinned
        # surface (_MODULES/_EXPECTED above) and still covered by
        # `test_public_api_surface_matches_pinned_snapshot`.
        "ontolith.store.duckdb",
        "ontolith.interfaces.rest",
        "ontolith.interfaces.graphql",
    }
)


def test_pinned_packages_import_cleanly_without_any_optional_extra() -> None:
    """Every pinned package NOT in `_OPTIONAL_EXTRA_PACKAGES` must import
    with only the base (non-optional) dependency set installed — none of
    them may eagerly pull in something gated behind
    `[project.optional-dependencies]` (`pyyaml`/`rdflib` behind `interop`,
    `duckdb` behind `store-duckdb`, `fastapi`/`strawberry-graphql`/`mcp`
    behind `rest`/`graphql`/`mcp`).

    The import list is derived from `_MODULES` (minus the documented
    exclusion set above) rather than hardcoded separately — an earlier
    version of this test hardcoded its own list, which a new pinned
    package could silently miss the same way this test itself exists to
    catch a silently-missed optional-dependency leak. Round-1 review of
    this audit found and fixed exactly that gap.

    Found during the M4 API-surface-freeze audit: `ontolith.schema`
    briefly re-exported `schema.linkml`'s `from_yaml`/`to_yaml` (a genuine
    gap — the interop extra's own module `__all__` already declared them
    public, the package `__init__.py` just never picked it up, the same
    shape as the `AsOfView`/`AuthProvider`/`VECTOR_SCOPES` gaps this same
    audit did fix) — verified broken (`ModuleNotFoundError: No module
    named 'yaml'` on plain `import ontolith.schema`) and reverted before
    merge, because `schema/linkml.py` imports `yaml` at module level and
    `pyyaml` is an `interop`-extra, not a base dependency. This test
    exists so that exact mistake, or its equivalent for `rdflib`/`duckdb`/
    any future optional dependency, fails CI automatically next time
    instead of depending on whoever's making the change noticing a
    same-shape docstring warning on a sibling module (which is what
    caught it this time, by luck of reading the neighboring file, not by
    any enforced check).

    Runs in a real subprocess (not in-process `sys.modules` poisoning) so
    a poisoned import can't leak into any other test in this process —
    `sys.modules['yaml'] = None` (etc.) makes Python's import system treat
    the module as confirmed-absent (raises `ModuleNotFoundError`
    immediately, no filesystem/site-packages lookup) without needing a
    real venv lacking those packages.
    """
    poisoned = ["yaml", "rdflib", "duckdb", "fastapi", "uvicorn", "strawberry", "mcp"]
    import_lines = "\n".join(
        f"import {name}" for name in sorted(set(_MODULES) - _OPTIONAL_EXTRA_PACKAGES)
    )
    script = (
        "import sys\n"
        f"for name in {poisoned!r}:\n"
        "    sys.modules[name] = None\n"
        f"{import_lines}\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"A pinned package eagerly imports an optional dependency.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
