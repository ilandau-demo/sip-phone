# sipphone — setup and usage (current: 2.4.0)

Python SIP 2.0 softphone for demo calls into devices, registered to BroadWorks
platforms (Nextiva) as a "Generic SIP Phone". Four subsystems behind defined
interfaces: telephony (`phone.py`), presence (`sipphone/presence/`), voice
(`sipphone/voice/`), and the Chrome/Edge extension (which can run Team
Presence on its own). Browser UI on http://127.0.0.1:8088, also embedded in the
extension side panel (see chrome-extension.md).

## 2.4.0 architecture (non-negotiables from the plan)
- **BLF is gone**: no SIP SUBSCRIBE/dialog events, no Busy Lamp Field reads,
  no `blf.py`. Presence = XSI directory + XSI Events only. Nothing polls for
  presence (directory refresh every 15 min, subscriptions renewed before expiry,
  optional 5-min registration sweep for Offline with admin logins).
- **Two logins only** (`sipphone/authentication/`): `UserCredentials` (the
  user's own XSI login) or `AdminCredentials` (Admin XSI login; desktop app
  only, never sent to the browser). `from_options(mode, admin_opts)` builds
  them from config; `probe()` reports plainly what a login can do. Passwords in
  config.ini / voice.json are stored protected: `dpapi:<b64>` on Windows
  (CryptProtectData, bound to the Windows user), `b64:` elsewhere + chmod 600;
  `config_store.read()` reveals, `update()` protects (plain old values still load).
- **Presence engine** `presence/manager.py: PresenceManager(creds, cache_path,
  users, hide, show_services, concurrency, registration_sweep)` — same
  `start/stop/snapshot()` shape as before plus `health()`, `person(id)`,
  `on_change(fn)`. Modules: `directory.py` (people/services split, cache
  `directory-cache.json`, `find()`), `connection.py` (EventsClient: keep-alive
  pool, channel, acks with path fallback, heartbeats), `subscriptions.py`
  (ThreadPool concurrency, per-person monitor state ok/not-monitored/error,
  refusals remembered 1 h, `renew_due` at 40 % margin), `event_processor.py`
  (DocSplitter, eventID dedupe ring of 512, per-target sequenceNumber stale
  drop, hook follows the call list), `models.py` (states available / ringing /
  dialing / on-call / on-hold / dnd / offline / unknown; `person_dict` carries
  monitor, stale, last_update, directory fields), `notifications.py`
  (AvailabilityWatcher rules in `watch.json`, events available / after-call /
  busy / dnd / offline, one-shot by default, 20 s min gap, never from stale),
  `search.py` (search_people / filter_people). Refused people stay listed as
  **unknown · not-monitored**; a channel drop keeps last-known state flagged
  `stale` (90 s without heartbeat/event also = stale); reconnect = backoff ×2
  with jitter, capped 120 s; `reconnects` history in health.
- API: `/api/monitor` (snapshot), `/api/presence/health`, `/api/presence/person?id=`,
  `/api/presence/search?q=&status=&department=&on_call=&monitored=`,
  `/api/presence/watch` GET (rules, recent) / POST {user_id, events, once, sound}.
  `/api/state` carries `notifications` (last 5) for toasts.
- **Voice engine** `voice/manager.py: VoiceEngine(config_dir)` → `registry.py`
  (`voice.json`: providers [{id,type,name,enabled,config}], defaults {tts,
  tts_fallback, stt, voice, rate, pitch, volume, assignments{purpose: id}}),
  `provider_interface.py` (TTSProvider: availability() → Available /
  not-configured / model-required / unavailable with reason+action, voices(),
  synthesize(text, voice, rate, pitch, volume, cancel) → (pcm16, rate),
  capabilities dict; STTProvider.transcribe(pcm8k) → text; `register(cls)` for
  third-party types). Built-ins: tts/piper (scans config voices_dir, models dir,
  config folder for *.onnx+json), tts/kokoro (kokoro-onnx), tts/native (Windows
  System.Speech via PowerShell / macOS say / espeak-ng), tts/remote
  (OpenAI-compatible /v1/audio/speech, ElevenLabs), tts/custom (command with
  {text}{voice}{rate}{out}); stt/faster_whisper, whisper, vosk, sherpa_onnx,
  custom (command with {wav}). `synthesize()` never raises: primary → fallback →
  silence; `recognize()` → "" on failure. `model_manager.py`: catalog (Piper
  voices from HF rhasspy/piper-voices, Kokoro v1.0, Vosk small), download with
  progress/verify/remove, models dir default `<config>/models`.
  `migrate_from_config([tts],[stt])` runs once when voice.json is empty.
  Audio path: `TTSAudio(lambda t: voice.synthesize(t, "call"))` and
  `Transcriber(engine=voice.recognize)` are always in the chain in phone mode,
  so enabling a provider later needs no restart. API: `/api/voice` (status),
  `/api/voice/providers/add|update|remove`, `/api/voice/defaults`,
  `/api/voice/test` (returns WAV), `/api/voice/say`, `/api/voice/models` (+
  `/download|/remove|/verify|/dir`). Settings → Voice engine shows feature cards
  (TTS / transcript / voice notifications) with real availability states and
  opens the "Voice engine" sheet (defaults, providers, add, models).
- **Team tab UI** (ui.html): search-as-you-type with highlight, filter chips with
  counts + department select (sessionStorage), details dialog `#pdlg` (fields
  returned by the directory only, notify-me checkboxes, Call/Transfer), bell per
  row, toasts + beep + desktop Notification; re-render only on a signature
  change (1 s polls on large directories). Status keys changed `idle` →
  `available`; `error` folded into `unknown` + `monitor` field.
