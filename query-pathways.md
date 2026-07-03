# Query pathways: filter generation under the production data shape

*Written 2026-07-03, alongside the retrieval architecture review (REVIEW_FINDINGS.md §I). Focus: can filter query generation be optimised further, and what in it is sensitive to the format/shape of production data (exemplar-v2: ~50 entry types across ~15 categories, ~100 params each with ~20 reliably populated, relations/proliferations/curated aliases — see REVIEW_FINDINGS §H).*

---

## How a query becomes a filter today

1. **Field selection** — `ragkit.select_fields`: embed the query, cosine-rank the ingest-time field-descriptor embeddings, take top-15, union the `always` set from `catalogue.partition_fields` (high-coverage, low-cardinality categorical fields). Two-pass alternative: pass 1 shows only partition fields, pass 2 recomputes in-category coverage.
2. **Spec render** — `catalogue.catalogue_to_prompt`: one line per offered field; numeric lines carry p5–p95/median (category-scoped when a single category is known — two-pass only), categorical/multi-value lines enumerate **all** known values verbatim.
3. **LLM call** — `extract_filter_ex`: fixed instruction block + spec + question → JSON; brace-matching parse, one retry feeding the parse error back; `json_mode` (`response_format: json_object`) exists but is opt-in and unverified live.
4. **Validation** — `validate_filter`: unknown fields dropped; labels mapped exact → normalized → abbreviation table → difflib (cutoff 0.85); numeric bounds unit-converted; everything audited (`_remapped`/`_converted`).
5. **Application** — `_passes` per record (any-variant semantics for value lists), filter modes `hard`/`soft`/`fill`/`auto` at retrieval.

Measured on the live 63-record db: the single-pass spec is **~2,450 tokens** (61 lines, `min_count=2`, cap 60), of which **~34% is value enumerations**. The three longest lines are all enumerations (`systemType`, `Produced`, `Country of origin`, ~100 tokens each). This is the per-query fixed cost every auto-filter question pays before retrieval even starts.

---

## Production-shape sensitivities (ordered by risk)

