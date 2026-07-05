# Reverse-enrichment demo — Corpus 2 (harder) findings

A second, deliberately harder PDF corpus (`testdocs2/`, 12 docs, `gen_testdocs2.py`,
gold `gold2.json`) built to stress three axes the first corpus did not: **tables**
(spec sheet + multi-system comparison matrix), **red herrings** (commercial /
schedule / mis-unit numbers next to a catalogued system), and a **prose integration
press release** (a new relation edge). Same cheap model (`gemma3-4b`), fresh state
DB `ab2_4b.db`, two batches + graduation.

## Headline

| corpus | precision | recall |
|---|---|---|
| Corpus 1 (original) | 1.00 | 1.00 |
| **Corpus 2 (harder)** | **1.00** | **0.17** (1 / 6) |

The contrast is the point. Precision held perfectly — **zero false positives**: every
red herring, the attribution-trap table, and both distractors produced nothing. But
recall collapsed to 0.17, and **every miss was in the deterministic mapping layer, not
the model.** The cheap model read the tables and prose correctly (it extracted
"deploy time = 8 min", "engagement ceiling = 30 km", "operator = Kuwait", "F-35 can
carry ASRAAM"); the pipeline then failed to map those correct claims onto fields.

This also recalibrates Corpus 1: its 1.00/1.00 was partly because its gold labels
happened to use inputs the guards already handle (operator countries already in the
catalogue, canonical un-abbreviated attribute names, exactly-listed relation verbs).

## What worked (verified positives)

1. **Attribution trap held perfectly.** The multi-system comparison table
   (`table_missile_compare`) put a non-catalogued *Patriot PAC-3* row next to three
   catalogued missiles. All three Patriot values (35 km / 316 kg / Mach 5) parked as
   `unlinked`; **none bled into a neighbouring catalogued record.** Zero leakage.
2. **Near-identical numeric dedup works** (the user's core "should match" ask):
   S-400 "402 km" (vs DB 400), 48N6M "250 km", 9M96 "120 km", AIM-120 "180 km" all
   → `dropped: already_present` via the 2% tolerance. Near-identical values correctly
   collapse instead of surfacing as spurious conflicts.
3. **Red herrings produced nothing.** `redherring_procurement` (contract value $2.5 B,
   40 launchers, 8 months, 1,200 personnel) and `redherring_units` (200 personnel /
   250-hour readiness / 300 sorties, whose *numbers* collide with S-300's 200 km range
   and 300 km detection range) both surfaced zero — commercial/administrative numbers
   and mis-unit numbers were not mistaken for parametrics.
4. **Corroboration graduation works across units.** AIM-120 "maximum altitude" parked
   `uncorroborated` in batch 1 (low-trust single source), then graduated in batch 2
   when a second low-trust source stated it as "~65,000 ft" (≈19.8 km). The two
   surface as sibling proposals sharing a partial fingerprint. This was the **only**
   expected proposal that surfaced — and it did so correctly.

## Unforeseen issues (the valuable part)

1. **Novel operator countries are invisible.** *Biggest finding.* The operator/
   proliferation path only recognises a new operator if that country **already operates
   something else in the catalogue** (`rc.countries` is a closed set built from
   existing proliferations). Kuwait and Algeria — genuinely new operators, exactly the
   kind of fact this tool exists to catch — are not in that set, so
   `gapfill_typhoon_operator` (Kuwait) and `gapfill_su57_operator` (Algeria) both
   parked as `unmapped`. Corpus 1 dodged this by only using Belarus/Vietnam/Poland,
   which already appear elsewhere. **Fix:** detect operator *values* with a general
   place-name/GPE recogniser (or accept the LLM's country slot directly after
   validation), not membership in the existing-operator lexicon.

2. **Abbreviated table headers defeat exact-match synonym mapping.** PyMuPDF flattens
   the spec table faithfully, but the *headers are abbreviated*: "Deploy Time",
   "Engagement Ceiling", "Radar Det. Range", "Simult. Targets Engaged". `map_attribute`
   is exact/prefix-based, so all four → `None` → `unmapped`, even though
   "deployment time" → OK, "ceiling" → OK, "radar detection range" → OK. This alone
   killed the S-400 conflict (deploy time 8 min vs 5) **and** the S-400 gap-fill
   (engagement ceiling 30 km). **Fix:** normalise abbreviations ("det." → "detection",
   "simult." → "simultaneous") and/or fuzzy/token-overlap attribute matching.

3. **Relation-verb substring matcher over-triggers on parameter names.** `classify_claim`
   routes any attribute containing `launch` (etc.) to the relation branch — so
   **"launch weight" is misclassified as a relation**, not the `Weight` parametric,
   and parks as `unmapped`. Side effect: the near-identical *weight* dedup (1805 vs
   1800 kg) was never even tested — the claim was hijacked before reaching the numeric
   comparator. Any parameter name embedding a motion verb ("launch weight", "firing
   range") is vulnerable.

4. **Unit-before-value ordering defeats the unit-adjacency guard.** "Mach 3" / "Mach 4"
   (unit precedes number) fail `_unit_adjacent` (which scans for `<number><unit>`), so
   the unit is stripped and the value parks as `incomplete`. The Maximum-speed dedup
   (Mach 3 = DB) never fired. Same failure mode would hit "$5M", "USD 300 million".

5. **Relation extraction is fragile when one sentence names both endpoints.** For
   `integration_f35_asraam` the model emitted "F-35 / can carry / external wing
   stations" — the partner missile (ASRAAM) landed in neither `entity` nor `value` but
   only in the quote, and record-linking attached the claim to *ASRAAM* as the subject.
   The relation branch then couldn't find a *distinct* second record and parked it. So
   even with the verb recognised, a richly-worded integration announcement (the
   realistic case) doesn't cleanly yield the edge. **Fix:** for relation claims, run
   record-linking over the *whole quote* and propose the edge between the two
   highest-confidence catalogued mentions, rather than trusting entity/value slots.

## Does a bigger model help here?

No — and cheaply confirmable without spending: the failures are model-independent. The
cheap model already produced the *correct* claims; issues 1–4 are pure deterministic-
layer bugs a larger model would hit identically. Only issue 5 (relation slotting) might
improve with a stronger model, but the fix belongs in the linker, not the prompt.

## Takeaway

The pipeline is strongly **precision-biased**: it will not emit garbage (0 FP across
two corpora, including adversarial red herrings and an attribution trap), but on
realistic inputs it silently drops a large fraction of *real* facts into the
`unmapped` / `incomplete` parked pile. For a report-only human-review tool this reframes
the parked pile from "leftovers" to **the primary recall surface** — the weekly review
UI must expose parked `unmapped`/`incomplete` fragments (grouped by record) for a human
to rescue, and the highest-leverage engineering is generalising the mapping layer
(GPE-based operator detection, abbreviation/fuzzy attribute mapping, verb-name
disambiguation, unit-order-agnostic parsing, quote-scoped relation linking) — not a
bigger LLM.

## Phase A fixes (2026-07-05)

Implemented the six deterministic mapping-layer fixes the corpus-2 eval pointed at.
All are in `refcat.py` (mapping/lexicon), `pipeline.py` (classification) and
`report.py` (rescue surface). No change to the one-call-per-doc structure or the
claims-only contract.

| corpus | precision (before→after) | recall (before→after) |
|---|---|---|
| Corpus 1 (regression) | 1.00 → **1.00** | 1.00 → **1.00** (10/10) |
| Corpus 2 (harder) | 1.00 → **1.00** | **0.17 → 1.00 (6/6)** |

Both eval gates pass with **zero false positives** on either corpus: the red
herrings, the Patriot PAC-3 attribution-trap table, `unlinked_s350`, and both
distractors still surface nothing. Corpus-1 suppression (reject Belarus → rerun
does not resurface) and corpus-2 suppression (reject Kuwait → `rerun_typhoon_kuwait`
does not resurface) both still hold. The corroboration pair still parks in batch 1
and graduates in batch 2 (2 claims graduated).

### Per-issue outcome

1. **Relation-verb hijack — FIXED.** `classify_claim` now resolves the parametric
   field FIRST; the relation branch is entered only when the attribute maps to no
   field, and verb detection moved from bare-substring to a word-boundary stem
   regex (`_RELATION_VERB_RE`). "launch weight" → Weight, "firing range" →
   Maximum range (both now reach the numeric comparator instead of parking).

2. **Novel operator countries — FIXED.** Added a static ~190-entry `WORLD_COUNTRIES`
   list (no deps). Used ONLY on the operator branch and ONLY when the country name
   appears literally in the claim's *quote* (string-level grounding). The
   second-signal precision gate (`DocContext.second_signal` / `_country_in` against
   per-record `rc.operators`) was left untouched. Kuwait (Typhoon) and Algeria
   (Su-57) now gap-fill.

3. **Abbreviated table headers — FIXED (conservative).** `map_attribute` gained
   (a) token-wise abbreviation expansion (`det.`→detection, `simult.`→simultaneous,
   `deploy`→deployment, `max.`→maximum, …) with an exact re-match, then (b)
   token-subset matching. "Deploy Time", "Radar Det. Range", "Simult. Targets
   Engaged", "Max. Range" resolve via expansion; "Engagement Ceiling" resolves via
   token-subset (SYNONYMS key `ceiling`). Precision guard: token-subset accepts only
   if exactly one distinct field wins, and canonical-field subset is restricted to
   **multi-token** field names — a first draft matched the single-token field `Crew`
   from "crew rotation cycle" and manufactured an S-300 gap-fill FP; the multi-token
   guard removes that class of leak (verified: the redherring now parks). No
   edit-distance fuzzy matching was added (token-level only).

4. **Unit-before-value ordering — FIXED.** `_clean_value_unit` and `_unit_adjacent`
   now accept `<unit><number>` as well as `<number><unit>`. "Mach 3" splits to
   value 3 / unit Mach and grounds against the doc's "Mach 3"; with pint
   unavailable in this env the Maximum-speed dedup runs on same-unit ("mach")
   comparison and correctly collapses 9M96 "Mach 3" as `already_present`. The
   `<number><unit>` unit-hallucination guard is preserved (the readiness-drill
   red herring's "12-hour cycle" still fails adjacency and parks). "USD 300 million"
   deliberately does *not* split (trailing "million") and stays parked — that keeps
   the procurement red herring silent, which is the desired precision behaviour.

5. **Quote-scoped relation linking — FIXED deterministically.** The relation branch
   now runs `find_mentions` over the quote + grounding sentence and proposes the
   edge between the two highest-confidence **distinct** catalogued mentions
   (`_relation_endpoints`), not the model's mis-slotted entity/value. Precision
   guards: both endpoints must be records mentioned in the doc; ambiguous-alias-only
   mentions (`AMBIGUOUS_ALIASES`) cannot be an endpoint; >2 tied endpoints → park.
   The subject (and thus the proposal's record + fingerprint) is the endpoint
   mentioned first in the doc, so the two edge directions dedup to one canonical
   proposal. F-35 ↔ ASRAAM now surfaces with `record=F-35` even though the model
   emitted "F-35 / can carry / external wing stations" (ASRAAM only in the quote).

6. **Report rescue surface — DONE.** `report.py` now emits a "Parked for review
   (rescue candidates)" section grouping `unmapped`/`incomplete` fragments BY RECORD
   (raw attribute + value + quote), with unlinked fragments in their own section —
   turning the parked pile into a reviewable recall surface.

### Model-tier A/B

**Not run — not warranted.** Issue #5 was the only plausible LLM-tier candidate, but
the deterministic quote-scoped linker reached recall 1.00 on corpus 2 (relation
included) with the cheap `gemma-3-4b`, confirming the original finding that these
failures are model-independent mapping-layer bugs. Spending on a mid-tier A/B would
not have changed a passing gate, so the deterministic fix stands as the default.

### Spend

All Phase A eval runs (corpus 1: 3 batches / 15 LLM calls; corpus 2: 3 batches /
12 LLM calls) used `gemma-3-4b` for a combined ≈9.2k prompt + ≈5.1k completion
tokens — well under **$0.001 total**.
