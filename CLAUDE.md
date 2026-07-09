# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Two related, independently runnable systems that share the same record corpus and hosted-LLM plumbing:

1. **Root: a single-file RAG prototype** (`ragkit.py` + satellites) — ingest defence-equipment JSON records into SQLite (FTS5 + MiniLM embeddings), answer questions with hybrid retrieval + an LLM, and a Flask bench (`compare_server.py`) to compare answers across model tiers side by side.
2. **`enrich_demo/`: a reverse-enrichment pipeline** — reads noisy PDFs, extracts neutral factual claims with one cheap LLM call per document, and — via entirely deterministic code — proposes gap-fills/conflicts/relation-edges against the *same* record catalogue. Report-only; never LLM-drafted prose, never a new record.

Both load records via `record_model.normalize_record` from **`test_records/*.json`** (25 curated air-defence records — missiles, radars, launchers). **`rag_test.db` on disk is a stale 63-record Wikipedia corpus, unrelated to this domain — do not treat it as ground truth for anything schema- or content-related.** It's still useful as a bigger/messier corpus to ingest for retrieval-scale testing.

## Environment

- Python is the shared Anaconda interpreter: `C:\Users\robot\anaconda3\python.exe` (not on `PATH`). Always invoke it by full path, and run commands from the repo root (a local `catalogue.py` shadows the PyPI package of the same name if run from elsewhere).
- `OPENROUTER_API_KEY` lives in `key.env` (gitignored) at the repo root; `_env.ps1` (dot-sourced by `ask.ps1`/`serve.ps1`/`compare.ps1`) loads it into the process env and sets `KMP_DUPLICATE_LIB_OK=TRUE`, `OMP_NUM_THREADS=1`, `TOKENIZERS_PARALLELISM=false` (needed to avoid an intermittent Windows/conda OpenMP segfault when the embedder loads). Running the raw `python` commands below outside the `.ps1` launchers needs those same env vars set by hand.
- In a network-restricted sandbox, also set `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`. The installed Hugging Face stack otherwise attempts a metadata `HEAD` request even when model weights are cached. Treat a process that stops after the two `Loading weights` bars without printing the script/report completion lines as a failed/crashed gate, not a pass.
- `prepare_offline.py` materializes the pinned embedder/reranker revisions from `huggingface_models.json` into the gitignored portable `.hf-cache`; `offline_env.ps1` selects that cache and blocks network lookups. `demo.ps1 rag -Offline -NoReranker` is the lowest-risk isolated-sandbox smoke path. See `USER_GUIDE.md` before changing this contract.
- `units.py` (repo root) wraps `pint` for unit conversion; if pint's import chain is ever broken again by a stale `dask`/`xarray` vs. numpy version mismatch, `_ureg` silently becomes `None` and every `convert()` call raises `ConversionError` rather than crashing — check `python -c "import units; print(units._ureg is None)"` before assuming conversion is unavailable.
- No pytest anywhere in this repo. `enrich_demo/test_fixes.py` and `test_i3_context.py` are plain standalone scripts (each `def test_*` runs unconditionally under `if __name__ == "__main__"`); run the whole file, there's no test-selection flag.

## Common commands

### RAG prototype (root)
```bash
PY="/c/Users/robot/anaconda3/python.exe"

# Ingest a directory of record JSON files into a SQLite index
"$PY" ragkit.py ingest test_records --db rag.db

# Ask one question (add --auto-filter to let the model derive a metadata filter)
"$PY" ragkit.py ask "What is the range of the S-400?" --db rag_test.db --backend openrouter --model gemma4

# Launch the model-comparison web bench (side-by-side, up to 3 models + context preview)
"$PY" compare_server.py --db rag_test.db     # or: .\compare.ps1  (also sets env)

# Offline eval harness: retrieval hit-rate + prompt-size stats; add --backend to also
# score the LLM-dependent stages (filter-extraction accuracy, answer/citation checks)
"$PY" ragkit.py eval --db rag_test.db --eval-set eval_set.json
```
The `.ps1` launchers (`ask.ps1`, `serve.ps1`/`compare.ps1`) are the normal entry points on this machine — they source `_env.ps1` for you.

