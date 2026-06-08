"""Substance Painter -> UEFN Bridge — Painter Side.

Dockable panel: Connect / Sync / Live sync / Sync-on-save / Import-mesh.
- Auto-discovers the UEFN listener port (scans 8780-8785, reads the real bound port).
- Sync-on-save (default): syncs when you save the Substance project (Ctrl+S).
- Live sync (optional toggle): debounced sync as you paint (layer edits).
- Smart sync: only sends textures that actually changed (full-content hash).
- Exports the mesh straight from the Substance project (export_mesh) — the original
  .fbx/.obj is NOT required; the UEFN side reuses an existing mesh instead of replacing it.

Setup: copy this file (and the fonts/ folder) into Painter's  python/plugins  folder and
(re)start Painter. The plugin manager calls start_plugin() automatically.

by KiKoZl • Surprise Co. | github.com/KiKoZl1
"""

import hashlib, json, os, sys, tempfile, time
from urllib.request import Request, urlopen
from urllib.error import URLError

import substance_painter.event
import substance_painter.export
import substance_painter.project
import substance_painter.resource
import substance_painter.textureset
import substance_painter.ui

try:
    from PySide6 import QtWidgets, QtCore, QtGui
except Exception:
    from PySide2 import QtWidgets, QtCore, QtGui

# ============================================================
# CONFIG
# ============================================================

UEFN_HOST = "127.0.0.1"
PORT_MIN, PORT_MAX = 8780, 8785          # listener scans this range; we discover the bound one
EXPORT_FMT = "png"
EXPORT_BITS = "8"
EXPORT_PRESET = "PBR Metallic Roughness"
EXPORT_DIR = os.path.join(tempfile.gettempdir(), "substance_uefn_bridge")
IMPORT_MESH = True                       # default ON; mesh sent on first sync (UEFN skips if it already exists)

# ============================================================
# STATE
# ============================================================

_conn = None
_uefn_port = PORT_MIN
_syncing = False
_last_sync = 0.0
_sync_count = 0
_file_hashes = {}     # filename -> full-content hash (smart sync)

# UI / events
_dock = None
_panel = None
_log_sink = None
_subscribed = False
_debounce = None
_live_enabled = False   # Live sync (debounced on layer edits) — off by default
_save_enabled = True    # Sync on project save (Ctrl+S) — on by default

def _log(m):
    print(f"[Bridge] {m}")
    if _log_sink:
        try: _log_sink(m, "info")
        except Exception: pass

def _err(m):
    print(f"[Bridge ERROR] {m}", file=sys.stderr)
    if _log_sink:
        try: _log_sink(m, "error")
        except Exception: pass

# ============================================================
# HTTP  (port discovery, never hardcoded)
# ============================================================

def _url(port=None): return f"http://{UEFN_HOST}:{port or _uefn_port}"

def _probe(port):
    """GET liveness on `port`; return the listener's reported port if alive, else None."""
    try:
        with urlopen(Request(_url(port)), timeout=3.0) as r:
            data = json.loads(r.read().decode())
            if data.get("status") == "ok":
                return int(data.get("port", port))
    except Exception:
        return None
    return None

def _discover_port():
    """Find the UEFN listener across PORT_MIN..PORT_MAX. Sets _uefn_port and returns it (or None)."""
    global _uefn_port
    for p in range(PORT_MIN, PORT_MAX + 1):
        rp = _probe(p)
        if rp:
            _uefn_port = rp
            return rp
    return None

