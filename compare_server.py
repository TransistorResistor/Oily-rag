#!/usr/bin/env python3
"""
compare_server.py - web frontend to compare RAG output across model tiers.

Serves a single-page UI that lets you:
  - pick up to 3 models from a tiered lineup (with descriptions + hardware needs)
  - see the EXACT context retrieved and the full prompt sent to the models
  - send that prompt to the selected models and view output side by side

All generation goes through OpenRouter, so set OPENROUTER_API_KEY. Retrieval,
filtering, and context assembly reuse ragkit.py unchanged.

Run (simplest):
  .\compare.ps1                      # sets env + launches + opens the browser

Or directly:
  pip install flask
  $env:OPENROUTER_API_KEY = "sk-or-v1-..."
  python compare_server.py --db rag_test.db     # opens http://localhost:8099

Efficiency notes: the MiniLM embedder (~18s / ~350MB cold) is preloaded once at
boot by warm_start(), so the first query is instant rather than a mystery hang.
Each worker thread reuses a single SQLite connection and the parsed catalogue is
cached, so requests don't reopen/re-parse on every call.
"""

import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser

from flask import Flask, request, jsonify, Response

import ragkit
from models_registry import MODELS, MODELS_BY_ID

app = Flask(__name__)
# Model used to derive the metadata filter (separate from the models under test).
# Needs reliable JSON/schema adherence; a 4B model isn't enough on a large catalogue.
FILTER_MODEL = "mistral-small"
# max_context_tokens caps how much retrieved text is packed into the prompt, so
# large records can't blow the context window / cost. None here would mean
# "unbounded"; we default to a sane cap in run_server/main.
# filter_min_count / filter_max_fields prune the filter-extraction spec by field
# coverage (drop near-empty fields, cap to the most-populated) so a large catalogue
# doesn't bloat every prompt. two_pass swaps single-pass extraction for the
# broad-category-then-detail extractor (2 LLM calls; better field selection on a
# large / heterogeneous catalogue). See ragkit.extract_filter{,_2pass}.
# filter_mode: how a validated filter is applied at retrieval time.
#   hard -> gate (only matching records are eligible)
#   soft -> rank boost (matching records preferred, others can still surface)
#   auto -> k-guard: hard-gate only when the matched set is comfortably larger than
#           k, else soften -- so a narrow/spurious filter can't starve or misdirect
#           retrieval. 'auto' is the safe default (differs from hard only in the
#           small-match regime where a hard gate is fragile).
CONFIG = {"db": "rag_test.db", "k": 4, "max_context_tokens": 3000,
          "filter_min_count": 2, "filter_max_fields": 60, "two_pass": False,
          "filter_mode": "auto"}

# --- shared retrieval state: loaded once, reused across requests --------------
# Flask serves with threaded=True and sqlite connections aren't shareable across
# threads, so we keep one connection per worker thread (reused across requests)
# instead of the old build_context() pattern of opening a fresh connection every
# call and never closing it (which leaked connections + re-parsed the catalogue).
_local = threading.local()
_catalogue_cache = {}
_catalogue_lock = threading.Lock()


def get_conn():
    con = getattr(_local, "con", None)
    if con is None or getattr(_local, "con_db", None) != CONFIG["db"]:
        con = ragkit.connect(CONFIG["db"])
        _local.con = con
        _local.con_db = CONFIG["db"]
    return con


def get_catalogue(con):
    """Parse the filter catalogue once per db (it only changes on re-ingest)."""
    db = CONFIG["db"]
    cat = _catalogue_cache.get(db)
    if cat is None:
        with _catalogue_lock:
            cat = _catalogue_cache.get(db)
            if cat is None:
                cat = ragkit.load_catalogue(con)
                _catalogue_cache[db] = cat
    return cat


