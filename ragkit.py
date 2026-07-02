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
import difflib
import glob
import json
import math
import os
import re
import sqlite3
import statistics
import struct
import sys
import threading
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
import models_registry
import record_model
import units as units_mod

DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # matches Clipper
EMBED_DIM = 384
# Cross-encoder used to rerank the top retrieval candidates. Unlike the
# bi-encoder embedder (query and passage encoded separately, then cosine), a
# cross-encoder scores each (query, passage) pair jointly, which is markedly
# more precise at picking the passages actually about the question.
DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# The categorical field per-category numeric stats (REVIEW_FINDINGS A4) are
# grouped by. "systemType" is the finest-grained broad partition field this
# corpus has (Air-to-Air Missile / Main Battle Tank / ...) -- fine-grained
# enough that a category's numeric spread (e.g. AAM range) is actually
# coherent, unlike "systemGroup" (Weapon/Vehicle/...) which is still too
# broad. A single named constant so ingest (which builds the stats) and
# extract_filter_2pass (which reads them back) can't drift apart.
CATEGORY_STAT_FIELD = "systemType"

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
    # One row per CATALOGUE FIELD (not per record), holding an embedding of a
    # short descriptor of the field (name + type + unit + example values) --
    # REVIEW_FINDINGS A2 "catalogue-as-retrieval": at query time, select_fields
    # ranks fields by cosine similarity to the query embedding instead of
    # dumping all ~200+ fields into every filter-extraction prompt. Absent
    # entirely for a db ingested before this phase; select_fields falls back to
    # coverage order in that case (see load_field_embeddings).
    con.execute(
        "CREATE TABLE IF NOT EXISTS field_embeddings (field TEXT PRIMARY KEY, vec BLOB)"
    )
    con.commit()


def _field_descriptor(field, spec):
    """A short text description of ONE catalogue field, built for embedding
    (REVIEW_FINDINGS A2): the query-relevance signal for field SELECTION isn't
    the field's stored VALUES, it's what the field means and what kind of
    thing it holds, so a query like "small mass" can find the "Mass" field by
    meaning even though it never says "Mass". Deliberately short (a handful of
    words/examples, not the full catalogue entry) -- this is embedded once per
    field at ingest, so it costs nothing per query, but a bloated descriptor
    would still dilute the embedding with noise."""
    t = spec.get("type")
    parts = [field]
    if t == "numeric":
        u = spec.get("unit")
        parts.append(f"numeric measurement{' in ' + u if u else ''}")
        if spec.get("median") is not None:
            parts.append(f"typical value around {spec['median']}")
    elif t == "categorical":
        vals = spec.get("values") or []
        parts.append("categorical field")
        if vals:
            parts.append("values include " + ", ".join(str(v) for v in vals[:8]))
    elif t == "multi_value":
        vals = spec.get("values") or []
        parts.append("multi-value list field")
        if vals:
            parts.append("elements include " + ", ".join(str(v) for v in vals[:8]))
    elif t == "date":
        parts.append("date field")
    else:
        parts.append("free text field")
        ex = spec.get("examples") or []
        if ex:
            parts.append("e.g. " + "; ".join(str(e) for e in ex))
    return " - ".join(parts)


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
    con.execute("DROP TABLE IF EXISTS field_embeddings")
    init_db(con)

    records = list(load_records(src))
    if not records:
        print("No records found.", file=sys.stderr)
        return
    raws = [r for _, _, _, r, _ in records]

    # Expand every record into passages, remembering which parent each came from.
    passages = []  # (passage_rid, parent_rid, title, passage_text)
    # {parent_rid: {field: value}} -- kept around (not just written to
    # record_params) so build_category_stats below can re-aggregate it without
    # a second pass over the raw records or a round-trip through the db.
    fields_by_parent = {}
    for parent_rid, title, text, raw, canon in records:
        typed, _units = record_model.typed_fields(canon)
        fields_by_parent[parent_rid] = typed
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

    # Catalogue-as-retrieval field embeddings (REVIEW_FINDINGS A2, ingest half):
    # embed a short descriptor of each catalogue field (name + type + unit + a
    # few example values/labels) so select_fields can rank fields by MEANING
    # ("small mass" -> Mass, "long range" -> Range) at query time instead of
    # every filter-extraction prompt showing all ~200+ fields. One row per
    # field, not per record -- cheap even on a modest embedder.
    if catalogue:
        field_names = list(catalogue.keys())
        descriptors = [_field_descriptor(f, catalogue[f]) for f in field_names]
        field_vecs = embed(descriptors, embed_model)
        for f, v in zip(field_names, field_vecs):
            con.execute(
                "INSERT OR REPLACE INTO field_embeddings (field, vec) VALUES (?,?)",
                (f, pack(v)),
            )
        print(f"Field embeddings: {len(field_names)} catalogue fields embedded "
              f"for query-time field selection.", file=sys.stderr)

    # Per-category numeric stats (REVIEW_FINDINGS A4): global p5/p95 mixes a
    # cartridge with a carrier; group the same percentile stats by
    # CATEGORY_STAT_FIELD ("systemType") so a filter-extraction model shown the
    # narrowed category (two-pass pass 2) calibrates "long range" against the
    # ~8-110 km an AAM actually spans, not the corpus-wide 8-11,000 km. See
    # catalogue.build_category_stats / catalogue_to_prompt's category_stats kwarg.
    category_stats = {}
    if CATEGORY_STAT_FIELD in catalogue:
        category_stats = cat_mod.build_category_stats(
            fields_by_parent, catalogue, CATEGORY_STAT_FIELD)
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('category_stats', ?)",
        (json.dumps({"field": CATEGORY_STAT_FIELD, "stats": category_stats}),),
    )
    if category_stats:
        print(f"Per-category numeric stats: {len(category_stats)} "
              f"{CATEGORY_STAT_FIELD} value(s) covered.", file=sys.stderr)

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


def load_category_stats(con):
    """{"field": <the CATEGORY_STAT_FIELD used at ingest, e.g. "systemType">,
    "stats": {category_value: {numeric_field: {count,min,p5,median,p95,max}}}}
    built at ingest by catalogue.build_category_stats (REVIEW_FINDINGS A4).
    {} for a db ingested before this phase (or a catalogue with no
    CATEGORY_STAT_FIELD -- e.g. a corpus with no systemType-like column)."""
    row = con.execute(
        "SELECT value FROM meta WHERE key='category_stats'").fetchone()
    return json.loads(row[0]) if row else {}


def load_field_embeddings(con):
    """{field: np.ndarray(384,)} for every catalogue field embedded at ingest
    (REVIEW_FINDINGS A2; see ingest()'s field_embeddings table / _field_
    descriptor). {} for a db ingested before this phase -- select_fields treats
    that as "no embeddings available" and falls back to coverage order."""
    try:
        rows = con.execute("SELECT field, vec FROM field_embeddings").fetchall()
    except sqlite3.OperationalError:
        return {}  # db predates the field_embeddings table entirely
    return {field: unpack(vec) for field, vec in rows}


# Common words too generic to count as a field "naming itself" in a query
# (see _field_name_hit) -- "and"/"have"/"which" etc. would otherwise falsely
# match against unrelated field names that happen to share one of these words.
_FIELD_NAME_STOPWORDS = {
    "and", "the", "of", "in", "on", "at", "for", "to", "a", "an", "or",
    "with", "is", "are", "have", "has", "which", "what", "who", "how",
    "does", "do", "this", "that", "its", "it", "as", "by",
}


def _field_name_hit(field, ql):
    """True if a significant word of `field`'s NAME appears verbatim
    (whole-word, case-insensitive) in `ql` (the already-lowercased query).
    Used by select_fields as a lexical prior on top of embedding cosine (see
    its docstring) -- e.g. a query literally containing "range"/"mass" should
    never lose the "Range"/"Mass" fields to embedding dilution from other,
    more verbose parts of the same query."""
    words = [w for w in re.findall(r"[a-z0-9]+", field.lower())
             if len(w) > 2 and w not in _FIELD_NAME_STOPWORDS]
    return any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", ql)
              for w in words)


def select_fields(con, query, catalogue, k=15, always=(),
                  embed_model=DEFAULT_EMBED_MODEL):
    """Pick the catalogue fields most relevant to `query` (REVIEW_FINDINGS A2
    "catalogue-as-retrieval"): cosine-rank every embedded field descriptor
    (see _field_descriptor/load_field_embeddings) against the query embedding,
    take the top `k`, and UNION `always` (pass extract_filter_2pass's/
    catalogue.partition_fields' broad category fields here, so systemGroup/
    systemType/Country of origin are always offered even when the query's
    wording doesn't happen to echo them).

    Ranking is a small lexical+embedding fusion, not pure cosine: a field
    whose OWN NAME is literally named in the query (_field_name_hit) --
    "long range and small mass" names "Range"/"Mass" almost verbatim -- is
    ranked ahead of pure-embedding matches. This matters in practice: a WHOLE-
    QUERY sentence embedding is easily dominated by whichever part of the
    query has the most shared vocabulary with OTHER field names (e.g. "air-
    to-air missiles" pulling every armament-ish field name to the top of raw
    cosine, burying "Range"/"Mass" well past a k=12 cutoff even though the
    query names them outright). This is the same fusion spirit as retrieve()'s
    vector+FTS RRF -- cheap exact-mention signal on top of the semantic one,
    not a replacement for it (a query that DOESN'T name a field verbatim,
    e.g. "long-range", still falls through to pure cosine).

    This is a single-pass ALTERNATIVE to plain coverage-ordered pruning
    (catalogue_to_prompt's min_count/max_fields) -- it narrows the field SPEC
    shown to the model to the ~k fields the query is actually about instead of
    every reasonably-populated field, which matters most for small models on
    a large/heterogeneous catalogue. It is NOT a replacement for
    extract_filter_2pass: two-pass narrows by CATEGORY (which corpus subset)
    and recomputes coverage within it -- complementary, not redundant. (A
    future single-pass extension: pass category_stats/category_value once a
    category is otherwise known, e.g. from a pinned alias match -- not wired
    here, see extract_filter_2pass for where that's currently plumbed.)

    Falls back to the pre-A2 coverage-ordered top-k when the db has no
    field_embeddings (load_field_embeddings returns {}) -- e.g. a db ingested
    before this phase."""
    always = [f for f in always if f in catalogue]
    field_vecs = load_field_embeddings(con)
    if not field_vecs:
        # Same "top-k UNION always" shape as the embedding path below (just
        # ranked by coverage instead of relevance), so callers get a
        # consistently-sized spec regardless of which mode is active.
        cov_order = sorted(catalogue, key=lambda f: -(catalogue[f].get("count") or 0))
        picked = cov_order[:k]
        for f in always:
            if f not in picked:
                picked.append(f)
        return picked

    qvec = embed([query], embed_model)[0]
    fields = [f for f in field_vecs if f in catalogue]
    if not fields:
        return list(always)
    mat = np.vstack([field_vecs[f] for f in fields])
    sims = mat @ qvec  # both normalised -> cosine
    sims_by_field = dict(zip(fields, (float(s) for s in sims)))
    ql = (query or "").lower()
    ranked = sorted(fields,
                    key=lambda f: (-_field_name_hit(f, ql), -sims_by_field[f]))
    picked = ranked[:k]
    for f in always:
        if f not in picked:
            picked.append(f)
    return picked


