"""Cross-platform Python interpreter detection for the AITracer backend setup.

Single source of truth. Never relies solely on PATH (QGIS strips it).
Preference order:
  1. Python bundled with QGIS  (same dir as sys.executable, e.g. python3.12)
  2. Common well-known paths   (Homebrew on macOS, /usr/bin on Linux, py launcher on Windows)
  3. PATH-based search         (version-specific then generic, as fallback)
"""

import subprocess
import sys
from pathlib import Path

MIN_VERSION = (3, 10)


def find_python() -> str:
    """Return the path to a Python >= 3.10 interpreter.

    Raises RuntimeError with a user-friendly message if none is found.
    """
    for candidate in _candidates():
        version = _check_version(candidate)
        if version and version >= MIN_VERSION:
            return str(candidate)

    raise RuntimeError(
        f"No Python ≥ {MIN_VERSION[0]}.{MIN_VERSION[1]} found.\n"
        "Install Python 3.10 or later:\n"
        "  macOS:   brew install python@3.13\n"
        "  Linux:   sudo apt install python3.12\n"
        "  Windows: https://www.python.org/downloads/"
    )


# ------------------------------------------------------------------ #
# Candidate generation                                                #
# ------------------------------------------------------------------ #

def _candidates():
    """Yield candidate Paths, deduplicated, in preference order."""
    seen: set[str] = set()

    def emit(p):
        p = Path(p)
        try:
            resolved = str(p.resolve())
        except Exception:
            return
        if resolved not in seen and p.exists():
            seen.add(resolved)
            yield p

    # 1. Python bundled alongside the QGIS executable
    #    sys.executable = …/QGIS.app/Contents/MacOS/QGIS
    #    Python lives in the same directory as python3.12, python3, etc.
    qgis_bin_dir = Path(sys.executable).parent
    for name in _python_names():
        yield from emit(qgis_bin_dir / name)

    # 2. sys.prefix/bin  (works on Linux AppImages and some Windows installs)
    prefix_bin = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")
    for name in _python_names():
        yield from emit(prefix_bin / name)

    # 3. Well-known install locations that QGIS's PATH won't include

    if sys.platform == "darwin":
        # Homebrew on Apple Silicon and Intel
        for base in ("/opt/homebrew/bin", "/usr/local/bin"):
            for name in _python_names():
                yield from emit(Path(base) / name)
        # MacPorts
        for name in _python_names():
            yield from emit(Path("/opt/local/bin") / name)

    elif sys.platform.startswith("linux"):
        for name in _python_names():
            yield from emit(Path("/usr/bin") / name)
            yield from emit(Path("/usr/local/bin") / name)

    elif sys.platform == "win32":
        # Python Launcher (py.exe) and common install prefixes
        import shutil
        py = shutil.which("py")
        if py:
            yield from emit(Path(py))
        for base in (
            Path(r"C:\Python313"), Path(r"C:\Python312"), Path(r"C:\Python311"), Path(r"C:\Python310"),
            Path.home() / "AppData" / "Local" / "Programs" / "Python",
        ):
            for name in _python_names():
                yield from emit(base / name)

    # 4. PATH-based fallback (may not work inside QGIS but try anyway)
    import shutil
    for name in _python_names():
        found = shutil.which(name)
        if found:
            yield from emit(Path(found))


def _python_names():
    """Version-specific then generic names, most specific first."""
    if sys.platform == "win32":
        return [
            "python3.13.exe", "python3.12.exe", "python3.11.exe", "python3.10.exe",
            "python3.exe", "python.exe",
        ]
    return ["python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"]


# ------------------------------------------------------------------ #
# Version check                                                       #
# ------------------------------------------------------------------ #

def _check_version(path: Path):
    """Return (major, minor) for the given interpreter, or None on failure.

    Also returns None for embedded interpreters that cannot create venvs
    (e.g. Python bundled inside a macOS .app bundle).
    """
    # Skip embedded app-bundle Pythons on macOS — they lack ensurepip
    if sys.platform == "darwin" and ".app/" in str(path):
        return None
    try:
        result = subprocess.run(
            [str(path), "-c",
             "import sys; v=sys.version_info; print(v.major, v.minor)"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            major, minor = map(int, result.stdout.strip().split())
            return (major, minor)
    except Exception:
        pass
    return None
