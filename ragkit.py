#!/usr/bin/env python3
"""
ragkit.py - single-file RAG prototype.

Ingests JSON record files (mix of free text + parametric values with definitions),
builds a local SQLite index (FTS5 keyword + MiniLM vector embeddings), and answers
RAG queries using either a local HuggingFace transformers model or a web LLM
(Anthropic / OpenAI-compatible endpoint).

Embedder defaults to sentence-transformers/all-MiniLM-L6-v2 to match Clipper, so the
384-dim vector space is identical and embeddings are interchangeable between systems.

Usage
-----
  pip install sentence-transformers numpy            # always
  pip install transformers torch accelerate          # only for --backend local
  # web backend needs no extra deps (uses urllib)

  # 1. Ingest a directory of .json record files
  python ragkit.py ingest ./records --db rag.db

  # 2. Ask a question (CLI), local model
  python ragkit.py ask "What is the max operating temperature of unit X?" \
      --db rag.db --backend local --model Qwen/Qwen2.5-1.5B-Instruct

  # 2b. Ask via web LLM (Anthropic). Set ANTHROPIC_API_KEY in env.
  python ragkit.py ask "..." --db rag.db --backend anthropic

  # 2c. Ask via OpenAI-compatible endpoint (e.g. your local vLLM server)
  python ragkit.py ask "..." --db rag.db --backend openai \
      --base-url http://localhost:8000/v1 --model Qwen/Qwen2.5-14B-Instruct

  # 2d. Ask via OpenRouter, to trial a more capable model (Gemma 4, Mistral,
  # Qwen, etc.) without standing up local infra. Set OPENROUTER_API_KEY in env
  # (get one at https://openrouter.ai/keys). --model accepts a short alias
  # (gemma4, gemma4-31b, mistral-small, mistral-medium, qwen2.5-72b, qwen3-30b)
  # or any full OpenRouter model slug, e.g. "google/gemma-4-31b-it".
  python ragkit.py ask "..." --db rag.db --backend openrouter --model gemma4
  python ragkit.py ask "..." --db rag.db --backend openrouter --model mistral-small
  python ragkit.py ask "..." --db rag.db --backend openrouter   # uses default model

  # 3. Web interface: the side-by-side model-comparison bench (OpenRouter).
  # Same UI as `python compare_server.py`; preloads the embedder and opens the
  # browser. Needs OPENROUTER_API_KEY to run models (context preview works
  # without one). Use the .ps1 launchers to set env + key automatically.
  python ragkit.py serve --db rag_test.db      # opens http://localhost:8099

Record JSON format (flexible)
-----------------------------
Each .json file is one record OR a list of records. A record is a dict.
Two record shapes are recognised, and can be mixed within the same corpus:

  1) ragkit-native shape:

  {
    "id": "PUMP-204",                       # optional; falls back to filename + index
    "title": "Centrifugal Pump 204",        # optional
    "text": "Free-text description ...",     # any free-text fields are concatenated
    "notes": "More prose ...",
    "parameters": {                          # parametric values, optionally with defs
        "max_temp": {"value": 180, "unit": "C", "definition": "Max sustained casing temp"},
        "flow_rate": {"value": 50, "unit": "L/min"},
        "material": "316 stainless"          # bare value also fine
    }
  }

  2) pages_schema / schema-example.json shape:

  {
    "modelID": 2001, "nomenclature": "AIM-120 AMRAAM",
    "systemGroup": "Weapon", "systemType": "Air-to-Air Missile",
    "descriptions": [                        # prose, keyed by descrType
        {"descrType": "Overview", "description": "...", "shortDescription": "..."},
        {"descrType": "History", "description": "..."}, ...
    ],
    "parametrics": [                         # structured facts, one row per parameter
        {"parameter": "Length", "parameterValue": "3.65", "uom": "m",
         "parameterDescr": "Overall length of the system."}, ...
    ],
    "media": [{"url": "...", "title": "..."}]
  }

`nomenclature` doubles as the title; `modelID` doubles as the id. Both
`parameters` (dict) and `parametrics` (list) feed the catalogue (see
catalogue.py) and the embedded/searchable text; `descriptions` feeds only
the searchable text (prose isn't filter-worthy -- that's what semantic
search is for). Anything that isn't a recognised structural key is treated
as additional text.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import struct
import sys
import urllib.request
import urllib.error

# Safe defaults for running the embedder in-process (e.g. `serve`/the bench)
# without the .ps1 launchers. torch and numpy each bundle their own OpenMP
# runtime, which segfaults intermittently on Windows/conda unless the first is
# set; the others just make embedding quieter/deterministic. setdefault means an
# explicit value from _env.ps1 (or the shell) still wins.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np

import catalogue as cat_mod
import record_model
import units as units_mod

DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # matches Clipper
EMBED_DIM = 384
# Cross-encoder used to rerank the top retrieval candidates. Unlike the
# bi-encoder embedder (query and passage encoded separately, then cosine), a
# cross-encoder scores each (query, passage) pair jointly, which is markedly
# more precise at picking the passages actually about the question.
DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# --------------------------------------------------------------------------- #
# Embedding                                                                    #
# --------------------------------------------------------------------------- #

_embedders = {}


def get_embedder(model_name=DEFAULT_EMBED_MODEL):
    """Lazy-load the sentence-transformer, cached per model name so it loads
    once per process (the cold load is ~18s / ~350MB) and a request for a
    different model doesn't silently return the first one. Same family as Clipper."""
    emb = _embedders.get(model_name)
    if emb is None:
        from sentence_transformers import SentenceTransformer
        emb = SentenceTransformer(model_name)
        _embedders[model_name] = emb
    return emb


_rerankers = {}


def get_reranker(model_name=DEFAULT_RERANK_MODEL):
    """Lazy-load the cross-encoder reranker, cached per model name (like the
    embedder). Loaded once per process; preloaded at server boot by warm_start."""
    rk = _rerankers.get(model_name)
    if rk is None:
        from sentence_transformers import CrossEncoder
        rk = CrossEncoder(model_name)
        _rerankers[model_name] = rk
    return rk


def embed(texts, model_name=DEFAULT_EMBED_MODEL):
    vecs = get_embedder(model_name).encode(
        texts, normalize_embeddings=True, show_progress_bar=False
    )
    return np.asarray(vecs, dtype=np.float32)


def pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob):
    return np.array(struct.unpack(f"{len(blob)//4}f", blob), dtype=np.float32)


# --------------------------------------------------------------------------- #
# Record loading (canonical model)                                            #
# --------------------------------------------------------------------------- #
#
# flatten_record() and extract_parametrics() used to live here, each
# independently re-parsing the two record shapes with subtly different
# coercion rules than catalogue.extract_fields (REVIEW_FINDINGS C1: three
# parsers, three chances to diverge). record_model.py now parses a raw record
# into the canonical model ONCE (see load_records below); record_model.to_text
# and record_model.rich_params are the thin views that replace them.

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text):
    return [s for s in _SENT_SPLIT.split(text.strip()) if s]


