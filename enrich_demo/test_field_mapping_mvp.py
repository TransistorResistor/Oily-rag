#!/usr/bin/env python3
"""Offline regression gate for the catalogue-driven enrichment mapper MVP."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pipeline
import refcat
import report
import state


def _rc():
    return refcat.load_reference()


def _ctx(rc, text, text_fields=False):
    ctx = pipeline.DocContext(rc, text)
    ctx.field_mapper = "catalogue"
    ctx.text_fields = text_fields
    return ctx


def test_generated_catalogue_uses_schema_metadata():
    rc = _rc()
    audit = rc.field_audit()
    assert audit["field_count"] >= 80
    maximum_range = next(f for f in audit["fields"]
                         if f["field"] == "Maximum range")
    assert maximum_range["definitions"]
    assert "Number" in maximum_range["data_types"]
    assert "km" in maximum_range["units"]
    assert maximum_range["examples"]


def test_curated_and_contextual_aliases():
    rc = _rc()
    mapped = rc.resolve_attribute(
        "emplacement duration",
        {"value": "100", "unit": "s",
         "quote": "Emplacement duration was 100 s."}, "1010")
    assert mapped["field"] == "Deployment time"
    assert mapped["mapping_method"] == "curated_alias"
    bare = rc.resolve_attribute(
        "time", {"value": "100", "unit": "s", "quote": "Time (s) 100"},
        "1010")
    assert bare["field"] is None
    assert bare["mapping_status"] == "ambiguous"
    contextual = rc.resolve_attribute(
        "time", {"value": "100", "unit": "s",
                 "quote": "Deployment time was 100 s."}, "1010")
    assert contextual["field"] == "Deployment time"
    assert contextual["mapping_method"] == "contextual_alias"


def test_split_dual_value_recovered_from_quote_and_parked():
    rc = _rc()
    text = ("Derby is an air-to-air missile. "
            "Range (km/NM) | 100 / 181 | Time (s) | 100 | Length | 10 m")
    claim = {"entity": "Derby", "attribute": "Range", "value": "100",
             "unit": "km/NM", "qualifier": "100 / 181",
             "quote": "Range (km/NM) | 100 / 181"}
    result = pipeline.classify_claim(
        claim, "1023", _ctx(rc, text), rc, text)
    assert result["status_hint"] == "parked"
    assert result["park_reason"] == "inconsistent_dual"


def test_incompatible_unit_parks_even_when_field_is_empty():
    rc = _rc()
    text = "The S-400 air defence system has a maximum altitude of 30 minutes."
    claim = {"entity": "S-400", "attribute": "maximum altitude",
             "value": "30", "unit": "minutes", "qualifier": None,
             "quote": text}
    result = pipeline.classify_claim(
        claim, "1010", _ctx(rc, text), rc, text)
    assert result["status_hint"] == "parked"
    assert result["park_reason"] == "incompatible_unit"


def test_text_gap_match_and_difference_policy():
    rc = _rc()
    match_text = "Derby air-to-air missile manufacturer is Rafael Advanced Defense Systems."
    match_claim = {"entity": "Derby", "attribute": "Manufacturer",
                   "value": "Rafael Advanced Defense Systems", "unit": None,
                   "qualifier": None, "quote": match_text}
    match = pipeline.classify_claim(
        match_claim, "1023", _ctx(rc, match_text, True), rc, match_text)
    assert match["status_hint"] == "dropped"
    assert match["drop"] == "already_present"

    diff_text = "Derby air-to-air missile manufacturer is Example Aerospace."
    diff_claim = dict(match_claim, value="Example Aerospace", quote=diff_text)
    difference = pipeline.classify_claim(
        diff_claim, "1023", _ctx(rc, diff_text, True), rc, diff_text)
    assert difference["status_hint"] == "parked"
    assert difference["park_reason"] == "text_difference"

    gap_text = "The S-400 air defence system uses command guidance."
    gap_claim = {"entity": "S-400", "attribute": "Guidance system",
                 "value": "command guidance", "unit": None,
                 "qualifier": None, "quote": gap_text}
    gap = pipeline.classify_claim(
        gap_claim, "1010", _ctx(rc, gap_text, True), rc, gap_text)
    assert gap["status_hint"] == "ok"
    assert gap["proposal_type"] == "gap_fill"


def test_negated_claim_parks_before_classification():
    rc = _rc()
    text = ("The Derby air-to-air missile does not have a maximum range "
            "of 40 km.")
    claim = {"entity": "Derby", "attribute": "maximum range", "value": "40",
             "unit": "km", "qualifier": None, "quote": text}
    con = state.connect(":memory:")
    doc = {"doc_id": "negated", "title": "Negated", "path": "negated.pdf",
           "date": None, "content_hash": "negated", "text": text}
    rows = pipeline.process_document(
        doc, _ctx(rc, text), [claim], rc, con, 1, "stub")
    assert len(rows) == 1
    assert rows[0]["status"] == "parked"
    assert rows[0]["park_reason"] == "nonasserted"


def test_source_scan_parks_unextracted_intradoc_conflict():
    rc = _rc()
    text = ("The NASAMS surface-to-air missile system reaches a maximum "
            "altitude of 21 km. Parameter Value Unit Maximum Altitude 16 km.")
    claim = {"entity": "NASAMS", "attribute": "maximum altitude",
             "value": "21", "unit": "km", "qualifier": None,
             "quote": ("The NASAMS surface-to-air missile system reaches a "
                       "maximum altitude of 21 km.")}
    con = state.connect(":memory:")
    doc = {"doc_id": "intradoc", "title": "Intradoc", "path": "intradoc.pdf",
           "date": None, "content_hash": "intradoc", "text": text}
    rows = pipeline.process_document(
        doc, _ctx(rc, text), [claim], rc, con, 1, "stub")
    assert len(rows) == 1
    assert rows[0]["status"] == "parked"
    assert rows[0]["park_reason"] == "intradoc_conflict"


def test_single_flattened_header_does_not_invent_conflict():
    rc = _rc()
    text = ("NASAMS surface-to-air missile assessed figures. System Radar "
            "Detection Range (km) Maximum Altitude (km) Deployment Time (min) "
            "NASAMS 120 21 10")
    claim = {"entity": "NASAMS", "attribute": "Maximum Altitude",
             "value": "21", "unit": "km", "qualifier": None, "quote": text}
    con = state.connect(":memory:")
    doc = {"doc_id": "flat", "title": "Flat", "path": "flat.pdf",
           "date": None, "content_hash": "flat", "text": text}
    rows = pipeline.process_document(
        doc, _ctx(rc, text), [claim], rc, con, 1, "stub")
    assert len(rows) == 1
    assert rows[0]["status"] == "surfaced"
    assert rows[0]["park_reason"] is None


def test_mapping_diagnostics_and_alias_suggestions_are_persisted():
    con = state.connect(":memory:")
    state.insert_claim(con, {
        "doc_id": "d1", "attribute": "overall physical size",
        "mapping_candidate": "Length", "mapping_method": "definition_profile",
        "mapping_score": 0.72, "mapping_tier": "medium",
        "mapping_status": "resolved", "quote": "Overall physical size is 10 m",
        "status": "parked", "park_reason": "text_difference",
    })
    con.commit()
    row = con.execute(
        "SELECT mapping_candidate,mapping_score FROM claims").fetchone()
    assert row["mapping_candidate"] == "Length"
    assert row["mapping_score"] == 0.72
    suggestions = report._alias_suggestions(con)
    assert suggestions[0]["term"] == "overall physical size"
    assert suggestions[0]["field"] == "Length"


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} field-mapping MVP tests passed")


if __name__ == "__main__":
    main()
