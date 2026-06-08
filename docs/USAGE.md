# Usage

A walkthrough of the **paint → engine** workflow. Assumes both halves are installed
(see [INSTALL.md](INSTALL.md)).

## Quick start

1. **UEFN**: `Tools ▸ Execute Python Script ▸ uefn_substance_bridge.py` → the dashboard opens.
2. **Painter**: open a project that has a mesh and at least *BaseColor / Normal / Roughness*
   painted, then click **CONNECT** in the **SP ⇄ UEFN** panel.
3. First connect creates everything in UEFN and (by default) imports + spawns your mesh.
4. Keep painting. **Save** (`Ctrl+S`) to push an update — or flip **LIVE** on for continuous updates.

Assets land in your project under:
```
/<YourProject>/Substance/<MaterialName>/
   M_<MaterialName>_<set>     parent Material (PBR)
   MI_<MaterialName>_<set>    Material Instance (what's applied)
   T_<MaterialName>_<set>_*   imported textures
   SM_<MaterialName>          imported mesh (if Import Mesh is on)
```

## The panel

| Control | What it does |
|---|---|
| **CONNECT / DISCONNECT** | Start/stop the link to UEFN. Connect runs an initial sync. |
| **SYNC NOW** | Push the current textures on demand. |
| **LIVE** | When ON, auto-syncs ~1.2 s after you stop painting (debounced). |
| **ON SAVE** | When ON (default), syncs every time you save the project (`Ctrl+S`). |
| **IMPORT MESH** | When ON (default), the mesh is exported from the project and imported into UEFN. |

## Sync modes

- **Manual** — just click **SYNC NOW** whenever you want.
- **On save (default)** — keep painting, hit `Ctrl+S`; the bridge syncs the changed maps.
- **Live** — toggle **LIVE**; the bridge syncs shortly after each pause in painting.

All modes are **smart**: only texture maps that actually changed are re-sent, and Material
Instances update **without recompiling** the material (fast).

## Mesh

- The mesh is exported straight from the Substance project — you **don't** need the original
  `.fbx`/`.obj` on disk.
- **Scale** and **Combine Meshes** (on the UEFN dashboard) control the FBX import.
- If a mesh with the same name already exists in UEFN, it's **reused, not replaced**.
- Prefer to keep your mesh in UEFN already? Turn **IMPORT MESH** off and use **APPLY** instead
  (select the actor in UEFN, then click **APPLY** on the dashboard to assign the material).

## Reconnecting

Disconnect and connect again any time — the bridge detects existing materials/mesh and **reuses**
them (idempotent). It never blanket-deletes assets in the destination folder.

## Troubleshooting

| Symptom | Fix |
|---|---|
| **“Cannot reach UEFN”** | Make sure the UEFN dashboard is open; nothing else should hold `:8780`. The Painter side auto-scans `8780–8785`. |
| **No mesh appears** | Enable **IMPORT MESH**; check the panel/dashboard log for a mesh-export message. |
| **Export preset not found** | Keep the *“PBR Metallic Roughness”* export preset installed in Painter. |
| **Two panels in Painter** | You have two copies of the plugin — keep only one in `python/plugins/`. |
| **Live didn't trigger** | Make sure **LIVE** is ON (yellow) and you paused painting ~1.5 s; check the Painter console for `subscribe ... failed`. |

## Limitations

- **StaticMesh only** in v1.0 (skeletal mesh is planned — the mesh from `export_mesh` carries
  geometry only, so skeletal will use the original rigged FBX).
- Same-machine only (local file handoff).
