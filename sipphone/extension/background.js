// SIP Phone — background service worker.
// Holds the native-messaging port to the desktop app (which also keeps this
// worker alive), mirrors its status into the toolbar badge, raises
// notifications, and turns right-clicks on phone numbers into calls.

const HOST = 'com.sipphone.host';
const DEFAULTS = { notifyIncoming: true, notifyTeamRinging: false };

let port = null;
let status = { up: false };
let hostState = 'connecting';          // connecting | ok | missing | error | updating
let updateWatch = null;                // timer while the app installs an update
let missingRetry = null;               // slow retry while the bridge isn't registered
let reqId = 0;
const pending = new Map();

const who = c => (c && c.name && c.name !== c.number) ? `${c.name} - ${c.number}` : ((c && c.number) || '');
const prefs = async () => Object.assign({}, DEFAULTS, await chrome.storage.sync.get(DEFAULTS));

// ---------------------------------------------------------------- connection
function connect() {
  if (port) return;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (e) {
    hostState = 'missing'; publish(); updateBadge(); return;
  }
  hostState = 'connecting';
  port.onMessage.addListener(onHostMessage);
  port.onDisconnect.addListener(() => {
    const err = (chrome.runtime.lastError && chrome.runtime.lastError.message) || '';
    port = null;
    const wasUpdating = hostState === 'updating' || !!status.updating;
    hostState = wasUpdating ? 'updating' : (/not found|forbidden|access/i.test(err) ? 'missing' : 'error');
    status = { up: false, updating: wasUpdating };
    for (const [, p] of pending) p.resolve({ ok: false, error: err || 'Lost connection to SIP Phone' });
    pending.clear();
    publish(); updateBadge();
    if (wasUpdating) waitForUpdate();                          // the bridge is being replaced: poll instead
    else if (/forbidden|access/i.test(err)) introduce();        // bridge is there but doesn't know this copy's ID
    else if (hostState !== 'missing') setTimeout(connect, 3000); // app crashed/restarted: reconnect
    else if (!missingRetry) missingRetry = setInterval(() => { // installer re-registers it: pick that up
      if (hostState === 'missing') { hostState = 'connecting'; connect(); }
      else { clearInterval(missingRetry); missingRetry = null; }
    }, 20000);
  });
  publish();
}

// Chrome refused the native connection: the bridge is registered, but this
// copy's extension ID (a Chrome Web Store install gets its own) isn't on its
// allow-list yet. If the app answers over HTTP, tell it who we are — it adds
// the ID and rewrites the bridge manifest — then connect again.
let introducing = false;
async function introduce() {
  if (introducing) return;
  introducing = true;
  let ok = false, added = false;
  try {
    const r = await fetch('http://127.0.0.1:8088/api/extension/hello', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: chrome.runtime.id }), cache: 'no-store' });
    const j = r.ok ? await r.json() : {};
    ok = !!j.ok; added = !!j.added;
  } catch (e) { /* app not running: fall through to the slow retry */ }
  introducing = false;
  if (ok) { hostState = 'connecting'; setTimeout(connect, added ? 1500 : 3000); return; }
  hostState = 'missing'; publish(); updateBadge();
  if (!missingRetry) missingRetry = setInterval(() => {
    if (hostState === 'missing') { hostState = 'connecting'; connect(); }
    else { clearInterval(missingRetry); missingRetry = null; }
  }, 20000);
}

// While the update helper swaps the files, nothing may hold the bridge open.
// Watch the app's local port instead, and reconnect once it is back.
function waitForUpdate() {
  if (updateWatch) return;
  const t0 = Date.now();
  updateWatch = setInterval(async () => {
    let back = false;
    try { back = (await fetch('http://127.0.0.1:8088/api/version', { cache: 'no-store' })).ok; } catch (e) {}
    if (back || Date.now() - t0 > 10 * 60 * 1000) {
      clearInterval(updateWatch); updateWatch = null;
      hostState = 'connecting'; status = { up: false };
      publish(); connect();
    }
  }, 2000);
}

