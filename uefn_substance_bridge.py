"""Substance Painter to UEFN Live Bridge — UEFN Side.

Architecture:
  - Creates a PBR Parent Material per texture set (with real textures as defaults)
  - Creates Material Instances from parent (no recompile on sync)
  - Smart sync: only reimports textures that actually changed
  - HTTP listener + tkinter dashboard

Run: UEFN > Tools > Execute Python Script > this file

github.com/KiKoZl1 | Surprise Co. (surpriseugc.com)
"""

import unreal
import tkinter as tk
import io, json, math, os, queue, socket, sys, threading, time, traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, List, Optional, Set

# ============================================================
# CONFIG
# ============================================================

DEFAULT_PORT = 8780
MAX_PORT = 8785
TICK_BATCH = 5
HTTP_TIMEOUT = 120.0
POLL_INTERVAL = 0.02
STALE_SEC = 120.0

# ============================================================
# GLOBALS
# ============================================================

_http_server = None
_http_thread = None
_bound_port = 0
_command_queue = queue.Queue()
_responses = {}
_responses_lock = threading.Lock()
_req_counter = 0

_project_path = ""
_combine_meshes = True
_import_scale = 1.0

_synced = {}
_last_ping = 0.0
_last_sync = 0.0
_total_syncs = 0
_log_ring = []
_gui = None

# Channel detection
CHANNEL_MAP = {
    "basecolor": "BaseColor", "base_color": "BaseColor", "diffuse": "BaseColor",
    "albedo": "BaseColor", "normal": "Normal", "normalmap": "Normal",
    "normal_map": "Normal", "roughness": "Roughness", "metallic": "Metallic",
    "metalness": "Metallic", "height": "Height", "displacement": "Height",
    "ambientocclusion": "AO", "ambient_occlusion": "AO", "ao": "AO",
    "emissive": "Emissive", "emission": "Emissive",
    "opacity": "Opacity", "specular": "Specular",
}

# Material property for direct connection (used in master template)
MAT_PROP = {
    "BaseColor": "MP_BASE_COLOR", "Normal": "MP_NORMAL",
    "Roughness": "MP_ROUGHNESS", "Metallic": "MP_METALLIC",
    "Emissive": "MP_EMISSIVE_COLOR", "Opacity": "MP_OPACITY",
    "Specular": "MP_SPECULAR", "AO": "MP_AMBIENT_OCCLUSION",
}

# Display names for dashboard
CH_DISPLAY = {
    "BaseColor": "Base Color", "Normal": "Normal", "Roughness": "Roughness",
    "Metallic": "Metallic", "Height": "Height", "AO": "AO",
    "Emissive": "Emissive", "Opacity": "Opacity", "Specular": "Specular",
}
CH_COLORS = {
    "BaseColor": "#e07840", "Normal": "#7b7de0", "Roughness": "#5a9a5a",
    "Metallic": "#a8a8a8", "Height": "#8a7a5e", "AO": "#606060",
    "Emissive": "#d0d040", "Opacity": "#4090d0", "Specular": "#d04040",
}

# ============================================================
# HELPERS
# ============================================================

def _log(msg, level="info"):
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    _log_ring.append(entry)
    if len(_log_ring) > 300: _log_ring.pop(0)
    tag = "[SubstanceBridge]"
    if level == "error": unreal.log_error(f"{tag} {msg}")
    elif level == "warning": unreal.log_warning(f"{tag} {msg}")
    else: unreal.log(f"{tag} {msg}")
    if _gui: _gui._on_log(entry, level)

def _base_dir(name=""):
    base = _project_path.strip("/") if _project_path else "Game"
    return f"/{base}/Substance/{name}" if name else f"/{base}/Substance"

def _base_name(material_name):
    """Strip a leading 'M_' so generated asset names don't double up (M_M_...)."""
    return material_name[2:] if material_name.startswith("M_") else material_name

def _detect_project_root():
    """UEFN project content root = first path segment of the open map's package.
    UEFN has no /Game; each project's content lives under /<ProjectName>."""
    try:
        ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        w = ues.get_editor_world()
        pkg = w.get_outermost().get_name() if w else ""
        seg = pkg.strip("/").split("/")[0] if pkg else ""
        if seg:
            return "/" + seg
    except Exception:
        pass
    return ""

# ---- Brand fonts: register bundled OFL .ttf (process-private) so tkinter can use them ----
try:
    _FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
except Exception:
    _FONTS_DIR = ""

def _load_brand_fonts():
    try:
        import ctypes
        if not _FONTS_DIR or not os.path.isdir(_FONTS_DIR):
            return
        for fn in os.listdir(_FONTS_DIR):
            if fn.lower().endswith((".ttf", ".otf")):
                try:
                    ctypes.windll.gdi32.AddFontResourceExW(ctypes.c_wchar_p(os.path.join(_FONTS_DIR, fn)), 0x10, 0)
                except Exception:
                    pass
    except Exception:
        pass

_load_brand_fonts()

def _detect_channel(filename):
    n = os.path.splitext(filename)[0].lower()
    for key, ch in CHANNEL_MAP.items():
        if n.endswith(key) or n.endswith("_" + key): return ch
    return None

def _get_set_name(filename):
    n = os.path.splitext(filename)[0]
    nl = n.lower()
    for key in CHANNEL_MAP:
        if nl.endswith("_" + key): return n[:-(len(key) + 1)]
        if nl.endswith(key): return n[:-len(key)].rstrip("_")
    return n

def _short_set(full):
    parts = full.split("_")
    for i in range(len(parts)):
        c = "_".join(parts[i:])
        if c.lower().startswith("m_"): return c
    return full

def _import_tex(fp, dest, name):
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", fp)
    task.set_editor_property("destination_path", dest)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("replace_existing_settings", True)
    task.set_editor_property("automated", True)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    exp = f"{dest}/{name}"
    if unreal.EditorAssetLibrary.does_asset_exist(exp): return exp
    try:
        ps = task.get_editor_property("imported_object_paths")
        if ps and len(ps) > 0: return str(ps[0])
    except: pass
    return None

