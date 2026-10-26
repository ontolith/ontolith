"""Multi-operation proposal benchmark (ADR-0055).

`propose_batch` evaluates the configured policy once per operation, so a
batch's cost grows with its size. This times a full 100-operation batch
(the cap): one entity plus 99 assertions on it, auto-accepted, including the
per-operation policy evaluation and the single-transaction apply.

Informational: no budget ceiling is enforced (it is not a SPEC §9 row); the
point is that a regression on this path is measured at all.

Run with: uv run pytest tests/benchmarks/test_propose_batch.py --benchmark-only
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import AssertLiteralOp, CreateEntityOp, LocalRef, Ontology
from ontolith.core import FixedClock
from ontolith.govern import MAX_BATCH_OPERATIONS

AUTHOR_ID = "bench@example.com"
T0 = datetime(2025, 1, 1, tzinfo=UTC)
ROUNDS = 10


@pytest.fixture
def kb(tmp_path: Path) -> Ontology:
    kb = Ontology.connect(tmp_path / "bench.db", clock=FixedClock(T0))
    kb.create_principal(AUTHOR_ID, kind="human", default_capability="write")
    return kb


@pytest.mark.benchmark
@pytest.mark.parametrize("size", [10, MAX_BATCH_OPERATIONS])
def test_bench_propose_batch(benchmark, kb: Ontology, size: int) -> None:
    """One auto-accepted batch of `size` operations."""
    counter = {"n": 0}

    def build() -> tuple[tuple[list[CreateEntityOp | AssertLiteralOp]], dict[str, object]]:
        n = counter["n"]
        counter["n"] += 1
        ops: list[CreateEntityOp | AssertLiteralOp] = [CreateEntityOp(key="e", concept="Bench")]
        ops += [
            AssertLiteralOp(
                subject=LocalRef("e"),
                predicate=f"Bench.field_{n}_{i}",
                value=f"value-{i}",
                value_type="Text",
                source="bench",
            )
            for i in range(size - 1)
        ]
        return (ops,), {}

    def run(ops: list[CreateEntityOp | AssertLiteralOp]) -> None:
        kb.propose_batch(ops, AUTHOR_ID)

    benchmark.pedantic(run, setup=build, rounds=ROUNDS, iterations=1)

    assert len(kb.backend.entities(concept="Bench")) == ROUNDS
