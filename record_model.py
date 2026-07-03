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
      "params": [{"name","value","unit","descr",  # LIST: dups + order kept
                  "qualifier","component","dtype"}, ...],
      "extras": {field: scalar | list},           # other typed fields; nested dicts
                                                   #   flatten to dotted paths
      "media":  [ ... ],                           # media objects, passed through
      "relations": [{"child_id","child_name",...}],# system-to-system edges (H4)
      "aliases": [str, ...],                       # curated alias/code names (H5)
    }

    Exemplar-v2 additions (REVIEW_FINDINGS section H): params carry an optional
    `qualifier` (parameterSubTitle / comments -- the label that distinguishes
    variant rows of the same parameter), `component` and `dtype` (source's
    authoritative type hint). Relationship rows inside parametrics[] and the
    relations[] list both land in `relations`; proliferations[] becomes
    membership-filterable "Operated by"/"Produced by" fields plus a prose
    section; curated aliases[]/codes[] land in `aliases` for entity pinning.
    All strings are HTML-entity-decoded exactly once on the way in (H1).

Why `params` is a LIST: a record can legitimately repeat a parameter name (a
"Range" per variant; a value plus a qualifying subtitle row). Keeping the list
preserves them end-to-end (REVIEW_FINDINGS C3/H2): typed_fields() keeps every
variant (a repeated name's value becomes a list; numeric filters match if ANY
variant matches) and rich_params() stacks variants into one labelled cell.
collapse_params() remains for callers that explicitly want one-value-per-name
(last-wins).

