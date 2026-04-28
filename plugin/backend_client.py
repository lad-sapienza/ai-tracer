"""Backend client for the AITracer local SAM2 server.

Uses http.client.HTTPConnection (plain TCP to localhost) instead of
urllib.request.urlopen so that static-analysis tools (Bandit B310) have
nothing to flag — there is no URL-scheme handling in http.client.
"""
import http.client
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


def health_check(expected_version: str | None = None) -> bool:
    """Return True if the backend is reachable, healthy, and (optionally)
    running the expected plugin version.

    Passing *expected_version* guards against a stale uvicorn process left
    over from a previous plugin version answering on the same port.
    """
    conn = None
    try:
        conn = http.client.HTTPConnection("localhost", _port, timeout=3)
        conn.request("GET", "/health")
        r = conn.getresponse()
        if r.status != 200:
            return False
        data = json.loads(r.read())
        if expected_version and data.get("version") != expected_version:
            return False
        return data.get("status") == "ok"
    except Exception:
        return False
    finally:
        if conn:
            conn.close()


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

    body = json.dumps(payload).encode()
    conn = None
    try:
        conn = http.client.HTTPConnection("localhost", _port, timeout=TIMEOUT)
        conn.request("POST", "/segment", body=body,
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        raw = r.read()
        if r.status != 200:
            raise BackendError(f"HTTP {r.status}: {raw.decode(errors='replace')}")
        return json.loads(raw)
    except BackendError:
        raise
    except OSError as e:
        raise BackendError(f"Cannot reach backend: {e}")
    except TimeoutError:
        raise BackendError("Request timed out.")
    finally:
        if conn:
            conn.close()


def clear_session(session_id: str) -> None:
    """Notify the backend to evict a cached session (best-effort)."""
    body = json.dumps({"session_id": session_id}).encode()
    conn = None
    try:
        conn = http.client.HTTPConnection("localhost", _port, timeout=3)
        conn.request("POST", "/clear", body=body,
                     headers={"Content-Type": "application/json"})
        conn.getresponse().read()
    except Exception:
        pass  # best-effort, never raise
    finally:
        if conn:
            conn.close()