def resolve_field_select_mode(con, field_select):
    """Resolve the `field_select` config knob ('embed' | 'coverage' | None) to
    a concrete mode. REVIEW_FINDINGS E2: ragkit.answer and compare_server's
    `_select_filter_fields` used to each inline their OWN version of this
    same "auto-detect if not specified" check (subtly differently -- see
    DEFAULTS' field_select entry for how that mattered when the config value
    is literally None rather than absent); this is the one shared
    implementation both now call.

    None means "auto": embed-rank fields by query relevance
    (REVIEW_FINDINGS A2) when this db actually has field_embeddings (see
    load_field_embeddings), else fall back to coverage-ordered pruning -- a db
    ingested before that phase has none, and 'embed' would have nothing to
    rank. An explicit 'embed' or 'coverage' passes through unchanged (a
    caller/CLI override always wins over the auto-detect)."""
    if field_select in ("embed", "coverage"):
        return field_select
    return "embed" if load_field_embeddings(con) else "coverage"


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


# --------------------------------------------------------------------------- #
# Retrieval caching (REVIEW_FINDINGS D1)                                       #
# --------------------------------------------------------------------------- #
#
# Every retrieve() call used to (a) load ALL passage embeddings from sqlite and
# vstack them, (b) separately re-scan rowid/parent_rid/rid for _eligible_rowids
# and the 'auto' filter-mode's matched-parent count, (c) issue one SELECT per
# rerank candidate for its text, and (d) issue one more SELECT per output row
# for its final text/title/parent -- 3-4 full table scans plus dozens of point
# queries, PER QUERY, even though the corpus only changes on re-ingest.
# _record_fields_map (record_params) had the same one-full-scan-per-call shape,
# and is itself called several times within a single request (filtering, the
# table, the digest).
#
# Both are cached here, in memory, keyed by db FILE PATH rather than by
# sqlite3.Connection -- compare_server gives every worker thread its own
# connection (threading.local), but they all read the same file, so a
# path-keyed cache lets every thread share ONE copy instead of each rebuilding
# its own. Invalidation is an mtime check on every access: ingest() rewrites
# the file, which bumps its mtime, so a stale cache is detected and rebuilt on
# the very next call -- no explicit "please invalidate" call is required for
# the normal re-ingest-then-query flow. invalidate_caches() is the seam for
# callers that want to force a reload without waiting on that check (e.g.
# tests, or an in-process ingest immediately followed by a query on the same
# mtime-resolution tick). Both caches are guarded by a lock since compare_server
# serves threaded=True and multiple worker threads can race to build the same
# entry; whichever thread wins the lock builds it once, the rest reuse it.

_passage_cache = {}       # abs db path -> dict, see _build_passage_cache_entry
_passage_cache_lock = threading.Lock()
_fields_cache = {}        # abs db path -> (mtime, {parent_rid: (title, fields)})
_fields_cache_lock = threading.Lock()


def _db_path_of(con):
    """The db FILE PATH backing `con`, for cache keys / mtime checks. PRAGMA
    database_list's first row is always 'main'; its 3rd column is the absolute
    path sqlite3 opened ('' for an anonymous/':memory:' db). Callers treat a
    falsy result (or ':memory:') as 'don't cache' (see _cacheable) -- a single
    shared key for every in-memory db would let unrelated ones collide."""
    row = con.execute("PRAGMA database_list").fetchone()
    return row[2] if row else None


def _cache_key(db_path):
    return os.path.abspath(db_path)


def _cacheable(db_path):
    return bool(db_path) and db_path != ":memory:"


def _build_passage_cache_entry(con, mtime):
    """One full pass over `records`, building every array/lookup retrieve()
    needs so it never has to query per-row again for this db version."""
    rows = con.execute(
        "SELECT rowid, parent_rid, rid, title, text, embedding "
        "FROM records").fetchall()
    rowids = np.array([r[0] for r in rows], dtype=np.int64)
    matrix = (np.vstack([unpack(r[5]) for r in rows]) if rows
              else np.zeros((0, EMBED_DIM), dtype=np.float32))
    return {
        "rowids": rowids,                              # ndarray[int], row i <-> matrix[i]
        "matrix": matrix,                               # ndarray[N, EMBED_DIM]
        "parent": {r[0]: (r[1] or r[2]) for r in rows},  # rowid -> parent_rid (coalesced)
        "rid": {r[0]: r[2] for r in rows},               # rowid -> passage rid
        "title": {r[0]: r[3] for r in rows},             # rowid -> title
        "text": {r[0]: r[4] for r in rows},              # rowid -> passage text
        "mtime": mtime,
    }


def _load_passage_cache(con, db_path):
    """{"rowids", "matrix", "parent", "rid", "title", "text", "mtime"} for
    every passage row (REVIEW_FINDINGS D1) -- see the section docstring above
    for the caching/invalidation contract. Not cached for an unresolvable or
    ':memory:' path (_cacheable) -- built fresh on every call in that case,
    i.e. the pre-caching behaviour, so an in-memory test db is always correct
    (just not sped up)."""
    try:
        mtime = os.path.getmtime(db_path) if db_path else None
    except OSError:
        mtime = None
    if not _cacheable(db_path):
        return _build_passage_cache_entry(con, mtime)
    key = _cache_key(db_path)
    entry = _passage_cache.get(key)
    if entry is not None and entry["mtime"] == mtime:
        return entry
    with _passage_cache_lock:
        entry = _passage_cache.get(key)  # re-check: another thread may have refreshed it
        if entry is not None and entry["mtime"] == mtime:
            return entry
        entry = _build_passage_cache_entry(con, mtime)
        _passage_cache[key] = entry
        return entry


def invalidate_caches(db_path=None):
    """Drop the passage + fields caches (REVIEW_FINDINGS D1's invalidation
    seam). Both already self-invalidate on the NEXT call whenever the db
    file's mtime has changed (covers a normal re-ingest), so this is only
    needed by a caller that wants to force a reload without waiting on that
    check. db_path=None clears every cached db; otherwise only that one."""
    with _passage_cache_lock, _fields_cache_lock:
        if db_path is None:
            _passage_cache.clear()
            _fields_cache.clear()
        else:
            key = _cache_key(db_path)
            _passage_cache.pop(key, None)
            _fields_cache.pop(key, None)


def prime_caches(con):
    """Populate the passage + fields caches for `con`'s db right now
    (REVIEW_FINDINGS D1), so the FIRST real query after boot is instant
    instead of paying the cold-scan cost on a user's click. Called by
    compare_server.warm_start(); safe to call again later -- it's just an
    mtime re-check that returns the already-cached entry when the db hasn't
    changed."""
    db_path = _db_path_of(con)
    _load_passage_cache(con, db_path)
    _record_fields_map(con)


def _build_record_fields_map(con):
    rows = con.execute(
        "SELECT parent_rid, title, fields_json FROM record_params").fetchall()
    return {parent_rid: (title, json.loads(fields_json) if fields_json else {})
            for parent_rid, title, fields_json in rows}


def _record_fields_map(con):
    """{parent_rid: (title, fields_dict)} for every ingested record's typed
    filter fields, read ONCE per record from record_params (REVIEW_FINDINGS B2)
    instead of rescanning a copy duplicated onto every passage row. Shared by
    _eligible_rowids/count_matches/field_coverage/record_table.

    Memoized per db path (REVIEW_FINDINGS D1), invalidated on file mtime
    change (see _load_passage_cache's docstring for why path-keyed rather
    than connection-keyed) -- record_params is rescanned once per db
    VERSION, not once per call, even though several calls typically happen
    within a single request (filtering, the table, the digest)."""
    _ensure_current_schema(con)
    db_path = _db_path_of(con)
    if not _cacheable(db_path):
        return _build_record_fields_map(con)
    try:
        mtime = os.path.getmtime(db_path)
    except OSError:
        mtime = None
    key = _cache_key(db_path)
    cached = _fields_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    with _fields_cache_lock:
        cached = _fields_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        result = _build_record_fields_map(con)
        _fields_cache[key] = (mtime, result)
        return result


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

_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?]+$")


def _normalize_label(s):
    """Casefold/whitespace/punctuation-normalize a label for tolerant
    comparison (REVIEW_FINDINGS A3 fast path): "USA", " usa", "USA." and "Usa"
    should all compare equal to each other before any fuzzy-matching cost is
    paid. Deliberately narrow (no stemming/synonyms) -- this is step 1 of 3;
    _LABEL_ABBREVIATIONS and difflib fuzzy matching in _map_label are the
    fallbacks for labels that aren't literally the same string modulo
    formatting."""
    s = re.sub(r"\s+", " ", str(s).strip().casefold())
    return _TRAILING_PUNCT_RE.sub("", s)


# Common abbreviation -> full-name expansions for the country/entity labels
# this domain actually uses. A SMALL model asked to fill "Country of origin"
# very often emits the abbreviation ("USA", "UK", "USSR") rather than the
# corpus's full-name label ("United States", "United Kingdom", "Soviet
# Union") -- and unlike a typo or punctuation/case variant, an abbreviation
# shares almost NO characters with its expansion (difflib.SequenceMatcher
# gives "usa" vs. "united states" a ratio of ~0.37, far below any cutoff that
# wouldn't also accept unrelated labels), so difflib genuinely cannot recover
# this class on its own. A short, fixed lookup is the right (bounded, still
# conservative -- exact keys only, no guessing) tool for it, tried BEFORE the
# fuzzy fallback. Deliberately scoped to this catalogue's actual values
# rather than an exhaustive world-country-code table.
_LABEL_ABBREVIATIONS = {
    "usa": "united states", "us": "united states",
    "u.s.": "united states", "u.s.a.": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom",
    "ussr": "soviet union", "cccp": "soviet union",
    "prc": "china", "uae": "united arab emirates",
    "rok": "south korea", "dprk": "north korea",
    "frg": "west germany", "gdr": "east germany",
}