def chunk_record(title, text, target_chars=900, overlap_sentences=1):
    """Split a flattened record into self-contained passages for retrieval.

    Whole Wikipedia-scale records are too large to embed or retrieve as one unit:
    a single vector has to represent a multi-topic page, and a hit dumps the
    entire page into the prompt. So we split into passages instead --

      - prose is packed sentence-wise up to ~target_chars, carrying a small
        overlap (the last `overlap_sentences`) across boundaries so a fact that
        straddles a split stays retrievable in both neighbours;
      - the Parameter lines (structured, high-value, tiny) are kept together as
        their own passage so parametric questions hit them cleanly.

    Each passage is prefixed with the title so it stands alone in a prompt.
    Returns a list of passage strings (always at least one)."""
    lines = text.split("\n")
    param_lines = [l for l in lines if l.startswith("Parameter ")]
    prose_lines = [l for l in lines
                   if l and not l.startswith("Parameter ")
                   and not l.startswith("Title:")]
    header = f"Title: {title}\n" if title else ""

    chunks = []

    # --- prose: greedy sentence packing with a small overlap ---
    sents = []
    for seg in prose_lines:
        sents.extend(_split_sentences(seg))
    cur, cur_len = [], 0
    for s in sents:
        if cur and cur_len + len(s) + 1 > target_chars:
            chunks.append(header + " ".join(cur))
            cur = cur[-overlap_sentences:] if overlap_sentences else []
            cur_len = sum(len(x) + 1 for x in cur)
        cur.append(s)
        cur_len += len(s) + 1
    if cur:
        chunks.append(header + " ".join(cur))

    # --- parameters: their own passage(s), split only if very long ---
    if param_lines:
        pcur, plen = [], 0
        for pl in param_lines:
            if pcur and plen + len(pl) > int(target_chars * 1.5):
                chunks.append(header + "\n".join(pcur))
                pcur, plen = [], 0
            pcur.append(pl)
            plen += len(pl) + 1
        if pcur:
            chunks.append(header + "\n".join(pcur))

    chunks = [c.strip() for c in chunks if c.strip()]
    return chunks or [(header + text).strip()]


def load_records(path):
    """Yield (record_id, title, text, raw_record, canon_record) from a file or
    directory. Each raw record is normalized into the canonical model (see
    record_model.py) EXACTLY ONCE here -- every downstream consumer (embedding
    text, rich params, typed fields) derives its view from this ONE `canon`
    instead of re-parsing the raw JSON with its own coercion rules (the
    three-parsers hazard record_model fixes; REVIEW_FINDINGS C1). Both `raw`
    and `canon` are yielded: catalogue.build_catalogue still wants raw records
    (it re-normalizes per-record itself -- see catalogue.py), everything else
    in ingest() uses `canon`."""
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.json")))
    else:
        files = [path]

    for fp in files:
        with open(fp, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        records = data if isinstance(data, list) else [data]
        base = os.path.splitext(os.path.basename(fp))[0]
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                continue
            rid = str(rec.get("id") or rec.get("modelID") or f"{base}#{i}")
            title = rec.get("title") or rec.get("nomenclature") or rid
            canon = record_model.normalize_record(rec)
            yield rid, title, record_model.to_text(canon), rec, canon


# --------------------------------------------------------------------------- #
# DB build                                                                     #
# --------------------------------------------------------------------------- #

def connect(db_path):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL;")
    return con


def init_db(con):
    con.execute(
        """CREATE TABLE IF NOT EXISTS records (
               rowid INTEGER PRIMARY KEY,
               rid TEXT UNIQUE,        -- passage id, e.g. AIM-120_AMRAAM#0/2
               parent_rid TEXT,        -- source record id, for grouping + citation
               title TEXT,
               text TEXT,              -- one passage of the record, not the whole thing
               embedding BLOB
           )"""
    )
    # Migration for indexes built before passages: add parent_rid if missing.
    cols = [r[1] for r in con.execute("PRAGMA table_info(records)").fetchall()]
    if "parent_rid" not in cols:
        con.execute("ALTER TABLE records ADD COLUMN parent_rid TEXT")
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts "
        "USING fts5(text, content='records', content_rowid='rowid')"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    # One row per source record holding BOTH its full rich parametric fields
    # (value + unit + description, for the structured-table view) AND its typed
    # filter-fields dict. Typed fields used to be duplicated onto every PASSAGE
    # row (~45x more copies than records for this corpus -- REVIEW_FINDINGS B2);
    # filtering is record-level anyway, so they now live ONCE here, keyed by
    # parent_rid. See _record_fields_map / _ensure_current_schema below for the
    # read side.
    con.execute(
        "CREATE TABLE IF NOT EXISTS record_params ("
        "  parent_rid TEXT PRIMARY KEY, title TEXT, params_json TEXT, "
        "  fields_json TEXT)"
    )
    con.commit()


def ingest(db_path, src, embed_model=DEFAULT_EMBED_MODEL):
    con = connect(db_path)
    # A record now expands into several passage rows, so rebuild cleanly rather
    # than upserting by rid (old whole-doc rows would otherwise linger). This
    # also means an OLD per-passage-`fields` db is always fully replaced by a
    # re-ingest -- see _ensure_current_schema for what happens if one is instead
    # opened directly for querying without re-ingesting.
    con.execute("DROP TABLE IF EXISTS records_fts")
    con.execute("DROP TABLE IF EXISTS records")
    con.execute("DROP TABLE IF EXISTS record_params")
    init_db(con)

    records = list(load_records(src))
    if not records:
        print("No records found.", file=sys.stderr)
        return
    raws = [r for _, _, _, r, _ in records]

    # Expand every record into passages, remembering which parent each came from.
    passages = []  # (passage_rid, parent_rid, title, passage_text)
    for parent_rid, title, text, raw, canon in records:
        typed, _units = record_model.typed_fields(canon)
        for j, chunk in enumerate(chunk_record(title, text)):
            passages.append((f"{parent_rid}/{j}", parent_rid, title, chunk))
        # Store the rich parametrics (value + unit + description, for the
        # structured-table view) AND the typed filter fields -- one row per
        # source record, both derived from the SAME canonical record so they
        # can't drift relative to each other (REVIEW_FINDINGS C1).
        con.execute(
            "INSERT OR REPLACE INTO record_params "
            "(parent_rid, title, params_json, fields_json) VALUES (?,?,?,?)",
            (parent_rid, title, json.dumps(record_model.rich_params(canon)),
             json.dumps(typed)),
        )

    print(f"Embedding {len(passages)} passages from {len(records)} records "
          f"with {embed_model} ...", file=sys.stderr)
    vecs = embed([p[3] for p in passages], embed_model)

    for (prid, parent_rid, title, chunk), vec in zip(passages, vecs):
        cur = con.execute(
            "INSERT INTO records (rid, parent_rid, title, text, embedding) "
            "VALUES (?,?,?,?,?)",
            (prid, parent_rid, title, chunk, pack(vec)),
        )
        con.execute("INSERT INTO records_fts(rowid, text) VALUES (?,?)",
                    (cur.lastrowid, chunk))

    # Build + persist the filter catalogue (the model's "filter vocabulary").
    # `dropped` captures every JSON structure that couldn't be indexed as a filter
    # field, so a new/variant input shape (new top-level list-of-objects, an
    # unrecognised param-list shape, an untypable field) is LOUD at import time
    # rather than silently missing from filtering later.
    dropped = {}
    catalogue = cat_mod.build_catalogue(raws, dropped=dropped)
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('catalogue', ?)",
        (json.dumps(catalogue),),
    )
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('import_dropped', ?)",
        (json.dumps(dropped),),
    )

    # Entity alias table (G1, ingest half): alias (full title / designation like
    # "F-16" / popular name / meaningful title word) -> [parent_rid, ...], for a
    # later retrieval phase to deterministically pin named-entity queries instead
    # of relying solely on embeddings (weak at exact designations). Built once
    # here and stored so it isn't re-derived per query.
    aliases = record_model.build_alias_table(
        [(parent_rid, title) for parent_rid, title, _text, _raw, _canon in records])
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('aliases', ?)",
        (json.dumps(aliases),),
    )
    print(f"alias table: {len(aliases)} aliases for {len(records)} records",
          file=sys.stderr)

    con.commit()
    n = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    print(f"Done. {len(records)} records -> {n} passages indexed in {db_path}.",
          file=sys.stderr)
    print(f"Catalogue: {len(catalogue)} fields classified.", file=sys.stderr)
    print(import_report(dropped, len(records)), file=sys.stderr)


