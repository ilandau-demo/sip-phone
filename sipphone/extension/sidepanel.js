// Side panel: show the phone UI from the local app, starting it if needed.
const BASE = 'http://127.0.0.1:8088';
const $ = id => document.getElementById(id);
const frame = $('ui');
let busy = false;
let restarting = 0;          // set when the phone UI says it's restarting itself

async function appUp() {
  try { return (await fetch(BASE + '/api/version', { cache: 'no-store' })).ok; }
  catch (e) { return false; }
}

function message(title, body, retry) {
  frame.hidden = true;
  $('team').hidden = true; $('setup').hidden = true;
  $('msg').hidden = false;
  $('msg-team').hidden = !(retry && /install/i.test(title));
  $('msg-title').textContent = title;
  $('msg-body').textContent = body || '';
  $('msg-btn').hidden = !retry;
}

async function wantsSettings() {
  try {
    const { openSettings } = await chrome.storage.session.get('openSettings');
    if (openSettings && Date.now() - openSettings < 15000) {
      await chrome.storage.session.remove('openSettings');
      return true;
    }
  } catch (e) {}
  return false;
}

async function showUI() {
  const settings = await wantsSettings();
  if (!frame.src) frame.src = BASE + '/?embed=1' + (settings ? '#settings' : '');
  else if (settings) frame.contentWindow.postMessage({ sipphone: 'open-settings' }, BASE);
  $('msg').hidden = true; $('team').hidden = true; $('setup').hidden = true;
  frame.hidden = false;
}

// ------------------------------------------------- Team view without the app
const SERVICE_TYPES = [['hunt_group', 'Hunt groups'], ['call_center', 'Call centers / queues'], ['auto_attendant', 'Auto attendants'],
  ['paging', 'Paging groups'], ['voice_portal', 'Voice portal / voicemail'], ['conference', 'Conference bridges'], ['other', 'Other services']];
const teamCfg = async () => (await chrome.storage.local.get('teamOnly')).teamOnly || null;
const STATUS = { available: ['available', 'Available'], idle: ['available', 'Available'], 'on-call': ['oncall', 'On a call'], 'on-hold': ['onhold', 'On hold'], ringing: ['ringing', 'Ringing'],
  dialing: ['dialing', 'Calling out'], dnd: ['dnd', 'Do Not Disturb'], offline: ['offline', 'Offline'], error: ['error', 'Unavailable'], unknown: ['unknown', 'Unknown'] };
const FILTERS = [['available', 'Available'], ['ringing', 'Ringing'], ['on-call', 'On a call'], ['on-hold', 'On hold'], ['dnd', 'DND'], ['unknown', 'Unknown']];
const WATCH_EVENTS = [['available', 'Becomes available'], ['after-call', 'Finishes a call'], ['busy', 'Goes on a call'], ['dnd', 'Turns on Do Not Disturb']];
let teamQ = '', teamStatuses = new Set(), teamDept = '', teamSig = '', lastSnap = null, watchRules = {};
try { const sv = JSON.parse(sessionStorage.getItem('teamfilter') || '{}'); teamQ = sv.q || ''; teamStatuses = new Set(sv.statuses || []); teamDept = sv.dept || ''; } catch (e) {}
const saveFilter = () => { try { sessionStorage.setItem('teamfilter', JSON.stringify({ q: teamQ, statuses: [...teamStatuses], dept: teamDept })); } catch (e) {} };
const norm = x => (x || '').toString().toLowerCase();
const personMatches = (u, q) => { if (!q) return true; const hay = [u.name, u.first, u.last, u.extension, u.number, u.department, u.title, u.email, u.location, u.user_id].map(norm).join(' '); return q.split(/\s+/).filter(Boolean).every(t => hay.includes(t)); };
const hl = (el, text, q) => { el.textContent = ''; const terms = q.split(/\s+/).filter(Boolean); if (!terms.length) { el.textContent = text; return; }
  const re = new RegExp('(' + terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'ig');
  for (const part of text.split(re)) { if (!part) continue; const m = part.match(re); if (m && m[0] === part) { const mk = document.createElement('mark'); mk.textContent = part; el.appendChild(mk); } else el.appendChild(document.createTextNode(part)); } };
