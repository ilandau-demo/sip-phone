"""Settings screen backend: what the UI may read and change in config.ini.

Passwords are write-only: the UI is told whether one is saved, never what it
is. Saving writes config.ini (keeping its comments) and then applies what it
can straight away:

  * account fields  -> the phone re-registers with the new details
  * Team view       -> the Xsi monitor is rebuilt
  * pickup code     -> used for the next pickup
  * listen port, audio devices, text-to-speech, speech-to-text
                    -> need a restart (the UI offers a Restart button)
"""
from __future__ import annotations

import importlib.util
import logging
import os
import threading
from typing import Callable, Dict, List, Optional

from . import config_store

log = logging.getLogger("sipphone.settings")

TEMPLATE_USERNAME = "1234567890"
TEMPLATE_PASSWORD = "YOUR_SIP_PASSWORD"

# section, key, apply-group, kind
FIELDS = [
    ("app", "mode", "mode", "str"),
    ("account", "display_name", "account", "str"),
    ("account", "username", "account", "str"),
    ("account", "auth_username", "account", "str"),
    ("account", "password", "account", "secret"),
    ("account", "domain", "account", "str"),
    ("account", "proxy", "account", "str"),
    ("account", "proxy_port", "account", "port"),
    ("local", "bind_port", "restart", "port0"),
    ("audio", "input_device", "restart", "str"),
    ("audio", "output_device", "restart", "str"),
    ("tts", "engine", "restart", "str"),
    ("stt", "engine", "restart", "str"),
    ("stt", "model", "restart", "str"),
    ("presence", "mode", "admin", "str"),
    ("presence", "xsi_user", "admin", "str"),
    ("presence", "xsi_password", "admin", "secret"),
    ("presence", "channels", "admin", "str"),
    ("admin", "xsi_host", "admin", "str"),
    ("admin", "username", "admin", "str"),
    ("admin", "password", "admin", "secret"),
    ("admin", "directory_user", "admin", "str"),
    ("admin", "users", "admin", "str"),
    ("admin", "hide_user", "admin", "str"),
    ("admin", "users_only", "admin", "bool"),
    ("admin", "show_services", "admin", "str"),
    ("admin", "poll_seconds", "admin", "float"),
    ("admin", "pickup_code", "pickup", "str"),
    ("update", "source", "update", "str"),
    ("update", "auto", "update", "bool"),
    ("app", "start_with_windows", "startup", "bool"),
    ("app", "show_team", "ui", "bool"),
    ("app", "show_transcript", "ui", "bool"),
    ("app", "debug", "restart", "bool"),
    ("extension", "store_id", "bridge", "str"),
    ("account", "label", "line1", "str"),
]
# Lines 2–8: [line2] … [line8], same account keys plus a label and an on/off switch
for _n in range(2, 9):
    FIELDS += [(f"line{_n}", "enabled", f"line{_n}", "bool"), (f"line{_n}", "label", f"line{_n}", "str"),
               (f"line{_n}", "display_name", f"line{_n}", "str"), (f"line{_n}", "username", f"line{_n}", "str"),
               (f"line{_n}", "auth_username", f"line{_n}", "str"), (f"line{_n}", "password", f"line{_n}", "secret"),
               (f"line{_n}", "domain", f"line{_n}", "str"), (f"line{_n}", "proxy", f"line{_n}", "str"),
               (f"line{_n}", "proxy_port", f"line{_n}", "port")]
DEFAULTS = {("app", "show_team"): "true", ("app", "show_transcript"): "true", ("app", "mode"): "phone", ("app", "debug"): "false", ("update", "auto"): "false", ("app", "start_with_windows"): "false",
            ("account", "proxy_port"): "5060", ("local", "bind_port"): "5060",
            ("admin", "xsi_host"): "https://xsi.nextiva.com", ("admin", "users_only"): "true",
            ("admin", "poll_seconds"): "5", ("admin", "pickup_code"): "*97"}
for _n in range(2, 9):
    DEFAULTS[(f"line{_n}", "proxy_port")] = "5060"
    DEFAULTS[(f"line{_n}", "enabled")] = "true"