def import_report(dropped, n_records):
    """Render the import-diagnostics summary: which JSON structures were NOT indexed
    as filter fields (or were indexed with a caveat, e.g. a cross-record unit
    conflict), so blind spots in a new/variant schema surface immediately.
    `dropped` is the dict populated by catalogue.build_catalogue."""
    if not dropped:
        return "Import OK: every field was indexed as a filter dimension or prose."
    lines = [f"! Import diagnostics: {len(dropped)} field/structure(s) NOT fully "
             f"indexed as filterable (searchable as prose only, indexed with a "
             f"caveat, or ignored):"]
    for path, info in sorted(dropped.items(), key=lambda kv: -kv[1]["count"]):
        ex = (f"  e.g. keys {info['example_keys']}"
              if info.get("example_keys") else "")
        lines.append(f"    - {path}: {info['reason']} "
                     f"[in {info['count']}/{n_records} record(s)]{ex}")
    lines.append("  To index one of these for filtering, add an adapter in "
                 "record_model.py (see the 'list of objects' branch in "
                 "_absorb_extras).")
    return "\n".join(lines)


def load_catalogue(con):
    row = con.execute("SELECT value FROM meta WHERE key='catalogue'").fetchone()
    return json.loads(row[0]) if row else {}


def load_aliases(con):
    """The {alias_lowercase: [parent_rid, ...]} table built at ingest time (see
    record_model.build_alias_table). {} for a db ingested before G1/this phase."""
    row = con.execute("SELECT value FROM meta WHERE key='aliases'").fetchone()
    return json.loads(row[0]) if row else {}


def _ensure_current_schema(con):
    """Guard against querying a db built before the fields-de-duplication change
    (REVIEW_FINDINGS B2): typed filter fields used to live in a per-passage
    `fields` column on `records`; they now live ONCE per record, in
    `record_params.fields_json`. Reading an old db through the new code without
    this check would surface as a confusing 'no such column: fields_json'
    OperationalError deep inside a filter helper -- fail fast here instead, with
    a message that says exactly what to do. A never-ingested (empty) db has no
    record_params table at all yet, which is fine -- nothing to guard against."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(record_params)").fetchall()}
    if cols and "fields_json" not in cols:
        raise RuntimeError(
            "this db was built before the fields-storage change (typed filter "
            "fields now live once per record, not duplicated per passage); "
            "re-ingest required (schema changed) -- run: "
            "python ragkit.py ingest <src> --db <this db>"
        )


def _record_fields_map(con):
    """{parent_rid: (title, fields_dict)} for every ingested record's typed
    filter fields, read ONCE per record from record_params (REVIEW_FINDINGS B2)
    instead of rescanning a copy duplicated onto every passage row. Shared by
    _eligible_rowids/count_matches/field_coverage/record_table."""
    _ensure_current_schema(con)
    rows = con.execute(
        "SELECT parent_rid, title, fields_json FROM record_params").fetchall()
    return {parent_rid: (title, json.loads(fields_json) if fields_json else {})
            for parent_rid, title, fields_json in rows}


# --------------------------------------------------------------------------- #
# Retrieval (hybrid: vector + FTS5, fused with RRF)                            #
# --------------------------------------------------------------------------- #

def fts_query_string(q):
    # FTS5 MATCH wants bare terms OR'd; strip punctuation, drop empties
    terms = re.findall(r"[A-Za-z0-9_]+", q)
    return " OR ".join(terms) if terms else q


# --------------------------------------------------------------------------- #
# Metadata filtering (validated against the catalogue)                         #
# --------------------------------------------------------------------------- #

def validate_filter(flt, catalogue):
    """Validate a filter dict against the catalogue. Returns (clean_filter, errors).

    Filter shape (all keys optional):
      {
        "<numeric_field>": {"min": <num>, "max": <num>},
        "<categorical_field>": {"in": [<label>, ...]} or {"eq": <label>},
        "<date_field>": {"date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"}
      }
    Anything not grounded in the catalogue is dropped with an error noted, so a
    model-emitted filter can never reference unknown fields or invalid labels.
    """
    clean, errors = {}, []
    if not isinstance(flt, dict):
        return {}, ["filter is not an object"]
    for field, cond in flt.items():
        spec = catalogue.get(field)
        if spec is None:
            errors.append(f"unknown field '{field}' dropped")
            continue
        if not isinstance(cond, dict):
            # The filter-extraction model is asked for {"field": {"in": [...]}}
            # etc., but sometimes emits a bare value instead (e.g.
            # {"Platform": ["F-22 Raptor"]} instead of {"Platform": {"in":
            # [...]}}) -- every branch below assumes cond.get()/cond[...],
            # so guard here rather than crashing on untrusted model output.
            errors.append(f"{field}: filter condition is not an object, dropped")
            continue
        t = spec["type"]
        if t == "numeric":
            canonical_unit = spec.get("unit")
            filter_unit = cond.get("unit")
            # Parse the raw min/max bounds first.
            c = {}
            for bound in ("min", "max"):
                if bound in cond:
                    try:
                        c[bound] = float(cond[bound])
                    except (TypeError, ValueError):
                        errors.append(f"{field}.{bound} not numeric, dropped")
            if not c:
                continue
            # If the filter carries a unit different from the field's canonical
            # unit, convert the bounds into the canonical unit before comparing.
            # The stored data stays in canonical units; only the filter moves.
            converted = None
            if filter_unit and canonical_unit:
                # Only convert (and record it) when the units actually differ
                # after normalization; same-unit is a silent no-op.
                same = (units_mod.normalize_unit(filter_unit)
                        == units_mod.normalize_unit(canonical_unit))
                if not same:
                    try:
                        for bound in ("min", "max"):
                            if bound in c:
                                c[bound] = units_mod.convert(
                                    c[bound], filter_unit, canonical_unit)
                        converted = {"from": filter_unit, "to": canonical_unit}
                    except units_mod.ConversionError as e:
                        # dimensional mismatch / unconvertible / pint missing:
                        # drop the whole filter for this field rather than compare
                        # mismatched magnitudes and return wrong records.
                        errors.append(f"{field}: {e}; filter dropped")
                        continue
            entry = {"type": "numeric", **c}
            if converted:
                entry["_converted"] = converted  # audit trail
            clean[field] = entry
        elif t == "categorical":
            allowed = set(spec["values"])
            wanted = cond.get("in") or ([cond["eq"]] if "eq" in cond else [])
            valid = [v for v in wanted if v in allowed]
            invalid = [v for v in wanted if v not in allowed]
            for v in invalid:
                errors.append(f"{field}='{v}' not a known label, dropped")
            if valid:
                clean[field] = {"type": "categorical", "in": valid}
        elif t == "multi_value":
            allowed = set(spec["values"])
            wanted = cond.get("contains") or cond.get("in") or (
                [cond["eq"]] if "eq" in cond else [])
            valid = [v for v in wanted if v in allowed]
            invalid = [v for v in wanted if v not in allowed]
            for v in invalid:
                errors.append(f"{field} contains '{v}' not a known element, dropped")
            if valid:
                clean[field] = {"type": "multi_value", "contains": valid}
        elif t == "date":
            c = {}
            for bound in ("date_from", "date_to"):
                if bound in cond and cat_mod._is_date(cond[bound]):
                    c[bound] = cond[bound]
                elif bound in cond:
                    errors.append(f"{field}.{bound} not a valid date, dropped")
            if c:
                clean[field] = {"type": "date", **c}
        else:
            errors.append(f"field '{field}' is {t}, not value-filterable; dropped")
    return clean, errors


def _eligible_parents(con, clean_filter):
    """Which parent (source-record) rids pass the validated filter, reading each
    record's typed fields ONCE from record_params (REVIEW_FINDINGS B2) instead of
    rescanning a copy duplicated onto every passage row. None if no filter (all
    records eligible) -- same "no filter" contract _eligible_rowids has for rowids."""
    if not clean_filter:
        return None
    fields_map = _record_fields_map(con)
    return {parent_rid for parent_rid, (_title, fields) in fields_map.items()
            if _passes(fields, clean_filter)}


def _eligible_rowids(con, clean_filter):
    """Passage-ROWID view of _eligible_parents, for retrieve()/record_digest's
    restrict/eligible set -- retrieval operates over passage embeddings/FTS rows,
    so that set must stay passage rowids even though eligibility itself is now a
    per-record (not per-passage) computation. Returns None if no filter."""
    eligible_parents = _eligible_parents(con, clean_filter)
    if eligible_parents is None:
        return None
    rows = con.execute("SELECT rowid, parent_rid, rid FROM records").fetchall()
    return {rowid for rowid, parent_rid, rid in rows
            if (parent_rid or rid) in eligible_parents}


def count_matches(con, clean_filter):
    """How many distinct source records pass the filter — not passages, and not
    capped by k. Lets callers report 'filter matched N records, showing top k'
    so a large filtered set isn't silently reduced to k with no signal."""
    if not clean_filter:
        return None
    return len(_eligible_parents(con, clean_filter) or set())


