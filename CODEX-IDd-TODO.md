# Codex-identified TODOs

Reviewed 2026-07-05 against the current working tree. This is a live issue list, not a repeat of historical items already marked fixed in `REVIEW_FINDINGS.md` or `enrich_demo/FINDINGS*.md`.

## P0 — correctness or data-loss hazards

### 1. Make RAG ingest staged and atomic

**Fixed 2026-07-09:** `ragkit.ingest()` now validates/normalizes source records before opening the target, builds into a same-directory temp SQLite file, sanity-checks it, then atomically replaces the target and cleans temp files on failure.

`ragkit.ingest()` drops `records`, FTS, params, field embeddings, and relations before it loads/validates the source, and `init_db()` commits that empty replacement. An empty/mistyped source therefore erases a usable index; an embedding/model failure leaves the old index gone as well (`ragkit.py:399-415`).

- Validate and normalize all source records before touching the target.
- Build into a temporary DB, complete all embeddings/catalogue/meta writes, run sanity checks, then atomically replace the target.
- Add regression tests for an empty source and an injected embedding exception preserving the original DB.

### 2. Canonicalize per-record values, not only catalogue statistics

**Already fixed (verified 2026-07-10):** `ragkit._canonicalize_stored_fields()` converts every stored numeric value into the catalogue's canonical unit at ingest (after `build_catalogue` picks it), and `validate_filter` converts filter bounds into the same unit with an `_converted` audit trail; `test_rag_effectiveness_fixes.py::test_interval_catalogue_and_unit_canonicalization` covers the mixed `m`/`mm` Diameter case. This item was stale when re-checked before dispatch.

`catalogue._reconcile_units()` converts its temporary aggregate value list into the majority unit, but `ragkit.ingest()` has already written unconverted `record_model.typed_fields()` values to `record_params.fields_json`. Filters compare those stored numbers as if they used the catalogue unit. The curated corpus already triggers this: `Diameter` has metres for six records and millimetres for AIM-9X/Derby/Python-5, so a catalogue unit of `m` coexists with stored values `127`/`160` that are later treated as metres (`catalogue.py:117-207`, `ragkit.py:449-457`).

- Choose a canonical unit per field first, then convert every stored numeric value to it.
- Preserve unit metadata per value/variant; duplicate-name variants may legitimately carry different units.
- Test a filter such as `Diameter <= 1 m` against mixed `m`/`mm` records.

### 3. Do not mark failed claim extraction as processed

**Fixed 2026-07-09:** extraction parser/transport failures now append to `doc_failures`, increment run errors, skip `docs_seen`, and remain retryable until a clean extraction succeeds.

`enrich_demo.llm.extract_claims()` returns `([], usage, raw, "unparseable JSON")` or `"no claims array"`. `pipeline.run_batch()` still calls `process_document()`, `record_doc()`, and appends the document to `processed`; future runs hash-skip it forever and `error_count` stays zero (`enrich_demo/llm.py:98-116`, `enrich_demo/pipeline.py:845-895`).

- Treat a non-null extraction error as failed/unprocessed and leave it retryable.
- Persist the raw response/error in a failure ledger for diagnosis without adding `docs_seen`.
- Add tests for malformed JSON, missing `claims`, transport failure, and a clean retry.

### 4. Stop scratch enrichment runs from overwriting stable evidence

**Fixed 2026-07-09:** `report.build()` keeps legacy `proposals.json`/`report_runN.md` only for the default `enrich_state` DB and writes only DB-scoped proposal/report names for non-default state DBs.

`report.build()` always writes shared `enrich_demo/proposals.json` and `report_runN.md`, even for a non-default state DB; only the extra proposals filename is DB-scoped. A normal throwaway run can therefore overwrite the tracked demo outputs, and two DBs with the same run number collide (`enrich_demo/report.py:62-79, 207-210`).

