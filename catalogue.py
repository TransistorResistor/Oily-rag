#!/usr/bin/env python3
"""
catalogue.py - build a "filter vocabulary" from a set of records.

At ingest time we sample every record's structured fields and classify each
field into one of: numeric, categorical, date, multi_value, free_text. For each
we compute a compact summary the LLM can be given (and that we can validate
filters against):

  numeric     -> 5th/95th percentile range, median, unit
  categorical -> the full label set (capped)
  date        -> min/max span
  multi_value -> the union of elements seen (membership/"contains" filtering)
  free_text   -> a flag + a couple of example snippets (filter via semantics)

The catalogue does double duty: it informs the filter-extraction prompt AND is
the source of truth the emitted filter is validated against before any SQL runs.

Field parsing itself lives in record_model.py (the canonical record model);
extract_fields() below is a thin wrapper over record_model.typed_fields() so
this module no longer reimplements the shape-parsing/coercion rules that used
to independently diverge from ragkit.py's own parsers (REVIEW_FINDINGS C1).
What THIS module owns is turning a corpus of typed field values into a
classified catalogue: majority-type classification (A1), cross-record unit
reconciliation (C4), and the categorical/free_text heuristics.
"""

import statistics
from collections import Counter

import record_model
import units as units_mod

# Heuristics ---------------------------------------------------------------- #
CATEGORICAL_MAX_DISTINCT = 50       # more distinct values than this -> not categorical
CATEGORICAL_RATIO = 0.5             # distinct/total below this -> categorical-ish
CATEGORICAL_ABS_FLOOR = 20          # <= this many distinct short values -> categorical
                                    # regardless of ratio (handles small corpora where
                                    # distinct/total is high but the field is clearly
                                    # an enumerable label set, e.g. status, material)
FREE_TEXT_MIN_LEN = 40             # long strings lean free-text
SAMPLE_EXAMPLES = 2                 # example snippets to show for free-text

# Majority-type classification (REVIEW_FINDINGS A1): a field used to need ~80%
# of its values to coerce cleanly to number/date or the WHOLE field degraded to
# free_text -- silently losing a clean majority (e.g. "Maximum speed": 23/37
# records give a clean Mach number, 14 are raw strings like "35 mph (56 km/h)";
# 62% numeric used to fail the old 80% bar and drop all 37 to free_text).
# MAJORITY_MIN is the bar to classify as numeric/date AT ALL, indexing the typed
# subset; between MAJORITY_MIN and FULL_MIN the entry is additionally flagged
# "partial" (with a typed_count) so a filter/UI consumer knows not every record
# has a comparable value for it.
MAJORITY_MIN = 0.5
FULL_MIN = 0.8


def extract_fields(rec, dropped=None):
    """Pull typed, filterable scalar fields from a record dict. Returns
    ({field_name: value}, {field_name: unit}) -- same contract as always.

    Thin wrapper: normalizes the record into the canonical model (record_model.
    normalize_record) and takes its typed_fields() view, so nested dicts flatten
    to dotted paths, the 'parameters'/'params' container stays transparent,
    param-shaped dicts collapse to value+unit, dates/ranges normalize, and native
    lists pass through -- all exactly as record_model documents (and identically
    to how ragkit.py derives the same record's embedding text and rich params,
    since it's the same canonical model -- REVIEW_FINDINGS C1).

    `dropped`: optional dict; structures that can't be indexed as filter fields
    (prose descriptions, media, unrecognised list shapes) are recorded into it via
    record_model.note_drop so ingest can report them loudly -- same contract as
    before.
    """
    canon = record_model.normalize_record(rec, dropped=dropped)
    return record_model.typed_fields(canon)


def _is_date(s):
    """Strict ISO-ish date check for FILTER BOUNDS (a date_from/date_to a model
    or caller supplies), deliberately separate from record_model.is_date_like
    (which fuzzy-parses the natural-language dates found IN the corpus, e.g. "15
    December 2005"). A filter bound should be unambiguous, so this stays narrow."""
    import datetime as _dt
    if not isinstance(s, str):
        return False
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            _dt.datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    return False


