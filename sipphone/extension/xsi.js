// Team presence straight from the phone system, with no desktop app: a port of
// sipphone/xsi_events.py. Runs in the extension's offscreen document and keeps
// one streaming Xsi-Events channel open; state goes out through onChange().
//
// Needs: a host permission for the Xsi server (requested at setup), fetch with
// ReadableStream (chunked XML), DOMParser.
'use strict';

const ACTIONS = '/com.broadsoft.xsi-actions/v2.0';
const EVENTS = '/com.broadsoft.xsi-events/v2.0';
const ASYNC = '/com.broadsoft.async' + EVENTS;
const PACKAGES = ['Standard Call', 'Do Not Disturb'];
const EXPIRES = 3600;
const HEARTBEAT_MS = 15000;

const SERVICE_TYPES = [
  ['hunt_group', 'Hunt groups', ['hunt group']],
  ['call_center', 'Call centers / queues', ['call center', 'call centre', 'queue']],
  ['auto_attendant', 'Auto attendants', ['auto attendant', 'auto-attendant']],
  ['paging', 'Paging groups', ['group paging', 'paging']],
  ['voice_portal', 'Voice portal / voicemail', ['voice portal', 'voice messaging', 'voicemail']],
  ['conference', 'Conference bridges', ['meet-me', 'meet me', 'collaborate', 'conference']],
  ['other', 'Other services', []],
];
const OTHER_SERVICE = ['voice portal', 'voice messaging', 'voicemail', 'flexible seating', 'broadworks anywhere',
  'find me', 'find-me', 'route point', 'instant group', 'trunk group', 'collaborate', 'meet-me', 'meet me'];
const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const typePats = SERVICE_TYPES.filter(t => t[2].length).map(([k, , kws]) => [k, new RegExp('\\b(' + kws.map(esc).join('|') + ')\\b', 'i')]);
const otherPat = new RegExp('\\b(' + OTHER_SERVICE.map(esc).join('|') + ')\\b', 'i');
function serviceType(name, userId) {
  for (const [k, re] of typePats) if (re.test(name || '')) return k;
  if ((userId || '').includes('^') || otherPat.test(name || '')) return 'other';
  return '';
}
const SERVICE_LABELS = Object.fromEntries(SERVICE_TYPES.map(([k, l]) => [k, l]));

// ------------------------------------------------------------------ XML helpers
const parser = new DOMParser();
const local = el => el.localName || el.tagName.replace(/^.*:/, '');
const children = (el, name) => el ? [...el.getElementsByTagName('*')].filter(c => local(c) === name) : [];
const find = (el, name) => el ? [...el.getElementsByTagName('*')].find(c => local(c) === name) || null : null;
const text = (el, name) => { const c = find(el, name); return c ? (c.textContent || '').trim() : ''; };
function parseXml(s) {
  const doc = parser.parseFromString(s, 'application/xml');
  return doc.getElementsByTagName('parsererror').length ? null : doc.documentElement;
}

// Cuts the stream into whole documents (chunk boundaries aren't visible to us).
class DocSplitter {
  constructor() { this.buf = ''; }
  feed(s) { this.buf += s; const out = []; for (;;) { const d = this.next(); if (d === null) break; out.push(d); } return out; }
  next() {
    let b = this.buf.replace(/^\s+/, '');
    if (!b) { this.buf = ''; return null; }
    let start = 0;
    if (b.startsWith('<?xml')) { const e = b.indexOf('?>'); if (e < 0) return null; start = e + 2; }
    const m = /<([A-Za-z_][\w:.-]*)/.exec(b.slice(start));
    if (!m) return null;
    const root = m[1], gt = b.indexOf('>', start + m.index + m[0].length);
    if (gt < 0) return null;
    if (b[gt - 1] === '/') { const doc = b.slice(0, gt + 1); this.buf = b.slice(gt + 1); return doc; }
    const close = '</' + root + '>', i = b.indexOf(close, gt);
    if (i < 0) return null;
    const doc = b.slice(0, i + close.length); this.buf = b.slice(i + close.length); return doc;
  }
}

