#!/usr/bin/env python3
"""
pipeline.py - orchestration for one enrichment run.

Flow per document (see README for the design contract):
  provider -> (skip if content-hash already in docs_seen) -> ONE LLM claim
  extraction call -> deterministic: entity-linking (second-signal gate) ->
  claim->field mapping (data dictionary) -> validation (quote-grounding, unit
  normalisation, dedup/conflict) -> fingerprints -> park or surface.
After all docs: a pure-SQL graduation pass re-checks parked 'uncorroborated'
claims, then the proposals threshold-view is rematerialised.

Rule throughout: propose facts, never LLM-drafted prose.
"""

import hashlib
import json
import re

import provider
import refcat as refcat_mod
import state as state_mod
import llm as llm_mod

CORROBORATION_THRESHOLD = 2      # sources needed to graduate an uncorroborated claim

# provenance cues that mark a *document* as low-trust (single-source claims from
# it park as 'uncorroborated' until a second source corroborates). These are
# PROVENANCE hedges, distinct from VALUE hedges ("up to","estimated") which only
# annotate a claim's qualifier and do NOT lower trust.
_LOWTRUST_CUES = re.compile(
    r"(?i)unconfirmed|analysts?\s+(?:speculate|believe|estimate|assess)|"
    r"not\s+been\s+officially\s+confirmed|leaked|social media|unverified|"
    r"rumou?red|reportedly circulating|open-source speculation")

# Claim-level assertion safety. Capability language such as "can engage" remains
# assertive; negated and explicitly hypothetical statements park before mapping.
_NONASSERTED_CUES = re.compile(
    r"(?i)\b(?:no|not|never|neither|without|cannot|can't|doesn't|does\s+not|"
    r"didn't|did\s+not|won't|wouldn't|might|may|could|proposed|planned|"
    r"hypothetical|conceptual)\b")


