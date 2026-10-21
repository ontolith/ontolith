# Product Requirements Document — **Ontolith**

*A Python framework/SDK for building collaborative knowledge bases where humans and AI agents are co-equal authors of a shared, governed ontology.*

| | |
|---|---|
| **Working name** | Ontolith *(placeholder — Greek κοινή, "the common/shared tongue"; alternatives in Appendix A)* |
| **Document type** | Product Requirements Document (proposal) |
| **Status** | Draft v0.2 — open questions resolved |
| **Owner** | *[you]* |
| **Last updated** | 2026-06-20 |

> **Note on scope.** This is a *proposal* meant to be argued with. The eight design decisions from the v0.1 review are now folded in (see §16). A short "still to validate" list remains; treat remaining specifics (thresholds, benchmark targets, dialect coverage) as starting points.

---

## 1. TL;DR

Ontolith is an open-source Python framework for defining and operating a **living ontology** that both **people** and **AI agents** read from and write to as first-class contributors. Unlike agent-memory libraries (which mostly help a single agent remember across sessions) and unlike classic ontology toolkits (which are schema-as-code for the semantic web), Ontolith treats the ontology as a **shared, deliberately-designed, governed artifact**: every assertion carries provenance and a confidence score, every contributor is an identified principal (human or AI) with an accountable owner, and every change flows through a configurable proposal/review pipeline. Extensibility is via a plugin system (importers, exporters, reasoners, validators, embedders, connectors, storage backends). The headline use case is a **collaborative knowledge ecosystem** — a knowledge "commons" co-tended by humans and AI systems. It ships **open-core**: an Apache-2.0 framework with a managed offering to follow.

---

## 2. Problem & motivation

Organizations increasingly run mixed teams of people *and* AI agents against the same body of knowledge. Today those two populations use incompatible substrates:

- **Humans** curate knowledge in wikis, docs, and schema-as-code ontologies. These are authoritative and reviewable but opaque to agents and slow to evolve.
- **AI agents** accumulate knowledge in vector stores and agent-memory layers. These are fast and machine-native but largely unreviewable, weakly governed, and built around *one agent remembering*, not *many parties agreeing*.

The result is two diverging copies of "what we know," neither trusted by the other. When an agent writes a fact, a human can't easily see who said it, why, or how confident the source was. When a human curates a concept, agents can't reliably consume it as structured knowledge.

**The gap:** there is no widely-adopted, Python-native framework where a shared ontology is the single substrate, humans and agents are co-equal authors, and trust is engineered in via provenance, confidence, and review — rather than bolted on. Ontolith targets that gap.

**Why now:** mixed human/agent workflows are becoming the default; agent-tool standards (e.g. MCP) make agent-native interfaces tractable; and the market has validated demand for both knowledge graphs *and* human-owned canonical knowledge — but not yet unified them.

---

## 3. Landscape & positioning

Two adjacent categories exist; Ontolith sits deliberately between them.

| Category | Representative tools (2026) | Strengths | Why they don't close the gap |
|---|---|---|---|
| **Classic Python ontology / modeling** | Owlready2, LinkML / Ontology Access Kit, RDFLib | Rigorous schemas, reasoning, semantic-web interop | Schema-as-code for humans/standards; not collaboration-native, no agent-author model, no provenance-per-assertion or review workflow |
| **Agent memory / knowledge graphs** | Mem0, Zep (Graphiti), Cognee, Letta/MemGPT, LangMem, LlamaIndex Memory | Fast, machine-native, temporal facts, doc ingestion | Built around *an agent* remembering or extracting from docs; weak human authorship/governance; ontology is implicit, not deliberately designed |

**Ontolith's wedge:** a *collaboration-first, dual-principal* ontology framework. The differentiators competitors don't combine:

1. **Co-equal human + AI authorship** with one identity/permission model for both.
2. **Provenance and confidence on every assertion**, not just on documents.
3. **Configurable governance** (propose → review → merge) as a core primitive, not an afterthought.
4. **A deliberately-designed ontology** (typed concepts, relations, axioms) rather than an emergent graph.
5. **Honest conflict handling** — temporal supersession *and* explicit contradictions, a combination the incumbents mostly lack.
6. **Plugin extensibility** so it federates with the tools above instead of replacing them (e.g. import a Cognee graph, export to LinkML/OWL, embed with any model).

