from pathlib import Path

from qgis.PyQt.QtCore import pyqtSignal, Qt, QRectF, QSize
from qgis.PyQt.QtGui import QKeySequence, QPainter
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QSizePolicy, QFrame, QSlider, QShortcut
)

_LOGO_PATH = str(Path(__file__).resolve().parent / "ui" / "tracer-by-lad.svg")


class _SvgBanner(QLabel):
    """QLabel that renders an SVG scaled to its full width, maintaining aspect ratio."""

    MAX_HEIGHT = 48

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._renderer = QSvgRenderer(path)
        if self._renderer.isValid():
            s = self._renderer.defaultSize()
            h = self.MAX_HEIGHT
            w = int(s.width() * h / s.height())
            self.setFixedSize(w, h)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event):
        if self._renderer.isValid():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._renderer.render(painter, QRectF(0, 0, self.width(), self.height()))
            painter.end()


class AITracerDock(QDockWidget):
    accepted = pyqtSignal()
    cancelled = pyqtSignal()
    simplify_changed = pyqtSignal(float)
    tool_toggled = pyqtSignal(bool)   # True = activate, False = deactivate
    reset_requested = pyqtSignal()    # user pressed "Reset installation"

    def __init__(self, parent=None):
        super().__init__("AITracer by LAD", parent)
        self.setObjectName("AITracerDock")
        self._tool_active = False
        self._build_ui()
        self._setup_shortcuts()
        self.setMinimumHeight(400)

    def _build_ui(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Header: SVG banner scales to full dock width, preserving aspect ratio
        banner = _SvgBanner(_LOGO_PATH)
        banner.setToolTip("AITracer by LAD — Laboratorio di Archeologia Digitale")
        layout.addWidget(banner)

        # Activate / Deactivate button
        self._toggle_btn = QPushButton("▶  Activate")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setStyleSheet("font-weight: bold;")
        self._toggle_btn.clicked.connect(self._on_toggle_clicked)
        layout.addWidget(self._toggle_btn)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        # Simplification slider (0–10 → 0.00–0.50 map units, step 0.05)
        simplify_layout = QHBoxLayout()
        simplify_layout.addWidget(QLabel("Simplify:"))
        self._simplify_slider = QSlider(Qt.Orientation.Horizontal)
        self._simplify_slider.setRange(0, 50)
        self._simplify_slider.setValue(0)
        self._simplify_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._simplify_slider.setTickInterval(5)
        self._simplify_slider.setToolTip(
            "Douglas-Peucker simplification tolerance.\n"
            "0 = no simplification  |  50 = 0.50 map units"
        )
        self._simplify_slider.valueChanged.connect(self._on_simplify_slider)
        simplify_layout.addWidget(self._simplify_slider)
        self._simplify_value_label = QLabel("0.00")
        self._simplify_value_label.setFixedWidth(30)
        simplify_layout.addWidget(self._simplify_value_label)
        layout.addLayout(simplify_layout)

        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line2)

        # Status
        self._status_label = QLabel("Activate the tool to start segmentation.")
        self._status_label.setWordWrap(True)
        self._status_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout.addWidget(self._status_label)

        # Confidence (hidden until result arrives)
        self._confidence_label = QLabel()
        self._confidence_label.setVisible(False)
        layout.addWidget(self._confidence_label)

        # Accept / Cancel
        btn_layout = QHBoxLayout()
        self._accept_btn = QPushButton("Accept")
        self._accept_btn.setEnabled(False)
        self._accept_btn.clicked.connect(self.accepted)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self.cancelled)
        btn_layout.addWidget(self._accept_btn)
        btn_layout.addWidget(self._cancel_btn)
        layout.addLayout(btn_layout)

        # Tips
        tip = QLabel(
            "Left-click: add to selection\n"
            "Right-click: remove from selection\n"
            "Enter: accept — Escape: cancel\n"
            "Tip: keep only the raster layer visible."
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(tip)

        layout.addStretch()

        # Reset installation button (troubleshooting)
        self._reset_btn = QPushButton("⟳  Reset installation")
        self._reset_btn.setToolTip(
            "Delete the backend virtual environment and re-run first-time setup.\n"
            "Use this if the backend fails to start after an update or a failed install.\n"
            "The standalone Python (~/.aitracer/python_standalone) is kept."
        )
        self._reset_btn.setStyleSheet("color: gray; font-size: 10px;")
        self._reset_btn.setFlat(True)
        self._reset_btn.clicked.connect(self.reset_requested)
        layout.addWidget(self._reset_btn)

        # Footer links
        footer_line = QFrame()
        footer_line.setFrameShape(QFrame.Shape.HLine)
        footer_line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(footer_line)

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

    def _on_simplify_slider(self, value: int):
        tolerance = value * 0.01
        self._simplify_value_label.setText(f"{tolerance:.2f}")
        self.simplify_changed.emit(tolerance)

    def _on_toggle_clicked(self, checked: bool):
        self._tool_active = checked
        self._update_toggle_label()
        self.tool_toggled.emit(checked)

    def _update_toggle_label(self):
        if self._tool_active:
            self._toggle_btn.setText("⏹  Deactivate")
            self._toggle_btn.setStyleSheet("font-weight: bold; color: darkred;")
        else:
            self._toggle_btn.setText("▶  Activate")
            self._toggle_btn.setStyleSheet("font-weight: bold;")

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    def set_tool_active(self, active: bool):
        """Sync the toggle button state from main.py (e.g. on cancel/unload)."""
        self._tool_active = active
        self._toggle_btn.setChecked(active)
        self._update_toggle_label()

    def simplify_tolerance(self) -> float:
        return self._simplify_slider.value() * 0.01

    def set_status(self, text: str):
        self._status_label.setText(text)

    def set_confidence(self, value):
        if value is None:
            self._confidence_label.setVisible(False)
        else:
            self._confidence_label.setText(f"Confidence: {value:.0%}")
            self._confidence_label.setVisible(True)

    def set_session_active(self, active: bool):
        self._accept_btn.setEnabled(active)
        self._cancel_btn.setEnabled(active)
        self._sc_accept.setEnabled(active)
        self._sc_accept2.setEnabled(active)
        self._sc_cancel.setEnabled(active)
        if not active:
            self.set_confidence(None)
