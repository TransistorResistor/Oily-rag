#!/usr/bin/env python3
"""
refcat.py - the reference catalogue + data-dictionary + deterministic claim->field
mapping for the reverse-enrichment demo.

WHY test_records/ AND NOT rag_test.db
--------------------------------------
The task describes "~25 linked air-defence records (S-300/S-400 ... Python-5,
Derby, ASRAAM ...)". Those records live ONLY in ``test_records/*.json`` (canonical
exemplar-v2 shape). The on-disk ``rag_test.db`` is a *stale, unrelated* 63-record
Wikipedia corpus (F-22, M1 Abrams, Tomahawk, ...) -- it was never rebuilt from the
new corpus (verified: ``records LIKE '%S-400 Triumf%'`` -> 0 rows). Using
test_records/ as the reference catalogue is therefore both (a) faithful to the
dataset the task actually means and (b) automatically honours the HARD RULE that
rag_test.db is never touched -- we never even open it.

The records are parsed with the repo's own ``record_model.normalize_record`` so the
pipeline sees exactly the canonical model (params/relations/aliases/proliferations)
the rest of the system uses, and ``units.py`` is reused for unit normalisation.
"""

import glob
import os
import re
import sys

# reuse the repo's canonical parser + unit layer (one dir up)
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
import record_model  # noqa: E402
import units  # noqa: E402

_UNIT_SPELL = {
    "minute": "min", "minutes": "min", "min": "min", "mins": "min",
    "second": "s", "seconds": "s", "sec": "s", "secs": "s", "s": "s",
    "hour": "h", "hours": "h", "hr": "h", "hrs": "h", "h": "h",
    "kilometer": "km", "kilometers": "km", "kilometre": "km",
    "kilometres": "km", "km": "km", "kms": "km",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m", "m": "m",
    "kilogram": "kg", "kilograms": "kg", "kg": "kg", "kgs": "kg",
    "nauticalmile": "nm", "nauticalmiles": "nm", "nmi": "nm", "nm": "nm",
    "mile": "mi", "miles": "mi", "mi": "mi", "foot": "ft",
    "feet": "ft", "ft": "ft", "mach": "mach",
}


def _unit_key(unit):
    key = re.sub(r"[\s._]", "", (unit or "").strip().lower())
    return _UNIT_SPELL.get(key, key)

REF_DIR = os.path.join(_REPO, "test_records")

# --------------------------------------------------------------------------- #
# Data dictionary: canonical parameter -> definition, and attribute synonyms   #
# --------------------------------------------------------------------------- #
# The canonical parameter names + their fixed data-dictionary definitions
# (parameterDescr) are discovered from the corpus at load time. SYNONYMS maps the
# free-text `attribute` a cheap LLM emits ("range", "top speed", "operator") onto
# a canonical field. Deliberately hand-curated and conservative: an attribute we
# can't confidently map is left unmapped (parked), never guessed onto a field.
SYNONYMS = {
    # weapon range
    "range": "Maximum range",
    "maximum range": "Maximum range",
    "max range": "Maximum range",
    "maximum engagement range": "Maximum range",
    "engagement range": "Maximum range",
    "effective range": "Maximum range",
    "maximum effective range": "Maximum range",
    "intercept range": "Maximum range",
    # radar / detection
    "detection range": "Detection range",
    "radar range": "Detection range",
    "radar detection range": "Radar detection range",
    # altitude
    "altitude": "Maximum altitude",
    "maximum altitude": "Maximum altitude",
    "max altitude": "Maximum altitude",
    "engagement altitude": "Maximum altitude",
    "ceiling": "Maximum altitude",
    "altitude ceiling": "Maximum altitude",
    "service ceiling": "Service ceiling",
    # speed
    "speed": "Maximum speed",
    "maximum speed": "Maximum speed",
    "max speed": "Maximum speed",
    "top speed": "Maximum speed",
    # time
    "deployment time": "Deployment time",
    "setup time": "Deployment time",
    "emplacement time": "Deployment time",
    "ready time": "Deployment time",
    "reaction time": "Deployment time",
    # mass / size
    "weight": "Weight",
    "launch weight": "Weight",
    "mass": "Weight",
    "warhead weight": "Warhead weight",
    "warhead mass": "Warhead weight",
    "length": "Length",
    "diameter": "Diameter",
    "wingspan": "Wingspan",
    # counts / misc
    "introduced": "Introduced",
    "entered service": "Introduced",
    "simultaneous targets": "Simultaneous targets engaged",
    "targets engaged": "Simultaneous targets engaged",
    "simultaneous targets engaged": "Simultaneous targets engaged",
    "simultaneous targets tracked": "Simultaneous targets tracked",
    "off-boresight launch angle": "Off-boresight launch angle",
    "unit cost": "Unit cost",
    "cost": "Unit cost",
}

