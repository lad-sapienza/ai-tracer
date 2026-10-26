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
  Anything Model 2.1) returns a polygon outline in well under a second on a
  GPU and in about a second on CPU
- **Choice of model** — Tiny (fastest, default), Base+ or Large (most accurate)
  from a drop-down in the panel; larger models are downloaded the first time
  you select them (see [Models](#models))
- **Positive / negative prompts** — left-click to add to the selection,
  right-click to exclude areas; each click refines the mask without re-encoding
  the image, and reuses the previous mask so successive results stay stable
- **Undo last point** — Ctrl+Z removes the most recently added prompt point
  and re-segments, so you can correct mistakes without starting over
- **Real-time simplification** — Douglas-Peucker slider in screen pixels
  (0–6 px, finest control below 3 px, default 2.8 px) with a live vertex count;
  being in pixels it behaves the same at any zoom level or CRS, and your
  setting is remembered between sessions
- **Snap to neighbours** — optional topological snapping: the new polygon
  shares its boundary with polygons already in the output layer, tracing one
  coherent perimeter across several neighbours; see
  [Snapping to neighbours](#snapping-to-neighbours)
- **Edit the last polygon** — one click (or **E**) selects the polygon you just
  accepted and hands it to QGIS's own Vertex tool; no vertex-editing code to
  learn
- **Pause / Resume** — switching to another QGIS tool pauses AITracer instead
  of leaving it out of sync; **Resume** picks up where you left off, with the
  same model, output layer and field values
- **Keyboard shortcuts** — Enter to accept, Escape to cancel, Ctrl+Z to undo,
  E to edit the last polygon; no need to reach for the mouse
- **Output layer selector** — choose any existing polygon vector layer as the
  destination, or leave it blank to auto-create a dedicated *AITracer* memory
  layer with fields `fid`, `timestamp`, and `raster`
- **Clean panel UI** — all controls are hidden until the tool is activated,
  keeping the dock uncluttered when not in use
- **Idle auto-cancel** — sessions left inactive for 5 minutes are cancelled
  automatically, freeing backend memory
- **First-time setup is automatic** — the plugin downloads a standalone Python
  runtime, creates a virtual environment, installs all dependencies, and
  downloads the SAM2.1-tiny weights (~156 MB) the first time you activate the
  tool
- **Canvas-as-input** — the plugin captures a screenshot of the current QGIS
  canvas and sends it to SAM2; it does *not* read the original raster file.
  This keeps processing fast and memory-light on modest hardware, but has
  important consequences described below
- **Fully local** — all processing runs on your machine; no data is ever sent
  to external servers

---

## Requirements

| Component | Minimum version |
|-----------|----------------|
| QGIS | 4.0 |
| Python | 3.10 (downloaded automatically via python-build-standalone) |
| Internet | Required once, for first-time dependency and weight download |
| RAM | 4 GB recommended for Tiny and Base+; about 8 GB for Large |
| GPU | Optional — Apple Silicon (MPS) and CUDA are used when available, otherwise CPU |

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
4. Download the SAM2.1-tiny checkpoint (~156 MB) from Meta's servers

This takes 2–5 minutes depending on your connection. A progress dialog keeps
you informed. Subsequent activations start in seconds. The larger models are
not part of the first-time setup; they are downloaded on demand (see
[Models](#models)).

If anything goes wrong after an update, use the **⟳ Reset installation**
button at the bottom of the dock to wipe the venv and reinstall cleanly
(the standalone Python is preserved).

---

## Usage

1. Load a georeferenced raster layer in QGIS.
2. Open the *AITracer by LAD* dock panel (it appears on the right by default).
3. Click **▶ Activate** — the panel expands to show all controls.
4. Optionally pick a **Model** (Tiny is the default and the fastest).
5. **Left-click** on a feature in the canvas — a green polygon preview appears.
6. **Left-click** again to add more context; **right-click** to mark areas to
   exclude; **Ctrl+Z** to remove the last point.
7. Adjust the **Simplify** slider to control vertex density. Optionally tick
   **Snap to neighbours** to attach the outline to existing polygons.
8. Choose the **output layer** from the drop-down (defaults to the AITracer
   memory layer; any polygon vector layer in the project can be used).
9. Press **Enter** (or click **Accept**) to save the polygon. Press **Escape**
   (or **Cancel**) to discard.
10. Need to move a vertex? Click **✎ Edit last polygon** (or press **E**), fix
    it with QGIS's Vertex tool, then press **▶ Resume**. See
    [Editing and pausing](#editing-and-pausing).
11. Repeat for the next feature. Click **⏹ Deactivate** when done.

> **Important:** AITracer uses the canvas screenshot as input, not the original
> raster file. Zoom so the target feature fills the screen before clicking —
> output resolution depends entirely on your zoom level. See
> [How image input works](#how-image-input-works--what-you-see-is-what-sam2-gets)
> for full details.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| Left-click | Add positive prompt (include) |
| Right-click | Add negative prompt (exclude) |
| Ctrl+Z | Undo last prompt point |
| Enter / Return | Accept polygon |
| Escape | Cancel current session |
| E | Edit the last accepted polygon (when no session is in progress) |

---

## Models

AITracer uses SAM 2.1 and lets you pick the size of the model from the
**Model** drop-down. Switching is instant once the weights are on disk; the
first time you choose a model, you are asked to confirm a one-off download
(streamed to `~/.aitracer/weights/`, with progress and a Cancel button).

| Model | Download | First click | RAM | When to use |
|-------|----------|-------------|-----|-------------|
| Tiny (default) | 156 MB | 0.15 s GPU · 0.4 s CPU | ~1–1.5 GB | Everyday digitising, modest hardware |
| Base+ | 324 MB | 0.3 s GPU · 0.8 s CPU | ~1–2 GB | Cleaner boundaries at little extra cost |
| Large | 898 MB | 0.5 s GPU · 1.8 s CPU | ~2–3 GB | Hard cases, if you have the RAM |

Timings are indicative, measured on an Apple Silicon Mac (GPU = MPS) on a
1280×800 canvas. The first click of each view pays the model's full cost (the
image is encoded once); subsequent refinement clicks take about 10–25 ms for
every model, so a larger model mainly affects how long you wait after the
first click.

Your choice is remembered between sessions. Changing the model while a preview
is on screen cancels that preview, because its cached encoding belongs to the
previous model.

---

## Snapping to neighbours

When **Snap to neighbours** is ticked, the preview is attached to the polygons
already in the output layer, so adjacent features share their boundaries
instead of leaving gaps and overlaps:

1. Parts of the new polygon covering an existing polygon are cut away.
2. The outlines of all nearby polygons are treated as one network. Every edge
   of the new polygon that runs along it (both ends within the snap distance)
   is replaced by the path along that network, crossing from one neighbour to
   the next where their outlines meet — like QGIS's tracing tool. Where
   neighbours do not touch, the outline joins the two nearest points directly.
3. The vertices now lying on a neighbour's boundary are added to that neighbour,
   so both outlines have coincident vertices. The neighbours' shapes do not
   change.

The snap distance is set next to the checkbox in screen pixels (default 8 px),
so it behaves the same at any zoom level. The preview already shows the snapped
outline, and one undo in QGIS reverts the new polygon together with the
vertices added to its neighbours.

Notes:

- Snapping works only if the output layer uses the same CRS as the map.
- A lone vertex near a neighbour is not snapped: the minimum contact is one
  segment.
- Neighbours covered for more than half by the new polygon, or lying entirely
  inside it, are left alone, as that usually means a different object was traced.
- Snapped polygons can end up with many vertices, because they take over the
  vertices of the outlines they follow. Use **Simplify** and the layer's own
  tools if you need fewer.

---

## Editing and pausing

### Edit the last polygon

After accepting a polygon, **✎ Edit last polygon** (shortcut **E**) selects it,
makes its layer current and switches to QGIS's Vertex tool. Move, add or delete
vertices as usual, then press **▶ Resume** to go back to tracing; the selection
is cleared automatically. Layer undo (Ctrl+Z in QGIS) reverts your edits.

If the layer's edits have been saved in the meantime, or the layer has been
removed, the polygon can no longer be found and the button is disabled.

### Pause and Resume

Whenever you pick another QGIS tool while AITracer is active — the Vertex tool,
Identify, Pan, anything — AITracer **pauses**: the polygon preview in progress
is discarded and the button becomes **▶ Resume**. Resume re-activates the
tracing tool without restarting anything, so the model, the output layer and
the field values you set are unchanged. While paused, **⏹ Deactivate** is also
available and leaves your current tool alone.

---

## How image input works — what you see is what SAM2 gets

AITracer does **not** read the original raster file. Instead, it takes a
screenshot of the QGIS canvas at the moment you click, and sends that
screenshot to the SAM2 model. This is a deliberate design choice: it keeps
memory usage low and processing fast even on modest hardware — no gigapixel
raster needs to be loaded into RAM.

This design has three practical consequences you should keep in mind:

### Resolution depends on your zoom level, not the raster

The polygon returned by SAM2 is only as detailed as the pixels visible on
screen. If you are zoomed out, each screen pixel represents a large ground
area, and the polygon will have coarse boundaries. Zoom in until the target
feature fills a large part of the canvas to get the sharpest result.

### Features must be fully visible to be fully traced

Because the input image is a canvas screenshot, any part of a feature that
lies outside the current view is simply not in the image. If a wall, a plot
boundary, or any other shape extends beyond the screen edge, only the visible
portion will be included in the output polygon. Pan and zoom so the entire
feature is on screen before clicking.

### The canvas is locked during a session — intentionally

After the first click, the canvas extent is frozen until you accept or cancel.
Panning or zooming would invalidate the coordinate mapping between screen pixels
(where SAM2 works) and map coordinates (where the polygon must live), producing
incorrect geometries. The lock is lifted the moment you press Enter or Escape.

> **Workflow tip:** before activating the tool, zoom to the feature you want to
> digitize, leaving a small margin around it. Make sure the target raster is
> the only visible layer (other layers affect the screenshot). Then activate,
> click, refine, and accept.

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

### Attribute values

When an existing layer is selected, a **Field values** panel appears in the
dock listing the layer's editable fields, rendered with the editor widgets
configured in the layer (value maps, ranges, …). Values entered there are
applied to every accepted polygon until you change them — handy when
digitising a batch of features that share attributes (same stratigraphic
unit, same material, …), and they are remembered per layer when switching
between output layers.

Fields left empty fall back to the **default values** configured in
*Layer Properties → Attributes Form* (evaluated per feature, so expressions
like `now()` work). Fields named `fid`, `timestamp`, or `raster` are managed
automatically by the plugin and are not shown in the panel.

---

## Architecture

```
plugin/                  QGIS plugin (Python / PyQt6)
  main.py                Plugin lifecycle, session state, backend management
  dock.py                Dock panel UI
  map_tool.py            Canvas event capture (clicks, keyboard)
  geometry.py            Pixel ↔ geo coordinate conversion (QgsMapToPixel)
  snapping.py            Topological snapping of a new polygon to its neighbours
  canvas_capture.py      Canvas screenshot → base64 PNG
  preview.py             QgsRubberBand overlay
  backend_client.py      HTTP client for the local backend
  python_downloader.py   Downloads python-build-standalone on first run
  weights_downloader.py  Streams model checkpoints to disk (QgsFileDownloader)
  segmentation_worker.py Runs backend calls on a QThread
  backend/               FastAPI backend (runs as a subprocess)
    app.py               /health, /segment, /load and /clear endpoints
    model.py             SAM2ImagePredictor wrapper: session cache, mask
                         refinement, runtime model switching
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