### enrich_demo/ reverse-enrichment pipeline
```bash
cd enrich_demo
"$PY" gen_testdocs.py                          # build testdocs/*.pdf + gold.json (once)
"$PY" enrich.py run --only <doc_id,doc_id,...> --note batch1   # process specific docs
"$PY" evaluate.py                              # score proposals.json vs gold.json (P/R per type)
"$PY" enrich.py status                         # docs_seen / runs / claim status counts
"$PY" enrich.py report --run N                 # (re)render report_runN.md + proposals.json
"$PY" enrich.py list-proposals
"$PY" enrich.py reject <proposal_id> --reason "..."

# non-default --db and/or --render {text,md} write proposals_<dbstem>.json so
# concurrent corpora don't clobber each other's eval input:
"$PY" enrich.py --db myrun.db run --render md --folder testdocs2b --note md
"$PY" evaluate2b.py proposals_myrun.json
```
Corpora 2/2b have their own generator/gold/evaluator (`gen_testdocs2.py`/`gold2.json`/`evaluate2.py`, `gen_testdocs2b.py`/`gold2b.json`/`evaluate2b.py`). See `enrich_demo/README.md` for the full batch-1/batch-2 incrementality demo (hash-skip + corroboration graduation) and the suppression-ledger walkthrough.

### Tests
```bash
"$PY" enrich_demo/test_fixes.py     # 5 targeted robustness tests (state/pipeline/refcat)
"$PY" test_i3_context.py            # 4 context-assembly checks against local MiniLM (no LLM needed)
```

### User demos
```powershell
.\demo.ps1 rag
.\demo.ps1 enrichment
```

## Architecture

### RAG prototype: `ragkit.py` is the monolith, everything else is a satellite
`ragkit.py` (~3400 lines) owns ingest, retrieval, filter extraction/validation, context assembly, prompt building, and generation — all in one file, organized as clear top-to-bottom stages rather than classes. Read it stage by stage rather than expecting a package layout:

- **Record parsing**: delegated entirely to `record_model.py` — the single canonical parser for both supported record shapes (see `ragkit.py`'s module docstring for the two JSON shapes). Every other consumer (embedding text, the parametric table, the filter catalogue) derives a thin "view" from `record_model.normalize_record()`'s output; nothing re-parses raw records independently.
- **Filter vocabulary**: `catalogue.py` classifies each field (numeric/categorical/date/multi_value/free_text) from `record_model.typed_fields()` output, producing both the filter-extraction prompt spec and the validation target for `ragkit.validate_filter`.
- **Ingest** (`ragkit.ingest`): chunks each record's prose + parameters, embeds with MiniLM (`sentence-transformers/all-MiniLM-L6-v2`, 384-dim — chosen to match the Clipper project's embedding space), builds FTS5 + vector index + per-field embeddings + per-`systemType` numeric stats, stores an `embed_model` provenance key in `meta`.
- **A query's path** (`ragkit.answer`): optional LLM filter extraction (`extract_filter_ex`, one or two passes) → `validate_filter` against the catalogue → `retrieve()` (hybrid FTS+vector with RRF, filter applied as hard/soft/fill/auto per `--filter-mode`) → entity-alias pinning (`match_entities`) → one-hop relation expansion (`related_records`) + same-record parameter passages (`pinned_parameter_passages`) → analytic-query mini-table (`record_table`) → global token-budgeted `assemble_context` (passages/related/table/digest sections water-fill against a single `max_context_tokens` cap) → `build_prompt` → `generate()`.
- **Hosted-LLM calls** all funnel through **`llm_provider.py`** — the one place the OpenRouter transport lives (`chat()`/`chat_with_usage()`). To swap providers, reimplement that file's contract; prompts themselves live at the call sites (`ragkit.system_prompt_for`/`build_prompt`, the filter-extraction prompt inside `extract_filter_ex`, `enrich_demo/llm.py`'s `SYSTEM`/`USER_TMPL`), not in the adapter.
- **`compare_server.py`** is a thin Flask wrapper: it imports `ragkit` unchanged and adds a side-by-side multi-model UI plus a "preview the assembled context/prompt without spending a call" mode. `models_registry.py` is the model lineup (OpenRouter slug + rough VRAM tier) shown there and validated against OpenRouter's `/models` at startup.
- **`eval.py`** is the offline harness (`ragkit.py eval` subcommand shares its arg parser): retrieval hit-rate/MRR against `eval_set.json`'s scored gold cases is always run; the LLM-dependent stages (filter-extraction accuracy, answer/citation checks) are opt-in via `--backend` since they cost real API calls.
- **`units.py`** is a narrow pint wrapper used by `validate_filter` to convert a query filter's bound into a field's canonical unit before comparing; if conversion fails or pint is unavailable, callers are expected to fail safe (drop the filter/park the claim) rather than compare mismatched magnitudes.
- Config is unified in `ragkit.DEFAULTS` — the CLI's `ask` subcommand and `compare_server`'s bench read the same dict, so the two entry points can't silently drift on defaults (k, filter mode, token cap, etc).
- **`REVIEW_FINDINGS.md`** is the running architecture-review + implementation-status log (sections A–J); it's the source of truth for what's been fixed vs. still open — check it before assuming a known gap is unaddressed. `query-pathways.md` is a companion deep-dive specifically on filter-generation sensitivity to corpus shape.

