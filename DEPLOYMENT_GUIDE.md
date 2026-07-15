# RAG and reverse-enrichment deployment guide

This repository contains two related but independently runnable facets:

1. **RAG catalogue Q&A**: JSON records are normalized, embedded and indexed in
   SQLite, then retrieved for a local or hosted answer model.
2. **Reverse enrichment**: PDFs are extracted, one hosted LLM call produces
   neutral claims per new document, and deterministic code proposes gap-fills,
   conflicts and relation edges. It is report-only; it never writes a new
   catalogue record.

They share `record_model.normalize_record()` and the record JSON corpus, but they
do not share state databases. The RAG index must not be used as the enrichment
reference catalogue, and `rag_test.db` is a stale 63-record Wikipedia-derived
demo corpus rather than the authoritative 25-record air-defence snapshot.

## 1. Runtime and external dependencies

### Host assumptions

The maintained environment is:

- Windows with PowerShell;
- Python 3.10 at `C:\Users\robot\anaconda3\python.exe`;
- a writable copy of the repository;
- one process using the configured OpenMP safeguards:

```powershell
$env:KMP_DUPLICATE_LIB_OK = "TRUE"
$env:OMP_NUM_THREADS = "1"
$env:TOKENIZERS_PARALLELISM = "false"
```

The launchers apply these automatically through `_env.ps1`. Dot-source that file
before running Python commands directly.

### Python packages

Install the pinned demo environment from `requirements-demo.txt`:

| Package | Pinned version | Used by |
|---|---:|---|
| `Flask` | `3.1.0` | RAG and enrichment local web viewers |
| `numpy` | `2.2.6` | embeddings and numeric processing |
| `sentence-transformers` | `5.6.0` | MiniLM embeddings and optional cross-encoder reranking |
| `torch` | `2.10.0` | sentence-transformers runtime |
| `transformers` | `5.9.0` | optional local answer generation |
| `huggingface-hub` | `1.17.0` | downloading/loading pinned Hugging Face snapshots |
| `python-dateutil` | `2.8.2` | record date normalization |
| `pint` | `0.24.4` | unit conversion and dimensional checks |
| `PyMuPDF` (`fitz`) | `1.28.0` | enrichment PDF text extraction |
| `pymupdf4llm` | `1.28.0` | optional Markdown/table-oriented PDF extraction |
| `reportlab` | `5.0.0` | demo PDF generation and test corpus creation |

SQLite/FTS5, `urllib`, `json`, and the other standard-library pieces are supplied
by Python. The RAG code may need `accelerate` in an environment where the local
Transformers backend requires it; hosted generation does not.

### Hosted model/API dependency

OpenRouter is the only transport currently implemented in
`llm_provider.py`. It uses Python's standard-library HTTP client, so no OpenAI or
OpenRouter SDK is required.

Create `key.env` beside the repository root or set the process environment:

```text
OPENROUTER_API_KEY=your-key-here
```

Hosted calls are needed for:

- RAG answer generation through the OpenRouter backend;
- optional RAG metadata/filter extraction;
- enrichment claim extraction, one call per new PDF.

RAG answer-model choices are configured in `models_registry.py` and can be
selected by OpenRouter slug/alias. The repository does not require a separate
vector database, PostgreSQL service, message queue, container runtime, or model
server for the baseline deployment: SQLite, local Hugging Face retrieval models,
and the hosted HTTP endpoint are sufficient.

RAG context preview, local retrieval, and the enrichment proposal viewer do not
need an API key.

### Hugging Face model assets for RAG

RAG ingest and vector retrieval require the pinned embedding model:

| Role | Repository | Revision | Approximate size |
|---|---|---|---:|
| Embedding | `sentence-transformers/all-MiniLM-L6-v2` | `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` | 87 MB |
| Reranker | `cross-encoder/ms-marco-MiniLM-L-6-v2` | `c5ee24cb16019beea0893ab7796b1df96625c6b8` | 88 MB |

The embedding model is mandatory for ingest and every vector-backed query. The
reranker is optional and can be disabled with `-NoReranker` or
`RAGKIT_DISABLE_RERANK=1`.

The optional local answer model is `Qwen/Qwen2.5-1.5B-Instruct`; it is not needed
for ingest, retrieval, context preview, or enrichment. Hosted RAG answers and
enrichment use model slugs on OpenRouter and do not require local generator
weights.

### Enrichment extraction model

The enrichment default is `google/gemma-3-4b-it`, a small non-reasoning instruct
model with JSON output. It has been sufficient for the demo when paired with
deterministic guards. A 7B-14B non-reasoning instruct model is the sensible
production beta comparison if better claim completeness is worth the cost. A
larger reasoning model is not the default recommendation for this bounded
extraction task.

## 2. Connected-machine installation

From the repository root on a connected machine:

```powershell
$Python = "C:\Users\robot\anaconda3\python.exe"
& $Python -m pip install -r requirements-demo.txt
. .\_env.ps1
```

Verify the important imports and unit layer:

```powershell
& $Python -c "import fitz, flask, pint, reportlab, sentence_transformers, torch; print('imports ok')"
& $Python -c "import units; print('pint_available=', units._ureg is not None); print(units.convert(20, 'nm', 'km'))"
```

The expected conversion is approximately `37.04` km. If `pint_available=False`,
the enrichment pipeline will park some cross-unit comparisons rather than crash,
but the beta environment should repair the dependency installation before
quality testing.

Prepare the RAG model cache while Hugging Face access is available:

```powershell
& $Python prepare_offline.py
# Optional local Qwen answer model:
& $Python prepare_offline.py --include-local-generator
```

This creates the gitignored `.hf-cache` directory and a receipt at
`.hf-cache\ragkit-models.json`.

## 3. RAG deployment

### Build an index

Always ingest into a new or disposable DB path while testing:

```powershell
& $Python ragkit.py ingest test_records --db .beta\rag_air_defence.db
```

The ingest path normalizes each record through `record_model`, builds FTS5,
structured parameter tables, relations, field embeddings and metadata. The
embedding model and dimension are recorded in the DB. Do not mix an index built
with one embedding model and queries using another.

Check retrieval without an answer-model call:

```powershell
& $Python ragkit.py eval --db .beta\rag_air_defence.db --eval-set eval_set.json
```

### Run the UI

```powershell
. .\_env.ps1
.\demo.ps1 rag -DbPath .beta\rag_air_defence.db
```

The bench binds to `127.0.0.1:8099`. Context preview works without an API key;
hosted model comparison requires `OPENROUTER_API_KEY`.

CLI examples:

```powershell
& $Python ragkit.py ask "What is the range of the AIM-120?" `
  --db .beta\rag_air_defence.db --backend openrouter --model gemma4

# Local answer generation, if the optional local model/dependencies are present:
& $Python ragkit.py ask "What is the range of the AIM-120?" `
  --db .beta\rag_air_defence.db --backend local `
  --model Qwen/Qwen2.5-1.5B-Instruct
