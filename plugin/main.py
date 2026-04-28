import os
import shutil
import socket
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
from qgis.gui import QgsMapToolPan
from qgis.PyQt.QtCore import Qt, QEvent, QObject, QThread, QTimer
from qgis.PyQt.QtWidgets import QApplication, QProgressDialog

from .map_tool import SegmentationTool
from .segmentation_worker import SegmentationWorker
from .canvas_capture import capture_base64
from .geometry import geo_to_pixel, polygon_pixel_to_geo, simplify_polygon_geo
from .preview import PreviewOverlay
from .dock import AITracerDock
from . import backend_client
from . import python_downloader

PLUGIN_NAME = "AITracer by LAD"
PLUGIN_VERSION = "0.1.25"       # must match APP_VERSION in backend/app.py
TEMP_LAYER_NAME = "AITracer"
BACKEND_DIR = Path(__file__).resolve().parent / "backend"
VENV_DIR = Path.home() / ".aitracer" / "venv"  # outside QGIS-watched paths
BACKEND_PORT = 8765             # fallback; a free port is chosen at runtime
SESSION_IDLE_MS = 5 * 60 * 1000  # 5 minutes — auto-cancel idle sessions

# Windows venv uses Scripts\, Unix uses bin/
_VENV_BIN = "Scripts" if sys.platform == "win32" else "bin"
_EXE = ".exe" if sys.platform == "win32" else ""


def _venv(name: str) -> Path:
    """Return the path to an executable inside the managed venv."""
    return VENV_DIR / _VENV_BIN / (name + _EXE)


def _log(msg, level=Qgis.MessageLevel.Info):
    QgsMessageLog.logMessage(msg, PLUGIN_NAME, level)


class _DownloadThread(QThread):
    """Downloads SAM2 weights in the background so the UI stays responsive."""

    def __init__(self, python_exe: str, dest_path: str):
        super().__init__()
        self._python = python_exe
        self._dest = dest_path
        self.error: str = ""

    def run(self):
        script = (
            "import urllib.request; "
            "urllib.request.urlretrieve("
            "'https://dl.fbaipublicfiles.com/segment_anything_2/092824/"
            "sam2.1_hiera_tiny.pt',"
            f"'{self._dest}'"
            ")"
        )
        env = _clean_env()
        try:
            result = subprocess.run(
                [self._python, "-c", script],
                capture_output=True, env=env,
            )
            if result.returncode != 0:
                err = result.stderr.decode(errors="replace") or "non-zero exit"
                self.error = err[:300]
        except Exception as exc:
            self.error = str(exc)


