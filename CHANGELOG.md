# Changelog

## 2.6.0 — 2026-11-13 — up to 8 SIP accounts
- **Several accounts at once.** Line 1 is the Phone line in Settings; Settings →
  *More phone lines* adds lines 2–8 (`[line2]` … `[line8]` in config.ini: line
  name, display name, SIP username, authentication name, password — stored
  protected — domain, proxy, port, on/off). Each line registers on its own and
  gets its own local SIP port; adding, changing or removing a line applies live.
- **Account picker in the header**, where the name was: ● registered / ○ not,
  "on a call"; the chosen line is the one you dial from and Re-register.
- **One call has the audio** (desk-phone style): placing, answering or resuming
  a call on one line puts the talking call on every other line on hold. A call
  that rings on another line while you talk rings with Answer / Decline in a
  strip above the call; held calls on other lines sit there with Resume / Hang
  up. The main Hang up never ends the held calls on other lines. The toolbar
  badge and the Answer/Decline notification follow a call ringing on any line.
- Calls, the call screen blocks, Recent and Registration history show which line;
  call-back and Redial go out on the line the call was on.
- **Re-register reworked.** It now starts clean — re-resolves the server,
  forgets the learned NAT address and any cached authentication, opens a fresh
  registration — waits for the server's answer and shows it ("Front desk
  registered (expires 3600s)" or why not). A NAT port change is now noticed
  (only an address change was before). A failed registration also retries by
  itself (30 s, 60 s, … up to every 5 minutes) instead of staying down until
  someone clicks.
- **"Updated to …" shows once** as a short toast instead of a banner that stayed
  until the next restart.
- Fixes: Settings no longer stays on "Registering…" when the answer comes back
  within a second; the form refreshes before "Saved" so a field typed right
  after saving isn't wiped; saving no longer rebuilds the Team view every time
  (an empty *Event channels* counted as a change); push streams close when the
  server stops, so the extension shows "Restarting…" promptly.
- **Hide the Team view or the transcript.** Settings → *Use Telvoro as* has two
  new ticks (phone mode): *Show the Team view (presence)* — untick to remove
  everything about presence: the Team tab, teammates on the boards,
  availability alerts and the Team view settings section; and *Show the call
  transcript* — untick to hide the conversation area. Both apply at once and
  are kept in config.ini (`[app] show_team`, `show_transcript`).
- Extension files unchanged apart from the version number.

## 2.5.1 — 2026-11-12 — cleaner call screen, redial
- **Cleaner call screen.** Each call is its own block; when it ends it folds
  into a one-line summary (caller · time · duration · messages — click to
  reopen the transcript) and only the last finished call is kept, so the
  screen no longer grows call after call. Registration refreshes are no longer
  written there: Settings → Phone line → *Registration history* lists them
  (`GET /api/registration`). Settings/update notices show as short toasts, and
  availability alerts only as toasts (they were also transcript lines). Toast
  text was invisible (undefined colour) — fixed.
- **Redial.** With nothing in the number field the Call button reads *Redial*
  (tooltip shows who) and calls the last number you dialled — or, if you have
  never dialled out, the last call of any kind.
- READ ME FIRST and the extension's README carry the Telvoro name.
- Extension files unchanged apart from the version number.

