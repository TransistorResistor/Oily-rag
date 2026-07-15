# Query-planned answer retrieval: implementation plan

*Prepared 2026-07-11. Scope: the root RAG prototype and comparison bench. The enrichment pipeline remains a separate workstream except where shared record semantics affect RAG.*

## Objective

Evolve the current single-path retrieval pipeline into an evidence-planned catalogue assistant without replacing the parts that already work: canonical records, validated filters, hybrid FTS+dense retrieval, entity pinning, relations, structured tables, and globally budgeted prompts.

The target is not an unrestricted research agent. It is a reliable bounded-domain system that:

- answers exact named-record and parameter questions from authoritative structured data;
- executes explicit filtered, comparison, and shallow relation questions deterministically;
- uses hybrid passage retrieval for explanation and facts that are not structured;
- decomposes modest compound questions into a few evidence requests;
- asks for clarification when entity, field, or subjective criteria are materially ambiguous;
- exposes the plan, evidence authority, fallbacks, and stop reason to the CLI, bench, and eval harness.

The current behavior remains the fallback throughout migration. New routes land behind configuration flags until their gates pass.

## Non-goals and practical boundary

This work will not attempt unrestricted scenario judgement, doctrinal synthesis, arbitrary graph traversal, or unsupported historical reconstruction. In the first release:

- graph traversal is one hop by default and two hops only for an explicit, typed relation plan;
- compound questions are capped at three subplans;
- subjective terms such as *best*, *modern*, or *widely used* require an explicit criterion, a corpus-relative interpretation shown to the user, or clarification;
- absence from structured fields triggers prose fallback and is never treated as proof that a fact is unknown;
- deterministic planning may narrow retrieval, but a low-confidence plan must not silently suppress the current hybrid fallback.

## Target architecture

### `QueryPlan`

A versioned, serializable plan produced before ranking:

```json
{
  "version": 1,
  "intents": ["comparison", "parameter_lookup"],
  "entities": [{"rid": "1001", "mention": "F-22", "match": "exact_alias"}],
  "fields": [{"canonical": "Maximum speed", "mention": "speed"}],
  "constraints": [],
  "relations": [],
  "temporal": null,
  "sort_or_aggregate": null,
  "prose_needed": false,
  "ambiguities": [],
  "confidence": "high",
  "fallback": "hybrid"
}
```

The plan must preserve both the user's wording and every canonical mapping. It must not contain executable SQL or unvalidated catalogue values.

### `EvidenceBundle`

All routes produce a common evidence model rather than concatenating unrelated strings:

- evidence type: `parameter`, `record_passage`, `relation`, `table_row`, `digest`, or `negative_lookup`;
- source record ID and title;
- canonical field/relation and qualifier where applicable;
- authority: structured catalogue, relation row, record prose, or background retrieval;
- retrieval/routing reason and scores;
- text/value to render;
- citation identity;
- budget priority.

### `PreparedAnswer`

A shared orchestration result used by `ragkit.answer`, `compare_server`, and `eval.py`:

```text
PreparedAnswer
  plan
  evidence_bundle
  contexts
  prompt
  system_prompt_variant
  filter_info / diagnostics
  deterministic_reply | clarification | None
```

This replaces duplicated query orchestration while leaving provider transport and final generation separate.

## Delivery phases

### Phase 0 — Freeze baselines and extend the gold contract

Purpose: make evidence quality measurable before changing behavior.

Deliverables:

1. Capture fresh offline baselines for the curated and 1,000-record corpora with:
   - RRF-only at `k=4` and `k=10`;
   - the current cross-encoder at `k=4` and `k=10`;
   - per-class hit rate, first-hit rank, prompt tokens, and latency;
   - explicit completion markers so a post-model-load process crash cannot be mistaken for a pass.
2. Extend eval cases with optional evidence expectations:
   - canonical field/value or parameter-line pattern;
   - relation edge and direction;
   - all comparison entities;
   - expected table membership and ordering;
   - authoritative evidence section/rank;
   - expected clarification or refusal.
3. Add two eval modes:
   - **oracle plan**: supplied intent/filter/fields isolate evidence execution and ranking;
   - **end-to-end plan**: the real planner must derive the same plan from the user query.
4. Add candidate diagnostics: lexical/dense channel recall, overlap, unique parent count, duplicate-slot rate, plan route, fallback reason, and evidence sufficiency.
5. Add targeted cases for ambiguous fields, compound questions, subjective language, negation, temporal scope, variants, missing structured values, relation direction, corpus-meta questions, and conversational references expressed as complete standalone queries.

Relevant backlog absorbed: TODO 10 remainder, TODO 13, TODO 20, REVIEW F2/I8/Q6/R1, and query-pathways P8.

