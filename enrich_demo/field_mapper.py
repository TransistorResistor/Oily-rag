#!/usr/bin/env python3
"""Generated field catalogue and schema-aware claim-attribute resolver.

The catalogue is derived entirely from normalized record metadata. A small JSON
overlay supplies curated equivalent terms; definition/profile matching is a
candidate resolver, not an ontology. Low-margin mappings abstain.
"""

from __future__ import annotations

import collections
import json
import os
import re

import units


MAPPER_VERSION = "catalogue-v1"
DEFAULT_ALIASES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "field_aliases.json")

_STOP = {
    "a", "an", "and", "at", "by", "for", "from", "in", "is", "of",
    "on", "or", "the", "to", "with", "value", "rated", "applicable",
    "system", "platform", "equipment", "parameter", "primary", "maximum",
}


def _norm(text):
    text = str(text or "").lower().replace("_", " ")
    text = re.sub(r"[^a-z0-9%]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text):
    return {t for t in _norm(text).split() if t and t not in _STOP}


def _value_is_numeric(value):
    return re.match(r"^\s*[-+]?\d[\d,]*(?:\.\d+)?", str(value or "")) is not None


def _unit_parts(value):
    return [p.strip() for p in re.split(r"[/,]", str(value or "")) if p.strip()]


def _units_compatible(claim_unit, field_units):
    """True/False/None: compatible, incompatible, or insufficient metadata."""
    if not claim_unit or not field_units:
        return None
    for cu in _unit_parts(claim_unit):
        for fu in field_units:
            try:
                units.convert(1.0, cu, fu)
                return True
            except units.ConversionError:
                continue
    return False


class FieldCatalogue:
    """Runtime field profiles derived from canonical records."""

    def __init__(self, records, aliases_path=DEFAULT_ALIASES):
        self.records = records
        self.aliases_path = aliases_path
        self.aliases = self._load_aliases(aliases_path)
        self.alias_version = self.aliases.get("version", 0)
        self.fields = self._build_fields()
        self.name_index = {_norm(name): name for name in self.fields}
        self.alias_index = self._build_alias_index()
        self.contextual_only_terms = {
            _norm(spec.get("term"))
            for spec in self.aliases.get("contextual_aliases") or []
            if spec.get("term")
        }
        self.record_peers = self._build_record_peers()

    @staticmethod
    def _load_aliases(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {"version": 0, "fields": {}}
        except (OSError, ValueError):
            return {"version": 0, "fields": {}, "contextual_aliases": []}

    def _build_fields(self):
        profiles = {}
        for mid, record in self.records.items():
            for p in record.get("params", []):
                name = str(p.get("name") or "").strip()
                if not name:
                    continue
                f = profiles.setdefault(name, {
                    "name": name, "definitions": set(), "data_types": set(),
                    "units": set(), "components": set(), "examples": [],
                    "record_ids": set(), "record_count": 0,
                })
                f["record_ids"].add(str(mid))
                if p.get("descr"):
                    f["definitions"].add(str(p["descr"]).strip())
                if p.get("dtype"):
                    f["data_types"].add(str(p["dtype"]).strip())
                if p.get("unit"):
                    f["units"].add(str(p["unit"]).strip())
                if p.get("component"):
                    f["components"].add(str(p["component"]).strip())
                if p.get("value") not in (None, "") and len(f["examples"]) < 12:
                    example = str(p["value"]).strip()
                    if example not in f["examples"]:
                        f["examples"].append(example)
        for f in profiles.values():
            f["record_count"] = len(f["record_ids"])
            profile_text = " ".join(
                [f["name"]] + sorted(f["definitions"]) +
                sorted(f["components"]))
            f["name_tokens"] = _tokens(f["name"])
            f["profile_tokens"] = _tokens(profile_text)
        return profiles

    def _build_alias_index(self):
        out = {}
        for field, spec in (self.aliases.get("fields") or {}).items():
            if field not in self.fields:
                continue
            for alias in (spec or {}).get("aliases", []):
                out.setdefault(_norm(alias), []).append(field)
        return out

    def _build_record_peers(self):
        keys = {}
        for mid, record in self.records.items():
            ex = record.get("extras") or {}
            group = _norm(ex.get("systemGroup"))
            typ = _norm(ex.get("systemType"))
            keys[str(mid)] = (group, typ)
        peers = {}
        for mid, key in keys.items():
            group, typ = key
            peers[mid] = {
                other for other, okey in keys.items()
                if other != mid and ((typ and okey[1] == typ) or
                                     (group and okey[0] == group))
            }
        return peers

    def audit(self):
        fields = []
        for name in sorted(self.fields):
            f = self.fields[name]
            unit_list = sorted(f["units"])
            incompatible_units = False
            for i, left in enumerate(unit_list):
                for right in unit_list[i + 1:]:
                    try:
                        units.convert(1.0, left, right)
                    except units.ConversionError:
                        incompatible_units = True
            fields.append({
                "field": name,
                "definitions": sorted(f["definitions"]),
                "data_types": sorted(f["data_types"]),
                "units": unit_list,
                "components": sorted(f["components"]),
                "examples": list(f["examples"]),
                "record_count": f["record_count"],
                "issues": (["missing_definition"] if not f["definitions"] else []) +
                          (["missing_data_type"] if not f["data_types"] else []) +
                          (["mixed_data_type"] if len(f["data_types"]) > 1 else []) +
                          (["incompatible_units"] if incompatible_units else []),
            })
        return {
            "mapper_version": MAPPER_VERSION,
            "alias_version": self.alias_version,
            "field_count": len(fields),
            "fields": fields,
        }

    def _exact(self, attr, context):
        key = _norm(attr)
        if key in self.name_index:
            return self._result(self.name_index[key], "canonical_exact", 1.0,
                                "high", None, 0.0, [f"term={key}"])
        matches = list(dict.fromkeys(self.alias_index.get(key, [])))
        if len(matches) == 1:
            return self._result(matches[0], "curated_alias", 0.99, "high",
                                None, 0.0, [f"alias={key}"])
        for spec in self.aliases.get("contextual_aliases") or []:
            if _norm(spec.get("term")) != key:
                continue
            required = [_norm(x) for x in spec.get("requires_any_context", [])]
            if required and any(re.search(r"\b" + re.escape(x) + r"\b", context)
                                for x in required):
                field = spec.get("field")
                if field in self.fields:
                    return self._result(
                        field, "contextual_alias", 0.97, "high", None, 0.0,
                        [f"term={key}", "context=" + ",".join(required)])
        return None

    @staticmethod
    def _result(field, method, score, tier, runner_up, runner_score, evidence,
                status="resolved"):
        return {
            "field": field,
            "mapping_candidate": field,
            "mapping_status": status,
            "mapping_method": method,
            "mapping_score": round(float(score), 4),
            "mapping_tier": tier,
            "runner_up": runner_up,
            "runner_up_score": round(float(runner_score or 0.0), 4),
            "mapping_evidence": list(evidence),
            "mapper_version": MAPPER_VERSION,
        }

    def resolve(self, attribute, claim, mid=None, context_text=""):
        context = _norm(" ".join([
            context_text or "", str(claim.get("quote") or ""),
            str(claim.get("qualifier") or "")]))
        exact = self._exact(attribute, context)
        if exact:
            return exact

        qt = _tokens(attribute)
        if not qt:
            return self._result(None, "none", 0.0, "low", None, 0.0, [],
                                status="unmapped")

        claim_unit = str(claim.get("unit") or "").strip()
        numeric = _value_is_numeric(claim.get("value"))
        peer_ids = self.record_peers.get(str(mid), set()) if mid else set()
        candidates = []
        for name, f in self.fields.items():
            name_cover = len(qt & f["name_tokens"]) / max(1, len(qt))
            profile_cover = len(qt & f["profile_tokens"]) / max(1, len(qt))
            score = max(name_cover * 0.72, profile_cover * 0.62)
            evidence = []
            if name_cover:
                evidence.append(f"name_overlap={name_cover:.2f}")
            if profile_cover:
                evidence.append(f"definition_overlap={profile_cover:.2f}")
            if peer_ids and (peer_ids & f["record_ids"]):
                score += 0.08
                evidence.append("peer_field")
            compat = _units_compatible(claim_unit, f["units"])
            if compat is False:
                score -= 0.30
                evidence.append("unit_incompatible")
            elif compat is True:
                score += 0.10
                evidence.append("unit_compatible")
            dtypes = {_norm(x) for x in f["data_types"]}
            if numeric and ("number" in dtypes or f["units"]):
                score += 0.04
                evidence.append("numeric_type")
            component_hits = [c for c in f["components"] if _norm(c) in context]
            if component_hits:
                score += 0.05
                evidence.append("component_context")
            if score > 0:
                candidates.append((min(1.0, score), name, evidence))

        candidates.sort(key=lambda x: (-x[0], x[1].lower()))
        if not candidates:
            return self._result(None, "definition", 0.0, "low", None, 0.0, [],
                                status="unmapped")
        top_score, top_name, top_evidence = candidates[0]
        runner_score, runner_name = ((candidates[1][0], candidates[1][1])
                                     if len(candidates) > 1 else (0.0, None))
        margin = top_score - runner_score
        top_evidence.append(f"margin={margin:.2f}")
        if _norm(attribute) in self.contextual_only_terms:
            result = self._result(
                None, "definition_profile", top_score, "low", runner_name,
                runner_score, top_evidence, status="ambiguous")
            result["mapping_candidate"] = top_name
            return result
        if top_score >= 0.62 and margin >= 0.12:
            return self._result(top_name, "definition_profile", top_score,
                                "medium", runner_name, runner_score, top_evidence)
        status = "ambiguous" if top_score >= 0.35 else "unmapped"
        result = self._result(None, "definition_profile", top_score, "low",
                              runner_name, runner_score, top_evidence,
                              status=status)
        result["mapping_candidate"] = top_name
        return result

    def suggestions(self, rows):
        """Aggregate stored mapping diagnostics into equivalent-term suggestions."""
        grouped = collections.defaultdict(list)
        for row in rows:
            method = row.get("mapping_method")
            candidate = row.get("mapping_candidate") or row.get("canon_field")
            attr = _norm(row.get("attribute"))
            if not attr or not candidate or method in ("canonical_exact", "curated_alias"):
                continue
            grouped[(attr, candidate)].append(row)
        out = []
        for (term, field), items in grouped.items():
            scores = [float(i.get("mapping_score") or 0.0) for i in items]
            out.append({
                "term": term,
                "field": field,
                "count": len(items),
                "average_score": round(sum(scores) / max(1, len(scores)), 4),
                "documents": sorted({i.get("doc_id") for i in items if i.get("doc_id")}),
                "example_quotes": [i.get("quote") for i in items[:3] if i.get("quote")],
            })
        return sorted(out, key=lambda x: (-x["count"], -x["average_score"], x["term"]))