function request(cmd, extra = {}) {
  if (hostState === 'updating') return Promise.resolve({ ok: false, error: 'SIP Phone is updating — back in a moment.' });
  connect();
  if (!port) {
    return Promise.resolve({ ok: false, error: hostState === 'missing'
      ? 'The SIP Phone desktop app is not installed on this computer.' : 'Not connected to SIP Phone.' });
  }
  const id = ++reqId;
  return new Promise(resolve => {
    pending.set(id, { resolve });
    port.postMessage(Object.assign({ cmd, id }, extra));
    setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); resolve({ ok: false, error: 'SIP Phone did not respond.' }); }
    }, 25000);
  });
}

// How this copy was installed, and which build of the files it carries
// (install.json is written by the app when it packages the extension).
let installInfo = null;
async function getInstallInfo() {
  if (installInfo) return installInfo;
  try {
    installInfo = await (await fetch(chrome.runtime.getURL('install.json'))).json();
  } catch (e) {
    installInfo = { via: 'folder', build: '' };      // a 1.x copy loaded from the folder
  }
  return installInfo;
}

// The app reports the build of the extension files it ships. If this copy is
// older:  folder install -> reload once (the app has refreshed the folder);
//         zip install    -> ask the bridge for a fresh zip in Downloads and
//                           tell the user to drop it on the extensions page.
async function maybeSelfUpdate(appVersion, appBuild) {
  const info = await getInstallInfo();
  const mine = info.build || chrome.runtime.getManifest().version;
  const target = appBuild || appVersion;
  if (!target || target === mine) return;
  if (info.via === 'store') return;                 // the Web Store updates those
  if (info.via !== 'folder') { offerZipUpdate(appVersion, target).catch(() => {}); return; }  // zip / bundle copy
  const { reloadedFor } = await chrome.storage.local.get('reloadedFor');
  if (reloadedFor === target) return;              // folder wasn't updated (old copy loaded): don't loop
  await chrome.storage.local.set({ reloadedFor: target });
  chrome.runtime.reload();
}

let zipOffer = null;                                // {name, folder} of the saved zip
async function offerZipUpdate(appVersion, build) {
  const { zipOfferedFor } = await chrome.storage.local.get('zipOfferedFor');
  if (zipOfferedFor === build) return;
  const r = await request('extension-zip');
  if (!r.ok) return;
  await chrome.storage.local.set({ zipOfferedFor: build });
  zipOffer = { name: r.name, folder: r.folder };
  chrome.notifications.create('ext-zip', {
    type: 'basic', iconUrl: 'icons/icon128.png', title: 'SIP Phone extension update',
    message: `${r.name} was saved to your Downloads folder. Drag it onto the extensions page to update (SIP Phone ${appVersion}).`,
    requireInteraction: true, priority: 1, buttons: [{ title: 'Open extensions page' }]
  });
}
self.offerZipUpdate = offerZipUpdate;

function onHostMessage(m) {
  if (m.type === 'status') {
    if (m.up) maybeSelfUpdate(m.version, m.extension_build).catch(() => {});
    hostState = (!m.up && m.updating) ? 'updating' : 'ok';
    const prev = status;
    status = m;
    publish(); updateBadge(); updateMenus(); maybeNotify(prev, m);
  } else if ((m.type === 'result' || m.type === 'pong') && pending.has(m.id)) {
    pending.get(m.id).resolve(m);
    pending.delete(m.id);
  }
}

function publish() {
  chrome.storage.session.set({ status, hostState }).catch(() => {});
  chrome.runtime.sendMessage({ type: 'status', status, hostState }).catch(() => {});
  syncTeamOnly().catch(() => {});
}

