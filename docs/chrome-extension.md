# Telvoro — Chrome/Edge extension (2.4.0: Team Presence Only built in; Option A bridge since 1.6.0)

## 2.4.0: Team Presence Only (Option C, built — formerly "planned")
Works with no desktop app, no Python, no native messaging, no local models.
- Entry: side panel, when the bridge reports the app isn't installed →
  "Just show my team (no install)" → setup form (`#setup` in sidepanel.html):
  XSI server (default https://xsi.nextiva.com), user login, password, service
  types to list as speed dials, hide list. Check / Save. Saved as
  `chrome.storage.local.teamOnly` {enabled, host, user, password, hide,
  showServices}; the password stays in the browser profile. Only user logins —
  Admin XSI credentials never go to the browser.
- Host permission: `optional_host_permissions: https://*/*` (+ 127.0.0.1 /
  localhost for tests); `chrome.permissions.request({origins:[xsiOrigin + '/*']})`
  is called synchronously inside the Check/Save click (before any await) so the
  gesture counts. Extension fetches bypass CORS.
- Engine: `extension/xsi.js` is a JS port of `sipphone/presence/`: directory
  (`/user/<me>/directories/Enterprise`, people vs services via `serviceType`),
  streaming channel via `fetch` + `ReadableStream` (async path first, then
  plain), `DocSplitter`, subscriptions "Standard Call" + "Do Not Disturb" six at
  a time, refusals → `monitor: 'not-monitored'` (status unknown, remembered 1 h),
  acks with path fallback off the read loop, heartbeat every 15 s, refresh at
  60 %, eventID dedupe (512) + sequenceNumber stale drop, hook follows the call
  list, `isStale()` 90 s, reconnect with backoff + jitter, `health()`.
  Statuses: available / ringing / dialing / on-call / on-hold / dnd / offline /
  unknown; `snapshot()` ⇒ {users, services, connected, stale, refused, ringing,
  ack_error, …}.
- Where it runs: an **offscreen document** (`offscreen.html` + `offscreen.js`,
  reason DOM_PARSER) so the stream survives the service worker sleeping.
  background.js `syncTeamOnly()`: whenever `teamOnly.enabled` and the app is not
  up (`status.up` false — bridge missing or app stopped), create the offscreen
  doc and send `team-start {cfg}`; when the app comes up, `team-stop` + close
  (the app always wins). Runs on SW start → survives browser restarts.
  Messages: offscreen → `{type:'team', snap}` → `teamSnap`, `storage.session.team`,
  badge (`N` on calls / RING / "Team view" title), context menus hidden.
- Side panel Team view (`#team`): search-as-you-type with highlight, filter
  chips with counts + department select (sessionStorage), department groups,
  services section, details dialog (`#pdlg`) with directory fields and
  "Notify me when this person…" checkboxes, bell per row, footer "live /
  stale / reconnecting · N not monitored · acknowledgements failing".
- Availability notifications: rules in `chrome.storage.local.teamWatch`
  {userId: {name, events[available|after-call|busy|dnd|offline], once, sound}};
  background `judgeTransitions(snap)` compares statuses between snapshots only
  while connected and not stale, 20 s min gap per (user,event), one-shot rules
  consumed; `chrome.notifications` (silent when sound=false) and
  `chrome.tts.speak()` when the Options page's "Speak availability
  notifications" (`voiceNotify`) is on. Manifest permissions now include
  `offscreen` and `tts`.
- Tests: `tests/teamonly_e2e.py` (Chromium, no native host registered): install
  message offers it, setup + Check, live statuses, hunt group as service,
  badge, pushed ringing within 2 s, answered/released, search/filters/details,
  rule → notification, one-shot consumed, panel reopens to the team, turn off →
  subscriptions/channel deleted. The mock's origin is added as a required host
  permission in the test copy (headless Chrome can't show the prompt).
- Not in this mode: calling, pick-up, transfer (need the app). Possible later:
  Xsi Click-to-Dial (`calls/new`) with the user login.

## 2.1.1: which zip (user dropped the bundle)
- Dropping the share bundle `Telvoro-x.y.z.zip` → "Could not unzip extension for
  install" (no manifest.json at root). Bundle now: `Telvoro Browser Extension.zip`
  + `Browser Extension/` (unpacked, install.json via=bundle). Separate download:
  `Telvoro-Browser-Extension-<v>.zip`. Reproduced with a real drag under Xvfb
  (`tests/drop_e2e.py`).

## 2.1.0: droppable zip
- Chrome's extensions page accepts a dropped `.zip` in Developer mode; manifest.json
  must be at the zip root; fixed `key` keeps the ID. `extension_sync.zip_bytes()/
  write_zip()` build it (default `~/Downloads/telvoro-extension-<v>.zip`).
  `install.json` {via: zip|folder|bundle|store, build}; `build_id()` = sha256 of the
  files. background.js compares builds: folder → `chrome.runtime.reload()`; zip →
  `extension-zip` host command + notification with "Open extensions page".

## 2.0.0 additions
- Status carries `mode` and `updating`. Presence mode: badge = teammates on calls /
  RING, context menus hidden. Update hand-off: bridge exits on `updating`;
  background polls `/api/version` and reconnects. Web Store kit zip with LISTING.txt.

## Deploying (1.9.0)
- Source `sipphone/extension/` (in the wheel). `extension_sync.sync()` at every app
  start → `%USERPROFILE%\SIP Phone Extension`; Load unpacked once; self-reload on
  version change (Developer mode must stay on). Fixed `key` → ID
  `ekooaigkfmobhjmioleeahcanjnhhheh` (private key in repo `.extkey/`, never shipped).

## How it works (Option A, with the app)
Side panel embeds `http://127.0.0.1:8088/?embed=1`; badge RING / ON / OFF / "!";
Answer/Decline notifications; right-click **Call "…" with Telvoro**; Options
page: incoming-call alerts, teammate-ringing alerts, spoken availability
notifications, Open settings. Bridge = native messaging host `com.sipphone.host`
(`sipphone-native-host.exe`), registered by the app itself (2.2.0). Local API
lock-down: JSON POSTs from the page / a chrome-extension origin / no Origin only.

## Publishing (Chrome Web Store, unlisted)
Upload the `-webstore.zip` (no `key`); listing + justifications in LISTING.txt
(nativeMessaging, sidePanel, contextMenus, notifications, storage, offscreen,
tts, optional host permission for the XSI server — presence straight from the
phone system with the user's own login; no remote code). After approval add the
store ID to `$ExtensionIds` in `installer/install.ps1`.