def _parse_date(s):
    import datetime as _dt
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _reconcile_units(field, vals, units, dropped):
    """When a field carries >=2 distinct units across records (REVIEW_FINDINGS
    C4 -- the same field ingested with two different units used to silently keep
    whichever unit was seen first), convert minority-unit VALUES into the
    majority unit via units_mod.convert so the catalogue's numeric stats and
    later filter comparisons are apples-to-apples. `units` is the list of units
    aligned 1:1 with `vals` (None where a value carried no unit).

    A value that can't be dimensionally converted (mismatched dimension, pint
    unavailable) is left AS-IS and the field is noted in `dropped` with a clear
    reason, rather than silently comparing mismatched magnitudes.

    Returns (vals, majority_unit) -- vals is a new list (only the converted
    entries differ from the input); majority_unit is None if no value carried a
    unit at all."""
    present = [u for u in units if u]
    if not present:
        return vals, None
    counts = Counter(present)
    majority_unit = counts.most_common(1)[0][0]
    if len(counts) == 1:
        return vals, majority_unit

    out_vals = list(vals)
    unconverted = 0
    for i, (v, u) in enumerate(zip(vals, units)):
        if not u or u == majority_unit:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue  # nothing numeric to convert (shouldn't normally happen)
        try:
            out_vals[i] = units_mod.convert(v, u, majority_unit)
        except units_mod.ConversionError:
            unconverted += 1
    if unconverted and dropped is not None:
        others = sorted(set(counts) - {majority_unit})
        dropped[field] = {
            "reason": (
                f"unit conflict: values also seen in {others} alongside the "
                f"majority unit '{majority_unit}'; {unconverted} value(s) could "
                f"not be dimensionally converted and are left in their original "
                f"(non-canonical) unit -- the catalogue entry's unit is "
                f"'{majority_unit}'"),
            "count": unconverted, "example_keys": None,
        }
    return out_vals, majority_unit


def build_catalogue(records, units_by_field=None, dropped=None):
    """records: iterable of dicts (raw records, NOT flattened text).
    Returns a catalogue dict: {field: {type, ...summary}}.

    `dropped`: optional dict; when provided, structures that couldn't be indexed as
    filter fields (via extract_fields) plus fields whose values stayed untypable
    ('unknown') or hit a cross-record unit conflict are recorded so ingest can
    print a loud import-diagnostics report."""
    units_by_field = units_by_field or {}
    # collect all values per field, plus the unit (or None) that went with each
    # value -- needed (not just one first-seen unit per field) so _reconcile_units
    # can see every unit actually used, not just the first record's.
    values = {}       # field -> [values...]
    value_units = {}  # field -> [unit-or-None, ...], aligned 1:1 with values[field]
    for rec in records:
        fields, units = extract_fields(rec, dropped=dropped)
        for f, v in fields.items():
            if v is None:
                continue
            values.setdefault(f, []).append(v)
            value_units.setdefault(f, []).append(units.get(f))

    catalogue = {}
    for field, vals in values.items():
        vals, unit = _reconcile_units(field, vals, value_units.get(field, []),
                                      dropped)
        if field in units_by_field:
            unit = units_by_field[field]
        catalogue[field] = _classify_field(field, vals, unit)
        # a field with values that classified as 'unknown' is present but not
        # usefully filterable -- surface it in the import report too.
        if dropped is not None and catalogue[field].get("type") == "unknown":
            dropped.setdefault(field, {
                "reason": "values present but untypable (classified 'unknown')",
                "count": len(vals), "example_keys": None})
    return catalogue


