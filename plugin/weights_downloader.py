"""Streaming file download through QGIS's network stack.

Model checkpoints are large (up to ~900 MB). QgsFileDownloader writes them
to disk as data arrives instead of holding the whole file in memory, honours
the system certificate store and QGIS proxy settings, and reports progress.
"""
from qgis.core import QgsFileDownloader
from qgis.PyQt.QtCore import QEventLoop, QUrl


def download_file(url: str, dest: str, dlg=None, label: str = "") -> tuple:
    """Download *url* to *dest*.

    A local event loop keeps the GUI responsive while waiting. *dlg* is an
    optional QProgressDialog: its range and value show progress and its
    cancel button aborts the download. Returns (ok, message); on failure
    *dest* may hold a partial file and the caller should remove it.
    """
    loop = QEventLoop()
    state = {"done": False, "canceled": False, "error": None, "exited": False}

    def _on_exit():
        state["exited"] = True
        loop.quit()

    def _on_progress(received: int, total: int):
        if dlg is None:
            return
        if total > 0:
            dlg.setMaximum(total)
            dlg.setValue(min(received, total))
            dlg.setLabelText(
                f"{label}\n{received / 1e6:.0f} / {total / 1e6:.0f} MB"
            )

    # delayStart: connect every signal before the transfer begins.
    downloader = QgsFileDownloader(QUrl(url), dest, "", True)
    downloader.downloadCompleted.connect(lambda _url: state.update(done=True))
    downloader.downloadCanceled.connect(lambda: state.update(canceled=True))
    downloader.downloadError.connect(
        lambda errors: state.update(error="; ".join(str(e) for e in errors))
    )
    downloader.downloadProgress.connect(_on_progress)
    downloader.downloadExited.connect(_on_exit)
    if dlg is not None:
        dlg.canceled.connect(downloader.cancelDownload)

    downloader.startDownload()
    if not state["exited"]:
        loop.exec()

    if state["canceled"]:
        return False, "Download canceled."
    if state["error"]:
        return False, state["error"]
    if not state["done"]:
        return False, "Download did not complete."
    return True, ""