## 2.5.0 — 2026-11-09 — presence in under a second; renamed Telvoro - Presence & SIP Phone
- **Renamed** to *Telvoro - Presence & SIP Phone* ("Telvoro" in running text)
  everywhere people see it: Chrome Web Store name and summary ("An independent
  side-panel companion designed to work with Nextiva …"), toolbar tooltip, side panel, notifications, context
  menu, the app page, Start-menu entry, installer and docs. The old "SIP
  Phone" and "Nextiva Companion App" Start-menu and sign-in shortcuts are
  replaced (removed, not left beside the new ones); Uninstall removes any of them. Unchanged on purpose:
  the extension ID, the Python package `sipphone`, the bridge
  `com.sipphone.host`, the config folder and the `SIP Phone Extension` folder —
  existing installs keep their settings and stay connected.
- **Pushed, not polled.** The phone page and the browser bridge open one
  `/api/stream` (Server-Sent Events) and get every change pushed: Team rows,
  boards, call state, event lines, notifications. Measured on the mock
  platform: a teammate's change is painted 2–40 ms after the engine applies it
  (was up to 1,000 ms on the 1 s poll); the toolbar badge follows in ~20 ms.
  If the stream drops, the page polls at the old rate and the footer says
  "live (polling)" until it is back.
- **Several event channels per channel set.** The Xsp holds a channel's next
  event until the previous one is acknowledged, so one channel moves about one
  event per round trip. 2.5.0 opens 1/2/4 channels for directories of up to
  50/150/more people (`[presence] channels`, 0 = automatic, max 8; also in
  Settings → Team view → More) and acknowledges in parallel, one in-flight ack
  per channel. A burst of 100 events on 300 people clears in 0.78 s on the
  mock with 30 ms acks (four channels; ideal 0.75 s). If the platform refuses
  extra channels the engine runs on what it got and health says so.
- **Measured, in the footer.** Every event is timed: platform → us (from the
  event's own startTime/answerTime), engine, ack round trip, and paint time
  reported by the page. The Team tab footer shows "updates in 0.4 s (p95;
  platform 0.3 s)"; `/api/presence/health` has the full breakdown
  (`latency`, `channels`).
- Start-up: 300 people subscribe in ~0.5 s (`[admin] concurrency` default 12);
  the acknowledgement path that worked is remembered in the directory cache so
  the first ack after a restart does not probe; ack connections are warmed and
  use TCP_NODELAY.
- **Fix: "heartbeat failed: HTTP 404" → presence going stale.** When the XSI
  name resolves to several Xsp servers (or a balancer spreads connections),
  heartbeats and acks sent on a new connection could reach a server that
  doesn't hold the channel. The whole channel set is now pinned to the server
  the first channel landed on (TLS still verified against the name) and any
  balancer cookie from the channel reply is sent back on every request. A 404
  names the channel and server in the log and reopens at once.
- In-app updates reinstall a rebuilt wheel that has the version already
  installed (pip alone reports "already installed" and changes nothing).
- **Services: filter and rename.** The Team tab has a chip per service kind
  (Hunt groups, Call centers / queues, Auto attendants …) with counts; a
  service chip alone shows just those services, with a status chip people too,
  and the search works inside it. Settings → Team view → *Service display
  names*: pick any service the directory found, type the name to show instead
  of its ID, Save (or *Use directory name*). Saved in `group-names.json`;
  applies to the Team tab, boards, speed dials, transfer and search, live.
- **Group display names.** The group a person belongs to (the directory's
  group ID, e.g. 3149647G, shown as *Group* in their details) can be given a
  name such as "Arizona": Settings → Team view → *Group display names* lists
  every group with its member count. Saved in `org-group-names.json`; shown
  in details and searchable.
- **No Offline state.** Someone with no device registered shows Available;
  the Offline filter and the "goes offline" alert are gone, and the 5-minute
  registration sweep is off by default (`[admin] registration_sweep = true`
  turns it back on, it no longer changes the status).
- **Cleaner alerts.** A person's details now read "Alert me when Alice…" with
  one option per line (Becomes available / Finishes a call / Goes on a call /
  Turns on Do Not Disturb), a separate "Play a sound with the alert", and a
  note that each alert fires once. Same in the extension's Team view.
- Fix: the status dot in a person's details was always grey; it now uses the
  status colour (green Available, amber on a call, red ringing/DND).
- Store screenshots and promo tile redrawn with the Telvoro name; release files
  are now `Telvoro-<v>.zip`, `Telvoro-Browser-Extension-<v>.zip` and
  `webstore/telvoro-extension-<v>-webstore.zip`.
- Example config cleaned of the retired BLF options.
- Extension files unchanged — the store package is a version bump only.

## 2.4.7 — 2026-11-08
- Settings → Browser extension no longer asks for a store ID. Both copies of
  the extension (Chrome Web Store, zip/unpacked build) are accepted
  automatically; the bridge tells the app which copy launched it, and
  Settings shows "Connected: the Chrome Web Store copy". An "Extra extension
  ID" field remains under *Custom extension build* for self-signed copies.
  Extension files unchanged — no store upload needed.

## 2.4.6 — 2026-11-08
- The Chrome Web Store listing's ID (`kghndilnhefammdeabhnjlphlmeeomno`) is
  built into the bridge allow-list, and `Install.bat` now opens the store page
  (`$StoreUrl` set). Store installs connect to the app with no setup, even
  before the store copy updates to 2.4.5+. Extension files unchanged since
  2.4.5 — no new store upload needed.

## 2.4.5 — 2026-11-08
- Browser extension installed from the Chrome Web Store now connects to the
  desktop app by itself: when Chrome refuses the bridge because the copy's ID
  is unknown, the extension introduces itself to the running app
  (`POST /api/extension/hello`, origin-checked), the app adds the ID to
  `[extension] ids` and rewrites the bridge manifest, and the extension
  reconnects. No more pasting the store ID into Settings.
- Side panel no longer flips to "SIP Phone is stopped" while the app still
  answers over HTTP (bridge hiccups reconnect quietly); the bridge needs two
  failed polls in a row (5 s timeout each) before reporting the app down, and
  logs why in native-host.log.
- `--diagnose` reads the app's per-user config even when run from a folder
  that has none.

## 2.4.4 — 2026-11-08
- Start-menu and start-at-sign-in shortcuts now run `pythonw -m sipphone.app`
  instead of the pip-made `sipphone-app.exe` launcher, which stops working
  when the Python it was installed with is removed or updated (Store Python).
  Re-run `--setup`, `--install-shortcut` or toggle "Start with Windows" to
  rewrite an existing shortcut. Install.bat starts the app the same way.
- Start-up: a copy of the app left over from a removed install that holds
  port 8088 without answering is stopped and replaced, instead of the browser
  opening a dead "127.0.0.1 didn't send any data" page. If another program
  owns the port, a message box says which one.
- `python -m sipphone --diagnose` shows the installed version/interpreter and
  who holds the app port; stored-password (DPAPI) check hardened.

## 2.4.3 — 2026-11-05
- Installer: set `$StoreUrl` at the top of install.ps1 once the extension is on
  the Chrome Web Store; Install.bat then opens the store page (one click,
  auto-updates) instead of the drag-the-zip flow and records the store ID for
  the bridge. Without it, the zip/folder flow is unchanged.

## 2.4.2 — 2026-11-05
- Chrome Web Store support: Settings → Browser extension → "Chrome Web Store
  extension ID" (or [extension] store_id) adds the published extension's ID to the
  bridge registration immediately, so store installs can talk to the app.
  Submission kit regenerated (store zip, screenshots, listing, privacy policy).

## 2.4.1 — 2026-11-05
- Call scripts: lines spoken one after another on the active call with a pause
  you set between each. "Add a script" / "Edit scripts" next to the quick phrases:
  name, lines with "Pause after (s)", reorder, import a .txt (one line per row,
  `pause 3` between lines), export. One ▶ button plays the script; a bar shows the
  line being spoken with Stop; the script ends with the call. Stored in scripts.json.

## 2.4.0 — 2026-11-05
A modular release: telephony, presence, voice and the extension are separate
subsystems behind defined interfaces.
- **BLF removed entirely.** No SIP SUBSCRIBE/dialog events, no Busy Lamp Field
  reads, no blf.py; presence is BroadWorks XSI directory + XSI Events only.
- **Authentication**: exactly two options — user login (events) or Admin XSI login.
  Passwords are stored protected (DPAPI on Windows; obfuscated + chmod 600 elsewhere)
  in config.ini and voice.json. Check reports plainly what a login can do.
- **Presence engine** (`sipphone/presence/`): one engine for both logins. Directory
  cache (instant start, search never waits), batched subscriptions with
  configurable concurrency and renewal, duplicate/stale event handling, standard
  states (Available, Ringing, Dialing, On call, On hold, DND, Offline, Unknown),
  last-known state flagged stale while reconnecting, exponential backoff with
  jitter, a health report (/api/presence/health). Refused people stay listed as
  Unknown · not monitored — never shown available. Admin logins get Offline from a
  5-minute registration sweep. The polling admin monitor is gone.
- **Team tab**: search-as-you-type with highlighting, presence filter chips and a
  department filter (kept for the session), a details panel per person (fields
  the directory actually returns), and availability notifications per person and
  event (toast + sound + desktop/voice notification; one-shot by default; never
  from stale state). /api/presence/search, /person, /watch.
- **Team Presence Only** in the extension: works with no desktop app, no Python,
  no native messaging. Same directory, live presence, search, filters, details,
  availability notifications (Chrome notifications + built-in Chrome TTS),
  reconnects after a browser restart. The app, when running, takes over.
- **Voice engine** (`sipphone/voice/`): provider interface + registry; built-in
  TTS providers Piper, Kokoro, System voices, OpenAI-compatible API, ElevenLabs,
  Custom program; STT providers Faster-Whisper, Whisper, Vosk, Sherpa-ONNX,
  Custom program. Bring your own models (Piper-compatible .onnx in the models
  folder) or remote endpoints. Defaults, fallback provider, per-purpose
  assignment (calls vs. notifications), rate/volume, voice test in the browser,
  enable/disable/remove without restarting. Provider failures never interrupt a
  call. Optional model manager with sizes, licenses, progress, verify and remove.
  Old [tts]/[stt] settings migrate once into voice.json.
- **UI cleanup**: feature cards with real availability states (Available / Not
  configured / Model required / Unavailable), readable secondary text, consistent
  product name, no leftover BLF or placeholder copy.

## 2.3.8 — 2026-11-05
- Team view without the desktop app: the extension's side panel offers "Just show my team
  (no install)". A user login streams presence (Xsi-Events, port of xsi_events.py in
  extension/xsi.js) in an offscreen document; badge, ringing notifications, services as
  speed dials, Busy Lamp Field list or whole directory. When the app is running it takes over.
  New optional host permission for the XSI server (asked for once at setup).

