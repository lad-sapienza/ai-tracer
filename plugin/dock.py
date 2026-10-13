import logging
from pathlib import Path

from qgis.core import QgsMapLayerProxyModel
from qgis.gui import QgsCollapsibleGroupBox, QgsGui, QgsMapLayerComboBox
from qgis.PyQt.QtCore import pyqtSignal, Qt, QRectF, QSize
from qgis.PyQt.QtGui import QKeySequence, QPainter
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox,
    QPushButton, QLabel, QSizePolicy, QFrame, QSlider, QShortcut
)

try:
    from qgis.core import NULL
except ImportError:          # pragma: no cover — older/newer bindings
    NULL = None

_logger = logging.getLogger(__name__)

_LOGO_PATH = str(Path(__file__).resolve().parent / "ui" / "tracer-by-lad.svg")

# Fields the plugin fills automatically on insert (see main._insert_feature);
# they are never shown in the field-values panel.
AUTO_FIELDS = {"fid", "timestamp", "raster"}

# Simplification slider: 0–SIMPLIFY_STEPS maps quadratically to 0–SIMPLIFY_MAX_PX
# screen pixels, so the fine range (0.5–2 px, where the vertex count drops
# fastest) gets most of the slider travel. Pixels, not map units, so the
# slider means the same thing at any zoom level or CRS.
SIMPLIFY_STEPS = 100
SIMPLIFY_MAX_PX = 4.0
SIMPLIFY_DEFAULT_STEP = 42      # ≈ 0.7 px: follows the edge closely


def simplify_px(step: int) -> float:
    return SIMPLIFY_MAX_PX * (step / SIMPLIFY_STEPS) ** 2


def _is_unset(value) -> bool:
    """True when an editor widget holds no usable value (NULL or empty)."""
    if value is None or value == NULL:
        return True
    return isinstance(value, str) and value == ""


class _SvgBanner(QLabel):
    """Renders an SVG at its natural proportional size, centred horizontally."""

    MAX_HEIGHT = 56

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._renderer = QSvgRenderer(path)
        if self._renderer.isValid():
            s = self._renderer.defaultSize()
            if s.height() > 0:
                h = self.MAX_HEIGHT
                w = int(s.width() * h / s.height())
                self._render_size = QSize(w, h)
            else:
                self._render_size = QSize(self.MAX_HEIGHT, self.MAX_HEIGHT)
        else:
            self._render_size = QSize(self.MAX_HEIGHT, self.MAX_HEIGHT)
        self.setFixedHeight(self._render_size.height())
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event):
        if self._renderer.isValid():
            rw = self._render_size.width()
            rh = self._render_size.height()
            x = (self.width() - rw) / 2   # centre horizontally
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._renderer.render(painter, QRectF(x, 0, rw, rh))
            painter.end()