def field_coverage(con, clean_filter=None):
    """How many distinct source records carry a non-empty value for each stored
    field, optionally restricted to records passing `clean_filter`.

    Coverage is a proxy for how useful a field is as a filter dimension over the
    (sub)corpus. Globally it drives coverage-ordered pruning of the filter spec;
    restricted to a category (e.g. clean_filter = {systemGroup: Aircraft}) it tells
    the two-pass extractor which detail fields actually have data in that slice, so
    ship-only or gun-only parameters aren't offered for an aircraft question."""
    fields_map = _record_fields_map(con)
    eligible_parents = _eligible_parents(con, clean_filter) if clean_filter else None
    seen = {}   # field -> set(parent_rid)
    for parent_rid, (_title, fields) in fields_map.items():
        if eligible_parents is not None and parent_rid not in eligible_parents:
            continue
        for f, v in fields.items():
            if v is None or v == "" or v == []:
                continue
            seen.setdefault(f, set()).add(parent_rid)
    return {f: len(ps) for f, ps in seen.items()}


def extract_filter_2pass(query, catalogue, con, backend, model=None, base_url=None,
                         min_count=1, detail_min_count=2, max_detail_fields=40):
    """Two-pass filter extraction, for large catalogues where a single flat field
    list bloats the prompt and hurts field-selection accuracy:

      Pass 1 (categorise): show ONLY the broad partition fields (systemGroup,
              systemType, country, ...) and ask which categories the query is about.
      Pass 2 (detail): narrow the corpus to the pass-1 categories, recompute which
              detail fields actually carry data there (coverage), and present ONLY
              those (highest coverage first) for the full filter.

    Because detail fields are often category-specific (a tank has armour/calibre, a
    ship has displacement, a plane has service ceiling), narrowing by category first
    prunes far more than global coverage alone can.

    Returns (raw_filter, info): the merged *unvalidated* filter dict (same contract
    as extract_filter) and a small diagnostics dict. Costs two LLM round-trips."""
    info = {"two_pass": True}
    part = cat_mod.partition_fields(catalogue, min_count=min_count)
    info["pass1_fields"] = len(part)

    # ---- pass 1: which broad category / categories? ----
    cat_raw = {}
    if part:
        cat_raw = extract_filter(query, catalogue, backend, model, base_url,
                                 only_fields=set(part))
    cat_clean, _ = validate_filter(cat_raw, catalogue)
    info["pass1_filter"] = cat_clean

    # ---- narrow the corpus, recompute detail-field coverage within it ----
    cov = field_coverage(con, cat_clean or None)
    detail = {f for f, c in cov.items() if c >= detail_min_count}
    detail |= set(part)   # keep the partition fields available to refine in pass 2
    if max_detail_fields and len(detail) > max_detail_fields:
        detail = set(sorted(detail, key=lambda f: cov.get(f, 0),
                            reverse=True)[:max_detail_fields])
    info["pass2_fields"] = len(detail)

    # ---- pass 2: full filter over the narrowed, in-category field set ----
    det_raw = extract_filter(query, catalogue, backend, model, base_url,
                             only_fields=detail, min_count=min_count)
    # merge: pass-2 wins on conflicts; keep any pass-1 category it didn't repeat
    merged = {**cat_raw, **det_raw}
    return merged, info


def _passes(fields, clean_filter):
    for field, cond in clean_filter.items():
        val = fields.get(field)
        if val is None:
            return False
        t = cond["type"]
        if t == "numeric":
            try:
                v = float(val)
            except (TypeError, ValueError):
                return False
            if "min" in cond and v < cond["min"]:
                return False
            if "max" in cond and v > cond["max"]:
                return False
        elif t == "categorical":
            if val not in cond["in"]:
                return False
        elif t == "multi_value":
            # val is a list; pass if it contains ANY of the requested elements
            have = set(val) if isinstance(val, list) else {val}
            if not have & set(cond["contains"]):
                return False
        elif t == "date":
            d = cat_mod._parse_date(str(val))
            if d is None:
                return False
            if "date_from" in cond and d < cat_mod._parse_date(cond["date_from"]):
                return False
            if "date_to" in cond and d > cat_mod._parse_date(cond["date_to"]):
                return False
    return True


def _normalize_scores(d):
    """Min-max a {id: score} dict into [0,1] so heterogeneous signals (RRF sums,
    cross-encoder logits) are on one comparable scale for the soft-filter boost."""
    if not d:
        return {}
    vals = d.values()
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return {k: 1.0 for k in d}
    return {k: (v - lo) / (hi - lo) for k, v in d.items()}


