#!/usr/bin/env python3
"""
pages_to_schema.py - convert pages/*.json (Wikipedia-sourced records) into
records shaped like pages/schema-example.json:

  { modelID, systemGroup, systemType, nomenclature, updatedDate,
    descriptions: [{description, shortDescription, descrType, classification}],
    media: [{seq, mediaID, url, title}],
    parametrics: [{seq, parameter, parameterDescr, parameterUomValue,
                    classification, parameterValue, uom}] }

Two things this version does differently from the first pass:

1. descriptions are split into a fixed set of canonical types --
   Overview / History / Design / Usage / Variants -- instead of a single
   generic "Details" blob. Each page's full_text is segmented by its own
   internal section headings (detected two ways: a heading on its own
   line, e.g. "DEVELOPMENT", or "Label: " inline at a paragraph's start,
   e.g. "Development history: ..."), which are looked up against a
   heading vocabulary and classified into one of the five buckets via
   keyword rules. Pages with no internal headings at all (several of the
   radar-family pages) fall back to a light per-paragraph keyword scan
   of the prose itself. See CONVERSION_NOTES.md for the full rationale.

2. parametrics are widened per systemType. Many aircraft/helicopter
   records only carry infobox-level facts (Type, Manufacturer, First
   flight, ...) in `specs` -- their numeric dimensions (Length, Wingspan,
   Empty weight, Maximum speed, Range, ...) live instead in a
   "Specifications" bullet block buried in full_text. That block is
   parsed out and merged in, so aircraft records get real numeric
   parametrics instead of just qualitative fields.

After writing pages_schema/*.json, this script also (re)generates
schemas/<systemType>.schema.json -- one file per systemType, listing the
parameter/uom/parameterDescr vocabulary actually observed for that type
across the corpus. These are reference manifests, not JSON-Schema meta
documents; they mirror schema-example.json's instance style.

Usage: python pages_to_schema.py [pages_dir] [out_dir]
"""

import glob
import json
import os
import re
import sys
import datetime

NUM = r'\+?\d[\d,]*\.?\d*'

SKIP_FILES = {"_index.json", "schema-example.json"}

# --------------------------------------------------------------------------- #
# Known bad category/subcategory classifications in the source pages/*.json
# (e.g. R-60 is an air-to-air missile but got tagged "fighter_aircraft" by
# the html extractor's keyword rules -- its infobox Type field never says
# "aircraft").
# --------------------------------------------------------------------------- #
CATEGORY_OVERRIDES = {
    "R-60_missile.json": ("weapon", "air_to_air"),
}

# --------------------------------------------------------------------------- #
# systemGroup / systemType -- broad group + specific human-readable type,
# mirroring schema-example.json's "Sensors" / "Laser Sensor" pattern.
# --------------------------------------------------------------------------- #
GROUP_TYPE = {
    ("weapon", "air_to_air"): ("Weapon", "Air-to-Air Missile"),
    ("weapon", "air_to_surface"): ("Weapon", "Air-to-Surface Missile"),
    ("weapon", "surface_to_air"): ("Weapon", "Surface-to-Air Missile"),
    ("weapon", "cruise_missile"): ("Weapon", "Cruise Missile"),
    ("weapon", "ballistic_missile"): ("Weapon", "Ballistic Missile"),
    ("weapon", "torpedo"): ("Weapon", "Torpedo"),
    ("weapon", "firearm"): ("Weapon", "Firearm"),
    ("weapon", "cannon"): ("Weapon", "Cannon"),
    ("ammunition", "cartridge"): ("Ammunition", "Cartridge"),
    ("radar", "aircraft_radar"): ("Sensors", "Aircraft Radar"),
    ("fighter_aircraft", None): ("Aircraft", "Fighter Aircraft"),
    ("attack_aircraft", None): ("Aircraft", "Attack Aircraft"),
    ("helicopter", None): ("Aircraft", "Helicopter"),
    ("land_vehicle", "main_battle_tank"): ("Land Vehicle", "Main Battle Tank"),
    ("land_vehicle", "armored_fighting_vehicle"): ("Land Vehicle", "Armored Fighting Vehicle"),
    ("land_vehicle", "utility_vehicle"): ("Land Vehicle", "Utility Vehicle"),
    ("naval_vessel", "submarine"): ("Naval Vessel", "Submarine"),
    ("naval_vessel", "surface_combatant"): ("Naval Vessel", "Surface Combatant"),
    ("naval_vessel", "aircraft_carrier"): ("Naval Vessel", "Aircraft Carrier"),
}


