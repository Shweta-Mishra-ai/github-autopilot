"""
app/dashboard.py — Self-contained live ops dashboard.

Served at GET /dashboard. Zero build step, no external CDN — one HTML string
so it works on Render free tier with nothing to compile.

SECURITY
  The HTML shell contains NO secret. When METRICS_AUTH_TOKEN is set, the page
  asks for it once (kept in sessionStorage, never in the URL) and sends it as a
  Bearer header on its polls to /health — the same auth that endpoint already
  enforces (/health embeds the metrics the dashboard needs). Nothing new is
  exposed.
"""

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>GitHub Autopilot — Ops</title>
<style>
  :root {
    --bg:#0b1120; --panel:#111827; --border:#1f2937; --text:#e5e7eb;
    --dim:#9ca3af; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444;
    --cyan:#22d3ee; --violet:#818cf8;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:20px 24px; border-bottom:1px solid var(--border);
    display:flex; align-items:center; gap:12px; }
  header h1 { font-size:18px; margin:0; font-weight:650; }
  header .badge { font-size:12px; color:var(--dim); }
  .status-dot { width:10px; height:10px; border-radius:50%; background:var(--dim); }
  main { padding:24px; max-width:1100px; margin:0 auto; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:14px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:16px 18px; }
  .card .label { font-size:12px; color:var(--dim); text-transform:uppercase; letter-spacing:.04em; }
  .card .value { font-size:30px; font-weight:700; margin-top:6px;
    background:linear-gradient(135deg,var(--cyan),var(--violet));
    -webkit-background-clip:text; background-clip:text; color:transparent; }
  .card .sub { font-size:12px; color:var(--dim); margin-top:4px; }
  h2 { font-size:13px; color:var(--dim); text-transform:uppercase; letter-spacing:.05em; margin:28px 0 12px; }
  .row { display:flex; justify-content:space-between; padding:9px 0; border-bottom:1px solid var(--border); }
  .row:last-child { border-bottom:0; }
  .pill { font-size:12px; padding:2px 10px; border-radius:999px; font-weight:600; }
  .pill.ok { background:rgba(34,197,94,.15); color:var(--ok); }
  .pill.warn { background:rgba(245,158,11,.15); color:var(--warn); }
  .pill.bad { background:rgba(239,68,68,.15); color:var(--bad); }
  .foot { color:var(--dim); font-size:12px; margin-top:24px; text-align:center; }
  input { background:var(--bg); border:1px solid var(--border); color:var(--text);
    padding:8px 10px; border-radius:8px; width:280px; }
  button { background:var(--violet); color:#0b1120; border:0; padding:8px 14px;
    border-radius:8px; font-weight:600; cursor:pointer; margin-left:8px; }
  .hidden { display:none; }
  code { color:var(--cyan); }
</style>
</head>
<body>
<header>
  <span class="status-dot" id="dot"></span>
  <h1>GitHub Autopilot</h1>
  <span class="badge" id="ver">—</span>
  <span class="badge" style="margin-left:auto" id="clock">—</span>
</header>
<main>
  <div id="auth" class="card hidden">
    <div class="label">Auth required</div>
    <p class="sub">Enter <code>METRICS_AUTH_TOKEN</code> to view live metrics.</p>
    <input id="tok" type="password" placeholder="Bearer token"/>
    <button onclick="saveTok()">Connect</button>
  </div>

  <div id="app" class="hidden">
    <div class="grid">
      <div class="card"><div class="label">Status</div><div class="value" id="m_status">—</div><div class="sub" id="m_uptime">uptime —</div></div>
      <div class="card"><div class="label">Events processed</div><div class="value" id="m_events">—</div><div class="sub" id="m_errors">— errors</div></div>
      <div class="card"><div class="label">Queue depth</div><div class="value" id="m_qpending">—</div><div class="sub" id="m_qmode">—</div></div>
      <div class="card"><div class="label">Dropped / dead</div><div class="value" id="m_dropped">—</div><div class="sub" id="m_dead">— dead-letter</div></div>
    </div>

    <h2>Health checks</h2>
    <div class="card" id="checks"></div>

    <h2>LLM providers</h2>
    <div class="card" id="providers"></div>

    <h2>Thread pool</h2>
    <div class="card" id="pool"></div>
  </div>
</main>
<div class="foot">Polls every 5s · <span id="err"></span></div>

<script>
const $ = id => document.getElementById(id);
function tok(){ return sessionStorage.getItem('ap_tok') || ''; }
function saveTok(){ sessionStorage.setItem('ap_tok', $('tok').value.trim()); location.reload(); }
function hdrs(){ const t = tok(); return t ? {Authorization:'Bearer '+t} : {}; }

async function poll(){
  try {
    const h = await fetch('/health', {headers:hdrs()});
    if (h.status === 401){ $('auth').classList.remove('hidden'); $('app').classList.add('hidden'); return; }
    $('auth').classList.add('hidden'); $('app').classList.remove('hidden');
    const d = await h.json();
    render(d);
    $('err').textContent = '';
  } catch(e){ $('err').textContent = 'poll failed: ' + e.message; }
}

function pill(state){
  const s = String(state).toLowerCase();
  const cls = (s==='ok'||s==='closed') ? 'ok' : (s==='degraded'||s==='half_open'||s==='saturated') ? 'warn' : 'bad';
  return '<span class="pill '+cls+'">'+state+'</span>';
}

function render(d){
  $('dot').style.background = d.status==='ok' ? 'var(--ok)' : 'var(--warn)';
  $('ver').textContent = 'v'+(d.version||'?');
  $('clock').textContent = new Date().toLocaleTimeString();
  $('m_status').textContent = d.status||'—';
  const up = d.uptime_seconds||0;
  $('m_uptime').textContent = 'uptime '+Math.floor(up/3600)+'h '+Math.floor((up%3600)/60)+'m';

  const m = d.metrics||{};
  $('m_events').textContent = m.events_total ?? '—';
  $('m_errors').textContent = (m.errors_total ?? 0)+' errors';
  $('m_dropped').textContent = m.events_dropped ?? 0;

  const q = d.event_queue||{};
  $('m_qpending').textContent = q.pending ?? '—';
  $('m_qmode').textContent = (q.mode||'—')+(q.processing!=null?(' · '+q.processing+' in-flight'):'');
  $('m_dead').textContent = (q.dead ?? 0)+' dead-letter';

  const c = d.checks||{};
  $('checks').innerHTML =
    row('Redis', c.redis) + row('GitHub API', c.github_api) + row('Thread pool', c.thread_pool);

  const lp = c.llm_providers||{};
  $('providers').innerHTML = Object.keys(lp).length
    ? Object.entries(lp).map(([k,v])=>row(k, (v&&v.state)||v)).join('')
    : '<div class="sub">no providers reported yet</div>';

  const p = d.thread_pool||{};
  $('pool').innerHTML =
    '<div class="row"><span>Pending tasks</span><span>'+(p.pending_tasks??'—')+' / '+(p.queue_capacity??'—')+'</span></div>'+
    '<div class="row"><span>Workers</span><span>'+(p.max_workers??'—')+'</span></div>'+
    '<div class="row"><span>Saturation</span>'+pill((p.saturation_pct??0)>80?'saturated':'ok')+'</div>';
}
function row(label,state){ return '<div class="row"><span>'+label+'</span>'+pill(state)+'</div>'; }

poll(); setInterval(poll, 5000);
</script>
</body>
</html>"""


def dashboard_html() -> str:
    return DASHBOARD_HTML