**Interoperability, not isolation.** Ontolith should *bridge* to LinkML/RDF/OWL and to agent-memory stores via plugins, so it complements rather than competes with the incumbents.

---

## 4. Vision & product principles

1. **One substrate, two kinds of mind.** Humans and agents collaborate on the *same* knowledge, with the same primitives, distinguished only by identity and policy.
2. **Trust is engineered, not assumed.** No assertion exists without a source, an author, and a confidence. The system makes "who said this and why" a one-line lookup.
3. **The ontology is a designed object.** Contributors model concepts and relations on purpose; emergent structure is allowed but always reconcilable to the schema.
4. **Governance is configurable, not hard-coded.** A solo researcher and a regulated enterprise should use the same framework with very different review policies.
5. **Extensible by default.** Every boundary (storage, reasoning, embedding, ingest, export, identity, policy) is a plugin point.
6. **Pythonic and boring where it counts.** Defining an ontology should feel like writing dataclasses; the hard parts (provenance, merge, history) should be invisible until needed, and the foundation (storage) should be stable, not trendy.
7. **Local-first, scale-out later.** Start on a laptop with a single SQLite file and zero infra; the same code grows to a shared server.

---

## 5. Goals & non-goals

**Goals (v1)**
- A Python SDK to define ontologies as code and operate a knowledge base over them.
- A dual-principal model (human + AI) with identity, accountable ownership for agents, permissions, and per-assertion provenance/confidence.
- A configurable proposal → review → merge governance pipeline.
- Versioning / history / time-travel, plus conflict handling (temporal supersession + contradiction objects).
- A plugin architecture with a documented extension API and a handful of reference plugins.
- Agent-native access (an MCP server + tool interface) and a programmatic query API with hybrid symbolic + semantic retrieval.
- Pluggable persistence with a zero-config, SQLite-backed embedded default.

**Non-goals (v1)**
- *Not* a general-purpose database or a full RDF/OWL triplestore (we **interoperate** with these, not reimplement them).
- *Not* an LLM, embedding model, or reasoner of our own — we orchestrate pluggable ones.
- *Not* an end-user note-taking app or a hosted SaaS — Ontolith is a framework/SDK; a managed offering and UI are later, separable layers (§12).
- *Not* real-time multi-writer CRDT collaboration in v1 (proposals/review first; live co-editing is vNext).
- *Not* a fully automated reasoner that auto-resolves contradictions — v1 *detects and surfaces*; resolution is governed.

---

## 6. Target users & personas

| Persona | Description | Primary jobs-to-be-done |
|---|---|---|
| **Ontology Architect** | Defines the schema: concepts, relations, axioms, temporality, validation rules. | Model the domain in code; evolve the schema safely; set governance policy. |
| **Domain Contributor (human SME)** | Adds, curates, and corrects knowledge; reviews proposals. | Assert facts; review agent proposals; resolve contradictions. |
| **AI Agent** | Reads, queries, proposes, and flags inconsistencies under policy. | Ground itself in shared knowledge; propose new/updated facts with sources; surface contradictions. |
| **Integrator / Developer** | Embeds the SDK in an app or builds plugins. | Connect data sources; expose Ontolith to their agents; extend behavior. |
| **Governance / Curator** | Owns quality and trust of the commons. | Configure policy; audit provenance; manage trust levels and access. |

**Primary buyers/adopters:** individuals and organizations who want to *set up and own their ontology* and connect both their people and their AI systems to it.

---

## 7. Core concepts & domain model

Ontolith's meta-model is intentionally small. Everything else (governance, retrieval, plugins) operates over these primitives.

