"""
app/graphview.py — Interactive codebase map, served at GET /graph.

Same constraints as dashboard.py: one self-contained HTML string, no build
step, no CDN. The force simulation and canvas rendering are hand-written
because a graph library would mean an external script tag, which the Render
free tier and a strict CSP both make awkward.

The page fetches its data from GET /graph.json, which serves whatever
app.intelligence.codegraph produced. That endpoint is auth-gated the same way
/health is: a dependency graph is a map of the codebase, so it should not be
public on a private deployment.

Why a force layout and not a tree: import structure is a general graph, not a
hierarchy. Modules that clump together on screen are modules that actually
depend on each other, which is the thing a folder tree cannot show you.
"""

GRAPH_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="robots" content="noindex"/>
<title>Codebase Map — GitHub Autopilot</title>
<style>
  :root {
    --bg:#0b1120; --panel:#111827; --border:#1f2937; --text:#e5e7eb;
    --dim:#9ca3af; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444;
    --cyan:#22d3ee; --violet:#818cf8;
  }
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body { margin:0; background:var(--bg); color:var(--text); overflow:hidden;
    font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:14px 20px; border-bottom:1px solid var(--border);
    display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; font-weight:650; }
  header .stat { font-size:12px; color:var(--dim); }
  header .stat b { color:var(--text); font-weight:600; }
  #wrap { display:flex; height:calc(100% - 53px); }
  #stage { flex:1; position:relative; }
  canvas { display:block; width:100%; height:100%; cursor:grab; }
  canvas.dragging { cursor:grabbing; }
  aside { width:310px; border-left:1px solid var(--border); background:var(--panel);
    padding:16px 18px; overflow-y:auto; }
  aside h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
    color:var(--dim); margin:0 0 10px; font-weight:600; }
  aside section { margin-bottom:22px; }
  .legend-item { display:flex; align-items:center; gap:8px; margin-bottom:6px; font-size:13px; }
  .swatch { width:11px; height:11px; border-radius:50%; flex:none; }
  .listrow { display:flex; justify-content:space-between; gap:8px; padding:5px 0;
    border-bottom:1px solid var(--border); font-size:12.5px; }
  .listrow:last-child { border-bottom:none; }
  .listrow span:first-child { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .listrow span:last-child { color:var(--dim); flex:none; font-variant-numeric:tabular-nums; }
  .listrow.clickable { cursor:pointer; }
  .listrow.clickable:hover span:first-child { color:var(--cyan); }
  .pill { font-size:11px; padding:2px 8px; border-radius:999px; font-weight:600; }
  .pill.bad { background:rgba(239,68,68,.15); color:var(--bad); }
  .pill.warn { background:rgba(245,158,11,.15); color:var(--warn); }
  .pill.ok { background:rgba(34,197,94,.15); color:var(--ok); }
  #detail { font-size:13px; }
  #detail .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
    color:var(--dim); word-break:break-all; }
  #detail .kv { display:flex; justify-content:space-between; padding:4px 0; }
  #detail .kv span:last-child { color:var(--dim); font-variant-numeric:tabular-nums; }
  #search { width:100%; padding:7px 10px; background:var(--bg); color:var(--text);
    border:1px solid var(--border); border-radius:8px; font-size:13px; }
  #search:focus { outline:2px solid var(--cyan); outline-offset:-1px; }
  .btn { background:var(--bg); color:var(--text); border:1px solid var(--border);
    border-radius:8px; padding:6px 11px; font-size:12.5px; cursor:pointer; }
  .btn:hover { border-color:var(--cyan); color:var(--cyan); }
  .empty { color:var(--dim); font-size:13px; }
  #tooltip { position:absolute; pointer-events:none; background:var(--panel);
    border:1px solid var(--border); border-radius:8px; padding:7px 10px; font-size:12.5px;
    display:none; max-width:280px; z-index:5; }
  #err { padding:40px; color:var(--dim); }
  @media (max-width:820px) { aside { display:none; } }
</style>
</head>
<body>
<header>
  <h1>Codebase Map</h1>
  <span class="stat"><b id="s-mod">—</b> modules</span>
  <span class="stat"><b id="s-edge">—</b> imports</span>
  <span class="stat"><b id="s-loc">—</b> lines</span>
  <span class="stat" id="s-cyc"></span>
  <span style="flex:1"></span>
  <button class="btn" id="reheat">Re-layout</button>
</header>
<div id="wrap">
  <div id="stage">
    <canvas id="cv"></canvas>
    <div id="tooltip"></div>
  </div>
  <aside>
    <section>
      <h2>Find</h2>
      <input id="search" placeholder="filter modules…" autocomplete="off"/>
    </section>
    <section>
      <h2>Layers</h2>
      <div id="legend"></div>
    </section>
    <section>
      <h2>Selected</h2>
      <div id="detail"><div class="empty">Click a node.</div></div>
    </section>
    <section>
      <h2>Hotspots</h2>
      <div id="hotspots"></div>
    </section>
    <section>
      <h2>Unreferenced</h2>
      <div id="orphans"></div>
    </section>
  </aside>
