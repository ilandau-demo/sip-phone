// Offscreen document: keeps the Xsi-Events stream open while Chrome runs, when
// the Team view works without the desktop app. State goes to the worker.
'use strict';
let mon = null;
chrome.runtime.onMessage.addListener((m, _s, reply) => {
  if (!m || !m.offscreen) return;
  if (m.type === 'team-start') {
    if (mon) mon.stop();
    mon = new XsiPresence(m.cfg, snap => chrome.runtime.sendMessage({ type: 'team', snap }).catch(() => {}));
    mon.start();
    reply({ ok: true });
  } else if (m.type === 'team-stop') {
    const old = mon; mon = null;
    (old ? old.stop() : Promise.resolve()).catch(() => {}).then(() => {
      chrome.runtime.sendMessage({ type: 'team', snap: null }).catch(() => {});
      reply({ ok: true });
    });
    return true;
  } else if (m.type === 'team-check') {
    xsiCheck(m.cfg).then(reply);
    return true;
  }
});
chrome.runtime.sendMessage({ type: 'offscreen-ready' }).catch(() => {});
