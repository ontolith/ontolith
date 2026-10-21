# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Ontolith** is a Python framework/SDK for building collaborative knowledge bases where humans and AI agents are co-equal authors of a shared, governed ontology. It's an open-core project (Apache-2.0) targeting Python ≥3.11.

## Core Architecture Principles (Non-Negotiable)

### Dependency Rule (CI-Enforced via import-linter)
- `core/`, `schema/`, `govern/`, `query/` MUST NOT import `store/sqlite`, `interfaces/*`, or any concrete adapter
- Domain logic depends only on abstract ports: `StorageBackend`, `Embedder`, `AuthProvider`, `PolicyStrategy`, `Clock`, `IdProvider`
- Concrete adapters implement ports; violations are CI-blocking

### Immutability Invariants
- **Assertions are append-only**: only `status`, `valid_to`, and `supersedes`/successor links may mutate
- Never edit an assertion's `value` field in-place
- Creating, updating, or retracting an attribute produces new assertion records

### Determinism by Construction
- No `datetime.now()` or `uuid4()` in domain logic — use injected `Clock` and `IdProvider` ports
- Policy engine (`govern/policy`) is PURE: no I/O, deterministic, testable
- Bitemporal behavior must be reproducible in tests

### AI Safety & Governance
- AI principals MUST declare an accountable owner (enforced in code and DB CHECK constraint)
- Confidence is a single scalar (0.0-1.0) and is NEVER auto-combined in v1
- MCP server exposes NO direct write tool — only read/query/propose/flag_contradiction/provenance
- Agents default to `propose` capability, not `write`

### Conflict Handling (SPEC §10)
- Routing by temporality: `time_varying` properties use temporal supersession; `static` properties create contradictions
- Default temporality is `static`
- Static facts are NEVER silently overwritten — contradictions are flagged and routed to review
- Multiple values can coexist for `time_varying` properties if validity windows don't overlap

## Development Workflow

### Git Workflow (Trunk-Based Development)

**Branch Strategy:**
- Work on short-lived feature branches off `main`
- Branch naming: `<milestone>/<short-description>` or `<type>/<short-description>`
  - Examples: `m0/repo-skeleton`, `m1/assertion-core`, `fix/conflict-routing`, `docs/adr-0005`
- One branch per meaningful task or chunk of related tasks (aligned with Implementation Plan milestones)

**When to Create a Branch:**
- Starting work on a milestone deliverable (M0, M1, M2, etc.)
- Implementing a SPEC requirement or conformance vector
- Adding a new module, port, or adapter
- Fixing a bug or addressing a review finding
- Creating/updating ADRs or documentation

**Commit Strategy:**
- **Conventional Commits** (required — drives changelog and version bumps)
- Format: `<type>(<scope>): <description>`
- Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `perf`, `ci`
- Scope: module name (`core`, `govern`, `store`, `query`, `identity`, `interfaces`, etc.)

**Examples:**
```bash
feat(core): add Clock and IdProvider ports for determinism
feat(govern): implement conflict routing by temporality
test(conformance): add SPEC §19 supersession vector
fix(store): close validity window on time_varying supersession
docs(adr): record ADR-0005 conflict model decision
refactor(query): extract temporal filter logic
chore(ci): add import-linter to quality gates
```

**Commit message rules:**
- Do NOT include `Co-Authored-By:` trailers in commit messages

**Workflow Pattern:**

1. **Create branch for task/milestone:**
   ```bash
   git checkout -b m1/assertion-core
   ```

2. **Implement with atomic commits:**
   ```bash
   # One commit per logical change
   git add src/ontolith/core/assertion.py
   git commit -m "feat(core): add Assertion value object with provenance"
   
   git add tests/unit/test_assertion.py
   git commit -m "test(core): add Assertion validation tests"
   ```

3. **Verify before pushing:**
   ```bash
   # Run quality gates
   uv run pytest && ruff check && mypy --strict src
   
   # Run conformance if touching core/govern/store/query
   uv run pytest conformance/
   ```

4. **Push and create PR:**
   ```bash
   git push -u origin m1/assertion-core
   # Create PR via GitHub (or gh CLI)
   ```

5. **Merge when green:**
   - CI passes (all quality gates)
   - Code review approved
   - Conventional commits format verified
   - No unresolved conversations

