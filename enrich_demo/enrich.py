#!/usr/bin/env python3
"""
enrich.py - CLI for the reverse-enrichment demo.

Commands:
  python enrich.py run   [--only a,b,c] [--model gemma3-4b] [--note "..."]
  python enrich.py report [--run N]
  python enrich.py reject <proposal_id> [--reason "..."]
  python enrich.py list-proposals
  python enrich.py status

State lives in enrich_state.db (separate file). rag_test.db is never opened.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import state as state_mod           # noqa: E402
import refcat as refcat_mod         # noqa: E402
import pipeline as pipe             # noqa: E402
import report as report_mod         # noqa: E402
import llm as llm_mod               # noqa: E402

TESTDOCS = os.path.join(HERE, "testdocs")


def cmd_run(args):
    con = state_mod.connect(args.db)
    rc = refcat_mod.load_reference()
    only = [x.strip() for x in args.only.split(",")] if args.only else None
    print(f"Running batch (model={args.model}, render={args.render}) over "
          f"{args.folder}" + (f" only={only}" if only else "") + " ...")
    summ = pipe.run_batch(args.folder, con, rc, model=args.model, only=only,
                          note=args.note, render=args.render)
    print(f"\nRun {summ['run_id']}: processed {len(summ['processed'])} docs, "
          f"skipped {len(summ['skipped'])} (already seen), "
          f"{summ['llm_calls']} LLM calls, "
          f"{summ['prompt_tokens']}+{summ['completion_tokens']} tokens, "
          f"graduated {summ['graduated']} parked claim(s).")
    path, props = report_mod.build(con, summ["run_id"], HERE, db_path=args.db)
    print(f"Report: {path}")
    stem = os.path.splitext(os.path.basename(args.db))[0]
    extra = (f" (+ proposals_{stem}.json)"
             if stem and stem != "enrich_state" else "")
    print(f"Proposals: {os.path.join(HERE, 'proposals.json')}{extra} "
          f"({len(props)} live)")


def cmd_report(args):
    con = state_mod.connect(args.db)
    run_id = args.run
    if run_id is None:
        r = con.execute("SELECT MAX(run_id) m FROM runs").fetchone()
        run_id = r["m"]
    path, props = report_mod.build(con, run_id, HERE, db_path=args.db)
    print(f"Report for run {run_id}: {path} ({len(props)} live proposals)")


def cmd_reject(args):
    con = state_mod.connect(args.db)
    row = con.execute(
        "SELECT DISTINCT record_title, canon_field, value_disp, proposal_type "
        "FROM claims WHERE full_fp=?", (args.proposal_id,)).fetchone()
    if not row:
        print(f"No proposal with id {args.proposal_id}")
        return
    state_mod.reject_fp(con, args.proposal_id, args.reason, args.proposal_id)
    # flip any currently-surfaced/parked claims of this fp to rejected
    con.execute("UPDATE claims SET status='rejected', park_reason='rejected' "
                "WHERE full_fp=? AND status IN ('surfaced','parked')",
                (args.proposal_id,))
    con.commit()
    print(f"Rejected {args.proposal_id}: {row['record_title']} - "
          f"{row['canon_field']} = {row['value_disp']} ({row['proposal_type']}). "
          f"It will not resurface; recurrences land in 'Seen again'.")


def cmd_list(args):
    con = state_mod.connect(args.db)
    rows = con.execute(
        "SELECT DISTINCT full_fp, record_title, canon_field, value_disp, "
        "proposal_type, status FROM claims WHERE full_fp IS NOT NULL "
        "AND status IN ('surfaced','rejected') ORDER BY record_title").fetchall()
    for r in rows:
        print(f"  {r['full_fp']}  [{r['status']:8}] {r['proposal_type']:9} "
              f"{r['record_title']} :: {r['canon_field']} = {r['value_disp']}")


def cmd_status(args):
    con = state_mod.connect(args.db)
    d = con.execute("SELECT COUNT(*) n FROM docs_seen").fetchone()["n"]
    runs = con.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"]
    import collections
    st = collections.Counter()
    for r in con.execute("SELECT status FROM claims"):
        st[r["status"]] += 1
    print(f"docs_seen={d}  runs={runs}  claims-by-status={dict(st)}")


def main():
    ap = argparse.ArgumentParser(description="Reverse-enrichment demo CLI")
    ap.add_argument("--db", default=state_mod.DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--folder", default=TESTDOCS)
    r.add_argument("--only", default=None, help="comma list of doc_ids")
    r.add_argument("--model", default=llm_mod.DEFAULT_MODEL)
    r.add_argument("--note", default="")
    r.add_argument("--render", default="text", choices=["text", "md"],
                   help="PDF extraction mode: plain text (default) or markdown "
                        "pipe tables")
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("report")
    rp.add_argument("--run", type=int, default=None)
    rp.set_defaults(func=cmd_report)

    rj = sub.add_parser("reject")
    rj.add_argument("proposal_id")
    rj.add_argument("--reason", default="reviewer rejected")
    rj.set_defaults(func=cmd_reject)

    sub.add_parser("list-proposals").set_defaults(func=cmd_list)
    sub.add_parser("status").set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
