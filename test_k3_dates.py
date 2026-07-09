#!/usr/bin/env python3
"""Offline regression checks for K3 in-service/IOC date support."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import catalogue
import ragkit
import record_model


def _record(proliferations=None, descriptions=None):
    return {
        "modelID": "k3-test",
        "nomenclature": "K3 Test System",
        "systemGroup": "Weapon",
        "systemType": "Test System",
        "descriptions": descriptions or [],
        "proliferations": proliferations or [],
    }


def _fields(raw, dropped=None):
    canon = record_model.normalize_record(raw, dropped=dropped)
    fields, _units = record_model.typed_fields(canon)
    return fields, canon


def test_ioc_year_parsing_variants():
    fields, canon = _fields(_record([
        {"country": "A", "type": "IOC Year", "proliferation": 2019},
        {"country": "B", "Type": "IOC", "proliferation": "2020"},
        {"country": "C", "type": "ioc year", "proliferation": "FY2021"},
        {"country": "D", "type": "IOC Year", "proliferation": "unknown"},
    ]))
    assert fields["serviceEntryYear"] == 2019
    assert fields["Operated by (country)"] == ["A", "B", "C"]
    prolif = dict(canon["prose"])["Proliferation"]
    assert "D - IOC Year unknown" in prolif


def test_earliest_across_countries_selection():
    fields, _canon = _fields(_record([
        {"country": "Later", "type": "IOC Year", "proliferation": 2015},
        {"country": "Earlier", "type": "IOC Year", "proliferation": 2007},
    ]))
    assert fields["serviceEntryYear"] == 2007


def test_ioc_country_is_operator():
    fields, _canon = _fields(_record([
        {"country": "Only IOC", "type": "IOC Year", "proliferation": 2012},
    ]))
    assert fields["Operated by (country)"] == ["Only IOC"]


def test_projected_fielding_facet_not_year():
    fields, canon = _fields(_record([
        {"country": "Future", "type": "Projected Fielding",
         "proliferation": "0 - 5 years"},
    ]))
    assert fields["Fielding status"] == ["Projected"]
    assert "serviceEntryYear" not in fields
    assert "Future - Projected Fielding (0 - 5 years)" in dict(canon["prose"])["Proliferation"]


def test_untyped_rows_keep_status_bucket_behavior():
    fields, canon = _fields(_record([
        {"country": "Operator", "proliferation": "Using"},
        {"country": "Builder", "proliferation": "Production"},
        {"country": "Retired", "proliferation": "Retired"},
    ]))
    assert fields["Operated by (country)"] == ["Operator"]
    assert fields["Produced by (country)"] == ["Builder"]
    assert "serviceEntryYear" not in fields
    assert "Retired - Retired" in dict(canon["prose"])["Proliferation"]


def test_prose_mining_hit():
    fields, _canon = _fields(_record(descriptions=[{
        "descrType": "Overview",
        "description": "The system entered service with the test force in 1999.",
    }]))
    assert fields["serviceEntryYear"] == 1999


def test_prose_mining_ambiguous_skip():
    fields, _canon = _fields(_record(descriptions=[{
        "descrType": "Overview",
        "description": "IOC 1990 and fielded 2001 across separate configurations.",
    }]))
    assert "serviceEntryYear" not in fields


def test_structured_beats_prose_precedence():
    dropped = {}
    fields, _canon = _fields(_record(
        proliferations=[
            {"country": "A", "type": "IOC Year", "proliferation": 2019},
        ],
        descriptions=[{
            "descrType": "Overview",
            "description": "The system entered service in 2001.",
        }],
    ), dropped=dropped)
    assert fields["serviceEntryYear"] == 2019
    assert "serviceEntryYear.prose_conflict" in dropped


def test_catalogue_classifies_service_year_numeric():
    cat = catalogue.build_catalogue([
        _record([{"country": "A", "type": "IOC Year", "proliferation": 1991}]),
        _record([{"country": "B", "type": "IOC Year", "proliferation": "FY2019"}]),
    ])
    assert cat["serviceEntryYear"]["type"] == "numeric"


def test_missing_numeric_filter_fails_without_error():
    clean = {"serviceEntryYear": {"type": "numeric", "min": 1990, "max": 1999}}
    assert ragkit._passes({}, clean) is False
    assert ragkit._passes({"serviceEntryYear": "unknown"}, clean) is False
    assert ragkit._passes({"serviceEntryYear": 1995}, clean) is True


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} K3 date tests passed")


if __name__ == "__main__":
    main()
