# Ontolith

A Python framework/SDK for building collaborative knowledge bases where humans
and AI agents are co-equal authors of a shared, governed ontology.

Every fact has provenance. Every write from an AI is governed. Nothing is
silently overwritten. That's the whole pitch.

## Why Ontolith

Most knowledge-graph tooling assumes a single trusted writer. Ontolith assumes
the opposite: multiple humans and multiple AI agents, of varying trust levels,
all proposing facts about the same entities — sometimes agreeing, sometimes
not. The framework's job is to make that safe and auditable by construction,
not by convention.

- **Dual-principal model** — humans and AI agents are both first-class
  authors. Every AI principal declares an accountable human/team owner.
- **Provenance & confidence** — who asserted it, from what source, with what
  confidence, is captured automatically and retrievable in one call.
- **Governed collaboration** — a configurable proposal/review workflow.
  AI-authored proposals always route to human review by default.
- **Append-only, bitemporal** — nothing is edited in place. `as_of(t)`
  reconstructs exactly what was known and true at any past instant.
- **Conflict handling, not conflict-hiding** — a changing fact supersedes its
  predecessor; disagreeing sources produce an explicit, queryable
  contradiction, never a silent overwrite.
- **Governed plugins** — importers, exporters, reasoners, connectors, and
  validators loaded through `PluginRegistry` (third-party plugins,
  discovered via entry points) run process-isolated by default, with
  `capabilities.network`/`.filesystem` enforced at the OS syscall level on
  Linux (seccomp) — advisory only on macOS/Windows, where process/IPC
  isolation still applies. Validators/completeness
  validators wired in directly via `Ontology.connect()` (first-party,
  deployment-configured) run in-process instead — trusted the same way a
  `PolicyStrategy` already is.

## Where to go next

<div class="grid cards" markdown>

- **[Getting Started](getting-started.md)**
  Install Ontolith and build your first knowledge base in a few minutes.

- **[Concepts](concepts.md)**
  The handful of ideas — principals, assertions, temporality, conflict
  routing — that everything else is built from.

- **[Tutorials](tutorials/index.md)**
  Governed review workflows, time-travel queries, writing a plugin, and
  hybrid symbolic+vector search — each a runnable example.

- **[API Reference](reference/index.md)**
  Every class and function in the pinned public surface, generated from the
  library's own docstrings.

</div>

## Install

```bash
git clone https://github.com/ontolith/ontolith.git
cd ontolith
uv sync
uv run python examples/quickstart.py
```

Ontolith isn't published to PyPI yet — install from source (requires
[uv](https://github.com/astral-sh/uv) and Python ≥3.11).

## Project status

Ontolith is at **v1.1.0** — M0 through M4 of the
[Implementation Plan](https://github.com/ontolith/ontolith/blob/main/docs/Ontolith_Implementation_Plan.md)
are complete: substrate, collaboration, extensibility, and production
hardening (performance budgets, a full security review, an audited and
frozen public API surface). See the
[CHANGELOG](https://github.com/ontolith/ontolith/blob/main/CHANGELOG.md) for
the full history and [Known Issues](https://github.com/ontolith/ontolith/blob/main/docs/known-issues.md)
for honestly-disclosed gaps and residuals.
