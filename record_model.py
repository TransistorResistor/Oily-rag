#!/usr/bin/env python3
"""
record_model.py - the canonical internal record model + ingest-time normalizers.

Before this module the two supported record shapes (see ragkit.py's module
docstring) were parsed THREE times -- ragkit.flatten_record (embedding text),
ragkit.extract_parametrics (the structured table) and catalogue.extract_fields
(the filter fields) -- each with subtly different coercion rules. A third input
shape meant three edits and three chances to diverge (REVIEW_FINDINGS C1). This
module parses each raw record ONCE into a canonical structure that every consumer
derives a thin "view" from, so a new shape is a single new adapter here.

Canonical record (a plain dict):

    {
      "id":     str | None,                       # source id (modelID / id)
      "title":  str | None,                       # nomenclature / title
      "prose":  [(section_name, text), ...],      # searchable prose, order-kept
      "params": [{"name","value","unit","descr"}, ...],  # LIST: dups + order kept
      "extras": {field: scalar | list},           # other typed fields; nested dicts
                                                   #   flatten to dotted paths
      "media":  [ ... ],                           # media objects, passed through
    }

Why `params` is a LIST: a record can legitimately repeat a parameter name (a
"Range" per variant; a value plus a qualifying subtitle row). Keeping the list
preserves them end-to-end -- fixing at the MODEL level the documented last-wins
FUTURE note (REVIEW_FINDINGS C3). Consumers that genuinely need one value per
name (the typed-fields dict for filtering, the record_params table row) collapse
with an explicit, consistently-applied **last-wins** policy -- see
collapse_params()/rich_params() and typed_fields().

Value coercion is centralised in coerce_value() so every consumer coerces the
same way: strip thousands-separator commas and return a float when a unit is
present OR the (comma-stripped) string looks numeric, else keep the raw string.
Ingest-time date/range/multi-value normalisation of the *typed* fields lives in
typed_fields() / normalize_typed_value() and the date helpers below.
"""

import datetime as _dt
import re

try:
    # dateutil parses the human date formats the corpus actually uses
    # ("15 December 2005", "September 1991") that datetime.strptime can't.
    from dateutil import parser as _dateutil_parser
except Exception:  # pragma: no cover - dateutil is a declared dependency
    _dateutil_parser = None


# --------------------------------------------------------------------------- #
# Shape detection + param/unit key aliases                                     #
# --------------------------------------------------------------------------- #

_VALUE_KEYS = ("value", "val")     # value key aliases for param-shaped dicts
_UNIT_KEYS = ("unit", "uom")       # unit-of-measure key aliases

# Keys that identify the pages_schema / schema-example.json shape. Any of them
# is enough: a record carrying `parametrics`/`descriptions`/`nomenclature`/
# `modelID` is that shape; everything else is treated as the ragkit-native shape.
_PAGES_MARKERS = ("parametrics", "descriptions", "nomenclature", "modelID")


def _looks_like_pages_schema(raw):
    return any(k in raw for k in _PAGES_MARKERS)


def _blank():
    return {"id": None, "title": None, "prose": [], "params": [],
            "extras": {}, "media": []}


def is_canonical(x):
    """True if `x` is already a normalize_record() result (so views can accept
    either a raw record or a canonical one without re-normalising)."""
    return (isinstance(x, dict) and "prose" in x and "params" in x
            and "extras" in x and "media" in x)


# --------------------------------------------------------------------------- #
# Drop diagnostics (ported verbatim from catalogue._note_drop so the import    #
# report's reason-classification stays byte-identical across the refactor)     #
# --------------------------------------------------------------------------- #

def note_drop(dropped, path, value):
    """Record a structure that couldn't be indexed as a filter field, classifying
    it so the import report can tell prose (fine) from a genuinely unhandled shape
    (needs an adapter). Same logic as the old catalogue._note_drop."""
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


# --------------------------------------------------------------------------- #
# Shared low-level helpers used by both adapters                               #
# --------------------------------------------------------------------------- #

def _is_param_dict(d):
    """A param-shaped dict looks like {value/val, unit/uom, definition}. We treat
    any dict carrying a value key (optionally with a unit/uom) as one."""
    return isinstance(d, dict) and any(k in d for k in _VALUE_KEYS)


