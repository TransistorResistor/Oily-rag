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

> **Status (2026-07-03): H1–H6 implemented and committed** (`71ef02e`), verified
> by a 25-check exemplar probe, a 25-check end-to-end mini-corpus ingest
> (relations table, reverse "Fitted to", any-variant numeric filtering, cover-name
> pinning, admin demotion — all pass), and a full re-ingest + offline eval.
> Eval note: offline hit-rate is now 27/28 (was 28/28) — analytic-01's R-77
> slipped rank 4→5 in the *unfiltered* offline path after the constant
> `updatedDate:` noise line was (intentionally) removed from every record's
> first chunk; the production path applies a metadata filter to that query
> class, which the new "Operated by"/multi-variant machinery serves directly.
> Not gamed back on purpose. H7 (scale prep: dual-key category stats,
> relevance-ordered table shrink, eval cases needing v2-shaped corpus data)
> and H8's media-caption option remain open; H4(e) (budget-gated one-hop
> relation expansion) is now **implemented** — see REVIEW_FINDINGS §I3's
> "Partly done" note and `related_records()` in ragkit.py.

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

## I. Retrieval architecture review (added 2026-07-03)

Whole-pipeline architectural pass over the query path: ingest (canonical model → passage chunking → FTS5 + MiniLM embeddings + typed fields/relations/aliases) → query (filter extraction → validation → hybrid retrieve with RRF fusion → cross-encoder rerank → filter modes → entity pinning → table/digest → water-fill budgeted assembly).

**Verdict: the architecture is sound and appropriately sized.** The load-bearing decisions are right for the constraints (low–mid tier LLMs, small context windows, schema flexibility): one canonical parse with per-shape adapters; hybrid dense+sparse retrieval fused with RRF and reranked by a cross-encoder (the standard, evidence-backed stack); the propose→validate→audit-trail pattern for LLM-derived filters (the model never gets to invent fields or labels — the right trust boundary for small models); filter modes (`auto`/`fill`) that defend against spurious filters instead of trusting them; deterministic alias pinning to cover embeddings' known blind spot (exact designations); record-level filtering with passage-level retrieval and record-level citation; one global token budget with principled shrink orders; loud ingest diagnostics. Brute-force in-memory cosine is the **correct** choice at this scale — do not add a vector DB until the passage count demands it (see I7). The findings below are gaps *within* a sensible architecture, not reasons to change it.

### I1. Hard filters starve the FTS channel — fusion silently degrades to vector-only (verified)
The vector channel masks ineligible passages **before** its top-`pool` cut (`ragkit.py:1455-1462`); the keyword channel takes FTS's top-`pool` **first** and filters after (`ragkit.py:1468-1477`). Measured on the live db (hard filter on `systemType`, `pool=20`): *Air-to-Air Missile* → **1/20** FTS candidates survive (62 eligible FTS matches exist deeper in the rank); *Fighter Aircraft* → **4/20** (1,020 available). So exactly when a filter is active — the flagship use case — RRF fusion is effectively vector-only, and the keyword channel's contribution (exact designations, rare terms) is lost.
**Action:** filter FTS results *before* the pool cut: either fetch unlimited/larger `LIMIT` and filter until `pool` eligible hits are collected, or push eligibility into SQL (temp table of eligible rowids joined in the FTS query). Cheap, isolated fix in `retrieve()`.

> **Done (2026-07-04).** `retrieve()`'s keyword side now grows the FTS `LIMIT` geometrically (×4) under a hard filter until `pool` eligible hits are collected or FTS is exhausted, applying eligibility *before* the cut; the no-filter path is byte-identical (one `LIMIT pool` query). Measured on the live db: *Air-to-Air Missile* keyword survivors went **9/20 → 20/20**, *Fighter Aircraft* **18/20 → 20/20**. Eval unchanged at 27/28 (as expected — I8: the hit-rate eval can't see per-channel health, which is exactly why this bug hid).

### I2. The query orchestration is written twice and has already drifted
`ragkit.answer()` and `compare_server.build_context()` each hand-roll the same ~80-line pipeline (extract → validate → resolve mode → pin → retrieve → table/digest → assemble). They already disagree: the empty-set fallback (filter excluded everything → retry unfiltered) exists only in `answer()` (`ragkit.py:2854`); the bench has none (`compare_server.py:247-272`). Currently masked because the bench's `auto` mode can't produce an empty result, but any config change (e.g. exposing `filter_mode=hard` in the UI) surfaces it. E2 unified the *defaults*; the *logic* is still duplicated.
**Action:** extract one `run_query_pipeline(con, query, config) -> (contexts, table, digest, filter_info)` in ragkit; both entry points become thin wrappers (bench keeps its prompt-preview return shape).

### I3. Structured answering is filter-gated; pinned-entity queries can't reach it
`record_table`/`record_digest` only fire when a *validated filter* matched (`answer()`/`build_context` both gate on `clean`). But the most common query class — a named lookup ("what is the range of X?") — carries no filter: it gets exactly **one** passage for the pinned record (`max_per_parent=1`, pin injects the single best-cosine passage), and if that's a prose chunk rather than the right parameter chunk, the asked-for value simply isn't in the prompt. Same gap for comparisons: "Compare X and Y" (no filter) gets balanced passages but no table, even though both records' typed fields sit ready in `record_params`.
**Action:** when entities are pinned, (a) attach the pinned record's parameter passage when the query names a catalogue field (`_field_name_hit` infrastructure exists) — this is G4, elevated; (b) for multi-entity pins, synthesize a mini-table from `record_params` for just the pinned records (machinery exists; only the gate changes). Retrieval hit-rate eval can't see this gap — only answer-stage eval can (see I8).

> **Partly done — one-hop relation expansion shipped (commit pending, 2026-07-03).** The *cross-record* half of I3/H4e is implemented: `related_records()` expands from the pinned entities across `record_relations` (both directions — the missiles/radar a named fighter is *Fitted with*, and the platforms a named component is *Fitted to*), pulls each in-corpus neighbour's best query-matching passage **plus its parameter passage** (chunk_record isolates params into their own passage, so best-cosine alone would systematically miss the numbers a spanning question needs), and renders them as a budgeted "Related systems" block (4th water-fill section in `assemble_context`; no-related path is byte-identical). Wired symmetrically into `answer()` and `compare_server.build_context` (does not deepen I2). This is what makes "how far do the F-22's missiles reach" answerable — the linked missile record's parameters are now in context instead of only its name. **Still open:** the *same-record* half — (a) attaching a pinned record's own parameter passage on a field-named query, and (b) the multi-entity mini-table — plus I4's param-chunk grouping, which would sharpen which passage the expansion selects.

> **Done (2026-07-05) - same-record half shipped.** A field-named pinned query now appends each admitted pin's own best matching parameter passage (preferring a passage that contains the named field itself) to the ordinary passage section, so the existing global water-fill budget applies; no-pin and no-field-hit paths are byte-identical. A no-filter analytic query with two or more pins now builds `record_table` from exactly those pinned record IDs. Both paths are wired symmetrically in `answer()` and `compare_server.build_context`. Direct checks cover S-400 `Range = 400 km`, the exact F-22/F-35 two-row table, and both no-op guards; offline retrieval remains **27/28**. **Still open:** I4 parameter-chunk regrouping (a separate re-ingest change).

### I4. Parameter passages become grab-bags at production parameter counts
`chunk_record` packs `Parameter` lines into ~1,350-char chunks **in input order** (`ragkit.py:244-254`). At ~100 params/record (production shape), that's ~8–10 param chunks each mixing a dozen unrelated parameters: the embedding averages over unrelated content (weaker matching), the reranker sees 1 relevant line among 12, and a hit dumps a dozen irrelevant param lines into the prompt.
**Action:** group param lines by `component`/theme at chunking (the canonical params already carry `component`), so a chunk is "the propulsion parameters" rather than "params 13–25". Complements I3(a).

### I5. Query understanding is fragmented across independent mechanisms
Filter extraction (LLM), `is_analytic_query` (regex+aliases), `match_entities` (alias table), field selection (embeddings), and the table/digest decision each interpret the query independently and don't share what they learn — e.g. a pinned entity's `systemType` never seeds category-scoped stats for single-pass extraction (noted as unwired in `select_fields`' docstring), and intent classification is still regex-only (A7's fold-into-extraction proposal remains open).
**Action:** a small "query plan" step that runs the deterministic signals first (aliases → entities + their categories; field-name hits) and passes them into the one extraction call (which also returns intent). Deterministic bits stay authoritative; the LLM fills the rest. Detailed pathways in `query-pathways.md` (P1/P5).

