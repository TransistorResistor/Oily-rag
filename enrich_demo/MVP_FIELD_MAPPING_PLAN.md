# Enrichment field-mapping MVP

## Implementation status (2026-07-12)

Implemented behind `--field-mapper catalogue`; the legacy mapper remains the
default for compatibility. `--text-fields` independently enables conservative
Text/LOV gap-fills.

Delivered:

- generated field profiles from every normalized `parameterDescr`, `dataType`,
  `uom`, component and example;
- `field_aliases.json` v2 with curated and contextual equivalents;
- peer-record and definition-profile candidate scoring with confidence tiers and
  winner/runner-up diagnostics;
- persisted raw claims and mapper diagnostics, report labels, parked-candidate
  details, and equivalent-term suggestions;
- field-audit CLI output;
- opt-in Text/LOV match/gap/difference policy;
- assertion, incompatible-unit, dual-cell and intra-document consistency gates;
- offline regression coverage plus live/replayed corpus gates.

Current evidence: corpus 1 replay P/R 1.00/1.00, corpus 3 replay P/R 1.00/1.00,
table corpus 2b P/R 1.00/1.00, and a fresh mixed-table run correctly parks the
inconsistent dual range and ambiguous bare `Time` while surfacing `Length`.

## Decision

Test schema-aware reverse enrichment with the current JSON corpus, then run the
same experiment mostly autonomously over a read-only production export/snapshot.
Do this before building a production field registry or database-provider
abstraction.

The production design remains an interesting future direction, but it is not
required for the next experiment. The current pages-schema JSON already supplies
enough metadata on each parametric row to build a useful field catalogue at run
time:

- `parameterOnly` supplies the canonical parameter name;
- `parameterDescr` supplies one or more natural-language definitions;
- `dataType` supplies `Number`, `Text`, `LOV`, and related type hints;
- `uom` supplies the unit for quantities;
- `componentOnly` supplies useful scope;
- `parameterSubTitle` and `comments` distinguish variants;
- `parameterValue` supplies representative values.

The canonical `record_model.normalize_record()` adapter already retains these as
`name`, `descr`, `dtype`, `unit`, `component`, `qualifier`, and `value`. The MVP
should build on that contract rather than introduce another source format.

## MVP hypothesis

A generated field catalogue plus a small, versioned equivalent-term map will
resolve most extracted claim attributes to the database's real parameter names.
The report-only pipeline can accept some imperfect mappings in an explicitly
labelled experimental lane; only low-margin ambiguity or semantic safety failures
need to park.

This is deliberately less ambitious than a production registry:

- parameter names remain the field identity for the experiment;
- field definitions and examples are discovered from the loaded JSON;
- equivalent terms are curated in one small configuration file;
- mapping and comparison remain deterministic after the existing LLM claim
  extraction call;
- the tool remains report-only and never creates a new record.

## Autonomy posture

Optimize the MVP for useful autonomous coverage, not perfect mapping in every
case. Surfacing a proposal is not approval to mutate the catalogue.

Use three mapping tiers:

| Tier | Typical evidence | MVP action |
|---|---|---|
| High confidence | exact parameter/curated alias, compatible type/unit/context | resolve automatically |
| Medium confidence | strong definition/example match with a clear lead over the runner-up | resolve automatically, label `experimental_mapping` in the report |
| Low confidence | weak score, small winner/runner-up margin, incompatible evidence | park as `ambiguous_field` or `unmapped` |

Some checks remain hard gates regardless of the desired autonomy level:

- a unit incompatible with the candidate field's physical dimension;
- a negated, hypothetical, or otherwise non-asserted claim;
- materially conflicting values for the same record/field in one document;
- an internally inconsistent dual-unit cell;
- a value or entity that cannot be grounded in the document.

These cases are not merely suboptimal mappings; allowing them through creates a
misleading factual proposal.

## 1. Generate a field catalogue from the JSON

Build one entry per canonical `parameterOnly`/normalized parameter name across
the full reference corpus:

```json
{
  "Maximum range": {
    "definitions": [
      "Maximum effective engagement range",
      "Maximum range for the applicable missile configuration"
    ],
    "data_types": ["Number"],
    "units": ["km"],
    "components": ["Performance"],
    "examples": ["50", "120", "200", "400"],
    "record_count": 21
  }
}
```

Retain every distinct definition rather than taking the first definition seen.
Differences can be informative: the current demo already has fields such as
`Detection range` and `Maximum range` with several closely related definitions.

The catalogue builder should emit an audit containing:

- fields with multiple definitions;
- mixed or dimensionally incompatible units;
- mixed data types;
- fields with missing definitions/types;
- duplicate names used under materially different components;
- representative values and record coverage.

The audit is part of the test result. No inconsistency should be silently resolved.

## 2. Add a small equivalent-term overlay

Use a human-owned JSON file, for example `field_aliases.json`, for the important
many-to-one vocabulary that cannot be recovered reliably from field names alone:

```json
{
  "version": 1,
  "fields": {
    "Maximum range": {
      "aliases": [
        "range",
        "max range",
        "engagement range",
        "effective range"
      ]
    },
    "Deployment time": {
      "aliases": [
        "deployment time",
        "setup time",
        "emplacement time",
        "ready time"
      ]
    },
    "Length": {
      "aliases": ["length", "overall length"]
    }
  },
  "contextual_aliases": [
    {
      "term": "time",
      "field": "Deployment time",
      "requires_any_context": ["deploy", "setup", "emplacement", "ready"]
    }
  ]
}
```

Keep generic aliases contextual. Bare `time`, `range`, `weight`, and `altitude`
can refer to several parameters and should not become unconditional global
synonyms merely to improve recall on one document.

The initial file can be seeded from the existing `refcat.SYNONYMS` map, then
trimmed or contextualized as the audit exposes collisions.

## 3. Resolve an extracted attribute conservatively

For each grounded LLM claim, resolve its `attribute` in this order:

1. Exact normalized match to a canonical parameter name.
2. Exact normalized match to the curated equivalent-term overlay.
3. Abbreviation expansion followed by steps 1-2.
4. Definition-assisted candidate scoring against every `parameterDescr` for the
   field, using deterministic token overlap initially.
5. Filter/rerank candidates using evidence already present in the claim and JSON:
   data type, unit dimension, component wording, record/system type, and nearby
   table/header context.

Return a structured result rather than only a field name:

```json
{
  "field": "Deployment time",
  "mapping_status": "resolved",
  "mapping_method": "contextual_alias",
  "score": 1.0,
  "runner_up": "Reaction time",
  "evidence": ["term=time", "context=deployment", "unit=s"]
}
```

If two candidates remain plausible and the score margin is small, park the claim
as `ambiguous_field` and show both candidates. A strong definition/context winner
may resolve autonomously as `experimental_mapping`, but a low-confidence fuzzy
match must not create an unlabelled proposal.

This resolver should replace the current `map_attribute(attribute)` return value
behind a feature flag so the existing mapper remains available for A/B testing.

## 4. Use examples without turning them into authority

Examples from `parameterValue` are useful for:

- learning whether a text/LOV field behaves like a scalar or a small vocabulary;
- showing likely value shapes to the deterministic validator;
- recognizing spelling/case variants of existing LOV values;
- diagnosing fields whose values mix incompatible concepts.

Examples must not define the complete allowed vocabulary. A new manufacturer,
operator, status, or guidance type may be the genuine enrichment being sought.

## 5. MVP proposal policy by data type

Keep the existing numeric behavior, with the safety fixes identified by the
robustness review (unit-dimension validation, dual-unit consistency, negation,
and intra-document disagreement).

For the user's assumption that source and database text fields are reasonably
aligned, add a conservative text experiment:

| Source `dataType` | MVP behavior |
|---|---|
| `Number` | Existing normalized match/gap/conflict logic with registered units. |
| `LOV` | Case/punctuation-normalized exact matches drop; missing values may gap-fill; differing existing values park unless the field is explicitly configured as scalar/closed. |
| `Text` | Exact/normalized existing matches drop; absent fields may gap-fill; differing non-empty text parks as `text_difference`, not an automatic conflict. |
| Unknown/mixed | Park with the catalogue audit attached. |

This tests whether text gap-fills are useful without reintroducing the paraphrase
conflicts that the current numeric-only policy was designed to avoid.

## 6. Make mapping decisions visible

Add these fields to stored claim/report diagnostics:

- extracted attribute;
- resolved parameter name;
- mapping method and score;
- candidate fields and runner-up score;
- definition(s), unit/type and component evidence used;
- alias-overlay version;
- reason for parking.

The weekly report should group `ambiguous_field`, `unmapped`, and `text_difference`
claims separately. These are the feedback source for improving the equivalent-term
map; analysts should not have to inspect SQLite manually.

## 7. Other high-leverage improvements

### A. Build the candidate set from peer records

For a gap-fill, the target record does not contain the missing field, so looking
only at that record cannot discover the parameter. Instead, build likely fields
from records sharing `systemGroup`, `systemType`, and (where present) component.

This is a high-value middle ground between a global 83-field search and a governed
ontology. For example, fields common to other air-to-air missiles should receive a
strong prior when resolving a Derby claim, while unrelated radar-only fields are
down-ranked. Retain the global catalogue as a fallback so genuinely uncommon
fields are still reachable.

### B. Rank definitions, not only field-name tokens

Create one searchable field profile from:

```text
parameter name + all parameterDescr values + components + representative values
```

Start with TF-IDF/token similarity because it is reproducible and cheap. If
production vocabulary proves more varied, add cached field-profile embeddings as
an optional candidate generator. Embeddings should nominate candidates; unit,
type, context, and score-margin checks should still decide whether to resolve.

### C. Use the winner/runner-up margin as the autonomy control

A fixed top-score threshold is not enough. A score of 0.72 is persuasive if the
runner-up is 0.25, but ambiguous if the runner-up is 0.70. Record both scores and
configure separate thresholds for high-confidence, experimental, and parked
mappings. Tune them on the demo and later on a production shadow sample.

