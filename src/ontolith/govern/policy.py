"""Policy engine - pure functions for proposal evaluation.

Per SPEC §9.2: Policy is a pure function that takes a proposal and principal
and returns a decision (auto-accept, require review, or reject). "Pure" means
no writes and deterministic given its inputs — a strategy MAY read via ``kb``
(a pinned snapshot, not live KB state) without violating this.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Protocol

from ontolith.govern.proposal import Proposal
from ontolith.identity import Principal, min_capability

if TYPE_CHECKING:
    from ontolith.core import Assertion


class Decision:
    """Base class for policy decisions."""

    pass


class AutoAccept(Decision):
    """Automatically accept this proposal."""

    reason: str

    def __init__(self, reason: str = "Policy approved") -> None:
        self.reason = reason


class RequireReview(Decision):
    """Require human review before accepting."""

    reviewers: list[str]
    reason: str

    def __init__(self, reviewers: list[str], reason: str) -> None:
        # Copied, not aliased: several strategies (SourceQuorum,
        # ConfidenceThreshold, SourceRequired, RequireReviewByRole) hand this
        # constructor their own `self.reviewers`/config list directly. Without
        # a copy here, a caller mutating a returned Decision's `.reviewers`
        # (e.g. `decision.reviewers.append(...)`) would silently rewrite the
        # strategy's own configuration for every future evaluation - breaking
        # SPEC §9.2 purity (a strategy's decisions must depend only on its
        # declared inputs, not on what some earlier caller did to a decision
        # object) (found in review, KI-069).
        self.reviewers = list(reviewers)
        self.reason = reason


class Reject(Decision):
    """Reject this proposal."""

    reason: str

    def __init__(self, reason: str) -> None:
        self.reason = reason


class KbView(Protocol):
    """Structural read view a PolicyStrategy may query for KB-inspecting decisions.

    Deliberately narrower than SPEC §9.2's literal ``kb: ReadOnlyView`` — that
    class (``ontolith.plugins.views.ReadOnlyView``) wraps a *live*
    ``Ontology`` and isn't even the right type here (§9.2 requires
    ``evaluate`` to be "testable and replayable", which a live, unpinned view
    can't satisfy). Real callers pass a bitemporally-pinned ``AsOfView``
    (``ontology.py``) instead. Importing either concrete class into this
    module would also be circular: both live in modules that import
    ``Decision``/``PolicyStrategy`` from here. This minimal Protocol is the
    common shape both classes already satisfy structurally, with no
    inheritance or import required (KI-017, ADR-0025).

    The two concrete views a strategy might actually receive have different
    runtime semantics for the same call: ``AsOfView`` (what ``Ontology``
    passes) is temporally pinned and excludes flagged assertions by default;
    ``ReadOnlyView`` is live and defaults to ``status="active"``. Real
    callers only ever pass ``AsOfView`` today.
    """

    def assertions(
        self, subject: str | None = None, predicate: str | None = None
    ) -> list[Assertion]:
        """Assertions visible to this view, optionally filtered."""
        ...


class PolicyStrategy(Protocol):
    """Protocol for policy strategies.

    Policy strategies are PURE functions: no writes, deterministic given
    their inputs, testable. Reading via ``kb`` is permitted — it's a pinned
    snapshot (ADR-0025), not live, mutable KB state.
    """

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate a proposal and return a decision.

        Args:
            proposal: Proposal to evaluate
            principal: Principal who authored the proposal
            kb: Read view of the KB, pinned to the proposal's creation time
                (SPEC §9.2's ``kb`` parameter — ADR-0025)
            acting_as: Principal being delegated to, if any (SPEC §8.4)

        Returns:
            Decision (AutoAccept, RequireReview, or Reject)
        """
        ...