# attribute phrases meaning "country X operates/produces record Y" -> proliferation
OPERATOR_ATTRS = {
    "operator", "operators", "operated by", "operates", "operate", "operation",
    "user", "users", "in service with", "fielded by", "deployed by", "adopted by",
}
# attribute phrases meaning "record also known as / designated ..." -> alias
ALIAS_ATTRS = {
    "alias", "aliases", "also known as", "aka", "designation", "designated",
    "nato reporting name", "reporting name", "known as", "nickname",
}
# attribute/verb phrases naming a relation between two records
RELATION_ATTRS = {
    "fired from", "launched from", "fired by", "launched by", "carries",
    "carried by", "employs", "employed by", "fitted with", "fitted to",
    "armed with", "integrated with", "equips", "equipped with", "uses",
    "fires", "launches", "component of", "part of",
}

# Static world-country name list (no new deps). Used ONLY on the operator/
# proliferation branch, gated by BOTH an operator-type attribute AND the country
# appearing literally in the claim's quote -- this is what lets genuinely NEW
# operators (Kuwait, Algeria) surface, which the closed rc.countries set (built
# only from existing proliferations) can never see. NOT used for entity linking
# or the second-signal precision gate (which stays on per-record rc.operators).
WORLD_COUNTRIES = [
    "Afghanistan", "Albania", "Algeria", "Angola", "Argentina", "Armenia",
    "Australia", "Austria", "Azerbaijan", "Bahrain", "Bangladesh", "Belarus",
    "Belgium", "Bolivia", "Bosnia", "Botswana", "Brazil", "Brunei", "Bulgaria",
    "Cambodia", "Cameroon", "Canada", "Chad", "Chile", "China", "Colombia",
    "Croatia", "Cuba", "Cyprus", "Czech Republic", "Czechia", "Denmark",
    "Djibouti", "Ecuador", "Egypt", "Eritrea", "Estonia", "Ethiopia", "Finland",
    "France", "Gabon", "Georgia", "Germany", "Ghana", "Greece", "Guatemala",
    "Hungary", "Iceland", "India", "Indonesia", "Iran", "Iraq", "Ireland",
    "Israel", "Italy", "Ivory Coast", "Japan", "Jordan", "Kazakhstan", "Kenya",
    "Kuwait", "Kyrgyzstan", "Laos", "Latvia", "Lebanon", "Libya", "Lithuania",
    "Luxembourg", "Malaysia", "Mali", "Malta", "Mauritania", "Mexico", "Moldova",
    "Mongolia", "Montenegro", "Morocco", "Mozambique", "Myanmar", "Namibia",
    "Nepal", "Netherlands", "New Zealand", "Nigeria", "North Korea",
    "North Macedonia", "Norway", "Oman", "Pakistan", "Panama", "Paraguay",
    "Peru", "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia",
    "Rwanda", "Saudi Arabia", "Senegal", "Serbia", "Singapore", "Slovakia",
    "Slovenia", "Somalia", "South Africa", "South Korea", "South Sudan", "Spain",
    "Sri Lanka", "Sudan", "Sweden", "Switzerland", "Syria", "Taiwan",
    "Tajikistan", "Tanzania", "Thailand", "Tunisia", "Turkey", "Turkmenistan",
    "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom",
    "United States", "Uruguay", "Uzbekistan", "Venezuela", "Vietnam", "Yemen",
    "Zambia", "Zimbabwe",
]

