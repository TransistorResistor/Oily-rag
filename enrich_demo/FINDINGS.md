# Reverse-enrichment demo — findings

*Extraction model: `google/gemma-3-4b-it` (cheapest registry model). Reference
catalogue: the 25 air-defence records in `../test_records/`. 15 test PDFs across
2 batches + 1 recurrence doc. Total LLM usage: **15 calls, 4,893 prompt + 2,288
completion = 7,181 tokens, ≈ $0.0002.** One LLM call per document.*

---

## (a) Evaluation scores

Scored `proposals.json` (after both batches) against `gold.json` (`evaluate.py`):

```
type        TP  FP  FN  precision   recall
gap_fill     6   0   0       1.00     1.00
conflict     3   0   0       1.00     1.00
relation     1   0   0       1.00     1.00
--------------------------------------------------------------
OVERALL     10   0   0       1.00     1.00
```

- **Distractor / unlinked check:** 0 proposals traceable to `distractor_python_lang`,
  `distractor_derby_horse`, or `unlinked_patriot`. The entity-linking gate held.
- **Corroboration:** the S-400 "Maximum altitude" claim was **parked** in batch 1
  (single low-trust source) and **graduated** in batch 2 when the second source
  arrived — via a pure-SQL pass, no LLM, no doc re-read.
- **Incrementality:** batch 2 hash-skipped all 11 batch-1 docs (0 LLM calls for them).
- **Suppression:** after `reject`-ing the Belarus gap-fill, a new doc restating it
  produced **0** new proposals and the claim landed in "**Seen again x2**".
- **Hedged/partial:** the `hedged_s300` conflict surfaced **carrying its qualifier**
  (`up to`); the deliberately unitless value ("...listed simply as 5...") **parked as
  `incomplete`** instead of surfacing.

### Important honesty caveat about that 1.00/1.00
These are clean scores **on a 15-doc gold set that I wrote**, and — more
importantly — **after** several defensive guards were added in response to real
failures seen while building (below). The *first* end-to-end run produced a
genuine false positive (a fabricated-unit conflict, Issue 4) and two free-text
paraphrase false positives (Issue 8). The headline number is "the design can hit
1.00/1.00 on a cheap model," **not** "a cheap model does this out of the box." The
LLM stage is also non-deterministic (Issue 13), so an individual run can drop a
claim to recall < 1.0; the deterministic layers are what make the *precision*
robust.

### Per-type notes
- **conflict (3/3):** S-400 deployment 8 min vs DB 5 min; Python-5 range 35 km vs
  20 km; S-300 range 250 km (hedged) vs 200 km. All show DB-vs-doc side by side.
- **gap_fill (6/6):** S-400 operator Belarus; Python-5 operator Vietnam; SPYDER
  operator Poland; S-400 alias `40R6`; S-300 `Detection range` 300 km; S-400
  `Maximum altitude` (corroborated, 2 sources, distribution `30 km` / `98,000 ft`).
- **relation (1/1):** F-35 <-> AIM-9X Sidewinder (both records exist; edge absent
  from the DB; the F-35 record already had the AIM-120 edge, not this one).

---

## (b) Effectiveness verdict

**The architecture works, and the division of labour is the reason.** The cheap
model is used only as a fuzzy claim spotter; *every* decision that can create a
false proposal — is this really the S-400 or the word "python"? does the value
appear in the text? does 8 min contradict 5 min? has this been rejected before? —
is deterministic code. That is what lets a 4B model reach production-grade
precision here: its mistakes are absorbed by guards rather than published.

Concretely, the parts that carried the demo were **not** the LLM:
1. the **second-signal entity-linking gate** (killed both distractors and the
   Patriot doc with zero tuning of the model);
2. **quote-grounding + a unit-adjacency guard** (killed hallucinated values/units);
3. **fingerprints + a decisions ledger** (dedup, corroboration, suppression);
4. **numeric-only scoping** of parametric proposals (killed free-text paraphrase noise).

If I had trusted the model's output structure and done mapping "in the prompt,"
precision would have been far below 1.0 (the pre-guard runs prove it). The value
of the design is that it is **defensive by construction**.

**Where it is weakest:** recall depends on the model phrasing an attribute in a way
the deterministic synonym table recognises, and on the model not mangling the
claim shape; and the *type* of a proposal (gap-fill vs conflict) can flip on the
model's word choice (Issue 6). This is a genuinely cheap pipeline that is **safe
but phrasing-sensitive**.

---

## (c) Unforeseen issues and tradeoffs

