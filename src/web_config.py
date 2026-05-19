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


# ---------------------------------------------------------------------------
# Schema introspection
# ---------------------------------------------------------------------------

# Human-friendly metadata for known keys. The web UI falls back to a sensible
# default if a key is missing here, so this is purely cosmetic.
_KEY_META = {
    ("settings", "display_mode"):
        {"label": "Display", "desc": "HUD overlay or raw camera preview",
         "display": {"hud": "HUD", "cam": "CAM"}, "order": 1},
    ("settings", "mirror_display"):
        {"label": "Mirror", "desc": "Horizontal flip for windshield reflection",
         "order": 2},
    ("settings", "npu_enabled"):
        {"label": "NPU Detection", "desc": "On-device speed-sign inference",
         "order": 3},
    ("settings", "night_mode"):
        {"label": "Night Mode", "desc": "ISP tuning for low-light driving",
         "order": 4},
    ("settings", "region"):
        {"label": "Region", "desc": "Country profile for speed limits / units",
         "display": {"au": "AU", "cn": "CN"}, "order": 5},
    ("settings", "brightness"):
        {"label": "Brightness", "desc": "Framebuffer brightness (10-100)",
         "order": 6},
    ("fusion", "npu_confidence_min"):
        {"label": "NPU Confidence (with DB)",
         "desc": "Min trust level when GPS database has data", "order": 1},
    ("fusion", "npu_confidence_no_db"):
        {"label": "NPU Confidence (no DB)",
         "desc": "Min trust level when GPS database is silent", "order": 2},
    ("fusion", "npu_vote_required"):
        {"label": "Confirm Count",
         "desc": "Consecutive detections required to commit", "order": 3},
    ("fusion", "npu_override_timeout"):
        {"label": "NPU Timeout",
         "desc": "Seconds until NPU override resets", "order": 4},
    ("fusion", "camera_alert_radius"):
        {"label": "Camera Alert Radius",
         "desc": "Distance in meters to warn about cameras", "order": 5},
}


