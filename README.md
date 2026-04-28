# AITracer by LAD

**AI-powered human-in-the-loop raster digitization for archaeologists.**

AITracer is a [QGIS 4](https://qgis.org) plugin developed by the
[LAD – Laboratorio di Archeologia Digitale](https://lad.saras.uniroma1.it)
at Sapienza University of Rome. It lets you digitize features from any raster
layer (aerial photographs, drone imagery, scanned maps, georeferenced drawings)
by clicking on them — [SAM2](https://github.com/facebookresearch/sam2)
segments the object instantly, you refine the result with additional prompts,
then accept it as a vector polygon with a single keystroke.

![AITracer in action](plugin/ui/tracer-by-lad.svg)

---

## Features

- **One-click segmentation** — left-click on any raster feature; SAM2 (Segment
  Anything Model 2, tiny variant) returns a polygon outline in under a second
  on CPU
- **Positive / negative prompts** — left-click to add to the selection,
  right-click to exclude areas; each click refines the mask without re-encoding
  the image
- **Undo last point** — Ctrl+Z removes the most recently added prompt point
  and re-segments, so you can correct mistakes without starting over
- **Real-time simplification** — Douglas-Peucker slider (0–0.50 map units,
  0.01 step) lets you balance vertex density against smoothness before
  accepting
- **Keyboard shortcuts** — Enter to accept, Escape to cancel, Ctrl+Z to undo;
  no need to reach for the mouse
- **Output layer selector** — choose any existing polygon vector layer as the
  destination, or leave it blank to auto-create a dedicated *AITracer* memory
  layer with fields `fid`, `timestamp`, and `raster`
- **Clean panel UI** — all controls are hidden until the tool is activated,
  keeping the dock uncluttered when not in use
- **Idle auto-cancel** — sessions left inactive for 5 minutes are cancelled
  automatically, freeing backend memory
- **First-time setup is automatic** — the plugin downloads a standalone Python
  runtime, creates a virtual environment, installs all dependencies, and
  downloads the SAM2-tiny weights (~40 MB) the first time you activate the tool
- **Canvas-as-ROI** — zoom and pan to the area of interest before clicking;
  the current canvas view is used as the image input, so you implicitly control
  resolution and context
- **Fully local** — all processing runs on your machine; no data is ever sent
  to external servers

---

## Requirements

| Component | Minimum version |
|-----------|----------------|
| QGIS | 4.0 |
| Python | 3.10 (downloaded automatically via python-build-standalone) |
| Internet | Required once, for first-time dependency and weight download |
| RAM | 4 GB recommended (SAM2-tiny runs on CPU) |

macOS, Linux and Windows are supported.

---

## Installation

### From a release zip (recommended)

1. Download the latest `aitracer-vX.Y.Z.zip` from the
   [Releases](https://github.com/lad-sapienza/ai-tracer/releases) page.
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Browse to the downloaded zip and click **Install Plugin**.
4. Enable *AITracer by LAD* in the plugin list.

### From source (development)

```bash
git clone https://github.com/lad-sapienza/ai-tracer.git
cd ai-tracer
# Symlink the plugin folder into your QGIS profile
ln -s "$(pwd)/plugin" \
  ~/Library/Application\ Support/QGIS/QGIS4/profiles/default/python/plugins/aitracer
```

Then in QGIS: **Plugins → Manage and Install Plugins**, enable *AITracer by LAD*.

---

## First-time setup

The first time you click **▶ Activate**, the plugin will:

1. Download a standalone Python runtime to `~/.aitracer/python_standalone`
2. Create a virtual environment at `~/.aitracer/venv`
3. Install FastAPI, uvicorn, PyTorch (CPU), SAM2, OpenCV, and NumPy
4. Download the SAM2-tiny checkpoint (~40 MB) from Meta's servers

This takes 2–5 minutes depending on your connection. A progress dialog keeps
you informed. Subsequent activations start in seconds.

If anything goes wrong after an update, use the **⟳ Reset installation**
button at the bottom of the dock to wipe the venv and reinstall cleanly
(the standalone Python is preserved).

---

## Usage

1. Load a georeferenced raster layer in QGIS.
2. Open the *AITracer by LAD* dock panel (it appears on the right by default).
3. Click **▶ Activate** — the panel expands to show all controls.
4. **Left-click** on a feature in the canvas — a green polygon preview appears.
5. **Left-click** again to add more context; **right-click** to mark areas to
   exclude; **Ctrl+Z** to remove the last point.
6. Adjust the **Simplify** slider to control vertex density.
7. Choose the **output layer** from the drop-down (defaults to the AITracer
   memory layer; any polygon vector layer in the project can be used).
8. Press **Enter** (or click **Accept**) to save the polygon. Press **Escape**
   (or **Cancel**) to discard.
9. Repeat for the next feature. Click **⏹ Deactivate** when done.

> **Tip:** for best segmentation results, keep only the target raster layer
> visible before activating the tool. Other visible layers can affect the
> model's image encoding.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| Left-click | Add positive prompt (include) |
| Right-click | Add negative prompt (exclude) |
| Ctrl+Z | Undo last prompt point |
| Enter / Return | Accept polygon |
| Escape | Cancel current session |

---

## Output layer

By default, accepted polygons go into a temporary in-memory layer named
**AITracer**, created automatically with a semi-transparent orange style.
You can also select any existing polygon vector layer from the drop-down in
the dock panel.

The AITracer memory layer includes these fields:

| Field | Type | Description |
|-------|------|-------------|
| `fid` | Integer | Auto-incrementing feature ID |
| `timestamp` | String | ISO 8601 acceptance time |
| `raster` | String | Name of the topmost visible raster layer |

To persist the layer, right-click it in the Layers panel and choose
**Export → Save Features As**.

---

## Architecture

```
plugin/                  QGIS plugin (Python / PyQt6)
  main.py                Plugin lifecycle, session state, backend management
  dock.py                Dock panel UI
  map_tool.py            Canvas event capture (clicks, keyboard)
  geometry.py            Pixel ↔ geo coordinate conversion (QgsMapToPixel)
  canvas_capture.py      Canvas screenshot → base64 PNG
  preview.py             QgsRubberBand overlay
  backend_client.py      HTTP client for the local backend
  python_downloader.py   Downloads python-build-standalone on first run
  segmentation_worker.py Runs backend calls on a QThread
  backend/               FastAPI backend (runs as a subprocess)
    app.py               /health and /segment endpoints
    model.py             SAM2ImagePredictor wrapper with session cache
    utils.py             Mask → polygon conversion (cv2 contours)
```

The plugin starts a local FastAPI/uvicorn server on a free port the first time
the tool is activated. The server persists across segmentation sessions for the
lifetime of the QGIS process and is terminated on plugin unload.

---

## Acknowledgements

**AITracer by LAD** was conceived and directed by
**Julian Bogdani** (Sapienza University of Rome,
[LAD – Laboratorio di Archeologia Digitale](https://lad.saras.uniroma1.it)).

The software architecture, implementation, and iterative debugging were carried
out in close collaboration with
**[Claude](https://claude.ai) (Anthropic)**, an AI assistant, as a
human-AI pair-programming experiment in archaeological software development.

This project uses:
- [SAM2](https://github.com/facebookresearch/sam2) by Meta FAIR (Apache 2.0)
- [FastAPI](https://fastapi.tiangolo.com) (MIT)
- [PyTorch](https://pytorch.org) (BSD)
- [OpenCV](https://opencv.org) (Apache 2.0)
- [QGIS](https://qgis.org) (GPL-2.0)

---

## Contributing

Bug reports and feature requests are welcome via
[GitHub Issues](https://github.com/lad-sapienza/ai-tracer/issues).
Pull requests should target the `main` branch.

---

## License

AITracer by LAD is released under the
[GNU General Public License v3.0](LICENSE).

Copyright © 2026 Julian Bogdani / LAD – Laboratorio di Archeologia Digitale,
Sapienza University of Rome.
