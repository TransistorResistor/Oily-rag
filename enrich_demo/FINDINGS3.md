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