- For non-default DBs, write only DB-scoped proposal and report names (or a caller-supplied output directory).
- Reserve legacy names for the default demo DB or an explicit compatibility flag.
- Add a no-clobber test using two state DBs whose latest run is `1`.

## P1 — reliability and reproducibility

### 5. Include extraction configuration in enrichment identity

The document key is only `render + extracted_text`. Changing model, prompt/schema, parser version, or deterministic classification code does not cause reprocessing, despite `docs_seen` recording `llm_model` (`enrich_demo/provider.py:112-134`, `enrich_demo/state.py:126-143`). Add an extraction/pipeline version (and probably model) to the processing identity, while retaining a separate content hash for source deduplication.

### 6. Fail fast on embedding-model mismatch

**Fixed 2026-07-10:** query-time retrieval now errors on known embed-model mismatches by default, allows an explicit unsafe warning-only override, always rejects dimension mismatches, and warns once for provenance-less legacy DBs.

Ingest stores embedding model/dimension, but `_check_embed_model()` only prints a warning and retrieval proceeds with meaningless cross-model cosine scores; old DBs such as the current `rag_test.db` have no provenance key at all (`ragkit.py:570-590, 647-670, 1493`). Make a known mismatch an error by default, offer an explicit unsafe override, validate vector dimensions, and provide a migration/re-ingest diagnostic for provenance-less DBs.

### 7. Invalidate all live-server metadata caches on DB replacement

**Fixed 2026-07-10:** `compare_server` now keys its live SQLite connection, catalogue cache, and alias cache by `(absolute db path, mtime)`, reopens on replacement, and coordinates with `ragkit.invalidate_caches()`.

Passage/field caches use DB mtime, but `compare_server` caches catalogue and aliases forever by path. Re-ingesting/replacing a DB while the server runs refreshes passages but can retain stale filter specs and entity pins (`compare_server.py:104-144`). Use the same version/mtime key for every cache or expose one coordinated invalidation hook.

### 8. Make the token cap a real cap or rename it as soft

The budgeter can exceed a section allocation: `_truncate_to_tokens()` always preserves every parameter line, and table/digest renderers always keep at least one row/entry even when it cannot fit. Section headers/separators/question text are also outside the estimated variable-content budget (`ragkit.py:2147-2558`). Either enforce a hard final prompt cap with explicit truncation priorities or document/measure the guaranteed overhead and call the setting a soft target. Add adversarial tests with oversized parameter blocks and tiny caps.

### 9. Fix majority thresholds for odd sample counts

**Already fixed (verified 2026-07-09):** `catalogue.py` now uses `max(1, math.ceil(MAJORITY_MIN * n))` at both classification sites; this item was stale when re-checked.

Numeric/date classification uses `int(MAJORITY_MIN * n)`, which floors the requirement: 1/3, 2/5, or 3/7 typed records can qualify despite `MAJORITY_MIN = 0.5` (`catalogue.py:234,274`). Use `math.ceil()` (or a direct ratio comparison) and add boundary tests. The current curated corpus did not expose a below-50% classified field, so this is latent rather than a known output error.

### 10. Add a reproducible dependency/test environment

**Partially addressed 2026-07-05:** `requirements-demo.txt` pins the working demo packages; `huggingface_models.json`, `prepare_offline.py`, and `offline_env.ps1` define a portable pinned model cache; `RAGKIT_DISABLE_RERANK` now lets the server/eval retrieval path fall back to vector+FTS/RRF. Remaining: provide a platform-specific lock/environment export (especially for PyTorch), add a smoke test that emits an unmistakable completion line, and expose reranking as a first-class eval CLI flag rather than only an environment switch.

## P2 — maintainability and interface gaps

### 11. Finish consolidating model configuration

`ragkit` derives OpenRouter aliases from `models_registry.py`, but `enrich_demo/llm.py` still duplicates a subset. Derive both from the registry. Also expose `--filter-model` and `--host` consistently from the standalone `compare_server.py` CLI; `run_server()` supports both, but `main()` does not pass either (`compare_server.py:510-600`).