## 2.3.7 — 2026-11-05
- Event acknowledgements: the Xsp holds the next event on a channel until the previous one is
  acknowledged (or times out), so a failing ack looked like a ~5 s presence lag. Acks now check
  the reply, fall back to the async servlet path, and a persistent failure is shown in the Team
  tab footer and app.log instead of being silent. Team tab polls every second.

## 2.3.6 — 2026-11-05
- Events presence is much faster: one keep-alive connection per server instead of a new TLS
  handshake for every subscribe/ack, subscriptions made six at a time, event acknowledgements
  off the stream thread, and the Team tab refreshes every 2 s. Stopping the channel is instant.

## 2.3.5 — 2026-11-05
- Presence sources renamed: "User login (events)" and "Admin XSI login"; provider names dropped from the UI text.

## 2.3.4 — 2026-11-05
- "Also list as speed dials" (hunt groups, call centers, auto attendants, ...) now applies to the
  events source too, not only the Xsi admin source; the tick boxes show for both.

## 2.3.4 — 2026-11-05
- "Also list as speed dials" (hunt groups, queues, auto attendants, ...) now applies to the
  My Nextiva login (events) source too, not just the Xsi admin source.

## 2.3.3 — 2026-11-05
- Events source: "Who to watch" — your Busy Lamp Field list (default) or everyone in the company
  directory; with the directory option anyone Nextiva refuses is left out quietly (config: [presence] scope).