def _config_tex(path, ch):
    tex = unreal.EditorAssetLibrary.load_asset(path)
    if not tex: return
    try:
        if ch == "Normal":
            tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
            tex.set_editor_property("srgb", False)
        elif ch in ("Roughness","Metallic","AO","Height","Opacity","Specular"):
            tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
            tex.set_editor_property("srgb", False)
        elif ch in ("BaseColor","Emissive"):
            tex.set_editor_property("srgb", True)
        unreal.EditorAssetLibrary.save_asset(path)
    except: pass

def _asset_class(path):
    try:
        ad = unreal.EditorAssetLibrary.find_asset_data(path)
        return str(ad.asset_class_path.asset_name) if hasattr(ad, "asset_class_path") else str(getattr(ad, "asset_class", ""))
    except: return ""

def _serialize(obj):
    if obj is None or isinstance(obj, (bool, int, float, str)): return obj
    if isinstance(obj, (list, tuple)): return [_serialize(v) for v in obj]
    if isinstance(obj, dict): return {str(k): _serialize(v) for k, v in obj.items()}
    if hasattr(obj, "get_path_name"): return str(obj.get_path_name())
    try: return str(obj)
    except: return repr(obj)

# ============================================================
# PBR MASTER MATERIAL TEMPLATE
# ============================================================