</div>
<div id="err" style="display:none"></div>

<script>
"use strict";
const $ = id => document.getElementById(id);

// Layer colours. Fixed rather than generated so a module keeps its colour
// between runs — the map should look like the same map each time you open it.
const LAYER_COLORS = {
  handlers:"#22d3ee", core:"#818cf8", ai:"#f472b6", github:"#34d399",
  security:"#f59e0b", intelligence:"#a78bfa", mcp:"#60a5fa",
  tests:"#6b7280", evals:"#6b7280", other:"#9ca3af"
};
const colorFor = l => LAYER_COLORS[l] || LAYER_COLORS.other;

let NODES=[], EDGES=[], STATS={}, SELECTED=null, HOVER=null, FILTER="";
let alpha=1, cam={x:0,y:0,k:1}, dragNode=null, panning=false, last={x:0,y:0};

const cv=$('cv'), ctx=cv.getContext('2d');
let W=0,H=0,DPR=Math.min(window.devicePixelRatio||1,2);

function resize(){
  const r=cv.getBoundingClientRect();
  W=r.width; H=r.height;
  cv.width=W*DPR; cv.height=H*DPR;
  ctx.setTransform(DPR,0,0,DPR,0,0);
}
window.addEventListener('resize',()=>{resize();});

// ── Data ────────────────────────────────────────────────────────────────────
function token(){
  let t=sessionStorage.getItem('metrics_token');
  if(t===null){
    t=prompt('METRICS_AUTH_TOKEN (blank if unset):')||'';
    sessionStorage.setItem('metrics_token',t);
  }
  return t;
}

async function load(){
  let res;
  try{
    const t=token();
    res=await fetch('/graph.json',{headers:t?{'Authorization':'Bearer '+t}:{}});
  }catch(e){ return fail('Could not reach /graph.json'); }
  if(res.status===401){
    sessionStorage.removeItem('metrics_token');
    return fail('Unauthorized. Reload and enter the correct METRICS_AUTH_TOKEN.');
  }
  if(res.status===404){
    return fail('No graph has been generated yet. Run:<br><br>'+
      '<code>python -m app.intelligence.codegraph app server.py worker.py '+
      '--out docs/diagrams/codegraph.json</code>');
  }
  if(!res.ok) return fail('Failed to load graph ('+res.status+').');

  const data=await res.json();
  STATS=data.stats||{};
  // Seed positions on a circle: starting every node at the centre makes the
  // first frames a single overlapping blob that the simulation has to climb
  // out of, which looks broken even though it converges.
  const n=(data.nodes||[]).length;
  NODES=(data.nodes||[]).map((d,i)=>{
    const a=(i/Math.max(n,1))*Math.PI*2, r=Math.min(W,H)*0.32||220;
    return Object.assign({},d,{
      x:Math.cos(a)*r, y:Math.sin(a)*r, vx:0, vy:0,
      r:Math.max(4,Math.min(20,3+Math.sqrt(d.loc||1)*0.55))
    });
  });
  const byId={}; NODES.forEach(nd=>byId[nd.id]=nd);
  EDGES=(data.edges||[]).map(e=>({s:byId[e.source],t:byId[e.target],kind:e.kind}))
                        .filter(e=>e.s&&e.t);
  render_sidebar();
  resize(); alpha=1; tick();
}

function fail(msg){
  $('wrap').style.display='none';
  $('err').style.display='block';
  $('err').innerHTML=msg;
}

// ── Force simulation ────────────────────────────────────────────────────────
// Plain O(n^2) repulsion. At this scale (a few hundred modules) that is a
// fraction of a millisecond per frame, and it avoids a quadtree's complexity
// for a graph that will never be large enough to need one.
function step(){
  const REPEL=1400, SPRING=0.012, CENTER=0.0022, DAMP=0.86;
  for(let i=0;i<NODES.length;i++){
    const a=NODES[i];
    for(let j=i+1;j<NODES.length;j++){
      const b=NODES[j];
      let dx=b.x-a.x, dy=b.y-a.y;
      let d2=dx*dx+dy*dy;
      if(d2<0.01){ dx=(Math.random()-0.5); dy=(Math.random()-0.5); d2=0.01; }
      const d=Math.sqrt(d2);
      const f=REPEL/d2;
      const ux=dx/d, uy=dy/d;
      a.vx-=ux*f; a.vy-=uy*f;
      b.vx+=ux*f; b.vy+=uy*f;
    }
  }
  for(const e of EDGES){
    const dx=e.t.x-e.s.x, dy=e.t.y-e.s.y;
    const d=Math.sqrt(dx*dx+dy*dy)||1;
    const ideal=70+e.s.r+e.t.r;
    // A runtime import is a much weaker coupling than a top-level one, so it
    // pulls less — deliberately deferred imports should not drag two modules
    // together as if they were tightly bound.
    const k=SPRING*(e.kind==='runtime'?0.35:1);
    const f=(d-ideal)*k;
    const ux=dx/d, uy=dy/d;
    e.s.vx+=ux*f; e.s.vy+=uy*f;
    e.t.vx-=ux*f; e.t.vy-=uy*f;
  }
  for(const nd of NODES){
    if(nd===dragNode) continue;
    nd.vx-=nd.x*CENTER; nd.vy-=nd.y*CENTER;
    nd.vx*=DAMP; nd.vy*=DAMP;
    nd.x+=nd.vx*alpha; nd.y+=nd.vy*alpha;
  }
  alpha*=0.994;
}

