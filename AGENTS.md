# AGENTS.md

Instructions for coding agents (Codex, etc.) working in this repository. See `CLAUDE.md` for the Claude-Code-specific version of this same guidance — keep the two in sync if either changes.

## What this repo is

Two related, independently runnable systems sharing a record corpus and hosted-LLM plumbing:

1. **Root: a single-file RAG prototype** (`ragkit.py` + satellites) — ingests defence-equipment JSON records into SQLite (FTS5 + MiniLM embeddings), answers questions via hybrid retrieval + an LLM, with a Flask bench (`compare_server.py`) for side-by-side model comparison.
2. **`enrich_demo/`: a reverse-enrichment pipeline** — reads noisy PDFs, extracts neutral factual claims with one cheap LLM call per document, and via entirely deterministic code proposes gap-fills/conflicts/relation-edges against the same catalogue. Report-only: never LLM-drafted prose, never a new record.

Both load records via `record_model.normalize_record` from **`test_records/*.json`** (25 curated air-defence records). **`rag_test.db` on disk is a stale, unrelated 63-record Wikipedia corpus — never treat it as ground truth for schema or content**, though it's fine as a bigger corpus for retrieval-scale testing.

## Environment

- Python: the shared Anaconda interpreter at `C:\Users\robot\anaconda3\python.exe` (not on `PATH`). Invoke by full path; run from the repo root (a local `catalogue.py` shadows the PyPI package if run elsewhere).
- `OPENROUTER_API_KEY` lives in `key.env` (gitignored) at the repo root. Set `KMP_DUPLICATE_LIB_OK=TRUE`, `OMP_NUM_THREADS=1`, `TOKENIZERS_PARALLELISM=false` before loading the MiniLM embedder (works around an intermittent Windows/conda OpenMP segfault).
- In a network-restricted sandbox, also set `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`. The installed Hugging Face stack otherwise attempts a metadata `HEAD` request even when model weights are cached. Treat a process that stops after the two `Loading weights` bars without printing the script/report completion lines as a failed/crashed gate, not a pass.
- `prepare_offline.py` materializes the pinned embedder/reranker revisions from `huggingface_models.json` into the gitignored portable `.hf-cache`; `offline_env.ps1` selects that cache and blocks network lookups. `demo.ps1 rag -Offline -NoReranker` is the lowest-risk isolated-sandbox smoke path. See `USER_GUIDE.md` before changing this contract.
- `units.py` (repo root) wraps `pint`. If its import chain breaks again (historically: a stale `dask`/`xarray` install incompatible with numpy 2.0), `units._ureg` becomes `None` and every `convert()` raises `ConversionError` instead of crashing — check `python -c "import units; print(units._ureg is None)"` before assuming conversion is broken vs. just unavailable.
- **No pytest.** `enrich_demo/test_fixes.py` and `test_i3_context.py` are plain scripts; each runs all its `test_*` functions unconditionally when executed directly.

## If you cannot reach the network

Some sandboxes running this repo's agent have **no outbound network access** (OpenRouter calls fail with a socket error). If a hosted-LLM eval gate or `enrich.py run` needs a live API call and the sandbox blocks it: **say so explicitly (e.g. report BLOCKED) rather than fabricating a result.** Do not report gate scores (precision/recall/FP counts) you were not actually able to compute. This has come up before and the correct behavior was to leave the affected documents unprocessed for a retry, not guess.

## Common commands

```bash
PY="/c/Users/robot/anaconda3/python.exe"

# RAG prototype
"$PY" ragkit.py ingest test_records --db rag.db
"$PY" ragkit.py ask "What is the range of the S-400?" --db rag_test.db --backend openrouter --model gemma4
"$PY" compare_server.py --db rag_test.db
"$PY" ragkit.py eval --db rag_test.db --eval-set eval_set.json    # offline retrieval hit-rate; add --backend for LLM-stage checks

# enrich_demo (run from enrich_demo/)
"$PY" enrich.py run --only <doc_id,...> --note batch1
"$PY" evaluate.py                 # score proposals.json vs gold.json
"$PY" enrich.py status
"$PY" enrich.py --db myrun.db run --render md --folder testdocs2b --note md   # non-default --db/--render write proposals_<dbstem>.json

# Tests
"$PY" enrich_demo/test_fixes.py
"$PY" test_i3_context.py

# User demos
.\demo.ps1 rag
.\demo.ps1 enrichment
```