def _create_parent_material(mat_name, dest, textures, mel):
    """Create a parent Material dynamically based on available channels.

    Only creates nodes for channels that actually have textures.
    No DefaultTexture conflicts — every node has a real texture.

    textures: {channel: asset_path}  (only channels the Painter project exported)
    Returns: material asset path
    """
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    mat = tools.create_asset(mat_name, dest, unreal.Material, unreal.MaterialFactoryNew())
    if not mat: return None
    mp = mat.get_path_name()

    has = lambda ch: ch in textures

    def tex_p(name, x, y, ch):
        tex = unreal.EditorAssetLibrary.load_asset(textures[ch])
        n = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, x, y)
        n.set_editor_property("parameter_name", name)
        n.set_editor_property("texture", tex)
        return n

    def scalar_p(name, val, x, y):
        n = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, x, y)
        n.set_editor_property("parameter_name", name)
        n.set_editor_property("default_value", val)
        return n

    def vec_p(name, r, g, b, a, x, y):
        n = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, x, y)
        n.set_editor_property("parameter_name", name)
        n.set_editor_property("default_value", unreal.LinearColor(r, g, b, a))
        return n

    def mul_n(x, y):
        return mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, x, y)

    def lrp_n(x, y):
        return mel.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, x, y)

    y_pos = 0  # Track vertical position for clean layout

    # ---- BASE COLOR ----
    if has("BaseColor"):
        t = tex_p("T_BaseColor", -800, y_pos, "BaseColor")
        p_tint = vec_p("TintColor", 1,1,1,1, -600, y_pos + 50)
        m = mul_n(-400, y_pos)
        mel.connect_material_expressions(t, "RGB", m, "A")
        mel.connect_material_expressions(p_tint, "RGB", m, "B")
        mel.connect_material_property(m, "", unreal.MaterialProperty.MP_BASE_COLOR)
        y_pos += 250

    # ---- NORMAL ----
    if has("Normal"):
        t = tex_p("T_Normal", -800, y_pos, "Normal")
        mel.connect_material_property(t, "RGB", unreal.MaterialProperty.MP_NORMAL)
        y_pos += 250

    # ---- ROUGHNESS ----
    if has("Roughness"):
        t = tex_p("T_Roughness", -800, y_pos, "Roughness")
        p_min = scalar_p("RoughnessMin", 0.0, -600, y_pos + 50)
        p_max = scalar_p("RoughnessMax", 1.0, -600, y_pos + 100)
        l = lrp_n(-400, y_pos)
        mel.connect_material_expressions(p_min, "", l, "A")
        mel.connect_material_expressions(p_max, "", l, "B")
        mel.connect_material_expressions(t, "R", l, "Alpha")
        mel.connect_material_property(l, "", unreal.MaterialProperty.MP_ROUGHNESS)
        y_pos += 250

    # ---- METALLIC ----
    if has("Metallic"):
        t = tex_p("T_Metallic", -800, y_pos, "Metallic")
        p_min = scalar_p("MetallicMin", 0.0, -600, y_pos + 50)
        p_max = scalar_p("MetallicMax", 1.0, -600, y_pos + 100)
        l = lrp_n(-400, y_pos)
        mel.connect_material_expressions(p_min, "", l, "A")
        mel.connect_material_expressions(p_max, "", l, "B")
        mel.connect_material_expressions(t, "R", l, "Alpha")
        mel.connect_material_property(l, "", unreal.MaterialProperty.MP_METALLIC)
        y_pos += 250

    # ---- EMISSIVE ----
    if has("Emissive"):
        t = tex_p("T_Emissive", -800, y_pos, "Emissive")
        p_int = scalar_p("EmissiveIntensity", 1.0, -600, y_pos + 50)
        m = mul_n(-400, y_pos)
        mel.connect_material_expressions(t, "RGB", m, "A")
        mel.connect_material_expressions(p_int, "", m, "B")
        mel.connect_material_property(m, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
        y_pos += 250

    # ---- AO ----
    if has("AO"):
        t = tex_p("T_AO", -800, y_pos, "AO")
        mel.connect_material_property(t, "R", unreal.MaterialProperty.MP_AMBIENT_OCCLUSION)
        y_pos += 250

    # ---- OPACITY ----
    if has("Opacity"):
        t = tex_p("T_Opacity", -800, y_pos, "Opacity")
        mel.connect_material_property(t, "R", unreal.MaterialProperty.MP_OPACITY)
        y_pos += 250

    # ---- SPECULAR ----
    if has("Specular"):
        t = tex_p("T_Specular", -800, y_pos, "Specular")
        mel.connect_material_property(t, "R", unreal.MaterialProperty.MP_SPECULAR)
        y_pos += 250

    # ---- HEIGHT (node only, no direct material slot in UEFN) ----
    if has("Height"):
        tex_p("T_Height", -800, y_pos, "Height")
        y_pos += 250

    # Compile and save
    try: mel.recompile_material(mat)
    except: pass
    unreal.EditorAssetLibrary.save_asset(mp)
    _log(f"    Parent created: {mat_name} ({len(textures)} ch)")
    return mp


def _create_mi(mi_name, dest, parent_path, textures):
    """Create Material Instance from parent and set texture overrides."""
    mi_path = f"{dest}/{mi_name}"

    if unreal.EditorAssetLibrary.does_asset_exist(mi_path):
        mi = unreal.EditorAssetLibrary.load_asset(mi_path)
        if mi:
            _set_mi_tex(mi, mi_path, textures)
            return mi_path
        unreal.EditorAssetLibrary.delete_asset(mi_path)

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    try:
        factory = unreal.MaterialInstanceConstantFactoryNew()
        mi = tools.create_asset(mi_name, dest, unreal.MaterialInstanceConstant, factory)
    except Exception:
        _log(f"    MI not available, using parent directly", "warning")
        return parent_path

    if not mi: return parent_path

    parent = unreal.EditorAssetLibrary.load_asset(parent_path)
    if parent: mi.set_editor_property("parent", parent)

    mi_path = mi.get_path_name()
    _set_mi_tex(mi, mi_path, textures)
    return mi_path


def _set_mi_tex(mi, mi_path, textures):
    """Set texture params on MI. No recompile."""
    mel = unreal.MaterialEditingLibrary
    pmap = {"BaseColor":"T_BaseColor", "Normal":"T_Normal", "Roughness":"T_Roughness",
            "Metallic":"T_Metallic", "AO":"T_AO", "Emissive":"T_Emissive",
            "Height":"T_Height", "Opacity":"T_Opacity", "Specular":"T_Specular"}
    for ch, tp in textures.items():
        p = pmap.get(ch)
        if not p: continue
        tex = unreal.EditorAssetLibrary.load_asset(tp)
        if not tex: continue
        try: mel.set_material_instance_texture_parameter_value(mi, p, tex)
        except: pass
    unreal.EditorAssetLibrary.save_asset(mi_path)


# ============================================================
# COMMANDS
# ============================================================

_HANDLERS = {}
def _reg(n):
    def d(fn): _HANDLERS[n] = fn; return fn
    return d

def _dispatch(cmd, par):
    h = _HANDLERS.get(cmd)
    if not h: raise ValueError(f"Unknown: {cmd}")
    return h(**par)

@_reg("ping")
def _ping():
    global _last_ping; _last_ping = time.time()
    return {"status": "ok", "port": _bound_port, "synced": list(_synced.keys())}

@_reg("get_log")
def _get_log(last_n=50): return {"lines": _log_ring[-last_n:]}

@_reg("execute_python")
def _exec_py(code=""):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdout, sys.stderr
    g = {"__builtins__": __builtins__, "unreal": unreal, "result": None}
    try: sys.stdout, sys.stderr = out, err; exec(code, g)
    except: traceback.print_exc(file=err)
    finally: sys.stdout, sys.stderr = old
    return {"result": _serialize(g.get("result")), "stdout": out.getvalue(), "stderr": err.getvalue()}


def _import_mesh_asset(mesh_path, dest, sm_name, combine=True, scale=1.0, skeletal=False):
    """Import an FBX as Static (skeletal reserved for v1.1) with NO embedded
    materials/textures (so there is no junk to clean up). Returns imported mesh asset paths."""
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", mesh_path)
    task.set_editor_property("destination_path", dest)
    task.set_editor_property("destination_name", sm_name)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("automated", True)
    task.set_editor_property("save", True)
    ui = unreal.FbxImportUI()
    ui.set_editor_property("import_mesh", True)
    ui.set_editor_property("import_materials", False)
    ui.set_editor_property("import_textures", False)
    ui.set_editor_property("import_as_skeletal", skeletal)
    ui.set_editor_property("mesh_type_to_import",
        unreal.FBXImportType.FBXIT_SKELETAL_MESH if skeletal else unreal.FBXImportType.FBXIT_STATIC_MESH)
    try:
        smd = ui.get_editor_property("skeletal_mesh_import_data" if skeletal else "static_mesh_import_data")
        if not skeletal:
            smd.set_editor_property("combine_meshes", bool(combine))
        smd.set_editor_property("import_uniform_scale", float(scale))
    except Exception as e:
        _log(f"  mesh import options partial: {e}", "warning")
    task.set_editor_property("options", ui)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    out = []
    try:
        for p in (task.get_editor_property("imported_object_paths") or []):
            out.append(str(p))
    except Exception:
        pass
    exp = f"{dest}/{sm_name}"
    if not out and unreal.EditorAssetLibrary.does_asset_exist(exp):
        out.append(exp)
    return out


@_reg("substance_init")
def _init(material_name="", source_dir="", file_list=None, mesh_path="", **kw):
    global _last_sync, _total_syncs, _last_ping
    _last_ping = time.time()
    _log(f"INIT: {material_name}")

    base = _base_name(material_name)
    dest = _base_dir(material_name)

    # --- Check existing ---
    existing = []
    try:
        for a in unreal.EditorAssetLibrary.list_assets(dest, recursive=False):
            cls = _asset_class(str(a))
            if "MaterialInstance" in cls: existing.append(str(a))
    except: pass

    if existing:
        _log(f"  Already exists: {len(existing)} MI(s) — reconnecting")
        tex_sets = {}
        for a in unreal.EditorAssetLibrary.list_assets(dest, recursive=False):
            a = str(a)
            if "Texture" in _asset_class(a):
                fn = a.split("/")[-1]
                ch = _detect_channel(fn)
                sn = _get_set_name(fn[len(f"T_{base}_"):] if fn.startswith(f"T_{base}_") else fn)
                if ch:
                    if sn not in tex_sets: tex_sets[sn] = {}
                    tex_sets[sn][ch] = a
        _synced[material_name] = {
            "all_materials": {m.split("/")[-1].replace(f"MI_{base}_", "", 1): m for m in existing},
            "source_dir": source_dir, "dest": dest, "texture_sets": tex_sets,
            "base": base, "last_sync": time.time(), "sync_count": 0}
        _last_sync = time.time()
        return {"material_name": material_name, "reconnected": True, "materials": len(existing)}

    # --- Group files by texture set ---
    files = file_list or [f for f in os.listdir(source_dir)
             if f.lower().endswith((".png",".tga",".exr",".tiff",".tif",".jpg",".jpeg",".bmp"))]
    if not files: raise RuntimeError(f"No textures in '{source_dir}'")

    tex_sets = {}
    for f in files:
        ch = _detect_channel(f)
        if not ch: continue
        sn = _get_set_name(f)
        if sn not in tex_sets: tex_sets[sn] = {}
        tex_sets[sn][ch] = f

    _log(f"  {len(tex_sets)} set(s): {[_short_set(s) for s in tex_sets]}")

    # --- Mesh: reuse if present, else import (non-destructive, no embedded junk) ---
    mesh_imported = False
    mesh_existed = False
    spawned_actors = []
    sm_name = f"SM_{base}"
    sm_path = f"{dest}/{sm_name}"

    if mesh_path and os.path.isfile(mesh_path):
        if unreal.EditorAssetLibrary.does_asset_exist(sm_path):
            mesh_existed = True
            _log(f"  Mesh {sm_name} already in UEFN — reusing (not replaced)")
        else:
            _log(f"  Importing mesh: {sm_name} (combine={_combine_meshes}, scale={_import_scale})")
            meshes = [m for m in _import_mesh_asset(mesh_path, dest, sm_name, _combine_meshes, _import_scale)
                      if "StaticMesh" in _asset_class(m)]
            _log(f"  {len(meshes)} mesh(es) imported")
            try:
                _ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
                loc, rot = _ues.get_level_viewport_camera_info()
                yr, pr = math.radians(rot.yaw), math.radians(rot.pitch)
                sp = unreal.Vector(loc.x+math.cos(pr)*math.cos(yr)*500,
                                   loc.y+math.cos(pr)*math.sin(yr)*500,
                                   loc.z+math.sin(pr)*500)
            except Exception:
                sp = unreal.Vector(0, 0, 100)
            asub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
            for mp in meshes:
                ma = unreal.EditorAssetLibrary.load_asset(mp)
                if not ma: continue
                ac = None
                try: ac = asub.spawn_actor_from_object(ma, sp)
                except Exception: pass
                if not ac:
                    try:
                        ac = asub.spawn_actor_from_class(unreal.StaticMeshActor, sp)
                        if ac: ac.static_mesh_component.set_static_mesh(ma)
                    except Exception: pass
                if ac:
                    ac.set_actor_label(f"{base}_{mp.split('/')[-1]}")
                    spawned_actors.append(ac); mesh_imported = True
                    _log(f"  Spawned: {ac.get_actor_label()}")
    elif mesh_path:
        _log(f"  Mesh path not found on disk: {mesh_path}", "warning")

    # --- Import textures & create Parent Material + MI per set ---
    created_mats = {}
    all_channels = set()
    mel = unreal.MaterialEditingLibrary

    for sn, channels in tex_sets.items():
        short = _short_set(sn)
        _log(f"  --- Set: {short} ({len(channels)} ch) ---")

        tex_paths = {}
        for ch, fname in channels.items():
            fp = os.path.join(source_dir, fname)
            if not os.path.isfile(fp): continue
            aname = f"T_{base}_{os.path.splitext(fname)[0]}".replace(" ","_").replace("-","_")
            ap = _import_tex(fp, dest, aname)
            if ap:
                _config_tex(ap, ch); tex_paths[ch] = ap
                all_channels.add(ch); _log(f"    {fname} -> {ch}")

        if not tex_paths: continue

        # Create parent material with real textures (compiles once)
        parent_name = f"M_{base}_{short}".replace(" ","_").replace("-","_")
        parent_path = _create_parent_material(parent_name, dest, tex_paths, mel)
        if not parent_path: continue
        _log(f"    Parent: {parent_name}")

        # Create MI from parent (no recompile on future syncs)
        mi_name = f"MI_{base}_{short}".replace(" ","_").replace("-","_")
        mi_path = _create_mi(mi_name, dest, parent_path, tex_paths)
        if mi_path: created_mats[short] = mi_path
        _log(f"    MI: {mi_name}")

    # --- Apply MIs to mesh slots ---
    if spawned_actors and created_mats:
        _log(f"  Applying {len(created_mats)} MI(s) to {len(spawned_actors)} actor(s)")
        _log(f"  MI keys: {list(created_mats.keys())}")

        for actor in spawned_actors:
            try:
                comp = actor.static_mesh_component
                if not comp: continue
                label = actor.get_actor_label().lower()
                n_slots = comp.get_num_materials()
                _log(f"  Actor '{actor.get_actor_label()}': {n_slots} slot(s)")

                for si in range(n_slots):
                    # Try to get slot name from static mesh
                    sname = ""
                    try:
                        sm = comp.get_editor_property("static_mesh")
                        if sm:
                            sml = sm.get_editor_property("static_materials")
                            if si < len(sml):
                                sname = str(sml[si].get_editor_property("material_slot_name")).lower()
                    except: pass

                    # Fallback: get from current material
                    if not sname:
                        try:
                            m = comp.get_material(si)
                            sname = m.get_name().lower() if m else ""
                        except: pass

                    _log(f"    Slot {si} name: '{sname}'")

                    # Match MI by slot name, actor label, or index
                    best, bs = None, 0
                    for k, v in created_mats.items():
                        kl = k.lower()
                        sc = 0
                        # Exact or contains match on slot name
                        if sname and sname == kl: sc = 200
                        elif sname and sname in kl: sc = 100
                        elif sname and kl in sname: sc = 90
                        # Part matching on slot name
                        elif sname:
                            for p in sname.split("_"):
                                if len(p) > 2 and p in kl: sc += 30
                        # Actor label matching (e.g. "bottle_low" contains "bottle")
                        for p in label.replace(".", "_").split("_"):
                            if len(p) > 2 and p in kl: sc += 15
                        if sc > bs:
                            bs, best = sc, v
                            _log(f"      Match: '{k}' score={sc}")

                    # Fallback: by index
                    if not best:
                        vl = list(created_mats.values())
                        if si < len(vl): best = vl[si]
                        _log(f"      Fallback: index {si}")

                    if best:
                        ma = unreal.EditorAssetLibrary.load_asset(best)
                        if ma:
                            comp.set_material(si, ma)
                            _log(f"    Slot {si}: applied {best.split('/')[-1]}")
                        else:
                            _log(f"    Slot {si}: FAILED to load {best}", "error")

            except Exception as e:
                _log(f"  Apply error: {e}", "warning")

    # --- Save state ---
    _synced[material_name] = {
        "all_materials": created_mats, "source_dir": source_dir,
        "dest": dest, "texture_sets": tex_sets, "base": base, "has_mi": True,
        "last_sync": time.time(), "sync_count": 1}
    _last_sync = time.time(); _total_syncs += 1
    _log(f"INIT COMPLETE: {len(created_mats)} MI(s), {sum(len(c) for c in tex_sets.values())} tex")
    return {"material_name": material_name, "materials": len(created_mats),
            "textures": sum(len(c) for c in tex_sets.values()),
            "channels": list(all_channels),
            "mesh_imported": mesh_imported, "mesh_existed": mesh_existed}


@_reg("substance_sync")
def _sync_cmd(material_name="", changed_files=None, source_dir=""):
    """Smart sync: only reimport changed textures, then update MI parameters."""
    global _last_sync, _total_syncs, _last_ping
    _last_ping = time.time()
    state = _synced.get(material_name)
    if not state: raise RuntimeError(f"'{material_name}' not init")

    src = source_dir or state["source_dir"]
    dest = state["dest"]
    base = state.get("base") or _base_name(material_name)

    # If changed_files specified, only process those
    if changed_files:
        files = changed_files
    else:
        files = [f for f in os.listdir(src)
                 if f.lower().endswith((".png",".tga",".exr",".tiff",".tif",".jpg",".jpeg",".bmp"))]

    _log(f"SYNC: {material_name} ({len(files)} files)")
    reimported = 0

    # Group changed files by texture set
    changed_sets = {}  # set_name -> {channel: filename}
    for fname in files:
        ch = _detect_channel(fname)
        if not ch: continue
        sn = _get_set_name(fname)
        if sn not in changed_sets: changed_sets[sn] = {}
        changed_sets[sn][ch] = fname

    # Reimport only changed textures
    for sn, channels in changed_sets.items():
        for ch, fname in channels.items():
            fp = os.path.join(src, fname)
            if not os.path.isfile(fp): continue
            aname = f"T_{base}_{os.path.splitext(fname)[0]}".replace(" ","_").replace("-","_")
            ap = _import_tex(fp, dest, aname)
            if ap:
                _config_tex(ap, ch); reimported += 1

    # Update Material Instances (just set texture params — no recompile!)
    mel = unreal.MaterialEditingLibrary
    for short, mi_path in state.get("all_materials", {}).items():
        mi = unreal.EditorAssetLibrary.load_asset(mi_path)
        if not mi: continue

        # Find which texture set this MI corresponds to
        for sn, channels in changed_sets.items():
            if _short_set(sn).lower() == short.lower():
                # Rebuild tex_paths for this set
                tex_paths = {}
                for ch, fname in channels.items():
                    aname = f"T_{base}_{os.path.splitext(fname)[0]}".replace(" ","_").replace("-","_")
                    exp = f"{dest}/{aname}"
                    if unreal.EditorAssetLibrary.does_asset_exist(exp):
                        tex_paths[ch] = exp
                if tex_paths:
                    _set_mi_tex(mi, mi_path, tex_paths)
                    _log(f"  Updated MI: {short}")
                break

    state["last_sync"] = time.time()
    state["sync_count"] = state.get("sync_count", 0) + 1
    _last_sync = time.time(); _total_syncs += 1
    _log(f"SYNC COMPLETE: {reimported} reimported (MI update, no recompile)")
    return {"material_name": material_name, "reimported": reimported}


@_reg("substance_status")
def _status_cmd():
    m = {}
    for n, s in _synced.items():
        chs = set()
        for cs in s.get("texture_sets", {}).values(): chs.update(cs.keys())
        m[n] = {"materials": len(s.get("all_materials", {})),
                "channels": list(chs), "syncs": s.get("sync_count", 0)}
    return {"count": len(_synced), "materials": m}

@_reg("substance_disconnect")
def _disc_cmd(material_name="", delete_assets=False):
    state = _synced.pop(material_name, None)
    if not state: return {"success": False}
    if delete_assets:
        try:
            for a in unreal.EditorAssetLibrary.list_assets(state.get("dest",""), recursive=False):
                try: unreal.EditorAssetLibrary.delete_asset(str(a))
                except: pass
        except: pass
    _log(f"Disconnected: {material_name}")
    return {"success": True}

# ============================================================
# HTTP SERVER
# ============================================================

class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        global _last_ping; _last_ping = time.time()
        b = json.dumps({"status":"ok","port":_bound_port}).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b)))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        global _req_counter, _last_ping
        L = int(self.headers.get("Content-Length",0))
        raw = self.rfile.read(L)
        try: body = json.loads(raw)
        except: self._e(400,"Bad JSON"); return
        cmd = body.get("command",""); par = body.get("params",{})
        if not cmd: self._e(400,"No cmd"); return
        if cmd == "ping":
            _last_ping = time.time()
            b = json.dumps({"success":True,"result":{"status":"ok","port":_bound_port,
                 "synced":list(_synced.keys())}}).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(b)))
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers(); self.wfile.write(b); return
        _req_counter += 1
        rid = f"r_{_req_counter}_{time.time_ns()}"
        _command_queue.put((rid, cmd, par))
        dl = time.time() + HTTP_TIMEOUT
        while time.time() < dl:
            with _responses_lock:
                if rid in _responses: res = _responses.pop(rid); break
            time.sleep(POLL_INTERVAL)
        else: self._e(504,"timeout"); return
        b = json.dumps(res).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b)))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers(); self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(200)
        for h,v in [("Access-Control-Allow-Origin","*"),("Access-Control-Allow-Methods","GET,POST,OPTIONS"),
                     ("Access-Control-Allow-Headers","Content-Type")]:
            self.send_header(h,v)
        self.end_headers()

    def _e(self, c, m):
        b = json.dumps({"success":False,"error":m}).encode()
        self.send_response(c)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def log_message(self, *a): pass