class _UndoInterceptor(QObject):
    """Application-level event filter that intercepts Ctrl/Cmd+Z when an
    AITracer session is active.

    Installed on QApplication so it fires *before* QGIS's own undo action,
    letting us consume the event entirely and preventing the layer undo stack
    from also running.
    """

    def __init__(self, plugin: "VectorizePlugin"):
        super().__init__()
        self._plugin = plugin

    def eventFilter(self, obj, event) -> bool:
        if not self._plugin._session.get("active"):
            return False

        etype = event.type()
        if etype not in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride):
            return False

        key_z = event.key() == Qt.Key.Key_Z
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if not (key_z and ctrl):
            return False

        if etype == QEvent.Type.ShortcutOverride:
            # Claim the shortcut: Qt will deliver it as a normal KeyPress
            # instead of firing QGIS's registered undo QShortcut.
            event.accept()
            return False  # don't consume — let it become a KeyPress

        # KeyPress — run our undo and swallow the event entirely.
        self._plugin._on_undo()
        return True


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
        self._backend_port: int = BACKEND_PORT
        self._worker: SegmentationWorker | None = None
        self._worker_thread: QThread | None = None
        self._busy: bool = False   # True while a segmentation request is in flight
        # Keeps (worker, thread) pairs alive until the thread fully exits.
        # Without this, Python's GC can free the objects while Qt is still
        # using them, causing a segfault.
        self._live_threads: list = []
        self._undo_interceptor = _UndoInterceptor(self)
        QApplication.instance().installEventFilter(self._undo_interceptor)
        self._idle_timer = QTimer()
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(SESSION_IDLE_MS)
        self._idle_timer.timeout.connect(self._on_idle_timeout)

    # ------------------------------------------------------------------ #
    # QGIS plugin lifecycle                                               #
    # ------------------------------------------------------------------ #

    def initGui(self):
        self._tool = SegmentationTool(self._canvas)
        self._tool.clicked.connect(self._on_canvas_clicked)
        self._tool.accept_requested.connect(self._on_accept)
        self._tool.cancel_requested.connect(self._on_cancel)
        self._tool.undo_requested.connect(self._on_undo)

        self._overlay = PreviewOverlay(self._canvas)

        self._dock = AITracerDock(version=PLUGIN_VERSION, parent=self._iface.mainWindow())
        self._dock.accepted.connect(self._on_accept)
        self._dock.cancelled.connect(self._on_cancel)
        self._dock.simplify_changed.connect(self._on_simplify_changed)
        self._dock.tool_toggled.connect(self._toggle_tool)
        self._dock.reset_requested.connect(self._on_reset_backend)
        self._iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._dock)

        self._dock.set_status("Activate the tool to start segmentation.")

    def unload(self):
        QApplication.instance().removeEventFilter(self._undo_interceptor)
        self._on_cancel()
        # Wait briefly for any in-flight thread to finish before unloading.
        for _worker, thread in list(self._live_threads):
            thread.wait(2000)  # ms — don't block forever
        self._live_threads.clear()
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
            # Pre-select the AITracer layer in the combo if it already exists.
            self._dock.select_layer(self._find_aitracer_layer())
        else:
            self._end_session()
            self._canvas.setMapTool(self._prev_tool or QgsMapToolPan(self._canvas))
            self._dock.set_status("Deactivated. Press Activate to start.")

    # ------------------------------------------------------------------ #
    # Backend subprocess management                                       #
    # ------------------------------------------------------------------ #

    def _ensure_backend(self) -> bool:
        """Make sure the backend is running. Return True if ready."""
        if backend_client.health_check(expected_version=PLUGIN_VERSION):
            return True

        dlg = None
        if not _venv("uvicorn").exists():
            ok, dlg = self._run_setup()
            if not ok:
                return False
            # dlg is still open — _start_backend will update and close it.

        return self._start_backend(dlg=dlg)

    def _run_setup(self) -> tuple:
        """Download standalone Python, create venv, install requirements and
        model weights.

        Returns (True, dlg) on success — the dialog is intentionally left open
        so _start_backend() can reuse it for the model-load wait phase.
        Returns (False, None) on failure — the dialog has already been closed.
        """
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
        def _progress(pct, msg):
            dlg.setLabelText(msg)
            QApplication.processEvents()

        def _install_python() -> bool:
            """Download and install standalone Python. Returns True on success."""
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
            return True

        if not python_downloader.is_installed():
            if not _install_python():
                return False, None
        else:
            # Python appears installed — verify it actually runs.
            # A stale installation (wrong version, antivirus quarantine, etc.)
            # would cause a cryptic FileNotFoundError in the subprocess step;
            # catching it here gives a clear message and auto-reinstalls.
            ok, vmsg = python_downloader.verify()
            if not ok:
                _log(
                    f"Standalone Python is broken ({vmsg}). "
                    "Removing and reinstalling…",
                    Qgis.MessageLevel.Warning,
                )
                shutil.rmtree(python_downloader.STANDALONE_DIR, ignore_errors=True)
                if not _install_python():
                    return False, None

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
        # Use "python -m pip" everywhere instead of the pip[.exe] script.
        # On Windows, ensurepip may only create pip3.exe / pip3.12.exe in
        # Scripts\ — pip.exe is not guaranteed. python -m pip always works
        # as long as pip is installed in the venv.
        venv_python = str(_venv("python"))
        if sys.platform == "win32":
            steps = [
                ([python, "-m", "venv", "--without-pip", str(VENV_DIR)],
                 "Creating virtual environment…", None),
                ([venv_python, "-m", "ensurepip", "--upgrade"],
                 "Bootstrapping pip…", None),
                ([venv_python, "-m", "pip", "install", "--upgrade", "pip"],
                 "Upgrading pip…", None),
                ([venv_python, "-m", "pip", "install", "-r",
                  str(BACKEND_DIR / "requirements.txt")],
                 "Installing dependencies (this may take several minutes)…",
                 str(BACKEND_DIR)),
            ]
        else:
            steps = [
                ([python, "-m", "venv", str(VENV_DIR)],
                 "Creating virtual environment…", None),
                ([venv_python, "-m", "pip", "install", "--upgrade", "pip"],
                 "Upgrading pip…", None),
                ([venv_python, "-m", "pip", "install", "-r",
                  str(BACKEND_DIR / "requirements.txt")],
                 "Installing dependencies (this may take several minutes)…",
                 str(BACKEND_DIR)),
            ]

        for cmd, label, cwd in steps:
            if dlg.wasCanceled():
                dlg.close()
                return False, None
            _log(f"Running: {' '.join(str(c) for c in cmd)}")
            dlg.setLabelText(label)
            QApplication.processEvents()

            run_kwargs: dict = {"capture_output": True, "env": _clean_env()}
            if cwd:
                run_kwargs["cwd"] = cwd
            if sys.platform == "win32":
                run_kwargs["startupinfo"] = _win_startupinfo()

            try:
                result = subprocess.run(cmd, **run_kwargs)
            except FileNotFoundError as exc:
                dlg.close()
                msg = f"Executable not found during '{label}': {exc}\nCommand: {cmd[0]}"
                _log(msg, Qgis.MessageLevel.Critical)
                self._iface.messageBar().pushMessage(
                    PLUGIN_NAME, msg,
                    level=Qgis.MessageLevel.Critical, duration=15,
                )
                return False, None

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
                return False, None

        # ── Step 3: model weights ──────────────────────────────────────────
        weights_dir = BACKEND_DIR / "weights"
        weights_dir.mkdir(exist_ok=True)
        checkpoint = weights_dir / "sam2.1_hiera_tiny.pt"
        if not checkpoint.exists():
            if dlg.wasCanceled():
                dlg.close()
                return False, None
            dlg.setLabelText("Downloading SAM2-tiny weights (~40 MB)…")
            QApplication.processEvents()

            # Run the download in a background thread so the modal dialog
            # keeps updating and processEvents() continues to fire. This
            # prevents Qt from queuing up user events during the download
            # that would later be replayed unexpectedly during backend startup.
            dl_thread = _DownloadThread(
                str(_venv("python")), str(checkpoint)
            )
            dl_thread.start()
            while dl_thread.isRunning():
                if dlg.wasCanceled():
                    dl_thread.wait(3000)
                    dlg.close()
                    return False, None
                QApplication.processEvents()
                dl_thread.wait(200)   # wake every 200ms to update UI

            if dl_thread.error:
                dlg.close()
                self._iface.messageBar().pushMessage(
                    PLUGIN_NAME,
                    f"Failed to download model weights: {dl_thread.error}",
                    level=Qgis.MessageLevel.Critical, duration=10,
                )
                return False, None

        # Leave dialog open — _start_backend() will update label and close it.
        dlg.setLabelText("Starting AI backend…")
        QApplication.processEvents()
        return True, dlg

    def _start_backend(self, dlg=None) -> bool:
        """Launch the uvicorn subprocess and wait up to 60s for it to be ready.

        *dlg* is an optional QProgressDialog passed from _run_setup() so the
        setup dialog stays visible during the model-load wait instead of
        disappearing and leaving QGIS looking frozen.  The dialog is closed
        (success or failure) before this method returns.
        """
        self._backend_port = _find_free_port()
        backend_client.set_port(self._backend_port)

        uvicorn = _venv("uvicorn")
        log_file = VENV_DIR.parent / "backend.log"

        # If no dialog was passed (backend restart without fresh setup),
        # create a lightweight one so the user sees progress.
        owned_dlg = False
        if dlg is None:
            dlg = QProgressDialog(
                "Starting AI backend…", None, 0, 0,
                self._iface.mainWindow()
            )
            dlg.setWindowTitle("AITracer")
            dlg.setMinimumWidth(340)
            dlg.setModal(True)
            dlg.show()

        def _close_dlg():
            try:
                dlg.close()
            except Exception:
                pass

        popen_kw: dict = {
            "cwd": str(BACKEND_DIR),
            "stdout": open(log_file, "w"),
            "stderr": subprocess.STDOUT,
            "env": _clean_env(),
        }
        if sys.platform == "win32":
            popen_kw["startupinfo"] = _win_startupinfo()
        self._backend_log = popen_kw["stdout"]
        try:
            self._backend_proc = subprocess.Popen(
                [str(uvicorn), "app:app",
                 "--port", str(self._backend_port),
                 "--log-level", "info"],
                **popen_kw,
            )
        except OSError as exc:
            _close_dlg()
            _log(f"Failed to launch backend: {exc}", Qgis.MessageLevel.Critical)
            self._iface.messageBar().pushMessage(
                PLUGIN_NAME, f"Could not start backend: {exc}",
                level=Qgis.MessageLevel.Critical, duration=10
            )
            return False

        _log(f"Backend started on port {self._backend_port} (log: {log_file}), "
             "waiting for readiness…")

        elapsed = 0
        deadline = time.time() + 60  # SAM2 model load can be slow
        while time.time() < deadline:
            elapsed = int(time.time() - (deadline - 60))
            dlg.setLabelText(
                f"Loading AI model… ({elapsed}s)\n"
                "This may take up to a minute on first run."
            )
            QApplication.processEvents()  # keep QGIS UI responsive during startup
            # processEvents() can dispatch _stop_backend() if the user
            # deactivates while we are waiting — bail out cleanly.
            if self._backend_proc is None:
                _close_dlg()
                return False
            if backend_client.health_check(expected_version=PLUGIN_VERSION):
                _log("Backend ready.")
                _close_dlg()
                return True
            if self._backend_proc.poll() is not None:
                # Process exited — read log and report
                self._backend_log.flush()
                try:
                    log_tail = log_file.read_text()[-600:]
                except Exception:
                    log_tail = "(could not read log)"
                _close_dlg()
                _log(f"Backend crashed:\n{log_tail}", Qgis.MessageLevel.Critical)
                self._iface.messageBar().pushMessage(
                    PLUGIN_NAME,
                    f"Backend crashed on startup. See Log Messages → AITracer.\n{log_tail[:200]}",
                    level=Qgis.MessageLevel.Critical, duration=15
                )
                return False
            time.sleep(0.5)

        _close_dlg()
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

    def _on_reset_backend(self):
        """Delete the venv and re-run first-time setup on next activation.

        Triggered by the 'Reset installation' button in the dock.
        The standalone Python (~/.aitracer/python_standalone) is preserved
        so only the pip packages need to be reinstalled.
        """
        self._on_cancel()
        self._stop_backend()
        if VENV_DIR.exists():
            shutil.rmtree(VENV_DIR)
            _log("Venv removed by user request.")
        # Collapse the panel and restore the Activate button so the user
        # knows they need to click Activate to trigger re-installation.
        self._dock.set_tool_active(False)
        self._dock.set_status(
            "Installation reset. Press Activate to reinstall."
        )

    # ------------------------------------------------------------------ #
    # Temp layer management                                               #
    # ------------------------------------------------------------------ #

    def _find_aitracer_layer(self):
        """Return the existing AITracer memory layer, or None."""
        for layer in QgsProject.instance().mapLayersByName(TEMP_LAYER_NAME):
            is_poly_mem = (isinstance(layer, QgsVectorLayer)
                           and layer.geometryType() == Qgis.GeometryType.Polygon
                           and layer.dataProvider().name() == "memory")
            if is_poly_mem:
                return layer
        return None

    def _get_or_create_layer(self):
        existing = self._find_aitracer_layer()
        if existing:
            return existing
        project = QgsProject.instance()

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
        if self._dock is None:
            return   # plugin is being unloaded — ignore stale events
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
            self._push_status_bar(
                "AITracer session active — Enter: accept  |  Esc: cancel  |  Ctrl+Z: undo"
            )

        self._reset_idle_timer()
        px, py = geo_to_pixel(
            point,
            self._session["mtp"],
            self._session["dpr"],
        )
        self._session["prompt_history"].append((is_negative, [px, py]))
        if is_negative:
            self._session["negative_points"].append([px, py])
        else:
            self._session["positive_points"].append([px, py])

        self._run_segmentation()

    def _run_segmentation(self):
        """Fire an async segmentation request. Returns immediately."""
        # Discard any in-flight request (its result will be ignored).
        self._discard_worker()

        # Show busy cursor exactly once — _busy guards against stacking.
        if not self._busy:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            self._busy = True
        self._dock.set_status("Segmenting…")

        worker = SegmentationWorker(
            image_b64=(self._session["canvas_image_b64"]
                       if not self._session["session_id"] else None),
            positive_points=list(self._session["positive_points"]),
            negative_points=list(self._session["negative_points"]),
            session_id=self._session["session_id"],
        )
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_segment_done)
        worker.failed.connect(self._on_segment_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        # Register in _live_threads BEFORE start() — guarantees a Python
        # reference exists for the entire thread lifetime, preventing GC.
        pair = (worker, thread)
        self._live_threads.append(pair)
        thread.finished.connect(lambda p=pair: self._live_threads.remove(p)
                                if p in self._live_threads else None)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._worker = worker
        self._worker_thread = thread
        thread.start()

    def _discard_worker(self):
        """Disconnect result signals from the current in-flight worker.

        The underlying thread keeps running to completion and self-cleans up
        via deleteLater — we just stop listening to its result.
        """
        if self._worker is not None:
            try:
                self._worker.finished.disconnect(self._on_segment_done)
                self._worker.failed.disconnect(self._on_segment_failed)
            except RuntimeError:
                pass  # already disconnected
        self._worker = None
        self._worker_thread = None

    def _restore_cursor(self):
        """Restore the wait cursor if we set one."""
        if self._busy:
            QApplication.restoreOverrideCursor()
            self._busy = False

    # ---- async result slots (called on the main thread by Qt) ----

    def _on_segment_done(self, result: dict):
        self._restore_cursor()
        # (_live_threads cleanup happens via thread.finished signal)

        # Guard: session may have been cancelled while the request was in flight.
        if not self._session["active"]:
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

    def _on_segment_failed(self, msg: str):
        self._restore_cursor()
        # (_live_threads cleanup happens via thread.finished signal)

        if not self._session["active"]:
            return

        self._iface.messageBar().pushMessage(
            PLUGIN_NAME, msg,
            level=Qgis.MessageLevel.Warning, duration=5
        )
        self._dock.set_status("Segmentation failed. Try again.")

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

    def _on_undo(self):
        """Remove the most recently added prompt point and re-segment.

        Uses prompt_history (flat, chronological) so that undo always
        removes the last click regardless of whether it was positive or
        negative — matching the user's expected Cmd/Ctrl+Z behaviour.
        """
        if not self._session["active"]:
            return
        if not self._session["prompt_history"]:
            return

        self._reset_idle_timer()
        self._session["prompt_history"].pop()

        # Rebuild the typed lists from the remaining history.
        self._session["positive_points"] = [
            p for neg, p in self._session["prompt_history"] if not neg
        ]
        self._session["negative_points"] = [
            p for neg, p in self._session["prompt_history"] if neg
        ]

        if not self._session["prompt_history"]:
            # No points left — clear preview, keep session alive so the
            # next click reuses the cached image embedding on the backend.
            self._restore_cursor()
            self._discard_worker()
            self._session["raw_polygon_geo"] = []
            self._session["current_polygon_geo"] = []
            self._overlay.clear()
            self._dock.set_confidence(None)
            self._dock.set_status("All points removed. Left-click to start again.")
            return

        self._dock.set_status("Point removed. Re-segmenting…")
        self._run_segmentation()

    def _on_accept(self):
        polygon = self._session.get("current_polygon_geo")
        if not polygon:
            return
        # Use the dock's layer combo: None means "auto-create AITracer layer".
        layer = self._dock.selected_layer() or self._get_or_create_layer()
        if layer is None:
            return
        self._insert_feature(polygon, layer)
        # Keep the combo pointing at the output layer for subsequent accepts.
        self._dock.select_layer(layer)
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

    # ------------------------------------------------------------------ #
    # Idle timeout                                                        #
    # ------------------------------------------------------------------ #

    def _reset_idle_timer(self):
        """Restart the 5-minute idle countdown after every user interaction."""
        self._idle_timer.start()

    def _clear_idle_timer(self):
        self._idle_timer.stop()

    def _on_idle_timeout(self):
        if not self._session.get("active"):
            return
        _log("Session idle for 5 minutes — auto-cancelling.")
        self._iface.messageBar().pushMessage(
            PLUGIN_NAME,
            "AITracer session cancelled after 5 minutes of inactivity.",
            level=Qgis.MessageLevel.Info, duration=6,
        )
        self._on_cancel()

    # ------------------------------------------------------------------ #
    # Status bar                                                          #
    # ------------------------------------------------------------------ #

    def _push_status_bar(self, text: str):
        """Show a persistent message in the QGIS status bar."""
        self._iface.statusBarIface().showMessage(text)

    def _clear_status_bar(self):
        self._iface.statusBarIface().clearMessage()

    def _end_session(self):
        self._restore_cursor()
        self._discard_worker()
        self._clear_idle_timer()
        self._clear_status_bar()
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

        fields = layer.fields()
        feature = QgsFeature(fields)
        feature.setGeometry(QgsGeometry.fromPolygonXY([polygon_geo]))

        # Only set AITracer-specific attributes when the target layer has them
        # (they exist on the auto-created memory layer but not on user layers).
        if fields.indexOf("fid") >= 0:
            existing_fids = [f["fid"] for f in layer.getFeatures() if f["fid"]]
            feature.setAttribute("fid", max(existing_fids, default=0) + 1)
        if fields.indexOf("timestamp") >= 0:
            feature.setAttribute("timestamp",
                                 datetime.now().isoformat(timespec="seconds"))
        if fields.indexOf("raster") >= 0:
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


def _find_free_port() -> int:
    """Return an available TCP port on localhost.

    Uses the OS ephemeral-port mechanism: bind to port 0, let the kernel
    choose a free port, record it, then immediately release the socket.
    The port is not reserved, but the window between release and uvicorn
    binding is negligible on a local machine.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _empty_session() -> dict:
    return {
        "active": False,
        "canvas_image_b64": None,
        "mtp": None,   # QgsMapToPixel — handles rotation + HiDPI
        "dpr": 1.0,    # device pixel ratio
        # prompt_history is the single source of truth for click order:
        # list of (is_negative: bool, [x, y]) in chronological order.
        # positive_points and negative_points are derived from it.
        "prompt_history": [],
        "positive_points": [],
        "negative_points": [],
        "raw_polygon_geo": [],
        "current_polygon_geo": [],
        "raster_name": "",
        "session_id": None,
    }