# Abbreviations found in tabular / terse headers, expanded token-wise before the
# attribute is matched against SYNONYMS/canonical fields. Keyed WITH and WITHOUT a
# trailing dot (headers write "Det." and "Radar Det. Range").
_ABBREV = {
    "det": "detection", "det.": "detection",
    "simult": "simultaneous", "simult.": "simultaneous",
    "max": "maximum", "max.": "maximum",
    "min": "minimum", "min.": "minimum",
    "deploy": "deployment",
    "engmt": "engagement", "engmt.": "engagement",
    "alt": "altitude", "alt.": "altitude",
    "rng": "range", "rng.": "range",
    "wt": "weight", "wt.": "weight",
    "no": "number", "no.": "number",
}


def _expand_abbrev(a):
    """Token-wise abbreviation expansion ('radar det. range' -> 'radar detection
    range'). Leaves unknown tokens untouched."""
    out = []
    for t in a.split():
        rep = _ABBREV.get(t) or _ABBREV.get(t.rstrip("."))
        out.append(rep if rep else t)
    return " ".join(out)


# domain terms that mark a mention as genuinely defence-related (second signal)
DOMAIN_TERMS = [
    "missile", "sam", "surface-to-air", "air-to-air", "air defence",
    "air defense", "radar", "interceptor", "launcher", "seeker", "warhead",
    "fighter", "aircraft", "aam", "bvr", "beyond-visual-range", "battalion",
    "battery", "engagement", "supersonic", "phased-array", "phased array",
    "guidance", "infrared", "anti-aircraft", "munition", "airframe", "sortie",
]

# single-word aliases that collide with common English / other domains. An
# ambiguous alias CANNOT act as another entity's corroborating "second entity"
# signal, and on its own needs a domain term or an operator-country signal --
# this is what defeats the "Python programming" / "Apache Derby" distractors.
AMBIGUOUS_ALIASES = {
    "python", "derby", "alto", "triumph", "triumf", "felon", "raptor",
    "sparrow", "hawk", "growler", "gargoyle", "grumble", "lightning ii",
    "big bird", "cheese board", "grave stone", "flap lid",
}


def _canon_num(s):
    """Numeric prefix of a string like '380 km' -> 380.0, else None."""
    if s is None:
        return None
    m = re.match(r"^\s*[-+]?\d[\d,]*(?:\.\d+)?", str(s))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


