"""Ontolith CLI — command-line interface for knowledge base management."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, get_args

import typer

from ontolith import Ontology
from ontolith.core.errors import ValidationError
from ontolith.govern.contradiction import ContradictionState, safe_rationale_history
from ontolith.govern.proposal import ProposalState
from ontolith.schema.linkml import from_yaml
from ontolith.store.sqlite import migrations as sqlite_migrations

# Derived from ProposalState's/ContradictionState's own named Literal alias
# (govern/proposal.py, govern/contradiction.py), not hand-duplicated, so
# neither can silently drift if either type ever gains/loses a state
# (KI-077 — mirrors rest.py's/graphql.py's/mcp.py's identical constants;
# the CLI's own `--state` typo/case-mismatch shape is the same bug, on the
# one interface where a hand-typed value is most likely).
_PROPOSAL_STATES: tuple[str, ...] = get_args(ProposalState)
_CONTRADICTION_STATES: tuple[str, ...] = get_args(ContradictionState)

app = typer.Typer(
    name="ontolith",
    help="Ontolith knowledge base CLI.",
    no_args_is_help=True,
)
principal_app = typer.Typer(help="Manage principals.", no_args_is_help=True)
entity_app = typer.Typer(help="Manage entities.", no_args_is_help=True)
proposal_app = typer.Typer(help="Inspect and act on proposals.", no_args_is_help=True)
contradiction_app = typer.Typer(help="Inspect and act on contradictions.", no_args_is_help=True)
namespace_app = typer.Typer(help="Inspect namespaces.", no_args_is_help=True)
schema_app = typer.Typer(help="Inspect and apply the schema.", no_args_is_help=True)
admin_event_app = typer.Typer(help="Inspect admin-action audit events.", no_args_is_help=True)
db_app = typer.Typer(
    help="Inspect and apply on-disk storage-format migrations (SPEC §15, ADR-0052).",
    no_args_is_help=True,
)
app.add_typer(principal_app, name="principal")
app.add_typer(entity_app, name="entity")
app.add_typer(proposal_app, name="proposal")
app.add_typer(contradiction_app, name="contradiction")
app.add_typer(namespace_app, name="namespace")
app.add_typer(schema_app, name="schema")
app.add_typer(admin_event_app, name="admin-event")
app.add_typer(db_app, name="db")

# Module-level DB path, set by the root callback before any command runs.
_db_path: Path = Path("ontolith.db")


def _kb() -> Ontology:
    """Connect to the KB at the module-level `_db_path`.

    Every command calls this *before* entering its own `try:`/`except
    Exception as exc: typer.echo(f"Error: {exc}", err=True); raise
    typer.Exit(1)` block — a construction failure (e.g. `SchemaError` from
    an out-of-date format_version, ADR-0052) needs that exact same
    treatment, or it surfaces as a raw, unhandled traceback instead of the
    CLI's own consistent error convention. Handled centrally here rather
    than moving `_kb()` inside each of the ~25 command bodies individually.
    """
    try:
        return Ontology.connect(_db_path)
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None


@app.callback()
def root(
    db: Annotated[
        Path,
        typer.Option("--db", envvar="ONTOLITH_DB", help="Path to database file."),
    ] = Path("ontolith.db"),
) -> None:
    """Ontolith — collaborative knowledge base."""
    global _db_path
    _db_path = db


# ─── principal ────────────────────────────────────────────────────────────────


@principal_app.command("create")
def principal_create(
    principal_id: Annotated[str, typer.Argument(help="Principal ID (email or slug).")],
    kind: Annotated[str, typer.Option("--kind", help="Principal type: human, ai, or service.")],
    author: Annotated[
        str | None,
        typer.Option(
            "--author",
            help=(
                "Admin-capability principal creating this one. Required except for the "
                "very first principal in a fresh database, which has no admin yet to "
                "name (bootstrap: use the SDK directly, `Ontology.create_principal(...)`, "
                "for that one call — matches this CLI's own implicitly-trusted local "
                "access, same as every other principal-mutating command here)."
            ),
        ),
    ] = None,
    owner: Annotated[
        str | None,
        typer.Option("--owner", help="Accountable owner (required for AI principals)."),
    ] = None,
    capability: Annotated[
        str,
        typer.Option(
            "--capability", help="Default capability: read, propose, write, review, admin."
        ),
    ] = "propose",
    trust_level: Annotated[int, typer.Option("--trust-level", help="Trust score 0-10.")] = 0,
) -> None:
    """Create a new principal. Requires an existing admin principal named via
    `--author` (KI-054) — the one exception is bootstrapping the very first
    principal in a fresh database, where `--author` may be omitted."""
    kb = _kb()
    try:
        if author is not None:
            kb.require_admin(author)
        elif kb.backend.list_principals():
            # kb.backend (the raw port method) rather than kb.list_principals
            # (which itself requires admin - can't use it to detect whether
            # bootstrapping is legitimate without an admin already existing).
            typer.echo(
                "Error: --author is required (omit only to create the very first "
                "principal in a brand-new database). If this database has no admin "
                "principal at all yet, use the SDK directly instead: "
                'Ontology.connect(...).create_principal(..., default_capability="admin").',
                err=True,
            )
            raise typer.Exit(1)
        p = kb.create_principal(
            principal_id,
            kind=kind,
            owner=owner,
            default_capability=capability,
            trust_level=trust_level,
            author=author,
        )
        typer.echo(f"Created principal: {p.id}  kind={p.kind}  capability={p.default_capability}")
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@principal_app.command("list")
def principal_list(
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal performing the lookup.")
    ],
) -> None:
    """List all principals (KI-022). Requires the `--author` principal to hold `admin` capability."""
    kb = _kb()
    try:
        # No empty-list case to handle: require_admin(author) guarantees at
        # least `author` itself exists as a principal.
        for p in kb.list_principals(author=author):
            typer.echo(f"{p.id}  kind={p.kind}  capability={p.default_capability}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@principal_app.command("issue-token")
def principal_issue_token(
    principal_id: Annotated[str, typer.Argument(help="Principal ID to issue a token for.")],
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal performing the issuance.")
    ],
) -> None:
    """Issue a new API-key token for a principal (ADR-0014).

    The raw token is printed ONCE — it is not stored anywhere and cannot be
    recovered. Save it immediately; use the printed credential ID (not the
    token) to revoke it later via `principal revoke-token`. Requires the
    `--author` principal to hold `admin` capability.
    """
    kb = _kb()
    try:
        token, credential_id = kb.issue_token(principal_id, author=author)
        typer.echo(f"Token for {principal_id}: {token}")
        typer.echo(f"Credential ID (for revocation): {credential_id}")
        typer.echo("Store this token now — it will not be shown again.")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@principal_app.command("revoke-token")
def principal_revoke_token(
    credential_id: Annotated[str, typer.Argument(help="Credential ID to revoke.")],
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal performing the revocation.")
    ],
) -> None:
    """Revoke a previously issued API-key token by its credential ID.

    Requires the `--author` principal to hold `admin` capability.
    """
    kb = _kb()
    try:
        kb.revoke_token(credential_id, author=author)
        typer.echo(f"Revoked credential: {credential_id}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@principal_app.command("list-tokens")
def principal_list_tokens(
    principal_id: Annotated[str, typer.Argument(help="Principal ID to list credentials for.")],
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal performing the lookup.")
    ],
) -> None:
    """List credentials issued to a principal. Never shows the raw token or its hash.

    Requires the `--author` principal to hold `admin` capability.
    """
    kb = _kb()
    try:
        credentials = kb.list_tokens(principal_id, author=author)
        if not credentials:
            typer.echo(f"No credentials issued for {principal_id}.")
            return
        for c in credentials:
            if c.revoked_at:
                revoked_by = f" by {c.revoked_by}" if c.revoked_by else ""
                status = f"revoked at {c.revoked_at.isoformat()}{revoked_by}"
            else:
                status = "active"
            issued_by = f" by {c.issued_by}" if c.issued_by else ""
            typer.echo(f"{c.id}  issued={c.created_at.isoformat()}{issued_by}  {status}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── entity ───────────────────────────────────────────────────────────────────


@entity_app.command("create")
def entity_create(
    concept: Annotated[str, typer.Option("--concept", help="Concept name (e.g. Person).")],
    author: Annotated[str, typer.Option("--author", help="Principal ID of the author.")],
    key: Annotated[
        str | None,
        typer.Option("--key", help="Natural key within concept."),
    ] = None,
) -> None:
    """Create a new entity."""
    kb = _kb()
    try:
        entity = kb.create_entity(concept=concept, author=author, natural_key=key)
        typer.echo(f"Created entity: {entity.id}  concept={entity.concept}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── assert ───────────────────────────────────────────────────────────────────


@app.command("assert")
def assert_literal(
    subject: Annotated[str, typer.Argument(help="Subject entity ID.")],
    predicate: Annotated[str, typer.Argument(help="Predicate (e.g. Person.name).")],
    value: Annotated[str, typer.Argument(help="Literal value.")],
    value_type: Annotated[str, typer.Option("--type", help="Value type (Text, Integer, Date, …).")],
    author: Annotated[str, typer.Option("--author", help="Principal ID of the asserting author.")],
    confidence: Annotated[
        float | None,
        typer.Option("--confidence", help="Confidence score 0.0–1.0."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Source of the information."),
    ] = None,
    supersedes: Annotated[
        str | None,
        typer.Option(
            "--supersedes",
            help=(
                "Id of a specific existing assertion this one explicitly replaces "
                '(ADR-0050/KI-080, KI-099) — only meaningful for a cardinality="many", '
                'temporality="time_varying" property; rejected otherwise.'
            ),
        ),
    ] = None,
) -> None:
    """Make a literal assertion about an entity — a direct write (SPEC §9.3).

    Requires `write` (or `admin`) capability; bypasses the proposal queue but
    still passes through SPEC §10 conflict routing. For the governed
    propose → review → accept path, use the SDK's `Ontology.propose()`.
    """
    kb = _kb()
    try:
        a = kb.assert_literal(
            subject=subject,
            predicate=predicate,
            value=value,
            value_type=value_type,
            author=author,
            confidence=confidence,
            source=source,
            supersedes=supersedes,
        )
        typer.echo(f"Asserted: {a.id}  {a.predicate}={a.value!r}  by={a.author}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── assertions ───────────────────────────────────────────────────────────────


@app.command("assertions")
def list_assertions(
    subject: Annotated[
        str | None,
        typer.Option("--subject", help="Filter by subject entity ID."),
    ] = None,
    predicate: Annotated[
        str | None,
        typer.Option("--predicate", help="Filter by predicate."),
    ] = None,
    include_inactive: Annotated[
        bool,
        typer.Option("--all", help="Include non-active assertions."),
    ] = False,
) -> None:
    """List assertions, optionally filtered by subject or predicate."""
    kb = _kb()
    try:
        status = None if include_inactive else "active"
        results = kb.assertions(subject=subject, predicate=predicate, status=status)
        if not results:
            typer.echo("No assertions found.")
            return
        for a in results:
            conf = f"  confidence={a.confidence}" if a.confidence is not None else ""
            typer.echo(f"{a.id}  {a.predicate}={a.value!r}  by={a.author}  status={a.status}{conf}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── retract ──────────────────────────────────────────────────────────────────


@app.command("retract")
def retract_assertion(
    assertion_id: Annotated[str, typer.Argument(help="Assertion ID to retract.")],
    author: Annotated[
        str,
        typer.Option("--author", help="Principal ID of the retracting author."),
    ],
    acting_as: Annotated[
        str | None,
        typer.Option("--acting-as", help="Retract on behalf of another principal (delegation)."),
    ] = None,
) -> None:
    """Propose retraction of an assertion through the governed
    proposal/policy pipeline (SPEC §9). Does NOT delete or write directly —
    same propose/policy/conflict-routing pipeline as `ontolith assert`'s
    governed counterpart. May auto-accept, require review, or route through
    a contradiction's own capability floor depending on policy and the
    assertion's current state.
    """
    kb = _kb()
    try:
        proposal, decision = kb.retract(assertion_id, author=author, acting_as=acting_as)
        typer.echo(
            f"Retract proposed: {proposal.id}  state={proposal.state}  "
            f"decision={type(decision).__name__}  reason={proposal.policy_reason or '-'}"
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── query ────────────────────────────────────────────────────────────────────


@app.command("query")
def query_entities(
    concept: Annotated[str, typer.Argument(help="Concept to query (e.g. Person).")],
    where: Annotated[
        list[str] | None,
        typer.Option(
            "--where",
            help="Filter as KEY=VALUE (repeatable). KEY may be a bare property/relation "
            "name (equality) or use a __contains/__gt/__lt/__gte/__lte suffix (KI-039), "
            "e.g. --where age__gte=18.",
        ),
    ] = None,
    semantic: Annotated[
        str | None,
        typer.Option(
            "--semantic",
            help="Rank results by vector similarity to this text (SPEC §11.3) instead "
            "of/in addition to --where. Only sees content indexed by the most recent "
            "'ontolith reindex' — an entity asserted since then won't match until "
            "reindexed again.",
        ),
    ] = None,
    min_confidence: Annotated[
        float | None,
        typer.Option(
            "--min-confidence",
            help="Keep only entities with at least one qualifying assertion (also "
            "'flagged' if --include-flagged is set, and/or 'superseded'/'retracted' "
            "if --include-history is set) at or above this confidence (0.0-1.0). An "
            "assertion with no confidence recorded never satisfies this, even "
            "--min-confidence 0.0 (ADR-0004).",
        ),
    ] = None,
    trust_at_least: Annotated[
        int | None,
        typer.Option(
            "--trust-at-least",
            help="Keep only entities with at least one qualifying assertion whose "
            "effective trust_level is at or above this floor (KI-047).",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Cap the number of entities returned."),
    ] = None,
    include_flagged: Annotated[
        bool,
        typer.Option(
            "--include-flagged",
            help="Also match --where/--min-confidence/--trust-at-least against "
            "flagged (contradicted) assertions — excluded by default (SPEC §10.3, "
            "KI-081/093/094).",
        ),
    ] = False,
    include_history: Annotated[
        bool,
        typer.Option(
            "--include-history",
            help="Also match against superseded/retracted assertions — excluded by "
            "default (KI-081/093/094).",
        ),
    ] = False,
) -> None:
    """Query entities by concept, optionally filtered by --where and/or ranked
    by --semantic, with --min-confidence/--trust-at-least floors,
    --include-flagged/--include-history opt-ins (KI-081/094/096), and a
    --limit cap.

    No --as-of: MCP is the only one of the four shipped interfaces that
    exposes bitemporal time-travel on `query` today (KI-058). The Python
    SDK's `kb.as_of(t)` has it too, ahead of this command.
    """
    kb = _kb()
    try:
        q = kb.query(concept)
        if where:
            filters: dict[str, str] = {}
            for item in where:
                if "=" not in item:
                    typer.echo(f"Invalid filter (expected KEY=VALUE): {item!r}", err=True)
                    raise typer.Exit(1)
                k, _, v = item.partition("=")
                filters[k.strip()] = v.strip()
            q = q.where(**filters)
        if semantic is not None:
            q = q.semantic(semantic)
        if min_confidence is not None:
            q = q.min_confidence(min_confidence)
        if trust_at_least is not None:
            q = q.trust_at_least(trust_at_least)
        if include_flagged:
            q = q.include_flagged()
        if include_history:
            q = q.include_history()
        if limit is not None:
            q = q.limit(limit)
        results = q.all()
        if not results:
            typer.echo("No entities found.")
            return
        for entity in results:
            typer.echo(f"{entity.id}  concept={entity.concept}  key={entity.natural_key or '-'}")
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@app.command("reindex")
def reindex(
    concept: Annotated[
        str | None,
        typer.Option("--concept", help="Only re-index entities of this concept."),
    ] = None,
) -> None:
    """Re-embed entities' Text content into the vector index (SPEC §11.3)."""
    kb = _kb()
    try:
        count = kb.reindex(concept=concept)
        typer.echo(f"Reindexed {count} entit{'y' if count == 1 else 'ies'}.")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── proposal ─────────────────────────────────────────────────────────────────