- **Extension Team Presence Only** (`extension/xsi.js` = JS port of the engine,
  run in `offscreen.html` by the service worker): side panel "Just show my team
  (no install)" → setup form (XSI server, user login, password, services to
  list, hide) saved in `chrome.storage.local.teamOnly`; host permission for the
  XSI origin requested in the click. background.js: `syncTeamOnly()` starts the
  offscreen client whenever the app isn't up (also on browser start), badge
  from `teamSnap`, `judgeTransitions()` evaluates `teamWatch` rules →
  chrome.notifications + `chrome.tts.speak` when `voiceNotify` (options page).
  Search/filters/details/notification rules in sidepanel.js mirror the app.
  Manifest: permissions offscreen + tts, optional_host_permissions https://*/*.
- Tests: test_presence_engine (both logins, states, refused→unknown, reconnect
  stale, dedupe/stale, acks, renewal, search, notifications), test_auth,
  test_voice (fake providers, fallback isolation, secrets, migration, command
  providers, model manager over a local HTTP server), test_presence_mode (+ API),
  teamonly_e2e.py (Chromium, no app: setup, live events, search, filters,
  details, rule → notification, off), ext_e2e.py. MockXsi: USER (subscribes to
  `allowed`) and ADMIN (anyone; registrations); BLF endpoint returns 403 and
  counts `blf_requests` so tests prove it is never read.

## Settings screen
Gear icon, top right. Phone line; Team view (presence source User login
(events) / Admin XSI login / Off, login + password, Check, More: only watch these
users, hide, pick-up code, "Also list as speed dials" service types); Voice
engine (feature cards + Manage voice providers); Audio; Startup; Browser
extension; Updates. Save writes config.ini in place (comments kept, `.bak`);
account changes re-register live; Team-view changes rebuild the engine live;
local port / audio need a restart. Passwords write-only (blank = keep).

## Sharing with someone else
Send `Telvoro-<version>.zip` (Install.bat, install.ps1, the .whl, READ ME
FIRST.txt, `Telvoro Browser Extension.zip`, `Browser Extension/`). No
credentials inside. Presence-only users need only the extension zip. Full
install: Python 3.9+, unzip, Install.bat (installs wheel, optional piper-tts /
faster-whisper, `--setup`, bridge, extension folder, starts the app).

## Where things live
Per-user config folder (Store Python redirects AppData — let Python open files:
`python -m sipphone --init`): config.ini (+.bak), voice.json, models/,
directory-cache.json, watch.json, boards.json, contacts.json, phrases.txt,
history.json, app.log, native-host.log, update.log.

## Key config
- [account] SIP device credentials; proxy_port 5060.
- [presence] mode = events | admin | off; xsi_user / xsi_password (own login).
- [admin] xsi_host; username/password (admin); directory_user (seed);
  directory_type; user_domain; hide_user; show_services (hunt_group, call_center,
  auto_attendant, paging, voice_portal, conference, other); pickup_code;
  concurrency (default 6); registration_sweep (true); users (only watch these).
- [tts]/[stt]: legacy, migrated into voice.json once.
- [app] mode = phone | presence; debug; start_with_windows. [update] source/auto.

## XSI endpoints used
- Actions: /user/<seed>/directories/Enterprise; /user/<id>/profile/Registrations
  (admin sweep; 403 for users → sweep disabled).
- Events: POST /com.broadsoft.async/com.broadsoft.xsi-events/v2.0/channel
  (fallback non-async), POST .../user/<id> subscriptions "Standard Call" + "Do
  Not Disturb", eventresponse (plain path then async fallback; failing acks are
  shown — the Xsp holds the next event until the previous is acked), heartbeat
  PUT every 15 s, channel/subscription PUT refresh.

## Troubleshooting
- Team view footer / Settings → Check: "watch nobody" = no monitoring rights;
  "acknowledgements failing" = eventresponse refused; stale = channel down,
  last state shown. /api/presence/health has everything.
- Voice: Settings → Voice engine cards say Available / Not configured / Model
  required / Unavailable with the fix; Test voice plays in the browser.
- `--debug` / Settings → "Write a full SIP trace" for SIP.

## Earlier notes still valid
Bridge registration (2.2.0: app writes `%USERPROFILE%\.sipphone\com.sipphone.host.json`
+ HKCU keys itself), in-app updates (2.0.0: staging, update.lock, detached
helper, relaunch via `pythonw -m sipphone.app`), modes (2.0.0), installer flags
(`install.ps1 [-Quiet] [-NoTTS] [-STT] [-NoExtension] [-NoStartup] [-Mode] [-UpdateSource]`),
extension packaging (2.1.x: droppable zip + self-updating folder), custom boards
and pinnable tabs (2.3.3), directory-wide events and services as speed dials
(2.3.2–2.3.4), relaunch fix for Store Python (2.3.1).

## Version history
- 2.4.0 modular release: BLF removed; two logins with protected secrets;
  presence engine (directory cache, events, dedupe/stale, states, health,
  notifications, search); Team tab search/filters/details/notifications;
  Team Presence Only in the extension (no app); modular voice engine (BYO TTS/STT,
  model manager); UI cleanup
- 2.3.8 Team view without the app (first version); 2.3.7 ack fallback; 2.3.6
  keep-alive + parallel subscribes; 2.3.5 renamed sources; 2.3.4 services for
  events source; 2.3.3 directory scope + boards; 2.3.2 BLF option removed,
  services as speed dials; 2.3.1 relaunch fix
- 2.3.0 Xsi-Events presence source; 2.2.x installer fixes, bat-free bridge;
  2.1.x droppable extension zip; 2.0.0 presence-only mode, in-app updates,
  installer overhaul; 1.x see CHANGELOG.md
