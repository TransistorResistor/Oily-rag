#!/usr/bin/env python3
"""
state.py - the pipeline's own SQLite state (enrich_state.db). SEPARATE file from
rag_test.db, which is never opened. Holds:

  docs_seen  - processed documents, keyed by content hash (incremental reruns)
  claims     - the PRIMARY store: every validated/parked claim ever seen
  proposals  - the threshold VIEW, rematerialised each run from claims
  decisions  - the suppression ledger (rejected fingerprints don't resurface)
  runs       - one row per pipeline run (for LLM-call / token accounting)
"""

import os
import sqlite3
import time

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "enrich_state.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs_seen (
    doc_id       TEXT PRIMARY KEY,
    path         TEXT,
    title        TEXT,
    content_hash TEXT UNIQUE,
    doc_date     TEXT,
    first_run    INTEGER,
    llm_model    TEXT,
    n_claims     INTEGER
);
CREATE TABLE IF NOT EXISTS claims (
    claim_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER,
    doc_id       TEXT,
    doc_title    TEXT,
    doc_path     TEXT,
    model_id     TEXT,           -- linked reference record id (or NULL)
    record_title TEXT,
    entity_mention TEXT,
    attribute    TEXT,
    canon_field  TEXT,
    value_raw    TEXT,
    unit_raw     TEXT,
    value_norm   REAL,
    unit_norm    TEXT,
    value_disp   TEXT,           -- human display of the value
    qualifier    TEXT,
    quote        TEXT,
    proposal_type TEXT,          -- gap_fill | conflict | relation | NULL
    db_value     TEXT,           -- existing DB value(s) for conflicts
    status       TEXT,           -- surfaced | parked | rejected | dropped
    park_reason  TEXT,           -- unlinked|unmapped|incomplete|uncorroborated
    full_fp      TEXT,
    partial_fp   TEXT,
    created_run  INTEGER
);
CREATE TABLE IF NOT EXISTS decisions (
    full_fp      TEXT PRIMARY KEY,
    decision     TEXT,           -- reject
    reason       TEXT,
    proposal_id  TEXT,
    ts           REAL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL,
    docs         INTEGER,
    llm_calls    INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    model        TEXT,
    note         TEXT
);
"""


def connect(path=DEFAULT_DB):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def start_run(con, model, note=""):
    cur = con.execute(
        "INSERT INTO runs(ts,docs,llm_calls,prompt_tokens,completion_tokens,"
        "model,note) VALUES(?,?,?,?,?,?,?)",
        (time.time(), 0, 0, 0, 0, model, note))
    con.commit()
    return cur.lastrowid


def finish_run(con, run_id, docs, llm_calls, ptok, ctok):
    con.execute("UPDATE runs SET docs=?,llm_calls=?,prompt_tokens=?,"
                "completion_tokens=? WHERE run_id=?",
                (docs, llm_calls, ptok, ctok, run_id))
    con.commit()


def already_seen(con, content_hash):
    r = con.execute("SELECT doc_id FROM docs_seen WHERE content_hash=?",
                    (content_hash,)).fetchone()
    return r["doc_id"] if r else None


def record_doc(con, doc_id, path, title, content_hash, doc_date, run_id,
               model, n_claims):
    con.execute(
        "INSERT OR REPLACE INTO docs_seen(doc_id,path,title,content_hash,"
        "doc_date,first_run,llm_model,n_claims) VALUES(?,?,?,?,?,?,?,?)",
        (doc_id, path, title, content_hash, doc_date, run_id, model, n_claims))
    con.commit()


def insert_claim(con, c):
    cols = ("run_id,doc_id,doc_title,doc_path,model_id,record_title,"
            "entity_mention,attribute,canon_field,value_raw,unit_raw,value_norm,"
            "unit_norm,value_disp,qualifier,quote,proposal_type,db_value,status,"
            "park_reason,full_fp,partial_fp,created_run")
    keys = cols.split(",")
    con.execute(f"INSERT INTO claims({cols}) VALUES({','.join('?' * len(keys))})",
                tuple(c.get(k) for k in keys))


def is_rejected(con, full_fp):
    return con.execute("SELECT 1 FROM decisions WHERE full_fp=?",
                       (full_fp,)).fetchone() is not None


def reject_fp(con, full_fp, reason, proposal_id):
    con.execute("INSERT OR REPLACE INTO decisions(full_fp,decision,reason,"
                "proposal_id,ts) VALUES(?,?,?,?,?)",
                (full_fp, "reject", reason, proposal_id, time.time()))
    con.commit()