// ------------------------------------------------- Team view without the app
// With [teamOnly] saved (side panel → "Just show my team"), an offscreen
// document streams presence straight from the phone system whenever the
// desktop app isn't running. The app, when up, always wins.
let teamOnlyRunning = '';        // JSON of the config the offscreen client runs with
let teamSnap = null;
async function teamOnlyConfig() {
  const { teamOnly } = await chrome.storage.local.get('teamOnly');
  return teamOnly && teamOnly.enabled && teamOnly.user && teamOnly.password ? teamOnly : null;
}
async function ensureOffscreen() {
  if (!chrome.offscreen) return false;
  const has = await chrome.offscreen.hasDocument();
  if (!has) {
    await chrome.offscreen.createDocument({ url: 'offscreen.html', reasons: ['DOM_PARSER'],
      justification: 'Parse XML presence events streamed from the phone system' });
  }
  return true;
}
async function syncTeamOnly() {
  const cfg = await teamOnlyConfig();
  const want = cfg && !status.up ? JSON.stringify(cfg) : '';
  if (want === teamOnlyRunning) return;
  if (!want) {
    teamOnlyRunning = '';
    teamSnap = null;
    chrome.storage.session.set({ team: null }).catch(() => {});
    if (chrome.offscreen && await chrome.offscreen.hasDocument()) {
      await chrome.runtime.sendMessage({ offscreen: true, type: 'team-stop' }).catch(() => {});
      await chrome.offscreen.closeDocument().catch(() => {});
    }
    updateBadge();
    return;
  }
  if (!(await ensureOffscreen())) return;
  teamOnlyRunning = want;
  await chrome.runtime.sendMessage({ offscreen: true, type: 'team-start', cfg }).catch(() => {});
}
chrome.storage.onChanged.addListener((ch, area) => {
  if (area === 'local' && ch.teamOnly) syncTeamOnly().catch(() => {});
});
self.teamSnapNow = () => teamSnap;

// Availability notifications in Team-only mode: rules live in storage
// ({teamWatch: {userId: {name, events, once, sound}}}); transitions are judged
// here on each snapshot, only while the channel is live (never from stale state).
const WATCH_EVENTS = { available: 'is now available', 'after-call': 'finished a call and is available', busy: 'is on a call', dnd: 'turned on Do Not Disturb', offline: 'went offline' };
const CALLISH = ['on-call', 'on-hold', 'ringing', 'dialing'];
let prevStatus = new Map(), lastFired = new Map();
async function judgeTransitions(snap) {
  if (!snap || !snap.connected || snap.stale) { if (!snap) prevStatus = new Map(); return; }
  const { teamWatch = {}, voiceNotify = false } = await chrome.storage.local.get(['teamWatch', 'voiceNotify']);
  const next = new Map();
  let changed = false;
  for (const u of snap.users) {
    next.set(u.user_id, u.status);
    const old = prevStatus.get(u.user_id);
    if (old === undefined || old === u.status) continue;
    const rule = teamWatch[u.user_id];
    if (!rule) continue;
    const ev = new Set(rule.events || []), fired = [];
    if (ev.has('available') && u.status === 'available') fired.push('available');
    if (ev.has('after-call') && u.status === 'available' && CALLISH.includes(old)) fired.push('after-call');
    if (ev.has('busy') && CALLISH.includes(u.status) && !CALLISH.includes(old)) fired.push('busy');
    if (ev.has('dnd') && u.status === 'dnd') fired.push('dnd');
    if (ev.has('offline') && u.status === 'offline') fired.push('offline');
    for (const e of fired) {
      const key = u.user_id + ':' + e;
      if (Date.now() - (lastFired.get(key) || 0) < 20000) continue;
      lastFired.set(key, Date.now());
      const text = `${u.name} ${WATCH_EVENTS[e]}`;
      chrome.notifications.create('watch-' + u.user_id, { type: 'basic', iconUrl: 'icons/icon128.png', title: 'Team view', message: text, silent: rule.sound === false });
      if (voiceNotify && chrome.tts) chrome.tts.speak(text, { rate: 1.0, enqueue: true });
      if (rule.once !== false) { ev.delete(e); if (ev.size) rule.events = [...ev]; else delete teamWatch[u.user_id]; changed = true; }
    }
  }
  prevStatus = next;
  if (changed) await chrome.storage.local.set({ teamWatch });
}

