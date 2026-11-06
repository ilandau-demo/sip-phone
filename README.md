# sipphone — a SIP 2.0 softphone for Nextiva (and any standard SIP platform)

A small, readable SIP/2.0 (RFC 3261) user agent written in plain Python. No PJSIP,
no native build step — the SIP stack, Digest auth, SDP, RTP and G.711 codec are all
in this repo so you can see and change exactly what goes on the wire.

**Tested:** Digest-challenged REGISTER, outbound INVITE → 100/180/200/ACK, CANCEL,
BYE in both directions, inbound call answer/reject, OPTIONS keep-alive, G.711
µ-law/A-law round trip, RFC 4733 DTMF, 200-OK retransmission until ACK.

```
python3 -m unittest tests.test_flows -v      # 13 tests, ~6 s, no network needed
```

## Install (Windows, PowerShell)

```powershell
pip install --upgrade sipphone-1.1.0-py3-none-any.whl     # or any newer version file
pip install piper-tts                                     # optional: text-to-speech
sipphone --init                                           # creates and opens your config.ini
sipphone --tts piper --ui                                 # run
```

Updates are the same `pip install --upgrade <new .whl>`; your config, contacts and
phrases live in `%APPDATA%\sipphone` and are never touched by an update.
(`sipphone --version` shows what's installed.) If you already have a
`config.ini` in the folder you run from, that one is used instead.

### Start-menu app (Windows)

```powershell
sipphone --install-shortcut
```
adds **SIP Phone** to the Start menu — right-click it → *Pin to Start*. It runs
with no console, opens the UI, and uses the settings in your config (`[tts]
engine = piper`, `[stt] engine = whisper` are migrated into the voice engine
on first start). Quit from the UI.

### Team view (enterprise presence)

Presence comes from BroadWorks **XSI Events** — the platform pushes ringing,
calls, hold and Do Not Disturb for everyone in the company directory your
login may monitor. Nothing is polled for presence; the directory is re-read
every 15 minutes and subscriptions are renewed before they lapse.

Two logins, one engine (`sipphone/presence/`):

```ini
[presence]
mode         = events               ; your own user login (recommended)
xsi_user     = jdoe1234567@example.com
xsi_password =                      ; stored protected (DPAPI on Windows)

[admin]                             ; or mode = admin: an Admin XSI login
xsi_host       = https://xsi.example.com
username       = xsi.admin
password       =
directory_user = jdoe1234567        ; a real user whose directory lists everyone
```

People the platform refuses to let you monitor stay listed as **Unknown ·
not monitored** — never shown as available. The Team tab has search-as-you-
type (name, extension, department, title…), presence filters, a details panel
per person, and availability notifications ("tell me when Alice is free").
Services (hunt groups, queues, auto attendants…) can be listed as speed dials.
Set it all from **Settings → Team view**.

### Team view only (no phone line)

`[app] mode = presence` (or Settings → "Team view only"): the app never
registers and refuses calls; the Team view works from the user or admin login.
The browser extension can also do this **with no desktop app at all**: add the
extension, click its icon, choose "Just show my team" and enter your user
login — presence streams straight from the platform into the side panel.

### Updating

Settings → Updates → *Install from file* (a wheel or the share zip), or set an
update source (`[update] source` = folder, UNC share or https URL to a
`latest.json`) so every copy offers — or with `auto = true` installs — new
versions. The install itself runs in a detached helper after the app quits
(Windows locks the running .exe launchers and the browser bridge).

### Without the installer

`pip install --upgrade sipphone-<v>-py3-none-any.whl` then `python -m sipphone
--setup` (or just start `sipphone-app`): the app writes its config, the
Start-menu shortcut, the extension folder/zip and registers the browser bridge
itself (Windows: HKCU `NativeMessagingHosts` keys via `winreg`; the manifest is
kept in `%USERPROFILE%\.sipphone` because Store Python virtualises AppData).
`Install.bat` adds: installing Python if missing, piper/whisper prompts, and a
PowerShell fallback for Store Python < 3.11, whose registry writes are virtualised.

## Requirements

* Python 3.9+ (3.13 fine — no `audioop` dependency)
* For live mic/speaker: `pip install sounddevice numpy`
  (`sounddevice` needs PortAudio: `brew install portaudio` on macOS,
  `sudo apt install libportaudio2` on Debian/Ubuntu, bundled on Windows wheels)
* Without `sounddevice` the phone still registers and signals; audio falls back to
  `backend = file` (play a WAV / record the far end) or `null`.

## Connect it to Nextiva

Nextiva registers third‑party devices as a **Generic SIP Phone**:

1. Sign in to the NextOS admin portal → **Voice & Analytics → Users → Manage Users**.
2. Edit the user that should own this phone → **Device** tab.
3. Set *Available Devices* to **Generic SIP Phone**. The page shows four values:
   **SIP Username**, **Domain**, **Authentication Name**, **Password**. (Set a SIP
   password here if one isn't set — it is separate from the portal login.) Save.
4. Easiest: start the app (`sipphone-app`, or `python -m sipphone --ui`) and enter
   these in **Settings** (gear icon, top right) — it opens by itself on a fresh
   install and re-registers as soon as you save. Or copy `config.example.ini` →
   `config.ini` and fill in:

   | Nextiva field        | config.ini key     |
   |----------------------|--------------------|
   | SIP Username         | `username`         |
   | Domain               | `domain`           |
   | Authentication Name  | `auth_username`    |
   | Password             | `password`         |

   Leave `proxy` blank (the domain is the proxy). Port is **5060**. Codecs stay
   `PCMU,PCMA` — Nextiva is G.711. Nextiva does not pin third‑party devices to a
   MAC address, so you can run several of these against different users.

5. Run it:

   ```
   python3 -m sipphone --config config.ini
   ```

   ```
   registering 2485551234@nextiva.com via nextiva.com:5060 ...
   *** registration OK: expires 3600s
   sip> call 6165550199
   *** connected with 6165550199 (PCMU)
   sip> dtmf 1#
   sip> hangup
   ```

   Add `--debug` to print every SIP message (handy when Nextiva support asks for a
   trace), `--auto-answer` for an unattended line, `--call NUMBER` to dial on start.

### Browser UI

```
python3 -m sipphone --config config.ini --tts piper --ui
```

Opens `http://127.0.0.1:8088` in your browser (loopback only — nothing is exposed
to the network): dial pad, answer/decline, call timer, caller ID, and a transcript
panel where anything you type is spoken to the caller. Quick phrases are saved to
`phrases.txt` next to your config, and contacts (name + number, click the phone
icon to call, also used for caller ID) to `contacts.json`. The terminal prompt keeps working alongside it.

**Settings** (gear icon) edits `config.ini` in place, keeping its comments.
Account and Team-view changes apply immediately (re-register / rebuild the
monitor); listen port, audio devices and TTS/STT need a restart (one click in
app mode). Passwords are write-only — `GET /api/settings` reports only whether
one is saved.

The local API accepts POSTs only as `application/json` from the page itself
(`http://127.0.0.1:8088`), a browser extension, or a program that sends no
`Origin` header, and every request must carry a `127.0.0.1`/`localhost` Host
header — so other websites can't drive the phone (CSRF / DNS rebinding).

### Voice engine (text-to-speech and the caller transcript)

Voice is a modular subsystem (`sipphone/voice/`): the softphone talks to a
`VoiceEngine`, which talks to **providers** behind one interface. Add, test,
switch or remove providers from **Settings → Voice engine** — switching takes
effect on the next sentence, and a provider failure falls back to the next
one instead of interrupting the call.

Built-in text-to-speech providers: **Piper** (offline neural; drop your own
`.onnx` voices in the models folder), **Kokoro** (offline neural), **System
voices** (Windows / macOS / espeak — always available), an **OpenAI-compatible
API** (OpenAI, or a self-hosted server on another computer), **ElevenLabs**,
and **Custom program** (anything that writes a WAV). Speech-to-text:
**Faster-Whisper**, **Whisper**, **Vosk**, **Sherpa-ONNX**, **Custom program**.
Pick TTS and STT independently. The optional model manager downloads voices
and recognizers (sizes and licenses shown first); nothing is bundled.

Providers and defaults live in `voice.json` next to `config.ini` (API keys
protected). The old `[tts]`/`[stt]` sections are migrated once.

Type text during a call and the caller hears it; `--stt` shows what the
caller says as text. From the CLI:

```
sip> say Hi, this is an automated test call. Press 1 to confirm.
sip> dtmf 1
sip> shh                    # cut it off mid-sentence
```

Library use: `VoiceEngine(config_dir).synthesize(text) -> (pcm16, rate)`,
`register(MyProvider)` to add a provider type (see
`voice/provider_interface.py`).

### Firewall / NAT

* Outbound UDP 5060 to the Nextiva domain, and inbound RTP on `rtp_ports`
  (default **10000–20000/UDP**) to this machine.
* The phone sends `rport`, learns its public address from the registrar's
  `received`/`rport` and re‑registers with it, uses symmetric RTP, and sends a
  CRLF keep‑alive every 25 s — this is enough for most home/office NATs. If audio
  is one‑way, set `public_ip` to your WAN IP and forward the RTP range, or disable
  SIP ALG on the router (Nextiva's own guidance for every SIP device).

### If registration fails

| Message                       | Usual cause                                                    |
|-------------------------------|----------------------------------------------------------------|
| `FAILED (authentication failed)` | `auth_username`/`password` wrong — use the Device-tab values, not the portal login |
| `FAILED (403 Forbidden)`      | Device not set to Generic SIP Phone, or the user's line isn't provisioned |
| `FAILED (timeout)`            | UDP 5060 blocked, wrong domain, or a SIP ALG mangling packets |
| `cannot resolve SIP server`   | typo in `domain`/`proxy`                                       |

## Project layout

```
sipphone/
  message.py    SIP 2.0 request/response parsing & serialisation (RFC 3261 §7)
  digest.py     Digest auth MD5/SHA-256, qop=auth/auth-int (RFC 2617/7616)
  sdp.py        SDP offer/answer for G.711 + telephone-event
  transport.py  UDP transport, rx thread, keep-alive
  g711.py       PCMU / PCMA codec (pure Python + numpy fast path)
  rtp.py        RTP session, 20 ms pacing, RFC 4733 DTMF, symmetric RTP
  audio.py      sounddevice mic/speaker, WAV file backend, null backend
  phone.py      User agent: REGISTER/refresh/423/NAT, INVITE/ACK/CANCEL/BYE,
                re-INVITE hold, transfers, conference, retransmission timers
  tts.py/stt.py the audio-path adapters (speech frames into RTP; utterances out)
  authentication/  user login or Admin XSI login; protected credential store
  presence/     the presence engine: directory + cache, XSI Events connection,
                subscriptions, event processing (dedupe/stale), notifications, search
  voice/        the voice engine: provider interface, registry, built-in TTS/STT
                providers, model manager
  xsi.py        BroadWorks Xsi-Actions client (directory, registrations)
  cli.py        interactive front end + app start-up
  web.py/ui.html  local browser UI (--ui): JSON API over the same objects
  settings.py   Settings screen backend: validate, save, apply live / flag restart
  config_store.py  comment-preserving config.ini writer
  app.py        Windows app mode (sipphone-app), Start-menu shortcut, restart
  native_host.py  Chrome/Edge native-messaging bridge for the extension
  extension/    the Chrome/Edge extension (MV3): side panel, click-to-call, and
                Team Presence on its own (xsi.js, offscreen.js)
  extension_sync.py  packages it: droppable zip (Downloads) + self-updating folder
  updater.py    in-app updates: sources, staging, lock, detached pip helper
tests/test_flows.py        mock BroadSoft-style switch + end-to-end call tests
tests/test_presence_engine.py  the presence engine against a mock XSI server
tests/test_presence_mode.py  Team-view-only mode + presence API
tests/test_auth.py         credential store, logins, probe()
tests/test_voice.py        voice engine, providers, fallback, model manager
tests/test_settings.py     config writer, settings API, live re-register, API lock-down
tests/test_updater.py      update staging/sources/lock, real pip upgrade in a venv
tests/ext_e2e.py           the Chrome extension driven in Chromium (needs port 8088 free)
tests/teamonly_e2e.py      Team Presence Only in Chromium, no desktop app
```

## Using it as a library

```python
from sipphone.phone import Config, SIPPhone
from sipphone.audio import open_audio

phone = SIPPhone(Config(username="2485551234", auth_username="2485551234_x",
                        password="…", domain="nextiva.com"), audio=open_audio())
phone.on_incoming_call = lambda call: phone.answer(call.call_id)
phone.start()
phone.register(wait=True)
call = phone.call("6165550199")
...
phone.hangup(); phone.stop()
```

## Not implemented (yet)

TLS/SRTP (the standard device profile is UDP 5060 + RTP, so this works
without it), incoming REFER (answered 603), video, G.729.
Each would slot into `phone.py` / `rtp.py` without touching the rest.