def resolve_group_type(category, subcategory):
    key = (category, subcategory)
    if key in GROUP_TYPE:
        return GROUP_TYPE[key]
    group = (category or "Uncategorized").replace("_", " ").title()
    typ = (subcategory or category or "Uncategorized").replace("_", " ").title()
    return group, typ


# --------------------------------------------------------------------------- #
# Field-name canonicalization -- several Wikipedia infoboxes/spec blocks use
# different labels for the same concept; unify so a parameter name actually
# means one thing across every record.
# --------------------------------------------------------------------------- #
NAME_ALIASES = {
    "No. built": "Number built",
    "National origin": "Country of origin",
    "Place of origin": "Country of origin",
    "Country": "Country of origin",
    "Introduction date": "Introduced",
    "Operational range": "Range",
    "Flight ceiling": "Service ceiling",
}


def canonical_name(name):
    return NAME_ALIASES.get(name, name)


# --------------------------------------------------------------------------- #
# Dimension tables: canonical unit + alias -> factor to that canonical unit.
# is_metric marks aliases that belong to the metric/SI family so they're
# preferred over an imperial match found elsewhere in the same string.
# --------------------------------------------------------------------------- #
DIMENSIONS = {
    "mass": {"canonical": "kg", "aliases": [
        (r"kilograms?", 1.0, True),
        (r"kg", 1.0, True),
        # Armored vehicles/ships give mass in tonnes ("55.2 tonnes") or the
        # abbreviation "t" ("54 t"); "tonnes?" precedes bare "t" so the spelled
        # form is claimed first, and the (?![a-zA-Z]) guard in _alias_regex
        # keeps "t" from matching the "t" in "tonnes"/"short tons"/"long tons".
        (r"tonnes?|metric tons?", 1000.0, True),
        (r"pounds?", 0.45359237, False),
        (r"lb", 0.45359237, False),
        (r"t", 1000.0, True),
    ]},
    "length": {"canonical": "m", "aliases": [
        (r"met(?:re|er)s?", 1.0, True),
        (r"m", 1.0, True),
        (r"millimet(?:re|er)s?", 0.001, True),
        (r"mm", 0.001, True),
        (r"centimet(?:re|er)s?", 0.01, True),
        (r"cm", 0.01, True),
        (r"feet|foot", 0.3048, False),
        (r"ft", 0.3048, False),
        (r"inch(?:es)?", 0.0254, False),
        (r"in", 0.0254, False),
    ]},
    "area": {"canonical": "m2", "aliases": [
        (r"m\s*\^?\s*2|square met(?:re|er)s?", 1.0, True),
        (r"sq\.?\s*ft|square feet|ft\s*\^?\s*2", 0.09290304, False),
    ]},
    "range_km": {"canonical": "km", "aliases": [
        (r"kilomet(?:re|er)s?", 1.0, True),
        (r"km", 1.0, True),
        (r"met(?:re|er)s?", 0.001, True),
        (r"m", 0.001, True),
        (r"nmi", 1.852, False),
        (r"mi(?:les?)?", 1.609344, False),
    ]},
    "climb_rate": {"canonical": "m/s", "aliases": [
        (r"m/s", 1.0, True),
        (r"ft/min", 0.00508, False),
    ]},
    # Mach-only: see extract_quantity's mach_special handling -- absolute
    # speed units (km/h etc.) are deliberately not extracted for this field,
    # so there is no alias list here.
    "speed": {"canonical": "Mach", "mach_special": True},
    "freq": {"canonical": "GHz", "aliases": [
        (r"ghz", 1.0, True),
        (r"mhz", 0.001, True),
    ]},
    "power": {"canonical": "kW", "aliases": [
        (r"kw", 1.0, True),
    ]},
    "angle": {"canonical": "deg", "aliases": [
        (r"degrees?", 1.0, True),
    ]},
}

