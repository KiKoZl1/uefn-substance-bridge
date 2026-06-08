# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions use [SemVer](https://semver.org/).

## [1.0.0] — 2026-06-08

First public release. Verified against UE 5.8 / Fortnite 41.00 and Substance 3D Painter 11.0.3.

### Added
- **Sync-on-save** via `substance_painter.event.ProjectSaved` (on by default).
- **Live sync** (optional, debounced) via `TextureStateEvent` (fires as you paint; `LayerStacksModelDataChanged` is also subscribed for layer-structure changes).
- **Mesh transfer from the project** using `export_mesh` — no original `.fbx`/`.obj` needed.
- **Auto port discovery** on the Painter side (scans `8780–8785`, reads the bound port).
- **Auto-detected project content root** (no manual project path field).
- Functional **Combine Meshes** and **Import Scale** (via `FbxImportUI`).
- Mesh-type-aware import path (StaticMesh now; skeletal scaffolding for v1.1).
- Dockable, branded UI panel on the Painter side; redesigned UEFN dashboard.
- Bundled brand fonts (Space Grotesk, Inter, JetBrains Mono) with safe system fallbacks.

### Changed
- Smart-sync now hashes **full file content** (previously size + first 4 KB, which silently
  missed real edits).
- Init is **idempotent and non-destructive** — reuses existing mesh/material/textures and no
  longer blanket-deletes Material/Texture assets in the destination folder.
- Clean asset naming (`M_<asset>_<set>`, `MI_…`, `T_…`, `SM_<asset>`) — no more `M_M_` doubling.
- Spawn camera uses `UnrealEditorSubsystem` (deprecated `EditorLevelLibrary` removed).
- Export preset resolved via `list_resource_export_presets()` (robust across installs).
- HTTP request timeout raised to 120 s for heavier first-imports.
- Plugin no longer auto-runs on import (relies on the plugin manager) — fixes duplicate panels.

### Fixed
- `TextureSet.name()` deprecation (now uses the property).
- Fragile stale-response GC parsing in the UEFN tick.
