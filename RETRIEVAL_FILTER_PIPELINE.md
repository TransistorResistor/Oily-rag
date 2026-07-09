# Retrieval and filter pipeline explainer

This document describes the current `ragkit.py` query path and the main options
for improving embedding and cross-encoder retrieval.

## Current defaults

The defaults are centralised in `ragkit.DEFAULTS`:

| Setting | Current value | Purpose |
|---|---:|---|
| `k` | `4` | Maximum passages returned by retrieval. |
| `max_context_tokens` | `3000` | Global prompt/context budget used by `assemble_context()`. |
| `filter_mode` | `auto` | Decides whether a validated filter is a hard gate or an eligible-first fill. |
| `filter_min_count` | `2` | Hides very sparse catalogue fields from the filter prompt. |
| `filter_max_fields` | `60` | Coverage-pruning cap for the filter prompt. |
| `field_select` | `None` | Auto: use embedding-based catalogue-field selection if the DB supports it. |
| `field_select_k` | `15` | Number of query-relevant catalogue fields to show the filter model, plus always-included partition fields. |
| `min_rel` | `0.0` | No relevance floor by default. |
| `filter_model` | `mistral-small` | Bench default for filter extraction. |

The default embedder is `sentence-transformers/all-MiniLM-L6-v2`; the default
cross-encoder is `cross-encoder/ms-marco-MiniLM-L-6-v2`. `RAGKIT_DISABLE_RERANK=1`
disables the cross-encoder while retaining vector + FTS + RRF retrieval.

## Ingest-time pipeline

The ingest path is:

```text
raw JSON records
  -> record_model.normalize_record()
  -> record_model.to_text() / typed_fields() / rich_params()
  -> chunk_record()
  -> SQLite records + FTS5 + embeddings + metadata tables
```

Important generated structures:

- `records`: one row per passage/chunk, including parent record ID, title, text,
  embedding vector, and stored typed fields JSON.
- `records_fts`: FTS5 index over passage text.
- `record_params`: one row per parent record with structured fields/rich params.
- `record_relations`: normalized parent/child relationships from `relations[]`
  and relationship-like parametric rows.
- `field_embeddings`: one embedding per catalogue field descriptor, used for
  query-time filter-field selection.
- `meta.catalogue`: generated filter catalogue.
- `meta.category_stats`: per-`systemType` numeric summaries.
- `meta.aliases`: deterministic alias/code/name table for entity pinning.
- `meta.embed_model`: embedding model name and dimension used at ingest.

The important design choice is that `record_model.py` is the canonical parser.
Embedding text, catalogue fields, table rows, relation edges, and aliases all
derive from the same normalized record model.

## Filter extraction path

Filtering is optional. It runs when `auto_filter=True` or when the caller passes
an explicit filter.

```text
query
  -> choose filter-field prompt subset
  -> LLM emits JSON filter
  -> robust JSON parse / one retry on parse failure
  -> validate_filter()
  -> count matching records
  -> retrieve(... clean_filter=..., filter_mode=...)
```

### Catalogue-field selection

Single-pass filter extraction does not always show the whole catalogue. If the
DB has `field_embeddings`, `select_fields()` embeds the query and ranks catalogue
fields by semantic similarity. It always includes partition fields such as
`systemGroup`, `systemType`, and other high-coverage categoricals, then adds the
top query-relevant fields.

This is the current answer to catalogue bloat: the filter model sees a smaller,
more relevant field spec instead of a long heterogeneous schema dump.

### Filter JSON parsing

`extract_filter_ex()` asks the filter model for JSON only. It then:

1. strips code fences;
2. tries `json.loads`;
3. if that fails, extracts the first balanced `{...}` block;
4. if that still fails, retries once with the parse error;
5. surfaces `ok`, `empty`, or `parse_failed` in `filter_info`.

`json_mode=True` exists for OpenAI/OpenRouter-style `response_format`, but it is
not the default because provider behaviour needs to be verified per endpoint.

### Filter validation

`validate_filter()` is the trust boundary. The model can propose a filter, but
only the catalogue can approve it.

Validation does the following:

- drops unknown fields;
- drops malformed conditions;
- validates categorical and multi-value labels against observed catalogue values;
- normalizes/remaps close labels and records `_remapped`;
- converts numeric query units into the field’s canonical unit and records
  `_converted`;
- drops unit-incompatible numeric filters rather than comparing mismatched units.

### Filter modes

`retrieve()` resolves the filter mode:

- `hard`: only eligible rowids can be retrieved.
- `soft`: all rows can be retrieved, but eligible rows get a rank boost.
- `fill`: eligible rows are shown first, then non-matching rows top up the
  remaining slots.
- `auto`: hard-gate when the matching parent set is larger than `k`; otherwise
  use fill so narrow filters do not starve context.

## Retrieval path

The main retrieval path is hybrid:

```text
query
  -> deterministic alias/entity match
  -> vector search over cached MiniLM embeddings
  -> FTS5 keyword search
  -> Reciprocal Rank Fusion
  -> optional cross-encoder rerank
  -> optional filter boost/fill logic
  -> entity pinning / multi-entity balancing
  -> top-k context passages
```

### Vector channel

The query is embedded with the same sentence-transformer used at ingest. Passage
embeddings are loaded into an in-memory matrix by `_load_passage_cache()`, and
cosine similarity is just a matrix multiply because embeddings are normalized.

`_check_embed_model()` compares the query embedder against `meta.embed_model`.
Known mismatches warn; they should eventually become hard failures by default.

### FTS channel

The keyword channel uses SQLite FTS5. Under a hard filter, FTS candidates are
filtered before the top-`pool` cut by growing the FTS `LIMIT` until enough
eligible hits are collected or FTS is exhausted. This avoids silently degrading
to vector-only retrieval under selective filters.

### RRF fusion

Vector and FTS ranks are fused using Reciprocal Rank Fusion. This is the main
baseline that performed best on the current large synthetic corpus when used
with `k=10` and reranking disabled.

### Cross-encoder rerank

If reranking is enabled, `retrieve()` sends the top `rerank_pool` fused
candidates to `cross-encoder/ms-marco-MiniLM-L-6-v2`. The cross-encoder scores
each `(query, passage)` pair jointly and reorders that candidate pool. If loading
or inference fails, retrieval falls back to RRF order.

Sentence Transformers’ retrieve-and-rerank guidance matches this architecture:
first retrieve a larger candidate set with lexical or dense retrieval, then
rerank a smaller set with a CrossEncoder. Their docs also note the tradeoff:
cross-encoders are generally more accurate than bi-encoders, but slower because
they compute each query/document pair jointly.

Sources:

- Sentence Transformers retrieve-and-rerank: https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html
- Sentence Transformers cross-encoder usage: https://www.sbert.net/docs/cross_encoder/usage/usage.html
- Sentence Transformers pretrained models: https://www.sbert.net/docs/sentence_transformer/pretrained_models.html

### Entity pinning

`match_entities()` deterministically matches aliases, codes, titles, and other
known names before retrieval. This is necessary because embeddings are weak at
exact designations such as `AIM-120`, `S-400`, `ADX00205`, etc.

Current implementation gap: the `retrieve()` docstring says pins are guaranteed,
but the code currently skips forced injection when the pinned parent appears
anywhere in the ranked candidate list. On the large synthetic corpus this means
an exact-code match can sit at rank 5-10 and still fail a `k=4` eval. The check
should be changed from “is this parent anywhere in ranked candidates?” to “will
this parent survive final top-k assembly?”.

That is likely the highest-value retrieval fix exposed by `large_eval_set.json`.

### Multi-entity balancing

When two or more aliases are matched, retrieval caps how many passages any one
pinned entity can occupy. This prevents comparison queries from filling every
slot with only one side of the comparison.

## Context assembly path

After retrieval, `answer()` adds structured context:

1. same-record parameter passage for pinned field-specific queries;
2. one-hop related records via `record_relations`;
3. analytic table for filtered sets or pinned multi-entity comparisons;
4. digest for broad filtered result sets when no table is used;
5. global prompt assembly through `assemble_context()`.

The prompt budget is global at assembly time. Sections are water-filled rather
than independently appended without limit. The budget is still best described as
a soft target; some protected parameter/table content can exceed strict section
allocations.

## Current measured behaviour

On `large-test-corpus` with `large_eval_set.json`:

| Mode | k | Overall | Lookup | Parametric | Comparison | Analytic | Prose |
|---|---:|---:|---:|---:|---:|---:|---:|
| RRF only | 4 | 62.4% | 100% | 26.7% | 33.3% | 93.3% | 100% |
| RRF only | 10 | 91.4% | 100% | 83.3% | 88.9% | 93.3% | 100% |
| Cross-encoder | 4 | 57.0% | 100% | 26.7% | 38.9% | 53.3% | 100% |
| Cross-encoder | 10 | 71.0% | 100% | 43.3% | 61.1% | 80.0% | 100% |

Interpretation:

- The corpus is intentionally homogeneous, so `k=4` is a stress test.
- RRF-only at `k=10` is the best current baseline.
- The cross-encoder improves rank when it keeps the target, but reduces recall
  on this synthetic corpus. It appears to over-prefer semantically generic
  passage matches and push exact/structured hits out of the returned set.
- This does not prove cross-encoders are bad in general; it shows this particular
  reranker and final selection policy are not tuned for this corpus.

## Improvement options

### 1. Fix pin finalization before changing models

Make exact entity pins truly guaranteed in the final output unless a hard filter
excludes them.

Current failure mode:

```text
query: "What is the weight of MX00124?"
pin:   900124
rank:  7
k:     4
result: target not returned
```

Recommended behaviour:

- if a pin matches exactly and passes any hard filter, reserve at least one slot
  for that parent;
- choose the best passage for the pinned parent, preferably a parameter passage
  containing a query-named field;