TTS_ENGINES = ("", "piper", "espeak", "openai", "elevenlabs")
STT_ENGINES = ("", "whisper", "vosk")
LABELS = {
    ("account", "username"): "SIP Username", ("account", "auth_username"): "Authentication Name",
    ("account", "password"): "SIP password", ("account", "domain"): "Domain",
    ("account", "proxy_port"): "Proxy port", ("local", "bind_port"): "Local SIP port",
    ("admin", "poll_seconds"): "Refresh every", ("admin", "xsi_host"): "Xsi server",
    ("admin", "username"): "Admin login", ("admin", "password"): "Admin password",
    ("presence", "xsi_user"): "Directory login",
    ("presence", "xsi_password"): "Directory password", ("presence", "mode"): "Presence source",
    ("presence", "channels"): "Event channels",
    ("app", "mode"): "Mode", ("update", "source"): "Update source", ("app", "show_team"): "Show the Team view", ("app", "show_transcript"): "Show the call transcript",
}
APP_MODES = ("phone", "presence")
PRESENCE_MODES = ("", "events", "admin")


class SettingsError(ValueError):
    pass


def _label(sec: str, key: str) -> str:
    if sec.startswith("line") and sec[4:].isdigit():
        base = LABELS.get(("account", key), key.replace("_", " ").capitalize())
        return f"Line {sec[4:]}: {base}"
    return LABELS.get((sec, key), key.replace("_", " ").capitalize())


def _audio_devices() -> Dict[str, list]:
    """Input/output devices on the default host API, as values sounddevice accepts."""
    try:
        import sounddevice as sd  # type: ignore
        apis = sd.query_hostapis()
        try:
            default_in, default_out = sd.default.device
            api = sd.query_devices(default_out if default_out is not None and default_out >= 0 else default_in)["hostapi"]
        except Exception:
            api = 0
        ins, outs = [], []
        for d in sd.query_devices():
            if d["hostapi"] != api:
                continue
            value = f"{d['name']}, {apis[api]['name']}"
            if d["max_input_channels"] > 0:
                ins.append({"value": value, "name": d["name"]})
            if d["max_output_channels"] > 0:
                outs.append({"value": value, "name": d["name"]})
        return {"input": ins, "output": outs}
    except Exception:
        return {"input": [], "output": []}


def _features(cfg_dir: str, tts_model: str) -> dict:
    piper = importlib.util.find_spec("piper") is not None
    voice = False
    if piper:
        name = tts_model or "en_US-lessac-medium.onnx"
        if not name.endswith(".onnx"):
            name += ".onnx"
        from .cli import config_dir
        for folder in ({os.path.dirname(name)} if os.path.isabs(name) else {cfg_dir, os.getcwd(), config_dir()}):
            cand = os.path.join(folder, os.path.basename(name))
            if os.path.isfile(cand) and os.path.isfile(cand + ".json"):
                voice = True
    return {"piper": piper, "piper_voice": voice,
            "whisper": importlib.util.find_spec("faster_whisper") is not None,
            "sounddevice": importlib.util.find_spec("sounddevice") is not None}


