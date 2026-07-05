# Reverse-enrichment pipeline (demo)

A **report-only** pipeline that reads noisy PDFs, extracts neutral factual claims
with a cheap LLM (ONE call per document), and — using entirely deterministic code
— proposes additions/corrections to an existing defence-equipment catalogue:
**gap-fills**, **conflicts**, and **relation** edges. New whole records are out of
scope (an unknown system surfaces only as an "unlinked" parked fragment).

> Rule throughout: **propose facts, never LLM-drafted prose.** The model only
> emits candidate claims; every mapping, validation and threshold decision is code.

## Hard rules honoured
- **`rag_test.db` is never opened.** All pipeline state lives in a separate
  `enrich_state.db`. (In fact the on-disk `rag_test.db` is a *stale* 63-record
  Wikipedia corpus, unrelated to the air-defence records — see FINDINGS §Issue 1 —
  so the reference catalogue is loaded from `../test_records/*.json`, the canonical
  25 air-defence records, via the repo's own `record_model.normalize_record`.)
- **Cheap model:** extraction defaults to `google/gemma-3-4b-it` (the cheapest
  "Edge" model in `models_registry.py`). ~$0.0002 for the whole 15-doc eval.

## Layout
| file | role |
|---|---|
| `refcat.py`   | reference catalogue + data-dictionary + deterministic claim→field mapping + alias lexicon |
| `provider.py` | folder PDF provider (PyMuPDF text extraction, content-hash, boilerplate stripping) |
| `llm.py`      | the single LLM touch-point: one claim-extraction call per doc (OpenRouter, urllib) |
| `pipeline.py` | linking (second-signal gate), validation (quote-grounding, unit norm, dedup/conflict), fingerprints, parking, SQL graduation |
| `state.py`    | `enrich_state.db` schema (docs_seen / claims / proposals-view / decisions / runs) |
| `report.py`   | per-run Markdown report + `proposals.json` |
| `enrich.py`   | CLI |
| `gen_testdocs.py` | generates the 15-PDF noisy corpus + `gold.json` |
| `evaluate.py` | scores `proposals.json` vs `gold.json` |

## Setup
Uses the repo's Anaconda Python and `key.env` (OPENROUTER_API_KEY). Deps
(`reportlab`, `pymupdf`, `pint`) are already present in that environment.

```bash
PY="C:/Users/robot/anaconda3/python.exe"
cd enrich_demo
"$PY" gen_testdocs.py          # build testdocs/*.pdf + gold.json
```

## Run (two batches demonstrate incrementality)
```bash
# Batch 1 (11 docs incl. the FIRST of the corroborating altitude pair)
"$PY" enrich.py run --only gapfill_s400_belarus,gapfill_s300_detection,gapfill_s400_alias,\
conflict_s400_deploytime,conflict_python5_range,corrob_s400_altitude_a,distractor_python_lang,\
distractor_derby_horse,unlinked_patriot,hedged_s300,relation_f35_aim9x --note batch1

# Batch 2 (second corroborating doc + two more). Batch-1 docs are hash-skipped
# (0 LLM calls); the parked altitude claim GRADUATES here.
"$PY" enrich.py run --only corrob_s400_altitude_b,python5_vietnam,spyder_poland --note batch2

"$PY" evaluate.py             # precision/recall per type
```

## Suppression / decisions ledger
```bash
"$PY" enrich.py list-proposals
"$PY" enrich.py reject <proposal_id> --reason "unverified single source"
# a later doc restating the same fact is NOT resurfaced -> shows as "Seen again xN"
"$PY" enrich.py run --only rerun_s400_belarus --note batch3
```

## Other commands
```bash
"$PY" enrich.py status            # docs_seen / runs / claim status counts
"$PY" enrich.py report --run N    # (re)render report_runN.md + proposals.json
```

## Render mode + per-DB proposals (Phase B)
```bash
# --render text (default, PyMuPDF plain text) | md (markdown pipe tables via
# pymupdf4llm). Folded into the content hash, so the same PDF re-renders as a
# distinct doc under a different mode.
"$PY" enrich.py --db phaseB_md.db run --render md --folder testdocs2b --note md
```
`report.py` always writes the legacy `proposals.json` AND, for any non-default
`--db`, `proposals_<dbstem>.json` (so concurrent corpora don't clobber each
other's eval input). Both evaluators take an optional proposals path:
`"$PY" evaluate2b.py proposals_phaseB_md.json`. See **FINDINGS3.md** for the
text-vs-md A/B and the header-unit / dual-unit table handling.

## Outputs
- `report_runN.md` — grouped by record: New this run / Conflicts (DB vs doc with
  value distribution) / Outstanding from prior runs / "Seen again" (rejected) /
  parked fragments. Every item cites doc title + path + verbatim quote.
- `proposals.json` — machine-readable live proposals.

See **FINDINGS.md** for the eval scores and the unforeseen issues/tradeoffs.
