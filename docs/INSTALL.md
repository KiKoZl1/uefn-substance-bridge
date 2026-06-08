# Installation

The bridge has **two halves** that run on the **same machine**: one inside Substance 3D Painter,
one inside UEFN. Install both.

## Requirements

- **Windows** (the brand fonts register via the Win32 font API; the bridge still runs without them
  using system fallbacks).
- **Adobe Substance 3D Painter 11.x** (PySide6 / Python API ≥ 0.3.4). Older versions may work.
- **UEFN** with the **Python Editor Script Plugin** enabled
  (UEFN ▸ *Edit ▸ Plugins* ▸ search "Python" ▸ enable ▸ restart).
- Both apps on the **same computer** — textures and the mesh are handed off through a local temp
  folder, not over the network.

---

## 1. Painter side

1. Locate Painter's user plugins folder (create it if missing):
   ```
   C:\Users\<you>\Documents\Adobe\Adobe Substance 3D Painter\python\plugins\
   ```
2. Copy **both** of these into that folder:
   - `substance_painter_bridge.py`
   - the **`fonts/`** folder (keeps the branded look; optional — falls back to system fonts)
3. (Re)start Substance 3D Painter.
4. A dockable **“SP ⇄ UEFN”** panel appears. Drag it anywhere to dock it.

> Keep a **single** copy of `substance_painter_bridge.py` in the plugins folder — two copies create
> two panels.

## 2. UEFN side

1. Open your UEFN project.
2. `Tools ▸ Execute Python Script…`
3. Select `uefn_substance_bridge.py` (run it from wherever you keep this repo).
4. The **“SUBSTANCE ⇄ UEFN”** dashboard window opens, listening on `:8780`.

That's it — there's no manual project path to set; the bridge detects your project content root
automatically.

---

## Verify it's working

- UEFN dashboard shows `BRIDGE :8780` and your project path (e.g. `/MyProject`).
- In Painter, click **CONNECT** → the panel status turns to **CONNECTED**.

## Uninstall

- Painter: delete `substance_painter_bridge.py` (and `fonts/`) from the plugins folder, restart.
- UEFN: close the dashboard window (it stops the listener and frees the port).

## Notes on fonts

The bundled fonts (Space Grotesk, Inter, JetBrains Mono) are SIL OFL — see
[../fonts/OFL-NOTICE.md](../fonts/OFL-NOTICE.md). Deleting the `fonts/` folder is safe; the UI
falls back to Segoe UI / Consolas.
