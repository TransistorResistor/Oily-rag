#!/usr/bin/env python3
"""Offline regression checks for staged ingest and read-only DB validation."""

import glob
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ragkit  # noqa: E402


def _hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sentinel_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE records(rowid INTEGER PRIMARY KEY, rid TEXT)")
    con.execute("INSERT INTO records(rid) VALUES ('sentinel')")
    con.commit()
    con.close()


def _tiny_source(td):
    src = os.path.join(td, "records")
    os.mkdir(src)
    for name in ("40N6.json", "48N6M.json"):
        shutil.copy(os.path.join(HERE, "test_records", name), src)
    return src


class _StubEmbedder:
    def get_sentence_embedding_dimension(self):
        return 4


def _install_stub_embed():
    old_embed = ragkit.embed
    old_get = ragkit.get_embedder

    def embed(texts, model_name=ragkit.DEFAULT_EMBED_MODEL):
        rows = []
        for text in texts:
            seed = (sum(ord(ch) for ch in text) % 97) / 97.0
            rows.append([seed, seed + 0.1, seed + 0.2, seed + 0.3])
        return np.asarray(rows, dtype=np.float32)

    ragkit.embed = embed
    ragkit.get_embedder = lambda model_name=ragkit.DEFAULT_EMBED_MODEL: _StubEmbedder()
    return old_embed, old_get


def _restore_stub_embed(old):
    ragkit.embed, ragkit.get_embedder = old


def test_empty_or_missing_source_preserves_existing_db():
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "rag.db")
        _sentinel_db(db)
        before = _hash(db)
        missing = os.path.join(td, "missing")
        try:
            ragkit.ingest(db, missing)
            raise AssertionError("missing source did not fail")
        except ragkit.IngestError as exc:
            assert "Source not found" in str(exc)
        assert _hash(db) == before
        con = sqlite3.connect(db)
        assert con.execute("SELECT rid FROM records").fetchone()[0] == "sentinel"
        con.close()

        empty = os.path.join(td, "empty")
        os.mkdir(empty)
        try:
            ragkit.ingest(db, empty)
            raise AssertionError("empty source did not fail")
        except ragkit.IngestError as exc:
            assert "No records found" in str(exc)
        assert _hash(db) == before


def test_embedding_failure_preserves_existing_db_and_cleans_temp():
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "rag.db")
        src = _tiny_source(td)
        _sentinel_db(db)
        before = _hash(db)
        old = (ragkit.embed, ragkit.get_embedder)
        ragkit.embed = lambda texts, model_name=ragkit.DEFAULT_EMBED_MODEL: (
            (_ for _ in ()).throw(RuntimeError("synthetic embed failure")))
        ragkit.get_embedder = lambda model_name=ragkit.DEFAULT_EMBED_MODEL: _StubEmbedder()
        try:
            try:
                ragkit.ingest(db, src)
                raise AssertionError("embedding failure did not fail")
            except RuntimeError as exc:
                assert "synthetic embed failure" in str(exc)
        finally:
            _restore_stub_embed(old)
        assert _hash(db) == before
        assert not glob.glob(os.path.join(td, "rag.db.tmp-*"))


def test_happy_path_fresh_db_is_queryable():
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "rag.db")
        src = _tiny_source(td)
        old = _install_stub_embed()
        try:
            ragkit.ingest(db, src)
        finally:
            _restore_stub_embed(old)
        con = ragkit.connect_readonly(db)
        assert con.execute("SELECT COUNT(*) FROM records").fetchone()[0] > 0
        hit = con.execute(
            "SELECT COUNT(*) FROM records_fts WHERE records_fts MATCH 'range'"
        ).fetchone()[0]
        assert hit > 0
        con.close()


def test_readonly_connect_missing_db_is_clear_and_non_creating():
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "typo.db")
        try:
            ragkit.connect_readonly(db)
            raise AssertionError("missing db did not fail")
        except FileNotFoundError as exc:
            assert "DB not found or not ingested" in str(exc)
        assert not os.path.exists(db)

        real = os.path.join(td, "rag.db")
        src = _tiny_source(td)
        old = _install_stub_embed()
        try:
            ragkit.ingest(real, src)
        finally:
            _restore_stub_embed(old)
        con = ragkit.connect_readonly(real)
        assert con.execute("SELECT COUNT(*) FROM record_params").fetchone()[0] == 2
        con.close()


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} data-safety tests passed")


if __name__ == "__main__":
    main()
