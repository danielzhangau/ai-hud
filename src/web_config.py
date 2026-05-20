"""Web-based configuration UI for ai-hud, served over USB via adb forward.

Replaces the GT911 touch settings overlay when the touchscreen is unavailable.

Architecture:
    [PC Browser] -- http -> [adb forward] -> [HUD device :8080]
        the device runs a tiny stdlib HTTP server in a daemon thread,
        serving a single page (HTML + CSS + JS all inlined) and a JSON API.

API:
    GET  /                  -> single-page HTML
    GET  /api/state         -> {"settings":{...},"fusion":{...},
                                "region":"cn","version":"1.0.0","npu_running":...}
    POST /api/config        -> body: {"section":"settings","key":"...","value":...}
                                  -> applies via callbacks + persists, returns new state
    POST /api/action/reset  -> reset fusion section to defaults, returns new state

Schema is derived from config_manager.PARAM_DEFS so adding a new parameter
requires zero frontend changes. Listens on 127.0.0.1 only -- access from PC
requires `adb forward tcp:8080 tcp:8080`.

No third-party dependencies (Python stdlib only).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config_manager import PARAM_DEFS, get_choices


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 80

# Launcher's updater.py drops a small JSON sidecar here whenever it
# finishes a GitHub release probe (regardless of whether the user
# accepted the update). Dashboard reads this for the "new version"
# banner; absent or malformed -> banner stays hidden.
_UPDATE_STATUS_PATH = "/tmp/ai_hud_update_status.json"


def _read_update_status():
    """Return the dict from /tmp/ai_hud_update_status.json or None.

    Schema written by updater.py:
        {
          "current":      "0.1.0",          # device's version at probe time
          "latest":       "0.2.0",          # GitHub tag_name (no 'v' prefix)
          "update_available": True,         # bool, derived from semver
          "checked_at":   1716234000,       # unix seconds, probe time
          "url":          "https://..."     # release page (optional)
        }
    """
    try:
        with open(_UPDATE_STATUS_PATH, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

# Human-friendly metadata for the small set of settings the dashboard
# actually surfaces. Everything else is either auto-managed at runtime
# (region, night_mode) or considered a developer-only knob hidden in
# /root/ai_hud.conf (fusion.*). The web UI only exposes one toggle: the
# install-time mirror flag.
_KEY_META = {
    ("settings", "mirror_display"):
        {"label": "Mirror display", "order": 1,
         "desc": "Horizontally flip the HUD output. Turn on if the screen "
                 "is being reflected off the windshield; off if mounted "
                 "directly in front of the driver."},
}

# Which settings the dashboard is allowed to write. Fusion and other
# parameters still live in PARAM_DEFS (for /root/ai_hud.conf hand-edits)
# but are not user-facing -- POST /api/config rejects them outright.
_USER_EDITABLE = {
    ("settings", "mirror_display"),
}


def _build_schema():
    """Schema for the few user-editable settings the dashboard exposes."""
    sections = {}
    for (section, key) in _USER_EDITABLE:
        if (section, key) not in PARAM_DEFS:
            continue
        typ, default, vmin, vmax = PARAM_DEFS[(section, key)]
        meta = _KEY_META.get((section, key), {})
        choices = list(get_choices(section, key))
        if typ is str:
            kind = "choice" if choices else "text"
        elif typ is int and vmin == 0 and vmax == 1:
            kind = "toggle"
        elif typ is int:
            kind = "int"
        elif typ is float:
            kind = "float"
        else:
            kind = "text"
        item = {
            "key": key, "kind": kind,
            "label": meta.get("label", key),
            "desc": meta.get("desc", ""),
            "default": default,
            "order": meta.get("order", 999),
        }
        if choices:
            item["choices"] = choices
        sections.setdefault(section, []).append(item)
    for items in sections.values():
        items.sort(key=lambda x: (x["order"], x["key"]))
    return sections


# Schema is static -- build + serialize once at import.
_SCHEMA = _build_schema()
_SCHEMA_JSON_BYTES = json.dumps({"schema": _SCHEMA}).encode("utf-8")


# ---------------------------------------------------------------------------
# Inline frontend (single page, no build step)
# ---------------------------------------------------------------------------

_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI-HUD Dashboard</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='15' fill='%230e1118'/><circle cx='16' cy='16' r='13' fill='none' stroke='%233787ff' stroke-width='2'/><text x='16' y='16' dy='0.32em' font-family='-apple-system,Segoe UI,Arial,sans-serif' font-size='13' font-weight='800' fill='%233787ff' text-anchor='middle'>AI</text></svg>">
<style>
  :root {
    --bg: #080a0f; --card: #0e1118; --sep: #1c202a;
    --white: #ebf0fc; --dim: #6c7390; --desc: #525a73;
    --accent: #3787ff; --green: #2dd26e; --red: #f5373c; --amber: #f5b342;
    --on: #28be64; --off: #323744; --knob: #e6ebf5;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--white);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px; -webkit-font-smoothing: antialiased; }
  main { max-width: 720px; margin: 0 auto; padding: 16px 20px 80px; }
  header.bar { display: flex; align-items: center; justify-content: space-between;
    padding: 14px 4px 18px; border-bottom: 2px solid var(--accent); }
  header.bar h1 { margin: 0; font-size: 20px; letter-spacing: 0.5px; }
  header.bar .status { font-size: 12px; color: var(--dim); }
  header.bar .status.ok::before  { content: "\\25CF "; color: var(--green); }
  header.bar .status.bad::before { content: "\\25CF "; color: var(--red); }
  section.card { margin-top: 24px; }
  section.card h2 { font-size: 12px; color: var(--dim); margin: 0 0 8px;
    text-transform: uppercase; letter-spacing: 1.5px; }
  .panel { background: var(--card); border-radius: 8px; padding: 4px 0;
    border: 1px solid var(--sep); overflow: hidden; }
  .stat { display: flex; align-items: center; justify-content: space-between;
    padding: 12px 18px; border-bottom: 1px solid var(--sep); }
  .stat:last-child { border-bottom: none; }
  .stat .label { color: var(--dim); font-size: 13px; }
  .stat .value { font-variant-numeric: tabular-nums; }
  .stat .value.big { font-size: 18px; font-weight: 600; }
  .stat .value.muted { color: var(--dim); }
  .stat .value.ok { color: var(--green); }
  .stat .value.warn { color: var(--amber); }
  .stat .value.bad { color: var(--red); }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    margin-right: 6px; vertical-align: middle; }
  .dot.ok  { background: var(--green); }
  .dot.bad { background: var(--red); }
  .setting { display: flex; align-items: center; justify-content: space-between;
    padding: 14px 18px; border-bottom: 1px solid var(--sep); }
  .setting:last-child { border-bottom: none; }
  .setting .meta { flex: 1; min-width: 0; margin-right: 14px; }
  .setting .meta .l { font-weight: 500; }
  .setting .meta .d { font-size: 12px; color: var(--desc); margin-top: 3px; line-height: 1.4; }
  .toggle { position: relative; width: 46px; height: 26px; background: var(--off);
    border-radius: 13px; cursor: pointer; transition: background 0.15s; flex-shrink: 0; }
  .toggle.on { background: var(--on); }
  .toggle::after { content: ""; position: absolute; width: 20px; height: 20px;
    border-radius: 50%; background: var(--knob); top: 3px; left: 3px;
    transition: left 0.15s; }
  .toggle.on::after { left: 23px; }
  #toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
    padding: 10px 18px; background: var(--card); border: 1px solid var(--sep);
    border-radius: 4px; font-size: 13px; opacity: 0; transition: opacity 0.2s;
    pointer-events: none; }
  #toast.show { opacity: 1; }
  #toast.err { border-color: var(--red); color: var(--red); }
  /* Update banner -- shown only when launcher saw a newer release on
     GitHub. Amber to read as "info / action needed", not red. */
  #update-banner { display: none; margin-top: 16px; padding: 12px 16px;
    background: rgba(245, 179, 66, 0.10); border: 1px solid var(--amber);
    border-radius: 6px; font-size: 13px; line-height: 1.5; }
  #update-banner.show { display: block; }
  #update-banner .title { font-weight: 600; color: var(--amber); margin-bottom: 4px; }
  #update-banner .body { color: var(--white); }
  #update-banner .hint { color: var(--dim); font-size: 12px; margin-top: 6px; }
  footer { margin-top: 32px; padding: 12px 4px; font-size: 11px; color: var(--dim);
    border-top: 1px solid var(--sep); display: flex; justify-content: space-between; }
</style>
</head>
<body>
<main>
  <header class="bar">
    <h1>AI-HUD Dashboard</h1>
    <span id="conn" class="status">connecting...</span>
  </header>

  <!-- Update banner: hidden by default, shown when /api/state reports
       status.update.update_available = true. Filled in by render(). -->
  <div id="update-banner">
    <div class="title">New version available</div>
    <div class="body" id="update-body">--</div>
    <div class="hint">To install: close this browser tab, then double-click the AI-HUD Config launcher again. It will offer the update on startup.</div>
  </div>

  <section class="card">
    <h2>Live status</h2>
    <div class="panel">
      <div class="stat"><span class="label">GPS</span><span class="value muted" id="s-gps">--</span></div>
      <div class="stat"><span class="label">Speed limit</span><span class="value big muted" id="s-limit">--</span></div>
      <div class="stat"><span class="label">NPU detection</span><span class="value muted" id="s-npu">--</span></div>
      <div class="stat"><span class="label">Day / night</span><span class="value muted" id="s-night">--</span></div>
      <div class="stat"><span class="label">Region</span><span class="value muted" id="s-region">--</span></div>
      <div class="stat"><span class="label">Speed DB</span><span class="value muted" id="s-db">--</span></div>
    </div>
  </section>

  <section class="card">
    <h2>Setup</h2>
    <div class="panel" id="setup-panel"></div>
  </section>

  <footer>
    <span id="ver">v?</span>
    <span>Tip: most settings are automatic -- nothing to configure.</span>
  </footer>
</main>
<div id="toast"></div>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
let SCHEMA = null, STATE = null, pendingPost = 0, _stateSig = null;

function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  setTimeout(() => { t.className = ""; }, 1800);
}
function setConn(ok, label) {
  $("conn").className = "status " + (ok ? "ok" : "bad");
  $("conn").textContent = label || (ok ? "connected" : "disconnected");
}

async function fetchSchema() {
  const r = await fetch("/api/schema", { cache: "no-store" });
  SCHEMA = (await r.json()).schema;
}
async function fetchState() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    setConn(true, "live");
    const sig = JSON.stringify(data);
    if (sig === _stateSig) return;
    _stateSig = sig; STATE = data; render();
  } catch (e) {
    setConn(false, "device offline");
  }
}
async function postConfig(section, key, value) {
  pendingPost++;
  try {
    const r = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ section, key, value }),
    });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "save failed");
    STATE = data.state; _stateSig = JSON.stringify(STATE);
    render(); toast("saved");
  } catch (e) {
    toast("save failed: " + e.message, true);
  } finally { pendingPost--; }
}

function renderStatus(st) {
  const gpsOk = st.gps_valid === true;
  $("s-gps").innerHTML =
    `<span class="dot ${gpsOk ? "ok" : "bad"}"></span>` +
    (gpsOk ? `${st.gps_sats ?? 0} sats` : "no fix") +
    (st.gps_age_s !== undefined ? `  ·  ${st.gps_age_s}s ago` : "");
  $("s-gps").className = "value";

  if (st.speed_limit) {
    $("s-limit").textContent = `${st.speed_limit} km/h  ·  ${st.speed_limit_source || "default"}`;
    $("s-limit").className = "value big";
  } else {
    $("s-limit").textContent = "--"; $("s-limit").className = "value big muted";
  }

  if (st.npu_running) {
    $("s-npu").innerHTML = `<span class="dot ok"></span>running` +
      (st.last_detection ? `  ·  last: ${st.last_detection}` : "");
    $("s-npu").className = "value ok";
  } else {
    $("s-npu").innerHTML = `<span class="dot bad"></span>idle`;
    $("s-npu").className = "value bad";
  }

  if (st.night_mode === true)       { $("s-night").textContent = "NIGHT (auto)"; $("s-night").className = "value warn"; }
  else if (st.night_mode === false) { $("s-night").textContent = "DAY (auto)";   $("s-night").className = "value ok"; }
  else                              { $("s-night").textContent = "--";           $("s-night").className = "value muted"; }

  $("s-region").textContent = (st.region || "--").toUpperCase() + " (auto)";
  $("s-region").className = "value";

  if (st.db_info) {
    const dateStr = st.db_built ? `  ·  built ${st.db_built}` : "";
    $("s-db").textContent = st.db_info + dateStr;
    $("s-db").className = "value";
  } else {
    $("s-db").textContent = "no database loaded";
    $("s-db").className = "value muted";
  }
}

function makeToggle(section, item, value) {
  const t = document.createElement("div");
  t.className = "toggle" + (value ? " on" : "");
  t.onclick = () => postConfig(section, item.key, value ? 0 : 1);
  return t;
}
function renderSettingItem(section, item, value) {
  const row = document.createElement("div");
  row.className = "setting";
  const meta = document.createElement("div"); meta.className = "meta";
  const l = document.createElement("div"); l.className = "l"; l.textContent = item.label; meta.appendChild(l);
  if (item.desc) { const d = document.createElement("div"); d.className = "d"; d.textContent = item.desc; meta.appendChild(d); }
  row.appendChild(meta);
  if (item.kind === "toggle") row.appendChild(makeToggle(section, item, value));
  return row;
}
function renderUpdateBanner(upd) {
  const b = $("update-banner");
  if (!upd || !upd.update_available) { b.classList.remove("show"); return; }
  const cur = upd.current || "?";
  const lat = upd.latest  || "?";
  $("update-body").textContent =
    "Device is on v" + cur + ", v" + lat + " is available on GitHub.";
  b.classList.add("show");
}
function render() {
  if (!STATE) return;
  renderStatus(STATE.status || {});
  renderUpdateBanner(STATE.status && STATE.status.update);
  $("ver").textContent = "v" + (STATE.status && STATE.status.version || "?");
  const setup = $("setup-panel"); setup.innerHTML = "";
  if (SCHEMA && SCHEMA.settings && SCHEMA.settings.length) {
    for (const item of SCHEMA.settings) {
      setup.appendChild(renderSettingItem("settings", item, STATE.settings[item.key]));
    }
  } else {
    setup.innerHTML = '<div class="stat"><span class="label">Everything is automatic.</span></div>';
  }
}

fetchSchema().then(fetchState).catch(() => setConn(false, "device offline"));
setInterval(() => { if (pendingPost === 0) fetchState(); }, 2000);
</script>
</body>
</html>
"""

