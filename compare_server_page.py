#!/usr/bin/env python3
"""compare_server_page.py - the single-page UI for compare_server.py.
Kept separate so the server module stays focused on the API."""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RAG model bench</title>
<style>
  /* ---- design tokens ----
     subject: a developer's instrument for comparing RAG output across model
     capability tiers. organizing idea: a capability ladder. palette is a cool
     graphite workshop with a single signal-amber accent for the active state
     and a teal for retrieval/context (data, not action). type pairs a tight
     grotesque for UI chrome with a mono for everything the model actually sees. */
  :root{
    --bg:#0d0f13; --bg2:#12151b; --panel:#161a22; --panel2:#1b2029;
    --line:#272d39; --line2:#333b49;
    --ink:#e9edf4; --mut:#8a94a6; --dim:#5c6576;
    --amber:#e0a44e; --amber-ink:#1a1206;
    --teal:#54b5ab; --bad:#cf6679; --good:#5cb98a;
    --r:9px;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
    --ui:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--ink);font:14px/1.5 var(--ui);
    -webkit-font-smoothing:antialiased}
  ::selection{background:var(--amber);color:var(--amber-ink)}

  .wrap{display:grid;grid-template-columns:288px 1fr;height:100vh}
  @media (max-width:880px){.wrap{grid-template-columns:1fr;height:auto}}

  /* ---- left rail: the capability ladder ---- */
  .rail{background:var(--bg2);border-right:1px solid var(--line);
    overflow-y:auto;padding:18px 16px 28px}
  .brand{display:flex;align-items:baseline;gap:8px;margin:2px 2px 4px}
  .brand b{font-size:15px;letter-spacing:-.01em}
  .brand span{color:var(--dim);font-size:11px;font-family:var(--mono)}
  .rail-note{color:var(--mut);font-size:11.5px;margin:0 2px 16px;line-height:1.45}
  .ladder-head{display:flex;justify-content:space-between;align-items:center;
    margin:0 2px 8px}
  .ladder-head .lbl{font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
    color:var(--dim)}
  .ladder-head .count{font-family:var(--mono);font-size:11px;color:var(--amber)}

  .model{position:relative;display:block;border:1px solid var(--line);
    border-radius:var(--r);padding:11px 12px 12px 13px;margin-bottom:9px;
    cursor:pointer;background:var(--panel);transition:border-color .12s,background .12s}
  .model:hover{border-color:var(--line2);background:var(--panel2)}
  .model.sel{border-color:var(--amber);background:#1d1a12}
  .model.disabled{opacity:.42;cursor:not-allowed}
  .model .top{display:flex;align-items:center;gap:8px;margin-bottom:5px}
  .tier{font-family:var(--mono);font-size:9.5px;letter-spacing:.04em;
    text-transform:uppercase;color:var(--teal);border:1px solid var(--line2);
    border-radius:4px;padding:1px 5px;white-space:nowrap}
  .model.sel .tier{color:var(--amber);border-color:#5a4a24}
  .model .nm{font-weight:600;font-size:13px;letter-spacing:-.01em}
  .model .desc{color:var(--mut);font-size:11.5px;line-height:1.4;margin:4px 0 8px}
  .model .hw{display:flex;flex-wrap:wrap;gap:4px 6px;font-family:var(--mono);
    font-size:10px;color:var(--dim)}
  .model .hw b{color:var(--mut);font-weight:500}
  .pick{position:absolute;top:11px;right:11px;width:16px;height:16px;border-radius:5px;
    border:1.5px solid var(--line2);display:grid;place-items:center}
  .model.sel .pick{background:var(--amber);border-color:var(--amber)}
  .model.sel .pick::after{content:"";width:8px;height:8px;border-radius:2px;
    background:var(--amber-ink)}

  /* ---- right: query + context + outputs ---- */
  .main{overflow-y:auto;padding:22px 26px 60px;min-width:0}
  .qbar{display:flex;gap:10px;align-items:stretch;margin-bottom:8px}
  .qbar textarea{flex:1;resize:vertical;min-height:46px;max-height:160px;
    background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
    color:var(--ink);padding:12px 14px;font:14px/1.5 var(--ui)}
  .qbar textarea:focus{outline:none;border-color:var(--line2)}
  .qbar textarea::placeholder{color:var(--dim)}
  .send{flex:0 0 auto;background:var(--amber);color:var(--amber-ink);border:0;
    border-radius:var(--r);padding:0 20px;font:600 14px var(--ui);cursor:pointer;
    letter-spacing:.01em}
  .send:disabled{opacity:.5;cursor:wait}
  .opts{display:flex;align-items:center;gap:16px;margin:2px 2px 18px;
    color:var(--mut);font-size:12px}
  .opts label{display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none}
  .opts label.sub{color:var(--dim)}
  .opts label.disabled{opacity:.4;cursor:not-allowed}
  .opts input{accent-color:var(--amber)}
  .opts input:disabled{cursor:not-allowed}
  .opts .hint{color:var(--dim);font-family:var(--mono);font-size:11px;margin-left:auto}

  details.ctx{border:1px solid var(--line);border-radius:var(--r);
    background:var(--bg2);margin-bottom:20px;overflow:hidden}
  details.ctx>summary{list-style:none;cursor:pointer;padding:12px 15px;
    display:flex;align-items:center;gap:10px;font-size:12.5px}
  details.ctx>summary::-webkit-details-marker{display:none}
  .ctx .chev{color:var(--dim);transition:transform .15s;font-family:var(--mono)}
  details.ctx[open] .chev{transform:rotate(90deg)}
  .ctx .summlbl{font-weight:600;letter-spacing:.02em}
  .ctx .summmeta{color:var(--dim);font-family:var(--mono);font-size:11px;margin-left:auto}
  .ctx-body{border-top:1px solid var(--line);padding:14px 15px;display:grid;gap:14px}
  .ctx-sec .h{font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;
    color:var(--teal);margin:0 0 6px;font-family:var(--mono)}
  .filter-line{font-family:var(--mono);font-size:11.5px;color:var(--mut);
    background:var(--panel);border:1px solid var(--line);border-radius:6px;
    padding:7px 9px;white-space:pre-wrap;word-break:break-word}
  .src{border-left:2px solid var(--teal);padding:6px 0 6px 11px;margin:8px 0}
  .src .rid{font-family:var(--mono);font-size:11px;color:var(--teal)}
  .src .ti{font-size:12px;color:var(--ink);margin-left:6px}
  .src pre{margin:5px 0 0;font-family:var(--mono);font-size:11px;color:var(--mut);
    white-space:pre-wrap;word-break:break-word}
  /* structured table: exact fields per matched record */
  table.rec{border-collapse:collapse;width:100%;font-size:11.5px}
  table.rec th,table.rec td{border:1px solid var(--line);padding:6px 8px;
    text-align:left;vertical-align:top}
  table.rec th{color:var(--teal);font-family:var(--mono);font-size:10.5px;
    text-transform:uppercase;letter-spacing:.06em;cursor:pointer;
    white-space:nowrap;user-select:none;background:var(--panel)}
  table.rec th.sorted::after{content:" \25BE";color:var(--amber)}
  table.rec td .u{color:var(--mut)}
  table.rec td .descr{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
    overflow:hidden;color:var(--dim);font-size:10px;margin-top:2px;max-width:280px;cursor:help}
  table.rec td.rid{font-family:var(--mono);color:var(--teal);white-space:nowrap}
  table.rec tr:hover td{background:var(--panel)}
  pre.prompt{margin:0;font-family:var(--mono);font-size:11.5px;line-height:1.55;
    color:var(--ink);background:var(--panel);border:1px solid var(--line);
    border-radius:6px;padding:12px;white-space:pre-wrap;word-break:break-word;
    max-height:340px;overflow:auto}

  /* ---- output columns ---- */
  .outwrap{display:grid;gap:14px}
  .outwrap.n1{grid-template-columns:1fr}
  .outwrap.n2{grid-template-columns:1fr 1fr}
  .outwrap.n3{grid-template-columns:1fr 1fr 1fr}
  @media (max-width:1100px){.outwrap.n3,.outwrap.n2{grid-template-columns:1fr}}
  .out{border:1px solid var(--line);border-radius:var(--r);background:var(--panel);
    display:flex;flex-direction:column;min-height:120px}
  .out .head{display:flex;align-items:center;gap:8px;padding:11px 13px;
    border-bottom:1px solid var(--line)}
  .out .head .nm{font-weight:600;font-size:13px}
  .out .head .tier{margin-left:0}
  .out .head .lat{margin-left:auto;font-family:var(--mono);font-size:10.5px;color:var(--dim)}
  .out .body{padding:13px;font-size:13px;line-height:1.55;white-space:pre-wrap;
    word-break:break-word;flex:1}
  .out .body.err{color:var(--bad);font-family:var(--mono);font-size:12px}
  .out .body.wait{color:var(--dim);font-family:var(--mono);font-size:12px}
  .cite{color:var(--teal);font-family:var(--mono);font-size:.92em}

  .empty{color:var(--dim);font-size:13px;text-align:center;padding:50px 20px;
    border:1px dashed var(--line);border-radius:var(--r)}
  .spinner{display:inline-block;width:11px;height:11px;border:2px solid var(--line2);
    border-top-color:var(--amber);border-radius:50%;animation:spin .7s linear infinite;
    vertical-align:-1px;margin-right:6px}
  @keyframes spin{to{transform:rotate(360deg)}}
  @media (prefers-reduced-motion:reduce){.spinner{animation:none}}
</style>
</head>
<body>
<div class="wrap">
  <aside class="rail">
    <div class="brand"><b>RAG model bench</b><span>v1</span></div>
    <p class="rail-note">Pick up to three models, send the same retrieved context
      to each, and read the answers side by side.</p>
    <div class="ladder-head">
      <span class="lbl">Capability ladder</span>
      <span class="count" id="count">0 / 3</span>
    </div>
    <div id="ladder"></div>
  </aside>

  <main class="main">
    <div class="qbar">
      <textarea id="q" placeholder="Ask something about your records&#10;e.g. which active pumps run hotter than 150&#176;C?"></textarea>
      <button class="send" id="send">Compare</button>
    </div>
    <div class="opts">
      <label><input type="checkbox" id="autofilter"> Model-driven metadata filter</label>
      <label class="sub disabled" id="twopasslbl" title="Broad category first, then category-specific detail fields — smaller, sharper filter prompt on a large catalogue (2 LLM calls)."><input type="checkbox" id="twopass" disabled> two-pass</label>
      <label><input type="checkbox" id="ctxonly"> Preview context only</label>
      <span class="hint" id="hint">select models to begin</span>
    </div>

    <details class="ctx" id="ctxbox" style="display:none">
      <summary>
        <span class="chev">&#9656;</span>
        <span class="summlbl">Context sent to models</span>
        <span class="summmeta" id="ctxmeta"></span>
      </summary>
      <div class="ctx-body">
        <div class="ctx-sec" id="filtersec" style="display:none">
          <p class="h">Applied filter</p>
          <div class="filter-line" id="filterline"></div>
        </div>
        <div class="ctx-sec">
          <p class="h">Retrieved records (full passages)</p>
          <div id="srcs"></div>
        </div>
        <div class="ctx-sec" id="tablesec" style="display:none">
          <p class="h">Structured fields &mdash; all matched records</p>
          <div id="tablewrap" style="overflow-x:auto"></div>
        </div>
        <div class="ctx-sec" id="digestsec" style="display:none">
          <p class="h">Fuller matched set &mdash; snippet each</p>
          <div id="digest"></div>
        </div>
        <div class="ctx-sec">
          <p class="h">Full prompt</p>
          <pre class="prompt" id="prompt"></pre>
        </div>
      </div>
    </details>

    <div id="outzone">
      <div class="empty">Output appears here once you compare.</div>
    </div>
  </main>
</div>

<script>
const state = { models: [], selected: [] };

const el = (id) => document.getElementById(id);
function esc(s){return (s==null?"":String(s)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
// render [REC-ID] citations in answers as teal tokens (ids may contain #, /, .)
function citify(s){return esc(s).replace(/\[([^\]\n]+)\]/g,'<span class="cite">[$1]</span>');}

async function loadModels(){
  const r = await fetch("/api/models");
  state.models = await r.json();
  renderLadder();
}

function renderLadder(){
  const L = el("ladder");
  L.innerHTML = "";
  state.models.forEach(m => {
    const sel = state.selected.includes(m.id);
    const unavailable = m.available === false;   // verified missing on OpenRouter
    const full = state.selected.length >= 3 && !sel;
    const blocked = unavailable || full;
    const div = document.createElement("div");
    div.className = "model" + (sel?" sel":"") + (blocked?" disabled":"");
    const badge = unavailable
      ? '<span class="tier" style="color:var(--bad);border-color:var(--bad)">unavailable</span>'
      : `<span class="tier">${esc(m.tier)}</span>`;
    div.innerHTML = `
      <div class="pick"></div>
      <div class="top">${badge}
        <span class="nm">${esc(m.name)}</span></div>
      <div class="desc">${esc(m.desc)}</div>
      <div class="hw"><b>${esc(m.params)}</b> &middot; <span>${esc(m.vram)} VRAM</span>
        &middot; <span>${esc(m.hardware)}</span></div>`;
    if(!blocked) div.onclick = () => toggle(m.id);
    L.appendChild(div);
  });
  el("count").textContent = state.selected.length + " / 3";
  updateHint();
}

function toggle(id){
  const i = state.selected.indexOf(id);
  if(i>=0) state.selected.splice(i,1);
  else if(state.selected.length < 3) state.selected.push(id);
  renderLadder();
}

function updateHint(){
  const h = el("hint");
  if(state.selected.length === 0) h.textContent = "select models to begin";
  else h.textContent = state.selected.length + " selected";
}

function renderContext(d){
  el("ctxbox").style.display = "";
  const nsrc = d.sources ? d.sources.length : 0;
  el("ctxmeta").textContent = nsrc + " record" + (nsrc===1?"":"s");
  // filter
  const fa = d.filter && d.filter.applied && Object.keys(d.filter.applied).length;
  const plan = d.filter && d.filter.plan;
  const showPlan = plan || fa || (d.filter && (d.filter.errors||[]).length);
  el("filtersec").style.display = showPlan ? "" : "none";
  if(showPlan){
    let txt = fa ? JSON.stringify(d.filter.applied) : "# no complete filter applied";
    if(plan){
      txt += "\n# route: " + (plan.route||"hybrid") +
             "; confidence: " + (plan.confidence||"unknown") +
             "; complete: " + String(plan.complete !== false);
      if(plan.stop_reason) txt += "\n# stop: " + plan.stop_reason;
      if((plan.unresolved_constraints||[]).length)
        txt += "\n# unresolved: " + JSON.stringify(plan.unresolved_constraints);
      if((plan.ambiguities||[]).length)
        txt += "\n# ambiguities: " + JSON.stringify(plan.ambiguities);
    }
    if(typeof d.filter.matched_records === "number"){
      const shown = d.sources ? d.sources.length : 0;
      txt += "\n# matched " + d.filter.matched_records +
             " record" + (d.filter.matched_records===1?"":"s") +
             "; showing top " + shown;
    }
    if(d.filter.filter_mode){
      const m = d.filter.filter_mode;
      const note = m==="soft" ? " (rank boost — non-matching records can still surface)"
                 : m==="fill" ? " (k-guard: all matches shown, remaining slots topped up by search)"
                 : m==="hard" ? " (hard gate — only matching records eligible)" : "";
      txt += "\n# apply mode: " + m + note;
    }
    if(d.filter.two_pass){
      const tp = d.filter.two_pass;
      txt += "\n# two-pass: pass1 " + JSON.stringify(tp.pass1_filter||{}) +
             " [" + tp.pass1_fields + " partition fields] → pass2 [" +
             tp.pass2_fields + " in-category detail fields]";
    }
    if(d.filter.errors && d.filter.errors.length) txt += "\n# dropped: " + d.filter.errors.join("; ");
    el("filterline").textContent = txt;
  }
  // sources
  el("srcs").innerHTML = (d.sources||[]).map(s =>
    `<div class="src"><span class="rid">[${esc(s.rid)}]</span>`+
    `<span class="ti">${esc(s.title)}</span><pre>${esc(s.text)}</pre></div>`).join("")
    || '<div class="filter-line">No records retrieved.</div>';
  // structured table of the fuller matched set (analytic queries)
  const tbl = d.filter && d.filter.table;
  el("tablesec").style.display = (tbl && tbl.rows && tbl.rows.length) ? "" : "none";
  if(tbl && tbl.rows && tbl.rows.length) renderTable(tbl);
  // digest of the fuller matched set (breadth: 1 snippet per additional record)
  const dig = (d.filter && d.filter.digest) || [];
  el("digestsec").style.display = dig.length ? "" : "none";
  el("digest").innerHTML = dig.map(x =>
    `<div class="src"><span class="rid">[${esc(x.rid)}]</span>`+
    `<span class="ti">${esc(x.title)}</span><pre>${esc(x.snippet)}</pre></div>`).join("");
  el("prompt").textContent = (d.system_prompt? "SYSTEM:\n"+d.system_prompt+"\n\nUSER:\n":"") + (d.prompt||"");
}

// ---- structured table (exact fields per matched record, click-to-sort) ----
let _tableState = {tbl:null, sortCol:null, asc:false};
function renderTable(tbl){ _tableState = {tbl, sortCol:null, asc:false}; drawTable(); }
function cellSortVal(cell){
  if(!cell || cell.value==null || cell.value==="") return -Infinity;
  const num = parseFloat(String(cell.value).replace(/[, ]/g,""));
  return isNaN(num) ? String(cell.value).toLowerCase() : num;
}
function drawTable(){
  const {tbl, sortCol, asc} = _tableState;
  const cols = tbl.columns;
  let rows = tbl.rows.slice();
  if(sortCol){
    rows.sort((a,b)=>{
      const av=cellSortVal(a.cells[sortCol]), bv=cellSortVal(b.cells[sortCol]);
      const cmp = av<bv?-1:(av>bv?1:0); return asc?cmp:-cmp;
    });
  }
  let h = '<table class="rec"><thead><tr><th data-c="__rec">Record</th>';
  cols.forEach(c=> h += `<th data-c="${esc(c)}" class="${c===sortCol?'sorted':''}">${esc(c)}</th>`);
  h += '</tr></thead><tbody>';
  rows.forEach(r=>{
    h += `<tr><td class="rid">[${esc(r.rid)}] ${esc(r.title)}</td>`;
    cols.forEach(c=>{
      const cell = r.cells[c]||{};
      if(cell.value==null || cell.value===""){ h+='<td>&mdash;</td>'; return; }
      let s = `<span class="val">${esc(cell.value)}</span>`;
      if(cell.unit) s += ` <span class="u">${esc(cell.unit)}</span>`;
      if(cell.descr) s += `<span class="descr" title="${esc(cell.descr)}">${esc(cell.descr)}</span>`;
      h += `<td>${s}</td>`;
    });
    h += '</tr>';
  });
  h += '</tbody></table>';
  if(tbl.total > tbl.rows.length) h += `<div class="filter-line">+${tbl.total-tbl.rows.length} more not shown</div>`;
  el("tablewrap").innerHTML = h;
  el("tablewrap").querySelectorAll("th").forEach(th=> th.onclick = ()=>{
    const c = th.getAttribute("data-c");
    if(c==="__rec"){ _tableState.sortCol=null; }
    else if(_tableState.sortCol===c){ _tableState.asc=!_tableState.asc; }
    else { _tableState.sortCol=c; _tableState.asc=false; }
    drawTable();
  });
}

function renderOutputs(models, results){
  const zone = el("outzone");
  const n = Math.min(models.length, 3);
  zone.innerHTML = `<div class="outwrap n${n}" id="outwrap"></div>`;
  const wrap = el("outwrap");
  models.forEach(mid => {
    const m = state.models.find(x=>x.id===mid) || {name:mid,tier:""};
    const r = results ? results.find(x=>x.model_id===mid) : null;
    const card = document.createElement("div");
    card.className = "out";
    let body;
    if(!results){
      body = '<div class="body wait"><span class="spinner"></span>waiting&#8230;</div>';
    } else if(r && r.error){
      body = `<div class="body err">ERROR: ${esc(r.error)}</div>`;
    } else if(r){
      body = `<div class="body">${citify(r.answer)}</div>`;
    } else {
      body = '<div class="body err">no result</div>';
    }
    const lat = (r && !r.error) ? `<span class="lat">${r.latency_s}s</span>` : "";
    card.innerHTML = `<div class="head"><span class="tier">${esc(m.tier)}</span>`+
      `<span class="nm">${esc(m.name)}</span>${lat}</div>${body}`;
    wrap.appendChild(card);
  });
}

// two-pass only applies when the model-driven filter is on; enable/disable to match
function syncTwoPass(){
  const on = el("autofilter").checked;
  const tp = el("twopass");
  tp.disabled = !on;
  if(!on) tp.checked = false;
  el("twopasslbl").classList.toggle("disabled", !on);
}

async function run(){
  const q = el("q").value.trim();
  if(!q){ el("q").focus(); return; }
  const autofilter = el("autofilter").checked;
  const twopass = autofilter && el("twopass").checked;
  const ctxonly = el("ctxonly").checked;

  if(!ctxonly && state.selected.length === 0){
    el("hint").textContent = "pick at least one model first";
    return;
  }
  const btn = el("send"); btn.disabled = true;

  try {
    if(ctxonly){
      const r = await fetch("/api/context",{method:"POST",
        headers:{"content-type":"application/json"},
        body:JSON.stringify({query:q, auto_filter:autofilter, two_pass:twopass})});
      const d = await r.json();
      if(d.error){ el("outzone").innerHTML = `<div class="empty">${esc(d.error)}</div>`; }
      else { renderContext(d); el("ctxbox").open = true;
             el("outzone").innerHTML = '<div class="empty">Context preview only &mdash; uncheck to run models.</div>'; }
    } else {
      // show columns in waiting state immediately
      renderOutputs(state.selected, null);
      const r = await fetch("/api/compare",{method:"POST",
        headers:{"content-type":"application/json"},
        body:JSON.stringify({query:q, models:state.selected, auto_filter:autofilter, two_pass:twopass})});
      const d = await r.json();
      if(d.error){ el("outzone").innerHTML = `<div class="empty">${esc(d.error)}</div>`; }
      else { renderContext(d); renderOutputs(state.selected, d.results); }
    }
  } catch(e){
    el("outzone").innerHTML = `<div class="empty">Request failed: ${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

el("send").onclick = run;
el("autofilter").addEventListener("change", syncTwoPass);
el("q").addEventListener("keydown", e => {
  if((e.metaKey||e.ctrlKey) && e.key==="Enter") run();
});
syncTwoPass();
loadModels();
</script>
</body>
</html>
"""
