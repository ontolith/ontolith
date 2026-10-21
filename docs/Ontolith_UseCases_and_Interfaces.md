# Ontolith — Use Cases, Interfaces & Examples

*A forward-looking companion to the PRD (v0.2) and SPEC (v0.1). This document is **illustrative, not committal**: it imagines what could flourish on top of a collaborative human + AI ontology framework, to pressure-test the design and seed a roadmap of products, surfaces, and ecosystem plays.*

| | |
|---|---|
| **Document type** | Vision / opportunity map |
| **Status** | Draft for discussion |
| **Builds on** | PRD v0.2, SPEC v0.1 |
| **Last updated** | 2026-06-20 |

---

## 1. Why Ontolith is generative

Most knowledge tools optimize one axis: a wiki is human-authored and readable; a vector store is machine-native and fast; an ontology toolkit is rigorous but static; an agent-memory layer is fluid but unaccountable. Ontolith's bet is that a handful of primitives, combined, open a design space none of them reach alone:

- **Co-equal authorship.** Humans and agents write to the *same* graph, so knowledge stops forking into a "human copy" and a "machine copy."
- **Provenance + confidence on every assertion.** "Who said this, from what source, how sure, and when" is always a one-call lookup — which makes knowledge *defensible*, not just present.
- **Governance as a primitive.** Propose → review → merge means agents can contribute continuously without unverified facts polluting the commons.
- **Bitemporality.** "What is true" and "what we knew, when" are separable — so the past is queryable and audits are real.
- **Contradictions as objects.** Disagreement is captured and routed, not silently overwritten — so conflict becomes signal.
- **Plugin + MCP surfaces.** The graph is reachable by any tool and any agent, so it can sit at the center of an ecosystem rather than the edge of one app.

The use cases below are the ones where *these specific properties* are the unlock — not generic "store some knowledge" scenarios.

---

## 2. Use-case families

### 2.1 The trustworthy organizational brain

**Scenario.** A company's knowledge lives scattered across Slack, docs, tickets, and people's heads. Agents continuously read those sources and **propose** structured facts — "Team X owns Service Y," "Customer Z churned in Q2 because of …" — while humans review and curate. The result is a single graph that is actually trusted, because every fact shows its source and its reviewer.

**Why Ontolith.** Continuous AI enrichment *with* human accountability (co-authorship + governance); "where did this come from" defensibility (provenance); institutional memory you can rewind (bitemporality — "what did we believe about this account last quarter?").

**Shape.** A curated `org` namespace; importer plugins for each source; policy that auto-accepts high-confidence, low-stakes facts and routes the rest to the owning team. Onboarding, decision logs, and "who-knows-what" all become queries.

### 2.2 Shared ground truth for agent fleets

**Scenario.** An organization runs dozens of agents — a research agent, a support agent, a coding agent. Today each keeps its own memory, they disagree, and one agent's hallucination silently spreads. With Ontolith, all of them **read from and propose to one governed ontology**. When two agents disagree about a static fact, a **contradiction** opens instead of a silent overwrite, and a human (or a higher-trust agent) resolves it.

**Why Ontolith.** This is the headline. Per-agent memory fragments truth; a *shared, governed* substrate consolidates it. Confidence + provenance let a consuming agent weight facts; contradictions stop poison from propagating; MCP makes participation a tool call, not an integration project.

**Shape.** Agents authenticate as `ai` principals (each with an accountable human `owner`), default to `propose`, and ground their reasoning via `ontolith.query` with `min_confidence` and trust filters.

### 2.3 Evidence that evolves: living research & reviews

**Scenario.** A research group maintains a **living knowledge base of claims** in a field. Agents ingest new papers and propose claims with a source and a confidence; scientists review. As stronger evidence arrives, older claims are **superseded** along a validity timeline rather than deleted — and you can ask "what was the consensus *as of* last year?"

**Why Ontolith.** Confidence + source per claim; temporal supersession for `time_varying` evidence; contradictions surface genuinely conflicting findings across studies; `as_of` turns the KB into a time machine for how understanding changed.

**Shape.** A `claims` ontology (`Claim`, `Study`, `Method`, `supports`/`contradicts` relations); reasoner plugins that *propose* inferred relationships; a public read view with provenance.

### 2.4 High-stakes & regulated knowledge

**Scenario.** Legal, clinical, and financial teams need knowledge that holds up under scrutiny. Regulations are modeled as concepts; agents propose interpretations and mappings; domain experts review; and crucially you can answer **"what was the applicable policy on the date of this transaction?"** — not just what it is today.

**Why Ontolith.** Auditability is the whole game: immutable provenance, bitemporal "as-known-then" answers, never-silently-overwrite semantics, and review gates that keep unverified agent output out of the record. Confidence + source are compliance-grade metadata, not decoration.

**Shape.** Strict policy (source required, mandatory expert review for sensitive concepts); heavy use of `as_of`; export bridges to existing systems of record.

### 2.5 Civic & investigative knowledge commons

