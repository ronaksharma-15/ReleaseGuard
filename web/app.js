'use strict';

const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[c]);
const scenarios = {
  wrong_total: ['Prevent', 'Incorrect total', 'Return a wrong quote with HTTP 200 and test the behavior gate.'],
  slow: ['Recover', 'Slow under load', 'Introduce latency under synthetic traffic and verify recovery.'],
  healthy: ['Release', 'Healthy candidate', 'Advance a passing candidate through the full rollout.']
};
const statuses = {
  queued: 'Queued', validating: 'Validating behavior', canary: 'Canary in progress',
  paused: 'Rollout paused', rolling_back: 'Rolling back', blocked: 'Promotion blocked',
  recovered: 'Recovery verified', promoted: 'Release promoted', inconclusive: 'Inconclusive',
  needs_attention: 'Needs attention'
};
const color = (status) => ({blocked:'red',needs_attention:'red',rolling_back:'amber',paused:'amber',
  inconclusive:'amber',promoted:'green',recovered:'green'})[status] || 'cyan';
const symbols = {
  route:'<circle cx="5" cy="12" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="18" cy="19" r="2"/><path d="M7 12h2c4 0 2-7 7-7M9 12c4 0 2 7 7 7"/>',
  pulse:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',
  check:'<path d="m4 12 5 5L20 6"/>', clock:'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  brackets:'<path d="m7 6-5 6 5 6m10-12 5 6-5 6M14 4l-4 16"/>',
  layers:'<path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5"/>',
  list:'<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3" cy="6" r="1"/><circle cx="3" cy="12" r="1"/><circle cx="3" cy="18" r="1"/>',
  arrow:'<path d="M5 12h14m-6-6 6 6-6 6"/>'
};
function icon(name) {
  return `<svg class="icon" aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">${symbols[name] || ''}</svg>`;
}
function stamp(value) {
  if (!value) return '—';
  return new Date(value).toLocaleString(undefined, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
function ms(value) {
  if (value == null) return '—';
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Number(value.toFixed(1))} ms`;
}
const pct = (fraction) => fraction == null ? '—' : `${Number((fraction * 100).toFixed(1))}%`;
const cash = (value) => typeof value === 'number' ? `₹${value.toLocaleString('en-IN',{maximumFractionDigits:2})}` : '—';
const safeJSON = (value) => escapeHTML(JSON.stringify(value, null, 2));
function badge(status) {
  return `<span class="badge ${color(status)}"><i></i>${escapeHTML(statuses[status] || status)}</span>`;
}
let model = null;
let selectedId = '';
let busy = false;
let connected = false;
let snapshot = '';
let errorMessage = '';
let pollTimer;
let latestPoll = 0;

function report(message) {
  errorMessage = message;
  $('notice').textContent = message;
  $('notice').hidden = !message;
}
function selected() {
  return model?.releases.find((r) => r.id === selectedId) || model?.releases[0] || null;
}
function currentPolicy(r) { return r?.policy || model.policy || {}; }
function hero(r) {
  const tone = r ? color(r.status) : 'cyan';
  const title = r ? statuses[r.status] || r.status : 'Ready for a release';
  const copy = r ? r.summary : model.mode === 'compose' ? 'Waiting for the first candidate from the deployment pipeline.' : 'Launch a checkout candidate to see its behavior checks, traffic decisions and recovery evidence.';
  return `<section class="hero panel ${tone}">
    <div class="hero-graphic" aria-hidden="true"><div class="radar"><i></i><b></b><em></em><span></span></div></div>
    <div class="hero-content"><div class="eyebrow"><span class="status-dot"></span>${r ? 'Release decision' : 'Controller ready'}</div>
    <h2>${escapeHTML(title)}</h2><p>${escapeHTML(copy)}</p>
    <div class="hero-meta">${r ? `<span>${escapeHTML(scenarios[r.scenario]?.[0] || r.scenario)}</span><span class="mono">${escapeHTML(r.id)}</span><span>${escapeHTML(stamp(r.updated_at))}</span>` :
    `<span>${icon('layers')} Checkout API</span><span>Synthetic workload</span>`}</div></div></section>`;
}
function rollout(r) {
  const stages = currentPolicy(r).stages || [];
  const reached = new Set((r?.windows || []).map((w) => w.stage));
  let steps = stages.map((stage) => `<li class="${reached.has(stage) ? 'observed' : ''} ${r?.traffic === stage ? 'here' : ''}"><i></i><span>${escapeHTML(stage)}%</span></li>`).join('');
  return `<section class="panel rollout"><div class="panel-label">${icon('route')} Traffic routing</div>
    <div class="routing-value">${r ? escapeHTML(r.traffic) : '0'}<small>%</small></div><div class="muted">Candidate share${r && r.id !== model.releases[0]?.id ? ' at decision' : ''}</div>
    <ol class="stages" aria-label="Configured rollout stages">${steps}</ol>
    <div class="route-foot"><span class="status-dot"></span> ${r && r.id !== model.releases[0]?.id ? 'Stable before run' : 'Current stable'} <code>${escapeHTML((r && r.id !== model.releases[0]?.id ? r.stable_version : model.stable?.version) || '—')}</code></div></section>`;
}
function cards(r) {
  const windows = r?.windows || [];
  const candidateCount = windows.reduce((s,w) => s+(w.candidate.routed_count ?? w.candidate.count ?? 0),0);
  const stableCount = windows.reduce((s,w) => s+(w.stable.routed_count ?? 0),0);
  const fields = [
    ['brackets','Behavior fixtures',r?.behavior ? `${r.behavior.passed}<small> / ${r.behavior.total}</small>` : '—','Passing contract checks'],
    ['pulse','Routed requests', r ? String(candidateCount + stableCount) : '—',r ? `${candidateCount} candidate · ${stableCount} stable` : 'Attributed synthetic traffic'],
    ['clock','Detection interval',ms(r?.detection_ms),'First breach to decision'],
    ['check','Recovery interval',ms(r?.recovery_ms),'Rollback to verification']
  ];
  return `<div class="metrics">${fields.map(([i,l,v,f]) => `<section class="metric panel"><div class="panel-label">${icon(i)} ${l}</div><strong>${v}</strong><span class="muted">${escapeHTML(f)}</span></section>`).join('')}</div>`;
}
function signedValue(label, value) {
  return value ? `<div class="identity"><span>${escapeHTML(label)}</span><code>${escapeHTML(value)}</code></div>` : '';
}
function history(r) {
  const rows = model.releases.map((x) => `<button type="button" class="history-row ${x.id===r?.id?'chosen':''}" data-release="${escapeHTML(x.id)}" aria-pressed="${x.id===r?.id}">
    <span><strong>${escapeHTML(scenarios[x.scenario]?.[0] || x.scenario)}</strong><small class="mono">${escapeHTML(x.id)}</small></span>
    <span>${badge(x.status)}<small>${escapeHTML(stamp(x.created_at))}</small></span></button>`).join('');
  return `<section class="panel content-panel"><div class="section-title">${icon('layers')}<h2>Release history</h2><span class="count">${model.releases.length}</span></div>${rows || '<div class="empty"><strong>No releases yet</strong><p>Your first run will appear here.</p></div>'}</section>`;
}
function fixture(result, title, recovering=false) {
  if (!result) return '';
  const sample = result.failures?.[0] || result.sample;
  const cmp = sample ? `<div class="fixture-name">${escapeHTML(sample.name)}</div><div class="quote-grid">${[
    ['Expected', sample.expected?.total, 'Contract'],
    ['Stable', sample.stable?.values?.total, `HTTP ${sample.stable?.http_status ?? '—'}`],
    [recovering?'Routed':'Candidate', sample.candidate?.values?.total, `HTTP ${sample.candidate?.http_status ?? '—'}`]
  ].map(([k,v,f])=>`<div><span>${k}</span><strong>${cash(v)}</strong><small>${escapeHTML(f)}</small></div>`).join('')}</div>` : '';
  const fails = result.failures || [];
  return `<div class="record-section"><h3>${escapeHTML(title)}</h3><p>${result.passed} of ${result.total} fixtures passed · ${result.rules_checked} rules evaluated${result.skipped ? ` · ${result.skipped} skipped` : ''}</p>${cmp}
    ${fails.map((f,i) => `<details data-detail="${recovering?'recovery':'behavior'}-${i}"><summary>${escapeHTML(f.name)} <span>${escapeHTML(f.rule)}</span></summary><pre>${safeJSON({input:f.input,expected:f.expected,actual:f.actual,violations:f.violations})}</pre></details>`).join('')}</div>`;
}
function monitor(r) {
  const windows=r.windows || [];
  if (!windows.length) return '';
  const policy=currentPolicy(r);
  return `<div class="record-section"><h3>Observation windows</h3><p>Candidate limits · p95 ${escapeHTML(ms(policy.candidate_p95_limit_ms))} · error rate ${pct(policy.candidate_error_rate_limit)}. Rollback follows ${escapeHTML(policy.consecutive_breach_windows)} consecutive breach windows.</p>
    <div class="table-scroll"><table><thead><tr><th>Stage</th><th>Candidate p95</th><th>Stable p95</th><th>Requests¹</th><th>Errors¹</th><th>Verdict</th></tr></thead><tbody>
    ${windows.map((w)=>`<tr><td>${escapeHTML(w.stage)}%</td><td>${ms(w.candidate.p95_ms)}</td><td>${ms(w.stable.p95_ms)}</td><td>${w.candidate.count} / ${w.stable.count}</td><td>${pct(w.candidate.error_rate)} / ${pct(w.stable.error_rate)}</td><td><span class="signal ${w.verdict==='pass'?'green':'amber'}">${escapeHTML(w.verdict)}</span></td></tr>`).join('')}</tbody></table></div>
    <p class="footnote">¹ Candidate / stable. Stable measurements include ${windows.reduce((n,w)=>n+(w.stable.baseline_count || 0),0)} direct comparison probes. Missing or unattributed planned requests: ${windows.reduce((n,w)=>n+(w.unknown_count || 0),0)}.</p></div>`;
}
function evidence(r) {
  const retry=r?.id===model.releases[0]?.id && r?.status==='needs_attention' && model.mode==='local';
  return `<section class="panel content-panel"><div class="section-title">${icon('brackets')}<h2>Release evidence</h2></div>${r?`${retry?'<button class="secondary" data-retry>Retry recovery</button>':''}
    ${fixture(r.behavior,'Behavior gate')}${monitor(r)}${fixture(r.recovery,'Recovery verification',true)}
    <div class="record-section"><h3>Decision timeline</h3><ol class="timeline">${r.events.map((e)=>`<li class="${color(e.status)}"><span class="status-dot"></span><div>${escapeHTML(e.message)}<time>${escapeHTML(stamp(e.at))}</time></div></li>`).join('')}</ol></div>
    <div class="record-section"><h3>Version identity</h3>${[ ['Stable', r.stable_version],['Candidate',r.candidate_version],['Commit SHA',r.commit_sha],[model.mode==='local'?'Stable source fingerprint':'Stable image digest',r.stable_digest],[model.mode==='local'?'Candidate source fingerprint':'Candidate image digest',r.candidate_digest]].map(([k,v])=>signedValue(k,v)).join('')}</div>`:
    '<div class="empty"><strong>Awaiting a release</strong><p>Fixture comparisons, measured windows and the decision timeline will appear here.</p></div>'}</section>`;
}
function render() {
  if (!model) return;
  const r=selected();
  const key=JSON.stringify([model,selectedId]);
  if (snapshot!==key) {
    const open=[...document.querySelectorAll('details[open]')].map((d)=>d.dataset.detail);
    const focus=document.activeElement?.closest('[data-release],details');
    const focusedRelease=focus?.dataset.release;
    const focusedDetail=focus?.dataset.detail;
    $('screen').innerHTML=`<div class="overview">${hero(r)}${rollout(r)}</div>${cards(r)}<div class="records">${evidence(r)}${history(r)}</div>`;
    for (const d of document.querySelectorAll('details')) {
      if (open.includes(d.dataset.detail)) d.open=true;
      if (focusedDetail===d.dataset.detail) d.querySelector('summary').focus({preventScroll:true});
    }
    if(focusedRelease) [...document.querySelectorAll('[data-release]')].find(n=>n.dataset.release===focusedRelease)?.focus({preventScroll:true});
    snapshot=key;
  }
  $('environment').textContent=connected ? (model.mode==='compose'?'Cloud · Compose':'Local demo') : 'Disconnected';
  $('linkLight').classList.toggle('online',connected);
  $('screen').setAttribute('aria-busy','false');
  $('newRelease').hidden=model.mode!=='local';
  $('newRelease').disabled=!connected || busy || !!model.active_id || model.releases[0]?.status==='needs_attention';
  document.querySelectorAll('[data-scenario],[data-retry]').forEach(b=>b.disabled=!connected || busy || !!model.active_id);
}
async function request(path, options={}) {
  const response=await fetch(path,{cache:'no-store',...options});
  const data=await response.json();
  if(!response.ok) throw Error(data.error || `Controller returned ${response.status}`);
  return data;
}
async function refresh() {
  clearTimeout(pollTimer);
  const serial=++latestPoll;
  try {
    const state=await request('/api/state');
    if (serial!==latestPoll) return;
    model=state; connected=true;
    if(errorMessage.startsWith('Controller connection')) report('');
    render();
  } catch(e) {
    if(serial!==latestPoll) return;
    connected=false; report(`Controller connection lost. Retrying… ${e.message}`);
    if(!model){$('environment').textContent='Disconnected'; $('screen').setAttribute('aria-busy','false');}
    render();
  } finally {if(serial===latestPoll) pollTimer=setTimeout(refresh,model?.active_id?400:1500);}
}
const pendingIds=new Map();
async function launch(scenario, id) {
  if (busy || !connected) return;
  const path=id?`/api/releases/${encodeURIComponent(id)}/recover`:'/api/releases';
  if (scenario && !pendingIds.has(scenario)) pendingIds.set(scenario,crypto.randomUUID());
  busy=true; $('launchError').textContent=''; report(''); render();
  try{
    const r=await request(path,{method:'POST',headers:{'Content-Type':'application/json','X-ReleaseGuard-UI':'1'},
      body:JSON.stringify(id?{}:{scenario,request_id:pendingIds.get(scenario)})});
    pendingIds.delete(scenario); selectedId=r.id; $('launcher').close();
    await refresh();
  } catch(e){if($('launcher').open) $('launchError').textContent=e.message; else report(e.message);}
  finally{busy=false;render();}
}
$('scenarios').innerHTML=Object.entries(scenarios).map(([id,[title,name,desc]])=>`<button class="scenario" type="button" data-scenario="${id}"><span><b>${title}</b><small>${name}</small><em>${desc}</em></span>${icon('arrow')}</button>`).join('');
$('newRelease').addEventListener('click',()=>{$('launchError').textContent='';$('launcher').showModal();});
document.addEventListener('click',(event)=>{
  const el=event.target.closest('button');
  if(!el) return;
  if(el.dataset.scenario) launch(el.dataset.scenario);
  else if(el.dataset.release){selectedId=el.dataset.release;render();}
  else if(el.hasAttribute('data-retry')) launch(null, selected()?.id);
});
refresh();
