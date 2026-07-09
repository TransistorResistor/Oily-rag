#!/usr/bin/env python3
"""Offline direct context checks for REVIEW_FINDINGS I3 same-record work."""

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import ragkit


DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag_test.db")


def _base(con, query, aliases):
    pins = ragkit.match_entities(query, aliases)
    contexts = ragkit.retrieve(con, query, pin_entities=pins)
    return pins, contexts


def main():
    con = ragkit.connect(DB)
    catalogue = ragkit.load_catalogue(con)
    aliases = ragkit.load_aliases(con)

    query = "what is the range of the S-400?"
    pins, contexts = _base(con, query, aliases)
    assert pins == ["2049"]
    before = ragkit.build_prompt(query, contexts)
    added = ragkit.pinned_parameter_passages(
        con, query, pins, catalogue, contexts=contexts)
    assert added and added[0]["rid"] == "2049"
    line = "Parameter Range = 400 km"
    assert line not in before
    after = ragkit.build_prompt(query, contexts + added)
    assert line in after
    print("PASS pinned field query adds S-400 parameter value")

    query = "compare the F-22 and F-35"
    pins, contexts = _base(con, query, aliases)
    assert set(pins) == {"2030", "2031"}
    table = ragkit.record_table(
        con, query, {}, catalogue=catalogue, parent_rids=pins)
    assert table and {row["rid"] for row in table["rows"]} == set(pins)
    prompt = ragkit.build_prompt(query, contexts, table=table)
    assert "[2030]" in prompt and "[2031]" in prompt
    assert len(table["rows"]) == 2
    print("PASS two-pin comparison table contains exactly the pinned records")

    query = "summarize the collection"
    pins, contexts = _base(con, query, aliases)
    assert not pins
    before = ragkit.build_prompt(query, contexts)
    added = ragkit.pinned_parameter_passages(
        con, query, pins, catalogue, contexts=contexts)
    assert not added and ragkit.build_prompt(query, contexts + added) == before
    print("PASS no-pin context is byte-identical")

    query = "tell me about the F-22"
    pins, contexts = _base(con, query, aliases)
    assert pins == ["2030"]
    before = ragkit.build_prompt(query, contexts)
    added = ragkit.pinned_parameter_passages(
        con, query, pins, catalogue, contexts=contexts)
    assert not added and ragkit.build_prompt(query, contexts + added) == before
    print("PASS pinned no-field context is byte-identical")


if __name__ == "__main__":
    main()
