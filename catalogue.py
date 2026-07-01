#!/usr/bin/env python3
"""
catalogue.py - build a "filter vocabulary" from a set of records.

At ingest time we sample every record's structured fields and classify each
field into one of: numeric, categorical, date, free_text. For each we compute a
compact summary the LLM can be given (and that we can validate filters against):

  numeric     -> 5th/95th percentile range, median, unit
  categorical -> the full label set (capped)
  date        -> min/max span
  free_text   -> a flag + a couple of example snippets (filter via semantics)

The catalogue does double duty: it informs the filter-extraction prompt AND is
the source of truth the emitted filter is validated against before any SQL runs.
"""

import datetime as _dt
import json
import statistics

# Heuristics ---------------------------------------------------------------- #
CATEGORICAL_MAX_DISTINCT = 50       # more distinct values than this -> not categorical
CATEGORICAL_RATIO = 0.5             # distinct/total below this -> categorical-ish
CATEGORICAL_ABS_FLOOR = 20          # <= this many distinct short values -> categorical
                                    # regardless of ratio (handles small corpora where
                                    # distinct/total is high but the field is clearly
                                    # an enumerable label set, e.g. status, material)
FREE_TEXT_MIN_LEN = 40             # long strings lean free-text
SAMPLE_EXAMPLES = 2                 # example snippets to show for free-text


UNIT_KEYS = ("unit", "uom")        # unit-of-measure key aliases
VALUE_KEYS = ("value", "val")      # value key aliases for param-shaped dicts
_MULTIVALUE_PREFIX = "\x00multi\x00"   # internal marker for joined scalar lists


def _is_param_dict(d):
    """A param-shaped dict looks like {value/val: ..., unit/uom: ..., definition: ...}.
    We treat any dict carrying a value key (optionally with a unit/uom) as one."""
    if not isinstance(d, dict):
        return False
    return any(k in d for k in VALUE_KEYS)


def _param_value_unit(d):
    value = next((d[k] for k in VALUE_KEYS if k in d), None)
    unit = next((d[k] for k in UNIT_KEYS if k in d), None)
    return value, unit


def extract_fields(rec, dropped=None):
    """Pull typed, filterable scalar fields from a record dict, flattening nested
    objects into dotted field names (e.g. specs.thermal.max_temp).

    Returns ({field_name: value}, {field_name: unit}).
    - Nested dicts are recursed with dotted keys.
    - A param-shaped dict {value/val, unit/uom, definition} is collapsed to its
      scalar value (unit captured separately) wherever it appears, not just under
      a 'parameters' key.
    - A "parametrics" list (the schema-example.json / pages_schema shape --
      [{parameter, parameterValue, uom, parameterDescr, ...}, ...]) is walked
      the same way: each row becomes a bare field named by `parameter`, valued
      by `parameterValue` (coerced to float when `uom` is set, since that's
      this pipeline's convention for "this is a numeric quantity"), unit `uom`.
    - Scalar lists are joined into a single string so they classify as categorical
      (filterable by membership); other lists of objects are skipped for now.

    `dropped`: optional dict; when provided, structures this walker cannot index as
    filter fields (lists of objects, unrecognised param-list shapes, ...) are
    recorded into it as {path: {reason, count, example_keys}} so ingest can report
    them loudly instead of silently ignoring a new/variant JSON shape.
    """
    out = {}
    units = {}
    _walk(rec, "", out, units, skip_keys=("id", "title", "modelID"), dropped=dropped)
    return out, units


def _note_drop(dropped, path, value):
    """Record a structure that couldn't be indexed as a filter field, classifying
    it so the import report can tell prose (fine) from a genuinely unhandled shape
    (needs an adapter)."""
    if dropped is None:
        return
    reason = "unsupported value; not indexed as a filter field"
    example_keys = None
    if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
        first = value[0]
        example_keys = sorted(first.keys())
        if any(kk in first for kk in ("descrType", "description", "shortDescription")):
            reason = "prose descriptions (searchable via semantic search, not filterable)"
        elif any(kk in first for kk in ("parameter", "parameterValue", "parameterDescr")):
            reason = ("looks like a parametric list but rows lack parameter/"
                      "parameterValue; NOT indexed (add/adjust an adapter)")
        else:
            reason = "list of objects; NOT indexed as filter fields (needs an adapter)"
    elif isinstance(value, list):
        reason = "mixed/nested list; NOT indexed as filter fields (needs an adapter)"
    entry = dropped.setdefault(
        path, {"reason": reason, "count": 0, "example_keys": example_keys})
    entry["count"] += 1
    if entry.get("example_keys") is None and example_keys:
        entry["example_keys"] = example_keys


