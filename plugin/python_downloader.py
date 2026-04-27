"""Standalone Python installer for AITracer.

Downloads a self-contained Python interpreter from python-build-standalone
(https://github.com/astral-sh/python-build-standalone) so the plugin never
depends on the user having a particular system Python installed.

The downloaded interpreter lives at ~/.aitracer/python_standalone/python/
and is used only to create the plugin's venv — it is never imported into
the QGIS process directly.
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Callable

# ------------------------------------------------------------------ #
# Release manifest                                                    #
# ------------------------------------------------------------------ #

RELEASE_TAG = "20251014"

# (major, minor) → full patch version available in that release
_PYTHON_VERSIONS: dict[tuple[int, int], str] = {
    (3, 9):  "3.9.24",
    (3, 10): "3.10.19",
    (3, 11): "3.11.14",
    (3, 12): "3.12.12",
    (3, 13): "3.13.9",
}

STANDALONE_DIR = os.path.join(os.path.expanduser("~"), ".aitracer", "python_standalone")


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

def python_executable() -> str:
    """Absolute path to the standalone Python executable."""
    base = os.path.join(STANDALONE_DIR, "python")
    if sys.platform == "win32":
        return os.path.join(base, "python.exe")
    return os.path.join(base, "bin", "python3")


def is_installed() -> bool:
    """Return True if the standalone Python executable exists."""
    return os.path.exists(python_executable())


def install(
    progress_cb: Callable[[int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """Download and install standalone Python.

    Args:
        progress_cb:  called as ``progress_cb(percent, message)`` during setup.
        cancel_check: called with no arguments; return True to abort.

    Returns:
        ``(True, message)`` on success, ``(False, error_message)`` on failure.
    """
    if is_installed():
        return True, "Already installed"

    version = _full_version()
    url = _download_url()

    _cb(progress_cb, 0, f"Downloading Python {version} (~30 MB)…")

    fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
    os.close(fd)

    try:
        if cancel_check and cancel_check():
            return False, "Cancelled"

        # Use QgsBlockingNetworkRequest so QGIS proxy settings are respected.
        from qgis.core import QgsBlockingNetworkRequest
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtNetwork import QNetworkRequest

        request = QgsBlockingNetworkRequest()
        err = request.get(QNetworkRequest(QUrl(url)))

        if err != QgsBlockingNetworkRequest.NoError:
            return False, f"Download failed: {request.errorMessage()}\nURL: {url}"

        content = request.reply().content()
        size = len(content)

        if size < 10 * 1024 * 1024:
            return False, (
                f"Download incomplete ({size // 1024} KB — expected >10 MB). "
                "Check your internet connection or proxy settings."
            )

        # Sanity: first two bytes must be gzip magic 0x1f 0x8b
        raw = bytes(content.data())
        if raw[:2] != b"\x1f\x8b":
            preview = raw[:150].decode("utf-8", errors="replace")
            return False, (
                "Download failed: server returned an unexpected response "
                f"(not a gzip archive). Preview: {preview}"
            )

        _cb(progress_cb, 50, f"Downloaded {size // (1024 * 1024)} MB, extracting…")

        with open(tmp_path, "wb") as f:
            f.write(raw)

        if cancel_check and cancel_check():
            return False, "Cancelled"

        # Remove any previous (broken) installation.
        if os.path.exists(STANDALONE_DIR):
            shutil.rmtree(STANDALONE_DIR)
        os.makedirs(STANDALONE_DIR, exist_ok=True)

        _cb(progress_cb, 55, "Extracting Python…")
        with tarfile.open(tmp_path, "r:gz") as tar:
            _safe_extract(tar, STANDALONE_DIR)

        # On Unix, fix executable permissions and create python3 symlink.
        if sys.platform != "win32":
            _fix_unix_permissions()

        _cb(progress_cb, 80, "Verifying Python installation…")
        ok, msg = verify()
        if ok:
            _cb(progress_cb, 100, f"Python {version} ready ✓")
            return True, msg

        # Clean up so a re-try starts fresh.
        shutil.rmtree(STANDALONE_DIR, ignore_errors=True)
        return False, f"Verification failed: {msg}"

    except Exception as exc:
        errmsg = f"Installation failed: {exc}"
        if sys.platform == "win32":
            low = str(exc).lower()
            if any(k in low for k in ("denied", "access", "permission")):
                errmsg += (
                    "\n\nYour antivirus may be blocking the extraction. "
                    "Try temporarily disabling it and retrying."
                )
        return False, errmsg

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def verify() -> tuple[bool, str]:
    """Run the standalone Python and confirm it executes correctly."""
    exe = python_executable()
    if not os.path.exists(exe):
        return False, f"Executable not found: {exe}"

    env = _clean_env()
    kwargs: dict = {
        "capture_output": True, "text": True,
        "timeout": 30, "env": env,
    }
    if sys.platform == "win32":
        kwargs["startupinfo"] = _win_startupinfo()

    try:
        result = subprocess.run(
            [exe, "-c", "import sys; print(sys.version_info.major, sys.version_info.minor)"],
            **kwargs,
        )
        if result.returncode == 0:
            major, minor = map(int, result.stdout.strip().split())
            return True, f"Python {major}.{minor}"
        return False, (result.stderr or "non-zero exit")[:200]
    except Exception as exc:
        return False, str(exc)


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _qgis_python_version() -> tuple[int, int]:
    return (sys.version_info.major, sys.version_info.minor)


def _full_version() -> str:
    ver = _qgis_python_version()
    if ver in _PYTHON_VERSIONS:
        return _PYTHON_VERSIONS[ver]
    # Fallback: use the newest entry we know about.
    return _PYTHON_VERSIONS[max(_PYTHON_VERSIONS)]


def _platform_tag() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "aarch64-apple-darwin" if machine in ("arm64", "aarch64") else "x86_64-apple-darwin"
    if sys.platform == "win32":
        return "x86_64-pc-windows-msvc"
    # Linux
    return "aarch64-unknown-linux-gnu" if machine in ("arm64", "aarch64") else "x86_64-unknown-linux-gnu"


def _download_url() -> str:
    filename = f"cpython-{_full_version()}+{RELEASE_TAG}-{_platform_tag()}-install_only.tar.gz"
    return f"https://github.com/astral-sh/python-build-standalone/releases/download/{RELEASE_TAG}/{filename}"


def _safe_extract(tar: tarfile.TarFile, dest: str) -> None:
    """Extract tar, skipping any members whose paths escape *dest*."""
    abs_dest = os.path.realpath(dest) + os.sep
    for member in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, member.name))
        if not target.startswith(abs_dest):
            continue  # path traversal attempt — skip
        tar.extract(member, dest)


def _fix_unix_permissions() -> None:
    """Make binaries executable and create the python3 symlink if absent."""
    py_bin = os.path.join(STANDALONE_DIR, "python", "bin")
    if not os.path.isdir(py_bin):
        return
    executable_bits = (
        stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )
    for name in os.listdir(py_bin):
        full = os.path.join(py_bin, name)
        if os.path.isfile(full) and not os.path.islink(full):
            try:
                os.chmod(full, executable_bits)
            except OSError:
                pass

    py3 = os.path.join(py_bin, "python3")
    if not os.path.exists(py3):
        major, minor = _qgis_python_version()
        versioned = os.path.join(py_bin, f"python{major}.{minor}")
        if os.path.exists(versioned):
            os.symlink(f"python{major}.{minor}", py3)


def _clean_env() -> dict:
    """Return os.environ without PYTHONHOME/PYTHONPATH (avoids QGIS env leaking in)."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _win_startupinfo():
    """STARTUPINFO that hides the console window on Windows."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _cb(fn, pct: int, msg: str) -> None:
    if fn:
        fn(pct, msg)