function visible(nd){
  return !FILTER || nd.id.toLowerCase().includes(FILTER);
}
function neighbours(nd){
  const s=new Set();
  for(const e of EDGES){
    if(e.s===nd) s.add(e.t);
    if(e.t===nd) s.add(e.s);
  }
  return s;
}

function draw(){
  ctx.clearRect(0,0,W,H);
  ctx.save();
  ctx.translate(W/2+cam.x,H/2+cam.y); ctx.scale(cam.k,cam.k);

  const focus=SELECTED||HOVER;
  const near=focus?neighbours(focus):null;

  for(const e of EDGES){
    const lit=focus&&(e.s===focus||e.t===focus);
    const dim=(focus&&!lit)||!visible(e.s)||!visible(e.t);
    ctx.beginPath();
    ctx.moveTo(e.s.x,e.s.y); ctx.lineTo(e.t.x,e.t.y);
    ctx.strokeStyle = lit ? '#22d3ee' : (dim ? 'rgba(31,41,55,.45)' : 'rgba(75,85,99,.55)');
    ctx.lineWidth = (lit?1.6:0.8)/cam.k;
    if(e.kind==='runtime'){ ctx.setLineDash([3/cam.k,3/cam.k]); } else { ctx.setLineDash([]); }
    ctx.stroke();
  }
  ctx.setLineDash([]);

  for(const nd of NODES){
    const dim=(focus&&nd!==focus&&!near.has(nd))||!visible(nd);
    ctx.beginPath();
    ctx.arc(nd.x,nd.y,nd.r,0,Math.PI*2);
    ctx.fillStyle=colorFor(nd.layer);
    ctx.globalAlpha=dim?0.18:1;
    ctx.fill();
    if(nd===SELECTED){
      ctx.lineWidth=2.5/cam.k; ctx.strokeStyle='#fff'; ctx.stroke();
    }
    ctx.globalAlpha=1;
    // Labels only where they can be read: zoomed in, big, or focused.
    if(!dim && (cam.k>1.15 || nd.r>11 || nd===focus)){
      ctx.font=(11/cam.k)+'px ui-sans-serif,system-ui,sans-serif';
      ctx.fillStyle='#9ca3af'; ctx.textAlign='center';
      ctx.fillText(nd.id.split('.').pop(), nd.x, nd.y-nd.r-4/cam.k);
    }
  }
  ctx.restore();
}

function tick(){
  if(alpha>0.005 && !dragNode) step();
  else if(dragNode) step();
  draw();
  requestAnimationFrame(tick);
}

// ── Interaction ─────────────────────────────────────────────────────────────
function toWorld(px,py){
  return {x:(px-W/2-cam.x)/cam.k, y:(py-H/2-cam.y)/cam.k};
}
function pick(px,py){
  const p=toWorld(px,py);
  let best=null,bd=Infinity;
  for(const nd of NODES){
    if(!visible(nd)) continue;
    const d=Math.hypot(nd.x-p.x,nd.y-p.y);
    if(d<nd.r+6 && d<bd){ best=nd; bd=d; }
  }
  return best;
}
cv.addEventListener('mousedown',ev=>{
  const r=cv.getBoundingClientRect();
  const hit=pick(ev.clientX-r.left,ev.clientY-r.top);
  if(hit){ dragNode=hit; select(hit); }
  else { panning=true; cv.classList.add('dragging'); }
  last={x:ev.clientX,y:ev.clientY};
});
window.addEventListener('mousemove',ev=>{
  const r=cv.getBoundingClientRect();
  const mx=ev.clientX-r.left, my=ev.clientY-r.top;
  if(dragNode){
    const p=toWorld(mx,my);
    dragNode.x=p.x; dragNode.y=p.y; dragNode.vx=0; dragNode.vy=0;
    alpha=Math.max(alpha,0.35);
  } else if(panning){
    cam.x+=ev.clientX-last.x; cam.y+=ev.clientY-last.y;
    last={x:ev.clientX,y:ev.clientY};
  } else {
    const hit=pick(mx,my);
    HOVER=hit;
    const tt=$('tooltip');
    if(hit){
      tt.style.display='block';
      tt.style.left=Math.min(mx+14,W-290)+'px';
      tt.style.top=(my+14)+'px';
      tt.innerHTML='<b>'+esc(hit.id)+'</b><br>'+hit.loc+' lines · in '+
        hit.fan_in+' · out '+hit.fan_out;
    } else tt.style.display='none';
  }
});
window.addEventListener('mouseup',()=>{
  dragNode=null; panning=false; cv.classList.remove('dragging');
});
cv.addEventListener('wheel',ev=>{
  ev.preventDefault();
  const f=ev.deltaY<0?1.12:1/1.12;
  cam.k=Math.max(0.25,Math.min(4,cam.k*f));
},{passive:false});

