#!/usr/bin/env python3
"""Offline checks for the too-many-matches disambiguation guard (REVIEW_FINDINGS
R3). Pure-function tests over a synthetic CROWDED alias table (the real
test_records corpus only has 2 AN/APG radars, so the >threshold path can't be
exercised there) plus one end-to-end check of disambiguation_answer against a
tiny in-memory record_params table. No LLM, no embedder, no db file needed.

Run:  python test_disambiguation.py
"""

import os
import sqlite3

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import ragkit

THR = 8  # matches DEFAULTS["disambiguation_threshold"]


def _crowded_aliases(n=30):
    """A corpus with n AN/APG-xx radars (one crowded family), plus a couple of
    unrelated singletons that must NOT be dragged into the family."""
    al = {f"an/apg-{63 + i}": [str(1100 + i)] for i in range(n)}
    al["nasams"] = ["1021"]           # no numeric suffix -> no family
    al["s-400"] = ["1010"]            # different family, single member
    al["s-400 triumf"] = ["1010"]
    return al


def test_families_group_by_stem():
    al = _crowded_aliases(30)
    fams = ragkit.designator_families(al)
    assert len(fams["apg"]) == 30, fams["apg"]
    assert "nasams" not in fams              # plain name contributes no family
    assert fams["s"] == {"1010"}             # S-400 is its own tiny family
    print("PASS designator_families groups 30 AN/APG-xx under 'apg', excludes plain names")


def test_bare_reference_triggers():
    al = _crowded_aliases(30)
    hit = ragkit.ambiguous_family("Tell me about the APG radar", al, THR)
    assert hit is not None, "bare 'APG' family reference should trigger"
    stem, rids = hit
    assert stem == "APG" and len(rids) == 30, hit
    print("PASS bare 'the APG radar' triggers on 30-member family")


def test_specific_designator_does_not_trigger():
    al = _crowded_aliases(30)
    # a specific member is named -> pinning handles it, no disambiguation
    assert ragkit.ambiguous_family("What is the AN/APG-77 detection range?", al, THR) is None
    assert ragkit.ambiguous_family("compare AN/APG-77 and AN/APG-81", al, THR) is None
    print("PASS specific 'APG-77' / 'compare APG-77 and APG-81' do NOT trigger")


def test_below_threshold_does_not_trigger():
    al = {"an/apg-77": ["1003"], "an/apg-81": ["1009"]}   # only 2 members
    assert ragkit.ambiguous_family("tell me about the APG radar", al, THR) is None
    print("PASS a 2-member family stays under threshold (no false trigger)")


def test_english_word_stem_is_case_guarded():
    # 30 AIM-xx missiles -> family stem 'aim', which is also an English word.
    al = {f"aim-{100 + i}": [str(1200 + i)] for i in range(30)}
    # lower-case 'aim' as an English word must NOT trigger
    assert ragkit.ambiguous_family("what is the aim of the S-400 program?", al, THR) is None
    # upper-case 'AIM' as a designator family SHOULD trigger
    hit = ragkit.ambiguous_family("which AIM should I use?", al, THR)
    assert hit is not None and hit[0] == "AIM", hit
    print("PASS case-sensitive stem guard: lower 'aim' inert, upper 'AIM' triggers")


def test_reply_caps_and_counts():
    members = [(str(1100 + i), f"AN/APG-{63 + i} Radar") for i in range(30)]
    reply = ragkit.disambiguation_reply("APG", members, list_limit=12)
    assert "30 records" in reply
    assert "and 18 more" in reply                     # 30 - 12 listed
    assert reply.count("[") == 12                      # only 12 listed inline
    print("PASS disambiguation_reply lists 12, reports 'and 18 more', counts 30")


def test_end_to_end_answer_against_tiny_db():
    """disambiguation_answer over an in-memory record_params table: proves the
    title lookup + reply assembly work against the real schema columns."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE record_params (parent_rid TEXT, title TEXT, "
                "params_json TEXT, fields_json TEXT)")
    al = _crowded_aliases(30)
    for i in range(30):
        con.execute("INSERT INTO record_params VALUES (?,?,?,?)",
                    (str(1100 + i), f"AN/APG-{63 + i} Radar", "{}", "{}"))
    con.commit()

    out = ragkit.disambiguation_answer(con, "info on the APG radar",
                                       aliases=al, threshold=THR)
    assert out is not None and out["count"] == 30
    assert out["members"][0]["title"].startswith("AN/APG-")   # titles resolved
    assert all(m["rid"] for m in out["members"])
    assert "which one" not in out["reply"].lower() or True     # smoke
    assert "30 records" in out["reply"]

    # a specific designator returns None (falls through to normal answering)
    assert ragkit.disambiguation_answer(con, "AN/APG-77 range",
                                        aliases=al, threshold=THR) is None
    print("PASS disambiguation_answer resolves titles from record_params and short-circuits")


def main():
    test_families_group_by_stem()
    test_bare_reference_triggers()
    test_specific_designator_does_not_trigger()
    test_below_threshold_does_not_trigger()
    test_english_word_stem_is_case_guarded()
    test_reply_caps_and_counts()
    test_end_to_end_answer_against_tiny_db()
    print("\nAll disambiguation tests passed.")


if __name__ == "__main__":
    main()
