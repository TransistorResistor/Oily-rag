#!/usr/bin/env python3
"""Targeted, offline regression checks for the 2026-07-05 robustness fixes."""

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pipeline
import refcat
import state


def test_docs_seen():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "state.db")
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE docs_seen(doc_id TEXT PRIMARY KEY,path TEXT,"
                    "title TEXT,content_hash TEXT UNIQUE,doc_date TEXT,"
                    "first_run INTEGER,llm_model TEXT,n_claims INTEGER)")
        old.execute("INSERT INTO docs_seen VALUES(?,?,?,?,?,?,?,?)",
                    ("legacy", "old.pdf", "Old", "legacy-hash", None,
                     1, "stub", 0))
        old.commit()
        old.close()
        con = state.connect(path)
        assert state.already_seen(con, "legacy-hash") == "legacy"
        state.record_doc(con, "same-doc", "text.pdf", "T", "text-hash", None,
                         1, "stub", 0)
        state.record_doc(con, "same-doc", "md.pdf", "T", "md-hash", None,
                         2, "stub", 0)
        assert state.already_seen(con, "text-hash") == "same-doc"
        assert state.already_seen(con, "md-hash") == "same-doc"
        state.record_doc(con, "renamed-doc", "md.pdf", "T", "md-hash", None,
                         3, "stub", 0)
        assert state.already_seen(con, "md-hash") == "renamed-doc"
        assert con.execute("SELECT COUNT(*) FROM docs_seen").fetchone()[0] == 3
        con.close()


def _claim(con, doc_id, full_fp, partial_fp, value, unit):
    con.execute(
        "INSERT INTO claims(doc_id,status,park_reason,full_fp,partial_fp,"
        "value_norm,unit_norm) VALUES(?,?,?,?,?,?,?)",
        (doc_id, "parked", "uncorroborated", full_fp, partial_fp, value, unit))


def test_graduation_by_value():
    con = state.connect(":memory:")
    _claim(con, "a", "same-value-a", "same-field", 30, "km")
    _claim(con, "b", "same-value-b", "same-field", 30, "km")
    _claim(con, "c", "value-one", "conflicting-field", 20, "km")
    _claim(con, "d", "value-two", "conflicting-field", 50, "km")
    _claim(con, "e", "cross-unit-a", "cross-unit-field", 30, "km")
    _claim(con, "f", "cross-unit-b", "cross-unit-field", 98000, "ft")
    assert pipeline.graduation_pass(con) == 4
    rows = {r[0]: r[1] for r in con.execute(
        "SELECT full_fp,status FROM claims ORDER BY full_fp")}
    assert rows["same-value-a"] == rows["same-value-b"] == "surfaced"
    assert rows["value-one"] == rows["value-two"] == "parked"
    assert rows["cross-unit-a"] == rows["cross-unit-b"] == "surfaced"


def test_run_batch_continues_and_finishes():
    con = state.connect(":memory:")
    docs = [{"doc_id": str(i), "title": str(i), "text": "x", "path": str(i),
             "content_hash": f"hash-{i}", "date": None} for i in range(1, 4)]
    old_iter = pipeline.provider.iter_documents
    old_extract = pipeline.llm_mod.extract_claims
    old_ctx = pipeline.DocContext
    old_process = pipeline.process_document
    pipeline.provider.iter_documents = lambda folder, render="text": iter(docs)

    def extract(title, text, model=None):
        if title == "2":
            raise TimeoutError("synthetic timeout")
        return [], {}, "", None

    pipeline.llm_mod.extract_claims = extract
    pipeline.DocContext = lambda rc, text: type(
        "Ctx", (), {"low_trust": False})()
    pipeline.process_document = lambda *args: []
    try:
        result = pipeline.run_batch("unused", con, object(), model="stub",
                                    verbose=False)
    finally:
        pipeline.provider.iter_documents = old_iter
        pipeline.llm_mod.extract_claims = old_extract
        pipeline.DocContext = old_ctx
        pipeline.process_document = old_process
    assert result["processed"] == ["1", "3"]
    assert result["failed"] == ["2"]
    assert state.already_seen(con, "hash-1") == "1"
    assert state.already_seen(con, "hash-2") is None
    assert state.already_seen(con, "hash-3") == "3"
    run = con.execute("SELECT docs,llm_calls,error_count FROM runs").fetchone()
    assert tuple(run) == (2, 3, 1)


def test_compare_numeric_units():
    rc = object.__new__(refcat.RefCatalogue)
    rc.field_unit = lambda field: "km"
    rc.db_values = lambda mid, field: [(50, "km")]
    verdict = rc.compare_numeric(20, "kg", "Maximum range", "x")[0]
    assert verdict == "incomparable"
    rc.field_unit = lambda field: "min"
    rc.db_values = lambda mid, field: [(5, "min")]
    assert rc.compare_numeric(5, "minutes", "Deployment time", "x")[0] == "match"


def test_clean_value_unit_currency_scale():
    assert pipeline._clean_value_unit("USD 300 million", "") == (
        "300", "USD million")


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} targeted robustness tests passed")


if __name__ == "__main__":
    main()