@proposal_app.command("list")
def list_proposals(
    state: Annotated[
        str | None,
        typer.Option("--state", help="Filter by state (default: require_review)."),
    ] = "require_review",
    all_states: Annotated[
        bool,
        typer.Option("--all", help="Show proposals in every state, ignoring --state."),
    ] = False,
) -> None:
    """List proposals, defaulting to those pending review.

    Without this, a reviewer has no way to discover what route_to_review
    (SPEC §10.3) routed to them short of querying the backend directly.
    ``--state pending`` merges ``require_review`` and ``changes_requested``
    — see ``Ontology.proposals`` (KI-027) for why that isn't the default.
    """
    kb = _kb()
    try:
        # Only when --state will actually be used: --all already bypasses
        # it entirely (below), so a leftover/bogus --state alongside --all
        # is harmless and shouldn't error. Unlike REST/GraphQL's `state`,
        # no "all" value here — --all is CLI's own separate boolean flag
        # (KI-077 review: an unrecognized value previously reached
        # kb.proposals()'s own WHERE state = ? unfiltered and silently
        # matched zero rows, indistinguishable from "no proposals in that
        # state" - the same bug KI-076/KI-077 already fixed on MCP/REST/
        # GraphQL, on the interface where a hand-typed typo is likeliest).
        if not all_states and state not in (None, *_PROPOSAL_STATES, "pending"):
            raise ValidationError(
                f"Invalid state: {state!r} (expected one of "
                f"{[*_PROPOSAL_STATES, 'pending']}, or pass --all for every state)"
            )
        results = kb.proposals(state=None if all_states else state)
        if not results:
            typer.echo("No proposals found.")
            return
        for p in results:
            reviewers_suffix = f"  reviewers={', '.join(p.reviewers)}" if p.reviewers else ""
            typer.echo(
                f"{p.id}  state={p.state}  author={p.author}  created={p.created_at}"
                f"{reviewers_suffix}"
            )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@proposal_app.command("accept")