def build_context(query, auto_filter, two_pass=None):
    """Run retrieval (+ optional model-driven filter) and return the contexts,
    the assembled prompt, and the filter info — the exact material a model sees."""
    con = get_conn()
    catalogue = get_catalogue(con)
    filter_info = {"applied": {}, "errors": [], "source": None}
    clean = None
    if two_pass is None:
        two_pass = CONFIG.get("two_pass", False)
    if auto_filter and catalogue:
        # Derive the filter with a model that reliably emits the JSON schema. A
        # 4B model produces malformed/duplicate-shape filters against a large
        # catalogue; mistral-small is the registry's filter-extraction pick.
        # The spec is pruned/ordered by field coverage (filter_min_count/
        # filter_max_fields) so a 200+-field catalogue doesn't flood the prompt.
        tp = None
        if two_pass:
            raw, tp = ragkit.extract_filter_2pass(
                query, catalogue, con, "openrouter",
                model=FILTER_MODEL, base_url=None,
                min_count=CONFIG.get("filter_min_count", 1))
        else:
            raw = ragkit.extract_filter(
                query, catalogue, "openrouter", model=FILTER_MODEL, base_url=None,
                min_count=CONFIG.get("filter_min_count", 1),
                max_fields=CONFIG.get("filter_max_fields"))
        if raw:
            clean, errors = ragkit.validate_filter(raw, catalogue)
            filter_info = {"applied": clean, "errors": errors, "source": "model"}
            if tp:
                filter_info["two_pass"] = tp
            # report how many records match before the top-k cap, so a large
            # filtered set isn't silently reduced to k with no signal.
            if clean:
                filter_info["matched_records"] = ragkit.count_matches(con, clean)

    # Decide how the filter is applied. 'auto' (the k-guard) hard-gates only when
    # the matched set is comfortably larger than k, else softens to a rank boost so
    # a narrow/spurious filter can't starve or misdirect retrieval. Resolve it here
    # (we already know matched_records) so the effective mode is visible to the UI.
    matched = filter_info.get("matched_records")
    eff_mode = CONFIG.get("filter_mode", "hard")
    if clean and eff_mode == "auto":
        eff_mode = "hard" if (matched or 0) > CONFIG["k"] else "fill"
    if clean:
        filter_info["filter_mode"] = eff_mode

    contexts = ragkit.retrieve(con, query, k=CONFIG["k"],
                               clean_filter=clean or None,
                               max_context_tokens=CONFIG["max_context_tokens"],
                               filter_mode=eff_mode, matched_parents=matched)

    # Represent the fuller matched set beyond the top-k passages. Analytic
    # questions (compare / which has the most / numeric filter) get a structured
    # table with the exact fields incl. descriptions; other queries get a
    # snippet-per-record digest for prose breadth.
    digest, table = [], None
    matched = filter_info.get("matched_records")
    if clean and matched:
        if ragkit.is_analytic_query(query, clean):
            table = ragkit.record_table(con, query, clean, catalogue=catalogue)
            if table:
                filter_info["table"] = table
        if not table and matched > len(contexts):
            shown = {c["rid"] for c in contexts}
            digest = ragkit.record_digest(con, query, clean, exclude=shown)
            if digest:
                filter_info["digest"] = digest

    prompt = ragkit.build_prompt(query, contexts, digest=digest, table=table)
    return contexts, prompt, filter_info


@app.route("/api/models")
def api_models():
    return jsonify(MODELS)


@app.route("/api/context", methods=["POST"])
def api_context():
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    auto_filter = bool(data.get("auto_filter"))
    two_pass = data.get("two_pass")  # None -> use CONFIG default
    if not query:
        return jsonify({"error": "empty query"}), 400
    try:
        contexts, prompt, finfo = build_context(query, auto_filter, two_pass=two_pass)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "system_prompt": ragkit.SYSTEM_PROMPT,
        "prompt": prompt,
        "filter": finfo,
        "sources": [{"rid": c["rid"], "title": c["title"], "text": c["text"]}
                    for c in contexts],
    })


def _run_one(model_id, system_prompt, user_prompt):
    m = MODELS_BY_ID.get(model_id)
    if not m:
        return {"model_id": model_id, "error": "unknown model"}
    t0 = time.time()
    try:
        reply = ragkit._openrouter_raw(system_prompt, user_prompt, m["slug"])
        return {"model_id": model_id, "name": m["name"], "answer": reply,
                "latency_s": round(time.time() - t0, 2), "error": None}
    except Exception as e:
        return {"model_id": model_id, "name": m["name"], "answer": None,
                "latency_s": round(time.time() - t0, 2), "error": str(e)}