def _classify_field(field, vals, unit):
    n = len(vals)

    # multi-value: record_model.typed_fields returns NATIVE lists for a scalar-
    # list field (no join/sentinel round-trip -- REVIEW_FINDINGS C2 fix). A field
    # is multi-value if ANY record's value for it is a list; the value set is the
    # union of every list's elements (a stray non-list value in the same field
    # -- shouldn't normally happen -- is folded in as a singleton element rather
    # than silently dropped).
    if any(isinstance(v, list) for v in vals):
        elements = set()
        for v in vals:
            if isinstance(v, list):
                elements.update(str(x).strip() for x in v if str(x).strip())
            elif str(v).strip():
                elements.add(str(v).strip())
        return {"type": "multi_value", "count": n, "values": sorted(elements)}

    # numeric? Majority-type classification (REVIEW_FINDINGS A1): see the module
    # docstring / MAJORITY_MIN and FULL_MIN. Stats (p5/p95/median/min/max) are
    # computed over the clean numeric subset only.
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if nums and len(nums) >= max(1, int(MAJORITY_MIN * n)):
        s = sorted(nums)
        entry = {
            "type": "numeric",
            "count": n,
            "unit": unit,
            "p5": round(_percentile(s, 0.05), 4),
            "p95": round(_percentile(s, 0.95), 4),
            "median": round(statistics.median(s), 4),
            "min": s[0],
            "max": s[-1],
        }
        if len(nums) < FULL_MIN * n:
            entry["partial"] = True
            entry["typed_count"] = len(nums)
        return entry

    # date? Same majority logic, using record_model's dateutil-backed date
    # detection (accepts "15 December 2005", not just %Y-%m-%d) instead of the
    # old strict ISO-only check -- so e.g. "Introduced"/"First flight" can
    # classify as date instead of free_text.
    strs = [v for v in vals if isinstance(v, str)]
    date_like = [v for v in strs if record_model.is_date_like(v)]
    if date_like and len(date_like) >= max(1, int(MAJORITY_MIN * n)):
        dates = [record_model.to_iso(v) for v in date_like]
        entry = {"type": "date", "count": n, "min": min(dates), "max": max(dates)}
        if len(date_like) < FULL_MIN * n:
            entry["partial"] = True
            entry["typed_count"] = len(date_like)
        return entry

    # categorical vs free_text (unchanged heuristics)
    if strs:
        distinct = sorted(set(strs))
        avg_len = sum(len(x) for x in strs) / len(strs)
        # Categorical if the values are short AND there's a small, enumerable set.
        # Two ways to qualify: an absolute floor (good for small corpora) or the
        # ratio test (good for large ones). Either suffices.
        short_values = avg_len < FREE_TEXT_MIN_LEN
        small_set = (
            len(distinct) <= CATEGORICAL_ABS_FLOOR
            or (len(distinct) <= CATEGORICAL_MAX_DISTINCT
                and len(distinct) / n <= CATEGORICAL_RATIO)
        )
        if short_values and small_set:
            return {"type": "categorical", "count": n, "values": distinct}
        # free text
        examples = distinct[:SAMPLE_EXAMPLES]
        return {
            "type": "free_text",
            "count": n,
            "examples": [e[:80] for e in examples],
        }
    # fallback
    return {"type": "unknown", "count": n}


def normalize_stored_fields(typed):
    """Historically converted the multi-value sentinel-string marker into a real
    list before storage (REVIEW_FINDINGS C2). record_model.typed_fields now
    returns native lists directly, so there's nothing left to convert -- kept as
    a passthrough so existing callers don't need to change."""
    return typed


def partition_fields(catalogue, min_count=1, max_cardinality=40, limit=8):
    """The broad 'what kind of system' dimensions to offer in a first-pass filter:
    high-coverage categorical / multi-value fields with a small, enumerable value
    set (systemGroup, systemType, country of origin, ...). Ordered by coverage desc.

    These are the fields that meaningfully *partition* the corpus, so a cheap first
    pass can settle "which category is this about?" before a second pass dives into
    the detailed (often category-specific) parameters. See ragkit.extract_filter_2pass.
    """
    cands = []
    for field, spec in catalogue.items():
        if spec.get("type") not in ("categorical", "multi_value"):
            continue
        values = spec.get("values") or []
        if len(values) > max_cardinality:
            continue
        cnt = spec.get("count", 0) or 0
        if cnt < min_count:
            continue
        cands.append((cnt, len(values), field))
    # most-covered first; tie-break toward fewer buckets (crisper partitions)
    cands.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [f for _, _, f in cands[:limit]]


