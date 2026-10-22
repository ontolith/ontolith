"""Open-contradiction extension benchmark (KI-100).

`Ontology._apply_with_conflict_routing`'s "extend an already-open
contradiction" path does backend round-trips per existing member of that
contradiction for every new write that lands in it, so its cost grows with
the member count. `test_traversal.py`'s write benchmarks deliberately keep
every round off this path (a fresh (subject, predicate) per round), so
nothing else in the suite measures it.

Each round here builds its own open contradiction with `members` members
*outside* the timed region (`benchmark.pedantic`'s `setup`), then times one
write that extends it. Parametrized over member counts so the growth is
visible in the benchmark output.

Informational: no budget ceiling is enforced. KI-100 records that no ceiling
is known yet, and this is not a SPEC §9 row; the point is that a regression
or an improvement on this path is now measured at all.

Run with: uv run pytest tests/benchmarks/test_contradiction_extension.py --benchmark-only
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock

AUTHOR_ID = "bench@example.com"
SUBJECT_CONCEPT = "ContradictionBench"
T0 = datetime(2025, 1, 1, tzinfo=UTC)
ROUNDS = 10


@pytest.fixture
def kb(tmp_path: Path) -> Ontology:
    """A small empty KB. No schema is registered, so every predicate is
    static/single and a second differing value opens a contradiction."""
    kb = Ontology.connect(tmp_path / "bench.db", clock=FixedClock(T0))
    kb.create_principal(AUTHOR_ID, kind="human", default_capability="write")
    return kb


@pytest.mark.benchmark
@pytest.mark.parametrize("members", [10, 100, 500])
def test_bench_extend_open_contradiction(benchmark, kb: Ontology, members: int) -> None:
    """One write landing in an open contradiction that already holds
    `members` flagged assertions."""
    subject = kb.create_entity(SUBJECT_CONCEPT, author=AUTHOR_ID).id
    counter = {"n": 0}
    last: dict[str, str] = {}

    def setup() -> tuple[tuple[str, str], dict[str, object]]:
        n = counter["n"]
        counter["n"] += 1
        predicate = f"{SUBJECT_CONCEPT}.field_{n}"
        for i in range(members):
            kb.assert_literal(subject, predicate, f"value-{i}", "Text", AUTHOR_ID)
        last["predicate"] = predicate
        return (predicate, f"value-{members}"), {}

    def extend(predicate: str, value: str) -> None:
        kb.assert_literal(subject, predicate, value, "Text", AUTHOR_ID)

    benchmark.pedantic(extend, setup=setup, rounds=ROUNDS, iterations=1)

    # Sanity check, outside the timed region: the timed write really did
    # extend one open contradiction (not corroborate or open a new one).
    [contradiction] = [
        c for c in kb.contradictions() if c.subject == subject and c.predicate == last["predicate"]
    ]
    assert len(contradiction.member_ids) == members + 1
