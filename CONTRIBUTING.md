# Contributing

Thanks for your interest in improving the **Substance Painter → UEFN Bridge**!

## Project shape

Two halves, same machine, talking over local HTTP on `127.0.0.1:8780–8785`:

- `substance_painter_bridge.py` — runs **inside Substance 3D Painter** (PySide6 plugin).
- `uefn_substance_bridge.py` — runs **inside the UEFN editor** (imports `unreal`, tkinter dashboard).

Because `unreal.*` is not thread-safe, the UEFN side never touches it from the HTTP thread —
commands are queued and executed on the editor main thread via a Slate post-tick callback.

## Dev setup

1. Edit the files in this repo (source of truth).
2. **Painter:** copy `substance_painter_bridge.py` + `fonts/` into
   `Documents/Adobe/Adobe Substance 3D Painter/python/plugins/`, then reload the plugin
   (`importlib.reload`) or restart Painter.
3. **UEFN:** `Tools ▸ Execute Python Script ▸ uefn_substance_bridge.py`. Run only **one** instance.

## Conventions

- Python 3.10+ (host) / 3.11 (embedded). Standard library only on both sides (plus the host apps'
  bundled `substance_painter`, `unreal`, PySide6, tkinter). **No third-party pip deps.**
- Keep it dependency-light and same-machine simple.
- Verify a change compiles: `python -m py_compile substance_painter_bridge.py uefn_substance_bridge.py`.
- Match the existing terse style.

## Pull requests

- Describe what you changed and how you tested it (see the PR template).
- One focused change per PR where possible.

## Reporting bugs

Open an issue with your Substance Painter version, UEFN/UE version, OS, steps to reproduce, and the
logs from the Painter Python console + the UEFN dashboard activity panel.