Value coercion is centralised in coerce_value() so every consumer coerces the
same way: strip thousands-separator commas and return a float when a unit is
present OR the (comma-stripped) string looks numeric, else keep the raw string.
Ingest-time date/range/multi-value normalisation of the *typed* fields lives in
typed_fields() / normalize_typed_value() and the date helpers below.
"""

import datetime as _dt
import html as _html
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

# Source-audit metadata fields (REVIEW_FINDINGS H6): present on ~every record at
# 100% coverage, so left un-demoted they dominate coverage-ranked field lists,
# pull "when was X introduced"-style extraction onto updatedDate, and put URLs
# into the embedding text. They stay STORED (typed_fields keeps them, so an
# explicit filter on them still works) but are: omitted from to_text(), flagged
# "admin" in the catalogue (see catalogue.build_catalogue), and excluded from
# field selection / the default filter-prompt spec. "name" is here because the
# pages-schema source duplicates the nomenclature into it.
ADMIN_FIELDS = frozenset({
    "releaseID", "reviewDate", "createdDate", "updatedDate", "versionDate",
    "productLink", "aliasList", "name",
})


def _looks_like_pages_schema(raw):
    return any(k in raw for k in _PAGES_MARKERS)


def _unescape_strings(x):
    """Decode HTML entities in every string of a raw record, exactly ONCE (H1):
    the production source HTML-escapes text fields (&#x27;, &amp;, ...), which
    otherwise survive verbatim into embedding text, FTS tokens and prompts.
    Deliberately a single pass -- a literal '&amp;amp;' in source decodes to
    '&amp;', never recursively to '&'. Keys are decoded too (component/parameter
    names can carry '&amp;', e.g. 'R&amp;D')."""
    if isinstance(x, str):
        return _html.unescape(x)
    if isinstance(x, list):
        return [_unescape_strings(i) for i in x]
    if isinstance(x, dict):
        return {(_html.unescape(k) if isinstance(k, str) else k):
                _unescape_strings(v) for k, v in x.items()}
    return x


def _blank():
    return {"id": None, "title": None, "prose": [], "params": [],
            "extras": {}, "media": [], "relations": [], "aliases": []}


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
        if media:    # an empty list isn't a blind spot worth reporting
            note_drop(dropped, "media", media)


# A source `parameter` name arrives pre-suffixed when the record has several
# variant rows of the same parameter ("Wing Sweep - 1", "Engine Thrust - 2").
# Without stripping, every variant becomes its OWN catalogue field, fragmenting
# the field space (REVIEW_FINDINGS H-b). `parameterOnly` is authoritative when
# present; this regex is the fallback for rows that only carry `parameter`.
_PARAM_SUFFIX_RE = re.compile(r"\s+-\s+\d+$")


def _extract_parametrics(raw, canon, dropped):
    """parametrics[] -> params (or relations). Rows that don't match the expected
    shape are dropped with a clear reason rather than silently ignored.

    Exemplar-v2 (REVIEW_FINDINGS H2): the field identity is `parameterOnly`
    (fallback: `parameter` with any " - N" variant suffix stripped); the row's
    distinguishing label -- `parameterSubTitle` and/or `comments` ("Standard
    configuration" vs "Overclocked/Emergency configuration") -- is kept as
    `qualifier` so duplicate-name rows stay meaningfully distinct end-to-end
    instead of collapsing last-wins (C3). `component`/`dataType` ride along.
    A row carrying childModelID/childModel is a system-to-system EDGE, not a
    value -- it routes to canon["relations"] (H4) so it can't masquerade as a
    free-text field."""
    para = raw.get("parametrics")
    if not isinstance(para, list):
        return
    if not _is_parametrics_list(para):
        note_drop(dropped, "parametrics", para)
        return
    for it in para:
        if it.get("childModelID") is not None or it.get("childModel"):
            canon["relations"].append({
                "child_id": it.get("childModelID"),
                "child_name": it.get("childModel") or it.get("parameterValue"),
                "component": it.get("componentOnly") or it.get("component"),
                "relation_type": it.get("parameterOnly") or it.get("parameter"),
            })
            continue
        name = it.get("parameterOnly") or it.get("parameter")
        if not name:
            continue
        name = str(name)
        if not it.get("parameterOnly"):
            name = _PARAM_SUFFIX_RE.sub("", name)
        subtitle = it.get("parameterSubTitle")
        comments = it.get("comments")
        if subtitle and comments:
            qualifier = f"{subtitle}; {comments}"
        else:
            qualifier = subtitle or comments
        canon["params"].append({
            "name": name,
            "value": it.get("parameterValue"),
            "unit": it.get("uom") or None,
            "descr": it.get("parameterDescr"),
            "qualifier": str(qualifier) if qualifier not in (None, "") else None,
            "component": it.get("componentOnly") or it.get("component") or None,
            "dtype": it.get("dataType") or None,
        })


def _extract_relations(raw, canon, dropped):
    """relations[] -> canon["relations"] (REVIEW_FINDINGS H4). Each row is an
    edge between this record (or one of its variant models) and another system;
    both endpoints are kept because the edge is meaningful even when the child
    record isn't in the corpus (the edge itself answers 'what engine powers X')."""
    rel = raw.get("relations")
    if not isinstance(rel, list):
        return
    for r in rel:
        if not isinstance(r, dict):
            continue
        if r.get("childModel") is None and r.get("childModelID") is None:
            continue
        canon["relations"].append({
            "child_id": r.get("childModelID"),
            "child_name": r.get("childModel"),
            "parent_id": r.get("parentModelID"),
            "parent_name": r.get("parentModel"),
            "component": r.get("parentComponent"),
            "relation_type": r.get("relationType"),
            "child_system_group": r.get("childSystemGroup"),
            "child_system_type": r.get("childSystemType"),
            "child_equip_code": r.get("childPrimaryEquipCode"),
        })


def _relations_prose(canon):
    """One 'Related systems' prose segment per edge, so the edge is retrievable
    (FTS + embedding) and citable even when the related record isn't ingested."""
    segs = []
    for r in canon["relations"]:
        name = r.get("child_name")
        if not name:
            continue
        bits = [b for b in (r.get("child_system_type"),
                            f"{r['component']} component" if r.get("component")
                            else None) if b]
        segs.append(f"{name} ({'; '.join(bits)})" if bits else str(name))
    return "; ".join(segs)


# Status-word buckets for proliferations[] (REVIEW_FINDINGS H3). The source
# vocabulary is open-ended (mostly "Using"/"Production", but also Retired /
# Developing / Ordered / ...): a status matching neither bucket keeps its
# country OUT of the two filter fields -- a retired operator is not an
# operator -- but stays fully searchable via the Proliferation prose section.
# "Developing" counts as producing: a country developing the system is its
# (emerging) producer, which is what "who makes X" queries are after.
_PROLIF_OPERATING_RE = re.compile(r"(?i)using|user|operat|service|deploy")
_PROLIF_PRODUCING_RE = re.compile(r"(?i)produc|manufactur|develop|design|build")


def _extract_proliferations(raw, canon, dropped):
    """proliferations[] -> two membership-filterable country fields ("Operated
    by (country)" / "Produced by (country)"), a "Proliferation region" field,
    and one prose section retaining the full per-country detail (status +
    organization). Returns True when consumed, so the adapter can skip the
    flattened countryList/regionList/trigraphLists conveniences (they lose the
    Using-vs-Production distinction this preserves)."""
    prol = raw.get("proliferations")
    if not isinstance(prol, list) or not prol:
        return False
    operators, producers, regions, segs = [], [], [], []
    for p in prol:
        if not isinstance(p, dict):
            continue
        country = p.get("country")
        status = str(p.get("proliferation") or "").strip()
        region = p.get("region")
        org = p.get("organization")
        if region:
            regions.append(str(region))
        if country and status:
            if _PROLIF_OPERATING_RE.search(status):
                operators.append(str(country))
            if _PROLIF_PRODUCING_RE.search(status):
                producers.append(str(country))
        if country or status:
            seg = str(country or "unknown country")
            if status:
                seg += f" - {status}"
            if org:
                seg += f" ({org})"
            segs.append(seg)

    def _uniq(xs):
        return list(dict.fromkeys(xs))

    if operators:
        canon["extras"]["Operated by (country)"] = _uniq(operators)
    if producers:
        canon["extras"]["Produced by (country)"] = _uniq(producers)
    if regions:
        canon["extras"]["Proliferation region"] = _uniq(regions)
    if segs:
        canon["prose"].append(("Proliferation", "; ".join(segs)))
    return True


def _extract_alias_names(raw, canon):
    """aliases[] (curated Common/Project/Cover names) + codes[] +
    primaryEquipCode -> canon["aliases"] (REVIEW_FINDINGS H5), consumed at
    ingest into the entity-pinning table. Cover names share no vocabulary with
    the title -- exactly the lookups embedding retrieval fails on and pinning
    wins. Also emits an 'Also known as' prose line so the names are findable
    by FTS/embedding inside passages, not just via pinning."""
    names = []
    al = raw.get("aliases")
    if isinstance(al, list):
        for a in al:
            if isinstance(a, dict) and a.get("alias"):
                names.append(str(a["alias"]).strip())
    codes = raw.get("codes")
    if isinstance(codes, list):
        for c in codes:
            if isinstance(c, dict) and c.get("code"):
                names.append(str(c["code"]).strip())
    pec = raw.get("primaryEquipCode")
    if pec is not None and str(pec).strip():
        names.append(str(pec).strip())
    canon["aliases"] = list(dict.fromkeys(n for n in names if n))
    if canon["aliases"]:
        canon["prose"].append(("Also known as", ", ".join(canon["aliases"])))


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
            if not v:
                continue    # empty list: nothing to index, nothing to report
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
    """pages_schema / exemplar-v2 shape: modelID+nomenclature, prose in
    descriptions[], structured facts in parametrics[], media[], plus the v2
    structures: curated aliases[]/codes[], proliferations[], relations[]
    (REVIEW_FINDINGS section H). Any other scalar (systemGroup, systemType,
    updatedDate, ...) is a typed extra field."""
    canon = _blank()
    canon["id"] = raw.get("id") or raw.get("modelID")
    canon["title"] = raw.get("title") or raw.get("nomenclature")
    _extract_prose(raw, canon, dropped)
    _extract_media(raw, canon, dropped)
    _extract_alias_names(raw, canon)
    _extract_parametrics(raw, canon, dropped)
    _extract_relations(raw, canon, dropped)
    has_prolif = _extract_proliferations(raw, canon, dropped)
    if canon["relations"]:
        rel_text = _relations_prose(canon)
        if rel_text:
            canon["prose"].append(("Related systems", rel_text))
    # Everything else -> extras. We DON'T skip 'nomenclature': the legacy field
    # walker indexed it as a field (it only skipped id/title/modelID), so keeping
    # it preserves catalogue field naming. to_text() omits it from the text since
    # it already leads as the Title header.
    skip = ["id", "title", "modelID", "descriptions", "media", "parametrics"]
    for k in ("relations", "aliases", "codes"):
        if isinstance(raw.get(k), list):
            skip.append(k)       # consumed above; a non-list shape stays LOUD
    if has_prolif:
        # countryList/regionList/trigraphLists are flattened conveniences of
        # proliferations[] -- superseded by the structured fields just derived
        # (they'd re-add the same countries WITHOUT the Using/Production split).
        skip += ["proliferations", "countryList", "regionList", "trigraphLists"]
    _absorb_extras(raw, "", canon, dropped, skip_keys=tuple(skip))
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
    raw = _unescape_strings(raw)     # H1: decode HTML entities exactly once
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
    # matching the legacy flatten_record scope). ADMIN_FIELDS (audit dates,
    # productLink, ...) are omitted: they'd put URLs and always-identical dates
    # into every record's embedding text (REVIEW_FINDINGS H6).
    for k, v in canon["extras"].items():
        if "." in k or k in _TEXT_SKIP_EXTRAS or k in ADMIN_FIELDS:
            continue
        if isinstance(v, (str, int, float, bool)):
            parts.append(f"{k}: {v}")
    for section, text in canon["prose"]:
        parts.append(f"{section}: {text}")
    for p in canon["params"]:
        value = "" if p["value"] is None else p["value"]
        line = f"Parameter {p['name']}"
        if p.get("qualifier"):
            # variant label ("Max Emergency Power"; "Standard configuration") --
            # without it, duplicate-name rows read as contradictory values (H2)
            line += f" [{p['qualifier']}]"
        line += f" = {value}"
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

    Duplicate-name variant rows (REVIEW_FINDINGS H2/C3) STACK into one cell --
    value becomes "100 (Standard configuration); 120 (Overclocked/Emergency
    configuration)" -- so the table shows every variant under the CATALOGUE's
    field name (record_table matches columns by that name) instead of silently
    keeping only the last row. A single row's qualifier is folded into its value
    the same way. Trade-off (documented in record_table's FUTURE note): a
    stacked cell is a display string, so record_table's numeric column sort
    sinks multi-variant rows; the honest full story beats a sortable half-truth."""
    canon = canon if is_canonical(canon) else normalize_record(canon)
    grouped = {}
    for p in canon["params"]:
        grouped.setdefault(p["name"], []).append(p)
    out = {}
    for name, rows in grouped.items():
        if len(rows) == 1 and not rows[0].get("qualifier"):
            p = rows[0]
            out[name] = {"value": p["value"], "unit": p["unit"],
                         "descr": p["descr"]}
            continue
        segs = []
        for p in rows:
            v = "" if p["value"] is None else str(p["value"])
            segs.append(f"{v} ({p['qualifier']})" if p.get("qualifier") else v)
        out[name] = {"value": "; ".join(segs),
                     "unit": next((p["unit"] for p in rows if p["unit"]), None),
                     "descr": next((p["descr"] for p in rows if p["descr"]), None)}
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


def coerce_value(value, unit=None, force_numeric=False):
    """Centralised scalar coercion used by every consumer that needs a typed value.
    Strips thousands-separator commas and returns a float when a unit is present OR
    the (comma-stripped) string looks numeric; otherwise returns the value
    unchanged. Non-strings pass through untouched.

    force_numeric (H2): the exemplar-v2 source carries an authoritative
    dataType hint ("Number") -- when the caller passes it through, try the float
    even without a unit or a numeric-looking string (still falls back to the raw
    value if the source hint is wrong)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return value
    s = value.strip()
    stripped = _strip_thousands(s)
    if unit or force_numeric or _NUMERIC_RE.match(stripped):
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


def normalize_typed_value(raw, unit, dtype=None):
    """Normalise ONE typed field value for filtering/classification:
      - a unit means "this is a quantity" -> numeric coercion, never a date;
      - a source dataType hint (exemplar-v2, H2) is authoritative: "Number" ->
        numeric coercion even unitless; "Date" -> ISO date when parseable;
      - otherwise a whole-value date -> ISO string;
      - a range value is kept as-is here (its from/to siblings are derived at the
        field level in typed_fields); everything else -> coerce_value.
    Native lists (multi-value) pass through unchanged."""
    if isinstance(raw, list):
        return raw
    if unit:
        return coerce_value(raw, unit)
    d = str(dtype).strip().lower() if dtype else ""
    if d in ("number", "numeric", "integer", "int", "float", "decimal"):
        return coerce_value(raw, None, force_numeric=True)
    if d == "date" and isinstance(raw, str) and is_date_like(raw.strip()):
        return to_iso(raw.strip())
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
    normalised consistently.

    Duplicate param names (variant rows -- REVIEW_FINDINGS H2/C3) keep EVERY
    value: the field's value becomes a LIST, and a numeric filter passes when
    ANY variant satisfies it (see ragkit._passes / catalogue._classify_field's
    numeric-with-lists handling). The old last-wins collapse silently dropped
    e.g. the standard-configuration value in favour of the emergency one.
    A param name still overrides a same-named extra entirely (unchanged policy).

    Relations (H4) contribute a membership-filterable "Fitted with" field: the
    related child-system names.

    Range-valued fields (e.g. "In service" = "1980-present") additionally derive
    two typed date fields "<field> (from)" and "<field> (to)"; the original string
    is kept as its own (free-text) field. An open range omits the (to) field --
    there's no defensible upper bound for "present"."""
    canon = canon if is_canonical(canon) else normalize_record(canon)
    out, units = {}, {}

    def norm(name, raw, unit, dtype=None):
        if unit:
            units[name] = unit
        # derive from/to date fields for range values (only when unitless strings)
        if not unit and isinstance(raw, str):
            rng = parse_range(raw.strip())
            if rng:
                out[f"{name} (from)"] = rng[0]
                if rng[1] is not None:
                    out[f"{name} (to)"] = rng[1]
        return normalize_typed_value(raw, unit, dtype)

    for f, v in canon["extras"].items():
        if v is None or v == "":
            continue
        out[f] = norm(f, v, None)

    grouped = {}                         # param base name -> [values...]
    for p in canon["params"]:
        if p["value"] is None or p["value"] == "":
            continue
        grouped.setdefault(p["name"], []).append(
            norm(p["name"], p["value"], p["unit"], p.get("dtype")))
    for name, vals in grouped.items():   # params win over a same-named extra
        flat = []
        for v in vals:
            flat.extend(v) if isinstance(v, list) else flat.append(v)
        out[name] = flat[0] if len(flat) == 1 else flat

    rel_names = [r.get("child_name") for r in canon.get("relations") or []
                 if r.get("child_name")]
    if rel_names:
        out["Fitted with"] = list(dict.fromkeys(str(n) for n in rel_names))

    return out, units


def collapse_params(canon):
    """Duplicate-name collapse helper (last-wins) exposed for callers that
    explicitly want one value per name. NOTE: since H2, neither typed_fields()
    nor rich_params() uses this -- both now PRESERVE variant rows; this is only
    for a caller that consciously wants the lossy collapse."""
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


def build_alias_table(id_title_pairs, extra_aliases=None):
    """Build {alias_lowercase: [parent_rid, ...]} from each record's title, for
    deterministic entity pinning at query time. Aliases come from: the full title;
    designation tokens (F-16, AIM-120, Su-57); parenthesised and comma-separated
    popular names; and meaningful title words (skipping stopwords + generic type
    words).

    extra_aliases (H5/H4): {rid: [name, ...]} of additional names to map to a
    record -- the curated aliases[]/codes[] the source ships (cover names share
    NO vocabulary with the title, exactly where title-derived aliasing and
    embeddings both fail) and, from ingest, out-of-corpus related-system names
    (so a query naming a component still pins the platform that carries it).
    Designation tokens are mined from these too."""
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
    for rid, names in (extra_aliases or {}).items():
        for n in names:
            n = str(n).strip()
            add(n, rid)
            for m in _DESIGNATION_RE.findall(n):
                add(m, rid)
    return aliases