@app.route("/api/compare", methods=["POST"])
def api_compare():
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    model_ids = data.get("models") or []
    auto_filter = bool(data.get("auto_filter"))
    two_pass = data.get("two_pass")  # None -> use CONFIG default
    if not query:
        return jsonify({"error": "empty query"}), 400
    if not model_ids:
        return jsonify({"error": "no models selected"}), 400
    if len(model_ids) > 3:
        return jsonify({"error": "pick at most 3 models"}), 400

    try:
        contexts, prompt, finfo = build_context(query, auto_filter, two_pass=two_pass)
    except Exception as e:
        return jsonify({"error": f"retrieval failed: {e}"}), 500

    # fan out to the selected models concurrently so side-by-side isn't serial
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(_run_one, mid, ragkit.SYSTEM_PROMPT, prompt)
                   for mid in model_ids]
        for fut in futures:
            results.append(fut.result())
    # preserve the order the user selected
    order = {mid: i for i, mid in enumerate(model_ids)}
    results.sort(key=lambda r: order.get(r["model_id"], 99))

    return jsonify({
        "system_prompt": ragkit.SYSTEM_PROMPT,
        "prompt": prompt,
        "filter": finfo,
        "sources": [{"rid": c["rid"], "title": c["title"], "text": c["text"]}
                    for c in contexts],
        "results": results,
    })


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


# --------------------------------------------------------------------------- #
# Startup: warm the caches so the first query is instant, not an 18s hang.     #
# --------------------------------------------------------------------------- #

def _resolve_db(db):
    """If the given db doesn't exist, fall back to a common name so first-run
    doesn't fail on the default. Returns the path to actually use."""
    if os.path.exists(db):
        return db
    for cand in ("rag_test.db", "rag.db"):
        if cand != db and os.path.exists(cand):
            print(f"  db '{db}' not found; using '{cand}'", file=sys.stderr)
            return cand
    return db  # let warm_start surface the empty/missing-db hint