### 12. Avoid creating an empty DB on a read/query typo

**Fixed 2026-07-09:** query-only RAG entry points now use `connect_readonly()` with an existence/schema check and fail with a one-line "DB not found or not ingested" message before SQLite can create a file.

`ragkit.connect()` uses ordinary SQLite open semantics, so `ask`/eval against a misspelled path creates a new empty DB and fails later with a schema error. Separate read-only/query connections from ingest connections and report “DB not found or not ingested” before SQLite creates anything (`ragkit.py:294-297`).

### 13. Add focused automated coverage around newly complex seams

Current checks are standalone scripts and gold evaluators. Add deterministic tests for canonical adapters, mixed units, filter validation, cache invalidation, atomic ingest, malformed LLM output, report path isolation, and hard prompt budgeting. Keep hosted-LLM quality gates opt-in and never substitute fabricated results when network access is unavailable.

## Retrieval-specific improvement queue (reviewed 2026-07-11)

These items turn the deeper answer-retrieval review into measurable work. They complement `REVIEW_FINDINGS.md` and `RETRIEVAL_FILTER_PIPELINE.md`; where those files contain a design idea, this list states the implementation and gate still required.

The consolidated delivery sequence, backlog reconciliation, and final functionality-review gate are in `QUERY_PLANNER_IMPLEMENTATION_PLAN.md`. Items 14-20 remain the issue-level acceptance targets; the implementation plan defines their order and packaging.

### 14. Introduce an intent-aware retrieval plan before ranking

**Partially implemented 2026-07-11:** planner v1, complete/partial constraint semantics, deterministic class/nationality/currency parsing, early exact/clarification routes, and shared bench/eval preparation are shipped in the working tree. Remaining route expansion and gates are tracked in `QUERY_PLANNER_IMPLEMENTATION_PLAN.md`.

The current path sends exact lookups, parametric lookups, comparisons, filtered analytics, relation questions, and prose questions through the same passage-level vector + FTS + RRF candidate path, then repairs some cases afterward with pins, parameter attachment, relations, or a table. Build a small deterministic query plan from alias matches, field-name intents, numeric constraints, class nouns, comparison/analytic cues, and relation cues. The plan should select the appropriate evidence route:

- exact entity + exact field: read the named record's structured value first, with passage retrieval only for supporting prose;
- analytic/numeric query: resolve class and numeric constraints into a validated filter, then rank records inside that eligible set and build the structured table;
- comparison: reserve evidence for every named entity and select the same requested fields for each;
- prose/explanation: use hybrid passage retrieval and reranking;
- relation query: retrieve authoritative relation rows before one-hop prose expansion.

Keep the LLM filter extractor as a fallback for genuinely ambiguous wording, not the mandatory planner for constraints that can be parsed deterministically. Add plan diagnostics to `filter_info` so the UI/eval can show which route was selected.

### 15. Make reranking recall-safe and query-class aware

The captured large-corpus baseline shows RRF-only at `k=10` reaching 91.4% retrieval hit-rate while the current cross-encoder policy reaches 71.0%. The reranker currently replaces the RRF order for its candidate pool. Change it to a guarded precision stage:

- protect exact pins and hard-filter matches from demotion out of the final set;
- rerank within buckets (pinned, eligible, relation, background) rather than across unlike evidence types;
- compare weighted RRF/cross-encoder score fusion against full order replacement;
- enable reranking by query class only where it improves the class gate (likely prose; not automatically exact-code/parametric);
- calibrate thresholds on held-out cases rather than min-max normalizing every query to an artificial 0..1 range.

Gate RRF-only, order-replacement, bucketed, and fused policies at both `k=4` and `k=10`, reporting recall, first-hit rank, latency, and context sufficiency per class.

### 16. Add fielded lexical retrieval and query-term weighting

