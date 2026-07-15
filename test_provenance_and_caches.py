#!/usr/bin/env python3
"""Offline checks for embedding provenance and compare_server DB replacement."""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import ragkit  # noqa: E402


class _StubEmbedder:
    def __init__(self, dim):
        self.dim = dim

    def get_sentence_embedding_dimension(self):
        return self.dim


def _install_stub_embed(dims=None):
    old_embed = ragkit.embed
    old_get = ragkit.get_embedder
    dims = dict(dims or {})

    def dim_for(model_name):
        return dims.get(model_name, dims.get("*", 4))

    def embed(texts, model_name=ragkit.DEFAULT_EMBED_MODEL):
        dim = dim_for(model_name)
        rows = []
        for text in texts:
            seed = (sum(ord(ch) for ch in text) % 97) / 97.0
            rows.append([seed + (i * 0.1) for i in range(dim)])
        return np.asarray(rows, dtype=np.float32)

    ragkit.embed = embed
    ragkit.get_embedder = lambda model_name=ragkit.DEFAULT_EMBED_MODEL: (
        _StubEmbedder(dim_for(model_name)))
    return old_embed, old_get


def _restore_stub_embed(old):
    ragkit.embed, ragkit.get_embedder = old


def _reset_ragkit_warning_state():
    ragkit._embed_model_warned.clear()
    ragkit._embed_model_provenance_warned.clear()


def _source(td, names):
    src = os.path.join(td, "records_" + str(len(os.listdir(td))))
    os.mkdir(src)
    for name in names:
        shutil.copy(os.path.join(HERE, "test_records", name), src)
    return src


def _ingest_tiny(db, src, model="stub-a", dims=None):
    old = _install_stub_embed(dims or {"*": 4})
    try:
        ragkit.ingest(db, src, embed_model=model)
    finally:
        _restore_stub_embed(old)


def _retrieve_once(db, model="stub-a", allow=False, dims=None):
    old = _install_stub_embed(dims or {"*": 4})
    con = None
    try:
        con = ragkit.connect_readonly(db)
        return ragkit.retrieve(
            con, "range missile", k=1, embed_model=model, rerank=False,
            allow_embed_mismatch=allow)
    finally:
        if con is not None:
            con.close()
        _restore_stub_embed(old)


def _set_embed_meta(db, model, dim):
    con = sqlite3.connect(db)
    try:
        con.execute(
            "UPDATE meta SET value=? WHERE key='embed_model'",
            (json.dumps({"model": model, "dim": dim}),),
        )
        con.commit()
    finally:
        con.close()


def test_retrieve_errors_on_model_name_mismatch():
    _reset_ragkit_warning_state()
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "rag.db")
        _ingest_tiny(db, _source(td, ["40N6.json", "48N6M.json"]),
                     model="stub-a")
        try:
            _retrieve_once(db, model="stub-b")
            raise AssertionError("model mismatch did not fail")
        except ragkit.EmbedModelMismatchError as exc:
            msg = str(exc)
            assert "stub-a" in msg
            assert "stub-b" in msg
            assert "Re-ingest" in msg
            assert "--allow-embed-mismatch" in msg


def test_allow_embed_mismatch_warns_and_retrieves():
    _reset_ragkit_warning_state()
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "rag.db")
        _ingest_tiny(db, _source(td, ["40N6.json", "48N6M.json"]),
                     model="stub-a")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            hits = _retrieve_once(db, model="stub-b", allow=True)
        assert hits
        text = err.getvalue()
        assert "WARNING" in text
        assert "stub-a" in text
        assert "stub-b" in text


def test_dimension_mismatch_errors_even_with_override():
    _reset_ragkit_warning_state()
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "rag.db")
        _ingest_tiny(db, _source(td, ["40N6.json", "48N6M.json"]),
                     model="stub-a")
        _set_embed_meta(db, "stub-a", 5)
        try:
            _retrieve_once(db, model="stub-a", allow=True)
            raise AssertionError("dimension mismatch did not fail")
        except ragkit.EmbedModelMismatchError as exc:
            msg = str(exc)
            assert "dimension mismatch" in msg.lower()
            assert "dim 5" in msg
            assert "dim 4" in msg
            assert "--allow-embed-mismatch cannot override" in msg


def test_provenance_less_db_warns_but_retrieves():
    _reset_ragkit_warning_state()
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "rag.db")
        _ingest_tiny(db, _source(td, ["40N6.json", "48N6M.json"]),
                     model="stub-a")
        con = sqlite3.connect(db)
        try:
            con.execute("DELETE FROM meta WHERE key='embed_model'")
            con.commit()
        finally:
            con.close()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            hits = _retrieve_once(db, model="stub-b")
        assert hits
        text = err.getvalue()
        assert "no embed_model provenance" in text
        assert "cannot verify" in text
        assert "Re-ingest" in text


def test_compare_server_reopens_and_rebuilds_caches_on_db_replace():
    _reset_ragkit_warning_state()
    import compare_server  # noqa: E402

    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "rag.db")
        src1 = _source(td, ["40N6.json", "48N6M.json"])
        src2 = _source(td, ["30N6E_Flap_Lid.json"])
        _ingest_tiny(db, src1, model=ragkit.DEFAULT_EMBED_MODEL)

        old_con = getattr(compare_server._local, "con", None)
        if old_con is not None:
            old_con.close()
        compare_server._local.__dict__.clear()
        compare_server._catalogue_cache.clear()
        compare_server._aliases_cache.clear()
        compare_server.CONFIG["db"] = db

        con1 = None
        con2 = None
        try:
            con1 = compare_server.get_conn()
            cat1 = compare_server.get_catalogue(con1)
            aliases1 = compare_server.get_aliases(con1)
            count1 = con1.execute("SELECT COUNT(*) FROM record_params").fetchone()[0]
            assert count1 == 2
            assert cat1
            assert aliases1
            old_version = compare_server._local.con_version

            # Windows SQLite handles block os.replace(); keep the warmed stale
            # server state, but close the physical handle so the test can simulate
            # an externally replaced DB and then verify get_conn() refreshes it.
            con1.close()
            _ingest_tiny(db, src2, model=ragkit.DEFAULT_EMBED_MODEL)
            now = time.time() + 2.0
            os.utime(db, (now, now))

            con2 = compare_server.get_conn()
            cat2 = compare_server.get_catalogue(con2)
            aliases2 = compare_server.get_aliases(con2)
            count2 = con2.execute("SELECT COUNT(*) FROM record_params").fetchone()[0]
            assert compare_server._local.con_version != old_version
            assert count2 == 1
            assert con2 is not con1
            assert cat2
            assert aliases2
            assert cat2.get("systemType", {}).get("values") != (
                cat1.get("systemType", {}).get("values"))
        finally:
            for con in (con2, con1):
                if con is not None:
                    try:
                        con.close()
                    except Exception:
                        pass
            compare_server._local.__dict__.clear()


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} provenance/cache tests passed")


if __name__ == "__main__":
    main()