class AITracerDock(QDockWidget):
    accepted = pyqtSignal()
    cancelled = pyqtSignal()
    simplify_changed = pyqtSignal(float)
    tool_toggled = pyqtSignal(bool)   # True = activate, False = deactivate
    reset_requested = pyqtSignal()    # user pressed "Reset installation"
    resume_requested = pyqtSignal()   # user pressed "Resume" while paused
    edit_last_requested = pyqtSignal()  # user pressed "Edit last polygon"
    model_selected = pyqtSignal(str)  # user picked a model key in the combo
    layer_changed = pyqtSignal(object)  # emits the selected QgsVectorLayer or None

    def __init__(self, version: str = "", parent=None):
        super().__init__("AITracer by LAD", parent)
        self.setObjectName("AITracerDock")
        self._tool_active = False
        self._paused = False
        self._session_active = False
        self._version = version
        self._build_ui()
        self._setup_shortcuts()
        self.setMinimumHeight(400)

    def _build_ui(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Always-visible header ─────────────────────────────────────────
        banner = _SvgBanner(_LOGO_PATH)
        banner.setToolTip("AITracer by LAD — Laboratorio di Archeologia Digitale")
        layout.addWidget(banner)

        self._toggle_btn = QPushButton("▶  Activate")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.clicked.connect(self._on_toggle_clicked)
        self._update_toggle_label()
        layout.addWidget(self._toggle_btn)

        # Shown only while paused (another QGIS map tool is active), so the
        # user can still deactivate without first resuming.
        self._stop_btn = QPushButton("⏹  Deactivate")
        self._stop_btn.setStyleSheet("color: darkred;")
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        self._stop_btn.setVisible(False)
        layout.addWidget(self._stop_btn)

        # ── Collapsible controls (hidden until tool is activated) ─────────
        self._controls = QWidget()
        ctrl = QVBoxLayout(self._controls)
        ctrl.setContentsMargins(0, 0, 0, 0)
        ctrl.setSpacing(6)

        ctrl.addWidget(_hline())

        # Segmentation model
        model_row = QWidget()
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.addWidget(QLabel("Model:"))
        self._model_combo = QComboBox()
        self._model_combo.setToolTip(
            "SAM2.1 model used for segmentation.\n"
            "Larger models are more accurate but heavier; they are\n"
            "downloaded the first time you select them."
        )
        self._model_combo.activated.connect(self._on_model_activated)
        model_layout.addWidget(self._model_combo, 1)
        ctrl.addWidget(model_row)

        # Simplification slider (0–50 → 0.00–0.50 map units)
        simplify_row = QWidget()
        simplify_layout = QHBoxLayout(simplify_row)
        simplify_layout.setContentsMargins(0, 0, 0, 0)
        simplify_layout.addWidget(QLabel("Simplify:"))
        self._simplify_slider = QSlider(Qt.Orientation.Horizontal)
        self._simplify_slider.setRange(0, SIMPLIFY_STEPS)
        self._simplify_slider.setValue(SIMPLIFY_DEFAULT_STEP)
        self._simplify_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._simplify_slider.setTickInterval(10)
        self._simplify_slider.setToolTip(
            "Douglas-Peucker simplification tolerance, in screen pixels.\n"
            "0 = every contour pixel  |  fine control below 2 px\n"
            "Independent of zoom level and CRS."
        )
        self._simplify_slider.valueChanged.connect(self._on_simplify_slider)
        simplify_layout.addWidget(self._simplify_slider)
        self._vertex_count = None
        self._simplify_value_label = QLabel()
        self._simplify_value_label.setMinimumWidth(80)
        self._refresh_simplify_label()
        simplify_layout.addWidget(self._simplify_value_label)
        ctrl.addWidget(simplify_row)

        ctrl.addWidget(_hline())

        # Output layer selector
        ctrl.addWidget(QLabel("Output layer (blank = new AITracer layer):"))
        self._layer_combo = QgsMapLayerComboBox()
        _poly = getattr(
            QgsMapLayerProxyModel,
            "PolygonLayer",
            getattr(getattr(QgsMapLayerProxyModel, "Filter", None), "PolygonLayer", None),
        )
        if _poly is not None:
            self._layer_combo.setFilters(_poly)
        self._layer_combo.setAllowEmptyLayer(True)
        self._layer_combo.setCurrentIndex(0)
        self._layer_combo.setToolTip(
            "Layer where accepted polygons will be added.\n"
            "Choose an existing polygon layer or leave blank\n"
            "to auto-create a new AITracer layer."
        )
        self._layer_combo.layerChanged.connect(self._on_layer_changed)
        ctrl.addWidget(self._layer_combo)

        # Field values for new polygons (rebuilt whenever the layer changes)
        self._fields_group = QgsCollapsibleGroupBox("Field values")
        self._fields_group.setToolTip(
            "Values applied to every accepted polygon, until you change them.\n"
            "Fields left empty fall back to the layer's default values."
        )
        self._fields_form = QFormLayout(self._fields_group)
        self._fields_form.setContentsMargins(6, 6, 6, 6)
        self._fields_group.setVisible(False)
        ctrl.addWidget(self._fields_group)

        self._field_wrappers = []      # [(field_name, QgsEditorWidgetWrapper)]
        self._fields_layer = None
        self._saved_field_values = {}  # layer_id -> {field_name: value}

        ctrl.addWidget(_hline())

        # Status
        self._status_label = QLabel("Left-click on the raster to start.")
        self._status_label.setWordWrap(True)
        self._status_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        ctrl.addWidget(self._status_label)

        # Confidence (hidden until a result arrives)
        self._confidence_label = QLabel()
        self._confidence_label.setVisible(False)
        ctrl.addWidget(self._confidence_label)

        # Accept / Cancel
        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        self._accept_btn = QPushButton("Accept")
        self._accept_btn.setEnabled(False)
        self._accept_btn.clicked.connect(self.accepted)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self.cancelled)
        btn_layout.addWidget(self._accept_btn)
        btn_layout.addWidget(self._cancel_btn)
        ctrl.addWidget(btn_row)

        # Hands the last accepted polygon over to QGIS's native vertex tool.
        self._edit_last_btn = QPushButton("✎  Edit last polygon (E)")
        self._edit_last_btn.setToolTip(
            "Select the last accepted polygon and switch to QGIS's vertex tool "
            "(shortcut: E). Press Resume afterwards to continue tracing."
        )
        self._edit_last_btn.setEnabled(False)
        self._edit_last_btn.clicked.connect(self.edit_last_requested)
        ctrl.addWidget(self._edit_last_btn)

        # Tips
        tip = QLabel(
            "Left-click: add to selection\n"
            "Right-click: remove from selection\n"
            "Ctrl+Z: undo last point\n"
            "Enter: accept — Escape: cancel\n"
            "E: edit last accepted polygon\n"
            "Tip: keep only the raster layer visible."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray; font-size: 10px;")
        ctrl.addWidget(tip)

        ctrl.addStretch()

        ctrl.addWidget(_hline())

        # Reset installation (troubleshooting)
        self._reset_btn = QPushButton("⟳  Reset installation")
        self._reset_btn.setToolTip(
            "Delete the backend virtual environment and re-run first-time setup.\n"
            "Use this if the backend fails to start after an update or a failed install.\n"
            "The standalone Python (~/.aitracer/python_standalone) is kept."
        )
        self._reset_btn.setStyleSheet(
            "QPushButton { color: darkred; font-weight: bold; "
            "border: 1px solid darkred; border-radius: 3px; padding: 4px 8px; }"
            "QPushButton:hover { background-color: #ffeaea; }"
            "QPushButton:pressed { background-color: #ffcccc; }"
        )
        self._reset_btn.clicked.connect(self.reset_requested)
        ctrl.addWidget(self._reset_btn)

        self._controls.setVisible(False)
        layout.addWidget(self._controls)

        # ── Always-visible footer ─────────────────────────────────────────
        layout.addStretch()

        layout.addWidget(_hline())

        footer_layout = QHBoxLayout()
        footer_layout.setSpacing(12)
        for label, url in [
            ("GitHub", "https://github.com/lad-sapienza/ai-tracer"),
            ("Issues", "https://github.com/lad-sapienza/ai-tracer/issues"),
            ("Info", "https://lad.saras.uniroma1.it/blog/ai-tracer"),
        ]:
            lnk = QLabel(f'<a href="{url}">{label}</a>')
            lnk.setOpenExternalLinks(True)
            lnk.setStyleSheet("font-size: 10px;")
            footer_layout.addWidget(lnk)
        footer_layout.addStretch()
        if self._version:
            ver_lbl = QLabel(f"AITracer by LAD v{self._version}")
            ver_lbl.setStyleSheet("color: gray; font-size: 10px;")
            ver_lbl.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            footer_layout.addWidget(ver_lbl)
        layout.addLayout(footer_layout)

        self.setWidget(container)

    def _setup_shortcuts(self):
        self._sc_accept = QShortcut(QKeySequence(Qt.Key.Key_Return), self)
        self._sc_accept.setContext(Qt.ShortcutContext.WindowShortcut)
        self._sc_accept.setEnabled(False)
        self._sc_accept.activated.connect(self.accepted)

        self._sc_accept2 = QShortcut(QKeySequence(Qt.Key.Key_Enter), self)
        self._sc_accept2.setContext(Qt.ShortcutContext.WindowShortcut)
        self._sc_accept2.setEnabled(False)
        self._sc_accept2.activated.connect(self.accepted)

        self._sc_cancel = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._sc_cancel.setContext(Qt.ShortcutContext.WindowShortcut)
        self._sc_cancel.setEnabled(False)
        self._sc_cancel.activated.connect(self.cancelled)

        # E = edit last polygon. Enabled only while the button is usable and
        # no segmentation session is running, so a stray key press can't
        # discard a preview. Text fields keep receiving the letter normally
        # (Qt lets editing widgets claim printable keys before shortcuts).
        self._sc_edit = QShortcut(QKeySequence(Qt.Key.Key_E), self)
        self._sc_edit.setContext(Qt.ShortcutContext.WindowShortcut)
        self._sc_edit.setEnabled(False)
        self._sc_edit.activated.connect(self.edit_last_requested)

    def _update_edit_shortcut(self):
        self._sc_edit.setEnabled(
            self._edit_last_btn.isEnabled()
            and self._tool_active
            and not self._session_active
        )

    # ------------------------------------------------------------------ #
    # Private slots                                                        #
    # ------------------------------------------------------------------ #

    def _on_simplify_slider(self, value: int):
        self._refresh_simplify_label()
        self.simplify_changed.emit(simplify_px(value))

    def _refresh_simplify_label(self):
        text = f"{self.simplify_tolerance():.1f} px"
        if self._vertex_count is not None:
            text += f" · {self._vertex_count} pts"
        self._simplify_value_label.setText(text)

    def _on_toggle_clicked(self, checked: bool):
        if self._paused:
            # The button is checkable; keep it checked while paused.
            self._toggle_btn.setChecked(True)
            self.resume_requested.emit()
            return
        self._tool_active = checked
        self._update_toggle_label()
        self._update_edit_shortcut()
        self.tool_toggled.emit(checked)

    def _on_stop_clicked(self):
        self.set_paused(False)
        self.set_tool_active(False)
        self.tool_toggled.emit(False)

    def _update_toggle_label(self):
        if self._paused:
            self._toggle_btn.setText("▶  Resume")
            self._toggle_btn.setStyleSheet("font-weight: bold; color: darkgreen;")
        elif self._tool_active:
            self._toggle_btn.setText("⏹  Deactivate")
            self._toggle_btn.setStyleSheet("font-weight: bold; color: darkred;")
        else:
            self._toggle_btn.setText("▶  Activate")
            self._toggle_btn.setStyleSheet("font-weight: bold; color: darkgreen;")
        if hasattr(self, "_controls"):
            self._controls.setVisible(self._tool_active)
        if hasattr(self, "_stop_btn"):
            self._stop_btn.setVisible(self._paused)

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    def set_tool_active(self, active: bool):
        """Sync the toggle button state from main.py (e.g. on cancel/unload)."""
        self._tool_active = active
        if not active:
            self._paused = False
        self._toggle_btn.setChecked(active)
        self._update_edit_shortcut()
        self._update_toggle_label()

    def set_models(self, options: list, current: str):
        """Fill the model combo with [(key, label), ...] and select *current*."""
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for key, label in options:
            self._model_combo.addItem(label, key)
        self._model_combo.blockSignals(False)
        self.set_model(current)

    def set_model(self, key: str):
        """Select *key* without emitting model_selected."""
        idx = self._model_combo.findData(key)
        if idx >= 0:
            self._model_combo.blockSignals(True)
            self._model_combo.setCurrentIndex(idx)
            self._model_combo.blockSignals(False)

    def _on_model_activated(self, index: int):
        key = self._model_combo.itemData(index)
        if key:
            self.model_selected.emit(key)

    def set_edit_last_enabled(self, enabled: bool):
        self._edit_last_btn.setEnabled(enabled)
        self._update_edit_shortcut()

    def is_tool_active(self) -> bool:
        return self._tool_active

    def is_paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool):
        """Paused = tool is 'on' but another QGIS map tool is currently set."""
        self._paused = paused and self._tool_active
        self._update_toggle_label()

    def simplify_tolerance(self) -> float:
        """Current tolerance in screen pixels."""
        return simplify_px(self._simplify_slider.value())

    def simplify_step(self) -> int:
        return self._simplify_slider.value()

    def set_simplify_step(self, step: int):
        """Restore a saved slider position without emitting simplify_changed."""
        self._simplify_slider.blockSignals(True)
        self._simplify_slider.setValue(
            max(0, min(SIMPLIFY_STEPS, int(step)))
        )
        self._simplify_slider.blockSignals(False)
        self._refresh_simplify_label()

    def set_vertex_count(self, count):
        """Show the vertex count of the current preview (None to hide)."""
        self._vertex_count = count
        self._refresh_simplify_label()

    def set_status(self, text: str):
        self._status_label.setText(text)

    def set_confidence(self, value):
        if value is None:
            self._confidence_label.setVisible(False)
        else:
            self._confidence_label.setText(f"Confidence: {value:.0%}")
            self._confidence_label.setVisible(True)

    # ------------------------------------------------------------------ #
    # Field-values panel                                                  #
    # ------------------------------------------------------------------ #

    def _on_layer_changed(self, layer):
        self._rebuild_fields_panel(layer)
        self.layer_changed.emit(layer)

    def _rebuild_fields_panel(self, layer):
        """Rebuild the per-field editor widgets for *layer*.

        Uses the editor widgets configured in the layer (value maps, ranges,
        …) via the registry. Values entered by the user are remembered per
        layer id and restored when the layer is selected again.
        """
        self._save_field_values()
        self._disconnect_fields_layer()
        while self._fields_form.rowCount():
            self._fields_form.removeRow(0)
        self._field_wrappers = []

        if layer is None:
            self._fields_group.setVisible(False)
            return

        cfg = layer.editFormConfig()
        saved = self._saved_field_values.get(layer.id(), {})
        for idx, field in enumerate(layer.fields()):
            name = field.name()
            if name in AUTO_FIELDS:
                continue
            if layer.editorWidgetSetup(idx).type() == "Hidden":
                continue
            if cfg.readOnly(idx):
                continue
            try:
                wrapper = QgsGui.editorWidgetRegistry().create(
                    layer, idx, None, self._fields_group
                )
            except Exception:
                wrapper = None
            if wrapper is None or wrapper.widget() is None:
                continue
            wrapper.setEnabled(True)
            # Start from NULL (not the widget's natural zero/first-item state)
            # so untouched fields fall back to the layer's default values.
            try:
                wrapper.setValue(NULL)
            except Exception as exc:
                _logger.debug("Could not reset field '%s': %s", name, exc)
            if name in saved:
                wrapper.setValue(saved[name])
            default = field.defaultValueDefinition()
            if default.isValid() and default.expression():
                wrapper.widget().setToolTip(
                    f"Default when left empty: {default.expression()}"
                )
            self._fields_form.addRow(field.alias() or name, wrapper.widget())
            self._field_wrappers.append((name, wrapper))

        self._fields_layer = layer
        layer.updatedFields.connect(self._on_fields_updated)
        self._fields_group.setVisible(bool(self._field_wrappers))

    def _on_fields_updated(self):
        self._rebuild_fields_panel(self._fields_layer)

    def _disconnect_fields_layer(self):
        if self._fields_layer is not None:
            try:
                self._fields_layer.updatedFields.disconnect(
                    self._on_fields_updated
                )
            except (RuntimeError, TypeError):
                pass  # layer already deleted, or never connected
        self._fields_layer = None

    def _save_field_values(self):
        if self._fields_layer is None or not self._field_wrappers:
            return
        try:
            layer_id = self._fields_layer.id()
        except RuntimeError:        # underlying C++ layer deleted
            return
        self._saved_field_values[layer_id] = self.field_values()

    def field_values(self) -> dict:
        """Values entered in the field panel: {field_name: value}.

        Fields left empty are omitted, so the layer's default values apply.
        """
        out = {}
        for name, wrapper in self._field_wrappers:
            try:
                value = wrapper.value()
            except Exception as exc:
                _logger.debug("Could not read field '%s': %s", name, exc)
                continue
            if not _is_unset(value):
                out[name] = value
        return out

    def selected_layer(self):
        """Return the chosen output QgsVectorLayer, or None for 'new layer'."""
        return self._layer_combo.currentLayer()

    def select_layer(self, layer):
        """Pre-select *layer* in the output combo. Pass None for the blank entry."""
        if layer is None:
            self._layer_combo.setCurrentIndex(0)
        else:
            self._layer_combo.setLayer(layer)

    def set_session_active(self, active: bool):
        self._session_active = active
        self._update_edit_shortcut()
        self._accept_btn.setEnabled(active)
        self._cancel_btn.setEnabled(active)
        self._sc_accept.setEnabled(active)
        self._sc_accept2.setEnabled(active)
        self._sc_cancel.setEnabled(active)
        if not active:
            self.set_confidence(None)


# ------------------------------------------------------------------ #
# Helper                                                              #
# ------------------------------------------------------------------ #

def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f