def _map_label(value, allowed, cutoff=0.85):
    """Map a possibly-imprecise model-emitted label to a known catalogue label
    (REVIEW_FINDINGS A3): a small model asked to fill in "Country of origin"
    often emits "USA" when the catalogue's label is "United States", or just
    varies case/whitespace/punctuation ("Usa", "U.S.A"). validate_filter used
    to drop anything not an EXACT match, silently weakening the filter (the
    constraint just vanishes) even though the intent was obvious.

    Three tiers, cheapest and most-conservative first:
      1. exact match (the caller should already fast-path this -- see below --
         but it's harmless to re-check here);
      2. normalize both sides (_normalize_label) and compare;
      3. a known abbreviation expansion (_LABEL_ABBREVIATIONS) -- catches
         acronyms difflib structurally can't (see that dict's docstring);
      4. difflib.get_close_matches (stdlib, no network/model call) against the
         NORMALIZED allowed set with a conservative cutoff, so only a
         genuinely close label remaps -- an unrelated label (e.g. "France"
         when the model meant something else entirely) is left for the
         caller to drop, not silently coerced to the nearest thing.

    Returns the canonical label (a member of `allowed`) or None if nothing is
    close enough to trust."""
    if value in allowed:
        return value
    norm_value = _normalize_label(value)
    norm_to_canonical = {}
    for a in allowed:
        norm_to_canonical.setdefault(_normalize_label(a), a)
    if norm_value in norm_to_canonical:
        return norm_to_canonical[norm_value]
    expanded = _LABEL_ABBREVIATIONS.get(norm_value)
    if expanded and expanded in norm_to_canonical:
        return norm_to_canonical[expanded]
    close = difflib.get_close_matches(
        norm_value, list(norm_to_canonical.keys()), n=1, cutoff=cutoff)
    return norm_to_canonical[close[0]] if close else None


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
            # Exact matches are the fast path (REVIEW_FINDINGS A3): no fuzzy
            # cost at all when the model already emitted a valid label, which
            # is the common case. Only a label that ISN'T already valid pays
            # for normalization/difflib, and only labels still unmapped after
            # that get dropped.
            valid, remapped = [], {}
            for v in wanted:
                if v in allowed:
                    valid.append(v)
                    continue
                mapped = _map_label(v, allowed)
                if mapped is not None:
                    valid.append(mapped)
                    remapped[v] = mapped
                else:
                    errors.append(f"{field}='{v}' not a known label, dropped")
            if valid:
                entry = {"type": "categorical", "in": valid}
                if remapped:
                    entry["_remapped"] = remapped  # audit trail (mirrors numeric _converted)
                clean[field] = entry
        elif t == "multi_value":
            allowed = set(spec["values"])
            wanted = cond.get("contains") or cond.get("in") or (
                [cond["eq"]] if "eq" in cond else [])
            valid, remapped = [], {}
            for v in wanted:
                if v in allowed:
                    valid.append(v)
                    continue
                mapped = _map_label(v, allowed)
                if mapped is not None:
                    valid.append(mapped)
                    remapped[v] = mapped
                else:
                    errors.append(f"{field} contains '{v}' not a known element, dropped")
            if valid:
                entry = {"type": "multi_value", "contains": valid}
                if remapped:
                    entry["_remapped"] = remapped
                clean[field] = entry
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
    per-record (not per-passage) computation. Returns None if no filter.

    Reads the rowid->parent map from the passage cache (REVIEW_FINDINGS D1)
    instead of a dedicated 'SELECT rowid, parent_rid, rid FROM records' scan --
    this doubles as the cache-warming call, since it runs first thing in
    retrieve(), so retrieve()'s own _load_passage_cache call right after is
    always a cheap hit for that request."""
    eligible_parents = _eligible_parents(con, clean_filter)
    if eligible_parents is None:
        return None
    cache = _load_passage_cache(con, _db_path_of(con))
    return {rowid for rowid, parent in cache["parent"].items()
            if parent in eligible_parents}


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
    as extract_filter) and a small diagnostics dict. Costs two LLM round-trips.

    info also carries the parse/extraction outcome of BOTH passes
    (REVIEW_FINDINGS A5 -- see extract_filter_ex): "pass1_extraction"/
    "pass2_extraction" individually, plus a single "extraction" summarising
    both ("parse_failed" if either pass failed to parse, else "ok" if either
    produced a non-empty filter, else "empty") so a caller can read
    info["extraction"] the same way regardless of whether it used the
    single-pass or two-pass extractor."""
    info = {"two_pass": True}
    part = cat_mod.partition_fields(catalogue, min_count=min_count)
    info["pass1_fields"] = len(part)

    # ---- pass 1: which broad category / categories? ----
    cat_raw, pass1_status = {}, "empty"
    if part:
        cat_raw, ex1 = extract_filter_ex(query, catalogue, backend, model, base_url,
                                         only_fields=set(part))
        pass1_status = ex1["status"]
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

    # ---- category-scoped numeric stats (REVIEW_FINDINGS A4): once pass 1 has
    # narrowed to exactly one CATEGORY_STAT_FIELD value, show pass 2 that
    # category's own min/p5/median/p95/max instead of the corpus-wide spread
    # (catalogue.build_category_stats / catalogue_to_prompt's category_stats
    # kwarg) -- calibrates "long range" against the ~10-100km an AAM actually
    # spans, not the 8-11,000km cartridge-to-carrier global range. A future
    # single-pass extension could do the same once a category is otherwise
    # pinned (e.g. via an alias match); not wired there yet -- see
    # select_fields' docstring.
    category_value = None
    st_cond = cat_clean.get(CATEGORY_STAT_FIELD)
    if (st_cond and st_cond.get("type") == "categorical"
            and len(st_cond.get("in") or []) == 1):
        category_value = st_cond["in"][0]
    category_stats = None
    if category_value:
        cs = load_category_stats(con)
        if cs.get("field") == CATEGORY_STAT_FIELD:
            category_stats = cs.get("stats")
    info["pass2_category"] = category_value

    # ---- pass 2: full filter over the narrowed, in-category field set ----
    det_raw, ex2 = extract_filter_ex(
        query, catalogue, backend, model, base_url,
        only_fields=detail, min_count=min_count,
        category_stats=category_stats, category_value=category_value)
    info["pass1_extraction"] = pass1_status
    info["pass2_extraction"] = ex2["status"]
    statuses = {pass1_status, ex2["status"]}
    info["extraction"] = ("parse_failed" if "parse_failed" in statuses else
                          "ok" if "ok" in statuses else "empty")
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
             matched_parents=None, pin_entities=None, min_rel=0.0):
    """Hybrid vector + FTS5 retrieval, RRF-fused, optionally cross-encoder
    reranked -- plus (REVIEW_FINDINGS G1/G2/G3) deterministic entity pinning,
    balanced multi-entity comparisons, and relevance-floor adaptive packing.

    pin_entities (G1 consume, default None): parent_rids to GUARANTEE appear
    in the output (their single best-matching passage each), computed by the
    CALLER via match_entities(query, load_aliases(con)) -- embeddings are the
    weakest tool for exact designations ("AIM-120" vs "Slammer"), so a named
    lookup shouldn't have to rely on ranking alone. An entity already ranked
    in on its own merit is left alone (nothing to inject); one that isn't is
    INJECTED before the k-slot assembly loop below (so it competes for a slot
    like everything else -- respecting max_per_parent/k, see entity_cap) and,
    only for that forced injection, bypasses the min_rel floor (the whole
    point of pinning is that a correct match can score low on embedding/
    rerank similarity -- an entity that already ranked in on its own merit
    has no need for the exemption). An entity that fails an ACTIVE HARD
    filter (filter_mode resolves to 'hard' and none of the entity's own
    passages are in the eligible set) is SKIPPED entirely, not force-injected
    -- pinning augments ranking, it must never bypass a validated filter
    gate. Under 'soft'/'fill'/auto-resolved-to-'fill'/'off', the filter isn't
    a hard gate, so pinning always proceeds.

    When >=2 distinct entities in pin_entities are valid (REVIEW_FINDINGS
    G2 -- a comparison, e.g. "Compare the F-22 and F-35"), no single one may
    claim more than ceil(k / n_pinned) of the k slots, so whichever entity
    ranks/embeds better can't fill every slot and starve the other side.
    This applies whether or not each entity needed forced injection --
    "Compare X and Y" typically has BOTH already ranked in (the query
    literally names them), so the cap can't depend on injection having
    fired, only on how many distinct named entities are genuinely in play.
    At the library default max_per_parent=1 this is already a no-op (one
    parent can never get more than 1 slot regardless of entity_cap), but it
    matters once a caller raises max_per_parent for deeper per-entity
    comparisons.

    min_rel (G3, default 0.0): a floor in [0,1] on the NORMALIZED relevance
    score (`rel` -- normalized RRF score, upgraded to normalized cross-
    encoder score post-rerank; see _normalize_scores) below which a
    (non-pinned) candidate is skipped rather than padded into the k slots.
    k stays a MAX, not a target: with min_rel=0.0 (default) every candidate
    clears the floor (the worst-scored candidate normalizes to exactly 0.0,
    and the check is strict '<'), so behaviour is IDENTICAL to before this
    floor existed -- exactly top-k, same as always. A caller that wants
    adaptive packing (fewer than k when the tail is noise, or a strong
    k-1/k passage kept where a weak one used to pad it out) passes a modest
    floor (e.g. min_rel=0.2) instead of changing the default.

    max_context_tokens (unchanged contract, REVIEW_FINDINGS B1): a SOFT,
    passages-ONLY cap applied here via per-passage truncation
    (_truncate_to_tokens, which always preserves 'Parameter ' lines). This is
    NOT the global prompt budget -- a caller that also renders a table/digest
    should drive the real, whole-prompt budget through build_prompt's own
    max_context_tokens (see assemble_context), which allocates ONE budget
    across passages/table/digest and is authoritative for what actually
    reaches the model. Passing a cap here too is harmless (just a tighter,
    passages-only pre-cap that assembly's budget then further respects) --
    None (default) means retrieve() returns full-size passages, which is what
    this phase's callers (ragkit.answer / compare_server.build_context) now
    do, so ALL budgeting happens exactly once, in assembly.

    Passage embeddings + metadata come from the in-memory cache built by
    _load_passage_cache (REVIEW_FINDINGS D1) instead of a fresh full-table
    scan plus N+1 point queries every call; see that function's docstring.
    """
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
    cache = _load_passage_cache(con, _db_path_of(con))
    mode = "off" if eligible is None else filter_mode
    if mode == "auto":
        if matched_parents is None:
            matched_parents = len({p for rid, p in cache["parent"].items()
                                   if rid in eligible})
        threshold = guard_min if guard_min is not None else k
        mode = "hard" if matched_parents > threshold else "fill"
    restrict = (mode == "hard")

    # --- vector side ---
    qvec = embed([query], embed_model)[0]
    all_rowids, all_matrix = cache["rowids"], cache["matrix"]
    if all_rowids.size == 0:
        return []
    rowid_list = all_rowids.tolist()
    if restrict:
        mask = np.array([rid in eligible for rid in rowid_list], dtype=bool)
        ids = [rid for rid, keep in zip(rowid_list, mask) if keep]
        mat = all_matrix[mask]
        if not ids:
            return []  # hard filter excluded everything; caller decides on fallback
    else:
        ids, mat = rowid_list, all_matrix
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
    # cross-encoder score after rerank. This is the basis the soft filter adds to,
    # and (REVIEW_FINDINGS G3) the basis the min_rel floor gates on.
    rel = _normalize_scores({rid: scores[rid] for rid in ranked})

    # --- cross-encoder rerank over a wider candidate pool ---
    # RRF gives a fast, approximate ordering; rescoring its top candidates with a
    # cross-encoder (query + passage judged together) is much more precise at
    # surfacing the passages actually about the question. Degrades to RRF order
    # if the reranker can't load (e.g. offline first run).
    if rerank and len(ranked) > 1:
        cand = ranked[:rerank_pool]
        text_by_id = {rid: cache["text"][rid] for rid in cand if rid in cache["text"]}
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

    # --- entity pinning (G1 consume) + balanced multi-entity split (G2) ---
    pinned_rowid_set, pinned_parent_set, entity_cap = set(), set(), None
    if pin_entities:
        parent_to_rowids = {}
        for rid, p in cache["parent"].items():
            parent_to_rowids.setdefault(p, []).append(rid)
        idx_of = {rid: i for i, rid in enumerate(rowid_list)}

        def _best_rowid(rowids):
            # The entity's own best-matching passage (highest cosine to the
            # query) -- reuses qvec/all_matrix already in hand, no extra query.
            return max(rowids, key=lambda rid: (float(all_matrix[idx_of[rid]] @ qvec)
                                                if rid in idx_of else -1.0))

        have_parents = {cache["parent"].get(rid) for rid in ranked}
        pinned_rowids = []
        # Every pin_entities parent that's VALID (has passages, and passes an
        # active hard filter) counts toward the comparison split (G2) even if
        # it didn't need forced injection below -- "Compare the F-22 and
        # F-35" typically has BOTH already ranked in on their own merit (the
        # query literally names them), so entity_cap must not depend on
        # whether injection specifically fired, only on how many distinct
        # named entities are genuinely in play.
        valid_pinned_parents = []
        for parent in pin_entities:
            candidates = parent_to_rowids.get(parent, [])
            if mode == "hard" and eligible is not None:
                # A hard filter is an explicit gate; pinning augments ranking,
                # it must never bypass a validated filter (don't weaken
                # filter semantics). An entity that fails the active hard
                # filter is skipped here, not force-injected.
                candidates = [rid for rid in candidates if rid in eligible]
            if not candidates:
                continue  # unknown parent, or it fails the active hard filter
            valid_pinned_parents.append(parent)
            if parent in have_parents:
                continue  # already ranked in on its own merit -- nothing to INJECT
            pinned_rowids.append(_best_rowid(candidates))
            have_parents.add(parent)
        if pinned_rowids:
            seen = set(pinned_rowids)
            ranked = pinned_rowids + [rid for rid in ranked if rid not in seen]
            pinned_rowid_set = seen
        if len(valid_pinned_parents) >= 2:
            pinned_parent_set = set(valid_pinned_parents)
            entity_cap = math.ceil(k / len(pinned_parent_set))

    # --- assemble output: passage-level, diversified + cited by parent ---
    # Passages are retrieved individually (better matching on long docs, and a
    # hit is one bounded passage rather than a whole page), but we cap passages
    # per source so one document can't monopolise the k slots (tighter still,
    # per pinned entity, for comparisons -- entity_cap above), and we cite the
    # parent record so citations stay record-level and readable.
    out = []
    used_tokens = 0
    per_parent = {}
    for rowid in ranked:
        if len(out) >= k:
            break
        if rowid not in cache["text"]:
            continue
        if (rowid not in pinned_rowid_set and min_rel > 0.0
                and rel.get(rowid, 0.0) < min_rel):
            continue  # G3: below the relevance floor -- don't pad in noise
        parent = cache["parent"][rowid]
        cap = max_per_parent
        if entity_cap is not None and parent in pinned_parent_set:
            cap = min(max_per_parent, entity_cap)
        if per_parent.get(parent, 0) >= cap:
            continue
        text = cache["text"][rowid]
        if max_context_tokens is not None:
            # rough token estimate ~ chars/4; truncate body, always keep params line
            block_tokens = _est_tokens(text)
            if used_tokens + block_tokens > max_context_tokens and out:
                break  # budget hit and we already have at least one passage
            text = _truncate_to_tokens(text, max_context_tokens - used_tokens)
            used_tokens += _est_tokens(text)
        per_parent[parent] = per_parent.get(parent, 0) + 1
        out.append({"rid": parent, "passage": cache["rid"][rowid],
                    "title": cache["title"][rowid], "text": text})
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
    first, capped at `limit`, excluding parent rids in `exclude`.

    Reads passage embeddings/metadata from the shared cache (REVIEW_FINDINGS
    D1) instead of its own dedicated full-table scan -- this used to be one
    of the 3-4 redundant full scans a single request could trigger (alongside
    retrieve()'s own, now-cached, scan)."""
    eligible = _eligible_rowids(con, clean_filter)
    if not eligible:
        return []
    exclude = exclude or set()
    qvec = embed([query], embed_model)[0]
    cache = _load_passage_cache(con, _db_path_of(con))
    idx_of = {rid: i for i, rid in enumerate(cache["rowids"].tolist())}
    elig_rowids = [rid for rid in eligible if rid in idx_of]
    if not elig_rowids:
        return []
    sims = cache["matrix"][[idx_of[rid] for rid in elig_rowids]] @ qvec
    best = {}  # parent -> (score, title, snippet)
    for rowid, score in zip(elig_rowids, sims):
        parent = cache["parent"][rowid]
        if parent in exclude:
            continue
        score = float(score)
        if parent not in best or score > best[parent][0]:
            best[parent] = (score, cache["title"][rowid],
                            _snippet(cache["text"][rowid], snippet_chars))
    ordered = sorted(best.items(), key=lambda kv: -kv[1][0])[:limit]
    return [{"rid": p, "title": t, "snippet": s} for p, (_sc, t, s) in ordered]


# Tightened for REVIEW_FINDINGS A7: the old regex included bare "which| all|
# over|under|above|below|each", which fired on plain single-entity factual
# questions ("Which country designed the F-16?", "What is the range of X
# over its lifetime?") and injected a full structured table for no reason
# (token cost, see REVIEW_FINDINGS B1). This version only matches words that
# genuinely imply reasoning across MULTIPLE records: superlatives (longest,
# heaviest, ...), explicit comparison/ranking/listing/counting, and
# aggregates (average/mean/median/total). A bare numeric comparison word
# ("over"/"under"/"between") is deliberately NOT here -- a genuine numeric
# constraint is instead caught structurally, via clean_filter carrying a
# numeric field (see is_analytic_query below), not by guessing from wording.
_ANALYTIC_RE = re.compile(
    r"\b(compare|comparison|versus|vs|rank(?:ed|ing)?|sort(?:ed)?|"
    r"list\s+(?:all|the|every)|how\s+many|number\s+of|count\s+of|"
    r"most|least|longest|shortest|highest|lowest|heaviest|lightest|"
    r"fastest|slowest|largest|smallest|greatest|"
    r"average|mean|median|total|top\s*\d+)\b", re.I)

# A stronger subset of the above: signals that are explicitly about MULTIPLE
# records regardless of how many named entities appear in the query. Used to
# override the single-named-entity suppression below -- "List all specs of
# the AIM-120" should still get a table (it's an explicit "give me
# everything" ask), even though only one entity is named.
_MULTI_RECORD_RE = re.compile(
    r"\b(compare|comparison|versus|vs|rank(?:ed|ing)?|"
    r"list\s+(?:all|the|every)|how\s+many|number\s+of|count\s+of|each\s+of|"
    r"all\s+the)\b", re.I)


def _alias_pattern(alias):
    """Compile `alias` (expected already-lowercase) as a token-boundary-
    anchored regex (REVIEW_FINDINGS G1): "f-16" must match in "the f-16
    fighter" but NOT inside "f-160" or "af-16x" -- the (?<![a-z0-9])/
    (?![a-z0-9]) anchors do that without a full tokenizer. Shared by
    _matched_entities (is_analytic_query's single-entity suppression) and
    match_entities (retrieval pinning) so the anchor logic can't drift
    between the two call sites that both need it."""
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")


def _matched_entities(query, aliases):
    """The set of parent_rids named by ANY alias found in `query` (whole-word/
    phrase, case-insensitive). Used by is_analytic_query to detect "this query
    names exactly one known entity" (REVIEW_FINDINGS A7). Cheap: the alias
    table is built once at ingest (record_model.build_alias_table); this is a
    handful of regex scans over one short query string per call, not a corpus
    scan, so it's fine at this corpus's scale (hundreds of aliases)."""
    if not aliases or not query:
        return set()
    ql = query.lower()
    matched = set()
    for alias, rids in aliases.items():
        if _alias_pattern(alias).search(ql):
            matched.update(rids)
    return matched


def match_entities(query, aliases):
    """Which parent_rids are named by an alias in `query`, for deterministic
    retrieval PINNING (REVIEW_FINDINGS G1 consume -- see retrieve()'s
    pin_entities kwarg). Embeddings are the weakest tool for exact
    designations ("AIM-120" vs "AIM-120 AMRAAM" vs "Slammer"), so a
    named-entity query shouldn't have to rely on ranking alone to surface its
    record -- this is the deterministic half of that, computed by the CALLER
    (ragkit.answer / compare_server.build_context, which already have
    load_aliases(con) in hand) and handed to retrieve() so the function stays
    testable without a live db.

    Longest-alias-first: aliases are tried longest to shortest, so a more
    specific match (the full title, or a multi-word popular name) is recorded
    -- and therefore PRIORITIZED in the returned order -- ahead of a shorter
    alias that happens to also match. What actually PREVENTS a substring
    false-hit ("f-16" firing inside "f-160") is the word-boundary anchor
    (_alias_pattern), not the ordering -- the ordering is purely about which
    genuine match wins priority when k is tight and not every pin fits.

    Returns parent_rids in first-matched order, deduplicated (a dict's key
    order is insertion order in Python 3.7+, so ties at the same alias length
    fall back to the alias table's own order). [] if `aliases`/`query` is
    falsy or nothing matches -- retrieve()'s pin_entities treats that the
    same as "no pinning requested".

    >>> match_entities("What is the range of the AIM-120?",
    ...                {"aim-120": ["2006"], "f-16": ["2025"]})
    ['2006']
    """
    if not aliases or not query:
        return []
    ql = query.lower()
    rids = []
    for alias in sorted(aliases.keys(), key=len, reverse=True):
        if _alias_pattern(alias).search(ql):
            for rid in aliases[alias]:
                if rid not in rids:
                    rids.append(rid)
    return rids


def is_analytic_query(query, clean_filter, aliases=None):
    """Heuristic: does the question want to compare/aggregate across the
    matched set (-> structured table), rather than a prose 'why/how' answer
    about one thing? True if the filter constrains a numeric field, or the
    question uses comparison/superlative/aggregate language (see _ANALYTIC_RE)
    -- UNLESS the query names exactly one known entity (via the alias table,
    optional `aliases` kwarg -- ragkit.load_aliases) and isn't an explicit
    multi-record ask (_MULTI_RECORD_RE), in which case it's almost always a
    single-record factual lookup that a superlative-sounding word slipped
    into, not a "reason across many records" question (REVIEW_FINDINGS A7).
    `aliases` is optional (default None -> no suppression, matching the old
    regex-only behaviour) so existing positional callers keep working.

    >>> is_analytic_query("What is the range of the AIM-120?", {})
    False
    >>> is_analytic_query("Which country designed the F-16?", {})
    False
    >>> is_analytic_query("Tell me about the T-90", {})
    False
    >>> is_analytic_query("Which air-to-air missile has the longest range?", {})
    True
    >>> is_analytic_query("Compare the F-22 and F-35", {})
    True
    >>> is_analytic_query("List all US fighter aircraft", {})
    True
    >>> is_analytic_query("How many tanks weigh over 50 tonnes?", {})
    True
    """
    numeric_filter = bool(clean_filter and any(
        isinstance(c, dict) and c.get("type") == "numeric"
        for c in clean_filter.values()))
    signal = numeric_filter or bool(_ANALYTIC_RE.search(query or ""))
    if not signal:
        return False
    if aliases:
        entities = _matched_entities(query, aliases)
        if len(entities) == 1 and not _MULTI_RECORD_RE.search(query or ""):
            return False  # single-entity lookup, not a multi-record ask
    return True


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


def _render_table_text(table, token_budget=None):
    """Render the structured table for the prompt: one line per record, each
    field as value + unit + (description), so the model sees the full field.

    token_budget (REVIEW_FINDINGS B1, optional): if given and the full render
    exceeds it, shrink in two stages, cheapest/least-essential loss first:
      1. drop every cell's `descr` text (often the biggest single chunk of a
         table -- up to ~180 chars x 6 cols x 40 rows -- and the least
         essential: the value+unit alone still answers most questions);
      2. if STILL over budget, drop rows from the BOTTOM (rows are already
         sorted by the numeric column, or title -- see record_table -- so a
         "longest N" style question keeps its most relevant rows first).
    The '(+N more not shown)' marker always reflects the TRUE number of rows
    not rendered -- original total minus rows actually kept -- whether they
    were excluded upstream (record_table's own `limit`) or dropped here for
    budget, so the model is never told a false count. None (default,
    unbounded) reproduces the exact pre-B1 render: full descriptions, every
    row record_table returned."""
    cols = table["columns"]

    def render(rows, with_descr):
        lines = [f"Structured fields for all {table['total']} matched records "
                 f"(field=value unit (description)):"]
        for r in rows:
            segs = []
            for col in cols:
                c = r["cells"].get(col) or {}
                if c.get("value") in (None, ""):
                    continue
                seg = f"{col}={c['value']}"
                if c.get("unit"):
                    seg += f" {c['unit']}"
                descr = c.get("descr")
                if descr and with_descr:
                    # full descr is kept in the API/UI cell; cap it here so the
                    # in-prompt table stays token-bounded across many rows/cols.
                    if len(descr) > 180:
                        descr = descr[:180].rstrip() + "…"
                    seg += f" ({descr})"
                segs.append(seg)
            lines.append(f"[{r['rid']}] {r['title']}: " + "; ".join(segs))
        if table["total"] > len(rows):
            lines.append(f"(+{table['total'] - len(rows)} more not shown)")
        return "\n".join(lines)

    rows = table["rows"]
    text = render(rows, with_descr=True)
    if token_budget is None or _est_tokens(text) <= token_budget:
        return text

    # stage 1: drop descr text (cheapest content loss, usually the biggest saving)
    text = render(rows, with_descr=False)
    if _est_tokens(text) <= token_budget:
        return text

    # stage 2: binary-search the most rows (from the top, preserving sort
    # order) that fit the budget without descr text. Always keep at least the
    # top row -- the model should see SOMETHING concrete even under a very
    # tight budget, rather than a table that's all header + "(+N more)".
    lo, hi = 1, len(rows)
    best = render(rows[:1], with_descr=False)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = render(rows[:mid], with_descr=False)
        if _est_tokens(candidate) <= token_budget:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _render_digest_text(digest, token_budget=None):
    """Render the digest block for the prompt: one relevance-ranked snippet
    per record beyond the top-k passages (see record_digest).

    token_budget (REVIEW_FINDINGS B1, optional): if given and the full render
    exceeds it, drop entries from the BOTTOM (digest is already relevance-
    ordered) until it fits, appending a truthful '(+N more not shown)' note --
    the unbounded render never carries one (record_digest's own `limit` is
    the only cap today), so this note only appears when assembly itself had
    to cut further. None (default, unbounded) reproduces the exact pre-B1
    render."""
    header = ("Additional records matching the same metadata filter (one "
              "relevance snippet each; these are partial — note if a "
              "snippet is insufficient to answer):")

    def render(entries, n_dropped):
        lines = [f"[{d['rid']}] {d['title']} — {d['snippet']}" for d in entries]
        if n_dropped:
            lines.append(f"(+{n_dropped} more not shown)")
        return header + "\n" + "\n".join(lines)

    text = render(digest, 0)
    if token_budget is None or _est_tokens(text) <= token_budget:
        return text

    lo, hi = 1, len(digest)
    best = render(digest[:1], len(digest) - 1)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = render(digest[:mid], len(digest) - mid)
        if _est_tokens(candidate) <= token_budget:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _pack_passages(contexts, token_budget=None):
    """Render + pack passage blocks (REVIEW_FINDINGS B1's passage share of the
    global budget) in the given order (already ranked/pinned/floored by
    retrieve()) up to `token_budget`: per-passage truncation via
    _truncate_to_tokens (always keeps 'Parameter ' lines -- see its
    docstring), and once a passage doesn't fit even minimally truncated,
    later ones are dropped entirely rather than emitted as useless empty
    fragments (mirrors retrieve()'s own former inline loop, just running here
    against the FULL passages retrieve() now returns to assembly).
    token_budget=None (default) means unbounded -- every passage in full,
    nothing truncated or dropped (today's pre-B1 behaviour)."""
    blocks = [f"[{c['rid']}] {c['title']}\n{c['text']}" for c in contexts]
    if token_budget is None:
        return "\n\n---\n\n".join(blocks)
    kept = []
    used = 0
    for block in blocks:
        block_tokens = _est_tokens(block)
        if used + block_tokens > token_budget:
            if kept:
                break  # budget hit and we already have at least one passage
            # always keep at least the first passage, truncated as best we
            # can -- _truncate_to_tokens floors token_budget<=0 to 64 itself.
            block = _truncate_to_tokens(block, token_budget - used)
            block_tokens = _est_tokens(block)
        kept.append(block)
        used += block_tokens
    return "\n\n---\n\n".join(kept)


def _allocate_budget(needs, weights, total):
    """Water-fill `total` tokens across sections (REVIEW_FINDINGS B1's core
    mechanic): each section's NOMINAL share is total*weights[section], but a
    section that needs less than its share gives the difference back to the
    pool, which is re-split (by weight) among the sections still short of
    their (possibly-updated) share -- so unused budget is never wasted on a
    section that doesn't need it (e.g. a short-passage / big-table query lets
    the table use almost the whole budget instead of being capped at its
    nominal 35%).

    `needs` and `weights` are dicts keyed identically (one entry per
    section); a section absent from the final corpus (e.g. no digest) should
    still be passed with needs[section]=0, which fixes it to budget 0
    immediately and frees its whole weight-share to the others on the very
    first pass.

    Converges in at most len(needs) passes: each pass either fixes at least
    one more section's final budget (moving it from `remaining` to done) or
    every remaining section gets exactly its current share and the loop
    ends -- so it always terminates, and every fixed budget is <= that
    section's own need (never over-allocated beyond what it can use).
    Returns {section: tokens}."""
    fixed = {}
    remaining = dict(needs)
    pool = total
    while remaining:
        w_sum = sum(weights[s] for s in remaining) or 1.0
        share = {s: pool * (weights[s] / w_sum) for s in remaining}
        satisfied = [s for s in remaining if needs[s] <= share[s]]
        if not satisfied:
            # nobody left is fully covered by their proportional share --
            # everyone remaining gets exactly that share; done.
            fixed.update(share)
            break
        for s in satisfied:
            fixed[s] = needs[s]
            pool -= needs[s]
            del remaining[s]
    return fixed


def assemble_context(query, contexts, table=None, digest=None,
                     max_context_tokens=None, alloc=(0.50, 0.35, 0.15)):
    """THE B1 fix: build_prompt used to hand back full-size table/digest text
    with NO budget at all -- only retrieve()'s passages were ever capped by
    max_context_tokens (see retrieve()'s own, separate, passages-only cap).
    A broad filter + analytic query could blow the configured budget 4-5x via
    the table alone (up to ~40 rows x 6 cols x ~200-char descr each). This
    function is the ONE place all three variable sections of the prompt
    (passages / table / digest) are traded off against a SINGLE total budget.

    Sections get a nominal share of `max_context_tokens` (`alloc` = passages/
    table/digest weights, default 50/35/15 -- passages carry the actual prose
    the model reasons over, so they get the largest default share; table/
    digest are supplementary structure/breadth), but a section needing LESS
    than its share gives the unused tokens back to the others via water-
    filling (_allocate_budget) instead of wasting them.

    Shrink order per section (cheapest/least-essential first) -- see each
    render function's own docstring for the mechanics:
      - table: drop descr text, then drop rows from the bottom
        (_render_table_text).
      - digest: drop entries from the bottom (_render_digest_text).
      - passages: per-passage truncation (_truncate_to_tokens, via
        _pack_passages), dropping whole passages from the bottom once even a
        minimal truncation won't fit.

    max_context_tokens=None (default) means unbounded: every section renders
    in full, nothing is dropped or truncated -- the EXACT pre-B1-fix output,
    so a caller that doesn't pass a budget sees zero behaviour change.

    Returns (ctx_text, table_text_or_None, digest_text_or_None) -- the exact
    text build_prompt slots into its output (block separators, headers, etc.
    are unchanged from before this function existed; see _pack_passages/
    _render_table_text/_render_digest_text)."""
    table_text_full = _render_table_text(table) if (table and table.get("rows")) else None
    digest_text_full = _render_digest_text(digest) if digest else None

    if max_context_tokens is None:
        return _pack_passages(contexts), table_text_full, digest_text_full

    blocks = [f"[{c['rid']}] {c['title']}\n{c['text']}" for c in contexts]
    needs = {
        "passages": sum(_est_tokens(b) for b in blocks),
        "table": _est_tokens(table_text_full) if table_text_full else 0,
        "digest": _est_tokens(digest_text_full) if digest_text_full else 0,
    }
    weights = dict(zip(("passages", "table", "digest"), alloc))
    budgets = _allocate_budget(needs, weights, max_context_tokens)

    ctx_text = _pack_passages(contexts, budgets["passages"])
    table_text = (_render_table_text(table, token_budget=budgets["table"])
                  if table_text_full else None)
    digest_text = (_render_digest_text(digest, token_budget=budgets["digest"])
                  if digest_text_full else None)
    return ctx_text, table_text, digest_text


def _est_tokens(s):
    return max(1, len(s) // 4)  # crude but fine for budgeting


def _truncate_to_tokens(text, token_budget):
    """Truncate free-text but preserve parametric lines (high-value, tiny).
    Parameter lines start with 'Parameter ' from record_model.to_text().

    REVIEW_FINDINGS D3 fix: the previous version appended a truncated
    remainder FRAGMENT to `kept`, then rebuilt the output by re-filtering the
    ORIGINAL lines for membership in `kept` -- since the fragment's text
    differs from the original line (it's shorter, with a trailing '…'), it
    could never match by value, so the fragment was silently dropped every
    time a line needed truncating (not replaced by it -- lost outright, with
    nothing in its place). That membership check was also O(n^2) and
    conflated two identical lines into one slot. Fixed by tracking
    {original_index: text} and re-assembling by sorted index, so a truncated
    fragment keeps its place and its own identity regardless of whether an
    identical line exists elsewhere in the text."""
    if token_budget <= 0:
        token_budget = 64
    char_budget = token_budget * 4
    if len(text) <= char_budget:
        return text
    lines = text.split("\n")
    param_idx = [i for i, l in enumerate(lines) if l.startswith("Parameter ")]
    other_idx = [i for i, l in enumerate(lines) if not l.startswith("Parameter ")]

    # params are always kept whole, in full, regardless of budget
    kept = {i: lines[i] for i in param_idx}
    budget_left = char_budget - sum(len(t) + 1 for t in kept.values())
    for i in other_idx:
        l = lines[i]
        if budget_left <= 0:
            break
        if len(l) + 1 <= budget_left:
            kept[i] = l
            budget_left -= len(l) + 1
        else:
            kept[i] = l[:budget_left].rstrip() + "…"
            break
    ordered = [kept[i] for i in sorted(kept)]  # restore original line order
    return "\n".join(ordered)


# --------------------------------------------------------------------------- #
# Generation backends                                                          #
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = (
    "You answer strictly from the provided context records. "
    "Cite the record id(s) you used in square brackets. "
    "If the answer is not in the context, say you don't have that information."
)

# REVIEW_FINDINGS E3: when build_prompt includes a structured fields table
# (analytic queries -- see record_table/is_analytic_query), a small model will
# sometimes still answer from a PASSAGE's prose instead -- e.g. an older or
# rounded figure repeated in a description sentence -- even when it disagrees
# with the table's freshly-computed exact value. The table is the
# authoritative source for those fields; prose is not. One extra sentence,
# added ONLY when a table is actually present, fixes this cheaply.
#
# Kept as a SEPARATE constant rather than mutating SYSTEM_PROMPT in place so
# (a) existing callers that import SYSTEM_PROMPT directly see no change, and
# (b) it's obvious from the name alone which variant a given call site uses.
# system_prompt_for(table) below is the one place that decides between them;
# use it (not this constant directly) unless you specifically need the
# no-table wording.
SYSTEM_PROMPT_WITH_TABLE = (
    SYSTEM_PROMPT + " When a structured fields table is included below, treat "
    "it as the authoritative source for exact field values -- prefer it over "
    "any conflicting figure mentioned in the prose passages."
)


def system_prompt_for(table):
    """Pick the system-prompt variant for THIS request (REVIEW_FINDINGS E3):
    the table-authority sentence only when a table is actually present in the
    prompt, since it would otherwise be a dangling reference to something the
    model never sees. `table` is the same value build_prompt's own `table`
    argument receives (record_table's dict, or None/falsy) -- using the exact
    same truthiness check build_prompt uses to decide whether to render a
    table section at all, so the two decisions can never drift apart."""
    return SYSTEM_PROMPT_WITH_TABLE if table else SYSTEM_PROMPT


def build_prompt(query, contexts, digest=None, table=None, max_context_tokens=None):
    """Assemble the final prompt text: context passages, optional structured
    table (analytic queries), optional digest (roster of the rest of a large
    filtered set), then the question.

    max_context_tokens (REVIEW_FINDINGS B1, default None = unbounded, same as
    before this kwarg existed): the ONE global budget covering every variable
    part of this prompt (passages + table + digest combined), delegated to
    assemble_context -- see its docstring for the allocation/shrink strategy.
    This is the fix for the flagship context-budgeting bug: previously only
    retrieve()'s passages were ever capped, so a broad filter + analytic
    query's table/digest could blow the configured budget several times
    over with nothing to stop it."""
    ctx_text, table_text, digest_text = assemble_context(
        query, contexts, table=table, digest=digest,
        max_context_tokens=max_context_tokens)
    parts = [f"Context records:\n\n{ctx_text}"]
    # Structured backbone: exact fields for every matched record (analytic
    # queries). Placed before the digest since it's the source of truth for
    # values; the top-k passages above give prose depth.
    if table_text:
        parts.append(table_text)
    # When a metadata filter matched more records than fit in the top-k full
    # passages, append a compact roster of the rest (one relevance snippet each)
    # so the model can reason over the whole matched set, not just the top few.
    if digest_text:
        parts.append(digest_text)
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
                 max_tokens=None, response_format=None):
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
    # Opt-in structured-decoding hint (REVIEW_FINDINGS A5): most OpenAI-
    # compatible endpoints (and OpenRouter, for providers that support it)
    # accept response_format={"type":"json_object"} to structurally bias/
    # guarantee JSON output. Left as a plain pass-through (None by default --
    # see extract_filter_ex's json_mode kwarg) since it can't be verified
    # against a live endpoint in this offline environment, and a provider that
    # doesn't support it may error on an unrecognised field.
    if response_format:
        payload["response_format"] = response_format
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
# instead of the full OpenRouter slug.
#
# REVIEW_FINDINGS E1: this dict used to hand-duplicate models_registry.MODELS'
# slugs (admitted in the old comment here: "kept in sync with models_registry.py"
# -- i.e. kept in sync BY HAND, which is exactly how the two drift). Derived FROM
# the registry now: every registry entry's id becomes an alias for its own slug,
# so there's one source of truth for "what does this short name resolve to".
# _ALIAS_EXTRAS layers a SMALL, hand-curated set of additional nicknames on top
# -- either a shorter/older short name for a registry entry ("phi4" for the
# phi4-14b entry; "mistral-small" for mistral-small-24b) or a real, verified
# OpenRouter slug worth trialing from the CLI that isn't in the bench's fixed
# 6-model tier lineup at all ("llama3.3-70b"). Keeping this second dict small
# and separate (rather than editing the registry's ids to match) is the
# deliberate boundary: the registry's ids are the bench's stable UI keys,
# these are just extra spellings for the CLI's --model flag.
_ALIAS_EXTRAS = {
    "phi4": "microsoft/phi-4",
    "mistral-small": "mistralai/mistral-small-3.2-24b-instruct",
    "llama3.3-70b": "meta-llama/llama-3.3-70b-instruct",
}

# Same keys as before this change (gemma3-4b, gemma3-27b, phi4, qwen3-14b,
# qwen3-32b, mistral-small, llama3.3-70b) PLUS every registry id not already
# covered by one of those (phi4-14b, mistral-small-24b) -- so `ask.ps1 -Model
# qwen3-14b` and any script/habit built around the old key set keeps working
# unchanged, and the bench's own ids are now also valid --model values.
OPENROUTER_ALIASES = {m["id"]: m["slug"] for m in models_registry.MODELS}
OPENROUTER_ALIASES.update(_ALIAS_EXTRAS)


# --------------------------------------------------------------------------- #
# Unified defaults (REVIEW_FINDINGS E2)                                       #
# --------------------------------------------------------------------------- #
#
# ragkit.answer() (the CLI's `ask` command) and compare_server.CONFIG (the
# bench) used to each hard-code their OWN, DIFFERENT defaults for the same
# knobs -- filter_mode "hard" vs "auto", filter_min_count 1 vs 2, the
# answering model doing double duty as the filter-extraction model vs a
# dedicated FILTER_MODEL -- so the CLI and the bench could (and did) answer
# the exact same question differently for no principled reason. This dict is
# now the ONE place those defaults live; ragkit.answer's own parameter
# defaults and compare_server.CONFIG's construction both derive from it (see
# each), and a per-run CLI/UI override still works exactly as before -- this
# is a set of DEFAULTS, not a lock.
#
# Values were not picked arbitrarily: for every key the two call sites
# actually disagreed on, the BETTER of the two current values was kept (never
# a new third number), with the reasoning recorded here so a future change to
# one of these has to argue with it explicitly instead of silently drifting
# again:
#
#   k = 4                    -- both call sites already agreed.
#   max_context_tokens = 3000 -- the bench's value. The CLI's old default
#       (None = unbounded) is exactly the B1 failure mode the global prompt
#       budgeter (assemble_context) exists to prevent -- an explicit, generous
#       cap is the safe default; a caller that truly wants unbounded can still
#       pass 0 through --max-context-tokens (same "0 = uncapped" convention
#       both `ask` and `serve` already use).
#   filter_mode = "auto"      -- the bench's value; safer than the CLI's old
#       "hard": a hard gate on a spurious/over-broad model-derived filter can
#       silently return nothing, whereas 'auto' only hard-gates when the
#       matched set is comfortably larger than k (see retrieve()'s docstring).
#   filter_min_count = 2      -- the bench's value; drops singleton-coverage
#       fields (36% of this catalogue -- see the corpus snapshot in
#       REVIEW_FINDINGS.md) from the filter-extraction spec, which is both
#       cheaper and less noisy than the CLI's old 1.
#   filter_max_fields = 60    -- the bench's value; the CLI's old None
#       (unbounded) reproduces the ~8k-token full-catalogue spec cost the
#       corpus snapshot flags as the dominant fixed cost for a small model.
#   two_pass = False          -- both call sites already agreed.
#   field_select = None       -- "auto": embed-rank fields when this db has
#       field_embeddings (REVIEW_FINDINGS A2), else fall back to coverage
#       pruning (see resolve_field_select_mode). Functionally identical to
#       the bench's old literal "embed" for any db that actually HAS field
#       embeddings (the normal, freshly-ingested case), but also correct for
#       an older db with none -- which a hard-coded "embed" needs a fallback
#       check for anyway, so None is strictly the more robust of the two.
#   field_select_k = 15       -- both call sites already agreed.
#   min_rel = 0.0             -- both call sites already agreed (no floor).
#   filter_model = "mistral-small" -- the bench's value: a SEPARATE, dedicated
#       filter-extraction model chosen for reliable JSON/schema adherence,
#       rather than reusing whichever model is under test (the CLI's old
#       behaviour, and the other half of what E2 flags). ragkit.answer's own
#       `filter_model` parameter still defaults to None (= reuse the
#       answering model), NOT this value, because "mistral-small" is an
#       OpenRouter alias -- silently forcing it onto the CLI's local/
#       anthropic/openai backends would require an OpenRouter key those
#       backends have no other reason to need. Pass --filter-model explicitly
#       (works for the openrouter/openai backends) to opt into the bench's
#       behaviour from the CLI.
DEFAULTS = {
    "k": 4,
    "max_context_tokens": 3000,
    "filter_mode": "auto",
    "filter_min_count": 2,
    "filter_max_fields": 60,
    "two_pass": False,
    "field_select": None,
    "field_select_k": 15,
    "min_rel": 0.0,
    "filter_model": "mistral-small",
}


def _openrouter_raw(system, user, model, timeout=HTTP_TIMEOUT,
                    max_tokens=OPENAI_MAX_TOKENS, response_format=None):
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
                       max_tokens=max_tokens, response_format=response_format)