Gate:

- Baselines finish reproducibly in the documented offline environment.
- Existing retrieval and hosted evidence files are not overwritten.
- Every new expectation is scoreable without an LLM; hosted answer scoring remains opt-in.

### Phase 1 — Extract one behavior-preserving orchestration seam

Purpose: prevent CLI/bench/eval drift before adding planner decisions.

Deliverables:

1. Introduce `prepare_answer(...) -> PreparedAnswer` and move the existing sequence into it:
   - load catalogue/aliases;
   - validate explicit or extracted filters;
   - resolve filter mode;
   - match pins and unmatched designations;
   - retrieve passages;
   - attach same-record parameters;
   - expand relations;
   - construct table/digest;
   - assemble the prompt.
2. Make `ragkit.answer`, `compare_server.build_context`, and LLM-stage eval consume this function.
3. Move deterministic unknown-designation, crowded-family disambiguation, and direct-parameter replies before embeddings and hosted filter extraction where their required metadata is available.
4. Preserve current output shapes through compatibility adapters during the migration.
5. Add parity tests asserting byte-equivalent prompts, sources, filter audits, and deterministic replies for representative existing queries.

Relevant backlog absorbed: REVIEW I2/I5, Q4, R3, and the prior review finding that deterministic shortcuts currently run after expensive retrieval.

Gate:

- With planning disabled, CLI, bench, and eval produce the same evidence/prompt for the parity corpus.
- No hosted call or embedding occurs for deterministic negative-designation, disambiguation, or exact direct-answer routes.

### Phase 2 — Implement the deterministic planner MVP

Purpose: support the three highest-value query classes while retaining hybrid fallback.

Supported routes:

1. **Exact entity + field**
   - Resolve exactly one entity and one canonical field.
   - Read structured values/variants directly.
   - Retrieve prose only when explanation, provenance, or a missing structured value requires it.
2. **Named comparison**
   - Reserve every named entity.
   - Select the same requested fields for each entity.
   - Emit a deterministic mini-table and explicit missing-value cells.
3. **Explicit analytic filter**
   - Parse class terms, numeric/date bounds, units, categorical values, sort, and limit.
   - Validate through the existing catalogue/filter trust boundary.
   - Rank and render the complete matched record set via table/digest rather than arbitrary passage top-k.

Planner rules:

- deterministic signals first: aliases/codes, field aliases, units, dates, intervals, class nouns, comparison/aggregate cues, and relation verbs;
- current LLM filter extraction only as fallback for unresolved constraints;
- conservative field ambiguity: multiple plausible canonical fields produce clarification or labelled alternatives;
- plan confidence derives from stable evidence (exact alias/field, validated value, cross-signal agreement), not per-query min-max retrieval scores;
- at most three subplans for a compound query; otherwise ask the user to narrow it.

Relevant backlog absorbed: TODO 14, REVIEW A3/A5/A7, K1/K2/K4/K6/K7, L, P2/P4/P6, Q3, Q5, query-pathways P1/P3/P4/P6, and the domain abbreviation/class/nationality hints already in `ragkit.py`.

Gate:

- All supported-route oracle cases have the required structured evidence.
- End-to-end plan cases map to the expected entity, field, constraints, and route.
- Unsupported/ambiguous cases fall back or clarify; they do not silently execute a low-confidence structured plan.
- Existing prose retrieval hit rate does not regress when the planner chooses fallback.

### Phase 3 — Make structured evidence first-class

Purpose: stop using broad parameter chunks and prose as substitutes for data already in `record_params` and `record_relations`.

Deliverables:

1. Add a parameter-evidence index keyed by `(record_id, canonical_field, qualifier/component)` containing title/designation, field, value, unit, qualifier, component, and dictionary description.
2. Preserve variant identity and return labelled alternatives rather than flattening them.
3. Add authoritative relation evidence with parent, child, relation type, component, direction, and both IDs.
4. Add deterministic table sorting for numeric rankings, intervals, and multi-variant rows; record the sort policy in the plan.
5. Assign evidence authority and budget priority so named-record parameters and explicit relation rows precede background passages.
6. Re-ingest into disposable/new DBs and retain embed/index provenance.

Relevant backlog absorbed: TODO 18, REVIEW I3/I4, J5 prerequisite only (not PDF ingestion itself), L, P2/P3, Q4, and retrieval-pipeline parameter chunking.

Gate:

- Exact parameter cases always include the requested labelled field/value.
- Variant, interval, unit, and qualifier fixtures remain distinguishable.
- Relation answers cite the authoritative edge rather than inferring compatibility from prose proximity.
- Re-ingest preserves the established data-safety and provenance gates.

