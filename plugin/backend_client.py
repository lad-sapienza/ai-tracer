import urllib.request
import urllib.error
import json


BASE_URL = "http://localhost:8765"
TIMEOUT = 10.0


class BackendError(Exception):
    pass


def health_check() -> bool:
    """Return True if the backend is reachable and ready."""
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def segment(image_b64: str | None,
            positive_points: list,
            negative_points: list,
            session_id: str | None = None) -> dict:
    """Call POST /segment and return the response dict.

    Raises BackendError on network failure, timeout, or server error.
    """
    payload = {
        "positive_points": positive_points,
        "negative_points": negative_points,
    }
    if image_b64 is not None:
        payload["image"] = image_b64
    if session_id is not None:
        payload["session_id"] = session_id

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/segment",
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


def clear_session(session_id: str):
    """Notify the backend to evict a cached session."""
    payload = json.dumps({"session_id": session_id}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/clear",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # best-effort, never raise
