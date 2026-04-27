from qgis.core import Qgis, QgsGeometry, QgsPointXY
from qgis.gui import QgsMapCanvas, QgsRubberBand
from qgis.PyQt.QtGui import QColor


class PreviewOverlay:
    """Manages a single non-persistent polygon overlay on the map canvas."""

    FILL_COLOR = QColor(0, 200, 100, 60)    # semi-transparent green
    BORDER_COLOR = QColor(220, 50, 50, 220)  # red
    BORDER_WIDTH = 2

    def __init__(self, canvas: QgsMapCanvas):
        self._canvas = canvas
        self._band = None

    def show(self, polygon_geo: list):
        """Display polygon_geo (list of QgsPointXY) as a preview overlay.

        Replaces any existing overlay.
        """
        self.clear()
        self._band = QgsRubberBand(self._canvas, Qgis.GeometryType.Polygon)
        self._band.setColor(self.FILL_COLOR)
        self._band.setStrokeColor(self.BORDER_COLOR)
        self._band.setWidth(self.BORDER_WIDTH)
        self._band.setToGeometry(
            QgsGeometry.fromPolygonXY([polygon_geo]),
            None
        )

    def clear(self):
        """Remove the overlay from the canvas."""
        if self._band is not None:
            self._band.reset(Qgis.GeometryType.Polygon)
            self._canvas.scene().removeItem(self._band)
            self._band = None