def _param_value_unit_descr(d):
    value = next((d[k] for k in _VALUE_KEYS if k in d), None)
    unit = next((d[k] for k in _UNIT_KEYS if k in d), None)
    descr = d.get("definition") or d.get("description") or d.get("parameterDescr")
    return value, unit, descr


def _is_parametrics_list(v):
    return bool(v) and all(
        isinstance(item, dict) and "parameter" in item and "parameterValue" in item
        for item in v
    )


def _extract_prose(raw, canon, dropped):
    """descriptions[] -> ordered prose sections (searchable, not filterable).
    Recorded in `dropped` as prose so the import report stays loud about what is
    searchable-only (mirrors the legacy catalogue._walk)."""
    desc = raw.get("descriptions")
    if not isinstance(desc, list):
        return
    for d in desc:
        if isinstance(d, dict):
            text = d.get("description") or d.get("shortDescription")
            if text:
                canon["prose"].append((d.get("descrType") or "Description", text))
    note_drop(dropped, "descriptions", desc)


def _extract_media(raw, canon, dropped):
    """media[] -> passed through on the canonical record; recorded in `dropped`
    as a non-filterable list of objects (matches the legacy behaviour)."""
    media = raw.get("media")
    if isinstance(media, list):
        canon["media"] = media
        note_drop(dropped, "media", media)


def _extract_parametrics(raw, canon, dropped):
    """parametrics[] -> params. Rows that don't match the expected shape are
    dropped with a clear reason rather than silently ignored."""
    para = raw.get("parametrics")
    if not isinstance(para, list):
        return
    if not _is_parametrics_list(para):
        note_drop(dropped, "parametrics", para)
        return
    for it in para:
        name = it.get("parameter")
        if not name:
            continue
        canon["params"].append({
            "name": str(name),
            "value": it.get("parameterValue"),
            "unit": it.get("uom") or None,
            "descr": it.get("parameterDescr"),
        })


def _absorb_extras(node, prefix, canon, dropped, skip_keys=()):
    """Walk `node` recording filterable scalar fields into canon['extras'] and
    structures it can't index into `dropped`. Ported from the legacy
    catalogue._walk so catalogue field NAMES stay identical:
      - nested dicts recurse with dotted keys (specs.thermal.max_temp);
      - the 'parameters'/'params' container is transparent (children keep bare
        names) -- though the adapters normally consume it into params first;
      - a param-shaped dict {value,unit,definition} becomes a param (so its unit
        survives) rather than a bare extra;
      - a list of scalars is kept as a NATIVE list (multi-value; no sentinel
        round-trip -- fixes REVIEW_FINDINGS C2);
      - `skip_keys` are ignored at the top level only (id/title/modelID and the
        containers the adapter already consumed).
    """
    for k, v in node.items():
        if prefix == "" and k in skip_keys:
            continue
        path = f"{prefix}.{k}" if prefix else k

        # param-shaped dict anywhere -> a param (keeps value+unit together)
        if _is_param_dict(v):
            value, unit, descr = _param_value_unit_descr(v)
            if value is not None:
                canon["params"].append(
                    {"name": path, "value": value, "unit": unit, "descr": descr})
            continue

        # plain nested dict -> recurse (dotted). 'parameters'/'params' stay
        # transparent so their children keep bare names.
        if isinstance(v, dict):
            child_prefix = prefix if k in ("parameters", "params") else path
            _absorb_extras(v, child_prefix, canon, dropped)
            continue

        # scalar (incl. "" and False; None is ignored, matching the old walk)
        if isinstance(v, (str, int, float, bool)):
            canon["extras"][path] = v
            continue

        # list
        if isinstance(v, list):
            scalars = [x for x in v if isinstance(x, (str, int, float, bool))]
            if scalars and len(scalars) == len(v):
                canon["extras"][path] = scalars      # native multi-value list
                continue
            note_drop(dropped, path, v)
            continue

        if v is not None:
            note_drop(dropped, path, v)


# --------------------------------------------------------------------------- #
# The two adapters (chosen by shape detection)                                 #
# --------------------------------------------------------------------------- #

