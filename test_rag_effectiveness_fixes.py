#!/usr/bin/env python3
"""Focused regression checks for retrieval/filter effectiveness fixes.

Plain script, matching this repo's no-pytest convention.
"""

import sqlite3

import eval as eval_mod
import catalogue
import ragkit
import record_model


def test_numeric_interval_filter_semantics():
    raw = {
        "modelID": 1,
        "nomenclature": "Interval Test",
        "parametrics": [
            {"parameter": "Length", "parameterValue": "1 to 2", "uom": "m",
             "dataType": "Number"},
            {"parameter": "Width", "parameterValue": "1.5", "uom": "m",
             "dataType": "Number", "comments": "Typical range 1-2 m"},
        ],
    }
    fields, units = record_model.typed_fields(record_model.normalize_record(raw))
    assert fields["Length"] == {"lo": 1.0, "hi": 2.0}
    assert fields["Width"] == [1.5, {"lo": 1.0, "hi": 2.0}]
    assert units["Length"] == "m"
    assert ragkit._passes(fields, {"Length": {"type": "numeric", "min": 1.8}})
    assert ragkit._passes(fields, {"Width": {"type": "numeric", "min": 1.8}})
    assert not ragkit._passes({"Length": [1.0, 2.0]},
                              {"Length": {"type": "numeric", "min": 1.4, "max": 1.6}})


def test_interval_catalogue_and_unit_canonicalization():
    raws = [
        {"modelID": 1, "nomenclature": "A",
         "parametrics": [{"parameter": "Diameter", "parameterValue": "0.2",
                          "uom": "m", "dataType": "Number"}]},
        {"modelID": 2, "nomenclature": "B",
         "parametrics": [{"parameter": "Diameter", "parameterValue": "150",
                          "uom": "mm", "dataType": "Number"}]},
        {"modelID": 3, "nomenclature": "C",
         "parametrics": [{"parameter": "Diameter", "parameterValue": "100 to 200",
                          "uom": "mm", "dataType": "Number"}]},
    ]
    cat = catalogue.build_catalogue(raws)
    assert cat["Diameter"]["type"] == "numeric"
    assert cat["Diameter"]["unit"] in {"m", "mm"}
    fields, units = record_model.typed_fields(record_model.normalize_record(raws[1]))
    converted = ragkit._canonicalize_stored_fields(fields, units, cat)
    if cat["Diameter"]["unit"] == "m":
        assert abs(converted["Diameter"] - 0.15) < 1e-9
    else:
        assert abs(converted["Diameter"] - 150.0) < 1e-9


def test_field_remap_and_degenerate_filter():
    cat = {
        "Operated by (country)": {
            "type": "multi_value", "count": 2,
            "values": ["India", "United States"],
        },
        "systemType": {
            "type": "categorical", "count": 2,
            "values": ["Sensor", "Missile"],
        },
    }
    clean, errors = ragkit.validate_filter(
        {"Operated by": {"contains": ["India"]}}, cat)
    assert not errors
    assert "Operated by (country)" in clean
    assert clean["Operated by (country)"]["_field_remapped"] == "Operated by"

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE record_params (parent_rid TEXT, title TEXT, fields_json TEXT)")
    con.executemany(
        "INSERT INTO record_params VALUES (?,?,?)",
        [("1", "A", "{}"), ("2", "B", "{}")],
    )
    deg, reason = ragkit.is_degenerate_filter(
        con,
        {"systemType": {"type": "categorical", "in": ["Sensor", "Missile"]}},
        cat,
        matched_records=2,
    )
    assert deg and "systemType" in reason


def test_table_sort_stacked_numeric_values():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE record_params (parent_rid TEXT, title TEXT, params_json TEXT, fields_json TEXT)")
    con.executemany(
        "INSERT INTO record_params VALUES (?,?,?,?)",
        [
            ("a", "A", '{"Maximum range":{"value":"591; 1635","unit":"km","descr":null}}',
             '{"Maximum range":[591,1635]}'),
            ("b", "B", '{"Maximum range":{"value":"1426","unit":"km","descr":null}}',
             '{"Maximum range":1426}'),
        ],
    )
    con.execute("CREATE TABLE records (rowid INTEGER PRIMARY KEY, parent_rid TEXT, rid TEXT, title TEXT, text TEXT, embedding BLOB)")
    cat = {"Maximum range": {"type": "numeric", "unit": "km", "count": 2}}
    table = ragkit.record_table(
        con, "Rank by maximum range", {}, catalogue=cat, parent_rids=["a", "b"])
    assert [r["rid"] for r in table["rows"]] == ["a", "b"]