// --------------------------------------------------------------------- badge
function updateBadge() {
  const c = status.call, t = status.team;
  let text = '', color = '#5C6B70', title = 'SIP Phone';
  const presence = status.mode === 'presence';
  if (!status.up && teamSnap) {
    const r = teamSnap.ringing || [];
    if (r.length) { text = 'RING'; color = '#B8352A'; title = `${r[0]} is ringing`; }
    else if (teamSnap.active) { text = String(teamSnap.active); title = `Team view · ${teamSnap.active} of ${teamSnap.total} teammates on a call`; }
    else title = 'Team view' + (teamSnap.last_error ? ` — ${teamSnap.last_error}` : teamSnap.connected ? ' · live' : ' · connecting…');
  } else if (!status.up) {
    text = hostState === 'updating' ? '…' : hostState === 'missing' ? '!' : '';
    title = hostState === 'updating' ? 'SIP Phone is updating…'
          : hostState === 'missing' ? 'SIP Phone: desktop app not installed' : 'SIP Phone: not running — click to start';
  } else if (presence) {
    if (t && t.ringing && t.ringing.length) { text = 'RING'; color = '#B8352A'; title = `${t.ringing[0]} is ringing`; }
    else if (t && t.active) { text = String(t.active); title = `Team view · ${t.active} of ${t.total} teammates on a call`; }
    else title = `Team view · ${status.name || 'SIP Phone'}` + (t && t.error ? ` — ${t.error}` : '');
  } else if (c && c.state === 'incoming') {
    text = 'RING'; color = '#B8352A'; title = `Incoming call: ${who(c)}`;
  } else if (c && c.state === 'connected') {
    text = 'ON'; color = '#1E7A4C'; title = `On a call with ${who(c)}`;
  } else if (c && c.state === 'calling') {
    text = '…'; color = '#D08A1C'; title = `Calling ${who(c)}`;
  } else if (!status.registered) {
    text = 'OFF'; color = '#B8352A'; title = `SIP Phone: not registered — ${status.reg_detail || ''}`;
  } else if (t && t.active) {
    text = String(t.active); title = `${status.name || 'SIP Phone'} · ${t.active} of ${t.total} teammates on a call`;
  } else {
    title = `SIP Phone · ${status.name || ''} is registered`;
  }
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  if (chrome.action.setBadgeTextColor) chrome.action.setBadgeTextColor({ color: '#FFFFFF' });
  chrome.action.setTitle({ title });
}

// ------------------------------------------------------------- notifications
async function maybeNotify(prev, cur) {
  const p = await prefs();
  const was = prev.call && prev.call.state, now = cur.call && cur.call.state;
  if (p.notifyIncoming && now === 'incoming' && was !== 'incoming') {
    chrome.notifications.create('incoming', {
      type: 'basic', iconUrl: 'icons/icon128.png', title: 'Incoming call',
      message: who(cur.call) || 'Unknown caller', requireInteraction: true, priority: 2,
      buttons: [{ title: 'Answer' }, { title: 'Decline' }]
    });
  }
  if (was === 'incoming' && now !== 'incoming') chrome.notifications.clear('incoming');
  if (p.notifyTeamRinging) {
    const before = new Set((prev.team && prev.team.ringing) || []);
    for (const n of (cur.team && cur.team.ringing) || []) {
      if (!before.has(n)) {
        chrome.notifications.create(`team:${n}:${Date.now()}`, {
          type: 'basic', iconUrl: 'icons/icon128.png', title: `${n} is ringing`,
          message: 'Open SIP Phone to pick up the call for them.', priority: 1
        });
      }
    }
  }
}

chrome.notifications.onButtonClicked.addListener((id, idx) => {
  if (id === 'incoming') {
    request(idx === 0 ? 'answer' : 'reject');
    chrome.notifications.clear(id);
  } else if (id === 'ext-zip') {
    chrome.tabs.create({ url: 'chrome://extensions/' });
    chrome.notifications.clear(id);
  }
});

