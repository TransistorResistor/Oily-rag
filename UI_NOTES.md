# UI notes — production-ready surfaces for Q&A and enrichment

Design notes for taking the two prototypes to real UIs. Auth is out of scope here.
Companion to REVIEW_FINDINGS.md (§N new-record proposals, §O priority tiers + ack
ledger are referenced below as if implemented; they are design-only today).

## Shape of the whole thing

**One backend service, two thin UIs.** The repo already splits cleanly into an
*explorer* (Q&A over the catalogue) and a *curator workbench* (enrichment review).
Keep them as two front-ends over one long-lived Python API process, because:

- The embedder load is expensive (and needs the `KMP_DUPLICATE_LIB_OK` /
  `OMP_NUM_THREADS=1` env dance on this machine). A persistent process loads
  MiniLM once at startup — never spawn Python per request.
- Every fact both UIs display must come from the *same functions the CLIs use*.
  The prototypes already enforce this (`compare_server.py` imports `ragkit`
  unchanged; `enrich.py` verbs call `state.py`/`report.py`). The prod API layer
  keeps that rule: it is a JSON serializer over existing seams, never a
  reimplementation. If the UI needs a query the backend doesn't have, add the
  function to the backend module, then expose it.

The existing seams the API wraps:

| Seam | What it gives the UI |
|---|---|
| `compare_server.build_context(query, auto_filter, two_pass)` → `(contexts, prompt, finfo)` | The whole retrieval story without spending an LLM call |
| `ragkit.answer` internals (`extract_filter_ex` → `validate_filter` → `retrieve` → `assemble_context` → `build_prompt` → `generate`) | The staged ask pipeline; prod API should expose stage boundaries, not re-run the monolith blindly |
| `ragkit.DEFAULTS` | Single source for k / filter mode / token cap — serve as `GET /api/defaults` so UI settings can't drift from CLI |
| `catalogue.py` field spec | Filter-chip vocabulary: field names, types, units, LOV values |
| `record_model.normalize_record` | The record inspector's data (both UIs) |
| `models_registry.py` | Model picker entries + VRAM tier; validated against OpenRouter at startup |
| `enrich_demo/report.py::_proposals(con, run_id)` | The proposal materialization query — lift into a shared module and serve live; **do not parse `proposals.json`** (that file is eval evidence, not an API) |
| `enrich_demo/state.py` (`docs_seen`/`claims`/`decisions`/`runs`, plus §O `entry_ack`) | All curator state; SQLite is the source of truth |
| `enrich.py` verbs (`run`, `reject`, `ack`, `status`, `report`) | Every UI action must share the CLI's code path so ledger semantics stay identical |

SQLite is single-writer: curator *actions* (reject/ack) are tiny transactions and
fine; enrichment *runs* must be serialized behind a job queue (one at a time),
with the UI polling a job row or subscribing to SSE progress.

---

## Q&A surface (the explorer)

### Core interaction: ask → answer with its working shown

The differentiating feature is already built in `compare_server.py`: the
**transparency drawer**. Production keeps the two-step shape:

1. `POST /api/ask {query, model, auto_filter, filter_mode}` streams the answer
   (SSE), then delivers a final envelope: `{answer, filter: finfo, sources,
   table, timings, prompt_tokens}` — the same payload `api_compare` returns
   today, minus the multi-model fan-out.
2. A collapsed **"how this was answered"** drawer under every answer, populated
   from that envelope: extracted filter, retrieval sources, the mini-table if
   one was built, and prompt-size stats.

Streaming requires extending `llm_provider.py`'s contract with a streaming
variant of `chat()`; that file is the designated single swap point, so add it
there, not at call sites. Until then, a latency spinner with the
`latency_s`-style timing readout is acceptable — the calls are seconds, not
minutes, on the low/mid-tier models this system targets.

### Filter chips — the drawer made editable

`finfo` (filter + validation outcome) renders as **chips**: one per clause,
showing field, operator, value+unit, and whether it was applied hard/soft/fill.
Two wiring points make them more than decoration:

- **Chip edit/remove → re-retrieve without re-extracting.** The expensive,
  flaky step is LLM filter extraction; validation and retrieval are cheap and
  deterministic. `POST /api/ask` accepts an optional pre-validated `filter`
  object that bypasses `extract_filter_ex` entirely. A user who deletes a wrong
  chip gets a corrected answer for one generation call, zero extraction calls.
