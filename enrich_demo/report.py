#!/usr/bin/env python3
"""
report.py - render a per-run Markdown report + machine-readable proposals.json
from the claims store. Grouped by record: New this run / Conflicts / Outstanding
from prior runs / collapsed "Seen again xN" / collapsed near-threshold parked
fragments. Every surfaced item cites doc title + path + verbatim quote.
"""

import json
import os
import collections


def output_names(run_id, db_path=None):
    stem = os.path.splitext(os.path.basename(db_path or ""))[0]
    if stem and stem != "enrich_state":
        return f"proposals_{stem}.json", f"report_run{run_id}_{stem}.md"
    return "proposals.json", f"report_run{run_id}.md"


def _fmt_quote(q):
    q = (q or "").strip().replace("\n", " ")
    return q if len(q) <= 240 else q[:237] + "..."


def _proposals(con, run_id):
    """Materialise the proposal threshold-view from surfaced claims. One proposal
    per full_fp; aggregates its corroborating sources and value distribution
    (keyed by partial_fp)."""
    rows = con.execute(
        "SELECT * FROM claims WHERE status='surfaced' AND full_fp IS NOT NULL"
    ).fetchall()
    by_fp = collections.OrderedDict()
    for r in rows:
        by_fp.setdefault(r["full_fp"], []).append(r)
    props = []
    for fp, claims in by_fp.items():
        c0 = claims[0]
        first_run = min(cl["created_run"] for cl in claims)
        # value distribution across sources sharing the same partial_fp
        pfp = c0["partial_fp"]
        sib = con.execute(
            "SELECT value_disp, doc_title, doc_path, doc_id FROM claims "
            "WHERE partial_fp=? AND status='surfaced'", (pfp,)).fetchall()
        dist = collections.Counter(s["value_disp"] for s in sib)
        cluster_sources = len({s["doc_id"] for s in sib})
        sources = [{"doc_title": cl["doc_title"], "doc_path": cl["doc_path"],
                    "quote": cl["quote"], "value": cl["value_disp"]}
                   for cl in claims]
        mapping_tiers = collections.Counter(
            (cl["mapping_tier"] or "legacy") for cl in claims)
        props.append({
            "proposal_id": fp,
            "type": c0["proposal_type"],
            "record_id": c0["model_id"],
            "record": c0["record_title"],
            "field": c0["canon_field"],
            "value": c0["value_disp"],
            "db_value": c0["db_value"],
            "qualifier": c0["qualifier"],
            "n_sources": len({cl["doc_id"] for cl in claims}),
            "cluster_sources": cluster_sources,
            "value_distribution": dict(dist),
            "first_run": first_run,
            "is_new": first_run == run_id,
            "mapping_tier": c0["mapping_tier"],
            "mapping_method": c0["mapping_method"],
            "mapping_score": c0["mapping_score"],
            "mapping_tier_distribution": dict(mapping_tiers),
            "mapper_version": c0["mapper_version"],
            "alias_version": c0["alias_version"],
            "sources": sources,
        })
    return props


def _alias_suggestions(con):
    rows = con.execute(
        "SELECT attribute,mapping_candidate,mapping_method,mapping_score,doc_id,"
        "quote FROM claims WHERE attribute IS NOT NULL "
        "AND mapping_candidate IS NOT NULL "
        "AND mapping_method NOT IN ('legacy','canonical_exact','curated_alias',"
        "'contextual_alias','special_field')"
    ).fetchall()
    grouped = collections.defaultdict(list)
    for row in rows:
        term = " ".join((row["attribute"] or "").lower().split())
        if term:
            grouped[(term, row["mapping_candidate"])].append(row)
    out = []
    for (term, field), items in grouped.items():
        scores = [float(r["mapping_score"] or 0.0) for r in items]
        out.append({
            "term": term, "field": field, "count": len(items),
            "average_score": round(sum(scores) / len(scores), 4),
            "documents": sorted({r["doc_id"] for r in items}),
            "example_quotes": [r["quote"] for r in items[:3] if r["quote"]],
        })
    return sorted(out, key=lambda x: (-x["count"], -x["average_score"], x["term"]))


