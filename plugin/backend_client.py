import urllib.request
import urllib.error
import json

# Port is set at runtime by main.py after finding a free port.
# Default matches the historical hard-coded value.
_port: int = 8765
TIMEOUT = 10.0


class BackendError(Exception):
    pass


def set_port(port: int) -> None:
    """Update the port used for all subsequent backend calls."""
    global _port
    _port = port


def _url(path: str) -> str:
    return f"http://localhost:{_port}{path}"


def health_check(expected_version: str | None = None) -> bool:
    """Return True if the backend is reachable, healthy, and (optionally)
    running the expected plugin version.

    Passing *expected_version* guards against a stale uvicorn process left
    over from a previous plugin version answering on the same port.
    """
    try:
        with urllib.request.urlopen(_url("/health"), timeout=3) as r:
            if r.status != 200:
                return False
            data = json.loads(r.read())
            if expected_version and data.get("version") != expected_version:
                return False
            return data.get("status") == "ok"
    except Exception:
        return False


def segment(image_b64: str | None,
            positive_points: list,
            negative_points: list,
            session_id: str | None = None) -> dict:
    """Call POST /segment and return the response dict.

    Raises BackendError on network failure, timeout, or server error.
    """
    payload: dict = {
        "positive_points": positive_points,
        "negative_points": negative_points,
    }
    if image_b64 is not None:
        payload["image"] = image_b64
    if session_id is not None:
        payload["session_id"] = session_id

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _url("/segment"),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise BackendError(f"HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        raise BackendError(f"Cannot reach backend: {e.reason}")
    except TimeoutError:
        raise BackendError("Request timed out.")


def clear_session(session_id: str) -> None:
    """Notify the backend to evict a cached session (best-effort)."""
    payload = json.dumps({"session_id": session_id}).encode()
    req = urllib.request.Request(
        _url("/clear"),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # best-effort, never raise