### I6. Mixed score scales in the relevance vector (latent)
After rerank, `rel` holds normalized cross-encoder scores for the top-30 and normalized-RRF scores for the tail (`ragkit.py:1491,1511`), and `soft` mode adds a flat `soft_boost=0.15` on top (`:1519`) — three incomparable scales in one number. Harmless today (`min_rel=0.0` default, `soft` non-default), but the G3 floor and soft mode both gate on it, so enabling either applies thresholds across incomparable values.
**Action (when G3/soft are actually used):** floor on cross-encoder scores only (tail candidates below the rerank pool are by construction below the floor), and make `soft` a rank-based bump rather than a score addition. Document until then.

### I7. Scale cliffs and embedding provenance
Fine now; know where the edges are. (a) Passage cache: ~1.6 KB/passage for vectors + text — ~2.8k passages ≈ 5 MB today; production records are similar word-counts so growth is linear in record count; brute-force matmul stays fast to ~100k+ passages, RAM and mtime-triggered full rebuilds hit first — move to `sqlite-vec` only then (D1 unchanged). (b) `match_entities` runs one regex per alias per query (`ragkit.py:1759`) — fine at 167 aliases, sluggish at ~10k+ (curated aliases × production corpus); switch to one compiled alternation or Aho-Corasick then. (c) **The db never records which embedder built it** (`meta` stores catalogue/aliases/stats only): querying with a different `--embed-model` than ingest silently produces garbage cosine similarity. Store the model name + dim in `meta` at ingest; assert (or at least warn) at query time. Cheapest insurance in this list.

> **I7c done (2026-07-04).** `ingest()` now writes `meta['embed_model'] = {model, dim}`; `retrieve()` calls `_check_embed_model(con, embed_model)` before embedding the query, warning loudly (once per db+model pair) on a mismatch. Silent for dbs ingested before the key existed (nothing to compare) — the current `rag_test.db` has no key until re-ingest, so this is inert until the next `ingest`. I7a/I7b (alias-scan cost, sqlite-vec cliff) remain open as documented scale notes.

### I8. Eval measures too little of the machinery it protects
The offline eval scores top-k *hit-rate* only: it can't see per-channel health (I1 would have been visible as "FTS contributes 0 under filters"), rank movement (the 27/28 R-77 regression took a manual db diff to localize), whether a hit came from pinning or ranking, or whether the *right passage type* was retrieved (I3).
**Action:** extend `eval.py` with per-case diagnostics that are all cheap and offline: MRR alongside hit-rate; vector-only / FTS-only / fused ablation per case; a `pinned` flag on each hit; passage-kind (prose/param) of the hit. Gold *filter* cases for the v2 shape stay blocked on real corpus data (H7).

### I9. Minor
`_table_columns` matches field names by raw substring (`f.lower() in ql`, `ragkit.py:1820`) — "Mass" matches "massive", "Type" matches "prototype"; reuse `_field_name_hit`'s word-boundary matching.

> **Done (2026-07-04).** `_table_columns` now selects query-named columns via `_field_name_hit` (whole-word, stopword-filtered) instead of substring `in`. Verified: "how massive is the prototype" no longer pulls Mass/Type; "range and mass" still selects both.

**Recommended order:** ~~I1 (verified correctness bug, small fix)~~ **done** → ~~I7c (one-line insurance)~~ **done** → I3+I4 (the biggest answer-quality lever for the production shape; pairs with G4) → I2 (before any new query-path feature doubles the drift surface) → I8 (so the above are measurable) → I5 (larger consolidation, best done alongside the filter-generation pathways in `query-pathways.md`) → I6/~~I9~~ (**I9 done**) opportunistically.

> **Progress (2026-07-04):** I1 + I7c + I9 shipped together (top of the order + the cheap related I9). Next per the order: **I3+I4** (the answer-quality lever — same-record param-passage attach on field-named queries, multi-entity mini-table, param-chunk grouping), then **I2** (unify the twice-written pipeline before it drifts further), then **I8** (so I3/I4 become measurable — MRR, per-channel ablation, passage-kind). See §J (2026-07-04) for schema-semantics corrections that fold into this work: J1 (descr → catalogue-side) belongs in the same change as I4; J3 (LOV → categorical) is a cheap standalone.

**Filter-generation deep dive:** production data-shape sensitivities of the filter-extraction path (partition/categorical cardinality cliffs at ~50 entry types, unbounded value enumerations, compound labels, title-valued relation fields) and improvement pathways live in **`query-pathways.md`** (same folder), written alongside this review.

---

## J. Schema semantics correction: fixed `parameterDescr`, LOV dataType, PDF media (added 2026-07-04)

**Correction to the exemplar-v2 understanding (user-confirmed):** `parameterDescr` is a *fixed parameter definition defined in the schema* — it states what the parameter represents (a data-dictionary entry) and is identical for every row of that parameter, in every record. It does **not** vary per entry. Per-entry context, caveats or limitations on the supplied value are carried in `parameterSubTitle` and/or `comments`. There is also a **LOV dataType** (value drawn from a controlled list) alongside Number/Text/Date, not shown in the earlier exemplar.

The test corpus was regenerated to reflect this: corrected exemplar at `test_records/exemplar_schema_shape.json` (supersedes the root `exemplar_schema_shape.json`, which embeds per-entry text in `parameterDescr`, e.g. "Maximum thrust for Engine 1"); the four hand-written records were fixed (shared parameters now carry byte-identical definitions across records; caveats moved to `comments`; `dataType` normalised to Number/Text/Date/LOV; a `"comment"`→`"comments"` typo in AIM-120 that the adapter silently dropped was fixed) and six more records (modelIDs 1005–1010: Su-57, Eurofighter Typhoon, AIM-9X, F135 engine, AN/APG-81, S-400) were generated in the corrected shape.

### Retrieval implications (changes to recommended approaches)

