# RAG Demo → Beta: Review Findings

*Reviewed 2026-07-02 against the live corpus (`rag_test.db`: 63 records → 2,844 passages, 205 catalogue fields).*

Goal context: move from demo to prototype beta; must run well on **low–mid tier LLMs** (4B–32B), stay **schema-flexible**, filter **effectively**, and stay **inside small context windows**.

---

## Corpus snapshot (grounds most findings)

| Metric | Value |
|---|---|
| Records / passages | 63 / 2,844 |
| Catalogue fields | 205 (88 categorical, 90 free_text, 26 numeric, 1 date) |
| Fields present in exactly 1 record | 74 (36%) |
| Filter-spec prompt size (pruned: min_count=2, cap 60) | ~2,700 tokens |
| Filter-spec prompt size (full) | ~8,100 tokens |

The filter-extraction prompt alone is ~2.7k tokens **per query** even after pruning — that's the dominant fixed cost for a small model, and it grows with corpus heterogeneity.

---

## A. Filtering (highest-value area)

### A1. Mixed-type fields silently degrade to `free_text` — the single biggest consistency loss
`catalogue._classify_field` requires ≥80% of a field's values to be numeric (`catalogue.py:273`). Fields whose values are *inconsistently* typed at ingest fall below that and the **whole field** becomes unfilterable free_text — including the clean subset:

- **`Maximum speed`** (37 records): 23 are clean numeric Mach (`uom: "Mach"`), 14 (tanks/ships/helicopters) are raw strings → 62% numeric → whole field free_text. The 23 clean Mach values are lost to filtering.
- **`In service`** (30 records): range strings like `"1921–present"` — no range representation exists.
- **`Introduced`** (22), **`First flight`** (15): real dates in non-ISO formats (`"15 December 2005"`); `_is_date` only accepts `%Y-%m-%d`-style formats (`catalogue.py:189`).