### D. Mine equivalent-term suggestions automatically

Repeated extracted attributes are valuable feedback. When an unmapped term such
as `emplacement duration` repeatedly has the same strong definition-based winner,
write an alias suggestion containing:

- the observed term;
- proposed parameter name;
- occurrence count and record classes;
- winner/runner-up scores;
- representative quotes;
- any unit/type evidence.

Promoting a suggestion into `field_aliases.json` should remain a small human
action. This turns curation into review of high-value suggestions rather than
manual vocabulary brainstorming.

### E. Make table cells first-class evidence

Preserve a row/cell representation from markdown/table extraction so the mapper
receives `label`, `raw_value`, and header units together. Do not rely on the LLM to
keep `100 / 181` in the JSON `value` field: the mixed-table test showed it may put
only `100` there and leave the second number in the quote/qualifier. Deterministic
cell recovery should run before field comparison.

### F. Reconcile all claims from a document before surfacing

Group extracted claims by record and resolved parameter. Park a group if the same
document supplies materially incompatible values. This prevents a single sampled
claim from hiding an obvious prose/table contradiction.

### G. Separate extraction from remapping

Persist the raw LLM claims and extraction version independently of their mapping
result. A new alias, definition scorer, or confidence threshold can then re-run
mapping and classification without paying for another hosted extraction call.
Store the mapper version and alias-overlay version with every proposal.

### H. Add production-shadow telemetry

For a later production snapshot, report:

- mapping coverage by high/medium/parked tier;
- most frequent unmapped attributes;
- most frequent alias suggestions;
- fields with high ambiguity or incompatible units;
- proposal composition by system type and field;
- analyst acceptance/rejection by mapping method.

This evidence determines whether the stopgap is sufficient or a governed registry
is actually justified.

## 8. Minimum implementation slice

1. Add a generated `FieldCatalogue` view over the existing normalized records.
2. Move/seed the current synonym map into `field_aliases.json`.
3. Implement peer-record candidate discovery and
   `resolve_attribute(claim, record_context)` with winner/runner-up diagnostics.
4. Add a feature flag such as `--field-mapper legacy|catalogue`.
5. Store mapper diagnostics on claims or in a companion mapping-audit table.
6. Add high/medium/parked autonomy tiers and automatic alias-suggestion output.
7. Add opt-in text/LOV gap-fill behavior as described above.
8. Run both mappers against fresh disposable databases and compare proposal and
   parked-claim composition.

No SQL catalogue provider, stable field-ID migration, reviewer application, or
general ontology is required for this slice.

## 9. Acceptance gates

The MVP is useful if it materially improves autonomous mapping coverage without
violating the hard semantic safety gates. Medium-confidence mapping errors may be
accepted during report-only shadow testing when they are explicitly labelled and
measured.

- Existing corpus 1, corpus 2, and table corpus 2b retain their current expected
  proposals and zero distractor false positives.
- The input-robustness corpus must not surface the intra-document contradiction.
- The mixed table `Range (km/NM) | 100 / 181` parks as inconsistent rather than
  surfacing `100 km`.
- `Length | 10 m` resolves to `Length` and compares normally.
- Bare `Time (s) | 100` either resolves through explicit context or parks as
  `ambiguous_field`; it must not be guessed globally.
- Add at least one genuine Text gap-fill, one existing Text match, one paraphrase
  difference, and one LOV case.
- Report mapping precision, mapping coverage, proposal precision/recall, and the
  distribution of high/medium/parked mapping tiers separately.
- Produce ranked alias suggestions from recurring unmapped or definition-resolved
  terms, with enough evidence for quick promotion into `field_aliases.json`.

## Extraction model class

The current evidence supports a small, non-reasoning instruction model with
reliable JSON output as the minimum class. The tested 4B Gemma model finds the
needed claims cheaply, and the deterministic layer now handles its common table,
unit, slotting, and output-shape mistakes.

For a production shadow run, prefer a non-reasoning 7B-14B instruct model if the
incremental cost is acceptable. That class should improve claim completeness and
table-row fidelity without paying for reasoning tokens that do not help a bounded
extraction task. Require JSON-object/schema support, sufficient context for the
largest document chunk, low temperature, and predictable latency.

Move to a 20B-30B class only if measured extraction recall on representative
production PDFs remains inadequate after table-aware rendering/chunking. Treat
OCR or image-only pages as a document-processing/vision problem rather than trying
to compensate with a larger text model. Do not select a thinking/reasoning model
for routine parameter extraction unless an A/B gate shows a concrete gain.

## Deferred production idea

If the MVP demonstrates useful coverage, revisit a governed production registry
and catalogue-provider contract. That later design would add stable field IDs,
explicit applicability/cardinality/variant policies, canonical units, versioned
comparison rules, SQL/API-backed catalogue access, temporal validity, field-level
provenance, source lineage, and schema migrations.

Those features should be justified by MVP evidence. They are intentionally not
part of the current-data experiment.