def retrieve(con, query, k=4, pool=20, embed_model=DEFAULT_EMBED_MODEL, rrf_k=60,
             clean_filter=None, max_context_tokens=None, max_per_parent=1,
             rerank=True, rerank_pool=30, rerank_model=DEFAULT_RERANK_MODEL,
             filter_mode="hard", soft_boost=0.15, guard_min=None,
             matched_parents=None):
    # --- pre-retrieval metadata filter ---
    eligible = _eligible_rowids(con, clean_filter)  # None if no filter

    # Resolve HOW the filter is applied:
    #   hard -> restrict candidates to eligible rows (a gate).
    #   soft -> keep the whole corpus but add a modest boost to eligible passages,
    #           so a strongly-relevant non-matching passage can still outrank them
    #           (defends against an over-broad / spurious model-derived filter).
    #   fill -> eligible-first: show every matching record, then top up the
    #           remaining slots with the best non-matching passages. Guarantees the
    #           matches are all shown AND that k slots get filled.
    #   auto -> k-guard: hard-gate when the matched set is comfortably larger than k;
    #           otherwise 'fill', so a narrow match can't starve retrieval (returning
    #           fewer than k) yet its matches are never discarded.
    mode = "off" if eligible is None else filter_mode
    if mode == "auto":
        if matched_parents is None:
            pr = con.execute("SELECT rowid, parent_rid, rid FROM records").fetchall()
            matched_parents = len({(p or r) for (rid, p, r) in pr if rid in eligible})
        threshold = guard_min if guard_min is not None else k
        mode = "hard" if matched_parents > threshold else "fill"
    restrict = (mode == "hard")

    # --- vector side ---
    qvec = embed([query], embed_model)[0]
    rows = con.execute("SELECT rowid, embedding FROM records").fetchall()
    if not rows:
        return []
    if restrict:
        rows = [r for r in rows if r[0] in eligible]
        if not rows:
            return []  # hard filter excluded everything; caller decides on fallback
    ids = [r[0] for r in rows]
    mat = np.vstack([unpack(r[1]) for r in rows])
    sims = mat @ qvec  # both normalised -> cosine
    order = np.argsort(-sims)[:pool]
    vector_hits = [ids[i] for i in order]

    # --- keyword side ---
    try:
        kw = con.execute(
            "SELECT rowid FROM records_fts WHERE records_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (fts_query_string(query), pool),
        ).fetchall()
        keyword_hits = [r[0] for r in kw]
        if restrict:
            keyword_hits = [r for r in keyword_hits if r in eligible]
    except sqlite3.OperationalError:
        keyword_hits = []

    # --- Reciprocal Rank Fusion ---
    scores = {}
    for rank, rid in enumerate(vector_hits):
        scores[rid] = scores.get(rid, 0) + 1.0 / (rrf_k + rank)
    for rank, rid in enumerate(keyword_hits):
        scores[rid] = scores.get(rid, 0) + 1.0 / (rrf_k + rank)
    ranked = sorted(scores, key=scores.get, reverse=True)

    # Relevance in [0,1] per candidate: normalized RRF now, upgraded to normalized
    # cross-encoder score after rerank. This is the basis the soft filter adds to.
    rel = _normalize_scores({rid: scores[rid] for rid in ranked})

    # --- cross-encoder rerank over a wider candidate pool ---
    # RRF gives a fast, approximate ordering; rescoring its top candidates with a
    # cross-encoder (query + passage judged together) is much more precise at
    # surfacing the passages actually about the question. Degrades to RRF order
    # if the reranker can't load (e.g. offline first run).
    if rerank and len(ranked) > 1:
        cand = ranked[:rerank_pool]
        text_by_id = {}
        for rid in cand:
            r = con.execute("SELECT text FROM records WHERE rowid=?",
                            (rid,)).fetchone()
            if r:
                text_by_id[rid] = r[0]
        cand = [rid for rid in cand if rid in text_by_id]
        try:
            rk = get_reranker(rerank_model)
            ce = rk.predict([(query, text_by_id[rid]) for rid in cand])
            ce_by_id = {rid: float(s) for rid, s in zip(cand, ce)}
            reranked = sorted(cand, key=lambda r: -ce_by_id[r])
            seen = set(reranked)
            ranked = reranked + [rid for rid in ranked if rid not in seen]
            # candidates are now scored by the cross-encoder; the RRF tail keeps its
            # normalized-RRF relevance (already lower, as it ranked below the pool).
            rel.update(_normalize_scores(ce_by_id))
        except Exception as e:
            print(f"  (rerank skipped: {e}; using RRF order)", file=sys.stderr)

    # --- soft: modest relevance boost to eligible (escape hatch preserved) ---
    # A much-more-relevant non-matching passage can still outrank a matching one,
    # so retrieval degrades gracefully for an over-broad / spurious filter.
    if mode == "soft" and eligible:
        boosted = {rid: rel.get(rid, 0.0) + (soft_boost if rid in eligible else 0.0)
                   for rid in ranked}
        ranked.sort(key=lambda r: -boosted[r])

    # --- fill: eligible-first, then top up remaining slots with best non-match ---
    # The matched set can be semantically orthogonal to the query (so its passages
    # never entered the candidate pool); inject any missing eligible rows first
    # (ordered by vector similarity) so ALL matches are shown, then append the
    # non-matching candidates to fill k. Guarantees matches aren't starved.
    elif mode == "fill" and eligible:
        have = set(ranked)
        missing = [rid for rid in eligible if rid not in have]
        if missing:
            vec_by_id = dict(zip(ids, (float(s) for s in sims)))
            missing.sort(key=lambda r: -vec_by_id.get(r, 0.0))
            ranked = ranked + missing
        elig = [r for r in ranked if r in eligible]
        non = [r for r in ranked if r not in eligible]
        ranked = elig + non

    # --- assemble output: passage-level, diversified + cited by parent ---
    # Passages are retrieved individually (better matching on long docs, and a
    # hit is one bounded passage rather than a whole page), but we cap passages
    # per source so one document can't monopolise the k slots, and we cite the
    # parent record so citations stay record-level and readable.
    out = []
    used_tokens = 0
    per_parent = {}
    for rowid in ranked:
        if len(out) >= k:
            break
        row = con.execute(
            "SELECT rid, parent_rid, title, text FROM records WHERE rowid=?",
            (rowid,),
        ).fetchone()
        if not row:
            continue
        passage_rid, parent_rid, title, text = row
        parent = parent_rid or passage_rid
        if per_parent.get(parent, 0) >= max_per_parent:
            continue
        if max_context_tokens is not None:
            # rough token estimate ~ chars/4; truncate body, always keep params line
            block_tokens = _est_tokens(text)
            if used_tokens + block_tokens > max_context_tokens and out:
                break  # budget hit and we already have at least one passage
            text = _truncate_to_tokens(text, max_context_tokens - used_tokens)
            used_tokens += _est_tokens(text)
        per_parent[parent] = per_parent.get(parent, 0) + 1
        out.append({"rid": parent, "passage": passage_rid,
                    "title": title, "text": text})
    return out


def _snippet(text, n=200):
    """A short, single-line preview of a passage: drop the redundant 'Title:'
    header line and collapse whitespace."""
    body = (text.split("\n", 1)[1]
            if text.startswith("Title:") and "\n" in text else text)
    body = " ".join(body.split())
    return (body[:n].rstrip() + "…") if len(body) > n else body


def record_digest(con, query, clean_filter, embed_model=DEFAULT_EMBED_MODEL,
                  limit=24, snippet_chars=200, exclude=None):
    """For a filtered set larger than k, return a compact one-line-per-record
    digest so the model (and the UI) can see the WHOLE matched set, not just the
    top-k full passages. Each entry is a record's single best-matching passage
    (extractive snippet) relative to the query. Cheap: bi-encoder cosine, best
    passage per parent. Returns list of {rid, title, snippet}, most-relevant
    first, capped at `limit`, excluding parent rids in `exclude`."""
    eligible = _eligible_rowids(con, clean_filter)
    if not eligible:
        return []
    exclude = exclude or set()
    qvec = embed([query], embed_model)[0]
    rows = con.execute(
        "SELECT rowid, parent_rid, rid, title, text, embedding FROM records"
    ).fetchall()
    elig = [r for r in rows if r[0] in eligible]
    if not elig:
        return []
    sims = np.vstack([unpack(r[5]) for r in elig]) @ qvec
    best = {}  # parent -> (score, title, snippet)
    for (rowid, parent_rid, prid, title, text, _emb), score in zip(elig, sims):
        parent = parent_rid or prid
        if parent in exclude:
            continue
        score = float(score)
        if parent not in best or score > best[parent][0]:
            best[parent] = (score, title, _snippet(text, snippet_chars))
    ordered = sorted(best.items(), key=lambda kv: -kv[1][0])[:limit]
    return [{"rid": p, "title": t, "snippet": s} for p, (_sc, t, s) in ordered]


_ANALYTIC_RE = re.compile(
    r"\b(compare|which|list|how many|number of|most|least|longest|shortest|"
    r"highest|lowest|heaviest|lightest|fastest|slowest|largest|smallest|greatest|"
    r"max(?:imum)?|min(?:imum)?|average|mean|sort|rank|top|over|under|above|below|"
    r"more than|less than|greater|between|each|all)\b", re.I)


def is_analytic_query(query, clean_filter):
    """Heuristic: does the question want to compare/aggregate across the matched
    set (-> structured table), rather than a prose 'why/how' answer? True if the
    filter constrains a numeric field, or the question uses comparison/aggregate
    language."""
    if clean_filter and any(
            isinstance(c, dict) and c.get("type") == "numeric"
            for c in clean_filter.values()):
        return True
    return bool(_ANALYTIC_RE.search(query or ""))