**Important Rules:**
- **Never force push to main** — main is protected
- **Create NEW commits** rather than amending (unless explicitly fixing a PR commit)
- **One PR per logical feature/fix** — keep PRs focused and reviewable
- **Link to issues/milestones** in PR description when applicable
- **Update ADRs** if implementation changes a decision
- **Branch protection**: main requires green CI + 1 review

### Environment Setup
```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Run full quality gate
uv run pytest && ruff check && mypy --strict src

# Run conformance suite
uv run pytest conformance/

# Run benchmarks
uv run pytest tests/benchmarks/
```

### Definition of Done

A unit of work (branch/PR) is not complete until ALL of these are true:

**Code Quality:**
- [ ] All quality gates pass (see below)
- [ ] Dependency rule verified (import-linter clean)
- [ ] No violations of core invariants (append-only, policy purity, determinism)
- [ ] Coverage thresholds met (≥90% domain, 100% on correctness modules)

**Testing:**
- [ ] Conformance vectors pass (if touching core/govern/store/query)
- [ ] Property tests added for correctness-critical logic
- [ ] Unit/integration tests added for new functionality
- [ ] All tests pass locally and in CI

**Documentation:**
- [ ] Public API has docstrings (≥95% coverage)
- [ ] ADR created/updated if a decision changed
- [ ] CHANGELOG entry if user-facing change
- [ ] Examples updated if public behavior changed

**Review:**
- [ ] ontolith-reviewer agent findings addressed
- [ ] Human code review approved
- [ ] Conventional commit format verified
- [ ] No unresolved review conversations

**Milestone Alignment:**
- [ ] Contributes to current milestone exit criteria
- [ ] Does not introduce scope creep beyond milestone goals
- [ ] Documented in milestone tracking (if applicable)

**Before Merge:**
- [ ] Branch is up to date with main
- [ ] CI is green on final commit
- [ ] All review comments resolved
- [ ] Squash/rebase if commit history is messy (preserve meaningful commits)

### Testing Strategy (TDD on Correctness Modules)
- Write conformance/property tests BEFORE implementation for bitemporal and conflict logic
- **Property-based tests (Hypothesis)** are first-class for bitemporal reconstruction, conflict routing, and append-only invariants
- **Conformance kit** (SPEC §19 vectors) is reusable and allows storage backends to self-certify
- **Coverage requirements**: ≥90% on domain (`core`, `govern`, `query`), 100% on `govern/conflict` and bitemporal logic

### Code Quality Gates (CI-Blocking)
- Format: `ruff format --check` (clean)
- Lint: `ruff check` (zero errors)
- Types: `mypy --strict` on src (zero errors)
- Unit/integration/property tests: all pass
- Conformance suite (SPEC §19): all vectors pass
- Dependency rule: `import-linter` contract holds
- Coverage: thresholds met
- Conventional commits required (drives changelog)

### Milestone-Aligned Task Decomposition

Work is organized by **milestones** from the Implementation Plan (see `docs/Ontolith_Implementation_Plan.md` §2):

**M0 — Foundations** (Green CI skeleton):
- Repository setup, tooling, CI workflows
- Ports/protocols stubbed (Clock, IdProvider, StorageBackend, etc.)
- ADRs 0001-0008 recorded
- Error taxonomy defined
- import-linter contract active

**M1 — Substrate (0.1 MVP)**:
- Meta-model + IR, class DSL
- SQLite backend implementation
- Entities/assertions (append-only)
- Identity basics + capabilities
- Proposal→accept + threshold policy
- Basic query builder, Python SDK, CLI
- Exit criteria: conformance vectors pass, quickstart runs, traversal benchmark baseline

**M2 — Collaboration (0.2)**:
- Review workflow
- Bitemporal time-travel
- Conflict handling (supersession + contradictions)
- MCP server (read/propose/flag/provenance)
- Trust levels, delegation
- LinkML-aligned YAML, first 3 reference plugins
- Exit criteria: Full §19 conformance, property tests green, no MCP write tool

**M3 — Extensible (0.3)**, **M4 — Production (1.0)**, **vNext**: See Implementation Plan for full details

**Task Decomposition Rules:**

