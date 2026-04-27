import base64
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from qgis.core import (
    QgsFeature, QgsGeometry, QgsMessageLog, Qgis,
    QgsProject, QgsVectorLayer,
    QgsFillSymbol, QgsSingleSymbolRenderer,
)
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapToolPan
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QProgressDialog

from .map_tool import SegmentationTool
from .canvas_capture import capture_base64
from .geometry import geo_to_pixel, polygon_pixel_to_geo, simplify_polygon_geo
from .preview import PreviewOverlay
from .dock import AITracerDock
from . import backend_client
from .backend_client import BackendError
from . import python_downloader

PLUGIN_NAME = "AITracer by LAD"
TEMP_LAYER_NAME = "AITracer"
BACKEND_DIR = Path(__file__).resolve().parent / "backend"
VENV_DIR = Path.home() / ".aitracer" / "venv"  # outside QGIS-watched paths
BACKEND_PORT = 8765

# Windows venv uses Scripts\, Unix uses bin/
_VENV_BIN = "Scripts" if sys.platform == "win32" else "bin"
_EXE = ".exe" if sys.platform == "win32" else ""


def _venv(name: str) -> Path:
    """Return the path to an executable inside the managed venv."""
    return VENV_DIR / _VENV_BIN / (name + _EXE)


def _log(msg, level=Qgis.MessageLevel.Info):
    QgsMessageLog.logMessage(msg, PLUGIN_NAME, level)


