"""Asynchronous segmentation worker for AITracer.

Wraps the blocking backend_client.segment() call in a QObject so it can
be moved to a QThread without freezing the QGIS main thread.

Usage pattern (in main.py):
    worker = SegmentationWorker(...)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(self._on_segment_done)
    worker.failed.connect(self._on_segment_failed)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
"""
from qgis.PyQt.QtCore import QObject, pyqtSignal

from . import backend_client
from .backend_client import BackendError


class SegmentationWorker(QObject):
    """Runs backend_client.segment() on a background thread.

    Signals:
        finished(dict): emitted with the response dict on success.
        failed(str):    emitted with an error message on failure.
    """

    finished = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self,
                 image_b64: str | None,
                 positive_points: list,
                 negative_points: list,
                 session_id: str | None):
        super().__init__()
        self._image_b64 = image_b64
        self._positive_points = positive_points
        self._negative_points = negative_points
        self._session_id = session_id

    def run(self) -> None:
        """Called by the QThread's started signal — runs on the worker thread."""
        try:
            result = backend_client.segment(
                image_b64=self._image_b64,
                positive_points=self._positive_points,
                negative_points=self._negative_points,
                session_id=self._session_id,
            )
            self.finished.emit(result)
        except BackendError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f"Unexpected error: {exc}")