def _send(cmd, params=None, timeout=180.0):
    payload = json.dumps({"command": cmd, "params": params or {}}).encode()
    req = Request(_url(), data=payload, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
    except URLError as e:
        raise ConnectionError("UEFN not reachable") from e
    if not body.get("success", False):
        raise RuntimeError(f"UEFN: {body.get('error', '?')}")
    return body.get("result", {})

# ============================================================
# PROJECT INFO
# ============================================================

def _project_name():
    try:
        p = substance_painter.project.file_path()
        if p: return os.path.splitext(os.path.basename(p))[0]
    except Exception: pass
    try:
        m = substance_painter.project.last_imported_mesh_path()
        if m: return os.path.splitext(os.path.basename(m))[0]
    except Exception: pass
    return "SubstanceProject"

# ============================================================
# EXPORT (textures + mesh) + SMART DIFF
# ============================================================

def _resolve_preset_url(name=EXPORT_PRESET):
    """Resolve an export preset by name from the available presets; fallback to ResourceID."""
    try:
        for p in substance_painter.export.list_resource_export_presets():
            if p.resource_id.name == name:
                return p.resource_id.url()
        _log(f"Preset '{name}' not found in shelf; using default ResourceID")
    except Exception as e:
        _log(f"Preset lookup failed ({e}); using default ResourceID")
    return substance_painter.resource.ResourceID(context="starter_assets", name=name).url()

def _file_hash(path):
    """Full-content MD5 so ANY real edit is detected (no more 4KB false-negatives)."""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""

def _export_all(export_dir):
    if not substance_painter.project.is_open():
        raise RuntimeError("No project open")
    os.makedirs(export_dir, exist_ok=True)
    all_ts = substance_painter.textureset.all_texture_sets()
    elist = [{"rootPath": ts.name} for ts in all_ts]     # ts.name is a property in 11.x
    config = {
        "exportShaderParams": False,
        "exportPath": export_dir,
        "defaultExportPreset": _resolve_preset_url(),
        "exportList": elist,
        "exportParameters": [{"parameters": {
            "fileFormat": EXPORT_FMT, "bitDepth": EXPORT_BITS,
            "dithering": True, "paddingAlgorithm": "infinite"}}],
    }
    _log(f"Exporting {len(elist)} set(s)...")
    result = substance_painter.export.export_project_textures(config)
    status = getattr(result, "status", None)
    if status is not None and status != substance_painter.export.ExportStatus.Success:
        _log(f"Export status: {status} — {getattr(result, 'message', '')}")

    exported = []
    td = getattr(result, "textures", None)
    if td:
        for fl in td.values():
            for fp in fl: exported.append(os.path.basename(fp))
    else:
        exts = (".png", ".tga", ".exr", ".tiff", ".jpg", ".jpeg", ".bmp")
        exported = [f for f in os.listdir(export_dir) if f.lower().endswith(exts)]
    _log(f"Exported {len(exported)} texture(s)")
    return exported

def _export_mesh(export_dir):
    """Export the project's mesh to FBX (geometry comes from the .spp; no original file needed)."""
    try:
        opt = substance_painter.export.MeshExportOption.BaseMesh
        if not substance_painter.export.scene_is_triangulated():
            opt = substance_painter.export.MeshExportOption.TriangulatedMesh
        if substance_painter.export.scene_has_tessellation():
            opt = substance_painter.export.MeshExportOption.TessellationNormalsBaseMesh
        path = os.path.join(export_dir, "mesh.fbx")
        res = substance_painter.export.export_mesh(path, opt)
        if res.status == substance_painter.export.ExportStatus.Success and os.path.isfile(path):
            _log(f"Mesh exported from project ({opt})")
            return path
        _log(f"Mesh export failed: {getattr(res, 'message', '?')}")
    except Exception as e:
        _log(f"Mesh export error: {e}")
    return None

def _get_changed(export_dir, all_files):
    global _file_hashes
    changed = []
    for f in all_files:
        h = _file_hash(os.path.join(export_dir, f))
        if h != _file_hashes.get(f, ""):
            changed.append(f); _file_hashes[f] = h
    return changed

# ============================================================
# SYNC
# ============================================================

def _do_sync():
    global _syncing, _last_sync, _sync_count
    if not _conn or _syncing: return
    _syncing = True
    name = _conn["material_name"]; edir = _conn["export_dir"]; init = _conn.get("initialized", False)
    try:
        exported = _export_all(edir)
        if not exported: _log("Nothing exported"); return

        if not init:
            _log("First sync — creating in UEFN...")
            params = {"material_name": name, "source_dir": edir, "file_list": exported}
            if IMPORT_MESH:
                mp = _export_mesh(edir)
                if mp: params["mesh_path"] = mp
                else: _log("Mesh unavailable — syncing materials only")
            else:
                _log("Import mesh OFF — materials only")

            r = _send("substance_init", params)
            _conn["initialized"] = True
            for f in exported: _file_hashes[f] = _file_hash(os.path.join(edir, f))

            if r.get("reconnected"):
                _log(f"Reconnected: {name} ({r.get('materials', 0)} MI) — existing assets reused")
            else:
                _log(f"Created: {r.get('materials', 0)} MI, {r.get('textures', 0)} tex")
                if r.get("mesh_imported"): _log("Mesh imported & spawned in UEFN")
                elif r.get("mesh_existed"): _log("Mesh already in UEFN — reused (not replaced)")
        else:
            changed = _get_changed(edir, exported)
            if not changed: _log("No changes detected, skipping sync"); return
            _log(f"Changed: {len(changed)}/{len(exported)} texture(s)")
            r = _send("substance_sync", {"material_name": name, "changed_files": changed, "source_dir": edir})
            _log(f"Reimported: {r.get('reimported', 0)} (MI update, no recompile)")

        _last_sync = time.time(); _sync_count += 1
        _log(f"Sync #{_sync_count} done")
    except ConnectionError as e:
        _err(str(e))
    except Exception as e:
        _err(f"Sync failed: {e}")
    finally:
        _syncing = False

# ============================================================
# PUBLIC API
# ============================================================

def connect(material_name=""):
    global _conn, _file_hashes
    if _conn: disconnect()
    _file_hashes = {}

    if not substance_painter.project.is_open():
        _err("No project open!"); return

    if not material_name: material_name = _project_name()
    if not material_name.startswith("M_"): material_name = f"M_{material_name}"
    material_name = material_name.replace(" ", "_").replace("-", "_")
    _log(f"Material: {material_name}")

    port = _discover_port()
    if not port:
        _err("Cannot reach UEFN!\n"
             "  1. Open the UEFN project\n"
             "  2. Tools > Execute Python Script > uefn_substance_bridge.py\n"
             "  3. Dashboard opens -> then Connect here")
        return
    _log(f"UEFN connected on :{port}")

    edir = os.path.join(EXPORT_DIR, material_name)
    os.makedirs(edir, exist_ok=True)
    _conn = {"material_name": material_name, "export_dir": edir, "initialized": False}

    _log("Initial sync...")
    _do_sync()
    _log("=" * 42)
    _log(f"CONNECTED: {material_name}")
    _log("=" * 42)

def disconnect():
    global _conn, _file_hashes
    if not _conn: return
    n = _conn["material_name"]; _conn = None; _file_hashes = {}
    _log(f"Disconnected: {n}")

def sync():
    if not _conn: _err("Not connected"); return
    try:
        substance_painter.project.execute_when_not_busy(_do_sync)
    except Exception as e:
        _err(f"Cannot sync now (Painter busy?): {e}")

def status():
    if not _conn: _log("Not connected"); return
    online = "ONLINE" if _discover_port() else "OFFLINE"
    _log(f"Material: {_conn['material_name']} · syncs {_sync_count} · UEFN {online}")

# ============================================================
# UI — Dock Panel (Editorial Brutal)
# ============================================================

_QSS = """
QWidget#spuefn { background:#0d1117; }
QLabel { color:#FFFFFF; font-family:"Inter","Segoe UI"; font-size:12px; }
QLabel#title { font-family:"Space Grotesk","Segoe UI"; font-size:17px; font-weight:700; color:#FFFFFF; }
QLabel#cap { color:#8b949e; font-family:"JetBrains Mono","Consolas"; font-size:10px; }
QLabel#sec { color:#8b949e; font-weight:700; font-size:9px; }
QLabel#status { font-family:"JetBrains Mono","Consolas"; font-size:10px; font-weight:700; color:#8b949e; }
QLabel#foot { color:#484f58; font-size:9px; }
QPushButton { border-radius:0; }
QPushButton#primary { background:#FFFF29; color:#000000; border:0; padding:9px 12px;
    font-family:"Space Grotesk","Segoe UI"; font-weight:700; }
QPushButton#primary:hover { background:#FF087A; color:#FFFFFF; }
QPushButton#ghost { background:#1b212a; color:#FFFFFF; border:2px solid #2a2f37; padding:8px 12px;
    font-family:"Space Grotesk","Segoe UI"; font-weight:700; }
QPushButton#ghost:hover { border-color:#FFFF29; color:#FFFF29; }
QPushButton#danger { background:#1b212a; color:#FF087A; border:2px solid #2a2f37; padding:9px 12px;
    font-family:"Space Grotesk","Segoe UI"; font-weight:700; }
QPushButton#danger:hover { background:#FF087A; color:#FFFFFF; border-color:#FF087A; }
QPushButton#toggle { background:#1b212a; color:#8b949e; border:2px solid #2a2f37; padding:7px 10px;
    font-family:"JetBrains Mono","Consolas"; font-weight:700; font-size:10px; }
QPushButton#toggle:checked { background:#FFFF29; color:#000000; border-color:#FFFF29; }
QTextEdit { background:#000000; color:#8b949e; border:2px solid #2a2f37;
    font-family:"JetBrains Mono","Consolas"; font-size:11px; }
QFrame#div { background:#FFFF29; max-height:2px; min-height:2px; }
QFrame#divthin { background:#2a2f37; max-height:1px; min-height:1px; }
"""

def _load_qt_fonts():
    try:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.lower().endswith((".ttf", ".otf")):
                    QtGui.QFontDatabase.addApplicationFont(os.path.join(d, fn))
    except Exception:
        pass

def _mk_panel():
    _load_qt_fonts()
    w = QtWidgets.QWidget(); w.setObjectName("spuefn"); w.setStyleSheet(_QSS)
    w.setWindowTitle("SP → UEFN"); w.setMinimumWidth(280)
    lay = QtWidgets.QVBoxLayout(w); lay.setContentsMargins(16, 14, 16, 10); lay.setSpacing(8)

    # Header
    hl = QtWidgets.QHBoxLayout()
    t1 = QtWidgets.QLabel("SP"); t1.setObjectName("title")
    ta = QtWidgets.QLabel(" ⇄ "); ta.setObjectName("title"); ta.setStyleSheet("color:#FFFF29;")
    t2 = QtWidgets.QLabel("UEFN"); t2.setObjectName("title")
    hl.addWidget(t1); hl.addWidget(ta); hl.addWidget(t2); hl.addStretch(1)
    w_status = QtWidgets.QLabel("■ WAITING"); w_status.setObjectName("status")
    hl.addWidget(w_status); lay.addLayout(hl)
    cap = QtWidgets.QLabel("paint → engine bridge"); cap.setObjectName("cap"); lay.addWidget(cap)
    dv = QtWidgets.QFrame(); dv.setObjectName("div"); lay.addWidget(dv)

    # Connection
    cl = QtWidgets.QHBoxLayout()
    b_con = QtWidgets.QPushButton("CONNECT"); b_con.setObjectName("primary")
    b_dis = QtWidgets.QPushButton("DISCONNECT"); b_dis.setObjectName("danger")
    cl.addWidget(b_con, 2); cl.addWidget(b_dis, 1); lay.addLayout(cl)

    # Sync
    lay.addWidget(_seclbl("SYNC"))
    b_sync = QtWidgets.QPushButton("SYNC NOW"); b_sync.setObjectName("ghost"); lay.addWidget(b_sync)
    tl = QtWidgets.QHBoxLayout()
    b_live = QtWidgets.QPushButton("LIVE: OFF"); b_live.setObjectName("toggle"); b_live.setCheckable(True)
    b_save = QtWidgets.QPushButton("ON SAVE: ON"); b_save.setObjectName("toggle"); b_save.setCheckable(True); b_save.setChecked(True)
    tl.addWidget(b_live); tl.addWidget(b_save); lay.addLayout(tl)

    # Mesh
    lay.addWidget(_seclbl("MESH"))
    b_mesh = QtWidgets.QPushButton("IMPORT MESH: ON"); b_mesh.setObjectName("toggle"); b_mesh.setCheckable(True); b_mesh.setChecked(True)
    lay.addWidget(b_mesh)

    dt = QtWidgets.QFrame(); dt.setObjectName("divthin"); lay.addWidget(dt)
    lay.addWidget(_seclbl("ACTIVITY"))
    log = QtWidgets.QTextEdit(); log.setReadOnly(True); log.setMinimumHeight(120); lay.addWidget(log, 1)

    foot = QtWidgets.QLabel("by KiKoZl • Surprise Co."); foot.setObjectName("foot"); lay.addWidget(foot)

    def set_status():
        if _conn and _conn.get("initialized"):
            w_status.setText("■ CONNECTED"); w_status.setStyleSheet("color:#FFFF29;")
        elif _conn:
            w_status.setText("■ CONNECTING"); w_status.setStyleSheet("color:#8b949e;")
        else:
            w_status.setText("■ WAITING"); w_status.setStyleSheet("color:#8b949e;")

    def on_live(c):
        global _live_enabled; _live_enabled = bool(c); b_live.setText(f"LIVE: {'ON' if c else 'OFF'}")
    def on_save(c):
        global _save_enabled; _save_enabled = bool(c); b_save.setText(f"ON SAVE: {'ON' if c else 'OFF'}")
    def on_mesh(c):
        global IMPORT_MESH; IMPORT_MESH = bool(c); b_mesh.setText(f"IMPORT MESH: {'ON' if c else 'OFF'}")

    b_con.clicked.connect(lambda: (connect(), set_status()))
    b_dis.clicked.connect(lambda: (disconnect(), set_status()))
    b_sync.clicked.connect(lambda: sync())
    b_live.toggled.connect(on_live); b_save.toggled.connect(on_save); b_mesh.toggled.connect(on_mesh)

    def sink(msg, level="info"):
        try:
            done = any(k in msg for k in ("done", "Created", "Reimported", "CONNECTED", "imported"))
            color = "#FF087A" if level == "error" else ("#FFFF29" if done else "#8b949e")
            log.append(f'<span style="color:{color}">{msg}</span>')
        except Exception:
            pass
    global _log_sink; _log_sink = sink

    timer = QtCore.QTimer(w); timer.setInterval(2000); timer.timeout.connect(set_status); timer.start()
    w._timer = timer
    set_status()
    return w

def _seclbl(text):
    l = QtWidgets.QLabel(text); l.setObjectName("sec"); return l

# ============================================================
# EVENT-DRIVEN SYNC (sync-on-save + optional live)
# ============================================================

def _schedule_sync():
    """Debounced sync trigger (coalesces bursts of events)."""
    global _debounce
    try:
        if _debounce is None:
            _debounce = QtCore.QTimer(); _debounce.setSingleShot(True)
            _debounce.timeout.connect(lambda: sync())
        _debounce.start(1200)
    except Exception:
        sync()

def _on_saved(evt):
    if _conn and _save_enabled: _schedule_sync()

def _on_paint(evt):
    # TextureStateEvent / layer changes fire as you paint -> debounced live sync
    if _conn and _live_enabled: _schedule_sync()

def _subscribe_events():
    global _subscribed
    if _subscribed: return
    ev = substance_painter.event
    def _sub(name, cb):
        cls = getattr(ev, name, None)
        if cls is None:
            _log(f"event {name} unavailable in this Painter version"); return
        try: ev.DISPATCHER.connect_strong(cls, cb)
        except Exception as e: _log(f"subscribe {name} failed: {e}")
    _sub("ProjectSaved", _on_saved)              # sync on Ctrl+S (save)
    _sub("TextureStateEvent", _on_paint)         # the real "as you paint" trigger (live)
    _sub("LayerStacksModelDataChanged", _on_paint)
    _subscribed = True

def _unsubscribe_events():
    global _subscribed
    ev = substance_painter.event
    def _unsub(name, cb):
        cls = getattr(ev, name, None)
        if cls is None: return
        try: ev.DISPATCHER.disconnect(cls, cb)
        except Exception: pass
    _unsub("ProjectSaved", _on_saved)
    _unsub("TextureStateEvent", _on_paint)
    _unsub("LayerStacksModelDataChanged", _on_paint)
    _subscribed = False

# ============================================================
# PLUGIN LIFECYCLE  (driven by Painter's plugin manager)
# ============================================================

def start_plugin():
    global _dock, _panel
    if _panel is not None: return
    _panel = _mk_panel()
    _dock = substance_painter.ui.add_dock_widget(_panel)
    _subscribe_events()
    _log("Panel ready")

def close_plugin():
    global _dock, _panel, _log_sink
    _unsubscribe_events()
    disconnect()
    if _dock is not None:
        try: substance_painter.ui.delete_ui_element(_dock)
        except Exception: pass
    _dock = None; _panel = None; _log_sink = None
