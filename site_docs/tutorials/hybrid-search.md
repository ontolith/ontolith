# Hybrid Search

Full source:
[`examples/hybrid_search.py`](https://github.com/ontolith/ontolith/blob/main/examples/hybrid_search.py)

This tutorial combines vector similarity search with symbolic filters and
provenance-confidence floors — all through the same `QueryBuilder`.

## Indexing

Vectors enter the index through exactly one path: `reindex()`. No write
call auto-embeds — this is deliberate, so embedding cost is under your
control:

```python
kb = Ontology.connect(db_path)  # embedder defaults to a lightweight HashingEmbedder
n = kb.reindex("Researcher")  # re-embeds every Researcher's Text content
```

`reindex()` concatenates each entity's `Text`-typed active-assertion values
and embeds that as one string. It's idempotent — call it again after adding
more `Text` assertions and it picks up the new content. A real deployment
would configure a production `Embedder` (any class implementing the
`Embedder` protocol) instead of the default `HashingEmbedder`, which exists
so examples and tests don't need to download an ML model.

## Semantic search

```python
kb.query("Researcher").semantic("programming languages").limit(2).all()
```

## Intersecting with a symbolic filter

```python
kb.query("Researcher").semantic("kernels").where(h_index__gte=10).limit(5).all()
```

As implemented (ADR-0020), this runs **vector search first** with an
overfetch, then intersects that candidate set with the symbolic `.where()`
filter, preserving vector-rank order — not a symbolic prefilter reranked by
vector distance. The practical effect: `.limit(n)` bounds the *final*,
post-intersection result count, not the number of candidates considered.

This is a deliberate bounded-cost tradeoff, not free: the overfetch window
is 10× your own `.limit()` (200 if you set none), capped at 1000 candidates,
so a genuine symbolic match ranked *below* that window in the global
vector ranking is silently missed —
exactness is traded for a predictable p95 query cost.

## Filtering by provenance, not just content

`.min_confidence()`/`.trust_at_least()` filter on the *provenance* of an
entity's assertions, independent of `.semantic()`/`.where()`:

```python
kb.query("Researcher").min_confidence(0.8).all()
```

This keeps an entity if **at least one** of its assertions was recorded at
or above the threshold — not all of them. If every assertion about a given
researcher was recorded at `confidence=0.4`, that researcher is excluded;
if even one assertion (say, their name) was recorded at `confidence=1.0`,
they're kept. `.trust_at_least(level)` works the same way, filtering on the
*asserting principal's* trust level (accounting for delegation's `min()`
rule, ADR-0033) rather than confidence.

Chain everything together for a genuinely hybrid query — meaning matches,
symbolic constraints, and provenance floors, all in one call:

```python
kb.query("Researcher").semantic("kernels").where(h_index__gte=10).min_confidence(0.8).all()
```