class SettingsManager:
    """Reads/writes config.ini for the UI and applies changes to the running phone."""

    def __init__(self, config_path: str, phone, ui_state=None,
                 build_monitor: Optional[Callable[[dict], object]] = None,
                 on_restart: Optional[Callable[[], None]] = None,
                 running: Optional[Dict[str, str]] = None, started_mode: str = "",
                 on_update_opts: Optional[Callable[[dict], None]] = None) -> None:
        self.path = config_path
        self.phone = phone
        self.ui = ui_state
        self.build_monitor = build_monitor
        self.on_restart = on_restart
        self.on_update_opts = on_update_opts
        self.started_mode = started_mode or self.mode()
        self.lock = threading.Lock()
        # the values the running process actually started with, for "restart needed"
        self.running = dict(running or {})
        if not self.running:
            vals = self._values()
            self.running = {f"{s}.{k}": vals[s][k] for s, k, g, _ in FIELDS if g == "restart"}
        self.monitor_error = ""
        self.setup_needed = self.needs_setup()
        self.pending: List[str] = []

    # ------------------------------------------------------------------ reading
    def _cp(self):
        return config_store.read(self.path)

    def _raw(self, cp, sec: str, key: str) -> str:
        if (sec, key) == ("presence", "mode"):
            from .cli import presence_mode
            g = lambda s_, k_: cp.get(s_, k_) if cp.has_section(s_) and cp.has_option(s_, k_) else ""  # noqa: E731
            return presence_mode({"mode": g("presence", "mode"), "xsi_user": g("presence", "xsi_user"),
                                  "host": g("admin", "xsi_host") or g("admin", "host") or "x",
                                  "username": g("admin", "username")})
        if (sec, key) == ("app", "mode"):
            from .cli import app_mode
            return app_mode(cp.get("app", "mode") if cp.has_section("app") and cp.has_option("app", "mode") else "")
        if (sec, key) == ("app", "start_with_windows"):
            from .app import starts_with_windows
            return "true" if starts_with_windows() else "false"
        if cp.has_section(sec) and cp.has_option(sec, key):
            return cp.get(sec, key)
        if (sec, key) == ("admin", "xsi_host") and cp.has_section("admin") and cp.has_option("admin", "host"):
            return cp.get("admin", "host")
        return DEFAULTS.get((sec, key), "")

    def _values(self) -> Dict[str, Dict[str, str]]:
        cp = self._cp()
        out: Dict[str, Dict[str, str]] = {}
        for sec, key, _, kind in FIELDS:
            out.setdefault(sec, {})[key] = "" if kind == "secret" else self._raw(cp, sec, key)
        return out

    def mode(self) -> str:
        return self._raw(self._cp(), "app", "mode")

    def _account_missing(self, cp) -> bool:
        user = self._raw(cp, "account", "username").strip()
        pw = self._raw(cp, "account", "password").strip()
        dom = self._raw(cp, "account", "domain").strip()
        return not user or not dom or user == TEMPLATE_USERNAME or not pw or pw == TEMPLATE_PASSWORD

    def credential_problem(self) -> str:
        from .authentication import credential_store
        return credential_store.last_problem

    def needs_setup(self) -> bool:
        """True until the chosen mode has what it needs to work."""
        cp = self._cp()
        if self._raw(cp, "app", "mode") == "presence":
            src = self._raw(cp, "presence", "mode")
            if src == "admin":
                return not (self._raw(cp, "admin", "username").strip() and self._raw(cp, "admin", "password").strip())
            if src == "events":
                return not (self._raw(cp, "presence", "xsi_user").strip() and self._raw(cp, "presence", "xsi_password").strip())
            return True                                      # no presence source yet
        return self._account_missing(cp)

    def restart_pending(self) -> List[str]:
        vals = self._values()
        out = [f"{s}.{k}" for s, k, g, _ in FIELDS
               if g == "restart" and vals[s][k] != self.running.get(f"{s}.{k}", vals[s][k])]
        if self.started_mode == "presence" and vals["app"]["mode"] == "phone":
            out.append("app.mode")                           # audio/voice weren't loaded
        return out

    def get(self) -> dict:
        cp = self._cp()
        secrets = {}
        for sec, key, _, kind in FIELDS:
            if kind == "secret":
                v = self._raw(cp, sec, key).strip()
                secrets[f"{sec}.{key}"] = bool(v) and v != TEMPLATE_PASSWORD
        tts_model = self._raw(cp, "tts", "model")
        return {"values": self._values(), "secrets_set": secrets, "path": self.path,
                "mode": self._raw(cp, "app", "mode"), "started_mode": self.started_mode,
                "needs_setup": self.needs_setup(), "restart_pending": self.restart_pending(),
                "can_restart": self.on_restart is not None,
                "features": _features(os.path.dirname(os.path.abspath(self.path)), tts_model),
                "devices": _audio_devices(), "monitor_error": self.monitor_error,
                "service_types": [{"key": k, "label": lbl} for k, lbl, _ in __import__("sipphone.xsi", fromlist=["x"]).SERVICE_TYPES]}

    # ------------------------------------------------------------------ writing
    def _validate(self, incoming: dict) -> Dict[str, Dict[str, str]]:
        """Return {section: {key: value}} of fields that changed. Raises SettingsError."""
        if not isinstance(incoming, dict):
            raise SettingsError("Bad request")
        current = self._values()
        cp = self._cp()
        changes: Dict[str, Dict[str, str]] = {}
        for sec, key, _, kind in FIELDS:
            sv = incoming.get(sec)
            if not isinstance(sv, dict) or key not in sv or sv[key] is None:
                continue
            v = sv[key]
            if kind == "bool":
                v = "true" if (v is True or str(v).strip().lower() in ("true", "1", "yes", "on")) else "false"
            v = str(v)
            if kind != "secret":
                v = v.strip()
            elif v == "":
                continue                          # blank password = keep the saved one
            name = _label(sec, key)
            if kind in ("port", "port0"):
                if not v.isdigit() or not ((0 if kind == "port0" else 1) <= int(v) <= 65535):
                    raise SettingsError(f"{name} must be a number from {0 if kind == 'port0' else 1} to 65535")
            elif kind == "float":
                try:
                    f = float(v)
                except ValueError:
                    raise SettingsError(f"{name} must be a number of seconds")
                if f < 2 or f > 600:
                    raise SettingsError(f"{name} must be between 2 and 600 seconds")
            elif (sec, key) == ("presence", "channels"):
                if v == "":
                    v = "0"
                if not v.isdigit() or int(v) > 8:
                    raise SettingsError(f"{name} must be a number from 0 (automatic) to 8")
                if int(v) == 0 and current[sec][key] in ("", "0"):
                    continue                       # blank and 0 both mean automatic: not a change
            prob = config_store.check_value(v)
            if prob:
                raise SettingsError(f"{name} {prob}")
            old = self._raw(cp, sec, key) if kind == "secret" else current[sec][key]
            if v != old:
                changes.setdefault(sec, {})[key] = v

        merged = {s: dict(kv) for s, kv in current.items()}
        for s, kv in changes.items():
            merged[s].update(kv)
        acct = merged["account"]
        sip_needed = merged["app"]["mode"] != "presence"
        if sip_needed and not acct["username"]:
            raise SettingsError("SIP Username is required")
        if sip_needed and not acct["domain"]:
            raise SettingsError("Domain is required")
        if acct["domain"] and (" " in acct["domain"] or "/" in acct["domain"]):
            raise SettingsError("Domain should be a host name like nextiva.com")
        for n in range(2, 9):
            ln = merged.get(f"line{n}", {})
            if ln.get("enabled", "true") != "true" or not ln.get("username"):
                continue
            if not (ln.get("domain") or acct["domain"]):
                raise SettingsError(f"Line {n}: Domain is required")
            if " " in ln.get("domain", "") or "/" in ln.get("domain", ""):
                raise SettingsError(f"Line {n}: Domain should be a host name like nextiva.com")
            if not (changes.get(f"line{n}", {}).get("password") or self._raw(cp, f"line{n}", "password")):
                raise SettingsError(f"Line {n}: enter the SIP password")
        feats = _features(os.path.dirname(os.path.abspath(self.path)), self._raw(cp, "tts", "model"))
        if changes.get("tts", {}).get("engine") == "piper" and not feats["piper"]:
            raise SettingsError("Piper isn't installed. Re-run the installer and answer Y to text-to-speech.")
        if changes.get("stt", {}).get("engine") == "whisper" and not feats["whisper"]:
            raise SettingsError("Whisper isn't installed. Re-run the installer and answer Y to speech-to-text.")
        if merged["app"]["mode"] not in APP_MODES:
            raise SettingsError("Mode must be phone or presence")
        pres = merged["presence"]
        if pres["mode"] not in PRESENCE_MODES:
            raise SettingsError("Presence source must be events, Xsi admin or off")
        if merged["app"]["mode"] == "presence" and not pres["mode"]:
            raise SettingsError("Team view only needs a presence source: User login (events) or Admin XSI login")
        if merged["app"]["mode"] == "presence" and pres["mode"] == "admin" and not merged["admin"]["username"]:
            raise SettingsError("Enter the Xsi admin login for the Team view")
        if pres["mode"] == "events" and not pres["xsi_user"]:
            raise SettingsError("Enter the user login for the Team view")
        if pres["mode"] == "events" and not (changes.get("presence", {}).get("xsi_password") or self._raw(cp, "presence", "xsi_password")):
            raise SettingsError("Enter the password for the user login")
        src = merged["update"]["source"]
        if src and not (src.startswith(("https://", "http://", "\\\\")) or os.path.isdir(src)):
            raise SettingsError("Update source must be a folder (or network share) that exists, or an https:// address")
        if "mode" in changes.get("presence", {}) and not pres["mode"]:
            changes["presence"]["mode"] = "off"            # explicit, so an [admin] login doesn't turn it back on
        adm = merged["admin"]
        if adm["xsi_host"] and not adm["xsi_host"].startswith(("https://", "http://")):
            changes.setdefault("admin", {})["xsi_host"] = "https://" + adm["xsi_host"]
        return changes

    def save(self, incoming: dict) -> dict:
        with self.lock:
            changes = self._validate(incoming)
            if not changes:
                return {"ok": True, "changed": [], "applied": [], "restart_required": self.restart_pending()}
            config_store.update(self.path, changes)
            self.setup_needed = self.needs_setup()
            changed = [f"{s}.{k}" for s, kv in changes.items() for k in kv]
            log.info("settings saved: %s", ", ".join(changed))   # names only, never values
            groups = {g for s, k, g, _ in FIELDS if k in changes.get(s, {})}
            applied = []
            from .cli import load_config
            loaded = load_config(self.path)
            cfg, _audio, admin = loaded
            loaded_app = loaded.app
            if "account" in groups:
                kw = {f: getattr(cfg, f) for f in ("username", "password", "domain", "auth_username",
                                                   "display_name", "proxy", "proxy_port")}
                only_name = set(changes.get("account", {})) == {"display_name"}
                if only_name:
                    getattr(self.phone, "primary", self.phone).cfg.display_name = cfg.display_name
                else:
                    if self.ui is not None and not self.phone.presence_only:
                        self.ui.reg_detail = "registering…"
                        self.ui.add("system", "Account settings saved — registering again")
                    threading.Thread(target=self._reconfigure, args=(kw,), daemon=True,
                                     name="sip-reconfigure").start()
                applied.append("account")
            if "line1" in groups and hasattr(self.phone, "lines"):
                self.phone.lines[1].label = changes["account"]["label"] or "Line 1"
                applied.append("line1")
            line_groups = sorted(int(g[4:]) for g in groups if g.startswith("line") and g[4:].isdigit() and g != "line1")
            if line_groups and hasattr(self.phone, "set_line"):
                from .lines import line_configs
                confs = line_configs(config_store.read(self.path), cfg)
                for n in line_groups:
                    c = confs.get(n)
                    try:
                        self.phone.set_line(n, c[0] if c else None, c[1] if c else "")
                    except Exception as e:
                        raise SettingsError(f"Line {n}: {e}")
                    if self.ui is not None:
                        self.ui.add("system", f"Line {n} {'saved — registering' if c else 'turned off'}")
                applied.append("lines")
            if "mode" in groups:
                new_mode = changes["app"]["mode"]
                self.phone.set_presence_only(new_mode == "presence")
                if self.ui is not None:
                    self.ui.mode = new_mode
                    self.ui.add("system", "Switched to Team view only" if new_mode == "presence" else "Switched to phone mode")
                if new_mode == "phone" and "account" not in groups:
                    if self.ui is not None:
                        self.ui.reg_detail = "registering…"
                    threading.Thread(target=self._reconfigure, args=({},), daemon=True, name="sip-reregister").start()
                applied.append("mode")
            if "ui" in groups:
                if self.ui is not None:
                    cp_now = self._cp()
                    flag = lambda k: self._raw(cp_now, "app", k).strip().lower() not in ("false", "no", "0", "off")
                    self.ui.show_team = flag("show_team")
                    self.ui.show_transcript = flag("show_transcript")
                applied.append("ui")
            if "startup" in groups:
                from .app import set_start_with_windows
                try:
                    set_start_with_windows(changes["app"]["start_with_windows"] == "true")
                    applied.append("startup")
                except Exception as e:
                    log.warning("start-with-windows: %s", e)
                    self.monitor_error = ""
                    raise SettingsError(f"Couldn't change 'start with Windows': {e}")
            if "update" in groups and self.on_update_opts:
                self.on_update_opts(loaded_app)
                applied.append("update")
            if "pickup" in groups and self.ui is not None:
                self.ui.pickup_code = admin["pickup_code"] or "*97"
                applied.append("pickup")
            if "bridge" in groups:
                try:
                    from .native_host import register_bridge
                    register_bridge()
                    applied.append("bridge")
                except Exception as e:
                    log.warning("bridge re-registration: %s", e)
            if "admin" in groups or ("mode" in groups and self.ui is not None and self.ui.monitor is None):
                self._rebuild_monitor(admin)
                applied.append("team")
            self.pending = self.restart_pending()
            return {"ok": True, "changed": changed, "applied": applied,
                    "restart_required": self.pending, "monitor_error": self.monitor_error}

    def _reconfigure(self, kw: dict) -> None:
        try:
            self.phone.reconfigure(**kw)
        except Exception as e:  # pragma: no cover
            log.exception("re-register failed")
            if self.ui is not None:
                self.ui.reg_detail = str(e)

    def _rebuild_monitor(self, admin: dict) -> None:
        old = getattr(self.ui, "monitor", None) if self.ui is not None else None
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        new = None
        self.monitor_error = ""
        from .cli import presence_mode
        if presence_mode(admin) and self.build_monitor:
            try:
                new = self.build_monitor(admin)
            except Exception as e:
                self.monitor_error = str(e)
                log.warning("Team view disabled: %s", e)
        if self.ui is not None:
            self.ui.monitor = new
            self.ui.add("system", (f"Team view updated ({'user login' if presence_mode(admin) == 'events' else 'Admin XSI login'})") if new else
                        ("Team view off" if not self.monitor_error else f"Team view error: {self.monitor_error}"))

    # ------------------------------------------------------------------ test
    def test_admin(self, incoming: dict) -> dict:
        """Try the login in the form (blank password = the saved one) and report
        what it can do, plus the live channel state if the Team view is running."""
        from .authentication import from_options, probe
        cp = self._cp()
        a = incoming.get("admin", incoming) if isinstance(incoming, dict) else {}
        p = incoming.get("presence", {}) if isinstance(incoming, dict) else {}
        mode = str(p.get("mode", self._raw(cp, "presence", "mode")) or "")
        g = lambda sec, key: self._raw(cp, sec, key)  # noqa: E731
        opts = {"host": str(a.get("xsi_host") or g("admin", "xsi_host")).strip(),
                "user_domain": g("admin", "user_domain") or "nextiva.com",
                "verify_tls": g("admin", "verify_tls").strip().lower() not in ("false", "no", "0"),
                "username": str(a.get("username") if a.get("username") is not None else g("admin", "username")).strip(),
                "password": str(a.get("password") or "") or g("admin", "password"),
                "directory_user": str(a.get("directory_user") if a.get("directory_user") is not None else g("admin", "directory_user")).strip(),
                "directory_type": g("admin", "directory_type"),
                "xsi_user": str(p.get("xsi_user") if p.get("xsi_user") is not None else g("presence", "xsi_user")).strip(),
                "xsi_password": str(p.get("xsi_password") or "") or g("presence", "xsi_password")}
        if mode not in ("events", "admin"):
            return {"ok": False, "message": "Choose a presence source first."}
        if mode == "admin" and not opts["host"]:
            return {"ok": False, "message": "Enter the XSI server and admin login first."}
        r = probe(from_options(mode, opts))
        notes, ok = [r.message], r.ok
        if not ok:
            return {"ok": False, "message": r.message}
        mon = getattr(self.ui, "monitor", None) if self.ui is not None else None
        snap = mon.snapshot() if mon is not None and getattr(mon, "snapshot", None) else None
        if snap and snap.get("source") == "events":
            if snap.get("connected"):
                errs = [u for u in snap["users"] if u["status"] == "error"]
                notes.append(f"Event channel connected; {snap['total'] - len(errs) - snap.get('refused', 0)} of {snap['total']} "
                             f"people monitored" + (f" ({errs[0]['detail']})." if errs else ".")
                             + (f" {snap['refused']} not monitored (no rights)." if snap.get("refused") else ""))
                ok = ok and len(errs) < max(1, snap["total"])
            else:
                notes.append("Event channel not connected" + (f": {snap['last_error']}" if snap.get("last_error") else " yet") + ".")
                ok = ok and not snap.get("last_error")
        else:
            notes.append("Save to open the event channel.")
        return {"ok": ok, "message": " ".join(notes)}