class VectorizePlugin:
    def __init__(self, iface):
        self._iface = iface
        self._canvas = iface.mapCanvas()
        self._tool = None
        self._prev_tool = None
        self._dock = None
        self._overlay = None
        self._session = _empty_session()
        self._backend_proc = None
        self._backend_log = None

    # ------------------------------------------------------------------ #
    # QGIS plugin lifecycle                                               #
    # ------------------------------------------------------------------ #

    def initGui(self):
        self._tool = SegmentationTool(self._canvas)
        self._tool.clicked.connect(self._on_canvas_clicked)
        self._tool.accept_requested.connect(self._on_accept)
        self._tool.cancel_requested.connect(self._on_cancel)

        self._overlay = PreviewOverlay(self._canvas)

        self._dock = AITracerDock(self._iface.mainWindow())
        self._dock.accepted.connect(self._on_accept)
        self._dock.cancelled.connect(self._on_cancel)
        self._dock.simplify_changed.connect(self._on_simplify_changed)
        self._dock.tool_toggled.connect(self._toggle_tool)
        self._iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._dock)

        self._dock.set_status("Activate the tool to start segmentation.")

    def unload(self):
        self._on_cancel()
        self._stop_backend()
        if self._dock:
            self._iface.removeDockWidget(self._dock)
            self._dock.deleteLater()
            self._dock = None

    # ------------------------------------------------------------------ #
    # Tool activation                                                     #
    # ------------------------------------------------------------------ #

    def _toggle_tool(self, active: bool):
        if active:
            if not self._ensure_backend():
                self._dock.set_tool_active(False)
                return
            self._prev_tool = self._canvas.mapTool()
            self._canvas.setMapTool(self._tool)
            self._dock.set_status("Left-click to segment. Right-click to exclude.")
        else:
            self._end_session()
            self._canvas.setMapTool(self._prev_tool or QgsMapToolPan(self._canvas))
            self._dock.set_status("Deactivated. Press Activate to start.")

    # ------------------------------------------------------------------ #
    # Backend subprocess management                                       #
    # ------------------------------------------------------------------ #

    def _ensure_backend(self) -> bool:
        """Make sure the backend is running. Return True if ready."""
        if backend_client.health_check():
            return True

        if not _venv("uvicorn").exists():
            if not self._run_setup():
                return False

        return self._start_backend()

    def _run_setup(self) -> bool:
        """Download standalone Python, create venv, install requirements and
        model weights. Returns True on success."""
        from qgis.PyQt.QtWidgets import QApplication

        dlg = QProgressDialog(
            "Setting up AITracer backend…", "Cancel", 0, 0,
            self._iface.mainWindow()
        )
        dlg.setWindowTitle("AITracer — First-time Setup")
        dlg.setMinimumWidth(420)
        dlg.setModal(True)
        dlg.show()
        QApplication.processEvents()

        # ── Step 1: standalone Python ──────────────────────────────────────
        if not python_downloader.is_installed():
            def _progress(pct, msg):
                dlg.setLabelText(msg)
                QApplication.processEvents()

            ok, msg = python_downloader.install(
                progress_cb=_progress,
                cancel_check=dlg.wasCanceled,
            )
            if not ok:
                dlg.close()
                self._iface.messageBar().pushMessage(
                    PLUGIN_NAME,
                    f"Could not install Python runtime: {msg}",
                    level=Qgis.MessageLevel.Critical, duration=15,
                )
                return False
            if dlg.wasCanceled():
                dlg.close()
                return False

        python = python_downloader.python_executable()
        _log(f"Using standalone Python: {python}")

        # ── Step 2: venv + pip + dependencies ─────────────────────────────
        VENV_DIR.parent.mkdir(parents=True, exist_ok=True)

        # Always wipe any pre-existing venv so we start from a clean state.
        # A stale venv from a previous (failed) setup can leave pip absent,
        # which causes FileNotFoundError when the pip step runs.
        if VENV_DIR.exists():
            _log("Removing existing venv for a clean rebuild…")
            shutil.rmtree(VENV_DIR)

        # On Windows the venv Python is python.exe (no python3.exe), so we
        # must bootstrap pip manually via ensurepip instead of letting venv
        # install it (venv's pip installer calls python3.exe internally).
        # On macOS/Linux venv bundles pip cleanly — no manual bootstrap needed.
        if sys.platform == "win32":
            steps = [
                ([python, "-m", "venv", "--without-pip", str(VENV_DIR)],
                 "Creating virtual environment…", None),
                ([str(_venv("python")), "-m", "ensurepip", "--upgrade"],
                 "Bootstrapping pip…", None),
                ([str(_venv("pip")), "install", "--upgrade", "pip"],
                 "Upgrading pip…", None),
                ([str(_venv("pip")), "install", "-r",
                  str(BACKEND_DIR / "requirements.txt")],
                 "Installing dependencies (this may take several minutes)…",
                 str(BACKEND_DIR)),
            ]
        else:
            steps = [
                ([python, "-m", "venv", str(VENV_DIR)],
                 "Creating virtual environment…", None),
                ([str(_venv("pip")), "install", "--upgrade", "pip"],
                 "Upgrading pip…", None),
                ([str(_venv("pip")), "install", "-r",
                  str(BACKEND_DIR / "requirements.txt")],
                 "Installing dependencies (this may take several minutes)…",
                 str(BACKEND_DIR)),
            ]

        for cmd, label, cwd in steps:
            if dlg.wasCanceled():
                dlg.close()
                return False
            dlg.setLabelText(label)
            QApplication.processEvents()

            run_kwargs: dict = {"capture_output": True, "env": _clean_env()}
            if cwd:
                run_kwargs["cwd"] = cwd
            if sys.platform == "win32":
                run_kwargs["startupinfo"] = _win_startupinfo()

            result = subprocess.run(cmd, **run_kwargs)
            out = (result.stderr.decode(errors="replace")
                   or result.stdout.decode(errors="replace"))
            _log(f"{label}\n{out[:500]}")
            if result.returncode != 0:
                dlg.close()
                self._iface.messageBar().pushMessage(
                    PLUGIN_NAME,
                    f"Setup failed at '{label}': {out[:300]}",
                    level=Qgis.MessageLevel.Critical, duration=10,
                )
                return False

        # ── Step 3: model weights ──────────────────────────────────────────
        dlg.setLabelText("Downloading SAM2-tiny weights (~40 MB)…")
        QApplication.processEvents()
        if not self._download_weights():
            dlg.close()
            return False

        dlg.close()
        return True

    def _download_weights(self) -> bool:
        weights_dir = BACKEND_DIR / "weights"
        weights_dir.mkdir(exist_ok=True)
        checkpoint = weights_dir / "sam2.1_hiera_tiny.pt"
        if checkpoint.exists():
            return True

        python = _venv("python")
        script = (
            "import urllib.request; "
            "urllib.request.urlretrieve("
            "'https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt',"
            f"'{checkpoint}'"
            ")"
        )
        run_kw: dict = {"capture_output": True, "env": _clean_env()}
        if sys.platform == "win32":
            run_kw["startupinfo"] = _win_startupinfo()
        result = subprocess.run([str(python), "-c", script], **run_kw)
        if result.returncode != 0:
            self._iface.messageBar().pushMessage(
                PLUGIN_NAME,
                "Failed to download model weights. Check your internet connection.",
                level=Qgis.MessageLevel.Critical, duration=10
            )
            return False
        return True

    def _start_backend(self) -> bool:
        """Launch the uvicorn subprocess and wait up to 60s for it to be ready."""
        uvicorn = _venv("uvicorn")
        log_file = VENV_DIR.parent / "backend.log"

        popen_kw: dict = {
            "cwd": str(BACKEND_DIR),
            "stdout": open(log_file, "w"),
            "stderr": subprocess.STDOUT,
            "env": _clean_env(),
        }
        if sys.platform == "win32":
            popen_kw["startupinfo"] = _win_startupinfo()
        self._backend_log = popen_kw["stdout"]
        self._backend_proc = subprocess.Popen(
            [str(uvicorn), "app:app", "--port", str(BACKEND_PORT), "--log-level", "info"],
            **popen_kw,
        )
        _log(f"Backend subprocess started (log: {log_file}), waiting for readiness…")

        deadline = time.time() + 60  # SAM2 model load can be slow
        while time.time() < deadline:
            if backend_client.health_check():
                _log("Backend ready.")
                return True
            if self._backend_proc.poll() is not None:
                # Process exited already — read log and report
                self._backend_log.flush()
                try:
                    log_tail = log_file.read_text()[-600:]
                except Exception:
                    log_tail = "(could not read log)"
                _log(f"Backend crashed:\n{log_tail}", Qgis.MessageLevel.Critical)
                self._iface.messageBar().pushMessage(
                    PLUGIN_NAME,
                    f"Backend crashed on startup. See Log Messages → AITTrace.\n{log_tail[:200]}",
                    level=Qgis.MessageLevel.Critical, duration=15
                )
                return False
            time.sleep(0.5)

        self._backend_log.flush()
        self._iface.messageBar().pushMessage(
            PLUGIN_NAME,
            f"Backend did not start within 60s. Check {log_file}",
            level=Qgis.MessageLevel.Critical, duration=8
        )
        self._stop_backend()
        return False

    def _stop_backend(self):
        if self._backend_proc and self._backend_proc.poll() is None:
            self._backend_proc.terminate()
            try:
                self._backend_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._backend_proc.kill()
        self._backend_proc = None
        if hasattr(self, "_backend_log") and self._backend_log:
            self._backend_log.close()
            self._backend_log = None

    # ------------------------------------------------------------------ #
    # Temp layer management                                               #
    # ------------------------------------------------------------------ #

    def _get_or_create_layer(self):
        project = QgsProject.instance()
        for layer in project.mapLayersByName(TEMP_LAYER_NAME):
            if (isinstance(layer, QgsVectorLayer)
                    and layer.geometryType() == Qgis.GeometryType.Polygon
                    and layer.dataProvider().name() == "memory"):
                return layer

        crs = self._canvas.mapSettings().destinationCrs()
        uri = f"Polygon?crs={crs.authid()}&field=fid:integer&field=timestamp:string&field=raster:string"
        layer = QgsVectorLayer(uri, TEMP_LAYER_NAME, "memory")
        if not layer.isValid():
            _log("Failed to create temp layer.", Qgis.MessageLevel.Critical)
            return None
        _apply_default_style(layer)
        project.addMapLayer(layer)
        return layer

    def _topmost_raster_name(self) -> str:
        root = QgsProject.instance().layerTreeRoot()
        for tree_layer in root.findLayers():
            if not tree_layer.isVisible():
                continue
            layer = tree_layer.layer()
            if layer and layer.type() == Qgis.LayerType.Raster:
                return layer.name()
        return ""

    # ------------------------------------------------------------------ #
    # Session logic                                                       #
    # ------------------------------------------------------------------ #

    def _on_canvas_clicked(self, point, is_negative: bool):
        if not self._session["active"]:
            self._session = _empty_session()
            self._session["active"] = True
            self._session["canvas_image_b64"] = capture_base64(self._canvas)
            # QgsMapToPixel handles rotation, scale, and all canvas transforms.
            # Snapshot it now so it stays consistent throughout the session.
            self._session["mtp"] = self._canvas.mapSettings().mapToPixel()
            self._session["dpr"] = self._canvas.devicePixelRatioF()
            self._session["raster_name"] = self._topmost_raster_name()
            self._tool.set_session_active(True)
            self._dock.set_session_active(True)

        px, py = geo_to_pixel(
            point,
            self._session["mtp"],
            self._session["dpr"],
        )
        if is_negative:
            self._session["negative_points"].append([px, py])
            self._dock.set_status("Exclusion point added. Continue or Accept.")
        else:
            self._session["positive_points"].append([px, py])
            self._dock.set_status("Segmenting…")

        self._run_segmentation()

    def _run_segmentation(self):
        try:
            result = backend_client.segment(
                image_b64=self._session["canvas_image_b64"] if not self._session["session_id"] else None,
                positive_points=self._session["positive_points"],
                negative_points=self._session["negative_points"],
                session_id=self._session["session_id"],
            )
        except BackendError as e:
            self._iface.messageBar().pushMessage(
                PLUGIN_NAME, str(e),
                level=Qgis.MessageLevel.Warning, duration=5
            )
            self._dock.set_status("Segmentation failed. Try again.")
            return

        self._session["session_id"] = result["session_id"]

        pixel_polygon = result["polygon"]
        if not pixel_polygon:
            self._dock.set_status("No object found. Try clicking closer to the target.")
            return

        geo_polygon = polygon_pixel_to_geo(
            pixel_polygon,
            self._session["mtp"],
            self._session["dpr"],
        )
        self._session["raw_polygon_geo"] = geo_polygon
        self._dock.set_confidence(result.get("confidence"))
        self._update_preview()
        self._dock.set_status("Preview ready. Refine or Accept.")

    def _update_preview(self):
        raw = self._session.get("raw_polygon_geo")
        if not raw:
            return
        tolerance = self._dock.simplify_tolerance()
        simplified = simplify_polygon_geo(raw, tolerance)
        self._session["current_polygon_geo"] = simplified
        self._overlay.show(simplified)

    def _on_simplify_changed(self, _value: float):
        if self._session["active"]:
            self._update_preview()

    def _on_accept(self):
        polygon = self._session.get("current_polygon_geo")
        if not polygon:
            return
        layer = self._get_or_create_layer()
        if layer is None:
            return
        self._insert_feature(polygon, layer)
        sid = self._session.get("session_id")
        if sid:
            backend_client.clear_session(sid)
        self._end_session()
        self._dock.set_status("Accepted. Left-click to segment again.")

    def _on_cancel(self):
        sid = self._session.get("session_id")
        if sid:
            backend_client.clear_session(sid)
        self._end_session()
        if self._dock:
            self._dock.set_status("Cancelled. Left-click to segment.")

    def _end_session(self):
        self._overlay.clear()
        self._session = _empty_session()
        if self._tool:
            self._tool.set_session_active(False)
        if self._dock:
            self._dock.set_session_active(False)

    # ------------------------------------------------------------------ #
    # Feature insertion                                                   #
    # ------------------------------------------------------------------ #

    def _insert_feature(self, polygon_geo: list, layer):
        if not layer.isEditable():
            layer.startEditing()

        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPolygonXY([polygon_geo]))
        existing_fids = [f["fid"] for f in layer.getFeatures() if f["fid"]]
        next_fid = max(existing_fids, default=0) + 1
        feature.setAttribute("fid", next_fid)
        feature.setAttribute("timestamp", datetime.now().isoformat(timespec="seconds"))
        feature.setAttribute("raster", self._session.get("raster_name", ""))

        layer.beginEditCommand("Add segmented feature")
        ok = layer.addFeature(feature)
        if ok:
            layer.endEditCommand()
        else:
            layer.destroyEditCommand()
            self._iface.messageBar().pushMessage(
                PLUGIN_NAME, "Failed to insert feature.",
                level=Qgis.MessageLevel.Critical, duration=5
            )


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _apply_default_style(layer: QgsVectorLayer):
    """Apply a fixed default style to the AITracer output layer.

    Orange fill at 50% opacity, darker orange stroke.
    Chosen to be clearly distinct from the green rubber-band preview.
    """
    symbol = QgsFillSymbol.createSimple({
        "color": "255,140,0,128",        # orange, 50 % opacity (alpha=128)
        "outline_color": "200,100,0,255",
        "outline_width": "0.5",
        "outline_style": "solid",
    })
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))


def _clean_env() -> dict:
    """Return os.environ without PYTHONHOME/PYTHONPATH.

    Prevents the QGIS Python environment from leaking into standalone-Python
    subprocesses (QGIS sets PYTHONHOME to its own interpreter, which would
    confuse an unrelated Python executable).
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _win_startupinfo():
    """STARTUPINFO that hides the console window spawned on Windows."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _empty_session() -> dict:
    return {
        "active": False,
        "canvas_image_b64": None,
        "mtp": None,   # QgsMapToPixel — handles rotation + HiDPI
        "dpr": 1.0,    # device pixel ratio
        "positive_points": [],
        "negative_points": [],
        "raw_polygon_geo": [],
        "current_polygon_geo": [],
        "raster_name": "",
        "session_id": None,
    }