**Scenario.** Investigative journalists or an open community build a structured graph of entities and relationships — people, organizations, transactions, events — each fact tied to a source and a confidence. Conflicting accounts across sources become **contradiction objects** to adjudicate, and the bitemporal log shows **what was claimed when**.

**Why Ontolith.** Fact-checking is provenance-native; disagreement across sources is first-class; a "knowledge commons" co-tended by community members and AI assistants is exactly the dual-principal model. (The framework's own safety posture — agents propose, humans verify — fits sensitive reporting.)

**Shape.** A community namespace with role-based review; connector plugins for public-record sources; a public explorer with a "trust lens" (§3.2).

### 2.6 The personal, accountable second brain

**Scenario.** An individual's AI assistant maintains their personal knowledge graph — contacts, projects, what they're learning — by **proposing** additions the person can glance at and accept. The human stays author-of-record; they can always audit what the AI added and roll it back.

**Why Ontolith.** Local-first single-file ownership; agent-proposes / human-approves keeps the assistant useful without being creepy or wrong-by-accumulation; provenance answers "wait, why does my assistant think this?"

**Shape.** A local SQLite KB, a single `human` owner, one `ai` assistant principal, light policy (review everything, or auto-accept above a high confidence bar).

### 2.7 Engineering & product truth layers

**Scenario.** Coding agents and engineers co-maintain a graph of the system — services, owners, dependencies, incidents, architectural decisions. The graph is queried by other agents to ground their work ("who owns the billing service, and what changed last week?").

**Why Ontolith.** Code and architecture facts go stale fast → temporal supersession; decisions need rationale → provenance; agents both consume and contribute → co-authorship + MCP.

---

## 3. Interfaces & products that could be built on top

The SDK is the engine; these are the surfaces an ecosystem (and the managed business) would grow around it.

### 3.1 The Curation Console
A human governance surface: a **proposal inbox** (what agents and teammates want to add), a diff/preview of staged assertions, a **provenance panel** (author, source, model+version, confidence), and a **contradiction resolver** that ranks rival assertions by confidence × source-trust. This is where "review" lives for non-developers.

### 3.2 The Knowledge Explorer (with a "trust lens")
A graph/table browser where every node and edge can reveal its provenance, confidence, and a **validity timeline**. A time-slider drives `as_of` so you can scrub history. The trust lens lets you fade out low-confidence or unreviewed facts — making the *epistemic state* of the graph visible, not just its contents.

### 3.3 The Grounding Gateway
A hosted **MCP + REST endpoint** that any agent or app plugs into to read and propose against the commons — "the API to your organization's verified truth." Agents get `query`, `get`, `provenance`, `propose`, and `flag_contradiction`; never direct write. This is the surface that makes Ontolith the center of an agent ecosystem.

### 3.4 The Trust & Health Dashboard
Operational visibility for the commons: human-vs-AI authorship ratio, proposal acceptance rate, open contradictions, **stale-fact detection** (assertions whose `time_varying` value hasn't been refreshed), coverage gaps, and review latency. Turns "is our knowledge healthy?" into metrics.

### 3.5 Schema Studio & domain packs
A design surface for ontologies (class DSL ↔ LinkML-aligned YAML, round-tripped), with the ability to **publish reusable domain packs** — pre-built schemas for healthcare, legal, finance, devrel, biotech. Installing an ontology becomes as easy as `pip install`-ing a package.

### 3.6 Chat-over-Ontology
A natural-language Q&A surface that answers from the graph **with citations and confidence**, not vibes. Because retrieval is provenance-aware, the answer can say "X (per source S, reviewed by R, confidence 0.9) — though a contradicting claim is under review." This is RAG with a conscience.

### 3.7 Embeddable knowledge widgets & a notification layer
Drop-in "verified knowledge" panels for internal tools, plus a digest: *"Your assistant proposed 12 facts today; 3 need review; 1 contradiction opened on Customer Acme's renewal date."* Governance that comes to you.

---

## 4. Worked examples

These exercise the actual API and MCP surfaces from the SPEC, so the vision stays grounded.

### 4.1 An org brain: agent proposes, human curates, app queries with provenance

```python
from ontolith import Ontology, Concept, Relation, Text, Date, Ref


class Team(Concept):
    name: Text


class Service(Concept):
    name: Text
    owner: Ref[Team] = Relation(inverse="services", temporality="time_varying")


kb = Ontology.connect("acme-org", schema=[Team, Service])

alice = kb.principal("alice@acme.com", kind="human")
mapper = kb.principal(
    "ownership-mapper", kind="ai", owner="alice@acme.com", auth="oidc", default_capability="propose"
)

# Agent reads Slack + the service catalog and proposes an ownership fact
mapper.propose(
    Service(name="billing", owner=Team(name="Payments")),
    source="slack://eng-ownership/2026-06-18",
    confidence=0.78,
    rationale="Two engineers confirmed in #eng-ownership",
)
# Policy: 0.78 < auto-accept 0.9 -> routed to review, assigned to the Payments lead.

# An internal app later asks the graph, and shows its work:
svc = kb.get(Service, name="billing")
print(svc.owner)  # -> Team(name="Payments")
print(svc.provenance("owner"))  # author, source, reviewer, confidence, valid_from
```

When ownership changes next quarter, the new proposal **supersedes** the old one (it's `time_varying`), and `kb.as_of("2026-03-01").get(Service, name="billing")` still returns the old owner — institutional memory, intact.

### 4.2 An agent fleet sharing ground truth — and catching a hallucination

```python
# Two agents participate in the same research namespace.
scout = kb.principal("scout", kind="ai", owner="alice@acme.com", default_capability="propose")
auditor = kb.principal("auditor", kind="ai", owner="alice@acme.com", default_capability="propose")

# scout proposes a (static) biographical fact
scout.propose(
    Person(name="Ada Lovelace", born="1815-12-10"),
    source="https://example.org/bio-A",
    confidence=0.82,
)

# auditor, grounding on another source, sees a conflicting date and flags it
auditor.flag_contradiction(
    subject="Ada Lovelace",
    predicate="born",
    note="Source bio-B gives 1815-12-10 but ledger gives a different date; please verify.",
)
# A contradiction object opens; BOTH assertions are retained but excluded from default
# retrieval until a human resolves it. Neither agent can silently win.
```

Over MCP, that same interaction is just two tool calls (`ontolith.propose`, `ontolith.flag_contradiction`) — any compliant agent joins the commons without bespoke glue, and the no-write-tool rule guarantees it can never overwrite the record.

### 4.3 A living review: confidence, supersession, and "consensus as of…"

```python
# As evidence accumulates, claims gain/lose support over time.
litbot = kb.principal("litbot", kind="ai", owner="pi@lab.edu", default_capability="propose")

litbot.propose(
    Claim(text="Compound X reduces marker M", status="supported", strength=0.6),
    source="doi:10.1/aaa",
    confidence=0.6,
)

# A stronger meta-analysis arrives; reviewers accept an updated, higher-strength claim,
# which supersedes the prior one along the validity timeline.
litbot.propose(
    Claim(text="Compound X reduces marker M", status="supported", strength=0.85),
    source="doi:10.1/meta-2026",
    confidence=0.85,
)

# What did the field believe a year ago vs now?
last_year = kb.as_of("2025-06-01").query(Claim).where(text__contains="Compound X")
today = kb.query(Claim).where(text__contains="Compound X")
```

### 4.4 Regulated lookup: "what was the policy on the transaction date?"

```python
# A compliance app answers as-of-the-event, not as-of-today — the audit-critical question.
policy_on_date = (
    kb.as_of(transaction.booked_at)
    .query(Regulation)
    .where(applies_to="cross-border-transfer")
    .min_confidence(0.9)
)
# Every returned rule carries its source, its reviewer, and the date it became effective.
```

### 4.5 Chat-over-ontology answering with provenance

A natural-language layer turns "Who owns billing and has that changed recently?" into a structured query plus a `provenance` lookup, and answers:

> **Payments** owns the billing service (source: #eng-ownership, reviewed by the Payments lead, confidence 0.9). Ownership moved from **Platform** to **Payments** on 2026-04-02. *No open contradictions.*

The difference from ordinary RAG is the parenthetical: the answer is **accountable**.

---

## 5. Ecosystem flourishing

Because every boundary is a plugin (SPEC §13), the interesting growth is at the edges:

- **Domain ontology packs** — installable schemas (healthcare, legal, finance, biotech, devrel) so teams start from a vetted model, not a blank file. A registry of these is a natural community + marketplace play.
- **Connector marketplace** — importers/connectors for Slack, Notion, GitHub, Salesforce, PubMed, EDGAR, and more, each turning a source into governed proposals.
- **Reasoner plugins** — domain rule engines and LLM-assisted inference that **propose** (never auto-write) derived knowledge for human/policy approval.
- **Embedder & retrieval plugins** — swap embedding models per namespace; specialized rerankers for the hybrid pipeline.
- **Bridge plugins** — LinkML, then RDF/OWL, then agent-memory stores, letting Ontolith sit *between* the semantic-web world and the agent-memory world rather than competing with either.

A healthy ecosystem looks like: a few canonical domain packs, dozens of connectors, and a long tail of reasoners — all composing because they share the assertion/provenance/governance core.

---

## 6. The through-line

Every use case above is a variation on one idea: **a knowledge commons that humans and AI tend together, where trust is built into the substrate.** The agent-memory world made knowledge fast but unaccountable; the ontology world made it rigorous but static and human-only. Ontolith's wager is that the valuable thing to build now is the place in between — and that the products which flourish on top will be the ones that make *epistemic state* (who knows what, how sure, since when, and where there's disagreement) a first-class, visible, governable thing.

> **Caveat.** This is a map of possibilities, not a commitment. The next step would be to pick one or two beachhead use cases (the agent-fleet ground-truth layer and the org brain are the strongest candidates) and validate them against real workloads before the ecosystem framing earns investment.
