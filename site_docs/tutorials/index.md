# Tutorials

Each tutorial below walks through one runnable script from
[`examples/`](https://github.com/ontolith/ontolith/tree/main/examples) in the
repository. Clone the repo and run any of them directly:

```bash
uv run python examples/governance_and_review.py
uv run python examples/bitemporal_queries.py
uv run python examples/writing_a_plugin.py
uv run python examples/hybrid_search.py
uv run python examples/erasing_personal_data.py
```

- **[Governance & Review](governance-and-review.md)** — why AI proposals
  always require human review, and the full
  propose → request-changes → resubmit → accept lifecycle.
- **[Bitemporal Queries](bitemporal-queries.md)** — `time_varying`
  supersession, and reconstructing the past with `as_of(t)`.
- **[Writing a Plugin](writing-a-plugin.md)** — registering a real
  reference plugin through the sandboxed, capability-scoped plugin system.
- **[Hybrid Search](hybrid-search.md)** — combining vector similarity
  search with symbolic `.where()` filters and provenance-confidence floors.
- **[Erasing Personal Data](erasing-personal-data.md)** — the opt-in
  `redaction` mode: marking what identifies a person, who may erase it, and
  how to check the bytes are gone.