def _build_schema():
    """Build JSON-serializable schema for the frontend from PARAM_DEFS."""
    sections = {}
    for (section, key), (typ, default, vmin, vmax) in PARAM_DEFS.items():
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
            "key": key,
            "kind": kind,
            "label": meta.get("label", key),
            "desc": meta.get("desc", ""),
            "default": default,
            "order": meta.get("order", 999),
        }
        if choices:
            item["choices"] = choices
            item["display"] = meta.get("display", {c: c.upper() for c in choices})
        if kind in ("int", "float"):
            item["min"] = vmin
            item["max"] = vmax
            item["step"] = 1 if kind == "int" else 0.05

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
<title>AI-HUD Config</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><circle cx='16' cy='16' r='15' fill='%230e1118'/><circle cx='16' cy='16' r='13' fill='none' stroke='%233787ff' stroke-width='2'/><text x='16' y='16' dy='0.32em' font-family='-apple-system,Segoe UI,Arial,sans-serif' font-size='13' font-weight='800' fill='%233787ff' text-anchor='middle'>AI</text></svg>">
<style>
  :root {
    --bg: #080a0f;
    --header: #0e1118;
    --row-a: #0c0f15;
    --row-b: #10131b;
    --sep: #1c202a;
    --white: #ebf0fc;
    --dim: #5058707c;
    --desc: #41485c;
    --accent: #3787ff;
    --green: #2dd26e;
    --red: #f5373c;
    --on: #28be64;
    --off: #323744;
    --knob: #e6ebf5;
    --danger: #641a1e;
    --danger-border: #a0282d;
    --btn: #1e2330;
    --btn-border: #2d3241;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--white);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 14px; }
  main { max-width: 720px; margin: 0 auto; padding: 16px 20px 80px; }
  header.bar { display: flex; align-items: center; justify-content: space-between;
    padding: 14px 4px 18px; border-bottom: 2px solid var(--accent); }
  header.bar h1 { margin: 0; font-size: 20px; letter-spacing: 0.5px; }
  header.bar .status { font-size: 12px; color: var(--dim); }
  header.bar .status.ok::before  { content: "\\25CF "; color: var(--green); }
  header.bar .status.bad::before { content: "\\25CF "; color: var(--red); }
  section.card { margin-top: 24px; }
  section.card h2 { font-size: 14px; color: var(--dim); margin: 0 0 8px;
    text-transform: uppercase; letter-spacing: 1px; }
  .row { display: flex; align-items: center; justify-content: space-between;
    padding: 14px 16px; border-bottom: 1px solid var(--sep); }
  .row:nth-child(odd)  { background: var(--row-a); }
  .row:nth-child(even) { background: var(--row-b); }
  .row .meta { flex: 1; min-width: 0; }
  .row .label { font-weight: 500; }
  .row .desc  { font-size: 12px; color: var(--desc); margin-top: 2px; }
  .row .control { flex-shrink: 0; margin-left: 16px; display: flex;
    align-items: center; gap: 8px; }
  /* iOS-style toggle */
  .toggle { position: relative; width: 48px; height: 26px; background: var(--off);
    border-radius: 13px; cursor: pointer; transition: background 0.15s; }
  .toggle.on { background: var(--on); }
  .toggle::after { content: ""; position: absolute; width: 20px; height: 20px;
    border-radius: 50%; background: var(--knob); top: 3px; left: 3px;
    transition: left 0.15s; }
  .toggle.on::after { left: 25px; }
  /* Choice buttons */
  .choice { display: flex; gap: 4px; }
  .choice button { padding: 6px 14px; background: var(--btn);
    border: 1px solid var(--btn-border); color: var(--dim); border-radius: 4px;
    cursor: pointer; font-size: 13px; font-family: inherit; }
  .choice button.active { background: var(--accent); border-color: var(--accent);
    color: white; }
  /* Number stepper */
  .stepper { display: flex; align-items: center; gap: 4px; }
  .stepper button { width: 32px; height: 32px; background: var(--btn);
    border: 1px solid var(--btn-border); color: var(--white); cursor: pointer;
    font-size: 16px; font-family: inherit; border-radius: 4px; }
  .stepper button.minus { color: var(--red); }
  .stepper button.plus  { color: var(--green); }
  .stepper .value { min-width: 64px; text-align: center; font-variant-numeric: tabular-nums; }
  /* Danger button */
  .danger-btn { width: 100%; padding: 12px; background: var(--danger);
    border: 1px solid var(--danger-border); color: var(--white); cursor: pointer;
    font-size: 14px; font-family: inherit; border-radius: 4px; margin-top: 8px; }
  /* Toast */
  #toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
    padding: 10px 18px; background: var(--btn); border: 1px solid var(--btn-border);
    border-radius: 4px; font-size: 13px; opacity: 0; transition: opacity 0.2s;
    pointer-events: none; }
  #toast.show { opacity: 1; }
  #toast.err { border-color: var(--red); color: var(--red); }
  footer { margin-top: 32px; padding: 12px 4px; font-size: 12px; color: var(--dim);
    border-top: 1px solid var(--sep); display: flex; justify-content: space-between; }
</style>
</head>
<body>
<main>
  <header class="bar">
    <h1>AI-HUD Settings</h1>
    <span id="conn" class="status">connecting...</span>
  </header>
  <section class="card">
    <h2>Display &amp; Detection</h2>
    <div id="settings-list"></div>
  </section>
  <section class="card">
    <h2>Fusion Parameters</h2>
    <div id="fusion-list"></div>
    <button class="danger-btn" id="reset-btn">Reset Fusion Defaults</button>
  </section>
  <footer>
    <span id="ver">v?</span>
    <span id="region">--</span>
  </footer>
</main>
<div id="toast"></div>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
let SCHEMA = null;
let STATE = null;
let pollTimer = null;
let pendingPost = 0;

function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  setTimeout(() => { t.className = ""; }, 1800);
}

function setConn(ok, label) {
  const c = $("conn");
  c.className = "status " + (ok ? "ok" : "bad");
  c.textContent = label || (ok ? "connected" : "disconnected");
}