CURRENCY_FIELDS = {"Unit cost"}
COUNT_FIELDS = {"Number built", "Units delivered"}

# canonical field name -> dimension key (None => kept as free text)
FIELD_DIMENSION = {
    "Unit cost": "currency",
    "Mass": "mass", "Weight": "mass", "Warhead weight": "mass",
    "Empty weight": "mass", "Max takeoff weight": "mass", "Gross weight": "mass",
    "Length": "length", "Diameter": "length", "Antenna diameter": "length",
    "Wingspan": "length", "Height": "length", "Service ceiling": "length",
    "Main rotor diameter": "length",
    "Wing area": "area", "Main rotor area": "area",
    "Range": "range_km", "Flight altitude": "range_km",
    "Combat range": "range_km", "Ferry range": "range_km",
    "Rate of climb": "climb_rate",
    "Maximum speed": "speed",
    "Frequency": "freq",
    "Power": "power",
    "Azimuth": "angle", "Elevation": "angle",
    "Number built": "count", "Units delivered": "count",
}

FIELD_DEFINITIONS = {
    "Type": "System type/role classification.",
    "Country of origin": "Country or countries where the system was designed/originates.",
    "Manufacturer": "Company or companies that manufacture the system.",
    "Designer": "Organization credited with the system's design.",
    "Built by": "Manufacturing sites/companies that produced the system under license or partnership.",
    "Status": "Current operational status of the system.",
    "Primary users": "Principal operator(s) of the system.",
    "Primary user": "Principal operator of the system.",
    "Used by": "Known operators of the system.",
    "Number built": "Total number of units produced.",
    "Units delivered": "Total number of units delivered to customers.",
    "Manufactured": "Date range during which the system was in production.",
    "Introduced": "Date the system entered operational service.",
    "In service": "Period during which the system has been/was in active service.",
    "First flight": "Date of the type's first flight.",
    "Designed": "Year design work began.",
    "Produced": "Production period.",
    "Production ended": "Year production of the system ended.",
    "Developed from": "Earlier system this design is derived from.",
    "Developed into": "Later systems derived from this design.",
    "Variant": "Named variant of the base system.",
    "Variants": "Named variants of the base system.",
    "Wars": "Conflicts in which the system has seen use.",
    "Crew": "Number of crew required to operate the system.",
    "Capacity": "Payload/passenger capacity.",
    "Length": "Overall length of the system.",
    "Wingspan": "Wingspan or fin span.",
    "Height": "Overall height of the system.",
    "Wing area": "Total wing planform area.",
    "Main rotor diameter": "Diameter of the main rotor disc.",
    "Main rotor area": "Disc area swept by the main rotor.",
    "Empty weight": "Aircraft weight excluding fuel, payload, and crew.",
    "Gross weight": "Typical loaded weight for a standard mission.",
    "Max takeoff weight": "Maximum certified takeoff weight.",
    "Weight": "Overall weight of the system.",
    "Mass": "Overall mass of the system.",
    "Fuel capacity": "Internal (and optionally external) fuel capacity.",
    "Powerplant": "Engine(s) fitted and their type/thrust rating.",
    "Engine": "Engine type and configuration.",
    "Maximum speed": "Top speed, expressed as a Mach number where the source gives one.",
    "Cruise speed": "Typical cruising speed.",
    "Range": "Maximum operational range.",
    "Combat range": "Range achievable on a representative combat mission profile.",
    "Ferry range": "Maximum range in ferry (long-range transit) configuration.",
    "Flight altitude": "Operational flight altitude band.",
    "Service ceiling": "Maximum altitude the system can maintain controlled flight/engagement.",
    "Rate of climb": "Maximum sustained rate of climb.",
    "Wing loading": "Weight per unit wing area.",
    "Thrust/weight": "Thrust-to-weight ratio.",
    "g limits": "Structural load factor limits.",
    "Guns": "Internal gun armament.",
    "Hardpoints": "Number and capacity of external weapon stations.",
    "Armament": "Weapons the system is equipped to carry/fire.",
    "Diameter": "Body/airframe diameter.",
    "Warhead": "Warhead type and/or description.",
    "Warhead weight": "Mass of the warhead section.",
    "Detonation mechanism": "Fuzing/detonation method.",
    "Guidance system": "Method(s) used to guide the weapon to its target.",
    "Steering system": "Method used to steer/control the airframe in flight.",
    "Propellant": "Rocket motor propellant type.",
    "Launch platform": "Aircraft/vessels/vehicles the weapon can be launched from.",
    "Unit cost": "Approximate unit cost in US dollars.",
    "Frequency": "Operating radio frequency.",
    "Power": "Transmit power output.",
    "Antenna diameter": "Diameter of the radar antenna/dish.",
    "Antenna": "Antenna configuration/element count.",
    "Azimuth": "Angular field of regard in azimuth (left-right).",
    "Elevation": "Angular field of regard in elevation (up-down).",
    "Platform": "Aircraft type(s) the system is fitted to.",
    "Search cone": "Total angular search coverage.",
    "Accuracy": "Positional/targeting accuracy.",
    "Missiles": "Missile types associated with the system.",
    "Radars": "Radar types associated with the system.",
}