- Custom boards: the ⋯ button next to the tabs makes your own tabs mixing teammates (with presence),
  services and contacts (speed dials), and pins/unpins the built-in Team, Contacts and Recent tabs.
  Stored in boards.json next to contacts.json; the last tab is remembered.

## 2.3.2 — 2026-11-05
- BLF Check: when Nextiva answers the SIP SUBSCRIBE with 403, say so and point to the events source.
- The "BLF list on my SIP line" presence source is gone from Settings (Nextiva refuses it for generic devices); configs that still say blf open as events.
- Xsi admin source: only people get presence. New "Also list as speed dials" tick boxes choose which service types (hunt groups, queues, auto attendants, paging, voice portal, conference bridges, other) appear in their own section of the Team tab as click-to-call entries; config key [admin] show_services.

## 2.3.1 — 2026-11-05
- Fix: after an in-app update (and on restart / bridge autostart) the relaunch
  used the pip-made `sipphone-app.exe`, which fails with "Fatal Error in
  Launcher … pythonw.exe … cannot find the file" when started from inside a
  Microsoft Store Python process (its AppData\Local is redirected). All
  relaunches from Python now run `pythonw -m sipphone.app`; the exe launcher is
  only used by Explorer / the Start menu / the installer.

## 2.3.0 — 2026-11-05
- New Team-view source **"My Nextiva login (events)"** — the way Unity-style
  clients do it: an Xsi-Events channel opened with the user's own login,
  subscribed to the "Standard Call" and "Do Not Disturb" packages for everyone on
  the user's Busy Lamp Field list (or "Only watch these users"). BroadWorks
  pushes ringing, on a call (with the far party), hold, hook status and Do Not
  Disturb — no admin account, no SIP subscription, so it also sidesteps the 403
  Nextiva returns to BLF SUBSCRIBEs from generic devices. Heartbeats every 15 s,
  acknowledgements, channel/subscription refresh, automatic reconnect. Now the
  recommended source; "Check" reports login, list size and channel state.
