"""Materialize the DB + filter catalogue an LLM reviews, for inspection/sharing."""

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from collections import Counter


EMBEDDER_ENV = {
    "KMP_DUPLICATE_LIB_OK": "TRUE",
    "OMP_NUM_THREADS": "1",
    "TOKENIZERS_PARALLELISM": "false",
    "PYTHONIOENCODING": "utf-8",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}


def _set_embedder_env():
    for key, value in EMBEDDER_ENV.items():
        os.environ.setdefault(key, value)


def _resolve_db_path(out_dir, db_arg):
    out_abs = os.path.abspath(out_dir)
    db_path = db_arg
    if not os.path.isabs(db_path):
        db_path = os.path.join(out_abs, db_path)
    db_abs = os.path.abspath(db_path)
    common = os.path.commonpath([out_abs, db_abs])
    if common != out_abs:
        raise SystemExit(
            f"Refusing to operate on db outside --out: {db_abs}"
        )
    return db_abs


def _remove_db_artifacts(db_path):
    for path in (db_path, db_path + "-wal", db_path + "-shm"):
        if os.path.exists(path):
            os.remove(path)


def _count_table(con, table):
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return None


def _field_type_counts(catalogue):
    counts = Counter()
    for spec in catalogue.values():
        if spec.get("admin"):
            counts["admin"] += 1
        else:
            counts[spec.get("type", "unknown")] += 1
    return counts


def _write_summary(path, record_count, passage_count, catalogue):
    counts = _field_type_counts(catalogue)
    lines = [
        f"records: {record_count}",
        f"passages: {passage_count}",
        f"catalogue fields: {len(catalogue)}",
        "fields by type:",
    ]
    for name in ("numeric", "categorical", "date", "multi_value",
                 "free_text", "admin"):
        lines.append(f"  {name}: {counts.get(name, 0)}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_prompt(path, records_dir, record_count, field_count, prompt):
    today = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    header = [
        "# Catalogue snapshot",
        f"# Source records dir: {records_dir}",
        f"# Record count: {record_count}",
        f"# Field count: {field_count}",
        f"# Generated: {today}",
        "# Note: live queries may see a query-narrowed subset of these fields.",
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(header))
        f.write(prompt)
        f.write("\n")


def build_snapshot(records_dir, out_dir, db_arg):
    _set_embedder_env()

    import catalogue as cat_mod
    import ragkit

    out_abs = os.path.abspath(out_dir)
    os.makedirs(out_abs, exist_ok=True)
    db_path = _resolve_db_path(out_abs, db_arg)
    _remove_db_artifacts(db_path)

    ragkit.ingest(db_path, records_dir)

    con = ragkit.connect(db_path)
    try:
        catalogue = ragkit.load_catalogue(con)
        record_count = _count_table(con, "record_params")
        passage_count = _count_table(con, "records")
        prompt = cat_mod.catalogue_to_prompt(
            catalogue,
            only_fields=None,
            min_count=1,
            max_fields=None,
            category_stats=None,
            category_value=None,
        )
    finally:
        con.close()

    catalogue_path = os.path.join(out_abs, "catalogue.json")
    prompt_path = os.path.join(out_abs, "catalogue_prompt.txt")
    summary_path = os.path.join(out_abs, "summary.txt")

    with open(catalogue_path, "w", encoding="utf-8") as f:
        json.dump(catalogue, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    _write_prompt(prompt_path, records_dir, record_count, len(catalogue), prompt)
    _write_summary(summary_path, record_count, passage_count, catalogue)

    return {
        "db": db_path,
        "catalogue": catalogue_path,
        "prompt": prompt_path,
        "summary": summary_path,
    }


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Build a DB-backed snapshot of the filter catalogue prompt."
    )
    p.add_argument("--records", default="test_records",
                   help="a .json file or directory of record JSON files")
    p.add_argument("--out", default="catalogue_snapshot",
                   help="directory to write the snapshot artifacts")
    p.add_argument("--db", default="snapshot.db",
                   help="db filename or path under --out")
    args = p.parse_args(argv)

    paths = build_snapshot(args.records, args.out, args.db)
    for key in ("db", "catalogue", "prompt", "summary"):
        print(f"{key}: {paths[key]}")


if __name__ == "__main__":
    main(sys.argv[1:])