- **Rejected clauses shown, not hidden.** `validate_filter` already drops
  clauses it can't ground in the catalogue (unknown field, unconvertible unit).
  Render those as struck-through chips with the rejection reason. This is the
  honest-failure principle from the §K review: when retrieval was broader than
  the user asked, say so.

Chip vocabulary (field names, types, LOVs, canonical units) comes from the
catalogue endpoint, so an edited chip is validated client-side against the same
spec `validate_filter` uses server-side.

### Citations → record inspector

Answers cite `[rid]`. Make every citation a link into a **record inspector**
panel (`GET /api/record/{rid}` → `normalize_record` output), rendered as:

- header: nomenclature, systemType, aliases, codes;
- descriptions by `descrType`;
- **parametrics as a real table**, grouped by `component`, showing
  parameter / value / uom / comments, with `parameterSubTitle` variants
  (Combat range - 1 / - 2) kept as distinct rows and `parameterDescr` as a
  hover/tooltip definition — it's the data-dictionary meaning, not per-record
  prose (§J);
- proliferations as typed rows (IOC Year, Projected Fielding, Using — these are
  the K3 date source, so display them as the structured facts they are);
- relations as clickable hops (the one-hop expansion the retriever already does
  — the UI should let the human do the same hop).

The inspector is a **shared component** with the enrichment UI (see below).
Also give it a browse entry point (`GET /api/records?type=...&filter=...`) so
the catalogue is explorable without asking a question first.

### Analytic queries

When `finfo["table"]` is set, the answer was grounded in a computed mini-table
(`record_table`). Render that table as sortable HTML alongside the prose answer,
badged **"computed from the index, not by the model"** — it's the strongest
trust signal the system has, and burying it in the prompt preview wastes it.

### Honest empty/degenerate states

Design these as first-class screens, not error toasts (all observed in the §K
vague-query review):

- retrieval returned nothing under a hard filter → offer one-click "relax to
  soft filter" (re-ask with `filter_mode` changed, no re-extraction);
- superlative/meta-queries the system refuses → show the refusal *and* what a
  supported reformulation looks like;
- degenerate filter (matches everything) → note that the filter added nothing.

### What the Q&A surface is *not*

- **Not a chat.** `ragkit.answer` is single-turn; there is no conversation
  state. A prod UI that shows a chat transcript implies follow-ups work.
  Render history as a list of independent Q→A cards (re-runnable, shareable via
  querystring), until/unless multi-turn is actually built.
- **Compare mode is a bench, not the front door.** Keep `compare_server`'s
  side-by-side as an internal/eval route behind a "workbench" toggle; the prod
  default is one configured model, with `models_registry` tiers surfaced as
  friendly labels (fast / balanced / best) instead of raw OpenRouter slugs.

---

## Enrichment surface (the curator workbench)

### Information architecture: entry-first inbox

The unit of curator attention is the **entry** (modelID), not the proposal —
this is what §O(f)/(g) formalize. Landing view is a ranked inbox of entry
cards, ordered by §O effective rank:

- card shows: record title, **base tier / out-of-step count / effective rank**
  (all three, so ordering is explainable — never a buried scalar), and proposal
  counts by type (conflict / gap-fill / relation / alias-link);
- "new since acknowledged" badge when the §O(g) watermark detects novel
  fingerprints;
- expanding a card lists its proposals grouped by field.

Wire: `GET /api/entries` runs the §O ranking over live `_proposals()` output
joined with `priority_tiers.json` and the `entry_ack` table. Server-side, one
query; no client-side ranking logic.

### Proposal detail: the quote is the trust anchor

Each proposal (`GET /api/proposals/{fp}`, shape = the `_proposals()` dict)
renders with the **verbatim quote + doc title first**, then the structured
claim. Specifically:

- proposed value vs `db_value` side by side for conflicts, with `qualifier`;
- `value_distribution` when sources disagree (e.g. `{"400 km": 2, "380 km": 1}`)
  as a small histogram — corroboration strength at a glance, using
  `n_sources`/`cluster_sources` already in the payload;
