# Ontolith

[![CI](https://github.com/ontolith/ontolith/workflows/CI/badge.svg)](https://github.com/ontolith/ontolith/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A Python framework/SDK for building collaborative knowledge bases where humans and AI agents are co-equal authors of a shared, governed ontology.

## Features

- **Dual-principal model**: Humans and AI agents as co-equal authors
- **Provenance & confidence**: Every assertion tracked with source and confidence
- **Governed collaboration**: Configurable proposal/review workflow
- **Bitemporal time-travel**: Query knowledge as of any point in time
- **Conflict handling**: Temporal supersession and explicit contradictions
- **Plugin extensibility**: Importers, exporters, reasoners, validators

## Quick Start

```bash
# Clone repository
git clone https://github.com/ontolith/ontolith.git
cd ontolith

# Install dependencies (requires Python 3.11+)
uv sync

# Run tests
uv run pytest

# Run quality checks
uv run ruff check
uv run mypy --strict src conformance/conftest.py

# Run the quickstart example
uv run python examples/quickstart.py
```

## Development Status

✅ **v1.1.0** - M0–M4 complete; 1.0.0 tagged 2026-09-28

Install with `pip install ontolith` (see Quick Start above). See [docs/Ontolith_Implementation_Plan.md](docs/Ontolith_Implementation_Plan.md) for the full milestone history and [vNext](docs/Ontolith_Implementation_Plan.md) plans.

## Documentation

**[Full documentation site](https://ontolith.github.io/ontolith/)** — a Getting
Started guide, conceptual overview, tutorials (governance & review,
bitemporal queries, writing a plugin, hybrid search), and the auto-generated
API reference. Built from [`site_docs/`](site_docs/) via
[`mkdocs.yml`](mkdocs.yml); deployed by
[`.github/workflows/docs.yml`](.github/workflows/docs.yml) on every push to
`main`.

Reference/planning documents:

- [Product Requirements](docs/Ontolith_PRD.md)
- [Technical Specification](docs/Ontolith_SPEC.md)
- [Use Cases & Interfaces](docs/Ontolith_UseCases_and_Interfaces.md)
- [Implementation Plan](docs/Ontolith_Implementation_Plan.md)
- [Architecture Decision Records](docs/adr/)
- [Known Issues](docs/known-issues.md)

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Apache-2.0 - See [LICENSE](LICENSE) for details.