#!/usr/bin/env python3
"""Offline checks for extraction failure retryability and report file isolation."""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import pipeline
import report
import state


DOC = {
    "doc_id": "doc1",
    "title": "Doc 1",
    "text": "The 40N6 range is 380 km.",
    "path": "doc1.pdf",
    "content_hash": "hash-doc1",
    "date": None,
}


def _run_with_extract(con, extract):
    old_iter = pipeline.provider.iter_documents
    old_extract = pipeline.llm_mod.extract_claims
    old_ctx = pipeline.DocContext
    old_process = pipeline.process_document
    pipeline.provider.iter_documents = lambda folder, render="text": iter([dict(DOC)])
    pipeline.llm_mod.extract_claims = extract
    pipeline.DocContext = lambda rc, text: type("Ctx", (), {"low_trust": False})()
    pipeline.process_document = lambda *args: []
    try:
        return pipeline.run_batch("unused", con, object(), model="stub",
                                  verbose=False)
    finally:
        pipeline.provider.iter_documents = old_iter
        pipeline.llm_mod.extract_claims = old_extract
        pipeline.DocContext = old_ctx
        pipeline.process_document = old_process


def _assert_retryable_failure(con, result, err_text):
    assert result["processed"] == []
    assert result["failed"] == ["doc1"]
    assert result["error_count"] == 1
    assert state.already_seen(con, DOC["content_hash"]) is None
    row = con.execute(
        "SELECT error, raw_snippet FROM doc_failures ORDER BY failure_id DESC"
    ).fetchone()
    assert err_text in row["error"]


def test_malformed_json_failure_is_retryable():
    con = state.connect(":memory:")
    result = _run_with_extract(
        con, lambda title, text, model=None: ([], {}, "{not json", "unparseable JSON"))
    _assert_retryable_failure(con, result, "unparseable JSON")


def test_missing_claims_failure_is_retryable():
    con = state.connect(":memory:")
    result = _run_with_extract(
        con, lambda title, text, model=None: ([], {}, '{"items":[]}', "no claims array"))
    _assert_retryable_failure(con, result, "no claims array")


def test_transport_exception_is_retryable_and_nonfatal():
    con = state.connect(":memory:")

    def boom(title, text, model=None):
        raise TimeoutError("synthetic transport timeout")

    result = _run_with_extract(con, boom)
    _assert_retryable_failure(con, result, "synthetic transport timeout")


def test_clean_retry_after_failure_records_doc_seen():
    con = state.connect(":memory:")
    bad = _run_with_extract(
        con, lambda title, text, model=None: ([], {}, "bad", "unparseable JSON"))
    assert bad["failed"] == ["doc1"]
    good = _run_with_extract(con, lambda title, text, model=None: ([], {}, "{}", None))
    assert good["processed"] == ["doc1"]
    assert good["failed"] == []
    assert state.already_seen(con, DOC["content_hash"]) == "doc1"
    assert con.execute("SELECT COUNT(*) FROM doc_failures").fetchone()[0] == 1


def _fabricated_state(path, value):
    con = state.connect(path)
    run_id = state.start_run(con, "stub", "fabricated")
    state.finish_run(con, run_id, 1, 0, 0, 0, 0)
    con.execute(
        "INSERT INTO claims(run_id,doc_id,doc_title,doc_path,model_id,"
        "record_title,canon_field,value_disp,proposal_type,status,full_fp,"
        "partial_fp,created_run,quote) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, f"doc-{value}", f"Doc {value}", f"doc{value}.pdf", "40N6",
         "40N6 missile", "Maximum range", f"{value} km", "gap_fill",
         "surfaced", f"fp-{value}", f"pfp-{value}", run_id, "quoted text"))
    con.commit()
    return con, run_id


def test_non_default_reports_do_not_write_legacy_names():
    with tempfile.TemporaryDirectory() as td:
        db1 = os.path.join(td, "tmp_alpha.db")
        db2 = os.path.join(td, "tmp_beta.db")
        con1, run1 = _fabricated_state(db1, "alpha")
        con2, run2 = _fabricated_state(db2, "beta")
        path1, props1 = report.build(con1, run1, td, db_path=db1)
        path2, props2 = report.build(con2, run2, td, db_path=db2)
        assert os.path.basename(path1) == "report_run1_tmp_alpha.md"
        assert os.path.basename(path2) == "report_run1_tmp_beta.md"
        assert os.path.exists(os.path.join(td, "proposals_tmp_alpha.json"))
        assert os.path.exists(os.path.join(td, "proposals_tmp_beta.json"))
        assert props1 and props2
        assert not os.path.exists(os.path.join(td, "proposals.json"))
        assert not os.path.exists(os.path.join(td, "report_run1.md"))
        con1.close()
        con2.close()


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} extraction/report safety tests passed")


if __name__ == "__main__":
    main()