```

## 4. Enrichment deployment

### Reference data and state

The enrichment reference is loaded from `test_records/*.json` by
`enrich_demo/refcat.py`. Enrichment state is separate in `enrich_state.db` (or a
path supplied with `--db`). The RAG DB is never opened.

The current MVP mapper is enabled explicitly:

```powershell
Set-Location enrich_demo
& $Python enrich.py field-audit --output ..\.beta\field_audit.json
& $Python enrich.py --db ..\.beta\enrich_state.db run `
  --folder testdocs `
  --field-mapper catalogue `
  --text-fields `
  --note "beta enrichment"
```

Use `--text-fields` only when you want the experimental Text/LOV gap-fill path.
Numeric fields and relations work without it.

The run produces a DB-scoped report and proposals file. The report includes
mapping tier/method, parked candidates, and equivalent-term suggestions.

Useful review commands:

```powershell
& $Python enrich.py status
& $Python enrich.py list-proposals
& $Python enrich.py report --run 1
& $Python enrich.py reject <proposal_id> --reason "not independently verified"
```

Enrichment processing requires network access and an OpenRouter key because it
makes one extraction call per new PDF. Existing proposal JSON and Markdown
reports can be reviewed offline.

## 5. Network-isolated deployment

On a connected machine with the same OS, CPU architecture and Python version as
the target, prepare wheels:

```powershell
New-Item -ItemType Directory -Force offline_bundle\wheels
& $Python -m pip download -r requirements-demo.txt -d offline_bundle\wheels
```

Copy the repository, `.hf-cache`, and (if needed) `offline_bundle\wheels` to the
isolated host. Install packages without an index:

```powershell
& $Python -m pip install --no-index `
  --find-links offline_bundle\wheels -r requirements-demo.txt
```

Enable the pinned local model cache before RAG commands:

```powershell
. .\offline_env.ps1
```

Then use:

```powershell
.\demo.ps1 rag -Offline -NoReranker
```

The lowest-risk isolated path is RAG context preview with the reranker disabled.
Do not report hosted answer or enrichment quality results when the network is
blocked. Existing enrichment reports/viewer data remain usable offline, but new
PDF extraction calls do not.

## 6. Beta onboarding for a new JSON parameter/schema snapshot

Assume the new records retain the general pages-schema shape:
`modelID`/`nomenclature`, `parametrics[]`, `parameterOnly`, `parameterValue`,
`parameterDescr`, `dataType`, `uom`, components, variants and relations.

### Step 1: Freeze and validate a snapshot

Keep the beta records in a separate directory and preserve the source snapshot
date. Before ingesting, check:

- every record has a stable unique `modelID` or `id`;
- titles/nomenclature are non-empty;
- `parametrics` rows contain usable parameter names and values;
- `parameterDescr`, `dataType` and `uom` coverage is measured;
- repeated parameter names have meaningful subtitles/comments/components;
- units are spelled consistently or are convertible;
- relation endpoints and aliases are present where expected;
- no records are silently skipped by normalization.

The canonical parser supports the two existing JSON shapes. A quick audit can be
run with:

```powershell
& $Python -c "import json,glob,record_model; dropped={}; n=0; [record_model.normalize_record(json.load(open(f,encoding='utf-8')), dropped) for f in glob.glob('beta_records/*.json')]; print('files=',len(glob.glob('beta_records/*.json'))); print('dropped=',json.dumps(dropped,indent=2))"
```

Treat any dropped structure as an adapter/data-quality issue, not as a successful
beta import.

### Step 2: Build a disposable RAG index

```powershell
& $Python ragkit.py ingest beta_records --db .beta\rag_beta.db
& $Python ragkit.py eval --db .beta\rag_beta.db --eval-set beta_eval_set.json
```

Use a fresh DB whenever the record snapshot, embedding model or canonicalization
rules change. Do not append a new schema snapshot to an old index and assume the
embeddings remain comparable.

Create a small beta evaluation set containing:

- exact ID/title lookups;
- one query per important numeric field and unit;
- comparisons involving repeated/variant values;
- relation questions;
- negative or out-of-catalogue questions;
- at least one expected structured-table answer.

Record retrieval hit rate and evidence sufficiency separately. A correct record
without the requested field/value is not a successful beta result.

### Step 3: Point enrichment at the new reference snapshot

The current CLI reference path is still hard-coded to `test_records`. For a safe
beta, do not overwrite the tracked demo corpus. Use one of these approaches:

1. Run the beta from an isolated worktree/copy where `test_records` is replaced by
   the beta snapshot; or
2. Add the small follow-up `--ref-dir` option to `refcat.load_reference()` and the
   enrichment CLI before operationalizing multiple simultaneous catalogues.

The second option is the recommended deployment change. It should also make the
reference directory and snapshot identifier visible in the run metadata.

Once the beta reference is selected, generate the field audit:

```powershell
& $Python enrich.py field-audit --output ..\.beta\field_audit_beta.json
```

Review fields with:

- multiple definitions;
- mixed data types;
- incompatible units;
- different components sharing a name;
- low record coverage;
- repeated values without clear variant qualifiers.

### Step 4: Seed equivalent terms

New parameter names are automatically visible to the generated field catalogue.
Terms that differ from those names need entries in
`enrich_demo/field_aliases.json`:

```json
{
  "version": 3,
  "fields": {
    "Maximum engagement altitude": {
      "aliases": ["engagement ceiling", "service ceiling"]
    }
  },
  "contextual_aliases": []
}
```

Start with exact aliases and contextual aliases. Let the beta report generate
definition-profile suggestions for recurring unmapped terms, then promote only
the suggestions that are semantically correct.

Do not make generic terms such as `range`, `time`, `weight` or `speed` global
aliases unless record/component/unit context makes the interpretation unique.

### Step 5: Run a report-only shadow batch

Use a new enrichment state DB and keep text proposals explicitly identifiable:

```powershell
& $Python enrich.py --db ..\.beta\enrich_beta.db run `
  --folder beta_pdfs `
  --field-mapper catalogue `
  --text-fields `
  --note "beta snapshot 2026-07-12"
```

Measure:

- extraction success and retryable failures;
- high/medium/parked mapping tiers;
- frequent unmapped attributes and alias suggestions;
- proposal precision/recall against a small gold set;
- parked reasons, especially ambiguous fields and incompatible units;
- analyst acceptance/rejection by mapping method;
- token cost and latency per document;
- false positives from tables, negation, temporal language and distractors.

Do not use the beta state DB as a production catalogue write path. The intended
workflow is review, reject/suppress where appropriate, and only then apply an
independent controlled catalogue update.

## 7. Operational checklist

Before each beta run:

- confirm the Python interpreter and package versions;
- confirm the OpenRouter key is available if hosted calls are expected;
- confirm the model alias resolves to the intended provider model;
- confirm the RAG embedder/cache and DB provenance match;
- use new DB paths for new snapshots or mapper versions;
- run `field-audit` for the reference JSON;
- inspect the alias overlay version;
- prepare a disposable state DB and proposal output directory;
- keep a small gold/evaluation set with expected fields and source documents;
- preserve the run note, snapshot ID, mapper version and model name.

If a hosted call is blocked or a response is unparsable, report the beta as
blocked/partial. Do not infer precision, recall or proposal counts for documents
that were never successfully extracted.
