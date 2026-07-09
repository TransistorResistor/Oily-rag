# RAG Demo user guide

This repository has two user-facing demos:

1. **Catalogue Q&A / model comparison** — retrieve grounded context from a SQLite catalogue, preview the exact prompt, and optionally compare hosted models.
2. **Reverse-enrichment review** — inspect proposed catalogue gap-fills, conflicts, and relationships with their source quotes. This surface is read-only.

Both demos bind to `127.0.0.1` by default. They do not need to be exposed to the LAN.

## 1. Prerequisites

The prepared development environment uses:

- Windows PowerShell
- Python 3.10 at `C:\Users\robot\anaconda3\python.exe`
- Packages pinned in `requirements-demo.txt`

From the repository root on a connected machine:

```powershell
& 'C:\Users\robot\anaconda3\python.exe' -m pip install -r requirements-demo.txt
```

The normal PowerShell launchers source `_env.ps1`, which selects that interpreter and applies the Windows OpenMP safeguards required by PyTorch/MiniLM.

If Windows reports that script execution is disabled, invoke a launcher through a process-scoped bypass (this does not change the machine policy):

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1 rag
```

`key.env` is optional. It is required only for OpenRouter generation and should contain:

```text
OPENROUTER_API_KEY=your-key-here
```

Context preview, local retrieval, and the enrichment-review demo do not need an API key.

## 2. Demo: catalogue Q&A and model comparison

Start the surface:

```powershell
.\demo.ps1 rag
```

It opens <http://localhost:8099>. The default `rag_test.db` is the larger historical Wikipedia-derived demo corpus. It is useful for exercising retrieval, but it is not the authoritative 25-record air-defence catalogue.

In the UI:

1. Enter a question such as `Compare the F-22 and F-35`.
2. Select **Preview context only** to run retrieval without spending an LLM call.
3. Optionally enable the model-driven metadata filter. This uses a hosted LLM and therefore needs network access and an OpenRouter key.
4. On a connected machine, select up to three models and compare their answers against exactly the same assembled prompt.
5. Expand the context panel to inspect filters, record IDs, passages, structured fields, and the final prompt.

To build a fresh index from the authoritative air-defence records, use a new DB filename because ingest is currently destructive:

```powershell
. .\_env.ps1
& $Python ragkit.py ingest test_records --db air_defence_demo.db
.\demo.ps1 rag -DbPath air_defence_demo.db
```

The one-question CLI remains available:

```powershell
.\ask.ps1 'What is the range of the AIM-120?'
```

## 3. Demo: reverse-enrichment review

Start the read-only proposal viewer:

```powershell
.\demo.ps1 enrichment
```

It opens <http://localhost:8100> and reads `enrich_demo/proposals.json`. The cards can be searched and filtered by proposal type. Expand a card to compare the existing catalogue value with the proposed value and inspect every grounded source quote.

Review a DB-specific proposal file without modifying it:

```powershell
.\demo.ps1 enrichment -Proposals enrich_demo\proposals_verify_2b.json
```

The viewer never writes decisions or state. The enrichment CLI remains the workflow for processing documents and recording rejections:

```powershell
Set-Location enrich_demo
& 'C:\Users\robot\anaconda3\python.exe' enrich.py status
& 'C:\Users\robot\anaconda3\python.exe' enrich.py list-proposals
```

Running `enrich.py run` performs one OpenRouter call per new document and is therefore **not available in a truly network-isolated sandbox**. Existing `proposals*.json`, reports, PDFs, state DBs, and the new review UI remain fully usable offline. If a live extraction call is blocked, report it as blocked; never invent claims or evaluation scores.

## 4. Required Hugging Face weights

Retrieval uses two pinned repositories listed in `huggingface_models.json`:

| Role | Repository | Pinned revision | Approx. snapshot size |
|---|---|---|---:|
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2` | `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` | 87 MB |
| Reranking | `cross-encoder/ms-marco-MiniLM-L-6-v2` | `c5ee24cb16019beea0893ab7796b1df96625c6b8` | 88 MB |

The embedding model is mandatory for ingest and every query. The reranker improves precision but can be disabled in a constrained sandbox; retrieval then uses vector + FTS5 reciprocal-rank fusion.

No generative model is needed for context preview. `Qwen/Qwen2.5-1.5B-Instruct` is an optional local CLI answer model and is much larger than the retrieval bundle.

## 5. Prepare a portable offline bundle

Run this once on a machine with Hugging Face access:

```powershell
. .\_env.ps1
& $Python prepare_offline.py
```

This creates `.hf-cache\hub` using the normal Hugging Face cache layout and writes `.hf-cache\ragkit-models.json` as a receipt. `.hf-cache` is intentionally gitignored; copy it alongside the repository when moving into the isolated environment.

To include the optional local Qwen answer model as well:

```powershell
& $Python prepare_offline.py --include-local-generator
```

If the isolated machine does not already have the Python environment, also prepare a wheel directory on a connected machine whose OS, CPU architecture, and Python version match the target:

```powershell
New-Item -ItemType Directory -Force offline_bundle\wheels
& $Python -m pip download -r requirements-demo.txt -d offline_bundle\wheels
```

Copy these items to the isolated machine:

- the repository, including `rag_test.db` or another already-ingested DB;
- `.hf-cache`;
- optionally `offline_bundle\wheels` if packages must be installed there.

## 6. Run in a network-isolated sandbox

If packages need installing:

```powershell
& 'C:\Users\robot\anaconda3\python.exe' -m pip install --no-index `
  --find-links offline_bundle\wheels -r requirements-demo.txt
```

Enable offline mode explicitly:

```powershell
. .\offline_env.ps1
```

This sets `HF_HOME`/`HF_HUB_CACHE` to the portable cache and enables both `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`, preventing the installed Hugging Face stack from attempting metadata requests.

Then launch the RAG demo:

```powershell
.\demo.ps1 rag -Offline
```

Under a restrictive execution policy, use:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\demo.ps1 rag -Offline
```

Offline mode skips OpenRouter model discovery. Use **Preview context only** in the UI. If the cross-encoder is unstable or the sandbox has a tight memory limit:

```powershell
.\demo.ps1 rag -Offline -NoReranker
```

The enrichment review has no model dependency and can simply be launched with:

```powershell
.\demo.ps1 enrichment
```

## 7. Verification and troubleshooting

Check that the portable model cache is complete before entering isolation:

```powershell
Test-Path .hf-cache\ragkit-models.json
Get-Content .hf-cache\ragkit-models.json
```

Run the deterministic checks:

```powershell
. .\_env.ps1
& $Python -m compileall -q .
& $Python enrich_demo\test_fixes.py
```

Common symptoms:

- **A Hugging Face URL appears offline:** the offline environment was not enabled, or `.hf-cache` was copied incompletely.
- **Process stops after `Loading weights`:** this is a failed gate, not success. Confirm the OpenMP variables from `_env.ps1`; retry with `-NoReranker` to isolate the cross-encoder.
- **`OPENROUTER_API_KEY` missing:** context preview still works. Hosted answer comparison and document extraction do not.
- **Wrong or empty DB:** confirm the `--db`/`-DbPath` value. Do not run exploratory ingest against the only useful DB.
- **No enrichment cards:** verify the proposal path is a JSON list and use the absolute path with `-Proposals` if launching from another directory.