- **J1. Treat `parameterDescr` as catalogue metadata, not record evidence.** Since the definition is corpus-constant per parameter: (a) store ONE definition per catalogue field at ingest and stop repeating it in every record's `to_text` param lines — identical boilerplate repeated across records inflates every param passage with zero discriminative signal and pulls different records' param-chunk embeddings *toward each other* (the opposite of what retrieval needs); emit `Parameter <name> [<qualifier>] = <value> <unit>` per line and keep the definition catalogue-side. (b) Field selection (A2/I5) strengthens for free: embed field name + canonical definition once — the definition is now trustworthy vocabulary for matching query terms to fields ("how heavy" → "Weight without fuel, payload or crew"), and never needs per-record re-embedding. (c) Table shrink stage 1 (drop per-cell descr, `ragkit.py:2138`) becomes lossless — the definition can be re-attached at answer time from the catalogue. (d) `rich_params`' first-non-null-descr collapse (`record_model.py:606`) is now provably correct rather than a lucky heuristic. Slot J1 alongside the I4 param-chunk regrouping (same code, one re-ingest).
- **J2. `qualifier` is now the sole carrier of row-specific meaning — protect it in every shrink order.** With descr fixed, everything that distinguishes variant rows (configuration, variant, estimate/caveat status) lives in `parameterSubTitle`/`comments`. Audit: any path that keeps a value but drops its qualifier now silently merges contradictory variants (the H-a failure mode reborn). `to_text` and stacked table cells already carry it; verify digest lines and any future compaction do too. Long `comments` (data limitations) should be *kept* in passage text — they are exactly the caveats a low-tier answering model must see to avoid overclaiming.
- **J3. LOV dataType → authoritative categorical.** `dtype == "LOV"` should (a) classify the catalogue field as categorical directly, bypassing the distinct/total heuristics AND the `CATEGORICAL_MAX_DISTINCT` cap (an LOV list with 80 entries is still a closed vocabulary); (b) make the field a first-rank filter/facet candidate in field selection; (c) validate LLM-proposed filter values by exact/fuzzy match against the *observed* LOV value set (A3's fuzzy mapping gets a trustworthy target list); (d) in `normalize_typed_value`, never coerce an LOV value to number/date. Cheap — `dtype` already rides on canonical params end-to-end. Caveat: we only observe the values *used* in the corpus, not the full schema list; treat the observed set as open until a schema-level LOV list is available (if the source can export it, ingest it into the catalogue directly).
- **J4. Normalise the `dataType` vocabulary at the adapter boundary** (case-insensitive: number/numeric, text, date, lov) so adapters and coercion don't fork on casing across source versions.

### J5. Future line of effort (earmarked, not scheduled): PDF media retrieval & indexing

`media[]` rows sometimes reference PDFs, identifiable by `mimeType: "application/pdf"` (the corrected exemplar and several test records now include such rows). Earmarked pipeline: at ingest, fetch PDF media (failure-tolerant, cached by mediaID), extract text per page, and chunk as passages linked to the parent record with a distinct source label (`<media title>, p.<n>`) for citation; at query time these compete in the normal hybrid retrieval pool, and for **low-context results** appear as budgeted short extracts — a PDF passage must never displace the record's own parameter passage (water-fill section ordering / per-source caps), and a 100-page PDF must not swamp its record (per-media passage cap). Prerequisites: I4 (chunk grouping) and the embed-model provenance check (I7c, done). Not started; revisit after I3/I4/I2.

---

## K. Underspecified / colloquial query handling (added 2026-07-05)

Source: live run of `.hf-cache/New Queries.txt` (7 deliberately vague questions) against a fresh ingest of `test_records/` (26 records), gemma3-4b answering, `mistral-small` filter extraction, all DEFAULTS. Scorecard: 2 good (European IR AAM → ASRAAM; US's most modern SAM → NASAMS), 2 partial (decade fighters; fighters-changed trend), 1 factually wrong (medium-range AAMs listed the S-400 and 9M96 as AAMs), 1 refusal ("best AAM" → "cannot be answered"), 1 unroutable (database-updates meta query). Findings ordered by answer impact:

### K1. Superlative/subjective queries refuse instead of surfacing candidates
"What is the best AAM?" → "This question cannot be answered from the provided context records." Retrieval was fine (AIM-120, ASRAAM, Derby in context); the strict-grounding `SYSTEM_PROMPT` gives the model no sanctioned way to answer a question with no deterministic answer, so a compliant small model refuses. The query author's expectation ("should surface a few reasonable (modern/long-range) entries") is the right target behaviour.
**Action:** detect superlative/subjective intent (`best`, `most modern/advanced/capable`, `top`) — fold into the filter-extraction call as one JSON field (same slot as A7's intent flag). On hit: (a) build the candidate mini-table for the resolved class with its discriminating fields (range, guidance, status, dates); (b) switch to a prompt variant with one extra sentence: *"If the question asks for a 'best' or 'most advanced' system, present the leading candidates from the context with their distinguishing figures and state the criteria you used; do not refuse."* Same pattern as E3's table-authority sentence: variant prompt, only when triggered.

### K2. `systemType` taxonomy can't separate AAMs from SAM interceptors — produced a factually wrong answer
`Anti-Aircraft Missile` (9 records) covers both air-to-air missiles (AIM-120, AIM-9X, ASRAAM, Python-5, Derby) and the S-300/S-400 interceptor missiles (40N6, 48N6M, 9M96, 9M96DM). "What medium-range AAMs are on the market today?" answered with the S-400 and 9M96 listed as AAMs. Aggravator: the extractor proposed `systemType = "Air-to-Air Missile"` — a *good* guess — and `validate_filter` dropped it as an unknown label (A3's exact-match brittleness observed live), leaving only the numeric range band, which the SAM interceptors also satisfy.
**Action:** (a) **derived launch-domain facet at ingest**: deterministic keyword mapping over the free-text `Type` parameter ("air-to-air" / "surface-to-air" / "within-visual-range" …) → a new categorical field (`launchDomain` or similar), reported in import diagnostics like other derivations; (b) constraint on A3's fuzzy label mapping: nearest-string would map `Air-to-Air Missile` → `Anti-Aircraft Missile` and silently re-include the SAMs — unknown labels must be matched against field *values and derived facets* (embedding or token overlap against the observed value set), not just the label list, and the mapping must be recorded in `filter_info`.

### K3. No domain date fields — decade/recency questions have nothing to filter on
"Fighters that entered service in the 90's and 2000's": the extractor invented `date_from`/`date_to` (dropped as unknown fields — correctly, but silently ending all temporal filtering). The only date fields in the catalogue are record-metadata dates (`updatedDate` etc.); service-entry dates live exclusively in prose ("entered service in December 2005"). The date logic then falls to the low-tier answering model, which included the Su-57 (2020) in a 90's/2000's list. Same root cause degrades "how have fighters changed over the last 20 years" and any future "most modern" tie-break.
**Live-schema note (2026-07-06):** in the production JSON, in-service/IOC dates (treat as identical) arrive in up to three places: (1) `proliferations[]` entries typed `"IOC Year"` with a per-country year (`{country: "USA", type: "IOC Year", proliferation: 2019}`); (2) `proliferations[]` entries typed `"Projected Fielding"` with a *relative* window (`proliferation: "0 - 5 years"`); (3) description prose ("IOC", "initial operational capability", "entered service" — format inconsistent). The current `_extract_proliferations` adapter predates typed entries: a numeric year in `proliferation` matches neither status regex (country silently dropped from the operator field), and nothing distinguishes an IOC row from a status row. Prose mining therefore demotes from primary source to fallback.
**Action (revised — tiered source precedence):**
(a) **Tier 1, structured:** extend `_extract_proliferations` to branch on the `type` discriminator (absent → current status-bucket behaviour, backward compatible):
  - `IOC Year` → parse the year (tolerate int/string/"FY2019"; unparseable → prose only) and derive a numeric **`serviceEntryYear` = earliest IOC year across countries** — the filterable scalar for decade/recency filters and "most modern" tie-breaks. Keep the per-country detail in the Proliferation prose segment ("USA — IOC 2019") so country-scoped questions have citable evidence, and count an IOC'd country as an operator (IOC reached ⇒ operating) for `Operated by (country)`.
  - `Projected Fielding` → **not** folded into `serviceEntryYear` (it's a relative window, and the system hasn't entered service). Derive a categorical `Fielding status: Projected` facet + prose retention. Crucially, in-service date filters must *exclude* projected-only records — this fixes the Su-57-in-the-90s class of error deterministically instead of trusting model date math. (Resolving "0 – 5 years" to an absolute window by anchoring to the record's `updatedDate` is possible later, with a loud provenance caveat; categorical-only is the safe first cut.)
(b) **Tier 2, prose fallback:** the original mining plan (regex + dateutil over descriptions), applied only to records with no structured IOC; widen patterns to `IOC / initial operational capability / declared operational / fielded / entered service / introduced`; lower-confidence provenance caveat in the catalogue entry. Where both tiers exist and disagree, structured wins and the disagreement goes to import diagnostics.
(c) **Semantics:** IOC is per-*country*; the scalar is defined as earliest-anywhere ("when did this system enter service" in the common sense). Country-scoped date queries ("when did the US field X?") route to the per-country prose, not the scalar; if that becomes a real query class, the home is A1(c)-style structured (field, country, year) triples — out of scope now.
(d) **Extractor side (unchanged):** give the filter extractor today's date + relative-time resolution ("last 20 years" → `min: 2006`), and advertise `serviceEntryYear` in the field spec with its aliases (entered service / IOC / fielded). Extractor guessing plausible-but-absent date fields is itself signal — echo dropped-field names into `filter_info` for the UI so the gap is visible.
(e) **Testability:** extend a few `test_records/*.json` with typed `IOC Year` / `Projected Fielding` proliferation rows (they currently have none) so the adapter change and the decade queries are gate-testable; Q1/Q5/Q6 from `.hf-cache/New Queries.txt` are the acceptance queries.
**Status: IMPLEMENTED 2026-07-06** (Codex-executed, independently verified). Field name is `serviceEntryYear` (numeric; catalogue prompt advertises aliases *entered service / IOC / fielded*). `_extract_proliferations` branches on a case-insensitive `type` key ("IOC Year" → earliest-across-countries year + country into `Operated by (country)`; "Projected Fielding" → `Fielding status: Projected` facet, never a year; untyped rows unchanged — but note rows with an *unrecognized* type are prose-only, not status-bucketed). Prose fallback is clause-scoped (keyword + 1930–2035 year in same clause, ambiguity ⇒ skip, structured wins with disagreement logged to import diagnostics). Missing-year semantics confirmed: `_passes` returns False on absent/non-numeric values, so Projected-only records fail year filters without error. `extract_filter_ex` now injects today's date + relative-time-resolution instruction. Six records tagged (S-400 2005, AIM-120 1991, ASRAAM 1998, F-22 2005, NASAMS 1997, Su-57 2020 + export-customer Projected Fielding row), consistent with each record's prose; `test_records/exemplar_schema_shape.json` documents both typed row shapes. Gates: `test_k3_dates.py` 10/10, `test_i3_context.py` 4/4, fresh ingest clean (26 records / 127 passages / 109 fields).
> ⚠️ **New finding (K3-adjacent, pre-existing): `eval_set.json` is stale.** It still targets the pre-§J corpus (T-90, F-16, modelIDs 2xxx) while `test_records/` has been the 26-record air-defence corpus (modelIDs 1001–1025) since 2026-07-04 — offline eval scores 0.0% against a fresh ingest, so the "27/28 baseline" gate has been silently dead since the regeneration. Needs a regenerated gold set for the new corpus (natural home for H7's proliferation/relation/variant cases and a vague-query §K section, incl. `serviceEntryYear` decade cases).
>
> **Addressed 2026-07-08.** `eval_set.json` is now v2 for the current `test_records/` corpus: 32 cases covering lookups, parametric questions, comparisons, analytic/filter cases, prose, negatives, relations/platform fields, proliferation/operator fields, and `serviceEntryYear`. Verified against a fresh scratch ingest (`eval_current.db`: 26 records / 127 passages / 109 fields) with a fully offline MiniLM+FTS/RRF run and `RAGKIT_DISABLE_RERANK=1`: 30 scored + 2 negative cases, 96.7% retrieval hit-rate, prompt max 1392 tokens. The sole miss is the intentionally filter-dependent cost/status query (`analytic-06`), which unfiltered retrieval does not surface without LLM metadata filtering.

### K4. Degenerate filters pass validation and inject the whole corpus as digest
"Best AAM" extraction returned `systemType IN (<all 7 labels>)` → matched 26/26 → `hard` mode → a digest roster of the *entire corpus* appended to the prompt: pure token noise carrying zero constraint.
**Action:** after validation, if `matched_records` == corpus record count (or a field constraint enumerates every known label of that field), treat as no-filter: drop it, record `"degenerate"` in `filter_info`, skip the digest. Cheap guard in `answer()`.

### K5. Domain abbreviations miss the corpus vocabulary
"AAM"/"SAM"/"IR" never appear as standalone tokens in the records ("air-to-air missile", "imaging infrared" do). Embedding-side retrieval mostly compensated, but FTS contributes nothing for these queries and filter extraction has to guess unaided.
**Action:** a small deterministic expansion table (AAM → air-to-air missile, SAM → surface-to-air missile, IR → infrared, BVR → beyond-visual-range, …) applied to the FTS query string and prepended as glossary lines to the filter-extraction prompt. Corpus-agnostic, no LLM cost. (Related: G5 multi-query expansion is the general fix; this is the free 80%.)

### K6. Country/region vocabulary is dirty in the curated corpus — A6/A3 observed in production shape
`Country of origin` values include `Europe (UK, Germany, Italy, Spain)`, `Norway; United States`, `Russia/USSR`; the catalogue carries duplicate fields (`Country of origin` vs `Country of Origin`, `Status` vs `Operational Status`) and split labels (`Fighter` vs `Fighter Aircraft` in `systemType`). "European IR-guided AAMs" succeeded only because `Proliferation region` happened to carry Europe values; "US's most modern SAM" worked only via `Operated by (country)`.
**Action:** (a) ingest-side value normalization for compound country strings (split on `;`, `/`, parenthesized member lists) so multi_value fields carry atomic labels; (b) a region→members expansion table consulted by `validate_filter` ("European" → UK/Germany/Italy/… OR the region fields); (c) fold into A6's field-name canonicalization — the curated corpus already demonstrates the drift A6 predicted for real data.

### K7. Corpus-meta queries are unroutable
"Give me a summary of recent updates to the Database" → empty filter, 4 arbitrary passages, "I don't have that information." Yet `updatedDate` is in the catalogue for all 26 records — the *data* exists; there is no route to it.
**Action:** a meta-query route ahead of retrieval: detect corpus-level intent (mentions of the database/catalogue itself, "recent updates", "how many records", "what's covered") → answer deterministically from `meta` + the catalogue (record count, `systemType` breakdown, `updatedDate` distribution, newest records), no LLM retrieval pass at all (or hand the stats block to the LLM for phrasing only). Also the honest fallback home: if intent is meta but the stat isn't tracked, say *what* the database does track.

### K8. (minor) `fill`-mode padding is unlabeled background noise
"US's most modern SAM" matched exactly 1 record (NASAMS); fill mode padded the context with S-300, F-22 and AIM-120 passages. gemma3-4b coped, but for the weakest tier the padding is indistinguishable from evidence.
**Action:** label fill-sourced passages in the prompt (e.g. "Background (not matching the filter):") or suppress fill when a hard/pinned match set is small and confident; keep the labeled form for recall safety.

**Suggested order:** K4 + K5 first (cheap guards, no re-ingest); K2(a) + K3(a) together (one derived-facet pass at ingest, one re-ingest); K1 next (prompt variant + intent flag, pairs with A7); K7 standalone any time; K6 rides A6/A3; K8 opportunistic.

---

## L. Numeric range values in the live schema — "1 to 2" values + comment-stated ranges (added 2026-07-06)

**Live-schema note (user, 2026-07-06):** parametrics rows can carry a *range* in the value field — `parameterValue: "1 to 2", uom: "m"` — identifiable only by the `to` token, but used fairly consistently in the live model. Other rows carry a nominal scalar in the value field with the range stated in `comments` using similar wording; the nominal should fall within that range.

### What happens today (verified in code)
- **L-a. The range string silently defeats numeric filtering.** `coerce_value("1 to 2", "m")` fails `float()` and the raw string rides through `typed_fields` unchanged (the existing range derivation in `norm()` is date-only and unitless-only). In `ragkit._passes` the numeric branch gets no parseable number → `return False`: a record whose stated range plainly overlaps the query bound is excluded by every hard numeric filter on that field — the same semantics as a *missing* value, with no diagnostic.
- **L-b. Stats/classification erosion.** With majority-classification the field stays numeric while clean-scalar siblings dominate, but every range-valued record contributes nothing to p5/p95/min/max. If range strings become the majority for a field, the whole field degrades out of numeric — the A1 failure mode reintroduced by a value *format*.
- **L-c. Search is already fine.** `to_text` emits `Parameter Length = 1 to 2 m (…)` so FTS/vector find it, and `comments` ride along as the param `qualifier` (embedded + shown in context). The gap is exclusively structured filtering + stats.

### Design (recommended)
- **(a) First-class numeric intervals at ingest.** In `record_model`, when the row has a unit or dataType `Number` (quantity context required — prose fields never parsed), parse `^<num>\s*to\s*<num>$` (case-insensitive, thousands-stripped; keyed on the word `to` only — hyphens stay date-range territory, avoiding negative-number/ISO-date collisions) into an interval value `{"lo": 1.0, "hi": 2.0}`. A dict, not a tuple: it must survive the JSON round-trip through the DB fields column distinguishably. Raw string kept for display (`rich_params`/table untouched).
- **(b) Interval semantics in the consumers.** `_passes`: an interval passes iff it *overlaps* the filter's [min,max] — the interval extension of H2's ANY-variant reading ("can it do 1.8 m? yes, in some configuration"). Deliberately distinct from discrete variant lists: configs [100, 120] should FAIL a 105–115 band filter, interval 100–120 should PASS — which is why intervals must not be flattened into endpoint lists. `catalogue._classify_field` + the per-systemType stats builder: an interval counts toward the numeric majority and feeds *both* endpoints into stats (fixes L-b). Canonical-unit conversion converts both endpoints.
- **(c) Comment-stated ranges.** The nominal scalar stays the primary filterable value. Mine `qualifier`/comments with a strict standalone `<num> <sep> <num> [unit]` pattern where `<sep>` is `to`, a hyphen, or an en-dash, with or without surrounding spaces (user, 2026-07-06: comments use all three). A stated unit must convert to the row's uom via `units.py` (incomparable → skip). Separator-tiered acceptance: `to` needs only the unit check; the dash forms (which collide with ISO dates, year spans like "2005-2010", and hyphenated prose) additionally require *lo ≤ nominal ≤ hi* — the user-stated invariant doubles as the acceptance gate for the riskier separators, so a stray year span on a length field can't become an interval. On a clean hit, attach the interval as an additional variant beside the nominal (H2 list) so "≥ 1.8" matches a nominal-1.5 / range-to-2.0 record under the same ANY semantics. Anything ambiguous (temporal "grew from 1 to 2", multiple ranges, unit mismatch, negative-number ambiguity like "-5-10") → nominal only, fail safe.
- **(d) Consistency diagnostic.** Nominal outside its own mined range → import diagnostics (`<field>.range_conflict`), never a guess — same pattern as K3's structured-beats-prose logging; the "nominal within range" invariant is a cheap data-QA signal.
- **(e) Earmarked, not first cut:** open-ended forms ("up to 300", "300+") as half-open intervals — same machinery; and optionally letting the *date* `parse_range` accept " to " as a separator.
- **(f) Filter-extraction prompt: no change.** The field remains an ordinary numeric catalogue entry (LLM still emits min/max); its stats simply get truthful.
- **(g) Tests** — standalone `test_ranges.py` (house style): parse variants (unit/no-unit, case, thousands separators); prose containing "to" NOT parsed ("air to air", "torpedo"); interior-band filter passes an interval but fails a discrete variant list; endpoints feed stats; comment-mining separator matrix (`to` / `-` / `–`, spaced and unspaced) + unit conversion + the dash-form nominal-containment gate (year span "2005-2010" on a length field rejected; "1-2 m" around nominal 1.5 accepted) + out-of-range diagnostic; date ranges and `serviceEntryYear` unaffected; a majority-range field classifies numeric.

**Effort:** small-moderate — one parser + three consumer touch-points (`_passes`, `_classify_field`, stats builder), re-ingest required. Test-corpus records should gain a few "N to N" and comment-range rows (mirroring live shapes, prose-consistent) so the gate exercises real ingest.

---

## M. Fine-tuned small local model for filter extraction (added 2026-07-07, design only)

**Question (user, 2026-07-07):** would it be practical to train/fine-tune a small language model to produce the metadata filter, using the information available in the database?

**Verdict: yes — the task is unusually well suited** (narrow, closed-vocabulary NL→JSON slot-filling with the legal fields/values supplied in the prompt), and the existing pipeline already provides the safety net (`validate_filter`, soft/auto filter modes, `parse_failed` surfacing) that makes a weaker-but-cheaper model low-risk: its worst case is today's parse-failure path. What it genuinely buys is **offline/airgapped operation** (slots into the pinned `.hf-cache` story like MiniLM), latency, per-query cost ≈ 0, and JSON-format reliability — *not* smarter filters: the §K failure modes (taxonomy gaps, superlatives, degenerate filters) are schema-semantics problems no filter model fixes.

### Design constraints (both load-bearing)
- **(a) Catalogue-conditioned, never schema-in-weights.** Train on `(catalogue spec, query) → filter JSON` — exactly the prompt `extract_filter_ex` already builds (ragkit.py ~2989–3010). The schema is a moving target (corpus 10→25 on 2026-07-04, K3 added `serviceEntryYear`, §L will add intervals); a model that memorizes field names goes stale on every corpus change, one that *reads the spec* generalizes and re-ingest costs nothing. Deployment is then a backend swap inside `_dispatch_filter_llm`/`llm_provider.py`; nothing downstream changes.
- **(b) Training data is constructed, not labelled — and the generator is shared with the eval-set fix.** Gold filters can be built programmatically: sample record + field(s) from the catalogue → construct the filter dict → big-LLM paraphrases it into natural questions (unit-shifted phrasings, relative dates, §K colloquial forms) → keep only pairs passing two deterministic checks: `validate_filter` accepts the gold, and applying it via `_passes` matches the source record while excluding non-matching ones. Add "no filter applies" and off-vocabulary-distractor cases. A few thousand verified triples ≈ a few dollars of paraphrase calls. **This is ~the same machinery needed to regenerate the stale `eval_set.json` (§K3 warning) — build it once, get training corpus + eval gate from one effort.**

### Staged plan (measure before training)
1. **Regenerate the eval set first** (needed regardless — offline eval has been dead since 2026-07-04). `eval.py`'s filter-accuracy stage becomes the metric every later step is judged against.
2. **Baseline cheaply:** current hosted model with `json_mode=True` (already implemented, default-off, unverified against a live provider); then an off-the-shelf small instruct model (Qwen2.5-1.5B/3B class) locally with grammar-constrained JSON decoding, few-shot. Constrained decoding alone often closes most of the gap on a task this structured — if it hits parity, stop here.
3. **Only if step 2 falls short: LoRA/QLoRA fine-tune** on the synthetic set (few GPU-hours on a 1.5–3B base; cloud if local VRAM is short). CPU inference of a small GGUF is fine for one short call per query.

**Effort:** the generator (step 1 + training corpus) is the real work and is independently justified; steps 2–3 are incremental experiments gated on measured accuracy. **Prerequisite ordering:** do this *after* §L lands, so the spec format the model is trained to read already includes interval semantics.

---

## N. Enrichment: proposing NEW records for out-of-catalogue systems (added 2026-07-07, design only)

**Question (user, 2026-07-07):** in the enrichment pipeline, how to propose new entries — within the existing schema — for systems that don't exist in the database? A Qwen-3.6-27B-class model is available; can it be done smaller?

This deliberately relaxes the pipeline's "never a new record" invariant, but only that one: report-only, verbatim-quote citation, and deterministic validation downstream of the single extraction call all stay. The invariant becomes "never an *unreviewed, single-source, prose-drafted* record".

### Where candidates come from (no new LLM call)
- **The parked pile already contains them.** Claims whose subject fails alias resolution are parked `unlinked` (pipeline.py ~687–693) and retain `entity_mention, attribute, value_raw, unit_raw, value_norm` + doc/quote provenance (state.py claims schema). Cluster parked-unlinked claims by normalized entity mention (casefold, designation normalization; named mentions only). A cluster becomes a **candidate** when it has claims from **≥2 independent docs** (the second-signal/corroboration principle reused — one brochure alone never spawns a record proposal) and ≥K mappable attributes.
- **False-novelty gate — the dangerous failure is a "new" system that's an unknown alias of an existing record** (a doc saying "T-50" when the catalogue has Su-57). Layered, all deterministic: exact alias_index miss already happened; then fuzzy match of the normalized candidate name against all titles+aliases; then MiniLM embedding similarity of the cluster's claim text vs existing record text. A strong similarity hit routes to a distinct, cheaper proposal type — **`alias_link`** ("X appears to be an alias of record Y", with quotes) — instead of `new_record`. `alias_link` has standalone value regardless.

### What a `new_record` proposal contains
- A **skeleton record JSON in the exact test_records shape**: nomenclature + observed aliases, systemGroup/systemType, proliferations, relations to *existing* records (when relation claims are present), and parametrics rows whose `parameter`/`uom`/`dataType`/`parameterDescr` are borrowed from the data dictionary via refcat's existing attribute→field mapping (§J: parameterDescr is a fixed dictionary definition — copy it, never draft it; unknown attribute names stay as parked residue attached to the candidate, they don't mint dictionary entries).
- **Every populated field cites doc + verbatim quote.** No LLM-drafted prose anywhere: any descriptions section is stitched verbatim source quotes, labeled as such. LOV fields (systemType, Country of origin, Status) must validate against the catalogue's existing LOV vocabulary.
- **Reserved candidate-ID namespace** (e.g. 9xxxx) so a candidate can never collide with or be mistaken for a live modelID. The proposal is a curator artifact; the human creates the actual record.

### Where the LLM fits — and model size (the user's question)
- The per-doc extraction call is **unchanged** (gemma-3-4b class already holds P/R gates). The only new LLM subtasks are narrow, closed-choice, and deterministically validated: (a) canonical-name selection within a cluster (which mention is the designation vs a descriptor — answer must appear verbatim in a quote), (b) systemType/systemGroup classification when no claim states it (answer must be a member of the existing LOV). Both are fine at **4–8B**; (b) may even go zero-LLM first (infer type from the mapped-attribute pattern — Wingspan+Ferry range ⇒ aircraft), LLM as fallback.
- **A 27B is a comfort upgrade, not an enabler.** The architecture keeps the model out of every decision that could fabricate a record. If the 27B is available, the highest-value place to spend it is the *existing* extraction call on noisy source PDFs (better recall/claim atomization, identical interface via llm.py) — not the new-record machinery.

### Lifecycle / state / eval
- New `proposal_type` values `new_record` and `alias_link` beside gap_fill/conflict/relation. Candidate clusters persist (new `candidates` table or a claim status) so corroboration accumulates across runs like the existing graduation pass. Fingerprint on the normalized canonical name so the rejection ledger suppresses a rejected candidate permanently. If the record later exists (curator created it / alias added), the candidate auto-retires and its claims re-enter the normal link→gap-fill path — parked claims are never consumed, only re-linkable.
- **Eval rides Phase C (brochure corpus):** gold docs about systems deliberately absent from test_records, with traps — (1) unknown-alias-of-existing (must yield `alias_link`, not `new_record`); (2) single-source system (must stay parked); (3) garbled/fictional designation (must not surface); (4) genuine new system across 2 docs with cross-unit corroboration (must surface with correct skeleton). Score skeleton fields P/R in the evaluate*.py style.

**Effort:** moderate — clustering + novelty gate + skeleton renderer + state/report additions; extraction call untouched.

---

## O. Priority tiers from entry-view data (added 2026-07-07, design only)

**Goal (user, 2026-07-07):** designate system priority tiers from database entry-view data (CSV) to rank recommendation priority for existing entries. User is happy to transform the CSV if needed — design below takes it as-is.

- **(a) Sidecar, not pipeline-DB.** A repo-root `priority.py` (importable by both enrich_demo and ragkit, like units.py) with a deterministic preprocessing step: `priority.py build views.csv → priority_tiers.json`. Flexible column detection (modelID | nomenclature/alias, view count, optional timestamp); join on modelID when present, else via the existing alias index with word-boundary matching; unmatched rows are *reported*, never guessed. No CSV transformation required from the user.
- **(b) Tiers, not raw counts.** View counts are heavy-tailed; map to a small ordinal set (default T1–T4 by quantile: top 10% / next 25% / next 40% / tail + never-viewed). A manual `tier` column in the CSV **always wins** over the computed quantile — "designate" implies explicit control; quantiles are the default fill. Recency weighting (trailing-90-day views if timestamps exist) is earmarked, not first cut.
- **(c) Consumption — report ranking.** report.py orders/sections proposals by (target record's tier, then severity: conflict > gap_fill > relation > alias_link); proposals.json rows gain a `priority_tier` key (verify evaluate*.py tolerate extra keys — expected yes). Same tiering applied to **parked-pile triage**: parked claims against T1 records list first — the parked pile is the known recall surface (FINDINGS2), tiering is what makes reviewing it affordable.
- **(d) §N interaction:** new-record candidates have no view data by definition; the analogous demand signal is cross-doc mention frequency of the cluster — report it beside the tier column so curators compare like with like.
- **(e) RAG side, later & separate:** the same priority_tiers.json *could* feed fill-mode ordering or retrieval tie-breaks, but that changes answer behavior — a separate decision, not bundled here.
- **(f) Out-of-step bump (user, 2026-07-07): discrepancy count raises priority.** View tier measures *demand*; the number of out-of-step fields measures *need* — combine them. Definition: an entry's **out-of-step count** = number of *distinct* `canon_field`s with an open (non-rejected, non-acknowledged) `conflict` proposal, plus 0.5 weight per distinct open `gap_fill` field (missing is half as urgent as wrong). Count distinct fields, not proposals — three docs disagreeing with the DB on the same field is one out-of-step field (with stronger corroboration, which the report already shows), not three. Effect is a **tier bump, not a re-score**: ≥2 weighted out-of-step fields lifts the entry one tier for ranking purposes (T2→T1 etc.), ≥5 lifts two, capped at T1; a manual CSV `tier` is a floor for demand but does *not* suppress the bump (a hand-designated T3 with six conflicts should still surface early). The report shows all three values — base tier, out-of-step count, effective rank — so the ordering is always explainable, never a buried scalar.
- **(g) Per-entry acknowledge/snooze ledger (user, 2026-07-07).** Curators need to say "seen it, not now" per *entry* once notified — today's rejection ledger only suppresses per *proposal*. Add an `entry_ack` table to enrich_state.db (state.py): `model_id, action (snooze|ignore|deprioritize), until_date NULL, reason, created_at, evidence_watermark`. CLI: `enrich.py ack <modelID> --snooze-until DATE | --ignore | --deprioritize --reason "..."`, plus `enrich.py ack --list` and `--revoke`. Semantics, in keeping with report-only philosophy:
  - **snooze** — entry drops out of the ranked section until `until_date`; **ignore** — indefinitely, until revoked; **deprioritize** — stays listed but ranks as if T4 with no bump. None of the three deletes or blocks evidence: claims are still processed, proposals still recorded, fingerprints still accrue — only *notification ranking* is damped.
  - **Auto-resurface on genuinely new evidence.** `evidence_watermark` = the set of open proposal fingerprints against that entry at ack time (the existing `_sha(...)` fingerprints — no new machinery). A later run that produces a fingerprint *not in the watermark* (a different field went out of step, or a different conflicting value appeared) resurfaces the entry with a "new since acknowledged" marker, overriding snooze/deprioritize (not ignore — ignore is explicit and holds until revoked, but the report's collapsed appendix still notes the new-evidence count so nothing is silently lost).
  - Acknowledged entries render in a collapsed "acknowledged" appendix of the report rather than vanishing — same auditability principle as the rejection ledger.
- **No LLM anywhere; effort small (a–e) + small (f–g).** Gate: standalone `test_priority.py` (column detection, alias join, quantile edges, override-wins, unmatched-row report; out-of-step counting incl. distinct-field dedup and tier-bump caps; ack semantics: snooze expiry, ignore-until-revoked, deprioritize ranking, watermark resurfacing on a novel fingerprint).

---

## P. Large-corpus online RAG run: retrieval/filter/answer failure modes (added 2026-07-08)

Source: `large-test-corpus/` (1,000 generated pages-schema records; 10 groups, 44 system types, 2,864 passages in `large_eval_test.db`) plus `large_eval_set.json` and an 8-query hosted run captured in `large_online_query_report.md`. Settings for the online run: OpenRouter answer model mostly `qwen3-14b`, filter model `mistral-small-24b`, `k=10`, `RAGKIT_DISABLE_RERANK=1` because the measured large-corpus baseline was better with RRF-only (`k=10`: 91.4% retrieval hit-rate) than cross-encoder rerank (`k=10`: 71.0%).

### P1. Exact-code parametric lookup works, but still exposes pin finalization weakness

Query: `For MX00124, what are its weight and maximum range?`

Result: answer was correct: `MX-00124 Air-to-Air-G4` weight `601 kg`, maximum range `901 km`, cited `[900124]`. `filter_info` had `pinned_parameters_count = 1`, so the same-record parameter-passage attachment did its job.

Data detail: the retrieved context list had unrelated records above the target; `900124` appeared at rank 4 as an overview passage and again as an appended parameter passage after the top-10 context list (`900124/1`). The filter model also tried `name`, which is `free_text`, so validation dropped it.

Perceived cause: entity pinning is not yet a true final-output guarantee. The target can be present somewhere in the candidate/ranked set but still not receive the best final slot ordering for a parametric query. The appended parameter passage rescued this case, but the ranked contexts show the same failure mode that made many `large_eval_set` parametric cases miss at `k=4`.

Action: change pin logic from "parent appears anywhere in ranked candidates" to "parent will survive final top-k assembly"; reserve one slot for exact-code pins that pass any hard filter, and choose the best field-bearing parameter passage for field-named queries.

### P2. Ranked analytic/table filtering is useful, but row-order preservation is weak

Query: `Which in-production missiles have maximum range over 1000 km? Rank them by maximum range.`

Result: filter extraction succeeded cleanly:

- `systemGroup = Missiles`
- `Status = In Production`
- `Maximum range >= 1000 km`
- `matched_records = 18`
- table columns included `Maximum range`

The first attempt with `qwen3-14b` returned `message.content = null` from OpenRouter and crashed in `llm_provider.chat_with_usage()` because it calls `.strip()` unguarded. Retrying the same retrieval/filter path with `gemma3-27b` produced a full answer, but the ranked list had ordering defects: `MX-00147` at `1410 km` and `MX-00189` at `1426 km` were listed after lower-range rows (`1231`-`1283 km`), and a multi-variant row (`MX-00165`, `591 km / 1635 km`) was placed last despite one variant exceeding 1,000 km.

Perceived cause: the structured table is present, but answer generation is still free-form prose synthesis. The model is asked to rank, but the table/order is not made binding enough, and multi-variant cells are hard for the model to sort correctly. Separately, `llm_provider.py` assumes hosted provider responses always carry non-null text content.

Action: sort analytic tables deterministically before prompt assembly for ranked/superlative numeric queries, add an explicit "preserve table row order" prompt instruction, and harden `llm_provider.chat_with_usage()` to surface null content/finish reason/raw provider choice instead of crashing.

### P3. Relation retrieval finds records, but relation evidence is not authoritative enough in the prompt

Queries:

- `How is SX00377 related to AX00002?`
- `What is AX00006 fitted with, and what roles do those child systems have?`

Data detail: `record_relations` contains `AX00002 -> SX00377` (`900002` fitted with child `900377`) and `AX00006` has multiple fitted child systems including `SX00337`, `WCX00667`, and `PPX00717`.

Results:

- Q3 retrieved both `900377` and `900002`, and validation produced a `Fitted to` filter matching one record, but the model answered that no direct relationship was indicated.
- Q4 answered with two child systems and plausible roles, but omitted one fitted child (`PPX00717`).

Perceived cause: relation information is available in structured state, but the final prompt does not render a compact authoritative relation table for relation-intent queries. The model is forced to infer the relation from ordinary passages/related blocks, and can miss or contradict the deterministic `record_relations` table.

Action: add a deterministic relation section when the query names two related records or asks relation verbs such as "fitted with", "carried by", "integrated with", "related to". Render parent, child, relation type, component, and both record IDs. Tell the answer model this relation block is authoritative.

### P4. Filter validation needs field-name alias/fuzzy mapping, not only value-label mapping

Queries:

- `Which sensors have detection range above 700 km and power consumption under 50 kW?`
- `Which systems operated by India have projected fielding in the next 0 to 5 years?`

Results:

- Q6: the filter model emitted lowercase `detection range` and `power consumption`; validation dropped both as unknown fields and kept only `systemGroup = Sensors`. The answer still reasoned from retrieved passages and found no valid match, but the intended numeric hard filter did not run.
- Q7 first run: the model emitted `Operated by` instead of canonical `Operated by (country)`, so the India constraint was dropped. A retry emitted the correct canonical field and produced `matched_records = 4`.

Perceived cause: A3-style fuzzy mapping exists for categorical values, but field names themselves are still exact-match only. The filter prompt can show canonical names, but lower-tier models naturally emit close aliases, casing differences, or shortened labels.

Action: add field-name normalization/remapping before `validate_filter()` rejects unknown fields. Start with case/whitespace/punctuation normalization, then explicit aliases (`Operated by` -> `Operated by (country)`, `detection range` -> `Detection range`, `power consumption` -> `Power consumption`), then conservative fuzzy matching against catalogue field names. Record mappings in `filter_info` the same way `_remapped` records value-label fixes.

### P5. Proliferation/projected-fielding route works structurally, but hosted answers can truncate

Query: `Which systems operated by India have projected fielding in the next 0 to 5 years?`

Retry result: filter extraction eventually produced the intended structure:

- `Operated by (country) contains India`
- `Fielding status contains Projected`
- `serviceEntryYear min 2026 max 2031`
- `matched_records = 4`
- table rows = 4

The generated answer listed relevant records but truncated mid-sentence (`"0`), despite the structured evidence being present.

Perceived cause: this looks like hosted-model/output handling rather than retrieval. It may be OpenRouter/model finish behavior, a max-token/default issue, or unhandled provider content shape. The current report path does not capture finish reason or raw usage, so the exact cause is opaque.

Action: have `llm_provider.chat_with_usage()` preserve finish reason and usage, and have online reports capture them. Consider setting explicit answer `max_tokens` for table-heavy responses.

### P6. Negative handling is good, but broad fallback filters still add noise

Query: `What is the range of the QZ-999 missile?`

Result: the model correctly answered that QZ-999 was not present. Filter extraction produced only `systemGroup = Missiles`; `name` was dropped as `free_text`, giving `matched_records = 100`.

Perceived cause: the system has no direct "unknown exact designation" route. With no entity pin and an unfilterable name, retrieval falls back to broad missile context. The answer model handled this case correctly, but weaker models may substitute a nearby missile.

Action: add a deterministic pre-route for unmatched exact-looking designations. If a query contains a designation-like token and `match_entities()` finds no match, surface "no matching record ID/alias found" directly or mark retrieval context as background, not evidence for the missing designation.

### P7. Large-corpus reranker result argues for bucketed/guarded reranking

Measured retrieval-only results on `large_eval_set.json`:

| Mode | k | Overall | Lookup | Parametric | Comparison | Analytic | Prose |
|---|---:|---:|---:|---:|---:|---:|---:|
| RRF only | 4 | 62.4% | 100% | 26.7% | 33.3% | 93.3% | 100% |
| RRF only | 10 | 91.4% | 100% | 83.3% | 88.9% | 93.3% | 100% |
| Cross-encoder | 4 | 57.0% | 100% | 26.7% | 38.9% | 53.3% | 100% |
| Cross-encoder | 10 | 71.0% | 100% | 43.3% | 61.1% | 80.0% | 100% |

Perceived cause: the MS MARCO MiniLM cross-encoder improves rank when it keeps the right passage, but on this homogeneous synthetic corpus it demotes some exact/structured/filter-relevant hits below top-k. The reranker is being allowed to replace recall ordering rather than act as a precision stage inside protected buckets.

Action: keep exact pins and hard-filter-eligible rows protected from cross-encoder demotion; rerank within buckets (pinned / filter-matching / background), or fuse normalized RRF and cross-encoder scores with a pin/filter bonus rather than replacing the RRF order.

---

## Q. Hosted answer-quality gates after P fixes: recurring issues (added 2026-07-08)

Sources:

- Curated/current corpus: `hosted_answer_gate_scratch_effectiveness.json`, `scratch_effectiveness.db`, `eval_set.json`, `k=4`, `qwen3-14b` answer model, `mistral-small` filter model, RRF-only. Result: retrieval 96.7%; filter precision/recall 0.65/0.60 over 8 filter cases; answer-contains pass 50% over 8 substring cases; negative handling 100%; 2 grouped-citation false positives.
- Large corpus sample 1: `hosted_answer_gate_large_k4_limit24.json`, first 24 large cases (lookup + parametric). Result: retrieval 100%; answer-contains pass 75% over 4 substring cases; no hallucinated citations.
- Large corpus sample 2: `hosted_answer_gate_large_targeted.json`, targeted comparison/analytic/negative subset. Result: retrieval 90% over 10 scored cases; filter precision/recall 0.50/0.42 over 6 filter cases; negative handling 100%; 1 grouped-citation false positive.
- Full large hosted run was attempted twice but did not complete within 15/30 minute command windows. The harness writes JSON only at the end, so no partial full-run report was available. At current latency, the full 98-case run is roughly 100+ serial hosted calls and needs batching/checkpointing before it is a routine gate.

Implementation status: Q1, Q2, Q3 scoring diagnostics/equivalences, Q4 prompt ordering/authority, and Q6 targeted/checkpointed eval controls are implemented. Remaining work is a fresh hosted gate to measure whether the answer-model behavior improves beyond the scorer/runtime fixes.

Rerun status (2026-07-08): curated hosted rerun completed via checkpoint plus a four-case continuation (`hosted_answer_gate_scratch_effectiveness_rerun.jsonl`, `hosted_answer_gate_scratch_effectiveness_rerun_remaining.json`). Combined result: 32/32 LLM cases completed; answer substring pass 6/8; negative pass 2/2; filter precision/recall about 0.70/0.875; no citation hallucinations after grouped-citation parsing. The two remaining answer failures are real: AIM-120 range still answered from an unrelated 25 km record, and AN/APG-77 detection range was still reported as unspecified.

Targeted large hosted rerun completed in `hosted_answer_gate_large_targeted_rerun.json`: retrieval hit-rate 90% (same as prior targeted run), filter precision/recall 0.92/1.00, and prompt sizes under cap. Initial negative/citation failures were scorer artifacts: bracket labels like `[Record 900203]`, `[Parameter Status]`, and `[No relevant record found...]` were over-parsed as record IDs, and "does not mention ... unknown based on context" was a valid refusal phrase missing from the keyword list. `eval.py` now ignores non-ID bracket tokens, expands refusal phrases, and treats substring-normalized categorical labels such as `F-35 Lightning II` vs `Lockheed Martin F-35 Lightning II` as value overlaps.

Follow-up implementation: exact single-record field lookups now have a deterministic direct-answer path before hosted generation. When exactly one known entity and one field intent are present, `record_params` supplies the cited value directly, preserving stacked variant rows. Field wording is aliasable through built-ins plus optional `field_aliases.json` / `RAGKIT_FIELD_ALIASES` / `ragkit.py ask --field-aliases`. Targeted eval on `parametric-01,parametric-02` now passes answer/citation checks without a hosted generation call.

Future exploration: complex questions now need a small deterministic query planner rather than more prompt tuning. Track failure modes around multi-hop relation joins (platform -> fitted system -> parameter), variant-specific filters/comparisons, arithmetic aggregates over structured rows, ambiguous field intent ("reach" vs maximum/detection range), corpus-meta negatives, conflict/provenance questions, and richer temporal expressions. Candidate shape: classify query intent into direct lookup, comparison, filtered list, aggregate, relation join, or prose fallback; execute the structured parts against `record_params`/relations; then use the LLM only for final wording when needed.

### Q1. Eval scorer is over-strict for numeric answers

Repeated pattern: answers are semantically correct but fail `expected_answer_contains` because the model formats numbers naturally.

- Curated: expected `20000` / `95000000`; model wrote `20,000` / `95,000,000`.
- Large: expected `172869426`; model wrote `172,869,426`.

Impact: false negatives in the answer-quality gate, not answer failures.

Fix: normalize numeric substrings before matching: strip thousands separators, tolerate currency/unit adjacency, and compare numeric tokens as values. Keep literal substring matching for nonnumeric expected strings. This belongs in `score_answer`, not the RAG path.

Review interaction: none of P1-P7 fixes this. F2's eval harness now needs this refinement because it is being used as a real quality gate, not just a rough smoke test.

### Q2. Citation verifier treats grouped citations as hallucinated IDs

Repeated pattern: the model cites grouped IDs such as `[1001, 1004]` or `[900401, 900403]`. The verifier parses the whole comma-separated group as one citation token and flags it as not in context.

Impact: false hallucinated-citation reports; the individual cited records were in context.

Fix: in `score_citations`, split bracket contents on commas/semicolons/whitespace after extracting the bracket group, then validate each rid independently. Keep the old behavior for single citations.

Review interaction: this is the intended G6 citation-verification path, but the parser is too literal. P2's prompt changes are not the right fix.

### Q3. Filter extraction still loses value/field semantics despite field-name remapping

Repeated pattern across curated and large:

- `Operated by (country)` and `Fielding status` are often selected as fields, but the scorer records `matched_fields = 0` because values do not line up or the model adds extra date/service fields.
- `systemGroup` is frequently omitted for class queries such as "missile records" or "sensors".
- Field aliases remain incomplete: large `Weight` vs expected `Combat weight`; relation queries may use `Fitted to` where older gold expects `Platform`.
- Nationality adjectives remain ambiguous: "Russian anti-aircraft missiles" may map to operator/proliferation instead of `Country of origin`.

Impact: answers can still be good when retrieval happens to include the right rows, but the validated filter is weaker than intended.

Fixes:

- Extend filter scoring to report value-level mismatches, not just field precision/recall.
- Add value normalization in filter scoring using the same `_map_label` logic validation uses.
- Expand field aliasing/canonicalization: `Weight` -> `Combat weight` in vehicle contexts; treat `Platform` and `Fitted to` as equivalent for relation-shaped v2 data or update the gold.
- Add deterministic query-plan hints before extraction: class nouns ("missile", "sensor", "ground vehicle") should force or strongly bias the corresponding partition field.
- Add nationality-adjective routing: default "Russian/French/US <system class>" to `Country of origin` unless the query says operated by/user/operator/service.

Review interaction: P4 is partially addressed by field-name remapping, but these failures are value semantics and field equivalence. K6 helps dirty labels at ingest, but not adjective intent. I5's query-plan step is the natural home for deterministic class/entity/nationality signals.

### Q4. Exact pinned parametric answers are better, but one curated wrong-record answer remains

Curated `parametric-01` asked for AIM-120 maximum range. Retrieval included AIM-120 first, but the model answered `25 km` from `[1021]` instead of expected `180`. Large parametric sample did not reproduce this exact wrong-record failure; its only failed answer check was the numeric-format false negative above.

Curated `parametric-02` asked for AN/APG-77 detection range. The target record was first in context, but the model said the value was not specified.

Impact: these are real answer failures, not scorer artifacts.

Fixes:

- Put same-record pinned parameter passages before background/related passages for exact field-named lookups, or render them as a distinct "Authoritative parameters for the named record" block.
- Add a prompt sentence parallel to table authority: for exact named-record parameter questions, prefer the named record's parameter block over other retrieved records.
- Inspect the AN/APG-77 prompt to verify whether the expected field is present and under what name; if absent, fix parameter passage selection or field aliasing.

Review interaction: P1's pin-finalization fix materially improved large retrieval (`k=4` large offline rose to 98.9%), but pin survival alone does not force the answer model to prefer the pinned record's parameter line. I3 same-record parameter attachment helps, but hosted results show it needs stronger authority/ordering.

### Q5. Negative handling is consistently good

Curated and targeted-large negative cases passed 100%. The large negative cases still retrieved arbitrary background records, but the model correctly refused to answer from them.

Fix: no urgent model-side change. P6's unmatched-designation route remains useful for weaker models and exact-looking unknown codes.

Review interaction: P6 is implemented in spirit for exact-looking unknown designations; broader corpus-meta negatives still need K7-style deterministic routing if they become a real query class.

### Q6. Hosted-gate runtime needs checkpointing/batching

The full large hosted gate is not operationally convenient: 98 cases produce roughly 98 answer calls plus 15 filter calls, serially, and JSON is emitted only at the end. Two full attempts timed out before a report was written.

Fixes:

- Add `--case-ids` and/or `--class` filters to `eval.py` so targeted gates do not require temporary subset files.
- Add per-case JSONL checkpointing during `run_llm_stages`, then aggregate at the end. A timeout should leave usable evidence.
- Consider bounded concurrency for hosted LLM stages after checkpointing and rate-limit handling exist.

Review interaction: this extends F2. The existing eval harness is good enough for offline retrieval, but hosted answer-quality gates need resumability before they can be routine on the large corpus.

---

## Major improvements (proposed now — recommended order)

1. **Global prompt token budgeter** (B1) — prevents the worst small-model failure (blown context) with a mechanical change to `build_prompt` and its callers.
2. **Canonical internal record model + ingest-time normalizers** (C1 + A1 + C2) — one parser, majority-type classification, date/range normalization, native lists. Fixes the flagship consistency loss (`Maximum speed` et al.) and makes new schemas one-adapter cheap.
3. **Filter-extraction hardening for small models** (A5 + A3 + A7) — schema-constrained decoding, retry-on-parse-error, fuzzy label mapping, intent flag folded into the same call.
4. **Catalogue-as-retrieval field selection** (A2 + A4) — top-K query-relevant fields with per-category numeric stats; cuts the per-query filter prompt ~3× and improves selection accuracy where it's weakest (4B–14B).
5. **Eval harness** (F2) — ~30 gold questions with expected record IDs and expected filters; a `ragkit eval` command printing retrieval hit-rate, filter precision/recall, and prompt-size stats per model. Prerequisite for tuning 1–4 without guessing.
6. **Retrieval caching + fields de-duplication** (D1 + B2) — in-memory embedding matrix and per-record fields; buys headroom to grow the corpus 10–50× without an architecture change.

Quick wins (do anytime): F1 bind localhost, E2 unify defaults, E1 single model registry, D3 truncation bug, F3 rename `catalogue.py`, F4 `.gitignore`.