### `enrich_demo/`: deterministic claim pipeline, one LLM call per document
Layout (see `enrich_demo/README.md` for the full table): `provider.py` (PDF text extraction) → `llm.py` (the *only* LLM touch-point — one claim-extraction call per doc) → `pipeline.py` (linking/second-signal gate, quote-grounding, unit normalization via `refcat.py`'s deterministic field-mapping + `units.py`, dedup/conflict classification, fingerprinting, parking, SQL-free graduation of corroborated low-trust claims) → `state.py` (`enrich_state.db` schema: `docs_seen`/`claims`/`runs`/rejection ledger) → `report.py` (per-run Markdown report + `proposals.json`). `enrich.py` is the CLI tying it together.
- **`refcat.py`** is the reference catalogue: loads `test_records/*.json` (never `rag_test.db`), builds the alias index, and does all attribute→field mapping and numeric comparison (`compare_numeric` returns `match`/`conflict`/`gap`/`incomparable` — the last one deliberately avoids comparing across unconvertible units rather than guessing).
- Every proposal must cite a doc title/path + verbatim quote; nothing is surfaced without deterministic-code validation downstream of the single LLM call.
- Three gold corpora exist for regression testing (`gold.json`/`gold2.json`/`gold2b.json`, each with its own generator + `evaluate*.py`), covering gap-fill/conflict/relation cases plus deliberate traps (distractors, red herrings, hedged/malformed claims, cross-unit corroboration pairs, dual-unit table cells). `enrich_demo/FINDINGS.md`/`FINDINGS2.md`/`FINDINGS3.md` are the running write-ups of what each phase found and fixed — check the latest before assuming a listed bug is still open.

## Conventions when working in this repo

- Don't commit changes unless explicitly asked to.
- Treat `ragkit.py ingest` as destructive until `CODEX-IDd-TODO.md` P0.1 is fixed: it drops the target index before validating that the source is non-empty or that embedding will finish. Validate the source first and ingest into a disposable/new DB; never point an exploratory run at the only useful index.
- Use a disposable enrichment DB for eval work. An LLM response that is returned but unparsable is currently recorded as a processed document, and every report build writes the shared `proposals.json`/`report_runN.md` names; see `CODEX-IDd-TODO.md` P0.3-P0.4. Do not interpret such a run as safely retryable or overwrite the tracked demo evidence.
- When you fix a bug or ship a feature, verify it against the relevant gate(s) above (offline test scripts and/or a fresh eval-gate run) rather than reasoning from the diff alone. State DBs (`*.db`) and per-run `proposals_*.json` files are scratch evidence; do not overwrite the tracked `enrich_demo/proposals.json`, `report_run1.md`, or `report_run2.md` with throwaway output.