def _alias_regex(word, pm):
    lead = r"(?:\+/-|\xb1)\s*" if pm else ""
    return re.compile(rf"{lead}({NUM})\s*(?:{word})(?![a-zA-Z])", re.IGNORECASE)


def _spans_overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def _to_float(numstr):
    """Parse a matched number, handling both thousands-grouped commas
    ("34,750" -> 34750) and the European decimal comma some non-US-sourced
    spec sheets use ("1,8" -> 1.8, "0,75" -> 0.75)."""
    s = numstr.strip()
    if "," in s:
        if re.fullmatch(r"\+?\d{1,3}(,\d{3})+(\.\d+)?", s):
            return float(s.replace(",", ""))
        if re.fullmatch(r"\+?\d+,\d{1,2}", s):
            return float(s.replace(",", "."))
        return float(s.replace(",", ""))
    return float(s)


def extract_quantity(text, dim_key):
    """Return (value, uom, note) extracted from text for the given dimension,
    or None if no recognizable quantity is present."""
    if dim_key == "currency":
        m = re.search(rf"(?:US)?\$\s*({NUM})\s*(million|billion|thousand|M|B|K)?(?![a-zA-Z])",
                       text, re.IGNORECASE)
        if not m:
            return None
        val = _to_float(m.group(1))
        suffix = (m.group(2) or "").lower()
        val *= {"million": 1e6, "m": 1e6, "billion": 1e9, "b": 1e9,
                "thousand": 1e3, "k": 1e3}.get(suffix, 1)
        return val, "USD", None

    if dim_key == "count":
        m = re.search(rf"(?<![A-Za-z])({NUM})(?![A-Za-z\d])", text)
        if not m:
            return None
        val = _to_float(m.group(0))
        note = None
        if text.strip().startswith(">"):
            note = 'source gives a lower bound ("greater than"); see description'
        elif "+" in text[:text.find(m.group(0)) + len(m.group(0)) + 2]:
            note = "source indicates additional units beyond this figure; see description"
        return val, "units", note

    dim = DIMENSIONS[dim_key]
    if dim.get("mach_special"):
        m = re.search(r"Mach\s*(\d+(?:[.,]\d+)?)", text)
        if m:
            return _to_float(m.group(1)), "Mach", None
        return None

    matches = []  # (start, end, value, is_metric, is_pm)
    for word, factor, is_metric in dim["aliases"]:
        for is_pm in (True, False):
            regex = _alias_regex(word, is_pm)
            for m in regex.finditer(text):
                span = m.span()
                if any(_spans_overlap(span, (s, e)) for s, e, *_ in matches):
                    continue
                num = _to_float(m.group(1))
                matches.append((span[0], span[1], num * factor, is_metric, is_pm))
    if not matches:
        return None
    matches.sort(key=lambda t: t[0])
    metric = [m for m in matches if m[3]]
    chosen = metric[0] if metric else matches[0]
    note = None
    if chosen[4]:
        note = "source gives a +/- (symmetric) range; see description"
    return chosen[2], dim["canonical"], note