def gen_local(query, contexts, model="Qwen/Qwen2.5-1.5B-Instruct", digest=None,
              table=None, max_context_tokens=None):
    return gen_local_raw(system_prompt_for(table),
                         build_prompt(query, contexts, digest, table,
                                      max_context_tokens), model)


def gen_anthropic(query, contexts, model="claude-sonnet-4-6", digest=None,
                  table=None, max_context_tokens=None):
    return _anthropic_raw(system_prompt_for(table),
                          build_prompt(query, contexts, digest, table,
                                       max_context_tokens), model)


def gen_openrouter(query, contexts, model=None, digest=None, table=None,
                   max_context_tokens=None):
    return _openrouter_raw(system_prompt_for(table),
                           build_prompt(query, contexts, digest, table,
                                        max_context_tokens),
                           model or OPENROUTER_DEFAULT_MODEL)


def gen_openai(query, contexts, model, base_url="http://localhost:8000/v1",
               digest=None, table=None, max_context_tokens=None):
    return _openai_raw(system_prompt_for(table),
                       build_prompt(query, contexts, digest, table,
                                    max_context_tokens),
                       model, base_url)


def generate(query, contexts, backend, model=None, base_url=None, digest=None,
             table=None, max_context_tokens=None):
    """max_context_tokens (REVIEW_FINDINGS B1) is the GLOBAL prompt budget --
    passed straight through to build_prompt/assemble_context, which is the
    only place passages/table/digest are traded off against ONE total. This
    replaced retrieve()'s max_context_tokens as the authoritative cap; see
    ragkit.answer for the caller-side change (retrieve() is now called
    without a cap so it returns full-size passages for assembly to budget)."""
    if backend == "local":
        return gen_local(query, contexts, model or "Qwen/Qwen2.5-1.5B-Instruct",
                         digest, table, max_context_tokens)
    if backend == "anthropic":
        return gen_anthropic(query, contexts, model or "claude-sonnet-4-6",
                             digest, table, max_context_tokens)
    if backend == "openrouter":
        return gen_openrouter(query, contexts, model, digest, table,
                              max_context_tokens)
    if backend == "openai":
        if not model:
            raise RuntimeError("--model required for openai backend")
        return gen_openai(query, contexts, model,
                          base_url or "http://localhost:8000/v1", digest, table,
                          max_context_tokens)
    raise RuntimeError(f"unknown backend {backend}")