- every source: doc title, path, quote. Deep-linking into the PDF at the quoted
  page needs `provider.py` to start capturing page numbers per extracted span —
  small change, large UX payoff; do it before building the viewer;
- a compact copy of the record inspector (shared component) scoped to the
  affected field's component group, so the curator sees the proposal in
  context of the record it would change.

### Actions: report-only means "accept" emits, never writes

The pipeline's contract is report-only — no UI button may mutate the catalogue.
Three proposal-level actions, one entry-level:

- **Accept** → `POST /api/proposals/{fp}/accept` emits a schema-shaped patch
  (the record's JSON fragment with the new/changed parametric row, plus
  citations) to an export queue / downloadable JSON. It's a hand-off artifact
  for whatever system owns the catalogue, and it marks the proposal accepted in
  `decisions` so it stops surfacing.
- **Reject** → `POST /api/proposals/{fp}/reject {reason}` — must call the same
  code the CLI `enrich.py reject` uses, so the fingerprint-suppression ledger
  behaves identically whichever interface was used.
- **Defer** → leaves it open; it keeps riding the §O ranking.
- **Entry ack** → `POST /api/entries/{model_id}/ack {action: snooze|ignore|
  deprioritize, until, reason}` — the §O(g) ledger. UI must show acknowledged
  entries in a collapsed "acknowledged" section (with the new-evidence count),
  and offer revoke. Nothing disappears.

### Parked-pile triage — the recall surface

FINDINGS2 established the parked pile is where lost recall lives. Give it a
dedicated tab, not an afterthought:

- filter by `park_reason` (unlinked / unmappable / incomparable / hedged);
- cluster **unlinked** claims by `entity_mention` — this exact clustering is
  §N's new-record candidate input, so the tab doubles as the §N review queue:
  a cluster with ≥2 docs and mappable attributes renders a skeleton-record
  preview (test_records JSON shape, per-field citations) with
  **propose-new-record / this-is-an-alias-of… / dismiss** actions
  (alias suggestion = §N false-novelty gate output, surfaced as `alias_link`);
- for **unmappable** claims, the safe promote path is vocabulary, not code: a
  curator-maintained attribute-synonym table that `refcat.py`'s deterministic
  field mapping consults on the next run. The curator never edits mapping
  logic; they add a synonym, and the pipeline stays deterministic and
  re-runnable.

### Runs, docs, and failure visibility

A "runs" view over `runs` + `docs_seen`:

- run history with per-run deltas (the `is_new` flag already computes "new this
  run" — surface it as the run's headline);
- per-doc status including content-hash skip ("unchanged since run N");
- **extraction failures must be loud.** Today an unparsable LLM response still
  records the doc as processed (CODEX-IDd P0.3). The prod UI treats that as a
  red failed-doc state with a re-run button — which requires fixing P0.3 first
  so failure is actually recorded as failure. UI work here is blocked on that
  fix; sequence it accordingly.
- run trigger: `POST /api/runs {folder, note}` → background job (serialized —
  SQLite single-writer), progress via polling/SSE; the claim-status funnel from
  `enrich.py status` (docs seen → claims → surfaced/parked/rejected) as the
  run-summary graphic.

---

## Cross-cutting

- **Shared record inspector** component between both UIs; same
  `GET /api/record/{rid}` endpoint.
- **Explainability over scores everywhere.** Both surfaces follow the same
  rule already in the backend design: show the inputs to a ranking (tier +
  out-of-step count; RRF sources; filter clauses kept/dropped), never just the
  output order.
- **Prompt-budget indicator** on the Q&A side (tokens used vs
  `max_context_tokens`) — context-window discipline is a stated beta goal for
  low/mid-tier models; make it visible rather than a silent constraint.
- **Config**: one `GET /api/defaults` from `ragkit.DEFAULTS` + catalogue
  snapshot; the UI never hardcodes k, filter mode, token caps, field lists, or
  model slugs.
- **Tech shape**: Flask (already in-tree) or FastAPI serving JSON + SSE; UI as
  a small SPA (or server-rendered + htmx — the payloads are simple enough).
  Nothing here needs websockets, client-side state libraries, or a build
  pipeline heavier than the team wants to maintain.