def catalogue_to_prompt(catalogue, only_fields=None, min_count=1, max_fields=None):
    """Render the catalogue as a compact spec for the filter-extraction prompt.

    A large catalogue dumped verbatim bloats every filter-extraction prompt and
    hurts smaller models' field-selection accuracy (needle-in-a-haystack). So the
    spec is trimmed and ordered by *coverage* — how many records actually carry the
    field:
      - fields are listed most-populated first, so the most useful, most likely
        filter dimensions lead;
      - fields below `min_count` records of coverage are dropped (a field present in
        one stray record is a poor, noisy filter dimension);
      - `only_fields`, when given, restricts the spec to that set (used by the
        two-pass extractor to show only partition fields, then only in-category
        detail fields);
      - `max_fields`, when given, caps the spec to the top-N by coverage.
    A numeric/date field classified "partial" (REVIEW_FINDINGS A1 -- not every
    record has a comparable value) gets a short coverage note, e.g. "(23/37
    records have a filterable value)", so the model knows a miss on this field
    doesn't necessarily mean "no match".
    Backward-compatible: a catalogue built before `count` existed simply isn't
    pruned (its fields sort as coverage 0 but nothing is dropped when counts are
    absent).
    """
    items = []
    for field, spec in catalogue.items():
        if only_fields is not None and field not in only_fields:
            continue
        cnt = spec.get("count")
        if min_count and cnt is not None and cnt < min_count:
            continue
        items.append((field, spec, cnt if cnt is not None else 0))
    # most-populated fields first; stable tie-break by field name
    items.sort(key=lambda t: (-t[2], t[0]))
    if max_fields:
        items = items[:max_fields]

    lines = ["Filterable fields:"]
    for field, spec, _cnt in items:
        t = spec["type"]
        partial_note = ""
        if spec.get("partial"):
            partial_note = (f" ({spec.get('typed_count')}/{spec.get('count')} "
                            f"records have a filterable value)")
        if t == "numeric":
            u = f" {spec['unit']}" if spec.get("unit") else ""
            lines.append(
                f"- {field}: numeric, typical range {spec['p5']}–{spec['p95']}{u} "
                f"(median {spec['median']}){partial_note}; filter with min/max."
            )
        elif t == "categorical":
            lines.append(
                f"- {field}: categorical, must be one of "
                f"{spec['values']}; filter with exact value(s)."
            )
        elif t == "date":
            lines.append(
                f"- {field}: date, range {spec['min']} to {spec['max']}"
                f"{partial_note}; filter with date_from/date_to."
            )
        elif t == "multi_value":
            lines.append(
                f"- {field}: multi-value list, elements include "
                f"{spec['values']}; filter with contains (any of these)."
            )
        elif t == "free_text":
            ex = "; ".join(spec["examples"])
            lines.append(
                f"- {field}: free text (NOT value-filterable; use semantic search). "
                f"e.g. {ex}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    import json
    # quick manual run: python catalogue.py records/
    import ragkit  # reuse loader for raw records
    import glob, os
    src = sys.argv[1] if len(sys.argv) > 1 else "records"
    raw = []
    files = sorted(glob.glob(os.path.join(src, "*.json"))) if os.path.isdir(src) else [src]
    for fp in files:
        data = json.load(open(fp))
        raw.extend(data if isinstance(data, list) else [data])
    cat = build_catalogue(raw)
    print(json.dumps(cat, indent=2))
    print("\n" + catalogue_to_prompt(cat))
