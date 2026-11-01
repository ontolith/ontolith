"""Extending an open contradiction skips the status write for a member that
is already flagged (KI-100)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ontolith import Ontology
from ontolith.core import FixedClock

AUTHOR = "author@example.com"
T0 = datetime(2025, 1, 1, tzinfo=UTC)


@pytest.fixture
def kb(tmp_path: Path) -> Ontology:
    kb = Ontology.connect(tmp_path / "ext.db", clock=FixedClock(T0))
    kb.create_principal(AUTHOR, kind="human", default_capability="write")
    return kb


def test_extension_skips_status_write_for_already_flagged_members(
    kb: Ontology, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = kb.create_entity("Thing", author=AUTHOR).id
    first = kb.assert_literal(subject, "Thing.name", "a", "Text", AUTHOR)
    second = kb.assert_literal(subject, "Thing.name", "b", "Text", AUTHOR)
    assert kb.backend.get_assertion(first.id).status == "flagged"
    assert kb.backend.get_assertion(second.id).status == "flagged"

    writes: list[tuple[str, str]] = []
    real = kb.backend.set_assertion_status

    def spy(assertion_id: str, status: str, valid_to: str | None = None) -> None:
        writes.append((assertion_id, status))
        real(assertion_id, status, valid_to)

    monkeypatch.setattr(kb.backend, "set_assertion_status", spy)
    third = kb.assert_literal(subject, "Thing.name", "c", "Text", AUTHOR)

    # Both earlier members were already flagged: no redundant write.
    assert writes == []
    [contradiction] = kb.contradictions()
    assert set(contradiction.member_ids) == {first.id, second.id, third.id}
    assert kb.backend.get_assertion(third.id).status == "flagged"
    # No new transition event for the already-flagged members.
    assert len(kb.backend.get_assertion_events(first.id)) == 1
    assert len(kb.backend.get_assertion_events(second.id)) == 1