def accept_proposal(
    proposal_id: Annotated[str, typer.Argument(help="Proposal ID to accept.")],
    reviewer: Annotated[
        str,
        typer.Option("--reviewer", "--author", help="Reviewer principal accepting this proposal."),
    ],
) -> None:
    """Accept a pending proposal, committing its operations (SPEC §9).

    Requires the `--reviewer` principal to hold `review` (or `admin`)
    capability, not be AI-kind, and not be the proposal's own author or
    delegate (SPEC §9.4). The proposal must be in `require_review` or
    `under_review` state — a `changes_requested` proposal must go through
    `proposal resubmit` (KI-027) first.
    """
    kb = _kb()
    try:
        proposal = kb.accept_proposal(proposal_id, reviewer)
        typer.echo(f"Accepted: {proposal.id}  state={proposal.state}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@proposal_app.command("reject")
def reject_proposal(
    proposal_id: Annotated[str, typer.Argument(help="Proposal ID to reject.")],
    reviewer: Annotated[
        str,
        typer.Option("--reviewer", "--author", help="Reviewer principal rejecting this proposal."),
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Optional explanation for the rejection.")
    ] = "",
) -> None:
    """Reject a pending proposal. No operations are applied (SPEC §9).

    Requires the `--reviewer` principal to hold `review` (or `admin`)
    capability, not be AI-kind, and not be the proposal's own author or
    delegate (SPEC §9.4). The proposal must be in `require_review` or
    `under_review` state.
    """
    kb = _kb()
    try:
        proposal = kb.reject_proposal(proposal_id, reviewer, reason=reason)
        typer.echo(f"Rejected: {proposal.id}  state={proposal.state}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@proposal_app.command("review")
def review_proposal(
    proposal_id: Annotated[str, typer.Argument(help="Proposal ID to request changes on.")],
    reviewer: Annotated[
        str,
        typer.Option("--reviewer", "--author", help="Reviewer principal requesting changes."),
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Optional explanation of what needs to change.")
    ] = "",
) -> None:
    """Request changes on a pending proposal — does not accept or reject it
    (SPEC §9.1/§9.4).

    Moves the proposal to `changes_requested`; its author or delegate can
    then move it back to `submitted` via `proposal resubmit` (KI-027).
    Requires the `--reviewer` principal to hold `review` (or `admin`)
    capability, not be AI-kind, and not be the proposal's own author or
    delegate. The proposal must be in `require_review` or `under_review`
    state.
    """
    kb = _kb()
    try:
        proposal = kb.request_changes(proposal_id, reviewer, reason=reason)
        typer.echo(f"Changes requested: {proposal.id}  state={proposal.state}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@proposal_app.command("assign")
def assign_reviewers(
    proposal_id: Annotated[str, typer.Argument(help="Proposal ID to reassign reviewers on.")],
    actor: Annotated[
        str,
        # No --author alias, unlike accept/reject/review's --reviewer: on
        # this command the proposal's own author is exactly the principal
        # the self-review guard forbids from calling it, so an --author
        # alias here would name the one role that can't use it.
        typer.Option("--actor", help="Principal performing the reassignment."),
    ],
    reviewer: Annotated[
        list[str] | None,
        typer.Option(
            "--reviewer",
            help="Reviewer to assign (repeatable). Replaces the current list wholesale.",
        ),
    ] = None,
    clear: Annotated[
        bool,
        typer.Option(
            "--clear", help="Remove every reviewer assignment. Required instead of --reviewer."
        ),
    ] = False,
) -> None:
    """Reassign a pending proposal's reviewers (SPEC §9.4's `assign` action, KI-078/KI-079).

    Requires the `--actor` principal to hold `review` (or `admin`) capability,
    not be AI-kind, and not be the proposal's own author or delegate — the
    same guard `accept`/`reject`/`review` share. The proposal must be in
    `require_review` or `under_review` state. `--reviewer` replaces the
    entire list, not merges with it; pass every name that should remain.

    Exactly one of `--reviewer` (one or more) or `--clear` is required —
    unlike REST/GraphQL, where an empty list is just an ordinary argument
    value, a bare CLI invocation with neither has no natural "did the
    caller mean to clear everything, or forget the flag?" default. Since
    clearing discards the assignment with no way to recover it (no event
    records the prior list, only that a change happened), this command
    requires the caller to say which they meant rather than silently
    treating "no flags" as "clear everything."
    """
    kb = _kb()
    try:
        if bool(reviewer) == clear:
            # Covers both "neither" (ambiguous: forgot the flag, or meant to
            # clear?) and "both" (contradictory: assign these, or clear
            # everything?) - `bool(reviewer) == clear` is True for exactly
            # those two cases (False==False, True==True), False whenever
            # exactly one was given. Found in review: an earlier version
            # only rejected "neither", so `--reviewer x --clear` silently
            # ignored --clear and assigned anyway.
            typer.echo(
                "Error: pass --reviewer (repeatable) to assign, or --clear to remove "
                "every assignment — not both, not neither",
                err=True,
            )
            raise typer.Exit(1)
        proposal = kb.assign_reviewers(proposal_id, reviewer or [], actor)
        shown = ", ".join(proposal.reviewers) or "-"
        typer.echo(f"Reviewers assigned: {proposal.id}  reviewers={shown}")
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@proposal_app.command("resubmit")
def resubmit_proposal(
    proposal_id: Annotated[str, typer.Argument(help="Proposal ID to resubmit.")],
    author: Annotated[
        str,
        typer.Option("--author", help="Proposal's own author (or delegate) resubmitting it."),
    ],
) -> None:
    """Resubmit a proposal after changes were requested (SPEC §9.1, KI-027).

    Only the proposal's own author or delegating principal may call this —
    the payload is replayed unedited through a fresh policy evaluation
    (which may auto-accept, require review again, or reject — resubmitting
    does not guarantee acceptance). The proposal must be in
    `changes_requested` state.
    """
    kb = _kb()
    try:
        proposal, decision = kb.resubmit(proposal_id, author)
        typer.echo(
            f"Resubmitted: {proposal.id}  state={proposal.state}  "
            f"decision={type(decision).__name__}  reason={proposal.policy_reason or '-'}"
        )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── contradiction ────────────────────────────────────────────────────────────


@contradiction_app.command("list")
def list_contradictions(
    state: Annotated[
        str | None,
        typer.Option("--state", help="Filter by state (default: open)."),
    ] = "open",
    all_states: Annotated[
        bool,
        typer.Option("--all", help="Show contradictions in every state, ignoring --state."),
    ] = False,
    # Named distinctly from `flag`'s own `--rationale <text>` option (a
    # different kind of value - text there, a bool switch here) to avoid a
    # same-name-different-meaning trap between the two sibling commands.
    show_rationale: Annotated[
        bool,
        typer.Option(
            "--show-rationale",
            help=(
                "Print each contradiction's accumulated rationale_history "
                "entries (KI-075) in full, not just their count."
            ),
        ),
    ] = False,
) -> None:
    """List contradictions, defaulting to open (unresolved) ones."""
    kb = _kb()
    try:
        # Only when --state will actually be used - see proposal list's
        # identical guard/rationale (KI-077 review).
        if not all_states and state not in (None, *_CONTRADICTION_STATES):
            raise ValidationError(
                f"Invalid state: {state!r} (expected one of "
                f"{list(_CONTRADICTION_STATES)}, or pass --all for every state)"
            )
        results = kb.contradictions(state=None if all_states else state)
        if not results:
            typer.echo("No contradictions found.")
            return
        for c in results:
            history = safe_rationale_history(c.metadata)
            suffix = f"  rationale_entries={len(history)}" if history else ""
            typer.echo(
                f"{c.id}  state={c.state}  subject={c.subject}  "
                f"predicate={c.predicate}  members={len(c.member_ids)}{suffix}"
            )
            if show_rationale:
                for entry in history:
                    typer.echo(f"  [{entry['at']}] {entry['actor']}: {entry['rationale']}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@contradiction_app.command("flag")
def flag_contradiction(
    assertion_id_a: Annotated[str, typer.Argument(help="First assertion ID.")],
    assertion_id_b: Annotated[str, typer.Argument(help="Second, conflicting assertion ID.")],
    author: Annotated[
        str,
        typer.Option("--author", help="Principal ID flagging the contradiction."),
    ],
    rationale: Annotated[
        str | None,
        typer.Option("--rationale", help="Optional explanation for the flag."),
    ] = None,
) -> None:
    """Flag two assertions as contradictory, opening or extending a
    contradiction (SPEC §10.3). Requires propose capability or higher
    (ADR-0008) — the same tier as `ontolith retract`/`ontolith.propose`,
    not a reviewer-only action.
    """
    kb = _kb()
    try:
        contradiction, action = kb.flag_contradiction(
            assertion_id_a, assertion_id_b, author, rationale=rationale
        )
        typer.echo(
            f"{action.capitalize()}: {contradiction.id}  state={contradiction.state}  "
            f"members={len(contradiction.member_ids)}"
        )
        # KI-075: echo the full accumulated trail, not just this call's own
        # rationale — the only way to confirm an "extend" call's rationale
        # was appended rather than dropped (KI-071) without a separate
        # `contradiction list` round-trip.
        for entry in safe_rationale_history(contradiction.metadata):
            typer.echo(f"  [{entry['at']}] {entry['actor']}: {entry['rationale']}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@contradiction_app.command("resolve")
def resolve_contradiction(
    contradiction_id: Annotated[str, typer.Argument(help="Contradiction ID to resolve.")],
    winner: Annotated[
        str,
        typer.Option("--winner", help="Assertion ID of the winning member."),
    ],
    reviewer: Annotated[
        str,
        typer.Option("--reviewer", "--author", help="Reviewer principal resolving this."),
    ],
) -> None:
    """Resolve an open contradiction by selecting a winning assertion
    (SPEC §10.4). Members with the winner's value stay active; members
    with a different value are retracted. Requires `review` (or `admin`) capability, non-AI, and
    not a party to the contradiction (KI-026).
    """
    kb = _kb()
    try:
        contradiction = kb.resolve_contradiction(contradiction_id, winner, reviewer)
        typer.echo(f"Resolved: {contradiction.id}  state={contradiction.state}  winner={winner}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── namespace ────────────────────────────────────────────────────────────────


@namespace_app.command("list")
def list_namespaces() -> None:
    """List all registered namespaces (SPEC §12.2, KI-022).

    This project is still single-namespace throughout (ADR-0015) — today
    this always prints exactly one entry, the seeded default namespace.
    """
    kb = _kb()
    try:
        for n in kb.list_namespaces():
            typer.echo(f"{n.id}  created={n.created_at}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── schema ───────────────────────────────────────────────────────────────────


@schema_app.command("show")
def schema_show(
    namespace: Annotated[
        str,
        typer.Option("--namespace", help="Namespace to inspect."),
    ] = "default",
) -> None:
    """Print concepts, properties, and relations for the active schema (SPEC §14.2).

    Same field set as MCP's `ontolith.schema`/REST's `GET /schema` output
    (KI-029, KI-038) — the CLI had no way to inspect a registered schema at
    all before this command existed, unlike every other primary interface.
    Property/relation attribute order is normalized here (cardinality,
    temporality, required, [inverse]) rather than copied verbatim — REST's
    `PropertyOut`/`RelationOut` and MCP's dict output don't actually agree
    with each other on relation field order either, so there is no single
    "the" order to mirror.
    """
    kb = _kb()
    try:
        ir = kb.backend.get_schema(namespace)
        if ir is None:
            typer.echo(f"No schema registered for namespace {namespace!r}.")
            return
        typer.echo(f"namespace={ir.namespace}  version={ir.version}")
        for concept_name, concept_def in ir.concepts.items():
            typer.echo(f"\n{concept_name}")
            for prop_name, prop_def in concept_def.properties.items():
                typer.echo(
                    f"  {prop_name}: {prop_def.value_type}"
                    f"  cardinality={prop_def.cardinality}"
                    f"  temporality={prop_def.temporality}"
                    f"  required={prop_def.required}"
                )
            for rel_name, rel_def in concept_def.relations.items():
                typer.echo(
                    f"  {rel_name} -> {rel_def.target_concept}"
                    f"  cardinality={rel_def.cardinality}"
                    f"  temporality={rel_def.temporality}"
                    f"  required={rel_def.required}"
                    f"  inverse={rel_def.inverse}"
                )
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


@schema_app.command("migrate")
def schema_migrate(
    file: Annotated[Path, typer.Argument(help="Path to a LinkML-aligned YAML schema document.")],
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal applying the schema.")
    ],
) -> None:
    """Apply a new schema version from a YAML file (SPEC §14.2, KI-048).

    Thin wrapper around `Ontology.apply_schema`: reads a LinkML-aligned YAML
    document (ADR-0013 dialect, `schema.linkml.from_yaml`) from `file` and
    persists it as a new schema version. The document's own `version:`/`id:`
    fields drive the applied namespace/version — `apply_schema`'s existing
    strict-monotonic check (exactly `current_latest + 1`, or `1` if no
    schema is registered yet) is unchanged by this command, so the file must
    already declare the correct next version. Requires `--author` to hold
    `admin` capability (SPEC §6).

    `file` must contain the *complete* schema for the namespace, not a
    delta — `apply_schema` replaces the namespace's active `SchemaIR`
    wholesale, it does not merge the new version with the prior one. A
    document that omits a concept the prior version declared makes that
    concept (and its predicates) unknown to schema-validated writes going
    forward, same as any other `apply_schema` caller.

    Does not migrate or backfill existing assertion data written under a
    prior schema version — applying a new version is purely additive to the
    schema's own version history (ADR-0034); property renames/type changes
    against already-stored data are explicitly out of scope (KI-048).
    """
    kb = _kb()
    try:
        text = file.read_text()
        schema = from_yaml(text)
        applied = kb.apply_schema(schema, author=author)
        typer.echo(f"Applied schema: namespace={applied.namespace}  version={applied.version}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


# ─── db ───────────────────────────────────────────────────────────────────────


@db_app.command("status")
def db_status() -> None:
    """Report the on-disk format_version at `--db` without changing anything
    (SPEC §15, ADR-0052).

    Deliberately does not go through `_kb()`/`Ontology.connect()` — that
    path refuses (SchemaError) to open a file below the current
    format_version, which is exactly the file this command exists to
    inspect. Reads via `sqlite_migrations.migrate_file(dry_run=True)`
    directly, so it's a pure preview: nothing on disk changes, matching
    SPEC §15's "dry-run mode" requirement.
    """
    try:
        report = sqlite_migrations.migrate_file(_db_path, dry_run=True)
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    if report.up_to_date:
        typer.echo(f"format_version={report.from_version} — up to date.")
        return
    typer.echo(
        f"format_version={report.from_version}, current is {report.to_version} — "
        f"{len(report.steps)} migration(s) pending:"
    )
    for step in report.steps:
        reversible = "reversible" if step.reversible else "irreversible"
        typer.echo(f"  -> v{step.version} ({reversible}): {step.description}")
    typer.echo("Run `ontolith db migrate` to apply.")


@db_app.command("migrate")
def db_migrate(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report pending migrations without applying them."),
    ] = False,
) -> None:
    """Apply pending on-disk format-version migrations at `--db` (SPEC §15,
    ADR-0052).

    Standalone — deliberately does not go through `_kb()`/`Ontology.connect()`,
    which refuses to open a file below the current format_version; this
    command is what actually brings such a file forward. Safe to run
    unconditionally (e.g. before every deploy): a file already at the
    current format_version reports nothing pending and changes nothing.

    Does not migrate or backfill *domain* data (`ontolith schema migrate`'s
    own scope, ADR-0034, is a separate and narrower thing — a namespace's
    schema version, not this backend's own DDL shape).
    """
    try:
        report = sqlite_migrations.migrate_file(_db_path, dry_run=dry_run)
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    if report.up_to_date:
        typer.echo(f"Already at format_version={report.to_version} — nothing to do.")
        return
    verb = "Would apply" if dry_run else "Applied"
    for step in report.steps:
        typer.echo(f"{verb} v{step.version}: {step.description}")
    if dry_run:
        typer.echo(
            f"{len(report.steps)} migration(s) pending (format_version "
            f"{report.from_version} -> {report.to_version}). Re-run without --dry-run to apply."
        )
    else:
        typer.echo(f"Migrated format_version {report.from_version} -> {report.to_version}.")


# ─── admin-event ───────────────────────────────────────────────────────────────


@admin_event_app.command("list")
def admin_events_list(
    author: Annotated[
        str, typer.Option("--author", help="Admin-capability principal performing the lookup.")
    ],
    actor: Annotated[
        str | None, typer.Option("--actor", help="Filter to events performed by this principal.")
    ] = None,
    target: Annotated[
        str | None, typer.Option("--target", help="Filter to events against this target.")
    ] = None,
) -> None:
    """List recorded admin-action events: create_principal/apply_schema/
    register_plugin (KI-072, ADR-0042).

    Token issuance/revocation aren't included here — see `principal
    list-tokens`'s `issued_by`/`revoked_by` columns for those. Requires
    the `--author` principal to hold `admin` capability.
    """
    kb = _kb()
    try:
        events = kb.get_admin_events(author=author, actor=actor, target=target)
        if not events:
            typer.echo("No admin events recorded.")
            return
        for e in events:
            detail = f"  {e.detail}" if e.detail else ""
            typer.echo(f"{e.at.isoformat()}  {e.actor}  {e.action}  target={e.target}{detail}")
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        kb.close()


__all__ = ["app"]
