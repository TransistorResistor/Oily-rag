# Codex-identified TODOs

Reviewed 2026-07-05 against the current working tree. This is a live issue list, not a repeat of historical items already marked fixed in `REVIEW_FINDINGS.md` or `enrich_demo/FINDINGS*.md`.

## P0 — correctness or data-loss hazards

### 1. Make RAG ingest staged and atomic

`ragkit.ingest()` drops `records`, FTS, params, field embeddings, and relations before it loads/validates the source, and `init_db()` commits that empty replacement. An empty/mistyped source therefore erases a usable index; an embedding/model failure leaves the old index gone as well (`ragkit.py:399-415`).

- Validate and normalize all source records before touching the target.
- Build into a temporary DB, complete all embeddings/catalogue/meta writes, run sanity checks, then atomically replace the target.
- Add regression tests for an empty source and an injected embedding exception preserving the original DB.

### 2. Canonicalize per-record values, not only catalogue statistics

`catalogue._reconcile_units()` converts its temporary aggregate value list into the majority unit, but `ragkit.ingest()` has already written unconverted `record_model.typed_fields()` values to `record_params.fields_json`. Filters compare those stored numbers as if they used the catalogue unit. The curated corpus already triggers this: `Diameter` has metres for six records and millimetres for AIM-9X/Derby/Python-5, so a catalogue unit of `m` coexists with stored values `127`/`160` that are later treated as metres (`catalogue.py:117-207`, `ragkit.py:449-457`).

- Choose a canonical unit per field first, then convert every stored numeric value to it.
- Preserve unit metadata per value/variant; duplicate-name variants may legitimately carry different units.
- Test a filter such as `Diameter <= 1 m` against mixed `m`/`mm` records.

### 3. Do not mark failed claim extraction as processed

`enrich_demo.llm.extract_claims()` returns `([], usage, raw, "unparseable JSON")` or `"no claims array"`. `pipeline.run_batch()` still calls `process_document()`, `record_doc()`, and appends the document to `processed`; future runs hash-skip it forever and `error_count` stays zero (`enrich_demo/llm.py:98-116`, `enrich_demo/pipeline.py:845-895`).

- Treat a non-null extraction error as failed/unprocessed and leave it retryable.
- Persist the raw response/error in a failure ledger for diagnosis without adding `docs_seen`.
- Add tests for malformed JSON, missing `claims`, transport failure, and a clean retry.

### 4. Stop scratch enrichment runs from overwriting stable evidence

`report.build()` always writes shared `enrich_demo/proposals.json` and `report_runN.md`, even for a non-default state DB; only the extra proposals filename is DB-scoped. A normal throwaway run can therefore overwrite the tracked demo outputs, and two DBs with the same run number collide (`enrich_demo/report.py:62-79, 207-210`).

- For non-default DBs, write only DB-scoped proposal and report names (or a caller-supplied output directory).
- Reserve legacy names for the default demo DB or an explicit compatibility flag.
- Add a no-clobber test using two state DBs whose latest run is `1`.

## P1 — reliability and reproducibility

### 5. Include extraction configuration in enrichment identity

The document key is only `render + extracted_text`. Changing model, prompt/schema, parser version, or deterministic classification code does not cause reprocessing, despite `docs_seen` recording `llm_model` (`enrich_demo/provider.py:112-134`, `enrich_demo/state.py:126-143`). Add an extraction/pipeline version (and probably model) to the processing identity, while retaining a separate content hash for source deduplication.

### 6. Fail fast on embedding-model mismatch

Ingest stores embedding model/dimension, but `_check_embed_model()` only prints a warning and retrieval proceeds with meaningless cross-model cosine scores; old DBs such as the current `rag_test.db` have no provenance key at all (`ragkit.py:570-590, 647-670, 1493`). Make a known mismatch an error by default, offer an explicit unsafe override, validate vector dimensions, and provide a migration/re-ingest diagnostic for provenance-less DBs.

### 7. Invalidate all live-server metadata caches on DB replacement

Passage/field caches use DB mtime, but `compare_server` caches catalogue and aliases forever by path. Re-ingesting/replacing a DB while the server runs refreshes passages but can retain stale filter specs and entity pins (`compare_server.py:104-144`). Use the same version/mtime key for every cache or expose one coordinated invalidation hook.

### 8. Make the token cap a real cap or rename it as soft

The budgeter can exceed a section allocation: `_truncate_to_tokens()` always preserves every parameter line, and table/digest renderers always keep at least one row/entry even when it cannot fit. Section headers/separators/question text are also outside the estimated variable-content budget (`ragkit.py:2147-2558`). Either enforce a hard final prompt cap with explicit truncation priorities or document/measure the guaranteed overhead and call the setting a soft target. Add adversarial tests with oversized parameter blocks and tiny caps.

### 9. Fix majority thresholds for odd sample counts

Numeric/date classification uses `int(MAJORITY_MIN * n)`, which floors the requirement: 1/3, 2/5, or 3/7 typed records can qualify despite `MAJORITY_MIN = 0.5` (`catalogue.py:234,274`). Use `math.ceil()` (or a direct ratio comparison) and add boundary tests. The current curated corpus did not expose a below-50% classified field, so this is latent rather than a known output error.

### 10. Add a reproducible dependency/test environment

**Partially addressed 2026-07-05:** `requirements-demo.txt` pins the working demo packages; `huggingface_models.json`, `prepare_offline.py`, and `offline_env.ps1` define a portable pinned model cache; `RAGKIT_DISABLE_RERANK` now lets the server/eval retrieval path fall back to vector+FTS/RRF. Remaining: provide a platform-specific lock/environment export (especially for PyTorch), add a smoke test that emits an unmistakable completion line, and expose reranking as a first-class eval CLI flag rather than only an environment switch.

## P2 — maintainability and interface gaps

### 11. Finish consolidating model configuration

`ragkit` derives OpenRouter aliases from `models_registry.py`, but `enrich_demo/llm.py` still duplicates a subset. Derive both from the registry. Also expose `--filter-model` and `--host` consistently from the standalone `compare_server.py` CLI; `run_server()` supports both, but `main()` does not pass either (`compare_server.py:510-600`).

### 12. Avoid creating an empty DB on a read/query typo

`ragkit.connect()` uses ordinary SQLite open semantics, so `ask`/eval against a misspelled path creates a new empty DB and fails later with a schema error. Separate read-only/query connections from ingest connections and report “DB not found or not ingested” before SQLite creates anything (`ragkit.py:294-297`).

### 13. Add focused automated coverage around newly complex seams

Current checks are standalone scripts and gold evaluators. Add deterministic tests for canonical adapters, mixed units, filter validation, cache invalidation, atomic ingest, malformed LLM output, report path isolation, and hard prompt budgeting. Keep hosted-LLM quality gates opt-in and never substitute fabricated results when network access is unavailable.