def build(con, run_id, out_dir, db_path=None):
    props = _proposals(con, run_id)
    # ---- proposals.json --------------------------------------------------- #
    proposals_name, report_name = output_names(run_id, db_path=db_path)
    def _dump(name):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            json.dump(props, f, indent=2)
    _dump(proposals_name)

    # ---- group by record -------------------------------------------------- #
    by_rec = collections.OrderedDict()
    for p in sorted(props, key=lambda x: (x["record"] or "")):
        by_rec.setdefault(p["record"], []).append(p)

    L = []
    L.append(f"# Reverse-enrichment report - run {run_id}\n")
    run = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run:
        L.append(f"*Model:* `{run['model']}`  |  *Docs this run:* {run['docs']}  "
                 f"|  *LLM calls:* {run['llm_calls']}  |  *Tokens:* "
                 f"{run['prompt_tokens']} prompt + {run['completion_tokens']} "
                 f"completion  |  *Field mapper:* `{run['field_mapper']}`\n")
    total_new = sum(1 for p in props if p["is_new"])
    L.append(f"**{len(props)} live proposals** ({total_new} new this run) across "
             f"{len(by_rec)} records.\n")

    for rec, plist in by_rec.items():
        L.append(f"\n## {rec}\n")
        new = [p for p in plist if p["is_new"] and p["type"] != "conflict"]
        conf = [p for p in plist if p["type"] == "conflict"]
        old = [p for p in plist if not p["is_new"] and p["type"] != "conflict"]

        if new:
            L.append("### New this run\n")
            for p in new:
                _emit(L, p)
        if conf:
            L.append("### Conflicts (DB vs document)\n")
            for p in conf:
                L.append(f"- **{p['field']}** - conflict")
                L.append(f"    - DB value: `{p['db_value']}`")
                L.append(f"    - Document value(s): "
                         + ", ".join(f"`{v}` x{n}"
                                     for v, n in p["value_distribution"].items()))
                if p["qualifier"]:
                    L.append(f"    - Qualifier: _{p['qualifier']}_")
                for s in p["sources"]:
                    L.append(f"    - {s['doc_title']}  ({s['doc_path']})")
                    L.append(f"      > {_fmt_quote(s['quote'])}")
                L.append("")
        if old:
            L.append("### Outstanding from prior runs\n")
            for p in old:
                _emit(L, p)

    # ---- collapsed: rejected recurring ("Seen again xN") ------------------ #
    rej = con.execute(
        "SELECT full_fp, record_title, canon_field, value_disp, proposal_type, "
        "COUNT(*) n, COUNT(DISTINCT doc_id) nd FROM claims WHERE status='rejected' "
        "GROUP BY full_fp").fetchall()
    if rej:
        L.append("\n## Suppressed (rejected but recurring)\n")
        for r in rej:
            L.append(f"- _Seen again x{r['nd']}_: {r['record_title']} - "
                     f"{r['canon_field']} = `{r['value_disp']}` "
                     f"({r['proposal_type']}) - previously rejected, not resurfaced")
        L.append("")

    # ---- rescue surface: parked unmapped/incomplete fragments ------------- #
    # For a report-only tool the parked pile is the PRIMARY recall surface, not
    # leftovers. Surface unmapped/incomplete fragments grouped BY RECORD (where a
    # record was linked) with raw attribute + value + quote, so a human reviewer
    # can rescue a real fact the deterministic mapper couldn't place. Fragments
    # that linked to no record stay in their own Unlinked section.
    frags = con.execute(
        "SELECT record_title, attribute, value_raw, unit_raw, value_disp, quote, "
        "doc_title, doc_path, park_reason,mapping_candidate,mapping_score,"
        "runner_up FROM claims "
        "WHERE status='parked' AND park_reason IN ('unmapped','ambiguous_field',"
        "'incomplete','incompatible_unit','text_difference','intradoc_conflict',"
        "'nonasserted','inconsistent_dual') "
        "ORDER BY record_title").fetchall()
    linked = collections.OrderedDict()
    unlinked = []
    for r in frags:
        rec = r["record_title"]
        if rec:
            linked.setdefault(rec, []).append(r)
        else:
            unlinked.append(r)

    def _frag_line(r):
        raw = (r["value_raw"] or "").strip()
        u = (r["unit_raw"] or "").strip()
        val = (raw + (" " + u if u else "")).strip() or (r["value_disp"] or "?")
        candidate = (f"; candidate `{r['mapping_candidate']}`"
                     f" score {float(r['mapping_score'] or 0):.2f}"
                     if r["mapping_candidate"] else "")
        runner = f"; runner-up `{r['runner_up']}`" if r["runner_up"] else ""
        return (f"  - _{r['attribute'] or '?'}_ = `{val}` "
                f"({r['park_reason']}{candidate}{runner}) - {r['doc_title']}\n"
                f"      > {_fmt_quote(r['quote'])}")

    if linked or unlinked:
        L.append("\n## Parked for review (rescue candidates)\n")
        L.append("*Unmapped/incomplete fragments the mapper could not place - "
                 "grouped by linked record for human review.*\n")
        for rec, rl in linked.items():
            L.append(f"### {rec}")
            for r in rl:
                L.append(_frag_line(r))
            L.append("")
        if unlinked:
            L.append("### Unlinked (no catalogued record matched)")
            for r in unlinked:
                L.append(_frag_line(r))
            L.append("")

    # ---- collapsed: near-threshold parked fragments ----------------------- #
    parked = con.execute(
        "SELECT park_reason, COUNT(*) n FROM claims WHERE status='parked' "
        "GROUP BY park_reason").fetchall()
    if parked:
        L.append("\n## Parked fragments (below surfacing bar)\n")
        for r in parked:
            L.append(f"- **{r['park_reason']}**: {r['n']} claim(s) held in state DB")
        # show the uncorroborated ones with their source count (near-threshold)
        unc = con.execute(
            "SELECT record_title, canon_field, value_disp, partial_fp, qualifier, "
            "doc_title, doc_path, quote FROM claims "
            "WHERE status='parked' AND park_reason='uncorroborated'").fetchall()
        if unc:
            L.append("\n  Near-threshold (uncorroborated, awaiting a 2nd source):")
            for r in unc:
                nsrc = con.execute(
                    "SELECT COUNT(DISTINCT doc_id) n FROM claims WHERE partial_fp=?"
                    " AND status IN ('surfaced','parked')",
                    (r["partial_fp"],)).fetchone()["n"]
                L.append(f"  - {r['record_title']} - {r['canon_field']} = "
                         f"`{r['value_disp']}` ({nsrc} source(s)) - "
                         f"{r['doc_title']}")
                L.append(f"      > {_fmt_quote(r['quote'])}")
        L.append("")

    suggestions = _alias_suggestions(con)
    if suggestions:
        L.append("\n## Equivalent-term suggestions\n")
        L.append("*Definition-profile mappings that may merit promotion into "
                 "`field_aliases.json`.*\n")
        for s in suggestions:
            L.append(f"- `{s['term']}` -> **{s['field']}** "
                     f"({s['count']} occurrence(s), average score "
                     f"{s['average_score']:.2f})")
            for quote in s["example_quotes"][:2]:
                L.append(f"    > {_fmt_quote(quote)}")
        L.append("")

    md = "\n".join(L)
    path = os.path.join(out_dir, report_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path, props


def _emit(L, p):
    tag = {"gap_fill": "gap-fill", "relation": "relation"}.get(p["type"], p["type"])
    nclust = p.get("cluster_sources", p["n_sources"])
    corr = f" - corroborated by {nclust} sources" if nclust > 1 else ""
    mapping = (" - experimental mapping"
               if p.get("mapping_tier") == "medium" else "")
    if p["type"] == "relation":
        L.append(f"- **relation**: {p['value']}{corr}{mapping}")
    else:
        L.append(f"- **{p['field']}** = `{p['value']}` ({tag}){corr}{mapping}")
    if p["qualifier"]:
        L.append(f"    - Qualifier: _{p['qualifier']}_")
    if nclust > 1 and len(p["value_distribution"]) > 1:
        L.append("    - Value distribution across sources: "
                 + ", ".join(f"{n} source(s) say `{v}`"
                             for v, n in p["value_distribution"].items()))
    for s in p["sources"]:
        L.append(f"    - {s['doc_title']}  ({s['doc_path']})")
        L.append(f"      > {_fmt_quote(s['quote'])}")
    L.append("")
