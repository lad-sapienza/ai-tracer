from qgis.core import QgsPointXY
from qgis.gui import QgsMapCanvas, QgsMapTool
from qgis.PyQt.QtCore import pyqtSignal, Qt


class SegmentationTool(QgsMapTool):
    """Map tool that captures clicks and emits them as segmentation prompts.

    Left-click   → positive prompt (add to polygon)
    Right-click  → negative prompt (remove from polygon)

    While a session is active the tool suppresses pan, zoom, and the
    default context menu so that the locked canvas extent stays valid.
    """

    clicked = pyqtSignal(QgsPointXY, bool)  # (point_in_map_crs, is_negative)
    accept_requested = pyqtSignal()         # Enter / Return
    cancel_requested = pyqtSignal()         # Escape
    undo_requested = pyqtSignal()           # Ctrl+Z

    def __init__(self, canvas: QgsMapCanvas):
        super().__init__(canvas)
        self._session_active = False

    def set_session_active(self, active: bool):
        self._session_active = active

    def canvasPressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            point = self.toMapCoordinates(event.pos())
            self.clicked.emit(point, False)
        elif event.button() == Qt.MouseButton.RightButton and self._session_active:
            point = self.toMapCoordinates(event.pos())
            self.clicked.emit(point, True)
        elif not self._session_active:
            super().canvasPressEvent(event)

    def canvasReleaseEvent(self, event):
        # Suppress right-click context menu during an active session
        if event.button() == Qt.MouseButton.RightButton and self._session_active:
            return
        if not self._session_active:
            super().canvasReleaseEvent(event)

    def canvasMoveEvent(self, event):
        if not self._session_active:
            super().canvasMoveEvent(event)

    def wheelEvent(self, event):
        if not self._session_active:
            super().wheelEvent(event)

    def keyPressEvent(self, event):
        if self._session_active:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                event.accept()
                self.accept_requested.emit()
                return
            if event.key() == Qt.Key.Key_Escape:
                event.accept()
                self.cancel_requested.emit()
                return
            key_z = event.key() == Qt.Key.Key_Z
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if key_z and ctrl:
                event.accept()
                self.undo_requested.emit()
                return
        if not self._session_active:
            super().keyPressEvent(event)