chrome.notifications.onClicked.addListener(id => {
  if (id === 'ext-zip') { chrome.tabs.create({ url: 'chrome://extensions/' }); chrome.notifications.clear(id); return; }
  chrome.windows.getLastFocused(w => { if (w) chrome.sidePanel.open({ windowId: w.id }).catch(() => {}); });
  chrome.notifications.clear(id);
});

// ------------------------------------------------------------- click-to-call
function extractNumber(raw) {
  const s = String(raw || '').trim();
  const m = s.match(/\+?\d[\d\s().\-]{5,}\d/) || s.match(/[\d*#+]{2,}/);
  return m ? m[0].replace(/[^\d+*#]/g, '') : '';
}

async function callNumber(raw) {
  const number = extractNumber(raw);
  if (!number) return { ok: false, error: `No phone number found in “${String(raw).slice(0, 40)}”.` };
  return request('call', { number });
}
self.callNumber = callNumber;          // used by tests
self.request = request;
self.extractNumber = extractNumber;
self.maybeSelfUpdate = maybeSelfUpdate;
self.onHostMessage = onHostMessage;
self.hostStateNow = () => hostState;

function setupMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: 'call-sel', title: 'Call “%s” with SIP Phone', contexts: ['selection'] });
    chrome.contextMenus.create({ id: 'call-link', title: 'Call with SIP Phone', contexts: ['link'],
                                 targetUrlPatterns: ['tel:*', 'callto:*', 'sip:*'] });
    updateMenus();
  });
}

// No call menus when the app is a Team view only.
let menusVisible = true;
function updateMenus() {
  const want = status.mode !== 'presence' && !(teamSnap && !status.up);
  if (want === menusVisible) return;
  menusVisible = want;
  for (const id of ['call-sel', 'call-link']) chrome.contextMenus.update(id, { visible: want }, () => chrome.runtime.lastError);
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  // Opening the side panel must happen first, inside the click gesture.
  if (tab && tab.windowId !== undefined) chrome.sidePanel.open({ windowId: tab.windowId }).catch(() => {});
  const raw = info.menuItemId === 'call-link'
    ? decodeURIComponent((info.linkUrl || '').replace(/^(tel|callto|sip):/i, ''))
    : info.selectionText;
  const r = await callNumber(raw);
  if (!r.ok) {
    chrome.notifications.create({ type: 'basic', iconUrl: 'icons/icon128.png',
                                  title: 'Couldn’t place the call', message: r.error || 'Unknown error' });
  }
});

// ----------------------------------------------------------------- lifecycle
function init() {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
  connect();
  updateBadge();
}
chrome.runtime.onInstalled.addListener(() => { setupMenus(); init(); });
chrome.runtime.onStartup.addListener(init);

chrome.runtime.onMessage.addListener((m, _sender, reply) => {
  if (m && m.type === 'team') {                       // from the offscreen presence client
    const prev = teamSnap;
    teamSnap = m.snap;
    chrome.storage.session.set({ team: teamSnap }).catch(() => {});
    updateBadge();
    judgeTransitions(teamSnap).catch(() => {});
    if (teamSnap && !status.up) maybeNotify({ team: { ringing: (prev && prev.ringing) || [] } }, { team: { ringing: teamSnap.ringing || [] } }).catch(() => {});
    return;
  }
  if (m && m.type === 'offscreen-ready') { if (teamOnlyRunning) teamOnlyRunning = ''; syncTeamOnly().catch(() => {}); return; }
  if (m && m.type === 'team-check') {               // side panel → offscreen round trip
    ensureOffscreen().then(() => chrome.runtime.sendMessage({ offscreen: true, type: 'team-check', cfg: m.cfg })).then(reply, e => reply({ ok: false, message: String(e) }));
    return true;
  }
  if (m && m.type === 'hello') {
    if (hostState === 'missing') hostState = 'connecting';   // user may have installed it since
    connect();
    reply({ status, hostState });
    return;
  }
  if (m && m.type === 'cmd') {
    request(m.cmd, m.args || {}).then(reply);
    return true;                        // async reply
  }
});

init();
