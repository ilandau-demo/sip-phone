const DEFAULTS = { notifyIncoming: true, notifyTeamRinging: false };
const ids = Object.keys(DEFAULTS);
chrome.storage.sync.get(DEFAULTS).then(v => { for (const k of ids) document.getElementById(k).checked = !!v[k]; });
chrome.storage.local.get({ voiceNotify: false }).then(v => { document.getElementById('voiceNotify').checked = !!v.voiceNotify; });
document.getElementById('voiceNotify').addEventListener('change', e => {
  chrome.storage.local.set({ voiceNotify: e.target.checked });
  if (e.target.checked && chrome.tts) chrome.tts.speak('Voice notifications are on.');
});
for (const k of ids) {
  document.getElementById(k).addEventListener('change', e => {
    chrome.storage.sync.set({ [k]: e.target.checked }).then(() => {
      document.getElementById('saved').textContent = 'Saved';
      setTimeout(() => { document.getElementById('saved').textContent = ''; }, 1500);
    });
  });
}
fetch(chrome.runtime.getURL('install.json')).then(r => r.json()).then(i => {
  const how = { zip: 'installed from a zip (drag a new zip onto chrome://extensions when Nextiva Companion App asks)',
                bundle: 'loaded from the download folder (drag the new zip onto chrome://extensions when Nextiva Companion App asks)',
                folder: 'loaded from the SIP Phone Extension folder (updates itself)',
                store: 'installed from the store (updates itself)' }[i.via] || i.via;
  const el = document.createElement('div'); el.id = 'how'; el.style.cssText = 'margin-top:8px;font-size:13px;color:#51656B';
  el.textContent = `Extension ${chrome.runtime.getManifest().version} — ${how}.`;
  document.getElementById('conn').after(el);
}).catch(() => {});
chrome.storage.session.get(['status', 'hostState']).then(({ status, hostState }) => {
  const el = document.getElementById('conn');
  if (hostState === 'missing') el.textContent = 'Desktop app: not installed on this computer.';
  else if (status && status.up) el.textContent = `Desktop app: running (v${status.version}) · ${status.registered ? 'registered as ' + (status.name || '') : 'not registered'}`;
  else el.textContent = 'Desktop app: not running.';
});

// Settings live in the phone UI; open the side panel straight onto them.
// sidePanel.open() must run inside the click, so the window id is looked up first.
let winId = null;
chrome.windows.getCurrent().then(w => { winId = w.id; });
document.getElementById('open-settings').addEventListener('click', () => {
  const opened = winId !== null ? chrome.sidePanel.open({ windowId: winId }) : Promise.reject();
  chrome.storage.session.set({ openSettings: Date.now() });
  opened.catch(() => {
    document.getElementById('saved').textContent = 'Click the Nextiva Companion App toolbar icon, then the gear (⚙) at the top.';
  });
});