**Action:** (a) classify by majority type and index the typed subset (with a coverage note in the catalogue entry so the LLM knows it's partial); (b) normalize dates at ingest (`dateutil`); (c) add a range type (`from`/`to` pair) for service-period style fields; (d) report type-degradations in the import diagnostics the same LOUD way dropped structures already are.

### A2. Catalogue bloat / needle-in-haystack field selection
205 fields for 63 records; 36% singletons. Two-pass extraction (`ragkit.extract_filter_2pass`) helps, but costs 2 LLM round-trips and pass-2 can still show 40 fields.

**Action (proposed as major improvement):** *catalogue-as-retrieval* — embed each catalogue entry (field name + type + a few values/unit) at ingest; at query time select top ~12–15 fields by similarity to the query, always including the partition fields (`systemGroup`, `systemType`, `Country of origin`, …). One LLM call, <1k-token spec, better field selection for small models. Keep two-pass as an option for very heterogeneous corpora.

### A3. Exact-match label validation is brittle for small models
`validate_filter` drops any label not exactly in the catalogue set (`ragkit.py:596–614`): a model emitting `"USA"` when the label is `"United States"` silently loses the constraint (an error string is recorded, but the filter is weakened).
**Action:** normalize (case/whitespace/punctuation) then fuzzy- or embedding-map to the nearest known label, recording the mapping in the filter audit trail like the existing `_converted` entry. Only drop when nothing is close.

### A4. Global numeric stats mislead across categories
`Length` spans 0.838–332.8 m (cartridge → carrier), `Range` 8–11,000 km. The p5/p95 shown in the filter spec are cross-category noise; a model calibrating "long range" against them will produce bad bounds.
**Action:** compute per-`systemType` numeric summaries at ingest; in two-pass (or after catalogue-retrieval field selection), present stats for the narrowed category, not global.

### A5. Filter extraction fails silently and unstructured
`extract_filter` does naive code-fence stripping + `json.loads`; **any** failure returns `{}` with no signal, no retry (`ragkit.py:1400–1403`).
**Action:** (a) use OpenRouter's `response_format: json_schema` (supported by the registry models) with a schema generated from the catalogue; (b) fallback: regex-extract the first `{...}` block, retry once feeding the parse error back; (c) surface "extraction failed" in `filter_info` so the UI/CLI can show it — currently indistinguishable from "no filter applies".

### A6. Overlapping fields dilute the filter vocabulary
`Mass`/`Weight`/`Empty weight`/`Gross weight`; `Range`/`Combat range`/`Ferry range`; `Used by`/`Primary users`; free-text `Type` (61 records) duplicating `systemType`. `NAME_ALIASES` exists but lives in the corpus-specific converter (`pages_to_schema.py:104`).
**Action:** move field aliasing/canonicalization into an ingest-side config (per-source profile), not converter code; alias at catalogue-build time so all consumers see one name.

### A7. `is_analytic_query` over-triggers
The regex (`ragkit.py:967–971`) includes `which|all|over|under|above|below|each` — "Which country designed the F-16?" gets a full structured table injected (token cost, see B1).
**Action:** fold intent classification into the filter-extraction call (one extra JSON field, zero extra round-trips) and keep the regex only as fallback.

---

## B. Context-window budgeting

### B1. Table and digest bypass the token budget entirely — biggest context risk
`max_context_tokens` (default 3,000 in the bench) caps **only the passages** inside `retrieve()`. `build_prompt` then appends:
- the structured table: up to 40 rows × 6 cols, each cell up to ~200 chars incl. description → **~10–12k tokens worst case** (`_render_table_text`, `ragkit.py:1082`);
- the digest: up to 24 × ~220 chars → ~1.5k tokens (`record_digest`, `ragkit.py:935`).

A broad filter (`systemGroup=Weapon`, ~30 matches) + analytic query can produce a prompt 4–5× the configured budget — exactly the failure mode for 4B–14B models.
**Action (proposed as major improvement):** one global budgeter in `build_prompt`/callers: allocate the budget across sections (e.g. passages 50% / table 35% / digest 15%), shrink the table by dropping the `descr` text first, then rows (keep the `+N more` marker). Cheap and mechanical.

### B2. Per-passage duplication of record-level `fields` JSON
Every passage row carries its parent's full typed-fields JSON (`ragkit.py:443–446`) — 2,844 copies of 63 records' fields. Filtering is record-level anyway.
**Action:** store fields once per record (extend `record_params`), and have `_eligible_rowids` resolve record→passages. Shrinks the DB and makes every filter scan ~45× cheaper.

---

## C. Schema flexibility

### C1. Three parallel parsers of the record shapes (consistency hazard)
`flatten_record` (`ragkit.py:182`), `extract_parametrics` (`ragkit.py:258`), and `catalogue.extract_fields/_walk` (`catalogue.py:52`) each independently reimplement the two record shapes with **different coercion rules** (e.g. `extract_fields` floats `parameterValue` only when `uom` is set; `flatten_record` never coerces). A third input shape means three edits and three chances to diverge.
**Action (proposed as major improvement):** a canonical internal record model built once at load — `{id, title, prose_sections[], params[{name, value, unit, descr}], extra_scalars{}}` — with per-source adapters producing it. Embedding text, catalogue, fields, and table all derive from that one structure. This is the main lever for "flexible DB schema": a new shape = one adapter.

### C2. Lossy multi-value handling
Scalar lists are joined with `", "` and re-split on commas (`catalogue.py:147`, `normalize_stored_fields`) — values containing commas fragment. Store lists natively (they already end up as JSON) and drop the sentinel-string round-trip.

### C3. Duplicate parameter names: last-wins
Documented FUTURE in `extract_parametrics` (`ragkit.py:268`) — per-variant values (e.g. Range per variant) are silently dropped. Cells-as-lists is the right follow-up; raise priority if variant queries matter for beta.

### C4. Import diagnostics are good — extend them
The `dropped` reporting (`catalogue._note_drop`, `import_report`) is exactly the right pattern. Extend it to cover A1 type-degradations and unit conflicts (same field ingested with two different units currently keeps the first unit seen, `catalogue.py:240`, silently).

---

## D. Retrieval & scaling

- **D1. Full scans per query:** every query loads *all* embeddings from SQLite and vstacks (`ragkit.py:803`), and `_eligible_rowids` / `count_matches` / `field_coverage` / `record_digest` each rescan all rows' fields JSON — 3–4 full scans per request. Fine at 2.8k passages; cache the embedding matrix + fields dict in memory at `warm_start` (invalidate on ingest). Move to `sqlite-vec`/FAISS only when the corpus is 50k+ passages.
- **D2. N+1 queries:** rerank and assembly fetch passage text one row at a time (`ragkit.py:850`, `903`). Batch with one `IN (...)` query.
- **D3. `_truncate_to_tokens` bug:** the truncated remainder line is appended to `kept` but then filtered out because `ordered` re-derives from original lines (`ragkit.py:1136–1140`) — the fragment is silently lost; also `l in kept` is O(n²) and conflates duplicate lines. Small fix.
- **D4. FTS query:** all terms OR'd (`fts_query_string`) — acceptable with BM25 rank; revisit only if precision issues appear (e.g. boost title matches).

## E. LLM handling (low–mid tier)

- **E1. Model lists duplicated:** `OPENROUTER_ALIASES` (`ragkit.py:1284`) vs `models_registry.MODELS` — admitted in a comment; make the registry the single source and derive aliases.
- **E2. CLI vs bench behave differently for the same question:** CLI `answer()` defaults `filter_mode="hard"`, `filter_min_count=1`, and uses the *answering* model for filter extraction; the bench uses `auto`, `min_count=2`, and a dedicated `FILTER_MODEL` (`compare_server.py:45,61`). Unify defaults in one config module and expose `--filter-mode`/`--filter-model` on the CLI.
- **E3. `SYSTEM_PROMPT`** is minimal — good. When the table is present, add one line telling the model the table is authoritative for values (small models otherwise quote passage prose that disagrees with the table).

## F. Ops / hygiene

- **F1. `app.run(host="0.0.0.0")`** (`compare_server.py:379`) exposes the bench — and your OpenRouter spend — to the LAN. Default to `127.0.0.1` with an opt-in flag.
- **F2. No eval harness or tests.** Nothing measures retrieval hit-rate or filter-extraction accuracy, so "consistency" can't be tuned or regression-checked. Proposed as a major improvement (see below).
- **F3. Module name collision:** local `catalogue.py` shadows the PyPI `catalogue` package (a spaCy/thinc dependency present in the Anaconda env) — and vice versa when run from another cwd (observed during this review). Rename to `field_catalogue.py` at beta.
- **F4. Secrets:** `key.env` sits in the project root; the folder isn't a git repo yet — add `.gitignore` (`key.env`, `*.db*`, `__pycache__`) *before* `git init`.
- **F5. Stale artifacts:** `smoke_test.db-shm/-wal` linger without their db; `files.zip` in root.

---

## G. Further retrieval/context improvements (added 2026-07-02)

Ideas beyond the original findings, specific to the retrieval/context path. G1–G3 are being implemented alongside the majors; the rest are future work.

- **G1. Entity/alias-aware retrieval (pinning).** The most common query class here is a named lookup ("What is the range of the AIM-120?"). Embeddings are the *weakest* tool for exact designations (AIM-120 vs AIM-120 AMRAAM vs "Slammer"). Build an alias table at ingest (nomenclature, title tokens, designation patterns like `F-16`, parenthesized names); at query time, deterministically match aliases and *pin* the named record's best passages into the results before similarity fill. Big consistency win for near-zero cost.
- **G2. Balanced multi-entity retrieval for comparisons.** "Compare the F-22 and F-35 radars" can currently fill all k slots from one entity. When ≥2 aliases match, split the passage budget across the matched entities so both sides of the comparison are always present.
- **G3. Relevance floor + token-aware adaptive packing.** k is fixed at 4: irrelevant tail passages get packed even when the reranker scores them near zero (noise that measurably hurts small models), and strong 5th/6th passages get dropped even when budget remains. Instead: pack passages in rerank order until the token budget or a normalized-score floor is hit, with k as a max.
- **G4. Small-to-big / sibling expansion.** After picking a prose passage, optionally attach its record's tiny parameter passage when the question is parametric (and the Overview passage for "what is X" questions). Passages stay small for matching; context gets the right neighbours.
- **G5. Multi-query expansion / HyDE.** Paraphrase the query 2–3 ways (or generate a hypothetical answer) with a cheap model and union the retrievals. Helps vocabulary-mismatch queries; costs a round-trip — gate it on low top-score from the first retrieval rather than always-on.
- **G6. Citation verification.** Post-generation, regex the `[rid]` citations and flag any not present in the prompt (hallucinated citations are the classic small-model failure). Cheap, surfaces trust info in the UI.
- **G7. Embedder upgrade path.** MiniLM-L6 (2021) is the floor now; bge-small-en-v1.5 or nomic-embed-text are drop-in-sized and stronger. Constraint: docstring says the 384-dim space is kept compatible with Clipper — so treat as an A/B experiment via the eval harness, not a default swap.
- **G8. FTS title weighting.** Index title as a separate FTS column with a higher bm25 weight so designation keyword hits rank above body mentions (complements G1).
- **G9. Prompt-prefix ordering for provider caching.** Keep static content (system prompt, filter spec) byte-identical and leading so OpenRouter providers that support prefix caching can reuse it across queries; put the query last (already the case in the filter prompt).

---

## H. Exemplar v2 input shape — readiness + plan (added 2026-07-03)

Assessed against a truncated exemplar of the real production shape (verified empirically by running it through `record_model.normalize_record` + all views). Typical real record: 5k–20k words of JSON (1–2k prose, rest parametrics), ~50 entry types across ~15 categories (`systemType`/`systemGroup`), up to ~100 parameters each with only ~20 reliably populated corpus-wide. Text fields carry HTML entities (`&#x27;`, `&amp;`, …).

### What works today, unchanged
- Shape detection picks the pages_schema adapter (`modelID`/`nomenclature`/`parametrics` markers all present); id/title, prose sections, media pass-through, and plain scalar extras (`systemGroup`, `systemType`, `primaryEquipCode`) all land correctly.
- The import diagnostics correctly go LOUD on every structure it can't index: `aliases`, `codes`, `trigraphLists`, `proliferations`, `relations` are each flagged "needs an adapter" — the honesty mechanism works; the adapters just don't exist yet.
- Sparse population (~20 of ~100 params reliable) is exactly what the majority-type + `partial` flag + coverage pruning machinery was built for.

### What's wrong or lossy (verified)
- **H-a. Duplicate params: the `comments` disambiguator is dropped and last-wins destroys data (C3, now live).** Two `Max Value` rows (100 = "Standard configuration", 120 = "Overclocked/Emergency") → filter/table keep only 120; `to_text` emits BOTH lines with no distinguishing comment — contradictory prose for the answering model. `comments`, `parameterSubTitle`, `component`, `componentDescr`, `dataType`, `parameterOnly` are all silently ignored by `_extract_parametrics`.
- **H-b. Subtitled params fragment the catalogue.** `parameter` arrives pre-suffixed ("Wing Sweep - 1"/"- 2", "Engine Thrust - 1"/"- 2") → each variant becomes its own catalogue field. At 100 params × subtitle variants × 50 entry types this explodes the field space that A2/field-selection just shrank, and subtitle semantics ("Max Emergency Power" vs "Typical Operation") are lost.
- **H-c. Relationship rows masquerade as params.** Parametric rows carrying `childModelID` (e.g. "Relationship Classification" = "Component A Model") become free-text fields; multiple children collapse last-wins. The `relations[]` list is dropped entirely.
- **H-d. Proliferations dropped.** Only the flattened `countryList`/`regionList` strings survive (as free-text extras) — the Using/Production distinction, organizations, and trigraphs are lost. "Which countries operate X" is answerable only via a weak FTS line; "what fighters does Country A operate" not at all.
- **H-e. Curated aliases dropped.** `aliases[]` (Common/Project/**Cover** names) never reach the entity-pinning table — verified: `build_alias_table` sees titles only. Cover names have zero semantic similarity to the system name; this is precisely where embeddings fail and pinning was built to win.
- **H-f. Admin metadata pollutes retrieval.** `releaseID`, `reviewDate`/`createdDate`/`updatedDate`/`versionDate` (4 date fields at 100% coverage — they will dominate coverage-ranked field lists and confuse "when was X introduced"-type extraction), `productLink` URL in embedding text, `aliasList`/`name` duplicates.
- **H-g. HTML entities survive un-decoded** into embedding text, FTS, and prompts (`&#x27;` verified in prose output). Nothing in the pipeline unescapes.
- **H-h. Minor:** empty lists (`nomenclatureMarkings`, `parametricUrls`) are flagged as "mixed/nested list" noise in diagnostics; `media` now carries `caption`/`mimeType` (still fine as pass-through).

### Plan (recommended order)
1. **H1. Decode HTML entities at the adapter boundary.** `html.unescape()` applied once to every string field in `normalize_record` (both adapters) — descriptions, parameter values/descrs/comments, captions, titles. Single pass only (no loop — avoids over-decoding `&amp;amp;`). Trivial, do first; re-ingest required.
2. **H2. Real parameter identity (fixes C3 + H-a/b/c).** Canonical params gain `base` (= `parameterOnly`, fallback: `parameter` with a trailing `" - N"` stripped), `qualifier` (= `parameterSubTitle` or `comments`), `component`, `component_descr`, `dtype` (= `dataType`, authoritative for coercion when present). Catalogue field name = `base` (qualify with component only on cross-component collision). `typed_fields` stores multi-valued numerics as value lists — a numeric filter matches if ANY variant matches; stats use per-record min/max. `to_text` renders `Parameter Engine Thrust [Max Emergency Power] = 120 kN (…)`; `rich_params`/table keep one row per variant with the qualifier merged into the name. Rows with `childModelID` are routed to relations (H4), not params.
3. **H3. Proliferations adapter.** Derive multi-value filter fields — `Operated by (country)` (status ∈ Using-like), `Produced by (country)` (Production-like), `Region` — plus one prose section per record ("Proliferation: Country A — Using (Mil Org A); Country B — Production (Manufacturer A)") so organizations/status stay searchable and citable. Supersedes `countryList`/`regionList` (keep them only as fallback when `proliferations` is absent). Status vocabulary must be confirmed against real data (Ordered/Development/Retired?).
4. **H5. Curated aliases + equipment codes into pinning.** Feed `aliases[]` (all types, cover names especially) and `codes[]`/`primaryEquipCode` into `build_alias_table` alongside the title-derived aliases. Biggest single consistency win available for named lookups on the real corpus.
5. **H4. Relations as a first-class edge store.** (a) `record_relations` table at ingest: (rid, direction, related_model_id, related_name, component, relation_type, related_system_group/type, equip_code) — from `relations[]` AND `childModelID` parametric rows. (b) A "Related systems" prose section ("Propulsion: Generic Engine Model-1 (Turbo-Prop)") so the edge itself is retrievable/citable even when the child record isn't in the corpus. (c) Multi-value filter fields `Fitted with` / `Fitted to` (reverse edge when both endpoints are ingested) — makes "which platforms use engine Y" a plain categorical filter, no join logic for the LLM. (d) Child/parent model names → alias table entries pointing at both endpoint rids (pinning surfaces the platform when the engine is named, and vice versa). (e) G4 hook: for comparison/analytic queries with leftover budget, append digest lines for one-hop in-corpus relatives.
6. **H6. Admin-metadata demotion.** An `admin: true` flag in the catalogue for `releaseID`, the four audit dates, `productLink`, `aliasList`, `name`: stored and queryable on request, but excluded from `to_text`, from `field_embeddings`/`select_fields`, and from the default catalogue prompt.
7. **H7. Scale prep + eval extension.** Default `field_select` ON (catalogue will be ~5× larger); per-category stats keyed on both `systemGroup` and `systemType`; when shrinking the table, drop rows by field-selection relevance rather than input order; add eval cases for proliferation, relation, and variant-qualifier queries; re-measure filter-prompt tokens at real-corpus scale.
8. **H8. Diagnostics polish.** Ignore empty lists; optionally append media `caption`/`title` to prose.

All of H1–H6 land in `record_model.py` adapters plus small query-side extensions (multi-value numeric matching, the relations table) — the canonical-model refactor was built exactly so this class of change stays one-file-per-concern.

---

## Major improvements (proposed now — recommended order)

1. **Global prompt token budgeter** (B1) — prevents the worst small-model failure (blown context) with a mechanical change to `build_prompt` and its callers.
2. **Canonical internal record model + ingest-time normalizers** (C1 + A1 + C2) — one parser, majority-type classification, date/range normalization, native lists. Fixes the flagship consistency loss (`Maximum speed` et al.) and makes new schemas one-adapter cheap.
3. **Filter-extraction hardening for small models** (A5 + A3 + A7) — schema-constrained decoding, retry-on-parse-error, fuzzy label mapping, intent flag folded into the same call.
4. **Catalogue-as-retrieval field selection** (A2 + A4) — top-K query-relevant fields with per-category numeric stats; cuts the per-query filter prompt ~3× and improves selection accuracy where it's weakest (4B–14B).
5. **Eval harness** (F2) — ~30 gold questions with expected record IDs and expected filters; a `ragkit eval` command printing retrieval hit-rate, filter precision/recall, and prompt-size stats per model. Prerequisite for tuning 1–4 without guessing.
6. **Retrieval caching + fields de-duplication** (D1 + B2) — in-memory embedding matrix and per-record fields; buys headroom to grow the corpus 10–50× without an architecture change.

Quick wins (do anytime): F1 bind localhost, E2 unify defaults, E1 single model registry, D3 truncation bug, F3 rename `catalogue.py`, F4 `.gitignore`.