// ------------------------------------------------------------------ call state
function parseCall(el) {
  const c = { callId: text(el, 'callId'), state: text(el, 'state'), personality: text(el, 'personality'), name: '', number: '', start: 0 };
  const rp = find(el, 'remoteParty');
  if (rp) {
    c.name = text(rp, 'name');
    const addr = text(rp, 'address') || text(rp, 'userDN');
    c.number = addr ? addr.replace(/^(tel|sip):/, '').split(';')[0].split('@')[0].replace(/^\+1/, '') : '';
  }
  for (const k of ['answerTime', 'startTime']) { const t = text(el, k); if (/^\d+$/.test(t)) { c.start = +t / 1000; break; } }
  return c;
}
function statusOf(t) {
  const calls = Object.values(t.calls), low = c => c.state.toLowerCase();
  const active = calls.filter(c => ['active', 'answered'].includes(low(c)));
  const held = calls.filter(c => ['held', 'remoteheld', 'remote held'].includes(low(c)));
  const alerting = calls.filter(c => ['alerting', 'ringing'].includes(low(c)));
  const u = t.user;
  if (active.length || held.length || alerting.length) {
    let chosen, st;
    if (active.length) { chosen = active[0]; st = 'on-call'; }
    else if (held.length) { chosen = held[0]; st = 'on-hold'; }
    else { chosen = alerting[0]; st = /originat/i.test(chosen.personality) ? 'dialing' : 'ringing'; }
    u.with_name = chosen.name; u.with_number = chosen.number;
    u.direction = /originat/i.test(chosen.personality) ? 'outbound' : 'inbound';
    u.since = (st === 'on-call' || st === 'on-hold') ? chosen.start : 0;
    return st;
  }
  u.with_name = u.with_number = u.direction = ''; u.since = 0;
  if (t.hook.toLowerCase() === 'off-hook') return 'on-call';
  if (t.dnd) return 'dnd';
  if (t.monitor !== 'ok' || !t.synced) return 'unknown';
  return 'available';
}
const STATE_LABELS = { available: 'Available', ringing: 'Ringing', dialing: 'Calling out', 'on-call': 'On a call', 'on-hold': 'On hold', dnd: 'Do Not Disturb', offline: 'Offline', unknown: 'Unknown' };
const STALE_AFTER_MS = 90000;