- `[presence] mode = events`; `xsi_user`/`xsi_password` required for it.
- 2.2.3: "Write a full SIP trace to app.log" switch (Settings → Startup) and the
  switch's own reason text on a refused BLF subscription.

## 2.2.2 — 2026-11-05
- Installer fix: `function Py { & $PY ... }` with `$PY = 'py'` called itself
  (PowerShell command names are case-insensitive) → "call depth overflow" on any
  PC with the py launcher — since 1.5.6. Now `Invoke-Py` running `py.exe` /
  `python.exe`. Install.bat keeps its window open; install.ps1 writes install.log
  and names each step.

## 2.2.0 — 2026-11-05
- No installer needed for the browser bridge: the app registers the native
  messaging host itself on every start (`register_bridge()`: manifest in
  `%USERPROFILE%\.sipphone`, HKCU keys for Chrome, Edge and Chromium via winreg;
  per-browser NativeMessagingHosts folders on Linux/macOS). `--setup` and
  `--register-bridge` do it on demand; Settings → Browser extension shows
  "Browser link: ready" or a "Connect now" button. Install.bat only falls back to
  its own PowerShell registration for a Microsoft Store Python older than 3.11
  (the Store build disables registry virtualisation from 3.11 on).
- So a plain `pip install --upgrade <wheel>` + starting the app is a complete
  install/upgrade on a PC that already has Python 3.11+ (READ ME: "Installing
  without Install.bat").
- `--prepare-update` unregisters the bridge while files are replaced (re-done
  at the next start); the allowed extension ids live in `native_host.py`.

## 2.1.1 — 2026-11-05
- Packaging fix: the browser extension is now unmistakable. The share bundle
  holds `SIP Phone Browser Extension.zip` (drop it on chrome://extensions) and an
  unpacked `Browser Extension` folder (for "Load unpacked"); the big
  SIP-Phone-x.y.z.zip is the installer and can't be dropped — the READ ME says so.
  A copy loaded from the bundle folder gets zip-style update notifications.
- `tests/drop_e2e.py`: a real OS drag-and-drop onto chrome://extensions
  (Xvfb + xdotool + GTK) proving the zip installs with the fixed id, and that the
  bundle zip is refused with Chrome's "Could not unzip" message.

## 2.1.0 — 2026-11-05
- The browser extension now also comes as a **zip you drop onto
  chrome://extensions** (Developer mode on) — Chrome unzips it and loads it as an
  unpacked extension with the same fixed ID, so the bridge keeps working. Files sit
  at the zip's root (Chrome requires manifest.json there).
  - `python -m sipphone --extension` saves it to Downloads, opens an Explorer window
    with it selected and the browser's extensions page; Install.bat does the same.
  - Settings → Browser extension: "Download extension zip" and "Open extensions page".
  - The share bundle includes `SIP Phone Extension.zip`.
- Each packaged copy carries `install.json` {via, build}; the app reports the build
  of the files it ships. Folder installs reload only when the files really changed
  (not on every app version); a zip install gets a notification "x.zip was saved to
  Downloads — drag it onto the extensions page" with an "Open extensions page"
  button, once per build. Store installs are left to the store.
- Options page shows how the extension was installed.

## 2.0.0 — 2026-11-05
Deployment and a second way to use the app.

- **Team view only** mode (`[app] mode = presence`, or the mode picker at the top
  of Settings): no SIP registration, no call control — just the presence board.
  Works with the BLF list (SIP credentials used only for the subscription) or an
  Xsi admin login. The side panel shows the board; the toolbar badge shows how
  many teammates are on calls / who is ringing; no call menus. Switching modes
  applies live (phone mode after a presence-mode start needs one restart for audio).
- **Updates inside the app**: Settings → Updates → "Install from file" (a .whl or
  the whole share zip), or an *update source* — a folder / network share with the
  wheels, or an https:// `latest.json` — checked at start and every 6 h, with a
  banner "x.y.z is available · Install now" and an optional automatic install when
  no call is active. A detached helper waits for the app and the browser bridge to
  exit, runs pip, and starts the app again; the extension shows "Updating…" and
  reconnects by itself. `update.lock` keeps the bridge from restarting the old app.
- **Installer**: installs Python by itself (winget, python.org build) if missing;
  recognises the zip-not-extracted mistake; stops the running copy and the bridge
  safely before upgrading (and retries); `-Quiet` for hands-off roll-outs, plus
  `-Mode`, `-UpdateSource`, `-NoTTS`, `-STT`, `-NoExtension`, `-NoStartup`;
  offers "start at sign-in"; `Uninstall.bat`.
- `python -m sipphone --set section.key=value` pre-fills settings for roll-outs;
  `--startup on|off`; `--prepare-update` / `--finish-update` for installers.
- Settings → Startup: "Start SIP Phone when I sign in" (background, no tab).
- Extension: slow retry when the bridge isn't registered yet (no click needed
  after an install); "updating" and presence-mode states.

## 1.9.0 — 2026-11-04
- The browser extension now lives in one fixed folder,
  `%USERPROFILE%\SIP Phone Extension`, which the app refreshes every time it
  starts. Load it once with "Load unpacked"; after that, updating the app also
  updates the extension: it sees the newer app version over the bridge and
  reloads itself from the folder (once per version, so a stale copy can't loop).
- `python -m sipphone --extension` sets the folder up, copies its path to the
  clipboard and opens it plus chrome://extensions (or Edge's).
- Install.bat asks "Add SIP Phone to Chrome/Edge?" on first install and does the
  above; on later installs it just refreshes the folder.
- The share bundle no longer contains a separate "Chrome Extension" folder; the
  extension ships inside the app (sipphone/extension).

## 1.8.1 — 2026-11-04
- Recent calls: every entry has a working phone button for click-to-call (the
  button existed but had no styling, so it didn't show properly).
- BLF Team view + your own Nextiva login: tries to read each teammate's Do Not
  Disturb (Xsi services/DoNotDisturb, every 30 s) and shows the red slashed dot.
  BroadWorks only allows this for users your login may monitor; if it refuses,
  the Team footer says "Do Not Disturb not available to your login" and it
  re-checks every 10 minutes.
- The BLF list URI can be left blank when your Nextiva login is filled in: it's
  read from your Busy Lamp Field service (Xsi services/BusyLampField).
- Settings → Team view → Check now also reports the BLF list found, the
  directory, and whether DND is readable with your login.

## 1.8.0 — 2026-11-04
- Team view can now come from the user's **BLF list** instead of an Xsi admin
  login. The phone SUBSCRIBEs (Event: dialog, eventlist) to the BLF list URI
  using its own SIP credentials; the switch pushes ringing / on a call / on
  hold / idle (and who they're talking to, when the switch includes it) the
  moment it changes. No polling, no admin account.
- Optional "Directory login" — the user's own Nextiva login, not an admin —
  adds extensions and departments from the Enterprise directory.
- Settings → Team view: "Presence source" = BLF list (recommended) / Xsi admin /
  Off. "Check" reports the live subscription state and tests the directory
  login. Existing configs with an [admin] login keep working as "Xsi admin".
- New [presence] config section: mode, blf_uri, xsi_user, xsi_password.
- SIP engine: generic SUBSCRIBE dialogs (digest auth, refresh, NOTIFY before
  200 OK, server-terminated → resubscribe, fresh dialog after account changes).
- "Pick up" works from the BLF list by number when no extension is known.

## 1.7.0 — 2026-11-04
- Settings screen in the app (gear icon, top right) — works in the browser and
  in the Chrome/Edge side panel. Set the SIP account (username, authentication
  name, password, domain, proxy/port), Team view (Xsi server, admin login,
  directory user, hide list, pick-up code, refresh), text-to-speech /
  speech-to-text, and microphone/speaker. Saved to config.ini with its comments
  kept. Account changes re-register immediately; Team view changes rebuild the
  monitor immediately; port/voice/audio changes offer a one-click Restart.
- Passwords are write-only: the page shows whether one is saved, never the value.
  "Test connection" checks the Xsi admin login before saving.
- First run: Settings opens by itself while the template account is still in
  place; the installer starts the app at the end instead of opening Notepad.
- Extension options page: "Open settings" jumps to Settings in the side panel;
  the side panel shows "Restarting…" and reconnects during a restart.
- Security: the local API now only accepts JSON requests from the phone page,
  the extension or local programs, and checks the Host header, so other websites
  can't place calls or read/change settings through it.
- Fixes: passwords containing % no longer break config loading; the app starts
  (with a warning banner) even if the TTS voice, speech recognizer, audio device
  or SIP port is unavailable, so Settings can fix it; `python -m sipphone.app`
  now runs the app.

## 1.6.0 — 2026-11-04
- Chrome / Edge extension (chrome-extension/): the phone in the browser side
  panel, toolbar badge (RING / ON / OFF / teammates on calls), Answer/Decline
  notifications for incoming calls, optional teammate-ringing alerts, and
  right-click "Call … with SIP Phone" on selected numbers and tel: links.
- Native messaging host `sipphone-native-host` (com.sipphone.host): starts the
  phone without a browser tab, pushes live status to the extension, runs
  call/answer/reject/hangup/quit. Registered for Chrome and Edge by Install.bat.
- `--no-browser` for the app; compact `?embed=1` UI for the side panel; fixed
  horizontal overflow on narrow (<760px) windows.

## 1.5.6 — 2026-11-04
- New `sipphone --setup`: one-step first-time setup (config, Piper voice download,
  Start-menu shortcut, opens the config). Turns on TTS/STT in a new config when
  those packages are installed. Used by the shareable installer.
- Config template defaults to Nextiva's Xsi host (https://xsi.nextiva.com) and SIP
  port 5060; personal example IDs replaced with generic ones.

## 1.5.5 — 2026-10-22
- --check-admin now shows the actual Team list (after the users_only filter and
  hide_user), with "Hidden by hide_user" and "Filtered as service instances"
  lines, so you can verify what the Team view will show. Previously it printed the
  raw directory, which made hide_user look like it wasn't applied.

## 1.5.4 — 2026-10-22
- hide_user matching is more forgiving: a value now matches a user's userId
  (full, bare, or substring), their extension, or the trailing digits of their
  DID (e.g. the last 4). The userId remains the surest value to use.

## 1.5.3 — 2026-10-22
- [admin] hide_user = <userId(s) or extension(s)>: hides those lines from the Team
  view (e.g. your own). Matches userId, userId@domain, extension, or number.

## 1.5.2 — 2026-10-21
- Team view groups members by department (from the enterprise directory), with a
  per-department header and an "N on a call" count. Falls back to a flat list when
  no departments are set in the directory.

## 1.5.1 — 2026-10-21
- Call Parks are now shown in the Team view (they count as users); hunt groups,
  paging, auto attendants, call centers, etc. are still filtered.
- The service-instance filter is configurable: [admin] exclude = <comma keywords>
  overrides the built-in list.

## 1.5.0 — 2026-10-21
- Header shows your display name (from [account] display_name) instead of the SIP username.
- Tabs reordered to Team, Contacts, Recent; Team is the default when configured.
- Caller ID now shows "Name - Number" on incoming/active calls and in the Team view.
- Team rows show each person's extension; hover to call them.
- Answer on a teammate's behalf: a ringing teammate shows "Pick up" (Directed Call
  Pickup, pickup_code default *97 + their extension).
- From an active call, per-teammate actions: Transfer (blind), Warm (attended), Conf.
  * Warm/attended transfer holds the call, dials the target so you can talk, then
    completes via REFER-with-Replaces (Complete transfer / Conference / Cancel bar).
  * Conference bridges both parties with a local 3-way audio mixer.
- Phone now supports a second concurrent call (consultation) behind these features.

## 1.4.8 — 2026-10-21
- Team view now shows Ringing and Do Not Disturb:
  * Ringing (an incoming Alerting call) = flashing red dot, with the caller's name.
    Detected from the active-calls list, since a ringing phone is still on-hook.
  * Do Not Disturb (services/DoNotDisturb active) = red dot with a line through it.
  * Outbound ringing shows as "Calling out" (amber pulse).
- Status priority: active/held call > ringing > DND > idle. A user on a call with
  DND on still shows as on a call. HookStatus remains the fallback where /calls
  isn't available (that mode can't see ringing).

## 1.4.7 — 2026-10-21
- Team status now uses the HookStatus endpoint (/calls/HookStatus), the reliable
  presence signal on Nextiva: On-Hook = available, Off-Hook = on a call. It also
  appends the user domain (user_domain, default nextiva.com) to bare directory
  userIds, e.g. jsmith1234567 -> jsmith1234567@nextiva.com.
- When a user is off-hook, it still tries /calls for who they're speaking with,
  and if that endpoint isn't usable (HTTP 500 on Nextiva) it falls back to
  busy/idle from HookStatus alone without erroring.

## 1.4.6 — 2026-10-21
- Fix: clicking the Start-menu app now always runs the installed version. If an
  older instance is still running on the UI port (the reason updates didn't show),
  the app shuts it down and starts the current version in its place.
- The running version is shown in the UI header (e.g. "v1.4.6"), and the page is
  served no-cache so an upgrade never loads a stale UI.
- Team filter rewritten: service instances (Call Park, Hunt Group, Group Paging,
  Call Center, Voice Portal, etc., and any userId containing '^') are dropped by
  name/ID pattern, since on Nextiva they answer /calls like a real user would.

## 1.4.5 — 2026-10-21
- Team view now uses the Enterprise directory by default (works on Nextiva;
  the Group directory did not). directory_type = enterprise | group.
- users_only = true (default): filters the directory to real people, dropping
  service instances (auto attendants, hunt groups, call centers, voice portals)
  — detected by whether the entry exposes a call-status endpoint.
- directory_user seed can be a bare userId like jdoe1234567.

## 1.4.4 — 2026-10-21
- Team view: whole-group auto-discovery seeded from a real user. Set
  [admin] directory_user = ca#######@nextiva.com and leave 'users' blank to
  watch everyone in that user's group (the admin login itself usually has no
  directory). --check-admin tests this path too.

## 1.4.3 — 2026-10-21
- `--check-admin`: when [admin] lists specific users, it now tests a real user's
  /calls endpoint (the actual monitoring path) instead of the group directory,
  and gives targeted guidance for 401/403/404. Confirmed the Nextiva Xsi base is
  https://xsi.nextiva.com + /com.broadsoft.xsi-actions/v2.0 (host-only in config).
- Config: the [admin] users field is the reliable way to pick who to watch when
  the admin login can't enumerate the group directory.

## 1.4.2 — 2026-10-21
- New: `sipphone --check-admin` (or `python -m sipphone --check-admin`) tests the
  [admin] Xsi connection and prints the exact result — the resolved request URL
  and whether it reached the server, was rejected (auth), lacked privilege
  (forbidden), hit the wrong path (404), or couldn't reach the host — with a
  specific next step for each. Use it to diagnose an empty Team view.

## 1.4.1 — 2026-10-20
- Fix: the Start-menu app now always opens the per-user config
  (%APPDATA%\sipphone\config.ini) regardless of the folder it is launched from,
  instead of preferring a config.ini that happened to be in the current folder.
  It also logs which config file it loaded (see app.log) so the active account
  is always verifiable. The `sipphone` terminal command is unchanged: run from a
  folder with its own config.ini, it still uses that one.

## 1.4.0 — 2026-10-20
- Admin / supervisor **Team** view: with BroadWorks/Nextiva Xsi-Actions admin
  credentials, see which users in your group are on a call and who they're
  speaking with. New `[admin]` config section (xsi_host, username, password,
  optional users list, poll_seconds). Read-only; polls over HTTPS.
- Auto-discovers your group directory, or watches a specific list of userIds.

## 1.3.0 — 2026-10-20
- Windows app: `sipphone --install-shortcut` adds **SIP Phone** to the Start menu
  (pin it to Start or the taskbar). It launches with no console window, opens the UI,
  and logs to `app.log` in the config folder. Launching it again just reopens the UI.
- Quit button in the UI (app mode) stops the phone cleanly.

## 1.2.0 — 2026-10-20
- Speech-to-text: what the caller says appears in the transcript (`--stt whisper`,
  `pip install faster-whisper`; model downloads on first run). Toggle "Transcribe caller".
- Hold / Resume (re-INVITE), Mute (mic only — typed speech still goes through),
  and blind Transfer (REFER) to a number or contact, in the UI and the CLI.
- Recent calls tab with redial, missed calls in red, saved to `history.json`.
- Browser ringer on incoming calls (two-tone, can be turned off).

## 1.1.1 — 2026-10-20
- Piper voice files are found in the config folder (`%APPDATA%\sipphone`), the current
  folder, or by absolute path in `model =`; a clear message says where to put them.

## 1.1.0 — 2026-10-20
First release as an installable package (`pip install sipphone-1.1.0-py3-none-any.whl`).
- `sipphone` command; `sipphone --init` creates a per-user config
  (`%APPDATA%\sipphone\config.ini` on Windows) — no more running from a folder.
- Browser UI (`--ui`): dial pad, call controls, caller ID, transcript, TTS composer,
  quick phrases, contacts with click-to-call.
- Text-to-speech into the call (`--tts piper|espeak|openai|elevenlabs`).
- SIP 2.0 stack: Digest auth, REGISTER refresh/NAT, INVITE/CANCEL/BYE, re-INVITE hold,
  OPTIONS, RTP G.711 µ-law/A-law, RFC 4733 DTMF.