class RefCatalogue:
    def __init__(self, records):
        self.records = records                       # {model_id: canon}
        self.dict_fields = {}                         # name -> {descr,unit,dtype}
        self.alias_index = {}                         # alias_lc -> [model_id,...]
        self.countries = set()                        # all known operator countries
        self._build()

    # ---- loading ---------------------------------------------------------- #
    def _build(self):
        pairs = []
        extra_aliases = {}
        for mid, c in self.records.items():
            pairs.append((mid, c.get("title")))
            names = list(c.get("aliases") or [])
            extra_aliases[mid] = names
            # data dictionary
            for p in c.get("params", []):
                nm = p.get("name")
                if not nm:
                    continue
                d = self.dict_fields.setdefault(
                    nm, {"descr": None, "units": set(), "dtype": None})
                if p.get("descr") and not d["descr"]:
                    d["descr"] = p["descr"]
                if p.get("unit"):
                    d["units"].add(p["unit"])
                if p.get("dtype") and not d["dtype"]:
                    d["dtype"] = p["dtype"]
            for ctry in c.get("extras", {}).get("Operated by (country)", []) or []:
                self.countries.add(ctry)
            for ctry in c.get("extras", {}).get("Produced by (country)", []) or []:
                self.countries.add(ctry)
        # alias table via the repo's own builder (title designations + curated)
        self.alias_index = record_model.build_alias_table(pairs, extra_aliases)
        # Inject bare family names that the title-derived builder misses because
        # the title is hyphenated ("Python-5" never yields bare "python"). These
        # ARE how people refer to the family and are exactly the collision-prone
        # tokens the second-signal gate must survive -- so the distractor test is
        # only genuine if they are in the lexicon.
        for title_key, bare in (("Python-5", "python"), ("Derby", "derby"),
                                ("ASRAAM", "asraam")):
            for mid, c in self.records.items():
                if c.get("title") == title_key:
                    self.alias_index.setdefault(bare, [])
                    if mid not in self.alias_index[bare]:
                        self.alias_index[bare].append(mid)

    # ---- record accessors ------------------------------------------------- #
    def title(self, mid):
        return self.records[mid].get("title")

    def operators(self, mid):
        ex = self.records[mid].get("extras", {})
        return set(ex.get("Operated by (country)", []) or [])

    def aliases(self, mid):
        return set(a.lower() for a in (self.records[mid].get("aliases") or []))

    def db_values(self, mid, field):
        """All raw parametric values a record holds for a canonical field."""
        out = []
        for p in self.records[mid].get("params", []):
            if p.get("name") == field and p.get("value") not in (None, ""):
                out.append((p.get("value"), p.get("unit")))
        return out

    def field_unit(self, field):
        d = self.dict_fields.get(field)
        if d and d["units"]:
            return sorted(d["units"])[0]
        return None

    def field_dtype(self, field):
        d = self.dict_fields.get(field)
        return d["dtype"] if d else None

    def relations(self, mid):
        """set of related child/parent names (lowercased) for edge dedup."""
        out = set()
        for r in self.records[mid].get("relations", []):
            for k in ("child_name", "parent_name"):
                if r.get(k):
                    out.add(str(r[k]).lower())
        return out

    # ---- mapping ---------------------------------------------------------- #
    def map_attribute(self, attribute):
        """free-text attribute -> canonical field name, or None."""
        if not attribute:
            return None
        a = attribute.strip().lower()
        if a in SYNONYMS:
            return SYNONYMS[a]
        # tolerate trailing/leading noise ("maximum range of", "the range")
        a2 = re.sub(r"^(the|a|an|its|maximum|max)\s+", "", a)
        a2 = re.sub(r"\s+(of|is|for|to)$", "", a2)
        if a2 in SYNONYMS:
            return SYNONYMS[a2]
        # substring hit on a known canonical field name
        for canon in self.dict_fields:
            if a == canon.lower():
                return canon
        # ---- (a) abbreviation expansion, then retry exact ----------------- #
        # tabular headers are terse/abbreviated ("Deploy Time", "Radar Det.
        # Range", "Simult. Targets Engaged"); expand and re-match exactly.
        exp = _expand_abbrev(a)
        if exp != a:
            if exp in SYNONYMS:
                return SYNONYMS[exp]
            for canon in self.dict_fields:
                if exp == canon.lower():
                    return canon
        # ---- (b) token-subset match --------------------------------------- #
        # a known SYNONYMS key / canonical field whose tokens are ALL present in
        # the (expanded) attribute ("engagement ceiling" -> ceiling). Conservative:
        # accept ONLY if exactly one distinct canonical field wins; any tie parks.
        # Token-level only -- deliberately no edit-distance fuzzy matching.
        attr_tokens = set(re.findall(r"[a-z]+", exp))
        if attr_tokens:
            cands = set()
            for key, fld in SYNONYMS.items():
                kt = set(re.findall(r"[a-z]+", key))
                if kt and kt <= attr_tokens:
                    cands.add(fld)
            for canon in self.dict_fields:
                kt = set(re.findall(r"[a-z]+", canon.lower()))
                # multi-token field names only: a single generic field token
                # ("Crew", "Type", "Radar") must NOT swallow a qualified phrase
                # ("crew rotation cycle" -> Crew) and manufacture a proposal.
                if len(kt) >= 2 and kt <= attr_tokens:
                    cands.add(canon)
            if len(cands) == 1:
                return next(iter(cands))
        return None

    def compare_numeric(self, value_raw, unit_raw, field, mid):
        """Compare a doc value against the record's DB value(s) for `field`.
        Returns ('match'|'conflict'|'gap', normalized_value, normalized_unit,
        db_repr). Uses units.py to convert into the DB field's canonical unit."""
        canon_unit = self.field_unit(field)
        dv = self.db_values(mid, field)
        val_num = _canon_num(value_raw)
        # normalise the doc value into the field's canonical unit when possible
        norm_val, norm_unit = val_num, (unit_raw or canon_unit)
        if val_num is not None and unit_raw and canon_unit:
            try:
                norm_val = units.convert(val_num, unit_raw, canon_unit)
                norm_unit = canon_unit
            except units.ConversionError:
                norm_val, norm_unit = val_num, unit_raw
        if not dv:
            return "gap", norm_val, norm_unit, None
        # compare against every existing variant value
        db_reprs = []
        compared = False
        incomparable = False
        for (dval, dunit) in dv:
            dnum = _canon_num(dval)
            db_reprs.append(f"{dval}"
                            + (f" {dunit}" if dunit else ""))
            if dnum is None or norm_val is None:
                if str(dval).strip().lower() == str(value_raw).strip().lower():
                    return "match", norm_val, norm_unit, "; ".join(db_reprs)
                continue
            cmpv = norm_val
            if norm_unit and dunit and _unit_key(norm_unit) != _unit_key(dunit):
                try:
                    cmpv = units.convert(norm_val, norm_unit, dunit)
                except units.ConversionError:
                    incomparable = True
                    continue
            compared = True
            if abs(cmpv - dnum) <= max(1e-6, abs(dnum) * 0.02):   # 2% tolerance
                return "match", norm_val, norm_unit, "; ".join(db_reprs)
        if incomparable and not compared:
            return "incomparable", norm_val, norm_unit, "; ".join(db_reprs)
        return "conflict", norm_val, norm_unit, "; ".join(db_reprs)

    # ---- entity linking helpers ------------------------------------------- #
    def find_mentions(self, text):
        """Deterministic alias scan. Returns list of
        (model_id, alias_matched, is_ambiguous, span_start). Case-insensitive,
        word-boundary matched."""
        low = text.lower()
        hits = []
        for alias, mids in self.alias_index.items():
            if len(alias) < 2:
                continue
            # word-boundary search (aliases can contain - and digits)
            for m in re.finditer(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])",
                                 low):
                amb = alias in AMBIGUOUS_ALIASES
                for mid in mids:
                    hits.append((mid, alias, amb, m.start()))
        return hits


def load_reference(ref_dir=REF_DIR):
    records = {}
    for f in sorted(glob.glob(os.path.join(ref_dir, "*.json"))):
        if "exemplar_schema" in os.path.basename(f):
            continue
        import json
        raw = json.load(open(f, encoding="utf-8"))
        canon = record_model.normalize_record(raw)
        mid = str(canon.get("id"))
        canon["_file"] = os.path.basename(f)
        records[mid] = canon
    return RefCatalogue(records)


if __name__ == "__main__":
    rc = load_reference()
    print(f"loaded {len(rc.records)} reference records")
    print(f"data-dictionary fields: {len(rc.dict_fields)}")
    print(f"alias index size: {len(rc.alias_index)}")
    print(f"known operator countries: {len(rc.countries)}")
    # smoke: mentions in a sample line
    for mid, al, amb, pos in rc.find_mentions(
            "The S-400 fired a 40N6 missile; Python was also seen.")[:10]:
        print("  mention", rc.title(mid), "via", repr(al), "ambiguous" if amb else "")