- **Concept** — a typed class of thing (e.g. `Person`, `Organization`, `Policy`). Defined as code.
- **Property** — a typed attribute on a concept (literal: text/number/date/etc.), with a declared **temporality**.
- **Relation** — a typed, directional link between concepts, optionally with an inverse, also carrying a **temporality**.
- **Temporality** — a per-property/relation attribute, `time_varying` or `static` (**default `static`**), that determines how a conflicting write is handled (see Assertion / §8 P3).
- **Entity (Instance)** — a concrete individual of a concept.
- **Assertion** — the atomic unit of knowledge: *(subject, property|relation, value)* plus metadata. **Every assertion carries**: author (principal), source, **confidence** (a single scalar `0.0–1.0` = the asserting principal's *stated belief* that it's true, deliberately distinct from any system-derived corroboration score), timestamp, and validity window (`valid_from`/`valid_to`). An open metadata blob travels alongside so confidence semantics can grow later without a schema break.
- **Axiom / Rule** — constraints and inferences over the schema (cardinality, disjointness, derived relations, validation predicates).
- **Principal** — an identified actor. `kind ∈ {human, ai, service}`, with permissions and a per-namespace **trust level**. **AI principals must declare an accountable owner** (a responsible human or team). The **model + version** behind an AI principal is captured *per assertion* in provenance, since one agent identity may run different models over time.
- **Provenance record** — the immutable trail behind any assertion (who, what source, when, which model+version, why, confidence, and any delegation chain).
- **Delegation (acting-as)** — when an agent acts on behalf of a human, provenance records both, so an assertion can be attributed to "agent X acting for user Y."
- **Proposal** — a staged set of changes awaiting policy evaluation (auto-accept, review, or reject).
- **Contradiction** — an object linking two or more assertions that disagree about the same **static** fact; rivals are kept live-but-flagged and routed to review.
- **Namespace / Domain** — a scoping boundary for an ontology and its governance policy.

### 7.1 Illustrative Python API (DX sketch)

```python
from ontolith import Ontology, Concept, Relation, Text, Date, Ref


# 1. Define the schema (the ontology) as code; declare temporality per field
class Person(Concept):
    name: Text
    born: Date | None = None  # static (default): conflicts -> contradiction
    employer: Ref["Organization"] = Relation(
        inverse="employees",
        temporality="time_varying",  # changes -> temporal supersession
    )


class Organization(Concept):
    name: Text
    industry: Text | None = None


# 2. Open a shared knowledge base over that schema
kb = Ontology.connect("acme-research", schema=[Person, Organization])

# 3. Principals - humans and agents are both first-class
alice = kb.principal("alice@acme.com", kind="human")
scout = kb.principal(
    "scout-agent",
    kind="ai",
    owner="alice@acme.com",  # accountable human/team - REQUIRED for AI principals
    auth="oidc",  # service identity (client-credentials / workload identity)
    default_capability="propose",  # read + propose by default; never direct write
)

# 4. Agents propose; policy decides whether review is required
proposal = scout.propose(
    Person(name="Ada Lovelace", born="1815-12-10"),
    source="https://example.org/biography",
    confidence=0.82,  # the agent's stated belief, 0.0-1.0
    rationale="Extracted from biography corpus, single source",
)
# Provenance auto-captures model+version and acting_as (the human who triggered scout).

if proposal.confidence < kb.policy.auto_accept_threshold:
    proposal.request_review(assignee=alice)  # human-in-the-loop
else:
    proposal.accept()

# 5. Query / ground - hybrid symbolic + semantic (sqlite-vec embedded by default)
results = kb.query(Person).where(employer=engine_co.id).semantic("computing pioneers").limit(10)

# 6. Provenance, time-travel, and conflict are built in
ada = kb.get(Person, name="Ada Lovelace")
ada.history()  # full change log across all principals
ada.provenance("born")  # who/what asserted this, when, model+version, confidence
ada.contradictions()  # any unresolved disagreements on static facts
kb.as_of("2025-01-01").get(Person, name="Ada Lovelace")  # time-travel
```

The **same operations are exposed as MCP tools** (read/query, propose, flag_contradiction, provenance lookups), so any compliant agent can participate without bespoke glue.

---

## 8. Functional requirements (capability pillars)

