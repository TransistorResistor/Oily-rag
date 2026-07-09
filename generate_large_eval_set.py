#!/usr/bin/env python3
"""
Generate a gold eval set for large-test-corpus/.

The output uses the same schema as eval_set.json so it can be passed directly to
eval.py:

  python eval.py --db <large-db> --eval-set large_eval_set.json

The cases are derived from the generated corpus rather than hand-authored
against brittle filenames. Regenerating large-test-corpus with the same seed and
then rerunning this script will reproduce the same gold set.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import catalogue
import record_model


DEFAULT_CORPUS = Path("large-test-corpus")
DEFAULT_OUT = Path("large_eval_set.json")
DEFAULT_SEED = 20260708


def load_records(corpus: Path):
    rows = []
    for p in sorted(corpus.glob("*.json")):
        if p.name.lower() == "readme.md":
            continue
        raw = json.loads(p.read_text(encoding="utf-8"))
        canon = record_model.normalize_record(raw)
        fields, units = record_model.typed_fields(canon)
        rows.append({
            "path": p,
            "raw": raw,
            "canon": canon,
            "fields": fields,
            "units": units,
            "id": str(canon["id"]),
            "title": canon["title"],
            "group": raw.get("systemGroup"),
            "type": raw.get("systemType"),
        })
    if not rows:
        raise RuntimeError(f"no JSON records found in {corpus}")
    return rows


def first_scalar(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def as_numbers(v):
    vals = v if isinstance(v, list) else [v]
    out = []
    for x in vals:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            pass
    return out


def fmt_value(v):
    v = first_scalar(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def match_filter(row, flt):
    fields = row["fields"]
    for field, cond in flt.items():
        if field not in fields:
            return False
        value = fields[field]
        vals = value if isinstance(value, list) else [value]
        if "in" in cond:
            if not any(str(v) in set(map(str, cond["in"])) for v in vals):
                return False
        if "contains" in cond:
            want = set(map(str, cond["contains"]))
            if not any(str(v) in want for v in vals):
                return False
        nums = as_numbers(value)
        if "min" in cond and not any(n >= float(cond["min"]) for n in nums):
            return False
        if "max" in cond and not any(n <= float(cond["max"]) for n in nums):
            return False
    return True


def expected_for(rows, flt, limit=40):
    ids = [r["id"] for r in rows if match_filter(r, flt)]
    return ids[:limit]


def pick_by_group(rows, rng, n_per_group=2):
    out = []
    groups = sorted({r["group"] for r in rows})
    for g in groups:
        pool = [r for r in rows if r["group"] == g]
        out.extend(rng.sample(pool, min(n_per_group, len(pool))))
    return out


PARAM_FIELDS_BY_GROUP = {
    "Aircraft": ["Maximum speed", "Combat range", "Service ceiling", "Unit cost"],
    "Missiles": ["Maximum range", "Maximum speed", "Weight", "Unit cost"],
    "Air Defense Systems": ["Maximum range", "Radar detection range", "Deployment time"],
    "Sensors": ["Detection range", "Power consumption", "Total weight"],
    "Ground Vehicles": ["Combat weight", "Road speed", "Operational range"],
    "Naval Platforms": ["Displacement", "Maximum sea speed", "Operational range"],
    "Weapons and Components": ["Weight", "Effective range", "Unit cost"],
    "Power and Propulsion Systems": ["Maximum thrust", "Dry weight", "Length"],
    "C4ISR and Electronic Warfare": ["Node capacity", "Effective range", "Bandwidth"],
    "Space Systems": ["Launch mass", "Orbital altitude", "Design life"],
}


def add_lookup_cases(cases, rows, rng):
    for i, row in enumerate(pick_by_group(rows, rng, n_per_group=2), start=1):
        code = row["raw"]["primaryEquipCode"]
        cases.append({
            "id": f"large-lookup-{i:02d}",
            "class": "lookup",
            "query": f"What is {code}?",
            "expected_rids": [row["id"]],
            "required_rids": [],
            "expected_filter": None,
            "expected_answer_contains": [],
            "notes": f"{row['group']} / {row['type']} lookup for {row['title']}; modelID {row['id']}.",
        })


def add_parametric_cases(cases, rows, rng):
    selected = []
    for group, fields in PARAM_FIELDS_BY_GROUP.items():
        pool = [r for r in rows if r["group"] == group]
        rng.shuffle(pool)
        count = 0
        for row in pool:
            available = [f for f in fields if f in row["fields"]]
            if not available:
                continue
            field = rng.choice(available)
            selected.append((row, field))
            count += 1
            if count >= 3:
                break
    for i, (row, field) in enumerate(selected, start=1):
        unit = row["units"].get(field)
        code = row["raw"]["primaryEquipCode"]
        cases.append({
            "id": f"large-parametric-{i:02d}",
            "class": "parametric",
            "query": f"What is the {field.lower()} of {code}?",
            "expected_rids": [row["id"]],
            "required_rids": [],
            "expected_filter": None,
            "expected_answer_contains": [fmt_value(row["fields"][field])],
            "notes": f"{row['title']} ({code}) {field} = {fmt_value(row['fields'][field])}"
                     + (f" {unit}." if unit else "."),
        })


def add_comparison_cases(cases, rows, rng):
    pairs = []
    for group, fields in PARAM_FIELDS_BY_GROUP.items():
        pool = [r for r in rows if r["group"] == group]
        for field in fields:
            candidates = [r for r in pool if as_numbers(r["fields"].get(field))]
            if len(candidates) >= 2:
                a, b = rng.sample(candidates, 2)
                pairs.append((a, b, field))
                break
    for i, (a, b, field) in enumerate(pairs[:10], start=1):
        acode = a["raw"]["primaryEquipCode"]
        bcode = b["raw"]["primaryEquipCode"]
        cases.append({
            "id": f"large-comparison-{i:02d}",
            "class": "comparison",
            "query": f"Compare {acode} and {bcode} by {field.lower()}.",
            "expected_rids": [],
            "required_rids": [a["id"], b["id"]],
            "expected_filter": None,
            "expected_answer_contains": [],
            "notes": f"Requires both {a['id']} ({acode}) and {b['id']} ({bcode}) in context; comparison field {field}.",
        })


def add_relation_comparison_cases(cases, rows, rng):
    by_id = {r["id"]: r for r in rows}
    relation_pairs = []
    for row in rows:
        for rel in row["raw"].get("relations") or []:
            child = by_id.get(str(rel.get("childModelID")))
            if child:
                relation_pairs.append((row, child, rel.get("parentComponent") or "integration"))
    rng.shuffle(relation_pairs)
    for i, (parent, child, component) in enumerate(relation_pairs[:8], start=1):
        pcode = parent["raw"]["primaryEquipCode"]
        ccode = child["raw"]["primaryEquipCode"]
        cases.append({
            "id": f"large-relation-{i:02d}",
            "class": "comparison",
            "query": f"How is {ccode} related to {pcode}?",
            "expected_rids": [],
            "required_rids": [parent["id"], child["id"]],
            "expected_filter": None,
            "expected_answer_contains": [],
            "notes": f"Relation edge via {component}; parent {parent['id']} ({pcode}) child {child['id']} ({ccode}).",
        })


def add_analytic_cases(cases, rows):
    analytic_specs = [
        ("fighter-aircraft", "List fighter aircraft records.", {"systemType": {"in": ["Fighter Aircraft"]}}),
        ("sam-systems", "Which long-range SAM systems are in the corpus?", {"systemType": {"in": ["Long-Range SAM System"]}}),
        ("aam", "Which air-to-air missile records are in the corpus?", {"systemType": {"in": ["Air-to-Air Missile"]}}),
        ("air-defense-radars", "List air defense radars.", {"systemType": {"in": ["Air Defense Radar"]}}),
        ("india-operators", "Which systems are operated by India?", {"Operated by (country)": {"contains": ["India"]}}),
        ("projected", "Which records have projected fielding in the next 0 to 5 years?", {"Fielding status": {"contains": ["Projected"]}}),
        ("in-production", "Which missile records are in production?", {"systemGroup": {"in": ["Missiles"]}, "Status": {"in": ["In Production"]}}),
        ("modern-aircraft", "Which aircraft entered service after 2015?", {"systemGroup": {"in": ["Aircraft"]}, "serviceEntryYear": {"min": 2015}}),
        ("long-range-missiles", "Which missiles have maximum range over 1000 km?", {"systemGroup": {"in": ["Missiles"]}, "Maximum range": {"min": 1000, "unit": "km"}}),
        ("cheap-sensors", "Which sensors cost less than $5 million?", {"systemGroup": {"in": ["Sensors"]}, "Unit cost": {"max": 5000000, "unit": "USD"}}),
        ("fast-naval", "Which naval platforms exceed 30 knots?", {"systemGroup": {"in": ["Naval Platforms"]}, "Maximum sea speed": {"min": 30, "unit": "kn"}}),
        ("heavy-ground", "Which ground vehicles weigh over 60000 kg?", {"systemGroup": {"in": ["Ground Vehicles"]}, "Combat weight": {"min": 60000, "unit": "kg"}}),
        ("high-satellites", "Which space systems operate above 20000 km altitude?", {"systemGroup": {"in": ["Space Systems"]}, "Orbital altitude": {"min": 20000, "unit": "km"}}),
        ("high-node-c4", "Which C4ISR systems support more than 1000 nodes?", {"systemGroup": {"in": ["C4ISR and Electronic Warfare"]}, "Node capacity": {"min": 1000, "unit": "nodes"}}),
        ("powerful-engines", "Which propulsion systems have maximum thrust above 150 kN?", {"systemGroup": {"in": ["Power and Propulsion Systems"]}, "Maximum thrust": {"min": 150, "unit": "kN"}}),
    ]
    idx = 1
    for suffix, query, flt in analytic_specs:
        ids = expected_for(rows, flt)
        if not ids:
            continue
        cases.append({
            "id": f"large-analytic-{idx:02d}-{suffix}",
            "class": "analytic",
            "query": query,
            "expected_rids": ids,
            "required_rids": [],
            "expected_filter": flt,
            "expected_answer_contains": [],
            "notes": f"{len(ids)} matching records shown/capped to first 40 IDs.",
        })
        idx += 1


def add_prose_cases(cases, rows, rng):
    for i, row in enumerate(pick_by_group(rows, rng, n_per_group=1), start=1):
        code = row["raw"]["primaryEquipCode"]
        cases.append({
            "id": f"large-prose-{i:02d}",
            "class": "prose",
            "query": f"What is the integration purpose of {code}?",
            "expected_rids": [row["id"]],
            "required_rids": [],
            "expected_filter": None,
            "expected_answer_contains": [],
            "notes": "Targets the synthetic Integration description section.",
        })


def add_negative_cases(cases):
    queries = [
        "What is the reactor output of the fictional Atlantis-class cruiser?",
        "How many Iron Dome batteries are in this synthetic corpus?",
        "What is the crew complement of the B-2 Spirit bomber?",
        "Which record describes the Lunar Spear hypersonic glider?",
        "What is the range of the non-existent QZ-999 missile?",
    ]
    for i, query in enumerate(queries, start=1):
        cases.append({
            "id": f"large-negative-{i:02d}",
            "class": "negative",
            "query": query,
            "expected_rids": [],
            "required_rids": [],
            "expected_filter": None,
            "expected_answer_contains": [],
            "notes": "No matching record should exist in the generated large-test-corpus.",
        })


def validate_filters(cases, cat):
    missing = []
    for case in cases:
        for field in (case.get("expected_filter") or {}):
            if field not in cat:
                missing.append((case["id"], field))
    if missing:
        details = ", ".join(f"{cid}:{field}" for cid, field in missing)
        raise RuntimeError(f"expected_filter field(s) absent from catalogue: {details}")


def build_eval_set(corpus: Path, seed: int):
    rng = random.Random(seed)
    rows = load_records(corpus)
    cat = catalogue.build_catalogue([r["raw"] for r in rows])
    cases = []
    add_lookup_cases(cases, rows, rng)
    add_parametric_cases(cases, rows, rng)
    add_comparison_cases(cases, rows, rng)
    add_relation_comparison_cases(cases, rows, rng)
    add_analytic_cases(cases, rows)
    add_prose_cases(cases, rows, rng)
    add_negative_cases(cases)
    validate_filters(cases, cat)
    return {
        "version": 1,
        "created": "2026-07-08",
        "notes": (
            "Gold set for large-test-corpus. Generated from the corpus contents "
            "by generate_large_eval_set.py; expected_rids are ANY-OF unless "
            "required_rids is present."
        ),
        "source_corpus": str(corpus),
        "cases": cases,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = p.parse_args(argv)
    data = build_eval_set(args.corpus, args.seed)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8", newline="\n")
    classes = {}
    for case in data["cases"]:
        classes[case["class"]] = classes.get(case["class"], 0) + 1
    print(f"Wrote {len(data['cases'])} cases to {args.out}")
    print("Classes:", ", ".join(f"{k}={classes[k]}" for k in sorted(classes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