## Architecture

**`ragkit.py`** (~3400 lines) is a deliberate monolith covering ingest, retrieval, filter extraction/validation, context assembly, prompt building, and generation, organized as sequential stages rather than classes. Satellites: `record_model.py` (the single canonical parser for both supported record JSON shapes — everything else derives a thin view from its output, never re-parses raw records), `catalogue.py` (builds the filter vocabulary from `record_model.typed_fields()` output), `units.py` (pint wrapper for filter unit conversion), `llm_provider.py` (**the only place the OpenRouter transport lives** — `chat()`/`chat_with_usage()`; prompts themselves live at call sites like `ragkit.build_prompt` and `enrich_demo/llm.py`, not in this adapter), `models_registry.py` (model lineup for the bench). `compare_server.py` imports `ragkit` unchanged and adds a multi-model side-by-side UI. `eval.py` is the offline harness (shared arg parser with `ragkit.py eval`): retrieval hit-rate always runs; LLM-dependent stages are opt-in via `--backend` since they cost real calls. Config is unified in `ragkit.DEFAULTS` so the CLI and the bench can't silently drift.

A query's path through `ragkit.answer`: optional LLM filter extraction → `validate_filter` against the catalogue → `retrieve()` (hybrid FTS+vector, RRF-combined, filter mode hard/soft/fill/auto) → entity-alias pinning → one-hop relation expansion + same-record parameter passages → analytic-query mini-table → token-budgeted `assemble_context` → `build_prompt` → `generate()`.

`REVIEW_FINDINGS.md` is the running architecture-review + implementation-status log — check it before assuming a known gap is unaddressed. `query-pathways.md` is a companion deep-dive on filter-generation sensitivity to corpus shape.

**`enrich_demo/`**: `provider.py` (PDF extraction) → `llm.py` (the *only* LLM call, one per document) → `pipeline.py` (linking/second-signal gate, quote-grounding, unit normalization via `refcat.py` + `units.py`, dedup/conflict classification, fingerprinting, parking, corroboration graduation) → `state.py` (`enrich_state.db`) → `report.py` (Markdown report + `proposals.json`). `refcat.py` loads `test_records/*.json` (never `rag_test.db`) and does all deterministic attribute→field mapping and numeric comparison. Every proposal must cite a doc title/path + verbatim quote. Three gold corpora (`gold.json`/`gold2.json`/`gold2b.json`, each with its own generator + `evaluate*.py`) exist for regression testing, including deliberate traps (distractors, red herrings, cross-unit corroboration pairs, dual-unit table cells). `enrich_demo/FINDINGS.md`/`FINDINGS2.md`/`FINDINGS3.md` are the running write-ups of what's been found/fixed per phase — check the latest before assuming a listed bug is still open.

## Conventions when working in this repo

- Don't commit changes unless explicitly asked to.
- Treat `ragkit.py ingest` as destructive until `CODEX-IDd-TODO.md` P0.1 is fixed: it drops the target index before validating that the source is non-empty or that embedding will finish. Validate the source first and ingest into a disposable/new DB; never point an exploratory run at the only useful index.
- Use a disposable enrichment DB for eval work. An LLM response that is returned but unparsable is currently recorded as a processed document, and every report build writes the shared `proposals.json`/`report_runN.md` names; see `CODEX-IDd-TODO.md` P0.3-P0.4. Do not interpret such a run as safely retryable or overwrite the tracked demo evidence.
- When you fix a bug or ship a feature, verify it against the relevant gate(s) above (offline test scripts and/or a fresh eval-gate run) rather than reasoning from the diff alone — this repo has a documented history of fixes that looked correct but broke a gold-corpus regression (see `enrich_demo/FINDINGS3.md`'s "Robustness fixes" and "Pint restored" sections for two concrete examples of exactly this).
- State DBs (`*.db`) and per-run `proposals_*.json` files are gitignored/untracked scratch evidence — fine to create freely for verification, but don't overwrite the tracked `enrich_demo/proposals.json`, `report_run1.md`, `report_run2.md` (stable demo evidence) with throwaway eval-run output; restore them via `git checkout --` if a run clobbers them.