def _adapt_pages_schema(raw, dropped):
    """pages_schema / schema-example.json shape: modelID+nomenclature, prose in
    descriptions[], structured facts in parametrics[], media[]. Any other scalar
    (systemGroup, systemType, updatedDate, ...) is a typed extra field."""
    canon = _blank()
    canon["id"] = raw.get("id") or raw.get("modelID")
    canon["title"] = raw.get("title") or raw.get("nomenclature")
    _extract_prose(raw, canon, dropped)
    _extract_media(raw, canon, dropped)
    _extract_parametrics(raw, canon, dropped)
    # Everything else -> extras. We DON'T skip 'nomenclature': the legacy field
    # walker indexed it as a field (it only skipped id/title/modelID), so keeping
    # it preserves catalogue field naming. to_text() omits it from the text since
    # it already leads as the Title header.
    _absorb_extras(raw, "", canon, dropped,
                   skip_keys=("id", "title", "modelID",
                              "descriptions", "media", "parametrics"))
    return canon


def _adapt_native(raw, dropped):
    """ragkit-native shape: id/title, free-text scalar fields (text, notes, ...)
    and a `parameters`/`params` dict of {value,unit,definition} or bare scalars."""
    canon = _blank()
    canon["id"] = raw.get("id") or raw.get("modelID")
    canon["title"] = raw.get("title") or raw.get("nomenclature")
    for key in ("parameters", "params"):
        cont = raw.get(key)
        if isinstance(cont, dict):
            for name, spec in cont.items():
                if isinstance(spec, dict):
                    value, unit, descr = _param_value_unit_descr(spec)
                    # a plain nested dict (no value key) under parameters keeps the
                    # legacy transparent-recurse behaviour instead of being a param
                    if value is None and not _is_param_dict(spec):
                        _absorb_extras(spec, "", canon, dropped)
                        continue
                    canon["params"].append(
                        {"name": str(name), "value": value, "unit": unit,
                         "descr": descr})
                else:
                    canon["params"].append(
                        {"name": str(name), "value": spec, "unit": None,
                         "descr": None})
    _extract_prose(raw, canon, dropped)
    _extract_media(raw, canon, dropped)
    _absorb_extras(raw, "", canon, dropped,
                   skip_keys=("id", "title", "modelID", "parameters", "params",
                              "descriptions", "media"))
    return canon


def normalize_record(raw, dropped=None):
    """Parse a raw record dict into the canonical model (see module docstring).

    `dropped`: optional dict; structures that can't be indexed as filter fields
    (prose descriptions, media, unrecognised list shapes) are recorded into it via
    note_drop() so ingest can report them loudly -- same contract as the old
    catalogue.build_catalogue `dropped`."""
    if not isinstance(raw, dict):
        raise TypeError("record must be a dict")
    if _looks_like_pages_schema(raw):
        return _adapt_pages_schema(raw, dropped)
    return _adapt_native(raw, dropped)


# --------------------------------------------------------------------------- #
# Views over the canonical model                                              #
# --------------------------------------------------------------------------- #

# Extra fields that name the record itself and are already shown as the Title
# header; kept as typed fields (catalogue naming) but omitted from the flattened
# text so we don't duplicate the title as a "nomenclature: ..." line.
_TEXT_SKIP_EXTRAS = ("nomenclature",)


def to_text(canon):
    """The embedding/search text view (replaces ragkit.flatten_record). Keeps the
    exact line formats downstream depends on: a leading "Title: " header and one
    "Parameter <name> = <value> <unit> (<descr>)" line per param (chunk_record and
    _truncate_to_tokens key off both prefixes)."""
    canon = canon if is_canonical(canon) else normalize_record(canon)
    parts = []
    if canon["title"]:
        parts.append(f"Title: {canon['title']}")
    # top-level scalar extras as "key: value" lines (nested dotted paths and
    # multi-value lists are structured data, not prose, so they're not emitted --
    # matching the legacy flatten_record scope)
    for k, v in canon["extras"].items():
        if "." in k or k in _TEXT_SKIP_EXTRAS:
            continue
        if isinstance(v, (str, int, float, bool)):
            parts.append(f"{k}: {v}")
    for section, text in canon["prose"]:
        parts.append(f"{section}: {text}")
    for p in canon["params"]:
        value = "" if p["value"] is None else p["value"]
        line = f"Parameter {p['name']} = {value}"
        if p["unit"]:
            line += f" {p['unit']}"
        if p["descr"]:
            line += f" ({p['descr']})"
        parts.append(line)
    return "\n".join(parts)


