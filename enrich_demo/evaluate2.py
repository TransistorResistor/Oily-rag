#!/usr/bin/env python3
"""
evaluate.py - score the final proposals2.json against gold2.json.

Per proposal type: precision / recall. Distractor + unlinked docs must yield ZERO
proposals. False positives are printed verbatim (they are the interesting part).

Run after both batches:  python evaluate.py
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _num(s):
    m = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", str(s or ""))
    return float(m.group(0).replace(",", "")) if m else None


def _doc_id(path):
    return os.path.splitext(os.path.basename(path or ""))[0]


def load(props_path=None):
    # Optional CLI arg selects the proposals file (default proposals2.json), so a
    # per-DB proposals_<stem>.json can be scored without renaming. A bare stem or
    # relative name is resolved against HERE.
    gold = json.load(open(os.path.join(HERE, "gold2.json"), encoding="utf-8"))
    pp = props_path or "proposals2.json"
    if not os.path.isabs(pp):
        pp = os.path.join(HERE, pp)
    props = json.load(open(pp, encoding="utf-8"))
    return gold, props


def gold_proposals(gold):
    """Flatten gold2.json into expected surfaced proposals + expected zero docs."""
    exp = []
    zero_docs = set()
    parks = []
    for g in gold:
        if g["expect_zero"]:
            zero_docs.add(g["doc_id"])
        for item in g["gold"]:
            t = item["type"]
            if t == "park":
                parks.append((item["record"], item.get("field")))
            elif t == "relation":
                exp.append({"type": "relation", "record": item["record"],
                            "other": item["other"], "doc": g["doc_id"]})
            else:
                exp.append({"type": t, "record": item["record"],
                            "field": item["field"], "value": item.get("value"),
                            "corroborated": item.get("corroborated", False),
                            "doc": g["doc_id"]})
    return exp, zero_docs, parks


def match(gp, props):
    """Return the list of proposal_ids that satisfy gold proposal gp (empty if
    unmet). Corroboration is scored at the CLUSTER level: a corroborated gold
    item is met when >=2 distinct source docs surface the same record+field,
    even though (by design) they materialise as sibling proposals keyed on
    different full fingerprints (different values/units)."""
    if gp.get("corroborated"):
        sibs = [p for p in props if p["record"] == gp["record"]
                and (p.get("field") or "").lower() == (gp.get("field") or "").lower()]
        docs = set()
        for p in sibs:
            docs |= {_doc_id(s["doc_path"]) for s in p["sources"]}
        return [p["proposal_id"] for p in sibs] if len(docs) >= 2 else []
    for p in props:
        if p["record"] != gp["record"]:
            continue
        if gp["type"] == "relation":
            other = (gp["other"] or "").lower()
            val = (p.get("value") or "").lower()
            if p["type"] == "relation" and other.split()[0] in val:
                return [p["proposal_id"]]
            continue
        if p["type"] != gp["type"]:
            continue
        if (p.get("field") or "").lower() != (gp.get("field") or "").lower():
            continue
        gv, pv = gp.get("value"), p.get("value")
        gn, pn = _num(gv), _num(pv)
        if gn is not None and pn is not None:
            if abs(gn - pn) <= max(1e-6, abs(gn) * 0.03):
                return [p["proposal_id"]]
        elif gv and pv and (gv.lower() in pv.lower() or pv.lower() in gv.lower()):
            return [p["proposal_id"]]
    return []


def main():
    gold, props = load(sys.argv[1] if len(sys.argv) > 1 else None)
    exp, zero_docs, parks = gold_proposals(gold)

    # de-dup corroboration: two gold rows (a/b) map to one expectation
    seen = set()
    exp2 = []
    for gp in exp:
        key = (gp["type"], gp["record"], gp.get("field"), gp.get("corroborated"))
        if gp.get("corroborated"):
            if key in seen:
                continue
            seen.add(key)
        exp2.append(gp)
    exp = exp2

    types = ["gap_fill", "conflict", "relation"]
    tp = {t: 0 for t in types}
    fn = {t: 0 for t in types}
    matched_props = set()
    missed = []
    for gp in exp:
        m = match(gp, props)
        if m:
            tp[gp["type"]] += 1
            matched_props.update(m)
        else:
            fn[gp["type"]] += 1
            missed.append(gp)

    # false positives: any surfaced proposal not matching a gold expectation
    fps = []
    for p in props:
        if p["proposal_id"] in matched_props:
            continue
        # a proposal whose only sources are expect_zero docs is a hard FP
        srcdocs = {_doc_id(s["doc_path"]) for s in p["sources"]}
        fps.append({"proposal": p, "from_distractor": bool(srcdocs & zero_docs)})

    # count FPs per type (approx: assign to the proposal's type)
    fp_ct = {t: 0 for t in types}
    for f in fps:
        t = f["proposal"]["type"]
        fp_ct[t] = fp_ct.get(t, 0) + 1

    print("=" * 68)
    print("REVERSE-ENRICHMENT EVAL  (proposals2.json vs gold2.json)")
    print("=" * 68)
    print(f"{'type':10} {'TP':>3} {'FP':>3} {'FN':>3} {'precision':>10} "
          f"{'recall':>8}")
    for t in types:
        prec = tp[t] / (tp[t] + fp_ct.get(t, 0)) if (tp[t] + fp_ct.get(t, 0)) else 1.0
        rec = tp[t] / (tp[t] + fn[t]) if (tp[t] + fn[t]) else 1.0
        print(f"{t:10} {tp[t]:>3} {fp_ct.get(t,0):>3} {fn[t]:>3} "
              f"{prec:>10.2f} {rec:>8.2f}")
    TP, FP, FN = sum(tp.values()), len(fps), sum(fn.values())
    P = TP / (TP + FP) if (TP + FP) else 1.0
    R = TP / (TP + FN) if (TP + FN) else 1.0
    print("-" * 68)
    print(f"{'OVERALL':10} {TP:>3} {FP:>3} {FN:>3} {P:>10.2f} {R:>8.2f}")

    print("\nDISTRACTOR / UNLINKED CHECK (must be zero proposals):")
    hard = [f for f in fps if f["from_distractor"]]
    print(f"  proposals traceable to {sorted(zero_docs)}: {len(hard)}")

    if missed:
        print("\nMISSED (false negatives):")
        for gp in missed:
            print(f"  - {gp['type']} {gp['record']} :: "
                  f"{gp.get('field') or gp.get('other')} = {gp.get('value')}")

    if fps:
        print("\nFALSE POSITIVES (verbatim):")
        for f in fps:
            p = f["proposal"]
            tag = " [FROM DISTRACTOR]" if f["from_distractor"] else ""
            print(f"  - [{p['type']}] {p['record']} :: {p['field']} = "
                  f"{p['value']!r}{tag}")
            for s in p["sources"]:
                print(f"      src {_doc_id(s['doc_path'])}: {s['quote'][:110]!r}")
    else:
        print("\nNo false positives.")

    print("\nPARKED-AS-EXPECTED CHECK:")
    for rec, fld in parks:
        hit = any(p["record"] == rec and (p.get("field") or "") == (fld or "")
                  for p in props)
        print(f"  {rec} :: {fld} expected PARKED (not surfaced): "
              f"{'OK' if not hit else 'LEAKED as proposal!'}")


if __name__ == "__main__":
    main()