def test_eval_numeric_answer_and_grouped_citations():
    case = {
        "id": "x", "class": "parametric",
        "expected_answer_contains": ["172869426"],
    }
    scored = eval_mod.score_answer(case, "The unit cost is 172,869,426 USD [900006].")
    assert scored["answer_contains_ok"]

    cites = eval_mod.score_citations("Compare them [1001, 1004] and [1007].",
                                     ["1001", "1004", "1007"])
    assert cites["cited"] == ["1001", "1004", "1007"]
    assert cites["hallucinated"] == []

    cites = eval_mod.score_citations(
        "Uses [Record 900203] and status [Parameter Status].",
        ["900203"])
    assert cites["cited"] == ["900203"]
    assert cites["hallucinated"] == []

    neg = eval_mod.score_answer(
        {"id": "n", "class": "negative", "expected_answer_contains": []},
        "The corpus does not mention Iron Dome, so the answer is unknown based on the context.")
    assert neg["negative_ok"]


def test_eval_contains_filter_match_and_case_selection():
    assert eval_mod._filter_field_match(
        {"contains": ["India"]}, {"type": "multi_value", "contains": ["India"]})
    assert eval_mod._filter_field_match(
        {"in": ["F-35 Lightning II"]},
        {"type": "categorical", "in": ["Lockheed Martin F-35 Lightning II"]})
    selected = eval_mod._select_cases(
        [{"id": "a", "class": "lookup"}, {"id": "b", "class": "analytic"}],
        case_ids="b", classes="analytic")
    assert [c["id"] for c in selected] == ["b"]


def test_pinned_parameter_prompt_is_authoritative_first():
    contexts = [
        {"rid": "2", "title": "Other", "text": "Title: Other\nParameter Range = 25 km"},
        {"rid": "1", "title": "Named", "text": "Title: Named\nParameter Range = 180 km",
         "section": "pinned_parameters"},
    ]
    prompt = ragkit.build_prompt("What is the range of Named?", contexts)
    assert prompt.index("Authoritative parameters") < prompt.index("[2] Other")
    assert "prefer these for exact field values" in prompt


def test_direct_parameter_answer_and_aliases():
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE record_params "
        "(parent_rid TEXT, title TEXT, params_json TEXT, fields_json TEXT)")
    con.execute(
        "INSERT INTO record_params VALUES (?,?,?,?)",
        ("1002", "AIM-120 AMRAAM",
         '{"Maximum range":{"value":"105 (AIM-120C-7); 180 (AIM-120D)",'
         '"unit":"km","descr":"Maximum effective range"},'
         '"Detection range":{"value":"400","unit":"km","descr":"Radar range"}}',
         "{}"))
    catalogue = {
        "Maximum range": {"type": "numeric", "unit": "km"},
        "Detection range": {"type": "numeric", "unit": "km"},
        "Radar": {"type": "categorical"},
    }
    aliases = {"aim-120 amraam": ["1002"], "aim-120": ["1002"]}

    direct = ragkit.direct_parameter_answer(
        con, "What is the maximum range of the AIM-120 AMRAAM?",
        catalogue=catalogue, aliases=aliases,
        field_aliases={"range": "Maximum range"})
    assert direct and "180" in direct["reply"] and "[1002]" in direct["reply"]
    assert direct["field"] == "Maximum range"

    direct = ragkit.direct_parameter_answer(
        con, "What is the detection range of the AIM-120 AMRAAM?",
        catalogue=catalogue, aliases=aliases,
        field_aliases={"range": "Maximum range"})
    assert direct and direct["field"] == "Detection range"

    direct = ragkit.direct_parameter_answer(
        con, "What is the throw distance of AIM-120?",
        catalogue=catalogue, aliases=aliases,
        field_aliases={"throw distance": "Maximum range"})
    assert direct and direct["field"] == "Maximum range"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_rag_effectiveness_fixes.py: all checks passed")