def fmt_num(x):
    r = round(x, 4)
    if r == int(r):
        return str(int(r))
    return f"{r:.4f}".rstrip("0").rstrip(".")


def build_parametric(seq, canonical_field, raw_value):
    dim_key = FIELD_DIMENSION.get(canonical_field)
    definition = FIELD_DEFINITIONS.get(
        canonical_field, f"{canonical_field} (descriptive text, not a unit-bearing quantity)."
    )
    quantity = extract_quantity(raw_value, dim_key) if dim_key else None

    if quantity is None:
        parametric = {
            "seq": seq,
            "parameter": canonical_field,
            "parameterDescr": f"{definition} Source text: \"{raw_value}\".",
            "parameterUomValue": raw_value,
            "classification": "U",
            "parameterValue": raw_value,
            "uom": "",
        }
        return parametric, False

    value, uom, note = quantity
    value_str = fmt_num(value)
    descr = f"{definition} Source text: \"{raw_value}\"."
    if note:
        descr += f" Note: {note}."
    parametric = {
        "seq": seq,
        "parameter": canonical_field,
        "parameterDescr": descr,
        "parameterUomValue": f"{value_str} {uom}",
        "classification": "U",
        "parameterValue": value_str,
        "uom": uom,
    }
    return parametric, True


def first_sentence(text, limit=200):
    text = text.strip()
    m = re.search(r"(.{1,%d}?[.!?])(\s|$)" % limit, text)
    snippet = m.group(1) if m else text[:limit]
    if len(snippet) < len(text) and not m:
        snippet = snippet.rstrip() + "..."
    return snippet.strip()


# --------------------------------------------------------------------------- #
# Description bucketing: split full_text into Overview / History / Design /
# Usage / Variants using whatever internal structure the page has.
# --------------------------------------------------------------------------- #

BUCKET_ORDER = ["Overview", "History", "Design", "Usage", "Variants"]

EXTRA_HEADINGS = {
    "development history", "design and development", "combat history",
    "further developments", "related development", "country-specific modifications",
    "notable incidents", "production and operational history",
    "general characteristics", "performance", "preliminary specifications",
    "development and programme history", "description", "missiles", "radars",
    "avionics", "note", "notes", "guidance", "introduction",
}

TEMPLATE_SUBHEADS = {"general characteristics", "performance", "armament", "avionics", "propulsion"}
BOILERPLATE_STOP = {
    "references", "bibliography", "external links", "see also", "gallery", "notes", "note",
    "accidents", "accidents and incidents", "notable accidents", "notable accidents and incidents",
    "notable recent accidents and incidents", "aircraft on display", "preserved aircraft",
    "airworthy", "operators", "current", "former", "in popular culture",
    "notable appearances in media", "appearances in media",
}
SPEC_HEADINGS = {"specifications", "preliminary specifications"}
ALL_SPEC_LABELS = SPEC_HEADINGS | TEMPLATE_SUBHEADS | BOILERPLATE_STOP