def _start_http():
    global _http_server, _http_thread, _bound_port
    if _http_server: return _bound_port
    for p in range(DEFAULT_PORT, MAX_PORT+1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1",p)); s.close()
            _http_server = HTTPServer(("127.0.0.1",p), _H)
            _bound_port = p
            _http_thread = threading.Thread(target=_http_server.serve_forever, daemon=True)
            _http_thread.start(); _log(f"HTTP on :{p}"); return p
        except OSError: continue
    raise RuntimeError("No free port")

def _stop_http():
    global _http_server, _http_thread, _bound_port
    if not _http_server: return
    _http_server.shutdown()
    if _http_thread: _http_thread.join(timeout=3)
    _http_server = None; _http_thread = None; _bound_port = 0

# ============================================================
# GUI
# ============================================================

class Dashboard:
    def __init__(self, port):
        self.port = port
        self.root = tk.Tk()
        self.root.title(f"Substance Bridge :{port}")
        self.root.geometry("460x740")
        self.root.minsize(420, 620)
        self.root.configure(bg="#0d1117")
        self.root.attributes("-topmost", True)
        # Editorial Brutal palette (attr names kept for back-compat with _upd/_reb/_on_log)
        self.BG="#0d1117"; self.BG2="#12161c"; self.BG3="#1b212a"
        self.FG="#FFFFFF"; self.DIM="#8b949e"; self.ACC="#FFFF29"
        self.GRN="#FFFF29"; self.RED="#FF087A"; self.ORG="#8b949e"
        self.PRP="#FF087A"; self.BRD="#2a2f37"; self.GHOST="#484f58"
        from tkinter import font as _tkfont
        fams = set(_tkfont.families())
        self.F_DISPLAY = next((f for f in ("Space Grotesk","Bahnschrift","Segoe UI Semibold","Segoe UI") if f in fams), "Segoe UI")
        self.F_BODY    = next((f for f in ("Inter","Segoe UI") if f in fams), "Segoe UI")
        self.F_MONO    = next((f for f in ("JetBrains Mono","Consolas") if f in fams), "Consolas")
        self.tick_h=None; self.fc=0; self._mh=""
        self._auto_path()
        self._build(); self._on_set(); self._start_tick()

    def _auto_path(self):
        global _project_path
        _project_path = _detect_project_root() or "/Game"

    def _div(self, color=None, thick=2, padx=18, pady=8):
        tk.Frame(self.root, bg=color or self.BRD, height=thick).pack(fill="x", padx=padx, pady=pady)

    def _build(self):
        bg=self.BG
        # --- Header ---
        h=tk.Frame(self.root,bg=bg); h.pack(fill="x",padx=18,pady=(14,0))
        tk.Label(h,text="SUBSTANCE",font=(self.F_DISPLAY,15,"bold"),fg=self.FG,bg=bg).pack(side="left")
        tk.Label(h,text=" ⇄ ",font=(self.F_DISPLAY,15,"bold"),fg=self.ACC,bg=bg).pack(side="left")
        tk.Label(h,text="UEFN",font=(self.F_DISPLAY,15,"bold"),fg=self.FG,bg=bg).pack(side="left")
        self.dot=tk.Label(h,text="■",font=(self.F_BODY,11),fg=self.DIM,bg=bg); self.dot.pack(side="right")
        self.clbl=tk.Label(h,text="WAITING",font=(self.F_MONO,8,"bold"),fg=self.DIM,bg=bg); self.clbl.pack(side="right",padx=(0,6))
        meta=tk.Frame(self.root,bg=bg); meta.pack(fill="x",padx=18,pady=(3,0))
        tk.Label(meta,text=f"BRIDGE :{self.port}   ·   {_project_path}",font=(self.F_MONO,8),fg=self.GHOST,bg=bg).pack(side="left")
        self._div(self.ACC,2,18,10)

        # --- Mesh import ---
        tk.Label(self.root,text="MESH IMPORT",font=(self.F_BODY,7,"bold"),fg=self.DIM,bg=bg).pack(anchor="w",padx=18)
        row=tk.Frame(self.root,bg=bg); row.pack(fill="x",padx=18,pady=(5,0))
        tg,_=self._toggle(row,"Combine Meshes",_combine_meshes,self._set_combine); tg.pack(side="left")
        tk.Label(row,text="SCALE",font=(self.F_BODY,7,"bold"),fg=self.DIM,bg=bg).pack(side="left",padx=(16,4))
        self.sv=tk.StringVar(value=str(_import_scale))
        se=tk.Entry(row,textvariable=self.sv,bg="#000000",fg=self.FG,insertbackground=self.ACC,
                    font=(self.F_MONO,9),relief="flat",width=5,highlightbackground=self.BRD,
                    highlightcolor=self.ACC,highlightthickness=2)
        se.pack(side="left",ipady=2); se.bind("<KeyRelease>",lambda e:self._on_set())
        self._div(None,1,18,8)

        # --- Stat cards ---
        cs=tk.Frame(self.root,bg=bg); cs.pack(fill="x",padx=18)
        r1=tk.Frame(cs,bg=bg); r1.pack(fill="x",pady=2)
        self._stat(r1,"LISTENER",f":{self.port}","left",self.ACC)
        self.sub_v=tk.StringVar(value="WAITING"); self._stat(r1,"SUBSTANCE",self.sub_v,"right",self.FG)
        r2=tk.Frame(cs,bg=bg); r2.pack(fill="x",pady=2)
        self.syn_v=tk.StringVar(value="—"); self._stat(r2,"LAST SYNC",self.syn_v,"left",self.FG)
        self.tot_v=tk.StringVar(value="0"); self._stat(r2,"TOTAL SYNCS",self.tot_v,"right",self.ACC)
        self._div(None,1,18,8)

        # --- Synced materials ---
        mh=tk.Frame(self.root,bg=bg); mh.pack(fill="x",padx=18)
        tk.Label(mh,text="SYNCED MATERIALS",font=(self.F_BODY,8,"bold"),fg=self.DIM,bg=bg).pack(side="left")
        self.mc=tk.Label(mh,text="0",font=(self.F_MONO,10,"bold"),fg=self.ACC,bg=bg); self.mc.pack(side="right")
        self.mf=tk.Frame(self.root,bg=bg); self.mf.pack(fill="x",padx=18,pady=4)
        self._wait()
        self._div(self.ACC,2,18,8)

        # --- Actions ---
        ab=tk.Frame(self.root,bg=bg); ab.pack(fill="x",padx=18,pady=(0,2))
        self._btn(ab,"FORCE SYNC",self._force,"primary")
        self._btn(ab,"APPLY",self._apply,"ghost")
        self._btn(ab,"DISCONNECT",self._dall,"danger")
        self._div(None,1,18,8)

        # --- Activity ---
        lh=tk.Frame(self.root,bg=bg); lh.pack(fill="x",padx=18)
        tk.Label(lh,text="ACTIVITY",font=(self.F_BODY,8,"bold"),fg=self.DIM,bg=bg).pack(side="left")
        cl=tk.Label(lh,text="CLEAR",font=(self.F_MONO,7,"bold"),fg=self.GHOST,bg=bg,cursor="hand2"); cl.pack(side="right")
        cl.bind("<Button-1>",lambda e:self._cl())
        self.log=tk.Text(self.root,height=6,bg="#000000",fg=self.DIM,font=(self.F_MONO,8),
                         relief="flat",state="disabled",highlightthickness=2,highlightbackground=self.BRD,
                         padx=8,pady=6)
        self.log.pack(fill="both",expand=True,padx=18,pady=(4,6))
        self.log.tag_configure("error",foreground=self.RED)
        self.log.tag_configure("sync",foreground=self.ACC)

        # --- Footer ---
        ft=tk.Frame(self.root,bg="#000000",height=26); ft.pack(fill="x",side="bottom"); ft.pack_propagate(False)
        tk.Label(ft,text="by KiKoZl • Surprise Co.",font=(self.F_BODY,7),fg=self.GHOST,bg="#000000").pack(side="left",padx=10)
        tk.Label(ft,text="github.com/KiKoZl1",font=(self.F_MONO,7),fg=self.GHOST,bg="#000000").pack(side="right",padx=10)

    def _stat(self,parent,label,value,side,accent):
        f=tk.Frame(parent,bg=self.BG2,highlightbackground=self.BRD,highlightcolor=self.BRD,highlightthickness=2)
        f.pack(side=side,expand=True,fill="x",padx=2,ipady=7)
        tk.Label(f,text=label,font=(self.F_BODY,7,"bold"),fg=self.DIM,bg=self.BG2).pack()
        if isinstance(value,str):
            tk.Label(f,text=value,font=(self.F_MONO,12,"bold"),fg=accent,bg=self.BG2).pack()
        else:
            tk.Label(f,textvariable=value,font=(self.F_MONO,12,"bold"),fg=accent,bg=self.BG2).pack()
    def _toggle(self,parent,text,initial,cmd):
        fr=tk.Frame(parent,bg=self.BG)
        cv=tk.Canvas(fr,width=40,height=20,bg=self.BG,highlightthickness=0,cursor="hand2"); cv.pack(side="left")
        lbl=tk.Label(fr,text=text.upper(),font=(self.F_BODY,8,"bold"),fg=self.FG,bg=self.BG); lbl.pack(side="left",padx=(8,0))
        st={"on":bool(initial)}
        def draw():
            cv.delete("all"); on=st["on"]
            cv.create_rectangle(2,3,38,17,fill=(self.ACC if on else "#1b212a"),outline=(self.ACC if on else self.BRD),width=2)
            if on: cv.create_rectangle(24,5,36,15,fill="#000000",outline="")
            else: cv.create_rectangle(4,5,16,15,fill=self.DIM,outline="")
        def tog(e=None): st["on"]=not st["on"]; draw(); cmd(st["on"])
        cv.bind("<Button-1>",tog); lbl.bind("<Button-1>",tog); draw()
        return fr,(lambda: st["on"])
    def _set_combine(self,on):
        global _combine_meshes; _combine_meshes=bool(on)
    def _btn(self,parent,text,cmd,kind="primary"):
        if kind=="primary": bg,fg,hbg,hfg=self.ACC,"#000000",self.RED,"#FFFFFF"
        elif kind=="danger": bg,fg,hbg,hfg=self.BG3,self.RED,self.RED,"#FFFFFF"
        else: bg,fg,hbg,hfg=self.BG3,self.FG,self.ACC,"#000000"
        b=tk.Label(parent,text=text,font=(self.F_DISPLAY,9,"bold"),bg=bg,fg=fg,cursor="hand2",padx=10,pady=8)
        b.pack(side="left",expand=True,fill="x",padx=2)
        b.bind("<Enter>",lambda e:b.configure(bg=hbg,fg=hfg))
        b.bind("<Leave>",lambda e:b.configure(bg=bg,fg=fg))
        b.bind("<Button-1>",lambda e:cmd())
    def _wait(self):
        for w in self.mf.winfo_children(): w.destroy()
        tk.Label(self.mf,text="WAITING FOR SUBSTANCE PAINTER\nClick CONNECT in the Painter panel",
                 fg=self.DIM,bg=self.BG,font=(self.F_BODY,8),justify="left").pack(pady=8,anchor="w")

    def _on_set(self):
        global _import_scale
        try: _import_scale=float(self.sv.get())
        except Exception: _import_scale=1.0
    def _on_log(self,e,l="info"):
        try:
            self.log.configure(state="normal")
            self.log.insert("end",e+"\n","error" if l=="error" else "sync" if "COMPLETE" in e else "")
            self.log.see("end"); self.log.configure(state="disabled")
        except: pass
    def _cl(self): self.log.configure(state="normal"); self.log.delete("1.0","end"); self.log.configure(state="disabled")

    def _force(self):
        for n,s in _synced.items():
            src=s["source_dir"]
            if not os.path.isdir(src): continue
            fs=[f for f in os.listdir(src) if f.lower().endswith((".png",".tga",".exr",".tiff",".tif",".jpg",".jpeg",".bmp"))]
            _command_queue.put((f"g_{time.time_ns()}","substance_sync",{"material_name":n,"changed_files":fs,"source_dir":src}))
            _log(f"Force sync: {n}")
    def _apply(self):
        if not _synced: _log("No materials","warning"); return
        actors=unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_selected_level_actors()
        if not actors: _log("No selection","warning"); return
        n=list(_synced.keys())[0]; mats=_synced[n].get("all_materials",{})
        if not mats: return
        fm=unreal.EditorAssetLibrary.load_asset(list(mats.values())[0])
        if not fm: return
        c=0
        for a in actors:
            try:
                comp=a.static_mesh_component
                if comp:
                    for i in range(comp.get_num_materials()): comp.set_material(i,fm)
                    c+=1
            except: pass
        _log(f"Applied to {c} actor(s)")
    def _dall(self):
        if _synced: _synced.clear(); _log("All disconnected")

    def _upd(self):
        now=time.time()
        if _last_ping>0 and now-_last_ping<10:
            self.dot.configure(fg=self.GRN); self.clbl.configure(text="CONNECTED",fg=self.GRN); self.sub_v.set("CONNECTED")
        elif _last_ping>0 and now-_last_ping<60:
            self.dot.configure(fg=self.ORG); self.clbl.configure(text="IDLE",fg=self.ORG); self.sub_v.set(f"IDLE {int(now-_last_ping)}s")
        elif _last_ping>0:
            self.dot.configure(fg=self.RED); self.clbl.configure(text="LOST",fg=self.RED); self.sub_v.set(f"LOST {int((now-_last_ping)/60)}m")
        else:
            self.dot.configure(fg=self.DIM); self.clbl.configure(text="WAITING",fg=self.DIM); self.sub_v.set("WAITING")
        if _last_sync>0:
            e=int(now-_last_sync); self.syn_v.set(f"{e}s ago" if e<60 else f"{e//60}m ago")
        self.tot_v.set(str(_total_syncs)); self.mc.configure(text=str(len(_synced)))
        h=str([(n,s.get("sync_count",0)) for n,s in _synced.items()])
        if h!=self._mh: self._mh=h; self._reb()

    def _reb(self):
        for w in self.mf.winfo_children(): w.destroy()
        if not _synced: self._wait(); return
        for name,state in _synced.items():
            card=tk.Frame(self.mf,bg=self.BG2,highlightbackground=self.BRD,highlightcolor=self.BRD,highlightthickness=2); card.pack(fill="x",pady=3)
            r1=tk.Frame(card,bg=self.BG2); r1.pack(fill="x",padx=8,pady=(7,2))
            tk.Label(r1,text="■",fg=self.ACC,bg=self.BG2,font=(self.F_BODY,9)).pack(side="left")
            tk.Label(r1,text=f" {name}",fg=self.FG,bg=self.BG2,font=(self.F_DISPLAY,9,"bold")).pack(side="left")
            sc=state.get("sync_count",0)
            tk.Label(r1,text=f"{sc} SYNC{'S' if sc!=1 else ''}",fg=self.DIM,bg=self.BG2,font=(self.F_MONO,7)).pack(side="right")
            r2=tk.Frame(card,bg=self.BG2); r2.pack(fill="x",padx=8,pady=(0,2))
            chs=set()
            for cs in state.get("texture_sets",{}).values(): chs.update(cs.keys())
            for ch in sorted(chs):
                tk.Label(r2,text=f" {ch.upper()} ",fg=self.FG,bg=self.BG3,font=(self.F_MONO,6,"bold"),padx=2).pack(side="left",padx=1,pady=1)
            r3=tk.Frame(card,bg=self.BG2); r3.pack(fill="x",padx=8,pady=(0,7))
            nm=len(state.get("all_materials",{}))
            tp="MI" if state.get("has_mi") else "MAT"
            tk.Label(r3,text=f"{nm} {tp} · PBR TEMPLATE",fg=self.DIM,bg=self.BG2,font=(self.F_MONO,7)).pack(side="left")

    def _start_tick(self):
        def tick(dt):
            try:
                if not self.root.winfo_exists(): self._stop(); return
                n=0
                while not _command_queue.empty() and n<TICK_BATCH:
                    try: rid,cmd,par=_command_queue.get_nowait()
                    except queue.Empty: break
                    try: res=_dispatch(cmd,par); resp={"success":True,"result":res}
                    except Exception as e:
                        _log(f"'{cmd}' failed: {e}","error")
                        resp={"success":False,"error":str(e),"traceback":traceback.format_exc()}
                    with _responses_lock: _responses[rid]=resp
                    n+=1
                now=time.time()
                with _responses_lock:
                    _stale=[]
                    for k in list(_responses):
                        try: _ts=float(k.split("_")[-1])/1e9
                        except Exception: _ts=now
                        if _ts<now-STALE_SEC: _stale.append(k)
                    for k in _stale: del _responses[k]
                self.fc+=1
                if self.fc%30==0: self._upd()
                self.root.update()
            except tk.TclError: self._stop()
        self.tick_h=unreal.register_slate_post_tick_callback(tick); _log("Dashboard ready")

    def _stop(self):
        if self.tick_h: unreal.unregister_slate_post_tick_callback(self.tick_h); self.tick_h=None
        _stop_http(); _log("Dashboard closed")

# ============================================================
port = _start_http()
_gui = Dashboard(port)
