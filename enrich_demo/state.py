#!/usr/bin/env python3
"""
state.py - the pipeline's own SQLite state (enrich_state.db). SEPARATE file from
rag_test.db, which is never opened. Holds:

  docs_seen  - processed documents, keyed by content hash (incremental reruns)
  claims     - the PRIMARY store: every validated/parked claim ever seen
  doc_failures - append-only claim-extraction failures, retryable
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
    doc_id       TEXT,
    path         TEXT,
    title        TEXT,
    content_hash TEXT PRIMARY KEY,
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
    raw_claim_json TEXT,
    mapping_status TEXT,
    mapping_method TEXT,
    mapping_score REAL,
    mapping_tier TEXT,
    mapping_candidate TEXT,
    runner_up TEXT,
    runner_up_score REAL,
    mapping_evidence TEXT,
    mapper_version TEXT,
    alias_version INTEGER,
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
CREATE TABLE IF NOT EXISTS doc_failures (
    failure_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       TEXT,
    path         TEXT,
    title        TEXT,
    content_hash TEXT,
    run_id       INTEGER,
    error        TEXT,
    raw_snippet  TEXT,
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
    note         TEXT,
    field_mapper TEXT NOT NULL DEFAULT 'legacy',
    text_fields  INTEGER NOT NULL DEFAULT 0,
    alias_version INTEGER NOT NULL DEFAULT 0,
    error_count  INTEGER NOT NULL DEFAULT 0
);
"""


def _migrate(con):
    """Apply the small backward-compatible schema changes used here."""
    docs_info = con.execute("PRAGMA table_info(docs_seen)").fetchall()
    pk = next((row[1] for row in docs_info if row[5]), None)
    if docs_info and pk != "content_hash":
        con.execute("ALTER TABLE docs_seen RENAME TO docs_seen_old")
        con.execute("""
            CREATE TABLE docs_seen (
                doc_id TEXT, path TEXT, title TEXT,
                content_hash TEXT PRIMARY KEY, doc_date TEXT,
                first_run INTEGER, llm_model TEXT, n_claims INTEGER
            )
        """)
        con.execute("""
            INSERT OR IGNORE INTO docs_seen
                (doc_id,path,title,content_hash,doc_date,first_run,llm_model,n_claims)
            SELECT doc_id,path,title,content_hash,doc_date,first_run,llm_model,n_claims
            FROM docs_seen_old
        """)
        con.execute("DROP TABLE docs_seen_old")
    run_cols = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
    if "error_count" not in run_cols:
        con.execute("ALTER TABLE runs ADD COLUMN error_count INTEGER NOT NULL DEFAULT 0")
    for col, ddl in (
            ("field_mapper", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("text_fields", "INTEGER NOT NULL DEFAULT 0"),
            ("alias_version", "INTEGER NOT NULL DEFAULT 0")):
        if col not in run_cols:
            con.execute(f"ALTER TABLE runs ADD COLUMN {col} {ddl}")
    claim_cols = {row[1] for row in con.execute("PRAGMA table_info(claims)")}
    for col, ddl in (
            ("raw_claim_json", "TEXT"),
            ("mapping_status", "TEXT"),
            ("mapping_method", "TEXT"),
            ("mapping_score", "REAL"),
            ("mapping_tier", "TEXT"),
            ("mapping_candidate", "TEXT"),
            ("runner_up", "TEXT"),
            ("runner_up_score", "REAL"),
            ("mapping_evidence", "TEXT"),
            ("mapper_version", "TEXT"),
            ("alias_version", "INTEGER")):
        if col not in claim_cols:
            con.execute(f"ALTER TABLE claims ADD COLUMN {col} {ddl}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_claims_partial_status "
                "ON claims(partial_fp,status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_claims_record_field "
                "ON claims(model_id,canon_field,status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_claims_mapping "
                "ON claims(mapping_tier,mapping_status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_failures_hash "
                "ON doc_failures(content_hash)")
    con.commit()


def connect(path=DEFAULT_DB):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def start_run(con, model, note="", field_mapper="legacy", text_fields=False,
              alias_version=0):
    cur = con.execute(
        "INSERT INTO runs(ts,docs,llm_calls,prompt_tokens,completion_tokens,"
        "model,note,field_mapper,text_fields,alias_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (time.time(), 0, 0, 0, 0, model, note, field_mapper,
         int(bool(text_fields)), int(alias_version or 0)))
    con.commit()
    return cur.lastrowid


def finish_run(con, run_id, docs, llm_calls, ptok, ctok, error_count=0):
    con.execute("UPDATE runs SET docs=?,llm_calls=?,prompt_tokens=?,"
                "completion_tokens=?,error_count=? WHERE run_id=?",
                (docs, llm_calls, ptok, ctok, error_count, run_id))
    con.commit()


def record_failure(con, doc_id, path, title, content_hash, run_id, error,
                   raw_response=None):
    raw = "" if raw_response is None else str(raw_response)
    con.execute(
        "INSERT INTO doc_failures(doc_id,path,title,content_hash,run_id,error,"
        "raw_snippet,ts) VALUES(?,?,?,?,?,?,?,?)",
        (doc_id, path, title, content_hash, run_id, str(error), raw[:1000],
         time.time()))
    con.commit()


def already_seen(con, content_hash):
    r = con.execute("SELECT doc_id FROM docs_seen WHERE content_hash=?",
                    (content_hash,)).fetchone()
    return r["doc_id"] if r else None


def record_doc(con, doc_id, path, title, content_hash, doc_date, run_id,
               model, n_claims):
    con.execute(
        "INSERT INTO docs_seen(doc_id,path,title,content_hash,doc_date,first_run,"
        "llm_model,n_claims) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(content_hash) DO UPDATE SET doc_id=excluded.doc_id, "
        "path=excluded.path,title=excluded.title,doc_date=excluded.doc_date, "
        "llm_model=excluded.llm_model,n_claims=excluded.n_claims",
        (doc_id, path, title, content_hash, doc_date, run_id, model, n_claims))
    con.commit()


def insert_claim(con, c):
    cols = ("run_id,doc_id,doc_title,doc_path,model_id,record_title,"
            "entity_mention,attribute,canon_field,value_raw,unit_raw,value_norm,"
            "unit_norm,value_disp,qualifier,quote,raw_claim_json,mapping_status,"
            "mapping_method,mapping_score,mapping_tier,mapping_candidate,runner_up,"
            "runner_up_score,mapping_evidence,mapper_version,alias_version,"
            "proposal_type,db_value,status,park_reason,full_fp,partial_fp,created_run")
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
