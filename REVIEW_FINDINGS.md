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

## Major improvements (proposed now — recommended order)

1. **Global prompt token budgeter** (B1) — prevents the worst small-model failure (blown context) with a mechanical change to `build_prompt` and its callers.
2. **Canonical internal record model + ingest-time normalizers** (C1 + A1 + C2) — one parser, majority-type classification, date/range normalization, native lists. Fixes the flagship consistency loss (`Maximum speed` et al.) and makes new schemas one-adapter cheap.
3. **Filter-extraction hardening for small models** (A5 + A3 + A7) — schema-constrained decoding, retry-on-parse-error, fuzzy label mapping, intent flag folded into the same call.
4. **Catalogue-as-retrieval field selection** (A2 + A4) — top-K query-relevant fields with per-category numeric stats; cuts the per-query filter prompt ~3× and improves selection accuracy where it's weakest (4B–14B).
5. **Eval harness** (F2) — ~30 gold questions with expected record IDs and expected filters; a `ragkit eval` command printing retrieval hit-rate, filter precision/recall, and prompt-size stats per model. Prerequisite for tuning 1–4 without guessing.
6. **Retrieval caching + fields de-duplication** (D1 + B2) — in-memory embedding matrix and per-record fields; buys headroom to grow the corpus 10–50× without an architecture change.

Quick wins (do anytime): F1 bind localhost, E2 unify defaults, E1 single model registry, D3 truncation bug, F3 rename `catalogue.py`, F4 `.gitignore`.
