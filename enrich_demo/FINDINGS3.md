# Phase B - markdown render A/B + harder table structures

Two things landed in Phase B:

1. a **render mode** for the provider - `text` (PyMuPDF plain text, default,
   unchanged) vs `md` (markdown pipe tables via `pymupdf4llm`), plumbed through
   `enrich.py run --render {text,md}` -> `run_batch` -> `provider.iter_documents`;
2. a new **corpus 2b** (`testdocs2b/`, `gen_testdocs2b.py`, `gold2b.json`,
   `evaluate2b.py`) of harder table structures, scored in **both** modes.

Headline: **every gate is P=1.00 / R=1.00 with zero false positives**, in both
render modes. The honest surprise (below) is that **md rendering bought nothing
measurable** - the wins came from the deterministic mapping layer, and md
rendering actually *introduced* a cheap-model failure mode we had to guard.

## Score tables (all fresh state DBs, `gemma-3-4b`)

| corpus (mode) | gap_fill | conflict | relation | overall P | overall R | FP |
|---|---|---|---|---|---|---|
| Corpus 1 (text) | 6/6 | 3/3 | 1/1 | **1.00** | **1.00** (10/10) | 0 |
| Corpus 2 (text) | 4/4 | 1/1 | 1/1 | **1.00** | **1.00** (6/6) | 0 |
| Corpus 2b (text) | 2/2 | 1/1 | - | **1.00** | **1.00** (3/3) | 0 |
| Corpus 2b (md)   | 2/2 | 1/1 | - | **1.00** | **1.00** (3/3) | 0 |

Corpus 1/2 are the Phase A regression gates (text mode), re-verified **under
PyMuPDF 1.28** (upgraded from 1.21 to get `pymupdf4llm`/`find_tables` - see
Environment). Corpus 1 is scored at the pre-rejection point (batches 1+2; the
Belarus rejection in batch 3 is a separate suppression test).

### Corpus-2 table docs A/B (PART 1.4) - per-doc claim composition

Ran only the two table docs in each mode (`phaseB_text.db` / `phaseB_md.db`):

| doc | mode | surfaced | dropped (already_present) | parked |
|---|---|---|---|---|
| `table_s400_spec` | text | 2 (Deployment time 8 min *conflict*; Maximum altitude 30 km *gap*) | 3 | 0 |
| `table_s400_spec` | md   | **2 (identical)** | 3 | 0 |
| `table_missile_compare` | text | 0 | 9 | 3 unlinked (Patriot PAC-3) |
| `table_missile_compare` | md   | **0 (identical)** | 9 | 3 unlinked |

The two modes produce **byte-identical outcomes** on both docs. The attribution
trap (Patriot PAC-3 next to three catalogued missiles) holds in both.

### Token cost (prompt + completion)

| run | text | md | delta |
|---|---|---|---|
| corpus-2 table docs (2 calls) | 754 + 736 | 812 + 731 | md +58 prompt, -5 completion |
| corpus 2b (5 calls) | 1635 + 545 | 1734 + 504 | md +99 prompt, -41 completion |

md rendering costs a handful more prompt tokens (the `|`/`**`/`#` syntax) and a
few fewer completion tokens; net cost is **within noise**. A full corpus run is
still ~ $0.0002.

## What md rendering bought vs what the deterministic fixes bought

This attribution is the whole point of the A/B, so it is spelled out honestly.

**md rendering, by itself, bought nothing** on these corpora - and briefly *hurt*
recall before we guarded it. Two cheap-model quirks surfaced under `--render md`:

* **Header-as-JSON-key.** Fed a pipe table, `gemma-3-4b` mirrors the columns into
  JSON, emitting the header as a dynamic *key*:
  `{"entity":"NASAMS","Maximum Altitude (km)":"21","unit":"km","quote":"NASAMS|21"}`
  - with **no `attribute`/`value` fields**. On the first md run this parked the
  whole 5a table as `ungrounded` (R went to 0 for that doc). Fixed with a
  deterministic `_coerce_claim` adapter that reads the single unexpected key as
  attribute/value (invents nothing - the data is in the claim).