class ThresholdPolicy:
    """Threshold policy evaluating capability and trust level.

    Rules (evaluated in order):
    - AI principals: always require review, regardless of trust level or
      delegation (ADR-0003) — the author's own kind is never laundered away
      by delegating to a higher-capability principal.
    - Human/service with write/review/admin capability: auto-accept
    - Read-only principals: reject immediately (no propose rights)
    - Non-AI principals with propose capability + trust_level >= 5: auto-accept
    - Everyone else: require review

    When ``acting_as`` is set (delegation), the effective capability is
    min(capability(principal), capability(acting_as)) and the effective trust
    level is the more conservative (lower) of the two (SPEC §8.4) — the
    delegating principal's capability is never substituted wholesale.
    """

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView | None = None,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate proposal based on principal capabilities and trust level.

        ``kb`` is accepted for ``PolicyStrategy`` conformance but never read —
        this strategy's decisions depend only on ``principal``/``acting_as``,
        so it defaults to ``None`` rather than requiring every caller
        (including the many pre-existing pure tests of this class) to
        construct a KB view it will never use (ADR-0025).
        """
        # AI always requires review — trust level and delegation never override
        # this (ADR-0003). Checked first, before any effective-capability math,
        # so an AI author's own kind can never be bypassed via acting_as.
        if principal.kind == "ai":
            reviewers = [principal.owner] if principal.owner else []
            return RequireReview(
                reviewers=reviewers,
                reason=f"AI proposals require review (owner: {principal.owner})",
            )

        capability: str = principal.default_capability
        trust_level = principal.trust_level
        if acting_as is not None:
            capability = min_capability(capability, acting_as.default_capability)
            trust_level = min(trust_level, acting_as.trust_level)

        # Humans with write capability can auto-accept
        if principal.kind == "human" and capability in ("write", "review", "admin"):
            return AutoAccept(f"Trusted human principal ({principal.id})")

        # Service principals with write can auto-accept
        if principal.kind == "service" and capability in ("write", "admin"):
            return AutoAccept(f"Trusted service principal ({principal.id})")

        # Read-only principals cannot propose — reject immediately
        if capability == "read":
            return Reject(f"Principal {principal.id} has read-only access and cannot propose")

        # Trust-elevated propose: sufficient trust lifts a non-AI propose-capability principal
        if capability == "propose" and trust_level >= 5:
            return AutoAccept(f"Trust-elevated principal ({principal.id}, trust={trust_level})")

        # Default: require review
        return RequireReview(
            reviewers=[],
            reason=f"Principal {principal.id} requires review approval",
        )


class SourceQuorum:
    """Auto-accepts once ``threshold`` distinct sources corroborate a fact (SPEC §9.2).

    Counts the proposal's own source together with existing assertions
    visible in ``kb`` (i.e. active as of the proposal's creation time) on the
    same ``(subject, predicate, value)`` that carry a non-``None`` source — an
    assertion with no recorded source can't establish independent
    corroboration, so it never counts toward the quorum.

    Rejects principals without at least ``propose`` capability, mirroring
    ``ThresholdPolicy``'s read-only rejection — with no other pre-write
    capability check in `Ontology.propose`/`propose_ref`/`retract`, KI-015's
    resolution requires every ``PolicyStrategy`` to enforce this floor itself
    (`docs/known-issues.md`).

    Retractions and sourceless proposals always require review: a
    retraction isn't a corroborable fact, and a quorum can't be established
    without knowing the proposing source. Only the proposal's first staged
    operation is inspected; `Ontology` calls it once per operation of a
    multi-operation proposal (ADR-0055), each time with a single-operation
    view. A `create_entity` operation requires review.

    Does not special-case AI-authored proposals: ``ThresholdPolicy``'s
    "AI always requires review" rule (ADR-0003) is that strategy's own
    design choice, not a cross-cutting invariant every ``PolicyStrategy``
    must reimplement — an AI-authored proposal CAN auto-accept here once
    quorum is reached (`conformance/test_source_quorum_policy.py` pins this
    as deliberate, unchanged behavior). A deployment that wants both rules
    combines them explicitly via ``Composite`` (KI-061, ADR-0040) — e.g.
    ``Composite(all=[SourceQuorum(2), some_ai_review_strategy])`` — rather
    than this class silently gaining an AI check of its own.

    Corroboration is checked against ``kb``'s pinned-at-``created_at`` state
    only — it does not compare the proposal's own ``valid_from``/``valid_to``
    against the matching existing assertions' validity windows. For a
    ``time_varying`` predicate with a backfilled or future-dated proposal,
    this can compare facts from different real-world periods; SourceQuorum
    is best suited to ``static`` properties.
    """

    def __init__(self, threshold: int, reviewers: list[str] | None = None) -> None:
        """Configure the strategy.

        Args:
            threshold: Minimum number of distinct sources required to
                auto-accept. Must be >= 1.
            reviewers: Reviewers assigned when the quorum isn't met.
                Defaults to no reviewers assigned.

        Raises:
            ValueError: threshold is less than 1
        """
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.threshold = threshold
        self.reviewers = list(reviewers) if reviewers is not None else []

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate proposal based on capability and distinct-source corroboration count."""
        capability: str = principal.default_capability
        if acting_as is not None:
            capability = min_capability(capability, acting_as.default_capability)
        if capability == "read":
            return Reject(f"Principal {principal.id} has read-only access and cannot propose")

        operations = proposal.payload.get("operations") or []
        if not operations:
            return RequireReview(self.reviewers, "Proposal has no staged operations")
        op = operations[0]
        kind = op.get("kind")

        if kind == "retract":
            return RequireReview(self.reviewers, "SourceQuorum does not auto-accept retractions")
        if kind not in ("assert_literal", "assert_ref"):
            return RequireReview(
                self.reviewers, f"SourceQuorum does not recognize op kind {kind!r}"
            )

        source = op.get("source")
        if not source:
            return RequireReview(
                self.reviewers,
                f"Proposal has no source; quorum of {self.threshold} cannot be established",
            )

        value_kind = "literal" if kind == "assert_literal" else "ref"
        value = op["value"] if kind == "assert_literal" else op["target"]
        existing = kb.assertions(subject=op["subject"], predicate=op["predicate"])
        sources = {
            a.source
            for a in existing
            if a.value == value and a.value_kind == value_kind and a.source
        }
        sources.add(source)

        if len(sources) >= self.threshold:
            return AutoAccept(f"Source quorum reached ({len(sources)}/{self.threshold})")
        return RequireReview(
            self.reviewers,
            f"Source quorum not met ({len(sources)}/{self.threshold} distinct sources)",
        )


class ConfidenceThreshold:
    """Auto-accepts once a proposal's own asserted confidence meets ``threshold`` (SPEC §9.2).

    Unlike ``SourceQuorum``, this never reads ``kb`` — the decision depends
    only on the confidence value the proposal's own author already staged
    on the operation (``propose``/``propose_ref``'s ``confidence``
    parameter), not on any corroborating state. This deliberately does not
    combine or average multiple assertions' confidence — SPEC's "confidence
    is NEVER auto-combined in v1" invariant applies here exactly as it does
    everywhere else; this strategy only ever compares the single scalar the
    proposal itself carries.

    A proposal with no confidence (``None`` — the default when a caller
    doesn't pass one) always requires review, never auto-accepts: silently
    treating "no stated confidence" as "confidence 0" or "confidence 1"
    would both be a guess this strategy has no basis to make, and SPEC's own
    confidence model has no concept of an implicit default.

    Retractions and unrecognized operation kinds always require review, for
    the same reason ``SourceQuorum`` does: a retraction carries no
    confidence value at all (``Ontology.retract``'s payload has no
    ``confidence`` key), so there's nothing here to threshold against. Only
    the proposal's first staged operation is inspected; `Ontology` calls it once per operation of a
    multi-operation proposal (ADR-0055), each time with a single-operation
    view. A ``create_entity`` operation requires review.

    Rejects principals without at least ``propose`` capability, mirroring
    ``SourceQuorum``'s own KI-015 floor enforcement — this floor is not
    inherited for free by any new ``PolicyStrategy``
    (``docs/known-issues.md`` KI-015).

    Does not special-case AI-authored proposals, for the same reason
    ``SourceQuorum`` doesn't (ADR-0025 §5): ``ThresholdPolicy``'s "AI always
    requires review" rule is that strategy's own design choice, not a
    cross-cutting invariant. A deployment wanting both combines them via
    ``Composite`` (ADR-0040).
    """

    def __init__(self, threshold: float, reviewers: list[str] | None = None) -> None:
        """Configure the strategy.

        Args:
            threshold: Minimum confidence (inclusive) required to
                auto-accept. Must be within [0.0, 1.0], matching SPEC's own
                confidence scalar range.
            reviewers: Reviewers assigned when confidence is below
                threshold, missing, or the operation isn't evaluable.
                Defaults to no reviewers assigned.

        Raises:
            ValueError: threshold is outside [0.0, 1.0]
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0.0 and 1.0")
        self.threshold = threshold
        self.reviewers = list(reviewers) if reviewers is not None else []

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView | None = None,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate proposal based on capability and the operation's own confidence.

        ``kb`` is accepted for ``PolicyStrategy`` conformance but never
        read — this strategy's decision depends only on the proposal's own
        payload and the acting principal's capability.
        """
        capability: str = principal.default_capability
        if acting_as is not None:
            capability = min_capability(capability, acting_as.default_capability)
        if capability == "read":
            return Reject(f"Principal {principal.id} has read-only access and cannot propose")

        operations = proposal.payload.get("operations") or []
        if not operations:
            return RequireReview(self.reviewers, "Proposal has no staged operations")
        op = operations[0]
        kind = op.get("kind")

        if kind == "retract":
            return RequireReview(
                self.reviewers, "ConfidenceThreshold does not auto-accept retractions"
            )
        if kind not in ("assert_literal", "assert_ref"):
            return RequireReview(
                self.reviewers, f"ConfidenceThreshold does not recognize op kind {kind!r}"
            )

        confidence = op.get("confidence")
        if confidence is None:
            return RequireReview(
                self.reviewers,
                "Proposal has no confidence value; cannot compare against threshold",
            )

        if confidence >= self.threshold:
            return AutoAccept(f"Confidence {confidence} meets threshold {self.threshold}")
        return RequireReview(
            self.reviewers,
            f"Confidence {confidence} below threshold {self.threshold}",
        )


class SourceRequired:
    """Auto-accepts only when a proposal's operation carries a non-empty ``source`` (SPEC §9.2).

    A narrower, unconditional cousin of ``SourceQuorum``: where
    ``SourceQuorum`` counts *distinct* sources across the proposal and
    existing corroborating assertions, ``SourceRequired`` only checks that
    *this* proposal names *any* source at all — no ``kb`` read, no
    corroboration count, no threshold. A deployment that wants both
    ("every fact needs a source, AND at least 2 of them") composes
    ``Composite(all=[SourceRequired(), SourceQuorum(2)])`` (ADR-0040)
    rather than this strategy reimplementing quorum counting itself.

    Retractions and unrecognized operation kinds always require review, the
    same treatment ``SourceQuorum``/``ConfidenceThreshold`` give them:
    ``Ontology.retract``'s payload carries no ``source`` key, so there's
    nothing here to check. Only the proposal's first staged operation is
    inspected, matching every sibling strategy in this module.

    Rejects principals without at least ``propose`` capability (KI-015
    floor, `docs/known-issues.md`) and does not special-case AI-authored
    proposals, for the identical reasons ``SourceQuorum``/
    ``ConfidenceThreshold`` document on themselves.
    """

    def __init__(self, reviewers: list[str] | None = None) -> None:
        """Configure the strategy.

        Args:
            reviewers: Reviewers assigned when no source is present, or the
                operation isn't evaluable. Defaults to no reviewers
                assigned.
        """
        self.reviewers = list(reviewers) if reviewers is not None else []

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView | None = None,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate proposal based on capability and presence of a source.

        ``kb`` is accepted for ``PolicyStrategy`` conformance but never
        read — presence of a source is entirely determined by the
        proposal's own payload.
        """
        capability: str = principal.default_capability
        if acting_as is not None:
            capability = min_capability(capability, acting_as.default_capability)
        if capability == "read":
            return Reject(f"Principal {principal.id} has read-only access and cannot propose")

        operations = proposal.payload.get("operations") or []
        if not operations:
            return RequireReview(self.reviewers, "Proposal has no staged operations")
        op = operations[0]
        kind = op.get("kind")

        if kind == "retract":
            return RequireReview(self.reviewers, "SourceRequired does not auto-accept retractions")
        if kind not in ("assert_literal", "assert_ref"):
            return RequireReview(
                self.reviewers, f"SourceRequired does not recognize op kind {kind!r}"
            )

        source = op.get("source")
        if not source:
            return RequireReview(self.reviewers, "Proposal has no source")
        return AutoAccept(f"Source provided: {source!r}")


class RequireReviewByRole:
    """Always routes to review, assigning reviewers by the author's declared role (SPEC §9.2).

    Unlike every other strategy in this module, ``RequireReviewByRole``
    never auto-accepts and never inspects the proposal's payload at all —
    its entire purpose is *which reviewers* a proposal gets routed to, not
    whether it needs review in the first place. A deployment that also
    wants some proposals to skip review entirely composes this with an
    accepting strategy via ``Composite(any=[...])`` (ADR-0040); used alone,
    every proposal it evaluates lands in ``require_review``.

    "Role" has no dedicated field on ``Principal`` — SPEC names ``kind``
    (human/ai/service), capability, and trust level, but nothing called
    "role". This strategy reads ``principal.metadata.get("role")``:
    ``Principal.metadata`` is already documented as "open JSON blob for
    future extension", exactly the mechanism a deployment-specific concept
    like organizational role is meant to use without requiring a schema
    change to ``Principal`` itself. This is a deliberate, not accidental,
    choice — see ADR-0045 for the alternatives considered (a dedicated
    ``Principal.role`` field, keying by ``kind`` instead) and why they were
    rejected.

    Role is read from ``principal`` (the author) only, never from
    ``acting_as`` — mirroring ``ThresholdPolicy``'s "AI's own kind is never
    laundered away by delegating" precedent (ADR-0003): reviewer routing is
    about who is really proposing, not who they're temporarily acting as,
    so delegation can't be used to dodge a role's assigned reviewers.

    A missing role, a non-``str`` role (``metadata`` is an unvalidated
    ``dict[str, Any]``), and an unmapped role all fall back to ``default``
    — each distinguished in the returned reason text, so callers can tell
    "nobody declared a role" from "a declared role isn't usable" from "a
    role was declared but nothing routes it" without inspecting
    ``principal`` themselves — never an error and never an empty
    ``RequireReview`` with no explanation.

    Rejects principals without at least ``propose`` capability (KI-015
    floor, `docs/known-issues.md`), matching every sibling strategy in this
    module, evaluated with the same effective (``acting_as``-aware)
    capability ``ThresholdPolicy``/``SourceQuorum`` use — unlike role
    itself, capability gating is deliberately not laundering-resistant in
    the same way, since SPEC §8.4 already defines delegation's effective
    capability as the more conservative of the two principals.
    """

    def __init__(
        self,
        role_reviewers: Mapping[str, Sequence[str]],
        *,
        default: Sequence[str] | None = None,
    ) -> None:
        """Configure the strategy.

        Args:
            role_reviewers: Maps a role name (as found in
                ``principal.metadata["role"]``) to the reviewers assigned
                when a proposal's author has that role.
            default: Reviewers assigned when the author has no declared
                role, or a role not present in ``role_reviewers``. Defaults
                to no reviewers assigned.
        """
        self._role_reviewers: dict[str, list[str]] = {
            role: list(reviewers) for role, reviewers in role_reviewers.items()
        }
        self._default = list(default) if default is not None else []

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView | None = None,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Route to review, with reviewers chosen by the author's declared role.

        ``kb``/``proposal`` are accepted for ``PolicyStrategy`` conformance
        but never read — this strategy's decision depends only on
        ``principal``/``acting_as``, matching ``ThresholdPolicy``'s own
        "kb defaults to None, never used" shape.
        """
        capability: str = principal.default_capability
        if acting_as is not None:
            capability = min_capability(capability, acting_as.default_capability)
        if capability == "read":
            return Reject(f"Principal {principal.id} has read-only access and cannot propose")

        role = principal.metadata.get("role")
        if role is None:
            return RequireReview(
                self._default,
                f"Principal {principal.id} has no declared role; using default reviewers",
            )
        # `metadata` is an unvalidated `dict[str, Any]` - a non-str value
        # (e.g. a list, accidentally hashable-looking int) gets its own
        # reason distinct from "no role declared" above, kept a third,
        # explicit case rather than an error out of evaluate() (SPEC §16) or
        # a silent re-use of the "missing" wording (which would misreport a
        # role that *was* declared, just not usably). This also sidesteps a
        # non-hashable value (e.g. `{"role": ["legal"]}`) raising `TypeError`
        # from the `in` check below (found in review, KI-069).
        if not isinstance(role, str):
            return RequireReview(
                self._default,
                f"Principal {principal.id}'s declared role is not a string "
                f"({role!r}); using default reviewers",
            )
        if role not in self._role_reviewers:
            return RequireReview(
                self._default,
                f"No reviewers configured for role {role!r}; using default reviewers",
            )
        return RequireReview(
            self._role_reviewers[role],
            f"Requires review by role {role!r} (principal: {principal.id})",
        )


class RequireReviewForAI:
    """Routes AI-kind principals to review; AutoAccepts everyone else (SPEC §9.2, KI-088).

    Promotes the composition pattern ``Composite``'s own docstring has
    sketched inline since ADR-0040 into a real, exported, tested class —
    ``ThresholdPolicy`` is the only strategy that unconditionally routes
    AI-kind principals to review; every non-default strategy in this
    module (``SourceQuorum``, ``ConfidenceThreshold``, ``SourceRequired``)
    deliberately does not special-case AI authorship on its own (KI-061,
    ADR-0025), so a
    deployment wanting both composes a small AI-blocking strategy
    alongside one — this is that strategy, shippable instead of
    hand-derived from a comment each time::

        policy = Composite(all=[RequireReviewForAI(), SourceQuorum(2)])

    This *is* a reversal of ADR-0040's own explicit decision to not ship
    this class — its Alternatives Considered rejected exactly this,
    preferring a docstring example over "a new public symbol with its own
    maintenance surface." KI-088 revisits that call explicitly (recorded
    in ADR-0040's own 2026-09-09 update) rather than letting the pattern
    silently drift into being shipped without anyone deciding it should
    be: the tradeoff ADR-0040 weighed hasn't changed (SPEC §9.2 still
    doesn't name an "AI-review" strategy, the class is still small), but
    the docstring-sketch pattern's real cost — every deployer re-deriving
    it from a comment, correctly, each time — outweighed that concern once
    it had been in production use since KI-061. ``RequireReviewForAI``
    does only the one thing its name says; using the whole of
    ``ThresholdPolicy`` in ``Composite`` instead would re-impose *its* own
    capability/trust-level gate too, defeating the point of choosing a
    different strategy (like ``SourceQuorum``) in the first place — that
    part of ADR-0040's original reasoning is unchanged.

    ``evaluate()`` returns ``RequireReview([principal.owner], ...)`` for an
    AI-kind principal (an empty reviewer list if ``owner`` is falsy — SPEC
    §8.1 requires AI principals to declare one in practice, but this
    strategy doesn't assume it), and ``AutoAccept(...)`` for everyone else.

    Rejects principals without at least ``propose`` capability first,
    matching every sibling strategy's own KI-015 floor enforcement
    (`docs/known-issues.md`) — checked *before* the AI-kind test, unlike
    ``ThresholdPolicy``'s ordering (AI-kind first, capability math never
    reached for an AI author). ``ThresholdPolicy`` can get away with
    AI-first because it has no other reachable outcome for a read-only AI
    that would differ; here, checking capability first means a read-only
    principal of *any* kind is rejected the same way every other strategy
    in this module already rejects one, rather than a read-only AI
    reaching ``RequireReview`` (a working, if misleading, outcome) while a
    read-only human reaches it via a different, uncomposed strategy's own
    check. Composition doesn't change the practical result either way —
    ``Composite(all=...)``'s most-restrictive-wins merge treats
    ``Reject``/``RequireReview`` identically as "the group doesn't
    auto-accept" — but this class enforces its own floor rather than
    depending on being composed with a strategy that does, so it behaves
    correctly standalone too.

    AI-kind is read from ``principal`` (the author) only, never from
    ``acting_as`` — mirroring ``ThresholdPolicy``'s "AI's own kind is
    never laundered away by delegating" precedent (comment at this
    module's ``ThresholdPolicy.evaluate``; SPEC §8.4 defines delegation's
    effective *capability* as the more conservative of the two principals,
    but says nothing about laundering *kind*, so this strategy makes the
    same explicit choice every other kind-sensitive check in this module
    already makes).
    """

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView | None = None,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate proposal based on capability floor and the author's kind.

        ``proposal``/``kb`` are accepted for ``PolicyStrategy`` conformance
        but never read — this strategy's decision depends only on
        ``principal``/``acting_as``, matching ``ThresholdPolicy``'s/
        ``RequireReviewByRole``'s own "kb defaults to None, never used" shape.
        """
        capability: str = principal.default_capability
        if acting_as is not None:
            capability = min_capability(capability, acting_as.default_capability)
        if capability == "read":
            return Reject(f"Principal {principal.id} has read-only access and cannot propose")

        if principal.kind == "ai":
            reviewers = [principal.owner] if principal.owner else []
            return RequireReview(
                reviewers, f"AI proposals require review (owner: {principal.owner})"
            )
        return AutoAccept(f"Principal {principal.id} is not AI-kind")


def _merge_reject(decisions: list[Reject]) -> Reject:
    """Combine multiple Reject decisions into one, concatenating reasons in
    input order (each retains its originating strategy's own wording)."""
    return Reject("; ".join(d.reason for d in decisions))


def _merge_require_review(decisions: list[RequireReview]) -> RequireReview:
    """Combine multiple RequireReview decisions into one: reviewers are a
    dedup'd union preserving first-seen order (the same reviewer named by
    two strategies is only assigned once); reasons are concatenated in
    input order."""
    reviewers: list[str] = []
    seen: set[str] = set()
    for d in decisions:
        for reviewer in d.reviewers:
            if reviewer not in seen:
                seen.add(reviewer)
                reviewers.append(reviewer)
    return RequireReview(reviewers, "; ".join(d.reason for d in decisions))


def _merge_auto_accept(decisions: list[AutoAccept]) -> AutoAccept:
    """Combine multiple AutoAccept decisions into one, concatenating reasons
    in input order."""
    return AutoAccept("; ".join(d.reason for d in decisions))


def _most_restrictive(decisions: list[Decision]) -> Decision:
    """The most restrictive decision among `decisions` (Reject > RequireReview
    > AutoAccept), merging every decision at that severity level.

    Used for `Composite(all=...)`: every strategy must independently agree
    to auto-accept — one Reject or RequireReview anywhere in the group
    overrides every AutoAccept the others returned, since "all" strategies
    must approve for the group as a whole to approve.
    """
    rejects = [d for d in decisions if isinstance(d, Reject)]
    if rejects:
        return _merge_reject(rejects)
    reviews = [d for d in decisions if isinstance(d, RequireReview)]
    # A Decision that is none of the three shipped types is not an accept:
    # treat it as requiring review rather than dropping it, so it can only
    # make the group stricter (a custom strategy's own subclass must never
    # fail open into AutoAccept).
    unknown = [d for d in decisions if not isinstance(d, AutoAccept | RequireReview | Reject)]
    if reviews or unknown:
        return _merge_require_review(
            [
                *reviews,
                *(RequireReview([], f"Unrecognized decision {type(d).__name__}") for d in unknown),
            ]
        )
    accepts = [d for d in decisions if isinstance(d, AutoAccept)]
    return _merge_auto_accept(accepts)


def _least_restrictive(decisions: list[Decision]) -> Decision:
    """The least restrictive decision among `decisions` (AutoAccept >
    RequireReview > Reject), merging every decision at that severity level.

    Used for `Composite(any=...)`: only one strategy needs to approve — a
    single AutoAccept anywhere in the group is enough for the group as a
    whole to approve, regardless of what the others returned.
    """
    accepts = [d for d in decisions if isinstance(d, AutoAccept)]
    if accepts:
        return _merge_auto_accept(accepts)
    reviews = [d for d in decisions if isinstance(d, RequireReview)]
    if reviews:
        return _merge_require_review(reviews)
    rejects = [d for d in decisions if isinstance(d, Reject)]
    return _merge_reject(rejects)


class Composite:
    """Combines PolicyStrategy instances into one decision (SPEC §9.2's
        ``Composite(all=…, any=…)``).

        Every strategy in ``all`` must independently return ``AutoAccept`` for
        the ``all`` group to approve — a single ``Reject``/``RequireReview``
        anywhere in the group overrides every ``AutoAccept`` the others
        returned. At least one strategy in ``any`` must return ``AutoAccept``
        for the ``any`` group to approve. Both groups must approve for
        ``Composite`` itself to auto-accept (an unset group is excluded from
        the combination entirely, rather than contributing a placeholder
        decision — a caller passing only ``all=`` or only ``any=`` gets exactly
        that group's own semantics, unconstrained by the other).

        This is SPEC §9.2's sanctioned way to layer an unconditional rule (e.g.
        ThresholdPolicy's "AI principals always require review", ADR-0003) on
        top of a KB-inspecting strategy like ``SourceQuorum``, which
        deliberately does not special-case AI authorship on its own (KI-061,
        ADR-0025 §5). ``RequireReviewForAI`` (KI-088) is exactly that small
        unconditional-half strategy, shipped rather than hand-derived::

            policy = Composite(all=[RequireReviewForAI(), SourceQuorum(2)])

    ``RequireReviewForAI`` is not ``ThresholdPolicy`` under a new name —
    using the whole of ``ThresholdPolicy`` here would re-impose *its own*
    trust-level gate too (on top of the KI-015 capability floor
    ``RequireReviewForAI`` already enforces on its own), defeating the point
    of choosing ``SourceQuorum`` in the first place. Composed explicitly
    like this, ``RequireReviewForAI`` does only the AI-review half.

        When multiple strategies in the same group land at the same decision
        severity (e.g. two ``RequireReview``s), their reviewers are merged as a
        dedup'd union and their reasons are concatenated — every contributing
        strategy's rationale is preserved, not just the first one evaluated.

        Reads via ``kb`` are still permitted (this composes, not replaces, the
        contained strategies) — purity/determinism holds as long as every
        contained strategy holds it.

        ``Composite`` does not itself enforce KI-015's read-capability floor
        (``docs/known-issues.md``: "any future ``PolicyStrategy`` needs to make
        the same deliberate choice; it is not inherited for free") — it is
        exactly as permissive or restrictive as its contained strategies, by
        design, since it is a combinator rather than a leaf strategy. This is
        safe for ``all=``: any member's own ``Reject`` for a read-capability
        principal (e.g. ``SourceQuorum``'s) still wins, since ``all`` uses the
        most-restrictive decision. It is a real sharp edge for ``any=``: if one
        member in the group doesn't check capability at all and would
        otherwise ``AutoAccept``, that member's decision can win the group even
        though a stricter sibling (like ``SourceQuorum``) would have rejected
        the same principal — the same "OR" semantics that let one strategy's
        approval cover for another's stricter rule also let it cover for a
        missing capability check. Every strategy composed into an ``any=``
        group should enforce the floor itself if that matters for the
        deployment, the same way ``SourceQuorum`` already does.
    """

    def __init__(
        self,
        *,
        all: Sequence[PolicyStrategy] = (),
        any: Sequence[PolicyStrategy] = (),
    ) -> None:
        """Configure the strategy.

        Args:
            all: Strategies that must every one auto-accept.
            any: Strategies of which at least one must auto-accept.

        Raises:
            ValueError: neither `all` nor `any` has any strategies —
                a Composite with nothing to compose is a configuration
                mistake, not a meaningful policy.
        """
        if not all and not any:
            raise ValueError("Composite requires at least one strategy in `all` or `any`")
        # Stored as tuples, not lists, and under a private name: a public
        # mutable list would let `composite.all.clear()` silently empty a
        # group after construction, bypassing the `__init__` guard above
        # and making `evaluate()` fail open (found in review) instead of
        # raising as a freshly-emptied `Composite()` would.
        self._all = tuple(all)
        self._any = tuple(any)

    def evaluate(
        self,
        proposal: Proposal,
        principal: Principal,
        kb: KbView,
        acting_as: Principal | None = None,
    ) -> Decision:
        """Evaluate every contained strategy and combine their decisions.

        Unlike ``ThresholdPolicy`` (which never reads ``kb`` and so
        defaults it to ``None`` for caller convenience), ``kb`` is required
        here — a contained strategy (e.g. ``SourceQuorum``) may genuinely
        need it, mirroring ``SourceQuorum``'s own required-``kb`` signature.
        """
        group_results: list[Decision] = []
        if self._all:
            group_results.append(
                _most_restrictive(
                    [s.evaluate(proposal, principal, kb, acting_as) for s in self._all]
                )
            )
        if self._any:
            group_results.append(
                _least_restrictive(
                    [s.evaluate(proposal, principal, kb, acting_as) for s in self._any]
                )
            )
        # __init__ guarantees at least one group is non-empty, and both are
        # immutable tuples set once at construction, so group_results is
        # never empty here.
        return _most_restrictive(group_results)


__all__ = [
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
]