def _sha(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


# Markdown syntax that md-render mode injects but text mode never emits. Stripped
# in the grounding path so (a) pipe-separated cells like "| 20 | min |" collapse
# to "20 min" and unit-adjacency / value-grounding still fire, and (b) citation
# quotes are not polluted with `|`, `**`, `#`. A pure no-op for text mode (which
# contains none of these characters), so text-mode behaviour is unchanged.
_MD_TABLE_SEP = re.compile(r"(?m)^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _strip_md(s):
    s = (s or "")
    s = _MD_TABLE_SEP.sub(" ", s)          # drop |---|---| separator rows
    s = s.replace("|", " ")                # table cell separators -> spaces
    s = s.replace("`", "")
    s = re.sub(r"\*+", "", s)              # **bold** / *italic*
    s = re.sub(r"(?m)^\s*#{1,6}\s*", "", s)  # heading markers
    return s


def _norm_for_ground(s):
    s = _strip_md((s or "").lower())
    for d in ("–", "—", "‑", "‒"):
        s = s.replace(d, "-")
    s = s.replace(",", "")
    s = re.sub(r"\s+", " ", s)
    return s


def _value_token(value_raw):
    """The literal token to quote-ground: the numeric prefix if present, else the
    whole value string."""
    m = re.match(r"^\s*([-+]?\d[\d,]*(?:\.\d+)?)", str(value_raw or ""))
    if m:
        return m.group(1).replace(",", "")
    return str(value_raw or "").strip()


def _tok_re(tok):
    """Match a token, digit-boundary-aware so '40' does not match inside 'S-400'
    and '8' does not match inside '2800'."""
    t = re.escape(_norm_for_ground(tok))
    if re.match(r"^[\d.]", tok):
        return r"(?<!\d)" + t + r"(?!\d)"
    return r"(?<![\w-])" + t + r"(?![\w-])"


def _contains(haystack, tok):
    return re.search(_tok_re(tok), _norm_for_ground(haystack)) is not None


def _sentence_with(doc_text, token):
    # collapse ALL whitespace first: PyMuPDF hard-wraps lines mid-sentence
    # ("...of 8\nminutes..."), so splitting on raw '\n' would sever a value from
    # its unit. Split only on real sentence punctuation. Markdown markers are
    # stripped first so a cited grounding sentence from a pipe table reads as
    # clean prose ("Deploy Time 8 min ...") rather than "| Deploy Time | 8 min |".
    flat = re.sub(r"\s+", " ", _strip_md(doc_text))
    for sent in re.split(r"(?<=[.!?])\s+", flat):
        if _contains(sent, token):
            return sent.strip()
    return None


def ground(value_raw, quote, doc_text):
    """Quote-grounding anti-hallucination guard. Returns ('quote'|'doc'|None,
    grounding_sentence). 'None' => the value appears nowhere -> reject."""
    tok = _value_token(value_raw)
    if not tok:
        return None, None
    if quote and _contains(quote, tok):
        return "quote", quote
    sent = _sentence_with(doc_text, tok)
    if sent:
        return "doc", sent
    return None, None


def _is_nonasserted(text):
    return bool(_NONASSERTED_CUES.search(text or ""))


def _unit_adjacent(doc_text, value_raw, unit_raw):
    """True if the model's unit actually appears next to its value in the doc
    (tolerant of the newline wrapping). Guards against a cheap model FABRICATING
    a unit for an explicitly unitless value ('5' -> value 5, unit 'days')."""
    numtok = _value_token(value_raw)
    if not unit_raw or not numtok:
        return True
    flat = _norm_for_ground(doc_text)
    n = re.escape(_norm_for_ground(numtok))
    u = re.escape(_norm_for_ground(unit_raw))
    # accept BOTH orderings: '<number><unit>' (380 km) AND '<unit><number>'
    # (Mach 3, USD 300) -- unit-before-value is common for speeds/currencies.
    pat = r"(?<!\d)" + n + r"\s*" + u + r"|" + u + r"\s*" + n + r"(?!\d)"
    return re.search(pat, flat) is not None


def _clean_value_unit(value_raw, unit_raw):
    """Cheap models fold the unit into value ('380 km'). Split it back out so the
    unit is populated and the value is bare."""
    v = str(value_raw or "").strip()
    u = (unit_raw or "").strip()
    m = re.match(r"^\s*([-+]?\d[\d,]*(?:\.\d+)?)\s*([A-Za-z%/°µ]+.*)?$", v)
    if m:
        num, tail = m.group(1), (m.group(2) or "").strip()
        if not u and tail:
            u = tail
        v = num
        return v, u
    # unit-before-value order ('Mach 3', 'USD 300 million'): the model folded a
    # leading unit token into value. Split it back out so the value is bare and
    # the unit is populated, mirroring the '<number><unit>' case above.
    m2 = re.match(r"^\s*([A-Za-z$€£][A-Za-z$€£.]*)\s+([-+]?\d[\d,]*(?:\.\d+)?)\s*$", v)
    if m2:
        lead, num = m2.group(1).strip(), m2.group(2)
        if not u:
            u = lead
        v = num
        return v, u
    m3 = re.match(
        r"^\s*([A-Za-z]+)\s+([-+]?\d[\d,]*(?:\.\d+)?)\s+"
        r"(thousand|million|billion|trillion)\s*$", v, re.IGNORECASE)
    if m3:
        lead, num, scale = m3.groups()
        if not u:
            u = f"{lead} {scale}"
        v = num
    return v, u


# --------------------------------------------------------------------------- #
# Header-unit / dual-unit handling (Phase B). All deterministic, mode-agnostic. #
# --------------------------------------------------------------------------- #
# Known unit tokens (lowercased) accepted from a header parenthetical "(km)" or a
# units column. Conservative allowlist so a NON-unit parenthetical such as
# "(per battery)" or "(assessed maximum)" is never mistaken for a unit slot.
_KNOWN_UNITS = {
    "km", "nm", "nmi", "mi", "m", "cm", "mm", "ft", "kg", "g", "t", "lb",
    "kn", "kt", "kts", "mach", "min", "s", "sec", "h", "hr", "hrs",
    "deg", "degree", "degrees", "usd", "m/s", "km/h", "kph", "mph",
}

# length/distance -> km, for the dual-value mutual-consistency cross-check ONLY
# (pint is unavailable in this env, so units.convert cannot be relied on here).
# Used to verify the two numbers in an "18/10 km/nm" cell denote one distance.
_LEN_TO_KM = {
    "km": 1.0, "nm": 1.852, "nmi": 1.852, "mi": 1.609344,
    "m": 0.001, "cm": 1e-5, "mm": 1e-6, "ft": 0.0003048,
}

# a header cell like "Maximum Range (km)" or "Max range (km/nm)". Group 1 is the
# attribute phrase, group 2 the parenthetical unit(s).
_HDR_UNIT_RE = re.compile(r"([A-Za-z][A-Za-z .&/-]{2,40}?)\s*\(([^)]{1,14})\)")


def _known_unit(u):
    return (u or "").strip().lower() in _KNOWN_UNITS


def _num_or_none(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _split_attr_unit(attr):
    """Header-unit-in-attribute (5a): 'maximum altitude (km)' -> ('maximum
    altitude', 'km'); 'maximum range (km/nm)' -> ('maximum range', 'km/nm').
    Only strips the trailing parenthetical when every slash-part is a known unit,
    so '(per battery)' / '(assessed)' stay put. Returns (clean_attr, unit|None)."""
    m = re.search(r"\(([^)]*)\)\s*$", attr or "")
    if not m:
        return attr, None
    inside = m.group(1).strip()
    parts = [p.strip() for p in inside.split("/")]
    if parts and all(_known_unit(p) for p in parts):
        return attr[:m.start()].strip(), inside
    return attr, None


def _dual_unit_value(raw_val, unit_src):
    """Dual-unit value (5b): unit_src is the UNIT string ('km/nm'), raw_val the
    VALUE ('18/10'). Returns {'units':[u1,u2],'values':[v1,v2]} only when both
    split into exactly two parts and both units are known, else None."""
    v = str(raw_val or "").strip()
    if "/" not in (unit_src or "") or "/" not in v:
        return None
    uparts = [u.strip() for u in unit_src.split("/")]
    vnums = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", v)
    if len(uparts) != 2 or len(vnums) != 2 or not all(_known_unit(u) for u in uparts):
        return None
    return {"units": uparts, "values": [n.replace(",", "") for n in vnums]}


def _dual_consistent(dual):
    """Do the two numbers denote the SAME quantity (18 km ~= 10 nm)? True/False,
    or None when the unit family is outside the local table (can't verify)."""
    v0, v1 = _num_or_none(dual["values"][0]), _num_or_none(dual["values"][1])
    u0, u1 = dual["units"][0].lower(), dual["units"][1].lower()
    if u0 in _LEN_TO_KM and u1 in _LEN_TO_KM and v0 is not None and v1 is not None:
        b0, b1 = v0 * _LEN_TO_KM[u0], v1 * _LEN_TO_KM[u1]
        return abs(b0 - b1) <= max(1e-6, b0 * 0.05)   # 5% mutual tolerance
    return None


def _pick_dual(dual, field, rc):
    """Choose ONE (value, unit) from a dual-unit value: prefer the position whose
    unit matches the DB field's canonical unit, else position 0. Never emits two
    values and never pairs a number with the wrong unit."""
    canon = (rc.field_unit(field) or "").strip().lower()
    for i, u in enumerate(dual["units"]):
        if u.strip().lower() == canon:
            return dual["values"][i], dual["units"][i]
    return dual["values"][0], dual["units"][0]


# Common unit SPELLINGS collapsed to one key, so "minutes"/"min" and
# "kilometres"/"km" read as the same unit even with pint unavailable (else the
# cross-unit guard below would wrongly park a spelling variant). This is spelling
# equivalence only -- it never equates DIFFERENT units (km vs nm stay distinct).
_UNIT_SPELL = {
    "minute": "min", "minutes": "min", "min": "min", "mins": "min",
    "second": "s", "seconds": "s", "sec": "s", "secs": "s", "s": "s",
    "hour": "h", "hours": "h", "hr": "h", "hrs": "h", "h": "h",
    "kilometer": "km", "kilometers": "km", "kilometre": "km",
    "kilometres": "km", "km": "km", "kms": "km",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m", "m": "m",
    "kilogram": "kg", "kilograms": "kg", "kg": "kg", "kgs": "kg",
    "nauticalmile": "nm", "nauticalmiles": "nm", "nmi": "nm", "nm": "nm",
    "mile": "mi", "miles": "mi", "mi": "mi",
    "degree": "deg", "degrees": "deg", "deg": "deg",
    "foot": "ft", "feet": "ft", "ft": "ft", "mach": "mach",
}


def _unit_key(u):
    k = re.sub(r"[\s._]", "", (u or "").strip().lower())
    return _UNIT_SPELL.get(k, k)


def _can_convert(a, b):
    """True if unit `a` is comparable to `b`: the same unit (allowing spelling
    variants) or units.convert works. With pint unavailable, genuinely different
    units (nm vs km) are NOT convertible here -> the caller parks rather than
    compares mismatched magnitudes."""
    if _unit_key(a) == _unit_key(b):
        return True
    if refcat_mod.units.normalize_unit(a) == refcat_mod.units.normalize_unit(b):
        return True
    try:
        refcat_mod.units.convert(1.0, a, b)
        return True
    except Exception:
        return False


def _scan_header_field_units(text, rc):
    """Doc-level scan for column headers that carry the unit, e.g. 'Maximum Range
    (km)' or a dual 'Maximum Range (km / nm)'. Returns {canonical_field: {units}}
    for every header phrase that maps to a catalogued numeric field. This lets the
    mapping layer recover / trust a unit for a bare or non-adjacent numeric cell
    (5a, and the decomposed 5b) regardless of whether the model parked the unit in
    the attribute, the unit slot, or nowhere -- anchored to a '(unit)' literally
    in the document, so it never fabricates a unit."""
    out = {}
    for m in _HDR_UNIT_RE.finditer(_strip_md(text)):
        uparts = [u.strip() for u in m.group(2).split("/")]
        if not uparts or not all(_known_unit(u) for u in uparts):
            continue
        # the captured phrase can absorb a leading cell ("system radar detection
        # range") once pipes are flattened; take the LONGEST trailing sub-phrase
        # that maps to a unit-bearing field.
        toks = m.group(1).strip().lower().split()
        for i in range(len(toks)):
            field = rc.map_attribute(" ".join(toks[i:]))
            if field and rc.field_unit(field):
                out.setdefault(field, set()).update(uparts)
                break
    return out


# Claim keys the extraction contract defines. A cheap model fed a markdown pipe
# table sometimes mirrors the table into JSON, emitting the column header as a
# dynamic KEY ({"entity":"NASAMS","Maximum Altitude (km)":"21","unit":"km"}) with
# no "attribute"/"value". This adapter reads that one extra key as the
# attribute/value -- deterministic, invents nothing (the data is in the claim).
_KNOWN_CLAIM_KEYS = {"entity", "attribute", "value", "unit", "qualifier", "quote"}


def _coerce_claim(claim):
    if not isinstance(claim, dict):
        return claim
    if claim.get("attribute") or claim.get("value"):
        return claim
    extras = [(k, v) for k, v in claim.items() if k not in _KNOWN_CLAIM_KEYS]
    if len(extras) == 1:
        k, v = extras[0]
        c = dict(claim)
        c["attribute"], c["value"] = k, v
        return c
    return claim


def _country_in(text, countries):
    low = (text or "").lower()
    for c in countries:
        if re.search(r"(?<![\w])" + re.escape(c.lower()) + r"(?![\w])", low):
            return c
    return None


def _domain_term_in(text):
    low = (text or "").lower()
    return any(t in low for t in refcat_mod.DOMAIN_TERMS)


# Relation verb stems, anchored at a WORD boundary (not bare substring) so a
# parameter name that merely embeds a motion verb ("launch weight", "firing
# range") is not dragged into the relation branch. Stems only (no trailing
# boundary): 'carr' -> carry/carries/carried, 'integrat' -> integrated/integration.
_RELATION_VERB_RE = re.compile(
    r"(?<![a-z])(?:fired|fires|firing|launch|carr|employ|fitted|armed|equip|"
    r"integrat|fires)")


def _relation_endpoints(text, ctx, rc):
    """Distinct catalogued records with a STRONG (non-ambiguous) mention in `text`
    that are ALSO mentioned in the document. Ordered by mention confidence
    (longest matched alias first). Ambiguous-alias-only mentions (AMBIGUOUS_ALIASES)
    are excluded -- they can't anchor a relation edge."""
    strong = {}
    for mid, alias, amb, pos in rc.find_mentions(text or ""):
        if amb or mid not in ctx.mentioned_ids:
            continue
        strong[mid] = max(strong.get(mid, 0), len(alias))
    return [m for m, _ in sorted(strong.items(), key=lambda kv: -kv[1])]


def _order_endpoints(eps, ctx):
    """Subject = the endpoint mentioned FIRST in the whole doc ('X carries Y' ->
    X). Positional and doc-wide, so it is stable regardless of claim order -- the
    two relation directions collapse onto one canonical edge under dedup."""
    pos = {}
    for mid, alias, amb, p in ctx.mentions:
        if mid in eps and not amb:
            pos[mid] = min(pos.get(mid, 10 ** 9), p)
    e = sorted(eps, key=lambda m: pos.get(m, 10 ** 9))
    return e[0], e[1]


class DocContext:
    """Per-document linking context: which records are mentioned, whether a
    domain term / second entity is present, computed once."""
    def __init__(self, rc, text):
        self.rc = rc
        self.text = text
        self.field_mapper = "legacy"
        self.text_fields = False
        self.last_mapping = None
        self.mentions = rc.find_mentions(text)
        self.mentioned_ids = {}
        for mid, alias, amb, pos in self.mentions:
            self.mentioned_ids.setdefault(mid, {"aliases": set(), "amb": True})
            self.mentioned_ids[mid]["aliases"].add(alias)
            if not amb:
                self.mentioned_ids[mid]["amb"] = False    # has >=1 strong alias
        self.has_domain = _domain_term_in(text)
        self.low_trust = bool(_LOWTRUST_CUES.search(text or ""))
        # column headers that carry the unit ("Maximum Range (km)") -> field:unit
        self.header_field_units = _scan_header_field_units(text, rc)
        # strong (non-ambiguous) OTHER entities present, for the second-entity signal
        self.strong_ids = {mid for mid, info in self.mentioned_ids.items()
                           if not info["amb"]}

    def second_signal(self, mid):
        """Does the doc give a SECOND corroborating signal for record `mid`?
        Returns (bool, reason). An ambiguous-only mention may NOT rely on another
        entity (two collision-prone aliases can't validate each other) -- it needs
        a domain term or an operator-country of THIS record."""
        info = self.mentioned_ids.get(mid)
        if not info:
            return False, "not mentioned"
        strong_here = not info["amb"]
        op = _country_in(self.text, self.rc.operators(mid))
        # strong second entity (a DIFFERENT record with a non-ambiguous mention)
        other_strong = any(o != mid for o in self.strong_ids)
        if self.has_domain:
            return True, "domain-term"
        if op:
            return True, f"operator-country:{op}"
        if strong_here and other_strong:
            return True, "co-entity"
        return False, "no-second-signal"


def _link_claim_record(claim, ctx, rc):
    """Which mentioned record does this claim refer to? Match the claim's entity /
    quote text against the aliases of records mentioned in the doc."""
    hay = _norm_for_ground(
        (claim.get("entity") or "") + " " + (claim.get("quote") or ""))
    best = None
    best_len = 0
    for mid in ctx.mentioned_ids:
        for alias in ctx.mentioned_ids[mid]["aliases"]:
            if re.search(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])", hay):
                if len(alias) > best_len:
                    best, best_len = mid, len(alias)
    return best


def classify_claim(claim, mid, ctx, rc, doc_text="", ground_sent=""):
    """Return a dict describing the proposal (or the park/drop reason).
    Deterministic; no LLM. Keys: proposal_type, canon_field, value_disp,
    value_norm, unit_norm, db_value, target_value, park_reason, status_hint,
    record_mid (relation subject override)."""
    attr_raw = (claim.get("attribute") or "").strip().lower()
    # header-unit-in-attribute (5a): strip a trailing '(km)' / '(km/nm)' off the
    # attribute so it maps cleanly, keeping the parenthetical as a unit fallback.
    attr, paren_unit = _split_attr_unit(attr_raw)
    quote = claim.get("quote") or ""
    # dual-unit value (5b): detect BEFORE _clean_value_unit (which would mangle
    # '18/10'). Unit source = explicit unit slot, else the attribute parenthetical.
    dual_unit_src = ((claim.get("unit") or "").strip() or (paren_unit or ""))
    dual = _dual_unit_value(claim.get("value"), dual_unit_src)
    # Cheap models sometimes keep only the first number in `value` and leave the
    # complete dual cell in quote/qualifier. Recover a literal A/B pair from the
    # grounded evidence before classification; never synthesize a missing number.
    if dual is None and "/" in dual_unit_src:
        evidence = " ".join(str(x or "") for x in
                            (quote, claim.get("qualifier")))
        pair = re.search(
            r"([-+]?\d[\d,]*(?:\.\d+)?)\s*/\s*"
            r"([-+]?\d[\d,]*(?:\.\d+)?)", evidence)
        if pair:
            dual = _dual_unit_value(
                f"{pair.group(1)}/{pair.group(2)}", dual_unit_src)
    value_raw, unit_raw = _clean_value_unit(claim.get("value"), claim.get("unit"))
    unit_from_header = False
    if not unit_raw and paren_unit and "/" not in paren_unit:
        unit_raw, unit_from_header = paren_unit, True   # 5a single header unit

    # Map the attribute to a parametric field FIRST. A parameter name that merely
    # embeds a motion verb ("launch weight", "firing range") must reach the
    # numeric comparator, NOT be hijacked into the relation branch below -- so we
    # only fall through to relation detection when the attribute maps to no field.
    if getattr(ctx, "field_mapper", "legacy") == "catalogue":
        mapping = rc.resolve_attribute(
            attr, claim, mid=mid,
            context_text=" ".join([quote, ground_sent, doc_text[:500]]))
        field = mapping.get("field")
    else:
        field = rc.map_attribute(attr)
        mapping = {
            "field": field, "mapping_candidate": field,
            "mapping_status": "resolved" if field else "unmapped",
            "mapping_method": "legacy", "mapping_score": 1.0 if field else 0.0,
            "mapping_tier": "high" if field else "low", "runner_up": None,
            "runner_up_score": 0.0, "mapping_evidence": [],
            "mapper_version": "legacy",
        }
    ctx.last_mapping = mapping

    # ---- relation ---------------------------------------------------------- #
    if field is None and (attr in refcat_mod.RELATION_ATTRS
                          or _RELATION_VERB_RE.search(attr)):
        # Quote-scoped linking: the partner system is often present ONLY in the
        # quote ("F-35 ... to carry ... ASRAAM"), not in the entity/value slots
        # the model mis-fills. Propose the edge between the two highest-confidence
        # DISTINCT catalogued mentions in the quote + grounding sentence.
        eps = _relation_endpoints((quote or "") + " " + (ground_sent or ""),
                                  ctx, rc)
        if len(eps) == 2:                       # exactly two -> a clean edge
            subj, obj = _order_endpoints(eps, ctx)
            if rc.title(obj).lower() in rc.relations(subj) or \
                    rc.title(subj).lower() in rc.relations(obj):
                return {"proposal_type": None, "park_reason": None,
                        "status_hint": "dropped", "drop": "relation_present",
                        "canon_field": "relation", "record_mid": subj}
            a, b = sorted([subj, obj])
            return {"proposal_type": "relation", "canon_field": "relation",
                    "value_disp": f"{rc.title(subj)} <-> {rc.title(obj)}",
                    "target_value": f"{a}|{b}", "other_id": obj,
                    "record_mid": subj,
                    "db_value": None, "value_norm": None, "unit_norm": None,
                    "park_reason": None, "status_hint": "ok"}
        # fewer than two, or more than two tie -> park rather than guess an edge
        return {"proposal_type": None, "park_reason": "unmapped",
                "status_hint": "parked",
                "canon_field": "relation", "value_disp": claim.get("value")}

    # ---- proliferation (operator country) --------------------------------- #
    country = None
    if attr in refcat_mod.OPERATOR_ATTRS or "operat" in attr or "user" in attr \
            or "service with" in attr:
        # the country may be in value, entity, or quote
        country = _country_in(claim.get("value") or "", rc.countries) or \
            _country_in(claim.get("entity") or "", rc.countries) or \
            _country_in(quote, rc.countries)
        # novel operator country the closed rc.countries set can't see (Kuwait,
        # Algeria): accept a world-country name, but ONLY if it appears literally
        # in the claim's QUOTE (string-level grounding) on an operator attribute.
        if country is None:
            country = _country_in(quote, refcat_mod.WORLD_COUNTRIES)
    if country:
        if country in rc.operators(mid):
            return {"proposal_type": None, "park_reason": None,
                    "status_hint": "dropped", "drop": "operator_present",
                    "canon_field": "Operated by (country)"}
        return {"proposal_type": "gap_fill", "canon_field": "Operated by (country)",
                "value_disp": country, "target_value": country,
                "db_value": ", ".join(sorted(rc.operators(mid))) or "(none)",
                "value_norm": None, "unit_norm": None,
                "park_reason": None, "status_hint": "ok"}

    # ---- alias ------------------------------------------------------------- #
    if attr in refcat_mod.ALIAS_ATTRS or "alias" in attr or "designat" in attr \
            or "reporting name" in attr or "known as" in attr:
        alias_val = (claim.get("value") or "").strip()
        if alias_val:
            if alias_val.lower() in rc.aliases(mid) or \
                    alias_val.lower() == (rc.title(mid) or "").lower():
                return {"proposal_type": None, "park_reason": None,
                        "status_hint": "dropped", "drop": "alias_present",
                        "canon_field": "alias"}
            return {"proposal_type": "gap_fill", "canon_field": "alias",
                    "value_disp": alias_val, "target_value": alias_val.lower(),
                    "db_value": ", ".join(sorted(rc.aliases(mid))) or "(none)",
                    "value_norm": None, "unit_norm": None,
                    "park_reason": None, "status_hint": "ok"}

    # ---- parametric -------------------------------------------------------- #
    # `field` was resolved at the top of the function (before relation gating).
    if field is None:
        reason = ("ambiguous_field"
                  if mapping.get("mapping_status") == "ambiguous"
                  else "unmapped")
        return {"proposal_type": None, "park_reason": reason,
                "status_hint": "parked", "canon_field": None,
                "value_disp": f"{value_raw} {unit_raw}".strip()}

    dtype = rc.field_dtype(field)
    is_numeric_field = (dtype in ("Number",)) or bool(rc.field_unit(field))
    # Scope: only NUMERIC parametric fields become gap_fill/conflict proposals.
    # Free-text/LOV fields (Type, Function, Warhead, Guidance system, ...) are
    # prose-like: a document phrasing ("air-to-air missile") is usually a
    # paraphrase or substring of the DB value ("Short-range all-aspect air-to-air
    # missile"), so exact-match comparison manufactures spurious conflicts and
    # low-value text gap-fills. Those are parked, not surfaced (design tradeoff:
    # structured facts surface; free-text is left to prose/semantic search).
    if not is_numeric_field and not getattr(ctx, "text_fields", False):
        return {"proposal_type": None, "park_reason": "text_field",
                "status_hint": "parked", "canon_field": field,
                "value_disp": value_raw}
    if not is_numeric_field:
        verdict, text_value, dbrepr = rc.compare_text(value_raw, field, mid)
        if verdict == "match":
            return {"proposal_type": None, "park_reason": None,
                    "status_hint": "dropped", "drop": "already_present",
                    "canon_field": field}
        if verdict == "difference":
            return {"proposal_type": None, "park_reason": "text_difference",
                    "status_hint": "parked", "canon_field": field,
                    "value_disp": text_value, "db_value": dbrepr,
                    "value_norm": None, "unit_norm": None}
        target = re.sub(r"\s+", " ", re.sub(
            r"[^a-z0-9]+", " ", text_value.lower())).strip()
        return {"proposal_type": "gap_fill", "canon_field": field,
                "value_disp": text_value, "target_value": target,
                "db_value": None, "value_norm": None, "unit_norm": None,
                "park_reason": None, "status_hint": "ok"}

    # ---- dual-unit value (5b) --------------------------------------------- #
    # "Maximum range (km/nm) | 18/10": pick ONE value (the position whose unit is
    # the DB field's canonical unit) after cross-checking the two numbers denote
    # the same quantity. Inconsistent pairs (the 5d trap) park rather than guess.
    if dual is not None:
        if _dual_consistent(dual) is False:
            return {"proposal_type": None, "park_reason": "inconsistent_dual",
                    "status_hint": "parked", "canon_field": field,
                    "value_disp": "/".join(dual["values"])}
        value_raw, unit_raw = _pick_dual(dual, field, rc)
        unit_from_header = True                  # header-sourced -> skip adjacency

    val_num = refcat_mod._canon_num(value_raw)
    hdr_units = ctx.header_field_units.get(field) or set()
    hdr_lc = {u.lower() for u in hdr_units}
    # a unit the column header itself states for this field is authoritative: the
    # value and unit are non-adjacent by table construction, so trust it and skip
    # the adjacency guard (covers 5a and the decomposed 5b '37 km'/'20 nm' pair).
    if unit_raw and not unit_from_header and unit_raw.lower() in hdr_lc:
        unit_from_header = True
    # unit-hallucination guard: if the model gave a unit but it does NOT sit next
    # to the value in the source text (and no header vouches for it), treat the
    # value as unitless (-> incomplete) rather than trusting a fabricated unit.
    if unit_raw and not unit_from_header \
            and not _unit_adjacent(doc_text, value_raw, unit_raw):
        unit_raw = ""
    # ---- header-unit recovery (5a) ---------------------------------------- #
    # a bare numeric cell whose unit lives only in the column header: recover it
    # from the header scan, keyed on the field this claim mapped to. Runs AFTER
    # the adjacency guard so it also rescues a value whose model unit was
    # (correctly) stripped as non-adjacent. Prefers the DB field's canonical unit;
    # for a dual header only the canonical position is unambiguous.
    if not unit_raw and hdr_units:
        canon = (rc.field_unit(field) or "").lower()
        pick = next((u for u in hdr_units if u.lower() == canon), None)
        if pick is None and len(hdr_units) == 1:
            pick = next(iter(hdr_units))
        if pick:
            unit_raw, unit_from_header = pick, True
    # incomplete: a numeric field but no unit given and the field is a quantity
    if is_numeric_field and val_num is not None and not unit_raw \
            and rc.field_unit(field):
        return {"proposal_type": None, "park_reason": "incomplete",
                "status_hint": "parked", "canon_field": field,
                "value_disp": value_raw,
                "target_value": None, "value_norm": val_num, "unit_norm": None,
                "db_value": None}
    # vague / non-numeric value on a numeric field -> incomplete
    if is_numeric_field and val_num is None:
        return {"proposal_type": None, "park_reason": "incomplete",
                "status_hint": "parked", "canon_field": field,
                "value_disp": value_raw, "target_value": None,
                "value_norm": None, "unit_norm": None, "db_value": None}

    # cross-unit incomparability guard: when the DB holds a value to compare
    # against but the doc unit differs from the field's canonical unit AND cannot
    # be converted, do NOT compare raw magnitudes -- that mixes units (e.g. 20 nm
    # vs 50 km -> spurious conflict). Park as incomplete so only the
    # canonical-unit sibling of a decomposed dual value surfaces. An ABSENT field
    # still gap-fills (no DB value -> guard skipped).
    canon_unit = rc.field_unit(field)
    if unit_raw and canon_unit and not _can_convert(unit_raw, canon_unit):
        return {"proposal_type": None, "park_reason": "incompatible_unit",
                "status_hint": "parked", "canon_field": field,
                "value_disp": f"{value_raw} {unit_raw}".strip(),
                "target_value": None, "value_norm": val_num, "unit_norm": None,
                "db_value": None}

    verdict, nval, nunit, dbrepr = rc.compare_numeric(value_raw, unit_raw, field, mid)
    if verdict == "incomparable":
        return {"proposal_type": None, "park_reason": "incomplete",
                "status_hint": "parked", "canon_field": field,
                "value_disp": f"{value_raw} {unit_raw}".strip(),
                "target_value": None, "value_norm": nval, "unit_norm": nunit,
                "db_value": dbrepr}
    if verdict == "match":
        return {"proposal_type": None, "park_reason": None,
                "status_hint": "dropped", "drop": "already_present",
                "canon_field": field}
    ptype = "gap_fill" if verdict == "gap" else "conflict"
    disp = f"{value_raw} {unit_raw}".strip()
    tv = f"{round(nval, 6) if nval is not None else value_raw}"
    return {"proposal_type": ptype, "canon_field": field, "value_disp": disp,
            "target_value": tv, "value_norm": nval, "unit_norm": nunit,
            "db_value": dbrepr, "park_reason": None, "status_hint": "ok"}


def _mapping_columns(mapping, rc):
    mapping = mapping or {}
    evidence = mapping.get("mapping_evidence") or []
    return {
        "mapping_status": mapping.get("mapping_status"),
        "mapping_method": mapping.get("mapping_method"),
        "mapping_score": mapping.get("mapping_score"),
        "mapping_tier": mapping.get("mapping_tier"),
        "mapping_candidate": mapping.get("mapping_candidate"),
        "runner_up": mapping.get("runner_up"),
        "runner_up_score": mapping.get("runner_up_score"),
        "mapping_evidence": json.dumps(evidence, ensure_ascii=False),
        "mapper_version": mapping.get("mapper_version"),
        "alias_version": getattr(getattr(rc, "field_catalogue", None),
                                 "alias_version", 0),
    }


def _source_values_for_field(text, labels, canon_unit):
    """Find literal numeric values following a field label in source text.

    This is a safety scan, not claim generation. It only determines whether the
    source visibly gives multiple incompatible values for a field the LLM already
    extracted.
    """
    flat = re.sub(r"\s+", " ", _strip_md(text or ""))
    found = []
    for label in {str(x or "").strip() for x in labels if str(x or "").strip()}:
        words = re.findall(r"[A-Za-z0-9]+", label)
        if not words:
            continue
        pattern = r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"
        hits = list(re.finditer(pattern, flat, re.IGNORECASE))
        # A single flattened table header can be followed by several unrelated
        # row values. Only use this source-level safety scan when the field label
        # itself recurs (e.g. prose plus a contradictory table row). Multiple
        # extracted claims are reconciled separately without this restriction.
        if len(hits) < 2:
            continue
        for hit in hits:
            tail = flat[hit.end():hit.end() + 90]
            num = re.search(
                r"(?<!\d)([-+]?\d[\d,]*(?:\.\d+)?)(?!\d)"
                r"\s*([A-Za-z%Â°Âµ]+)?", tail)
            if not num:
                continue
            raw = num.group(1).replace(",", "")
            unit = (num.group(2) or canon_unit or "").strip()
            value = refcat_mod._canon_num(raw)
            if value is None:
                continue
            norm, norm_unit = value, unit
            if unit and canon_unit:
                try:
                    norm = refcat_mod.units.convert(value, unit, canon_unit)
                    norm_unit = canon_unit
                except refcat_mod.units.ConversionError:
                    continue
            item = {"value_norm": norm, "unit_norm": norm_unit}
            if not any(_corroboration_relation(item, old) == "agree"
                       for old in found):
                found.append(item)
    return found


def _apply_intradoc_safety(rows, doc_text, rc):
    """Park proposal groups contradicted by another value in the same source."""
    groups = {}
    for row in rows:
        if row.get("proposal_type") not in ("gap_fill", "conflict"):
            continue
        if row.get("status") not in ("surfaced", "parked"):
            continue
        if not row.get("partial_fp"):
            continue
        groups.setdefault(row["partial_fp"], []).append(row)
    for group in groups.values():
        observed = [{"value_norm": r.get("value_norm"),
                     "unit_norm": r.get("unit_norm")} for r in group
                    if r.get("value_norm") is not None]
        field = group[0].get("canon_field")
        labels = [field] + [r.get("attribute") for r in group]
        observed.extend(_source_values_for_field(
            doc_text, labels, rc.field_unit(field) if field else None))
        conflict = any(
            _corroboration_relation(a, b) == "conflict"
            for i, a in enumerate(observed) for b in observed[i + 1:])
        if conflict:
            for row in group:
                row.update(status="parked", park_reason="intradoc_conflict")


def process_document(doc, ctx, claims, rc, con, run_id, model):
    """Turn one document's extracted claims into stored claim rows."""
    rows = []
    seen_fps = set()
    seen_values = {}    # partial_fp -> [{'value_norm','unit_norm'}, ...] this doc
    for claim in claims:
        # markdown-table extractions sometimes carry the attribute as a dynamic
        # JSON key ({"Maximum Altitude (km)":"21"}); recover attribute/value.
        claim = _coerce_claim(claim)
        # cheap models occasionally emit a claim as a bare string / wrong shape;
        # skip it as malformed rather than crashing (recorded as a dropped row).
        if not isinstance(claim, dict):
            state_mod.insert_claim(con, {
                "run_id": run_id, "doc_id": doc["doc_id"],
                "doc_title": doc["title"], "doc_path": doc["path"],
                "entity_mention": str(claim)[:120], "attribute": None,
                "value_raw": None, "unit_raw": None, "quote": None,
                "created_run": run_id, "model_id": None, "record_title": None,
                "canon_field": None, "value_norm": None, "unit_norm": None,
                "value_disp": None, "proposal_type": None, "db_value": None,
                "status": "dropped", "park_reason": "malformed_claim",
                "qualifier": None, "full_fp": None, "partial_fp": None})
            continue
        entity = (claim.get("entity") or "").strip()
        quote = claim.get("quote") or ""
        value_raw_disp = claim.get("value")
        # link to a record mentioned in THIS doc
        mid = _link_claim_record(claim, ctx, rc)

        base = {
            "run_id": run_id, "doc_id": doc["doc_id"], "doc_title": doc["title"],
            "doc_path": doc["path"], "entity_mention": entity,
            "attribute": claim.get("attribute"), "qualifier": claim.get("qualifier"),
            "value_raw": str(value_raw_disp), "unit_raw": claim.get("unit"),
            "quote": quote, "created_run": run_id, "model_id": None,
            "record_title": None, "canon_field": None, "value_norm": None,
            "unit_norm": None, "value_disp": None, "proposal_type": None,
            "db_value": None, "status": None, "park_reason": None,
            "full_fp": None, "partial_fp": None,
            "raw_claim_json": json.dumps(claim, ensure_ascii=False,
                                         sort_keys=True),
            "mapping_status": None, "mapping_method": None,
            "mapping_score": None, "mapping_tier": None,
            "mapping_candidate": None, "runner_up": None,
            "runner_up_score": None, "mapping_evidence": None,
            "mapper_version": getattr(ctx, "field_mapper", "legacy"),
            "alias_version": getattr(getattr(rc, "field_catalogue", None),
                                     "alias_version", 0),
        }

        if mid is None:
            base.update(status="parked", park_reason="unlinked")
            rows.append(base)
            continue

        ok, reason = ctx.second_signal(mid)
        if not ok:
            base.update(status="parked", park_reason="unlinked", model_id=mid,
                        record_title=rc.title(mid))
            rows.append(base)
            continue

        # quote-grounding (anti-hallucination) BEFORE trusting the value
        gmode, gsent = ground(value_raw_disp, quote, doc["text"])
        if gmode is None:
            base.update(status="dropped", park_reason="ungrounded", model_id=mid,
                        record_title=rc.title(mid))
            rows.append(base)
            continue
        if gmode == "doc":
            base["quote"] = gsent            # cite a real grounding sentence

        if _is_nonasserted(base["quote"] or gsent):
            base.update(status="parked", park_reason="nonasserted",
                        model_id=mid, record_title=rc.title(mid))
            rows.append(base)
            continue

        res = classify_claim(claim, mid, ctx, rc, doc["text"],
                             ground_sent=(gsent or ""))
        mapping = ctx.last_mapping or {}
        if res.get("canon_field") in (
                "relation", "Operated by (country)", "alias") \
                and not mapping.get("field"):
            mapping = {
                "mapping_status": "resolved", "mapping_method": "special_field",
                "mapping_score": 1.0, "mapping_tier": "high",
                "mapping_candidate": res.get("canon_field"), "runner_up": None,
                "runner_up_score": 0.0, "mapping_evidence": [],
                "mapper_version": getattr(ctx, "field_mapper", "legacy"),
            }
        base.update(_mapping_columns(mapping, rc))
        # relations re-attribute to the SUBJECT endpoint (record_mid); everything
        # else stays on the linked record. mid_eff drives record + fingerprints so
        # the two edge directions dedup onto one canonical proposal.
        mid_eff = res.get("record_mid") or mid
        base.update(model_id=mid_eff, record_title=rc.title(mid_eff),
                    canon_field=res.get("canon_field"),
                    value_disp=res.get("value_disp"),
                    value_norm=res.get("value_norm"),
                    unit_norm=res.get("unit_norm"),
                    proposal_type=res.get("proposal_type"),
                    db_value=res.get("db_value"))

        if res["status_hint"] == "dropped":
            base.update(status="dropped", park_reason=res.get("drop"))
            rows.append(base)
            continue
        if res["status_hint"] == "parked":
            base.update(status="parked", park_reason=res.get("park_reason"))
            rows.append(base)
            continue

        # a real proposal -- fingerprints
        ptype = res["proposal_type"]
        target = res.get("canon_field")
        tv = res.get("target_value")
        if ptype == "conflict":
            full_fp = _sha(mid_eff, ptype, target, tv, res.get("db_value"))
        else:
            full_fp = _sha(mid_eff, ptype, target, tv)
        partial_fp = _sha(mid_eff, target)
        base.update(full_fp=full_fp, partial_fp=partial_fp)

        # dedup within this run
        if full_fp in seen_fps:
            base.update(status="dropped", park_reason="dup_in_run")
            rows.append(base)
            continue
        seen_fps.add(full_fp)

        # same-document restatement in a different unit (e.g. a table row split
        # into a '37 km' claim and a separately-extracted '20 nm' claim for the
        # same field): once units are convertible, both independently classify
        # against the DB and would otherwise emit two proposals for one fact.
        # Dedup on PROVEN agreement only -- an unconvertible pair stays separate
        # (indeterminate is not evidence they're the same statement).
        if ptype in ("gap_fill", "conflict"):
            prior_list = seen_values.setdefault(partial_fp, [])
            this_val = {"value_norm": res.get("value_norm"),
                        "unit_norm": res.get("unit_norm")}
            if any(_corroboration_relation(this_val, prior) == "agree"
                   for prior in prior_list):
                base.update(status="dropped", park_reason="dup_in_run")
                rows.append(base)
                continue
            prior_list.append(this_val)

        # suppression ledger
        if state_mod.is_rejected(con, full_fp):
            base.update(status="rejected", park_reason="rejected")
            rows.append(base)
            continue

        # low-trust single source parks as uncorroborated; graduation may surface
        if ctx.low_trust:
            base.update(status="parked", park_reason="uncorroborated")
        else:
            base.update(status="surfaced")
        rows.append(base)

    _apply_intradoc_safety(rows, doc["text"], rc)
    for r in rows:
        state_mod.insert_claim(con, r)
    con.commit()
    return rows


def _corroboration_relation(target, other):
    """Return agree, conflict, or indeterminate for two normalized values."""
    av, bv = target["value_norm"], other["value_norm"]
    au, bu = target["unit_norm"] or "", other["unit_norm"] or ""
    if av is None or bv is None:
        return "indeterminate"
    av, bv = float(av), float(bv)
    if not au and not bu:
        comparable = bv
    elif not au or not bu:
        return "indeterminate"
    elif refcat_mod._unit_key(au) == refcat_mod._unit_key(bu):
        comparable = bv
    else:
        try:
            comparable = refcat_mod.units.convert(bv, bu, au)
        except refcat_mod.units.ConversionError:
            return "indeterminate"
    tolerance = max(1e-6, abs(av) * 0.02)
    return "agree" if abs(comparable - av) <= tolerance else "conflict"


def graduation_pass(con):
    """Re-check parked low-trust claims without an LLM or document re-read.

    Claims cluster by record+field (`partial_fp`). Same/convertible units either
    agree within the numeric comparator's 2% tolerance or demonstrably conflict.
    Only demonstrable agreement counts as support. Incomparable units remain
    parked: autonomous corroboration must not turn dimensional uncertainty into
    a factual proposal.
    """
    rows = con.execute(
        "SELECT claim_id,doc_id,partial_fp,value_norm,unit_norm,status,park_reason "
        "FROM claims WHERE partial_fp IS NOT NULL "
        "AND status IN ('surfaced','parked') "
        "AND (park_reason IS NULL OR park_reason <> 'unmapped')").fetchall()
    clusters = {}
    for row in rows:
        clusters.setdefault(row["partial_fp"], []).append(row)
    graduated = 0
    for claims in clusters.values():
        for target in claims:
            if target["status"] != "parked" \
                    or target["park_reason"] != "uncorroborated":
                continue
            supporting_docs = {
                other["doc_id"] for other in claims
                if _corroboration_relation(target, other) == "agree"
            }
            if len(supporting_docs) >= CORROBORATION_THRESHOLD:
                cur = con.execute(
                    "UPDATE claims SET status='surfaced', park_reason=NULL "
                    "WHERE claim_id=?", (target["claim_id"],))
                graduated += cur.rowcount
    con.commit()
    return graduated


def run_batch(folder, con, rc, model=llm_mod.DEFAULT_MODEL, only=None,
              note="", verbose=True, render="text", field_mapper="legacy",
              text_fields=False):
    """Process a batch of documents. `only` = optional set/list of doc_ids to
    restrict to (the rest of the folder is left for a later batch). `render`
    selects the provider text-extraction mode ('text'|'md'). Returns a
    summary dict."""
    alias_version = getattr(getattr(rc, "field_catalogue", None),
                            "alias_version", 0)
    run_id = state_mod.start_run(
        con, model, note, field_mapper=field_mapper, text_fields=text_fields,
        alias_version=alias_version)
    only = set(only) if only else None
    processed, skipped, failed = [], [], []
    llm_calls = ptok = ctok = 0
    graduated = 0
    try:
        for doc in provider.iter_documents(folder, render=render):
            if only is not None and doc["doc_id"] not in only:
                continue
            seen = state_mod.already_seen(con, doc["content_hash"])
            if seen:
                skipped.append(doc["doc_id"])
                if verbose:
                    print(f"  skip (already processed): {doc['doc_id']}")
                continue
            llm_calls += 1
            try:
                claims, usage, raw, err = llm_mod.extract_claims(
                    doc["title"], doc["text"], model=model)
            except Exception as exc:
                failed.append(doc["doc_id"])
                state_mod.record_failure(
                    con, doc["doc_id"], doc["path"], doc["title"],
                    doc["content_hash"], run_id, str(exc), None)
                print(f"  ERROR {doc['doc_id']}: {exc}")
                continue
            ptok += usage.get("prompt_tokens", 0) or 0
            ctok += usage.get("completion_tokens", 0) or 0
            if err is not None:
                failed.append(doc["doc_id"])
                state_mod.record_failure(
                    con, doc["doc_id"], doc["path"], doc["title"],
                    doc["content_hash"], run_id, err, raw)
                if verbose:
                    print(f"  ERROR {doc['doc_id']}: {err}")
                continue
            ctx = DocContext(rc, doc["text"])
            ctx.field_mapper = field_mapper
            ctx.text_fields = bool(text_fields)
            rows = process_document(doc, ctx, claims, rc, con, run_id, model)
            state_mod.record_doc(con, doc["doc_id"], doc["path"], doc["title"],
                                 doc["content_hash"], doc["date"], run_id, model,
                                 len(claims))
            processed.append(doc["doc_id"])
            if verbose:
                surf = sum(1 for r in rows if r["status"] == "surfaced")
                print(f"  processed {doc['doc_id']}: {len(claims)} claims, "
                      f"{surf} surfaced ("
                      f"{'LOW-TRUST' if ctx.low_trust else 'normal'}"
                      f"{'' if err is None else '; ERR:'+err})")
        graduated = graduation_pass(con)
    finally:
        state_mod.finish_run(con, run_id, len(processed), llm_calls, ptok, ctok,
                             len(failed))
    return {"run_id": run_id, "processed": processed, "skipped": skipped,
            "failed": failed, "error_count": len(failed),
            "llm_calls": llm_calls, "prompt_tokens": ptok,
            "completion_tokens": ctok, "graduated": graduated,
            "field_mapper": field_mapper, "text_fields": bool(text_fields),
            "alias_version": alias_version}