def _dispatch_filter_llm(sys_p, user_p, backend, model, base_url, response_format=None):
    """The per-backend dispatch extract_filter_ex needs twice (initial call +
    one parse-error retry) -- factored out so the retry doesn't duplicate the
    if/elif ladder. Raises RuntimeError for an unknown backend (extract_filter
    used to just return {} silently; extract_filter_ex's caller catches this
    and records it as a parse_failed with a clear reason instead)."""
    if backend == "local":
        return gen_local_raw(sys_p, user_p, model or "Qwen/Qwen2.5-1.5B-Instruct")
    if backend == "anthropic":
        return _anthropic_raw(sys_p, user_p, model or "claude-sonnet-4-6",
                              max_tokens=FILTER_MAX_TOKENS)
    if backend == "openrouter":
        return _openrouter_raw(sys_p, user_p, model or OPENROUTER_DEFAULT_MODEL,
                               max_tokens=FILTER_MAX_TOKENS,
                               response_format=response_format)
    if backend == "openai":
        return _openai_raw(sys_p, user_p, model, base_url or "http://localhost:8000/v1",
                           max_tokens=FILTER_MAX_TOKENS, response_format=response_format)
    raise RuntimeError(f"unknown backend '{backend}'")


def _first_balanced_json(text):
    """Brace-matching scan (REVIEW_FINDINGS A5) for the first balanced {...}
    block in `text`, string/escape-aware so a brace inside a quoted value
    ("descr": "uses {braces}") doesn't throw off the depth count. Returns the
    substring or None if no balanced block exists. Used as extract_filter_ex's
    fallback when a model wraps its JSON in prose ("Here's the filter: {...}
    let me know if you need anything else") that naive fence-stripping +
    json.loads can't parse."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)  # unbalanced from here; try the next '{'
    return None


def _parse_filter_json(raw):
    """Parse a model's filter-JSON response robustly (REVIEW_FINDINGS A5).
    Strips ```json fences, tries json.loads on the whole (fenced-stripped)
    response first (the common case), and falls back to brace-matching the
    first balanced {...} block for prose-wrapped responses. Returns
    (parsed_or_None, error_str_or_None) -- error is only meaningful when
    parsed is None."""
    text = (raw or "").strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text), None
    except Exception as e:
        err = str(e)
    block = _first_balanced_json(text)
    if block is not None:
        try:
            return json.loads(block), None
        except Exception as e2:
            err = str(e2)
    return None, err


def extract_filter_ex(query, catalogue, backend, model=None, base_url=None,
                      only_fields=None, min_count=1, max_fields=None,
                      category_stats=None, category_value=None,
                      retry_on_parse_error=True, json_mode=False):
    """Like extract_filter, but returns (raw_filter_dict, info) where `info`
    distinguishes WHY the dict came back the way it did (REVIEW_FINDINGS A5).
    extract_filter's plain {} return can't tell "the model said no filter
    applies" from "the model's output didn't parse" -- those look identical to
    every downstream caller, which is exactly the silent-failure problem A5
    flags. info = {"status": "ok"|"empty"|"parse_failed", "attempts": 1|2,
    "parse_error": <str, only set when status == "parse_failed">}:
      "ok"           -- parsed a non-empty filter dict.
      "empty"        -- parsed cleanly to {} (the model explicitly said "no
                        filter applies" -- a real, trustworthy answer).
      "parse_failed" -- both attempts (initial + one retry) failed to parse,
                        or the LLM call itself raised. Callers (ragkit.answer,
                        compare_server.build_context) surface this so the UI
                        can say "filter extraction failed" instead of quietly
                        behaving exactly like "empty".

    Parsing (see _parse_filter_json/_first_balanced_json) is layered so a
    response wrapped in prose still recovers:
      1. strip ```json fences, try json.loads directly;
      2. on failure, brace-match the first balanced {...} block and retry;
      3. on failure, make ONE more LLM call (same backend/model) with the
         parse error appended and an explicit "return ONLY valid JSON"
         instruction, then repeat 1-2 on that response. Disable with
         retry_on_parse_error=False.

    only_fields/min_count/max_fields/category_stats/category_value pass
    straight through to catalogue_to_prompt (see its docstring) -- this
    function only adds parsing robustness + the retry + the status signal on
    top of what extract_filter already did.

    json_mode (opt-in, default OFF): when True and backend is openrouter/
    openai, request response_format={"type":"json_object"} (see _openai_raw),
    which structurally biases/guarantees JSON on providers that support it --
    a real (if partial) instance of A5's schema-constrained-decoding
    suggestion. Left default-off because this environment can't verify it
    against a live endpoint (every provider's support/behaviour differs); flip
    it on once you've confirmed your provider honours it."""
    spec = cat_mod.catalogue_to_prompt(
        catalogue, only_fields=only_fields, min_count=min_count,
        max_fields=max_fields, category_stats=category_stats,
        category_value=category_value)
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
    fmt = ({"type": "json_object"}
           if (json_mode and backend in ("openrouter", "openai")) else None)

    info = {"attempts": 1}
    try:
        raw = _dispatch_filter_llm(sys_p, user_p, backend, model, base_url,
                                   response_format=fmt)
    except Exception as e:
        info["status"] = "parse_failed"
        info["parse_error"] = f"LLM call failed: {e}"
        return {}, info

    parsed, err = _parse_filter_json(raw)
    if parsed is None and retry_on_parse_error:
        info["attempts"] = 2
        retry_user = (f"{user_p}\n\n(Your previous response could not be "
                      f"parsed as JSON: {err}. Return ONLY valid JSON, no "
                      f"prose, no code fences.)")
        try:
            raw2 = _dispatch_filter_llm(sys_p, retry_user, backend, model, base_url,
                                       response_format=fmt)
            parsed, err = _parse_filter_json(raw2)
        except Exception as e:
            err = f"retry LLM call failed: {e}"

    if parsed is None:
        info["status"] = "parse_failed"
        info["parse_error"] = err
        return {}, info
    if not isinstance(parsed, dict):
        info["status"] = "parse_failed"
        info["parse_error"] = f"parsed JSON is not an object ({type(parsed).__name__})"
        return {}, info
    info["status"] = "ok" if parsed else "empty"
    return parsed, info


def extract_filter(query, catalogue, backend, model=None, base_url=None,
                   only_fields=None, min_count=1, max_fields=None):
    """Ask the LLM to emit a JSON filter grounded in the catalogue. Returns the
    raw parsed dict (unvalidated) or {} on failure -- same contract as always,
    kept so every existing caller (extract_filter_2pass's old behaviour,
    compare_server, the CLI) needs zero changes. Delegates to extract_filter_ex
    (REVIEW_FINDINGS A5: robust parsing + one parse-error retry) and discards
    its `info` -- callers that want to know WHY an empty filter came back
    ("no filter applies" vs. "the model's output didn't parse") should call
    extract_filter_ex directly instead of this wrapper; see ragkit.answer and
    compare_server.build_context for the pattern."""
    parsed, _info = extract_filter_ex(
        query, catalogue, backend, model, base_url,
        only_fields=only_fields, min_count=min_count, max_fields=max_fields)
    return parsed


def answer(db_path, query, backend, model=None, base_url=None,
           embed_model=DEFAULT_EMBED_MODEL, k=DEFAULTS["k"], filters=None,
           auto_filter=False, max_context_tokens=DEFAULTS["max_context_tokens"],
           two_pass=DEFAULTS["two_pass"], filter_min_count=DEFAULTS["filter_min_count"],
           filter_max_fields=DEFAULTS["filter_max_fields"],
           filter_mode=DEFAULTS["filter_mode"], field_select=DEFAULTS["field_select"],
           field_select_k=DEFAULTS["field_select_k"], min_rel=DEFAULTS["min_rel"],
           filter_model=None):
    """filters: an explicit filter dict (from UI/caller).
    auto_filter: if True and no explicit filters, ask the model to derive one.
    two_pass: use the broad-category-then-detail extractor (extract_filter_2pass).
    filter_min_count/filter_max_fields: coverage-based pruning of the single-pass
    field spec (see catalogue_to_prompt).
    filter_mode: how the validated filter is applied at retrieval time --
    'hard' (gate), 'soft' (rank boost), or 'auto' (k-guard: hard only when the
    matched set is comfortably larger than k). See retrieve().
    field_select/field_select_k (REVIEW_FINDINGS A2, single-pass only): 'embed'
    ranks catalogue fields by query-embedding similarity (see select_fields)
    and shows the filter-extraction model only the top field_select_k (UNION
    the partition fields) instead of every coverage-pruned field; 'coverage'
    keeps the pre-A2 behaviour (only_fields=None, catalogue_to_prompt's own
    min_count/max_fields pruning). None (default) auto-picks 'embed' when the
    db has field_embeddings (see resolve_field_select_mode), else 'coverage' --
    so an old db (ingested before this phase) behaves exactly as before with
    no config change needed. Doesn't apply to two_pass (see select_fields'
    docstring for why that's a deliberate, not missing, choice).
    min_rel (REVIEW_FINDINGS G3, default 0.0 = no floor, current behaviour):
    passed through to retrieve() -- see its docstring.
    filter_model (REVIEW_FINDINGS E2, default None = reuse `model`/`backend`):
    a separate model for the auto_filter extraction call, e.g. an OpenRouter
    alias like DEFAULTS["filter_model"] ("mistral-small") for more reliable
    JSON/schema adherence than whatever model is under test. Left as an
    opt-in override (not defaulted to DEFAULTS["filter_model"] here) because
    that value is an OpenRouter-only alias -- silently forcing it on the
    local/anthropic/openai backends would require an OpenRouter key those
    backends have no other reason to need; pass it explicitly to opt in.

    Every keyword default above is DEFAULTS[...] (REVIEW_FINDINGS E2): this is
    the single source ragkit.answer (this function, the CLI's `ask`) and
    compare_server.CONFIG (the bench) both derive their defaults from, so the
    same question gets the same behaviour from either entrypoint unless a
    caller/flag explicitly overrides a knob. See DEFAULTS' own docstring
    comment for why each value is what it is.

    max_context_tokens (REVIEW_FINDINGS B1): the GLOBAL prompt budget -- now
    applied once, in generate()/build_prompt/assemble_context, across
    passages+table+digest together. retrieve() itself is called WITHOUT a
    cap (full-size passages), so assembly has the real text to budget
    against instead of double-truncating already-shortened passages.
    Returns (reply, contexts, filter_info)."""
    con = connect(db_path)
    catalogue = load_catalogue(con)
    aliases = load_aliases(con)
    filter_info = {"applied": {}, "errors": [], "source": None, "fell_back": False,
                  "extraction": None}

    raw_filter = filters
    if raw_filter is None and auto_filter and catalogue:
        fmodel = filter_model or model
        if two_pass:
            raw_filter, tp_info = extract_filter_2pass(
                query, catalogue, con, backend, fmodel, base_url,
                min_count=filter_min_count)
            filter_info["two_pass"] = tp_info
            filter_info["extraction"] = tp_info.get("extraction", "empty")
        else:
            only_fields = None
            mode = resolve_field_select_mode(con, field_select)
            if mode == "embed":
                always = cat_mod.partition_fields(catalogue, min_count=filter_min_count)
                only_fields = select_fields(con, query, catalogue,
                                            k=field_select_k, always=always)
            raw_filter, ex_info = extract_filter_ex(
                query, catalogue, backend, fmodel, base_url,
                only_fields=only_fields,
                min_count=filter_min_count, max_fields=filter_max_fields)
            filter_info["extraction"] = ex_info["status"]
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

    # Entity pinning (REVIEW_FINDINGS G1 consume): deterministic alias match
    # computed once here (aliases already loaded above) and handed to
    # retrieve() -- guarantees a named record surfaces even when embeddings/
    # RRF rank it poorly (the common case for exact designations).
    pin = match_entities(query, aliases)

    contexts = retrieve(con, query, k=k, embed_model=embed_model,
                        clean_filter=clean or None,
                        filter_mode=eff_mode,
                        matched_parents=filter_info.get("matched_records"),
                        pin_entities=pin, min_rel=min_rel)

    # Empty-set fallback: if a filter excluded everything, retry unfiltered
    # rather than misleadingly answering "no information".
    if not contexts and clean:
        filter_info["fell_back"] = True
        contexts = retrieve(con, query, k=k, embed_model=embed_model,
                            pin_entities=pin, min_rel=min_rel)

    if not contexts:
        return "No records indexed.", [], filter_info

    # Represent the fuller matched set beyond the top-k passages. Analytic
    # questions get a structured table (exact fields incl. descriptions);
    # otherwise a snippet-per-record digest for prose breadth.
    digest, table = [], None
    matched = filter_info.get("matched_records")
    if clean and not filter_info["fell_back"] and matched:
        if is_analytic_query(query, clean, aliases=aliases):
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
                     digest=digest, table=table,
                     max_context_tokens=max_context_tokens)
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