Ordered roughly by how much they bit. Most were discovered by watching real
`gemma-3-4b` output, not by design review.

### 1. The reference DB on disk was the wrong corpus (data/plumbing)
`rag_test.db` is a **stale 63-record Wikipedia corpus** (F-22, M1 Abrams,
Tomahawk...), not the air-defence records the task describes
(`records LIKE '%S-400 Triumf%'` -> 0 rows). The 25 air-defence records exist only
as `test_records/*.json`. I built the reference catalogue from those (via the
repo's `record_model`), which is faithful to the intended dataset **and**
automatically satisfies "never touch `rag_test.db`". Lesson for the real system:
the enrichment pipeline needs a *defined, current* snapshot contract with the DB;
pointing it at whatever `.db` is lying around would have silently linked against
the wrong catalogue.

### 2. "Another DB entity" is an unsafe second signal when aliases collide
The spec allows a mention to be validated by "another DB entity" nearby. But the
`distractor_python_lang` doc mentions **both** "Python" *and* "Apache **Derby**" —
two catalogue entities — so a naive co-entity rule would have **validated the
distractor**. Fix: aliases that are common words (`python`, `derby`, `alto`,
`raptor`, ...) are flagged **ambiguous**, and an ambiguous mention may *not* use
another entity as its second signal (two collision-prone aliases can't validate
each other) — it needs a **domain term** or an **operator-country of that specific
record**. This single rule is what makes the distractors produce zero proposals.

### 3. A non-operator country near a mention is a false signal
`distractor_python_lang` says Python "was created by Guido van Rossum in the
**Netherlands**." The Netherlands is a real operator country *in the catalogue*
(of NASAMS) — so a generic "operator country nearby" check would fire. The gate
therefore requires the country to be an operator **of the specific matched
record** (Python-5's operators are Israel/India/Turkey), not any known country.

### 4. Cheap model fabricates a unit for an explicitly unitless value
`hedged_s300` says the emplacement time is *"listed simply as 5, with no unit
stated."* `gemma-3-4b` returned `value:"5", unit:"days"` — inventing "days". This
sailed past quote-grounding (the number *5* is in the quote) and produced a
**spurious conflict** (`Deployment time = 5 days` vs DB 30 min) in the first run.
Fix: a **unit-adjacency guard** — the model's unit must actually appear next to
its value in the source text; if not, the value is treated as unitless and parks
as `incomplete`. Anti-hallucination has to cover the *unit*, not just the value.

### 5. PDF text extraction severs values from their units
PyMuPDF hard-wraps lines mid-sentence: the deployment doc came out as
`"...deployment time of 8\nminutes..."`. My first grounding split on `\n`, so "8" and
"minutes" landed in different "sentences" and the unit-adjacency guard *wrongly*
stripped a **legitimate** unit -> the real conflict got mis-parked as incomplete.
Fix: collapse all whitespace before sentence-splitting. Corollary bug: matching
the value token "8" or "40" as a plain substring hit inside "S-**400**" / "2**8**00";
needed **digit-boundary** matching. And running headers/footers
(`CONFIDENTIAL // OSINT DIGEST ...`) bled into the first "sentence" of a doc and
into a citation until the provider stripped boilerplate lines.

### 6. Mapping ambiguity: the proposal *type* flips on the model's word choice
The same underlying fact can surface as a **gap-fill or a conflict** depending on
one adjective. `map_attribute("detection range")` -> `Detection range` (a field
S-300 lacks -> gap-fill), but `map_attribute("range")` -> `Maximum range` (S-300 has
200 km -> **conflict**). So whether gemma writes "detection range of 300 km" or
"range of 300 km" changes the output category entirely. The deterministic synonym
table narrows this but can't eliminate it — the ambiguity is real in the data
dictionary itself.

### 7. Near-duplicate fields the mapper can't unify
S-400 has `Operating altitude range = 0 to 27000 m` (~27 km) but no field named
`Maximum altitude`. A doc "maximum altitude of 30 km" maps to `Maximum altitude`
and surfaces as a **gap-fill** — technically correct (that field is empty) but
**semantically redundant** with the altitude the record already carries. Field
mapping keys on names; it has no notion that two differently-named fields describe
the same physical quantity. A real deployment would want a concept layer
(component + parameterDescr embeddings) above the name-based dictionary.

### 8. Free-text / LOV parametric fields are a paraphrase-noise minefield
`python5_vietnam` triggered a spurious `Type` **conflict**: doc "air-to-air
missile" vs DB "Short-range all-aspect air-to-air missile" — the doc value is a
*substring/paraphrase*, not a contradiction; exact-match comparison called it a
conflict. It also produced a low-value `Function = interceptor` gap-fill.
Tradeoff taken: **only numeric parametric fields become proposals**; free-text/LOV
values are parked (`text_field`). This removes the noise cleanly but means the
pipeline **does not** surface genuine free-text gap-fills (a new prose "Guidance
system" description, say). For this catalogue that's the right call — free text is
better served by semantic search than by diff-style proposals.

### 9. Fingerprint granularity: corroboration != identical value
The two corroborating altitude docs say **"30 km"** and **"98,000 ft"** (~29.9 km).
Corroboration is designed to cluster on the *partial* fingerprint (record +
attribute), and it correctly counted 2 sources and graduated the claim. But the
**full** fingerprint includes the value, so the two never collapse into one
proposal — they surface as **sibling proposals** sharing a partial fingerprint,
each with `n_sources = 1`. The report had to reconstruct the cluster to show
"corroborated by 2 sources / distribution: 1 says `30 km`, 1 says `98,000 ft`."
Open question the design doesn't settle: are 30 km and 98,000 ft "the same fact"
(they corroborate the *attribute's existence*) or two competing values (they
disagree by 0.1 km)? The demo treats them as corroborating; a stricter system
might want a numeric-agreement tolerance on top of the partial fingerprint.

### 10. Multi-valued fields make "conflict vs new-variant" undecidable
S-400 `Maximum range` legitimately holds **three** variant values (400/250/120 km,
one per missile). A doc value of **380 km** matches none, so the code flags a
**conflict** — but 380 could equally be a *fourth* variant or a fresh estimate,
not a contradiction (verified: `380 -> conflict`, `400 -> already-present/drop`,
`500 -> conflict`). I sidestepped this in the gold by targeting single-valued
fields, but for a variant-heavy catalogue the conflict/gap-fill distinction on
multi-valued fields is genuinely ill-posed without variant-level alignment
(which missile? which configuration?) — information the free-text doc rarely
states cleanly.

### 11. Cheap-model output-shape quirks (each needed a guard)
Real `gemma-3-4b` behaviours that would crash or mislead a naive consumer:
- **unit folded into the value** — `value:"380 km"` instead of `value:"380",
  unit:"km"` (needed a value/unit re-split);
- **entity/attribute inversion** — "Belarus operates the S-400" came back as
  `entity:"Belarus", attribute:"operates", value:"S-400"` (handled by detecting
  the country across entity/value/quote rather than trusting `entity`);
- **a claim emitted as a bare string** instead of an object (crashed the loop until
  a malformed-claim guard was added);
- **`{...}` with no `claims` array** on the two distractors (returned an object
  like `{}`), which happens to be the *desired* zero-claim outcome but arrives as
  a parse "error" — had to treat gracefully.

### 12. Thinking models are a poor fit for cheap extraction
The mid-tier alternative `qwen3-14b` returned **valid** JSON but (a) prefixed it
with stray prose (`"d.\n{...}"` — the balanced-JSON parser recovered it) and (b)
burned **403 reasoning tokens** on a trivial 2-fact extraction, ~4x the token
cost and latency of gemma-3-4b for no quality gain on this task. For pure claim
extraction the smallest non-thinking model was both cheaper and no worse — but it
is the one with the value/unit quirks in Issue 11. There is no free lunch: you
either budget for reasoning tokens or you write the guards.

### 13. Extraction is non-deterministic; recall is therefore noisy
Even at `temperature 0.1`, gemma varied run-to-run: the S-400 deployment conflict
**surfaced** in one run and **parked** in another (a `minutes`/wrapping interaction,
now fixed), and `hedged_s300` yielded 2 claims one run and 7 the next. The
deterministic pipeline makes *precision* stable, but *recall* rides on a stochastic
first stage. Any recall figure from a single run should be read as +/-1 claim.

---

## Bottom line
A claim-first, deterministic-everything-after design lets a **$0.0002, 4B-parameter**
model drive safe reverse-enrichment: perfect precision on this corpus, correct
handling of distractors, corroboration, incrementality and suppression. The
non-obvious cost is that a cheap model needs **~half a dozen defensive guards**
that the original design didn't call out (unit hallucination, output-shape quirks,
PDF line-wrapping, ambiguous-alias cross-validation, free-text paraphrase, the
conflict/variant and near-duplicate-field ambiguities). Those guards — not the
prompt — are where the real engineering is.