1. **Create branches aligned with milestones**: `m0/repo-skeleton`, `m1/assertion-core`, `m2/conflict-routing`
2. **Break milestones into implementable chunks**:
   - One chunk = one PR
   - Chunk size: completable in 1-3 sessions
   - Each chunk has tests + documentation
3. **TDD for correctness modules**:
   - Write failing conformance vector FIRST
   - Implement feature
   - Verify with conformance-runner agent
   - All tests pass before PR
4. **Exit criteria are gates**: Milestone not "done" until its exit criteria pass
5. **ADR per significant decision**: Record in `docs/adr/` before implementing

### Repository Structure
```
src/ontolith/
├── core/        # meta-model, IR, validation, Clock & IdProvider ports
├── schema/      # class DSL, YAML loader, codegen, migration
├── store/       # StorageBackend port + SQLite default adapter
├── identity/    # principals, auth ports, capabilities, delegation
├── govern/      # proposals, policy (PURE), review, conflict
├── query/       # builder, traversal, hybrid retrieval
├── plugins/     # registry, protocols, lifecycle, sandbox
├── interfaces/  # sdk, cli, rest, graphql, mcp
└── observe/     # structured logging, metrics, audit

conformance/     # SPEC §19 reusable test kit
tests/           # unit, property, integration, contract, benchmarks
examples/        # quickstart.py and use-case demos (CI-tested)
docs/adr/        # Architecture Decision Records (MADR format)
```

## Schema Definition

### Class-based DSL (Primary DX)
```python
from ontolith import Concept, Relation, Text, Date, Ref


class Person(Concept):
    name: Text
    born: Date | None = None  # temporality defaults to "static"
    employer: Ref["Organization"] = Relation(
        inverse="employees",
        temporality="time_varying",  # changes supersede, don't contradict
    )
```

### Internal Representation (IR)
- Single source of truth: JSON document per namespace
- Two front-ends compile to it: class DSL (ships first) and LinkML-aligned YAML (0.2)
- Codegen is bidirectional: `classes ↔ IR ↔ YAML`

## Key Concepts

### Temporality
- Per-property/relation attribute: `static` (default) or `time_varying`
- Determines conflict handling: `static` → contradiction object; `time_varying` → temporal supersession
- Declared explicitly in schema

### Confidence
- Single scalar 0.0-1.0 representing asserting principal's stated belief
- Stored alongside open `metadata` blob for future evolution
- Corroborating assertions are surfaced, NEVER auto-combined in v1

### Provenance
- Immutable trail: who, what source, when, which model+version, why, confidence, delegation chain
- Captured automatically on every assertion
- Must be retrievable in one call for any assertion

### Principals
- Identified actors: `human`, `ai`, or `service`
- AI principals require accountable owner (human/team) and capture model+version per assertion
- Capabilities (increasing): `read < propose < write < review < admin`

## Performance Budgets (Baselines in M1, Enforced by M4)

On laptop with SQLite default backend:
- `propose` + policy eval + commit: p95 < 50ms
- Single-entity `get` with provenance: p95 < 10ms
- 3-hop traversal (100k assertions): p95 < 200ms
- Hybrid query (symbolic + vector, k=10): p95 < 150ms
- `as_of` reconstruction (100k assertions): p95 < 300ms

## Decision Records

All significant cross-cutting decisions are recorded as ADRs (MADR format) in `docs/adr/`.
Starting decisions (from PRD §16):
- ADR-0001 to ADR-0008: Core design decisions (storage, schema, agent identity, confidence, conflict model, licensing, interop priority, MCP surface)
- New decisions require an ADR before implementation

## Plugin Development

Plugins implement protocol interfaces:
- `Importer`, `Exporter`, `Reasoner`, `Validator`, `Embedder`, `Connector`, `StorageBackend`, `AuthProvider`, `PolicyStrategy`
- Discovered via entry points under `ontolith.plugins` group
- Must declare `name`, `version`, `capabilities` manifest
- Reasoner-derived assertions MUST enter through proposal path (never bypass governance)
- Interop sequence: LinkML → RDF/OWL → agent-memory bridges

## Important Implementation Notes