// ------------------------------------------------------------------ the monitor
class XsiPresence {
  // cfg: {host, user, password, domain, users: [], hide: [], showServices: []}
  constructor(cfg, onChange) {
    this.cfg = cfg;
    this.onChange = onChange || (() => {});
    let host = (cfg.host || 'https://xsi.nextiva.com').trim();
    if (!/^https?:\/\//.test(host)) host = 'https://' + host;
    this.base = host.replace(/\/+$/, '');
    this.auth = 'Basic ' + btoa(unescape(encodeURIComponent(`${cfg.user}:${cfg.password}`)));
    this.domain = cfg.domain || (cfg.user.includes('@') ? cfg.user.split('@')[1] : 'nextiva.com');
    this.channelSetId = 'sipphone-ext-' + Math.random().toString(16).slice(2, 10);
    this.channelId = ''; this.ackPath = ''; this.streamBase = this.base;
    this.targets = new Map(); this.order = []; this.services = [];
    this.ready = false; this.connected = false; this.lastError = ''; this.ackError = ''; this.listSource = ''; this.refused = 0;
    this.stopped = false; this.abort = null; this.acks = []; this.acking = false;
    this.seen = new Map(); this.duplicates = 0; this.staleDropped = 0; this.lastEvent = 0; this.lastHeartbeat = 0;
    this.reconnects = []; this.directoryAt = 0; this.refusedAt = new Map();
  }
  uid(u) { return u.includes('@') ? u : `${u}@${this.domain}`; }
  hidden(u) {
    const hide = (this.cfg.hide || []).map(h => h.trim().toLowerCase().replace('@' + this.domain, '')).filter(Boolean);
    if (!hide.length) return false;
    const uid = (u.user_id || '').toLowerCase(), keys = [uid, uid.split('@')[0]];
    const ext = (u.extension || '').replace(/\D/g, ''), num = (u.number || '').replace(/\D/g, '');
    return hide.some(h => {
      if (keys.includes(h) || (h && uid.includes(h))) return true;
      const hd = h.replace(/\D/g, '');
      return (hd && hd === ext) || (hd && num && hd.length >= 3 && num.endsWith(hd));
    });
  }

  async request(method, path, body, base) {
    const r = await fetch((base || this.base) + path, {
      method, body: body || undefined, cache: 'no-store',
      headers: { Authorization: this.auth, 'Content-Type': 'application/xml; charset=utf-8', Accept: 'application/xml' },
    });
    return { status: r.status, text: await r.text() };
  }
  async get(path) {
    const { status, text: t } = await this.request('GET', ACTIONS + path);
    if (status === 401) throw new Error('authentication failed — check the user ID and password');
    if (status === 403) throw new Error('forbidden (HTTP 403) for ' + path);
    if (status !== 200) throw new Error(`HTTP ${status} for ${path}`);
    const root = parseXml(t);
    if (!root) throw new Error('unreadable reply for ' + path);
    return root;
  }
  async directory() {
    const root = await this.get(`/user/${encodeURIComponent(this.uid(this.cfg.user))}/directories/Enterprise`);
    return children(find(root, 'enterpriseDirectory') || find(root, 'groupDirectory') || root, 'directoryDetails').map(d => ({
      user_id: text(d, 'userId'), name: (text(d, 'firstName') + ' ' + text(d, 'lastName')).trim() || text(d, 'userId'),
      number: text(d, 'number'), extension: text(d, 'extension'), department: text(d, 'department'),
    })).filter(u => u.user_id);
  }
  async discover() {
    const show = new Set(this.cfg.showServices || []);
    let dir = [];
    try { dir = await this.directory(); }
    catch (e) { if (!(this.cfg.users || []).length) throw e; }
    const idx = new Map();
    const services = [];
    for (const d of dir) {
      idx.set(d.user_id.toLowerCase(), d); idx.set(d.user_id.toLowerCase().split('@')[0], d);
      d.kind = serviceType(d.name, d.user_id);
      if (d.kind && show.has(d.kind) && !this.hidden(d)) services.push(Object.assign({}, d, { kind_label: SERVICE_LABELS[d.kind], status: 'unknown' }));
    }
    services.sort((a, b) => a.kind.localeCompare(b.kind) || a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
    this.services = services;
    let found;
    const configured = (this.cfg.users || []).map(u => u.trim()).filter(Boolean);
    if (configured.length) { found = configured.map(u => ({ user_id: this.uid(u), name: '', number: '', extension: '', department: '' })); this.listSource = 'configured list'; }
    else {
      const me = this.uid(this.cfg.user).toLowerCase();
      found = dir.filter(d => !d.kind && this.uid(d.user_id).toLowerCase() !== me).map(d => Object.assign({}, d, { user_id: this.uid(d.user_id) }));
      this.listSource = 'company directory';
      if (!found.length) throw new Error('the company directory lists no people');
    }
    for (const u of found) {
      const d = idx.get(u.user_id.toLowerCase()) || idx.get(u.user_id.toLowerCase().split('@')[0]);
      if (d) { u.name = u.name || d.name; u.number = u.number || d.number; u.extension = u.extension || d.extension; u.department = d.department; }
      u.name = u.name || u.user_id.split('@')[0];
    }
    return found.filter(u => !this.hidden(u));
  }

  // ---- channel
  async openChannel() {
    const body = `<?xml version="1.0" encoding="UTF-8"?><Channel xmlns="http://schema.broadsoft.com/xsi"><channelSetId>${this.channelSetId}</channelSetId><priority>1</priority><weight>100</weight><expires>${EXPIRES}</expires></Channel>`;
    let last = '';
    for (const path of [ASYNC + '/channel', EVENTS + '/channel']) {
      this.abort = new AbortController();
      let r;
      try {
        r = await fetch(this.base + path, { method: 'POST', body, signal: this.abort.signal, cache: 'no-store',
          headers: { Authorization: this.auth, 'Content-Type': 'application/xml; charset=utf-8', Accept: 'application/xml' } });
      } catch (e) { last = 'cannot reach the Xsi events service: ' + e.message; continue; }
      if (r.status === 401) throw new Error('authentication failed — check the user ID and password');
      if (r.status !== 200 || !r.body) { last = `HTTP ${r.status} opening the event channel at ${path}`; try { await r.text(); } catch (e) {} continue; }
      const reader = r.body.getReader(), dec = new TextDecoder(), splitter = new DocSplitter();
      let docs = [];
      while (!docs.length) {
        const { value, done } = await reader.read();
        if (done) break;
        docs = splitter.feed(dec.decode(value, { stream: true }));
      }
      if (!docs.length) { last = 'the event channel opened but sent no channel id'; reader.cancel().catch(() => {}); continue; }
      const root = parseXml(docs[0]);
      this.channelId = root ? text(root, 'channelId') : '';
      if (!this.channelId) { last = 'no channelId in the channel reply'; reader.cancel().catch(() => {}); continue; }
      return { reader, dec, splitter, pending: docs.slice(1) };
    }
    throw new Error(last || 'could not open the event channel');
  }
  async subscribe(target, pkg) {
    const body = `<?xml version="1.0" encoding="UTF-8"?><Subscription xmlns="http://schema.broadsoft.com/xsi"><event>${pkg}</event><expires>${EXPIRES}</expires><channelSetId>${this.channelSetId}</channelSetId><applicationId>sipphone</applicationId></Subscription>`;
    const { status, text: t } = await this.request('POST', `${EVENTS}/user/${encodeURIComponent(target)}`, body);
    if (status === 401) throw new Error('authentication failed — check the user ID and password');
    if (status === 403) throw new Error(`not monitored — your login may not watch ${target}`);
    if (status !== 200) throw new Error(`HTTP ${status} subscribing to ${pkg} for ${target}`);
    const root = parseXml(t), sid = root ? text(root, 'subscriptionId') : '';
    if (!sid) throw new Error('no subscriptionId in the reply');
    return sid;
  }
  async ack(eventId) {
    const body = `<?xml version="1.0" encoding="UTF-8"?><EventResponse xmlns="http://schema.broadsoft.com/xsi"><eventID>${eventId}</eventID><statusCode>200</statusCode><reason>OK</reason></EventResponse>`;
    const paths = this.ackPath ? [this.ackPath] : [EVENTS + '/channel/eventresponse', ASYNC + '/channel/eventresponse'];
    let last = 0;
    for (const p of paths) {
      const { status } = await this.request('POST', p, body, this.streamBase);
      if (status === 200 || status === 204) { this.ackPath = p; return; }
      last = status;
    }
    throw new Error(`HTTP ${last} acknowledging event ${eventId}`);
  }
  ackLater(id) {
    this.acks.push(id);
    if (this.acking) return;
    this.acking = true;
    (async () => {
      while (this.acks.length && !this.stopped) {
        const e = this.acks.shift();
        try { await this.ack(e); if (this.ackError) { this.ackError = ''; this.emit(); } }
        catch (err) { if (!this.ackError) { this.ackError = err.message; this.emit(); } }
      }
      this.acking = false;
    })();
  }
  async heartbeat() { const { status } = await this.request('PUT', `${EVENTS}/channel/${this.channelId}/heartbeat`, '', this.streamBase); if (status !== 200 && status !== 204) throw new Error(`HTTP ${status} on heartbeat`); }
  async refresh() {
    await this.request('PUT', `${EVENTS}/channel/${this.channelId}`, `<Channel xmlns="http://schema.broadsoft.com/xsi"><expires>${EXPIRES}</expires></Channel>`, this.streamBase);
    for (const t of this.targets.values()) for (const sid of Object.values(t.subs)) await this.request('PUT', `${EVENTS}/subscription/${sid}`, `<?xml version="1.0" encoding="UTF-8"?><Subscription xmlns="http://schema.broadsoft.com/xsi"><expires>${EXPIRES}</expires></Subscription>`);
  }

  // ---- events
  handle(doc) {
    const root = parseXml(doc);
    if (!root) return '';
    const name = local(root);
    if (name === 'ChannelHeartbeat') { this.lastHeartbeat = Date.now(); return 'heartbeat'; }
    if (name !== 'Event') return name;
    const eventId = text(root, 'eventID');
    const data = find(root, 'eventData');
    const typ = el => { if (!el) return ''; const a = [...el.attributes].find(x => x.localName === 'type'); return a ? a.value.replace(/^.*:/, '') : ''; };
    const kind = data ? typ(data) : typ(root);
    if (kind === 'ChannelTerminatedEvent') return 'terminated';
    if (eventId) {
      this.ackLater(eventId);
      if (this.seen.has(eventId)) { this.duplicates++; return 'duplicate'; }
      this.seen.set(eventId, 1); if (this.seen.size > 512) this.seen.delete(this.seen.keys().next().value);
    }
    const seqText = text(root, 'sequenceNumber'), seq = /^\d+$/.test(seqText) ? +seqText : 0;
    const targetId = text(root, 'targetId');
    let t = this.targets.get(targetId);
    if (!t) { const short = targetId.toLowerCase().split('@')[0]; t = [...this.targets.values()].find(x => x.user.user_id.toLowerCase().split('@')[0] === short); }
    if (!t || !data) return kind;
    if (seq && t.lastSeq && seq < t.lastSeq && kind !== 'CallSubscriptionEvent') { this.staleDropped++; return 'stale'; }
    if (seq) t.lastSeq = Math.max(t.lastSeq, seq);
    const calls = children(data, 'call');
    if (kind === 'CallSubscriptionEvent') {
      t.calls = {};
      for (const c of calls) { const pc = parseCall(c); if (pc.callId && !['released', 'detached'].includes(pc.state.toLowerCase())) t.calls[pc.callId] = pc; }
      t.hook = text(data, 'hookStatus'); t.synced = true;
    } else if (kind === 'HookStatusEvent') { t.hook = text(data, 'hookStatus'); t.synced = true; }
    else if (kind === 'DoNotDisturbEvent') { t.dnd = text(data, 'active').toLowerCase() === 'true'; t.synced = true; }
    else if (kind.startsWith('Call') || kind.startsWith('Conference')) {
      for (const c of calls) {
        const pc = parseCall(c);
        if (!pc.callId) continue;
        if (['released', 'detached'].includes(pc.state.toLowerCase()) || kind === 'CallReleasedEvent' || kind === 'CallDetachedEvent') delete t.calls[pc.callId];
        else {
          const old = t.calls[pc.callId];
          if (old && !pc.start) pc.start = old.start;
          if (old && !pc.name && !pc.number) { pc.name = old.name; pc.number = old.number; }
          t.calls[pc.callId] = pc;
        }
      }
      if ((kind === 'CallReleasedEvent' || kind === 'CallReleasingEvent') && !calls.length) t.calls = {};
      t.hook = Object.keys(t.calls).length ? 'Off-Hook' : 'On-Hook';     // the call list is the truth
      t.synced = true;
    }
    t.lastUpdate = this.lastEvent = Date.now();
    this.emit();
    return kind;
  }

  isStale() { return !this.connected || Date.now() - Math.max(this.lastHeartbeat, this.lastEvent) > STALE_AFTER_MS; }
  health() {
    const ts = [...this.targets.values()];
    return { connected: this.connected, ready: this.ready, stale: this.isStale(), last_event: this.lastEvent / 1000, last_heartbeat: this.lastHeartbeat / 1000,
      channel_id: this.channelId, subscriptions: { ok: ts.filter(t => t.monitor === 'ok').length, refused: ts.filter(t => t.monitor === 'not-monitored').length, error: ts.filter(t => t.monitor === 'error').length },
      reconnects: this.reconnects.slice(), ack_error: this.ackError, duplicates_dropped: this.duplicates, stale_dropped: this.staleDropped,
      directory: { people: this.order.length, services: this.services.length, synced_at: this.directoryAt / 1000 }, last_error: this.lastError, login: 'user login ' + this.cfg.user };
  }
  snapshot() {
    const users = [], stale = this.isStale();
    for (const id of this.order) {
      const t = this.targets.get(id); if (!t) continue;
      const u = t.user, st = statusOf(t);
      users.push(Object.assign({}, u, { status: st, dnd: t.dnd, monitor: t.monitor, stale: stale && t.monitor === 'ok', last_update: t.lastUpdate / 1000,
        state_label: STATE_LABELS[st] || st, detail: t.monitor === 'not-monitored' ? 'Not monitored — your login may not watch this person' : t.monitor === 'error' ? t.error : '' }));
    }
    const order = { ringing: 0, 'on-call': 1, 'on-hold': 2, dialing: 3, dnd: 4, available: 5, offline: 6, unknown: 7 };
    users.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
    const active = users.filter(u => ['on-call', 'on-hold', 'ringing', 'dialing'].includes(u.status)).length;
    return { enabled: true, users, services: this.services, ready: this.ready, connected: this.connected, stale: this.isStale(), last_error: this.lastError,
      active, total: users.length, source: 'events', login: 'user', list_source: this.listSource,
      refused: this.refused, ack_error: this.ackError, ringing: users.filter(u => u.status === 'ringing').map(u => u.name), at: Date.now(), states: STATE_LABELS };
  }
  emit() { try { this.onChange(this.snapshot()); } catch (e) {} }

  // ---- lifecycle
  start() { this.stopped = false; this.run(); }
  async stop() {
    this.stopped = true;
    const subs = [...this.targets.values()].flatMap(t => Object.values(t.subs)), cid = this.channelId;
    this.channelId = '';
    try {
      await Promise.all(subs.map(sid => this.request('DELETE', `${EVENTS}/subscription/${sid}`).catch(() => {})));
      if (cid) await this.request('DELETE', `${EVENTS}/channel/${cid}`, '', this.streamBase).catch(() => {});
    } catch (e) {}
    try { if (this.abort) this.abort.abort(); } catch (e) {}
  }
  async run() {
    let backoff = 5000;
    while (!this.stopped) {
      try { await this.session(); backoff = 5000; }
      catch (e) {
        if (this.stopped) return;
        this.connected = false; this.lastError = e.message || String(e);
        this.reconnects.push({ at: Date.now(), reason: this.lastError }); this.reconnects.splice(0, Math.max(0, this.reconnects.length - 20));
        this.emit();
      }
      if (this.stopped) return;
      await new Promise(r => setTimeout(r, backoff + Math.random() * backoff / 2));   // backoff with jitter
      backoff = Math.min(backoff * 2, 120000);
    }
  }
  async session() {
    const users = await this.discover();
    this.directoryAt = Date.now();
    const next = new Map();
    for (const u of users) {
      const old = this.targets.get(u.user_id);
      if (old) { old.user = u; next.set(u.user_id, old); }
      else next.set(u.user_id, { user: u, calls: {}, hook: '', dnd: false, subs: {}, error: '', synced: false, monitor: 'pending', lastUpdate: 0, lastSeq: 0, registered: null });
    }
    this.targets = next;
    this.order = users.map(u => u.user_id);
    this.lastError = '';
    const { reader, dec, splitter, pending } = await this.openChannel();
    this.connected = true; this.lastHeartbeat = Date.now(); this.emit();
    // subscriptions, six at a time
    const queue = [...this.targets.values()];
    const worker = async () => {
      while (queue.length && !this.stopped) {
        const t = queue.shift();
        const refusedAt = this.refusedAt.get(t.user.user_id) || 0;
        if (refusedAt && Date.now() - refusedAt < 3600000) { t.monitor = 'not-monitored'; t.error = 'not monitored (remembered)'; continue; }
        t.subs = {}; t.monitor = 'ok'; t.error = '';
        for (const pkg of PACKAGES) {
          try { t.subs[pkg] = await this.subscribe(t.user.user_id, pkg); }
          catch (e) {
            if (e.message.startsWith('not monitored')) { t.monitor = 'not-monitored'; t.error = e.message; this.refusedAt.set(t.user.user_id, Date.now()); break; }
            if (pkg !== PACKAGES[0]) break;                     // calls subscribed, DND refused: still monitored
            t.monitor = 'error'; t.error = e.message; break;
          }
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(6, queue.length || 1) }, worker));
    if (this.stopped) return;
    let errs = [...this.targets.values()].filter(t => t.error);
    // people the phone system won't let us watch stay listed as Unknown
    const refused = [...this.targets.values()].filter(t => t.monitor === 'not-monitored');
    this.refused = refused.length;
    errs = [...this.targets.values()].filter(t => t.monitor === 'error');
    this.ready = true;
    if (this.targets.size && refused.length === this.targets.size) this.lastError = 'the phone system lets this login watch nobody in the directory — ask your provider for monitoring rights';
    else if (this.targets.size && errs.length === this.targets.size) this.lastError = errs[0].error;
    this.emit();
    for (const d of pending) this.handle(d);
    // heartbeats + refreshes on a timer while we sit on the stream
    let beat = Date.now(), refreshed = Date.now(), failure = '';
    const timer = setInterval(async () => {
      try {
        if (Date.now() - beat >= HEARTBEAT_MS) { await this.heartbeat(); beat = Date.now(); this.lastHeartbeat = beat; }
        if (Date.now() - refreshed >= EXPIRES * 600) { await this.refresh(); refreshed = Date.now(); }
      } catch (e) { failure = 'heartbeat failed: ' + e.message; try { this.abort.abort(); } catch (x) {} }
    }, 500);
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) throw new Error(failure || 'the event channel was closed by the server');
        for (const doc of splitter.feed(dec.decode(value, { stream: true }))) {
          if (this.handle(doc) === 'terminated') throw new Error('the event channel was terminated by the server');
        }
      }
    } catch (e) {
      if (this.stopped) return;
      throw new Error(failure || (e.name === 'AbortError' ? 'the event channel dropped' : e.message));
    } finally {
      clearInterval(timer);
      this.connected = false;
      try { reader.cancel(); } catch (e) {}
    }
  }
}

// Quick check for the setup form: login, list sizes.
async function xsiCheck(cfg) {
  const p = new XsiPresence(cfg, () => {});
  const notes = [];
  let ok = true;
  try {
    const dir = await p.directory();
    const people = dir.filter(d => !serviceType(d.name, d.user_id)).length;
    notes.unshift(`Login OK. Directory: ${people} people, ${dir.length - people} services. The phone system decides who this login may watch; the rest show as Unknown.`);
  } catch (e) { return { ok: false, message: 'Login failed: ' + e.message }; }
  return { ok, message: notes.join(' ') };
}

if (typeof self !== 'undefined') Object.assign(self, { XsiPresence, xsiCheck, DocSplitter, serviceType, SERVICE_TYPES, SERVICE_LABELS, STATE_LABELS, statusOf, parseCall, parseXml });
