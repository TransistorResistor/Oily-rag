#!/usr/bin/env python3
"""Read-only web demo for reviewing reverse-enrichment proposals."""

import argparse
import json
import os
import threading
import webbrowser

from flask import Flask, Response, jsonify


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROPOSALS = os.path.join(HERE, "proposals.json")
app = Flask(__name__)
CONFIG = {"proposals": DEFAULT_PROPOSALS}


def load_proposals():
    path = CONFIG["proposals"]
    if not os.path.exists(path):
        raise RuntimeError(f"proposals file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise RuntimeError("proposals JSON must contain a list")
    return data


@app.get("/api/proposals")
def api_proposals():
    try:
        proposals = load_proposals()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    counts = {}
    for proposal in proposals:
        kind = proposal.get("type") or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    return jsonify({
        "source": os.path.basename(CONFIG["proposals"]),
        "total": len(proposals), "counts": counts, "proposals": proposals,
    })


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "read_only": True,
                    "proposals": CONFIG["proposals"]})


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


def run_server(proposals=DEFAULT_PROPOSALS, host="127.0.0.1", port=8100,
               open_browser=True):
    CONFIG["proposals"] = os.path.abspath(proposals)
    if host not in ("127.0.0.1", "localhost"):
        print(f"WARNING: enrichment demo is exposed on {host}")
    url = f"http://localhost:{port}"
    print(f"Enrichment review demo -> {url}")
    print(f"Read-only source: {CONFIG['proposals']}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, threaded=True)


def main():
    ap = argparse.ArgumentParser(description="read-only enrichment proposal demo")
    ap.add_argument("--proposals", default=DEFAULT_PROPOSALS)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    run_server(args.proposals, args.host, args.port, not args.no_open)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Enrichment review</title>
<style>
:root{--bg:#0d0f13;--panel:#161a22;--panel2:#1b2029;--line:#2a303b;
--ink:#edf1f7;--mut:#9099aa;--dim:#626c7c;--amber:#e0a44e;--teal:#54b5ab;
--red:#d77888;--green:#65bd91;--mono:ui-monospace,SFMono-Regular,Consolas,monospace;
--ui:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--ui)}
header{border-bottom:1px solid var(--line);padding:22px max(24px,calc((100vw - 1180px)/2));
display:flex;justify-content:space-between;gap:20px;align-items:end;background:#11141a}
h1{font-size:20px;margin:0 0 3px;letter-spacing:-.02em}.sub{color:var(--mut);font-size:12px}
.readonly{font:10px var(--mono);color:var(--teal);border:1px solid #315c58;border-radius:20px;padding:5px 9px}
main{max-width:1180px;margin:auto;padding:24px}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:18px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:13px 14px}.stat b{font:22px var(--mono)}
.stat span{display:block;color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.1em}
.tools{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap}input,select{background:var(--panel);color:var(--ink);
border:1px solid var(--line);border-radius:8px;padding:10px 12px;font:13px var(--ui)}input{flex:1;min-width:240px}
.meta{margin-left:auto;color:var(--dim);font:11px var(--mono);align-self:center}.grid{display:grid;gap:11px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:9px;overflow:hidden}.card summary{cursor:pointer;
list-style:none;padding:14px 16px;display:grid;grid-template-columns:100px 1.2fr 1fr auto;gap:14px;align-items:center}
.card summary::-webkit-details-marker{display:none}.card[open]{border-color:#3b4452}.tag{font:10px var(--mono);text-transform:uppercase;
letter-spacing:.06em;padding:4px 7px;border-radius:4px;width:max-content}.conflict{color:var(--red);background:#2b171c}.gap_fill{color:var(--green);background:#14271e}.relation{color:var(--teal);background:#142624}
.record{font-weight:650}.field{color:var(--mut)}.value{font:12px var(--mono);color:var(--amber);text-align:right}.body{border-top:1px solid var(--line);padding:15px 16px;background:var(--panel2)}
.compare{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}.box{border:1px solid var(--line);border-radius:7px;padding:10px}.box span{display:block;color:var(--dim);font:9px var(--mono);text-transform:uppercase;letter-spacing:.1em}.box b{font:13px var(--mono)}
.source{border-left:2px solid var(--teal);padding:8px 11px;margin-top:9px}.source .where{font-size:11px;color:var(--mut)}blockquote{margin:5px 0 0;color:var(--ink);font-size:12px}.empty{text-align:center;color:var(--dim);padding:55px;border:1px dashed var(--line);border-radius:9px}
@media(max-width:760px){header{align-items:start}.stats{grid-template-columns:1fr 1fr}.card summary{grid-template-columns:90px 1fr}.field,.value{grid-column:2}.value{text-align:left}.compare{grid-template-columns:1fr}.meta{width:100%;margin:0}}
</style></head><body>
<header><div><h1>Reverse-enrichment review</h1><div class="sub">Inspect proposed catalogue changes with their grounded source evidence.</div></div><div class="readonly">READ ONLY</div></header>
<main><section class="stats" id="stats"></section><div class="tools"><input id="search" placeholder="Search record, field, value, or source"><select id="kind"><option value="">All proposal types</option><option value="gap_fill">Gap fills</option><option value="conflict">Conflicts</option><option value="relation">Relations</option></select><div class="meta" id="meta"></div></div><section class="grid" id="grid"><div class="empty">Loading proposals...</div></section></main>
<script>
const state={items:[],source:""}; const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const label=t=>({gap_fill:"gap fill",conflict:"conflict",relation:"relation"}[t]||t||"unknown");
function renderStats(counts,total){document.querySelector("#stats").innerHTML=[['Total',total],['Gap fills',counts.gap_fill||0],['Conflicts',counts.conflict||0],['Relations',counts.relation||0]].map(([n,v])=>`<div class="stat"><b>${v}</b><span>${n}</span></div>`).join("")}
function card(p){const sources=(p.sources||[]).map(s=>`<div class="source"><div class="where">${esc(s.doc_title)} · ${esc(s.doc_path)}</div><blockquote>“${esc(s.quote)}”</blockquote></div>`).join("");return `<details class="card"><summary><span class="tag ${esc(p.type)}">${esc(label(p.type))}</span><span class="record">${esc(p.record)}</span><span class="field">${esc(p.field||'Relationship')}</span><span class="value">${esc(p.value)}</span></summary><div class="body"><div class="compare"><div class="box"><span>Catalogue value</span><b>${esc(p.db_value??'Not present')}</b></div><div class="box"><span>Proposed value</span><b>${esc(p.value)}</b></div></div><div class="sub">${esc(p.n_sources||0)} source(s) · proposal ${esc(p.proposal_id)}</div>${sources}</div></details>`}
function render(){const q=document.querySelector("#search").value.trim().toLowerCase(),k=document.querySelector("#kind").value;const rows=state.items.filter(p=>(!k||p.type===k)&&(!q||JSON.stringify(p).toLowerCase().includes(q)));document.querySelector("#meta").textContent=`${rows.length} shown · ${state.source}`;document.querySelector("#grid").innerHTML=rows.length?rows.map(card).join(""):'<div class="empty">No proposals match these filters.</div>'}
async function boot(){try{const r=await fetch('/api/proposals'),d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);state.items=d.proposals;state.source=d.source;renderStats(d.counts,d.total);render()}catch(e){document.querySelector("#grid").innerHTML=`<div class="empty">${esc(e.message)}</div>`}}
document.querySelector("#search").addEventListener("input",render);document.querySelector("#kind").addEventListener("change",render);boot();
</script></body></html>"""


if __name__ == "__main__":
    main()