async function fetchSchema() {
  // Static -- fetched once at boot.
  const r = await fetch("/api/schema", { cache: "no-store" });
  const data = await r.json();
  SCHEMA = data.schema;
}

let _stateSig = null;
async function fetchState() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    setConn(true, "live");
    // Skip DOM rebuild if nothing changed (prevents 2s polling flicker).
    const sig = JSON.stringify(data);
    if (sig === _stateSig) return;
    _stateSig = sig;
    STATE = data;
    render();
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
    STATE = data.state;
    _stateSig = JSON.stringify(STATE);
    render();
    toast("saved");
  } catch (e) {
    toast("save failed: " + e.message, true);
  } finally {
    pendingPost--;
  }
}

async function postReset() {
  if (!confirm("Reset all fusion parameters to defaults?")) return;
  pendingPost++;
  try {
    const r = await fetch("/api/action/reset", { method: "POST" });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "reset failed");
    STATE = data.state;
    _stateSig = JSON.stringify(STATE);
    render();
    toast("fusion reset to defaults");
  } catch (e) {
    toast("reset failed: " + e.message, true);
  } finally {
    pendingPost--;
  }
}

function makeToggle(section, item, value) {
  const t = document.createElement("div");
  t.className = "toggle" + (value ? " on" : "");
  t.onclick = () => postConfig(section, item.key, value ? 0 : 1);
  return t;
}

function makeChoice(section, item, value) {
  const wrap = document.createElement("div");
  wrap.className = "choice";
  for (const c of item.choices) {
    const btn = document.createElement("button");
    btn.textContent = (item.display && item.display[c]) || c.toUpperCase();
    if (c === value) btn.classList.add("active");
    btn.onclick = () => { if (c !== value) postConfig(section, item.key, c); };
    wrap.appendChild(btn);
  }
  return wrap;
}

function makeStepper(section, item, value) {
  const wrap = document.createElement("div");
  wrap.className = "stepper";
  const minus = document.createElement("button");
  minus.className = "minus";
  minus.textContent = "−";
  const valEl = document.createElement("span");
  valEl.className = "value";
  valEl.textContent = (item.kind === "float") ? value.toFixed(2) : value;
  const plus = document.createElement("button");
  plus.className = "plus";
  plus.textContent = "+";
  minus.onclick = () => {
    let v = Math.max(item.min, value - item.step);
    if (item.kind === "int") v = Math.round(v);
    else v = Math.round(v * 100) / 100;
    if (v !== value) postConfig(section, item.key, v);
  };
  plus.onclick = () => {
    let v = Math.min(item.max, value + item.step);
    if (item.kind === "int") v = Math.round(v);
    else v = Math.round(v * 100) / 100;
    if (v !== value) postConfig(section, item.key, v);
  };
  wrap.appendChild(minus);
  wrap.appendChild(valEl);
  wrap.appendChild(plus);
  return wrap;
}

function renderItem(section, item, value) {
  const row = document.createElement("div");
  row.className = "row";
  const meta = document.createElement("div");
  meta.className = "meta";
  const label = document.createElement("div");
  label.className = "label";
  label.textContent = item.label;
  meta.appendChild(label);
  if (item.desc) {
    const d = document.createElement("div");
    d.className = "desc";
    d.textContent = item.desc;
    meta.appendChild(d);
  }
  row.appendChild(meta);
  const ctrl = document.createElement("div");
  ctrl.className = "control";
  let widget = null;
  if (item.kind === "toggle")        widget = makeToggle(section, item, value);
  else if (item.kind === "choice")   widget = makeChoice(section, item, value);
  else if (item.kind === "int" || item.kind === "float")
                                     widget = makeStepper(section, item, value);
  if (widget) ctrl.appendChild(widget);
  row.appendChild(ctrl);
  return row;
}

function render() {
  if (!SCHEMA || !STATE) return;
  const sList = $("settings-list");
  const fList = $("fusion-list");
  sList.innerHTML = "";
  fList.innerHTML = "";
  for (const item of (SCHEMA.settings || [])) {
    sList.appendChild(renderItem("settings", item, STATE.settings[item.key]));
  }
  for (const item of (SCHEMA.fusion || [])) {
    fList.appendChild(renderItem("fusion", item, STATE.fusion[item.key]));
  }
  $("ver").textContent = "v" + (STATE.version || "?");
  $("region").textContent = (STATE.region || "--").toUpperCase()
    + " | NPU " + (STATE.npu_running ? "ON" : "OFF");
}