* **Pipe pollution of grounding.** `| 20 | min |` breaks unit-adjacency and makes
  ugly citation quotes. Fixed by stripping md syntax (`|`, `**`, `#`, backticks,
  separator rows) in the grounding path (`_strip_md` inside `_norm_for_ground` /
  `_sentence_with`) - a **no-op for text mode** (which contains none of those
  chars), so text behaviour is unchanged.

**The deterministic mapping-layer fixes did all the real work**, and they work in
**both** render modes:

* **Header-unit awareness (5a).** A doc-level scan (`_scan_header_field_units`)
  builds `{field: {units}}` from `"Maximum Altitude (km)"` headers. A bare cell's
  unit is *recovered* from that map (keyed on the field the claim mapped to), and
  a header-stated unit is treated as authoritative so the **unit-adjacency guard
  is skipped** - the value and its unit are non-adjacent by table construction.
  Anchored to a literal `(unit)` in the doc, so it never fabricates a unit. Plus
  `_split_attr_unit` strips a trailing `(km)`/`(km/nm)` off the attribute so it
  still maps.
* **Dual-unit handling (5b).** The cheap model **decomposes** `"37 / 20"` under a
  `(km / nm)` header into *two* claims `(37, km)` + `(20, nm)` (not one `"37/20"`
  cell). The km sibling surfaces (header-confirmed unit -> conflict 37 vs DB 50);
  the nm sibling is **parked** by a new **cross-unit incomparability guard**
  (pint is unavailable, so nm->km can't be converted -> we refuse to compare raw
  magnitudes, which would mix units). Result: **exactly one value surfaces, never
  a number paired with the wrong unit.** The single-cell `"37/20"` form is also
  handled (`_dual_unit_value` / `_pick_dual` / `_dual_consistent`) for models that
  don't decompose.