# Pre-encoded once: the index page is static, encoding on every request wastes
# CPU and allocations on the device's ARM Cortex-A7.
_INDEX_HTML_BYTES = _INDEX_HTML.encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    # Class-level: populated by WebConfigServer.start_in_thread()
    server_ctx = None  # type: WebConfigServer

    # Silence default stderr access log (keeps HUD log clean).
    def log_message(self, format, *args):
        pass

    # -- response helper --------------------------------------------------
    def _send(self, body, content_type, status=200):
        """Write a single HTTP response. `body` must already be bytes."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status=200):
        self._send(json.dumps(payload).encode("utf-8"),
                   "application/json", status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(_INDEX_HTML_BYTES, "text/html; charset=utf-8")
            return
        if self.path == "/api/schema":
            self._send(_SCHEMA_JSON_BYTES, "application/json")
            return
        if self.path == "/api/state":
            self._send_json(self.server_ctx.snapshot())
            return
        self._send_json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self):
        if self.path == "/api/config":
            body = self._read_body()
            if not isinstance(body, dict):
                self._send_json({"ok": False, "error": "invalid json"}, 400)
                return
            section = body.get("section")
            key = body.get("key")
            value = body.get("value")
            if not section or not key:
                self._send_json({"ok": False, "error": "missing section/key"}, 400)
                return
            try:
                self.server_ctx.apply(section, key, value)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
                return
            self._send_json({"ok": True, "state": self.server_ctx.snapshot()})
            return

        # /api/action/reset removed -- fusion params are no longer surfaced
        # to end users, so there's nothing to reset from the dashboard.
        self._send_json({"ok": False, "error": "not found"}, status=404)


# ---------------------------------------------------------------------------
# Server orchestrator
# ---------------------------------------------------------------------------


# Dispatch table for user-editable settings. Anything else hitting
# /api/config is rejected. region/night_mode/display_mode/npu_enabled
# changed callbacks remain wired in hud_live.py for internal runtime
# updates -- they're just no longer reachable from the dashboard.
_CALLBACK_MAP = {
    ("settings", "mirror_display"): "on_mirror_change",
}


class WebConfigServer:
    """Owns the HTTP server thread and bridges API calls to the HUD core.

    Callbacks (all optional) are the same ones settings_ui.py uses, so we can
    drop this in without changing the HUD's main loop behavior:
        on_npu_toggle(bool)
        on_region_change(str)
        on_display_mode_change(str)
        on_mirror_change(bool)
        on_night_mode_change(bool)
        on_fusion_reload()
    """

    def __init__(self, config, region_mgr, app_version, host=DEFAULT_HOST,
                 port=DEFAULT_PORT, status_fn=None, callbacks=None):
        self.config = config
        self.region_mgr = region_mgr
        self.app_version = app_version
        self.host = host
        self.port = port
        # Optional callable returning a dict of runtime status fields.
        # hud_live.py injects one that reports live GPS / NPU / day-night
        # state -- the dashboard shows these as read-only telemetry.
        self._status_fn = status_fn or (lambda: {})
        self._cb = callbacks or {}
        self._httpd = None
        self._thread = None

    # -- snapshot for /api/state -----------------------------------------
    # Mostly a status payload (GPS, NPU, day/night, etc.) plus the small
    # set of user-editable settings. Schema is fetched separately via
    # /api/schema -- keep this payload small so the 2s poll stays cheap.
    def snapshot(self):
        settings = {}
        for (section, key) in _USER_EDITABLE:
            if (section, key) not in PARAM_DEFS:
                continue
            typ = PARAM_DEFS[(section, key)][0]
            if typ is int:
                settings[key] = self.config.get_int(section, key)
            elif typ is float:
                settings[key] = self.config.get_float(section, key)
            else:
                settings[key] = self.config.get_str(section, key)

        status = {
            "region": self.region_mgr.region,
            "version": self.app_version,
        }
        # Pull live telemetry from hud_live.py if it provided a status_fn.
        try:
            extra = self._status_fn() or {}
            status.update(extra)
        except Exception as e:
            status["status_error"] = str(e)
        # Update-status sidecar: written by the launcher's updater.py on
        # every connection. Tells the dashboard whether a newer release is
        # waiting and when the launcher last checked. Best-effort -- if the
        # file is missing or malformed the banner just stays hidden.
        upd = _read_update_status()
        if upd is not None:
            status["update"] = upd
        return {"settings": settings, "status": status}

    # -- apply config change ---------------------------------------------
    def apply(self, section, key, value):
        # The dashboard can only write the small whitelist of settings the
        # operator legitimately needs (install-time mirror toggle today).
        # Everything else lives in /root/ai_hud.conf as a developer knob.
        if (section, key) not in _USER_EDITABLE:
            raise ValueError(f"param not editable from dashboard: {section}.{key}")
        if (section, key) not in PARAM_DEFS:
            raise ValueError(f"unknown param: {section}.{key}")

        self.config.set(section, key, value)
        self.config.save()

        typ = PARAM_DEFS[(section, key)][0]
        if typ is int:
            new_val = self.config.get_int(section, key)
        elif typ is float:
            new_val = self.config.get_float(section, key)
        else:
            new_val = self.config.get_str(section, key)

        cb_name = _CALLBACK_MAP.get((section, key))
        if cb_name is None:
            return
        cb = self._cb.get(cb_name)
        if cb is None:
            return
        if typ is int and PARAM_DEFS[(section, key)][2] == 0 \
                and PARAM_DEFS[(section, key)][3] == 1:
            cb(bool(new_val))
        else:
            cb(new_val)

    # -- lifecycle -------------------------------------------------------
    def start_in_thread(self):
        """Bind socket and serve in a daemon thread. Returns True on success."""
        try:
            _Handler.server_ctx = self
            self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as e:
            print(f"[web] WARNING: cannot bind {self.host}:{self.port} ({e})")
            return False

        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="web-config",
            daemon=True,
        )
        self._thread.start()
        # When bound to a private USB-side IP (the normal case), advertise the
        # mDNS hostname so the PC can reach us without knowing the IP.
        if self.host == "0.0.0.0":
            url = "http://ai-hud.local/" if self.port == 80 \
                else f"http://ai-hud.local:{self.port}/"
        else:
            url = f"http://{self.host}:{self.port}/"
        print(f"[web] config UI at {url}")
        return True

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
