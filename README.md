# Substance Painter → UEFN Bridge

[![Release](https://img.shields.io/github/v/release/KiKoZl1/uefn-substance-bridge?color=FFFF29)](https://github.com/KiKoZl1/uefn-substance-bridge/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-FFFF29.svg)](LICENSE)
![Substance 3D Painter](https://img.shields.io/badge/Substance%203D%20Painter-11.x-FF087A)
![UEFN](https://img.shields.io/badge/UEFN-UE%205.8-white)

**Paint in Substance 3D Painter, see it in UEFN — instantly.**

https://github.com/user-attachments/assets/c635ccfb-d6d5-49f2-8062-f5e1a5124228

A two-process bridge that pushes your PBR textures *and* mesh from Adobe Substance 3D Painter
straight into Unreal Editor for Fortnite (UEFN): builds the material network, creates a Material
Instance, imports + spawns the mesh, and live-updates as you paint — no manual export/import loop.

> by **KiKoZl** · Surprise Co. · [github.com/KiKoZl1](https://github.com/KiKoZl1)

---

## Features

- **Sync-on-save** — hit `Ctrl+S` in Painter and your textures land in UEFN. (on by default)
- **Live sync** — optional toggle: debounced auto-sync as you paint.
- **Mesh transfer with no source file** — the mesh is exported straight from the `.spp`
  (`export_mesh`), so you don't need the original `.fbx`/`.obj` on disk.
- **Smart sync** — only the texture maps that actually changed are re-sent (full-content hash).
- **Auto material network** — a PBR parent `Material` + `Material Instance` per texture set, with
  correct sRGB / compression per channel; subsequent syncs update the MI **without recompiling**.
- **Idempotent & non-destructive** — reuses existing meshes/materials, never blanket-deletes your assets.
- **Zero manual paths** — the UEFN project content root is detected automatically.
- **Auto port discovery** — Painter finds the UEFN listener across `8780–8785` on its own.

## Requirements

- **Same machine** for both apps (textures/mesh are handed off via a local temp path).
- **Adobe Substance 3D Painter 11.x** (PySide6 / Python API ≥ 0.3.4). *Older versions may work.*
- **UEFN** with the *Python Editor Script Plugin* enabled.
- Windows (the bundled brand fonts are registered via the Win32 font API; the bridge still runs
  without them using system fallbacks).

## Install

> Full step-by-step guide: **[docs/INSTALL.md](docs/INSTALL.md)** · Usage walkthrough: **[docs/USAGE.md](docs/USAGE.md)**

**UEFN side**
1. Open your UEFN project.
2. `Tools ▸ Execute Python Script…` → select `uefn_substance_bridge.py`.
3. The **Substance Bridge** dashboard window appears (listening on `:8780`).

**Painter side**
1. Copy `substance_painter_bridge.py` **and the `fonts/` folder** into Painter's plugins folder:
   `Documents/Adobe/Adobe Substance 3D Painter/python/plugins/`
2. (Re)start Substance 3D Painter. The **SP ⇄ UEFN** panel appears (dock it anywhere).

## Usage

1. Start the UEFN side, then the Painter side (order doesn't matter — Painter retries discovery).
2. Open a Painter project with a mesh and at least BaseColor / Normal / Roughness painted.
3. Click **CONNECT** in the panel. First connect imports textures + material (+ mesh, if enabled).
4. Keep painting. **Save** (`Ctrl+S`) to sync, or flip **LIVE** on for continuous updates.
5. Use **IMPORT MESH** to toggle geometry transfer; **APPLY** (UEFN dashboard) assigns the
   material to a selected actor instead.

Assets land under `/<YourProject>/Substance/<MaterialName>/`.

## How it works

```
Substance 3D Painter (PySide6 plugin)            UEFN editor (unreal + tkinter)
  substance_painter_bridge.py                      uefn_substance_bridge.py
  • export textures + export_mesh                   • HTTP server :8780-8785
  • diff (full-content hash)                        • queue → Slate tick (main thread)
  • events: ProjectSaved / LayerStacks             • import tex + build M + MI + mesh
        │   HTTP POST {command, params}  ───────────────►  • dashboard
        └────────────────  127.0.0.1  ──────────────────┘
        heavy files handed off by local temp path (same machine)
```

The UEFN listener never touches `unreal.*` from the HTTP thread — commands are queued and executed
on the editor main thread via a Slate post-tick callback (because `unreal.*` is not thread-safe).

## Troubleshooting

- **"Cannot reach UEFN"** — make sure the UEFN dashboard is open; check nothing else holds `:8780`.
- **No mesh appears** — enable **IMPORT MESH**; the panel logs if the mesh can't be exported.
- **Export preset not found** — the bridge resolves "PBR Metallic Roughness" from your installed
  export presets; install/keep that preset, or it falls back to the default shelf id.
- **Two panels/toolbars** — keep a single copy of `substance_painter_bridge.py` in the plugins folder.

## Roadmap

- Skeletal mesh support (the import path is already mesh-type-aware; v1.1 will wire it to the
  original rigged FBX, since `export_mesh` carries geometry only).

## License

MIT — see [LICENSE](LICENSE). Bundled fonts are under the SIL Open Font License — see
[fonts/OFL-NOTICE.md](fonts/OFL-NOTICE.md).