### S1. The partition cliff: `systemType` falls out of the partition set at ~50 values
`partition_fields` rejects any field with more than `max_cardinality=40` distinct values (`catalogue.py:316`). The current corpus has 19 systemTypes; production has **~50**. At that point the corpus's primary partition field silently vanishes from: the two-pass pass-1 spec (pass 1 can no longer categorise), `select_fields`' `always` union (systemType is only offered when the query's wording happens to echo it), and — because category-scoped numeric stats only activate when a filter names exactly one `CATEGORY_STAT_FIELD` value — the A4 stats machinery effectively never fires. Nothing warns.

### S2. The categorical→free_text cliff at 51 values
`_classify_field` stops classifying a field as categorical past `CATEGORICAL_MAX_DISTINCT=50` distinct values (`catalogue.py:35`). Production sits at ~50 entry types: **one more systemType and the field degrades to `free_text` — entirely unfilterable — silently** (the import diagnostics flag dropped *structures*, not type downgrades; REVIEW_FINDINGS C4). The same cliff threatens any curated enumeration that grows: equipment codes, organisation lists.

### S3. Unbounded value enumerations in the spec
`catalogue_to_prompt` renders every categorical/multi-value field's **full** value list, and `multi_value` classification has **no cardinality cap at all** (`catalogue.py:259-266`). The H4/H3 fields make this acute at production scale: `Fitted to`/`Fitted with` values are *record titles* (one per related system), `Operated by (country)` can approach ~100+ countries. A handful of such fields in the spec adds thousands of tokens — and enumerations are already 34% of the spec on the small corpus.

### S4. Compound labels break membership filtering
Live catalogue values include `'Germany, Italy, Spain, United Kingdom'` and `'India ; Russia'` as *single* categorical labels (Country of origin). A model emitting `"Russia"` fails exact match, normalization, the abbreviation table, and difflib at 0.85 (`"russia"` vs `"india ; russia"` ≈ 0.60) — the constraint is dropped and those records can never match a single-country filter. The v2 proliferations adapter fixed this class for operator/producer fields (real lists); origin-style fields arriving as joined strings still have it. Production strings with embedded separators (`;`, `,`, `/`) will hit this wherever an adapter doesn't split them.

### S5. Partition selection is a heuristic and already misfires
`partition_fields` currently picks `'g limits'`, `'Produced'` (year-range strings like `'1953-present'`), `'Manufactured'`, `'Designed'` as corpus partitions — coverage+cardinality alone can't distinguish "what kind of thing is this" fields from incidental low-cardinality ones. At production scale (5× catalogue), more junk qualifies, and each junk partition field costs pass-1 spec tokens and misdirects the model.

### S6. Title-valued relation fields need alias-aware value mapping
`Fitted with`/`Fitted to` values are full record titles (e.g. `"Generic Engine Model-1 (Turbo-Prop)"`). Users say "the Generic Engine" — difflib at 0.85 can't bridge that (≈0.64), so the filter drops. The ingest alias table already knows `"generic engine"` → that record; `validate_filter` just doesn't consult it.

### S7. Per-category stats are single-keyed and rarely triggered
Category-scoped numeric ranges (the fix for "long range" calibrated against 8–11,000 km) key on `systemType` only and activate only in two-pass when pass 1 produced exactly one category. Single-pass (the default) never sees them. At ~50 types × sparse params, many (type, field) cells will also be too thin (`min_count=2`) while the `systemGroup` (~15 categories) roll-up would be dense. (H7 already notes the dual-key half.)

### S8. Free-text spec lines are negative-value at scale
~90 of 205 current fields are free_text; each spec line says "NOT value-filterable" — informative once, but at a 5× production catalogue they compete for `select_fields`' top-15 slots and spec tokens while never producing a filter. `validate_filter` already rejects filters on them, so hiding them from the spec loses nothing.

### S9. Spec churn defeats provider prefix caching
Embed-ranked field selection means the spec differs per query, so OpenRouter providers with prefix caching re-process ~2.5k tokens every call (G9). The instruction block is static but sits before the varying spec — the cacheable prefix is only ~150 tokens.

---

## Pathways (ordered by value ÷ effort)

### P1. Deterministic category + entity pre-extraction (biggest win)
Build a **value-alias table for partition fields** at ingest (exactly like the entity alias table: `"air-to-air missile(s)"` → `systemType="Air-to-Air Missile"`, `"fighters"` → `"Fighter Aircraft"`, plus systemGroup and country demonyms). At query time, match deterministically before any LLM call. This:
- resolves S1 (category detection no longer depends on the model choosing from a 50-value enumeration — the enumeration can shrink to a reference);
- unlocks category-scoped numeric stats for **single-pass** extraction (the hook is already noted as unwired in `select_fields`'/`extract_filter_2pass`'s docstrings);
- lets pass-1 of two-pass be skipped entirely when the category is matched (halves LLM round-trips for the two-pass path);
- enables a **skip path**: query names exactly one entity (alias pin), no analytic/numeric cue (`_ANALYTIC_RE`, no field-name hit) → skip filter extraction altogether. Named lookups are the most common query class and currently pay the full ~2.5k-token extraction call for a filter that comes back empty.

### P2. Cap enumerations in the spec (safe by construction)
Render at most ~15 values per categorical/multi-value line, most-frequent first, with `"(+N more valid values exist)"`. **This cannot lose correctness**: `validate_filter` validates against the catalogue's full value set, not the shown subset, and the fuzzy mapper recovers close labels the model produces from its own knowledge. Directly caps S3 and shrinks the spec's largest component (34% and growing). Pair with dropping free_text lines from the spec (S8) — rejection already happens in validation.

### P3. Source-profile config instead of pure heuristics
A small per-source config (checked into the repo, one per input shape) declaring: the partition fields (fixes S5), per-field type overrides (`systemType: categorical` regardless of cardinality — fixes S2's cliff), split rules for compound labels (`Country of origin: split on [;,/]` → multi_value — fixes S4), and expected-classification assertions so the import diagnostics go LOUD when a field's inferred type changes between ingests (the C4 extension). This is the schema-flexibility story applied to the *catalogue* the way `record_model` adapters applied it to *records*.

### P4. Alias-aware value mapping in `validate_filter` (fixes S6)
For fields whose values are record titles (`Fitted with`/`Fitted to` — taggable in the catalogue entry at ingest), try the ingest alias table as a mapping tier between the abbreviation table and difflib: `"generic engine"` → rid → title → canonical value. Small, contained, reuses existing data.

### P5. Take extraction off the critical path
Two independent levers: **(a) cache** validated filters keyed by (normalized query, db mtime) — the bench replays identical queries across models constantly, and today each replay re-extracts; **(b) parallelize** — start unfiltered retrieval (cheap, cached, ~ms) concurrently with the extraction call, and re-rank/gate when the filter lands. With P1's skip path, most named lookups never wait on an LLM at all.

### P6. Structured decoding with a real schema (finishes A5a)
`json_mode` plumbing exists but sends only `{"type":"json_object"}`. Generate a proper JSON Schema from the *offered* fields (enum for categorical values post-P2-cap, number bounds for numeric) and send `response_format: {"type":"json_schema", ...}` where the provider supports it. Eliminates the parse-retry path for supporting providers. Needs one live verification per provider (blocked offline; OPENROUTER_API_KEY).

### P7. Prefix-stable prompt layout (G9)
Order the extraction prompt: static instruction block + static partition-field spec first (byte-identical across queries), query-relevant dynamic fields last, question at the end. With P2's caps the static prefix is small enough to keep fully stable; providers with prefix caching then reuse most of the prompt.

### P8. Filter-accuracy eval for the v2 shape
The eval harness has an LLM filter-extraction stage that has never run live. Add gold *filters* (not just gold rids) for proliferation ("which countries operate X"), relation ("what platforms carry Y"), variant ("thrust over N in emergency power"), and compound-label queries — blocked on v2-shaped corpus data (H7) and an API key, but the cases can be written now.

---

## Cross-reference

| Pathway | Builds on / supersedes |
|---|---|
| P1 | A4 (category stats), G1 (alias pinning pattern), the unwired hook in `select_fields` |
| P2 | A2 (catalogue-as-retrieval), corpus-snapshot token measurements |
| P3 | A6 (field aliasing config), C4 (diagnostics extension), H7 (scale prep) |
| P4 | A3 (label mapping tiers), H4 (relation fields) |
| P5 | E2 (unified pipeline — do after REVIEW_FINDINGS I2's consolidation) |
| P6 | A5(a) (schema-constrained decoding) |
| P7 | G9 (prefix caching) |
| P8 | F2/H7 (eval extension) |

**Suggested order:** P2 + S8-trim (pure token win, no behaviour risk) → P1 (biggest accuracy+latency win) → P3 (before the production corpus lands, so the cliffs never fire) → P4 → P5 → P6/P7 → P8 when live keys/corpus allow.
