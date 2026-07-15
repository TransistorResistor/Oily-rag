#!/usr/bin/env python3
"""
evaluate3.py - score proposals against gold3.json (corpus 3, input-robustness).

Reuses the corpus-2b scorer verbatim (evaluate2b._num / _doc_id /
gold_proposals / match) so the matching logic lives in exactly one place; this
file only supplies the gold3 path, the default proposals name, and the banner.
Per proposal type: precision / recall. The four expect_zero traps (new-entity,
negation, intra-doc contradiction, OCR-garble) must yield ZERO proposals; the
two positive controls (one conflict, one gap_fill) prove the corpus is not
silent by accident. False positives are printed verbatim -- they are the
interesting part, since every trap here is a "must stay silent" case.

Run after processing testdocs3/ into a per-DB proposals file, e.g.:
    python ../ragkit.py ...              # (N/A - enrich side)
    python enrich.py --db run3.db run --folder testdocs3 --note corpus3
    python evaluate3.py proposals_run3.json
Default proposals file is proposals3.json.
"""

import json
import os
import sys

from evaluate2b import _doc_id, gold_proposals, match

HERE = os.path.dirname(os.path.abspath(__file__))


def load(props_path=None):
    gold = json.load(open(os.path.join(HERE, "gold3.json"), encoding="utf-8"))
    pp = props_path or "proposals3.json"
    if not os.path.isabs(pp):
        pp = os.path.join(HERE, pp)
    props = json.load(open(pp, encoding="utf-8"))
    return gold, props


def main():
    gold, props = load(sys.argv[1] if len(sys.argv) > 1 else None)
    exp, zero_docs, parks = gold_proposals(gold)

    # de-dup corroboration (a corpus-3 gold row could be flagged corroborated
    # in future; mirror evaluate2b so the two evaluators can't drift)
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

    fps = []
    for p in props:
        if p["proposal_id"] in matched_props:
            continue
        srcdocs = {_doc_id(s["doc_path"]) for s in p["sources"]}
        fps.append({"proposal": p, "from_distractor": bool(srcdocs & zero_docs)})

    fp_ct = {t: 0 for t in types}
    for f in fps:
        t = f["proposal"]["type"]
        fp_ct[t] = fp_ct.get(t, 0) + 1

    print("=" * 68)
    print("REVERSE-ENRICHMENT EVAL  (proposals3.json vs gold3.json)")
    print("input-robustness corpus: new-entity / negation / temporal /")
    print("intra-doc contradiction / OCR-garble + positive controls")
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

    print("\nMUST-STAY-SILENT CHECK (expect_zero docs, must yield 0 proposals):")
    hard = [f for f in fps if f["from_distractor"]]
    print(f"  proposals traceable to {sorted(zero_docs)}: {len(hard)}")

    if missed:
        print("\nMISSED (false negatives -- expected but not surfaced):")
        for gp in missed:
            print(f"  - {gp['type']} {gp['record']} :: "
                  f"{gp.get('field') or gp.get('other')} = {gp.get('value')}")

    if fps:
        print("\nFALSE POSITIVES (verbatim):")
        for f in fps:
            p = f["proposal"]
            tag = " [FROM EXPECT-ZERO DOC]" if f["from_distractor"] else ""
            print(f"  - [{p['type']}] {p['record']} :: {p['field']} = "
                  f"{p['value']!r}{tag}")
            for s in p["sources"]:
                print(f"      src {_doc_id(s['doc_path'])}: {s['quote'][:110]!r}")
    else:
        print("\nNo false positives.")


if __name__ == "__main__":
    main()