def serve(db_path, port=8099, k=4, max_context_tokens=None, open_browser=True,
          min_rel=None, host="127.0.0.1"):
    # Imported lazily to avoid a circular import at module load (compare_server
    # imports ragkit) and to keep flask an optional dependency of the CLI.
    # host (REVIEW_FINDINGS F1): local-only by default; see
    # compare_server.run_server's docstring for the LAN-exposure warning.
    import compare_server
    compare_server.run_server(db_path, port=port, k=k,
                              max_context_tokens=max_context_tokens,
                              open_browser=open_browser, min_rel=min_rel,
                              host=host)


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
    pa.add_argument("-k", type=int, default=DEFAULTS["k"])
    pa.add_argument("--auto-filter", action="store_true",
                    help="let the model derive a metadata filter from the query")
    pa.add_argument("--filter", help="explicit filter as JSON, e.g. "
                    "'{\"max_temp\":{\"min\":150},\"status\":{\"in\":[\"active\"]}}'")
    # --max-context-tokens through --filter-model (REVIEW_FINDINGS E2): these
    # used to be bench-only (compare_server's CLI/CONFIG) while `ask` either
    # had no equivalent knob at all or silently used a different default --
    # exactly the "CLI vs bench behave differently for the same question" gap
    # E2 flags. All defaults below come from the single DEFAULTS dict so `ask`
    # and the bench agree unless a flag/CLI override says otherwise.
    pa.add_argument("--max-context-tokens", type=int, default=DEFAULTS["max_context_tokens"],
                    help="cap total assembled prompt tokens (passages+table+"
                         "digest combined -- REVIEW_FINDINGS B1); 0 = uncapped "
                         f"(default {DEFAULTS['max_context_tokens']})")
    pa.add_argument("--show-catalogue", action="store_true",
                    help="print the field catalogue and exit")
    pa.add_argument("--filter-mode", choices=("hard", "soft", "fill", "auto"),
                    default=DEFAULTS["filter_mode"],
                    help="how a filter is applied: hard gate, soft rank-boost, "
                         "fill (eligible-first + top-up), or auto k-guard "
                         f"(default {DEFAULTS['filter_mode']})")
    pa.add_argument("--two-pass", action="store_true",
                    help="broad-category-then-detail filter extraction (2 LLM "
                         f"calls) instead of single-pass (default "
                         f"{DEFAULTS['two_pass']})")
    pa.add_argument("--filter-min-count", type=int, default=DEFAULTS["filter_min_count"],
                    help="drop filter fields with coverage below N records "
                         f"(default {DEFAULTS['filter_min_count']})")
    pa.add_argument("--filter-max-fields", type=int, default=DEFAULTS["filter_max_fields"],
                    help="show at most N filter fields, highest-coverage first "
                         f"(default {DEFAULTS['filter_max_fields']})")
    pa.add_argument("--filter-model", default=None,
                    help="separate model for auto-filter extraction (OpenRouter "
                         "alias or slug, e.g. mistral-small); default: reuse "
                         "--model (see DEFAULTS['filter_model'] for why this "
                         "isn't defaulted to the bench's dedicated model here)")
    pa.add_argument("--field-select", choices=("embed", "coverage"), default=DEFAULTS["field_select"],
                    help="single-pass filter-field selection: 'embed' (query-"
                         "relevance ranked, REVIEW_FINDINGS A2) or 'coverage' "
                         "(pre-A2 behaviour); default auto-picks 'embed' when "
                         "the db has field_embeddings, else 'coverage'")
    pa.add_argument("--field-select-k", type=int, default=DEFAULTS["field_select_k"],
                    help="how many fields the 'embed' selector shows (plus the "
                         f"always-included partition fields) (default "
                         f"{DEFAULTS['field_select_k']})")
    pa.add_argument("--min-rel", type=float, default=DEFAULTS["min_rel"],
                    help="relevance floor in [0,1] on normalized rerank/RRF "
                         "score below which a candidate passage is dropped "
                         "instead of padded into k (REVIEW_FINDINGS G3); "
                         f"default {DEFAULTS['min_rel']} preserves the old "
                         "always-fill-k behaviour")

    ps = sub.add_parser("serve", help="launch the model-comparison web bench")
    ps.add_argument("--db", default="rag_test.db")
    ps.add_argument("--port", type=int, default=8099)
    ps.add_argument("-k", type=int, default=DEFAULTS["k"])
    ps.add_argument("--max-context-tokens", type=int, default=DEFAULTS["max_context_tokens"],
                    help="cap on the assembled prompt's tokens -- passages+"
                         "table+digest combined (0 = no cap; REVIEW_FINDINGS B1)")
    ps.add_argument("--no-open", action="store_true",
                    help="don't auto-open the browser")
    ps.add_argument("--min-rel", type=float, default=None,
                    help="relevance floor in [0,1] (REVIEW_FINDINGS G3); "
                         "default 0.0 (no floor, current behaviour)")
    ps.add_argument("--host", default="127.0.0.1",
                    help="bind address (REVIEW_FINDINGS F1): default "
                         "127.0.0.1 (local-only); pass 0.0.0.0 to expose on "
                         "the LAN (and your OpenRouter spend with it) -- "
                         "prints a warning when doing so")

    # `eval` subcommand (REVIEW_FINDINGS F2): the flag set lives in eval.py's
    # own build_arg_parser (shared with `python eval.py` directly) so the two
    # entrypoints can't drift. Imported here rather than at module load to
    # avoid a circular import -- eval.py does `import ragkit`, and by the time
    # main() actually runs (only under `if __name__ == "__main__"` below) this
    # module is already fully loaded, so that import just reuses it from
    # sys.modules instead of re-entering a half-initialized ragkit.
    import eval as eval_mod
    pe = sub.add_parser("eval", help="offline eval harness: retrieval hit-rate, "
                        "prompt-size stats, optional LLM-stage checks (REVIEW_FINDINGS F2)")
    eval_mod.build_arg_parser(pe)

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
            max_context_tokens=a.max_context_tokens or None,  # 0 -> uncapped
            two_pass=a.two_pass, filter_min_count=a.filter_min_count,
            filter_max_fields=a.filter_max_fields, filter_mode=a.filter_mode,
            field_select=a.field_select, field_select_k=a.field_select_k,
            min_rel=a.min_rel, filter_model=a.filter_model)
        print(reply)
        print("\n--- retrieved ---", file=sys.stderr)
        for c in ctx:
            print(f"[{c['rid']}] {c['title']}", file=sys.stderr)
        if finfo.get("applied"):
            print(f"--- filter ({finfo['source']}): {json.dumps(finfo['applied'])}",
                  file=sys.stderr)
        if finfo.get("errors"):
            print(f"--- filter notes: {finfo['errors']}", file=sys.stderr)
        # extraction == "parse_failed" is otherwise indistinguishable from a
        # legitimate "no filter applies" ("empty") -- surface it explicitly
        # (REVIEW_FINDINGS A5) rather than silently answering as if unfiltered.
        if finfo.get("extraction") == "parse_failed":
            print("--- filter extraction FAILED to parse the model's response; "
                  "treated as no filter", file=sys.stderr)
        if finfo.get("fell_back"):
            print("--- filter excluded all records; fell back to unfiltered",
                  file=sys.stderr)
    elif a.cmd == "serve":
        serve(a.db, port=a.port, k=a.k,
              max_context_tokens=a.max_context_tokens or None,  # 0 -> uncapped
              open_browser=not a.no_open, min_rel=a.min_rel, host=a.host)
    elif a.cmd == "eval":
        _report, ok = eval_mod.run_eval(
            db=a.db, eval_set=a.eval_set, k=a.k, min_rel=a.min_rel,
            max_context_tokens=a.max_context_tokens, backend=a.backend,
            model=a.model, base_url=a.base_url, filter_model=a.filter_model,
            json_out=a.json_out, limit=a.limit)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