STOP_RE = re.compile(
    r'specification|^references?$|bibliograph|external link|^see also$|^gallery$|'
    r'appearances in (media|popular culture)|^in popular culture$|^notes?$')
VARIANTS_RE = re.compile(r'\bvariant')
USAGE_RE = re.compile(
    r'operational|\bcombat\b|\boperator|\bservice\b|\bdeploy|\baccident|\bincident|'
    r'on display|\bpreserved|\bairworthy|\bbattalion|\boperation$|\bexercise\b|'
    r'export prospects|country-specific|\bwar\b|\bwars\b|demands on|^current$|^former$|'
    r'potential operators|future operators|cancelled operators|primary users|'
    r'potential sales|combat use')
HISTORY_RE = re.compile(
    r'\bdevelop|\bbackground|\borigin|^history$|\bprogram|\bbid$|\bprototype|\bschedule|'
    r'partnership|\bproduction\b|procurement|competition|upgrad|moderni[sz]|replacement|'
    r'\btesting\b|ban on exports|termination')
DESIGN_RE = re.compile(
    r'^design|construction|cockpit|avionics|armament|\bengine|powerplant|propulsion|'
    r'airframe|stealth|sensor|guidance|warhead|^description$|^missiles$|^radars$|'
    r'component|operating modes|technical characteristics|capabilit|requirements|'
    r'maintenance|^naming|general characteristics|^performance$|off-boresight|detonation')
OVERVIEW_RE = re.compile(r'^overview$|^introduction$|general information|^summary$')


def normalize_heading(text):
    text = re.sub(r'\([^)]*\)', '', text)
    text = text.strip().lower()
    text = re.sub(r'[:.]+$', '', text).strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def build_heading_whitelist(pages_dir):
    wl = set()
    for fp in glob.glob(os.path.join(pages_dir, "*.json")):
        if os.path.basename(fp) in SKIP_FILES:
            continue
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        for s in d.get("sections") or []:
            wl.add(normalize_heading(s))
    wl |= EXTRA_HEADINGS
    return wl


def extract_label(para, whitelist):
    """Detect a heading at the start of `para`, either on its own line
    ("DEVELOPMENT\\nrest...") or inline with a colon ("Development: rest...").
    Returns (normalized_label_or_None, remaining_text)."""
    first_line, _, remainder = para.partition('\n')
    norm = normalize_heading(first_line)
    if norm in whitelist and len(first_line) <= 80:
        return norm, remainder.strip()
    m = re.match(r'^([^:\n]{1,90}):\s+', para)
    if m:
        norm2 = normalize_heading(m.group(1))
        if norm2 in whitelist:
            return norm2, para[m.end():].strip()
    return None, para


def classify_heading(norm):
    if not norm:
        return None
    if STOP_RE.search(norm):
        return "STOP"
    if VARIANTS_RE.search(norm):
        return "Variants"
    if USAGE_RE.search(norm):
        return "Usage"
    if HISTORY_RE.search(norm):
        return "History"
    if DESIGN_RE.search(norm):
        return "Design"
    if OVERVIEW_RE.search(norm):
        return "Overview"
    return None


def content_bucket_hint(para):
    b = classify_heading(normalize_heading(para[:100]))
    return b if b not in (None, "STOP") else None


def looks_like_kv_dump(para):
    """Detect a raw infobox echo (several "Key: value" lines back to back)
    embedded at the top of some hand-built full_text fields -- not prose,
    skip it (the same data is already captured in parametrics)."""
    lines = [l for l in para.split('\n') if l.strip()]
    if len(lines) < 2:
        return False
    hits = sum(1 for l in lines if re.match(r'^[A-Za-z][\w /]{1,30}:\s', l))
    return hits >= 2 and hits >= len(lines) - 1


def is_meta_note(para):
    low = para.lower()
    return low.startswith('note:') and ('fetched content' in low or 'this article' in low or 'the article' in low)