* **Two latent bugs found and fixed along the way:** (a) the old `compare_numeric`
  fell back to comparing *raw magnitudes* across unconvertible units (20 nm vs
  50 km) - a latent FP source, now guarded; (b) spelling variants (`minutes` vs
  canonical `min`) were briefly over-parked by that guard until `_unit_key`
  spelling-equivalence was added (this was the only regression caught mid-Phase-B:
  corpus-1's S-400 deploy-time conflict, now restored).

Net: **md and text converge to identical claim compositions** on every table doc
once the deterministic layer is header-unit / dual-unit / claim-shape aware.

## Per-structure outcomes (5a-5d)

| structure | anchor | outcome | how |
|---|---|---|---|
| **5a** unit in column header, bare cells | NASAMS `Maximum Altitude (km)` = 21 | **gap-fill surfaces** (both modes); `Radar Detection Range` 120 & `Deployment Time` 10 dedup/drop | header-unit scan + adjacency-skip; md's header-as-key coerced |
| **5b** dual-unit header + dual value | Derby `Maximum Range (km / nm)` = 37 / 20 | **one conflict surfaces** (37 km vs DB 50); nm sibling parked; weight 118 dedups | header-confirmed unit + cross-unit incomparability guard |
| **5c** a units column (Param \| Value \| Unit) | S-300 `Maximum Altitude` \| 27 \| km | **gap-fill surfaces**; range 200 & deploy 30 dedup/drop | value+unit associate; adjacency passes after pipe-strip |
| **5d-i** inconsistent dual (trap) | Python-5 `20 / 30` km/nm | **zero** | km sibling dedups (20 = DB); nm sibling parked unconvertible (single-cell form would hit `_dual_consistent`) |
| **5d-ii** header-unit, non-catalogued (trap) | S-350 header table | **zero** | no catalogued mention -> unlinked; header machinery does not manufacture a proposal |

Precision (0 FP) is the invariant and held on every structure, both modes.

## Surprises / tradeoffs

1. **Cheap models decompose dual-unit cells** into per-unit claims rather than
   emitting one `"A/B"` cell. The deterministic design therefore had to handle the
   *decomposed* shape (cross-unit sibling suppression), not just parse `"18/10"`.
2. **Cheap models mirror md pipe-table headers into JSON keys** (header-as-key),
   producing malformed claims. md rendering is only safe with the coercion
   adapter.
3. Once the deterministic fixes are in, **md rendering changes no outcome** on any
   table doc here - same surfaced/parked/dropped, same recall, ~same tokens.
4. The **parked pile stays the recall surface**: the nm siblings park as
   `incomplete` (visible in the report's rescue section), which is the correct
   report-only behaviour - a reviewer sees "Derby maximum range = 20 nm" without
   it being surfaced as a mixed-unit proposal.

## Recommendation

* **Keep `text` as the default render mode.** It is simpler, has no extra
  dependency, avoids the header-as-key quirk, and - with the deterministic
  header-unit / dual-unit fixes - matches md on every table structure tested.
  `--render md` stays available for corpora where a *stronger* model (less prone
  to header-as-key) or genuinely mis-flattened tables might benefit; re-run the
  A/B before switching a deployment default.
* **Keep the deterministic header-unit handling.** That is where the table wins
  actually came from, it is render-mode-agnostic, and it removed a latent
  cross-unit FP source as a bonus.

## Proposals-filename QoL fix (documented choice)

`report.py` still always writes the legacy `proposals.json` (so `evaluate.py`
keeps working unchanged), and **additionally** writes `proposals_<dbstem>.json`
whenever a non-default `--db` is used. Concurrent corpora no longer clobber each
other's eval input: run with `--db phaseB_c2_text.db`, then score with
`python evaluate2.py proposals_phaseB_c2_text.json` (both evaluators now take an
optional proposals-path argument; a bare name resolves against the demo dir).

## Environment note

`pymupdf4llm` required upgrading **PyMuPDF 1.21 -> 1.28** in the shared Anaconda
env (1.21 also lacks `page.find_tables()`, needed for the offline md fallback).
The upgrade left a harmless stale `~itz` / `-ymupdf` dist directory (cosmetic pip
warning). All Phase A regression gates were re-run under 1.28 and pass unchanged.
`pint` remains unavailable, so all cross-unit conversion is refused (which is why
the cross-unit incomparability guard is load-bearing, not cosmetic).

Total Phase B LLM spend (all iterations, incl. discarded pre-fix runs): well
under **$0.01** (~$0.001) on `gemma-3-4b`.

## Robustness fixes (2026-07-05)

Five follow-up defects were fixed without changing the extraction contract:

1. `docs_seen` now uses `content_hash` as its primary identity. `connect()`
   migrates the old `doc_id`-primary-key table in place, so text and markdown
   hashes for the same PDF coexist and either render is skipped on a later run.
2. Low-trust graduation clusters on `partial_fp`, then compares each claim's
   normalized value/unit pair. Same or convertible units must agree within 2%;
   demonstrable conflicts do not corroborate. An unconvertible cross-unit pair
   is `INDETERMINATE` and deliberately counts as support: with pint unavailable
   this gives equivalent km/ft reports the benefit of the doubt while the report
   exposes both values to the reviewer. If conversion becomes available, those
   pairs automatically resolve to `AGREE` or `CONFLICT`. NULL `park_reason`
   surfaced rows remain eligible and `unmapped` rows remain excluded.
3. `run_batch` catches hosted-LLM exceptions per document, reports and counts
   them in `runs.error_count`, leaves the failed document unseen for retry, and
   finalizes the run in `finally` while continuing with later documents.
4. `RefCatalogue.compare_numeric` now returns `incomparable` when a claim unit
   cannot be reconciled with a specific stored value's unit; the pipeline parks
   that outcome instead of comparing raw magnitudes. Spelling-equivalent units
   such as `minutes` / `min` still compare normally.
5. `_clean_value_unit` now handles a currency scale after a unit-first value:
   `USD 300 million` becomes value `300`, unit `USD million`.

Targeted offline coverage is in `test_fixes.py` (5/5 passing). An external gate
run after the first, over-strict `full_fp` implementation exposed the cross-unit
regression: Corpus 1 reached P=1.00/R=0.90 and Corpus 2 P=1.00/R=0.83 because
their km/ft corroboration pairs did not graduate; Corpus 2b remained clean at
P=1.00/R=1.00/0 FP. The pairwise AGREE/CONFLICT/INDETERMINATE refinement above
addresses that cause; all three gates were then re-run fresh and pass
(`verify2_c1.db` / `verify2_c2.db` / `verify_2b.db`, per-DB proposals files kept).

| fresh text-mode gate | model | latest status |
|---|---|---|
| Corpus 1 (batches 1+2, pre-rejection) | `gemma-3-4b` | **P=1.00 / R=1.00 (10/10) / 0 FP**, corroboration pair graduated in batch 2 |
| Corpus 2 | `gemma-3-4b` | **P=1.00 / R=1.00 (6/6) / 0 FP**, AMRAAM pair graduated in batch 2 |
| Corpus 2b | `gemma-3-4b` | **P=1.00 / R=1.00 (3/3) / 0 FP** |

## Pint restored + two bugs it uncovered (2026-07-05, later same day)

The shared Anaconda env's `pint` import was silently dying three layers down
(`pint` -> `dask.array` -> `np.round_`, removed in NumPy 2.0 -- an
`AttributeError`, not the `ImportError` pint's own guard expects, so it
propagated up and killed the whole import). Upgraded `dask` 2022.7.0 ->
2026.6.0, which uncovered the same pattern one layer further down (`dask` ->
`xarray` -> `np.unicode_`, also removed); upgraded `xarray` 2022.11.0 ->
2025.6.1. `units.convert` is no longer a guaranteed-raise; real conversion is
live. Re-ran all three gates fresh to check nothing depended on that
assumption, and it found two real bugs, both fixed same session:

1. **`nm` silently parsed as nanometer, not nautical mile.** `units.py`'s
   `_ALIASES` mapped `nmi` -> `nautical_mile` but not the bare `nm` spelling
   this domain actually uses throughout (`pipeline.py`'s own `_UNIT_SPELL`
   table treats `nm` as the canonical nautical-mile key). Pint's registry
   parses unaliased `nm` as nanometer, so `convert(20, 'nm', 'km')` silently
   returned `2e-11` instead of `~37.04` -- no exception, so nothing caught it.
   Fixed by adding `"nm": "nautical_mile"` to `_ALIASES`.
2. **Same-document unit-restatement no longer deduped.** `tbl2b_dual_unit`
   states Derby's range as two independent claims, "37 km" and "20 nm" (the
   same fact in two units). With pint truly unavailable, the cross-unit
   incomparability guard used to park the second claim as a side effect (it
   couldn't prove `nm`<->`km`, so it parked rather than compare) -- that
   guard was accidentally doing within-document dedup, not just
   correctness-guarding. With real conversion the guard no longer fires, both
   claims independently reach `compare_numeric`, and both correctly disagree
   with the DB -- producing two "conflict" proposals for one fact instead of
   one. Fixed with an explicit dedup in `process_document`: claims for the
   same `partial_fp` (record+field) whose normalized values provably `agree`
   (via `_corroboration_relation`, reusing yesterday's tolerance logic) drop
   the second as `dup_in_run`. Indeterminate (unconvertible) pairs are left
   separate on purpose -- can't prove they're the same statement, so don't
   silently merge them.

All three gates re-verified fresh after both fixes: Corpus 1 10/10, Corpus 2
6/6, Corpus 2b 3/3, all P=1.00/R=1.00/0 FP. `test_fixes.py` still 5/5. Offline
RAG retrieval eval unaffected (27/28, unrelated code path). One gold case
(`gapfill_s300_detection`) was independently confirmed to be **flaky at the
LLM-sampling layer** (gemma-3-4b sometimes emits only the child-radar-attributed
duplicate claim and drops the system-level one the gold case needs, roughly
1-in-4 samples) -- not a regression, resampled 3/3 clean before settling on the
representative run above.