$('reheat').addEventListener('click',()=>{ alpha=1; });
$('search').addEventListener('input',e=>{ FILTER=e.target.value.trim().toLowerCase(); });

function esc(s){
  return String(s).replace(/[&<>"']/g,c=>(
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function select(nd){
  SELECTED=nd;
  const ins=EDGES.filter(e=>e.t===nd).map(e=>e.s.id).sort();
  const outs=EDGES.filter(e=>e.s===nd).map(e=>e.t.id).sort();
  $('detail').innerHTML =
    '<div class="mono">'+esc(nd.path)+'</div>'+
    '<div class="kv"><span>Layer</span><span>'+esc(nd.layer)+'</span></div>'+
    '<div class="kv"><span>Lines</span><span>'+nd.loc+'</span></div>'+
    '<div class="kv"><span>Functions</span><span>'+nd.functions+'</span></div>'+
    '<div class="kv"><span>Classes</span><span>'+nd.classes+'</span></div>'+
    '<div class="kv"><span>Imported by</span><span>'+ins.length+'</span></div>'+
    '<div class="kv"><span>Imports</span><span>'+outs.length+'</span></div>'+
    (nd.external_deps&&nd.external_deps.length
      ? '<div class="kv"><span>External</span><span>'+esc(nd.external_deps.join(', '))+'</span></div>'
      : '')+
    listBlock('Imported by',ins)+listBlock('Imports',outs);
}
function listBlock(title,items){
  if(!items.length) return '';
  return '<h2 style="margin-top:14px">'+title+'</h2>'+
    items.slice(0,25).map(i=>'<div class="listrow"><span>'+esc(i)+'</span></div>').join('');
}

function render_sidebar(){
  $('s-mod').textContent=STATS.modules??NODES.length;
  $('s-edge').textContent=STATS.edges??EDGES.length;
  $('s-loc').textContent=(STATS.total_loc??0).toLocaleString();

  const cyc=STATS.cycles||[];
  $('s-cyc').innerHTML = cyc.length
    ? '<span class="pill bad">'+cyc.length+' import cycle'+(cyc.length>1?'s':'')+'</span>'
    : '<span class="pill ok">no cycles</span>';

  const layers=[...new Set(NODES.map(n=>n.layer))].sort();
  $('legend').innerHTML=layers.map(l=>{
    const count=NODES.filter(n=>n.layer===l).length;
    return '<div class="legend-item"><span class="swatch" style="background:'+
      colorFor(l)+'"></span>'+esc(l)+' <span style="color:var(--dim)">('+count+')</span></div>';
  }).join('');

  const hs=STATS.hotspots||[];
  $('hotspots').innerHTML = hs.length
    ? hs.slice(0,8).map(h=>'<div class="listrow clickable" data-id="'+esc(h.id)+'">'+
        '<span>'+esc(h.id)+'</span><span>'+h.loc+'L · '+h.fan_in+'←</span></div>').join('')
    : '<div class="empty">none</div>';

  const orph=STATS.orphans||[];
  $('orphans').innerHTML = orph.length
    ? orph.map(o=>'<div class="listrow clickable" data-id="'+esc(o)+'">'+
        '<span>'+esc(o)+'</span><span>0←</span></div>').join('')
    : '<div class="empty">every module is imported somewhere</div>';

  document.querySelectorAll('.listrow.clickable').forEach(el=>{
    el.addEventListener('click',()=>{
      const nd=NODES.find(n=>n.id===el.dataset.id);
      if(nd){ select(nd); cam.x=-nd.x*cam.k; cam.y=-nd.y*cam.k; }
    });
  });
}

resize();
load();
</script>
</body>
</html>"""


def graph_html() -> str:
    return GRAPH_HTML