$("reset-btn").onclick = postReset;

// Fetch static schema once, then poll state every 2s for liveness.
fetchSchema().then(fetchState).catch(() => setConn(false, "device offline"));
pollTimer = setInterval(() => {
  if (pendingPost === 0) fetchState();
}, 2000);
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

        if self.path == "/api/action/reset":
            try:
                self.server_ctx.reset_fusion()
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
                return
            self._send_json({"ok": True, "state": self.server_ctx.snapshot()})
            return

        self._send_json({"ok": False, "error": "not found"}, status=404)


# ---------------------------------------------------------------------------
# Server orchestrator
# ---------------------------------------------------------------------------


# Map of (section, key) -> callback name in the callbacks dict.
# Fusion params share a single reload callback; settings keys are per-field.
_CALLBACK_MAP = {
    ("settings", "npu_enabled"):    "on_npu_toggle",
    ("settings", "night_mode"):     "on_night_mode_change",
    ("settings", "mirror_display"): "on_mirror_change",
    ("settings", "region"):         "on_region_change",
    ("settings", "display_mode"):   "on_display_mode_change",
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
                 port=DEFAULT_PORT, npu_state_fn=None, callbacks=None):
        self.config = config
        self.region_mgr = region_mgr
        self.app_version = app_version
        self.host = host
        self.port = port
        # Lets us reflect runtime NPU state (which may diverge from config
        # if the C side rejected the toggle). Optional.
        self._npu_state_fn = npu_state_fn or (
            lambda: bool(config.get_int("settings", "npu_enabled")))
        self._cb = callbacks or {}
        self._httpd = None
        self._thread = None

    # -- snapshot for /api/state -----------------------------------------
    # Schema is fetched separately via /api/schema -- keep this payload small
    # so the 2s polling loop stays cheap on the USB link.
    def snapshot(self):
        settings = {}
        fusion = {}
        for (section, key), (typ, _d, _vmin, _vmax) in PARAM_DEFS.items():
            if typ is int:
                v = self.config.get_int(section, key)
            elif typ is float:
                v = self.config.get_float(section, key)
            else:
                v = self.config.get_str(section, key)
            if section == "settings":
                settings[key] = v
            elif section == "fusion":
                fusion[key] = v
        return {
            "settings": settings,
            "fusion": fusion,
            "region": self.region_mgr.region,
            "version": self.app_version,
            "npu_running": bool(self._npu_state_fn()),
        }

    # -- apply config change ---------------------------------------------
    def apply(self, section, key, value):
        if (section, key) not in PARAM_DEFS:
            raise ValueError(f"unknown param: {section}.{key}")

        self.config.set(section, key, value)
        self.config.save()

        # Re-read what we actually stored (after clamp/coerce).
        typ = PARAM_DEFS[(section, key)][0]
        if typ is int:
            new_val = self.config.get_int(section, key)
        elif typ is float:
            new_val = self.config.get_float(section, key)
        else:
            new_val = self.config.get_str(section, key)

        # Dispatch to the matching callback (table-driven).
        if section == "fusion":
            cb = self._cb.get("on_fusion_reload")
            if cb:
                cb()
            return

        cb_name = _CALLBACK_MAP.get((section, key))
        if cb_name is None:
            return
        cb = self._cb.get(cb_name)
        if cb is None:
            return
        # int 0/1 params semantically represent booleans for these callbacks.
        if typ is int and PARAM_DEFS[(section, key)][2] == 0 \
                and PARAM_DEFS[(section, key)][3] == 1:
            cb(bool(new_val))
        else:
            cb(new_val)

    def reset_fusion(self):
        self.config.reset_section("fusion")
        self.config.save()
        cb = self._cb.get("on_fusion_reload")
        if cb:
            cb()

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