chrome.storage.local.get('teamWatch').then(v => { watchRules = v.teamWatch || {}; });
chrome.storage.onChanged.addListener((ch, area) => { if (area === 'local' && ch.teamWatch) { watchRules = ch.teamWatch.newValue || {}; teamSig = ''; if (teamShown && lastSnap) renderTeam(lastSnap); } });
const relTime = t => { if (!t) return ''; const s = Math.floor(Date.now() / 1000 - t); if (s < 0 || s > 86400) return '';
  const m = Math.floor(s / 60), h = Math.floor(m / 60); return s < 60 ? s + 's' : h ? `${h}:${String(m % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : `${m}:${String(s % 60).padStart(2, '0')}`; };
let teamShown = false;
function renderChips(users) {
  const box = $('team-chips'); box.innerHTML = '';
  const counts = {}; for (const u of users) counts[u.status] = (counts[u.status] || 0) + 1;
  for (const [k, label] of FILTERS) {
    if (!counts[k] && !teamStatuses.has(k)) continue;
    const b = document.createElement('button'); b.type = 'button'; b.className = teamStatuses.has(k) ? 'on' : ''; b.textContent = label;
    const n = document.createElement('span'); n.className = 'n'; n.textContent = counts[k] || 0; b.appendChild(n);
    b.setAttribute('aria-pressed', teamStatuses.has(k) ? 'true' : 'false');
    b.onclick = () => { if (teamStatuses.has(k)) teamStatuses.delete(k); else teamStatuses.add(k); saveFilter(); teamSig = ''; renderTeam(lastSnap); };
    box.appendChild(b);
  }
  const depts = [...new Set(users.map(u => (u.department || '').trim()).filter(Boolean))].sort();
  if (depts.length > 1) {
    const sel = document.createElement('select'); sel.setAttribute('aria-label', 'Department');
    const o = document.createElement('option'); o.value = ''; o.textContent = 'All departments'; sel.appendChild(o);
    for (const d of depts) { const op = document.createElement('option'); op.value = d; op.textContent = d; sel.appendChild(op); }
    sel.value = depts.includes(teamDept) ? teamDept : '';
    sel.onchange = () => { teamDept = sel.value; saveFilter(); teamSig = ''; renderTeam(lastSnap); };
    box.appendChild(sel);
  }
  if (teamStatuses.size || teamDept || teamQ) {
    const r = document.createElement('button'); r.type = 'button'; r.className = 'reset'; r.textContent = 'Reset';
    r.onclick = () => { teamStatuses.clear(); teamDept = ''; teamQ = ''; $('team-q').value = ''; $('team-qx').hidden = true; saveFilter(); teamSig = ''; renderTeam(lastSnap); };
    box.appendChild(r);
  }
}
$('team-q').value = teamQ; $('team-qx').hidden = !teamQ;
$('team-q').addEventListener('input', () => { teamQ = $('team-q').value.trim().toLowerCase(); $('team-qx').hidden = !teamQ; saveFilter(); teamSig = ''; renderTeam(lastSnap); });
$('team-qx').addEventListener('click', () => { $('team-q').value = ''; teamQ = ''; $('team-qx').hidden = true; saveFilter(); teamSig = ''; renderTeam(lastSnap); $('team-q').focus(); });
$('team-q').addEventListener('keydown', e => { if (e.key === 'Escape') $('team-qx').click(); });
function renderTeam(snap) {
  const list = $('team-list'), foot = $('team-foot');
  lastSnap = snap;
  if (!snap) { list.innerHTML = ''; teamSig = ''; foot.textContent = 'Connecting to the phone system…'; foot.className = 'team-foot'; return; }
  renderChips(snap.users);
  const shown = snap.users.filter(u => personMatches(u, teamQ) && (!teamStatuses.size || teamStatuses.has(u.status)) && (!teamDept || (u.department || '').trim() === teamDept));
  const sig = JSON.stringify([teamQ, [...teamStatuses], teamDept, snap.stale, snap.connected, snap.last_error, snap.ready, Object.keys(watchRules),
    shown.map(u => [u.user_id, u.status, u.with_number, u.with_name, u.stale, u.monitor])]);
  if (sig === teamSig) { list.querySelectorAll('.dur[data-since]').forEach(d => { d.textContent = relTime(+d.dataset.since); }); return; }
  teamSig = sig;
  list.innerHTML = '';
  if (!shown.length) { const e = document.createElement('div'); e.className = 'empty'; e.textContent = snap.users.length ? 'No one matches — clear the search or filters.' : 'The directory is empty.'; list.appendChild(e); }
  const who = u => (u.with_name && u.with_number && u.with_name !== u.with_number) ? `${u.with_name} - ${u.with_number}` : (u.with_name || u.with_number || '');
  const row = u => {
    const [cls, label] = STATUS[u.status] || ['', u.status];
    const r = document.createElement('div'); r.className = 'mate ' + cls + (u.stale ? ' stale' : '');
    const dot = document.createElement('span'); dot.className = 'dot';
    const body = document.createElement('div');
    const nm = document.createElement('div'); nm.className = 'nm'; hl(nm, u.name, teamQ); nm.title = 'Details'; nm.onclick = () => openPerson(u);
    if (u.extension) { const sm = document.createElement('small'); sm.textContent = 'x' + u.extension; nm.appendChild(sm); }
    const st = document.createElement('div'); st.className = 'st';
    const w = who(u);
    st.textContent = (u.status === 'on-call' || u.status === 'on-hold') && w ? (u.status === 'on-hold' ? 'On hold · ' : u.direction === 'inbound' ? 'From ' : 'With ') + w
      : u.status === 'ringing' ? (w ? 'Incoming · ' + w : 'Ringing') : u.monitor === 'not-monitored' ? 'Not monitored' : u.monitor === 'error' ? (u.detail || label) : label;
    if (u.monitor === 'not-monitored') st.title = u.detail || '';
    body.append(nm, st);
    const side = document.createElement('div'); side.className = 'side';
    if ((u.status === 'on-call' || u.status === 'on-hold') && u.since) { const d = document.createElement('span'); d.className = 'dur'; d.textContent = relTime(u.since); d.dataset.since = u.since; side.appendChild(d); }
    const bell = document.createElement('button'); bell.type = 'button'; bell.className = 'bell' + (watchRules[u.user_id] ? ' on' : ''); bell.textContent = '🔔';
    bell.title = watchRules[u.user_id] ? 'Notifying you: ' + (watchRules[u.user_id].events || []).join(', ') : 'Notify me when…'; bell.setAttribute('aria-label', bell.title);
    bell.onclick = () => openPerson(u, true); side.appendChild(bell);
    r.append(dot, body, side);
    return r;
  };
  const anyDept = shown.some(u => (u.department || '').trim()) && !teamQ;
  if (anyDept) {
    const groups = new Map();
    for (const u of shown) { const d = (u.department || '').trim() || 'No department'; if (!groups.has(d)) groups.set(d, []); groups.get(d).push(u); }
    const names = [...groups.keys()].sort((a, b) => a === 'No department' ? 1 : b === 'No department' ? -1 : a.localeCompare(b));
    for (const d of names) {
      const members = groups.get(d), h = document.createElement('div'); h.className = 'dept';
      const n = document.createElement('span'); n.textContent = d;
      const c = document.createElement('span'); const act = members.filter(m => ['on-call', 'on-hold', 'ringing', 'dialing'].includes(m.status)).length;
      c.textContent = act ? `${act} on a call` : String(members.length); h.append(n, c); list.appendChild(h);
      for (const u of members) list.appendChild(row(u));
    }
  } else for (const u of shown) list.appendChild(row(u));
  let lastKind = null;
  for (const sv of (snap.services || []).filter(sv => personMatches(sv, teamQ) && !teamStatuses.size)) {
    if (sv.kind !== lastKind) { lastKind = sv.kind; const h = document.createElement('div'); h.className = 'dept'; h.textContent = sv.kind_label || 'Services'; list.appendChild(h); }
    const r = document.createElement('div'); r.className = 'svc';
    const nm = document.createElement('div'); nm.className = 'nm'; nm.textContent = sv.name;
    const st = document.createElement('div'); st.className = 'st'; st.textContent = (sv.extension ? 'x' + sv.extension : '') + (sv.number && sv.number !== sv.extension ? (sv.extension ? ' · ' : '') + sv.number : '');
    r.append(nm, st); list.appendChild(r);
  }
  foot.className = 'team-foot' + (snap.last_error ? ' err' : '');
  foot.textContent = snap.last_error ? snap.last_error
    : !snap.ready ? 'Opening the event channel…'
    : snap.connected ? `${snap.active} of ${snap.total} on a call · live${snap.stale ? ' (stale)' : ''}${snap.refused ? ' · ' + snap.refused + ' not monitored' : ''}` + (snap.ack_error ? ' · acknowledgements failing — updates will lag' : '')
    : 'Reconnecting to the event channel… showing last known state';
}
// ---- person details + notification rules
let pdUser = null;
function openPerson(p, focusWatch) {
  pdUser = p;
  const [cls, label] = STATUS[p.status] || ['', p.status];
  $('pd-dot').className = 'dot ' + cls; $('pd-name').textContent = p.name;
  const w = (p.with_name && p.with_number && p.with_name !== p.with_number) ? `${p.with_name} - ${p.with_number}` : (p.with_name || p.with_number || '');
  $('pd-state').textContent = p.monitor === 'not-monitored' ? 'Not monitored — your login may not watch this person' : p.monitor === 'error' ? 'Monitoring error: ' + (p.detail || '')
    : label + (w && ['on-call', 'on-hold', 'ringing'].includes(p.status) ? ' · ' + w : '') + (p.stale ? ' · stale' : '');
  const dl = $('pd-fields'); dl.innerHTML = '';
  const add = (k, v) => { if (!v) return; const dt = document.createElement('dt'); dt.textContent = k; const dd = document.createElement('dd'); dd.textContent = v; dl.append(dt, dd); };
  add('Extension', p.extension); add('Number', p.number && p.number !== p.extension ? p.number : ''); add('Mobile', p.mobile);
  add('Department', p.department); add('Title', p.title); add('Email', p.email); add('Location', p.location); add('Group', p.group);
  add('Do Not Disturb', p.dnd ? 'On' : 'Off'); add('Last update', p.last_update ? relTime(p.last_update) + ' ago' : ''); add('User ID', p.user_id);
  const rule = watchRules[p.user_id] || { events: [] };
  const opts = $('pd-watch-opts'); opts.innerHTML = '';
  for (const [k, txt] of WATCH_EVENTS) { const l = document.createElement('label'); l.className = 'wopt'; const c = document.createElement('input'); c.type = 'checkbox'; c.value = k; c.checked = rule.events.includes(k); const sp = document.createElement('span'); sp.textContent = txt; l.append(c, sp); opts.appendChild(l); }
  $('pd-watch-sound').checked = rule.sound !== false;
  $('pdlg').showModal();
  if (focusWatch) { const f = opts.querySelector('input'); if (f) f.focus(); }
}
$('pdlg').addEventListener('close', async () => {
  if (!pdUser) return;
  const events = [...$('pd-watch-opts').querySelectorAll('input:checked')].map(c => c.value);
  const rules = Object.assign({}, watchRules);
  if (events.length) rules[pdUser.user_id] = { name: pdUser.name, events, once: true, sound: $('pd-watch-sound').checked };
  else delete rules[pdUser.user_id];
  if (JSON.stringify(rules) !== JSON.stringify(watchRules)) await chrome.storage.local.set({ teamWatch: rules });
});
async function showTeam() {
  $('msg').hidden = true; frame.hidden = true; $('setup').hidden = true; $('team').hidden = false; teamShown = true;
  renderTeam((await chrome.storage.session.get('team')).team || null);
}
chrome.storage.onChanged.addListener((ch, area) => {
  if (area === 'session' && ch.team && teamShown) renderTeam(ch.team.newValue || null);
});
setInterval(() => { if (teamShown) document.querySelectorAll('#team-list .dur[data-since]').forEach(d => { d.textContent = relTime(+d.dataset.since); }); }, 1000);

function collectSetup() {
  return { enabled: true, host: $('su-host').value.trim() || 'https://xsi.nextiva.com', user: $('su-user').value.trim(), password: $('su-pass').value,
    hide: $('su-hide').value.split(',').map(x => x.trim()).filter(Boolean),
    showServices: [...document.querySelectorAll('#su-svc input:checked')].map(c => c.dataset.k) };
}
function setupMsg(t, ok) { $('su-msg').textContent = t; $('su-msg').className = 'su-msg ' + (t ? (ok ? 'ok' : 'bad') : ''); }
async function showSetup() {
  const cfg = (await teamCfg()) || {};
  $('msg').hidden = true; frame.hidden = true; $('team').hidden = true; $('setup').hidden = false; teamShown = false;
  $('su-host').value = cfg.host || 'https://xsi.nextiva.com'; $('su-user').value = cfg.user || ''; $('su-pass').value = cfg.password || '';
  $('su-hide').value = (cfg.hide || []).join(', ');
  const box = $('su-svc'); box.innerHTML = '';
  for (const [k, label] of SERVICE_TYPES) {
    const l = document.createElement('label'), c = document.createElement('input'); c.type = 'checkbox'; c.dataset.k = k; c.checked = (cfg.showServices || []).includes(k);
    const sp = document.createElement('span'); sp.textContent = label; l.append(c, sp); box.appendChild(l);
  }
  $('su-off').hidden = !cfg.enabled;
  setupMsg('');
}
// The host permission must be requested inside the click, before any await.
function hostOrigin(host) { let h = (host || 'https://xsi.nextiva.com').trim(); if (!/^https?:\/\//.test(h)) h = 'https://' + h; try { return new URL(h).origin + '/*'; } catch (e) { return ''; } }
function grant() {
  const origin = hostOrigin($('su-host').value);
  if (!origin) return Promise.resolve(false);
  return chrome.permissions.request({ origins: [origin] }).catch(() => false);
}
$('su-check').addEventListener('click', () => {
  const p = grant();
  const cfg = collectSetup();
  if (!cfg.user || !cfg.password) { setupMsg('Enter your user login and password.', false); return; }
  setupMsg('Checking…', true);
  p.then(ok => {
    if (!ok) { setupMsg('Allow access to the XSI server to continue.', false); return; }
    return chrome.runtime.sendMessage({ type: 'team-check', cfg }).then(r => setupMsg(r.message, r.ok));
  }).catch(e => setupMsg(String(e), false));
});
$('su-save').addEventListener('click', () => {
  const p = grant();
  const cfg = collectSetup();
  if (!cfg.user || !cfg.password) { setupMsg('Enter your user login and password.', false); return; }
  p.then(async ok => {
    if (!ok) { setupMsg('Allow access to the XSI server to continue.', false); return; }
    await chrome.storage.local.set({ teamOnly: cfg });
    await showTeam();
  });
});
$('su-off').addEventListener('click', async () => {
  const cfg = (await teamCfg()) || {};
  await chrome.storage.local.set({ teamOnly: Object.assign(cfg, { enabled: false }) });
  teamShown = false; show();
});
$('team-gear').addEventListener('click', showSetup);
$('msg-team').addEventListener('click', showSetup);

async function show() {
  if (busy) return;
  busy = true;
  try {
    if (await appUp()) { await showUI(); return; }
    const tc = await teamCfg();
    if (tc && tc.enabled) { await showTeam(); return; }
    const hello = await chrome.runtime.sendMessage({ type: 'hello' });
    await new Promise(r => setTimeout(r, 300));          // let a fresh connection report back
    const st = (await chrome.storage.session.get('hostState')).hostState || (hello && hello.hostState);
    if (st === 'updating') {
      message('Updating Telvoro…', 'The new version starts by itself in a moment.', false);
      return;
    }
    if (st === 'missing') {
      message('Install the Telvoro desktop app — or just watch your team',
        'Calls, transfers and the full phone need the desktop app: run Install.bat from the Telvoro download, then click Try again. For presence alone, nothing needs installing.',
        true);
      return;
    }
    message('Starting Telvoro…', 'This takes a few seconds.', false);
    const r = await chrome.runtime.sendMessage({ type: 'cmd', cmd: 'launch' });
    if (r && r.ok && await appUp()) { frame.removeAttribute('src'); await showUI(); return; }
    message('Telvoro didn’t start',
      (r && r.error) || 'Open Telvoro from the Start menu, then click Try again.', true);
  } finally {
    busy = false;
  }
}

$('msg-btn').addEventListener('click', show);

// The phone page tells us before it restarts itself (Settings → Restart).
window.addEventListener('message', e => {
  if (e.origin === BASE && e.data && e.data.sipphone === 'restarting') restarting = Date.now();
});

async function waitForRestart() {
  message('Restarting Telvoro…', 'Applying your new settings.', false);
  frame.removeAttribute('src');
  const t0 = Date.now();
  while (Date.now() - t0 < 60000) {
    await new Promise(r => setTimeout(r, 800));
    if (await appUp()) { restarting = 0; await showUI(); return; }
  }
  restarting = 0;
  message('Telvoro didn’t come back', 'Open Telvoro from the Start menu, then click Try again.', true);
}

// Options page → "Open settings" while the panel is already open.
chrome.storage.onChanged.addListener((ch, area) => {
  if (area === 'session' && ch.openSettings && ch.openSettings.newValue && !frame.hidden && frame.src) showUI();
});

// Follow the app going down (Quit) or coming back.
chrome.runtime.onMessage.addListener(m => {
  if (!m || m.type !== 'status') return;
  if (!m.status.up && (m.hostState === 'updating' || m.status.updating)) {
    frame.hidden = true; frame.removeAttribute('src');
    message('Updating Telvoro…', 'The new version starts by itself in a moment.', false);
  } else if (!m.status.up && restarting && Date.now() - restarting < 60000) {
    if (!frame.hidden) { frame.hidden = true; waitForRestart(); }
  } else if (!m.status.up && !frame.hidden) {
    // The bridge lost the app. If the app still answers over HTTP the bridge
    // itself is what's down (it reconnects by itself) — keep the phone page.
    appUp().then(up => {
      if (up || frame.hidden) return;
      frame.hidden = true;
      frame.removeAttribute('src');
      teamCfg().then(tc => { if (tc && tc.enabled) showTeam(); else { message('Telvoro is stopped', 'Click Start to bring it back.', true); $('msg-btn').textContent = 'Start'; } });
    });
  } else if (m.status.up && (frame.hidden || teamShown) && !busy) {
    $('msg-btn').textContent = 'Try again';
    show();
  }
});

show();