### Phase 4 — Improve semantic fallback retrieval

Purpose: improve explanation/prose recall after structured routes are protected.

Deliverables, each independently switchable and A/B tested:

1. Fielded FTS5 columns for title/designation, parameter name, relation text, and body, with explicit BM25 weights.
2. Query-term construction that preserves designations/domain abbreviations but removes or down-weights low-information question words.
3. Designation normalization for hyphen/space/compact variants.
4. Recall-safe reranking:
   - pins and hard-filter evidence protected;
   - rerank within evidence buckets;
   - compare score fusion with full order replacement;
   - enable by query class only when its class gate improves.
5. Experimental record-first/passages-second retrieval behind a flag. Adopt only if evidence sufficiency or record recall improves over the measured candidate-diversity baseline.
6. Embedder A/B only after the lexical/reranking policy is stable; every model gets its own new DB and provenance-checked eval.

Relevant backlog absorbed: TODO 15-17, REVIEW D4/G7/G8/I6/I7/P7, and `RETRIEVAL_FILTER_PIPELINE.md` options 2-7. Multi-query/HyDE remains deferred until cheaper deterministic and lexical routes are exhausted.

Gate:

- No reduction in exact, parametric, comparison, or hard-filter recall.
- A fallback change ships only if it improves its intended class on held-out cases with acceptable latency/memory.
- Cross-encoder policy must beat or justify its cost against RRF-only; model reputation is not evidence.

### Phase 5 — Confidence-driven context and hard budget semantics

Purpose: assemble the smallest sufficient evidence set without presenting relative scores as calibrated confidence.

Deliverables:

1. Replace fixed-depth behavior with policy based on plan/evidence signals:
   - exact lookup: one named record plus authoritative field and optional supporting prose;
   - comparison: all named entities plus common requested fields;
   - analytic: structured table/digest;
   - uncertain prose: widen candidate depth within budget.
2. Record why retrieval stopped: exact evidence complete, all comparison entities covered, table complete, confidence margin, budget exhausted, or fallback limit.
3. Decide TODO 8 explicitly:
   - preferably enforce a hard final prompt cap including headers/question/system overhead;
   - otherwise rename the option and expose measured overage.
4. Preserve parameter/relation evidence first, then prose, then background; never preserve every parameter line unboundedly.
5. Add contradiction/missing-evidence markers so generation cannot mistake background context for proof.

Relevant backlog absorbed: TODO 8/19, REVIEW B1/G3/I6/P5/Q4, and UI honest-state requirements.

Gate:

- Adversarial tiny-budget tests obey the documented contract.
- Every supported route satisfies its evidence requirements or returns an explicit insufficiency/clarification state.
- Prompt-size, latency, and evidence-count distributions are reported per route.

### Phase 6 — Surface the plan and failure states in the product

Purpose: make deterministic assumptions editable and failures visible.

Deliverables:

1. Bench/API envelope includes plan version, intent, entities, fields, constraints, route, confidence signals, ambiguity, fallbacks, and stop reason.
2. Render extraction failure, dropped/remapped clauses, degenerate filters, disambiguation, missing structured fields, and relation paths even when no filter was applied.
3. Prevent model fan-out when the plan requires clarification or deterministic disambiguation.
4. Add editable filter/plan chips for user correction; rerun retrieval without another planner call where possible.
5. Add request validation and spend controls: unique model IDs, query/body limits, cancellation feedback, and authentication/rate limiting when bound beyond loopback.
6. Consolidate model configuration and expose defaults through one API contract.

Relevant backlog absorbed: TODO 11, REVIEW A5/E1/E2/F1/I2/P5, `UI_NOTES.md` Q&A/filter/honest-state sections, and prior functionality-review UI/API findings.

Gate:

- A user can see and correct every material entity/field/filter interpretation.
- Clarification states make zero answer-model calls.
- CLI and bench display equivalent plan/evidence diagnostics.

### Phase 7 — Stabilization and final functionality review

Purpose: verify the product as a whole after the behavior-changing work, then decide whether to extend scope.

Pre-review release candidate gates:

1. All deterministic standalone tests pass.
2. Curated and large-corpus offline gates complete with no unexplained regression against Phase 0.
3. Oracle-plan versus end-to-end-plan deltas identify planning failures separately from evidence/ranking failures.
4. Targeted hosted gates complete from checkpointed evidence; no score is reported for an incomplete/network-blocked run.
5. Data-safety, DB provenance, cache replacement, missing-DB, and prompt-budget tests pass.
6. Manual bench scenarios cover exact lookup, ambiguous field, comparison, analytic filter, missing value with prose fallback, relation, negative designation, compound query, and clarification.
7. Performance report includes cold/warm latency, memory, prompt tokens, hosted calls avoided, and plan-route distribution.