def _walk(node, prefix, out, units, skip_keys=(), dropped=None):
    for k, v in node.items():
        if prefix == "" and k in skip_keys:
            continue
        path = f"{prefix}.{k}" if prefix else k

        # param-shaped dict -> collapse to value (+unit), don't recurse further
        if _is_param_dict(v):
            value, unit = _param_value_unit(v)
            if value is not None:
                out[path] = value
                if unit:
                    units[path] = unit
            continue

        # plain nested dict (incl. a 'parameters'/'params' container) -> recurse.
        # The conventional 'parameters'/'params' container is transparent: its
        # children keep their bare names (max_temp, not parameters.max_temp) so
        # filter field names stay clean and match how users refer to them.
        if isinstance(v, dict):
            child_prefix = prefix if k in ("parameters", "params") else path
            _walk(v, child_prefix, out, units, dropped=dropped)
            continue

        # scalar
        if isinstance(v, (str, int, float, bool)):
            out[path] = v
            continue

        # list
        if isinstance(v, list):
            if k == "parametrics" and _is_parametrics_list(v):
                _walk_parametrics(v, out, units)
                continue
            scalars = [x for x in v if isinstance(x, (str, int, float, bool))]
            if scalars and len(scalars) == len(v):
                # scalar list -> join for visibility/embedding. Prefix marks it as
                # a multi-value field so the catalogue won't present the joined
                # string as a single clean categorical label. Proper per-element
                # membership filtering is a later refinement.
                out[path] = _MULTIVALUE_PREFIX + ", ".join(str(x) for x in scalars)
                continue
            # other lists of objects (e.g. "descriptions", "media"): not indexed as
            # filter fields (would need indexed/aggregated paths). Recorded so the
            # import report is loud about what a new/variant shape left on the table.
            _note_drop(dropped, path, v)
            continue

        # anything else (None etc.) -- ignore, but note genuinely unexpected types
        if v is not None:
            _note_drop(dropped, path, v)


def _is_parametrics_list(v):
    return bool(v) and all(
        isinstance(item, dict) and "parameter" in item and "parameterValue" in item
        for item in v
    )


def _walk_parametrics(items, out, units):
    for item in items:
        name = item.get("parameter")
        if not name:
            continue
        raw_value = item.get("parameterValue")
        uom = item.get("uom") or None
        value = raw_value
        if uom and isinstance(raw_value, str):
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = raw_value
        if value is None or value == "":
            continue
        # last one wins if the same parameter name repeats within a record,
        # matching the dict-key semantics used everywhere else in this file
        out[name] = value
        if uom:
            units[name] = uom


def _is_date(s):
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


def build_catalogue(records, units_by_field=None, dropped=None):
    """records: iterable of dicts (raw records, NOT flattened text).
    Returns a catalogue dict: {field: {type, ...summary}}.

    `dropped`: optional dict; when provided, structures that couldn't be indexed as
    filter fields (via extract_fields) plus fields whose values stayed untypable
    ('unknown') are recorded so ingest can print a loud import-diagnostics report."""
    units_by_field = units_by_field or {}
    # collect all values per field
    values = {}   # field -> list of raw values
    field_units = {}
    for rec in records:
        fields, units = extract_fields(rec, dropped=dropped)
        for f, v in fields.items():
            if v is None:
                continue
            values.setdefault(f, []).append(v)
        for f, u in units.items():
            field_units.setdefault(f, u)
    field_units.update(units_by_field)

    catalogue = {}
    for field, vals in values.items():
        catalogue[field] = _classify_field(field, vals, field_units.get(field))
        # a field with values that classified as 'unknown' is present but not
        # usefully filterable -- surface it in the import report too.
        if dropped is not None and catalogue[field].get("type") == "unknown":
            dropped.setdefault(field, {
                "reason": "values present but untypable (classified 'unknown')",
                "count": len(vals), "example_keys": None})
    return catalogue


def _classify_field(field, vals, unit):
    n = len(vals)
    # multi-value (joined scalar list)? Detect the marker and surface the union
    # of individual elements as the known value set, flagged as multi-value so
    # callers know membership semantics (contains-any) apply, not exact-equality.
    # `count` = how many records carry a value for this field (its coverage). It
    # drives coverage-ordered pruning of the filter spec (most-populated fields
    # first, near-empty ones dropped) — see catalogue_to_prompt.
    if any(isinstance(v, str) and v.startswith(_MULTIVALUE_PREFIX) for v in vals):
        elements = set()
        for v in vals:
            if isinstance(v, str) and v.startswith(_MULTIVALUE_PREFIX):
                body = v[len(_MULTIVALUE_PREFIX):]
                elements.update(e.strip() for e in body.split(",") if e.strip())
        return {"type": "multi_value", "count": n, "values": sorted(elements)}

    # numeric?
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if nums and len(nums) >= max(1, int(0.8 * n)):
        s = sorted(nums)
        return {
            "type": "numeric",
            "count": n,
            "unit": unit,
            "p5": round(_percentile(s, 0.05), 4),
            "p95": round(_percentile(s, 0.95), 4),
            "median": round(statistics.median(s), 4),
            "min": s[0],
            "max": s[-1],
        }
    # date?
    strs = [v for v in vals if isinstance(v, str)]
    if strs and all(_is_date(v) for v in strs):
        dates = [_parse_date(v) for v in strs]
        return {
            "type": "date",
            "count": n,
            "min": min(dates).isoformat(),
            "max": max(dates).isoformat(),
        }
    # categorical vs free_text
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
    """Convert internal multi-value markers into real lists for storage and
    filtering. Strips the marker so it never reaches embeddings/FTS/comparisons."""
    out = {}
    for k, v in typed.items():
        if isinstance(v, str) and v.startswith(_MULTIVALUE_PREFIX):
            body = v[len(_MULTIVALUE_PREFIX):]
            out[k] = [e.strip() for e in body.split(",") if e.strip()]
        else:
            out[k] = v
    return out


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
        if t == "numeric":
            u = f" {spec['unit']}" if spec.get("unit") else ""
            lines.append(
                f"- {field}: numeric, typical range {spec['p5']}–{spec['p95']}{u} "
                f"(median {spec['median']}); filter with min/max."
            )
        elif t == "categorical":
            lines.append(
                f"- {field}: categorical, must be one of "
                f"{spec['values']}; filter with exact value(s)."
            )
        elif t == "date":
            lines.append(
                f"- {field}: date, range {spec['min']} to {spec['max']}; "
                f"filter with date_from/date_to."
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
