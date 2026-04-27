import base64
from io import BytesIO

from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtGui import QImage


def capture(canvas: QgsMapCanvas) -> bytes:
    """Capture the current map canvas as raw PNG bytes."""
    pixmap = canvas.grab()
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
    buf = BytesIO()
    # Save via QImage to get proper PNG bytes
    from qgis.PyQt.QtCore import QBuffer, QIODevice
    qbuf = QBuffer()
    qbuf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(qbuf, "PNG")
    qbuf.close()
    return bytes(qbuf.data())


def capture_base64(canvas: QgsMapCanvas) -> str:
    """Capture the canvas and return a base64-encoded PNG string."""
    return base64.b64encode(capture(canvas)).decode("utf-8")