- then fill remaining slots from RRF/rerank order.

This is cheaper and more deterministic than changing embedding models.

### 2. Separate exact-code search from semantic search

For defence-equipment corpora, exact designations are first-class keys, not just
words.

Recommended additions:

- title/code/alias exact-match table queried before vector/FTS;
- title/code-weighted FTS column instead of one flat FTS body;
- deterministic designation expansion for hyphen/no-hyphen variants:
  `MX00124`, `MX-00124`, `MX 00124`;
- never let generic suffixes like `D7` become high-power aliases.

### 3. Improve parameter passage chunking

The large corpus exposed the known I4 problem: parameter chunks can be grab-bags.
For parametric questions, the correct record can be pinned, but the best-ranked
passage may be a generic overview rather than the parameter row.

Recommended chunking:

- one short parameter chunk per component/theme;
- optionally one micro-chunk per high-value parameter family;
- include `record title + primaryEquipCode + field name + value + unit` in each
  parameter chunk;
- avoid long repeated `parameterDescr` boilerplate in every record chunk.

### 4. A/B better bi-encoders, but measure locally

Candidate families:

- `sentence-transformers/all-MiniLM-L12-v2`: same 384-dimensional output as L6,
  likely modest quality gain, slower.
- `sentence-transformers/multi-qa-MiniLM-L6-cos-v1`: retrieval-tuned for
  question/answer style semantic search.
- `sentence-transformers/multi-qa-mpnet-base-cos-v1`: stronger but heavier.
- `sentence-transformers/msmarco-MiniLM-L12-cos-v5`: passage-retrieval tuned;
  official docs show it slightly ahead of the L6 MS MARCO variant but slower.

Sentence Transformers’ docs explicitly say leaderboard performance is only a
starting point and task-specific experimentation is necessary. For this repo,
the gold sets are now good enough to run that experiment directly.

Suggested A/B command:

```powershell
$EMB = "sentence-transformers/all-MiniLM-L12-v2"
& 'C:\Users\robot\anaconda3\python.exe' ragkit.py ingest large-test-corpus --db large_l12.db --embed-model $EMB

$env:RAGKIT_DISABLE_RERANK='1'
& 'C:\Users\robot\anaconda3\python.exe' eval.py --db large_l12.db --eval-set large_eval_set.json --k 10 --json large_l12_eval_k10.json
```

Rules for interpreting results:

- compare against RRF-only `k=10`, not reranker `k=4`;
- compare class breakdown, not only overall hit-rate;
- track ingest/query latency and memory;
- never query a DB with a different embedder from the one used at ingest.

### 5. Treat the cross-encoder as an optional precision stage, not the source of recall

The current reranker takes the top `rerank_pool=30`, reranks it, and then final
selection uses that order. On the large corpus, this reduced recall.

Safer alternatives:

- keep hard-pinned exact entities outside reranker demotion;
- rerank only within buckets, e.g. pinned bucket, eligible-filter bucket,
  background bucket;
- use RRF for recall and cross-encoder only to order passages within the same
  parent or same candidate bucket;
- raise `rerank_pool` only after measuring latency;
- add a fusion formula instead of replacing order:
  `final = alpha * normalized_rrf + beta * normalized_cross_encoder + pin_bonus`;
- tune cross-encoder use by query class: useful for prose questions, risky for
  exact-code parametric retrieval unless pins are protected.

### 6. Try a stronger or domain-tuned reranker only after pinning is fixed

Candidate approaches:

- larger MS MARCO cross-encoders for quality comparisons;
- a small domain-tuned cross-encoder trained from the generated gold cases plus
  hard negatives;
- a lightweight binary classifier for “does this passage contain the requested
  field for this exact record?” for parametric lookups.

The domain-tuned option is attractive because the task is narrow:

```text
query + passage -> relevant / not relevant
```

Training data can be generated from known record IDs, parameter fields, relation
edges, and hard negatives from current retrieval misses.

### 7. Add sparse/neural lexical retrieval later

FTS5 is doing useful work and should remain. If corpus size grows or acronym
matching becomes more difficult, consider adding:

- BM25 title/body weighting;
- SPLADE-style sparse retrieval;
- OpenSearch/Elasticsearch only when SQLite FTS + in-memory vectors become the
  bottleneck.

Do not add a vector database solely for 1,000-10,000 records. Current brute-force
matrix search is simple and fast enough at this scale.

## Recommended order

1. Fix exact pin finalization so a valid exact entity pin survives top-k.
2. Group/split parameter chunks so parametric queries retrieve the right row.
3. Add title/code-weighted FTS and designation normalization.
4. Re-run large eval at `k=4` and `k=10`, RRF-only and reranker.
5. A/B `all-MiniLM-L12-v2`, `multi-qa-MiniLM-L6-cos-v1`, and one MPNet model.
6. Revisit cross-encoder strategy: bucketed rerank or score fusion.
7. Only then consider domain-tuning a reranker.

