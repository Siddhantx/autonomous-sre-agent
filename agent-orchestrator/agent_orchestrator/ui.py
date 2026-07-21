"""Single-page web UI for incidents, approvals, and changes.

Served at GET /ui — a self-contained HTML page that fetches the existing
API endpoints client-side. No build step, no JS framework, no external deps.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>APOE — Incident Dashboard</title>
<style>
  :root { --bg: #0d1117; --fg: #c9d1d9; --card: #161b22; --border: #30363d;
          --accent: #58a6ff; --green: #3fb950; --red: #f85149; --yellow: #d29922; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
         background: var(--bg); color: var(--fg); padding: 1rem; max-width: 1200px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
  h2 { font-size: 1.1rem; margin: 1.5rem 0 0.5rem; color: var(--accent); }
  .subtitle { color: #8b949e; font-size: 0.85rem; margin-bottom: 1rem; }
  .tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 1rem; }
  .tab { padding: 0.5rem 1rem; cursor: pointer; border-bottom: 2px solid transparent;
         color: #8b949e; font-size: 0.9rem; }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .panel { display: none; }
  .panel.active { display: block; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { text-align: left; padding: 0.5rem; border-bottom: 1px solid var(--border);
       color: #8b949e; font-weight: 600; }
  td { padding: 0.5rem; border-bottom: 1px solid var(--border); }
  .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 3px;
           font-size: 0.75rem; font-weight: 600; }
  .badge-resolved { background: #0d2818; color: var(--green); }
  .badge-escalated { background: #341a00; color: var(--yellow); }
  .badge-failed { background: #3d1214; color: var(--red); }
  .badge-pending { background: #1c2333; color: var(--accent); }
  .badge-created, .badge-diagnosing, .badge-diagnosed, .badge-planning,
  .badge-remediating { background: #1c2333; color: var(--fg); }
  .btn { padding: 0.3rem 0.7rem; border: 1px solid var(--border); border-radius: 4px;
         background: var(--card); color: var(--fg); cursor: pointer; font-size: 0.8rem; }
  .btn:hover { border-color: var(--accent); }
  .btn-approve { border-color: var(--green); color: var(--green); }
  .btn-reject { border-color: var(--red); color: var(--red); }
  .empty { color: #8b949e; text-align: center; padding: 2rem; }
  .auth-bar { display: flex; gap: 0.5rem; margin-bottom: 1rem; align-items: center; }
  .auth-bar input { background: var(--card); border: 1px solid var(--border); color: var(--fg);
                    padding: 0.4rem; border-radius: 4px; font-size: 0.85rem; width: 200px; }
  .auth-bar label { color: #8b949e; font-size: 0.85rem; }
  .refresh { margin-left: auto; }
  #status { font-size: 0.8rem; color: #8b949e; margin-left: 0.5rem; }
  .detail { max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
</head>
<body>
<h1>APOE Dashboard</h1>
<p class="subtitle">Autonomous Production Operations Engineer — incident overview</p>

<div class="auth-bar">
  <label for="apikey">API Key:</label>
  <input type="password" id="apikey" placeholder="APOE_API_KEY">
  <button class="btn refresh" onclick="refreshAll()">Refresh</button>
  <span id="status"></span>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('incidents')">Incidents</div>
  <div class="tab" onclick="showTab('approvals')">Approvals</div>
  <div class="tab" onclick="showTab('changes')">Changes</div>
  <div class="tab" onclick="showTab('policy')">Policy Suggestions</div>
</div>

<div id="incidents" class="panel active">
  <table><thead><tr>
    <th>ID</th><th>Trigger</th><th>State</th><th>Root Cause</th>
    <th>Confidence</th><th>Actions</th><th>Created</th>
  </tr></thead><tbody id="incidents-body"></tbody></table>
</div>

<div id="approvals" class="panel">
  <table><thead><tr>
    <th>ID</th><th>Incident</th><th>Action</th><th>Target</th>
    <th>Confidence</th><th>Status</th><th>Actions</th>
  </tr></thead><tbody id="approvals-body"></tbody></table>
</div>

<div id="changes" class="panel">
  <table><thead><tr>
    <th>Time</th><th>Service</th><th>Kind</th><th>Summary</th><th>Actor</th>
  </tr></thead><tbody id="changes-body"></tbody></table>
</div>

<div id="policy" class="panel">
  <table><thead><tr>
    <th>Action Type</th><th>Target</th><th>Approvals</th><th>Suggested Rule</th>
  </tr></thead><tbody id="policy-body"></tbody></table>
</div>

<script>
const $ = s => document.querySelector(s);
const key = () => $('#apikey').value;
const headers = () => key() ? {'X-API-Key': key(), 'Content-Type': 'application/json'} : {};
const badge = s => `<span class="badge badge-${s}">${s}</span>`;
const status = msg => { $('#status').textContent = msg; setTimeout(() => $('#status').textContent = '', 3000); };
const incidents = {};

function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelector(`.tab[onclick*="${name}"]`).classList.add('active');
  document.getElementById(name).classList.add('active');
}

async function fetchJSON(url, opts) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`${r.status}`);
    return await r.json();
  } catch(e) { status('Error: ' + e.message); return null; }
}

async function loadIncidents() {
  const body = $('#incidents-body');
  const ids = Object.keys(incidents);
  if (!ids.length) { body.innerHTML = '<tr><td colspan="7" class="empty">No incidents yet. Trigger one via POST /incidents.</td></tr>'; return; }
  body.innerHTML = '';
  for (const id of ids.reverse()) {
    const s = incidents[id];
    const d = s.diagnosis;
    body.innerHTML += `<tr>
      <td><code>${s.incident_id}</code></td>
      <td class="detail">${s.trigger}</td>
      <td>${badge(s.state)}</td>
      <td>${d ? d.root_cause : '—'}</td>
      <td>${d ? (d.confidence * 100).toFixed(0) + '%' : '—'}</td>
      <td>${s.results ? s.results.length : 0}</td>
      <td>${new Date(s.created_at).toLocaleTimeString()}</td>
    </tr>`;
  }
}

async function loadApprovals() {
  const data = await fetchJSON('/approvals');
  const body = $('#approvals-body');
  if (!data || !data.length) { body.innerHTML = '<tr><td colspan="7" class="empty">No pending approvals.</td></tr>'; return; }
  body.innerHTML = '';
  for (const a of data) {
    body.innerHTML += `<tr>
      <td><code>${a.approval_id}</code></td>
      <td><code>${a.incident_id}</code></td>
      <td>${a.action.action_type}</td>
      <td>${a.action.target}</td>
      <td>${(a.confidence * 100).toFixed(0)}%</td>
      <td>${badge(a.status)}</td>
      <td>
        <button class="btn btn-approve" onclick="approveAction('${a.approval_id}')">Approve</button>
        <button class="btn btn-reject" onclick="rejectAction('${a.approval_id}')">Reject</button>
      </td>
    </tr>`;
  }
}

async function loadChanges() {
  const data = await fetchJSON('/changes');
  const body = $('#changes-body');
  if (!data || !data.length) { body.innerHTML = '<tr><td colspan="5" class="empty">No changes recorded.</td></tr>'; return; }
  body.innerHTML = '';
  for (const c of data) {
    body.innerHTML += `<tr>
      <td>${c.at || '—'}</td><td>${c.service}</td><td>${c.change_kind}</td>
      <td class="detail">${c.summary}</td><td>${c.actor}</td>
    </tr>`;
  }
}

async function loadPolicy() {
  const data = await fetchJSON('/policy/suggestions');
  const body = $('#policy-body');
  if (!data || !data.length) { body.innerHTML = '<tr><td colspan="4" class="empty">No promotion candidates yet.</td></tr>'; return; }
  body.innerHTML = '';
  for (const p of data) {
    body.innerHTML += `<tr>
      <td>${p.action_type}</td><td>${p.target}</td>
      <td>${p.consecutive_approvals}</td>
      <td><pre style="font-size:0.75rem;color:#8b949e;white-space:pre-wrap">${p.suggested_rule}</pre></td>
    </tr>`;
  }
}

async function approveAction(id) {
  const r = await fetchJSON(`/approvals/${id}/approve`, {method:'POST', headers: headers()});
  if (r) { status('Approved'); refreshAll(); }
}

async function rejectAction(id) {
  const reason = prompt('Rejection reason:');
  if (!reason) return;
  const r = await fetchJSON(`/approvals/${id}/reject`, {
    method: 'POST', headers: headers(),
    body: JSON.stringify({reason: reason})
  });
  if (r) { status('Rejected'); refreshAll(); }
}

function refreshAll() { loadApprovals(); loadChanges(); loadPolicy(); loadIncidents(); }
refreshAll();
setInterval(refreshAll, 15000);
</script>
</body>
</html>"""


@router.get("/ui", response_class=HTMLResponse)
async def dashboard() -> str:
    return _HTML