The final functionality review should assess:

- correctness and answer evidence, not only record retrieval;
- user-question coverage and clarification burden;
- plan transparency and correction workflow;
- contradictions, variants, temporal/negation behavior, and missing data;
- CLI/bench/eval parity;
- security/spend controls;
- maintainability of rules versus the fallback model;
- whether two-hop relations, richer compound decomposition, PDF media indexing, corpus-meta queries, or a trained planner/reranker are justified by observed failures.

Exit decision:

- **Extend** only where review failures cluster into a tractable query class.
- **Hold** when clarification/fallback is honest and adequate.
- **Do not add rules** for isolated questions that would weaken established routes.

## Backlog reconciliation

### Included in this plan

- TODO 8, 10 remainder, 11, 13-20.
- REVIEW filtering/query semantics still relevant to planning: A3/A5/A6/A7, K1/K2/K4/K6/K7, L, P4/Q3.
- Retrieval/context work: B1, D4, G3/G7/G8, I2/I4/I5/I6/I8, P2/P3/P7, Q4/Q6.
- UI transparency and honest failure states from `UI_NOTES.md`.

### Already implemented and treated as invariants

- canonical record model and rich parameter variants;
- unit canonicalization and interval overlap semantics;
- catalogue field selection and category statistics;
- entity/code/curated-alias pinning and final pin reservation;
- same-record parameter attachment and multi-entity table support;
- one-hop related-record expansion;
- unmatched-designation and crowded-family guards;
- staged atomic ingest, read-only query connections, embed-model provenance, and cache rollover.

These should receive regression coverage, not be reimplemented.

### Deferred until the final review supplies evidence

- HyDE or LLM multi-query expansion;
- domain-trained embeddings/rerankers/planner;
- SPLADE/OpenSearch/vector database migration;
- unrestricted two-plus-hop graph reasoning;
- PDF media fetching/indexing;
- new-record enrichment proposals and curator-priority features;
- conversational pronoun/reference state (the first planner release accepts standalone questions; the client may rewrite follow-ups).

### Separate open workstream

Enrichment extraction identity (TODO 5) and other enrichment lifecycle improvements remain valid but are not dependencies of this RAG plan. They should not be bundled into retrieval changes or allowed to overwrite stable enrichment evidence during evaluation.

## Recommended implementation packages

Keep changes reviewable and revertible:

1. **Measurement package:** Phase 0 only.
2. **Orchestration package:** Phase 1, behavior-preserving.
3. **Planner MVP package:** Phase 2 with flags and fallback.
4. **Structured evidence package:** Phase 3 plus disposable re-ingest.
5. **Fallback retrieval experiments:** Phase 4, one A/B lever per commit/package.
6. **Budget/confidence package:** Phase 5.
7. **Product transparency package:** Phase 6.
8. **Release candidate and functionality review:** Phase 7; no feature work mixed into the review baseline.

Do not tune embeddings, reranking, FTS weighting, planner rules, and prompt wording in one package: the eval would not reveal which change caused an improvement or regression.

## Implementation checkpoint — 2026-07-11 correctness package

Implemented and offline-verified:

- versioned query-plan diagnostics with `complete`, `unresolved_constraints`, ambiguity, route, confidence, and stop reason;
- currency symbols and thousand/million/billion numeric scales;
- deterministic class and nationality hints, with duplicate generic `Type` suppression;
- partial deterministic filters merge with model extraction when enabled and otherwise do not narrow hybrid fallback;
- complete deterministic analytic filters use a hard eligible set rather than padding with unrelated `fill` contexts;
- exact structured answers, unknown designations, subjective-criterion clarification, and crowded-family clarification run before vector provenance/retrieval;
- shared `ragkit.prepare_answer()` used by the bench and hosted eval path;
- bench model fan-out is skipped for deterministic/clarification replies;
- plan diagnostics and unresolved/ambiguity states are rendered in the bench;
- request body/query/model-list spend guards;
- hosted eval consumes production prepared evidence and supports optional `expected_evidence` scoring.

Verified probes include `$5 million` (one correct sensor match, no unrelated padding), Russian missiles over a numeric range, exact parameter lookup with an intentionally mismatched embedding-model provenance record, and subjective *most modern* clarification with zero retrieval/model calls.

Still open from the full plan: dedicated `EvidenceBundle` types and parameter index (Phase 3), fielded FTS and guarded reranking experiments (Phase 4), hard prompt-cap semantics (Phase 5), editable plan chips/authentication beyond basic request guards (Phase 6), and the release-candidate functionality review (Phase 7).
