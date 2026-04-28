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


def _safe_urlopen(url_or_req, timeout: float):
    """Wrapper around urlopen that asserts the target is always localhost HTTP.

    This satisfies static-analysis tools (e.g. Bandit B310) that flag
    urllib.request.urlopen for potentially accepting file:/ or custom schemes.
    The backend is a local subprocess — only http://localhost is ever valid.
    """
    raw_url = url_or_req if isinstance(url_or_req, str) else url_or_req.full_url
    if not raw_url.startswith("http://localhost:"):
        raise ValueError(f"Refusing non-localhost URL: {raw_url}")
    return urllib.request.urlopen(url_or_req, timeout=timeout)  # noqa: S310


def health_check(expected_version: str | None = None) -> bool:
    """Return True if the backend is reachable, healthy, and (optionally)
    running the expected plugin version.

    Passing *expected_version* guards against a stale uvicorn process left
    over from a previous plugin version answering on the same port.
    """
    try:
        with _safe_urlopen(_url("/health"), timeout=3) as r:
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
        with _safe_urlopen(req, timeout=TIMEOUT) as r:
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
        _safe_urlopen(req, timeout=3)
    except Exception:
        pass  # best-effort, never raise