def rich_params(canon):
    """The structured-table view (replaces ragkit.extract_parametrics): the full
    parametric fields as {name: {value, unit, descr}}, keeping raw values (so the
    table shows "4604", not "4604.0") and the record's description/subtitle.
    Collapses duplicate names last-wins (documented policy; see module docstring)."""
    canon = canon if is_canonical(canon) else normalize_record(canon)
    out = {}
    for p in canon["params"]:
        out[p["name"]] = {"value": p["value"], "unit": p["unit"],
                          "descr": p["descr"]}
    return out


# --------------------------------------------------------------------------- #
# Centralised value coercion                                                   #
# --------------------------------------------------------------------------- #

# A thousands separator is a comma between digits with 3 digits following (and a
# non-digit or end after them). This avoids eating a decimal comma or a comma in
# free text like "one, two".
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(\D|$))")
_NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _strip_thousands(s):
    return _THOUSANDS_RE.sub("", s)


def coerce_value(value, unit=None):
    """Centralised scalar coercion used by every consumer that needs a typed value.
    Strips thousands-separator commas and returns a float when a unit is present OR
    the (comma-stripped) string looks numeric; otherwise returns the value
    unchanged. Non-strings pass through untouched."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return value
    s = value.strip()
    stripped = _strip_thousands(s)
    if unit or _NUMERIC_RE.match(stripped):
        try:
            return float(stripped)
        except ValueError:
            return value
    return value


# --------------------------------------------------------------------------- #
# Date + range normalisation (ingest-time)                                     #
# --------------------------------------------------------------------------- #

# Parse with a fixed default so missing day/month don't leak TODAY's date in
# (dateutil defaults omitted fields to now): "1974" -> 1974-01-01, not 1974-<today>.
_DATE_DEFAULT = _dt.datetime(2000, 1, 1)
# A value is only treated as a date if it references a 4-digit year (1000-2999) or
# a month name. This keeps bare numbers (Mach 4, Crew 9) -- which dateutil would
# happily read as day-of-month -- OUT of the date type.
_YEAR_RE = re.compile(r"\b[12]\d{3}\b")
_MONTH_RE = re.compile(
    r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)")
_DASHES = ("–", "—", "-")   # en-dash, em-dash, hyphen


def _parse_datetime(s):
    if _dateutil_parser is None:
        return None
    try:
        return _dateutil_parser.parse(s, fuzzy=False, default=_DATE_DEFAULT)
    except (ValueError, OverflowError, TypeError):
        return None


def is_date_like(s):
    """True only when the WHOLE value parses as a date AND it looks like one (has a
    year or month token) -- so we normalise real dates without mangling prose or
    bare numbers."""
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not s or not (_YEAR_RE.search(s) or _MONTH_RE.search(s)):
        return False
    return _parse_datetime(s) is not None


def to_iso(s):
    """ISO date string for a date-like value, else None."""
    dt = _parse_datetime(s)
    return dt.date().isoformat() if dt else None


def parse_range(s):
    """Detect a service-period/date range and return (from_iso, to_iso) where
    to_iso is None for an open range ("... - present"). Handles en-dash AND hyphen
    ("1980-present", "1980–present", "1973-2017", "Month YYYY-present"). Returns
    None when the value isn't a two-part date range (so ISO dates like 2005-12-15,
    which split into three parts, and hyphenated prose are left alone)."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    for dash in _DASHES:
        if dash not in s:
            continue
        parts = s.split(dash)
        if len(parts) != 2:
            continue                       # not a clean two-sided range
        left, right = parts[0].strip(), parts[1].strip()
        from_iso = to_iso(left) if is_date_like(left) else None
        if from_iso is None:
            continue
        if right.lower() == "present":
            return (from_iso, None)        # open range: omit the (to) field
        to = to_iso(right) if is_date_like(right) else None
        if to is not None:
            return (from_iso, to)
    return None


def normalize_typed_value(raw, unit):
    """Normalise ONE typed field value for filtering/classification:
      - a unit means "this is a quantity" -> numeric coercion, never a date;
      - otherwise a whole-value date -> ISO string;
      - a range value is kept as-is here (its from/to siblings are derived at the
        field level in typed_fields); everything else -> coerce_value.
    Native lists (multi-value) pass through unchanged."""
    if isinstance(raw, list):
        return raw
    if unit:
        return coerce_value(raw, unit)
    if isinstance(raw, str):
        s = raw.strip()
        if parse_range(s):
            return s                       # keep the original range string
        if is_date_like(s):
            return to_iso(s)
    return coerce_value(raw, None)