def build_descriptions(page, whitelist):
    full_text = page.get("full_text", "") or ""
    paras = [p.strip() for p in full_text.split('\n\n') if p.strip()]
    buckets = {b: [] for b in BUCKET_ORDER}
    current = "Overview"
    seen_any = False  # the very first real paragraph is always lead/Overview
                       # material -- only content-hint (not label) switching
                       # is suppressed for it, so a genuine heading can still
                       # redirect it.

    for para in paras:
        if looks_like_kv_dump(para) or is_meta_note(para):
            continue
        label, rest = extract_label(para, whitelist)
        if label is not None:
            bucket = classify_heading(label)
            if bucket == "STOP":
                break
            if bucket:
                current = bucket
            text = rest
        else:
            if seen_any:
                hint = content_bucket_hint(para)
                if hint:
                    current = hint
            text = para
        if text:
            buckets[current].append(text)
            seen_any = True

    if not buckets["Overview"]:
        summary = (page.get("summary") or "").strip()
        if summary:
            buckets["Overview"] = [summary]

    descriptions = []
    for b in BUCKET_ORDER:
        if buckets[b]:
            text = "\n\n".join(buckets[b])
            descriptions.append({
                "description": text,
                "shortDescription": first_sentence(text),
                "descrType": b,
                "classification": "U",
            })
    return descriptions


# --------------------------------------------------------------------------- #
# Specifications-block harvesting: several aircraft/helicopter pages carry
# their real dimensions (Length, Wingspan, Empty weight, Maximum speed, ...)
# only as a "Specifications" bullet block inside full_text, not in `specs`.
# --------------------------------------------------------------------------- #

FIELD_LINE_RE = re.compile(r'^([A-Za-z][A-Za-z0-9 /\-\.]{1,45}):\s+(.+)$', re.DOTALL)


def find_spec_block_fields(full_text):
    paras = [p.strip() for p in full_text.split('\n\n') if p.strip()]
    fields = []
    in_block = False
    for para in paras:
        label, _ = extract_label(para, ALL_SPEC_LABELS)
        if not in_block:
            if label in SPEC_HEADINGS:
                in_block = True
            continue
        if label in BOILERPLATE_STOP:
            break
        if label in TEMPLATE_SUBHEADS:
            continue
        m = FIELD_LINE_RE.match(para)
        if m:
            fields.append((m.group(1).strip(), m.group(2).strip()))
    return fields


# --------------------------------------------------------------------------- #
# Record assembly
# --------------------------------------------------------------------------- #

def build_record(page, model_id, updated_date, whitelist):
    specs = dict(page.get("specs") or {})

    for raw_name, raw_value in find_spec_block_fields(page.get("full_text", "") or ""):
        canon = canonical_name(raw_name)
        if canon in {canonical_name(k) for k in specs}:
            continue
        # Skip nested sub-breakdowns already folded into a broader field's
        # value (e.g. a "Missiles"/"Rockets"/"Bombs" line that's just a
        # verbatim excerpt of the "Hardpoints" armament list captured above).
        # Length-gated so short values (e.g. Crew "1") don't trivially match
        # as a substring of an unrelated longer field.
        if len(raw_value) >= 8 and any(
            isinstance(v, str) and raw_value in v for v in specs.values()
        ):
            continue
        specs[raw_name] = raw_value

    seq = 1001
    parametrics = []
    numeric_count = 0
    total_count = 0
    unparsed = []

    for raw_name, raw_value in specs.items():
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        canonical_field = canonical_name(raw_name)
        parametric, was_numeric = build_parametric(seq, canonical_field, raw_value)
        parametrics.append(parametric)
        total_count += 1
        if was_numeric:
            numeric_count += 1
        elif FIELD_DIMENSION.get(canonical_field):
            unparsed.append((canonical_field, raw_value))
        seq += 1

    descriptions = build_descriptions(page, whitelist)

    media = []
    url = page.get("url")
    if url:
        media.append({
            "seq": 1,
            "mediaID": model_id * 100 + 1,
            "url": url,
            "title": "Wikipedia source page",
        })

    category, subcategory = page.get("category"), page.get("subcategory")
    system_group, system_type = resolve_group_type(category, subcategory)

    record = {
        "modelID": model_id,
        "systemGroup": system_group,
        "systemType": system_type,
        "nomenclature": page.get("title", ""),
        "updatedDate": updated_date,
        "descriptions": descriptions,
        "media": media,
        "parametrics": parametrics,
    }
    stats = {
        "total": total_count,
        "numeric": numeric_count,
        "text": total_count - numeric_count,
        "unparsed_numeric_fields": unparsed,
        "descrTypes": [d["descrType"] for d in descriptions],
        "systemType": system_type,
    }
    return record, stats