def verify_slugs(timeout=5):
    """Best-effort: mark each registry model available/unavailable against
    OpenRouter's public /models list, so a wrong/renamed slug is flagged and
    disabled in the UI instead of 404-ing on the user's click. Non-fatal if
    offline — everything stays enabled and the call just errors on use."""
    try:
        req = urllib.request.Request(ragkit.OPENROUTER_BASE_URL + "/models",
                                     headers={"user-agent": "ragkit-bench"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            known = {m.get("id") for m in json.load(resp).get("data", [])}
    except Exception as e:
        print(f"  (couldn't verify slugs: {e}; leaving all models enabled)",
              file=sys.stderr)
        for m in MODELS:
            m["available"] = True
        return
    missing = []
    for m in MODELS:
        m["available"] = m["slug"] in known
        if not m["available"]:
            missing.append(f"{m['id']} → {m['slug']}")
    if missing:
        print("  ! slugs NOT on OpenRouter (disabled in UI): " + "; ".join(missing),
              file=sys.stderr)
    else:
        print(f"  all {len(MODELS)} model slugs verified on OpenRouter",
              file=sys.stderr)


def _check_api_key():
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("  ! OPENROUTER_API_KEY not set — 'Preview context only' still works,",
              file=sys.stderr)
        print("    but running models will error. Get a key at "
              "https://openrouter.ai/keys,", file=sys.stderr)
        print("    then set it (or put it in key.env and use .\\compare.ps1) and "
              "restart.", file=sys.stderr)


def warm_start():
    """Preload everything a request needs so the first query is instant:
    the MiniLM embedder (~18s / ~350MB cold) plus the sqlite conn + catalogue.
    Also sanity-checks that the db actually has records."""
    con = get_conn()
    try:
        n = con.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    except Exception:
        n = 0
    if n == 0:
        print(f"  ! db '{CONFIG['db']}' has no records. Ingest first, e.g.:",
              file=sys.stderr)
        print(f"      python ragkit.py ingest ./pages_schema --db {CONFIG['db']}",
              file=sys.stderr)
    else:
        cat = get_catalogue(con)
        print(f"  db ready: {n} passages, {len(cat)} catalogue fields",
              file=sys.stderr)
        # Echo the import diagnostics (structures not indexed as filter fields) so a
        # new/variant JSON shape's blind spots are loud even after ingest.
        row = con.execute(
            "SELECT value FROM meta WHERE key='import_dropped'").fetchone()
        dropped = json.loads(row[0]) if row and row[0] else {}
        if dropped:
            print(f"  ! import: {len(dropped)} field/structure(s) NOT indexed as "
                  f"filterable — {', '.join(sorted(dropped)[:6])}"
                  f"{' …' if len(dropped) > 6 else ''} "
                  f"(re-run ingest to see full report)", file=sys.stderr)
    verify_slugs()
    print("  loading embedder + reranker (one-time, ~15-25s cold)…",
          file=sys.stderr, flush=True)
    t0 = time.time()
    ragkit.get_embedder()
    # a dummy encode allocates torch's inference buffers now (~50MB) so the
    # first real query doesn't pay that allocation on the user's click.
    ragkit.embed(["warm up"])
    # preload the cross-encoder reranker too; best-effort so an offline first run
    # (model not yet cached) still boots — retrieval just falls back to RRF order.
    try:
        ragkit.get_reranker().predict([("warm up", "warm up")])
    except Exception as e:
        print(f"  (reranker unavailable: {e}; retrieval will use RRF order)",
              file=sys.stderr)
    print(f"  embedder + reranker ready in {time.time() - t0:.1f}s", file=sys.stderr)


def run_server(db="rag_test.db", port=8099, k=4, max_context_tokens=3000,
               open_browser=True, filter_min_count=None, filter_max_fields=None,
               two_pass=None, filter_mode=None):
    """Single entrypoint used by both this file's CLI and `ragkit.py serve`."""
    CONFIG["db"] = _resolve_db(db)
    CONFIG["k"] = k
    CONFIG["max_context_tokens"] = max_context_tokens
    if filter_min_count is not None:
        CONFIG["filter_min_count"] = filter_min_count
    if filter_max_fields is not None:
        CONFIG["filter_max_fields"] = filter_max_fields
    if two_pass is not None:
        CONFIG["two_pass"] = two_pass
    if filter_mode is not None:
        CONFIG["filter_mode"] = filter_mode
    url = f"http://localhost:{port}"
    print(f"RAG model bench → {url}   (db={CONFIG['db']})", file=sys.stderr)
    _check_api_key()
    warm_start()
    if open_browser:
        # fire after the socket is bound (app.run below); server is already warm
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print(f"  ready — serving on {url}", file=sys.stderr)
    app.run(host="0.0.0.0", port=port, threaded=True)


def main():
    p = argparse.ArgumentParser(description="RAG model comparison bench")
    p.add_argument("--db", default="rag_test.db")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("-k", type=int, default=4)
    p.add_argument("--max-context-tokens", type=int, default=3000,
                   help="cap on retrieved text packed into the prompt (0 = no cap)")
    p.add_argument("--no-open", action="store_true",
                   help="don't auto-open the browser")
    p.add_argument("--filter-min-count", type=int, default=None,
                   help="drop filter fields with coverage below N records "
                        f"(default {CONFIG['filter_min_count']})")
    p.add_argument("--filter-max-fields", type=int, default=None,
                   help="show at most N filter fields, highest-coverage first "
                        f"(default {CONFIG['filter_max_fields']})")
    p.add_argument("--two-pass", action="store_true",
                   help="broad-category-then-detail filter extraction (2 LLM calls)")
    p.add_argument("--filter-mode", choices=("hard", "soft", "fill", "auto"),
                   default=None,
                   help="how a filter is applied: hard gate, soft rank-boost, fill "
                        "(eligible-first + top-up), or auto k-guard "
                        f"(default {CONFIG['filter_mode']})")
    args = p.parse_args()
    run_server(args.db, args.port, args.k,
               max_context_tokens=args.max_context_tokens or None,  # 0 -> uncapped
               open_browser=not args.no_open,
               filter_min_count=args.filter_min_count,
               filter_max_fields=args.filter_max_fields,
               two_pass=True if args.two_pass else None,
               filter_mode=args.filter_mode)


# PAGE is defined in compare_server_page.py and injected at import time to keep
# this file focused on the API; see that module for the UI.
from compare_server_page import PAGE  # noqa: E402


if __name__ == "__main__":
    main()