- **Validate at edges, trust within**: Pydantic validation at SDK/REST/MCP boundaries only
- **sqlite-vec dependency**: Pre-v1, version-pinned, isolated behind storage port (swappable)
- **One transaction per proposal acceptance**: All writes within proposal acceptance in single transaction
- **Public API**: Fully typed, `py.typed` shipped, no `Any` on public surfaces
- **Error taxonomy**: Stable error codes (SPEC §16) with machine-readable codes mapped to HTTP/MCP

## Common Patterns to Avoid

- Do not improvise bitemporal or conflict semantics — follow SPEC §10 exactly
- Do not add features beyond requirements (no premature abstractions)
- Do not auto-combine confidence scores (v1 limitation)
- Do not expose direct-write MCP tools
- Do not violate the dependency rule (domain importing adapters)
- Do not use datetime.now() or uuid4() directly in domain code

## Quick Reference: Common Git Operations

### Starting Work on a Milestone Task

```bash
# Ensure you're on latest main
git checkout main
git pull

# Create milestone-aligned branch
git checkout -b m1/assertion-core

# Or for smaller tasks
git checkout -b fix/policy-validation
git checkout -b docs/adr-conflict-model
```

### During Implementation

```bash
# Atomic commits with conventional format
git add src/ontolith/core/assertion.py
git commit -m "feat(core): add Assertion value object with provenance"

git add tests/unit/test_assertion.py  
git commit -m "test(core): add Assertion validation tests"

git add docs/adr/ADR-0009-assertion-immutability.md
git commit -m "docs(adr): record ADR-0009 assertion immutability"

# Check status frequently
git status

# Run gates before pushing
uv run pytest && ruff check && mypy --strict src
```

### Before Creating PR

```bash
# Ensure branch is up to date with main
git checkout main
git pull
git checkout your-branch
git rebase main  # or git merge main

# Final gate check
uv run pytest conformance/  # if touching core/govern/store/query
uv run pytest --cov=src/ontolith

# Push
git push -u origin your-branch
```

### Creating PR

```bash
# Via GitHub web UI or gh CLI
gh pr create --title "feat(core): implement assertion model with provenance" \
  --body "Implements M1 assertion core with append-only semantics.

## Changes
- Add Assertion value object (SPEC §5.3)
- Implement provenance tracking
- Add validation for required fields
- 100% test coverage on core assertion logic

## Testing
- Unit tests: tests/unit/test_assertion.py
- Property tests: tests/property/test_append_only.py
- Conformance: n/a (substrate, no vectors yet)

Closes #<issue> (if applicable)
Part of milestone M1"
```

### After PR Feedback

```bash
# Address review comments with new commits
git add src/ontolith/core/assertion.py
git commit -m "fix(core): handle None confidence per review"

git push  # updates PR automatically
```

### Milestone Checkpoint

```bash
# Check current milestone progress
git log --oneline --grep="^feat(m1)" --grep="^fix(m1)" --all-match

# Tag milestone completion (maintainers only)
git tag -a v0.1.0 -m "M1 - Substrate complete"
git push origin v0.1.0
```

### Emergency: Revert a Merged PR

```bash
# Create revert PR (preserves history)
git revert <commit-sha>
git push origin revert-branch

# NEVER force push to main
```

## Working with Claude Code Agents

### Typical Session Flow

```bash
# 1. Start with a milestone task
# Claude: "I'll implement the Clock port for M0"

# 2. Create branch
git checkout -b m0/clock-port

# 3. Implement with atomic commits
# (Claude creates commits following conventional format)

# 4. Run conformance
# Agent: conformance-runner

# 5. Review before finalizing
# Agent: ontolith-reviewer

# 6. Address findings and commit fixes
git add ...
git commit -m "fix(core): address reviewer findings on Clock interface"

# 7. Final gate check and push
uv run pytest && ruff check && mypy --strict src
git push -u origin m0/clock-port

# 8. Create PR
gh pr create ...
```

### Key Reminders for Agents

- **Always create a branch** for meaningful work (never commit directly to main)
- **Use conventional commits** — format is `<type>(<scope>): <description>`
- **Run gates before pushing** — failing CI wastes time
- **One commit per logical change** — makes review easier
- **Reference milestone** in branch name when applicable
- **Update ADRs** when implementation affects a decision
- **Check Definition of Done** before claiming task complete
