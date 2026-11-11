# Examples

Every script below lives in
[`examples/`](https://github.com/ontolith/ontolith/tree/main/examples) in the
repository and is independently runnable — each connects to a temporary
SQLite database, runs, prints what it's doing, and cleans up after itself.

```bash
uv sync
uv run python examples/quickstart.py
uv run python examples/governance_and_review.py
uv run python examples/bitemporal_queries.py
uv run python examples/writing_a_plugin.py
uv run python examples/hybrid_search.py
uv run python examples/erasing_personal_data.py
```

| Script | Demonstrates | Tutorial |
|---|---|---|
| [`quickstart.py`](https://github.com/ontolith/ontolith/blob/main/examples/quickstart.py) | Connecting, schema, principals, entities, literal/ref assertions, provenance | [Getting Started](getting-started.md) |
| [`governance_and_review.py`](https://github.com/ontolith/ontolith/blob/main/examples/governance_and_review.py) | AI-vs-human proposal paths, request_changes → resubmit → accept, contradiction routing | [Governance & Review](tutorials/governance-and-review.md) |
| [`bitemporal_queries.py`](https://github.com/ontolith/ontolith/blob/main/examples/bitemporal_queries.py) | `time_varying` supersession, an injected `FixedClock`, `as_of(t)` time-travel | [Bitemporal Queries](tutorials/bitemporal-queries.md) |
| [`writing_a_plugin.py`](https://github.com/ontolith/ontolith/blob/main/examples/writing_a_plugin.py) | Registering & calling a real reference plugin through the process-isolated sandbox | [Writing a Plugin](tutorials/writing-a-plugin.md) |
| [`hybrid_search.py`](https://github.com/ontolith/ontolith/blob/main/examples/hybrid_search.py) | `.semantic()`, symbolic `.where()` intersection, `.min_confidence()` | [Hybrid Search](tutorials/hybrid-search.md) |
| [`erasing_personal_data.py`](https://github.com/ontolith/ontolith/blob/main/examples/erasing_personal_data.py) | A `redaction`-mode knowledge base, `personal` schema marks, `erase_subject`, `Assertion.redacted`, confirming the bytes are gone from the file | [Erasing Personal Data](tutorials/erasing-personal-data.md) |

These same scripts are run in CI (`ci.yml`'s "Tests" job) against every
push to `main`, so they stay accurate as the API evolves — if one of them
breaks, that's a CI failure, not a stale doc.
