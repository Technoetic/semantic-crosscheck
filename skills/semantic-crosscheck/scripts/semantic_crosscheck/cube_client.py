"""Minimal Cube REST client using only the standard library.

- HS256 JWT signed with the per-run API secret (kept in memory, passed to the Cube adapter by environment).
- /cubejs-api/v1/load is polled while Cube answers {"error": "Continue wait"}.
- Never sends renewQuery (Cube 1.8 rejects it) and always sets an explicit limit (the default is 10,000 rows).
- Accepts a /load answer only in Cube's load envelope: a JSON object whose "data" is a list of JSON objects (the
  rows). Any other 2xx answer without an "error" member (not an object, no "data", "data" of another type, a row that
  is not an object) raises CubeResponseError instead of becoming an empty answer: zero rows read from a malformed
  response would pass V1 and V2 vacuously against empty gold rows, pass a V3 pair test of at most PAIR_KEY_CAP keys
  over no keys whatever the source data holds, and count as a V4 kill against non-empty gold rows. A 2xx body that is
  not JSON raises it too, so every malformed answer gets one category; meta() keeps the decode error, which the cube
  stage reports by type. Duplicate keys and nonfinite JSON constants are rejected; fractional numbers keep exact
  decimal precision before comparison.
- Talks to the loopback adapter directly. urllib's default opener applies HTTP_PROXY and the system proxy settings,
  and does not bypass 127.0.0.1 unless NO_PROXY lists it, so the JWT and the query values (bound filter values from
  the database) would go to the proxy; every request therefore uses an opener without proxies.
"""

import base64
from decimal import DecimalException
import hashlib
import hmac
import http.client
import json
import math
from numbers import Number
import time
import urllib.error
import urllib.request

from .constraints import decode_json

DEFAULT_LIMIT = 50000
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _timeout(value) -> float:
    if isinstance(value, bool) or not isinstance(value, Number):
        raise ValueError("timeout_s must be finite and positive")
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("timeout_s must be finite and positive") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("timeout_s must be finite and positive")
    return seconds


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt(secret: str, ttl: int = 3600) -> str:
    now = int(time.time())
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"iat": now, "exp": now + ttl}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


class CubeError(Exception):
    pass


class CubeResponseError(CubeError):
    """A /load answer outside Cube's load envelope; compare.error_detail reports it as cube = invalid_response."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Default redirects drain fp.read() without a byte limit before following
        # Location. Let HTTPError reach our bounded reader instead. The loopback
        # Cube REST adapter does not require redirects.
        return None


def _deadline(timeout_s) -> float:
    return time.monotonic() + _timeout(timeout_s)


def _remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise CubeError("timed out waiting for Cube")
    return seconds


class CubeClient:
    def __init__(self, base_url: str, secret: str, timeout_s: float = 120.0,
                 max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES):
        self.timeout_s = _timeout(timeout_s)
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self.max_response_bytes = max_response_bytes
        self.base = base_url.rstrip("/")
        self.secret = secret
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _read_response(self, response, deadline: float) -> bytes:
        """Bound entity bytes, checking elapsed time whenever public read1 returns.

        This is not hard cancellation: headers, DNS and chunk framing may keep
        control inside urllib longer than the budget. No private sockets/workers.
        """
        headers = getattr(response, "headers", {})
        expected = None
        if headers.get("Transfer-Encoding", "").lower() != "chunked":
            try:
                expected = int(headers.get("Content-Length", ""))
            except ValueError:
                pass
            if expected is not None and expected < 0:
                expected = None
        raw = bytearray()
        while True:
            _remaining(deadline)
            try:
                chunk = response.read1(min(64 * 1024, self.max_response_bytes + 1 - len(raw)))
            except TimeoutError:
                raise CubeError("timed out waiting for Cube") from None
            except http.client.HTTPException:
                raise CubeResponseError("the response body has invalid HTTP framing") from None
            _remaining(deadline)
            raw.extend(chunk)
            if len(raw) > self.max_response_bytes:
                raise CubeResponseError("Cube response exceeds byte limit")
            if not chunk:
                # read1 can return EOF without IncompleteRead, unlike read().
                if expected is not None and len(raw) != expected:
                    raise CubeResponseError("the response body is incomplete")
                return bytes(raw)

    def _req(self, path: str, payload: dict | None = None, *, deadline: float | None = None):
        if deadline is None:
            deadline = _deadline(self.timeout_s)
        _remaining(deadline)
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json", "Authorization": jwt(self.secret)})
        try:
            with self._opener.open(req, timeout=min(30.0, _remaining(deadline))) as r:
                raw = self._read_response(r, deadline)
        except urllib.error.HTTPError as e:
            with e:
                raw = self._read_response(e, deadline)
            body = raw.decode("utf-8", "replace")[:500]
            _remaining(deadline)
            raise CubeError(f"HTTP {e.code}: {body}") from e
        except TimeoutError:
            raise CubeError("timed out waiting for Cube") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise CubeError("timed out waiting for Cube") from None
            raise
        _remaining(deadline)
        try:
            result = decode_json(raw)
        except (ValueError, DecimalException):
            if path == "/cubejs-api/v1/load":
                raise CubeResponseError("the load response is not valid JSON") from None
            raise
        _remaining(deadline)
        return result

    def ready(self) -> bool:
        try:
            with self._opener.open(self.base + "/readyz", timeout=5) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def meta(self) -> dict:
        return self._req("/cubejs-api/v1/meta")

    def load(self, query: dict) -> list[dict]:
        q = dict(query)
        q.pop("renewQuery", None)
        q.setdefault("limit", DEFAULT_LIMIT)
        deadline = _deadline(self.timeout_s)
        while True:
            _remaining(deadline)
            try:
                r = self._req("/cubejs-api/v1/load", {"query": q}, deadline=deadline)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:   # an HTTP 200 whose body is not JSON
                raise CubeResponseError("the load response is not JSON") from e
            _remaining(deadline)
            if not isinstance(r, dict):
                raise CubeResponseError("the load response is not a JSON object")
            if r.get("error") == "Continue wait":
                time.sleep(min(0.25, _remaining(deadline)))
                continue
            if "error" in r:
                raise CubeError(str(r["error"])[:500])
            data = r.get("data")
            if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
                raise CubeResponseError("the load response has no data list of row objects")
            if len(data) >= q["limit"]:
                raise CubeError(f"result reached the limit ({q['limit']}); raise the limit or page the query")
            _remaining(deadline)
            return data