def write_type_schemas(records, out_dir):
    """Aggregate the parametrics vocabulary actually observed per systemType
    and write it out as a reference manifest, schemas/<slug>.schema.json."""
    schemas_dir = os.path.join(os.path.dirname(out_dir) or ".", "schemas")
    os.makedirs(schemas_dir, exist_ok=True)

    by_type = {}
    for rec in records:
        key = (rec["systemGroup"], rec["systemType"])
        entry = by_type.setdefault(key, {})
        for d in rec["descriptions"]:
            pass
        for p in rec["parametrics"]:
            name = p["parameter"]
            if name not in entry:
                entry[name] = {
                    "parameter": name,
                    "uom": p["uom"],
                    "parameterDescr": FIELD_DEFINITIONS.get(
                        name, f"{name} (descriptive text, not a unit-bearing quantity)."
                    ),
                    "count": 0,
                }
            entry[name]["count"] += 1

    written = []
    for (group, typ), fields in sorted(by_type.items()):
        slug = re.sub(r'[^a-z0-9]+', '_', typ.lower()).strip('_')
        out = {
            "systemGroup": group,
            "systemType": typ,
            "descriptionTypes": BUCKET_ORDER,
            "parametrics": sorted(
                fields.values(), key=lambda f: (-f["count"], f["parameter"])
            ),
        }
        path = os.path.join(schemas_dir, f"{slug}.schema.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        written.append(path)
    return written


def main():
    pages_dir = sys.argv[1] if len(sys.argv) > 1 else "pages"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "pages_schema"
    os.makedirs(out_dir, exist_ok=True)

    whitelist = build_heading_whitelist(pages_dir)

    files = sorted(
        f for f in glob.glob(os.path.join(pages_dir, "*.json"))
        if os.path.basename(f) not in SKIP_FILES
    )

    updated_date = datetime.date.today().isoformat()

    print(f"{'file':45} {'numeric':>8} {'text':>6} {'total':>6}  descrTypes")
    records = []
    for i, fp in enumerate(files, start=1):
        with open(fp, "r", encoding="utf-8") as fh:
            page = json.load(fh)

        override = CATEGORY_OVERRIDES.get(os.path.basename(fp))
        if override:
            page["category"], page["subcategory"] = override

        model_id = 2000 + i
        record, stats = build_record(page, model_id, updated_date, whitelist)
        out_path = os.path.join(out_dir, os.path.basename(fp))
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)
        records.append(record)
        name = os.path.basename(fp)
        print(f"{name:45} {stats['numeric']:>8} {stats['text']:>6} {stats['total']:>6}  {stats['descrTypes']}")
        for field, raw in stats["unparsed_numeric_fields"]:
            print(f"    ! {field!r} expected a numeric quantity but none was found in: {raw!r}")

    print(f"\nWrote {len(files)} records to {out_dir}/")

    schema_paths = write_type_schemas(records, out_dir)
    print(f"Wrote {len(schema_paths)} per-systemType schemas to schemas/")


if __name__ == "__main__":
    main()