def typed_fields(canon):
    """The filter-field view (replaces catalogue.extract_fields): a record's typed
    scalar fields as ({field: value}, {field: unit}), with values coerced/date-
    normalised consistently and duplicate param names collapsed last-wins.

    Range-valued fields (e.g. "In service" = "1980-present") additionally derive
    two typed date fields "<field> (from)" and "<field> (to)"; the original string
    is kept as its own (free-text) field. An open range omits the (to) field --
    there's no defensible upper bound for "present"."""
    canon = canon if is_canonical(canon) else normalize_record(canon)
    out, units = {}, {}

    def add(name, raw, unit):
        if raw is None or raw == "":
            return
        out[name] = normalize_typed_value(raw, unit)
        if unit:
            units[name] = unit
        # derive from/to date fields for range values (only when unitless strings)
        if not unit and isinstance(raw, str):
            rng = parse_range(raw.strip())
            if rng:
                out[f"{name} (from)"] = rng[0]
                if rng[1] is not None:
                    out[f"{name} (to)"] = rng[1]

    for f, v in canon["extras"].items():
        add(f, v, None)
    for p in canon["params"]:            # after extras so params win on a clash
        add(p["name"], p["value"], p["unit"])
    return out, units


def collapse_params(canon):
    """Duplicate-name collapse helper (last-wins) exposed for callers that want the
    policy without the full rich_params dict. Kept tiny + documented so the
    last-wins policy is applied from ONE place."""
    out = {}
    for p in canon["params"]:
        out[p["name"]] = p
    return out


# --------------------------------------------------------------------------- #
# Entity alias table (ingest half of G1)                                       #
# --------------------------------------------------------------------------- #

# Words that don't identify a specific system, so they make poor aliases.
_ALIAS_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "for", "with", "to", "in", "on", "by",
    "mk", "type", "model",
}
_ALIAS_GENERIC = {
    "missile", "missiles", "radar", "radars", "aircraft", "class", "family",
    "system", "systems", "vehicle", "tank", "gun", "guns", "fighter", "bomber",
    "helicopter", "submarine", "destroyer", "cruiser", "carrier", "torpedo",
    "cannon", "rifle", "carbine", "cartridge", "round", "series", "program",
    "project", "weapon",
}
# Military designation tokens: 1-4 letters, optional -/space, then digits, then
# an optional alnum/dash tail. Matches AIM-120, F-16, Su-57, R-77, AGM-88, T-90.
_DESIGNATION_RE = re.compile(r"\b[A-Za-z]{1,4}[-/ ]?\d+[A-Za-z0-9-]*\b")


def build_alias_table(id_title_pairs):
    """Build {alias_lowercase: [parent_rid, ...]} from each record's title, for
    deterministic entity pinning at query time (a later phase consumes it -- this
    phase only builds + stores it). Aliases come from: the full title; designation
    tokens (F-16, AIM-120, Su-57); parenthesised and comma-separated popular names;
    and meaningful title words (skipping stopwords + generic type words)."""
    aliases = {}

    def add(alias, rid):
        if not alias:
            return
        a = alias.strip().lower()
        if len(a) < 2:
            return
        lst = aliases.setdefault(a, [])
        if rid not in lst:
            lst.append(rid)

    for rid, title in id_title_pairs:
        if not title:
            continue
        title = str(title).strip()
        add(title, rid)                                    # full title
        for m in _DESIGNATION_RE.findall(title):           # designations
            add(m, rid)
        for pn in re.findall(r"\(([^)]+)\)", title):       # parenthesised names
            add(pn, rid)
        for seg in title.split(","):                       # comma-separated names
            seg = seg.strip()
            if seg and seg != title:
                add(seg, rid)
        cleaned = re.sub(r"[(),]", " ", title)             # meaningful words
        for w in cleaned.split():
            wl = w.lower()
            if wl in _ALIAS_STOPWORDS or wl in _ALIAS_GENERIC:
                continue
            # keep short tokens only if they're designations (e.g. "F-2")
            if len(wl) < 3 and not _DESIGNATION_RE.match(w):
                continue
            add(w, rid)
    return aliases
