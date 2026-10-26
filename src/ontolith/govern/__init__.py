"""Governance layer - proposals, policy, review, conflict resolution.

This module handles the write path and governance:
- Proposals: staged operations awaiting approval
- Policy: pure functions for auto-accept/review decisions
- Review: human approval workflow (M2)
- Conflict: temporal supersession and contradictions (M2)

`ProposalState`/`ContradictionState` (the `Literal` type aliases annotating
`Proposal.state`/`Contradiction.state`, both pinned attributes of pinned
classes) and `safe_rationale_history` (the defensive coercion every read
surface projecting `Contradiction.metadata["rationale_history"]` needs,
since `metadata` is an open, schema-less blob) are exported here for the
same reason `AsOfView`/`AuthProvider`/`VECTOR_SCOPES` were added to their
own packages during the M4 API-surface-freeze audit (ADR-0019's Update) —
each was already public in spirit (declared in its own module's `__all__`)
but unreachable from this package.
"""

from ontolith.govern.conflict import Activate, ConflictResult, Contradict, Supersede, route
from ontolith.govern.contradiction import Contradiction, ContradictionState, safe_rationale_history
from ontolith.govern.operations import (
    MAX_BATCH_OPERATIONS,
    AssertLiteralOp,
    AssertRefOp,
    BatchOp,
    CreateEntityOp,
    LocalRef,
)
from ontolith.govern.policy import (
    AutoAccept,
    Composite,
    ConfidenceThreshold,
    Decision,
    KbView,
    PolicyStrategy,
    Reject,
    RequireReview,
    RequireReviewByRole,
    RequireReviewForAI,
    SourceQuorum,
    SourceRequired,
    ThresholdPolicy,
)
from ontolith.govern.proposal import Proposal, ProposalEvent, ProposalState
from ontolith.govern.provenance import Provenance

__all__ = [
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
]