### P1 — Ontology modeling core
- Define concepts, properties, relations, and axioms; **declare per-property temporality** (`time_varying` vs `static`, default `static`).
- A **single internal schema model (IR) is the source of truth**, with two front-ends that compile to it: a **class-based Python DSL** (ships first, best DX) and a **declarative YAML dialect aligned with LinkML** (ships 0.2). Codegen is **bidirectional** (classes ↔ YAML).
- Schema validation and safe **migration/versioning** (additive by default; breaking changes gated).
- Mixed open/closed-world behavior configurable per namespace.

### P2 — Dual-principal access
- One identity model spanning humans, AI agents, and services; pluggable auth (SSO/OIDC for humans, OIDC client-credentials / workload identity for agents).
- **AI principals must declare an accountable owner**; **model + version is captured per assertion** in provenance.
- Optional **delegation / acting-as** chain so provenance records both the agent and the human who invoked it.
- Per-principal **permissions** (read / propose / write / review / admin) and per-namespace **trust levels** that policy references.
- Agents default to **propose**, not write.

### P3 — Collaboration & governance
- **Proposals** as the default write path for low-trust principals; direct writes only for high-trust principals with explicit grants.
- Configurable **policy engine**: thresholds on confidence/trust, required reviewers, source requirements, per-concept rules.
- **Review workflow**: assign, comment, accept, reject, request-changes.
- **Confidence** is a single scalar `0.0–1.0` (asserting principal's stated belief), stored beside an open metadata blob for future evolution; corroborating scalars are **surfaced, not auto-combined**, in v1.
- Full **provenance** on every assertion; immutable audit log.
- **Versioning / time-travel**: reconstruct the KB (or any entity) as of any point in time.
- **Conflict handling routed by temporality**: `time_varying` properties use **temporal supersession** (close prior validity window, open a new one — no review, it's expected); `static` properties create a **contradiction object** (rivals kept live-but-flagged, routed to review, rankable by confidence × source trust).

### P4 — Plugin & extensibility system
- Stable extension API with discovery via entry points; clear lifecycle hooks.
- Plugin categories (see §10): **importers, exporters, reasoners, validators, embedders, connectors, storage backends, auth providers, policy strategies**.
- Sandboxing/permissions model for third-party plugins (security; see §11).

### P5 — Query & retrieval
- Programmatic query builder over the schema (filter by property/relation, traverse, aggregate).
- **Hybrid retrieval**: symbolic graph queries + semantic/vector search over entities and assertions (sqlite-vec embedded by default), for grounding.
- Provenance- and confidence-aware results (filter/rank by trust, recency, source, validity window).

### P6 — Interfaces
- **Python SDK** (primary).
- **CLI** for ops (init, migrate, import/export, inspect history, manage principals).
- **REST + GraphQL API** for app integration.
- **MCP server + agent-tool interface**: **read/query, propose, flag_contradiction, and provenance lookups** by default; **no direct write** without an elevated, explicitly granted capability.

### P7 — Storage & persistence
- Pluggable backend; **zero-config SQLite-backed assertion store** as the embedded default, with **sqlite-vec** in the same file for embedded hybrid (symbolic + vector) search — no extra infra for local use.
- Scale-out adapters behind the same interface (e.g. DuckDB+DuckPGQ, a maintained graph engine, or a server graph DB). **Benchmark deep multi-hop traversal early**, since the default leans on recursive SQL.
- Export/import to standard formats (JSON, plus LinkML/RDF/OWL bridges via plugins).

---

## 9. Architecture overview

A layered design; each layer is replaceable.

```
+---------------------------------------------------------------+
|  Interfaces:  Python SDK . CLI . REST/GraphQL . MCP/agent tools|
+---------------------------------------------------------------+
|  Governance & Collaboration:  proposals . policy engine .     |
|  review workflow . provenance . versioning . conflict handling|
+---------------------------------------------------------------+
|  Access & Identity:  principals (human/ai/service) . owners . |
|  perms . trust levels . delegation . auth providers           |
+---------------------------------------------------------------+
|  Query & Reasoning:  query builder . hybrid (symbolic+vector) |
|  retrieval . pluggable reasoners/validators                   |
+---------------------------------------------------------------+
|  Ontology Core (meta-model):  concepts . relations . axioms . |
|  entities . assertions . temporality                          |
+---------------------------------------------------------------+
|  Persistence:  SQLite-backed assertion store (default) +      |
|  sqlite-vec; pluggable scale-out backends                     |
+---------------------------------------------------------------+
|  Plugin runtime & registry  (cross-cuts all layers)           |
+---------------------------------------------------------------+
```

**Key design stances**
- **Assertion-centric storage** so provenance/confidence/validity attach naturally to the smallest unit of knowledge — which maps cleanly to rows-with-metadata in the SQLite default.
- **Policy as a pure function** over (proposal, principal, KB state) → decision, so governance is testable and swappable.
- **Plugins are capabilities, not forks**: importing a graph or swapping the embedder never requires touching core.

---

## 10. Plugin types / extension points

| Plugin type | Purpose | Example |
|---|---|---|
| **Importer** | Ingest external knowledge into the ontology | Notion/Slack/PDF/CSV → entities + assertions; import an existing graph |
| **Exporter** | Emit to external formats/systems | Export to LinkML (first), then RDF/OWL, JSON-LD, or a downstream KG |
| **Reasoner** | Derive new assertions / classify | Rule engine; description-logic bridge; LLM-assisted inference |
| **Validator** | Enforce constraints on writes | Cardinality, type, domain rules, source-required checks |
| **Embedder** | Produce vectors for semantic retrieval | Any embedding model, swappable per namespace |
| **Connector** | Live link to a system of record | CRM, ticketing, code host kept in sync |
| **Storage backend** | Persistence implementation | Embedded SQLite + sqlite-vec (default); DuckDB+DuckPGQ / graph engine / server DB for scale-out |
| **Auth provider** | Identity for principals | OIDC/SSO for humans, client-credentials/workload identity for agents |
| **Policy strategy** | Governance logic | Confidence-threshold, mandatory-review-by-role, source-quorum |

**Interop sequence: LinkML → RDF/OWL → agent-memory bridges.** LinkML comes first because the YAML schema dialect (§8 P1) is already LinkML-aligned, so the first bridge is largely a byproduct; LinkML round-trips to JSON/RDF, yielding partial RDF reach immediately. RDF/OWL export follows (initially via LinkML emission). Agent-memory bridges come last — high value but a more fluid ecosystem, and more about ingestion than standards.

---

## 11. Non-functional requirements

- **Performance:** local-first reads/writes feel instant on laptop-scale KBs via the SQLite default. Deep multi-hop traversal uses recursive SQL, so **benchmark it from MVP** and lean on a scale-out backend when traversal dominates. Keep the core thin; push indexing/scale to the storage layer.
- **Scalability:** same SDK from a single-file embedded KB to a shared, multi-principal server.
- **Security:** plugin sandboxing/permissioning; least-privilege for principals; agents default to *propose*, not *write*; full audit trail.
- **Trust & safety (AI-specific):** confidence + source required on agent writes; agents reach the KB over MCP only via **propose** and **flag_contradiction**, never direct write; accountable owner + per-assertion model capture make every fact attributable and reversible; review gates keep unverified agent assertions out of the commons.
- **Interoperability:** first-class bridges to LinkML/RDF/OWL and to common agent-memory stores.
- **Dependency maturity:** embedded vector search relies on **sqlite-vec, which is still pre-v1** — pin versions and isolate it behind the storage interface so it can be swapped. Embedded *graph* engines are volatile (e.g. KùzuDB was archived in 2025), which is itself the reason the default is plain SQLite.
- **Observability:** structured logs; metrics on proposal/acceptance rates, contradiction counts, retrieval quality.
- **Portability / ownership:** users own their data (a single SQLite file locally); no required cloud dependency; clean export.
- **Versioning & reproducibility:** any query reproducible against a historical KB state.

---

## 12. Licensing & business model (open-core)

Ontolith is **open-core**: a permissively-licensed OSS framework with a managed offering built on top as a separate business unit.

- **License:** **Apache-2.0 on the OSS core.** Embeddable developer tools win on frictionless adoption, and copyleft/source-available licensing creates exactly the hesitation to avoid in the layer everyone is meant to build on.
- **The plugin seams are the open-core boundary.** OSS core = meta-model, governance primitives, plugin runtime, SDK, embedded storage, MCP server. Managed plane = hosted multi-tenant servers, enterprise SSO/identity, scale-out storage ops, the collaboration UI, audit/compliance dashboards, managed embeddings, and a plugin/ontology marketplace. The `auth provider`, `storage backend`, and `policy strategy` extension points are exactly where proprietary managed implementations slot in — so architecture and business model reinforce each other.
- **Governance:** adopt a **CLA or DCO** so the project retains the right to offer proprietary builds; **keep trademark control** of the name even though the code is permissive.
- **Defensive option:** if a cloud provider hosting a competing managed version becomes a concrete threat, **AGPL-on-core** is the fallback — but it taxes the early adoption Ontolith needs, so default to permissive.

---

## 13. Phasing / roadmap

| Phase | Theme | Includes |
|---|---|---|
| **MVP (0.1)** | *The substrate works* | Class-based schema DSL; ontology core with per-property temporality; principals + accountable-owner model; assertions with single-scalar confidence + provenance; SQLite-backed embedded storage (+sqlite-vec); proposal/accept + simple threshold policy; Python SDK + CLI; basic query builder. |
| **0.2 — Collaboration** | *Humans and agents co-author* | Review workflow; versioning/time-travel; conflict handling (temporal supersession + contradiction objects); MCP server + agent tools (read/query, propose, flag_contradiction, provenance); trust levels; delegation/acting-as; LinkML-aligned YAML schema dialect; first reference plugins (one importer, one exporter, one embedder). |
| **0.3 — Extensible & retrievable** | *Open the boundaries* | Plugin registry + stable extension API; hybrid semantic retrieval (full); REST/GraphQL; pluggable scale-out storage (DuckDB+DuckPGQ / graph engine); **LinkML bridge first, then RDF/OWL**. |
| **1.0 — Production** | *Govern at scale* | Hardened policy engine; plugin sandboxing/security; performance benchmarks; docs + examples; migration tooling. |
| **vNext** | *Live commons* | Real-time multi-writer collaboration (CRDT-style merge); automated/assisted conflict resolution; federation across KBs; agent-memory bridges; optional UI; plugin/ontology marketplace. |

---

## 14. Success metrics

- **Adoption:** installs, KBs created, time-to-first-ontology (target: a working ontology in minutes from `pip install`).
- **Collaboration health:** ratio of human vs. AI authored assertions; **proposal acceptance rate**; review turnaround time.
- **Knowledge quality:** contradiction/violation rate over time; % assertions with valid source + confidence; reversal rate of agent-authored facts.
- **Grounding value:** downstream retrieval accuracy / answer quality for agents using Ontolith vs. baseline.
- **Ecosystem:** number of third-party plugins; bridges in active use.

---

## 15. Risks & mitigations

| Risk | Mitigation |
|---|---|
| **AI writes pollute the commons** (hallucinated facts) | Agents default to *propose*; confidence/source required; accountable owner per AI principal; review gates; provenance enables fast reversal. |
| **Custom Python core underperforms** on deep traversal | Keep core thin; SQLite-backed default with recursive CTEs for laptop scale; benchmark traversal from MVP; push scale to pluggable backends (DuckDB+DuckPGQ / graph engine / server DB). |
| **Embedded-graph backends are volatile** (e.g. KùzuDB archived 2025) | Default to stable, ubiquitous SQLite; keep any scale-out graph engine behind the storage plugin interface so a single backend's fate is contained. |
| **Embedded vector dependency is pre-v1** (sqlite-vec) | Pin versions; isolate behind the storage interface; alternatives exist if it stalls. |
| **Reinventing the semantic-web wheel** / interop loss | Don't reimplement reasoners or triplestores; ship LinkML → RDF/OWL bridges; position as complement. |
| **Concurrency & merge complexity** | v1 uses proposals/review (serialized writes) + temporal supersession for time-varying facts; defer live CRDT collaboration to vNext. |
| **Plugin security** (untrusted code) | Sandboxing + capability permissions; signed/registry plugins; least-privilege defaults. |
| **Crowded adjacent market** (agent memory) | Differentiate on collaboration + governance + designed ontology + honest conflict handling; integrate rather than compete. |
| **Governance overhead deters solo users** | Policy is configurable; defaults are light; review only kicks in above thresholds. |

---

## 16. Decisions resolved

The eight v0.1 open questions are now decided:

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Storage default | **SQLite-backed assertion store + sqlite-vec; pluggable scale-out** | Assertion model maps to rows-with-metadata; serves local-first/ownership; embedded-graph space is volatile. |
| 2 | Schema definition | **Both, via one IR**: class-based DSL first, LinkML-aligned YAML in 0.2; bidirectional codegen | Best DX for devs *and* a portable, interchange-friendly format; YAML doubles as the LinkML bridge. |
| 3 | Agent identity | **Service principal + mandatory accountable owner + per-assertion model capture + service auth + delegation/acting-as** | Always a human accountable for agent facts; attribution survives model changes; provenance records who triggered the agent. |
| 4 | Confidence semantics | **Single scalar `0.0–1.0`** (principal's stated belief) + open metadata blob; **no auto-combine** in v1 | Simple now, future-proofed against per-source/aggregated confidence later. |
| 5 | Conflict model | **Both, routed by temporality**: `time_varying` → supersession; `static` → contradiction + review; default `static` | Supersession ("world changed") and contradiction ("sources disagree") are different phenomena; defaulting static surfaces conflicts rather than burying them. |
| 6 | Licensing & business | **Open-core**: Apache-2.0 core, proprietary managed plane; CLA/DCO + trademark | Permissive maximizes adoption of an embeddable tool; plugin seams are the monetization boundary. |
| 7 | Interop priority | **LinkML → RDF/OWL → agent-memory** | LinkML compounds with the YAML decision and round-trips to RDF, so it's the highest reach-per-effort first bridge. |
| 8 | MCP surface | **Read/query + propose + flag_contradiction + provenance**; no direct write without elevated grant | Safe default that matches the trust model: agents propose, humans/policy merge. |

**Still to validate**
- Deep-traversal benchmark on the SQLite default; the scale at which a scale-out graph backend becomes necessary.
- Exact LinkML dialect coverage and round-trip fidelity.
- Confidence calibration across human vs. AI principals; if/when to introduce aggregation.
- Default policy thresholds (auto-accept confidence/trust) for common deployment profiles.
- Final name + available package/domain + trademark clearance.
- sqlite-vec (pre-v1) stability vs. alternatives over time.

---

## Appendix A — Naming

`Ontolith` is a working placeholder (κοινή — the shared common language of the Hellenistic world; apt for a *shared* knowledge tongue between humans and machines). Alternatives worth considering: **Commons**, **Concord**, **Agora**, **Tessera** (a mosaic tile / token of mutual recognition), **Noēsis**, **Lattice**, **Mycel**. Pick for clarity, memorability, and an available package name + domain.

## Appendix B — Glossary

- **Assertion** — atomic *(subject, predicate, value)* + provenance/confidence/validity.
- **Confidence** — single scalar `0.0–1.0`; the asserting principal's stated belief, not a system-derived score.
- **Principal** — an identified actor (human, AI, or service) with permissions and trust; AI principals have an accountable owner.
- **Provenance** — the immutable who/what/when/why/which-model behind an assertion, including any delegation chain.
- **Proposal** — staged changes evaluated by policy before entering the KB.
- **Temporality** — per-property attribute (`time_varying`/`static`) governing conflict handling.
- **Contradiction** — an object linking assertions that disagree about the same static fact.
- **Trust level** — a per-principal attribute policy uses to decide auto-accept vs. review.
- **Axiom** — a schema-level constraint or inference rule.

## Appendix C — Comparable projects (for positioning, not endorsement)

Classic ontology/modeling: Owlready2, LinkML / Ontology Access Kit, RDFLib. Agent memory / knowledge graphs: Mem0, Zep (Graphiti), Cognee, Letta/MemGPT, LangMem, LlamaIndex Memory. Ontolith's distinct position: collaboration-first, dual-principal authorship with governance, a deliberately-designed ontology, and honest conflict handling — interoperating with the above via plugins.