FTS currently indexes one flat passage-text column and ORs every query token, including low-information question words. Rebuild FTS with separately weighted title/designation, parameter-name, relation, and body columns. Normalize designation variants (`MX00124`, `MX-00124`, `MX 00124`) before search and retain exact alias/code lookup as the highest-authority lexical signal. Strip or strongly down-weight question stopwords while preserving domain abbreviations and quoted/exact phrases.

Measure lexical-only recall, dense-only recall, channel overlap, and fused recall. Do not replace FTS5 with a search service until these fielded SQLite improvements are measured and corpus scale actually requires it.

### 17. Retrieve records first, then passages within selected records

The first-stage candidate pools are passage-level but final diversity is record-level (`max_per_parent`). This can waste candidate slots when several chunks from one record rank highly, and it makes record recall depend on chunk count. A 2026-07-11 probe on the synthetic 1,000-record corpus found the problem is currently modest rather than catastrophic: top-20 vector results averaged 17.8-19.7 unique parents by class, top-20 FTS results 18.8-19.8, and the fused top-30 26.8-29.0. Preserve those measurements as a baseline.

Prototype hierarchical retrieval only behind an A/B flag:

1. aggregate passage signals to a record score (max plus a small multi-passage support bonus);
2. select/diversify top records;
3. choose the best evidence passage(s) inside each selected record, preferring a requested field-bearing parameter passage when applicable.

Adopt it only if it improves record recall or context sufficiency without harming exact pins. Add candidate-pool diagnostics (`unique_parents`, duplicate-slot rate, channel overlap) to eval so future chunk-count growth cannot silently create a pool-monopoly problem.

### 18. Index structured parameter evidence directly

Parameter chunks are still broad bundles split by character count. Add a compact parameter-evidence index keyed by `(record_id, canonical_field, qualifier/component)` with searchable text containing title/designation, field, value, unit, qualifier, and component. For a field-intent query, retrieve from this index or address it directly instead of hoping the correct line wins whole-chunk similarity. Keep prose passages for explanation and provenance.

Test exact fields, aliases such as `range` -> `Maximum range`, qualified variants, interval values, multi-unit values, and records with many parameters. The answer-stage gate must assert that the requested field/value line is present and placed before unrelated background, not merely that the correct record ID appears somewhere.

### 19. Replace fixed top-k and relative `min_rel` with confidence-driven packing

`k` is a fixed maximum and `min_rel=0` keeps the full tail by default. The available relevance floor is based on per-query min-max scores, so every query has an apparent `1.0` best result even when all candidates are weak, and mixed RRF/cross-encoder scores are not globally calibrated. Define confidence using stable signals: exact pin, validated hard filter, lexical phrase/designation hit, cross-channel agreement, score margin, and calibrated reranker probability where available.

Use those signals to choose retrieval depth and context composition: narrow exact lookups can return one authoritative record plus its parameter evidence; uncertain prose queries can widen the candidate pool/output; broad analytic queries should use a table/digest rather than more arbitrary passages. Record the stop reason in diagnostics.

### 20. Measure evidence sufficiency, not only record hit-rate

**Partially implemented 2026-07-11:** hosted eval now uses production prepared evidence and supports optional `expected_evidence` fields/values/record IDs. Remaining: populate the gold corpora broadly and add oracle-plan versus end-to-end-plan aggregate reporting.

The current offline gate can pass when the correct record is retrieved but the requested parameter, relation, or comparison evidence is absent or ordered behind distracting records. Extend the gold schema and scorer with evidence requirements:

- expected canonical field/value or parameter-line pattern;
- expected relation edge and direction;
- required coverage of every comparison entity;
- expected structured-table membership for analytic filters;
- maximum acceptable rank/section for authoritative named-record evidence;
- negative evidence checks for false-premise queries.

Run both an oracle-plan gate (known correct filter/intent, isolates retrieval mechanics) and an end-to-end plan gate (actual deterministic/LLM query understanding). This separates failures in query planning from failures in ranking and context assembly.