def _table_columns(query, clean_filter, catalogue, param_union, max_cols=6):
    """Pick table columns: filter fields first, then fields named in the query,
    then orientation defaults, then fill with parametrics present in the set."""
    cols = []

    def add(f):
        if f and f not in cols:
            cols.append(f)

    for f in (clean_filter or {}):
        add(f)
    ql = (query or "").lower()
    for f in catalogue:
        if f.lower() in ql:
            add(f)
    for f in ("systemGroup", "systemType"):
        if f in catalogue:
            add(f)
    for f in param_union:
        if len(cols) >= max_cols:
            break
        add(f)
    return cols[:max_cols]


def record_table(con, query, clean_filter, catalogue=None, limit=40, max_cols=6):
    """Structured view of the filtered set: one row per matched record, columns
    chosen from the filter + query + defaults, each CELL carrying the FULL field
    (value + unit + description) rather than just the value. Sorted by the first
    numeric column so 'longest/most' reads top-down. Returns
    {columns, rows:[{rid,title,cells:{col:{value,unit,descr}}}], total} or None.

    FUTURE: a cell is currently a single {value, unit, descr}; duplicate
    parameters / multiple subtitles per field collapse to one (see
    record_model.rich_params). Next iteration: cells become lists so repeated
    parameters and their subtitles are all shown (stacked sub-rows)."""
    eligible_parents = _eligible_parents(con, clean_filter)
    if not eligible_parents:
        return None
    catalogue = catalogue if catalogue is not None else load_catalogue(con)
    # title + typed fields per matched parent, read once per record
    # (REVIEW_FINDINGS B2) rather than from a representative passage row.
    fields_map = _record_fields_map(con)
    reps = {parent: fields_map[parent] for parent in eligible_parents
            if parent in fields_map}
    if not reps:
        return None
    # rich parametrics per parent + the union of available param names
    params_by_parent, union = {}, []
    for parent in reps:
        row = con.execute("SELECT params_json FROM record_params WHERE parent_rid=?",
                          (parent,)).fetchone()
        p = json.loads(row[0]) if row and row[0] else {}
        params_by_parent[parent] = p
        for name in p:
            if name not in union:
                union.append(name)
    columns = _table_columns(query, clean_filter, catalogue, union, max_cols)

    rows = []
    for parent, (title, fields) in reps.items():
        cells = {}
        for col in columns:
            if col in params_by_parent.get(parent, {}):
                pf = params_by_parent[parent][col]
                cells[col] = {"value": pf.get("value"), "unit": pf.get("unit"),
                              "descr": pf.get("descr")}
            elif col in fields:
                v = fields[col]
                if isinstance(v, list):
                    v = ", ".join(map(str, v))
                cells[col] = {"value": v,
                              "unit": (catalogue.get(col) or {}).get("unit"),
                              "descr": None}
            else:
                cells[col] = {"value": None, "unit": None, "descr": None}
        rows.append({"rid": parent, "title": title, "cells": cells})

    num_col = next((c for c in columns
                    if (catalogue.get(c) or {}).get("type") == "numeric"), None)
    if num_col:
        def _key(r):
            try:
                return -float(r["cells"][num_col]["value"])
            except (TypeError, ValueError):
                return float("inf")  # missing values sink to the bottom
        rows.sort(key=_key)
    else:
        rows.sort(key=lambda r: r["title"])
    return {"columns": columns, "rows": rows[:limit], "total": len(reps)}


def _render_table_text(table):
    """Render the structured table for the prompt: one line per record, each
    field as value + unit + (description), so the model sees the full field."""
    cols = table["columns"]
    lines = [f"Structured fields for all {table['total']} matched records "
             f"(field=value unit (description)):"]
    for r in table["rows"]:
        segs = []
        for col in cols:
            c = r["cells"].get(col) or {}
            if c.get("value") in (None, ""):
                continue
            seg = f"{col}={c['value']}"
            if c.get("unit"):
                seg += f" {c['unit']}"
            descr = c.get("descr")
            if descr:
                # full descr is kept in the API/UI cell; cap it here so the
                # in-prompt table stays token-bounded across many rows/columns.
                if len(descr) > 180:
                    descr = descr[:180].rstrip() + "…"
                seg += f" ({descr})"
            segs.append(seg)
        lines.append(f"[{r['rid']}] {r['title']}: " + "; ".join(segs))
    if table["total"] > len(table["rows"]):
        lines.append(f"(+{table['total'] - len(table['rows'])} more not shown)")
    return "\n".join(lines)


def _est_tokens(s):
    return max(1, len(s) // 4)  # crude but fine for budgeting


def _truncate_to_tokens(text, token_budget):
    """Truncate free-text but preserve parametric lines (high-value, tiny).
    Parameter lines start with 'Parameter ' from record_model.to_text()."""
    if token_budget <= 0:
        token_budget = 64
    char_budget = token_budget * 4
    if len(text) <= char_budget:
        return text
    lines = text.split("\n")
    param_lines = [l for l in lines if l.startswith("Parameter ")]
    other_lines = [l for l in lines if not l.startswith("Parameter ")]
    # keep all param lines, then fill remaining budget with other lines
    kept = list(param_lines)
    budget_left = char_budget - sum(len(l) + 1 for l in kept)
    for l in other_lines:
        if budget_left <= 0:
            break
        if len(l) + 1 <= budget_left:
            kept.append(l)
            budget_left -= len(l) + 1
        else:
            kept.append(l[:budget_left].rstrip() + "…")
            break
    # restore rough original ordering (title/text first, params after)
    ordered = [l for l in lines if l in kept]
    return "\n".join(ordered)


# --------------------------------------------------------------------------- #
# Generation backends                                                          #
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = (
    "You answer strictly from the provided context records. "
    "Cite the record id(s) you used in square brackets. "
    "If the answer is not in the context, say you don't have that information."
)


def build_prompt(query, contexts, digest=None, table=None):
    blocks = []
    for c in contexts:
        blocks.append(f"[{c['rid']}] {c['title']}\n{c['text']}")
    ctx = "\n\n---\n\n".join(blocks)
    parts = [f"Context records:\n\n{ctx}"]
    # Structured backbone: exact fields for every matched record (analytic
    # queries). Placed before the digest since it's the source of truth for
    # values; the top-k passages above give prose depth.
    if table and table.get("rows"):
        parts.append(_render_table_text(table))
    # When a metadata filter matched more records than fit in the top-k full
    # passages, append a compact roster of the rest (one relevance snippet each)
    # so the model can reason over the whole matched set, not just the top few.
    if digest:
        lines = [f"[{d['rid']}] {d['title']} — {d['snippet']}" for d in digest]
        parts.append(
            "Additional records matching the same metadata filter (one relevance "
            "snippet each; these are partial — note if a snippet is insufficient "
            "to answer):\n" + "\n".join(lines))
    parts.append(f"Question: {query}")
    return "\n\n".join(parts)


_local_pipe = {}


def _load_local(model):
    if model not in _local_pipe:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        tok = AutoTokenizer.from_pretrained(model)
        mdl = AutoModelForCausalLM.from_pretrained(
            model,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
            device_map="auto" if torch.cuda.is_available() else None,
        )
        _local_pipe[model] = (tok, mdl)
    return _local_pipe[model]


def gen_local_raw(system, user, model="Qwen/Qwen2.5-1.5B-Instruct", max_new=512):
    tok, mdl = _load_local(model)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    inputs = tok.apply_chat_template(
        messages, add_generation_prompt=True, return_tensors="pt"
    ).to(mdl.device)
    out = mdl.generate(inputs, max_new_tokens=max_new, do_sample=False)
    return tok.decode(out[0][inputs.shape[1]:], skip_special_tokens=True).strip()


# Default wall-clock timeout (seconds) for a single generation HTTP call. Without
# this, urllib.request.urlopen blocks forever, so one hung/slow upstream model
# would pin a worker thread (and, in the bench, freeze that column) indefinitely.
HTTP_TIMEOUT = 60


def _http_json(req, timeout):
    """POST + parse JSON with a bounded timeout, turning network/timeout failures
    into a clean RuntimeError (so the UI shows a readable message, not a bare
    TimeoutError or a hung request)."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (TimeoutError, urllib.error.URLError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(e, urllib.error.HTTPError):
            # surface the API's error body (rate limit, bad model id, etc.)
            try:
                reason = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code}: {reason}") from None
        raise RuntimeError(f"request failed after {timeout}s: {reason}") from None


def _anthropic_raw(system, user, model="claude-sonnet-4-6", max_tokens=1024,
                   timeout=HTTP_TIMEOUT):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    body = json.dumps({
        "model": model, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"},
    )
    data = _http_json(req, timeout)
    return "".join(b["text"] for b in data["content"] if b["type"] == "text").strip()


def _openai_raw(system, user, model, base_url="http://localhost:8000/v1",
                 api_key=None, extra_headers=None, timeout=HTTP_TIMEOUT,
                 max_tokens=None):
    if not model:
        raise RuntimeError("model required for openai-compatible backend")
    key = api_key or os.environ.get("OPENAI_API_KEY", "none")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.1,
    }
    # Bound the output. Without a cap a model can stream a runaway response for
    # minutes (urllib's timeout is per-read, not a total deadline, so it won't
    # fire), which is exactly how a small model hangs on a large filter spec.
    if max_tokens:
        payload["max_tokens"] = max_tokens
    body = json.dumps(payload).encode()
    headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
    headers.update(extra_headers or {})
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body, headers=headers,
    )
    data = _http_json(req, timeout)
    return data["choices"][0]["message"]["content"].strip()


OPENAI_MAX_TOKENS = 1024      # default cap for model answers
FILTER_MAX_TOKENS = 256       # filter JSON is tiny; keeps extraction snappy
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "google/gemma-3-27b-it"  # capable, self-hostable default

# Convenience aliases so --model can be short ("qwen3-14b", "mistral-small")
# instead of the full OpenRouter slug. Kept in sync with models_registry.py so
# the CLI and the bench trial the same real, verified slugs.
OPENROUTER_ALIASES = {
    "gemma3-4b": "google/gemma-3-4b-it",
    "gemma3-27b": "google/gemma-3-27b-it",
    "phi4": "microsoft/phi-4",
    "qwen3-14b": "qwen/qwen3-14b",
    "qwen3-32b": "qwen/qwen3-32b",
    "mistral-small": "mistralai/mistral-small-3.2-24b-instruct",
    "llama3.3-70b": "meta-llama/llama-3.3-70b-instruct",
}


def _openrouter_raw(system, user, model, timeout=HTTP_TIMEOUT,
                    max_tokens=OPENAI_MAX_TOKENS):
    model = OPENROUTER_ALIASES.get(model, model)
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. Get a key at https://openrouter.ai/keys"
        )
    # OpenRouter recommends (not strictly requires) these for app attribution
    # and to appear in their public rankings; harmless to omit but good practice.
    extra = {
        "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", "https://ragkit.local"),
        "X-Title": os.environ.get("OPENROUTER_TITLE", "ragkit"),
    }
    return _openai_raw(system, user, model, base_url=OPENROUTER_BASE_URL,
                       api_key=key, extra_headers=extra, timeout=timeout,
                       max_tokens=max_tokens)


def gen_local(query, contexts, model="Qwen/Qwen2.5-1.5B-Instruct", digest=None,
              table=None):
    return gen_local_raw(SYSTEM_PROMPT,
                         build_prompt(query, contexts, digest, table), model)


def gen_anthropic(query, contexts, model="claude-sonnet-4-6", digest=None,
                  table=None):
    return _anthropic_raw(SYSTEM_PROMPT,
                          build_prompt(query, contexts, digest, table), model)


def gen_openrouter(query, contexts, model=None, digest=None, table=None):
    return _openrouter_raw(SYSTEM_PROMPT,
                           build_prompt(query, contexts, digest, table),
                           model or OPENROUTER_DEFAULT_MODEL)


def gen_openai(query, contexts, model, base_url="http://localhost:8000/v1",
               digest=None, table=None):
    return _openai_raw(SYSTEM_PROMPT,
                       build_prompt(query, contexts, digest, table),
                       model, base_url)


def generate(query, contexts, backend, model=None, base_url=None, digest=None,
             table=None):
    if backend == "local":
        return gen_local(query, contexts, model or "Qwen/Qwen2.5-1.5B-Instruct",
                         digest, table)
    if backend == "anthropic":
        return gen_anthropic(query, contexts, model or "claude-sonnet-4-6",
                             digest, table)
    if backend == "openrouter":
        return gen_openrouter(query, contexts, model, digest, table)
    if backend == "openai":
        if not model:
            raise RuntimeError("--model required for openai backend")
        return gen_openai(query, contexts, model,
                          base_url or "http://localhost:8000/v1", digest, table)
    raise RuntimeError(f"unknown backend {backend}")


def extract_filter(query, catalogue, backend, model=None, base_url=None,
                   only_fields=None, min_count=1, max_fields=None):
    """Ask the LLM to emit a JSON filter grounded in the catalogue. Returns the
    raw parsed dict (unvalidated) or {} on failure. In production you'd pair this
    with grammar/schema-constrained decoding (vLLM guided decoding / llama.cpp
    GBNF) built from the catalogue so the output is structurally guaranteed.

    only_fields/min_count/max_fields shrink the field spec shown to the model (see
    catalogue_to_prompt): present only these fields, drop fields with coverage below
    min_count, and/or cap to the top max_fields by coverage. This keeps the prompt
    small and the field choice tractable on a large catalogue."""
    spec = cat_mod.catalogue_to_prompt(
        catalogue, only_fields=only_fields, min_count=min_count, max_fields=max_fields)
    sys_p = (
        "You convert a question into a JSON metadata filter. "
        "Output ONLY a JSON object, no prose. Use only the fields and values "
        "listed. Numeric: {\"field\":{\"min\":n,\"max\":n}}. If the question "
        "expresses a value in a different unit than the field's listed unit, add "
        "\"unit\":\"<the question's unit>\" and keep the number as the user said "
        "it (the system converts). e.g. field in miles, user says 50 km -> "
        "{\"field\":{\"max\":50,\"unit\":\"km\"}}. "
        "Categorical: {\"field\":{\"in\":[...]}}. "
        "Multi-value: {\"field\":{\"contains\":[...]}}. "
        "Date: {\"field\":{\"date_from\":\"YYYY-MM-DD\",\"date_to\":\"YYYY-MM-DD\"}}. "
        "If no filter applies, output {}."
    )
    user_p = f"{spec}\n\nQuestion: {query}\n\nJSON filter:"
    ctx = [{"rid": "_spec", "title": "filter spec", "text": user_p}]
    try:
        if backend == "local":
            raw = gen_local_raw(sys_p, user_p, model or "Qwen/Qwen2.5-1.5B-Instruct")
        elif backend == "anthropic":
            raw = _anthropic_raw(sys_p, user_p, model or "claude-sonnet-4-6",
                                 max_tokens=FILTER_MAX_TOKENS)
        elif backend == "openrouter":
            raw = _openrouter_raw(sys_p, user_p, model or OPENROUTER_DEFAULT_MODEL,
                                  max_tokens=FILTER_MAX_TOKENS)
        elif backend == "openai":
            raw = _openai_raw(sys_p, user_p, model,
                              base_url or "http://localhost:8000/v1",
                              max_tokens=FILTER_MAX_TOKENS)
        else:
            return {}
        raw = raw.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception:
        return {}


def answer(db_path, query, backend, model=None, base_url=None,
           embed_model=DEFAULT_EMBED_MODEL, k=4, filters=None,
           auto_filter=False, max_context_tokens=None,
           two_pass=False, filter_min_count=1, filter_max_fields=None,
           filter_mode="hard"):
    """filters: an explicit filter dict (from UI/caller).
    auto_filter: if True and no explicit filters, ask the model to derive one.
    two_pass: use the broad-category-then-detail extractor (extract_filter_2pass).
    filter_min_count/filter_max_fields: coverage-based pruning of the single-pass
    field spec (see catalogue_to_prompt).
    filter_mode: how the validated filter is applied at retrieval time --
    'hard' (gate), 'soft' (rank boost), or 'auto' (k-guard: hard only when the
    matched set is comfortably larger than k). See retrieve().
    Returns (reply, contexts, filter_info)."""
    con = connect(db_path)
    catalogue = load_catalogue(con)
    filter_info = {"applied": {}, "errors": [], "source": None, "fell_back": False}

    raw_filter = filters
    if raw_filter is None and auto_filter and catalogue:
        if two_pass:
            raw_filter, tp_info = extract_filter_2pass(
                query, catalogue, con, backend, model, base_url,
                min_count=filter_min_count)
            filter_info["two_pass"] = tp_info
        else:
            raw_filter = extract_filter(
                query, catalogue, backend, model, base_url,
                min_count=filter_min_count, max_fields=filter_max_fields)
        filter_info["source"] = "model"
    elif raw_filter is not None:
        filter_info["source"] = "explicit"

    clean = {}
    if raw_filter:
        clean, errors = validate_filter(raw_filter, catalogue)
        filter_info["applied"] = clean
        filter_info["errors"] = errors
        # how many records actually match (before the top-k cap), so a large
        # filtered set isn't silently reduced to k with no signal.
        if clean:
            filter_info["matched_records"] = count_matches(con, clean)

    # Record the mode actually used (auto resolves to hard/fill based on the matched
    # count), so callers/UI can see whether the filter gated or just topped up.
    eff_mode = filter_mode
    if clean and filter_mode == "auto":
        eff_mode = "hard" if (filter_info.get("matched_records") or 0) > k else "fill"
    if clean:
        filter_info["filter_mode"] = eff_mode

    contexts = retrieve(con, query, k=k, embed_model=embed_model,
                        clean_filter=clean or None,
                        max_context_tokens=max_context_tokens,
                        filter_mode=eff_mode,
                        matched_parents=filter_info.get("matched_records"))

    # Empty-set fallback: if a filter excluded everything, retry unfiltered
    # rather than misleadingly answering "no information".
    if not contexts and clean:
        filter_info["fell_back"] = True
        contexts = retrieve(con, query, k=k, embed_model=embed_model,
                            max_context_tokens=max_context_tokens)

    if not contexts:
        return "No records indexed.", [], filter_info

    # Represent the fuller matched set beyond the top-k passages. Analytic
    # questions get a structured table (exact fields incl. descriptions);
    # otherwise a snippet-per-record digest for prose breadth.
    digest, table = [], None
    matched = filter_info.get("matched_records")
    if clean and not filter_info["fell_back"] and matched:
        if is_analytic_query(query, clean):
            table = record_table(con, query, clean, catalogue=catalogue)
            if table:
                filter_info["table"] = table
        if not table and matched > len(contexts):
            shown = {c["rid"] for c in contexts}
            digest = record_digest(con, query, clean, embed_model=embed_model,
                                   exclude=shown)
            if digest:
                filter_info["digest"] = digest

    reply = generate(query, contexts, backend, model, base_url,
                     digest=digest, table=table)
    return reply, contexts, filter_info


# --------------------------------------------------------------------------- #
# Web interface                                                                #
# --------------------------------------------------------------------------- #
#
# There is one served UI: the side-by-side model comparison bench in
# compare_server.py. `serve()` below just launches it, so `python ragkit.py
# serve` and `python compare_server.py` are the same thing (one entrypoint,
# one port). The bench preloads the embedder at boot and reuses connections, so
# the first query is instant. For single-answer, non-OpenRouter backends
# (local / anthropic / openai), use `ragkit.py ask` on the CLI.


def serve(db_path, port=8099, k=4, max_context_tokens=None, open_browser=True):
    # Imported lazily to avoid a circular import at module load (compare_server
    # imports ragkit) and to keep flask an optional dependency of the CLI.
    import compare_server
    compare_server.run_server(db_path, port=port, k=k,
                              max_context_tokens=max_context_tokens,
                              open_browser=open_browser)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main():
    p = argparse.ArgumentParser(description="single-file RAG prototype")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="build the index from JSON records")
    pi.add_argument("src", help="a .json file or a directory of them")
    pi.add_argument("--db", default="rag.db")
    pi.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)

    pa = sub.add_parser("ask", help="run one query")
    pa.add_argument("query")
    pa.add_argument("--db", default="rag.db")
    pa.add_argument("--backend", choices=["local", "anthropic", "openrouter", "openai"], default="local")
    pa.add_argument("--model")
    pa.add_argument("--base-url")
    pa.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    pa.add_argument("-k", type=int, default=4)
    pa.add_argument("--auto-filter", action="store_true",
                    help="let the model derive a metadata filter from the query")
    pa.add_argument("--filter", help="explicit filter as JSON, e.g. "
                    "'{\"max_temp\":{\"min\":150},\"status\":{\"in\":[\"active\"]}}'")
    pa.add_argument("--max-context-tokens", type=int, default=None,
                    help="cap total retrieved context tokens")
    pa.add_argument("--show-catalogue", action="store_true",
                    help="print the field catalogue and exit")

    ps = sub.add_parser("serve", help="launch the model-comparison web bench")
    ps.add_argument("--db", default="rag_test.db")
    ps.add_argument("--port", type=int, default=8099)
    ps.add_argument("-k", type=int, default=4)
    ps.add_argument("--max-context-tokens", type=int, default=3000,
                    help="cap on retrieved text packed into the prompt (0 = no cap)")
    ps.add_argument("--no-open", action="store_true",
                    help="don't auto-open the browser")

    a = p.parse_args()
    if a.cmd == "ingest":
        ingest(a.db, a.src, a.embed_model)
    elif a.cmd == "ask":
        if a.show_catalogue:
            con = connect(a.db)
            print(cat_mod.catalogue_to_prompt(load_catalogue(con)))
            return
        explicit = json.loads(a.filter) if a.filter else None
        reply, ctx, finfo = answer(
            a.db, a.query, a.backend, a.model, a.base_url, a.embed_model, a.k,
            filters=explicit, auto_filter=a.auto_filter,
            max_context_tokens=a.max_context_tokens)
        print(reply)
        print("\n--- retrieved ---", file=sys.stderr)
        for c in ctx:
            print(f"[{c['rid']}] {c['title']}", file=sys.stderr)
        if finfo.get("applied"):
            print(f"--- filter ({finfo['source']}): {json.dumps(finfo['applied'])}",
                  file=sys.stderr)
        if finfo.get("errors"):
            print(f"--- filter notes: {finfo['errors']}", file=sys.stderr)
        if finfo.get("fell_back"):
            print("--- filter excluded all records; fell back to unfiltered",
                  file=sys.stderr)
    elif a.cmd == "serve":
        serve(a.db, port=a.port, k=a.k,
              max_context_tokens=a.max_context_tokens or None,  # 0 -> uncapped
              open_browser=not a.no_open)


if __name__ == "__main__":
    main()
